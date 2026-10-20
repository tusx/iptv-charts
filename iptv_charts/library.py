"""The media library: movies, series, provider matching, grabs and imports.

This module owns the database rows and the filesystem side of an import. It
does not own the background process: downloads are queued by asking
``downloader`` (imported lazily to avoid a cycle), whose worker calls back into
``import_download`` when the bytes are on disk. Searches and grabs are invoked
by worker handlers, never by an HTTP request thread.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from . import config, connections, db, matching, metadata, naming, probe, quality
from .clients import xtream

logger = logging.getLogger("iptv_charts.library")

ACTIVE_STATUSES = ("queued", "downloading", "importing")


# --------------------------------------------------------------------- helpers
def _row(row: Any) -> dict | None:
    return dict(row) if row is not None else None


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    keys = row.keys() if hasattr(row, "keys") else row
    return row[key] if key in keys else default


def _now() -> int:
    return int(time.time())


def _today() -> str:
    return date.today().isoformat()


def _aired(air_date: str | None) -> bool:
    """An episode is searchable once its air date is today or in the past."""
    if not air_date:
        return True
    return str(air_date)[:10] <= _today()


def _future_or_unknown(air_date: str | None) -> bool:
    """Whether an episode is unaired (or has no known air date)."""
    if not air_date:
        return True
    return str(air_date)[:10] >= _today()


def movie_available(movie: Mapping[str, Any]) -> bool:
    """Radarr-style minimum availability for a movie.

    ``announced`` allows a search immediately; ``in_cinemas`` waits for the
    release date; ``released`` additionally waits for TMDB to mark it
    Released. A missing date is treated as unavailable for the two waiting
    modes rather than downloading a cinema recording by mistake.
    """
    mode = str(movie.get("minimum_availability") or "released").lower()
    if mode == "announced":
        return True
    release_date = str(movie.get("release_date") or "")[:10]
    if not release_date:
        return False
    if release_date > _today():
        return False
    if mode == "in_cinemas":
        return True
    status = str(movie.get("status") or "").lower()
    return not status or status == "released"


def _add_monitor_mode(value: Any) -> str:
    mode = str(value or "all").strip().lower()
    if mode not in {"all", "future", "first", "latest", "none"}:
        raise ValueError("Unknown monitor option.")
    return mode


def _monitor_new_seasons(value: Any) -> str:
    mode = str(value or "all").strip().lower()
    if mode not in {"all", "future", "none"}:
        raise ValueError("Monitor new seasons must be all, future or none.")
    return mode


def _monitor_episode_on_add(episode: Mapping[str, Any], add_mode: str,
                            latest_season: int | None) -> bool:
    season = int(episode["season"])
    if season == 0:
        return False
    if add_mode == "none":
        return False
    if add_mode == "first":
        return season == 1
    if add_mode == "latest":
        return latest_season is not None and season == latest_season
    if add_mode == "future":
        return _future_or_unknown(episode.get("air_date"))
    return True  # all


def _monitor_new_episode(episode: Mapping[str, Any], rule: str) -> bool:
    season = int(episode["season"])
    if season == 0:
        return False
    if rule == "none":
        return False
    if rule == "future":
        return _future_or_unknown(episode.get("air_date"))
    return True


def _json_list(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(list(value), ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _tier_label(profile: quality.Profile, release: Mapping[str, Any]) -> str | None:
    rank = profile.rank(release, allow_unknown_source=True)
    if rank is None or rank >= len(profile.tiers):
        return None
    return profile.tiers[rank].name


def add_history(conn, event: str, title: str, *, kind: str | None = None,
                movie_id: int | None = None, series_id: int | None = None,
                episode_id: int | None = None, download_id: int | None = None,
                message: str | None = None) -> None:
    conn.execute(
        "INSERT INTO history (event, kind, movie_id, series_id, episode_id, "
        "download_id, title, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (event, kind, movie_id, series_id, episode_id, download_id,
         title, message, _now()),
    )
    conn.commit()


def _template(conn, key: str, default: str, allowed: frozenset[str]) -> str:
    stored = db.get_setting(conn, key, default) or default
    try:
        return naming.validate_template(stored, allowed)
    except naming.TemplateError as exc:
        logger.warning("ignoring invalid naming template %s: %s", key, exc)
        return default


def movie_templates(conn) -> dict[str, str]:
    return {
        "folder": _template(conn, "movie_folder_template", naming.DEFAULT_MOVIE_FOLDER,
                            naming.MOVIE_FOLDER_TOKENS),
        "file": _template(conn, "movie_file_template", naming.DEFAULT_MOVIE_FILE,
                          naming.MOVIE_FILE_TOKENS),
    }


def series_templates(conn) -> dict[str, str]:
    return {
        "folder": _template(conn, "series_folder_template", naming.DEFAULT_SERIES_FOLDER,
                            naming.SERIES_FOLDER_TOKENS),
        "season": _template(conn, "season_folder_template", naming.DEFAULT_SEASON_FOLDER,
                            naming.SEASON_FOLDER_TOKENS),
        "file": _template(conn, "episode_file_template", naming.DEFAULT_EPISODE_FILE,
                          naming.EPISODE_FILE_TOKENS),
    }


def default_profile_id(conn) -> int | None:
    row = db.get_one(conn, "SELECT id FROM quality_profiles ORDER BY id LIMIT 1")
    return int(row["id"]) if row else None


def _profile_row(conn, profile_id: int | None) -> sqlite3.Row:
    row = None
    if profile_id:
        row = db.get_one(conn, "SELECT * FROM quality_profiles WHERE id = ?", (int(profile_id),))
    if row is None and profile_id:
        raise ValueError("That quality profile does not exist.")
    if row is None:
        candidate = default_profile_id(conn)
        if candidate is None:
            raise ValueError("Create a quality profile in Settings first.")
        row = db.get_one(conn, "SELECT * FROM quality_profiles WHERE id = ?", (candidate,))
    return row


def _profile(conn, profile_id: int | None) -> quality.Profile:
    return quality.Profile.from_row(_profile_row(conn, profile_id))


def active_download(conn, *, movie_id: int | None = None,
                    episode_id: int | None = None) -> dict | None:
    if movie_id is not None:
        row = db.get_one(
            conn,
            "SELECT * FROM downloads WHERE movie_id = ? AND status IN (?, ?, ?) "
            "ORDER BY id DESC LIMIT 1",
            (movie_id, *ACTIVE_STATUSES),
        )
    elif episode_id is not None:
        row = db.get_one(
            conn,
            "SELECT * FROM downloads WHERE episode_id = ? AND status IN (?, ?, ?) "
            "ORDER BY id DESC LIMIT 1",
            (episode_id, *ACTIVE_STATUSES),
        )
    else:
        row = None
    return _row(row)


def recent_failure(conn, *, movie_id: int | None = None,
                   episode_id: int | None = None) -> dict | None:
    """A failed attempt in the last day, to avoid re-grabbing a dead release."""
    cutoff = _now() - 24 * 3600
    if movie_id is not None:
        row = db.get_one(
            conn,
            "SELECT * FROM downloads WHERE movie_id = ? AND status = 'failed' "
            "AND requested_at >= ? ORDER BY id DESC LIMIT 1",
            (movie_id, cutoff),
        )
    elif episode_id is not None:
        row = db.get_one(
            conn,
            "SELECT * FROM downloads WHERE episode_id = ? AND status = 'failed' "
            "AND requested_at >= ? ORDER BY id DESC LIMIT 1",
            (episode_id, cutoff),
        )
    else:
        row = None
    return _row(row)


# ---------------------------------------------------------------- movie CRUD
def get_movie(conn, movie_id: int) -> dict | None:
    return _row(db.get_one(conn, "SELECT * FROM movies WHERE id = ?", (int(movie_id),)))


def list_movies(conn, *, text: str = "", status: str = "") -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if text:
        clauses.append("(title LIKE ? ESCAPE '\\' OR original_title LIKE ? ESCAPE '\\')")
        pattern = f"%{text.replace('%', '\\%').replace('_', '\\_')}%"
        params.extend([pattern, pattern])
    if status == "monitored":
        clauses.append("monitored = 1")
    elif status == "missing":
        clauses.append("file_path IS NULL")
    elif status == "downloaded":
        clauses.append("file_path IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM movies {where} ORDER BY title COLLATE NOCASE", params
    ).fetchall()
    return [dict(row) for row in rows]


def add_movie(conn, tmdb_id: int, *, profile_id: int | None = None,
              monitored: bool = True, search_now: bool = True,
              minimum_availability: str = "released", priority: int = 0) -> dict:
    if db.get_one(conn, "SELECT id FROM movies WHERE tmdb_id = ?", (int(tmdb_id),)):
        raise ValueError("That movie is already in your library.")
    profile = _profile_row(conn, profile_id)
    details = metadata.movie_details(conn, int(tmdb_id))
    availability = str(minimum_availability or "released").strip().lower()
    if availability not in {"announced", "in_cinemas", "released"}:
        raise ValueError("Unknown minimum availability.")
    now = _now()
    cursor = conn.execute(
        """
        INSERT INTO movies (
            tmdb_id, imdb_id, title, original_title, original_language,
            alt_titles, year, overview, runtime, genres, poster_path,
            backdrop_path, release_date, rating, status, added_at, updated_at,
            monitored, minimum_availability, priority, quality_profile_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            details["tmdb_id"], details.get("imdb_id"), details["title"],
            details.get("original_title"), details.get("original_language"),
            json.dumps(details.get("alt_titles") or [], ensure_ascii=False),
            details.get("year"), details.get("overview"), details.get("runtime"),
            json.dumps(details.get("genres") or [], ensure_ascii=False),
            details.get("poster_path"), details.get("backdrop_path"),
            details.get("release_date"), details.get("rating"), details.get("status"),
            now, now, 1 if monitored else 0, availability,
            max(-100, min(100, int(priority or 0))), int(profile["id"]),
        ),
    )
    conn.commit()
    movie = get_movie(conn, int(cursor.lastrowid))
    logger.info("library: added movie %s (tmdb %s)", movie["title"], tmdb_id)
    # The API queues the initial search as a worker job after this transaction
    # commits. Doing it here would put ffprobe back on the web request thread.
    return get_movie(conn, movie["id"])


