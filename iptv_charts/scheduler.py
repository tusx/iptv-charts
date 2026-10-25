"""Periodic maintenance tasks and manual task runs.

The scheduler no longer owns a thread. It has two small jobs:

* ``enqueue_due`` reads ``task_state`` and inserts a ``worker_jobs`` row when a
  periodic maintenance task is due; and
* ``run_task`` is the handler the single background worker calls.

Automatic library search is no longer a scheduler task. It is declarative
bworker work (``bworker_work`` movie/episode rows), so there is exactly one
search loop and it respects ``bworker_policies``. Periodic metadata and catalog
refresh remain here because they are global tasks with no per-item state, and
manual library scans remain triggerable.

Because the worker runs one job at a time, two refreshes cannot overlap by
construction. Task state is still persisted in ``task_state`` so the Activity
page can show the last and next run.
"""

from __future__ import annotations

import logging
import os
import time

from . import db, jobs, library, sync
from . import bworker_store as bworker_store

logger = logging.getLogger("iptv_charts.scheduler")

#: ``search_missing`` deliberately left out: it is bworker work now. The
#: migrated task_state row is kept for display/history, but it is never queued.
TASK_NAMES = ("refresh_metadata", "refresh_catalog", "scan_library")
PERIODIC = {
    "refresh_metadata": ("refresh_interval_hours", 168, 600),
    "refresh_catalog": ("catalog_refresh_hours", 24, 300),
}

#: Periodic maintenance obeys the same per-work-type toggles as bworker work.
POLICY_FOR_TASK = {
    "refresh_metadata": "metadata_refresh",
    "refresh_catalog": "playlist_sync",
}

_TASK_PRIORITY = 15


def _setting(conn, key: str, default: str) -> str:
    try:
        return db.get_setting(conn, key, default) or default
    except Exception:
        return default


def _interval_seconds(conn, name: str) -> int | None:
    setting, default, _ = PERIODIC[name]
    try:
        hours = float(_setting(conn, setting, str(default)))
    except (TypeError, ValueError):
        hours = float(default)
    if hours <= 0:
        return None
    return max(60, min(int(hours * 3600), 30 * 24 * 3600))


def _ensure_state(conn) -> None:
    for name in TASK_NAMES:
        conn.execute("INSERT OR IGNORE INTO task_state (name) VALUES (?)", (name,))
    now = int(time.time())
    for name, (_setting_name, _default, first_delay) in PERIODIC.items():
        # A null next_run means "never scheduled" or "disabled with 0".
        # Re-check the interval so re-enabling a task starts it again.
        if _interval_seconds(conn, name) is None:
            continue
        conn.execute(
            "UPDATE task_state SET next_run = ? WHERE name = ? AND next_run IS NULL",
            (now + first_delay, name),
        )
    conn.commit()


def _set_state(conn, name: str, *, status: str | None = None,
               message: str | None = None, started: int | None = None,
               finished: int | None = None, next_run: int | None | object = ...) -> None:
    fields: list[str] = []
    params: list = []
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if message is not None:
        fields.append("message = ?")
        params.append(message[:500])
    if started is not None:
        fields.append("last_started = ?")
        params.append(started)
    if finished is not None:
        fields.append("last_finished = ?")
        params.append(finished)
    if next_run is not ...:
        fields.append("next_run = ?")
        params.append(next_run)
    if fields:
        conn.execute(f"UPDATE task_state SET {', '.join(fields)} WHERE name = ?",
                     (*params, name))
        conn.commit()


def _active(conn, name: str) -> dict | None:
    return jobs.active_for(conn, "task", f"task:{name}")


def enqueue_due() -> int:
    """Queue periodic maintenance tasks that are due. No provider I/O.

    The bworker calls this after its pause check and before claiming a job.
    Deferring metadata refresh while a sync is queued is deliberate: it must not
    run against a half-replaced catalog. The worker's single-job rule means the
    sync (priority 20) runs before the deferred task's next attempt.

    A disabled ``bworker_policies`` row suppresses its periodic task. The task
    stays due so re-enabling it queues on the next loop; ``activity_cleanup``
    is handled by the bworker itself and is always enabled.
    """
    if os.environ.get("IPTV_CHARTS_SCHEDULER", "").lower() in {"0", "false", "no", "off"}:
        return 0
    conn = db.connect()
    queued = 0
    try:
        _ensure_state(conn)
        now = int(time.time())
        rows = {row["name"]: row for row in conn.execute("SELECT * FROM task_state")}
        # Catalog refresh first: if it queues syncs, metadata refresh below is
        # deferred to the next pass rather than reading the old catalog.
        for name in ("refresh_catalog", "refresh_metadata"):
            row = rows.get(name)
            if row is None or row["next_run"] is None or int(row["next_run"]) > now:
                continue
            policy_type = POLICY_FOR_TASK.get(name)
            if policy_type and not bworker_store.policy_enabled(conn, policy_type):
                continue
            if name == "refresh_metadata" and sync.is_busy():
                _set_state(
                    conn, name, status="idle",
                    message="Deferred while a playlist sync is queued or running.",
                    next_run=now + 300,
                )
                logger.info("tasks: %s deferred while syncing", name)
                continue
            if _active(conn, name) is not None:
                continue
            job = jobs.enqueue(
                conn, "task", {"name": name},
                priority=_TASK_PRIORITY, dedupe_key=f"task:{name}",
            )
            if job.get("_created"):
                queued += 1
                _set_state(conn, name, status="queued", message="Queued")
                logger.info("tasks: %s queued", name)
        return queued
    finally:
        conn.close()

