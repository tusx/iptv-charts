"""Periodic library tasks and manual task runs.

The scheduler is one daemon thread with a single task lock: two metadata
refreshes or two search passes at once would multiply provider API calls and
could enqueue the same episodes twice. Tasks are persisted in task_state, so a
restart does not lose the last run time or the next due time.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from . import db, library, sync

logger = logging.getLogger("iptv_charts.scheduler")

TASK_NAMES = ("search_missing", "refresh_metadata", "refresh_catalog", "scan_library")
PERIODIC = {
    "search_missing": ("search_interval_hours", 6, 120),
    "refresh_metadata": ("refresh_interval_hours", 168, 600),
    "refresh_catalog": ("catalog_refresh_hours", 24, 300),
}

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None


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
        # A null next_run means either "never scheduled" or "disabled with 0".
        # Re-check the interval here so re-enabling a task (or a fresh
        # database) starts it again instead of waiting for a manual run.
        if _interval_seconds(conn, name) is None:
            continue
        conn.execute(
            "UPDATE task_state SET next_run = ? WHERE name = ? AND next_run IS NULL",
            (now + first_delay, name),
        )
    conn.commit()


# --------------------------------------------------------------------- worker
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


def _execute(name: str, *, conn=None, force: bool = False, locked: bool = False,
             release_lock: bool = False) -> bool:
    if name not in TASK_NAMES:
        raise ValueError("Unknown task.")
    if not locked and not _lock.acquire(blocking=False):
        return False
    own_conn = conn is None
    try:
        if conn is None:
            conn = db.connect()
        row = db.get_one(conn, "SELECT * FROM task_state WHERE name = ?", (name,))
        if row is None:
            _ensure_state(conn)
            row = db.get_one(conn, "SELECT * FROM task_state WHERE name = ?", (name,))
        now = int(time.time())
        if not force and row["next_run"] is not None and int(row["next_run"]) > now:
            return False

        # A metadata search during a catalog sync would use a half-replaced
        # catalog and compete for the provider's connection limit.
        if sync.is_busy():
            _set_state(conn, name, status="idle", message="Deferred while a playlist sync runs.",
                       next_run=now + 300)
            logger.info("tasks: %s deferred while syncing", name)
            return True

        if name == "refresh_catalog":
            # A sync uses one of the account's connection slots; starting one
            # while the download queue is busy can break both. Let downloads
            # finish first.
            active = db.get_one(
                conn,
                "SELECT COUNT(*) AS n FROM downloads WHERE status IN "
                "('queued', 'downloading', 'importing')",
            )["n"]
            if active:
                _set_state(conn, name, status="idle",
                           message="Deferred while downloads are active.",
                           next_run=now + 300)
                logger.info("tasks: refresh_catalog deferred while downloading")
                return True

        _set_state(conn, name, status="running", message="Running", started=now)
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
        _set_state(conn, name, status=status, message=str(message), finished=finished,
                   next_run=next_run)
        logger.info("tasks: %s finished (%s): %s", name, status, message)
        return True
    finally:
        if own_conn and conn is not None:
            conn.close()
        if not locked or release_lock:
            _lock.release()


def _loop() -> None:
    while not _stop.is_set():
        if _lock.acquire(blocking=False):
            try:
                _run_due()
            except Exception as exc:
                logger.exception("scheduler loop failed: %s", exc)
            finally:
                _lock.release()
        _stop.wait(30.0)


def _run_due() -> None:
    conn = db.connect()
    try:
        _ensure_state(conn)
        now = int(time.time())
        rows = {
            row["name"]: row for row in conn.execute("SELECT * FROM task_state")
        }
        # Catalog refresh first: if it queues a sync, the search below sees
        # sync.is_busy() and defers rather than using the old catalog.
        for name in ("refresh_catalog", "search_missing", "refresh_metadata"):
            row = rows.get(name)
            if row is None or row["next_run"] is None:
                continue
            if int(row["next_run"]) > now:
                continue
            _execute(name, conn=conn, force=True, locked=True)
    finally:
        conn.close()


# ---------------------------------------------------------------------- tasks
def _task_search_missing(conn) -> str:
    budget = 100
    searched_movies = queued_movies = 0
    movies = [
        movie for movie in library.list_movies(conn, status="monitored")
        if library.movie_needs_search(conn, movie)
    ]
    for movie in movies[:100]:
        if budget <= 0:
            break
        try:
            result = library.search_movie(conn, movie, reason="auto")
            searched_movies += 1
            if result.get("queued"):
                queued_movies += 1
                budget -= 1
        except Exception as exc:
            logger.warning("tasks: movie search failed for %s: %s", movie["title"], exc)

    searched_series = queued_series = 0
    series = [
        item for item in library.list_series(conn, status="monitored")
        if library.series_needs_search(conn, item)
    ]
    for item in series[:50]:
        if budget <= 0:
            break
        try:
            result = library.search_series(conn, item, reason="auto")
            searched_series += 1
            queued = int(result.get("queued") or 0)
            if queued:
                queued_series += queued
                budget -= queued
        except Exception as exc:
            logger.warning("tasks: series search failed for %s: %s", item["title"], exc)

    return (f"Searched {searched_movies} movie(s) and {searched_series} series; "
            f"queued {queued_movies + queued_series} download(s).")


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
    "search_missing": _task_search_missing,
    "refresh_metadata": _task_refresh_metadata,
    "refresh_catalog": _task_refresh_catalog,
    "scan_library": _task_scan_library,
}


# --------------------------------------------------------------------- facade
def start() -> None:
    if os.environ.get("IPTV_CHARTS_SCHEDULER", "").lower() in {"0", "false", "no", "off"}:
        logger.info("scheduler: disabled by IPTV_CHARTS_SCHEDULER")
        return
    conn = db.connect()
    try:
        _ensure_state(conn)
    finally:
        conn.close()
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="iptv-scheduler", daemon=True)
    _thread.start()
    logger.info("scheduler: started")


def stop(timeout: float = 3.0) -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=timeout)


def trigger(name: str) -> bool:
    """Start a task in the background. Returns False when one is already running."""
    if name not in TASK_NAMES:
        raise ValueError("Unknown task.")
    if not _lock.acquire(blocking=False):
        return False
    # Mark the task as running before returning, otherwise a caller that polls
    # immediately can see the old "idle" row and conclude the task already
    # finished. The task thread updates it again when it really starts.
    conn = db.connect()
    try:
        _set_state(conn, name, status="running", message="Starting…",
                   started=int(time.time()))
    finally:
        conn.close()
    thread = threading.Thread(
        target=_execute, args=(name,),
        kwargs={"force": True, "locked": True, "release_lock": True},
        name=f"iptv-task-{name}", daemon=True,
    )
    try:
        thread.start()
    except Exception:
        conn = db.connect()
        try:
            _set_state(conn, name, status="error", message="The task could not start.")
        finally:
            conn.close()
        _lock.release()
        raise
    return True


def snapshot(conn) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM task_state ORDER BY name"
    )]


def is_running() -> bool:
    return _lock.locked()


__all__ = [
    "TASK_NAMES",
    "is_running",
    "snapshot",
    "start",
    "stop",
    "trigger",
]