def update_movie(conn, movie_id: int, *, monitored: bool | None = None,
                 profile_id: int | None = None,
                 minimum_availability: str | None = None,
                 priority: int | None = None) -> dict:
    movie = get_movie(conn, movie_id)
    if movie is None:
        raise LookupError("That movie no longer exists.")
    if monitored is not None:
        conn.execute("UPDATE movies SET monitored = ? WHERE id = ?",
                     (1 if monitored else 0, movie_id))
    if profile_id is not None:
        _profile_row(conn, profile_id)
        conn.execute("UPDATE movies SET quality_profile_id = ? WHERE id = ?",
                     (int(profile_id), movie_id))
    if minimum_availability is not None:
        availability = str(minimum_availability).strip().lower()
        if availability not in {"announced", "in_cinemas", "released"}:
            raise ValueError("Unknown minimum availability.")
        conn.execute("UPDATE movies SET minimum_availability = ? WHERE id = ?",
                     (availability, movie_id))
    if priority is not None:
        conn.execute("UPDATE movies SET priority = ? WHERE id = ?",
                     (max(-100, min(100, int(priority))), movie_id))
    conn.commit()
    return get_movie(conn, movie_id)


def refresh_movie(conn, movie_id: int) -> dict:
    movie = get_movie(conn, movie_id)
    if movie is None:
        raise LookupError("That movie no longer exists.")
    details = metadata.movie_details(conn, movie["tmdb_id"], refresh=True)
    conn.execute(
        """
        UPDATE movies SET imdb_id = ?, title = ?, original_title = ?,
            original_language = ?, alt_titles = ?, year = ?, overview = ?,
            runtime = ?, genres = ?, poster_path = ?, backdrop_path = ?,
            release_date = ?, rating = ?, status = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            details.get("imdb_id"), details["title"], details.get("original_title"),
            details.get("original_language"),
            json.dumps(details.get("alt_titles") or [], ensure_ascii=False),
            details.get("year"), details.get("overview"), details.get("runtime"),
            json.dumps(details.get("genres") or [], ensure_ascii=False),
            details.get("poster_path"), details.get("backdrop_path"),
            details.get("release_date"), details.get("rating"), details.get("status"),
            _now(), movie_id,
        ),
    )
    conn.commit()
    return get_movie(conn, movie_id)


def remove_movie(conn, movie_id: int, *, delete_files: bool = False) -> None:
    movie = get_movie(conn, movie_id)
    if movie is None:
        raise LookupError("That movie no longer exists.")
    from . import downloader

    downloader.cancel_for(conn, movie_id=movie_id)
    if delete_files and movie.get("file_path"):
        _delete_tracked_file(movie["file_path"], config.movie_library_dir())
    conn.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
    add_history(conn, "deleted", movie["title"], kind="movie", movie_id=movie_id,
                message="files deleted" if delete_files else "kept files")
    conn.commit()
    logger.info("library: removed movie %s", movie["title"])


# --------------------------------------------------------------- movie search
def _probe_movie_candidates(conn, candidates: list[dict], profile: quality.Profile,
                            original_language: str | None, *,
                            acquire_timeout: float = 5.0,
                            budget: list[int] | None = None) -> list[dict]:
    """Probe the best candidates and annotate each with the profile decision.

    Candidates beyond the probe ceiling are still returned so manual search
    can show them; grabbing one probes it at that point.
    """
    limit = probe.max_candidates(conn)
    budget = budget if budget is not None else [probe.search_budget(conn)]
    annotated: list[dict] = []
    for index, candidate in enumerate(candidates):
        release = dict(candidate)
        if index >= limit or budget[0] <= 0:
            release["allowed"] = False
            release["reason"] = "Not probed in this search."
            annotated.append(release)
            continue
        release = probe.probe_movie_release(
            conn, release, acquire_timeout=acquire_timeout
        )
        budget[0] -= 1
        allowed, reason = profile.check(release, original_language)
        release["allowed"] = allowed
        release["reason"] = reason
        release["quality_label"] = _tier_label(profile, release) if allowed else None
        annotated.append(release)
    return annotated


def manual_movie_releases(conn, movie_id: int, *, query: str | None = None) -> list[dict]:
    movie = get_movie(conn, movie_id)
    if movie is None:
        raise LookupError("That movie no longer exists.")
    profile = _profile(conn, movie["quality_profile_id"])
    candidates = matching.movie_candidates(
        conn, movie, query_override=query, min_score=0.45 if query else 0.65
    )
    return _probe_movie_candidates(
        conn, candidates, profile, movie.get("original_language"),
        acquire_timeout=30.0,
    )


def search_movie(conn, movie: Mapping[str, Any], *, query: str | None = None,
                 reason: str = "auto") -> dict:
    """Find and queue the best release for one movie, or explain why not."""
    movie_id = int(movie["id"])
    if active_download(conn, movie_id=movie_id):
        return {"queued": False, "message": "A download for this movie is already active."}
    if not query and not movie_available(movie):
        message = (
            "Not available yet under the movie's minimum availability "
            f"({movie.get('minimum_availability') or 'released'})."
        )
        conn.execute("UPDATE movies SET last_search_at = ?, search_message = ? WHERE id = ?",
                     (_now(), message, movie_id))
        conn.commit()
        return {"queued": False, "message": message}

    profile = _profile(conn, movie["quality_profile_id"])
    candidates = matching.movie_candidates(
        conn, movie, query_override=query, min_score=0.85 if not query else 0.45
    )
    budget = [probe.search_budget(conn)]
    annotated = _probe_movie_candidates(
        conn, candidates, profile, movie.get("original_language"), budget=budget
    ) if candidates else []
    current = movie if movie.get("file_path") else None
    choice = quality.choose_release(
        annotated, profile, current=current,
        original_language=movie.get("original_language"),
    )

    if choice is None:
        if not candidates:
            message = "No matching VOD found in your playlists."
        else:
            reasons = [entry.get("reason") for entry in annotated if entry.get("reason")]
            message = "No allowed release available"
            if reasons:
                message += f": {reasons[0]}"
        conn.execute("UPDATE movies SET last_search_at = ?, search_message = ? WHERE id = ?",
                     (_now(), message, movie_id))
        conn.commit()
        return {"queued": False, "message": message}

    if not query and reason in {"auto", "add"} and recent_failure(conn, movie_id=movie_id):
        message = "The last attempt failed recently; skipping this automatic retry."
        conn.execute("UPDATE movies SET last_search_at = ?, search_message = ? WHERE id = ?",
                     (_now(), message, movie_id))
        conn.commit()
        return {"queued": False, "message": message}

    from . import downloader

    upgrade = bool(movie.get("file_path"))
    download = downloader.enqueue_movie(
        conn, movie, choice, reason="upgrade" if upgrade else reason
    )
    label = choice.get("quality_label") or choice.get("resolution") or "unknown"
    message = f"Queued {label} release from {choice['source_name']}."
    conn.execute("UPDATE movies SET last_search_at = ?, search_message = ? WHERE id = ?",
                 (_now(), message, movie_id))
    conn.commit()
    logger.info("library: movie %s -> download #%s (%s)", movie["title"],
                download["id"], label)
    return {"queued": True, "download": download, "release": choice, "message": message}


def grab_movie(conn, movie_id: int, source_id: int, remote_id: str,
               *, reason: str = "manual") -> dict:
    movie = get_movie(conn, movie_id)
    if movie is None:
        raise LookupError("That movie no longer exists.")
    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (int(source_id),))
    if source is None:
        raise LookupError("That playlist no longer exists.")
    item = db.get_one(
        conn,
        "SELECT * FROM items WHERE source_id = ? AND kind = 'vod' AND remote_id = ?",
        (int(source_id), str(remote_id)),
    )
    if item is None:
        raise LookupError("That VOD is no longer in the playlist; update the playlist and try again.")
    if active_download(conn, movie_id=movie_id):
        raise ValueError("A download for this movie is already active.")
    profile = _profile(conn, movie["quality_profile_id"])
    parsed = matching.parse_name(item["name"])
    release = {
        **dict(item),
        "source_name": source["name"],
        "source_kind": source["kind"],
        "resolution": parsed.resolution,
        "source": parsed.source,
        "score": 1.0,
    }
    release = probe.probe_movie_release(conn, release, acquire_timeout=30.0)
    allowed, reason_text = profile.check(release, movie.get("original_language"))
    if not allowed:
        raise ValueError(f"This release is not allowed by the profile: {reason_text}")
    release["quality_label"] = _tier_label(profile, release)
    from . import downloader

    return downloader.enqueue_movie(conn, movie, release, reason=reason)


# --------------------------------------------------------------- series CRUD
def get_series(conn, series_id: int) -> dict | None:
    return _row(db.get_one(conn, "SELECT * FROM series WHERE id = ?", (int(series_id),)))


def list_series(conn, *, text: str = "", status: str = "") -> list[dict]:
    clauses: list[str] = []
    params: list = []
    if text:
        clauses.append("(title LIKE ? ESCAPE '\\' OR original_title LIKE ? ESCAPE '\\')")
        pattern = f"%{text.replace('%', '\\%').replace('_', '\\_')}%"
        params.extend([pattern, pattern])
    if status == "monitored":
        clauses.append("monitored = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM series {where} ORDER BY title COLLATE NOCASE", params
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        counts = db.get_one(
            conn,
            "SELECT COUNT(*) AS total, SUM(file_path IS NOT NULL) AS have, "
            "SUM(air_date IS NOT NULL AND air_date <= ? AND file_path IS NULL) AS missing "
            "FROM series_episodes WHERE series_id = ?",
            (_today(), item["id"]),
        )
        item["episode_count"] = counts["total"] or 0
        item["episode_have"] = counts["have"] or 0
        item["episode_missing"] = counts["missing"] or 0
        out.append(item)
    return out


def series_episodes(conn, series_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM series_episodes WHERE series_id = ? "
        "ORDER BY season, episode",
        (int(series_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def get_episode(conn, episode_id: int) -> dict | None:
    return _row(db.get_one(conn, "SELECT * FROM series_episodes WHERE id = ?", (int(episode_id),)))


def _sync_episodes(conn, series_id: int, episodes: list[dict], *,
                   add_mode: str | None = None) -> None:
    """Upsert provider metadata without destroying local state.

    Existing monitored flags, files and search timestamps win; only metadata
    columns are refreshed. Episodes that disappear from the provider are kept,
    because deleting a row would orphan a file that already exists on disk.

    On the first import ``add_mode`` decides which episodes start monitored.
    On later refreshes a genuinely new episode follows the series'
    ``monitor_new_seasons`` rule: all, future (unaired/unknown) or none. This
    is Sonarr's new-season announcement behaviour without a notification
    channel.
    """
    series = get_series(conn, int(series_id))
    rule = _monitor_new_seasons(series.get("monitor_new_seasons") if series else "all")
    series_monitored = bool(series and series.get("monitored"))
    latest_season = max(
        (int(episode["season"]) for episode in episodes if int(episode["season"]) > 0),
        default=None,
    )
    for episode in episodes:
        season = int(episode["season"])
        number = int(episode["episode"])
        existing = db.get_one(
            conn,
            "SELECT id FROM series_episodes WHERE series_id = ? AND season = ? AND episode = ?",
            (int(series_id), season, number),
        )
        if existing is not None:
            conn.execute(
                """
                UPDATE series_episodes SET
                    absolute_number = ?, title = ?, overview = ?, air_date = ?,
                    tvdb_id = ?
                WHERE id = ?
                """,
                (
                    episode.get("absolute_number"),
                    episode.get("title") or f"Episode {number}",
                    episode.get("overview") or "", episode.get("air_date"),
                    episode.get("tvdb_id"), existing["id"],
                ),
            )
            continue
        if add_mode is not None:
            should_monitor = _monitor_episode_on_add(episode, add_mode, latest_season)
        else:
            should_monitor = _monitor_new_episode(episode, rule)
        conn.execute(
            """
            INSERT INTO series_episodes (
                series_id, season, episode, absolute_number, title, overview,
                air_date, tvdb_id, monitored
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(series_id), season, number, episode.get("absolute_number"),
                episode.get("title") or f"Episode {number}",
                episode.get("overview") or "", episode.get("air_date"),
                episode.get("tvdb_id"),
                1 if (series_monitored and should_monitor) else 0,
            ),
        )
    conn.commit()


