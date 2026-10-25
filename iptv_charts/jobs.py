"""The durable one-at-a-time work queue shared by the web and worker processes.

Everything slow or provider-facing goes through here: playlist syncs, stream
probes/search runs, downloads, metadata refreshes and library scans. The web
process may insert a row and poll its status, but it never runs the handler.
The worker process claims a single row at a time with ``BEGIN IMMEDIATE`` and
executes it to completion (or schedules a retry), so two provider operations
cannot accidentally run at once.

Rows are intentionally small. Domain-specific progress (download bytes,
sync phases) stays in the existing ``downloads`` / ``sources`` tables; the
``progress_data`` JSON here carries only what the generic worker UI needs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from . import config, db

TERMINAL = ("done", "error", "cancelled")
ACTIVE = ("queued", "running")
_STATUSES = set(ACTIVE + TERMINAL)


class WorkerUnavailable(RuntimeError):
    """The web asked for a synchronous job but no worker is running."""


class JobFailed(RuntimeError):
    """A worker job ended in error. ``error_type`` is the original class name."""

    def __init__(self, message: str, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type or ""


class JobCancelled(RuntimeError):
    """A synchronous job was cancelled before it produced a result."""


class JobTimeout(RuntimeError):
    """The worker did not finish a synchronous job in time."""


def _now() -> int:
    return int(time.time())


def _decode(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _encode(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def normalise(row: Mapping[str, Any] | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _decode(item.get("payload")) or {}
    item["result"] = _decode(item.get("result"))
    item["progress_data"] = _decode(item.get("progress_data")) or {}
    return item


def enqueue(conn, kind: str, payload: Mapping[str, Any] | None = None, *,
            priority: int = 0, dedupe_key: str | None = None,
            available_at: int | None = None) -> dict:
    """Insert a queued job, or return the active job with the same dedupe key.

    A dedupe key is unique only while a job is queued or running. Once it
    finishes, the same download/search can be queued again -- which is what a
    retry needs. This method commits, so callers should do any domain writes
    first; the worker may pick the job up immediately after.
    """
    if dedupe_key:
        existing = conn.execute(
            "SELECT * FROM worker_jobs WHERE dedupe_key = ? "
            "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            item = normalise(existing)
            item["_created"] = False
            return item

    now = _now()
    values = (
        str(kind), _encode(dict(payload or {})) or "{}",
        max(-1000, min(1000, int(priority or 0))),
        int(available_at or now), now, dedupe_key,
    )

    def insert() -> int:
        cursor = conn.execute(
            """
            INSERT INTO worker_jobs
                (kind, payload, status, priority, available_at, created_at, dedupe_key)
            VALUES (?, ?, 'queued', ?, ?, ?, ?)
            """,
            values,
        )
        conn.commit()
        return int(cursor.lastrowid)

    try:
        row_id = insert()
    except sqlite3.IntegrityError as first_error:
        # Another enqueue may have won the race for the dedupe key. Its row is
        # the correct one to observe. If it finished between our SELECT and
        # INSERT, retry the insert once rather than losing the request.
        conn.rollback()
        if not dedupe_key:
            raise
        row_id = None
        for _ in range(3):
            existing = conn.execute(
                "SELECT * FROM worker_jobs WHERE dedupe_key = ? "
                "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                item = normalise(existing)
                item["_created"] = False
                return item
            try:
                row_id = insert()
                break
            except sqlite3.IntegrityError:
                conn.rollback()
                continue
        if row_id is None:
            raise first_error

    item = get(conn, int(row_id))
    assert item is not None
    item["_created"] = True
    return item


def get(conn, job_id: int) -> dict | None:
    return normalise(conn.execute(
        "SELECT * FROM worker_jobs WHERE id = ?", (int(job_id),)
    ).fetchone())


def active_for(conn, kind: str, dedupe_key: str | None = None) -> dict | None:
    if dedupe_key:
        row = conn.execute(
            "SELECT * FROM worker_jobs WHERE kind = ? AND dedupe_key = ? "
            "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
            (str(kind), dedupe_key),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM worker_jobs WHERE kind = ? "
            "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
            (str(kind),),
        ).fetchone()
    return normalise(row)


def claim(conn) -> dict | None:
    """Atomically claim the next due job. Exactly one worker process uses this."""
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM worker_jobs
            WHERE status = 'queued' AND available_at <= ?
            ORDER BY priority DESC, id
            LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        cursor = conn.execute(
            "UPDATE worker_jobs SET status = 'running', started_at = ?, "
            "attempts = attempts + 1, message = 'Running', finished_at = NULL, "
            "error = NULL, error_type = NULL, result = NULL "
            "WHERE id = ? AND status = 'queued'",
            (now, int(row["id"])),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    return get(conn, int(row["id"]))


def update_progress(conn, job_id: int, *, message: str | None = None,
                    progress_data: Mapping[str, Any] | None = None) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if message is not None:
        fields.append("message = ?")
        params.append(message[:500])
    if progress_data is not None:
        fields.append("progress_data = ?")
        params.append(_encode(dict(progress_data)))
    if not fields:
        return
    try:
        conn.execute(
            f"UPDATE worker_jobs SET {', '.join(fields)} WHERE id = ?",
            (*params, int(job_id)),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # A progress tick is best-effort; the sync's bulk transaction may hold
        # the write lock for a moment. The next tick will catch up.
        try:
            conn.rollback()
        except Exception:
            pass


def finish(conn, job_id: int, status: str = "done", *,
           message: str | None = None, result: Any = None,
           error: str | None = None, error_type: str | None = None,
           progress_data: Mapping[str, Any] | None = None) -> None:
    if status not in _STATUSES:
        raise ValueError(f"Unknown job status: {status}")
    fields = [
        "status = ?", "finished_at = ?", "message = ?", "result = ?",
        "error = ?", "error_type = ?",
    ]
    params: list[Any] = [
        status, _now(), (message or "")[:500], _encode(result),
        (error or "")[:4000] if error else None, error_type,
    ]
    if progress_data is not None:
        fields.append("progress_data = ?")
        params.append(_encode(dict(progress_data)))
    if status in ACTIVE:
        raise ValueError("Use reschedule() for an active job.")
    conn.execute(
        f"UPDATE worker_jobs SET {', '.join(fields)} WHERE id = ?",
        (*params, int(job_id)),
    )
    conn.commit()


def reschedule(conn, job_id: int, *, available_at: int,
               message: str | None = None, progress_data: Mapping[str, Any] | None = None) -> None:
    """Return a running job to the queue for a later retry."""
    fields = ["status = 'queued'", "started_at = NULL", "finished_at = NULL",
              "available_at = ?", "error = NULL", "error_type = NULL", "result = NULL"]
    params: list[Any] = [int(available_at)]
    if message is not None:
        fields.append("message = ?")
        params.append(message[:500])
    if progress_data is not None:
        fields.append("progress_data = ?")
        params.append(_encode(dict(progress_data)))
    conn.execute(
        f"UPDATE worker_jobs SET {', '.join(fields)} WHERE id = ?",
        (*params, int(job_id)),
    )
    conn.commit()


def cancel(conn, job_id: int) -> bool:
    """Cancel a queued job immediately, or ask a running handler to stop.

    A queued job is marked terminal right away. That frees its dedupe key so a
    user can press Update again without the cancelled row swallowing the new
    request. A running job sets the flag its handler polls at safe points.
    """
    job_id = int(job_id)
    cursor = conn.execute(
        "UPDATE worker_jobs SET status = 'cancelled', finished_at = ?, "
        "cancel_requested = 1, message = 'Cancelled' "
        "WHERE id = ? AND status = 'queued'",
        (_now(), job_id),
    )
    if cursor.rowcount == 1:
        conn.commit()
        return True
    cursor = conn.execute(
        "UPDATE worker_jobs SET cancel_requested = 1, message = 'Cancelling…' "
        "WHERE id = ? AND status = 'running'",
        (job_id,),
    )
    conn.commit()
    return cursor.rowcount == 1


def cancel_by_key(conn, dedupe_key: str) -> bool:
    row = conn.execute(
        "SELECT id FROM worker_jobs WHERE dedupe_key = ? "
        "AND status IN ('queued', 'running') LIMIT 1",
        (str(dedupe_key),),
    ).fetchone()
    return cancel(conn, int(row["id"])) if row is not None else False


def recover(conn) -> int:
    """Requeue jobs interrupted by a worker crash or restart."""
    cursor = conn.execute(
        "UPDATE worker_jobs SET status = 'queued', started_at = NULL, "
        "finished_at = NULL, message = 'Interrupted; queued again', "
        "available_at = 0 WHERE status = 'running'"
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def recover_running(conn) -> int:
    """Requeue orphaned running rows.

    The worker calls this at the top of its loop, when no handler can be
    executing. It closes the gap left by a failed ``finish`` call: the job no
    longer runs, so it must not stay 'running' forever.
    """
    return recover(conn)


def cleanup(conn, *, older_than_days: int = 7) -> int:
    cutoff = _now() - max(1, int(older_than_days)) * 86400
    cursor = conn.execute(
        "DELETE FROM worker_jobs WHERE status IN ('done', 'error', 'cancelled') "
        "AND finished_at IS NOT NULL AND finished_at < ?",
        (cutoff,),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


# ------------------------------------------------------------------ worker UI
def heartbeat(conn, *, current_job_id: int | None = None,
              status: str = "idle") -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO worker_state
            (id, pid, started_at, heartbeat_at, current_job_id, status)
        VALUES (1, NULL, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            heartbeat_at = excluded.heartbeat_at,
            current_job_id = excluded.current_job_id,
            status = excluded.status
        """,
        (now, now, current_job_id, status),
    )
    conn.commit()


