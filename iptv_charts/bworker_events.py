"""Durable per-entity bworker history and the canonical stage vocabulary.

``bworker_events`` is the item-facing complement to the RAM activity log: it
records what happened to a movie, series, episode, source or global task and
is retained across restarts. The writer prunes to the newest
``MAX_EVENTS_PER_ENTITY`` rows for each ``(entity_type, entity_id)`` in the
same transaction as the insert, so the table cannot grow without bound.

Callers control the event cadence; this module does no de-duplication. The
bworker should record on stage transitions and on finish (see
``docs/implementation/0.8.0-contracts.md``). Recording never raises into the
work handler: :func:`record` catches every failure, logs it and returns
``False``.

Transaction behaviour: when the passed connection already has an open
transaction the insert/prune run inside a savepoint and the caller keeps
control of the commit (a rollback of the caller's transaction also discards
the event). On a connection with no transaction the helper commits its own
short write. This makes the helper safe to call from a handler that is in the
middle of its own writes.

The read helpers (:func:`recent`, :func:`movie_stage_context`,
:func:`series_stage_context`, :func:`episode_stage_context`) are cheap enough
for a server-rendered page route: they use the existing
``(entity_type, entity_id, id DESC)`` index and perform no provider or
network work. They are also what ``stage_web`` exposes over JSON, so the page
and the API always agree.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import date
from typing import Any, Iterable, Mapping

from . import bworker_store as store

logger = logging.getLogger("iptv_charts.bworker_events")

#: The polymorphic entity types allowed by the schema.
ENTITY_TYPES = ("movie", "series", "episode", "source", "global")

#: Event statuses allowed by the schema and the 0.8.0 contract.
EVENT_STATUSES = ("started", "progress", "succeeded", "failed", "skipped",
                  "info")

#: Canonical stage names used by the bworker and the item UI.
STAGES = (
    "pending",
    "waiting_availability",
    "waiting_air",
    "search",
    "searching_candidates",
    "probing",
    "downloading",
    "verifying",
    "renaming",
    "moving",
    "awaiting_upgrade",
    "complete",
    "no_candidates",
    "failed",
    "paused",
    "skipped",
)

#: How many events each entity keeps. Writers enforce this on every insert.
MAX_EVENTS_PER_ENTITY = 15

#: Work-row statuses from ``bworker_work.status``.
WORK_STATUSES = ("pending", "running", "retry", "failed_stopped", "disabled",
                 "complete")

#: Lower value means the unit is more "current" when several rows exist.
WORK_STATUS_PRIORITY = {
    "running": 0,
    "retry": 1,
    "failed_stopped": 2,
    "pending": 3,
    "complete": 4,
    "disabled": 5,
}

# Stage -> (label, badge, tone, active, terminal). ``badge`` mirrors the
# Bootstrap contextual suffixes used by the vendored CSS; ``tone`` is a
# semantic hint for feature CSS. No colour is hard-coded here.
_STAGE_DETAILS: dict[str, tuple[str, str, str, bool, bool]] = {
    "pending": ("Pending", "secondary", "muted", False, False),
    "waiting_availability": ("Waiting for availability", "info", "waiting",
                             False, False),
    "waiting_air": ("Waiting for air", "info", "waiting", False, False),
    "search": ("Searching", "info", "active", True, False),
    "searching_candidates": ("Searching candidates", "info", "active", True,
                             False),
    "probing": ("Probing", "primary", "active", True, False),
    "downloading": ("Downloading", "primary", "active", True, False),
    "verifying": ("Verifying", "primary", "active", True, False),
    "renaming": ("Renaming", "primary", "active", True, False),
    "moving": ("Moving", "primary", "active", True, False),
    "awaiting_upgrade": ("Awaiting upgrade", "warning", "waiting", False,
                         False),
    "complete": ("Complete", "success", "success", False, True),
    "no_candidates": ("No candidates", "warning", "warning", False, False),
    "failed": ("Failed", "danger", "danger", False, True),
    "paused": ("Paused", "warning", "warning", False, False),
    "skipped": ("Skipped", "secondary", "muted", False, True),
}

_EVENT_STATUS_DETAILS: dict[str, tuple[str, str, str, bool]] = {
    "started": ("Started", "info", "active", False),
    "progress": ("In progress", "primary", "active", False),
    "succeeded": ("Succeeded", "success", "success", True),
    "failed": ("Failed", "danger", "danger", True),
    "skipped": ("Skipped", "secondary", "muted", True),
    "info": ("Info", "secondary", "muted", False),
}

_WORK_STATUS_DETAILS: dict[str, tuple[str, str, str, bool]] = {
    "pending": ("Pending", "secondary", "waiting", False),
    "running": ("Running", "primary", "active", False),
    "retry": ("Retry scheduled", "warning", "warning", False),
    "failed_stopped": ("Stopped after failures", "danger", "danger", True),
    "disabled": ("Disabled", "secondary", "muted", True),
    "complete": ("Complete", "success", "success", True),
}


def _build_meta(details: Mapping[str, tuple], *, known: bool = True) -> dict:
    return {
        stage: {
            "value": stage,
            "label": label,
            "badge": badge,
            "tone": tone,
            "active": active,
            "terminal": terminal,
            "known": known,
        }
        for stage, (label, badge, tone, active, terminal) in details.items()
    }


def _build_status_meta(details: Mapping[str, tuple]) -> dict:
    return {
        status: {
            "value": status,
            "label": label,
            "badge": badge,
            "tone": tone,
            "terminal": terminal,
            "known": True,
        }
        for status, (label, badge, tone, terminal) in details.items()
    }


STAGE_META = _build_meta(_STAGE_DETAILS)
STAGE_LABELS = {stage: meta["label"] for stage, meta in STAGE_META.items()}
STAGE_BADGES = {stage: meta["badge"] for stage, meta in STAGE_META.items()}
EVENT_STATUS_META = _build_status_meta(_EVENT_STATUS_DETAILS)
EVENT_STATUS_LABELS = {
    status: meta["label"] for status, meta in EVENT_STATUS_META.items()
}
WORK_STATUS_META = _build_status_meta(_WORK_STATUS_DETAILS)
WORK_STATUS_LABELS = {
    status: meta["label"] for status, meta in WORK_STATUS_META.items()
}

#: Aggregate stage preference for a series with several pending episodes.
_AGGREGATE_STAGE_ORDER = (
    "failed", "downloading", "verifying", "renaming", "moving", "probing",
    "searching_candidates", "search", "awaiting_upgrade", "no_candidates",
    "waiting_availability", "waiting_air", "pending", "paused", "skipped",
    "complete",
)
_AGGREGATE_STAGE_PRIORITY = {
    stage: index for index, stage in enumerate(_AGGREGATE_STAGE_ORDER)
}

# Deliberately conservative: a URL in an exception message can carry a
# provider host or token, and none of it belongs in the web response. The raw
# message remains in SQLite for diagnostics.
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s<>\"']+")

_SAVEPOINT = "bworker_events_write"


# --------------------------------------------------------------- labels/meta
def _prettify(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    return " ".join(
        part.capitalize() for part in text.replace("-", "_").split("_") if part
    ) or "Unknown"


def stage_meta(stage: Any) -> dict:
    """Public metadata for a stage value.

    Unknown stage names are preserved (value/label prettified) and marked
    ``known=False`` so the UI can still show a badge rather than a blank.
    """
    key = str(stage or "").strip()
    meta = STAGE_META.get(key)
    if meta is not None:
        return dict(meta)
    return {
        "value": key or "unknown",
        "label": _prettify(key),
        "badge": "secondary",
        "tone": "muted",
        "active": False,
        "terminal": False,
        "known": False,
    }


def stage_label(stage: Any) -> str:
    return stage_meta(stage)["label"]


def event_status_meta(status: Any) -> dict:
    key = str(status or "").strip()
    meta = EVENT_STATUS_META.get(key)
    if meta is not None:
        return dict(meta)
    return {
        "value": key or "unknown",
        "label": _prettify(key),
        "badge": "secondary",
        "tone": "muted",
        "terminal": False,
        "known": False,
    }


def event_status_label(status: Any) -> str:
    return event_status_meta(status)["label"]


def work_status_meta(status: Any) -> dict:
    key = str(status or "").strip()
    meta = WORK_STATUS_META.get(key)
    if meta is not None:
        return dict(meta)
    return {
        "value": key or "unknown",
        "label": _prettify(key),
        "badge": "secondary",
        "tone": "muted",
        "terminal": False,
        "known": False,
    }


def work_status_label(status: Any) -> str:
    return work_status_meta(status)["label"]


def status_meta(status: Any, *, kind: str | None = None) -> dict:
    """Label a stage-status or work-status with an optional explicit kind."""
    if kind == "event":
        return event_status_meta(status)
    if kind == "work":
        return work_status_meta(status)
    if str(status or "") in EVENT_STATUS_META:
        return event_status_meta(status)
    return work_status_meta(status)


def status_label(status: Any, *, kind: str | None = None) -> str:
    return status_meta(status, kind=kind)["label"]


def stages_payload() -> dict:
    """Serialisable stage/status vocabulary for ``GET /api/stages``."""
    return {
        "stages": [stage_meta(stage) for stage in STAGES],
        "stage_labels": dict(STAGE_LABELS),
        "event_statuses": [event_status_meta(status)
                           for status in EVENT_STATUSES],
        "work_statuses": [work_status_meta(status)
                          for status in WORK_STATUSES],
    }


def redact_text(value: Any) -> str | None:
    """Strip URLs (which may carry provider hosts/tokens) from user-facing text."""
    if value is None:
        return None
    return _URL_RE.sub("[redacted-url]", str(value))


# --------------------------------------------------------------- persistence
def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _encode_progress(progress: Any) -> str | None:
    if progress is None:
        return None
    if isinstance(progress, str):
        try:
            json.loads(progress)
        except (TypeError, ValueError):
            return json.dumps(progress, ensure_ascii=False)
        return progress
    try:
        return json.dumps(progress, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _decode_progress(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _default_work_type(entity_type: str) -> str:
    return {
        "movie": "movie_search",
        "series": "series_search",
        "episode": "episode_search",
        "source": "playlist_sync",
        "global": "global",
    }.get(entity_type, "global")


def _normalise_entity(entity_type: Any, entity_id: Any
                      ) -> tuple[str, int | None] | None:
    etype = str(entity_type or "").strip().lower()
    if etype not in ENTITY_TYPES:
        return None
    if etype == "global":
        return etype, None
    try:
        return etype, int(entity_id)
    except (TypeError, ValueError):
        return None


def _run_write(conn, operation):
    """Run ``operation`` atomically, joining a caller transaction if present."""
    in_transaction = bool(getattr(conn, "in_transaction", False))
    if in_transaction:
        conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        result = operation()
        if in_transaction:
            conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        else:
            conn.commit()
        return result
    except Exception:
        try:
            if in_transaction:
                conn.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
                conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
            else:
                conn.rollback()
        except Exception:
            pass
        raise


def _trim(conn, entity_type: str, entity_id: int | None,
          keep: int) -> int:
    """Delete rows older than the newest ``keep`` for one entity."""
    oldest_kept = conn.execute(
        "SELECT id FROM bworker_events "
        "WHERE entity_type = ? AND entity_id IS ? "
        "ORDER BY id DESC LIMIT 1 OFFSET ?",
        (entity_type, entity_id, max(0, int(keep) - 1)),
    ).fetchone()
    if oldest_kept is None:
        return 0
    cursor = conn.execute(
        "DELETE FROM bworker_events "
        "WHERE entity_type = ? AND entity_id IS ? AND id < ?",
        (entity_type, entity_id, int(oldest_kept["id"])),
    )
    return max(0, int(cursor.rowcount or 0))


def record(conn, *, work_id: Any = None, work_type: str = "",
           entity_type: str, entity_id: Any = None, stage: str,
           status: str, message: Any = None, progress: Any = None,
           started_at: Any = None, finished_at: Any = None,
           now: Any = None) -> bool:
    """Append one durable event and prune the entity to 15 rows.

    Returns ``True`` when the row was written. Any failure (bad input, a
    locked or closed database, a vanished work row) is logged and reported as
    ``False``; it never propagates into the bworker.
    """
    try:
        normalised = _normalise_entity(entity_type, entity_id)
        if normalised is None:
            logger.warning("bworker_events: ignored invalid entity %r/%r",
                           entity_type, entity_id)
            return False
        etype, eid = normalised
        stage_value = str(stage or "").strip().lower()
        if not stage_value:
            return False
        status_value = str(status or "").strip().lower()
        if status_value not in EVENT_STATUSES:
            return False
        wid = _int_or_none(work_id)
        wtype = str(work_type or "").strip() or _default_work_type(etype)
        message_value = None if message is None else str(message)[:2000]
        progress_value = _encode_progress(progress)
        start = _int_or_none(started_at)
        finish = _int_or_none(finished_at)
        created = _int_or_none(now)
        if created is None:
            created = int(time.time())

        def _write(link_work_id: int | None):
            def operation():
                cursor = conn.execute(
                    "INSERT INTO bworker_events "
                    "(work_id, work_type, entity_type, entity_id, stage, "
                    " status, message, progress, started_at, finished_at, "
                    " created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (link_work_id, wtype, etype, eid, stage_value,
                     status_value, message_value, progress_value, start,
                     finish, created),
                )
                _trim(conn, etype, eid, MAX_EVENTS_PER_ENTITY)
                return int(cursor.lastrowid)

            return _run_write(conn, operation)

        try:
            _write(wid)
        except sqlite3.IntegrityError:
            # The work row can be removed between the caller claiming it and
            # the history write. History is more valuable than the link, so
            # retry once with a NULL work_id (the schema allows it).
            if wid is None:
                raise
            _write(None)
        return True
    except Exception as exc:
        logger.warning("bworker_events: could not record %s/%s event: %s",
                       entity_type, entity_id, exc)
        return False


def prune(conn, entity_type: str, entity_id: Any = None,
          keep: int = MAX_EVENTS_PER_ENTITY) -> int:
    """Enforce the retention limit for one entity and return rows removed.

    Idempotent: a second call returns 0. When the connection is inside a
    caller transaction the delete joins it; otherwise this method commits.
    """
    try:
        normalised = _normalise_entity(entity_type, entity_id)
        if normalised is None:
            return 0
        etype, eid = normalised
        limit = max(1, int(keep))
        return int(_run_write(conn, lambda: _trim(conn, etype, eid, limit)))
    except Exception as exc:
        logger.warning("bworker_events: could not prune %s/%s: %s",
                       entity_type, entity_id, exc)
        return 0


def _event_view(row: Mapping[str, Any]) -> dict:
    item = dict(row)
    stage = str(item.get("stage") or "")
    status = str(item.get("status") or "")
    stage_info = stage_meta(stage)
    status_info = event_status_meta(status)
    view = {
        "id": int(item["id"]),
        "work_id": item.get("work_id"),
        "work_type": item.get("work_type"),
        "entity_type": item.get("entity_type"),
        "entity_id": item.get("entity_id"),
        "stage": stage,
        "stage_label": stage_info["label"],
        "stage_badge": stage_info["badge"],
        "stage_tone": stage_info["tone"],
        "status": status,
        "status_label": status_info["label"],
        "status_badge": status_info["badge"],
        "status_tone": status_info["tone"],
        "message": redact_text(item.get("message")),
        "progress": _decode_progress(item.get("progress")),
        "started_at": item.get("started_at"),
        "finished_at": item.get("finished_at"),
        "created_at": item.get("created_at"),
    }
    started = item.get("started_at")
    finished = item.get("finished_at")
    if started is not None and finished is not None:
        try:
            view["duration_seconds"] = max(0, int(finished) - int(started))
        except (TypeError, ValueError):
            pass
    return view


def _event_rows(conn, *, entity_type: str, entity_id: int | None,
                limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM bworker_events "
        "WHERE entity_type = ? AND entity_id IS ? "
        "ORDER BY id DESC LIMIT ?",
        (entity_type, entity_id, max(1, min(int(limit), 100))),
    ).fetchall()
    return [_event_view(row) for row in rows]


def recent(conn, entity_type: str, entity_id: Any = None,
           limit: int = MAX_EVENTS_PER_ENTITY) -> list[dict]:
    """Newest-first events for one movie/series/episode/source/global entity.

    Each row carries the compact ``stage_label`` and ``status_label`` plus
    badge/tone hints. URLs are redacted before the row leaves this module.
    """
    normalised = _normalise_entity(entity_type, entity_id)
    if normalised is None:
        raise ValueError(f"Unknown history entity type: {entity_type!r}")
    etype, eid = normalised
    return _event_rows(conn, entity_type=etype, entity_id=eid, limit=limit)


def record_for_work(conn, work: Mapping[str, Any] | Any, stage: str,
                    status: str, message: Any = None, progress: Any = None,
                    *, started_at: Any = None, finished_at: Any = None,
                    now: Any = None) -> bool:
    """Record a stage transition for a ``bworker_work`` row.

    ``work`` may be the dict from ``bworker_store.get_work``/``claim_work``,
    a ``sqlite3.Row`` or ``WorkContext.work``; only identity fields are read.
    """
    try:
        values = dict(work) if work is not None else {}
    except (TypeError, ValueError):
        return False
    if not values:
        return False
    return record(
        conn,
        work_id=values.get("id") or values.get("work_id"),
        work_type=values.get("work_type") or "",
        entity_type=values.get("entity_type") or "",
        entity_id=values.get("entity_id"),
        stage=stage,
        status=status,
        message=message,
        progress=progress,
        started_at=started_at,
        finished_at=finished_at,
        now=now,
    )


def _record_stage(conn, entity_type: str, entity_id: Any, stage: str,
                  status: str, message: Any, progress: Any,
                  work_id: Any, work_type: str, started_at: Any,
                  finished_at: Any, now: Any) -> bool:
    return record(
        conn,
        work_id=work_id,
        work_type=work_type,
        entity_type=entity_type,
        entity_id=entity_id,
        stage=stage,
        status=status,
        message=message,
        progress=progress,
        started_at=started_at,
        finished_at=finished_at,
        now=now,
    )


def record_movie_stage(conn, movie_id: int, stage: str, status: str,
                       message: Any = None, progress: Any = None, *,
                       work_id: Any = None, work_type: str = "movie_search",
                       started_at: Any = None, finished_at: Any = None,
                       now: Any = None) -> bool:
    """Convenience wrapper for movie events (``work_id`` optional)."""
    return _record_stage(conn, "movie", movie_id, stage, status, message,
                         progress, work_id, work_type, started_at,
                         finished_at, now)


def record_series_stage(conn, series_id: int, stage: str, status: str,
                        message: Any = None, progress: Any = None, *,
                        work_id: Any = None,
                        work_type: str = "series_search",
                        started_at: Any = None, finished_at: Any = None,
                        now: Any = None) -> bool:
    """Convenience wrapper for series-level events."""
    return _record_stage(conn, "series", series_id, stage, status, message,
                         progress, work_id, work_type, started_at,
                         finished_at, now)


def record_episode_stage(conn, episode_id: int, stage: str, status: str,
                         message: Any = None, progress: Any = None, *,
                         work_id: Any = None,
                         work_type: str = "episode_search",
                         started_at: Any = None, finished_at: Any = None,
                         now: Any = None) -> bool:
    """Convenience wrapper for episode events."""
    return _record_stage(conn, "episode", episode_id, stage, status, message,
                         progress, work_id, work_type, started_at,
                         finished_at, now)


# -------------------------------------------------------------- work/entity
def _pick_work(works: Iterable[Mapping[str, Any]]) -> dict | None:
    rows = [work for work in works if work]
    if not rows:
        return None
    return min(
        rows,
        key=lambda work: (
            WORK_STATUS_PRIORITY.get(str(work.get("status") or ""), 9),
            -(_int_or_none(work.get("updated_at")) or 0),
            -(_int_or_none(work.get("id")) or 0),
        ),
    )


def _public_work(work: Mapping[str, Any] | None) -> dict | None:
    if work is None:
        return None
    stage = str(work.get("stage") or "")
    status = str(work.get("status") or "")
    stage_info = stage_meta(stage)
    status_info = work_status_meta(status)
    item = {
        "id": _int_or_none(work.get("id")),
        "work_key": work.get("work_key"),
        "work_type": work.get("work_type"),
        "entity_type": work.get("entity_type"),
        "entity_id": work.get("entity_id"),
        "enabled": bool(work.get("enabled")),
        "stage": stage,
        "stage_label": stage_info["label"],
        "stage_badge": stage_info["badge"],
        "stage_tone": stage_info["tone"],
        "status": status,
        "status_label": status_info["label"],
        "status_badge": status_info["badge"],
        "status_tone": status_info["tone"],
        "failure_count": work.get("failure_count"),
        "next_run_at": work.get("next_run_at"),
        "best_score": work.get("best_score"),
        "current_download_id": work.get("current_download_id"),
        "profile_revision": work.get("profile_revision"),
        "last_error": redact_text(work.get("last_error")),
        "message": redact_text(work.get("last_error")),
        "started_at": work.get("started_at"),
        "finished_at": work.get("finished_at"),
        "last_started_at": work.get("last_started_at"),
        "last_finished_at": work.get("last_finished_at"),
        "updated_at": work.get("updated_at"),
    }
    payload = work.get("payload")
    if isinstance(payload, Mapping) and str(work.get("entity_type")) == "episode":
        item["season"] = payload.get("season")
        item["episode"] = payload.get("episode")
    return item


def _download_row_view(row: Mapping[str, Any] | None) -> dict | None:
    """Public download projection; provider ids, names and paths stay private."""
    if row is None:
        return None
    item = dict(row)
    total = item.get("bytes_total")
    done = int(item.get("bytes_done") or 0)
    fraction = (done / total) if total else None
    return {
        "id": int(item["id"]),
        "kind": item.get("kind"),
        "status": item.get("status"),
        "title": item.get("title"),
        "mode": item.get("mode"),
        "resolution": item.get("resolution"),
        "source": item.get("source"),
        "quality_label": item.get("quality_label"),
        "bytes_done": done,
        "bytes_total": total,
        "fraction": fraction,
        "percent": round(fraction * 100, 1) if fraction is not None else None,
        "attempts": item.get("attempts"),
        "max_attempts": item.get("max_attempts"),
        "score": item.get("score"),
        "requested_at": item.get("requested_at"),
        "started_at": item.get("started_at"),
        "finished_at": item.get("finished_at"),
    }


def _download_view(conn, *, download_id: Any = None,
                   movie_id: int | None = None,
                   episode_id: int | None = None,
                   series_id: int | None = None) -> dict | None:
    """Look up one download by id or by the entity's current active download."""
    row = None
    if download_id is not None:
        row = conn.execute(
            "SELECT * FROM downloads WHERE id = ?",
            (int(download_id),),
        ).fetchone()
    active = tuple(store.ACTIVE_DOWNLOAD_STATUSES)
    placeholders = ",".join("?" for _ in active)
    if row is None and movie_id is not None:
        row = conn.execute(
            "SELECT * FROM downloads WHERE movie_id = ? "
            f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (int(movie_id), *active),
        ).fetchone()
    if row is None and episode_id is not None:
        row = conn.execute(
            "SELECT * FROM downloads WHERE episode_id = ? "
            f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (int(episode_id), *active),
        ).fetchone()
    if row is None and series_id is not None:
        row = conn.execute(
            "SELECT * FROM downloads WHERE series_id = ? "
            f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (int(series_id), *active),
        ).fetchone()
    return _download_row_view(row)