def add_series(conn, tmdb_id: int, *, profile_id: int | None = None,
               monitored: bool = True, search_now: bool = True,
               monitor_mode: str = "all", monitor_new_seasons: str = "all",
               priority: int = 0) -> dict:
    if db.get_one(conn, "SELECT id FROM series WHERE tmdb_id = ?", (int(tmdb_id),)):
        raise ValueError("That series is already in your library.")
    profile = _profile_row(conn, profile_id)
    details = metadata.series_details(conn, int(tmdb_id))
    add_mode = _add_monitor_mode(monitor_mode)
    new_seasons_rule = _monitor_new_seasons(monitor_new_seasons)
    now = _now()
    cursor = conn.execute(
        """
        INSERT INTO series (
            tmdb_id, tvdb_id, title, original_title, original_language,
            alt_titles, year, overview, genres, poster_path, backdrop_path,
            first_air_date, status, rating, added_at, updated_at, monitored,
            monitor_new_seasons, priority, quality_profile_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            details["tmdb_id"], details.get("tvdb_id"), details["title"],
            details.get("original_title"), details.get("original_language"),
            json.dumps(details.get("alt_titles") or [], ensure_ascii=False),
            details.get("year"), details.get("overview"),
            json.dumps(details.get("genres") or [], ensure_ascii=False),
            details.get("poster_path"), details.get("backdrop_path"),
            details.get("first_air_date"), details.get("status"), details.get("rating"),
            now, now, 1 if monitored else 0, new_seasons_rule,
            max(-100, min(100, int(priority or 0))), int(profile["id"]),
        ),
    )
    conn.commit()
    series = get_series(conn, int(cursor.lastrowid))
    refresh_series_episodes(conn, series, add_mode=add_mode)
    series = get_series(conn, series["id"])
    logger.info("library: added series %s (tmdb %s, monitor %s)",
                series["title"], tmdb_id, add_mode)
    # The API queues the initial search as a worker job after this transaction
    # commits; see add_movie above.
    return get_series(conn, series["id"])


def refresh_series_episodes(conn, series: Mapping[str, Any], *, refresh: bool = False,
                            add_mode: str | None = None) -> str:
    episodes, source, tvdb_id = metadata.episodes_for_series(conn, series, refresh=refresh)
    _sync_episodes(conn, int(series["id"]), episodes, add_mode=add_mode)
    conn.execute(
        "UPDATE series SET tvdb_id = COALESCE(?, tvdb_id), last_refreshed_at = ?, "
        "updated_at = ? WHERE id = ?",
        (tvdb_id, _now(), _now(), int(series["id"])),
    )
    conn.commit()
    logger.info("library: %s episodes for %s via %s",
                len(episodes), series["title"], source)
    return source


def refresh_series(conn, series_id: int) -> dict:
    series = get_series(conn, series_id)
    if series is None:
        raise LookupError("That series no longer exists.")
    details = metadata.series_details(conn, series["tmdb_id"], refresh=True)
    conn.execute(
        """
        UPDATE series SET tvdb_id = ?, title = ?, original_title = ?,
            original_language = ?, alt_titles = ?, year = ?, overview = ?,
            genres = ?, poster_path = ?, backdrop_path = ?, first_air_date = ?,
            status = ?, rating = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            details.get("tvdb_id") or series.get("tvdb_id"),
            details["title"], details.get("original_title"),
            details.get("original_language"),
            json.dumps(details.get("alt_titles") or [], ensure_ascii=False),
            details.get("year"), details.get("overview"),
            json.dumps(details.get("genres") or [], ensure_ascii=False),
            details.get("poster_path"), details.get("backdrop_path"),
            details.get("first_air_date"), details.get("status"), details.get("rating"),
            _now(), series_id,
        ),
    )
    conn.commit()
    series = get_series(conn, series_id)
    refresh_series_episodes(conn, series, refresh=True)
    return get_series(conn, series_id)