# ---------------------------------------------------------------------- tasks
# ``search_missing`` used to live here. It is now per-item bworker work; a
# stale queued job from a pre-0.8 database is answered with a no-op in
# ``run_task`` instead of starting a second search loop.
def _task_refresh_metadata(conn) -> str:
    movies = library.list_movies(conn)
    series = library.list_series(conn)
    refreshed_movies = refreshed_series = 0
    errors = 0
    for movie in movies[:100]:
        try:
            library.refresh_movie(conn, movie["id"])
            refreshed_movies += 1
        except Exception as exc:
            errors += 1
            logger.warning("tasks: refresh movie %s failed: %s", movie["title"], exc)
    for item in series[:50]:
        try:
            library.refresh_series(conn, item["id"])
            refreshed_series += 1
        except Exception as exc:
            errors += 1
            logger.warning("tasks: refresh series %s failed: %s", item["title"], exc)
    message = f"Refreshed {refreshed_movies} movie(s) and {refreshed_series} series."
    if errors:
        message += f" {errors} failed; see the log."
    return message


def _task_refresh_catalog(conn) -> str:
    try:
        hours = float(_setting(conn, "catalog_refresh_hours", "24"))
    except (TypeError, ValueError):
        hours = 24.0
    if hours <= 0:
        return "Automatic catalog refresh is disabled."
    cutoff = int(time.time()) - int(hours * 3600)
    rows = conn.execute(
        "SELECT id FROM sources WHERE last_synced_at IS NULL OR last_synced_at < ? "
        "ORDER BY id",
        (cutoff,),
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    if not ids:
        return "All playlists are up to date."
    queued = sync.enqueue(ids)
    return f"Queued {len(queued)} playlist sync(s)."


def _task_scan_library(conn) -> str:
    from . import scan

    return scan.run(conn)


TASK_FUNCS = {
    "refresh_metadata": _task_refresh_metadata,
    "refresh_catalog": _task_refresh_catalog,
    "scan_library": _task_scan_library,
}


# --------------------------------------------------------------------- runner
def run_task(name: str, scope: str = "all") -> dict:
    """Execute one task. The worker calls this exactly once per job."""
    if name == "search_missing":
        # A job queued before the 0.8.0 upgrade must not start the old loop.
        conn = db.connect()
        try:
            message = "Automatic search is owned by the bworker; nothing to do."
            _set_state(conn, name, status="idle", message=message,
                       finished=int(time.time()))
        finally:
            conn.close()
        logger.info("tasks: ignored retired search_missing task")
        return {"status": "done", "message": message,
                "result": {"task": name, "state": "idle"}}
    if name not in TASK_NAMES:
        raise ValueError("Unknown task.")
    if scope not in {"all", "movies", "series"}:
        scope = "all"
    conn = db.connect()
    started = int(time.time())
    try:
        row = db.get_one(conn, "SELECT * FROM task_state WHERE name = ?", (name,))
        if row is None:
            _ensure_state(conn)
        # A manual trigger can arrive while a playlist sync is queued. The
        # worker is serial, but task jobs deliberately outrank sync jobs; do
        # not let metadata refresh run against the catalog the sync is about
        # to replace. Periodic tasks are deferred in enqueue_due already.
        if name == "refresh_metadata" and sync.is_busy():
            message = "Deferred while a playlist sync is queued or running."
            _set_state(conn, name, status="idle", message=message,
                       finished=started, next_run=started + 300)
            logger.info("tasks: %s deferred while syncing", name)
            return {"status": "done", "message": message,
                    "result": {"task": name, "state": "idle"}}
        _set_state(conn, name, status="running", message="Running", started=started)
        logger.info("tasks: %s started", name)
        try:
            message = TASK_FUNCS[name](conn)
            status = "idle"
        except Exception as exc:
            status = "error"
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("tasks: %s failed: %s", name, exc)
        finished = int(time.time())
        next_run = None
        if name in PERIODIC:
            interval = _interval_seconds(conn, name)
            next_run = finished + interval if interval else None
        _set_state(conn, name, status=status, message=str(message),
                   finished=finished, next_run=next_run)
        logger.info("tasks: %s finished (%s): %s", name, status, message)
        return {
            "status": "done",
            "message": str(message),
            "result": {"task": name, "state": status},
        }
    finally:
        conn.close()


# --------------------------------------------------------------------- facade
def start() -> None:
    """Kept for older callers; periodic work now lives in the worker process."""
    logger.info("scheduler: periodic tasks are owned by the background worker")


def stop(timeout: float = 3.0) -> None:
    # The worker owns the loop; there is no scheduler thread to stop here.
    return None


def trigger(name: str, scope: str = "all") -> bool:
    """Queue a maintenance task now.

    Returns False when that task is already queued/running. The retired
    ``search_missing`` task raises ``ValueError`` instead of quietly creating
    a competing search loop; automatic search is bworker work.
    """
    if name == "search_missing":
        raise ValueError("Automatic search is owned by the bworker.")
    if name not in TASK_NAMES:
        raise ValueError("Unknown task.")
    if scope not in {"all", "movies", "series"}:
        raise ValueError("Unknown task scope.")
    conn = db.connect()
    try:
        _ensure_state(conn)
        if _active(conn, name) is not None:
            return False
        job = jobs.enqueue(
            conn, "task", {"name": name},
            priority=40, dedupe_key=f"task:{name}",
        )
        if not job.get("_created"):
            return False
        _set_state(conn, name, status="queued", message="Queued")
        return True
    finally:
        conn.close()


def snapshot(conn) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM task_state ORDER BY name"
    )]


def is_running() -> bool:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM worker_jobs WHERE kind = 'task' "
            "AND status IN ('queued', 'running') LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


__all__ = [
    "TASK_NAMES",
    "enqueue_due",
    "is_running",
    "run_task",
    "snapshot",
    "start",
    "stop",
    "trigger",
]
