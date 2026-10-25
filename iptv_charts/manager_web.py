"""Server-rendered pages for the movie and series library."""

from __future__ import annotations

import calendar as calendar_module
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from flask import (Blueprint, abort, current_app, g, redirect, render_template,
                   request, url_for)
from werkzeug.routing import BuildError

from . import bworker_store, config, db, library, metadata, quality, scan, scheduler

# The item stage/history module is owned by the bworker feature. Import it
# defensively so the movie page still renders (from the SQL fallback below)
# when this checkout predates that module.
try:  # pragma: no cover - the import succeeds on the release branch
    from . import bworker_events
except ImportError:  # pragma: no cover
    bworker_events = None

bp = Blueprint("manager", __name__)
logger = logging.getLogger(__name__)


def _conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _profiles(conn) -> list[dict]:
    """Profiles in display order, with the configured default marked.

    Builtins come first (4K, 1080p, 720p), then custom profiles. Add and
    import dialogs preselect the ``is_default`` row, which is the one named by
    ``settings.default_profile_key`` rather than the lowest id.
    """
    default_key = str(
        db.get_setting(conn, "default_profile_key", db.DEFAULT_PROFILE_KEY)
        or db.DEFAULT_PROFILE_KEY
    )
    builtins = {
        spec["builtin_key"]: index
        for index, spec in enumerate(db.BUILTIN_PROFILES)
    }
    profiles = [
        quality.profile_summary(row, default_key=default_key)
        for row in conn.execute("SELECT * FROM quality_profiles")
    ]
    profiles.sort(key=lambda profile: (
        0,
        builtins.get(profile.get("builtin_key"), len(builtins)),
        profile["name"].casefold(),
    ) if profile.get("is_builtin") else (
        1, 0, profile["name"].casefold(),
    ))
    if profiles and not any(profile.get("is_default") for profile in profiles):
        fallback = quality.find_default_profile(profiles, default_key)
        if isinstance(fallback, dict):
            fallback["is_default"] = True
    return profiles


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
            movie_id = int(item["movie_id"])
            existing = by_movie.get(movie_id)
            # The movie card can show one bar; prefer the main file over an
            # additional version so progress and status are not confusing.
            if existing is None or (
                item.get("mode") == "main" and existing.get("mode") != "main"
            ):
                by_movie[movie_id] = item
        if item.get("episode_id"):
            by_episode[int(item["episode_id"])] = item
    return by_movie, by_episode


def _movie_search_pending(conn, movie_id: int) -> bool:
    for row in conn.execute(
        "SELECT payload FROM worker_jobs WHERE kind = 'search_movie' "
        "AND status IN ('queued', 'running')"
    ):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if int(payload.get("movie_id") or 0) == int(movie_id):
            return True
    return False


