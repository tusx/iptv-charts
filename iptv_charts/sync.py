"""The ingest engine: pull a playlist into SQLite, reporting honest progress.

**One sync at a time, owned by the worker.** The web process only inserts a
``worker_jobs`` row. The background worker executes ``run_job`` for one source,
with the same single-at-a-time guarantee as every other background task. This
keeps memory flat and never opens two provider streams in parallel.

**Progress is durable.** A small reporter thread copies the in-memory ``Job``
fields into ``worker_jobs.progress_data`` and the ``sources`` row several times
a second. The web UI polls those rows, so progress survives a page reload -- and
is visible from a different process, which is the whole point of the split.

**Cancellation is cooperative.** The flag lives in SQLite because the button is
pressed in the web process. A cancelled first sync removes its partial catalog
so it cannot masquerade as complete; a cancelled re-sync of an existing source
is re-runnable and may leave the new rows in place until the next successful
sync. It never touches another source.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import connections, db, jobs
from .clients import m3u, xtream
from .errors import SyncCancelled

#: Rows per INSERT. Large enough that statement reuse dominates, small enough
#: that the pending list stays a few megabytes rather than tens.
BATCH_SIZE = 5000

KINDS = ("live", "vod", "series")

_ITEM_COLUMNS = (
    "(source_id, kind, remote_id, num, name, category_id, logo, epg_channel_id,"
    " container_ext, added_ts, tv_archive, rating, plot, genre, duration,"
    " cast_list, director, release_date, youtube_trailer, tmdb_id,"
    " request_headers)"
)
_ITEM_PLACEHOLDERS = "(" + ",".join("?" * 21) + ")"


# ----------------------------------------------------------------------- jobs
@dataclass
class Job:
    """Progress for one source's sync."""

    source_id: int
    name: str = ""
    state: str = "queued"          # queued | running | done | error | cancelled
    phase: str = ""                # connecting | account | download | parse | indexing
    detail: str = ""
    bytes_done: int = 0
    bytes_total: int | None = None
    items_done: int = 0
    message: str = ""
    error: str | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    interrupted: bool = False
    #: Whether the source had a catalog before this run. Used to remove the
    #: half-written rows if a first sync is cancelled (there is no previous
    #: catalog to preserve).
    had_catalog: bool = False
    job_id: int | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at) if self.started_at else 0.0

    @property
    def fraction(self) -> float | None:
        """Progress within the current phase, or None when indeterminate.

        None matters: several sources never send a Content-Length (PHP panels
        commonly stream chunked), and inventing a percentage for those would
        be a lie the user could see through.
        """
        if self.bytes_total:
            return min(1.0, self.bytes_done / self.bytes_total)
        return None

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "state": self.state,
            "phase": self.phase,
            "detail": self.detail,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "items_done": self.items_done,
            "message": self.message,
            "error": self.error,
            "elapsed": round(self.elapsed, 1),
            "fraction": self.fraction,
        }


# -------------------------------------------------------------------- queue
_SYNC_PRIORITY = 20


def _source_id_for_job(job: dict) -> int | None:
    try:
        return int((job.get("payload") or {}).get("source_id"))
    except (TypeError, ValueError):
        return None


def enqueue(source_ids: list[int]) -> list[int]:
    """Queue sources for the background worker. Returns the ids newly queued.

    A source already queued or running is skipped rather than duplicated, so
    hammering Update cannot start two syncs of the same playlist. The queue row
    is the only state; there is no in-process manager to lose on a restart.
    """
    queued: list[int] = []
    conn = db.connect()
    try:
        for source_id in source_ids:
            source_id = int(source_id)
            if db.get_one(conn, "SELECT id FROM sources WHERE id = ?", (source_id,)) is None:
                continue
            job = jobs.enqueue(
                conn, "sync", {"source_id": source_id},
                priority=_SYNC_PRIORITY, dedupe_key=f"sync:{source_id}",
            )
            if job.get("_created"):
                queued.append(source_id)
                conn.execute(
                    "UPDATE sources SET sync_status = 'queued', sync_message = 'Queued' "
                    "WHERE id = ?",
                    (source_id,),
                )
        conn.commit()
    finally:
        conn.close()
    return queued


def enqueue_all() -> list[int]:
    """Queue every configured source."""
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id FROM sources ORDER BY id").fetchall()
    finally:
        conn.close()
    return enqueue([int(row["id"]) for row in rows])


