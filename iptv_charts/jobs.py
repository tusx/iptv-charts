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
import sqlite3
import time
from typing import Any, Mapping

from . import db

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
    try:
        cursor = conn.execute(
            """
            INSERT INTO worker_jobs
                (kind, payload, status, priority, available_at, created_at, dedupe_key)
            VALUES (?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                str(kind), _encode(dict(payload or {})) or "{}",
                max(-1000, min(1000, int(priority or 0))),
                int(available_at or now), now, dedupe_key,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Another enqueue won the race for the dedupe key. Its row is the
        # correct one to observe.
        conn.rollback()
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
        raise

    item = get(conn, int(cursor.lastrowid))
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
    """Ask a running job to stop, or mark a queued one for cancellation.

    Handlers poll ``cancel_requested`` at safe points; a queued job is claimed
    normally and its handler sees the flag before doing any work.
    """
    cursor = conn.execute(
        "UPDATE worker_jobs SET cancel_requested = 1, message = 'Cancelling…' "
        "WHERE id = ? AND status IN ('queued', 'running')",
        (int(job_id),),
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


def worker_status(conn=None, *, max_age: int = 20) -> dict:
    own = conn is None
    if own:
        conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
    finally:
        if own:
            conn.close()
    if row is None:
        return {"alive": False, "status": "missing", "heartbeat_age": None,
                "current_job_id": None, "pid": None}
    age = max(0, _now() - int(row["heartbeat_at"] or 0))
    return {
        "alive": age <= int(max_age) and row["status"] != "stopped",
        "status": row["status"],
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
    if require_worker:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = worker_status()
            if status["alive"]:
                break
            time.sleep(0.1)
        else:
            raise WorkerUnavailable(
                "The background worker is not running. Start it with the web "
                "app (the container does this automatically)."
            )

    conn = db.connect()
    try:
        job = enqueue(conn, kind, payload, priority=priority, dedupe_key=dedupe_key)
        job_id = int(job["id"])
    finally:
        conn.close()

    deadline = time.monotonic() + max(1.0, float(timeout))
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
        time.sleep(0.05)

    raise JobTimeout(
        "The background worker is taking longer than expected. Check Activity "
        "or the app log; the job is still queued or running."
    )


def snapshot(conn, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM worker_jobs ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
    ).fetchall()
    return [normalise(row) for row in rows]


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
    "heartbeat",
    "normalise",
    "recover",
    "reschedule",
    "snapshot",
    "submit_and_wait",
    "update_progress",
    "worker_started",
    "worker_status",
]