def _active_for_movie(conn, movie_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM downloads WHERE movie_id = ? "
        "AND status IN ('queued', 'downloading', 'importing') "
        "ORDER BY CASE mode WHEN 'main' THEN 0 ELSE 1 END, id",
        (int(movie_id),),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        total = item.get("bytes_total")
        item["fraction"] = (item.get("bytes_done") or 0) / total if total else None
        out.append(item)
    return out


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


# ------------------------------------------------------- item stage/history
# The movie detail page renders the current bworker stage plus the durable
# last-15 event history server-side. bworker_events owns the canonical view;
# the fallback keeps the page usable if that module is not present yet, and a
# direct-read helper below is deliberately read-only: the web process never
# claims or advances bworker work.

_STAGE_FALLBACK_LABELS = {
    "pending": ("Pending", "muted"),
    "waiting_availability": ("Waiting for availability", "waiting"),
    "waiting_air": ("Waiting for air", "waiting"),
    "search": ("Searching", "active"),
    "searching_candidates": ("Searching candidates", "active"),
    "probing": ("Probing", "active"),
    "downloading": ("Downloading", "active"),
    "verifying": ("Verifying", "active"),
    "renaming": ("Renaming", "active"),
    "moving": ("Moving", "active"),
    "awaiting_upgrade": ("Awaiting upgrade", "waiting"),
    "complete": ("Complete", "success"),
    "no_candidates": ("No candidates", "warning"),
    "failed": ("Failed", "danger"),
    "paused": ("Paused", "warning"),
    "skipped": ("Skipped", "muted"),
}

_WORK_FALLBACK_LABELS = {
    "pending": ("Pending", "secondary", "waiting"),
    "running": ("Running", "primary", "active"),
    "retry": ("Retry scheduled", "warning", "warning"),
    "failed_stopped": ("Stopped after failures", "danger", "danger"),
    "disabled": ("Disabled", "secondary", "muted"),
    "complete": ("Complete", "success", "success"),
}

_EVENT_FALLBACK_LABELS = {
    "started": ("Started", "info", "active"),
    "progress": ("In progress", "primary", "active"),
    "succeeded": ("Succeeded", "success", "success"),
    "failed": ("Failed", "danger", "danger"),
    "skipped": ("Skipped", "secondary", "muted"),
    "info": ("Info", "secondary", "muted"),
}

_WORK_PRIORITY = {
    "running": 0, "retry": 1, "failed_stopped": 2,
    "pending": 3, "complete": 4, "disabled": 5,
}

_STAGE_API_RULE = "/api/movies/<int:movie_id>/stage"


def _stage_api_url(movie_id: int) -> str | None:
    """The per-movie stage JSON endpoint, when A04's route is installed.

    Looking the rule up by its path avoids depending on the blueprint's
    endpoint name. No route is registered in this build, so the template
    omits the polling hook and the existing action reloads remain the refresh
    path.
    """
    try:
        rules = list(current_app.url_map.iter_rules())
    except RuntimeError:
        return None
    for rule in rules:
        if rule.rule == _STAGE_API_RULE:
            try:
                return url_for(rule.endpoint, movie_id=int(movie_id))
            except BuildError:
                return None
    return None


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # reject NaN


def _bytes_label(value) -> str:
    number = _number(value)
    if number is None or number <= 0:
        return "0 B"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for index, unit in enumerate(units):
        if number < 1024 or index == len(units) - 1:
            if unit == "B" or number >= 10 or number.is_integer():
                return f"{number:.0f} {unit}"
            return f"{number:.1f} {unit}"
        number /= 1024
    return "0 B"


def _quantity(value) -> str:
    number = _number(value)
    return "?" if number is None else f"{number:g}"


def _progress_view(progress, download=None) -> dict | None:
    """Normalise event/download progress into a bar + readable label."""
    if isinstance(download, dict) and download:
        done = _number(download.get("bytes_done"))
        total = _number(download.get("bytes_total"))
        percent = _number(download.get("percent"))
        if percent is None and done is not None and total:
            percent = done / total * 100
        if percent is not None:
            percent = max(0.0, min(100.0, percent))
        if percent is not None and total:
            text = f"{percent:.0f}% · {_bytes_label(done)} / {_bytes_label(total)}"
        elif percent is not None:
            text = f"{percent:.0f}%"
        elif done:
            text = f"{_bytes_label(done)} downloaded"
        else:
            text = "Download in progress"
        return {
            "percent": None if percent is None else round(percent, 1),
            "text": text,
            "indeterminate": percent is None,
            "kind": "download",
            "status": download.get("status"),
        }

    if not isinstance(progress, dict) or not progress:
        return None
    done = _number(progress.get("done"))
    total = _number(progress.get("total"))
    percent = _number(progress.get("percent"))
    fraction = _number(progress.get("fraction"))
    if percent is None and fraction is not None:
        percent = fraction * 100
    if percent is None and done is not None and total:
        percent = done / total * 100
    if percent is not None:
        percent = max(0.0, min(100.0, percent))
    download_ish = any(
        key in progress for key in ("bytes_done", "bytes_total", "fraction")
    )
    if done is not None and total:
        if download_ish:
            text = f"{percent:.0f}% · {_bytes_label(done)} / {_bytes_label(total)}" \
                if percent is not None else \
                f"{_bytes_label(done)} / {_bytes_label(total)}"
        else:
            text = f"{_quantity(done)} / {_quantity(total)}"
            if percent is not None:
                text += f" · {percent:.0f}%"
    elif percent is not None:
        text = f"{percent:.0f}%"
    else:
        text = str(progress.get("message") or "Working")
    kind = "download" if download_ish else (
        "candidates" if total is not None and total <= 1000 else "progress")
    return {
        "percent": None if percent is None else round(percent, 1),
        "text": text,
        "indeterminate": percent is None,
        "kind": kind,
        "status": progress.get("stage") or progress.get("phase"),
    }


def _in_label(timestamp) -> str | None:
    """A short forward-looking label for ``next_run_at``."""
    value = _number(timestamp)
    if value is None:
        return None
    delta = int(value) - int(time.time())
    if delta <= 0:
        return "now"
    for limit, divisor, unit in (
        (90, 1, "second"),
        (3600, 60, "minute"),
        (86400, 3600, "hour"),
        (2592000, 86400, "day"),
    ):
        if delta < limit:
            amount = max(1, delta // divisor)
            return f"in {amount} {unit}{'s' if amount != 1 else ''}"
    days = delta // 86400
    return f"in {days} days"


def _stage_event_view(event: dict) -> dict:
    item = dict(event)
    item["progress"] = _progress_view(item.get("progress"))
    return item


def _sql_movie_stage_context(conn, movie_id: int) -> dict | None:
    """Direct-SQL fallback for the movie stage card (bworker_events absent)."""
    movie = library.get_movie(conn, movie_id)
    if movie is None:
        return None
    works = [
        dict(row) for row in conn.execute(
            "SELECT * FROM bworker_work WHERE entity_type = 'movie' "
            "AND entity_id = ? ORDER BY id", (int(movie_id),)
        )
    ]
    work = min(
        works,
        key=lambda row: (
            _WORK_PRIORITY.get(str(row.get("status") or ""), 9),
            -(int(row.get("updated_at") or 0)),
            -int(row.get("id") or 0),
        ),
        default=None,
    )
    raw_events = [
        dict(row) for row in conn.execute(
            "SELECT * FROM bworker_events WHERE entity_type = 'movie' "
            "AND entity_id = ? ORDER BY id DESC LIMIT 15", (int(movie_id),)
        )
    ]
    events = []
    for row in raw_events:
        stage_label, stage_tone = _STAGE_FALLBACK_LABELS.get(
            str(row.get("stage") or ""), (str(row.get("stage") or "Unknown"), "muted"))
        status_label, status_badge, status_tone = _EVENT_FALLBACK_LABELS.get(
            str(row.get("status") or ""), ("Info", "secondary", "muted"))
        events.append({
            **row,
            "stage_label": stage_label, "stage_tone": stage_tone,
            "stage_badge": "secondary",
            "status_label": status_label, "status_badge": status_badge,
            "status_tone": status_tone,
            "progress": _decode_json(row.get("progress")),
        })

    download = None
    download_id = work.get("current_download_id") if work else None
    if download_id:
        row = conn.execute("SELECT * FROM downloads WHERE id = ?",
                           (int(download_id),)).fetchone()
    else:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE movie_id = ? AND status IN "
            "('queued', 'downloading', 'importing') ORDER BY id DESC LIMIT 1",
            (int(movie_id),),
        ).fetchall()
        row = rows[0] if rows else None
    if row is not None:
        item = dict(row)
        total = item.get("bytes_total")
        done = int(item.get("bytes_done") or 0)
        fraction = (done / total) if total else None
        download = {
            "id": int(item["id"]), "status": item.get("status"),
            "mode": item.get("mode"), "title": item.get("title"),
            "bytes_done": done, "bytes_total": total,
            "fraction": fraction,
            "percent": round(fraction * 100, 1) if fraction is not None else None,
        }

    if work is not None:
        stage = str(work.get("stage") or "pending")
    elif events:
        stage = str(events[0].get("stage") or "pending")
    elif movie.get("file_path"):
        stage = "complete"
    elif not movie.get("monitored"):
        stage = "skipped"
    else:
        stage, _next = bworker_store.movie_initial_stage(movie)
    label, stage_tone = _STAGE_FALLBACK_LABELS.get(
        stage, (stage.replace("_", " ").title() or "Unknown", "muted"))
    if work is not None:
        status = str(work.get("status") or "pending")
        status_label, status_badge, status_tone = _WORK_FALLBACK_LABELS.get(
            status, ("Unknown", "secondary", "muted"))
    elif events:
        status = str(events[0].get("status") or "info")
        status_label, status_badge, status_tone = _EVENT_FALLBACK_LABELS.get(
            status, ("Info", "secondary", "muted"))
    elif movie.get("file_path"):
        status = "complete"
        status_label, status_badge, status_tone = _WORK_FALLBACK_LABELS["complete"]
    elif not movie.get("monitored"):
        status = "disabled"
        status_label, status_badge, status_tone = _WORK_FALLBACK_LABELS["disabled"]
    else:
        status = "pending"
        status_label, status_badge, status_tone = _WORK_FALLBACK_LABELS["pending"]
    message = None
    if work is not None and str(work.get("status") or "") in (
            "retry", "failed_stopped"):
        message = work.get("last_error")
    if not message and events:
        message = events[0].get("message")
    progress = _decode_json(events[0].get("progress")) if events else None
    best_score = work.get("best_score") if work is not None else None
    public_work = None
    if work is not None:
        public_work = {
            "id": work.get("id"), "work_key": work.get("work_key"),
            "work_type": work.get("work_type"),
            "stage": stage, "stage_label": label, "stage_tone": stage_tone,
            "status": status, "status_label": status_label,
            "status_badge": status_badge, "status_tone": status_tone,
            "failure_count": work.get("failure_count"),
            "next_run_at": work.get("next_run_at"),
            "best_score": best_score,
            "last_error": work.get("last_error"),
            "message": message,
            "started_at": work.get("started_at"),
            "finished_at": work.get("finished_at"),
            "updated_at": work.get("updated_at"),
        }
    return {
        "stage": stage, "label": label,
        "status": status, "status_label": status_label,
        "status_badge": status_badge, "status_tone": status_tone,
        "status_source": "work" if work is not None else "derived",
        "message": message,
        "progress": progress,
        "best_score": best_score if best_score is not None else movie.get("file_score"),
        "file_score": movie.get("file_score"),
        "next_run_at": work.get("next_run_at") if work is not None else None,
        "work": public_work,
        "works": [],
        "download": download,
        "events": events,
    }


def _decode_json(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _movie_profile(profiles: list[dict], movie: dict) -> dict:
    profile_id = movie.get("quality_profile_id")
    for profile in profiles:
        if profile.get("id") == profile_id:
            return profile
    for profile in profiles:
        if profile.get("is_default"):
            return profile
    return profiles[0] if profiles else {}


def _stage_state(context: dict) -> dict:
    """The one user-facing state label for the current work row."""
    stage = str(context.get("stage") or "")
    status = str(context.get("status") or "")
    if status == "failed_stopped" or stage == "failed":
        return {"stage": stage or "failed", "label": "Failed",
                "tone": "danger", "status": status}
    if context.get("paused") and status not in (
            "complete", "disabled", "failed_stopped"):
        return {"stage": stage or "paused", "label": "Paused",
                "tone": "warning", "status": status}
    if stage == "skipped" or status == "disabled":
        return {"stage": stage or "skipped", "label": "Skipped",
                "tone": "muted", "status": status}
    return {
        "stage": stage,
        "label": str(context.get("label") or "Pending"),
        "tone": str(context.get("stage_tone") or "muted"),
        "status": status,
    }


def movie_stage_summary(conn, movie_id: int) -> dict | None:
    """Compact stage badge data for index cards (integration hook).

    ``movies.html``/``_decorate_movies`` remain integration-owned. Calling
    this once per card gives the same label/tone/status the detail card shows;
    a future batched version should load the work and newest-event rows for
    all ids in one query instead of per card.
    """
    context = (_movie_stage_context(conn, movie_id)
               if bworker_events is not None
               else _sql_movie_stage_context(conn, movie_id))
    if context is None:
        return None
    try:
        paused = bool(bworker_store.get_state(conn).get("paused"))
    except Exception:
        paused = False
    context = dict(context)
    context["paused"] = paused
    state = _stage_state(context)
    return {
        "stage": state["stage"],
        "label": state["label"],
        "tone": state["tone"],
        "status": context.get("status"),
        "status_label": context.get("status_label"),
        "best_score": context.get("best_score"),
        "next_run_at": context.get("next_run_at"),
    }


def _movie_stage_context(conn, movie_id: int) -> dict | None:
    if bworker_events is not None:
        return bworker_events.movie_stage_context(conn, int(movie_id))
    return _sql_movie_stage_context(conn, int(movie_id))


def _movie_stage_view(conn, movie: dict, profiles: list[dict]) -> dict:
    """Everything the top card needs, built once for the server render."""
    context = dict(_movie_stage_context(conn, int(movie["id"])) or {})
    try:
        paused = bool(bworker_store.get_state(conn).get("paused"))
    except Exception:
        paused = False
    profile = _movie_profile(profiles, movie)
    work = context.get("work") or {}
    events = list(context.get("events") or [])

    context["paused"] = paused
    context["profile_name"] = profile.get("name") or "No profile"
    context["target_score"] = profile.get("target_score")
    context["max_score"] = profile.get("max_score")
    context["profile_id"] = profile.get("id")
    context["events"] = [_stage_event_view(event) for event in events]
    context["progress"] = (
        _progress_view(None, context.get("download"))
        or _progress_view(context.get("progress"))
    )
    context["state"] = _stage_state(context)
    # The state badge already says Failed/Paused. Keep the unit's own status
    # label ("Stopped after failures", "Running", ...) so the metric retains
    # the bworker detail instead of repeating the state.
    context["status_label"] = (
        "Paused" if paused and context.get("status") not in (
            "complete", "disabled", "failed_stopped")
        else context.get("status_label") or "Pending"
    )
    context["started_at"] = work.get("started_at") or (
        events[0].get("started_at") if events else None)
    context["updated_at"] = (
        work.get("updated_at")
        or (events[0].get("created_at") if events else None)
        or movie.get("last_search_at")
    )
    context["next_run_text"] = _in_label(context.get("next_run_at"))
    context["failure_count"] = int(work.get("failure_count") or 0)
    context["work_type_label"] = str(work.get("work_type") or "").replace(
        "_", " ").title() or "Movie search"
    context["stage_api_url"] = _stage_api_url(int(movie["id"]))
    first_event = context["events"][0].get("id") if context["events"] else ""
    context["signature"] = "|".join((
        str(context.get("stage") or ""),
        str(context.get("status") or ""),
        str(context.get("message") or ""),
        str(context.get("best_score") or ""),
        str((context.get("progress") or {}).get("text") or ""),
        str(first_event),
    ))
    return context


# -------------------------------------------------------------------- movies
@bp.get("/movies/add")
def add_movie_page():
    conn = _conn()
    return render_template(
        "movie_add.html",
        profiles=_profiles(conn),
        tmdb_configured=metadata.has_tmdb(conn),
        **_media_context(),
    )


@bp.get("/movies")
def movies():
    if request.args.get("add") == "1":
        return redirect(url_for("manager.add_movie_page"))
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
    candidates = library.movie_candidates_view(conn, movie)
    candidate_checks = library.movie_candidate_diagnostics(conn, movie_id)
    alternates = library.movie_alternates(conn, movie_id)
    main_identity = library.main_release_identity(conn, movie)
    profiles = _profiles(conn)
    return render_template(
        "movie.html",
        movie=movie,
        profiles=profiles,
        status=library.movie_status(conn, movie),
        stage=_movie_stage_view(conn, movie, profiles),
        history=_history_for(conn, movie_id=movie_id),
        candidates=candidates["candidates"],
        candidate_total=candidates["total"],
        candidate_probed=candidates["probed"],
        candidate_checks=candidate_checks,
        alternates=alternates,
        alternate_keys={
            f"{row['source_id']}:{row['remote_id']}" for row in alternates
            if row.get("source_id")
        },
        main_key=f"{main_identity[0]}:{main_identity[1]}" if main_identity else "",
        search_pending=_movie_search_pending(conn, movie_id),
        downloads=_active_for_movie(conn, movie_id),
        tmdb_configured=metadata.has_tmdb(conn),
        **_media_context(),
    )


# --------------------------------------------------------- series stage view
# The series page shows the aggregate bworker stage for the whole show, the
# current episode stages and the durable last-15 event history. The read-only
# helpers below reuse the generic movie helpers above (stage labels, progress
# formatting, next-run wording) and A04's series_stage_context when present.
# Nothing here writes bworker state: the web process only reads it.

_SERIES_STAGE_API_RULE = "/api/series/<int:series_id>/stage"

#: The exact vocabulary the series page promises. The short hint is what the
#: row's title attribute explains; it is also used for derived stages where
#: "Searching" alone would read as if a search were running right now.
_SERIES_STAGE_HINTS = {
    "pending": "Queued for the next bworker pass",
    "waiting_availability": "Not released yet — waiting for availability",
    "waiting_air": "Unaired — waiting for the episode to air",
    "search": "Aired — awaiting the search",
    "searching_candidates": "Scanning the catalog for candidates",
    "probing": "Probing candidate streams",
    "downloading": "Downloading the chosen release",
    "verifying": "Verifying the downloaded file",
    "renaming": "Renaming and importing the file",
    "moving": "Moving the file into the library",
    "awaiting_upgrade": "Below profile — awaiting a better release",
    "complete": "Downloaded — profile target met",
    "no_candidates": "No eligible candidate matched the profile",
    "failed": "Failed — reset the work to retry",
    "paused": "Paused",
    "skipped": "Skipped / unmonitored",
}

#: Stage preference for a season header when its episodes disagree.
_SERIES_STAGE_PRIORITY = (
    "failed", "downloading", "verifying", "renaming", "moving", "probing",
    "searching_candidates", "search", "awaiting_upgrade", "no_candidates",
    "waiting_availability", "waiting_air", "paused", "pending", "skipped",
    "complete",
)
_SERIES_STAGE_RANK = {
    stage: index for index, stage in enumerate(_SERIES_STAGE_PRIORITY)
}
_SERIES_ACTIVE_STAGES = {
    "searching_candidates", "probing", "downloading", "verifying", "renaming",
    "moving",
}


def _series_stage_api_url(series_id: int) -> str | None:
    """The per-series stage JSON route, only when stage_web is registered."""
    try:
        rules = list(current_app.url_map.iter_rules())
    except RuntimeError:
        return None
    for rule in rules:
        if rule.rule == _SERIES_STAGE_API_RULE:
            try:
                return url_for(rule.endpoint, series_id=int(series_id))
            except BuildError:
                return None
    return None


def _series_stage_meta(stage) -> dict:
    key = str(stage or "pending")
    label, tone = _STAGE_FALLBACK_LABELS.get(
        key, (key.replace("_", " ").title() or "Unknown", "muted"))
    badge = "secondary"
    if bworker_events is not None:
        try:
            info = bworker_events.stage_meta(key)
            label = str(info.get("label") or label)
            badge = str(info.get("badge") or badge)
            tone = str(info.get("tone") or tone)
        except Exception:  # pragma: no cover - keep the server render
            pass
    hint = _SERIES_STAGE_HINTS.get(key) or label
    return {
        "value": key,
        "label": label,
        "badge": badge,
        "tone": tone,
        "hint": hint,
        "title": f"{label} — {hint}" if hint and hint != label else label,
    }


def _series_work_status_meta(status) -> dict:
    key = str(status or "pending")
    label, badge, tone = _WORK_FALLBACK_LABELS.get(
        key, (key.replace("_", " ").title() or "Unknown", "secondary", "muted"))
    if bworker_events is not None:
        try:
            info = bworker_events.work_status_meta(key)
            label = str(info.get("label") or label)
            badge = str(info.get("badge") or badge)
            tone = str(info.get("tone") or tone)
        except Exception:  # pragma: no cover
            pass
    return {"value": key, "label": label, "badge": badge, "tone": tone}


def _series_event_status_meta(status) -> dict:
    key = str(status or "info")
    label, badge, tone = _EVENT_FALLBACK_LABELS.get(
        key, (key.replace("_", " ").title() or "Info", "secondary", "muted"))
    if bworker_events is not None:
        try:
            info = bworker_events.event_status_meta(key)
            label = str(info.get("label") or label)
            badge = str(info.get("badge") or badge)
            tone = str(info.get("tone") or tone)
        except Exception:  # pragma: no cover
            pass
    return {"value": key, "label": label, "badge": badge, "tone": tone}


def _series_airing(status) -> dict:
    """Normalise TMDB's free-form status into the card's Airing/Ended state."""
    text = " ".join(str(status or "").split())
    key = text.casefold()
    if key == "returning series":
        return {"state": "airing", "label": "Airing", "detail": text}
    if key in {"in production", "planned", "pilot", "upcoming"}:
        return {"state": "upcoming", "label": text or "Upcoming",
                "detail": "Not currently airing"}
    if key == "ended":
        return {"state": "ended", "label": "Ended", "detail": text}
    if key in {"canceled", "cancelled"}:
        return {"state": "ended", "label": "Canceled", "detail": text}
    if not text:
        return {"state": "unknown", "label": "Unknown",
                "detail": "No TMDB airing status"}
    return {"state": "unknown", "label": text,
            "detail": "Unrecognised TMDB airing status"}


def _series_profile(profiles: list[dict], item: dict) -> dict:
    profile_id = item.get("quality_profile_id")
    for profile in profiles:
        if profile.get("id") == profile_id:
            return profile
    for profile in profiles:
        if profile.get("is_default"):
            return profile
    return profiles[0] if profiles else {}


def _series_media_key(file_path) -> str | None:
    """Media-root-relative key for the storage delete endpoint, if confined.

    The storage API accepts only a relative key below the media root and
    refuses symlinks/``..``. An episode path outside the root (or a path that
    cannot be resolved) gets no remove action rather than a guaranteed 403.
    """
    if not file_path:
        return None
    try:
        root = Path(config.media_dir()).resolve()
        target = Path(str(file_path)).resolve()
        return target.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _series_episode_tech(episode: dict) -> dict:
    """Compact quality/audio/subtitle metadata copied from the catalog row."""
    media = episode.get("media") or {}
    resolution = episode.get("resolution") or media.get("resolution")
    if not resolution and media.get("video_height"):
        resolution = f"{int(media['video_height'])}p"
    quality = episode.get("quality_label") or episode.get("source")
    if quality and not episode.get("quality_label"):
        quality = str(quality).upper()
    video = media.get("video_codec")
    audio = [str(value) for value in (episode.get("audio_languages") or []) if value]
    if not audio and isinstance(media.get("audio_languages"), list):
        audio = [str(value) for value in media["audio_languages"] if value]
    subtitles = [str(value) for value in (episode.get("subtitle_languages") or [])
                 if value]
    if not subtitles and isinstance(media.get("subtitle_languages"), list):
        subtitles = [str(value) for value in media["subtitle_languages"] if value]
    parts = [str(value) for value in (resolution, quality, video) if value]
    if audio:
        parts.append("audio " + "/".join(audio))
    if subtitles:
        parts.append("subs " + "/".join(subtitles))
    return {
        "resolution": resolution,
        "quality": quality,
        "video": video,
        "audio": audio,
        "subtitles": subtitles,
        "summary": " · ".join(parts),
    }


def _series_episode_derived(episode: dict, series_monitored: bool,
                            target_score, upgrades: bool) -> tuple[str, str]:
    """The stage the UI can prove from the row alone (no bworker work row)."""
    if episode.get("file_path"):
        score = _number(episode.get("file_score"))
        if (upgrades and target_score and score is not None
                and score < float(target_score)):
            return "awaiting_upgrade", "complete"
        return "complete", "complete"
    if not episode.get("monitored") or not series_monitored:
        return "skipped", "disabled"
    if episode.get("download"):
        return "downloading", "running"
    air = str(episode.get("air_date") or "")[:10]
    if air and air > date.today().isoformat():
        return "waiting_air", "pending"
    return "search", "pending"


def _series_pick_work(works):
    return min(
        works,
        key=lambda row: (
            _WORK_PRIORITY.get(str(row.get("status") or ""), 9),
            -(int(row.get("updated_at") or 0)),
            -int(row.get("id") or 0),
        ),
        default=None,
    )


def _series_pick_stage(works) -> str | None:
    stages = [str(row.get("stage") or "pending") for row in works]
    if not stages:
        return None
    return min(stages, key=lambda stage: (_SERIES_STAGE_RANK.get(stage, 99), stage))


def _series_aggregate_stage(works, series_monitored: bool,
                            monitored_episodes: list[dict]
                            ) -> tuple[str, str]:
    """Fallback aggregate; mirrors bworker_events._aggregate_series."""
    by_status: dict[str, list[dict]] = {}
    for work in works:
        by_status.setdefault(str(work.get("status") or ""), []).append(work)
    if not series_monitored:
        return "skipped", "disabled"
    if by_status.get("running"):
        work = _series_pick_work(by_status["running"])
        return str(work.get("stage") or "pending"), "running"
    if by_status.get("retry"):
        work = _series_pick_work(by_status["retry"])
        return str(work.get("stage") or "failed"), "retry"
    if by_status.get("failed_stopped"):
        return "failed", "failed_stopped"
    if by_status.get("pending"):
        return _series_pick_stage(by_status["pending"]) or "pending", "pending"
    if by_status.get("complete"):
        stages = {str(work.get("stage") or "") for work in by_status["complete"]}
        return ("awaiting_upgrade" if "awaiting_upgrade" in stages
                else "complete"), "complete"
    if by_status.get("disabled"):
        return "skipped", "disabled"
    if not monitored_episodes:
        return "skipped", "disabled"
    if all(episode.get("file_path") for episode in monitored_episodes):
        return "complete", "complete"
    today = date.today().isoformat()
    if any(not episode.get("file_path")
           and (episode.get("air_date") or "0000-00-00") <= today
           for episode in monitored_episodes):
        return "search", "pending"
    if any((episode.get("air_date") or "9999-12-31") > today
           for episode in monitored_episodes):
        return "waiting_air", "pending"
    return "pending", "pending"


def _sql_series_stage_context(conn, series_id: int, item: dict,
                              episodes: list[dict], active: dict) -> dict:
    """Direct-SQL fallback for the series card when bworker_events is absent."""
    works = [dict(row) for row in conn.execute(
        "SELECT w.* FROM bworker_work w "
        "WHERE (w.entity_type = 'series' AND w.entity_id = ?) "
        "   OR (w.entity_type = 'episode' AND w.entity_id IN "
        "       (SELECT id FROM series_episodes WHERE series_id = ?)) "
        "ORDER BY w.id", (int(series_id), int(series_id)),
    )]
    works_by_episode: dict[int, list[dict]] = {}
    series_works: list[dict] = []
    for work in works:
        if str(work.get("entity_type")) == "episode":
            works_by_episode.setdefault(int(work.get("entity_id") or 0),
                                        []).append(work)
        else:
            series_works.append(work)
    series_monitored = bool(item.get("monitored"))
    monitored = [episode for episode in episodes
                 if series_monitored and bool(episode.get("monitored"))]
    monitored_ids = {int(episode["id"]) for episode in monitored}
    aggregate = [work for work in works
                 if str(work.get("entity_type")) == "episode"
                 and int(work.get("entity_id") or 0) in monitored_ids]
    stage, status = _series_aggregate_stage(aggregate, series_monitored, monitored)
    running_series = [work for work in series_works
                      if str(work.get("status") or "") == "running"]
    representative = _series_pick_work(running_series)
    if representative is not None:
        stage = str(representative.get("stage") or stage)
        status = "running"
    else:
        representative = (_series_pick_work(aggregate)
                          or _series_pick_work(series_works))

    rows = conn.execute(
        "SELECT ev.*, e.season AS event_season, e.episode AS event_number "
        "FROM bworker_events ev LEFT JOIN series_episodes e "
        "ON e.id = ev.entity_id AND ev.entity_type = 'episode' "
        "WHERE (ev.entity_type = 'series' AND ev.entity_id = ?) "
        "   OR (ev.entity_type = 'episode' AND e.series_id = ?) "
        "ORDER BY ev.id DESC LIMIT 15",
        (int(series_id), int(series_id)),
    ).fetchall()
    events: list[dict] = []
    latest_by_episode: dict[int, dict] = {}
    for row in rows:
        event = dict(row)
        stage_info = _series_stage_meta(event.get("stage"))
        status_info = _series_event_status_meta(event.get("status"))
        event.update({
            "stage_label": stage_info["label"],
            "stage_badge": stage_info["badge"],
            "stage_tone": stage_info["tone"],
            "status_label": status_info["label"],
            "status_badge": status_info["badge"],
            "status_tone": status_info["tone"],
            "progress": _decode_json(event.get("progress")),
        })
        if event.get("event_season") is not None:
            event["season"] = int(event["event_season"])
            event["episode"] = int(event["event_number"])
        events.append(event)
        if str(event.get("entity_type")) == "episode":
            latest_by_episode.setdefault(int(event.get("entity_id") or 0), event)

    target_score = None
    upgrades = True
    profile = _series_profile(
        [dict(row) for row in conn.execute("SELECT * FROM quality_profiles")],
        item,
    )
    if profile:
        try:
            summary = quality.profile_summary(profile)
            target_score = summary.get("target_score")
            upgrades = bool(summary.get("upgrades", True))
        except Exception:  # pragma: no cover - SQL fallback for display only
            target_score = None

    episode_views: list[dict] = []
    for episode in episodes:
        episode_id = int(episode["id"])
        work = _series_pick_work(works_by_episode.get(episode_id, []))
        latest_event = latest_by_episode.get(episode_id)
        derived_stage, derived_status = _series_episode_derived(
            episode, series_monitored, target_score, upgrades)
        if work is not None:
            episode_stage = str(work.get("stage") or derived_stage)
            episode_status = str(work.get("status") or "pending")
            source = "work"
        elif latest_event is not None:
            episode_stage = str(latest_event.get("stage") or derived_stage)
            episode_status = str(latest_event.get("status") or "info")
            source = "event"
        else:
            episode_stage, episode_status = derived_stage, derived_status
            source = "derived"
        stage_info = _series_stage_meta(episode_stage)
        status_info = (_series_event_status_meta(episode_status) if source == "event"
                       else _series_work_status_meta(episode_status))
        download = active.get(episode_id)
        progress = _progress_view(None, download)
        if progress is None and latest_event is not None:
            progress = _progress_view(latest_event.get("progress"))
        message = None
        if work is not None and str(work.get("status") or "") in (
                "retry", "failed_stopped"):
            message = work.get("last_error")
        if not message and latest_event is not None:
            message = latest_event.get("message")
        episode_views.append({
            "id": episode_id,
            "season": int(episode["season"]),
            "episode": int(episode["episode"]),
            "title": episode.get("title"),
            "air_date": episode.get("air_date"),
            "monitored": bool(episode.get("monitored")),
            "downloaded": bool(episode.get("file_path")),
            "stage": episode_stage,
            "stage_label": stage_info["label"],
            "stage_badge": stage_info["badge"],
            "stage_tone": stage_info["tone"],
            "status": episode_status,
            "status_label": status_info["label"],
            "status_badge": status_info["badge"],
            "status_tone": status_info["tone"],
            "status_source": source,
            "message": message,
            "progress": progress,
            "work_id": int(work["id"]) if work else None,
            "next_run_at": work.get("next_run_at") if work else None,
            "updated_at": work.get("updated_at") if work else None,
            "last_event": latest_event,
        })

    downloaded = sum(1 for episode in monitored if episode.get("file_path"))
    return {
        "stage": stage,
        "label": _series_stage_meta(stage)["label"],
        "status": status,
        "status_label": _series_work_status_meta(status)["label"],
        "status_badge": _series_work_status_meta(status)["badge"],
        "status_tone": _series_work_status_meta(status)["tone"],
        "status_source": "work" if representative is not None else "derived",
        "message": None,
        "progress": {"done": downloaded, "total": len(monitored)},
        "current_progress": None,
        "best_score": (representative.get("best_score")
                       if representative is not None else None),
        "next_run_at": (representative.get("next_run_at")
                        if representative is not None else None),
        "work": None,
        "works": [],
        "download": _progress_view(None, active.get(
            int(representative.get("entity_id") or 0))
            if representative is not None
            and str(representative.get("entity_type")) == "episode" else None),
        "counts": {
            "episodes": len(episodes),
            "monitored_episodes": len(monitored),
            "downloaded_episodes": downloaded,
        },
        "episodes": episode_views,
        "events": events,
    }


def _series_stage_context(conn, series_id: int, item: dict,
                          episodes: list[dict], active: dict) -> dict:
    if bworker_events is not None:
        try:
            context = bworker_events.series_stage_context(conn, int(series_id))
            if context:
                return dict(context)
        except Exception:
            logger.exception(
                "series stage context failed for #%s; using direct SQL",
                series_id)
    return _sql_series_stage_context(conn, series_id, item, episodes, active)


def _series_stage_view(conn, item: dict, episodes: list[dict], active: dict,
                       profiles: list[dict]) -> dict:
    """Build the top card and per-episode stage data for the server render."""
    context = _series_stage_context(conn, int(item["id"]), item, episodes, active)
    try:
        paused = bool(bworker_store.get_state(conn).get("paused"))
    except Exception:
        paused = False
    profile = _series_profile(profiles, item)
    target_score = profile.get("target_score")
    try:
        target_score = int(target_score) if target_score is not None else None
    except (TypeError, ValueError):
        target_score = None
    upgrades = bool(profile.get("upgrades", True)) if profile else True
    views = {
        int(view["id"]): view
        for view in (context.get("episodes") or [])
        if view.get("id") is not None
    }
    today = date.today().isoformat()

    for episode in episodes:
        episode_id = int(episode["id"])
        episode["download"] = active.get(episode_id)
        episode["audio_languages"] = _json_value(episode.get("audio_languages")) or []
        episode["subtitle_languages"] = (
            _json_value(episode.get("subtitle_languages")) or [])
        episode["media"] = _json_value(episode.get("media_info")) or {}
        view = views.get(episode_id) or {}
        derived_stage, derived_status = _series_episode_derived(
            episode, bool(item.get("monitored")), target_score, upgrades)
        source = str(view.get("status_source") or "derived")
        if not view or source == "derived":
            stage, status = derived_stage, derived_status
        else:
            stage = str(view.get("stage") or derived_stage)
            status = str(view.get("status") or "pending")
        stage_info = _series_stage_meta(stage)
        status_info = (_series_event_status_meta(status) if source == "event"
                       else _series_work_status_meta(status))
        message = view.get("message")
        if not message and episode["download"]:
            message = f"Download {episode['download'].get('status') or 'active'}"
        progress = _progress_view(view.get("progress"))
        if progress is None:
            progress = _progress_view(None, episode["download"])
        if progress is None and stage in _SERIES_ACTIVE_STAGES:
            progress = {
                "percent": None, "text": f"{stage_info['label']}…",
                "indeterminate": True, "kind": "stage", "status": stage,
            }
        title = stage_info["label"]
        if status_info["label"] and status_info["label"] != title:
            title = f"{title} · {status_info['label']}"
        if message:
            title = f"{title} · {message}"
        episode["stage"] = stage
        episode["stage_value"] = stage
        episode["stage_label"] = stage_info["label"]
        episode["stage_badge"] = stage_info["badge"]
        episode["stage_tone"] = stage_info["tone"]
        episode["stage_hint"] = stage_info["hint"]
        episode["stage_title"] = title
        episode["stage_status"] = status
        episode["stage_status_label"] = status_info["label"]
        episode["stage_source"] = source
        episode["stage_message"] = message
        episode["stage_progress"] = progress
        episode["stage_work_id"] = view.get("work_id")
        episode["stage_next_run_at"] = view.get("next_run_at")
        episode["tech"] = _series_episode_tech(episode)
        episode["file_key"] = _series_media_key(episode.get("file_path"))
        episode["aired"] = not episode.get("air_date") or (
            str(episode["air_date"])[:10] <= today)
        episode["file_score"] = episode.get("file_score")
        episode["score_target"] = target_score
        episode["below_target"] = bool(
            episode.get("file_path") and target_score is not None
            and episode.get("file_score") is not None
            and int(episode["file_score"]) < int(target_score))

    monitored_episodes = [episode for episode in episodes
                          if episode.get("monitored")]
    downloaded = sum(1 for episode in monitored_episodes
                     if episode.get("file_path"))
    missing = sum(
        1 for episode in monitored_episodes
        if not episode.get("file_path") and episode["aired"]
    )
    counts = {
        "episodes": len(episodes),
        "monitored": len(monitored_episodes),
        "downloaded": downloaded,
        "missing": missing,
        "unaired": sum(1 for episode in monitored_episodes
                       if not episode["aired"]),
        "all_downloaded": sum(1 for episode in episodes
                              if episode.get("file_path")),
    }

    seasons: dict[int, list[dict]] = {}
    for episode in episodes:
        seasons.setdefault(int(episode["season"]), []).append(episode)
    season_groups: list[dict] = []
    for number in sorted(seasons):
        rows = seasons[number]
        stages = [episode["stage"] for episode in rows]
        stage = min(stages, key=lambda value: (
            _SERIES_STAGE_RANK.get(value, 99), value)) if stages else "pending"
        stage_info = _series_stage_meta(stage)
        season_monitored = sum(1 for episode in rows if episode.get("monitored"))
        season_groups.append({
            "number": int(number),
            "episodes": rows,
            "episode_count": len(rows),
            "monitored_count": season_monitored,
            "downloaded_count": sum(1 for episode in rows
                                    if episode.get("file_path")),
            "all_monitored": season_monitored == len(rows) and bool(rows),
            "stage": stage_info,
        })

    stage_info = _series_stage_meta(context.get("stage"))
    status = str(context.get("status") or "pending")
    status_info = _series_work_status_meta(status)
    if paused and status not in ("complete", "disabled", "failed_stopped"):
        status_label = "Paused"
    elif status == "failed_stopped":
        status_label = "Stopped after failures"
    else:
        status_label = str(context.get("status_label") or status_info["label"])
    events = [_stage_event_view(event)
              for event in (context.get("events") or [])][:15]
    for event in events:
        if event.get("season") is not None and event.get("episode") is not None:
            event["episode_code"] = (
                f"S{int(event['season']):02d}E{int(event['episode']):02d}")
    progress = _progress_view(None, context.get("download"))
    if progress is None:
        progress = _progress_view(context.get("current_progress"))
    if progress is None:
        progress = _progress_view(context.get("progress"))
    state = {
        "stage": stage_info["value"],
        "label": stage_info["label"],
        "tone": stage_info["tone"],
        "badge": stage_info["badge"],
        "hint": stage_info["hint"],
        "status": status,
        "status_label": status_label,
        "status_tone": status_info["tone"],
        "message": context.get("message"),
        "next_run_at": context.get("next_run_at"),
        "next_run_text": _in_label(context.get("next_run_at")),
        "progress": progress,
        "best_score": context.get("best_score"),
        "paused": paused,
        "stage_api_url": _series_stage_api_url(int(item["id"])),
        "failure_count": int(
            (context.get("work") or {}).get("failure_count") or 0),
    }
    state["bworker_now"] = _series_bworker_now(state, paused)
    aggregate = context.get("progress") or {}
    done = aggregate.get("done")
    total = aggregate.get("total")
    state["signature"] = "|".join((
        str(context.get("stage") or "pending"),
        str(context.get("status") or ""),
        str(context.get("message") or ""),
        "" if context.get("best_score") is None
        else str(context.get("best_score")),
        f"{'' if done is None else done}/{'' if total is None else total}",
        str(events[0].get("id")) if events else "",
    ))
    return {
        "context": context,
        "state": state,
        "counts": counts,
        "seasons": season_groups,
        "events": events,
        "profile": profile,
        "target_score": target_score,
        "max_score": profile.get("max_score"),
        "upgrades": upgrades,
        "airing": _series_airing(item.get("status")),
        "paused": paused,
    }


def _series_bworker_now(state: dict, paused: bool) -> str:
    """One sentence for "what is bworker doing with this series right now?"."""
    stage = state["stage"]
    status = state["status"]
    label = state["label"]
    if paused and status not in ("complete", "disabled", "failed_stopped"):
        return "Bworker is paused; this series waits until it resumes."
    if stage in _SERIES_ACTIVE_STAGES or status == "running":
        return f"{label} now."
    if status == "retry":
        return f"Retry scheduled after {label.lower()}."
    if status == "failed_stopped" or stage == "failed":
        return "Stopped after failures; reset bworker work to retry."
    if stage == "paused":
        return "Paused."
    if stage == "waiting_air":
        return "Waiting for the next episode to air."
    if stage == "awaiting_upgrade":
        return "Below profile; bworker keeps looking for an upgrade."
    if stage == "no_candidates":
        return "No eligible candidate matched the profile; retrying later."
    if stage == "complete" or status == "complete":
        return "Complete — no work is due."
    if stage == "skipped":
        return "Skipped — the series or episode is unmonitored."
    return "Idle — waiting for the next scheduled search."


# -------------------------------------------------------------------- series
@bp.get("/series/add")
def add_series_page():
    conn = _conn()
    return render_template(
        "series_add.html",
        profiles=_profiles(conn),
        tmdb_configured=metadata.has_tmdb(conn),
        **_media_context(),
    )


@bp.get("/series")
def series():
    if request.args.get("add") == "1":
        return redirect(url_for("manager.add_series_page"))
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
    _decorate_series([item])
    profiles = _profiles(conn)
    view = _series_stage_view(conn, item, episodes, active, profiles)
    return render_template(
        "series_detail.html",
        series=item,
        seasons=view["seasons"],
        profiles=profiles,
        history=_history_for(conn, series_id=series_id),
        stage=view["state"],
        stage_events=view["events"],
        counts=view["counts"],
        airing=view["airing"],
        quality_profile=view["profile"],
        target_score=view["target_score"],
        max_score=view["max_score"],
        upgrades=view["upgrades"],
        bworker_paused=view["paused"],
        tmdb_configured=metadata.has_tmdb(conn),
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


def _calendar_context(conn, section: str, raw_month: str | None) -> dict:
    first, last, previous, following = _month(raw_month)
    start, end = first.isoformat(), last.isoformat()
    days: dict[str, list[dict]] = {}
    if section == "movies":
        rows = conn.execute(
            "SELECT * FROM movies WHERE release_date IS NOT NULL "
            "AND release_date BETWEEN ? AND ? ORDER BY release_date, title COLLATE NOCASE",
            (start, end),
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["kind"] = "movie"
            item["downloaded"] = bool(item.get("file_path"))
            item["poster"] = metadata.image_url("w92", item.get("poster_path"))
            days.setdefault(str(item["release_date"])[:10], []).append(item)
    else:
        rows = conn.execute(
            """
            SELECT e.*, s.title AS series_title, s.poster_path AS series_poster
            FROM series_episodes e JOIN series s ON s.id = e.series_id
            WHERE e.air_date IS NOT NULL AND e.air_date BETWEEN ? AND ?
            ORDER BY e.air_date, s.title COLLATE NOCASE, e.season, e.episode
            """,
            (start, end),
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["kind"] = "episode"
            item["downloaded"] = bool(item.get("file_path"))
            item["poster"] = metadata.image_url("w92", item.get("series_poster"))
            days.setdefault(str(item["air_date"])[:10], []).append(item)
    return {
        "section": section,
        "days": sorted(days.items()),
        "month_label": first.strftime("%B %Y"),
        "month_value": first.strftime("%Y-%m"),
        "previous": previous,
        "following": following,
    }


@bp.get("/movies/calendar")
def movies_calendar():
    return render_template(
        "calendar.html", **_calendar_context(_conn(), "movies", request.args.get("month"))
    )


@bp.get("/series/calendar")
def series_calendar():
    return render_template(
        "calendar.html", **_calendar_context(_conn(), "series", request.args.get("month"))
    )


@bp.get("/calendar")
def calendar_view():
    month = request.args.get("month")
    return redirect(url_for("manager.movies_calendar", **({"month": month} if month else {})))


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
        if profile and profile.upgrades and not profile.cutoff_met(row):
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
        if profile and profile.upgrades and not profile.cutoff_met(row):
            out.append(dict(row))
    return out


def _movie_wanted_context(conn) -> dict:
    movies = library.wanted_movies(conn)
    for movie in movies:
        movie["poster"] = metadata.image_url("w92", movie.get("poster_path"))
    return {
        "section": "movies",
        "movies": movies,
        "episodes": [],
        "cutoff_movies": _cutoff_movies(conn),
        "cutoff_episodes": [],
        "counts": _movie_counts(conn),
        "series_counts": _series_counts(conn),
    }


def _series_wanted_context(conn) -> dict:
    return {
        "section": "series",
        "movies": [],
        "episodes": library.wanted_episodes(conn),
        "cutoff_movies": [],
        "cutoff_episodes": _cutoff_episodes(conn),
        "counts": _movie_counts(conn),
        "series_counts": _series_counts(conn),
    }


@bp.get("/movies/wanted")
def movies_wanted():
    return render_template("wanted.html", **_movie_wanted_context(_conn()))


@bp.get("/series/wanted")
def series_wanted():
    return render_template("wanted.html", **_series_wanted_context(_conn()))


@bp.get("/wanted")
def wanted():
    return redirect(url_for("manager.movies_wanted"))


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
        schema=quality.builder_schema(),
        default_profile_key=str(
            db.get_setting(conn, "default_profile_key", db.DEFAULT_PROFILE_KEY)
            or db.DEFAULT_PROFILE_KEY
        ),
    )


__all__ = ["bp"]