def _download_progress(download: Mapping[str, Any] | None) -> dict | None:
    if not download:
        return None
    return {
        "done": download.get("bytes_done"),
        "total": download.get("bytes_total"),
        "percent": download.get("percent"),
    }


def _latest_event_progress(conn, *, entity_type: str,
                           entity_id: int | None = None,
                           series_id: int | None = None) -> Any:
    if series_id is not None:
        row = conn.execute(
            "SELECT ev.progress FROM bworker_events ev "
            "LEFT JOIN series_episodes e "
            "ON e.id = ev.entity_id AND ev.entity_type = 'episode' "
            "WHERE ((ev.entity_type = 'series' AND ev.entity_id = ?) "
            "   OR (ev.entity_type = 'episode' AND e.series_id = ?)) "
            "AND ev.progress IS NOT NULL ORDER BY ev.id DESC LIMIT 1",
            (int(series_id), int(series_id)),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT progress FROM bworker_events "
            "WHERE entity_type = ? AND entity_id IS ? "
            "AND progress IS NOT NULL ORDER BY id DESC LIMIT 1",
            (entity_type, entity_id),
        ).fetchone()
    return _decode_progress(row["progress"]) if row is not None else None


def _message_for(work: Mapping[str, Any] | None,
                 latest_event: Mapping[str, Any] | None) -> str | None:
    if work is not None and str(work.get("status") or "") in (
            "retry", "failed_stopped"):
        error = redact_text(work.get("last_error"))
        if error:
            return error
    if latest_event is not None and latest_event.get("message"):
        return latest_event["message"]
    if work is not None and work.get("last_error"):
        return redact_text(work.get("last_error"))
    return None