def update_series(conn, series_id: int, *, monitored: bool | None = None,
                  profile_id: int | None = None,
                  monitor_new_seasons: str | None = None,
                  priority: int | None = None) -> dict:
    series = get_series(conn, series_id)
    if series is None:
        raise LookupError("That series no longer exists.")
    if monitored is not None:
        conn.execute("UPDATE series SET monitored = ? WHERE id = ?",
                     (1 if monitored else 0, series_id))
    if profile_id is not None:
        _profile_row(conn, profile_id)
        conn.execute("UPDATE series SET quality_profile_id = ? WHERE id = ?",
                     (int(profile_id), series_id))
    if monitor_new_seasons is not None:
        conn.execute("UPDATE series SET monitor_new_seasons = ? WHERE id = ?",
                     (_monitor_new_seasons(monitor_new_seasons), series_id))
    if priority is not None:
        conn.execute("UPDATE series SET priority = ? WHERE id = ?",
                     (max(-100, min(100, int(priority))), series_id))
    conn.commit()
    return get_series(conn, series_id)


def set_episode_monitored(conn, episode_id: int, monitored: bool) -> dict:
    episode = get_episode(conn, episode_id)
    if episode is None:
        raise LookupError("That episode no longer exists.")
    conn.execute("UPDATE series_episodes SET monitored = ? WHERE id = ?",
                 (1 if monitored else 0, episode_id))
    conn.commit()
    return get_episode(conn, episode_id)