def worker_started(conn, pid: int) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO worker_state
            (id, pid, started_at, heartbeat_at, current_job_id, status)
        VALUES (1, ?, ?, ?, NULL, 'starting')
        ON CONFLICT(id) DO UPDATE SET
            pid = excluded.pid,
            started_at = excluded.started_at,
            heartbeat_at = excluded.heartbeat_at,
            current_job_id = NULL,
            status = 'starting'
        """,
        (int(pid), now, now),
    )
    conn.commit()


def _heartbeat_path() -> Path:
    return config.data_dir() / "worker.heartbeat.json"


def write_heartbeat_file(job_id: int | None, status: str) -> None:
    """A liveness signal that does not need the database write lock.

    A large restore can hold SQLite's writer for longer than the heartbeat
    timeout while the worker is perfectly alive. This small file lets
    /api/health distinguish that from a dead worker.
    """
    path = _heartbeat_path()
    tmp = path.with_suffix(".tmp")
    payload = {"at": time.time(), "current_job_id": job_id, "status": status}
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def _read_heartbeat_file() -> dict | None:
    try:
        with open(_heartbeat_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return None
        return {
            "at": float(payload.get("at") or 0),
            "status": str(payload.get("status") or "unknown"),
            "current_job_id": payload.get("current_job_id"),
        }
    except (OSError, ValueError, TypeError):
        return None


def worker_status(conn=None, *, max_age: int = 30) -> dict:
    own = conn is None
    if own:
        conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
    finally:
        if own:
            conn.close()
    db_at = int(row["heartbeat_at"] or 0) if row is not None else 0
    db_status = str(row["status"]) if row is not None else "missing"
    db_age = max(0, _now() - db_at)
    file_status = _read_heartbeat_file()
    # Use the lock-free file only when the database heartbeat has actually
    # gone stale (normally a large restore holding the writer). Otherwise a
    # stale file from another test/process could override a fresh row.
    if (row is not None and file_status is not None
            and db_age > 5 and file_status["at"] >= db_at):
        age = max(0, int(time.time() - file_status["at"]))
        return {
            "alive": age <= int(max_age) and file_status["status"] != "stopped",
            "status": file_status["status"],
            "heartbeat_age": age,
            "current_job_id": file_status["current_job_id"],
            "pid": row["pid"],
        }
    if row is None:
        return {"alive": False, "status": "missing", "heartbeat_age": None,
                "current_job_id": None, "pid": None}
    age = max(0, _now() - db_at)
    return {
        "alive": age <= int(max_age) and db_status != "stopped",
        "status": db_status,
        "heartbeat_age": age,
        "current_job_id": row["current_job_id"],
        "pid": row["pid"],
    }


# ------------------------------------------------------------ sync request
def submit_and_wait(kind: str, payload: Mapping[str, Any] | None = None, *,
                    priority: int = 50, dedupe_key: str | None = None,
                    timeout: float = 300.0,
                    require_worker: bool = True) -> Any:
    """Enqueue a job and wait for its result.

    This is used by interactive endpoints (manual search, release lists and
    grabs). The HTTP request thread only polls SQLite; the worker process is
    still the one running ffprobe and provider calls. Waiting preserves the
    existing synchronous API semantics without moving any heavy work into the
    web server.
    """
    conn = db.connect()
    try:
        job = enqueue(conn, kind, payload, priority=priority, dedupe_key=dedupe_key)
        job_id = int(job["id"])
    finally:
        conn.close()

    # Enqueue before checking liveness: if the worker is down, the user's
    # request is not lost -- it runs when the worker starts. The HTTP caller
    # still gets a clear 503 now instead of waiting for a timeout.
    if require_worker:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = worker_status()
            if status["alive"]:
                break
            time.sleep(0.1)
        else:
            raise WorkerUnavailable(
                "The background worker is not running. The job is queued and "
                "will start when the worker does."
            )

    deadline = time.monotonic() + max(1.0, float(timeout))
    next_liveness = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        conn = db.connect()
        try:
            row = get(conn, job_id)
        finally:
            conn.close()
        if row is None:
            raise JobFailed("The queued job disappeared.")
        if row["status"] == "done":
            return row.get("result")
        if row["status"] == "error":
            raise JobFailed(row.get("error") or "The background job failed.",
                            row.get("error_type"))
        if row["status"] == "cancelled":
            raise JobCancelled(row.get("message") or "The background job was cancelled.")
        if time.monotonic() >= next_liveness:
            status = worker_status()
            if not status["alive"]:
                raise WorkerUnavailable(
                    "The background worker stopped while this job was queued. "
                    "Start it again; the job will resume."
                )
            next_liveness = time.monotonic() + 2.0
        time.sleep(0.05)

    raise JobTimeout(
        "The background worker is taking longer than expected. Check Activity "
        "or the app log; the job is still queued or running."
    )


def snapshot(conn, limit: int = 30) -> list[dict]:
    """Active jobs first, then recent terminal history.

    A series search can queue dozens of downloads; ordering purely by id
    would push the one job actually running out of the Activity list.
    """
    limit = max(1, int(limit))
    active = conn.execute(
        "SELECT * FROM worker_jobs WHERE status IN ('queued', 'running') "
        "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    remaining = max(0, limit - len(active))
    recent = conn.execute(
        "SELECT * FROM worker_jobs WHERE status IN ('done', 'error', 'cancelled') "
        "ORDER BY id DESC LIMIT ?",
        (remaining,),
    ).fetchall() if remaining else []
    return [normalise(row) for row in [*active, *recent]]


def active_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM worker_jobs WHERE status IN ('queued', 'running')"
    ).fetchone()
    return int(row["n"] or 0)


def counts(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM worker_jobs GROUP BY status"
    ).fetchall()
    out = {status: 0 for status in ("queued", "running", "done", "error", "cancelled")}
    for row in rows:
        out[str(row["status"])] = int(row["n"])
    return out


def has_due(conn, *, now: int | None = None) -> bool:
    """True when a queued job is due right now (used for bworker timing)."""
    at = int(now if now is not None else _now())
    row = conn.execute(
        "SELECT 1 FROM worker_jobs "
        "WHERE status = 'queued' AND available_at <= ? LIMIT 1",
        (at,),
    ).fetchone()
    return row is not None


def next_due_at(conn, *, now: int | None = None) -> int | None:
    """Timestamp of the next queued job, or None when the queue is empty."""
    at = int(now if now is not None else _now())
    row = conn.execute(
        "SELECT MIN(available_at) AS available_at FROM worker_jobs "
        "WHERE status = 'queued'"
    ).fetchone()
    if row is None or row["available_at"] is None:
        return None
    return max(at, int(row["available_at"]))


__all__ = [
    "ACTIVE",
    "JobCancelled",
    "JobFailed",
    "JobTimeout",
    "TERMINAL",
    "WorkerUnavailable",
    "active_count",
    "active_for",
    "cancel",
    "cancel_by_key",
    "claim",
    "cleanup",
    "counts",
    "enqueue",
    "finish",
    "get",
    "has_due",
    "heartbeat",
    "next_due_at",
    "normalise",
    "recover",
    "recover_running",
    "reschedule",
    "snapshot",
    "submit_and_wait",
    "update_progress",
    "worker_started",
    "worker_status",
    "write_heartbeat_file",
]