def _status_view(work: Mapping[str, Any] | None,
                 latest_event: Mapping[str, Any] | None,
                 derived_status: str) -> tuple[str, dict, str]:
    if work is not None:
        status = str(work.get("status") or "pending")
        return status, work_status_meta(status), "work"
    if latest_event is not None:
        status = str(latest_event.get("status") or "info")
        return status, event_status_meta(status), "event"
    return derived_status, work_status_meta(derived_status), "derived"


def _movie_row(conn, movie_id: int):
    return conn.execute(
        "SELECT id, title, year, release_date, status, monitored, file_path, "
        "file_score, quality_profile_id FROM movies WHERE id = ?",
        (int(movie_id),),
    ).fetchone()


def _series_row(conn, series_id: int):
    return conn.execute(
        "SELECT id, title, status, monitored, quality_profile_id "
        "FROM series WHERE id = ?",
        (int(series_id),),
    ).fetchone()


def _episode_row(conn, episode_id: int, series_id: int | None = None):
    sql = (
        "SELECT e.*, s.title AS series_title, s.monitored AS series_monitored "
        "FROM series_episodes e JOIN series s ON s.id = e.series_id "
        "WHERE e.id = ?"
    )
    params: list[Any] = [int(episode_id)]
    if series_id is not None:
        sql += " AND e.series_id = ?"
        params.append(int(series_id))
    return conn.execute(sql, params).fetchone()


