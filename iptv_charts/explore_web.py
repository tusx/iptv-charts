"""Explore Movies and Explore Series pages and their read-only JSON APIs.

The pages are a thin server-rendered shell; the poster grids are filled by
``static/js/explore.js`` from the cached TMDB discovery feeds in
``metadata.py``. Nothing here writes to the library: the details dialog links
to the existing add endpoints (``POST /api/movies`` and ``POST /api/series``)
and the playlist lookup only reads the local catalog.

The blueprint is deliberately self-contained and is not registered in
``app.py`` yet. Integration adds:

    from .explore_web import bp as explore_bp
    app.register_blueprint(explore_bp)

No provider URL or credential is returned by these routes. Poster URLs point
at the local image proxy, and playlist results omit provider stream ids,
logos and request headers.
"""

from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, render_template, request, url_for

from . import db, items, metadata, quality
from .clients import tmdb

logger = logging.getLogger("iptv_charts.explore_web")

bp = Blueprint("explore", __name__)

#: Tabs shown on the page. The API also accepts ``now_playing`` for movies,
#: but the product tabs stop at Upcoming; the feed itself remains available.
MOVIE_TABS = (
    ("trending", "Trending"),
    ("popular", "Popular"),
    ("top_rated", "Top Rated"),
    ("upcoming", "Upcoming"),
)
SERIES_TABS = (
    ("trending", "Trending"),
    ("popular", "Popular"),
    ("top_rated", "Top Rated"),
    ("on_the_air", "On the Air"),
    ("airing_today", "Airing Today"),
)

#: Weekly trending is the more stable default for a poster grid.
DEFAULT_WINDOW = "week"

#: Playlist catalog kind per Explore section.
PLAYLIST_KINDS = {"movie": "vod", "series": "series"}

PLAYLIST_RESULT_LIMIT = 30


def _conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error(exc: Exception):
    if isinstance(exc, tmdb.TMDBKeyError):
        return _fail(str(exc), 409)
    if isinstance(exc, tmdb.TMDBError):
        return _fail(str(exc), 502)
    if isinstance(exc, ValueError):
        return _fail(str(exc), 400)
    logger.exception("explore request failed: %s", exc)
    return _fail("The request could not be completed. See the app log.", 500)


# ------------------------------------------------------------------- context
def _profiles(conn) -> list[dict]:
    return [
        quality.profile_summary(row)
        for row in conn.execute("SELECT * FROM quality_profiles ORDER BY id")
    ]


def _sources(conn) -> list[dict]:
    """Only the fields the picker needs; never URLs or credentials."""
    return [
        dict(row)
        for row in db.get_all(
            conn, "SELECT id, name, kind FROM sources ORDER BY name COLLATE NOCASE"
        )
    ]


def _page(kind: str):
    conn = _conn()
    is_movie = kind == "movie"
    return render_template(
        "explore_movies.html" if is_movie else "explore_series.html",
        kind=kind,
        feeds=MOVIE_TABS if is_movie else SERIES_TABS,
        default_window=DEFAULT_WINDOW,
        profiles=_profiles(conn),
        sources=_sources(conn),
        tmdb_configured=metadata.has_tmdb(conn),
        settings_url=url_for("settings"),
    )


# --------------------------------------------------------------------- pages
@bp.get("/explore/movies")
def movies_page():
    return _page("movie")


@bp.get("/explore/series")
def series_page():
    return _page("series")


# ----------------------------------------------------------------- feed data
def _library_map(conn, kind: str, tmdb_ids: list[int]) -> dict[int, int]:
    """TMDB id -> library row id for the titles already in the collection."""
    if not tmdb_ids:
        return {}
    table = "movies" if kind == "movie" else "series"
    placeholders = ",".join("?" for _ in tmdb_ids)
    mapping: dict[int, int] = {}
    for row in conn.execute(
        f"SELECT id, tmdb_id FROM {table} WHERE tmdb_id IN ({placeholders})",
        tmdb_ids,
    ):
        try:
            mapping[int(row["tmdb_id"])] = int(row["id"])
        except (TypeError, ValueError):
            continue
    return mapping


