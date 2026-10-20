"""Per-playlist connection accounting shared by the web and worker processes.

Providers sell a number of simultaneous connections. Every operation this app
starts against a provider counts against it: a playlist sync, a metadata
endpoint, an ffprobe, a download. The provider cannot be asked to enforce our
own queue, so this module does.

Earlier versions kept the registry in memory because one process owned every
provider operation. That is no longer true: the web server can fetch an episode
list for the browse dialog while the worker is downloading, and both must share
the same slot pool. The enforcement is therefore POSIX file locks, one file per
source and slot, under the app's data directory. ``flock`` is atomic across
processes and released automatically if a process dies, which avoids stale
leases after a crash.

A source's effective limit is:

1. ``sources.connection_limit`` when the user set one,
2. otherwise the provider-reported ``max_connections``,
3. otherwise 1.

Playback started outside the app (Jellyfin, an external player) is not visible
here; the app cannot reserve a slot it does not open.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config, db

logger = logging.getLogger("iptv_charts.connections")

_MAX_LIMIT = 32

#: Local leases, purely so ``release(source_id)`` knows which descriptor to
#: close. Enforcement itself is the kernel lock, not this dict.
_guard = threading.Lock()
_leases: dict[int, list[tuple[int, Path]]] = {}


class ConnectionBusy(RuntimeError):
    """No connection slot became free before the caller's deadline."""


def _lock_dir() -> Path:
    path = config.data_dir() / "locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slot_path(source_id: int, slot: int) -> Path:
    return _lock_dir() / f"source-{int(source_id)}-{int(slot)}.lock"


def _try_slot(source_id: int, slot: int) -> tuple[int, Path] | None:
    """Take one slot lock, or return None when another process owns it."""
    path = _slot_path(source_id, slot)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        logger.warning("connections: cannot open lock %s: %s", path, exc)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return None
    return fd, path


def _drop_slot(lease: tuple[int, Path]) -> None:
    fd, _path = lease
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _count_locked(source_id: int) -> int:
    """Count held slots by probing every possible slot.

    This is used for display and for ``available``. It briefly takes each free
    slot, then releases it; a concurrent acquirer can only see ``busy`` for
    that instant, never a corrupted count.
    """
    locked = 0
    for slot in range(_MAX_LIMIT):
        lease = _try_slot(source_id, slot)
        if lease is None:
            locked += 1
        else:
            _drop_slot(lease)
    return locked


def effective_limit(conn, source_id: int) -> int:
    """Resolve the limit for one source, defaulting to one connection."""
    row = db.get_one(
        conn,
        "SELECT connection_limit, max_connections FROM sources WHERE id = ?",
        (int(source_id),),
    )
    if row is None:
        return 1
    for value in (row["connection_limit"], row["max_connections"]):
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return max(1, min(number, _MAX_LIMIT))
    return 1


def active(source_id: int) -> int:
    return _count_locked(int(source_id))


def snapshot() -> dict[int, int]:
    """Active provider slots per source, as seen across processes.

    A page may render while the worker holds a download slot; the lock probe
    sees it. Only configured sources are included, so old lock files from
    deleted playlists do not appear in the UI.
    """
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id FROM sources").fetchall()
    finally:
        conn.close()
    out: dict[int, int] = {}
    for row in rows:
        count = _count_locked(int(row["id"]))
        if count:
            out[int(row["id"])] = count
    return out


def available(conn, source_id: int) -> bool:
    return _count_locked(int(source_id)) < effective_limit(conn, source_id)


def try_acquire(source_id: int, conn=None, *, limit: int | None = None) -> bool:
    """Take a free slot if one exists. The caller must release it."""
    source_id = int(source_id)
    if limit is None:
        limit = effective_limit(conn, source_id) if conn is not None else 1
    limit = max(1, min(int(limit), _MAX_LIMIT))
    for slot in range(limit):
        lease = _try_slot(source_id, slot)
        if lease is None:
            continue
        with _guard:
            _leases.setdefault(source_id, []).append(lease)
        return True
    return False


def release(source_id: int) -> None:
    source_id = int(source_id)
    with _guard:
        stack = _leases.get(source_id)
        lease = stack.pop() if stack else None
        if stack is not None and not stack:
            _leases.pop(source_id, None)
    if lease is not None:
        _drop_slot(lease)


@contextmanager
def acquire(conn, source_id: int, *, purpose: str = "",
            timeout: float | None = None) -> Iterator[None]:
    """Wait for a connection slot for one source.

    ``timeout`` is in seconds; ``None`` waits indefinitely. A download can
    hold the only slot for a long time, so callers that must stay responsive
    (automatic search, page rendering) pass a short timeout and handle
    ``ConnectionBusy``.
    """
    source_id = int(source_id)
    limit = effective_limit(conn, source_id)
    deadline = time.monotonic() + timeout if timeout is not None else None
    while not try_acquire(source_id, limit=limit):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionBusy(
                    f"The playlist's {limit}-connection limit is in use"
                    + (f" ({purpose})" if purpose else "")
                    + ". Try again when a download finishes."
                )
            time.sleep(min(0.2, max(0.01, remaining)))
        else:
            time.sleep(0.2)
    try:
        yield
    finally:
        release(source_id)


def wait_for_free(conn, source_id: int, *, purpose: str = "",
                  timeout: float | None = None) -> None:
    """Block until a slot is free, without taking it."""
    source_id = int(source_id)
    limit = effective_limit(conn, source_id)
    deadline = time.monotonic() + timeout if timeout is not None else None
    while _count_locked(source_id) >= limit:
        if deadline is not None and time.monotonic() >= deadline:
            raise ConnectionBusy(
                f"The playlist's {limit}-connection limit is in use"
                + (f" ({purpose})" if purpose else "") + "."
            )
        time.sleep(0.2)


__all__ = [
    "ConnectionBusy",
    "acquire",
    "active",
    "available",
    "effective_limit",
    "release",
    "snapshot",
    "try_acquire",
    "wait_for_free",
]