def _movie_derived(movie: Mapping[str, Any]) -> tuple[str, str]:
    if movie.get("file_path"):
        return "complete", "complete"
    if not bool(movie.get("monitored")):
        return "skipped", "disabled"
    stage, _next = store.movie_initial_stage(dict(movie))
    return stage, "pending"


def _episode_derived(episode: Mapping[str, Any]) -> tuple[str, str]:
    if episode.get("file_path"):
        return "complete", "complete"
    if not bool(episode.get("monitored")) or not bool(
            episode.get("series_monitored")):
        return "skipped", "disabled"
    stage, _next = store.episode_initial_stage(episode.get("air_date"))
    return stage, "pending"


def _entity_payload(kind: str, entity: Mapping[str, Any]) -> dict:
    if kind == "movie":
        return {
            "kind": "movie",
            "id": int(entity["id"]),
            "label": entity.get("title"),
            "year": entity.get("year"),
        }
    if kind == "series":
        return {
            "kind": "series",
            "id": int(entity["id"]),
            "label": entity.get("title"),
        }
    return {
        "kind": "episode",
        "id": int(entity["id"]),
        "label": (
            f"{entity.get('series_title')} S{int(entity['season']):02d}"
            f"E{int(entity['episode']):02d}"
        ),
        "series_id": int(entity["series_id"]),
        "season": int(entity["season"]),
        "episode": int(entity["episode"]),
        "title": entity.get("title"),
    }


