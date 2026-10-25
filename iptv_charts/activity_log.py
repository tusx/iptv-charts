"""The bworker activity log stored in Valkey (Redis protocol).

The log is deliberately *not* in SQLite. It is a small, fast, newest-first
stream the Activity page polls every few seconds, so keeping it in the same
in-memory store that already runs beside the app avoids growing the catalog
database with rows that are useful for a few minutes and then worthless.

Layout (one list plus one counter, exactly as the 0.8.0 contract requires):

* ``iptv-charts:activity``      newest entries at the tail (RPUSH)
* ``iptv-charts:activity:id``   monotonically increasing id counter (INCR)

Pushing is one Lua script: assign the next id, append, then -- when the list
reaches the configured cap -- trim it back to the newest ``trim_to`` entries.
Doing the length check and the trim in the same script means two bworker
processes (which the file lock prevents, but the store should not rely on
that) can never leave the list above the cap.

Failure policy: every public method catches every exception. Valkey being
down is a degraded activity log, never a failed download, sync or search.
Short connect/read timeouts and a small circuit breaker keep a hung server
from adding latency to the work loop.

The web process only reads. bworker is the only writer.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("iptv_charts.activity")

DEFAULT_URL = "redis://127.0.0.1:6379/0"
DEFAULT_CAP = 500
DEFAULT_TRIM_TO = 100

#: Deliberately short. The activity log is a UI nicety; blocking a download on
#: a wedged in-memory server would be absurd.
CONNECT_TIMEOUT = 0.25
SOCKET_TIMEOUT = 0.25

#: After a failure, stop hammering the server for this long. Kept short enough
#: that a restarted Valkey is noticed while the Activity page is open.
OFFLINE_RETRY_SECONDS = 10.0

LIST_KEY = "iptv-charts:activity"
COUNTER_KEY = "iptv-charts:activity:id"

# ---------------------------------------------------------------------- events
#: Event names used by the bworker writer. Kept as constants so the API and
#: tests do not have to re-spell them.
EVENT_STARTED = "started"
EVENT_FINISHED = "finished"
EVENT_NO_WORK = "no_work"
EVENT_PAUSED = "paused"
EVENT_RESUMED = "resumed"
EVENT_INTERRUPTED = "interrupted"

#: Status values. A "work" event carries running -> success/failure/skipped.
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_SKIPPED = "skipped"
STATUS_RETRY = "retry"
STATUS_INTERRUPTED = "interrupted"
STATUS_PAUSED = "paused"
STATUS_IDLE = "idle"


def make_entry(*, event: str, status: str, message: str = "",
               level: str = "info", work_id: int | None = None,
               work_type: str | None = None,
               target: Mapping[str, Any] | None = None,
               stage: str | None = None,
               progress: Mapping[str, Any] | None = None,
               attempt: int | None = None,
               next_run_at: int | None = None,
               started_at: int | None = None,
               finished_at: int | None = None,
               duration: int | float | None = None,
               at: int | None = None,
               **extra: Any) -> dict:
    """Build one activity entry.

    ``id`` is assigned by the store. ``at`` and ``started_at`` are both
    present because the UI shows "started 5s ago" from either, while the id
    counter is the only thing the store owns.
    """
    now = int(at if at is not None else time.time())
    entry: dict[str, Any] = {
        "at": now,
        "started_at": int(started_at) if started_at is not None else now,
        "event": str(event),
        "status": str(status),
        "level": str(level),
        "work_id": int(work_id) if work_id is not None else None,
        "work_type": work_type,
        "target": dict(target) if target is not None else None,
        "stage": stage,
        "message": str(message),
        "progress": dict(progress) if progress is not None else None,
        "attempt": int(attempt) if attempt is not None else None,
        "next_run_at": int(next_run_at) if next_run_at is not None else None,
        "finished_at": int(finished_at) if finished_at is not None else None,
        "duration": (round(float(duration), 3)
                     if duration is not None else None),
    }
    entry.update(extra)
    return entry


def _trim_pair(cap: Any, trim_to: Any) -> tuple[int, int]:
    try:
        cap_value = int(cap)
    except (TypeError, ValueError):
        cap_value = DEFAULT_CAP
    cap_value = max(2, cap_value)
    try:
        trim_value = int(trim_to)
    except (TypeError, ValueError):
        trim_value = DEFAULT_TRIM_TO
    trim_value = max(1, min(trim_value, cap_value - 1))
    return cap_value, trim_value


def _safe_url(url: str) -> str:
    """The URL with a password removed, for status output."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        user = parts.username
        if user:
            host = f"{user}:***@{host}" if parts.password else f"{user}@{host}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return "<configured>"


