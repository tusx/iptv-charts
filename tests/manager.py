#!/usr/bin/env python3
"""End-to-end tests for the Movies/Series manager.

The provider catalog, TMDB, TVDB and the media files are all served by one
local fixture server. Nothing here touches the internet, a real player, or the
user's media folders.

Run with:  .venv/bin/python tests/manager.py
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import integration  # noqa: E402  (sets its own temp paths; we override below)
from iptv_charts import db, downloader, metadata, scheduler, sources, sync  # noqa: E402
from iptv_charts.clients import tvdb as tvdb_client  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="iptv-manager-test-"))
_FAKE_BIN = _TMP / "bin"
_FAKE_BIN.mkdir()
_MEDIA = _TMP / "media"
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_MEDIA)
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"
os.environ["PATH"] = f"{_FAKE_BIN}:{os.environ.get('PATH', '')}"

# A fake ffprobe: test media carries a RES: marker; the real binary would
# reject these tiny fixture files. The last argument is the path.
(_FAKE_BIN / "ffprobe").write_text(
    "#!/bin/sh\n"
    "last=''\n"
    "for arg in \"$@\"; do last=\"$arg\"; done\n"
    "case \"$last\" in\n"
    "  http*)\n"
    "    id=$(printf '%s' \"$last\" | sed -n 's#.*/\\([0-9][0-9]*\\)\\.[A-Za-z0-9]*$#\\1#p')\n"
    "    width=1920; height=1080\n"
    "    audio='{\"index\":1,\"codec_type\":\"audio\",\"codec_name\":\"aac\",\"channels\":2,\"tags\":{\"language\":\"eng\"}}'\n"
    "    subs=''\n"
    "    case \"$id\" in\n"
    "      60002|60003|60020|60021) width=3840; height=2160 ;;\n"
    "    esac\n"
    "    case \"$id\" in\n"
    "      60003) audio='{\"index\":1,\"codec_type\":\"audio\",\"codec_name\":\"aac\",\"channels\":2,\"tags\":{\"language\":\"spa\"}}';\n"
    "             subs=',{\"index\":2,\"codec_type\":\"subtitle\",\"codec_name\":\"subrip\",\"tags\":{\"language\":\"eng\"}}' ;;\n"
    "      60020) audio='{\"index\":1,\"codec_type\":\"audio\",\"codec_name\":\"aac\",\"channels\":2,\"tags\":{\"language\":\"spa\"}}' ;;\n"
    "      60021) subs=',{\"index\":2,\"codec_type\":\"subtitle\",\"codec_name\":\"subrip\",\"tags\":{\"language\":\"eng\"}}' ;;\n"
    "    esac\n"
    "    printf '{\"streams\":[{\"index\":0,\"codec_type\":\"video\",\"codec_name\":\"h264\",\"width\":%s,\"height\":%s},%s%s],\"format\":{\"size\":8192,\"duration\":\"7200\",\"format_name\":\"mp4\"}}' \"$width\" \"$height\" \"$audio\" \"$subs\"\n"
    "    ;;\n"
    "  *)\n"
    "    width=1920; height=1080\n"
    "    case \"$(head -c 64 \"$last\" 2>/dev/null)\" in\n"
    "      *RES:2160*) width=3840; height=2160 ;;\n"
    "      *RES:720*) width=1280; height=720 ;;\n"
    "      *RES:480*) width=854; height=480 ;;\n"
    "    esac\n"
    "    printf '{\"streams\":[{\"width\":%s,\"height\":%s}]}' \"$width\" \"$height\"\n"
    "    ;;\n"
    "esac\n"
)
(_FAKE_BIN / "ffprobe").chmod(0o755)

# A fake ffmpeg for the HLS path: it writes a marked file to its output
# argument and reports progress, without needing a real media stack.
(_FAKE_BIN / "ffmpeg").write_text(
    "#!/bin/sh\n"
    "last=''\n"
    "for arg in \"$@\"; do last=\"$arg\"; done\n"
    "printf 'RES:1080\\n' > \"$last\"\n"
    "head -c 4096 /dev/zero | tr '\\0' x >> \"$last\"\n"
    "printf 'progress=end\\n'\n"
)
(_FAKE_BIN / "ffmpeg").chmod(0o755)

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


# --------------------------------------------------------------------- fixtures
SPECIAL_VOD = [
    {"num": 1, "name": "The Matrix (1999) [1080p]", "stream_type": "movie",
     "stream_id": 60001, "stream_icon": "http://localhost/m60001.jpg",
     "rating": "8.7", "added": "1700100000", "category_id": "2",
     "container_extension": "mp4"},
    {"num": 2, "name": "The Matrix (1999) [2160p]", "stream_type": "movie",
     "stream_id": 60002, "stream_icon": "http://localhost/m60002.jpg",
     "rating": "8.7", "added": "1700100000", "category_id": "2",
     "container_extension": "mkv"},
    {"num": 3, "name": "ES - Un talento único [[4K-Q]-Q] (2026)",
     "stream_type": "movie", "stream_id": 60003,
     "stream_icon": "http://localhost/m60003.jpg", "rating": "7.0",
     "added": "1700100000", "category_id": "2", "container_extension": "mkv"},
    {"num": 4, "name": "Slow Movie (2024) [1080p]", "stream_type": "movie",
     "stream_id": 60009, "stream_icon": "http://localhost/m60009.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "mp4"},
    {"num": 5, "name": "Dead Movie (2024) [1080p]", "stream_type": "movie",
     "stream_id": 60010, "stream_icon": "http://localhost/m60010.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "mp4"},
    {"num": 6, "name": "HTML Movie (2024) [1080p]", "stream_type": "movie",
     "stream_id": 60011, "stream_icon": "http://localhost/m60011.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "mp4"},
    {"num": 7, "name": "Manual Movie (2024) [720p]", "stream_type": "movie",
     "stream_id": 60013, "stream_icon": "http://localhost/m60013.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "mp4"},
    {"num": 8, "name": "HLS Movie (2024) [1080p]", "stream_type": "movie",
     "stream_id": 60014, "stream_icon": "http://localhost/m60014.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "m3u8"},
    {"num": 9, "name": "Probe Movie (2024) [2160p] CAM", "stream_type": "movie",
     "stream_id": 60020, "stream_icon": "http://localhost/m60020.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "mkv"},
    {"num": 10, "name": "Probe Movie (2024) [2160p] WEB-DL", "stream_type": "movie",
     "stream_id": 60021, "stream_icon": "http://localhost/m60021.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "mkv"},
    {"num": 11, "name": "Probe Movie (2024) [1080p] WEB-DL", "stream_type": "movie",
     "stream_id": 60022, "stream_icon": "http://localhost/m60022.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "mp4"},
    {"num": 12, "name": "Future Movie (2099) [1080p]", "stream_type": "movie",
     "stream_id": 60023, "stream_icon": "http://localhost/m60023.jpg",
     "rating": "6.0", "added": "1700100000", "category_id": "2",
     "container_extension": "mp4"},
]

SPECIAL_SERIES = [
    {"num": 1, "name": "Dark Hearts (2023) [1080p]", "series_id": 70001,
     "cover": "http://localhost/s70001.jpg", "plot": "A plot",
     "category_id": "3", "last_modified": "1700200000"},
    {"num": 2, "name": "GR - Supernatural [1080p]", "series_id": 70002,
     "cover": "http://localhost/s70002.jpg", "plot": "A plot",
     "category_id": "3", "last_modified": "1700200000"},
    {"num": 3, "name": "Throttle Show (2024) [1080p]", "series_id": 70003,
     "cover": "http://localhost/s70003.jpg", "plot": "A plot",
     "category_id": "3", "last_modified": "1700200000"},
    {"num": 4, "name": "Monitor Show (2024) [1080p]", "series_id": 70004,
     "cover": "http://localhost/s70004.jpg", "plot": "A plot",
     "category_id": "3", "last_modified": "1700200000"},
]

SERIES_INFO = {
    "70001": {"episodes": {
        "1": [
            {"id": 80001, "episode_num": 1, "title": "Pilot",
             "container_extension": "mp4", "info": {"duration": "00:45:00"}},
            {"id": 80002, "episode_num": 2, "title": "Second",
             "container_extension": "mp4", "info": {"duration": "00:45:00"}},
        ],
        "2": [
            {"id": 80011, "episode_num": 1, "title": "Return",
             "container_extension": "mp4", "info": {"duration": "00:45:00"}},
        ],
    }},
    "70002": {"episodes": {
        "1": [
            {"id": 80101, "episode_num": 1, "title": "Pilot",
             "container_extension": "mp4", "info": {"duration": "00:40:00"}},
        ],
    }},
    "70003": {"episodes": {
        "1": [
            {"id": 80201, "episode_num": 1, "title": "Throttle One",
             "container_extension": "mp4", "info": {"duration": "00:40:00"}},
            {"id": 80202, "episode_num": 2, "title": "Throttle Two",
             "container_extension": "mp4", "info": {"duration": "00:40:00"}},
        ],
    }},
    "70004": {"episodes": {
        "1": [
            {"id": 80301, "episode_num": 1, "title": "Released One",
             "container_extension": "mp4", "info": {"duration": "00:40:00"}},
        ],
        "2": [
            {"id": 80302, "episode_num": 1, "title": "Future One",
             "container_extension": "mp4", "info": {"duration": "00:40:00"}},
        ],
    }},
}

TMDB_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.manager-test-token"

TMDB_MOVIES = {
    603: {"title": "The Matrix", "original_title": "The Matrix",
          "release_date": "1999-03-31", "overview": "A hacker learns the truth.",
          "poster_path": "/matrix.jpg", "backdrop_path": "/matrix-bg.jpg",
          "vote_average": 8.7, "runtime": 136,
          "genres": [{"name": "Action"}, {"name": "Science Fiction"}],
          "external_ids": {"imdb_id": "tt0133093"},
          "alternative_titles": {"titles": []}},
    604: {"title": "A Unique Talent", "original_title": "Un talento único",
          "release_date": "2026-01-15", "overview": "A localised title test.",
          "poster_path": "/talent.jpg", "backdrop_path": None,
          "vote_average": 7.0, "runtime": 101,
          "genres": [{"name": "Comedy"}],
          "external_ids": {"imdb_id": "tt0604"},
          "alternative_titles": {"titles": [
              {"iso_3166_1": "ES", "title": "Un talento único"},
              {"iso_3166_1": "US", "title": "A Unique Talent"},
          ]}},
    605: {"title": "Slow Movie", "original_title": "Slow Movie",
          "release_date": "2024-02-01", "overview": "Slow.",
          "poster_path": "/slow.jpg", "backdrop_path": None, "vote_average": 6.0,
          "runtime": 90, "genres": [{"name": "Drama"}],
          "external_ids": {"imdb_id": "tt0605"}, "alternative_titles": {"titles": []}},
    606: {"title": "Dead Movie", "original_title": "Dead Movie",
          "release_date": "2024-03-01", "overview": "Fails once.",
          "poster_path": "/dead.jpg", "backdrop_path": None, "vote_average": 6.0,
          "runtime": 90, "genres": [{"name": "Drama"}],
          "external_ids": {"imdb_id": "tt0606"}, "alternative_titles": {"titles": []}},
    607: {"title": "Scanned Movie", "original_title": "Scanned Movie",
          "release_date": "2024-04-01", "overview": "Already on disk.",
          "poster_path": "/scan.jpg", "backdrop_path": None, "vote_average": 6.0,
          "runtime": 90, "genres": [{"name": "Drama"}],
          "external_ids": {"imdb_id": "tt0607"}, "alternative_titles": {"titles": []}},
    608: {"title": "HTML Movie", "original_title": "HTML Movie",
          "release_date": "2024-05-01", "overview": "Serves a web page.",
          "poster_path": "/html.jpg", "backdrop_path": None, "vote_average": 6.0,
          "runtime": 90, "genres": [{"name": "Drama"}],
          "external_ids": {"imdb_id": "tt0608"}, "alternative_titles": {"titles": []}},
    609: {"title": "Manual Movie", "original_title": "Manual Movie",
          "release_date": "2024-06-01", "overview": "Picked by hand.",
          "poster_path": "/manual.jpg", "backdrop_path": None, "vote_average": 6.0,
          "runtime": 90, "genres": [{"name": "Drama"}],
          "external_ids": {"imdb_id": "tt0609"}, "alternative_titles": {"titles": []}},
    610: {"title": "HLS Movie", "original_title": "HLS Movie",
          "release_date": "2024-07-01", "overview": "Needs a remux.",
          "poster_path": "/hls.jpg", "backdrop_path": None, "vote_average": 6.0,
          "runtime": 90, "genres": [{"name": "Drama"}],
          "external_ids": {"imdb_id": "tt0610"}, "alternative_titles": {"titles": []}},
    611: {"title": "Probe Movie", "original_title": "Probe Movie",
          "release_date": "2024-08-01", "overview": "Choose the WEB-DL, not the CAM.",
          "poster_path": "/probe.jpg", "backdrop_path": None, "vote_average": 6.0,
          "runtime": 90, "genres": [{"name": "Drama"}],
          "external_ids": {"imdb_id": "tt0611"}, "alternative_titles": {"titles": []},
          "original_language": "en"},
    614: {"title": "Future Movie", "original_title": "Future Movie",
          "release_date": "2099-01-01", "overview": "Not released for a long time.",
          "poster_path": "/future.jpg", "backdrop_path": None, "vote_average": 6.0,
          "runtime": 90, "genres": [{"name": "Science Fiction"}],
          "external_ids": {"imdb_id": "tt0614"}, "alternative_titles": {"titles": []},
          "original_language": "en"},
}

TMDB_SERIES = {
    9001: {"name": "Dark Hearts", "original_name": "Dark Hearts",
           "first_air_date": "2023-01-01", "overview": "A dark story.",
           "poster_path": "/dark.jpg", "backdrop_path": "/dark-bg.jpg",
           "vote_average": 7.5, "status": "Ended",
           "genres": [{"name": "Drama"}],
           "external_ids": {"tvdb_id": 9101},
           "seasons": [{"season_number": 1, "episode_count": 2},
                       {"season_number": 2, "episode_count": 1}],
           "alternative_titles": {"results": []}},
    9002: {"name": "Supernatural", "original_name": "Supernatural",
           "first_air_date": "2005-09-13", "overview": "Brothers.",
           "poster_path": "/spn.jpg", "backdrop_path": None,
           "vote_average": 8.0, "status": "Ended",
           "genres": [{"name": "Drama"}],
           "external_ids": {"tvdb_id": 9102},
           "seasons": [{"season_number": 1, "episode_count": 1}],
           "alternative_titles": {"results": []}},
    9003: {"name": "Throttle Show", "original_name": "Throttle Show",
           "first_air_date": "2024-01-01", "overview": "Connection limit test.",
           "poster_path": "/throttle.jpg", "backdrop_path": None,
           "vote_average": 6.0, "status": "Continuing",
           "genres": [{"name": "Drama"}],
           "external_ids": {"tvdb_id": 9103},
           "seasons": [{"season_number": 1, "episode_count": 2}],
           "alternative_titles": {"results": []}},
    9004: {"name": "Monitor Show", "original_name": "Monitor Show",
           "first_air_date": "2024-01-01", "overview": "Monitoring modes.",
           "poster_path": "/monitor.jpg", "backdrop_path": None,
           "vote_average": 6.0, "status": "Continuing",
           "genres": [{"name": "Drama"}],
           "external_ids": {"tvdb_id": 9104},
           "seasons": [{"season_number": 1, "episode_count": 1},
                       {"season_number": 2, "episode_count": 1}],
           "alternative_titles": {"results": []}},
}

TVDB_EPISODES = {
    9101: [
        {"id": 91011, "seasonNumber": 1, "number": 1, "name": "The Beginning",
         "aired": "2023-01-01", "overview": "First.", "image": ""},
        {"id": 91012, "seasonNumber": 1, "number": 2, "name": "The Middle",
         "aired": "2023-01-08", "overview": "Second.", "image": ""},
        {"id": 91021, "seasonNumber": 2, "number": 1, "name": "The Return",
         "aired": "2024-01-01", "overview": "Third.", "image": ""},
    ],
    9102: [
        {"id": 92011, "seasonNumber": 1, "number": 1, "name": "Pilot",
         "aired": "2005-09-13", "overview": "First.", "image": ""},
    ],
    9103: [
        {"id": 93011, "seasonNumber": 1, "number": 1, "name": "Throttle One",
         "aired": "2024-01-01", "overview": "First.", "image": ""},
        {"id": 93012, "seasonNumber": 1, "number": 2, "name": "Throttle Two",
         "aired": "2024-01-08", "overview": "Second.", "image": ""},
    ],
    9104: [
        {"id": 94011, "seasonNumber": 1, "number": 1, "name": "Released One",
         "aired": "2024-01-01", "overview": "First.", "image": ""},
        {"id": 94021, "seasonNumber": 2, "number": 1, "name": "Future One",
         "aired": "2099-01-01", "overview": "Future.", "image": ""},
    ],
}

MEDIA_MARKERS = {
    60001: ("1080", "mp4"), 60002: ("2160", "mkv"), 60003: ("2160", "mkv"),
    60009: ("1080", "mp4"), 60010: ("1080", "mp4"), 60011: ("1080", "mp4"),
    80001: ("1080", "mp4"), 80002: ("1080", "mp4"), 80011: ("1080", "mp4"),
    80101: ("1080", "mp4"), 60013: ("720", "mp4"), 60014: ("1080", "mkv"),
    60020: ("2160", "mkv"), 60021: ("2160", "mkv"), 60022: ("1080", "mp4"),
    60023: ("1080", "mp4"), 80201: ("1080", "mp4"), 80202: ("1080", "mp4"),
    80301: ("1080", "mp4"), 80302: ("1080", "mp4"),
}


class ManagerHandler(integration.Handler):
    dead_attempts: dict[int, int] = {}
    #: Start/end times for the two "Throttle Show" episode requests. The
    #: connection-limit test asserts these intervals never overlap.
    throttle_intervals: list[tuple[float, float]] = []
    throttle_lock = threading.Lock()
    tmdb_auth: list[tuple[str | None, str | None]] = []
    tvdb_auth: list[str | None] = []
    tvdb_search_params: list[dict] = []

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/player_api.php":
            action = (params.get("action") or [""])[0]
            user = (params.get("username") or [""])[0]
            if user == "gooduser" and action == "get_vod_streams":
                body = integration.xtream_array("vod", 300) + SPECIAL_VOD
                self._send(json.dumps(body).encode(), "application/json", gzip_body=True)
                return
            if user == "gooduser" and action == "get_series":
                body = integration.xtream_array("series", 80) + SPECIAL_SERIES
                self._send(json.dumps(body).encode(), "application/json", gzip_body=True)
                return
            if user == "gooduser" and action == "get_series_info":
                series_id = (params.get("series_id") or [""])[0]
                body = SERIES_INFO.get(series_id)
                if body is not None:
                    self._send(json.dumps(body).encode(), "application/json")
                    return

        if parsed.path.startswith("/movie/") or parsed.path.startswith("/series/"):
            self._media(parsed.path)
            return
        if parsed.path.startswith("/tmdb/3/"):
            self._tmdb(parsed.path, params)
            return
        if parsed.path.startswith("/tvdb/v4/"):
            self._tvdb(parsed.path, params)
            return
        if parsed.path.startswith("/tmdb-image/"):
            self._send(b"FAKEIMAGE", "image/jpeg")
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        if parsed.path == "/tvdb/v4/login":
            payload = json.loads(body or b"{}")
            if not payload.get("apikey"):
                self.send_error(401)
                return
            self._send(json.dumps({"status": "success", "data": {"token": "fake-token"}}).encode(),
                       "application/json")
            return
        self.send_error(404)

    # ------------------------------------------------------------------ media
    def _media(self, path: str) -> None:
        match = re.match(r"^/(?:movie|series)/[^/]+/[^/]+/(\d+)\.([a-z0-9]+)$", path)
        if not match:
            self.send_error(404)
            return
        media_id = int(match.group(1))
        extension = match.group(2)
        if media_id in (80201, 80202):
            started = time.time()
            time.sleep(0.35)
            finished = time.time()
            with self.throttle_lock:
                self.throttle_intervals.append((started, finished))
        if media_id == 60011:
            self._send(b"<html><body>login please</body></html>", "text/html")
            return
        if media_id == 60010:
            attempt = self.dead_attempts.get(media_id, 0) + 1
            self.dead_attempts[media_id] = attempt
            if attempt == 1:
                self.send_error(500)
                return
        if media_id == 60014:
            self._send(
                b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                b"#EXTINF:9.9,\nseg1.ts\n",
                "application/vnd.apple.mpegurl",
            )
            return
        marker, _ = MEDIA_MARKERS.get(media_id, ("1080", extension))
        content = f"RES:{marker}\n".encode() + b"x" * 8192
        content_type = "video/x-matroska" if extension == "mkv" else "video/mp4"

        if media_id == 60009:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(64 * 1024))
            self.end_headers()
            try:
                for _ in range(20):
                    self.wfile.write(b"x" * 3276)
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        range_header = self.headers.get("Range", "")
        start = 0
        range_match = re.match(r"bytes=(\d+)-", range_header or "")
        if range_match:
            start = int(range_match.group(1))
        body = content[start:]
        status = 206 if range_match else 200
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(content) - 1}/{len(content)}")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------- tmdb
    def _tmdb(self, path: str, params: dict) -> None:
        self.tmdb_auth.append((
            self.headers.get("Authorization"),
            (params.get("api_key") or [None])[0],
        ))
        sub = path[len("/tmdb/3"):]
        query = (params.get("query") or [""])[0].lower()
        if sub == "/search/movie":
            matches = []
            for tmdb_id, details in TMDB_MOVIES.items():
                title = details["title"].lower()
                original = details["original_title"].lower()
                if query and (query in title or query in original
                              or any(token in f"{title} {original}" for token in query.split())):
                    matches.append(self._movie_summary(tmdb_id, details))
            self._json({"page": 1, "results": matches, "total_results": len(matches)})
            return
        if sub == "/search/tv":
            matches = []
            for tmdb_id, details in TMDB_SERIES.items():
                title = details["name"].lower()
                if query and (query in title or any(token in title for token in query.split())):
                    matches.append(self._series_summary(tmdb_id, details))
            self._json({"page": 1, "results": matches, "total_results": len(matches)})
            return
        movie_match = re.match(r"^/movie/(\d+)$", sub)
        if movie_match:
            details = TMDB_MOVIES.get(int(movie_match.group(1)))
            if details is None:
                self.send_error(404)
                return
            tmdb_id = int(movie_match.group(1))
            self._json({
                "id": tmdb_id, **details,
                "original_language": details.get(
                    "original_language", "es" if tmdb_id == 604 else "en"
                ),
            })
            return
        series_match = re.match(r"^/tv/(\d+)$", sub)
        if series_match:
            details = TMDB_SERIES.get(int(series_match.group(1)))
            if details is None:
                self.send_error(404)
                return
            self._json({
                "id": int(series_match.group(1)), **details,
                "original_language": details.get("original_language", "en"),
            })
            return
        season_match = re.match(r"^/tv/(\d+)/season/(\d+)$", sub)
        if season_match:
            tmdb_id, season = int(season_match.group(1)), int(season_match.group(2))
            episodes = []
            if tmdb_id == 9001 and season == 1:
                episodes = [
                    {"episode_number": 1, "season_number": 1, "name": "Fallback One",
                     "overview": "", "air_date": "2023-01-01"},
                    {"episode_number": 2, "season_number": 1, "name": "Fallback Two",
                     "overview": "", "air_date": "2023-01-08"},
                ]
            elif tmdb_id == 9002 and season == 1:
                episodes = [
                    {"episode_number": 1, "season_number": 1, "name": "Fallback Pilot",
                     "overview": "", "air_date": "2005-09-13"},
                ]
            self._json({"season_number": season, "episodes": episodes})
            return
        self.send_error(404)

    def _movie_summary(self, tmdb_id: int, details: dict) -> dict:
        return {
            "id": tmdb_id, "title": details["title"],
            "original_title": details["original_title"],
            "release_date": details["release_date"],
            "overview": details["overview"], "poster_path": details.get("poster_path"),
            "backdrop_path": details.get("backdrop_path"),
            "vote_average": details.get("vote_average"),
            "original_language": details.get(
                "original_language", "es" if tmdb_id == 604 else "en"
            ),
        }

    def _series_summary(self, tmdb_id: int, details: dict) -> dict:
        return {
            "id": tmdb_id, "name": details["name"],
            "original_name": details["original_name"],
            "first_air_date": details["first_air_date"],
            "overview": details["overview"], "poster_path": details.get("poster_path"),
            "backdrop_path": details.get("backdrop_path"),
            "vote_average": details.get("vote_average"),
            "original_language": details.get("original_language", "en"),
        }

    # ------------------------------------------------------------------- tvdb
    def _tvdb(self, path: str, params: dict) -> None:
        self.tvdb_auth.append(self.headers.get("Authorization"))
        if path == "/tvdb/v4/search":
            self.tvdb_search_params.append({
                key: values[0] if values else "" for key, values in params.items()
            })
            query = (params.get("query") or [""])[0].lower()
            results = []
            for tvdb_id, name, year in ((9101, "Dark Hearts", "2023"),
                                        (9102, "Supernatural", "2005")):
                if query and any(token in name.lower() for token in query.split()):
                    results.append({"tvdb_id": str(tvdb_id), "name": name,
                                    "year": year, "overview": ""})
            self._json({"status": "success", "data": results})
            return
        match = re.match(r"^/tvdb/v4/series/(\d+)/episodes/default(?:/eng)?$", path)
        if match:
            page = int((params.get("page") or ["0"])[0])
            series_id = int(match.group(1))
            if series_id == 9104 and path.endswith("/eng"):
                # The translated OpenAPI schema omits episodes for some
                # responses; the client must retry the untranslated endpoint.
                self._json({
                    "status": "success",
                    "data": {"series": {"id": series_id}},
                    "links": {"next": None, "prev": None},
                })
                return
            all_episodes = TVDB_EPISODES.get(series_id, [])
            links = {"next": None, "prev": None, "total_items": len(all_episodes)}
            if series_id == 9103:
                # Exercise the documented links.next page-number pagination.
                episodes = all_episodes[:1] if page == 0 else (
                    all_episodes[1:] if page == 1 else []
                )
                if page == 0 and len(all_episodes) > 1:
                    links["next"] = 1
            else:
                episodes = all_episodes if page == 0 else []
            self._json({
                "status": "success",
                "data": {"episodes": episodes},
                "links": links,
            })
            return
        self.send_error(404)

    def _json(self, payload) -> None:
        self._send(json.dumps(payload).encode(), "application/json")


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ManagerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --------------------------------------------------------------------- helpers
def wait_until(predicate, timeout: float = 30.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def latest_download(**filters):
    conn = db.connect()
    try:
        clauses = []
        params = []
        for key, value in filters.items():
            clauses.append(f"{key} = ?")
            params.append(value)
        row = conn.execute(
            f"SELECT * FROM downloads WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT 1", params
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def download_by_id(download_id: int):
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def wait_download(download_id: int, timeout: float = 30.0):
    return wait_until(
        lambda: (lambda row: row if row and row["status"] in ("imported", "failed", "cancelled") else None)(
            download_by_id(download_id)
        ),
        timeout=timeout,
    )


def downloads_for_series(series_id: int) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM downloads WHERE series_id = ? ORDER BY id", (series_id,)
        )]
    finally:
        conn.close()


def page_text(client, path: str) -> str:
    response = client.get(path)
    return response.get_data(as_text=True)


def main() -> int:
    server, base = start_server()
    os.environ["IPTV_CHARTS_TMDB_BASE"] = f"{base}/tmdb/3"
    os.environ["IPTV_CHARTS_TVDB_BASE"] = f"{base}/tvdb/v4"
    os.environ["IPTV_CHARTS_TMDB_IMAGE_BASE"] = f"{base}/tmdb-image"

    from iptv_charts.app import create_app

    app = create_app()
    client = app.test_client()

    try:
        # ------------------------------------------------------------- catalog
        section("fixture catalog")
        conn = db.connect()
        source_id = sources.create(conn, "xtream", "Manager fixture",
                                   base_url=base, username="gooduser", password="pass")
        db.set_setting(conn, metadata.TMDB_KEY, TMDB_TOKEN)
        db.set_setting(conn, metadata.TVDB_KEY, "test-tvdb-key")
        conn.close()

        with tvdb_client.TVDBClient("test-tvdb-key", token="fake-token") as tvdb_api:
            tvdb_results = tvdb_api.search_series("Dark Hearts", year=2023)
        check("TVDB search returns a result", len(tvdb_results), 1)
        search_params = ManagerHandler.tvdb_search_params[-1]
        check("TVDB search sends the documented year filter",
              search_params.get("year"), "2023")
        check("TVDB search sends the series type",
              search_params.get("type"), "series")

        sync.enqueue([source_id])
        result = integration.wait_for_sync()
        check("catalog synced without error", result["jobs"][source_id]["state"], "done")

        section("playlist edit page and credential safety")
        response = client.get(f"/playlists/{source_id}/edit")
        check("edit page status", response.status_code, 200)
        edit_body = response.get_data(as_text=True)
        check_true("edit page shows the account", "gooduser" in edit_body)
        check_true("edit page does not render the password", 'value="pass"' not in edit_body)
        response = client.post(f"/api/sources/{source_id}/probe", json={
            "base_url": base, "username": "someone-else", "password": "",
        })
        check("blank password with a changed username is refused",
              response.status_code, 400)
        response = client.patch(f"/api/sources/{source_id}", json={
            "base_url": base, "username": "someone-else", "password": "",
        })
        check("blank password with a changed account is refused",
              response.status_code, 400)
        response = client.patch(f"/api/sources/{source_id}", json={
            "name": "Manager fixture renamed",
        })
        check("name-only edit accepted", response.status_code, 200)
        check("name-only edit does not resync",
              response.get_json()["catalog_changed"], False)
        response = client.patch(f"/api/sources/{source_id}", json={
            "kind": "m3u",
        })
        check("playlist type cannot be changed in place", response.status_code, 409)
        response = client.patch(f"/api/sources/{source_id}", json={
            "name": "Manager fixture",
        })
        check("fixture name restored", response.status_code, 200)

        conn = db.connect()
        matrix = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE source_id = ? AND kind = 'vod' "
            "AND name LIKE 'The Matrix%'", (source_id,)
        ).fetchone()["n"]
        series_count = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE source_id = ? AND kind = 'series' "
            "AND (name LIKE 'Dark Hearts%' OR name LIKE 'GR - Supernatural%')",
            (source_id,)
        ).fetchone()["n"]
        conn.close()
        check("both Matrix releases ingested", matrix, 2)
        check("special series ingested", series_count, 2)

        profiles = client.get("/api/quality-profiles").get_json()["profiles"]
        hd = next(profile for profile in profiles if profile["name"] == "HD")
        four_k = next(profile for profile in profiles if profile["name"] == "4K")

        # --------------------------------------------------------------- movie
        section("add a movie, download and import")
        response = client.post("/api/movies", json={
            "tmdb_id": 603, "quality_profile_id": hd["id"],
            "monitored": True, "search": True,
        })
        check("movie add status", response.status_code, 201)
        movie = response.get_json()["movie"]
        download = wait_until(lambda: latest_download(movie_id=movie["id"]))
        check_true("a download was queued", download)
        finished = wait_download(download["id"])
        check("movie download imported", finished["status"], "imported")
        check("movie resolution", finished["resolution"], "1080p")
        movie_path = Path(finished["final_path"])
        check_true("movie file exists", movie_path.exists())
        check("radarr-style movie folder", movie_path.parent.name, "The Matrix (1999)")
        check("radarr-style movie filename", movie_path.name, "The Matrix (1999) [1080p].mp4")
        check("media marker matches", movie_path.read_bytes()[:9], b"RES:1080\n")
        conn = db.connect()
        stored = conn.execute("SELECT file_path, resolution FROM movies WHERE id = ?",
                              (movie["id"],)).fetchone()
        conn.close()
        check("movie row points at the file", stored["file_path"], str(movie_path))
        check("movie row resolution", stored["resolution"], "1080p")

        section("quality upgrade replaces the old file")
        response = client.patch(f"/api/movies/{movie['id']}", json={
            "quality_profile_id": four_k["id"],
        })
        check("profile switch status", response.status_code, 200)
        old_path = Path(movie_path)
        response = client.post(f"/api/movies/{movie['id']}/search")
        check("upgrade search status", response.status_code, 200)
        upgrade = wait_until(lambda: latest_download(movie_id=movie["id"]))
        upgraded = wait_download(upgrade["id"])
        check("upgrade imported", upgraded["status"], "imported")
        check("upgrade resolution", upgraded["resolution"], "2160p")
        new_path = Path(upgraded["final_path"])
        check("upgrade filename", new_path.name, "The Matrix (1999) [2160p].mkv")
        check_true("old file removed after upgrade", not old_path.exists())
        check_true("new file exists", new_path.exists())
        conn = db.connect()
        history = conn.execute(
            "SELECT event FROM history WHERE movie_id = ? ORDER BY id DESC LIMIT 3",
            (movie["id"],)
        ).fetchall()
        conn.close()
        check_true("upgrade recorded in history", any(row["event"] == "upgraded" for row in history))

        check_true("TMDB requests use bearer auth",
                   any(auth == f"Bearer {TMDB_TOKEN}"
                       for auth, _api_key in ManagerHandler.tmdb_auth))
        check("TMDB requests never use api_key",
              all(api_key is None for _auth, api_key in ManagerHandler.tmdb_auth),
              True)

        section("localised title matches through TMDB aliases")
        response = client.post("/api/movies", json={
            "tmdb_id": 604, "quality_profile_id": four_k["id"],
            "monitored": True, "search": True,
        })
        check("localised movie add status", response.status_code, 201)
        localised = response.get_json()["movie"]
        localised_download = wait_until(lambda: latest_download(movie_id=localised["id"]))
        localised_done = wait_download(localised_download["id"])
        check("localised movie imported", localised_done["status"], "imported")
        check("localised title resolved",
              Path(localised_done["final_path"]).parent.name, "A Unique Talent (2026)")

        section("manual search and grab")
        response = client.post("/api/movies", json={
            "tmdb_id": 609, "quality_profile_id": hd["id"],
            "monitored": True, "search": False,
        })
        check("manual-only movie added", response.status_code, 201)
        manual_movie = response.get_json()["movie"]
        check("search=False did not auto-grab", latest_download(movie_id=manual_movie["id"]), None)
        releases = client.get(f"/api/movies/{manual_movie['id']}/releases").get_json()["releases"]
        check_true("manual search lists the release", releases)
        if releases:
            release = releases[0]
            response = client.post(f"/api/movies/{manual_movie['id']}/grab", json={
                "source_id": release["source_id"], "remote_id": release["remote_id"],
            })
            check("manual grab accepted", response.status_code, 200)
            manual_download = response.get_json()["download"]
            manual_done = wait_download(manual_download["id"])
            check("manual grab imported", manual_done["status"], "imported")
            check("manual grab resolution", manual_done["resolution"], "720p")

        section("HLS download is remuxed to mkv")
        response = client.post("/api/movies", json={
            "tmdb_id": 610, "quality_profile_id": hd["id"],
            "monitored": True, "search": True,
        })
        hls_movie = response.get_json()["movie"]
        hls_download = wait_until(lambda: latest_download(movie_id=hls_movie["id"]))
        hls_done = wait_download(hls_download["id"])
        check("HLS download imported", hls_done["status"], "imported")
        check("HLS finished filename uses mkv",
              Path(hls_done["final_path"]).name, "HLS Movie (2024) [1080p].mkv")
        check("HLS row container extension updated", hls_done["container_ext"], "mkv")

        # -------------------------------------------------------------- series
        section("series, TVDB episodes and per-episode downloads")
        response = client.post("/api/series", json={
            "tmdb_id": 9001, "quality_profile_id": hd["id"],
            "monitored": True, "search": True,
        })
        check("series add status", response.status_code, 201)
        series = response.get_json()["series"]
        conn = db.connect()
        episodes = conn.execute(
            "SELECT * FROM series_episodes WHERE series_id = ? ORDER BY season, episode",
            (series["id"],)
        ).fetchall()
        conn.close()
        check("TVDB episode count stored", len(episodes), 3)
        check("episode name from TVDB", episodes[0]["title"], "The Beginning")

        def series_downloads_done():
            conn = db.connect()
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM downloads WHERE series_id = ? ORDER BY id", (series["id"],)
            )]
            conn.close()
            if len(rows) < 3:
                return None
            if any(row["status"] in ("queued", "downloading", "importing") for row in rows):
                return None
            return rows

        rows = wait_until(series_downloads_done, timeout=40)
        check_true("three episode downloads completed", rows)
        if rows:
            check("all episode downloads imported",
                  sorted(row["status"] for row in rows), ["imported"] * 3)
            paths = sorted(Path(row["final_path"]) for row in rows)
            check("sonarr-style season folder", paths[0].parent.name, "Season 01")
            check("sonarr-style episode filename",
                  paths[0].name, "Dark Hearts - S01E01 - The Beginning [1080p].mp4")
            check("second episode filename",
                  paths[1].name, "Dark Hearts - S01E02 - The Middle [1080p].mp4")
            check("season 2 filename",
                  paths[2].name, "Dark Hearts - S02E01 - The Return [1080p].mp4")

        check_true("TVDB requests use bearer auth",
                   any(auth and auth.startswith("Bearer ")
                       for auth in ManagerHandler.tvdb_auth))

        section("TMDB episode fallback when TVDB is not configured")
        conn = db.connect()
        db.set_setting(conn, metadata.TVDB_KEY, "")
        conn.close()
        response = client.post("/api/series", json={
            "tmdb_id": 9002, "quality_profile_id": hd["id"],
            "monitored": True, "search": True,
        })
        check("fallback series add status", response.status_code, 201)
        fallback_series = response.get_json()["series"]
        conn = db.connect()
        fallback_episodes = conn.execute(
            "SELECT * FROM series_episodes WHERE series_id = ?", (fallback_series["id"],)
        ).fetchall()
        conn.close()
        check("TMDB fallback supplied the episode", len(fallback_episodes), 1)
        check("TMDB fallback episode name", fallback_episodes[0]["title"], "Fallback Pilot")
        fallback_download = wait_until(lambda: latest_download(series_id=fallback_series["id"]))
        fallback_done = wait_download(fallback_download["id"])
        check("fallback episode downloaded", fallback_done["status"], "imported")
        conn = db.connect()
        db.set_setting(conn, metadata.TVDB_KEY, "test-tvdb-key")
        conn.close()

        # ------------------------------------------------------- probe/quality
        section("probe-first custom quality, audio and subtitles")
        response = client.post("/api/quality-profiles", json={
            "name": "4K WEB-DL English",
            "qualities": [{"resolution": "2160p", "sources": ["web-dl"]}],
            "cutoff": "2160p WEB-DL",
            "upgrades": True,
            "audio_mode": "original",
            "subtitle_mode": "required",
            "subtitle_languages": ["eng"],
            "probe_required": True,
        })
        check("custom profile created", response.status_code, 201)
        custom_profile = response.get_json()["profile"]
        check("custom profile tier round-trips",
              custom_profile["qualities"][0]["sources"], ["web-dl"])
        check("custom profile audio mode", custom_profile["audio_mode"], "original")

        response = client.post("/api/movies", json={
            "tmdb_id": 611, "quality_profile_id": custom_profile["id"],
            "monitored": True, "search": False,
        })
        check("probe movie added", response.status_code, 201)
        probe_movie = response.get_json()["movie"]
        releases = client.get(f"/api/movies/{probe_movie['id']}/releases").get_json()["releases"]
        by_id = {str(release["remote_id"]): release for release in releases}
        check("both probe releases listed", len(by_id) >= 2, True)
        webdl_release = by_id.get("60021")
        cam_release = by_id.get("60020")
        check_true("WEB-DL release was probed", webdl_release and webdl_release.get("probe_ok"))
        if webdl_release:
            check("probe reports real 2160p", webdl_release["resolution"], "2160p")
            check("probe reports English audio", webdl_release["audio_languages"], ["eng"])
            check("probe reports English subtitles", webdl_release["subtitle_languages"], ["eng"])
            check("profile allows WEB-DL", webdl_release["allowed"], True)
            check("quality tier label attached", webdl_release["quality_label"], "2160p WEB-DL")
        if cam_release:
            check("CAM is outside the WEB-DL profile", cam_release["allowed"], False)
            check_true("CAM rejection says why", "source" in (cam_release.get("reason") or ""))
        response = client.get(f"/api/movies/{probe_movie['id']}/releases")
        check("manual releases status", response.status_code, 200)
        conn = db.connect()
        probe_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM stream_probes WHERE source_id = ?", (source_id,)
        ).fetchone()["n"]
        conn.close()
        check_true("probe results are cached", probe_rows >= 2)

        response = client.post(f"/api/movies/{probe_movie['id']}/grab", json={
            "source_id": webdl_release["source_id"],
            "remote_id": webdl_release["remote_id"],
        })
        check("probed grab accepted", response.status_code, 200)
        probe_download = response.get_json()["download"]
        probe_done = wait_download(probe_download["id"])
        check("probed release imported", probe_done["status"], "imported")
        check("probed download source", probe_done["source"], "web-dl")
        check("probed download quality", probe_done["quality_label"], "2160p WEB-DL")
        conn = db.connect()
        stored_movie = conn.execute(
            "SELECT source, quality_label, audio_languages, subtitle_languages "
            "FROM movies WHERE id = ?", (probe_movie["id"],)
        ).fetchone()
        conn.close()
        check("movie stores probed source", stored_movie["source"], "web-dl")
        check("movie stores probed quality", stored_movie["quality_label"], "2160p WEB-DL")
        check("movie stores probed audio", stored_movie["audio_languages"], '["eng"]')
        check("movie stores probed subtitles", stored_movie["subtitle_languages"], '["eng"]')
        response = client.post("/api/probes/clear", json={})
        check("probe cache cleared", response.status_code, 200)
        check_true("probe cache clear reports rows", response.get_json()["cleared"] >= 2)

        # ---------------------------------------------------------- availability
        section("minimum availability blocks an unreleased movie")
        response = client.post("/api/movies", json={
            "tmdb_id": 614, "quality_profile_id": hd["id"],
            "monitored": True, "search": True,
            "minimum_availability": "released",
        })
        check("future movie added", response.status_code, 201)
        future_movie = response.get_json()["movie"]
        check("future movie was not downloaded", latest_download(movie_id=future_movie["id"]), None)
        check_true("future movie explains why",
                   "Not available" in (future_movie.get("search_message") or ""))
        response = client.patch(f"/api/movies/{future_movie['id']}", json={
            "minimum_availability": "announced",
        })
        check("availability changed", response.status_code, 200)
        response = client.post(f"/api/movies/{future_movie['id']}/search")
        check("announced movie search accepted", response.status_code, 200)
        future_download = wait_until(lambda: latest_download(movie_id=future_movie["id"]))
        check_true("announced movie queued", future_download)
        future_done = wait_download(future_download["id"])
        check("announced movie imported", future_done["status"], "imported")

        # ------------------------------------------------------- monitor modes
        section("Sonarr-style monitor modes and new seasons")
        response = client.post("/api/series", json={
            "tmdb_id": 9004, "quality_profile_id": hd["id"],
            "monitored": True, "search": False,
            "monitor_mode": "future", "monitor_new_seasons": "none",
        })
        check("future-only series added", response.status_code, 201)
        monitor_series = response.get_json()["series"]
        conn = db.connect()
        monitor_episodes = {
            (row["season"], row["episode"]): row
            for row in conn.execute(
                "SELECT * FROM series_episodes WHERE series_id = ?",
                (monitor_series["id"],)
            )
        }
        conn.close()
        check("released episode is not monitored in future mode",
              monitor_episodes[(1, 1)]["monitored"], 0)
        check("future episode is monitored in future mode",
              monitor_episodes[(2, 1)]["monitored"], 1)

        TVDB_EPISODES[9104].append({
            "id": 94031, "seasonNumber": 3, "number": 1, "name": "Season Three",
            "aired": "2099-02-01", "overview": "New season.", "image": "",
        })
        response = client.post(f"/api/series/{monitor_series['id']}/refresh")
        check("series refresh with a new season", response.status_code, 200)
        conn = db.connect()
        new_season = conn.execute(
            "SELECT monitored FROM series_episodes WHERE series_id = ? "
            "AND season = 3 AND episode = 1", (monitor_series["id"],)
        ).fetchone()
        conn.close()
        check("monitor_new_seasons=none leaves the new season alone",
              new_season["monitored"], 0)

        response = client.patch(f"/api/series/{monitor_series['id']}", json={
            "monitor_new_seasons": "all",
        })
        check("new-season rule changed", response.status_code, 200)
        TVDB_EPISODES[9104].append({
            "id": 94032, "seasonNumber": 3, "number": 2, "name": "Season Three B",
            "aired": "2099-03-01", "overview": "New season.", "image": "",
        })
        client.post(f"/api/series/{monitor_series['id']}/refresh")
        conn = db.connect()
        new_season_b = conn.execute(
            "SELECT monitored FROM series_episodes WHERE series_id = ? "
            "AND season = 3 AND episode = 2", (monitor_series["id"],)
        ).fetchone()
        conn.close()
        check("monitor_new_seasons=all monitors the new season",
              new_season_b["monitored"], 1)

        # ------------------------------------------------------ connection limit
        section("connection limit serialises a season")
        response = client.patch(f"/api/sources/{source_id}", json={
            "connection_limit": 1,
        })
        check("source connection limit updated", response.status_code, 200)
        check("source connection limit stored",
              response.get_json()["source"]["connection_limit"], 1)
        response = client.post("/api/settings/downloads", json={"concurrency": 3})
        check("worker count raised", response.status_code, 200)
        downloader.ensure_started()
        ManagerHandler.throttle_intervals.clear()

        response = client.post("/api/series", json={
            "tmdb_id": 9003, "quality_profile_id": hd["id"],
            "monitored": True, "search": True, "monitor_mode": "all",
        })
        check("throttle series added", response.status_code, 201)
        throttle_series = response.get_json()["series"]
        throttle_downloads = wait_until(
            lambda: (lambda rows: rows if rows and all(
                row["status"] in ("imported", "failed", "cancelled") for row in rows
            ) else None)(downloads_for_series(throttle_series["id"])),
            timeout=40,
        )
        check_true("two throttled episodes downloaded", throttle_downloads)
        if throttle_downloads:
            check("both throttled episodes imported",
                  sorted(row["status"] for row in throttle_downloads),
                  ["imported", "imported"])
        intervals = sorted(ManagerHandler.throttle_intervals)
        check("two media requests observed", len(intervals), 2)
        if len(intervals) == 2:
            check("one connection at a time", intervals[0][1] <= intervals[1][0], True)
        response = client.patch(f"/api/sources/{source_id}", json={
            "connection_limit": None,
        })
        check("source limit restored to auto", response.status_code, 200)
        client.post("/api/settings/downloads", json={"concurrency": 1})

        section("queue priority edit")
        conn = db.connect()
        priority_download = conn.execute(
            "SELECT id FROM downloads WHERE series_id = ? ORDER BY id LIMIT 1",
            (throttle_series["id"],)
        ).fetchone()
        conn.close()
        response = client.patch(f"/api/downloads/{priority_download['id']}", json={
            "priority": 25,
        })
        check("priority edit accepted", response.status_code, 200)
        check("priority stored", response.get_json()["download"]["priority"], 25)

        # --------------------------------------------------------------- cancel
        section("cancel a slow download")
        response = client.post("/api/movies", json={
            "tmdb_id": 605, "quality_profile_id": hd["id"],
            "monitored": True, "search": True,
        })
        slow_movie = response.get_json()["movie"]
        slow = wait_until(lambda: latest_download(movie_id=slow_movie["id"]))
        wait_until(lambda: (download_by_id(slow["id"]) or {}).get("status") == "downloading")
        response = client.post(f"/api/downloads/{slow['id']}/cancel")
        check("cancel request status", response.status_code, 200)
        cancelled = wait_download(slow["id"])
        check("slow download cancelled", cancelled["status"], "cancelled")
        check("cancelled download left no final file", cancelled["final_path"], None)

        section("retry a transient provider failure")
        conn = db.connect()
        db.set_setting(conn, "download_max_attempts", "1")
        conn.close()
        response = client.post("/api/movies", json={
            "tmdb_id": 606, "quality_profile_id": hd["id"],
            "monitored": True, "search": True,
        })
        dead_movie = response.get_json()["movie"]
        dead = wait_until(lambda: latest_download(movie_id=dead_movie["id"]))
        dead_done = wait_download(dead["id"])
        check("first attempt failed", dead_done["status"], "failed")
        response = client.post(f"/api/downloads/{dead['id']}/retry")
        check("retry request accepted", response.status_code, 200)
        retried = wait_download(dead["id"], timeout=30)
        check("retry imported", retried["status"], "imported")
        conn = db.connect()
        db.set_setting(conn, "download_max_attempts", "3")
        conn.close()

        section("a provider HTML page is rejected as not-a-video")
        response = client.post("/api/movies", json={
            "tmdb_id": 608, "quality_profile_id": hd["id"],
            "monitored": True, "search": True,
        })
        html_movie = response.get_json()["movie"]
        html_download = wait_until(lambda: latest_download(movie_id=html_movie["id"]))
        html_done = wait_download(html_download["id"])
        check("HTML response failed", html_done["status"], "failed")
        check_true("error explains the web page", "web page" in (html_done["error"] or ""))

        # ----------------------------------------------------------------- scan
        section("library scan and review import")
        scan_folder = _MEDIA / "Movies" / "Scanned Movie (2024)"
        scan_folder.mkdir(parents=True, exist_ok=True)
        scan_file = scan_folder / "Scanned Movie (2024) [2160p].mkv"
        scan_file.write_bytes(b"RES:2160\n" + b"y" * 4096)
        response = client.post("/api/tasks/scan_library/run")
        check("scan task started", response.status_code, 200)
        completed = wait_until(
            lambda: (lambda tasks: next(
                (task for task in tasks if task["name"] == "scan_library"
                 and task["status"] in ("idle", "error")), None)
            )(client.get("/api/tasks").get_json()["tasks"]),
            timeout=30,
        )
        check_true("scan task finished", completed)
        results = client.get("/api/scan/results").get_json()["results"]
        scanned = next((row for row in results if row["path"] == str(scan_file)), None)
        check_true("scan proposed the new file", scanned)
        if scanned:
            check("scan matched TMDB", scanned["tmdb_id"], 607)
            check("scan probed the resolution", scanned["resolution"], "2160p")
            response = client.post(f"/api/scan/results/{scanned['id']}/import")
            check("scan import status", response.status_code, 200)
            conn = db.connect()
            imported_movie = conn.execute(
                "SELECT * FROM movies WHERE tmdb_id = 607"
            ).fetchone()
            conn.close()
            check_true("scan created the movie", imported_movie)
            check("scan attached the file", imported_movie["file_path"], str(scan_file))

        # --------------------------------------------------------------- backup
        section("backup export and restore")
        conn = db.connect()
        db.set_setting(conn, metadata.LANGUAGE, "en-US")
        conn.close()
        response = client.post("/api/backup/export", json={})
        check("backup export status", response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.data))
        manifest = json.loads(archive.read("manifest.json"))
        names = set(archive.namelist())
        check("backup format", manifest.get("format"), "iptv-charts-backup")
        check_true("backup excludes items", "items" in manifest.get("excluded", []))
        check_true("backup includes movies", "tables/movies.json" in names)
        check_true("backup includes sources", "tables/sources.json" in names)
        movie_rows = json.loads(archive.read("tables/movies.json"))
        check_true("backup captured library rows", len(movie_rows) >= 5)

        conn = db.connect()
        db.set_setting(conn, metadata.LANGUAGE, "fr-FR")
        conn.close()
        response = client.post("/api/backup/restore", data=response.data,
                               content_type="application/zip")
        result = response.get_json()
        check("restore status", response.status_code, 200)
        check_true("restore reports counts", result.get("counts", {}).get("movies", 0) >= 5)
        check("restore queues a sync", result.get("needs_sync"), True)
        conn = db.connect()
        language = db.get_setting(conn, metadata.LANGUAGE, "")
        restored_movie = conn.execute(
            "SELECT file_path FROM movies WHERE tmdb_id = 603"
        ).fetchone()
        restored_source = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
        conn.close()
        check("settings restored", language, "en-US")
        check("library file path restored", restored_movie["file_path"], str(new_path))
        check("source restored", restored_source, 1)
        integration.wait_for_sync()
        conn = db.connect()
        item_count = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        conn.close()
        check_true("catalog rebuilt after restore", item_count > 300)

        # -------------------------------------------------------------- pages/api
        section("rendered pages and settings APIs")
        for path, needle in (
            ("/", "IPTV"),
            ("/", "Movies"),
            ("/", "Series"),
            ("/playlists", "Save limit"),
            ("/movies", "The Matrix"),
            (f"/movies/{movie['id']}", "The Matrix"),
            (f"/series/{series['id']}", "The Beginning"),
            ("/calendar", "Calendar"),
            ("/wanted", "Wanted"),
            ("/activity", "Queue"),
            ("/scan", "Library scan"),
            ("/settings", "Probe cache"),
            ("/settings/profiles", "Quality hierarchy"),
        ):
            text = page_text(client, path)
            check_true(f"{path} renders", needle in text)

        settings_page = page_text(client, "/settings")
        check_true("settings shows ffprobe", "ffprobe" in settings_page)
        check_true("settings asks for the read access token",
                   "API Read Access Token" in settings_page)

        response = client.post("/api/settings/save", json={
            "metadata": {"tmdb_read_token": "a" * 32},
        })
        check("v3 API key is refused", response.status_code, 400)
        check("v3 key error names the metadata section",
              response.get_json()["section"], "metadata")
        profiles_page = page_text(client, "/settings/profiles")
        check_true("profiles page shows the custom tier",
                   "2160p WEB-DL" in profiles_page)

        activity_payload = client.get("/api/activity").get_json()
        check("activity API has a queue", isinstance(activity_payload.get("active"), list), True)
        check_true("activity API includes task state", activity_payload.get("tasks"))
        tasks_payload = client.get("/api/tasks").get_json()
        check("tasks API lists all four tasks", len(tasks_payload.get("tasks", [])), 4)

        image = client.get("/api/images/tmdb/w92/matrix.jpg")
        check("TMDB image proxy status", image.status_code, 200)
        check("TMDB image proxy body", image.data, b"FAKEIMAGE")
        check("TMDB image proxy type", image.headers.get("Content-Type"), "image/jpeg")
        check("image proxy refuses an unknown size",
              client.get("/api/images/tmdb/huge/matrix.jpg").status_code, 404)
        check("image proxy refuses a non-image path",
              client.get("/api/images/tmdb/w92/not-an-image.txt").status_code, 404)

        response = client.post("/api/settings/media", json={
            "movie_file_template": "{title}/{year}",
        })
        check("template with a separator refused", response.status_code, 400)
        response = client.post("/api/settings/media", json={
            "movie_file_template": "{title} ({year}) [{resolution}]",
        })
        check("valid template accepted", response.status_code, 200)
        check_true("template preview returned", "movie_file" in response.get_json()["preview"])

        response = client.post("/api/settings/metadata", json={
            "language": "not a language",
        })
        check("bad metadata language refused", response.status_code, 400)
        response = client.post("/api/settings/metadata", json={
            "language": "es-ES",
        })
        check("metadata language accepted", response.status_code, 200)

        response = client.post("/api/quality-profiles", json={
            "name": "Test profile",
            "resolutions": ["1080p", "720p"],
            "cutoff": "1080p",
            "upgrades": True,
        })
        check("profile created", response.status_code, 201)
        test_profile = response.get_json()["profile"]
        response = client.patch(f"/api/quality-profiles/{test_profile['id']}", json={
            "name": "Test profile 2",
            "resolutions": ["720p"],
            "cutoff": "720p",
            "upgrades": False,
        })
        check("profile edited", response.status_code, 200)
        response = client.delete(f"/api/quality-profiles/{test_profile['id']}")
        check("unused profile deleted", response.status_code, 200)

        response = client.post("/api/movies", json={"tmdb_id": 603},
                               headers={"Origin": "http://evil.example"})
        check("cross-site movie write refused", response.status_code, 403)

    except Exception as exc:
        import traceback
        traceback.print_exc()
        check("manager suite completed without an exception", False, True)
    finally:
        downloader.shutdown()
        scheduler.stop()
        server.shutdown()

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = [(label, detail) for ok, label, detail in _results if not ok]
    print(f"\n{'=' * 60}")
    if failed:
        print(f"\033[31m{len(failed)} FAILED\033[0m, {passed} passed\n")
        for label, detail in failed:
            print(f"  \033[31m✗\033[0m {label}\n      {detail}")
        return 1
    print(f"\033[32mall {passed} checks passed\033[0m")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
