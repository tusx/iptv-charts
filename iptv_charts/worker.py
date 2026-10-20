"""The single background worker: one infinite loop, one job at a time.

Every slow or provider-facing operation is a row in ``worker_jobs``. This
process claims one row, runs its handler to completion, records the outcome,
and only then looks at the next row. The web/waitress process never claims a
job; it only inserts rows and reads progress. In the container the two
processes are supervised side by side and share the SQLite database.

Handlers are deliberately thin wrappers around the existing library/sync/
downloader functions. The queue is the new part; the business logic stays in
the modules that already own it.
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import threading
import time
import traceback
from typing import Any, Callable, Mapping

from . import config, db, downloader, jobs, library, scan, scheduler, sync

logger = logging.getLogger("iptv_charts.worker")

#: Shared by the main loop and the heartbeat thread.
_state: dict[str, Any] = {"job_id": None, "status": "idle"}
_state_lock = threading.Lock()


def _set_worker_state(job_id: int | None, status: str) -> None:
    with _state_lock:
        _state["job_id"] = job_id
        _state["status"] = status


# ------------------------------------------------------------------ handlers
def _conn_scope() -> Any:
    return db.connect()


def _search_movie(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        movie = library.get_movie(conn, int(payload["movie_id"]))
        if movie is None:
            raise LookupError("That movie no longer exists.")
        result = library.search_movie(
            conn, movie, query=payload.get("query"),
            reason=str(payload.get("reason") or "manual"),
        )
        return {
            "status": "done",
            "message": result.get("message") or "Search finished.",
            "result": result,
        }
    finally:
        conn.close()


def _search_series(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        series = library.get_series(conn, int(payload["series_id"]))
        if series is None:
            raise LookupError("That series no longer exists.")
        result = library.search_series(
            conn, series, episode_id=payload.get("episode_id"),
            query=payload.get("query"),
            reason=str(payload.get("reason") or "manual"),
        )
        return {
            "status": "done",
            "message": result.get("message") or "Search finished.",
            "result": result,
        }
    finally:
        conn.close()


def _movie_releases(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        releases = library.manual_movie_releases(
            conn, int(payload["movie_id"]), query=payload.get("query")
        )
        return {
            "status": "done",
            "message": f"{len(releases)} release(s) checked.",
            "result": {"releases": releases},
        }
    finally:
        conn.close()


def _episode_releases(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        releases = library.manual_episode_releases(
            conn, int(payload["series_id"]), int(payload["episode_id"]),
            query=payload.get("query"),
        )
        return {
            "status": "done",
            "message": f"{len(releases)} release(s) checked.",
            "result": {"releases": releases},
        }
    finally:
        conn.close()


def _grab_movie(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        download = library.grab_movie(
            conn, int(payload["movie_id"]), int(payload["source_id"]),
            str(payload["remote_id"]),
        )
        return {
            "status": "done", "message": "Download queued.",
            "result": {"download": download},
        }
    finally:
        conn.close()


def _grab_episode(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        download = library.grab_episode(
            conn, int(payload["series_id"]), int(payload["episode_id"]),
            int(payload["source_id"]), str(payload["provider_remote_id"]),
            str(payload["stream_id"]),
        )
        return {
            "status": "done", "message": "Download queued.",
            "result": {"download": download},
        }
    finally:
        conn.close()


def _refresh_movie(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        movie = library.refresh_movie(conn, int(payload["movie_id"]))
        return {
            "status": "done", "message": "Metadata refreshed.",
            "result": {"movie": movie},
        }
    finally:
        conn.close()


def _refresh_series(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        series = library.refresh_series(conn, int(payload["series_id"]))
        return {
            "status": "done", "message": "Metadata refreshed.",
            "result": {"series": series},
        }
    finally:
        conn.close()


def _scan_import(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        result = scan.import_result(conn, int(payload["result_id"]))
        return {
            "status": "done", "message": f"Imported {result.get('title') or 'file'}.",
            "result": result,
        }
    finally:
        conn.close()


def _scan_import_all(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    del job
    conn = _conn_scope()
    try:
        result = scan.import_all(conn)
        return {
            "status": "done",
            "message": f"Imported {result.get('imported', 0)} file(s).",
            "result": result,
        }
    finally:
        conn.close()


def _sync(job: Mapping[str, Any], stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    return sync.run_job(
        int(payload["source_id"]), int(job["id"]), stop_event=stop
    )


def _download(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    return downloader.process_download(int(payload["download_id"]))


def _task(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    return scheduler.run_task(str(payload["name"]))


HANDLERS: dict[str, Callable[[Mapping[str, Any], threading.Event], dict]] = {
    "sync": _sync,
    "download": _download,
    "task": _task,
    "search_movie": _search_movie,
    "search_series": _search_series,
    "movie_releases": _movie_releases,
    "episode_releases": _episode_releases,
    "grab_movie": _grab_movie,
    "grab_episode": _grab_episode,
    "refresh_movie": _refresh_movie,
    "refresh_series": _refresh_series,
    "scan_import": _scan_import,
    "scan_import_all": _scan_import_all,
}


# --------------------------------------------------------------------- runner
def _apply_outcome(conn, job: Mapping[str, Any], outcome: Mapping[str, Any] | None) -> None:
    job_id = int(job["id"])
    outcome = dict(outcome or {})
    if outcome.get("retry"):
        jobs.reschedule(
            conn, job_id,
            available_at=int(outcome.get("available_at") or time.time()),
            message=str(outcome.get("message") or "Queued for retry."),
            progress_data=outcome.get("data"),
        )
        return
    status = str(outcome.get("status") or "done")
    if status not in ("done", "error", "cancelled"):
        status = "done"
    jobs.finish(
        conn, job_id, status,
        message=str(outcome.get("message") or ""),
        result=outcome.get("result"),
        error=outcome.get("error"),
        error_type=outcome.get("error_type"),
        progress_data=outcome.get("data"),
    )


def run_forever(stop_event: threading.Event | None = None) -> None:
    """The infinite loop. One claimed job at a time, forever."""
    stop = stop_event or threading.Event()
    db.init_db()

    # One worker, enforced rather than assumed. Two workers would each claim
    # a different job, and startup recovery in one could requeue a job the
    # other is still running. The lock is released automatically on exit.
    lock_path = config.data_dir() / "worker.lock"
    lock = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("worker: another worker already owns %s; exiting", lock_path)
        lock.close()
        return

    conn = db.connect()
    try:
        recovered = jobs.recover(conn)
        if recovered:
            logger.info("worker: requeued %d interrupted job(s)", recovered)
        jobs.worker_started(conn, os.getpid())
        jobs.heartbeat(conn, status="idle")
    finally:
        conn.close()

    # A crash in the middle of a transfer leaves its row 'downloading'; put it
    # back in the durable download queue and recreate the job row.
    downloader.recover()

    hb_stop = threading.Event()

    def heartbeat_loop() -> None:
        hb = None
        try:
            hb = db.connect()
            while not hb_stop.wait(5.0):
                with _state_lock:
                    job_id = _state["job_id"]
                    status = _state["status"]
                try:
                    jobs.heartbeat(hb, current_job_id=job_id, status=status)
                except Exception:
                    try:
                        hb.rollback()
                    except Exception:
                        pass
        finally:
            if hb is not None:
                hb.close()

    hb = threading.Thread(target=heartbeat_loop, name="iptv-worker-heartbeat", daemon=True)
    hb.start()

    claimed_conn = db.connect()
    loops = 0
    try:
        while not stop.is_set():
            _set_worker_state(None, "idle")
            try:
                jobs.heartbeat(claimed_conn, status="idle")
            except Exception:
                pass

            # Queue periodic tasks first; this is database-only work. If it
            # enqueues a sync, the search task is deferred to a later pass.
            try:
                queued_tasks = scheduler.enqueue_due()
                if queued_tasks:
                    logger.info("worker: queued %d periodic task(s)", queued_tasks)
            except Exception as exc:
                logger.warning("worker: could not enqueue periodic tasks: %s", exc)

            job = jobs.claim(claimed_conn)
            if job is None:
                loops += 1
                if loops % 3600 == 0:
                    try:
                        jobs.cleanup(claimed_conn)
                    except Exception:
                        pass
                stop.wait(1.0)
                continue

            job_id = int(job["id"])
            kind = str(job.get("kind") or "")
            handler = HANDLERS.get(kind)
            _set_worker_state(job_id, "running")
            try:
                jobs.heartbeat(claimed_conn, current_job_id=job_id, status="running")
            except Exception:
                pass
            logger.info("worker: started #%d %s", job_id, kind)
            started = time.monotonic()
            try:
                if handler is None:
                    raise ValueError(f"Unknown worker job kind: {kind}")
                outcome = handler(job, stop)
                _apply_outcome(claimed_conn, job, outcome)
                logger.info(
                    "worker: finished #%d %s in %.1fs: %s", job_id, kind,
                    time.monotonic() - started, outcome.get("message", "") if outcome else "",
                )
            except Exception as exc:
                logger.exception("worker: job #%d %s failed: %s", job_id, kind, exc)
                try:
                    if stop.is_set():
                        jobs.reschedule(
                            claimed_conn, job_id, available_at=int(time.time()),
                            message="Interrupted by shutdown; queued again.",
                        )
                    else:
                        jobs.finish(
                            claimed_conn, job_id, "error",
                            message="Worker job failed",
                            error=f"{type(exc).__name__}: {exc}",
                            error_type=type(exc).__name__,
                        )
                except Exception:
                    logger.exception("worker: could not record job failure")
            finally:
                _set_worker_state(None, "idle")
    finally:
        hb_stop.set()
        hb.join(timeout=3.0)
        try:
            conn = db.connect()
            try:
                jobs.heartbeat(conn, status="stopped")
            finally:
                conn.close()
        except Exception:
            pass
        claimed_conn.close()
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()
        logger.info("worker: stopped")


# ---------------------------------------------------------------- test/dev API
_thread: threading.Thread | None = None
_thread_stop: threading.Event | None = None


def start_background() -> None:
    """Start the worker in a daemon thread for tests and local dev.

    Production uses the separate ``worker.py`` process; this exists so test
    suites can exercise the same loop without a subprocess. The web app never
    calls it.
    """
    global _thread, _thread_stop
    if _thread is not None and _thread.is_alive():
        return
    _thread_stop = threading.Event()
    _thread = threading.Thread(
        target=run_forever, args=(_thread_stop,),
        name="iptv-worker", daemon=True,
    )
    _thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if jobs.worker_status()["alive"]:
            return
        time.sleep(0.05)


def stop_background(timeout: float = 5.0) -> None:
    global _thread, _thread_stop
    if _thread_stop is not None:
        _thread_stop.set()
    # Interrupt an in-flight download so shutdown is not held by a long HTTP
    # read. A sync is interrupted by its reporter watching the stop event.
    downloader.shutdown(timeout=1.0)
    if _thread is not None:
        _thread.join(timeout=timeout)
    _thread = None
    _thread_stop = None


def main() -> int:
    """Process entry point used by ``worker.py`` and the container."""
    from .app import configure_logging

    configure_logging()
    stop = threading.Event()

    def _stop(signum, _frame) -> None:
        logger.info("worker: received signal %s, stopping after this job", signum)
        stop.set()
        # Downloads check this to leave a resumable row rather than a broken
        # one. The worker exits once the current handler returns.
        downloader.shutdown(timeout=1.0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("worker: starting (pid %s)", os.getpid())
    try:
        run_forever(stop)
    except Exception:
        logger.error("worker: fatal error\n%s", traceback.format_exc())
        return 1
    return 0


__all__ = [
    "HANDLERS",
    "main",
    "run_forever",
    "start_background",
    "stop_background",
]