def cancel(source_id: int) -> bool:
    conn = db.connect()
    try:
        job = jobs.active_for(conn, "sync", f"sync:{int(source_id)}")
        if job is None:
            return False
        if not jobs.cancel(conn, int(job["id"])):
            return False
        conn.execute(
            "UPDATE sources SET sync_message = 'Cancelling…' WHERE id = ?",
            (int(source_id),),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def cancel_all() -> int:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM worker_jobs WHERE kind = 'sync' "
            "AND status IN ('queued', 'running')"
        ).fetchall()
        cancelled = 0
        for row in rows:
            if jobs.cancel(conn, int(row["id"])):
                cancelled += 1
                source_id = _source_id_for_job(jobs.normalise(row) or {})
                if source_id is not None:
                    conn.execute(
                        "UPDATE sources SET sync_message = 'Cancelling…' WHERE id = ?",
                        (source_id,),
                    )
        conn.commit()
        return cancelled
    finally:
        conn.close()


def _persist_now(job: Job) -> None:
    """Write a phase transition immediately, not on the reporter's next tick.

    Parsing a small playlist can finish inside one reporter interval. A
    phase boundary is exactly the sample the UI must not miss, so it is worth
    one synchronous small write.
    """
    if job.job_id is None:
        return
    conn = None
    try:
        conn = db.connect()
        jobs.update_progress(
            conn, int(job.job_id), message=job.message or "Syncing",
            progress_data=_progress_data(job),
        )
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()


def _progress_data(job: Job) -> dict[str, Any]:
    return {
        "phase": job.phase,
        "detail": job.detail,
        "bytes_done": int(job.bytes_done),
        "bytes_total": int(job.bytes_total) if job.bytes_total is not None else None,
        "items_done": int(job.items_done),
        "fraction": job.fraction,
    }


def _job_view(job: dict, name: str = "") -> dict[str, Any]:
    status = str(job.get("status") or "queued")
    state = status if status in ("queued", "running") else {
        "done": "done", "error": "error", "cancelled": "cancelled",
    }.get(status, "error")
    data = job.get("progress_data") or {}
    started = job.get("started_at")
    finished = job.get("finished_at")
    end = finished or time.time()
    elapsed = max(0.0, float(end) - float(started)) if started else 0.0
    fraction = data.get("fraction")
    if state == "done":
        fraction = 1.0
    return {
        "source_id": _source_id_for_job(job),
        "name": name,
        "state": state,
        "phase": data.get("phase") or "",
        "detail": data.get("detail") or "",
        "bytes_done": int(data.get("bytes_done") or 0),
        "bytes_total": data.get("bytes_total"),
        "items_done": int(data.get("items_done") or 0),
        "message": job.get("message") or "",
        "error": job.get("error") or None,
        "elapsed": round(elapsed, 1),
        "fraction": fraction,
    }


