#!/usr/bin/env python3
"""Tests for Import existing and manual metadata replacement.

Everything runs against temporary directories and a local fixture TMDB server;
no network, no user media, and no file is moved or deleted by an import.

Run with:  .venv/bin/python tests/import_media.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_TMP = Path(tempfile.mkdtemp(prefix="iptv-import-test-"))
_FAKE_BIN = _TMP / "bin"
_FAKE_BIN.mkdir()
_MEDIA = _TMP / "media"
_MOVIES = _MEDIA / "Movies"
_SERIES = _MEDIA / "Series"
_OUTSIDE = _TMP / "outside"
for directory in (_MOVIES, _SERIES, _OUTSIDE):
    directory.mkdir(parents=True, exist_ok=True)

os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_MEDIA)
os.environ["IPTV_CHARTS_MOVIE_DIR"] = str(_MOVIES)
os.environ["IPTV_CHARTS_SERIES_DIR"] = str(_SERIES)
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"
os.environ["PATH"] = f"{_FAKE_BIN}:{os.environ.get('PATH', '')}"

# Import uses ffprobe only after the user confirms; the fixture writes its
# resolution marker inside the tiny test file so no real media is needed.
(_FAKE_BIN / "ffprobe").write_text(
    "#!/bin/sh\n"
    "last=''\n"
    "for arg in \"$@\"; do last=\"$arg\"; done\n"
    "height=1080\n"
    "case \"$(head -c 64 \"$last\" 2>/dev/null)\" in\n"
    "  *RES:2160*) height=2160 ;;\n"
    "  *RES:720*) height=720 ;;\n"
    "  *RES:480*) height=480 ;;\n"
    "esac\n"
    "printf '{\"streams\":[{\"height\":%s}]}' \"$height\"\n"
)
(_FAKE_BIN / "ffprobe").chmod(0o755)

TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.import-test-token"

MOVIES = {
    603: {
        "title": "The Matrix", "original_title": "The Matrix",
        "release_date": "1999-03-31", "overview": "A hacker learns the truth.",
        "poster_path": "/matrix.jpg", "backdrop_path": "/matrix-bg.jpg",
        "vote_average": 8.7, "runtime": 136,
        "genres": [{"name": "Action"}, {"name": "Science Fiction"}],
        "external_ids": {"imdb_id": "tt0133093"},
        "alternative_titles": {"titles": []}, "status": "Released",
        "original_language": "en",
    },
    604: {
        "title": "A Unique Talent", "original_title": "Un talento único",
        "release_date": "2024-01-15", "overview": "A localised title.",
        "poster_path": "/talent.jpg", "backdrop_path": None,
        "vote_average": 7.0, "runtime": 101,
        "genres": [{"name": "Comedy"}],
        "external_ids": {"imdb_id": "tt0604"},
        "alternative_titles": {"titles": [{"iso_3166_1": "ES", "title": "Un talento único"}]},
        "status": "Released", "original_language": "es",
    },
    605: {
        "title": "Slow Movie", "original_title": "Slow Movie",
        "release_date": "2024-02-01", "overview": "Slow.",
        "poster_path": "/slow.jpg", "backdrop_path": None,
        "vote_average": 6.0, "runtime": 90,
        "genres": [{"name": "Drama"}],
        "external_ids": {"imdb_id": "tt0605"},
        "alternative_titles": {"titles": []}, "status": "Released",
        "original_language": "en",
    },
    606: {
        "title": "Another Movie", "original_title": "Another Movie",
        "release_date": "2024-03-01", "overview": "The corrected identity.",
        "poster_path": "/another.jpg", "backdrop_path": None,
        "vote_average": 7.2, "runtime": 98,
        "genres": [{"name": "Mystery"}],
        "external_ids": {"imdb_id": "tt0606"},
        "alternative_titles": {"titles": []}, "status": "Released",
        "original_language": "en",
    },
    607: {
        "title": "Tracked Movie", "original_title": "Tracked Movie",
        "release_date": "2024-04-01", "overview": "Already on disk.",
        "poster_path": "/tracked.jpg", "backdrop_path": None,
        "vote_average": 6.0, "runtime": 90,
        "genres": [{"name": "Drama"}],
        "external_ids": {"imdb_id": "tt0607"},
        "alternative_titles": {"titles": []}, "status": "Released",
        "original_language": "en",
    },
    608: {
        "title": "Explicit Choice", "original_title": "Explicit Choice",
        "release_date": "2024-05-01", "overview": "The user's explicit pick.",
        "poster_path": "/explicit.jpg", "backdrop_path": None,
        "vote_average": 6.5, "runtime": 92,
        "genres": [{"name": "Drama"}],
        "external_ids": {"imdb_id": "tt0608"},
        "alternative_titles": {"titles": []}, "status": "Released",
        "original_language": "en",
    },
}

SERIES = {
    9001: {
        "name": "Dark Hearts", "original_name": "Dark Hearts",
        "first_air_date": "2023-01-01", "overview": "A dark story.",
        "poster_path": "/dark.jpg", "backdrop_path": "/dark-bg.jpg",
        "vote_average": 7.5, "status": "Ended",
        "genres": [{"name": "Drama"}],
        "external_ids": {"tvdb_id": 9101},
        "seasons": [{"season_number": 1, "episode_count": 2},
                    {"season_number": 2, "episode_count": 1}],
        "alternative_titles": {"results": []}, "original_language": "en",
    },
    9002: {
        "name": "Supernatural", "original_name": "Supernatural",
        "first_air_date": "2005-09-13", "overview": "Brothers.",
        "poster_path": "/spn.jpg", "backdrop_path": None,
        "vote_average": 8.0, "status": "Ended",
        "genres": [{"name": "Drama"}],
        "external_ids": {"tvdb_id": 9102},
        "seasons": [{"season_number": 1, "episode_count": 1}],
        "alternative_titles": {"results": []}, "original_language": "en",
    },
    9004: {
        "name": "Other Show", "original_name": "Other Show",
        "first_air_date": "2024-01-01", "overview": "The duplicate target.",
        "poster_path": "/other.jpg", "backdrop_path": None,
        "vote_average": 6.0, "status": "Continuing",
        "genres": [{"name": "Drama"}],
        "external_ids": {"tvdb_id": 9104},
        "seasons": [{"season_number": 1, "episode_count": 1}],
        "alternative_titles": {"results": []}, "original_language": "en",
    },
    9005: {
        "name": "Import Show", "original_name": "Import Show",
        "first_air_date": "2022-01-01", "overview": "Files to import.",
        "poster_path": "/import.jpg", "backdrop_path": None,
        "vote_average": 7.0, "status": "Continuing",
        "genres": [{"name": "Drama"}],
        "external_ids": {"tvdb_id": 9105},
        "seasons": [{"season_number": 1, "episode_count": 2},
                    {"season_number": 2, "episode_count": 1}],
        "alternative_titles": {"results": []}, "original_language": "en",
    },
}

SERIES_EPISODES = {
    9001: {
        1: [
            {"episode_number": 1, "season_number": 1, "name": "Dark One",
             "overview": "First.", "air_date": "2023-01-01"},
            {"episode_number": 2, "season_number": 1, "name": "Dark Two",
             "overview": "Second.", "air_date": "2023-01-08"},
        ],
        2: [
            {"episode_number": 1, "season_number": 2, "name": "Dark Return",
             "overview": "Third.", "air_date": "2024-01-01"},
        ],
    },
    9002: {
        1: [
            {"episode_number": 1, "season_number": 1, "name": "Pilot",
             "overview": "First.", "air_date": "2005-09-13"},
        ],
    },
    9004: {
        1: [
            {"episode_number": 1, "season_number": 1, "name": "Other Pilot",
             "overview": "First.", "air_date": "2024-01-01"},
        ],
    },
    9005: {
        1: [
            {"episode_number": 1, "season_number": 1, "name": "Import One",
             "overview": "First.", "air_date": "2022-01-01"},
            {"episode_number": 2, "season_number": 1, "name": "Import Two",
             "overview": "Second.", "air_date": "2022-01-08"},
        ],
        2: [
            {"episode_number": 1, "season_number": 2, "name": "Import Future",
             "overview": "Far future.", "air_date": "2099-01-01"},
        ],
    },
}


def _tokens(query: str) -> list[str]:
    return [token for token in str(query or "").casefold().split() if token]


def _matches(query: str, *titles: str) -> bool:
    tokens = _tokens(query)
    if not tokens:
        return False
    haystack = " ".join(str(title or "") for title in titles).casefold()
    return all(token in haystack for token in tokens)


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 - keep test output quiet
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _summary(self, tmdb_id, details, kind):
        summary = {
            "id": int(tmdb_id),
            "overview": details.get("overview", ""),
            "poster_path": details.get("poster_path"),
            "backdrop_path": details.get("backdrop_path"),
            "vote_average": details.get("vote_average"),
            "original_language": details.get("original_language", "en"),
        }
        if kind == "movie":
            summary.update({
                "title": details["title"],
                "original_title": details.get("original_title"),
                "release_date": details["release_date"],
            })
        else:
            summary.update({
                "name": details["name"],
                "original_name": details.get("original_name"),
                "first_air_date": details["first_air_date"],
            })
        return summary

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path
        if not path.startswith("/tmdb/3/"):
            self.send_error(404)
            return
        sub = path[len("/tmdb/3"):]
        query = (params.get("query") or [""])[0]

        if sub == "/search/movie":
            results = [
                self._summary(tmdb_id, details, "movie")
                for tmdb_id, details in MOVIES.items()
                if _matches(query, details["title"], details.get("original_title", ""))
            ]
            self._json({"page": 1, "results": results,
                        "total_results": len(results)})
            return
        if sub == "/search/tv":
            results = [
                self._summary(tmdb_id, details, "tv")
                for tmdb_id, details in SERIES.items()
                if _matches(query, details["name"], details.get("original_name", ""))
            ]
            self._json({"page": 1, "results": results,
                        "total_results": len(results)})
            return
        season_match = re.match(r"^/tv/(\d+)/season/(\d+)$", sub)
        if season_match:
            tmdb_id, season = int(season_match.group(1)), int(season_match.group(2))
            episodes = SERIES_EPISODES.get(tmdb_id, {}).get(season)
            if episodes is None:
                self.send_error(404)
                return
            self._json({"season_number": season, "episodes": episodes})
            return
        movie_match = re.match(r"^/movie/(\d+)$", sub)
        if movie_match:
            tmdb_id = int(movie_match.group(1))
            details = MOVIES.get(tmdb_id)
            if details is None:
                self.send_error(404)
                return
            self._json({"id": tmdb_id, **details})
            return
        series_match = re.match(r"^/tv/(\d+)$", sub)
        if series_match:
            tmdb_id = int(series_match.group(1))
            details = SERIES.get(tmdb_id)
            if details is None:
                self.send_error(404)
                return
            self._json({"id": tmdb_id, **details})
            return
        self.send_error(404)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def write_media(path: Path, marker: str = "RES:1080") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(marker.encode("ascii") + b"\n" + b"x" * 512)
    return path


def count(conn, sql: str, params=()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def main() -> int:
    server, base = start_server()
    os.environ["IPTV_CHARTS_TMDB_BASE"] = f"{base}/tmdb/3"
    os.environ["IPTV_CHARTS_TMDB_IMAGE_BASE"] = f"{base}/tmdb-image"

    from iptv_charts import db, library, metadata, scan
    from iptv_charts.app import create_app
    from iptv_charts.import_web import bp as import_web_bp

    app = create_app()
    if "import_media" not in app.blueprints:
        app.register_blueprint(import_web_bp)
    client = app.test_client()

    try:
        conn = db.connect()

        # ---------------------------------------------------------- fixture media
        matrix_file = write_media(
            _MOVIES / "The Matrix (1999)" / "The Matrix (1999) [1080p].mkv")
        loose_file = write_media(_MOVIES / "Loose Movie (2024).mp4", "RES:720")
        tracked_file = write_media(
            _MOVIES / "Tracked Movie (2024)" / "Tracked Movie (2024).mkv")
        ignored_file = write_media(_MOVIES / "Ignored Movie (2024).mkv")
        episode_in_movies = write_media(
            _MOVIES / "Series Episode (2024) S01E01.mkv")
        explicit_file = write_media(_MOVIES / "Explicit Choice (2024).mkv")
        slow_file = write_media(_MOVIES / "Slow Movie (2024).mkv")
        text_file = _MOVIES / "notes.txt"
        text_file.write_text("not video", encoding="utf-8")

        import_one = write_media(
            _SERIES / "Import Show (2022)" / "Season 1"
            / "Import Show (2022) S01E01 [1080p].mkv")
        import_two = write_media(
            _SERIES / "Import Show (2022)" / "Season 1"
            / "Import Show (2022) S01E02 [1080p].mkv")
        import_future = write_media(
            _SERIES / "Import Show (2022)" / "Season 2"
            / "Import Show (2022) S02E01 [1080p].mkv")
        no_marker = write_media(_SERIES / "No Marker Show" / "file.mkv")
        supernatural_file = write_media(
            _SERIES / "Supernatural (2005)" / "Season 1"
            / "Supernatural (2005) S01E01.mkv")

        outside_file = write_media(_OUTSIDE / "Outside Movie (2024).mkv")
        outside_link = _MOVIES / "Outside Link.mkv"
        outside_link.symlink_to(outside_file)

        all_media = [
            matrix_file, loose_file, tracked_file, ignored_file,
            episode_in_movies, explicit_file, slow_file, import_one,
            import_two, import_future, no_marker, supernatural_file, outside_file,
        ]

        # --------------------------------------------------------- fixture library
        db.set_setting(conn, metadata.TMDB_KEY, TOKEN)
        profile_1080 = library.default_profile_id(conn)
        profile_720_row = db.get_one(
            conn, "SELECT id, name FROM quality_profiles WHERE builtin_key = '720p'")
        profile_720 = int(profile_720_row["id"]) if profile_720_row else None
        check("the default profile is the 1080p builtin", (
            db.get_one(conn, "SELECT builtin_key FROM quality_profiles WHERE id = ?",
                       (profile_1080,))["builtin_key"]
        ), "1080p")

        # The tracked movie already owns a file: discovery and import must not
        # offer or attach it again.
        tracked_movie = library.add_movie(conn, 607, profile_id=profile_1080)
        conn.execute(
            "UPDATE movies SET file_path = ?, file_size = ?, resolution = ? "
            "WHERE id = ?",
            (str(tracked_file), tracked_file.stat().st_size, "1080p",
             tracked_movie["id"]),
        )
        conn.commit()

        # A scan result explicitly ignored by the user stays out of review.
        conn.execute(
            "INSERT INTO scan_results (path, kind, title, status, scanned_at) "
            "VALUES (?, 'movie', 'Ignored Movie', 'ignored', ?)",
            (str(ignored_file), int(time.time())),
        )
        conn.commit()

        # The replacement source keeps a file, profile and download row that
        # applying new metadata must not disturb.
        slow_movie = library.add_movie(conn, 605, profile_id=profile_720)
        conn.execute(
            "UPDATE movies SET file_path = ?, file_size = ?, resolution = ?, "
            "imported_at = ?, monitored = 0 WHERE id = ?",
            (str(slow_file), slow_file.stat().st_size, "1080p",
             int(time.time()), slow_movie["id"]),
        )
        conn.execute(
            "INSERT INTO downloads (kind, movie_id, provider_kind, "
            "provider_remote_id, title, status, requested_at) "
            "VALUES ('movie', ?, 'vod', 'remote-1', 'Slow Movie', 'downloading', ?)",
            (slow_movie["id"], int(time.time())),
        )
        conn.commit()
        library.add_movie(conn, 604, profile_id=profile_1080)  # duplicate target

        supernatural = library.add_series(
            conn, 9002, profile_id=profile_720, monitored=False,
            monitor_mode="none")
        supernatural_episode = db.get_one(
            conn,
            "SELECT id FROM series_episodes WHERE series_id = ? "
            "AND season = 1 AND episode = 1",
            (supernatural["id"],),
        )
        conn.execute(
            "UPDATE series_episodes SET file_path = ?, file_size = ?, "
            "resolution = ?, imported_at = ? WHERE id = ?",
            (str(supernatural_file), supernatural_file.stat().st_size,
             "1080p", int(time.time()), supernatural_episode["id"]),
        )
        conn.commit()
        library.add_series(conn, 9004, profile_id=profile_1080, monitored=False,
                           monitor_mode="none")  # duplicate target

        # ------------------------------------------------------------- page/security
        section("page and security")
        page = client.get("/import")
        html = page.get_data(as_text=True)
        check("import page renders", page.status_code, 200)
        check_true("page links its stylesheet",
                   "/static/css/import_media.css" in html)
        check_true("page loads its script",
                   "/static/js/import_media.js" in html)
        check_true("page defaults to the movie tab",
                   'data-initial-tab="movie"' in html)
        check_true("page renders profile choices", 'id="import-profile"' in html)
        check_true("page has both kind tabs", ">Movies<" in html and ">Series<" in html)
        check_true("page uses no external assets",
                   "https://" not in html and "cdn." not in html)

        series_page = client.get("/import?tab=series")
        check_true("series tab is selectable",
                   'data-initial-tab="series"' in series_page.get_data(as_text=True))
        redirect = client.get("/import/movies")
        check("movie alias redirects", redirect.status_code, 302)
        check_true("movie alias targets the movie tab",
                   "tab=movie" in redirect.headers.get("Location", ""))
        redirect = client.get("/import/series")
        check("series alias redirects", redirect.status_code, 302)
        check_true("series alias targets the series tab",
                   "tab=series" in redirect.headers.get("Location", ""))

        css = client.get("/static/css/import_media.css").get_data(as_text=True)
        check_true("stylesheet is local and responsive",
                   "@media" in css and "@import" not in css
                   and "url(http" not in css)
        js = client.get("/static/js/import_media.js").get_data(as_text=True)
        check_true("script is vanilla and local",
                   "api/import/scan" in js and "https://" not in js
                   and "http://" not in js)

        blocked = client.get("/api/import/scan?kind=movie",
                             base_url="http://evil.example")
        check("non-loopback Host is refused", blocked.status_code, 403)
        blocked = client.post(
            "/api/import/confirm",
            json={"items": [{"path": str(matrix_file), "kind": "movie"}]},
            headers={"Origin": "https://evil.example"},
        )
        check("cross-origin writes are refused", blocked.status_code, 403)

        # ----------------------------------------------------------- untracked scan
        section("untracked scan")
        scan_rows_before = count(conn, "SELECT COUNT(*) FROM scan_results")
        response = client.get("/api/import/scan?kind=movie")
        data = response.get_json()
        check("movie scan succeeds", response.status_code, 200)
        check("movie scan reports ok", data["ok"], True)
        movies = {item["name"]: item for item in data["items"]}
        check_true("untracked movie is listed", matrix_file.name in movies)
        matrix_item = movies[matrix_file.name]
        check("parsed movie title", matrix_item["title"], "the matrix")
        check("parsed movie year", matrix_item["year"], 1999)
        check("parsed movie resolution", matrix_item["resolution"], "1080p")
        check("parsed movie kind", matrix_item["kind"], "movie")
        check("untracked movie is importable", matrix_item["importable"], True)
        check("discovery reports the relative path",
              matrix_item["relative"], "The Matrix (1999)/The Matrix (1999) [1080p].mkv")
        check("discovery reports a size", matrix_item["size"] > 0, True)
        check_true("tracked movie is not listed",
                   "Tracked Movie (2024).mkv" not in movies)
        check_true("ignored movie is not listed", "Ignored Movie (2024).mkv" not in movies)
        check_true("symlink outside the root is not listed",
                   "Outside Link.mkv" not in movies)
        check("episode-looking movie is not importable",
              movies[episode_in_movies.name]["importable"], False)
        check("scan is read-only", count(conn, "SELECT COUNT(*) FROM scan_results"),
              scan_rows_before)

        response = client.get("/api/import/scan?kind=series")
        data = response.get_json()
        check("series scan succeeds", response.status_code, 200)
        episodes = {(item["season"], item["episode"]): item for item in data["items"]}
        first = episodes[(1, 1)]
        check("parsed series title", first["title"], "import show")
        check("parsed series year", first["year"], 2022)
        check("parsed series marker", (first["season"], first["episode"]), (1, 1))
        check("series episode is importable", first["importable"], True)
        check_true("future episode is listed", (2, 1) in episodes)
        check_true("tracked episode is not listed",
                   all("Supernatural" not in item["name"] for item in data["items"]))
        broken = next(item for item in data["items"]
                      if item["path"] == str(no_marker.resolve()))
        check("file without an episode marker is not importable",
              broken["importable"], False)
        check("invalid scan kind is rejected",
              client.get("/api/import/scan?kind=music").status_code, 400)
        check_true("discovery leaves every media file in place",
                   all(path.exists() for path in all_media))

        # --------------------------------------------------------- metadata fetch
        section("metadata search and preview")
        response = client.get("/api/import/metadata/search?kind=movie&q=Matrix")
        data = response.get_json()
        check("movie metadata search succeeds", response.status_code, 200)
        check("movie search finds TMDB 603", data["results"][0]["tmdb_id"], 603)
        check("search result title", data["results"][0]["title"], "The Matrix")
        check("search result year", data["results"][0]["year"], 1999)
        check("search result has a local poster proxy",
              data["results"][0]["poster"], "/api/images/tmdb/w342/matrix.jpg")
        check("search marks untracked result", data["results"][0]["in_library"], False)
        check("empty search is rejected",
              client.get("/api/import/metadata/search?kind=movie&q=").status_code, 400)
        check("invalid search kind is rejected",
              client.get("/api/import/metadata/search?kind=music&q=x").status_code, 400)

        response = client.get("/api/import/metadata/preview?kind=movie&tmdb_id=603")
        preview = response.get_json()
        check("movie preview succeeds", response.status_code, 200)
        check("preview title", preview["match"]["title"], "The Matrix")
        check("preview overview", preview["match"]["overview"],
              "A hacker learns the truth.")
        check("preview poster", preview["match"]["poster"],
              "/api/images/tmdb/w342/matrix.jpg")
        check("invalid preview id is rejected",
              client.get("/api/import/metadata/preview?kind=movie&tmdb_id=nope")
              .status_code, 400)

        # ------------------------------------------------------------- movie import
        section("movie import")
        payload = {
            "items": [{
                "path": matrix_item["path"], "kind": "movie", "tmdb_id": 603,
                "title": matrix_item["title"], "year": matrix_item["year"],
                "resolution": matrix_item["resolution"],
                "quality_profile_id": profile_720,
            }],
            "quality_profile_id": profile_1080,
        }
        response = client.post("/api/import/confirm", json=payload)
        data = response.get_json()
        check("movie import succeeds", response.status_code, 200)
        item = data["item"]
        check("movie import reports ok", data["ok"], True)
        check("movie import attaches TMDB identity", item["tmdb_id"], 603)
        check("movie import is not a duplicate", item["already_imported"], False)
        check("movie import is monitored", item["monitored"], True)
        movie = library.get_movie(conn, item["movie_id"])
        check("movie file path is stored", movie["file_path"],
              str(matrix_file.resolve()))
        check("movie resolution is probed", movie["resolution"], "1080p")
        check("movie profile comes from the row payload",
              movie["quality_profile_id"], profile_720)
        check("movie is monitored", movie["monitored"], 1)
        check("movie import timestamp is set", movie["imported_at"] is not None, True)
        check("movie has no provider source", movie["main_source_id"], None)
        imported_result = conn.execute(
            "SELECT status, tmdb_id FROM scan_results WHERE path = ?",
            (str(matrix_file.resolve()),),
        ).fetchone()
        check("scan result is marked imported", imported_result["status"], "imported")
        check("scan result keeps the TMDB id", imported_result["tmdb_id"], 603)
        check("bworker work exists for the imported movie",
              count(conn, "SELECT COUNT(*) FROM bworker_work WHERE work_key = ?",
                    (f"movie:{movie['id']}:search",)), 1)

        data = client.get("/api/import/scan?kind=movie").get_json()
        check_true("imported movie disappears from review",
                   matrix_file.name not in {entry["name"] for entry in data["items"]})

        response = client.post("/api/import/confirm", json=payload)
        repeated = response.get_json()
        check("repeating an import is idempotent", response.status_code, 200)
        check("repeat reports already imported",
              repeated["item"]["already_imported"], True)
        check("repeat creates no second movie",
              count(conn, "SELECT COUNT(*) FROM movies WHERE tmdb_id = 603"), 1)
        check("repeat creates no second work row",
              count(conn, "SELECT COUNT(*) FROM bworker_work WHERE work_key = ?",
                    (f"movie:{movie['id']}:search",)), 1)

        # Same file, different identity; and same identity, different file.
        conflict = client.post("/api/import/confirm", json={
            "items": [{"path": matrix_item["path"], "kind": "movie",
                       "tmdb_id": 604, "quality_profile_id": profile_1080}],
        })
        check("a different identity for the same file is refused",
              conflict.status_code, 409)
        check("conflict response follows the JSON convention",
              conflict.get_json()["ok"], False)
        conflict = client.post("/api/import/confirm", json={
            "items": [{"path": str(loose_file), "kind": "movie",
                       "tmdb_id": 603, "quality_profile_id": profile_1080}],
        })
        check("a different file for the same identity is refused",
              conflict.status_code, 409)
        check("a different file was not attached",
              library.get_movie(conn, movie["id"])["file_path"],
              str(matrix_file.resolve()))

        # An explicit TMDB result is authoritative even if a caller leaves a
        # stale local match in the same payload.
        explicit = client.post("/api/import/confirm", json={
            "items": [{
                "path": str(explicit_file), "kind": "movie", "tmdb_id": 608,
                "matched_movie_id": slow_movie["id"],
                "quality_profile_id": profile_1080,
            }],
        })
        explicit_data = explicit.get_json()
        check("explicit TMDB metadata import succeeds", explicit.status_code, 200)
        explicit_movie = library.get_movie(conn, explicit_data["item"]["movie_id"])
        check("explicit TMDB id wins over a stale local match",
              explicit_movie["tmdb_id"], 608)
        check("explicit metadata attaches its own file",
              explicit_movie["file_path"], str(explicit_file.resolve()))
        check("a stale local match is not modified",
              library.get_movie(conn, slow_movie["id"])["file_path"],
              str(slow_file))

        # ---------------------------------------------------------------- path safety
        section("path safety")
        check("a path outside the movie root is refused",
              client.post("/api/import/confirm", json={
                  "items": [{"path": str(outside_file), "kind": "movie",
                             "tmdb_id": 603}],
              }).status_code, 400)
        check("a symlink escaping the movie root is refused",
              client.post("/api/import/confirm", json={
                  "items": [{"path": str(outside_link), "kind": "movie",
                             "tmdb_id": 603}],
              }).status_code, 400)
        check("a series file cannot be imported as a movie",
              client.post("/api/import/confirm", json={
                  "items": [{"path": str(import_one), "kind": "movie",
                             "tmdb_id": 603}],
              }).status_code, 400)
        check("a non-video file is refused",
              client.post("/api/import/confirm", json={
                  "items": [{"path": str(text_file), "kind": "movie",
                             "tmdb_id": 603}],
              }).status_code, 400)
        check("an unknown import kind is refused",
              client.post("/api/import/confirm", json={
                  "items": [{"path": str(matrix_file), "kind": "music"}],
              }).status_code, 400)
        check("a missing path is refused",
              client.post("/api/import/confirm", json={
                  "items": [{"path": "", "kind": "movie"}],
              }).status_code, 400)
        check("an unknown quality profile is refused",
              client.post("/api/import/confirm", json={
                  "items": [{"path": str(loose_file), "kind": "movie",
                             "tmdb_id": 604, "quality_profile_id": 999999}],
              }).status_code, 400)
        check_true("refused imports leave the media untouched",
                   all(path.exists() for path in all_media))

        # ------------------------------------------------------------ series import
        section("series import and monitoring modes")
        missing_seasons = client.post("/api/import/confirm", json={
            "items": [{"path": str(import_two), "kind": "episode",
                       "tmdb_id": 9005}],
            "quality_profile_id": profile_720,
            "series_mode": "manual", "seasons": [],
        })
        check("manual mode without a season is refused",
              missing_seasons.status_code, 400)
        check("refused manual mode does not create the series",
              library.list_series(conn, text="Import Show"), [])

        response = client.post("/api/import/confirm", json={
            "items": [{"path": str(import_one), "kind": "episode",
                       "tmdb_id": 9005, "season": 1, "episode": 1}],
            "quality_profile_id": profile_720,
            "series_mode": "manual", "seasons": [1],
        })
        data = response.get_json()
        check("series import succeeds", response.status_code, 200)
        imported_series = library.get_series(conn, data["item"]["series_id"])
        check("series import is monitored", imported_series["monitored"], 1)
        check("series profile comes from the payload",
              imported_series["quality_profile_id"], profile_720)
        first_episode = library.get_episode(conn, data["item"]["episode_id"])
        check("series episode file is stored", first_episode["file_path"],
              str(import_one.resolve()))
        check("series episode resolution is probed", first_episode["resolution"],
              "1080p")
        imported_episodes = library.series_episodes(conn, imported_series["id"])
        states = {(ep["season"], ep["episode"]): ep["monitored"]
                  for ep in imported_episodes}
        check("manual season 1 episode 1 is monitored", states[(1, 1)], 1)
        check("manual season 1 episode 2 is monitored", states[(1, 2)], 1)
        check("manual season 2 is not monitored", states[(2, 1)], 0)
        check("bworker work exists for monitored episodes",
              count(conn, "SELECT COUNT(*) FROM bworker_work WHERE work_key = ?",
                    (f"episode:{first_episode['id']}:search",)), 1)
        check("manual mode is idempotent", client.post(
            "/api/import/confirm", json={
                "items": [{"path": str(import_one), "kind": "episode",
                           "tmdb_id": 9005}],
                "quality_profile_id": profile_720,
                "series_mode": "manual", "seasons": [1],
            }).get_json()["item"]["already_imported"], True)

        scan.import_untracked(            conn, path=import_two, kind="episode", tmdb_id=9005,
            profile_id=profile_720, series_mode="all")
        states = {(ep["season"], ep["episode"]): ep["monitored"]
                  for ep in library.series_episodes(conn, imported_series["id"])}
        check("all mode monitors every regular episode",
              states, {(1, 1): 1, (1, 2): 1, (2, 1): 1})

        scan.import_untracked(
            conn, path=import_two, kind="episode", tmdb_id=9005,
            profile_id=profile_720, series_mode="none")
        states = {(ep["season"], ep["episode"]): ep["monitored"]
                  for ep in library.series_episodes(conn, imported_series["id"])}
        check("none mode monitors no episode",
              states, {(1, 1): 0, (1, 2): 0, (2, 1): 0})

        scan.import_untracked(
            conn, path=import_two, kind="episode", tmdb_id=9005,
            profile_id=profile_720, series_mode="future")
        states = {(ep["season"], ep["episode"]): ep["monitored"]
                  for ep in library.series_episodes(conn, imported_series["id"])}
        check("future mode monitors only the unaired episode",
              states, {(1, 1): 0, (1, 2): 0, (2, 1): 1})
        check("a conflicting identity for the imported episode is refused",
              client.post("/api/import/confirm", json={
                  "items": [{"path": str(import_one), "kind": "episode",
                             "tmdb_id": 9004}],
              }).status_code, 409)
        check_true("series files are still on disk",
                   import_one.exists() and import_two.exists()
                   and import_future.exists())

        # ------------------------------------------------------ manual movie metadata
        section("manual movie metadata")
        response = client.get(
            f"/api/import/metadata/movies/{slow_movie['id']}/search?q=Matrix")
        data = response.get_json()
        check("movie-specific search succeeds", response.status_code, 200)
        check("movie-specific search returns the current row",
              data["movie"]["id"], slow_movie["id"])
        check("movie search finds the replacement", data["results"][0]["tmdb_id"], 603)
        check("movie search sees existing library ownership",
              data["results"][0]["in_library"], True)
        check("search for another movie",
              client.get(
                  f"/api/import/metadata/movies/{slow_movie['id']}/search?q=Another"
              ).get_json()["results"][0]["tmdb_id"], 606)
        check("movie-specific search for a missing movie is 404",
              client.get("/api/import/metadata/movies/999999/search?q=x")
              .status_code, 404)

        response = client.post(
            f"/api/import/metadata/movies/{slow_movie['id']}",
            json={"tmdb_id": 606})
        data = response.get_json()
        check("manual movie replacement succeeds", response.status_code, 200)
        check("manual movie replacement reports change", data["changed"], True)
        updated_movie = library.get_movie(conn, slow_movie["id"])
        check("manual replacement updates the TMDB id",
              updated_movie["tmdb_id"], 606)
        check("manual replacement updates the title",
              updated_movie["title"], "Another Movie")
        check("manual replacement updates the overview",
              updated_movie["overview"], "The corrected identity.")
        check("manual replacement updates the IMDb id",
              updated_movie["imdb_id"], "tt0606")
        check("manual replacement keeps the file",
              updated_movie["file_path"], str(slow_file))
        check("manual replacement keeps monitoring",
              updated_movie["monitored"], 0)
        check("manual replacement keeps the quality profile",
              updated_movie["quality_profile_id"], profile_720)
        download = conn.execute(
            "SELECT status FROM downloads WHERE movie_id = ?",
            (slow_movie["id"],),
        ).fetchone()
        check("manual replacement keeps the download row",
              download["status"], "downloading")
        check("manual replacement records history", count(
            conn,
            "SELECT COUNT(*) FROM history WHERE event = 'metadata' AND movie_id = ?",
            (slow_movie["id"],)), 1)

        repeated = client.post(
            f"/api/import/metadata/movies/{slow_movie['id']}",
            json={"tmdb_id": 606}).get_json()
        check("re-applying the same movie identity reports no change",
              repeated["changed"], False)
        refused = client.post(
            f"/api/import/metadata/movies/{slow_movie['id']}",
            json={"tmdb_id": 604})
        check("refuses a movie TMDB id already in the library",
              refused.status_code, 409)
        check("refused movie id did not change the row",
              library.get_movie(conn, slow_movie["id"])["tmdb_id"], 606)
        check("refuses a TMDB id without a value",
              client.post(f"/api/import/metadata/movies/{slow_movie['id']}",
                          json={}).status_code, 400)
        check("movie metadata apply for a missing row is 404",
              client.post("/api/import/metadata/movies/999999",
                          json={"tmdb_id": 606}).status_code, 404)

        # ----------------------------------------------------- manual series metadata
        section("manual series metadata")
        response = client.get(
            f"/api/import/metadata/series/{supernatural['id']}/search?q=Dark")
        data = response.get_json()
        check("series-specific search succeeds", response.status_code, 200)
        check("series-specific search returns the current row",
              data["series"]["id"], supernatural["id"])
        check("series search finds the replacement",
              data["results"][0]["tmdb_id"], 9001)
        check("series preview succeeds",
              client.get(
                  "/api/import/metadata/preview?kind=series&tmdb_id=9001"
              ).get_json()["match"]["title"], "Dark Hearts")

        response = client.post(
            f"/api/import/metadata/series/{supernatural['id']}",
            json={"tmdb_id": 9001})
        data = response.get_json()
        check("manual series replacement succeeds", response.status_code, 200)
        check("manual series replacement refreshes episodes",
              data["episodes_refreshed"], True)
        updated_series = library.get_series(conn, supernatural["id"])
        check("manual series replacement updates the TMDB id",
              updated_series["tmdb_id"], 9001)
        check("manual series replacement updates the title",
              updated_series["title"], "Dark Hearts")
        refreshed_episodes = library.series_episodes(conn, supernatural["id"])
        check("manual series replacement updates the episode list",
              len(refreshed_episodes), 3)
        preserved = next(ep for ep in refreshed_episodes
                         if (ep["season"], ep["episode"]) == (1, 1))
        check("manual series replacement keeps the attached episode file",
              preserved["file_path"], str(supernatural_file))
        check("manual series replacement refreshes episode metadata",
              preserved["title"], "Dark One")

        refused = client.post(
            f"/api/import/metadata/series/{supernatural['id']}",
            json={"tmdb_id": 9004})
        check("refuses a series TMDB id already in the library",
              refused.status_code, 409)
        check("refused series id did not change the row",
              library.get_series(conn, supernatural["id"])["tmdb_id"], 9001)

        # --------------------------------------------------------------- media intact
        section("media untouched")
        check_true("all imported and rejected files still exist",
                   all(path.exists() for path in all_media))
        check_true("the escaped symlink was never followed", outside_link.is_symlink()
                   and outside_file.exists())
        check("no file was created inside an import request",
              count(conn, "SELECT COUNT(*) FROM scan_results WHERE status = 'imported'"),
              4)

        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(_TMP, ignore_errors=True)

    failures = [row for row in _results if not row[0]]
    print(f"\n{len(_results) - len(failures)}/{len(_results)} checks passed")
    if failures:
        for _ok, label, detail in failures:
            print(f"  FAIL {label}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