def set_season_monitored(conn, series_id: int, season: int, monitored: bool) -> None:
    if get_series(conn, series_id) is None:
        raise LookupError("That series no longer exists.")
    conn.execute(
        "UPDATE series_episodes SET monitored = ? WHERE series_id = ? AND season = ?",
        (1 if monitored else 0, int(series_id), int(season)),
    )
    conn.commit()


def remove_series(conn, series_id: int, *, delete_files: bool = False) -> None:
    series = get_series(conn, series_id)
    if series is None:
        raise LookupError("That series no longer exists.")
    from . import downloader

    downloader.cancel_for(conn, series_id=series_id)
    root = config.series_library_dir()
    if delete_files:
        episodes = series_episodes(conn, series_id)
        for episode in episodes:
            if episode.get("file_path"):
                _delete_tracked_file(episode["file_path"], root)
        templates = series_templates(conn)
        folder = _series_folder(conn, series, templates)
        try:
            if folder.exists() and folder.is_dir():
                shutil.rmtree(folder)
        except OSError as exc:
            logger.warning("library: could not remove %s: %s", folder, exc)
    conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
    add_history(conn, "deleted", series["title"], kind="series", series_id=series_id,
                message="files deleted" if delete_files else "kept files")
    conn.commit()
    logger.info("library: removed series %s", series["title"])


# -------------------------------------------------------------- provider data
def _source_headers(source_row: Mapping[str, Any]) -> dict:
    try:
        return json.loads((_get(source_row, "request_headers") or "{}"))
    except (TypeError, ValueError):
        return {}


def provider_series_episodes(conn, source_row: Mapping[str, Any], remote_id: str,
                             *, refresh: bool = False) -> list[dict]:
    """Normalised provider episodes, cached for a few hours.

    Xtream's get_series_info is one call per series and can be large, so it is
    cached in provider_series_cache. The cache is derived data, excluded from
    backups and safe to discard.
    """
    if source_row["kind"] != "xtream":
        raise ValueError("Series episode downloads need an Xtream playlist.")
    source_id = int(source_row["id"])
    remote_id = str(remote_id)
    cached = None if refresh else db.get_one(
        conn,
        "SELECT payload, fetched_at FROM provider_series_cache "
        "WHERE source_id = ? AND remote_id = ?",
        (source_id, remote_id),
    )
    if cached is not None and _now() - int(cached["fetched_at"]) <= 6 * 3600:
        try:
            value = json.loads(cached["payload"])
            if isinstance(value, list):
                return value
        except (TypeError, ValueError):
            pass

    headers = _source_headers(source_row)
    # get_series_info is a provider API call and counts against the account's
    # connection limit. It is deliberately inside the cache miss only: a
    # cached episode list must remain readable while a download uses the slot.
    with connections.acquire(
        conn, source_id, purpose="Series metadata", timeout=10.0
    ):
        client = xtream.XtreamClient(
            source_row["base_url"], source_row["username"], source_row["password"],
            user_agent=headers.get("user_agent"),
            referrer=headers.get("referrer"),
            headers=headers.get("headers"),
        )
        try:
            payload = client.series_info(remote_id)
        finally:
            client.close()

    raw = payload.get("episodes") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("The provider returned no episode list for that series.")
    episodes: list[dict] = []
    for season_key, entries in raw.items():
        try:
            season = int(str(season_key).strip())
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                number = int(str(entry.get("episode_num") or "").strip())
            except (TypeError, ValueError):
                continue
            stream_id = entry.get("id")
            if not stream_id or number <= 0:
                continue
            extension = naming.safe_extension(entry.get("container_extension"), "mp4")
            episodes.append({
                "season": season,
                "episode": number,
                "stream_id": str(stream_id),
                "title": entry.get("title") or f"Episode {number}",
                "duration": (entry.get("info") or {}).get("duration"),
                "container_ext": extension,
            })
    episodes.sort(key=lambda entry: (entry["season"], entry["episode"]))
    conn.execute(
        "INSERT INTO provider_series_cache (source_id, remote_id, payload, fetched_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(source_id, remote_id) DO UPDATE SET "
        "payload = excluded.payload, fetched_at = excluded.fetched_at",
        (source_id, remote_id, json.dumps(episodes, ensure_ascii=False), _now()),
    )
    conn.commit()
    return episodes


