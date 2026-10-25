#!/usr/bin/env python3
"""Tests for Explore Movies / Explore Series (agent A11).

A local fixture server stands in for TMDB; no test touches the internet, the
user's playlists or the user's media. Run with:

    .venv/bin/python tests/explore.py
"""

from __future__ import annotations

import json
import os
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

_TMP = Path(tempfile.mkdtemp(prefix="iptv-explore-test-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_TMP / "media")
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"

TMDB_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.explore-test-token"

# ---------------------------------------------------------------- fixture data
MOVIE_603 = {
    "id": 603,
    "title": "The Matrix",
    "original_title": "The Matrix",
    "release_date": "1999-03-31",
    "overview": "A hacker learns the truth.",
    "poster_path": "/matrix.jpg",
    "backdrop_path": "/matrix-bg.jpg",
    "vote_average": 8.7,
    "original_language": "en",
    "media_type": "movie",
}
MOVIE_604 = {
    "id": 604,
    "title": "A Unique Talent",
    "original_title": "Un talento único",
    "release_date": "2026-01-15",
    "overview": "A localised title.",
    "poster_path": None,
    "backdrop_path": None,
    "vote_average": 7.0,
    "original_language": "es",
    "media_type": "movie",
}
MOVIE_614 = {
    "id": 614,
    "title": "Future Movie",
    "original_title": "Future Movie",
    "release_date": "2099-01-01",
    "overview": "Not released yet.",
    "poster_path": "/future.jpg",
    "backdrop_path": None,
    "vote_average": 6.0,
    "original_language": "en",
    "media_type": "movie",
}
SERIES_9001 = {
    "id": 9001,
    "name": "Dark Hearts",
    "original_name": "Dark Hearts",
    "first_air_date": "2023-01-01",
    "overview": "A dark story.",
    "poster_path": "/dark.jpg",
    "backdrop_path": "/dark-bg.jpg",
    "vote_average": 7.5,
    "original_language": "en",
    "media_type": "tv",
}
SERIES_9002 = {
    "id": 9002,
    "name": "Supernatural",
    "original_name": "Supernatural",
    "first_air_date": "2005-09-13",
    "overview": "Brothers.",
    "poster_path": "/spn.jpg",
    "backdrop_path": None,
    "vote_average": 8.0,
    "original_language": "en",
    "media_type": "tv",
}

FEED_RESULTS = {
    "/trending/movie/day": [MOVIE_603, MOVIE_604],
    "/trending/movie/week": [MOVIE_604],
    "/movie/popular": [MOVIE_603],
    "/movie/top_rated": [MOVIE_604],
    "/movie/upcoming": [MOVIE_614],
    "/movie/now_playing": [MOVIE_603],
    "/trending/tv/day": [SERIES_9001, SERIES_9002],
    "/trending/tv/week": [SERIES_9002],
    "/tv/popular": [SERIES_9001],
    "/tv/top_rated": [SERIES_9002],
    "/tv/on_the_air": [SERIES_9001],
    "/tv/airing_today": [SERIES_9002],
}

MOVIE_DETAILS = {
    603: {
        **MOVIE_603,
        "external_ids": {"imdb_id": "tt0133093"},
        "alternative_titles": {"titles": []},
        "runtime": 136,
        "genres": [{"name": "Action"}, {"name": "Science Fiction"}],
        "status": "Released",
    },
    604: {
        **MOVIE_604,
        "external_ids": {"imdb_id": "tt0604"},
        "alternative_titles": {"titles": [{"title": "Un talento único"}]},
        "runtime": 101,
        "genres": [{"name": "Comedy"}],
        "status": "Released",
    },
}

SERIES_DETAILS = {
    9001: {
        **SERIES_9001,
        "external_ids": {"tvdb_id": None},
        "alternative_titles": {"results": []},
        "genres": [{"name": "Drama"}],
        "status": "Ended",
        "seasons": [{"season_number": 1, "episode_count": 1,
                     "air_date": "2023-01-01"}],
    },
    9002: {
        **SERIES_9002,
        "external_ids": {"tvdb_id": None},
        "alternative_titles": {"results": []},
        "genres": [{"name": "Drama"}],
        "status": "Ended",
        "seasons": [{"season_number": 1, "episode_count": 1,
                     "air_date": "2005-09-13"}],
    },
}


# ------------------------------------------------------------------- server
class TMDBHandler(BaseHTTPRequestHandler):
    """A tiny stand-in for api.themoviedb.org/3."""

    hits: dict[str, int] = {}
    fail_feeds: set[str] = set()
    fail_once: set[str] = set()
    auth: list[str | None] = []

    def log_message(self, *args) -> None:  # keep test output readable
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/tmdb/3/"):
            self.send_error(404)
            return
        sub = parsed.path[len("/tmdb/3"):]
        type(self).hits[sub] = type(self).hits.get(sub, 0) + 1
        type(self).auth.append(self.headers.get("Authorization"))

        if sub in type(self).fail_feeds:
            self.send_error(500)
            return
        if sub in type(self).fail_once:
            type(self).fail_once.discard(sub)
            self.send_error(500)
            return

        results = FEED_RESULTS.get(sub)
        if results is not None:
            self._json({"page": 1, "results": results, "total_results": len(results)})
            return

        if sub.startswith("/movie/"):
            try:
                tmdb_id = int(sub.rsplit("/", 1)[-1])
            except ValueError:
                tmdb_id = None
            details = MOVIE_DETAILS.get(tmdb_id)
            if details is not None:
                self._json(details)
                return
        if sub.startswith("/tv/"):
            parts = sub.split("/")
            try:
                tmdb_id = int(parts[2])
            except (IndexError, ValueError):
                tmdb_id = None
            if len(parts) == 3 and tmdb_id in SERIES_DETAILS:
                self._json(SERIES_DETAILS[tmdb_id])
                return
            if len(parts) == 5 and parts[3] == "season" and tmdb_id:
                self._json({"season_number": int(parts[4]), "episodes": []})
                return

        self.send_error(404)

    def _json(self, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), TMDBHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ------------------------------------------------------------------- checks
_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def raises(fn, exc: type[BaseException] = Exception) -> str:
    try:
        fn()
    except exc:
        return "raised"
    except Exception as error:  # noqa: BLE001 - report the wrong type
        return f"wrong exception {type(error).__name__}"
    return "no exception"


def main() -> int:
    server, base = start_server()
    os.environ["IPTV_CHARTS_TMDB_BASE"] = f"{base}/tmdb/3"

    # Import after the environment points at this test's temp dirs and fixture
    # server, exactly like the other suites do.
    from iptv_charts import db, items, metadata, sources
    from iptv_charts.app import create_app
    from iptv_charts.clients import tmdb
    from iptv_charts.explore_web import bp as explore_bp

    app = create_app()
    # app.py registration is integration-owned; register here only so this
    # suite exercises the real blueprint.
    if "explore" not in app.blueprints:
        app.register_blueprint(explore_bp)
    client = app.test_client()

    # Before any playlist exists, the picker says so instead of breaking.
    empty_page = client.get("/explore/movies")
    empty_body = empty_page.get_data(as_text=True)
    check("explore page renders with no playlists", empty_page.status_code, 200)
    check_true("empty playlist picker is explained",
               "No playlists configured" in empty_body)
    check_true("playlist search disabled without playlists",
               'id="explore-playlist-search"' in empty_body
               and "disabled" in empty_body.split('id="explore-playlist-search"')[1][:200])

    conn = db.connect()
    try:
        db.set_setting(conn, metadata.TMDB_KEY, TMDB_TOKEN)
        db.set_setting(conn, metadata.LANGUAGE, metadata.DEFAULT_LANGUAGE)

        profile_row = conn.execute(
            "SELECT id FROM quality_profiles ORDER BY id LIMIT 1"
        ).fetchone()
        check_true("builtin quality profile exists", profile_row is not None)
        profile_id = profile_row["id"]

        # A playlist with both catalog kinds. ``remote_id`` and ``logo`` are
        # deliberately provider URLs: the playlist search must not echo them.
        source_id = sources.create(
            conn, "m3u", "Explore playlist",
            m3u_url="http://127.0.0.1:9/list.m3u",
        )
        conn.execute(
            "INSERT INTO items (source_id, kind, remote_id, name, logo, "
            "container_ext, rating, plot, release_date, tmdb_id, added_ts) "
            "VALUES (?, 'vod', ?, ?, ?, 'mkv', 8.7, ?, '1999-03-31', '603', 0)",
            (source_id, "http://secret.example/stream/603",
             "The Matrix (1999)", "http://secret.example/logo.png",
             "A hacker learns the truth."),
        )
        conn.execute(
            "INSERT INTO items (source_id, kind, remote_id, name, logo, "
            "container_ext, rating, plot, release_date, tmdb_id, added_ts) "
            "VALUES (?, 'vod', ?, ?, ?, 'mp4', 7.1, ?, '2003-05-15', '604', 0)",
            (source_id, "http://secret.example/stream/604",
             "The Matrix Reloaded (2003)", "http://secret.example/logo2.png",
             "The second one."),
        )
        conn.execute(
            "INSERT INTO items (source_id, kind, remote_id, name, logo, "
            "container_ext, rating, plot, release_date, tmdb_id, added_ts) "
            "VALUES (?, 'series', ?, ?, ?, 'mp4', 7.5, ?, '2023-01-01', '9001', 0)",
            (source_id, "http://secret.example/series/9001",
             "Dark Hearts (2023)", "http://secret.example/dark.png",
             "A dark story."),
        )
        conn.commit()
    finally:
        conn.close()

    # ---------------------------------------------------------- client feeds
    section("TMDB client discovery feeds")
    with tmdb.TMDBClient(TMDB_TOKEN) as api:
        week = api.movie_feed("trending", "week")
        check("movie trending week count", len(week), 1)
        check("movie trending week title", week[0]["title"], "A Unique Talent")
        check("movie feed uses summary shape", week[0]["kind"], "movie")
        check("movie feed adds media_type", week[0]["media_type"], "movie")
        check("movie feed keeps poster path", week[0]["poster_path"], None)
        check("movie feed year", week[0]["year"], 2026)

        day = api.movie_feed("trending", "day")
        check("movie trending day count", len(day), 2)
        check("movie trending day first title", day[0]["title"], "The Matrix")

        top_rated = api.movie_feed("top_rated")
        check("movie top rated", [row["tmdb_id"] for row in top_rated], [604])

        tv_day = api.series_feed("trending", "day")
        check("series trending day count", len(tv_day), 2)
        check("series feed kind is tv", tv_day[0]["kind"], "tv")
        check("series feed media_type", tv_day[0]["media_type"], "tv")
        check("series feed release date", tv_day[0]["release_date"], "2023-01-01")

        airing = api.series_feed("airing_today")
        check("series airing today title", airing[0]["title"], "Supernatural")

        check(
            "unknown movie feed rejected",
            raises(lambda: api.movie_feed("bogus"), ValueError),
            "raised",
        )
        check(
            "unknown window rejected",
            raises(lambda: api.series_feed("trending", "month"), ValueError),
            "raised",
        )

    check_true(
        "TMDB feed requests use bearer auth",
        any(auth == f"Bearer {TMDB_TOKEN}" for auth in TMDBHandler.auth),
    )

    # --------------------------------------------------------------- caching
    section("metadata feed cache")
    conn = db.connect()
    try:
        metadata.clear_cache(conn)
        before = TMDBHandler.hits.get("/movie/popular", 0)
        first = metadata.movie_feed(conn, "popular")
        second = metadata.movie_feed(conn, "popular")
        check("first popular feed returned", [row["tmdb_id"] for row in first], [603])
        check("second popular feed matches", second, first)
        check(
            "second request served from cache",
            TMDBHandler.hits.get("/movie/popular", 0) - before,
            1,
        )

        key = "tmdb:movie:feed:en-US:popular:day"
        check_true(
            "feed cached under language key",
            metadata.cache_get(conn, key, metadata.FEED_TTL) is not None,
        )

        conn.execute(
            "UPDATE metadata_cache SET fetched_at = ? WHERE key = ?",
            (int(time.time()) - metadata.FEED_TTL - 1, key),
        )
        conn.commit()
        before = TMDBHandler.hits.get("/movie/popular", 0)
        metadata.movie_feed(conn, "popular")
        check(
            "expired cache refetches",
            TMDBHandler.hits.get("/movie/popular", 0) - before,
            1,
        )

        db.set_setting(conn, metadata.LANGUAGE, "fr-FR")
        metadata.movie_feed(conn, "popular")
        check_true(
            "language is part of the cache key",
            metadata.cache_get(
                conn, "tmdb:movie:feed:fr-FR:popular:day", metadata.FEED_TTL
            ) is not None,
        )
        db.set_setting(conn, metadata.LANGUAGE, metadata.DEFAULT_LANGUAGE)

        metadata.movie_feed(conn, "popular", "week")
        check(
            "non-trending window is normalised",
            metadata.cache_get(
                conn, "tmdb:movie:feed:en-US:popular:week", metadata.FEED_TTL
            ),
            None,
        )

        metadata.clear_cache(conn)
        metadata.movie_feed(conn, "trending", "day")
        metadata.movie_feed(conn, "trending", "week")
        check_true(
            "trending day cached separately",
            metadata.cache_get(
                conn, "tmdb:movie:feed:en-US:trending:day", metadata.FEED_TTL
            ) is not None,
        )
        check_true(
            "trending week cached separately",
            metadata.cache_get(
                conn, "tmdb:movie:feed:en-US:trending:week", metadata.FEED_TTL
            ) is not None,
        )
    finally:
        conn.close()

    # --------------------------------------------------------- provider error
    section("provider failures and retry")
    conn = db.connect()
    try:
        metadata.clear_cache(conn)
        TMDBHandler.fail_once.add("/movie/top_rated")
        before = TMDBHandler.hits.get("/movie/top_rated", 0)
        recovered = metadata.movie_feed(conn, "top_rated")
        check("transient 500 is retried", TMDBHandler.hits["/movie/top_rated"] - before, 2)
        check("retry returns the feed", [row["tmdb_id"] for row in recovered], [604])

        metadata.clear_cache(conn)
        TMDBHandler.fail_feeds.add("/tv/on_the_air")
        response = client.get("/api/explore/series?feed=on_the_air")
        check("provider failure maps to 502", response.status_code, 502)
        check("provider failure reports ok=false", response.get_json()["ok"], False)
        check(
            "failed feed is not cached",
            metadata.cache_get(
                conn, "tmdb:series:feed:en-US:on_the_air:day", metadata.FEED_TTL
            ),
            None,
        )
        TMDBHandler.fail_feeds.discard("/tv/on_the_air")

        metadata.clear_cache(conn)
        db.set_setting(conn, metadata.TMDB_KEY, "")
        db.set_setting(conn, metadata.TMDB_LEGACY_KEY, "")
        response = client.get("/api/explore/movies?feed=popular")
        check("missing TMDB key maps to 409", response.status_code, 409)
        check("missing key response is JSON", response.get_json()["ok"], False)
        check(
            "missing key raises TMDBKeyError",
            raises(lambda: metadata.series_feed(conn, "popular"), tmdb.TMDBKeyError),
            "raised",
        )
        page = client.get("/explore/movies")
        body = page.get_data(as_text=True)
        check("page still renders without TMDB", page.status_code, 200)
        check_true("page explains the missing token", "TMDB API Read Access Token" in body)

        db.set_setting(conn, metadata.TMDB_KEY, TMDB_TOKEN)
    finally:
        conn.close()

    # ----------------------------------------------------------------- pages
    section("page rendering")
    movie_page = client.get("/explore/movies")
    movie_body = movie_page.get_data(as_text=True)
    check("movie explore page status", movie_page.status_code, 200)
    for expected in (
        "Explore Movies", "Trending", "Popular", "Top Rated", "Upcoming",
        "Search in a playlist", "Add to collection", "explore.css",
        "explore.js", "explore-availability", "Explore Series",
        "Explore playlist",
    ):
        check_true(f"movie page contains {expected!r}", expected in movie_body)
    check("movie page has no Now Playing tab", "Now Playing" in movie_body, False)
    check_true("movie page data kind", 'data-kind="movie"' in movie_body)
    check_true(
        "movie page renders profile option",
        f'<option value="{profile_id}"' in movie_body,
    )
    check_true("movie page links Home", 'href="/"' in movie_body)

    series_page = client.get("/explore/series")
    series_body = series_page.get_data(as_text=True)
    check("series explore page status", series_page.status_code, 200)
    for expected in (
        "Explore Series", "Trending", "On the Air", "Airing Today",
        "Monitor episodes", "When a new season appears",
        "explore-monitor-mode", "explore-monitor-new-seasons",
        "Explore Movies", "explore.css", "explore.js",
    ):
        check_true(f"series page contains {expected!r}", expected in series_body)
    check("series page has no movie availability field",
          "explore-availability" in series_body, False)
    check_true("series page data kind", 'data-kind="series"' in series_body)
    check_true("series page links Home", 'href="/"' in series_body)

    for label, body in (("movie", movie_body), ("series", series_body)):
        check(f"{label} page has no external scripts", 'src="http' in body, False)
        check(f"{label} page has no external styles", 'href="http' in body, False)
        check(f"{label} page has no CDN reference", "cdn." in body.lower(), False)

    # ------------------------------------------------------------- feed APIs
    section("feed JSON API")
    movie_feeds = (
        ("trending", "day"),
        ("trending", "week"),
        ("popular", "day"),
        ("top_rated", "day"),
        ("upcoming", "day"),
        ("now_playing", "day"),
    )
    for feed, window in movie_feeds:
        response = client.get(
            f"/api/explore/movies?feed={feed}&window={window}"
        )
        payload = response.get_json()
        label = f"{feed}/{window}"
        check(f"movie {label} status", response.status_code, 200)
        check(f"movie {label} ok", payload.get("ok"), True)
        check(f"movie {label} kind", payload.get("kind"), "movie")
        check(f"movie {label} feed echoed", payload.get("feed"), feed)
        check_true(f"movie {label} has items", payload.get("items"))
        item = payload["items"][0]
        check(f"movie {label} item kind", item.get("kind"), "movie")
        check(f"movie {label} item media_type", item.get("media_type"), "movie")
        if item.get("poster_path"):
            check_true(
                f"movie {label} poster uses local proxy",
                (item.get("poster") or "").startswith("/api/images/tmdb/w342/"),
            )
        else:
            check(f"movie {label} has no poster", item.get("poster"), None)
        check_true(
            f"movie {label} no provider URL",
            "themoviedb.org" not in response.get_data(as_text=True),
        )
        check(
            f"movie {label} window echo",
            payload.get("window"),
            window if feed == "trending" else "day",
        )

    series_feeds = (
        ("trending", "day"),
        ("trending", "week"),
        ("popular", "day"),
        ("top_rated", "day"),
        ("on_the_air", "day"),
        ("airing_today", "day"),
    )
    for feed, window in series_feeds:
        response = client.get(
            f"/api/explore/series?feed={feed}&window={window}"
        )
        payload = response.get_json()
        label = f"{feed}/{window}"
        check(f"series {label} status", response.status_code, 200)
        check(f"series {label} ok", payload.get("ok"), True)
        check(f"series {label} kind", payload.get("kind"), "series")
        check_true(f"series {label} has items", payload.get("items"))
        item = payload["items"][0]
        check(f"series {label} item kind", item.get("kind"), "tv")
        check(f"series {label} item media_type", item.get("media_type"), "tv")
        check_true(
            f"series {label} poster uses local proxy",
            (item.get("poster") or "").startswith("/api/images/tmdb/w342/"),
        )

    check(
        "unknown feed rejected",
        client.get("/api/explore/movies?feed=bogus").status_code,
        400,
    )
    check(
        "unknown window rejected",
        client.get("/api/explore/movies?feed=trending&window=month").status_code,
        400,
    )

    popular = client.get("/api/explore/movies?feed=popular").get_json()
    check("movie not yet in library", popular["items"][0]["library_url"], None)
    check("movie poster proxy", popular["items"][0]["poster"],
          "/api/images/tmdb/w342/matrix.jpg")
    check("movie backdrop proxy", popular["items"][0]["backdrop"],
          "/api/images/tmdb/w780/matrix-bg.jpg")

    # -------------------------------------------------- add to the collection
    section("add to collection payload")
    response = client.post("/api/movies", json={
        "tmdb_id": 603,
        "quality_profile_id": profile_id,
        "monitored": True,
        "search": False,
        "minimum_availability": "released",
    })
    check("explore movie add status", response.status_code, 201)
    movie = response.get_json()["movie"]
    check("explore movie add title", movie["title"], "The Matrix")

    response = client.post("/api/movies", json={
        "tmdb_id": 603,
        "quality_profile_id": profile_id,
        "monitored": True,
        "search": False,
        "minimum_availability": "released",
    })
    check("duplicate movie add refused", response.status_code, 400)
    check_true(
        "duplicate error tells the user",
        "already in your library" in response.get_json()["error"],
    )

    response = client.post("/api/series", json={
        "tmdb_id": 9001,
        "quality_profile_id": profile_id,
        "monitored": True,
        "search": False,
        "monitor_mode": "all",
        "monitor_new_seasons": "all",
    })
    check("explore series add status", response.status_code, 201)
    series = response.get_json()["series"]
    check("explore series add title", series["title"], "Dark Hearts")

    popular = client.get("/api/explore/movies?feed=popular").get_json()
    check(
        "feed links to the existing movie",
        popular["items"][0]["library_url"],
        f"/movies/{movie['id']}",
    )
    series_popular = client.get("/api/explore/series?feed=popular").get_json()
    check(
        "feed links to the existing series",
        series_popular["items"][0]["library_url"],
        f"/series/{series['id']}",
    )

    # --------------------------------------------------------- playlist search
    section("playlist search")
    response = client.get(
        f"/api/explore/playlist-search?source_id={source_id}&kind=vod&q=Matrix"
    )
    payload = response.get_json()
    text = response.get_data(as_text=True)
    check("playlist movie search status", response.status_code, 200)
    check("playlist movie search ok", payload.get("ok"), True)
    check("playlist movie search total", payload.get("total"), 2)
    check("playlist movie search shown", payload.get("shown"), 2)
    check("playlist movie search has more", payload.get("has_more"), False)
    check("playlist source named", payload["source"]["name"], "Explore playlist")
    names = [item["name"] for item in payload["items"]]
    check("playlist movie names", names,
          ["The Matrix (1999)", "The Matrix Reloaded (2003)"])
    first = payload["items"][0]
    check("playlist movie year", first["year"], 1999)
    check("playlist movie category", first["category"], None)
    check_true(
        "playlist movie browse link",
        first["browse_url"].startswith(f"/sources/{source_id}/browse"),
    )
    check("playlist movie library link", first["library_url"], f"/movies/{movie['id']}")
    check("playlist result omits remote_id", "remote_id" in first, False)
    check("playlist result omits logo", "logo" in first, False)
    check_true("playlist search does not leak provider URLs",
               "secret.example" not in text)

    response = client.get(
        f"/api/explore/playlist-search?source_id={source_id}&kind=series&q=Dark"
    )
    payload = response.get_json()
    check("playlist series search status", response.status_code, 200)
    check("playlist series search count", len(payload["items"]), 1)
    check("playlist series search name", payload["items"][0]["name"],
          "Dark Hearts (2023)")
    check(
        "playlist series library link",
        payload["items"][0]["library_url"],
        f"/series/{series['id']}",
    )
    check_true(
        "playlist series browse link",
        payload["items"][0]["browse_url"].startswith(f"/sources/{source_id}/browse"),
    )

    response = client.get(
        f"/api/explore/playlist-search?source_id={source_id}&kind=vod&q=Dark"
    )
    check("playlist kind filter", response.get_json()["items"], [])
    check("playlist no-match total", response.get_json()["total"], 0)

    check(
        "playlist search requires a source",
        client.get("/api/explore/playlist-search?kind=vod&q=Matrix").status_code,
        400,
    )
    check(
        "playlist search requires a query",
        client.get(
            f"/api/explore/playlist-search?source_id={source_id}&kind=vod&q="
        ).status_code,
        400,
    )
    check(
        "playlist search rejects live kind",
        client.get(
            f"/api/explore/playlist-search?source_id={source_id}&kind=live&q=Matrix"
        ).status_code,
        400,
    )
    response = client.get(
        "/api/explore/playlist-search?source_id=999999&kind=vod&q=Matrix"
    )
    check("unknown playlist is 404", response.status_code, 404)
    check("unknown playlist ok=false", response.get_json()["ok"], False)

    response = client.get("/api/explore/sources")
    payload = response.get_json()
    check("sources endpoint status", response.status_code, 200)
    check("sources endpoint count", len(payload["sources"]), 1)
    check("sources endpoint name", payload["sources"][0]["name"], "Explore playlist")
    check("sources endpoint keys", set(payload["sources"][0]),
          {"id", "name", "kind"})

    # ------------------------------------------------------- item search shim
    conn = db.connect()
    try:
        result = items.query_items(conn, source_id, text="Reloaded", kind="vod")
        check("catalog search remains read-only", result["total"], 1)
    finally:
        conn.close()

    # --------------------------------------------------------------- results
    passed = sum(1 for ok, _, _ in _results if ok)
    failed = [(label, detail) for ok, label, detail in _results if not ok]
    print(f"\n{'=' * 60}")
    if failed:
        print(f"\033[31m{len(failed)} FAILED\033[0m, {passed} passed\n")
        for label, detail in failed:
            print(f"  \033[31m✗\033[0m {label}\n      {detail}")
        server.shutdown()
        return 1
    print(f"\033[32mall {passed} checks passed\033[0m")
    server.shutdown()
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
