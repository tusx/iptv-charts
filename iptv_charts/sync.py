"""The ingest engine: pull a playlist into SQLite, reporting honest progress.

Three properties drive the design.

**Sources sync sequentially, never in parallel.** Xtream panels enforce a
per-account connection limit -- the app displays ``active_connections`` and
``max_connections`` on the info page, which would be self-defeating if we
opened several at once. Parallel syncs would also multiply peak memory and peg
several cores parsing, which is the opposite of what this app is for. So there
is one worker thread draining a queue.

**All state is in-process.** A single ``waitress`` process serves every
request, so a dict behind a lock is visible to every browser tab with no IPC.
Putting progress in SQLite would mean a write per tick -- the opposite of
light -- and would leave meaningless rows behind after a restart. Durable
*results* go to the ``sources`` row, once, at the end.

**Cancellation rolls everything back.** Each source is loaded inside one
transaction, so cancelling leaves the previous catalog exactly as it was
rather than half-replaced.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import db
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


# -------------------------------------------------------------------- manager
class SyncManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._queue: deque[int] = deque()
        self._worker: threading.Thread | None = None
        self._batch_total = 0
        self._batch_done = 0
        self._running: int | None = None

    # ------------------------------------------------------------ scheduling
    def enqueue(self, source_ids: list[int]) -> list[int]:
        """Queue sources for syncing. Returns the ids actually queued.

        A source already queued or running is skipped rather than duplicated,
        so hammering "Update" cannot start two syncs of the same playlist.
        """
        queued: list[int] = []
        with self._lock:
            for source_id in source_ids:
                existing = self._jobs.get(source_id)
                if existing is not None and existing.active:
                    continue
                job = Job(source_id=source_id)
                self._jobs[source_id] = job
                self._queue.append(source_id)
                queued.append(source_id)

            if queued:
                # A fresh batch restarts the overall count.
                if self._running is None:
                    self._batch_total = len(queued)
                    self._batch_done = 0

        if queued:
            self._ensure_worker()
        return queued

    def cancel(self, source_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(source_id)
            if job is None or not job.active:
                return False
            job.cancel.set()
            job.message = "Cancelling…"
            return True

    def cancel_all(self) -> int:
        with self._lock:
            targets = [j for j in self._jobs.values() if j.active]
        for job in targets:
            job.cancel.set()
            job.message = "Cancelling…"
        return len(targets)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            # Daemon so a wedged sync can never prevent the process exiting.
            self._worker = threading.Thread(
                target=self._drain, name="iptv-sync", daemon=True
            )
            self._worker.start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    self._running = None
                    self._batch_total = 0
                    self._batch_done = 0
                    return
                source_id = self._queue.popleft()
                job = self._jobs.get(source_id)
                self._running = source_id

            if job is None:
                continue
            if job.cancel.is_set():
                job.state = "cancelled"
                job.message = "Cancelled"
                with self._lock:
                    self._batch_done += 1
                continue

            self._run_one(source_id, job)

    # -------------------------------------------------------------- one sync
    def _run_one(self, source_id: int, job: Job) -> None:
        job.state = "running"
        job.started_at = time.time()
        job.phase = "connecting"
        job.message = "Connecting"

        conn = None
        try:
            # Its own connection: this is a different thread from the request
            # handlers, and SQLite connections must not be shared across them.
            conn = db.connect()
            source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (source_id,))
            if source is None:
                raise RuntimeError("This playlist no longer exists.")

            job.name = source["name"]

            if source["kind"] == "xtream":
                _sync_xtream(conn, source, job)
            else:
                _sync_m3u(conn, source, job)

            job.state = "done"
            job.phase = ""
            job.detail = ""
            job.message = f"{job.items_done:,} items"
            _record_success(conn, source_id, job)
        except SyncCancelled:
            job.state = "cancelled"
            job.phase = ""
            job.message = "Cancelled — the previous catalog was left untouched"
            _record_failure(conn, source_id, "cancelled", job.message)
        except Exception as exc:  # surfaced to the user, not swallowed
            job.state = "error"
            job.phase = ""
            job.error = str(exc)
            job.message = "Failed"
            _record_failure(conn, source_id, "error", str(exc))
        finally:
            job.finished_at = time.time()
            if conn is not None:
                conn.close()
            with self._lock:
                self._batch_done += 1
                if self._running == source_id:
                    self._running = None

    # ---------------------------------------------------------------- status
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            jobs = {sid: job.as_dict() for sid, job in self._jobs.items()}
            batch_total = self._batch_total
            batch_done = self._batch_done
            running = self._running

        active = [j for j in jobs.values() if j["state"] in ("queued", "running")]

        overall = None
        if active and batch_total:
            current = jobs.get(running) if running is not None else None
            partial = 0.0
            if current and current["state"] == "running":
                partial = current["fraction"] or 0.0
            overall = min(1.0, (batch_done + partial) / batch_total)

        return {
            "jobs": jobs,
            "active": bool(active),
            "running": running,
            "batch_total": batch_total,
            "batch_done": batch_done,
            "overall": overall,
        }


_manager = SyncManager()


def enqueue(source_ids: list[int]) -> list[int]:
    return _manager.enqueue(source_ids)


def enqueue_all() -> list[int]:
    """Queue every configured source. Reads the DB on the caller's thread."""
    conn = db.connect()
    try:
        rows = db.get_all(conn, "SELECT id FROM sources ORDER BY id")
    finally:
        conn.close()
    return _manager.enqueue([row["id"] for row in rows])


def cancel(source_id: int) -> bool:
    return _manager.cancel(source_id)


def cancel_all() -> int:
    return _manager.cancel_all()


def snapshot() -> dict[str, Any]:
    return _manager.snapshot()


def is_busy() -> bool:
    return _manager.snapshot()["active"]


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
        _job.bytes_done = done
        _job.bytes_total = total

    def on_phase(phase: str, _job=job) -> None:
        _job.phase = phase

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
    "snapshot",
]