def _decorate_feed(conn, results: list[dict], kind: str) -> list[dict]:
    """Add local poster URLs and a link to an existing library item."""
    tmdb_ids: list[int] = []
    for item in results:
        try:
            tmdb_ids.append(int(item["tmdb_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    library = _library_map(conn, kind, tmdb_ids)

    decorated = []
    for item in results:
        entry = dict(item)
        entry["poster"] = metadata.image_url("w342", item.get("poster_path"))
        entry["backdrop"] = metadata.image_url("w780", item.get("backdrop_path"))
        try:
            tmdb_id = int(item["tmdb_id"])
        except (KeyError, TypeError, ValueError):
            tmdb_id = None
        library_id = library.get(tmdb_id) if tmdb_id is not None else None
        entry["library_id"] = library_id
        if library_id is None:
            entry["library_url"] = None
        elif kind == "movie":
            entry["library_url"] = url_for(
                "manager.movie_detail", movie_id=library_id
            )
        else:
            entry["library_url"] = url_for(
                "manager.series_detail", series_id=library_id
            )
        decorated.append(entry)
    return decorated


def _feed_payload(kind: str):
    conn = _conn()
    feed = (request.args.get("feed") or "popular").strip().lower()
    window = (request.args.get("window") or "day").strip().lower()
    allowed = metadata.MOVIE_FEEDS if kind == "movie" else metadata.SERIES_FEEDS
    if feed not in allowed:
        return _fail(f"Unknown {kind} feed.", 400)
    if window not in metadata.FEED_WINDOWS:
        return _fail("Unknown trending window.", 400)

    try:
        if kind == "movie":
            results = metadata.movie_feed(conn, feed, window)
        else:
            results = metadata.series_feed(conn, feed, window)
    except Exception as exc:  # provider/key/cache problems are expected
        return _error(exc)

    # Non-trending feeds have no window; report the normalised value the
    # cache actually uses so responses are stable and comparable.
    effective_window = window if feed == "trending" else "day"
    return jsonify({
        "ok": True,
        "kind": kind,
        "feed": feed,
        "window": effective_window,
        "items": _decorate_feed(conn, results, kind),
    })


@bp.get("/api/explore/movies")
def movies_feed():
    return _feed_payload("movie")


@bp.get("/api/explore/series")
def series_feed():
    return _feed_payload("series")


# ------------------------------------------------------------ playlist search
def _playlist_row(row, source_id: int, kind: str) -> dict:
    """A catalog item reduced to what is safe and useful in the dialog.

    ``remote_id`` is omitted on purpose: for M3U playlists it is the stream
    URL, which can contain provider credentials. Logos are omitted for the
    same reason (they are provider-hosted absolute URLs).
    """
    release = str(row.get("release_date") or "").strip()
    year = int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else None
    plot = str(row.get("plot") or "").strip()
    if len(plot) > 280:
        plot = plot[:279].rstrip() + "…"
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "kind": str(row["kind"]),
        "year": year,
        "release_date": release or None,
        "rating": row.get("rating"),
        "duration": row.get("duration"),
        "category": row.get("category_name"),
        "plot": plot,
        "browse_url": url_for(
            "browse", source_id=source_id, kind=kind, q=str(row["name"])
        ),
        "library_id": None,
        "library_url": None,
    }


def _attach_playlist_library(conn, rows: list[dict], kind: str) -> None:
    """Link a playlist result to its library item when TMDB ids agree."""
    if not rows:
        return
    item_ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in item_ids)
    item_tmdb: dict[int, int] = {}
    for row in conn.execute(
        f"SELECT id, tmdb_id FROM items WHERE id IN ({placeholders})", item_ids
    ):
        try:
            item_tmdb[int(row["id"])] = int(row["tmdb_id"])
        except (TypeError, ValueError):
            continue
    if not item_tmdb:
        return

    library = _library_map(conn, kind, sorted(set(item_tmdb.values())))
    if not library:
        return
    for row in rows:
        tmdb_id = item_tmdb.get(row["id"])
        library_id = library.get(tmdb_id) if tmdb_id is not None else None
        if library_id is None:
            continue
        row["library_id"] = library_id
        if kind == "movie":
            row["library_url"] = url_for(
                "manager.movie_detail", movie_id=library_id
            )
        else:
            row["library_url"] = url_for(
                "manager.series_detail", series_id=library_id
            )


@bp.get("/api/explore/playlist-search")
def playlist_search():
    conn = _conn()
    source_id = _int(request.args.get("source_id"))
    kind = (request.args.get("kind") or "").strip().lower()
    query = (request.args.get("q") or "").strip()[:120]

    if not source_id:
        return _fail("Choose a playlist first.")
    if kind not in ("vod", "series"):
        return _fail("Choose a playlist catalog kind (vod or series).")
    if not query:
        return _fail("Type a title to search.")

    source = db.get_one(
        conn, "SELECT id, name FROM sources WHERE id = ?", (source_id,)
    )
    if source is None:
        return _fail("That playlist no longer exists.", 404)

    try:
        result = items.query_items(
            conn, source_id, text=query, kind=kind, limit=PLAYLIST_RESULT_LIMIT
        )
        rows = [_playlist_row(row, source_id, kind) for row in result["items"]]
        _attach_playlist_library(conn, rows, "movie" if kind == "vod" else "series")
    except Exception as exc:
        return _error(exc)

    return jsonify({
        "ok": True,
        "source": {"id": int(source["id"]), "name": str(source["name"])},
        "kind": kind,
        "query": query,
        "items": rows,
        "total": result["total"],
        "shown": len(rows),
        "has_more": result["has_more"],
    })


@bp.get("/api/explore/sources")
def explore_sources():
    """The playlist picker source list. No URLs or credentials."""
    return jsonify({"ok": True, "sources": _sources(_conn())})


__all__ = ["bp"]
