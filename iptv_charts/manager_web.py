"""Server-rendered pages for the movie and series library."""

from __future__ import annotations

import calendar as calendar_module
import json
import time
from datetime import date, timedelta

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
    candidates = library.movie_candidates_view(conn, movie)
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
        alternates=alternates,
        alternate_keys={
            f"{row['source_id']}:{row['remote_id']}" for row in alternates
            if row.get("source_id")
        },
        main_key=f"{main_identity[0]}:{main_identity[1]}" if main_identity else "",
        search_pending=_movie_search_pending(conn, movie_id),
        downloads=_active_for_movie(conn, movie_id),
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