# ---------------------------------------------------------- single-entity ctx
def movie_stage_context(conn, movie_id: int) -> dict | None:
    """Current stage/progress/history for one movie, or ``None`` if missing.

    Safe to call from a server-rendered detail route; it performs four
    indexed reads and no network work.
    """
    movie_row = _movie_row(conn, movie_id)
    if movie_row is None:
        return None
    movie = dict(movie_row)
    works = store.work_for_entity(conn, "movie", int(movie_id))
    work = _pick_work(works)
    event_views = _event_rows(conn, entity_type="movie",
                              entity_id=int(movie_id),
                              limit=MAX_EVENTS_PER_ENTITY)
    latest = event_views[0] if event_views else None
    download = _download_view(
        conn,
        download_id=(work or {}).get("current_download_id"),
        movie_id=int(movie_id),
    )
    derived_stage, derived_status = _movie_derived(movie)
    if work is not None:
        stage = str(work.get("stage") or derived_stage)
    elif latest is not None:
        stage = str(latest.get("stage") or derived_stage)
    else:
        stage = derived_stage
    status, status_info, status_source = _status_view(
        work, latest, derived_status)
    progress = _latest_event_progress(
        conn, entity_type="movie", entity_id=int(movie_id))
    if progress is None:
        progress = _download_progress(download)
    best_score = work.get("best_score") if work is not None else None
    return {
        "entity": _entity_payload("movie", movie),
        "stage": stage,
        "label": stage_label(stage),
        "status": status,
        "status_label": status_info["label"],
        "status_badge": status_info["badge"],
        "status_tone": status_info["tone"],
        "status_source": status_source,
        "message": _message_for(work, latest),
        "progress": progress,
        "best_score": best_score if best_score is not None
        else movie.get("file_score"),
        "file_score": movie.get("file_score"),
        "next_run_at": work.get("next_run_at") if work is not None else None,
        "work": _public_work(work),
        "works": [_public_work(row) for row in works],
        "download": download,
        "events": event_views,
    }


