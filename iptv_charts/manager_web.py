"""Server-rendered pages for the movie and series library."""

from __future__ import annotations

import calendar as calendar_module
import json
from datetime import date, timedelta

from flask import Blueprint, abort, g, render_template, request

from . import config, db, languages, library, metadata, quality, scan, scheduler

bp = Blueprint("manager", __name__)


def _conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _profiles(conn) -> list[dict]:
    return [
        quality.profile_summary(row) for row in conn.execute(
            "SELECT * FROM quality_profiles ORDER BY id"
        )
    ]


def _media_context() -> dict:
    return {
        "media_dir": str(config.media_dir()),
        "media_host_dir": config.media_host_dir(),
        "movies_dir": str(config.movie_library_dir()),
        "series_dir": str(config.series_library_dir()),
    }


def _movie_counts(conn) -> dict:
    row = db.get_one(
        conn,
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(monitored = 1), 0) AS monitored, "
        "COALESCE(SUM(file_path IS NULL), 0) AS missing, "
        "COALESCE(SUM(file_path IS NOT NULL), 0) AS downloaded "
        "FROM movies",
    )
    return dict(row)


def _series_counts(conn) -> dict:
    row = db.get_one(
        conn,
        "SELECT COUNT(*) AS total, COALESCE(SUM(monitored = 1), 0) AS monitored "
        "FROM series",
    )
    counts = dict(row)
    episodes = db.get_one(
        conn,
        "SELECT COUNT(*) AS total, COALESCE(SUM(file_path IS NOT NULL), 0) AS downloaded "
        "FROM series_episodes",
    )
    counts["episodes"] = episodes["total"]
    counts["downloaded_episodes"] = episodes["downloaded"]
    return counts


def _active_maps(conn) -> tuple[dict, dict]:
    rows = conn.execute(
        "SELECT * FROM downloads WHERE status IN ('queued', 'downloading', 'importing')"
    ).fetchall()
    by_movie: dict[int, dict] = {}
    by_episode: dict[int, dict] = {}
    for row in rows:
        item = dict(row)
        total = item.get("bytes_total")
        item["fraction"] = (item.get("bytes_done") or 0) / total if total else None
        if item.get("movie_id"):
            by_movie[int(item["movie_id"])] = item
        if item.get("episode_id"):
            by_episode[int(item["episode_id"])] = item
    return by_movie, by_episode


def _genres(value):
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _json_value(value):
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(value or "null")
    except (TypeError, ValueError):
        return None
    return parsed


def _decorate_movies(movies: list[dict], active: dict) -> list[dict]:
    for movie in movies:
        movie["poster"] = metadata.image_url("w342", movie.get("poster_path"))
        movie["backdrop"] = metadata.image_url("w780", movie.get("backdrop_path"))
        movie["genres"] = _genres(movie.get("genres"))
        movie["audio_languages"] = _json_value(movie.get("audio_languages")) or []
        movie["subtitle_languages"] = _json_value(movie.get("subtitle_languages")) or []
        movie["media"] = _json_value(movie.get("media_info")) or {}
        movie["available"] = library.movie_available(movie)
        movie["download"] = active.get(int(movie["id"]))
        if movie.get("file_path"):
            movie["status"] = "downloaded"
        elif movie["download"]:
            movie["status"] = "downloading"
        else:
            movie["status"] = "missing"
    return movies


def _decorate_series(items: list[dict]) -> list[dict]:
    for series in items:
        series["poster"] = metadata.image_url("w342", series.get("poster_path"))
        series["backdrop"] = metadata.image_url("w780", series.get("backdrop_path"))
        series["genres"] = _genres(series.get("genres"))
    return items


def _history_for(conn, *, movie_id: int | None = None,
                 series_id: int | None = None) -> list[dict]:
    if movie_id is not None:
        rows = conn.execute(
            "SELECT * FROM history WHERE movie_id = ? ORDER BY id DESC LIMIT 40",
            (movie_id,),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM history WHERE series_id = ? ORDER BY id DESC LIMIT 60",
            (series_id,),
        )
    return [dict(row) for row in rows]


# -------------------------------------------------------------------- movies
@bp.get("/movies")
def movies():
    conn = _conn()
    query = (request.args.get("q") or "").strip()[:120]
    status = request.args.get("status") or ""
    if status not in {"", "monitored", "missing", "downloaded"}:
        status = ""
    rows = library.list_movies(conn, text=query, status=status)
    active, _ = _active_maps(conn)
    return render_template(
        "movies.html",
        movies=_decorate_movies(rows, active),
        counts=_movie_counts(conn),
        profiles=_profiles(conn),
        q=query,
        status=status,
        tmdb_configured=metadata.has_tmdb(conn),
        **_media_context(),
    )


