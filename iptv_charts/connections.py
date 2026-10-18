"""Per-playlist connection accounting.

Providers sell a number of simultaneous connections. Every operation this app
starts against a provider counts against it: a playlist sync, a metadata
endpoint, an ffprobe, a download. The provider cannot be asked to enforce our
own queue, so a single in-process registry does it.

A source's effective limit is:

1. ``sources.connection_limit`` when the user set one,
2. otherwise the provider-reported ``max_connections``,
3. otherwise 1.

The registry is in-memory. That is correct for this single-container design:
one process owns every provider operation. Playback started outside the app
(Jellyfin, an external player) is not visible here; the app cannot reserve a
slot it does not open.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from . import db

logger = logging.getLogger("iptv_charts.connections")

_MAX_LIMIT = 32

_lock = threading.Condition()
_active: dict[int, int] = {}
#: Callers blocked in ``acquire``. Downloads use ``try_acquire`` and stand
#: aside when someone is waiting, so a queued sync or probe cannot be starved
#: by a busy download queue.
_waiting: dict[int, int] = {}


class ConnectionBusy(RuntimeError):
    """No connection slot became free before the caller's deadline."""


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
    with _lock:
        return int(_active.get(int(source_id), 0))


def snapshot() -> dict[int, int]:
    with _lock:
        return dict(_active)


def available(conn, source_id: int) -> bool:
    limit = effective_limit(conn, source_id)
    with _lock:
        return _active.get(int(source_id), 0) < limit


def try_acquire(source_id: int, conn=None, *, limit: int | None = None) -> bool:
    """Take a slot if one is free. The caller must release it.

    Does not jump ahead of a caller blocked in ``acquire``: the download
    workers use this, and without the check a continuous queue could keep a
    playlist sync waiting forever. Pass ``limit`` when the caller already
    resolved it, to avoid a source lookup per queue row.
    """
    source_id = int(source_id)
    if limit is None:
        limit = effective_limit(conn, source_id) if conn is not None else 1
    with _lock:
        if _waiting.get(source_id, 0):
            return False
        current = _active.get(source_id, 0)
        if current >= limit:
            return False
        _active[source_id] = current + 1
        return True


def release(source_id: int) -> None:
    source_id = int(source_id)
    with _lock:
        current = _active.get(source_id, 0)
        if current <= 1:
            _active.pop(source_id, None)
        else:
            _active[source_id] = current - 1
        _lock.notify_all()


@contextmanager
def acquire(conn, source_id: int, *, purpose: str = "", timeout: float | None = None) -> Iterator[None]:
    """Wait for a connection slot for one source.

    ``timeout`` is in seconds; ``None`` waits indefinitely. A download can
    hold the only slot for a long time, so callers that must stay responsive
    (automatic search, page rendering) pass a short timeout and handle
    ``ConnectionBusy``.
    """
    source_id = int(source_id)
    limit = effective_limit(conn, source_id)
    deadline = time.monotonic() + timeout if timeout is not None else None
    with _lock:
        _waiting[source_id] = _waiting.get(source_id, 0) + 1
        try:
            while _active.get(source_id, 0) >= limit:
                if deadline is None:
                    _lock.wait(0.5)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConnectionBusy(
                        f"The playlist's {limit}-connection limit is in use"
                        + (f" ({purpose})" if purpose else "")
                        + ". Try again when a download finishes."
                    )
                _lock.wait(min(remaining, 0.5))
            _active[source_id] = _active.get(source_id, 0) + 1
        finally:
            remaining_waiting = _waiting.get(source_id, 0) - 1
            if remaining_waiting > 0:
                _waiting[source_id] = remaining_waiting
            else:
                _waiting.pop(source_id, None)
            _lock.notify_all()
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
    with _lock:
        while _active.get(source_id, 0) >= limit:
            if deadline is None:
                _lock.wait(0.5)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectionBusy(
                    f"The playlist's {limit}-connection limit is in use"
                    + (f" ({purpose})" if purpose else "") + "."
                )
            _lock.wait(min(remaining, 0.5))


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