def episode_stage_context(conn, series_id: int,
                          episode_id: int) -> dict | None:
    """Current stage/history for one episode of a series, or ``None``.

    ``None`` covers both a missing episode and an episode that belongs to a
    different series, which the API maps to 404.
    """
    episode_row = _episode_row(conn, episode_id, series_id)
    if episode_row is None:
        return None
    episode = dict(episode_row)
    works = store.work_for_entity(conn, "episode", int(episode_id))
    work = _pick_work(works)
    event_views = _event_rows(conn, entity_type="episode",
                              entity_id=int(episode_id),
                              limit=MAX_EVENTS_PER_ENTITY)
    latest = event_views[0] if event_views else None
    download = _download_view(
        conn,
        download_id=(work or {}).get("current_download_id"),
        episode_id=int(episode_id),
    )
    derived_stage, derived_status = _episode_derived(episode)
    if work is not None:
        stage = str(work.get("stage") or derived_stage)
    elif latest is not None:
        stage = str(latest.get("stage") or derived_stage)
    else:
        stage = derived_stage
    status, status_info, status_source = _status_view(
        work, latest, derived_status)
    progress = _latest_event_progress(
        conn, entity_type="episode", entity_id=int(episode_id))
    if progress is None:
        progress = _download_progress(download)
    best_score = work.get("best_score") if work is not None else None
    return {
        "entity": _entity_payload("episode", episode),
        "stage": stage,
        "label": stage_label(stage),
        "status": status,
        "status_label": status_info["label"],
        "status_badge": status_info["badge"],
        "status_tone": status_info["tone"],
        "status_source": status_source,
        "message": _message_for(work, latest),
        "progress": progress,
        "best_score": best_score if best_score is not None
        else episode.get("file_score"),
        "file_score": episode.get("file_score"),
        "next_run_at": work.get("next_run_at") if work is not None else None,
        "work": _public_work(work),
        "works": [_public_work(row) for row in works],
        "download": download,
        "events": event_views,
    }


# -------------------------------------------------------------------- series
def _series_work_rows(conn, series_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT w.* FROM bworker_work w "
        "WHERE (w.entity_type = 'series' AND w.entity_id = ?) "
        "   OR (w.entity_type = 'episode' AND w.entity_id IN "
        "       (SELECT id FROM series_episodes WHERE series_id = ?)) "
        "ORDER BY w.id",
        (int(series_id), int(series_id)),
    ).fetchall()
    return [work for work in (store.normalise(row) for row in rows) if work]


def _pick_pending_stage(rows: Iterable[Mapping[str, Any]]) -> str | None:
    stages = [str(row.get("stage") or "pending") for row in rows]
    if not stages:
        return None
    return min(stages,
               key=lambda stage: (_AGGREGATE_STAGE_PRIORITY.get(stage, 99),
                                  stage))