@bp.get("/movies/<int:movie_id>")
def movie_detail(movie_id: int):
    conn = _conn()
    movie = library.get_movie(conn, movie_id)
    if movie is None:
        abort(404, description="That movie no longer exists.")
    active, _ = _active_maps(conn)
    _decorate_movies([movie], active)
    return render_template(
        "movie.html",
        movie=movie,
        profiles=_profiles(conn),
        status=library.movie_status(conn, movie),
        history=_history_for(conn, movie_id=movie_id),
        **_media_context(),
    )


# -------------------------------------------------------------------- series
@bp.get("/series")
def series():
    conn = _conn()
    query = (request.args.get("q") or "").strip()[:120]
    status = request.args.get("status") or ""
    if status not in {"", "monitored"}:
        status = ""
    rows = library.list_series(conn, text=query, status=status)
    active, _ = _active_maps(conn)
    return render_template(
        "series.html",
        series=_decorate_series(rows),
        counts=_series_counts(conn),
        profiles=_profiles(conn),
        q=query,
        status=status,
        tmdb_configured=metadata.has_tmdb(conn),
        **_media_context(),
    )


@bp.get("/series/<int:series_id>")
def series_detail(series_id: int):
    conn = _conn()
    item = library.get_series(conn, series_id)
    if item is None:
        abort(404, description="That series no longer exists.")
    episodes = library.series_episodes(conn, series_id)
    _, active = _active_maps(conn)
    today = date.today().isoformat()
    seasons: dict[int, list[dict]] = {}
    for episode in episodes:
        episode["download"] = active.get(int(episode["id"]))
        episode["audio_languages"] = _json_value(episode.get("audio_languages")) or []
        episode["subtitle_languages"] = _json_value(episode.get("subtitle_languages")) or []
        episode["media"] = _json_value(episode.get("media_info")) or {}
        if episode.get("file_path"):
            episode["status"] = "downloaded"
        elif episode["download"]:
            episode["status"] = "downloading"
        elif episode.get("air_date") and str(episode["air_date"]) > today:
            episode["status"] = "unaired"
        else:
            episode["status"] = "missing"
        seasons.setdefault(int(episode["season"]), []).append(episode)
    _decorate_series([item])
    return render_template(
        "series_detail.html",
        series=item,
        seasons=sorted(seasons.items()),
        profiles=_profiles(conn),
        history=_history_for(conn, series_id=series_id),
        **_media_context(),
    )


# ------------------------------------------------------------------ calendar
def _month(raw: str | None) -> tuple[date, date, str, str]:
    try:
        year, month = (int(part) for part in str(raw or "").split("-", 1))
        first = date(year, month, 1)
    except (TypeError, ValueError):
        first = date.today().replace(day=1)
    last = date(first.year, first.month, calendar_module.monthrange(first.year, first.month)[1])
    previous = (first - timedelta(days=1)).replace(day=1)
    following = (last + timedelta(days=1)).replace(day=1)
    return first, last, previous.strftime("%Y-%m"), following.strftime("%Y-%m")


@bp.get("/calendar")
def calendar_view():
    conn = _conn()
    first, last, previous, following = _month(request.args.get("month"))
    start, end = first.isoformat(), last.isoformat()
    episodes = conn.execute(
        """
        SELECT e.*, s.title AS series_title, s.poster_path AS series_poster
        FROM series_episodes e JOIN series s ON s.id = e.series_id
        WHERE e.air_date IS NOT NULL AND e.air_date BETWEEN ? AND ?
        ORDER BY e.air_date, s.title COLLATE NOCASE, e.season, e.episode
        """,
        (start, end),
    ).fetchall()
    movies = conn.execute(
        "SELECT * FROM movies WHERE release_date IS NOT NULL "
        "AND release_date BETWEEN ? AND ? ORDER BY release_date, title COLLATE NOCASE",
        (start, end),
    ).fetchall()
    days: dict[str, list[dict]] = {}
    for row in episodes:
        item = dict(row)
        item["kind"] = "episode"
        item["downloaded"] = bool(item.get("file_path"))
        item["poster"] = metadata.image_url("w92", item.get("series_poster"))
        days.setdefault(str(item["air_date"])[:10], []).append(item)
    for row in movies:
        item = dict(row)
        item["kind"] = "movie"
        item["downloaded"] = bool(item.get("file_path"))
        item["poster"] = metadata.image_url("w92", item.get("poster_path"))
        days.setdefault(str(item["release_date"])[:10], []).append(item)
    return render_template(
        "calendar.html",
        days=sorted(days.items()),
        month_label=first.strftime("%B %Y"),
        month_value=first.strftime("%Y-%m"),
        previous=previous,
        following=following,
    )