def _provider_maps(conn, candidates: list[dict], *, limit: int = 8) -> list[tuple[dict, dict, dict]]:
    out = []
    for candidate in candidates[:limit]:
        source = _row(db.get_one(conn, "SELECT * FROM sources WHERE id = ?",
                                 (int(candidate["source_id"]),)))
        if source is None or source["kind"] != "xtream":
            continue
        try:
            episodes = provider_series_episodes(conn, source, candidate["remote_id"])
        except connections.ConnectionBusy:
            # Do not disguise a busy playlist as "series not found": the user
            # can retry, and the automatic task will try again next run.
            raise
        except Exception as exc:
            logger.warning("library: could not list episodes for provider series %s: %s",
                           candidate.get("name"), exc)
            continue
        out.append((candidate, source, {
            (int(entry["season"]), int(entry["episode"])): entry for entry in episodes
        }))
    return out


def _episodes_to_search(conn, series: Mapping[str, Any], *, episode_id: int | None = None,
                        include_files: bool = False) -> list[dict]:
    profile = _profile(conn, series["quality_profile_id"])
    if episode_id is not None:
        episode = get_episode(conn, episode_id)
        if episode is None or int(episode["series_id"]) != int(series["id"]):
            raise LookupError("That episode no longer exists.")
        return [episode]
    rows = [
        episode for episode in series_episodes(conn, int(series["id"]))
        if episode["monitored"] and _aired(episode.get("air_date"))
    ]
    out = []
    for episode in rows:
        if not episode.get("file_path"):
            out.append(episode)
            continue
        if include_files and profile.upgrades and not profile.cutoff_met(episode):
            out.append(episode)
    return out


def _episode_release(candidate: Mapping[str, Any], source: Mapping[str, Any],
                     provider_episode: Mapping[str, Any]) -> dict:
    return {
        **dict(candidate),
        "source_name": source["name"],
        "source_kind": source["kind"],
        "provider_episode": dict(provider_episode),
        "resolution": candidate.get("resolution") or "unknown",
        "source": candidate.get("source") or "unknown",
        "score": candidate.get("score") or 0.0,
    }


def _probe_episode_releases(
    conn, providers: list[tuple[dict, dict, dict]], episode: Mapping[str, Any],
    profile: quality.Profile, original_language: str | None,
    *, budget: list[int], acquire_timeout: float = 5.0,
) -> list[dict]:
    """Build and probe the provider releases for one Sonarr episode."""
    limit = probe.max_candidates(conn)
    releases: list[dict] = []
    for index, (candidate, source, mapping) in enumerate(providers):
        provider_episode = mapping.get((int(episode["season"]), int(episode["episode"])))
        if provider_episode is None:
            continue
        release = _episode_release(candidate, source, provider_episode)
        if index >= limit or budget[0] <= 0:
            release["allowed"] = False
            release["reason"] = "Not probed in this search."
            releases.append(release)
            continue
        release = probe.probe_episode_release(
            conn, release, acquire_timeout=acquire_timeout
        )
        budget[0] -= 1
        allowed, reason = profile.check(release, original_language)
        release["allowed"] = allowed
        release["reason"] = reason
        release["quality_label"] = _tier_label(profile, release) if allowed else None
        releases.append(release)
    return releases


def search_series(conn, series: Mapping[str, Any], *, episode_id: int | None = None,
                  query: str | None = None, reason: str = "auto") -> dict:
    series_id = int(series["id"])
    if not series.get("monitored") and episode_id is None:
        return {"queued": 0, "message": "This series is not monitored."}
    candidates = matching.series_candidates(
        conn, series, query_override=query, min_score=0.85 if not query else 0.45
    )
    if not candidates:
        message = "No matching series found in your playlists."
        conn.execute("UPDATE series SET last_search_at = ?, search_message = ? WHERE id = ?",
                     (_now(), message, series_id))
        conn.commit()
        return {"queued": 0, "message": message}

    episodes = _episodes_to_search(
        conn, series, episode_id=episode_id, include_files=True
    )
    if not episodes:
        message = "No missing or upgradable episodes."
        conn.execute("UPDATE series SET last_search_at = ?, search_message = ? WHERE id = ?",
                     (_now(), message, series_id))
        conn.commit()
        return {"queued": 0, "message": message}

    providers = _provider_maps(conn, candidates)
    if not providers:
        message = "No Xtream playlist currently has this series."
        conn.execute("UPDATE series SET last_search_at = ?, search_message = ? WHERE id = ?",
                     (_now(), message, series_id))
        conn.commit()
        return {"queued": 0, "message": message}

    profile = _profile(conn, series["quality_profile_id"])
    from . import downloader

    budget = [probe.search_budget(conn)]
    queued = 0
    skipped = 0
    rejected = 0
    for episode in episodes:
        if active_download(conn, episode_id=episode["id"]):
            continue
        if not query and reason in {"auto", "add"} and recent_failure(
                conn, episode_id=episode["id"]):
            skipped += 1
            continue
        if budget[0] <= 0:
            break
        releases = _probe_episode_releases(
            conn, providers, episode, profile, series.get("original_language"),
            budget=budget,
        )
        if not releases:
            rejected += 1
            continue
        choice = quality.choose_release(
            releases, profile,
            current=episode if episode.get("file_path") else None,
            original_language=series.get("original_language"),
        )
        if choice is None:
            rejected += 1
            continue
        downloader.enqueue_episode(
            conn, series, episode, choice, choice["provider_episode"],
            reason="upgrade" if episode.get("file_path") else reason,
        )
        queued += 1
        if queued >= 50:
            break

    if queued:
        message = f"Queued {queued} episode{'s' if queued != 1 else ''}."
    elif skipped:
        message = "Recent download failures were skipped; retry one manually."
    elif budget[0] <= 0:
        message = ("The probe budget was exhausted; the next search continues "
                   "with the remaining episodes.")
    elif rejected:
        message = "No allowed release passed the stream probe and quality profile."
    else:
        message = "No allowed release available (check the quality profile)."
    conn.execute("UPDATE series SET last_search_at = ?, search_message = ? WHERE id = ?",
                 (_now(), message, series_id))
    conn.commit()
    logger.info("library: series %s search queued %d episode(s)", series["title"], queued)
    return {"queued": queued, "message": message}