def _aggregate_series(works: list[dict], series_monitored: bool,
                      monitored_episodes: list[Mapping[str, Any]]
                      ) -> tuple[str, str, str]:
    """Return ``(stage, status, source)`` for a series' current work."""
    by_status: dict[str, list[dict]] = {}
    for work in works:
        by_status.setdefault(str(work.get("status") or ""), []).append(work)
    if not series_monitored:
        return "skipped", "disabled", "derived"
    if by_status.get("running"):
        work = _pick_work(by_status["running"])
        return str(work.get("stage") or "pending"), "running", "work"
    if by_status.get("retry"):
        work = _pick_work(by_status["retry"])
        return str(work.get("stage") or "failed"), "retry", "work"
    if by_status.get("failed_stopped"):
        return "failed", "failed_stopped", "work"
    if by_status.get("pending"):
        stage = _pick_pending_stage(by_status["pending"]) or "pending"
        return stage, "pending", "work"
    if by_status.get("complete"):
        stages = {str(work.get("stage") or "") for work in by_status["complete"]}
        stage = "awaiting_upgrade" if "awaiting_upgrade" in stages else "complete"
        return stage, "complete", "work"
    if by_status.get("disabled"):
        return "skipped", "disabled", "work"
    # No work rows: derive from files and air dates. This keeps a legacy or
    # not-yet-seeded library readable instead of showing an empty status.
    if not monitored_episodes:
        return "skipped", "disabled", "derived"
    if all(episode.get("file_path") for episode in monitored_episodes):
        return "complete", "complete", "derived"
    today = date.today().isoformat()
    aired_missing = any(
        not episode.get("file_path")
        and (episode.get("air_date") or "0000-00-00") <= today
        for episode in monitored_episodes
    )
    if not aired_missing:
        unaired = any(
            (episode.get("air_date") or "9999-12-31") > today
            for episode in monitored_episodes
        )
        if unaired:
            return "waiting_air", "pending", "derived"
    return "pending", "pending", "derived"


def _series_latest_events(conn, series_id: int) -> tuple[list[dict],
                                                         dict[int, dict]]:
    """Recent series+episode events and the latest event per episode."""
    rows = conn.execute(
        "SELECT ev.*, e.season AS event_season, e.episode AS event_number "
        "FROM bworker_events ev "
        "LEFT JOIN series_episodes e "
        "ON e.id = ev.entity_id AND ev.entity_type = 'episode' "
        "WHERE (ev.entity_type = 'series' AND ev.entity_id = ?) "
        "   OR (ev.entity_type = 'episode' AND e.series_id = ?) "
        "ORDER BY ev.id DESC LIMIT ?",
        (int(series_id), int(series_id), MAX_EVENTS_PER_ENTITY),
    ).fetchall()
    merged: list[dict] = []
    for row in rows:
        view = _event_view(row)
        if row["event_season"] is not None:
            view["season"] = int(row["event_season"])
            view["episode"] = int(row["event_number"])
        merged.append(view)

    latest_rows = conn.execute(
        "SELECT ev.* FROM bworker_events ev "
        "JOIN series_episodes e "
        "ON e.id = ev.entity_id AND ev.entity_type = 'episode' "
        "WHERE e.series_id = ? AND ev.id = ("
        "  SELECT MAX(ev2.id) FROM bworker_events ev2 "
        "  WHERE ev2.entity_type = 'episode' AND ev2.entity_id = ev.entity_id"
        ")",
        (int(series_id),),
    ).fetchall()
    latest_by_episode = {
        int(row["entity_id"]): _event_view(row) for row in latest_rows
        if row["entity_id"] is not None
    }
    return merged, latest_by_episode


def _series_episode_progress(conn, series_id: int) -> dict[int, Any]:
    rows = conn.execute(
        "SELECT ev.entity_id, ev.progress FROM bworker_events ev "
        "JOIN series_episodes e "
        "ON e.id = ev.entity_id AND ev.entity_type = 'episode' "
        "WHERE e.series_id = ? AND ev.progress IS NOT NULL AND ev.id = ("
        "  SELECT MAX(ev2.id) FROM bworker_events ev2 "
        "  WHERE ev2.entity_type = 'episode' AND ev2.entity_id = ev.entity_id "
        "    AND ev2.progress IS NOT NULL"
        ")",
        (int(series_id),),
    ).fetchall()
    return {int(row["entity_id"]): _decode_progress(row["progress"])
            for row in rows if row["entity_id"] is not None}


def _series_active_downloads(conn, series_id: int) -> dict[int, dict]:
    active = tuple(store.ACTIVE_DOWNLOAD_STATUSES)
    placeholders = ",".join("?" for _ in active)
    rows = conn.execute(
        "SELECT * FROM downloads WHERE series_id = ? "
        f"AND status IN ({placeholders}) ORDER BY id DESC",
        (int(series_id), *active),
    ).fetchall()
    downloads: dict[int, dict] = {}
    for row in rows:
        episode_id = row["episode_id"]
        if episode_id is None:
            continue
        view = _download_row_view(row)
        if view is not None:
            downloads.setdefault(int(episode_id), view)
    return downloads