# ------------------------------------------------------------------- Lua scripts
# Redis cjson is available in Valkey/Redis 2.6+. The scripts are intentionally
# tiny and do all state changes (counter, push, trim, update) atomically.
_PUSH_SCRIPT = """
local id = redis.call('INCR', KEYS[2])
local entry = cjson.decode(ARGV[1])
entry['id'] = id
local raw = cjson.encode(entry)
redis.call('RPUSH', KEYS[1], raw)
local length = redis.call('LLEN', KEYS[1])
local cap = tonumber(ARGV[2])
local trim_to = tonumber(ARGV[3])
if length >= cap then
    redis.call('LTRIM', KEYS[1], -trim_to, -1)
end
return {id, raw}
"""

_UPDATE_SCRIPT = """
local target = tostring(ARGV[1])
local patch = cjson.decode(ARGV[2])
local length = redis.call('LLEN', KEYS[1])
for index = 0, length - 1 do
    local raw = redis.call('LINDEX', KEYS[1], index)
    local ok, entry = pcall(cjson.decode, raw)
    if ok and type(entry) == 'table' and tostring(entry['id']) == target then
        for key, value in pairs(patch) do
            entry[key] = value
        end
        redis.call('LSET', KEYS[1], index, cjson.encode(entry))
        return 1
    end
end
return 0
"""

_TRIM_SCRIPT = """
local length = redis.call('LLEN', KEYS[1])
local cap = tonumber(ARGV[1])
local trim_to = tonumber(ARGV[2])
if length >= cap then
    redis.call('LTRIM', KEYS[1], -trim_to, -1)
    return trim_to
end
return length
"""

_INTERRUPT_SCRIPT = """
local at = tonumber(ARGV[1])
local count = 0
local length = redis.call('LLEN', KEYS[1])
for index = 0, length - 1 do
    local raw = redis.call('LINDEX', KEYS[1], index)
    local ok, entry = pcall(cjson.decode, raw)
    if ok and type(entry) == 'table' and entry['status'] == 'running' then
        entry['event'] = 'finished'
        entry['status'] = 'interrupted'
        entry['level'] = 'warning'
        entry['finished_at'] = at
        entry['message'] = 'Interrupted by a bworker restart'
        if entry['started_at'] ~= nil and entry['started_at'] ~= cjson.null then
            entry['duration'] = at - tonumber(entry['started_at'])
        end
        redis.call('LSET', KEYS[1], index, cjson.encode(entry))
        count = count + 1
    end
end
return count
"""