# -------------------------------------------------------------------- wanted
def _cutoff_movies(conn) -> list[dict]:
    profiles = {row["id"]: quality.Profile.from_row(row)
                for row in conn.execute("SELECT * FROM quality_profiles")}
    out = []
    for row in conn.execute(
        "SELECT * FROM movies WHERE monitored = 1 AND file_path IS NOT NULL "
        "ORDER BY title COLLATE NOCASE"
    ):
        profile = profiles.get(row["quality_profile_id"])
        if profile and profile.upgrades and not profile.cutoff_met(row["resolution"]):
            item = dict(row)
            item["poster"] = metadata.image_url("w92", item.get("poster_path"))
            out.append(item)
    return out


def _cutoff_episodes(conn) -> list[dict]:
    profiles = {row["id"]: quality.Profile.from_row(row)
                for row in conn.execute("SELECT * FROM quality_profiles")}
    series_profiles = {
        row["id"]: profiles.get(row["quality_profile_id"])
        for row in conn.execute("SELECT id, quality_profile_id FROM series")
    }
    out = []
    for row in conn.execute(
        """
        SELECT e.*, s.title AS series_title
        FROM series_episodes e JOIN series s ON s.id = e.series_id
        WHERE s.monitored = 1 AND e.monitored = 1 AND e.file_path IS NOT NULL
        ORDER BY s.title COLLATE NOCASE, e.season, e.episode
        """
    ):
        profile = series_profiles.get(row["series_id"])
        if profile and profile.upgrades and not profile.cutoff_met(row["resolution"]):
            out.append(dict(row))
    return out


@bp.get("/wanted")
def wanted():
    conn = _conn()
    movies = library.wanted_movies(conn)
    for movie in movies:
        movie["poster"] = metadata.image_url("w92", movie.get("poster_path"))
    episodes = library.wanted_episodes(conn)
    return render_template(
        "wanted.html",
        movies=movies,
        episodes=episodes,
        cutoff_movies=_cutoff_movies(conn),
        cutoff_episodes=_cutoff_episodes(conn),
        counts=_movie_counts(conn),
        series_counts=_series_counts(conn),
    )


# ------------------------------------------------------------------ activity
@bp.get("/activity")
def activity():
    conn = _conn()
    return render_template(
        "activity.html",
        tasks=scheduler.snapshot(conn),
    )


# ---------------------------------------------------------------------- scan
@bp.get("/scan")
def scan_page():
    conn = _conn()
    results = scan.results(conn)
    counts = {
        "pending": sum(1 for row in results if row["status"] == "pending"),
        "imported": sum(1 for row in results if row["status"] == "imported"),
        "ignored": sum(1 for row in results if row["status"] == "ignored"),
    }
    tasks = {row["name"]: row for row in scheduler.snapshot(conn)}
    return render_template(
        "scan.html",
        results=results,
        counts=counts,
        scan_task=tasks.get("scan_library"),
        media_host_dir=config.media_host_dir(),
    )


# ------------------------------------------------------------------ profiles
@bp.get("/settings/profiles")
def profiles_page():
    conn = _conn()
    profiles = _profiles(conn)
    usage = {
        row["id"]: row["n"]
        for row in conn.execute(
            "SELECT id, "
            "(SELECT COUNT(*) FROM movies m WHERE m.quality_profile_id = quality_profiles.id) + "
            "(SELECT COUNT(*) FROM series s WHERE s.quality_profile_id = quality_profiles.id) AS n "
            "FROM quality_profiles"
        )
    }
    return render_template(
        "profiles.html",
        profiles=profiles,
        usage=usage,
        resolutions=quality.RESOLUTIONS,
        sources=quality.SOURCE_CHOICES,
        audio_modes=quality.AUDIO_MODES,
        subtitle_modes=quality.SUBTITLE_MODES,
        languages=languages.COMMON_LANGUAGES,
    )


__all__ = ["bp"]