def series_stage_context(conn, series_id: int) -> dict | None:
    """Aggregate stage/progress and recent history for one series.

    Aggregation covers monitored episode work only; the returned ``events``
    list merges the series-level and episode-level histories (newest 15) and
    ``episodes`` carries a compact current stage per episode for the detail
    page. ``None`` means the series no longer exists.
    """
    series = _series_row(conn, series_id)
    if series is None:
        return None
    episodes = [dict(row) for row in conn.execute(
        "SELECT id, season, episode, title, air_date, monitored, file_path, "
        "file_score FROM series_episodes WHERE series_id = ? "
        "ORDER BY season, episode",
        (int(series_id),),
    ).fetchall()]
    works = _series_work_rows(conn, int(series_id))
    works_by_episode: dict[int, list[dict]] = {}
    series_works: list[dict] = []
    for work in works:
        if str(work.get("entity_type")) == "episode":
            works_by_episode.setdefault(int(work.get("entity_id") or 0),
                                        []).append(work)
        else:
            series_works.append(work)

    series_monitored = bool(series["monitored"])
    monitored_episodes = [
        episode for episode in episodes
        if series_monitored and bool(episode.get("monitored"))
    ]
    monitored_ids = {int(episode["id"]) for episode in monitored_episodes}
    aggregate_works = [
        work for work in works
        if str(work.get("entity_type")) == "episode"
        and int(work.get("entity_id") or 0) in monitored_ids
    ]
    stage, status, status_source = _aggregate_series(
        aggregate_works, series_monitored, monitored_episodes)
    # A running series-level unit (a series-wide search) is the current unit
    # even when individual episode rows are idle.
    running_series = [
        work for work in series_works
        if str(work.get("status") or "") == "running"
    ]
    if running_series:
        representative = _pick_work(running_series)
        stage = str(representative.get("stage") or stage)
        status = "running"
        status_source = "work"
    else:
        representative = _pick_work(aggregate_works) or _pick_work(series_works)

    events, latest_by_episode = _series_latest_events(conn, int(series_id))
    progress_by_episode = _series_episode_progress(conn, int(series_id))
    downloads_by_episode = _series_active_downloads(conn, int(series_id))
    latest = events[0] if events else None
    status_info = status_meta(status,
                              kind="event" if status_source == "event"
                              else "work")

    episode_views: list[dict] = []
    for episode in episodes:
        episode_id = int(episode["id"])
        work = _pick_work(works_by_episode.get(episode_id, []))
        latest_event = latest_by_episode.get(episode_id)
        derived_stage, derived_status = _episode_derived({
            **episode,
            "series_monitored": series_monitored,
        })
        if work is not None:
            episode_stage = str(work.get("stage") or derived_stage)
        elif latest_event is not None:
            episode_stage = str(latest_event.get("stage") or derived_stage)
        else:
            episode_stage = derived_stage
        if work is not None:
            episode_status = str(work.get("status") or "pending")
            episode_info = work_status_meta(episode_status)
            episode_source = "work"
        elif latest_event is not None:
            episode_status = str(latest_event.get("status") or "info")
            episode_info = event_status_meta(episode_status)
            episode_source = "event"
        else:
            episode_status = derived_status
            episode_info = work_status_meta(derived_status)
            episode_source = "derived"
        episode_progress = progress_by_episode.get(episode_id)
        if episode_progress is None:
            episode_progress = _download_progress(
                downloads_by_episode.get(episode_id))
        episode_stage_info = stage_meta(episode_stage)
        episode_views.append({
            "id": episode_id,
            "season": int(episode["season"]),
            "episode": int(episode["episode"]),
            "title": episode.get("title"),
            "air_date": episode.get("air_date"),
            "monitored": bool(episode.get("monitored")),
            "downloaded": bool(episode.get("file_path")),
            "stage": episode_stage,
            "stage_label": episode_stage_info["label"],
            "stage_badge": episode_stage_info["badge"],
            "stage_tone": episode_stage_info["tone"],
            "status": episode_status,
            "status_label": episode_info["label"],
            "status_badge": episode_info["badge"],
            "status_tone": episode_info["tone"],
            "status_source": episode_source,
            "message": _message_for(work, latest_event),
            "progress": episode_progress,
            "work_id": _int_or_none(work.get("id")) if work else None,
            "next_run_at": work.get("next_run_at") if work else None,
            "updated_at": work.get("updated_at") if work else None,
            "last_event": latest_event,
        })

    downloaded = sum(1 for episode in monitored_episodes
                     if episode.get("file_path"))
    episode_download = None
    if representative is not None:
        episode_download = _download_view(
            conn, download_id=representative.get("current_download_id"))
    if episode_download is None:
        episode_download = _download_view(conn, series_id=int(series_id))
    current_progress = _latest_event_progress(
        conn, entity_type="series", series_id=int(series_id))
    if current_progress is None:
        current_progress = _download_progress(episode_download)

    work_status_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}
    for work in aggregate_works:
        work_status = str(work.get("status") or "")
        work_stage = str(work.get("stage") or "")
        work_status_counts[work_status] = work_status_counts.get(
            work_status, 0) + 1
        stage_counts[work_stage] = stage_counts.get(work_stage, 0) + 1

    best_score = representative.get("best_score") if representative else None
    if best_score is None:
        scores = [_int_or_none(work.get("best_score")) for work in aggregate_works]
        scores = [score for score in scores if score is not None]
        best_score = max(scores) if scores else None

    return {
        "entity": _entity_payload("series", dict(series)),
        "stage": stage,
        "label": stage_label(stage),
        "status": status,
        "status_label": status_info["label"],
        "status_badge": status_info["badge"],
        "status_tone": status_info["tone"],
        "status_source": status_source,
        "message": _message_for(representative, latest),
        "progress": {"done": downloaded, "total": len(monitored_episodes)},
        "current_progress": current_progress,
        "best_score": best_score,
        "next_run_at": representative.get("next_run_at")
        if representative is not None else None,
        "work": _public_work(representative),
        "works": [_public_work(row) for row in series_works],
        "download": episode_download,
        "counts": {
            "episodes": len(episodes),
            "monitored_episodes": len(monitored_episodes),
            "downloaded_episodes": downloaded,
            "works": work_status_counts,
            "stages": stage_counts,
        },
        "episodes": episode_views,
        "events": events,
    }


__all__ = [
    "ENTITY_TYPES",
    "EVENT_STATUSES",
    "EVENT_STATUS_LABELS",
    "EVENT_STATUS_META",
    "MAX_EVENTS_PER_ENTITY",
    "STAGES",
    "STAGE_BADGES",
    "STAGE_LABELS",
    "STAGE_META",
    "WORK_STATUSES",
    "WORK_STATUS_LABELS",
    "WORK_STATUS_META",
    "episode_stage_context",
    "event_status_label",
    "event_status_meta",
    "movie_stage_context",
    "prune",
    "recent",
    "record",
    "record_episode_stage",
    "record_for_work",
    "record_movie_stage",
    "record_series_stage",
    "redact_text",
    "series_stage_context",
    "stage_label",
    "stage_meta",
    "stages_payload",
    "status_label",
    "status_meta",
    "work_status_label",
    "work_status_meta",
]