def snapshot() -> dict[str, Any]:
    """Latest sync job per source, shaped for the existing page templates."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM worker_jobs WHERE kind = 'sync' ORDER BY id DESC LIMIT 500"
        ).fetchall()
        sources = {
            int(row["id"]): str(row["name"])
            for row in conn.execute("SELECT id, name FROM sources")
        }
    finally:
        conn.close()

    latest: dict[int, dict] = {}
    for row in rows:
        job = jobs.normalise(row) or {}
        source_id = _source_id_for_job(job)
        if source_id is None or source_id in latest:
            continue
        latest[source_id] = job

    out = {
        source_id: _job_view(job, sources.get(source_id, ""))
        for source_id, job in latest.items()
    }
    active = [job for job in out.values() if job["state"] in ("queued", "running")]
    running = next(
        (job["source_id"] for job in out.values() if job["state"] == "running"),
        None,
    )
    return {
        "jobs": out,
        "active": bool(active),
        "running": running,
        "batch_total": len(active),
        "batch_done": 0,
        "overall": None,
    }


def is_busy() -> bool:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM worker_jobs WHERE kind = 'sync' "
            "AND status IN ('queued', 'running') LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# --------------------------------------------------------------------- runner
def run_job(source_id: int, job_id: int, *, stop_event: threading.Event | None = None) -> dict:
    """Execute one sync job. Called only by the background worker.

    Returns an outcome for the worker runner: ``status`` is the job's final
    status, ``retry`` asks the runner to put the row back in the queue, and
    ``data`` is the final progress payload for the UI.
    """
    source_id = int(source_id)
    job_id = int(job_id)
    job = Job(source_id=source_id, job_id=job_id)
    job.state = "running"
    job.started_at = time.time()
    job.phase = "connecting"
    job.message = "Connecting"
    reporter_stop = threading.Event()
    reporter: threading.Thread | None = None
    conn = None

    def stop_reporter() -> None:
        nonlocal reporter
        reporter_stop.set()
        if reporter is not None:
            # Longer than SQLite's busy timeout: a tick blocked on the write
            # lock must finish before the final source state is written.
            reporter.join(timeout=15.0)
            reporter = None

    try:
        conn = db.connect()
        source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (source_id,))
        if source is None:
            raise RuntimeError("This playlist no longer exists.")
        job.name = source["name"]
        job.had_catalog = db.get_one(
            conn, "SELECT 1 FROM items WHERE source_id = ? LIMIT 1", (source_id,)
        ) is not None

        # A cancel can arrive while the job waits in the queue. Do not open a
        # provider connection just to discover that.
        row = db.get_one(
            conn, "SELECT cancel_requested FROM worker_jobs WHERE id = ?", (job_id,)
        )
        if row and row["cancel_requested"]:
            job.state = "cancelled"
            job.message = "Cancelled"
            _record_failure(conn, source_id, "cancelled", job.message)
            return {
                "status": "cancelled", "message": job.message,
                "data": _progress_data(job),
            }

        def report() -> None:
            rconn = None
            try:
                rconn = db.connect()
                while not reporter_stop.wait(0.15):
                    try:
                        current = rconn.execute(
                            "SELECT cancel_requested FROM worker_jobs WHERE id = ?",
                            (job_id,),
                        ).fetchone()
                        if current is not None and current["cancel_requested"]:
                            job.cancel.set()
                        if stop_event is not None and stop_event.is_set():
                            job.interrupted = True
                            job.cancel.set()
                        data = _progress_data(job)
                        jobs.update_progress(
                            rconn, job_id,
                            message=job.message or "Syncing",
                            progress_data=data,
                        )
                        rconn.execute(
                            "UPDATE sources SET sync_status = 'syncing', sync_message = ? "
                            "WHERE id = ?",
                            ((job.message or "Syncing")[:500], source_id),
                        )
                        rconn.commit()
                    except Exception:
                        # A progress tick must never break the sync. The bulk
                        # transaction can briefly hold the write lock.
                        try:
                            rconn.rollback()
                        except Exception:
                            pass
            finally:
                if rconn is not None:
                    rconn.close()

        reporter = threading.Thread(
            target=report, name=f"iptv-sync-progress-{source_id}", daemon=True
        )
        reporter.start()
        conn.execute(
            "UPDATE sources SET sync_status = 'syncing', sync_message = 'Connecting' "
            "WHERE id = ?",
            (source_id,),
        )
        conn.commit()

        with connections.acquire(
            conn, source_id, purpose="Playlist sync", timeout=3600.0
        ):
            if source["kind"] == "xtream":
                _sync_xtream(conn, source, job)
            else:
                _sync_m3u(conn, source, job)

        # Stop the reporter before writing the final source state; otherwise
        # a tick racing this branch could overwrite 'ok' with 'syncing'.
        stop_reporter()
        job.state = "done"
        job.phase = ""
        job.detail = ""
        job.message = f"{job.items_done:,} items"
        _record_success(conn, source_id, job)
        return {
            "status": "done",
            "message": job.message,
            "result": {"items_done": int(job.items_done)},
            "data": _progress_data(job),
        }
    except SyncCancelled:
        stop_reporter()
        job.phase = ""
        if job.interrupted:
            job.state = "queued"
            job.message = "Interrupted by shutdown; it will resume."
            _record_failure(conn, source_id, "queued", job.message)
            return {
                "status": "queued", "retry": True, "message": job.message,
                "data": _progress_data(job),
            }
        job.state = "cancelled"
        job.message = "Cancelled"
        _record_failure(conn, source_id, "cancelled", job.message)
        if not job.had_catalog:
            _discard_partial_catalog(conn, source_id)
        return {
            "status": "cancelled", "message": job.message,
            "data": _progress_data(job),
        }
    except Exception as exc:  # surfaced to the user, not swallowed
        stop_reporter()
        job.state = "error"
        job.phase = ""
        job.error = str(exc)
        job.message = "Failed"
        _record_failure(conn, source_id, "error", str(exc))
        if not job.had_catalog:
            _discard_partial_catalog(conn, source_id)
        return {
            "status": "error", "message": "Failed", "error": str(exc),
            "data": _progress_data(job),
        }
    finally:
        job.finished_at = time.time()
        stop_reporter()
        if conn is not None:
            conn.close()


# ------------------------------------------------------------------ xtream
def _sync_xtream(conn, source, job: Job) -> None:
    source_id = source["id"]
    try:
        headers = json.loads(source["request_headers"] or "{}")
    except (TypeError, ValueError):
        headers = {}
    client = xtream.XtreamClient(
        source["base_url"], source["username"], source["password"],
        user_agent=headers.get("user_agent"),
        referrer=headers.get("referrer"),
        headers=headers.get("headers"),
    )

    try:
        job.phase = "account"
        job.detail = ""
        job.message = "Signing in"
        info = client.account_info()
        _store_account(conn, source_id, info)
        conn.commit()

        _raise_if_cancelled(job)

        with db.bulk_load(conn):
            conn.execute("DELETE FROM items WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM categories WHERE source_id = ?", (source_id,))
            conn.commit()

            for kind in KINDS:
                _raise_if_cancelled(job)

                job.phase = "categories"
                job.detail = f"{kind} categories"
                conn.commit()

                categories = client.categories(kind)
                category_ids = _insert_categories(conn, source_id, kind, categories)
                conn.commit()

                job.bytes_done = 0
                job.bytes_total = None
                job.detail = f"{kind} streams"

                def on_progress(done: int, total: int | None, _job=job) -> None:
                    _job.bytes_done = done
                    _job.bytes_total = total

                def on_phase(phase: str, _job=job) -> None:
                    # Do not write from here: this callback runs inside the
                    # bulk-load transaction, and a second connection would
                    # wait on the write lock. The reporter thread already
                    # copies the phase on its next tick.
                    _job.phase = phase

                batch: list[tuple] = []
                for raw in client.streams(
                    kind,
                    on_progress=on_progress,
                    on_phase=on_phase,
                    cancel=job.cancel,
                ):
                    item = xtream.normalise_stream(kind, raw)
                    if not item["remote_id"] or not item["name"]:
                        continue  # unusable row; skip rather than fail the sync

                    batch.append(_item_row(
                        source_id, kind, item,
                        category_ids.get(item["category_remote_id"] or ""),
                    ))
                    if len(batch) >= BATCH_SIZE:
                        job.items_done += _flush(conn, batch)

                if batch:
                    job.items_done += _flush(conn, batch)

            job.phase = "indexing"
            job.detail = "Building search index"
            conn.execute(
                "UPDATE categories SET item_count = ("
                "  SELECT COUNT(*) FROM items WHERE items.category_id = categories.id"
                ") WHERE source_id = ?",
                (source_id,),
            )
            conn.commit()
    finally:
        client.close()


def _store_account(conn, source_id: int, info: xtream.AccountInfo) -> None:
    conn.execute(
        """
        UPDATE sources SET
            user_info_json = ?, server_info_json = ?,
            account_status = ?, is_trial = ?, exp_date = ?, exp_unknown = ?,
            active_connections = ?, max_connections = ?, allowed_formats = ?,
            server_timezone = ?, server_protocol = ?, server_host = ?, server_port = ?
        WHERE id = ?
        """,
        (
            json.dumps(info.raw_user_info), json.dumps(info.raw_server_info),
            info.status, 1 if info.is_trial else 0, info.exp_date,
            1 if info.exp_unknown else 0,
            info.active_connections, info.max_connections,
            json.dumps(info.allowed_formats),
            info.timezone, info.server_protocol, info.server_host,
            info.https_port if info.server_protocol == "https" else info.server_port,
            source_id,
        ),
    )


def _insert_categories(conn, source_id: int, kind: str, categories: list[dict]) -> dict[str, int]:
    """Insert categories and return a remote_id -> local id map.

    Missing remote ids become "" rather than NULL: SQLite treats NULLs as
    distinct in a UNIQUE constraint, so leaving them NULL would let the same
    unnamed category be inserted repeatedly.
    """
    for category in categories:
        conn.execute(
            "INSERT INTO categories (source_id, kind, remote_id, name) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source_id, kind, remote_id) DO UPDATE SET name = excluded.name",
            (source_id, kind, category["remote_id"] or "", category["name"]),
        )

    rows = conn.execute(
        "SELECT id, remote_id FROM categories WHERE source_id = ? AND kind = ?",
        (source_id, kind),
    ).fetchall()
    return {row["remote_id"]: row["id"] for row in rows}


# --------------------------------------------------------------------- m3u
def _sync_m3u(conn, source, job: Job) -> None:
    source_id = source["id"]
    url = source["m3u_url"]

    try:
        extra_headers = json.loads(source["request_headers"] or "{}")
    except ValueError:
        extra_headers = {}

    job.phase = "connecting"
    job.message = "Fetching playlist"

    def on_progress(done: int, total: int | None, _job=job) -> None:
        if _job.phase == "parse" and _job.bytes_total:
            # Parsing re-reads the spooled file, so its byte counter starts at
            # zero again. Keep the bar full rather than making the UI look like
            # the download restarted; the phase label says what is happening.
            done = max(done, int(_job.bytes_done))
            total = _job.bytes_total
        _job.bytes_done = done
        _job.bytes_total = total

    def on_phase(phase: str, _job=job) -> None:
        if phase == "parse" and _job.bytes_total:
            # Persist the completed download before switching phase, then show
            # the parse phase at 100%; this makes both transitions observable
            # even for a playlist that parses in a few milliseconds.
            _job.bytes_done = int(_job.bytes_total)
            _persist_now(_job)
        _job.phase = phase
        _persist_now(_job)

    lines = m3u.fetch_lines(
        url,
        user_agent=extra_headers.get("user_agent"),
        referrer=extra_headers.get("referrer"),
        headers=extra_headers.get("headers"),
        on_progress=on_progress,
        on_phase=on_phase,
        cancel=job.cancel,
    )

    # Peek before committing to a parse: an HLS media playlist also starts
    # with #EXTM3U but is a single stream's segment list, not a channel list.
    head: list[str] = []
    for line in itertools.islice(lines, 25):
        head.append(line)
    if not any(line.strip().startswith("#EXTM3U") for line in head):
        raise m3u.M3UError(
            "This does not look like an M3U playlist — no #EXTM3U header was found."
        )
    if m3u.looks_like_hls("\n".join(head)):
        raise m3u.M3UError(
            "This is an HLS media playlist for a single stream, not a channel "
            "list. Point the app at a provider playlist URL instead."
        )

    stream = itertools.chain(head, lines)

    job.phase = "parse"
    job.detail = "Reading channels"
    _persist_now(job)

    with db.bulk_load(conn):
        conn.execute("DELETE FROM items WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM categories WHERE source_id = ?", (source_id,))
        conn.commit()

        category_ids: dict[tuple[str, str], int] = {}
        batch: list[tuple] = []

        for channel in m3u.parse_lines(stream):
            _raise_if_cancelled(job)
            if not channel.url:
                continue

            kind = m3u.classify_kind(channel.url, channel.radio)
            # group-title is a semicolon-separated list; the first entry is
            # the primary group and the rest are treated as tags we drop.
            group = (channel.group or "").split(";")[0].strip()
            category_id = _category_for(
                conn, source_id, kind, group, category_ids
            )

            batch.append(_item_row(
                source_id,
                kind,
                {
                    "remote_id": channel.url,
                    "name": channel.name,
                    "num": None,
                    "logo": channel.logo,
                    "epg_channel_id": channel.tvg_id,
                    # The extension is used for download filenames; M3U has
                    # no separate container field, so read it from the URL.
                    "container_ext": _m3u_extension(channel.url) if kind == "vod" else None,
                    "added_ts": None,
                    "tv_archive": None,
                    "category_remote_id": None,
                    "request_headers": _m3u_headers(channel),
                },
                category_id,
            ))

            if len(batch) >= BATCH_SIZE:
                job.items_done += _flush(conn, batch)

        if batch:
            job.items_done += _flush(conn, batch)

        job.phase = "indexing"
        job.detail = "Building search index"
        conn.execute(
            "UPDATE categories SET item_count = ("
            "  SELECT COUNT(*) FROM items WHERE items.category_id = categories.id"
            ") WHERE source_id = ?",
            (source_id,),
        )
        conn.commit()


def _category_for(conn, source_id: int, kind: str, group: str,
                  cache: dict[tuple[str, str], int]) -> int:
    """Get or create a category for an M3U group, creating at most one each."""
    key = (kind, group)
    cached = cache.get(key)
    if cached is not None:
        return cached

    cursor = conn.execute(
        "INSERT INTO categories (source_id, kind, remote_id, name) VALUES (?, ?, ?, ?)",
        (source_id, kind, group.lower(), group or "Ungrouped"),
    )
    cache[key] = cursor.lastrowid
    return cursor.lastrowid


# ------------------------------------------------------------------ helpers
def _m3u_extension(url: str) -> str | None:
    """The container extension an M3U VOD URL implies, when it has one."""
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    if suffix.isalnum() and 0 < len(suffix) <= 8:
        return suffix
    return None


def _m3u_headers(channel) -> str | None:
    """Preserve per-stream HTTP requirements instead of silently dropping them."""
    headers: dict[str, Any] = {}
    if channel.user_agent:
        headers["user_agent"] = channel.user_agent
    if channel.referrer:
        headers["referrer"] = channel.referrer
    if channel.headers:
        headers["headers"] = dict(channel.headers)
    return json.dumps(headers) if headers else None


def _item_row(source_id: int, kind: str, item: dict, category_id: int | None) -> tuple:
    return (
        source_id, kind, item["remote_id"], item.get("num"), item["name"],
        category_id, item.get("logo"), item.get("epg_channel_id"),
        item.get("container_ext"), item.get("added_ts"), item.get("tv_archive"),
        item.get("rating"), item.get("plot"), item.get("genre"),
        item.get("duration"), item.get("cast_list"), item.get("director"),
        item.get("release_date"), item.get("youtube_trailer"), item.get("tmdb_id"),
        item.get("request_headers"),
    )


def _flush(conn, batch: list[tuple]) -> int:
    """Write a batch, clear it, and return how many rows it held.

    Returning the count matters: clearing the batch is what keeps peak memory
    bounded, but it also means a caller cannot ask `len(batch)` afterwards --
    it would always be zero, and the reported item count would silently come
    up short by the final batch.
    """
    count = len(batch)
    conn.executemany(
        f"INSERT INTO items {_ITEM_COLUMNS} VALUES {_ITEM_PLACEHOLDERS}", batch
    )
    conn.commit()
    batch.clear()
    return count


def _raise_if_cancelled(job: Job) -> None:
    if job.cancel.is_set():
        raise SyncCancelled()


def _record_success(conn, source_id: int, job: Job) -> None:
    try:
        conn.execute(
            "UPDATE sources SET last_synced_at = ?, last_sync_seconds = ?, "
            "sync_status = 'ok', sync_message = NULL WHERE id = ?",
            (int(time.time()), round(job.elapsed, 1), source_id),
        )
        conn.commit()
    except Exception:
        pass  # bookkeeping must never turn a good sync into a failure


def _discard_partial_catalog(conn, source_id: int) -> None:
    """Remove rows from a first sync that was cancelled mid-import.

    An *existing* catalog is deliberately not touched here: the sync's bulk
    loader replaces rows in place, and recreating the old snapshot would be
    more expensive than the next retry. For a new source, though, leaving a
    truncated catalog makes the playlist look complete when it is not; remove
    it so the next sync starts clean.
    """
    try:
        conn.rollback()
        conn.execute("DELETE FROM items WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM categories WHERE source_id = ?", (source_id,))
        conn.commit()
    except Exception:
        pass


def _record_failure(conn, source_id: int, status: str, message: str) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
        conn.execute(
            "UPDATE sources SET sync_status = ?, sync_message = ? WHERE id = ?",
            (status, message[:500], source_id),
        )
        conn.commit()
    except Exception:
        pass


__all__ = [
    "BATCH_SIZE",
    "Job",
    "cancel",
    "cancel_all",
    "enqueue",
    "enqueue_all",
    "is_busy",
    "run_job",
    "snapshot",
]