class ValkeyActivityLog:
    """Activity store backed by Valkey/Redis.

    Safe to construct when the server is absent: no connection is opened
    until the first operation, and every operation fails soft.
    """

    backend = "valkey"

    def __init__(self, url: str | None = None, *,
                 cap: int = DEFAULT_CAP, trim_to: int = DEFAULT_TRIM_TO,
                 connect_timeout: float = CONNECT_TIMEOUT,
                 socket_timeout: float = SOCKET_TIMEOUT,
                 offline_retry: float = OFFLINE_RETRY_SECONDS) -> None:
        self.url = (url or os.environ.get("IPTV_CHARTS_VALKEY_URL")
                    or DEFAULT_URL)
        self.cap, self.trim_to = _trim_pair(cap, trim_to)
        self.connect_timeout = float(connect_timeout)
        self.socket_timeout = float(socket_timeout)
        self.offline_retry = max(0.0, float(offline_retry))
        self._list_key = LIST_KEY
        self._counter_key = COUNTER_KEY
        self._client_obj: Any | None = None
        self._client_error: str | None = None
        self._lock = threading.Lock()
        self._offline_until = 0.0
        self._last_error: str | None = None
        self._available: bool | None = None

    # ------------------------------------------------------------- internals
    def configure(self, cap: Any = None, trim_to: Any = None) -> None:
        if cap is None and trim_to is None:
            return
        self.cap, self.trim_to = _trim_pair(
            cap if cap is not None else self.cap,
            trim_to if trim_to is not None else self.trim_to,
        )

    def _client(self) -> Any:
        if self._client_obj is None:
            try:
                import redis  # imported lazily: degraded mode without the package
            except Exception as exc:  # pragma: no cover - requirements pins redis
                raise RuntimeError(f"redis package unavailable: {exc}") from exc
            self._client_obj = redis.Redis.from_url(
                self.url,
                decode_responses=True,
                socket_connect_timeout=self.connect_timeout,
                socket_timeout=self.socket_timeout,
                retry_on_timeout=False,
                socket_keepalive=True,
            )
        return self._client_obj

    def _run(self, operation):
        """Run one Valkey operation, converting every failure to ``None``."""
        now = time.monotonic()
        with self._lock:
            if now < self._offline_until:
                return None
        try:
            result = operation()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:300]
            with self._lock:
                self._offline_until = time.monotonic() + self.offline_retry
                self._last_error = message
                self._available = False
            logger.debug("activity: valkey unavailable: %s", message)
            return None
        with self._lock:
            self._offline_until = 0.0
            self._last_error = None
            self._available = True
        return result

    # ---------------------------------------------------------------- writes
    def push(self, entry: Mapping[str, Any]) -> dict | None:
        """Append one entry. Returns the entry with its assigned id, or None."""
        payload = dict(entry)
        payload.pop("id", None)
        payload.setdefault("at", int(time.time()))
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        cap, trim_to = self.cap, self.trim_to
        result = self._run(lambda: self._client().eval(
            _PUSH_SCRIPT, 2, self._list_key, self._counter_key,
            encoded, cap, trim_to,
        ))
        if not result:
            return None
        try:
            identifier, raw = result[0], result[1]
        except (TypeError, IndexError, KeyError):
            return None
        stored = json.loads(raw) if isinstance(raw, str) else dict(payload)
        if not isinstance(stored, dict):
            stored = dict(payload)
        stored["id"] = int(identifier)
        return stored

    def update(self, entry_id: int, patch: Mapping[str, Any]) -> bool:
        """Merge ``patch`` into the entry with ``entry_id``. Never raises."""
        if entry_id is None:
            return False
        try:
            encoded = json.dumps(dict(patch), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return False
        result = self._run(lambda: self._client().eval(
            _UPDATE_SCRIPT, 1, self._list_key, str(int(entry_id)), encoded,
        ))
        return bool(result)

    def trim(self, cap: Any = None, trim_to: Any = None) -> int | None:
        """Trim to ``trim_to`` when the list has reached ``cap``.

        Returns the resulting length (or None when Valkey is unavailable).
        """
        cap_value, trim_value = _trim_pair(
            cap if cap is not None else self.cap,
            trim_to if trim_to is not None else self.trim_to,
        )
        result = self._run(lambda: self._client().eval(
            _TRIM_SCRIPT, 1, self._list_key, cap_value, trim_value,
        ))
        try:
            return int(result)
        except (TypeError, ValueError):
            return None

    def mark_running_interrupted(self, now: int | None = None) -> int:
        """Mark every entry still saying ``running`` as interrupted."""
        at = int(now if now is not None else time.time())
        result = self._run(lambda: self._client().eval(
            _INTERRUPT_SCRIPT, 1, self._list_key, at,
        ))
        try:
            return int(result or 0)
        except (TypeError, ValueError):
            return 0

    # ----------------------------------------------------------------- reads
    def recent(self, limit: int = 100) -> list[dict]:
        """Newest first. Returns an empty list when Valkey is unavailable."""
        try:
            count = max(1, min(int(limit), DEFAULT_CAP))
        except (TypeError, ValueError):
            count = 100
        rows = self._run(lambda: self._client().lrange(
            self._list_key, -count, -1,
        ))
        if not rows:
            return []
        entries: list[dict] = []
        for raw in rows:
            try:
                item = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                entries.append(item)
        entries.reverse()
        return entries

    def status(self) -> dict:
        """Availability snapshot for the activity API and health endpoint."""
        with self._lock:
            circuit_open = time.monotonic() < self._offline_until
            cached_error = self._last_error
        if circuit_open:
            available = False
        else:
            pong = self._run(lambda: self._client().ping())
            available = bool(pong)
            with self._lock:
                cached_error = self._last_error
        return {
            "backend": self.backend,
            "available": available,
            "degraded": not available,
            "error": cached_error,
            "url": _safe_url(self.url),
            "cap": self.cap,
            "trim_to": self.trim_to,
        }

    # ------------------------------------------------------------ test helpers
    def reset_offline(self) -> None:
        with self._lock:
            self._offline_until = 0.0
            self._last_error = None


class MemoryActivityLog:
    """In-memory fake with exactly the same contract as the Valkey store.

    Used by tests and available for a future ``IPTV_CHARTS_ACTIVITY=memory``
    development mode. It is intentionally thread-safe: the bworker heartbeat
    thread and a progress watcher may push/update at the same time.
    """

    backend = "memory"

    def __init__(self, *, cap: int = DEFAULT_CAP,
                 trim_to: int = DEFAULT_TRIM_TO) -> None:
        self.cap, self.trim_to = _trim_pair(cap, trim_to)
        self._entries: list[dict] = []
        self._next_id = 1
        self._lock = threading.Lock()

    def configure(self, cap: Any = None, trim_to: Any = None) -> None:
        if cap is None and trim_to is None:
            return
        with self._lock:
            self.cap, self.trim_to = _trim_pair(
                cap if cap is not None else self.cap,
                trim_to if trim_to is not None else self.trim_to,
            )

    def push(self, entry: Mapping[str, Any]) -> dict:
        with self._lock:
            stored = copy.deepcopy(dict(entry))
            stored.pop("id", None)
            stored.setdefault("at", int(time.time()))
            stored["id"] = self._next_id
            self._next_id += 1
            self._entries.append(stored)
            if len(self._entries) >= self.cap:
                self._entries = self._entries[-self.trim_to:]
            return copy.deepcopy(stored)

    def update(self, entry_id: int, patch: Mapping[str, Any]) -> bool:
        if entry_id is None:
            return False
        try:
            identifier = int(entry_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            for entry in self._entries:
                if int(entry.get("id") or 0) == identifier:
                    entry.update(copy.deepcopy(dict(patch)))
                    return True
        return False

    def trim(self, cap: Any = None, trim_to: Any = None) -> int:
        cap_value, trim_value = _trim_pair(
            cap if cap is not None else self.cap,
            trim_to if trim_to is not None else self.trim_to,
        )
        with self._lock:
            if len(self._entries) >= cap_value:
                self._entries = self._entries[-trim_value:]
            return len(self._entries)

    def mark_running_interrupted(self, now: int | None = None) -> int:
        at = int(now if now is not None else time.time())
        count = 0
        with self._lock:
            for entry in self._entries:
                if entry.get("status") != STATUS_RUNNING:
                    continue
                entry["event"] = EVENT_FINISHED
                entry["status"] = STATUS_INTERRUPTED
                entry["level"] = "warning"
                entry["finished_at"] = at
                entry["message"] = "Interrupted by a bworker restart"
                started = entry.get("started_at")
                if started is not None:
                    entry["duration"] = at - int(started)
                count += 1
        return count

    def recent(self, limit: int = 100) -> list[dict]:
        try:
            count = max(1, min(int(limit), DEFAULT_CAP))
        except (TypeError, ValueError):
            count = 100
        with self._lock:
            return copy.deepcopy(self._entries[-count:][::-1])

    def status(self) -> dict:
        with self._lock:
            length = len(self._entries)
        return {
            "backend": self.backend,
            "available": True,
            "degraded": False,
            "error": None,
            "url": None,
            "cap": self.cap,
            "trim_to": self.trim_to,
            "entries": length,
        }

    def reset_offline(self) -> None:  # parity with ValkeyActivityLog
        return None

    # Convenience for tests asserting the store never grows past its cap.
    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# --------------------------------------------------------------------- factory
_store: Any | None = None
_store_lock = threading.Lock()


def default_store() -> Any:
    """The process-wide activity store (Valkey unless a test replaced it)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = ValkeyActivityLog()
        return _store


def set_default_store(store: Any) -> None:
    """Replace the process-wide store. Used by tests and the app factory."""
    global _store
    with _store_lock:
        _store = store


def reset_default_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "COUNTER_KEY",
    "DEFAULT_CAP",
    "DEFAULT_TRIM_TO",
    "DEFAULT_URL",
    "EVENT_FINISHED",
    "EVENT_INTERRUPTED",
    "EVENT_NO_WORK",
    "EVENT_PAUSED",
    "EVENT_RESUMED",
    "EVENT_STARTED",
    "LIST_KEY",
    "MemoryActivityLog",
    "STATUS_FAILURE",
    "STATUS_IDLE",
    "STATUS_INTERRUPTED",
    "STATUS_PAUSED",
    "STATUS_RETRY",
    "STATUS_RUNNING",
    "STATUS_SKIPPED",
    "STATUS_SUCCESS",
    "ValkeyActivityLog",
    "default_store",
    "make_entry",
    "reset_default_store",
    "set_default_store",
]
