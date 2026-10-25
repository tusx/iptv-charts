"""The bworker: one durable background unit at a time.

Two sources of work, claimed in this order:

1. ``worker_jobs`` -- imperative work inserted by the web process (playlist
   sync, a download, a manual search, an import, a task). This is the existing
   queue and the bworker keeps its handler contract unchanged.
2. ``bworker_work`` -- declarative, per-item automatic work (search a movie,
   watch an episode, probe, ...) with its own stage, cursor, retry policy and
   reset state.

The loop never runs two units concurrently. A bounded unit is one claimed
job or one work row; pause is observed between units, so an in-flight
download or a probe iteration is allowed to finish cleanly. The only writers
of the Valkey activity log are this process and the API's policy/pause
writers; the web never appends activity.

``iptv_charts.worker`` and the root ``worker.py`` remain thin compatibility
shims until the container/setup scripts move to ``bworker.py``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import traceback
from typing import Any, Callable, Mapping

from . import (acquisition, activity_log, bworker_events, config, db,
               downloader, jobs, library, scan, scheduler, sync)
from . import bworker_store as store

logger = logging.getLogger("iptv_charts.bworker")

JobHandler = Callable[[Mapping[str, Any], threading.Event], dict | None]
WorkHandler = Callable[["WorkContext"], Mapping[str, Any] | None]

#: Imperative queue handlers. Kept as a mutable registry so feature modules
#: (candidate pipeline, provider work) can register without editing this file.
JOB_HANDLERS: dict[str, JobHandler] = {}
#: Declarative work handlers keyed by bworker_work.work_type.
WORK_HANDLERS: dict[str, WorkHandler] = {}
#: Legacy name used by older code/tests that imported ``worker.HANDLERS``.
HANDLERS = JOB_HANDLERS

#: Download row statuses considered "still owned by a running unit".
ACTIVE_DOWNLOAD_STATUSES = store.ACTIVE_DOWNLOAD_STATUSES
TERMINAL_DOWNLOAD_STATUSES = ("imported", "failed", "cancelled")

#: One idle wait's no-work entry; not repeated until work runs or the loop
#: transitions out of idle.
PAUSE_POLL_SECONDS = 1.0
PROGRESS_UPDATE_SECONDS = 1.0


def register_job_handler(kind: str, handler: JobHandler) -> JobHandler:
    JOB_HANDLERS[str(kind)] = handler
    return handler


def register_work_handler(work_type: str, handler: WorkHandler) -> WorkHandler:
    WORK_HANDLERS[str(work_type)] = handler
    return handler


# ------------------------------------------------------------------ utilities
def _decode(value: Any) -> dict:
    if not value:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _setting(conn, key: str, default: str) -> str:
    try:
        return str(db.get_setting(conn, key, default) or default)
    except Exception:
        return default


def _setting_int(conn, key: str, default: int, *,
                 minimum: int = 0, maximum: int = 7 * 86400) -> int:
    try:
        value = int(float(_setting(conn, key, str(default))))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(value, maximum))


def _search_interval_seconds(conn, work: Mapping[str, Any]) -> int | None:
    """Per-item override, then the section setting. ``0`` disables re-search."""
    work_type = str(work.get("work_type") or "")
    entity_type = str(work.get("entity_type") or "")
    entity_id = int(work.get("entity_id") or 0)
    payload = work.get("payload") or {}
    hours: float | None = None
    if entity_type == "movie" and work_type == "movie_search":
        row = conn.execute(
            "SELECT search_interval_hours FROM movies WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is not None and row["search_interval_hours"] is not None:
            hours = float(row["search_interval_hours"])
        setting_key, fallback = "movie_search_interval_hours", 6
    else:
        series_id = int(payload.get("series_id") or entity_id or 0)
        series_row = None
        if series_id:
            series_row = conn.execute(
                "SELECT search_interval_hours FROM series WHERE id = ?",
                (series_id,),
            ).fetchone()
        if series_row is not None and series_row["search_interval_hours"] is not None:
            hours = float(series_row["search_interval_hours"])
        setting_key, fallback = "series_search_interval_hours", 4
    if hours is None:
        try:
            hours = float(_setting(conn, setting_key, str(fallback)))
        except (TypeError, ValueError):
            hours = float(fallback)
    if hours <= 0:
        return None
    return max(60, int(hours * 3600))


def _poll_seconds(_conn=None) -> int:
    """How often a work unit re-checks an active auto download."""
    return 60


# --------------------------------------------------------------------- context
class WorkContext:
    """What a declarative work handler is allowed to do.

    Handlers get the already-claimed SQLite connection, the work row, a
    progress/stage writer that updates the *same* activity entry, and a
    cancellation check. Progress writes to SQLite are best effort on a
    short-lived separate connection so a handler's transaction can never be
    rolled back by the progress reporter.
    """

    def __init__(self, *, runner: "Bworker", conn, work: Mapping[str, Any],
                 activity_id: int | None, stop: threading.Event) -> None:
        self.runner = runner
        self.conn = conn
        self.work = dict(work)
        self.activity = runner.activity
        self.activity_id = activity_id
        self.stop = stop
        self.stage = str(work.get("stage") or "pending")
        self.message = ""
        self.progress: dict | None = None
        self.current_download_id = work.get("current_download_id")
        self._last_write = float("-inf")
        self._event_stage = self.stage

    # ----------------------------------------------------------- identity
    @property
    def work_id(self) -> int:
        return int(self.work["id"])

    @property
    def work_type(self) -> str:
        return str(self.work.get("work_type") or "")

    @property
    def entity_type(self) -> str:
        return str(self.work.get("entity_type") or "")

    @property
    def entity_id(self) -> int | None:
        value = self.work.get("entity_id")
        return int(value) if value is not None else None

    @property
    def payload(self) -> dict:
        return _decode(self.work.get("payload"))

    def now(self) -> int:
        return self.runner._now()

    def check_stop(self) -> bool:
        return self.stop.is_set()

    @property
    def cancelled(self) -> bool:
        return self.stop.is_set()

    # ----------------------------------------------------------- progress
    def _activity_patch(self) -> dict:
        patch: dict[str, Any] = {"stage": self.stage,
                                 "status": activity_log.STATUS_RUNNING}
        if self.message:
            patch["message"] = self.message
        if self.progress is not None:
            patch["progress"] = self.progress
        if self.current_download_id:
            patch["current_download_id"] = int(self.current_download_id)
        return patch

    def _write_activity(self, *, force: bool) -> None:
        if not self.activity_id:
            return
        now = self.runner.clock()
        if not force and (now - self._last_write) < PROGRESS_UPDATE_SECONDS:
            return
        self._last_write = now
        try:
            self.activity.update(self.activity_id, self._activity_patch())
        except Exception:
            pass

    def _write_db_stage(self) -> None:
        try:
            connection = sqlite3.connect(str(config.db_path()), timeout=0.05)
            try:
                connection.execute(
                    "UPDATE bworker_work SET stage = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'running'",
                    (self.stage, self.now(), self.work_id),
                )
                connection.commit()
            finally:
                connection.close()
        except Exception:
            pass

    def _record_event(self, status: str, message: str | None = None,
                      progress: Mapping[str, Any] | None = None, *,
                      started_at: int | None = None,
                      finished_at: int | None = None) -> None:
        """Best-effort durable item history; never fails the unit."""
        connection = None
        try:
            connection = sqlite3.connect(str(config.db_path()), timeout=0.05)
            bworker_events.record_for_work(
                connection, self.work, self.stage, status,
                message if message is not None else self.message,
                progress if progress is not None else self.progress,
                started_at=started_at, finished_at=finished_at,
                now=self.now(),
            )
        except Exception:
            pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def set_stage(self, stage: str, message: str | None = None,
                  progress: Mapping[str, Any] | None = None) -> None:
        """A stage change is user-visible immediately (not throttled)."""
        self.stage = str(stage)
        if message is not None:
            self.message = str(message)
        if progress is not None:
            self.progress = dict(progress)
        self._write_activity(force=True)
        self._write_db_stage()
        if self.stage != self._event_stage:
            self._event_stage = self.stage
            self._record_event("progress", self.message, self.progress)

    def set_message(self, message: str) -> None:
        self.message = str(message)
        self._write_activity(force=False)

    def set_progress(self, done: int | float | None = None,
                     total: int | float | None = None,
                     message: str | None = None,
                     **extra: Any) -> None:
        progress: dict[str, Any] = {}
        if done is not None:
            progress["done"] = done
        if total is not None:
            progress["total"] = total
        progress.update(extra)
        self.progress = progress
        if message is not None:
            self.message = str(message)
        self._write_activity(force=False)

    def set_download(self, download_id: int | None) -> None:
        self.current_download_id = int(download_id) if download_id else None
        self._write_activity(force=True)


# ------------------------------------------------------------- progress watch
class _JobProgressWatcher(threading.Thread):
    """Mirror worker_jobs progress into the activity entry about once a second."""

    def __init__(self, *, connection_factory, job_id: int,
                 activity_id: int, activity, interval: float,
                 attempt: int, parent_stop: threading.Event) -> None:
        super().__init__(name=f"bworker-progress-{job_id}", daemon=True)
        self.connection_factory = connection_factory
        self.job_id = int(job_id)
        self.activity_id = int(activity_id)
        self.activity = activity
        self.interval = max(0.2, float(interval))
        self.attempt = attempt
        # Do not assign ``self._stop``: that name is Thread._stop(), which
        # threading.Thread.join() calls internally on Python 3.12. An Event
        # there makes every join raise "'Event' object is not callable".
        self._stop_event = threading.Event()
        self.parent_stop = parent_stop

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        previous: tuple[Any, Any] | None = None
        while not self._stop_event.wait(self.interval):
            if self.parent_stop.is_set():
                break
            conn = None
            try:
                conn = self.connection_factory()
                row = conn.execute(
                    "SELECT message, progress_data FROM worker_jobs WHERE id = ?",
                    (self.job_id,),
                ).fetchone()
                if row is None:
                    continue
                message = row["message"]
                progress = _decode(row["progress_data"])
                key = (message, json.dumps(progress, sort_keys=True, default=str))
                if key == previous:
                    continue
                previous = key
                patch: dict[str, Any] = {"progress": progress,
                                         "attempt": self.attempt}
                if message and message not in ("Running", "Queued"):
                    patch["message"] = message
                stage = progress.get("stage") or progress.get("phase")
                if stage:
                    patch["stage"] = str(stage)
                self.activity.update(self.activity_id, patch)
            except Exception:
                pass
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass


# ------------------------------------------------------------------ handlers
# Thin wrappers copied from the pre-0.8 worker. Business logic stays in the
# modules that own it; these functions only translate queue rows.
def _conn_scope():
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
        return {"status": "done",
                "message": result.get("message") or "Search finished.",
                "result": result}
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
        return {"status": "done",
                "message": result.get("message") or "Search finished.",
                "result": result}
    finally:
        conn.close()


def _movie_releases(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        releases = library.manual_movie_releases(
            conn, int(payload["movie_id"]), query=payload.get("query")
        )
        return {"status": "done",
                "message": f"{len(releases)} release(s) checked.",
                "result": {"releases": releases}}
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
        return {"status": "done",
                "message": f"{len(releases)} release(s) checked.",
                "result": {"releases": releases}}
    finally:
        conn.close()


def _probe_episode_candidate(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        release = library.probe_episode_candidate(
            conn, int(payload["series_id"]), int(payload["episode_id"]),
            int(payload["source_id"]), str(payload["provider_remote_id"]),
            str(payload["stream_id"]), refresh=bool(payload.get("refresh")),
        )
        probe_result = release.get("probe") or {}
        return {
            "status": "done",
            "message": (
                f"Probe finished: {release.get('resolution') or 'unknown'} "
                f"{release.get('video_codec') or ''}".strip()
                if probe_result.get("ok") else
                f"Probe failed: {probe_result.get('error') or 'unknown error'}"
            ),
            "result": {"release": release},
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
        return {"status": "done", "message": "Download queued.",
                "result": {"download": download}}
    finally:
        conn.close()


def _grab_episode(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        download = library.grab_episode(
            conn, int(payload["series_id"]), int(payload["episode_id"]),
            int(payload["source_id"]), str(payload["provider_remote_id"]),
            str(payload["stream_id"]), force=bool(payload.get("force")),
        )
        return {"status": "done", "message": "Download queued.",
                "result": {"download": download}}
    finally:
        conn.close()


def _refresh_movie(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        movie = library.refresh_movie(conn, int(payload["movie_id"]))
        return {"status": "done", "message": "Metadata refreshed.",
                "result": {"movie": movie}}
    finally:
        conn.close()


def _refresh_series(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        series = library.refresh_series(conn, int(payload["series_id"]))
        return {"status": "done", "message": "Metadata refreshed.",
                "result": {"series": series}}
    finally:
        conn.close()


def _probe_movie_candidate(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        release = library.probe_movie_candidate(
            conn, int(payload["movie_id"]), int(payload["source_id"]),
            str(payload["remote_id"]), refresh=bool(payload.get("refresh")),
        )
        probe = release.get("probe") or {}
        message = (f"Probed {release.get('resolution') or 'stream'}"
                   if probe.get("ok")
                   else f"Probe failed: {probe.get('error') or 'unknown error'}")
        return {"status": "done", "message": message, "result": release}
    finally:
        conn.close()


def _probe_movie_candidates(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        result = library.probe_movie_candidates(
            conn, int(payload["movie_id"]), refresh=bool(payload.get("refresh")),
        )
        return {"status": "done",
                "message": f"Probed {result['probed']} candidate(s); "
                           f"{result['failed']} failed.",
                "result": result}
    finally:
        conn.close()


def _scan_import(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    conn = _conn_scope()
    try:
        result = scan.import_result(conn, int(payload["result_id"]))
        return {"status": "done",
                "message": f"Imported {result.get('title') or 'file'}.",
                "result": result}
    finally:
        conn.close()


def _scan_import_all(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    del job
    conn = _conn_scope()
    try:
        result = scan.import_all(conn)
        return {"status": "done",
                "message": f"Imported {result.get('imported', 0)} file(s).",
                "result": result}
    finally:
        conn.close()


def _sync(job: Mapping[str, Any], stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    return sync.run_job(int(payload["source_id"]), int(job["id"]),
                        stop_event=stop)


def _prepare_auto_download(conn, download_id: int) -> None:
    """Auto/upgrade downloads must not also retry inside the downloader."""
    row = conn.execute(
        "SELECT reason, max_attempts FROM downloads WHERE id = ?",
        (int(download_id),),
    ).fetchone()
    if row is None:
        return
    reason = str(row["reason"] or "")
    if reason in ("auto", "upgrade", "manual-override"):
        if int(row["max_attempts"] or 0) != 1:
            conn.execute("UPDATE downloads SET max_attempts = 1 WHERE id = ?",
                         (int(download_id),))
            conn.commit()


def _download(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    download_id = int(payload["download_id"])
    conn = _conn_scope()
    try:
        if payload.get("bworker_managed") or payload.get("auto_work"):
            reason = "auto"
        else:
            row = conn.execute(
                "SELECT reason FROM downloads WHERE id = ?", (download_id,)
            ).fetchone()
            reason = str(row["reason"] or "") if row is not None else ""
        if reason in ("auto", "upgrade", "manual-override"):
            _prepare_auto_download(conn, download_id)
    finally:
        conn.close()
    return downloader.process_download(download_id)


def _task(job: Mapping[str, Any], _stop: threading.Event) -> dict:
    payload = job.get("payload") or {}
    return scheduler.run_task(str(payload["name"]),
                              str(payload.get("scope") or "all"))


# --- declarative work handlers ------------------------------------------------
def _download_poll_outcome(conn, ctx: WorkContext, download_id: int,
                           *, thing: str = "download") -> dict:
    row = conn.execute(
        "SELECT id, status, error, title FROM downloads WHERE id = ?",
        (int(download_id),),
    ).fetchone()
    if row is None:
        return {"status": "skipped", "stage": "skipped",
                "message": f"The {thing} no longer exists."}
    status = str(row["status"] or "")
    if status in ACTIVE_DOWNLOAD_STATUSES:
        ctx.set_stage("downloading", f"Downloading {row['title'] or thing}",
                      progress={"download_id": int(download_id)})
        return {"status": "success", "stage": "downloading",
                "message": f"Downloading \"{row['title'] or thing}\".",
                "next_run_at": ctx.now() + _poll_seconds(conn),
                "current_download_id": int(download_id)}
    if status == "failed":
        message = str(row["error"] or "the download failed")
        return {"status": "failure", "stage": "failed",
                "message": f"Download failed: {message}",
                "error": message}
    if status == "cancelled":
        return {"status": "skipped", "stage": "skipped",
                "message": "The download was cancelled."}
    # imported / done: the file exists; the next search interval checks for an
    # upgrade under the current profile.
    interval = _search_interval_seconds(conn, ctx.work)
    ctx.set_stage("awaiting_upgrade", "Download complete; checking upgrades later")
    return {"status": "success", "stage": "awaiting_upgrade",
            "message": "Download complete.",
            "next_run_at": (ctx.now() + interval) if interval else None}


def _work_movie_search(ctx: WorkContext) -> dict:
    movie = library.get_movie(ctx.conn, int(ctx.entity_id or 0))
    if movie is None:
        return {"status": "skipped", "stage": "skipped", "disable": True,
                "message": "That movie no longer exists."}
    if not bool(movie.get("monitored")):
        return {"status": "skipped", "stage": "skipped", "disable": True,
                "message": "The movie is not monitored."}
    active = store.link_active_download(ctx.conn, ctx.work, now=ctx.now())
    if active is not None:
        return _download_poll_outcome(ctx.conn, ctx, active, thing="movie")
    if not library.movie_available(movie):
        interval = _search_interval_seconds(ctx.conn, ctx.work)
        ctx.set_stage("waiting_availability",
                      f"Waiting for \"{movie['title']}\" to become available")
        return {"status": "success", "stage": "waiting_availability",
                "message": str(movie.get("search_message") or
                               "Waiting for availability."),
                "next_run_at": (ctx.now() + interval) if interval else None}

    ctx.set_stage("searching_candidates", f"Searching \"{movie['title']}\"",
                  progress={"candidate_batch": _setting_int(
                      ctx.conn, "candidate_batch_size", 25, minimum=1,
                      maximum=100)})
    outcome = acquisition.run_movie_iteration(
        ctx.conn, movie, work=ctx.work, reason="auto",
        stop=lambda: ctx.cancelled,
    )
    stage = str(outcome.get("stage") or "searching_candidates")
    message = str(outcome.get("message") or "Search finished.")
    ctx.set_stage(stage, message, outcome.get("progress"))
    download = outcome.get("download")
    if download:
        ctx.set_download(int(download["id"]))
        outcome["current_download_id"] = int(download["id"])
    outcome.setdefault("status", "success")
    return outcome


def _work_series_search(ctx: WorkContext) -> dict:
    series = library.get_series(ctx.conn, int(ctx.entity_id or 0))
    if series is None:
        return {"status": "skipped", "stage": "skipped", "disable": True,
                "message": "That series no longer exists."}
    if not bool(series.get("monitored")):
        return {"status": "skipped", "stage": "skipped", "disable": True,
                "message": "The series is not monitored."}
    active = store.link_active_download(ctx.conn, ctx.work, now=ctx.now())
    if active is not None:
        return _download_poll_outcome(ctx.conn, ctx, active, thing="episode")
    ctx.set_stage("searching_candidates", f"Searching \"{series['title']}\"")
    result = library.search_series(ctx.conn, series, reason="auto")
    interval = _search_interval_seconds(ctx.conn, ctx.work)
    next_run = (ctx.now() + interval) if interval else None
    if int(result.get("queued") or 0) > 0:
        ctx.set_stage("downloading", str(result.get("message") or "Download queued"))
        return {"status": "success", "stage": "downloading",
                "message": str(result.get("message") or "Download queued."),
                "next_run_at": ctx.now() + _poll_seconds(ctx.conn)}
    ctx.set_stage("awaiting_upgrade", str(result.get("message") or "No missing episodes"))
    return {"status": "success", "stage": "awaiting_upgrade",
            "message": str(result.get("message") or "Search finished."),
            "next_run_at": next_run}


def _work_episode_search(ctx: WorkContext) -> dict:
    payload = ctx.payload
    episode_id = int(payload.get("episode_id") or ctx.entity_id or 0)
    episode = library.get_episode(ctx.conn, episode_id)
    if episode is None:
        return {"status": "skipped", "stage": "skipped", "disable": True,
                "message": "That episode no longer exists."}
    series = library.get_series(ctx.conn, int(episode["series_id"]))
    if series is None:
        return {"status": "skipped", "stage": "skipped", "disable": True,
                "message": "That series no longer exists."}
    if not bool(series.get("monitored")) or not bool(episode.get("monitored")):
        return {"status": "skipped", "stage": "skipped", "disable": True,
                "message": "The episode is not monitored."}
    active = store.link_active_download(ctx.conn, ctx.work, now=ctx.now())
    if active is not None:
        return _download_poll_outcome(ctx.conn, ctx, active, thing="episode")
    air = episode.get("air_date")
    stage, _next = store.episode_initial_stage(air, ctx.now())
    if stage == "waiting_air":
        ctx.set_stage("waiting_air", "Waiting for the air date")
        return {"status": "success", "stage": "waiting_air",
                "message": "Waiting for the air date.",
                "next_run_at": _next}
    label = (f"{series['title']} S{int(episode['season']):02d}"
             f"E{int(episode['episode']):02d}")
    ctx.set_stage("searching_candidates", f"Searching \"{label}\"")
    outcome = acquisition.run_episode_iteration(
        ctx.conn, series, episode, work=ctx.work, reason="auto",
        stop=lambda: ctx.cancelled,
    )
    stage = str(outcome.get("stage") or "searching_candidates")
    message = str(outcome.get("message") or "Search finished.")
    ctx.set_stage(stage, message, outcome.get("progress"))
    download = outcome.get("download")
    if download:
        ctx.set_download(int(download["id"]))
        outcome["current_download_id"] = int(download["id"])
    outcome.setdefault("status", "success")
    return outcome


def _work_download(ctx: WorkContext) -> dict:
    """One download work unit: wait for the queue job, then poll the row."""
    download_id = ctx.work.get("current_download_id")
    if download_id is None:
        download_id = store.active_download_for_work(ctx.conn, ctx.work)
    if download_id is None:
        return {"status": "skipped", "stage": "skipped",
                "message": "No download is attached to this unit."}
    download_id = int(download_id)
    ctx.set_download(download_id)
    row = ctx.conn.execute(
        "SELECT status, reason FROM downloads WHERE id = ?", (download_id,)
    ).fetchone()
    if row is None:
        return {"status": "skipped", "stage": "skipped",
                "message": "The download no longer exists."}
    if row["status"] in ACTIVE_DOWNLOAD_STATUSES:
        _prepare_auto_download(ctx.conn, download_id)
        active_job = jobs.active_for(ctx.conn, "download",
                                     dedupe_key=f"download:{download_id}")
        if active_job is None:
            jobs.enqueue(ctx.conn, "download", {"download_id": download_id,
                                                "bworker_managed": True},
                         priority=40)
        return _download_poll_outcome(ctx.conn, ctx, download_id, thing="download")
    return _download_poll_outcome(ctx.conn, ctx, download_id, thing="download")


def _work_probe(ctx: WorkContext) -> dict:
    payload = ctx.payload
    movie_id = payload.get("movie_id")
    if movie_id is not None:
        ctx.set_stage("probing", "Probing candidates")
        result = library.probe_movie_candidates(
            ctx.conn, int(movie_id), refresh=bool(payload.get("refresh")),
        )
        return {"status": "success", "stage": "probing",
                "message": f"Probed {result.get('probed', 0)} candidate(s)."}
    return {"status": "skipped", "stage": "skipped",
            "message": "No probe target in this unit."}


def _work_playlist_sync(ctx: WorkContext) -> dict:
    payload = ctx.payload
    ctx.set_stage("search", "Updating playlist")
    source_id = payload.get("source_id")
    if source_id is not None:
        queued = sync.enqueue([int(source_id)])
    else:
        queued = sync.enqueue_all()
    hours = 24
    try:
        hours = float(_setting(ctx.conn, "catalog_refresh_hours", "24"))
    except (TypeError, ValueError):
        hours = 24.0
    next_run = ctx.now() + max(60, int(hours * 3600)) if hours > 0 else None
    return {"status": "success", "stage": "complete",
            "message": f"Queued {len(queued)} playlist sync(s).",
            "next_run_at": next_run}


def _work_metadata_refresh(ctx: WorkContext) -> dict:
    ctx.set_stage("search", "Refreshing metadata")
    message = scheduler.run_task("refresh_metadata", "all")
    try:
        hours = float(_setting(ctx.conn, "refresh_interval_hours", "168"))
    except (TypeError, ValueError):
        hours = 168.0
    next_run = ctx.now() + max(60, int(hours * 3600)) if hours > 0 else None
    return {"status": "success", "stage": "complete", "message": str(message),
            "next_run_at": next_run}


def _work_library_scan(ctx: WorkContext) -> dict:
    ctx.set_stage("searching_candidates", "Scanning the library")
    message = scheduler.run_task("scan_library", str(ctx.payload.get("scope") or "all"))
    return {"status": "success", "stage": "complete", "message": str(message)}


def _work_activity_cleanup(ctx: WorkContext) -> dict:
    ctx.set_stage("pending", "Cleaning the activity log")
    result = ctx.runner.run_cleanup(ctx.conn, force=True)
    return {"status": "success", "stage": "complete",
            "message": f"Activity log trimmed to {result.get('activity', 0)}.",
            "next_run_at": ctx.now() + 3600}


# ------------------------------------------------------------------ job target
_JOB_LABELS = {
    "sync": "Updating playlist",
    "download": "Downloading",
    "task": "Running maintenance task",
    "search_movie": "Searching",
    "search_series": "Searching",
    "movie_releases": "Checking releases",
    "episode_releases": "Checking releases",
    "grab_movie": "Queuing download",
    "grab_episode": "Queuing download",
    "refresh_movie": "Refreshing metadata",
    "refresh_series": "Refreshing metadata",
    "probe_movie_candidate": "Probing stream",
    "probe_movie_candidates": "Probing streams",
    "probe_episode_candidate": "Probing stream",
    "scan_import": "Importing file",
    "scan_import_all": "Importing files",
}


def _episode_label(conn, episode_id: int) -> str | None:
    row = conn.execute(
        "SELECT s.title, e.season, e.episode FROM series_episodes e "
        "JOIN series s ON s.id = e.series_id WHERE e.id = ?",
        (int(episode_id),),
    ).fetchone()
    if row is None:
        return None
    return (f"{row['title']} S{int(row['season']):02d}"
            f"E{int(row['episode']):02d}")


def _job_target(conn, job: Mapping[str, Any]) -> dict:
    payload = _decode(job.get("payload"))
    kind = str(job.get("kind") or "job")
    label = _JOB_LABELS.get(kind, kind.replace("_", " ").title())
    entity_type = "job"
    entity_id = int(job.get("id") or 0)
    try:
        if kind in ("sync", "refresh_catalog"):
            source_id = payload.get("source_id")
            if source_id is not None:
                row = conn.execute("SELECT name FROM sources WHERE id = ?",
                                   (int(source_id),)).fetchone()
                if row is not None:
                    label = f"Updating playlist \"{row['name']}\""
                entity_type, entity_id = "source", int(source_id)
        elif kind == "download":
            download_id = int(payload.get("download_id") or 0)
            row = conn.execute(
                "SELECT title, movie_id, episode_id, series_id FROM downloads "
                "WHERE id = ?", (download_id,),
            ).fetchone()
            if row is not None:
                label = f"Downloading \"{row['title'] or 'release'}\""
                if row["movie_id"] is not None:
                    entity_type, entity_id = "movie", int(row["movie_id"])
                elif row["episode_id"] is not None:
                    entity_type, entity_id = "episode", int(row["episode_id"])
                elif row["series_id"] is not None:
                    entity_type, entity_id = "series", int(row["series_id"])
                else:
                    entity_type, entity_id = "download", download_id
        elif kind == "search_movie":
            movie = library.get_movie(conn, int(payload.get("movie_id") or 0))
            if movie is not None:
                label = f"Searching \"{movie['title']}\""
                entity_type, entity_id = "movie", int(movie["id"])
        elif kind in ("search_series", "episode_releases"):
            series_id = payload.get("series_id")
            episode_id = payload.get("episode_id")
            if episode_id is not None:
                text = _episode_label(conn, int(episode_id))
                if text:
                    label = f"Searching \"{text}\""
                    entity_type, entity_id = "episode", int(episode_id)
            elif series_id is not None:
                series = library.get_series(conn, int(series_id))
                if series is not None:
                    label = f"Searching \"{series['title']}\""
                entity_type, entity_id = "series", int(series_id)
        elif kind in ("refresh_movie", "probe_movie_candidate",
                      "probe_movie_candidates", "grab_movie",
                      "movie_releases"):
            movie = library.get_movie(conn, int(payload.get("movie_id") or 0))
            if movie is not None:
                label = f"{_JOB_LABELS.get(kind, 'Working')} \"{movie['title']}\""
                entity_type, entity_id = "movie", int(movie["id"])
        elif kind in ("refresh_series",):
            series = library.get_series(conn, int(payload.get("series_id") or 0))
            if series is not None:
                label = f"{_JOB_LABELS.get(kind, 'Working')} \"{series['title']}\""
                entity_type, entity_id = "series", int(series["id"])
        elif kind in ("grab_episode", "probe_episode_candidate",
                      "scan_import", "scan_import_all"):
            episode_id = payload.get("episode_id")
            if episode_id is not None:
                text = _episode_label(conn, int(episode_id))
                if text:
                    label = f"{_JOB_LABELS.get(kind, 'Working')} \"{text}\""
                    entity_type, entity_id = "episode", int(episode_id)
        elif kind == "task":
            name = str(payload.get("name") or "task")
            label = f"Running {name.replace('_', ' ')}"
            entity_type, entity_id = "global", 0
    except Exception:
        pass
    return {"kind": entity_type, "id": entity_id, "label": label}


def _work_start_message(work: Mapping[str, Any], label: str) -> str:
    work_type = str(work.get("work_type") or "")
    stage = str(work.get("stage") or "")
    if work_type == "movie_search" or work_type == "episode_search":
        if stage == "waiting_availability":
            return f"Waiting for availability: \"{label}\""
        if stage == "waiting_air":
            return f"Waiting for the air date: \"{label}\""
        if stage == "downloading":
            return f"Watching download \"{label}\""
        if stage == "awaiting_upgrade":
            return f"Checking for an upgrade: \"{label}\""
        return f"Searching \"{label}\""
    if work_type == "series_search":
        return f"Searching \"{label}\""
    if work_type == "playlist_sync":
        return f"Updating playlist \"{label}\""
    if work_type == "metadata_refresh":
        return f"Refreshing metadata for \"{label}\""
    if work_type == "download":
        return f"Downloading \"{label}\""
    if work_type == "probe":
        return f"Probing \"{label}\""
    if work_type == "library_scan":
        return "Scanning the library"
    if work_type == "activity_cleanup":
        return "Cleaning the activity log"
    return str(work.get("work_key") or work_type or "Working")


# ---------------------------------------------------------------------- runner
class Bworker:
    """One bworker loop. Instantiate once per process; tests use ``step()``."""

    def __init__(self, *, activity=None, stop: threading.Event | None = None,
                 clock=None, wall_clock=None, connection_factory=None,
                 progress_watch: bool = True,
                 progress_interval: float = PROGRESS_UPDATE_SECONDS) -> None:
        self.activity = (activity if activity is not None
                         else activity_log.default_store())
        self.stop = stop or threading.Event()
        self.clock = clock or time.monotonic
        self.wall = wall_clock or time.time
        self._conn_factory = connection_factory or db.connect
        self._progress_watch = bool(progress_watch)
        self._progress_interval = float(progress_interval)
        self._current_lock = threading.Lock()
        self._current: dict[str, Any] = {
            "job_id": None, "work_id": None, "label": None, "status": "idle",
        }
        self._was_paused = False
        self._started = False
        self._no_work_logged = False
        self._last_cleanup = float("-inf")
        self._last_job_cleanup = float("-inf")
        self._activity_limits = (activity_log.DEFAULT_CAP,
                                 activity_log.DEFAULT_TRIM_TO)

    # ---------------------------------------------------------------- setup
    def _now(self) -> int:
        return int(self.wall())

    def _set_current(self, job_id: int | None, work_id: int | None,
                     label: str | None, status: str) -> None:
        with self._current_lock:
            self._current.update({"job_id": job_id, "work_id": work_id,
                                  "label": label, "status": status})

    def _current_snapshot(self) -> dict:
        with self._current_lock:
            return dict(self._current)

    def _configure_activity(self, conn) -> tuple[int, int]:
        cap = _setting_int(conn, "activity_log_cap",
                           activity_log.DEFAULT_CAP, minimum=10,
                           maximum=100000)
        trim = _setting_int(conn, "activity_log_trim_to",
                            activity_log.DEFAULT_TRIM_TO, minimum=1,
                            maximum=cap - 1)
        self._activity_limits = (cap, trim)
        configure = getattr(self.activity, "configure", None)
        if callable(configure):
            configure(cap, trim)
        return cap, trim

    def startup(self) -> None:
        """Recover orphans, seed work, heartbeat, and reconcile the pause log."""
        db.init_db()
        now = self._now()
        conn = self._conn_factory()
        try:
            try:
                jobs.recover(conn)
            except Exception:
                conn.rollback()
            try:
                orphaned = store.recover_orphans(conn, now=now)
                if orphaned["requeued"]:
                    logger.warning("bworker: requeued %d interrupted work row(s)",
                                   orphaned["requeued"])
            except Exception:
                conn.rollback()
                logger.exception("bworker: orphan recovery failed")
            try:
                self.activity.mark_running_interrupted(now)
            except Exception:
                pass
            try:
                store.seed_all(conn, now)
                store.reconcile_enabled(conn, now=now)
            except Exception:
                conn.rollback()
                logger.exception("bworker: work seeding failed")
            jobs.worker_started(conn, os.getpid())
            state = store.get_state(conn)
            self._was_paused = state["paused"]
            status = "paused" if state["paused"] else "starting"
            store.heartbeat(conn, status=status, now=now)
            jobs.heartbeat(conn, status=status)
            jobs.write_heartbeat_file(None, status)
            self._configure_activity(conn)
        finally:
            conn.close()
        try:
            downloader.recover()
        except Exception:
            logger.exception("bworker: download recovery failed")
        self._started = True
        self._reconcile_pause_entry()

    def _reconcile_pause_entry(self) -> None:
        try:
            conn = self._conn_factory()
            try:
                state = store.get_state(conn)
            finally:
                conn.close()
            recent = self.activity.recent(20)
            last = next((entry for entry in recent
                         if entry.get("event") in (activity_log.EVENT_PAUSED,
                                                   activity_log.EVENT_RESUMED)),
                        None)
            if state["paused"]:
                if last is None or last.get("event") != activity_log.EVENT_PAUSED:
                    self._push_pause_entry(paused=True)
                self._was_paused = True
            else:
                if last is not None and last.get("event") == activity_log.EVENT_PAUSED:
                    self._push_pause_entry(paused=False)
                self._was_paused = False
        except Exception:
            logger.debug("bworker: pause log reconciliation failed", exc_info=True)

    def _push_pause_entry(self, *, paused: bool) -> None:
        now = self._now()
        try:
            self.activity.push(activity_log.make_entry(
                event=(activity_log.EVENT_PAUSED if paused
                       else activity_log.EVENT_RESUMED),
                status=(activity_log.STATUS_PAUSED if paused
                        else activity_log.STATUS_IDLE),
                message="Bworker paused" if paused else "Bworker resumed",
                stage="paused" if paused else "idle",
                started_at=now, at=now,
            ))
        except Exception:
            pass

    def _ensure_pause_entries(self, *, paused: bool) -> None:
        if paused and not self._was_paused:
            self._push_pause_entry(paused=True)
            self._was_paused = True
        elif not paused and self._was_paused:
            self._push_pause_entry(paused=False)
            self._was_paused = False

    # ------------------------------------------------------------- housekeeping
    def run_cleanup(self, conn=None, *, force: bool = False) -> dict:
        """Trim the activity log (and old jobs hourly). Never raises."""
        own = conn is None
        if own:
            conn = self._conn_factory()
        try:
            cap, trim_to = self._configure_activity(conn)
            length = None
            try:
                length = self.activity.trim(cap, trim_to)
            except Exception:
                length = None
            now = self.clock()
            jobs_removed = 0
            if force or (now - self._last_job_cleanup) >= 3600:
                self._last_job_cleanup = now
                try:
                    jobs_removed = jobs.cleanup(conn)
                except Exception:
                    conn.rollback()
            self._last_cleanup = now
            return {"activity": length, "cap": cap, "trim_to": trim_to,
                    "jobs_removed": jobs_removed}
        finally:
            if own:
                conn.close()

    def _maybe_cleanup(self, conn) -> None:
        now = self.clock()
        if (now - self._last_cleanup) < 5.0:
            return
        self.run_cleanup(conn)

    def _housekeeping(self, conn) -> None:
        try:
            orphans = jobs.recover_running(conn)
            if orphans:
                logger.warning("bworker: requeued %d orphaned job(s)", orphans)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            store.consume_resets(conn, now=self._now())
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        self._maybe_cleanup(conn)

    # ----------------------------------------------------------------- entries
    def _push(self, **fields) -> int | None:
        try:
            entry = self.activity.push(activity_log.make_entry(**fields))
        except Exception:
            return None
        if isinstance(entry, Mapping) and entry.get("id") is not None:
            try:
                return int(entry["id"])
            except (TypeError, ValueError):
                return None
        return None

    def _log_no_work(self, conn, next_run_at: int | None) -> None:
        if self._no_work_logged:
            return
        self._no_work_logged = True
        self._push(
            event=activity_log.EVENT_NO_WORK,
            status=activity_log.STATUS_IDLE,
            message="No work due",
            stage="idle",
            next_run_at=next_run_at,
            progress={"worker_jobs": jobs.counts(conn),
                      "work": store.counts(conn)},
            at=self._now(),
        )

    def _finish_activity(self, activity_id: int | None, *,
                         status: str, stage: str | None, message: str,
                         next_run_at: int | None = None,
                         progress: Mapping[str, Any] | None = None,
                         level: str = "info", attempt: int | None = None,
                         started_at: int | None = None,
                         current_download_id: int | None = None) -> None:
        if not activity_id:
            return
        now = self._now()
        patch: dict[str, Any] = {
            "event": activity_log.EVENT_FINISHED,
            "status": status,
            "level": level,
            "message": message,
            "stage": stage,
            "next_run_at": next_run_at,
            "finished_at": now,
            "attempt": attempt,
        }
        if progress is not None:
            patch["progress"] = dict(progress)
        if started_at is not None:
            patch["duration"] = max(0, now - int(started_at))
        if current_download_id is not None:
            patch["current_download_id"] = int(current_download_id)
        try:
            self.activity.update(activity_id, patch)
        except Exception:
            pass

    # -------------------------------------------------------------- job units
    def _is_auto_download_job(self, conn, job: Mapping[str, Any]) -> bool:
        payload = _decode(job.get("payload"))
        if payload.get("bworker_managed") or payload.get("auto_work"):
            return True
        download_id = payload.get("download_id")
        if download_id is None:
            return False
        row = conn.execute(
            "SELECT reason FROM downloads WHERE id = ?", (int(download_id),)
        ).fetchone()
        return row is not None and str(row["reason"] or "") in (
            "auto", "upgrade", "manual-override")

    def run_claimed_job(self, conn, job: Mapping[str, Any]) -> dict:
        kind = str(job.get("kind") or "")
        job_id = int(job["id"])
        attempts = int(job.get("attempts") or 0)
        target = _job_target(conn, job)
        started_wall = self._now()
        started = self.clock()
        activity_id = self._push(
            event=activity_log.EVENT_STARTED,
            status=activity_log.STATUS_RUNNING,
            level="info",
            work_id=None,
            work_type=kind,
            target=target,
            stage="running",
            message=str(target.get("label") or kind),
            attempt=attempts,
            started_at=started_wall,
            at=started_wall,
            job_id=job_id,
        )
        if kind == "download" and self._is_auto_download_job(conn, job):
            payload = _decode(job.get("payload"))
            if payload.get("download_id") is not None:
                _prepare_auto_download(conn, int(payload["download_id"]))
        self._set_current(job_id, None, str(target.get("label") or kind),
                          "running")
        try:
            store.heartbeat(conn, status="running", now=self._now())
            jobs.heartbeat(conn, current_job_id=job_id, status="running")
            jobs.write_heartbeat_file(job_id, "running")
        except Exception:
            pass
        watcher = None
        if (self._progress_watch and activity_id
                and kind not in ("sync", "download")):
            watcher = _JobProgressWatcher(
                connection_factory=self._conn_factory, job_id=job_id,
                activity_id=activity_id, activity=self.activity,
                interval=self._progress_interval, attempt=attempts,
                parent_stop=self.stop,
            )
            watcher.start()
        outcome: dict = {}
        try:
            handler = JOB_HANDLERS.get(kind)
            if handler is None:
                raise ValueError(f"Unknown worker job kind: {kind}")
            outcome = dict(handler(job, self.stop) or {})
            self._apply_job_outcome(conn, job, outcome)
            logger.info("bworker: job #%d %s finished: %s", job_id, kind,
                        outcome.get("message", ""))
        except Exception as exc:
            logger.exception("bworker: job #%d %s failed: %s", job_id, kind, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            outcome = {
                "status": "error",
                "message": "Worker job failed",
                "error": f"{type(exc).__name__}: {exc}",
                "error_type": type(exc).__name__,
            }
            try:
                if self.stop.is_set():
                    jobs.reschedule(conn, job_id,
                                    available_at=self._now(),
                                    message="Interrupted by shutdown; queued again.")
                    outcome = {"status": "retry",
                               "message": "Interrupted; queued again.",
                               "available_at": self._now()}
                else:
                    jobs.finish(conn, job_id, "error",
                                message="Worker job failed",
                                error=outcome["error"],
                                error_type=outcome["error_type"])
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.exception("bworker: could not record job failure")
        finally:
            if watcher is not None:
                watcher.stop()
                watcher.join(timeout=2.0)
        duration = max(0.0, self.clock() - started)
        status = str(outcome.get("status") or "done")
        if outcome.get("retry") or status == "retry":
            activity_status = activity_log.STATUS_RETRY
            level = "warning"
        elif status in ("error", "failed"):
            activity_status = activity_log.STATUS_FAILURE
            level = "error"
        elif status == "cancelled":
            activity_status = activity_log.STATUS_SKIPPED
            level = "warning"
        else:
            activity_status = activity_log.STATUS_SUCCESS
            level = "info"
        self._finish_activity(
            activity_id,
            status=activity_status,
            stage="complete" if activity_status == activity_log.STATUS_SUCCESS else
                  ("failed" if activity_status == activity_log.STATUS_FAILURE else "skipped"),
            message=str(outcome.get("message") or
                        ("Queued for retry." if activity_status ==
                         activity_log.STATUS_RETRY else "")),
            next_run_at=(outcome.get("available_at")
                         if activity_status == activity_log.STATUS_RETRY else None),
            progress=outcome.get("data"),
            level=level,
            attempt=max(attempts, 1),
            started_at=started_wall,
        )
        self._set_current(None, None, None, "idle")
        try:
            store.heartbeat(conn, status="idle", now=self._now())
            jobs.heartbeat(conn, status="idle")
            jobs.write_heartbeat_file(None, "idle")
        except Exception:
            pass
        return {"kind": "job", "job_id": job_id, "kind_detail": kind,
                "status": outcome.get("status"), "elapsed": duration}

    def _apply_job_outcome(self, conn, job: Mapping[str, Any],
                           outcome: Mapping[str, Any] | None) -> None:
        job_id = int(job["id"])
        outcome = dict(outcome or {})
        if outcome.get("retry"):
            jobs.reschedule(
                conn, job_id,
                available_at=int(outcome.get("available_at") or self._now()),
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

    # ------------------------------------------------------------- work units
    def run_work(self, conn, work: Mapping[str, Any]) -> dict:
        work_id = int(work["id"])
        work_type = str(work.get("work_type") or "")
        target = store.work_target(conn, work)
        message = _work_start_message(work, str(target.get("label") or work_type))
        started_wall = self._now()
        started = self.clock()
        attempt = int(work.get("failure_count") or 0) + 1
        activity_id = self._push(
            event=activity_log.EVENT_STARTED,
            status=activity_log.STATUS_RUNNING,
            level="info",
            work_id=work_id,
            work_type=work_type,
            target=target,
            stage=str(work.get("stage") or "pending"),
            message=message,
            attempt=attempt,
            started_at=started_wall,
            at=started_wall,
        )
        self._set_current(None, work_id, str(target.get("label") or work_type),
                          "running")
        try:
            store.heartbeat(conn, status="running", current_work_id=work_id,
                            current_work_label=str(target.get("label") or
                                                   work_type),
                            now=self._now())
            jobs.heartbeat(conn, status="running")
            jobs.write_heartbeat_file(None, "running")
        except Exception:
            pass
        ctx = WorkContext(runner=self, conn=conn, work=work,
                          activity_id=activity_id, stop=self.stop)
        ctx._record_event("started", message, ctx.progress,
                          started_at=started_wall)
        outcome: dict = {}
        try:
            handler = WORK_HANDLERS.get(work_type)
            if handler is None:
                outcome = {"status": "skipped", "stage": "skipped",
                           "disable": True,
                           "message": f"No handler for work type {work_type}."}
            else:
                outcome = dict(handler(ctx) or {})
        except Exception as exc:
            logger.exception("bworker: work #%d %s failed: %s", work_id,
                             work_type, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            outcome = {"status": "failure", "stage": "failed",
                       "message": f"{type(exc).__name__}: {exc}",
                       "error": f"{type(exc).__name__}: {exc}"}
        try:
            if self.stop.is_set() and str(outcome.get("status")) not in (
                    "success", "done", "complete", "no_candidates",
                    "awaiting_upgrade"):
                # Shutdown is not a failure: leave the unit ready for restart.
                result = store.finish_skipped(
                    conn, work,
                    {"stage": ctx.stage or work.get("stage") or "pending",
                     "next_run_at": self._now(),
                     "message": "Interrupted by shutdown; queued again."},
                    now=self._now(),
                )
                activity_status = activity_log.STATUS_SKIPPED
                level = "warning"
                outcome_message = "Interrupted; queued again."
            else:
                result = self._apply_work_outcome(conn, work, ctx, outcome)
                activity_status, level = _activity_status_for_work(
                    result, outcome)
                outcome_message = str(
                    outcome.get("message") or result.get("message") or "")
        except Exception:
            logger.exception("bworker: could not record work outcome for #%d",
                             work_id)
            result = {"status": "failed_stopped", "stage": "failed"}
            activity_status, level = activity_log.STATUS_FAILURE, "error"
            outcome_message = "Could not record the work outcome."
        duration = max(0.0, self.clock() - started)
        self._finish_activity(
            activity_id,
            status=activity_status,
            stage=str(result.get("stage") or ctx.stage or "complete"),
            message=outcome_message,
            next_run_at=result.get("next_run_at"),
            progress=ctx.progress,
            level=level,
            attempt=max(int(result.get("failure_count") or attempt), 1),
            started_at=started_wall,
            current_download_id=result.get("current_download_id"),
        )
        event_status = {
            activity_log.STATUS_SUCCESS: "succeeded",
            activity_log.STATUS_FAILURE: "failed",
            activity_log.STATUS_SKIPPED: "skipped",
        }.get(activity_status, "info")
        ctx.stage = str(result.get("stage") or ctx.stage or "complete")
        ctx._record_event(event_status, outcome_message, ctx.progress,
                          started_at=started_wall, finished_at=self._now())
        self._set_current(None, None, None, "idle")
        try:
            store.heartbeat(conn, status="idle", now=self._now())
            jobs.heartbeat(conn, status="idle")
            jobs.write_heartbeat_file(None, "idle")
        except Exception:
            pass
        return {"kind": "work", "work_id": work_id,
                "work_type": work_type, "status": result.get("status"),
                "stage": result.get("stage"), "elapsed": duration,
                "next_run_at": result.get("next_run_at")}

    def _apply_work_outcome(self, conn, work: Mapping[str, Any],
                            ctx: WorkContext, outcome: Mapping[str, Any]) -> dict:
        now = self._now()
        status = str(outcome.get("status") or "success").lower()
        stage = outcome.get("stage") or ctx.stage or None
        message = str(outcome.get("message") or "")
        if outcome.get("disable"):
            return store.finish_skipped(conn, work, {
                "stage": stage or "skipped",
                "message": message,
                "disable": True,
                "next_run_at": None,
            }, now=now)
        if status in ("failure", "failed", "error", "retry"):
            error = str(outcome.get("error") or message or "Work failed")
            return store.finish_failure(conn, work, error=error,
                                        stage=stage or "failed", now=now)
        if status in ("skipped", "cancelled", "disabled"):
            return store.finish_skipped(conn, work, {
                "stage": stage or "skipped",
                "message": message,
                "next_run_at": outcome.get("next_run_at"),
                "disable": bool(outcome.get("disable")),
            }, now=now)
        # success / done / complete / no_candidates / awaiting_upgrade ...
        return store.finish_success(conn, work, {
            "stage": stage or "complete",
            "message": message,
            **{key: outcome[key] for key in (
                "next_run_at", "cursor", "best_score", "best_source_id",
                "best_remote_id", "best_episode_stream_id",
                "profile_revision", "current_download_id",
            ) if key in outcome},
        }, now=now)

    # ------------------------------------------------------------------- step
    def step(self) -> dict:
        """Run one bounded unit. Returns scheduling information for tests."""
        started = self.clock()
        conn = self._conn_factory()
        kind = "idle"
        work_id = None
        job_id = None
        paused = False
        wait = 0.0
        has_due = False
        try:
            self._housekeeping(conn)
            state = store.get_state(conn)
            paused = bool(state["paused"])
            self._ensure_pause_entries(paused=paused)
            if paused:
                kind = "paused"
                self._set_current(None, None, None, "paused")
                try:
                    store.heartbeat(conn, status="paused", now=self._now())
                    jobs.heartbeat(conn, status="paused")
                    jobs.write_heartbeat_file(None, "paused")
                except Exception:
                    pass
                wait = PAUSE_POLL_SECONDS
            else:
                try:
                    queued = scheduler.enqueue_due()
                    if queued:
                        logger.info("bworker: queued %d periodic task(s)", queued)
                except Exception as exc:
                    logger.warning("bworker: could not queue periodic tasks: %s", exc)
                job = None
                try:
                    job = jobs.claim(conn)
                except Exception:
                    logger.exception("bworker: job claim failed")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if job is not None:
                    self._no_work_logged = False
                    job_id = int(job["id"])
                    result = self.run_claimed_job(conn, job)
                    kind = "job"
                else:
                    work = store.claim_work(conn, now=self._now())
                    if work is not None:
                        self._no_work_logged = False
                        work_id = int(work["id"])
                        result = self.run_work(conn, work)
                        kind = "work"
                    else:
                        result = None
                try:
                    has_due = (jobs.has_due(conn)
                               or store.due_exists(conn, now=self._now()))
                except Exception:
                    has_due = False
                elapsed = max(0.0, self.clock() - started)
                if has_due:
                    wait = 0.0
                elif elapsed <= self._fast_loop_seconds(conn):
                    wait = float(self._idle_wait_seconds(conn))
                else:
                    wait = 0.0
                if kind == "idle" and wait > 0:
                    next_run = self._next_run_at(conn)
                    self._log_no_work(conn, next_run)
                if kind == "idle":
                    try:
                        store.heartbeat(conn, status="idle", now=self._now())
                        jobs.heartbeat(conn, status="idle")
                        jobs.write_heartbeat_file(None, "idle")
                    except Exception:
                        pass
            self._maybe_cleanup(conn)
            return {
                "kind": kind,
                "paused": paused,
                "ran": kind in ("job", "work"),
                "job_id": job_id,
                "work_id": work_id,
                "has_due": has_due,
                "wait": wait,
                "elapsed": max(0.0, self.clock() - started),
                "activity_id": None,
            }
        finally:
            conn.close()

    def _idle_wait_seconds(self, conn) -> int:
        return _setting_int(conn, "bworker_idle_wait_seconds", 30,
                            minimum=0, maximum=3600)

    def _fast_loop_seconds(self, conn) -> int:
        return _setting_int(conn, "bworker_fast_loop_seconds", 10,
                            minimum=0, maximum=3600)

    def _next_run_at(self, conn) -> int | None:
        candidates: list[int] = []
        try:
            jobs_next = jobs.next_due_at(conn)
            if jobs_next is not None:
                candidates.append(jobs_next)
        except Exception:
            pass
        try:
            work_next = store.next_due_at(conn, now=self._now())
            if work_next is not None:
                candidates.append(work_next)
        except Exception:
            pass
        return min(candidates) if candidates else None

    # ------------------------------------------------------------------- loop
    def _heartbeat_loop(self, stop: threading.Event) -> None:
        conn = None
        try:
            conn = self._conn_factory()
            while not stop.wait(5.0):
                current = self._current_snapshot()
                try:
                    state = store.get_state(conn)
                    status = "paused" if state["paused"] else str(
                        current.get("status") or "idle")
                    jobs.write_heartbeat_file(current.get("job_id"), status)
                    jobs.heartbeat(conn, current_job_id=current.get("job_id"),
                                   status=status)
                    store.heartbeat(
                        conn, status=status,
                        current_work_id=current.get("work_id"),
                        current_work_label=current.get("label"),
                        now=self._now(),
                    )
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                try:
                    self._maybe_cleanup(conn)
                except Exception:
                    pass
        finally:
            if conn is not None:
                conn.close()

    def run_forever(self) -> None:
        db.init_db()
        lock_path = config.data_dir() / "worker.lock"
        lock = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.error("bworker: another bworker already owns %s; exiting",
                         lock_path)
            lock.close()
            return
        try:
            self.startup()
        except Exception:
            logger.error("bworker: startup failed\n%s", traceback.format_exc())
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()
            raise
        hb_stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat_loop, args=(hb_stop,),
                              name="bworker-heartbeat", daemon=True)
        hb.start()
        try:
            while not self.stop.is_set():
                try:
                    result = self.step()
                except Exception:
                    logger.exception("bworker: loop iteration failed")
                    result = {"wait": 1.0}
                wait = float(result.get("wait") or 0.0)
                if wait > 0 and self.stop.wait(wait):
                    break
                elif wait <= 0:
                    # Due work is immediate; still check stop to avoid a tight
                    # loop during shutdown.
                    if self.stop.is_set():
                        break
        finally:
            hb_stop.set()
            hb.join(timeout=3.0)
            try:
                conn = self._conn_factory()
                try:
                    store.heartbeat(conn, status="stopped", now=self._now())
                    store.mark_stopped(conn, now=self._now())
                    jobs.heartbeat(conn, status="stopped")
                finally:
                    conn.close()
                jobs.write_heartbeat_file(None, "stopped")
            except Exception:
                pass
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            finally:
                lock.close()
            logger.info("bworker: stopped")


def _activity_status_for_work(result: Mapping[str, Any],
                              outcome: Mapping[str, Any]) -> tuple[str, str]:
    status = str(result.get("status") or "")
    if status == "failed_stopped":
        return activity_log.STATUS_FAILURE, "error"
    if status == "retry":
        return activity_log.STATUS_RETRY, "warning"
    if status == "disabled" or str(outcome.get("status")) in (
            "skipped", "cancelled"):
        return activity_log.STATUS_SKIPPED, "warning"
    if status == "pending":
        return activity_log.STATUS_SUCCESS, "info"
    return activity_log.STATUS_SUCCESS, "info"


# ------------------------------------------------------------------- health
def status(conn=None, *, activity=None) -> dict:
    """Bworker health/current-work snapshot for /api/health and the API."""
    own = conn is None
    if own:
        conn = db.connect()
    try:
        now = int(time.time())
        state = store.get_state(conn)
        worker = jobs.worker_status(conn)
        heartbeat_at = state.get("heartbeat_at")
        age = max(0, now - int(heartbeat_at)) if heartbeat_at else None
        alive = bool(age is not None and age <= 30
                     and state.get("status") != "stopped")
        paused = bool(state.get("paused"))
        current = None
        if state.get("current_work_id"):
            work = store.get_work(conn, int(state["current_work_id"]))
            if work is not None:
                target = store.work_target(conn, work)
                current = {
                    "work_id": int(work["id"]),
                    "work_type": work.get("work_type"),
                    "label": state.get("current_work_label") or
                             target.get("label"),
                    "target": target,
                    "stage": work.get("stage"),
                    "status": work.get("status"),
                    "started_at": work.get("started_at"),
                }
        if current is None and state.get("current_work_label"):
            current = {
                "work_id": state.get("current_work_id"),
                "work_type": None,
                "label": state.get("current_work_label"),
                "target": None,
                "stage": None,
                "status": "running" if not paused else "paused",
                "started_at": state.get("started_at"),
            }
        activity_store = (activity if activity is not None
                          else activity_log.default_store())
        try:
            activity_status = activity_store.status()
        except Exception as exc:
            activity_status = {"available": False, "degraded": True,
                               "error": f"{type(exc).__name__}: {exc}",
                               "backend": "unknown"}
        try:
            queue = {"worker_jobs": jobs.counts(conn),
                     "work": store.counts(conn)}
        except Exception:
            queue = {"worker_jobs": {}, "work": {}}
        return {
            "alive": alive,
            "paused": paused,
            "status": ("paused" if paused else str(state.get("status") or
                       "missing")),
            "heartbeat_age": age,
            "heartbeat_at": heartbeat_at,
            "started_at": state.get("started_at"),
            "pid": worker.get("pid"),
            "current_work_id": state.get("current_work_id"),
            "current_work_label": state.get("current_work_label"),
            "current": current,
            "activity": activity_status,
            "queue": queue,
        }
    finally:
        if own:
            conn.close()


# -------------------------------------------------------------- test/dev API
_thread: threading.Thread | None = None
_thread_stop: threading.Event | None = None
_runner: Bworker | None = None


def run_forever(stop_event: threading.Event | None = None) -> None:
    """Legacy-compatible module entry point.

    Older code imported ``iptv_charts.worker.run_forever(stop_event)`` and
    called it directly. Production uses ``bworker.main()`` and the ``Bworker``
    object; this wrapper keeps the old call shape working.
    """
    Bworker(stop=stop_event).run_forever()


def start_background() -> None:
    """Start the bworker in a daemon thread for tests and local dev."""
    global _thread, _thread_stop, _runner
    if _thread is not None and _thread.is_alive():
        return
    _thread_stop = threading.Event()
    _runner = Bworker(stop=_thread_stop)
    _thread = threading.Thread(target=_runner.run_forever,
                               name="iptv-bworker", daemon=True)
    _thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            if jobs.worker_status()["alive"]:
                return
        except Exception:
            pass
        time.sleep(0.05)


def stop_background(timeout: float = 5.0) -> None:
    global _thread, _thread_stop, _runner
    if _thread_stop is not None:
        _thread_stop.set()
    try:
        downloader.shutdown(timeout=1.0)
    except Exception:
        pass
    if _thread is not None:
        _thread.join(timeout=timeout)
    _thread = None
    _thread_stop = None
    _runner = None


def main() -> int:
    """Process entry point used by ``bworker.py`` and the container."""
    from .app import configure_logging

    configure_logging()
    stop = threading.Event()

    def _stop(signum, _frame) -> None:
        logger.info("bworker: received signal %s, stopping after this unit",
                    signum)
        stop.set()
        try:
            downloader.shutdown(timeout=1.0)
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("bworker: starting (pid %s)", os.getpid())
    try:
        Bworker(stop=stop).run_forever()
    except Exception:
        logger.error("bworker: fatal error\n%s", traceback.format_exc())
        return 1
    return 0


# --------------------------------------------------------------- registration
def _register_defaults() -> None:
    for kind, handler in {
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
        "probe_movie_candidate": _probe_movie_candidate,
        "probe_movie_candidates": _probe_movie_candidates,
        "probe_episode_candidate": _probe_episode_candidate,
        "scan_import": _scan_import,
        "scan_import_all": _scan_import_all,
    }.items():
        JOB_HANDLERS.setdefault(kind, handler)
    for work_type, handler in {
        "movie_search": _work_movie_search,
        "series_search": _work_series_search,
        "episode_search": _work_episode_search,
        "download": _work_download,
        "probe": _work_probe,
        "playlist_sync": _work_playlist_sync,
        "metadata_refresh": _work_metadata_refresh,
        "library_scan": _work_library_scan,
        "activity_cleanup": _work_activity_cleanup,
    }.items():
        WORK_HANDLERS.setdefault(work_type, handler)


_register_defaults()


__all__ = [
    "Bworker",
    "HANDLERS",
    "JOB_HANDLERS",
    "WORK_HANDLERS",
    "WorkContext",
    "main",
    "register_job_handler",
    "register_work_handler",
    "run_cleanup",
    "run_forever",
    "start_background",
    "status",
    "stop_background",
]