def manual_episode_releases(conn, series_id: int, episode_id: int,
                            *, query: str | None = None) -> list[dict]:
    series = get_series(conn, series_id)
    episode = get_episode(conn, episode_id)
    if series is None or episode is None or int(episode["series_id"]) != int(series_id):
        raise LookupError("That episode no longer exists.")
    profile = _profile(conn, series["quality_profile_id"])
    candidates = matching.series_candidates(
        conn, series, query_override=query, min_score=0.45 if query else 0.65
    )
    providers = _provider_maps(conn, candidates, limit=12)
    budget = [probe.search_budget(conn)]
    out = _probe_episode_releases(
        conn, providers, episode, profile, series.get("original_language"),
        budget=budget, acquire_timeout=30.0,
    )
    out.sort(key=lambda entry: (
        not entry.get("allowed"),
        -float(entry.get("score") or 0.0),
        quality.RANK.get(entry.get("resolution", "unknown"), len(quality.RESOLUTIONS) - 1),
    ))
    return out


def grab_episode(conn, series_id: int, episode_id: int, source_id: int,
                 provider_remote_id: str, stream_id: str) -> dict:
    series = get_series(conn, series_id)
    episode = get_episode(conn, episode_id)
    if series is None or episode is None or int(episode["series_id"]) != int(series_id):
        raise LookupError("That episode no longer exists.")
    source = _row(db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (int(source_id),)))
    if source is None:
        raise LookupError("That playlist no longer exists.")
    episodes = provider_series_episodes(conn, source, str(provider_remote_id), refresh=True)
    provider_episode = next(
        (entry for entry in episodes if str(entry["stream_id"]) == str(stream_id)), None
    )
    if provider_episode is None:
        raise LookupError("That provider episode is no longer available.")
    if (int(provider_episode["season"]) != int(episode["season"])
            or int(provider_episode["episode"]) != int(episode["episode"])):
        raise ValueError("That provider episode is not the season/episode you selected.")
    item = db.get_one(
        conn,
        "SELECT * FROM items WHERE source_id = ? AND kind = 'series' AND remote_id = ?",
        (int(source_id), str(provider_remote_id)),
    )
    if item is None:
        raise LookupError("That series is no longer in the playlist; update it and try again.")
    parsed = matching.parse_name(item["name"])
    release = {
        **dict(item),
        "source_name": source["name"],
        "source_kind": source["kind"],
        "provider_episode": provider_episode,
        "resolution": parsed.resolution,
        "source": parsed.source,
        "score": 1.0,
    }
    profile = _profile(conn, series["quality_profile_id"])
    release = probe.probe_episode_release(conn, release, acquire_timeout=30.0)
    allowed, reason_text = profile.check(release, series.get("original_language"))
    if not allowed:
        raise ValueError(f"This episode is not allowed by the profile: {reason_text}")
    release["quality_label"] = _tier_label(profile, release)
    if active_download(conn, episode_id=episode_id):
        raise ValueError("A download for this episode is already active.")
    from . import downloader

    return downloader.enqueue_episode(conn, series, episode, release,
                                      release["provider_episode"], reason="manual")


# -------------------------------------------------------------- import/files
def _series_folder(conn, series: Mapping[str, Any], templates: Mapping[str, str]) -> Path:
    root = config.series_library_dir()
    folder = root / naming.render_component(
        templates["folder"],
        {"title": series["title"], "year": series.get("year") or ""},
        naming.SERIES_FOLDER_TOKENS,
    )
    return naming.confine(folder, root)


def _delete_tracked_file(path: str | Path, root: Path) -> None:
    """Delete a file this app recorded, and empty folders it leaves behind."""
    try:
        target = naming.confine(path, root)
    except naming.TemplateError:
        logger.warning("library: refusing to delete outside %s: %s", root, path)
        return
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("library: could not delete %s: %s", target, exc)
        return
    parent = target.parent
    while parent != root and root in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def import_download(conn, download: Mapping[str, Any], temp_path: Path) -> Path:
    """Move a completed temp file into the library and update the DB."""
    if download["kind"] == "movie":
        movie = get_movie(conn, int(download["movie_id"]))
        if movie is None:
            raise ValueError("The movie was removed while its download was running.")
        final = _movie_target(conn, movie, download)
        previous = movie.get("file_path")
        _move_into_place(temp_path, final)
        conn.execute(
            "UPDATE movies SET file_path = ?, file_size = ?, resolution = ?, "
            "source = COALESCE(?, source), quality_label = COALESCE(?, quality_label), "
            "audio_languages = COALESCE(?, audio_languages), "
            "subtitle_languages = COALESCE(?, subtitle_languages), "
            "media_info = COALESCE(?, media_info), imported_at = ?, updated_at = ? "
            "WHERE id = ?",
            (
                str(final), final.stat().st_size, download.get("resolution"),
                download.get("source"), download.get("quality_label"),
                download.get("audio_languages"), download.get("subtitle_languages"),
                download.get("media_info"), _now(), _now(), movie["id"],
            ),
        )
        conn.commit()
        if previous and Path(previous) != final:
            _delete_tracked_file(previous, config.movie_library_dir())
        add_history(conn, "upgraded" if previous else "imported", movie["title"],
                    kind="movie", movie_id=movie["id"], download_id=download["id"],
                    message=str(final))
        logger.info("library: imported movie %s -> %s", movie["title"], final)
        return final

    episode = get_episode(conn, int(download["episode_id"]))
    series = get_series(conn, int(download["series_id"])) if download.get("series_id") else None
    if episode is None or series is None:
        raise ValueError("The episode was removed while its download was running.")
    final = _episode_target(conn, series, episode, download)
    previous = episode.get("file_path")
    _move_into_place(temp_path, final)
    conn.execute(
        "UPDATE series_episodes SET file_path = ?, file_size = ?, resolution = ?, "
        "source = COALESCE(?, source), quality_label = COALESCE(?, quality_label), "
        "audio_languages = COALESCE(?, audio_languages), "
        "subtitle_languages = COALESCE(?, subtitle_languages), "
        "media_info = COALESCE(?, media_info), imported_at = ? WHERE id = ?",
        (
            str(final), final.stat().st_size, download.get("resolution"),
            download.get("source"), download.get("quality_label"),
            download.get("audio_languages"), download.get("subtitle_languages"),
            download.get("media_info"), _now(), episode["id"],
        ),
    )
    conn.commit()
    if previous and Path(previous) != final:
        _delete_tracked_file(previous, config.series_library_dir())
    label = f"{series['title']} S{int(episode['season']):02d}E{int(episode['episode']):02d}"
    add_history(conn, "upgraded" if previous else "imported", label,
                kind="episode", series_id=series["id"], episode_id=episode["id"],
                download_id=download["id"], message=str(final))
    logger.info("library: imported %s -> %s", label, final)
    return final


def _movie_target(conn, movie: Mapping[str, Any], download: Mapping[str, Any]) -> Path:
    root = config.movie_library_dir()
    templates = movie_templates(conn)
    values = {
        "title": movie["title"],
        "year": movie.get("year") or "",
        "resolution": download.get("resolution") or "unknown",
        "source": quality.source_label(download.get("source")),
        "quality": download.get("quality_label") or download.get("resolution") or "unknown",
    }
    folder = root / naming.render_component(
        templates["folder"], values, naming.MOVIE_FOLDER_TOKENS,
    )
    filename = naming.render_component(
        templates["file"], values, naming.MOVIE_FILE_TOKENS,
    ) + "." + naming.safe_extension(download.get("container_ext"), "mp4")
    target = folder / filename
    naming.confine(folder, root)
    naming.confine(target, root)
    if target.exists():
        previous = movie.get("file_path")
        if not previous or Path(previous).resolve() != target.resolve():
            target = naming.unique_path(target)
    return target


def _episode_target(conn, series: Mapping[str, Any], episode: Mapping[str, Any],
                    download: Mapping[str, Any]) -> Path:
    root = config.series_library_dir()
    templates = series_templates(conn)
    folder = _series_folder(conn, series, templates)
    season_dir = naming.confine(
        folder / naming.render_component(
            templates["season"],
            {"season": episode["season"]},
            naming.SEASON_FOLDER_TOKENS,
        ),
        root,
    )
    values = {
        "series": series["title"],
        "year": series.get("year") or "",
        "season": episode["season"],
        "episode": episode["episode"],
        "episode_title": episode.get("title") or f"Episode {int(episode['episode'])}",
        "resolution": download.get("resolution") or "unknown",
        "source": quality.source_label(download.get("source")),
        "quality": download.get("quality_label") or download.get("resolution") or "unknown",
    }
    filename = naming.render_component(
        templates["file"], values, naming.EPISODE_FILE_TOKENS,
    ) + "." + naming.safe_extension(download.get("container_ext"), "mp4")
    target = season_dir / filename
    naming.confine(season_dir, root)
    naming.confine(target, root)
    if target.exists():
        previous = episode.get("file_path")
        if not previous or Path(previous).resolve() != target.resolve():
            target = naming.unique_path(target)
    return target


def _move_into_place(temp_path: Path, final: Path) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(temp_path, final)
    except OSError:
        # A foreign filesystem (a misconfigured mount) cannot be renamed
        # across; copy and remove rather than failing the whole import.
        shutil.move(str(temp_path), str(final))


# ------------------------------------------------------------------- queries
def movie_status(conn, movie: Mapping[str, Any]) -> str:
    if movie.get("file_path"):
        return "downloaded"
    if active_download(conn, movie_id=int(movie["id"])):
        return "downloading"
    return "missing"


def movie_needs_search(conn, movie: Mapping[str, Any]) -> bool:
    """Whether a monitored movie is missing a file or below its cutoff."""
    if not movie.get("monitored"):
        return False
    if not movie.get("file_path"):
        return movie_available(movie)
    profile = _profile(conn, movie["quality_profile_id"])
    return profile.upgrades and not profile.cutoff_met(movie)


def series_needs_search(conn, series: Mapping[str, Any]) -> bool:
    """Whether a monitored series has a missing or upgradable episode."""
    if not series.get("monitored"):
        return False
    return bool(_episodes_to_search(conn, series, include_files=True))


def wanted_movies(conn) -> list[dict]:
    rows = [
        dict(row) for row in conn.execute(
            "SELECT * FROM movies WHERE monitored = 1 AND file_path IS NULL "
            "ORDER BY title COLLATE NOCASE"
        )
    ]
    return [movie for movie in rows if movie_available(movie)]


def wanted_episodes(conn) -> list[dict]:
    return [
        dict(row) for row in conn.execute(
            """
            SELECT e.*, s.title AS series_title, s.year AS series_year,
                   s.poster_path AS series_poster
            FROM series_episodes e JOIN series s ON s.id = e.series_id
            WHERE s.monitored = 1 AND e.monitored = 1 AND e.file_path IS NULL
              AND (e.air_date IS NULL OR e.air_date <= ?)
            ORDER BY COALESCE(e.air_date, '') DESC, s.title COLLATE NOCASE,
                     e.season, e.episode
            """,
            (_today(),),
        )
    ]


__all__ = [
    "add_history",
    "add_movie",
    "add_series",
    "active_download",
    "default_profile_id",
    "get_episode",
    "get_movie",
    "get_series",
    "grab_episode",
    "grab_movie",
    "import_download",
    "list_movies",
    "list_series",
    "manual_episode_releases",
    "manual_movie_releases",
    "movie_available",
    "movie_needs_search",
    "movie_status",
    "movie_templates",
    "provider_series_episodes",
    "refresh_movie",
    "refresh_series",
    "refresh_series_episodes",
    "remove_movie",
    "remove_series",
    "search_movie",
    "search_series",
    "series_episodes",
    "series_needs_search",
    "series_templates",
    "set_episode_monitored",
    "set_season_monitored",
    "update_movie",
    "update_series",
    "wanted_episodes",
    "wanted_movies",
]
