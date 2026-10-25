"""SQLite persistence helpers for the bworker's declarative work schedule.

``worker_jobs`` stays the imperative queue; this module owns the other half:
one ``bworker_work`` row per recurring/per-item unit, its stage, retry state,
reset flag and the singleton ``bworker_state`` pause authority.

Nothing in this module runs a handler. It is safe for the web process to call
the read helpers (status, policies) and the pause/reset writers; only bworker
calls :func:`claim_work`, :func:`finish_success` and :func:`finish_failure`.

Work rows are seeded for monitored movies/episodes. The add/edit API routes
must call :func:`ensure_movie_work`, :func:`ensure_episode_work` or
:func:`ensure_series_work` after they change monitored state. Keeping those
helpers here (rather than in ``manager_api.py``) avoids coupling shared routes
to bworker internals; see the integration note in the 0.8.0 release notes.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from typing import Any, Mapping

from . import db

WORK_TYPES = (
    "movie_search",
    "series_search",
    "episode_search",
    "playlist_sync",
    "metadata_refresh",
    "download",
    "probe",
    "library_scan",
    "activity_cleanup",
)

ALWAYS_ENABLED = "activity_cleanup"

WORK_TYPE_LABELS = {
    "movie_search": "Movie search",
    "series_search": "Series search",
    "episode_search": "Episode search",
    "playlist_sync": "Playlist sync",
    "metadata_refresh": "Metadata refresh",
    "download": "Download",
    "probe": "Stream probe",
    "library_scan": "Library scan",
    "activity_cleanup": "Activity cleanup",
}

#: Used only when a policy row is missing (a database created before v5).
_POLICY_FALLBACK = {
    "movie_search": (None, 1, 21600, 1.0, 21600),
    "series_search": (None, 1, 14400, 1.0, 14400),
    "episode_search": (None, 1, 14400, 1.0, 14400),
    "playlist_sync": (None, 1, 300, 2.0, 3600),
    "metadata_refresh": (None, 1, 604800, 1.0, 604800),
    "download": (3, 0, 60, 2.0, 900),
    "probe": (3, 0, 300, 1.0, 300),
    "library_scan": (3, 0, 900, 2.0, 900),
    "activity_cleanup": (None, 1, 60, 2.0, 3600),
}

ACTIVE_DOWNLOAD_STATUSES = ("queued", "downloading", "probing", "verifying",
                            "importing", "renaming", "moving")


def _now(now: int | None = None) -> int:
    return int(now if now is not None else time.time())


def _decode(value: Any) -> Any:
    if value in (None, ""):
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _encode(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


# ------------------------------------------------------------------ date math
def _day_start(value: str | None) -> int | None:
    if not value:
        return None
    try:
        day = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return int(datetime(day.year, day.month, day.day,
                        tzinfo=timezone.utc).timestamp())


def _today_start() -> int:
    today = datetime.now(timezone.utc).date()
    return int(datetime(today.year, today.month, today.day,
                        tzinfo=timezone.utc).timestamp())


def movie_initial_stage(movie: Mapping[str, Any], now: int | None = None
                        ) -> tuple[str, int]:
    """Return ``(stage, next_run_at)`` for a newly monitored movie."""
    now = _now(now)
    release = _day_start(movie.get("release_date"))
    if release is not None and release > now:
        # Try again the morning the movie is released. The per-section search
        # interval isn't known here; the migration uses the same rule.
        return "waiting_availability", release
    if str(movie.get("status") or "").lower() in ("released", "canceled"):
        return "search", now
    return "search", now


def episode_initial_stage(air_date: str | None, now: int | None = None
                          ) -> tuple[str, int]:
    now = _now(now)
    air = _day_start(air_date)
    if air is not None and air > now:
        return "waiting_air", air
    return "search", now


# -------------------------------------------------------------------- policies
def policy(conn, work_type: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM bworker_policies WHERE work_type = ?", (str(work_type),)
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    item["infinite_retry"] = bool(item.get("infinite_retry"))
    return item


def policies(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM bworker_policies ORDER BY work_type"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["infinite_retry"] = bool(item.get("infinite_retry"))
        item["label"] = WORK_TYPE_LABELS.get(item["work_type"], item["work_type"])
        item["always_enabled"] = item["work_type"] == ALWAYS_ENABLED
        result.append(item)
    return result


def _policy_values(conn, work_type: str) -> dict:
    row = policy(conn, work_type)
    if row is not None:
        return {
            "max_failures": row.get("max_failures"),
            "infinite_retry": 1 if row.get("infinite_retry") else 0,
            "retry_delay_seconds": int(row.get("retry_delay_seconds") or 60),
            "backoff_multiplier": float(row.get("backoff_multiplier") or 2.0),
        }
    max_failures, infinite, delay, backoff, _cap = _POLICY_FALLBACK.get(
        work_type, (3, 0, 60, 2.0, 900)
    )
    return {
        "max_failures": max_failures,
        "infinite_retry": infinite,
        "retry_delay_seconds": delay,
        "backoff_multiplier": backoff,
    }


def update_policy(conn, work_type: str, changes: Mapping[str, Any]) -> dict:
    """Validate and apply a policy PATCH. Raises ``ValueError`` on bad input."""
    work_type = str(work_type or "").strip()
    row = policy(conn, work_type)
    if row is None:
        raise ValueError(f"Unknown work type: {work_type or '(empty)'}")
    changes = dict(changes or {})
    allowed = {
        "enabled", "max_failures", "infinite_retry", "retry_delay_seconds",
        "backoff_multiplier", "max_delay_seconds",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unsupported policy field(s): {', '.join(sorted(unknown))}")

    enabled = bool(row["enabled"])
    if "enabled" in changes:
        enabled = bool(changes["enabled"])
    if work_type == ALWAYS_ENABLED:
        if not enabled:
            raise ValueError("activity_cleanup cannot be disabled.")
        enabled = True

    def _int(name: str, minimum: int, current: Any, maximum: int | None = None) -> Any:
        if name not in changes:
            return current
        value = changes[name]
        if value is None:
            if name == "max_failures":
                return None
            raise ValueError(f"{name} cannot be null.")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer.") from None
        if number < minimum:
            raise ValueError(f"{name} must be at least {minimum}.")
        if maximum is not None and number > maximum:
            raise ValueError(f"{name} must be at most {maximum}.")
        return number

    max_failures = _int("max_failures", 1, row.get("max_failures"))
    infinite = (1 if bool(changes.get("infinite_retry", row["infinite_retry"]))
                else 0)
    delay = _int("retry_delay_seconds", 1, row.get("retry_delay_seconds"))
    max_delay = _int("max_delay_seconds", 1, row.get("max_delay_seconds"))
    if "backoff_multiplier" in changes:
        try:
            backoff = float(changes["backoff_multiplier"])
        except (TypeError, ValueError):
            raise ValueError("backoff_multiplier must be a number.") from None
        if not 1.0 <= backoff <= 10.0:
            raise ValueError("backoff_multiplier must be between 1.0 and 10.0.")
    else:
        backoff = float(row.get("backoff_multiplier") or 2.0)
    if not infinite and max_failures is None:
        max_failures = 3
    if max_delay is not None and max_delay < delay:
        raise ValueError("max_delay_seconds cannot be less than retry_delay_seconds.")

    conn.execute(
        """
        UPDATE bworker_policies SET enabled = ?, max_failures = ?,
            infinite_retry = ?, retry_delay_seconds = ?,
            backoff_multiplier = ?, max_delay_seconds = ?, updated_at = ?
        WHERE work_type = ?
        """,
        (1 if enabled else 0, max_failures, infinite, delay, backoff,
         max_delay, _now(), work_type),
    )
    conn.commit()
    return policy(conn, work_type) or {}


def policy_enabled(conn, work_type: str) -> bool:
    row = conn.execute(
        "SELECT enabled FROM bworker_policies WHERE work_type = ?", (str(work_type),)
    ).fetchone()
    if row is None:
        return True
    return bool(row["enabled"]) or str(work_type) == ALWAYS_ENABLED


# ----------------------------------------------------------------- work seeding
def movie_work_key(movie_id: int) -> str:
    return f"movie:{int(movie_id)}:search"


def episode_work_key(episode_id: int) -> str:
    return f"episode:{int(episode_id)}:search"


def series_work_key(series_id: int) -> str:
    return f"series:{int(series_id)}:search"


def _insert_work(conn, *, work_key: str, work_type: str, entity_type: str,
                 entity_id: int, payload: Mapping[str, Any], stage: str,
                 next_run_at: int, now: int, enabled: bool = True) -> bool:
    values = _policy_values(conn, work_type)
    status = "pending" if enabled else "disabled"
    if not enabled:
        stage = "skipped"
        next_run_at = None
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO bworker_work
            (work_key, work_type, entity_type, entity_id, payload, enabled,
             stage, status, failure_count, max_failures, infinite_retry,
             retry_delay_seconds, backoff_multiplier, next_run_at,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (work_key, work_type, entity_type, int(entity_id),
         _encode(dict(payload)), 1 if enabled else 0, stage, status,
         values["max_failures"], values["infinite_retry"],
         values["retry_delay_seconds"], values["backoff_multiplier"],
         next_run_at, now, now),
    )
    return cursor.rowcount == 1


def _enable_row(conn, work_key: str, stage: str, next_run_at: int,
                now: int) -> None:
    conn.execute(
        """
        UPDATE bworker_work
        SET enabled = 1, status = 'pending', stage = ?, next_run_at = ?,
            finished_at = NULL, last_error = NULL, updated_at = ?
        WHERE work_key = ? AND (enabled = 0 OR status = 'disabled')
        """,
        (stage, next_run_at, now, work_key),
    )


def _disable_row(conn, work_key: str, now: int, reason: str | None = None) -> None:
    conn.execute(
        """
        UPDATE bworker_work
        SET enabled = 0, status = 'disabled', stage = 'skipped',
            next_run_at = NULL, last_error = ?, updated_at = ?
        WHERE work_key = ? AND (enabled = 1 OR status != 'disabled')
        """,
        (reason, now, work_key),
    )


def ensure_movie_work(conn, movie_id: int, *, now: int | None = None) -> bool:
    """Create/reconcile the search work row for one movie.

    Returns True when a new row was inserted. Safe to call after every add or
    edit; monitoring changes are reconciled in place.
    """
    now = _now(now)
    movie = conn.execute(
        "SELECT id, monitored, release_date, status FROM movies WHERE id = ?",
        (int(movie_id),),
    ).fetchone()
    if movie is None:
        return False
    monitored = bool(movie["monitored"])
    stage, next_run = movie_initial_stage(dict(movie), now)
    key = movie_work_key(movie_id)
    created = _insert_work(
        conn, work_key=key, work_type="movie_search", entity_type="movie",
        entity_id=int(movie_id), payload={"movie_id": int(movie_id)},
        stage=stage, next_run_at=next_run, now=now, enabled=monitored,
    )
    if not created:
        if monitored:
            _enable_row(conn, key, stage, next_run, now)
        else:
            _disable_row(conn, key, now)
    conn.commit()
    return created


def ensure_episode_work(conn, episode_id: int, *, now: int | None = None) -> bool:
    """Create/reconcile the search work row for one episode."""
    now = _now(now)
    row = conn.execute(
        """
        SELECT e.id, e.series_id, e.season, e.episode, e.air_date,
               e.monitored AS episode_monitored, s.monitored AS series_monitored
        FROM series_episodes e
        JOIN series s ON s.id = e.series_id
        WHERE e.id = ?
        """,
        (int(episode_id),),
    ).fetchone()
    if row is None:
        return False
    monitored = bool(row["episode_monitored"]) and bool(row["series_monitored"])
    stage, next_run = episode_initial_stage(row["air_date"], now)
    key = episode_work_key(episode_id)
    payload = {
        "series_id": int(row["series_id"]),
        "episode_id": int(episode_id),
        "season": int(row["season"]),
        "episode": int(row["episode"]),
    }
    created = _insert_work(
        conn, work_key=key, work_type="episode_search", entity_type="episode",
        entity_id=int(episode_id), payload=payload, stage=stage,
        next_run_at=next_run, now=now, enabled=monitored,
    )
    if not created:
        if monitored:
            _enable_row(conn, key, stage, next_run, now)
        else:
            _disable_row(conn, key, now)
    conn.commit()
    return created


def ensure_series_work(conn, series_id: int, *, now: int | None = None) -> int:
    """Ensure work rows for every monitored episode of a series.

    Returns the number of newly created rows. Unmonitoring a series disables
    all its episode rows instead of deleting them, so remonitoring is cheap
    and history survives.
    """
    now = _now(now)
    series = conn.execute(
        "SELECT id, monitored FROM series WHERE id = ?", (int(series_id),)
    ).fetchone()
    if series is None:
        return 0
    episodes = conn.execute(
        "SELECT id FROM series_episodes WHERE series_id = ? ORDER BY season, episode",
        (int(series_id),),
    ).fetchall()
    if not bool(series["monitored"]):
        conn.execute(
            """
            UPDATE bworker_work
            SET enabled = 0, status = 'disabled', stage = 'skipped',
                next_run_at = NULL, updated_at = ?
            WHERE entity_type = 'episode'
              AND entity_id IN (SELECT id FROM series_episodes WHERE series_id = ?)
            """,
            (now, int(series_id)),
        )
        conn.commit()
        return 0
    created = 0
    for episode in episodes:
        if ensure_episode_work(conn, int(episode["id"]), now=now):
            created += 1
    return created


def remove_entity_work(conn, entity_type: str, entity_id: int) -> int:
    """Delete work rows for a deleted entity. Call before/while deleting it."""
    entity_type = str(entity_type)
    entity_id = int(entity_id)
    total = 0
    if entity_type == "series":
        rows = conn.execute(
            "SELECT id, payload FROM bworker_work WHERE entity_type = 'episode'"
        ).fetchall()
        stale = []
        for row in rows:
            payload = _decode(row["payload"])
            if int(payload.get("series_id") or 0) == entity_id:
                stale.append(int(row["id"]))
        for work_id in stale:
            conn.execute("DELETE FROM bworker_work WHERE id = ?", (work_id,))
        total += len(stale)
    cursor = conn.execute(
        "DELETE FROM bworker_work WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    )
    total += max(0, int(cursor.rowcount))
    conn.commit()
    return total


def seed_all(conn, now: int | None = None) -> int:
    """Seed rows for every monitored item (idempotent)."""
    created = int(db.seed_bworker_work(conn, now=_now(now)) or 0)
    conn.commit()
    return created


def reconcile_enabled(conn, *, now: int | None = None) -> dict:
    """Disable work whose entity vanished/unmonitored, re-enable returned ones."""
    now = _now(now)
    disabled = 0
    enabled = 0
    rows = conn.execute(
        "SELECT * FROM bworker_work WHERE entity_type IN ('movie','episode','series')"
    ).fetchall()
    for row in rows:
        entity_type = str(row["entity_type"])
        entity_id = int(row["entity_id"] or 0)
        if entity_type == "movie":
            entity = conn.execute(
                "SELECT monitored FROM movies WHERE id = ?", (entity_id,)
            ).fetchone()
            exists = entity is not None
            monitored = exists and bool(entity["monitored"])
        elif entity_type == "series":
            entity = conn.execute(
                "SELECT monitored FROM series WHERE id = ?", (entity_id,)
            ).fetchone()
            exists = entity is not None
            monitored = exists and bool(entity["monitored"])
        else:
            entity = conn.execute(
                """
                SELECT e.monitored AS episode_monitored,
                       s.monitored AS series_monitored
                FROM series_episodes e JOIN series s ON s.id = e.series_id
                WHERE e.id = ?
                """,
                (entity_id,),
            ).fetchone()
            exists = entity is not None
            monitored = (exists and bool(entity["episode_monitored"])
                         and bool(entity["series_monitored"]))
        if not exists:
            if bool(row["enabled"]) or row["status"] != "disabled":
                _disable_row(conn, row["work_key"], now,
                             "Entity no longer exists")
                disabled += 1
            continue
        if not monitored:
            if bool(row["enabled"]) or row["status"] != "disabled":
                _disable_row(conn, row["work_key"], now)
                disabled += 1
            continue
        if not bool(row["enabled"]) or row["status"] == "disabled":
            if entity_type == "movie":
                ensure_movie_work(conn, entity_id, now=now)
            elif entity_type == "episode":
                ensure_episode_work(conn, entity_id, now=now)
            else:
                ensure_series_work(conn, entity_id, now=now)
            enabled += 1
    conn.commit()
    return {"disabled": disabled, "enabled": enabled}


def counts(conn) -> dict:
    result = {status: 0 for status in
              ("pending", "running", "retry", "failed_stopped", "disabled",
               "complete")}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM bworker_work GROUP BY status"
    ).fetchall():
        result[str(row["status"])] = int(row["n"])
    result["total"] = sum(value for key, value in result.items())
    return result


# ----------------------------------------------------------------- fetching
def normalise(row: Mapping[str, Any] | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = _decode(item.get("payload"))
    item["enabled"] = bool(item.get("enabled"))
    item["infinite_retry"] = bool(item.get("infinite_retry"))
    item["reset_requested"] = bool(item.get("reset_requested"))
    return item


def get_work(conn, work_id: int) -> dict | None:
    return normalise(conn.execute(
        "SELECT * FROM bworker_work WHERE id = ?", (int(work_id),)
    ).fetchone())


def get_work_by_key(conn, work_key: str) -> dict | None:
    return normalise(conn.execute(
        "SELECT * FROM bworker_work WHERE work_key = ?", (str(work_key),)
    ).fetchone())


def work_for_entity(conn, entity_type: str, entity_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM bworker_work WHERE entity_type = ? AND entity_id = ? "
        "ORDER BY id",
        (str(entity_type), int(entity_id)),
    ).fetchall()
    return [item for item in (normalise(row) for row in rows) if item]


def all_work(conn, *, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM bworker_work ORDER BY "
        "CASE status WHEN 'running' THEN 0 WHEN 'failed_stopped' THEN 1 "
        "WHEN 'retry' THEN 2 ELSE 3 END, next_run_at, id LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    return [item for item in (normalise(row) for row in rows) if item]


# ------------------------------------------------------------------ claiming
_CLAIM_SQL = """
    SELECT w.* FROM bworker_work w
    LEFT JOIN bworker_policies p ON p.work_type = w.work_type
    WHERE w.enabled = 1
      AND w.status IN ('pending', 'retry')
      AND COALESCE(w.next_run_at, 0) <= ?
      AND (p.work_type IS NULL OR p.enabled = 1 OR w.work_type = 'activity_cleanup')
    ORDER BY COALESCE(w.next_run_at, 0), w.id
    LIMIT 1
"""


def claim_work(conn, *, now: int | None = None) -> dict | None:
    """Atomically claim one due work row, or return None."""
    now = _now(now)
    try:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(_CLAIM_SQL, (now,)).fetchone()
        if row is None:
            conn.commit()
            return None
        cursor = conn.execute(
            """
            UPDATE bworker_work
            SET status = 'running', started_at = ?, last_started_at = ?,
                finished_at = NULL, updated_at = ?, last_error = NULL,
                reset_requested = 0
            WHERE id = ? AND status IN ('pending', 'retry')
            """,
            (now, now, now, int(row["id"])),
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
    return get_work(conn, int(row["id"]))


def due_exists(conn, *, now: int | None = None) -> bool:
    now = _now(now)
    row = conn.execute(
        "SELECT 1 FROM bworker_work w "
        "LEFT JOIN bworker_policies p ON p.work_type = w.work_type "
        "WHERE w.enabled = 1 AND w.status IN ('pending', 'retry') "
        "AND COALESCE(w.next_run_at, 0) <= ? "
        "AND (p.work_type IS NULL OR p.enabled = 1 OR w.work_type = 'activity_cleanup') "
        "LIMIT 1",
        (now,),
    ).fetchone()
    return row is not None


def next_due_at(conn, *, now: int | None = None) -> int | None:
    now = _now(now)
    row = conn.execute(
        "SELECT MIN(COALESCE(w.next_run_at, 0)) AS at FROM bworker_work w "
        "LEFT JOIN bworker_policies p ON p.work_type = w.work_type "
        "WHERE w.enabled = 1 AND w.status IN ('pending', 'retry') "
        "AND (p.work_type IS NULL OR p.enabled = 1 OR w.work_type = 'activity_cleanup')",
    ).fetchone()
    if row is None or row["at"] is None:
        return None
    return max(now, int(row["at"]))


# --------------------------------------------------------------- reset/recover
def reset_work(conn, *, work_id: int | None = None,
               entity_type: str | None = None, entity_id: int | None = None,
               now: int | None = None) -> dict:
    """Reset one unit or all units to a clean pending state.

    A running unit gets ``reset_requested=1`` and its reset is consumed by the
    bworker at the next loop boundary (after the current bounded unit). An
    idle/retrying/failed unit is reset immediately. Exactly one selector is
    required; ``all_units=True`` is expressed by passing neither id nor entity.
    """
    now = _now(now)
    where: list[str] = []
    params: list[Any] = []
    if work_id is not None:
        where.append("id = ?")
        params.append(int(work_id))
    elif entity_type is not None:
        where.append("entity_type = ?")
        params.append(str(entity_type))
        if entity_id is not None:
            where.append("entity_id = ?")
            params.append(int(entity_id))
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        "SELECT id, status FROM bworker_work" + sql_where, params
    ).fetchall()
    if work_id is not None and not rows:
        return {"updated": 0, "reset_requested": 0, "work_ids": []}

    immediate: list[int] = []
    deferred: list[int] = []
    for row in rows:
        if row["status"] == "running":
            deferred.append(int(row["id"]))
        else:
            immediate.append(int(row["id"]))

    if deferred:
        conn.execute(
            "UPDATE bworker_work SET reset_requested = 1, updated_at = ? "
            "WHERE id IN (" + ",".join("?" for _ in deferred) + ")",
            [now, *deferred],
        )
    if immediate:
        conn.execute(
            """
            UPDATE bworker_work
            SET failure_count = 0, status = 'pending', stage = 'pending',
                next_run_at = ?, cursor = NULL, best_score = NULL,
                best_source_id = NULL, best_remote_id = NULL,
                best_episode_stream_id = NULL, profile_revision = NULL,
                reset_requested = 0, last_error = NULL, finished_at = NULL,
                updated_at = ?
            WHERE id IN (""" + ",".join("?" for _ in immediate) + ")",
            [now, now, *immediate],
        )
    conn.commit()
    return {
        "updated": len(immediate) + len(deferred),
        "reset_requested": len(deferred),
        "work_ids": immediate + deferred,
    }


def consume_resets(conn, *, now: int | None = None) -> int:
    """Apply resets that were requested while a unit was running."""
    now = _now(now)
    rows = conn.execute(
        "SELECT id FROM bworker_work WHERE reset_requested = 1"
    ).fetchall()
    if not rows:
        return 0
    ids = [int(row["id"]) for row in rows]
    conn.execute(
        """
        UPDATE bworker_work
        SET failure_count = 0, status = 'pending', stage = 'pending',
            next_run_at = ?, cursor = NULL, best_score = NULL,
            best_source_id = NULL, best_remote_id = NULL,
            best_episode_stream_id = NULL, profile_revision = NULL,
            reset_requested = 0, last_error = NULL, finished_at = NULL,
            updated_at = ?
        WHERE id IN (""" + ",".join("?" for _ in ids) + ")",
        [now, now, *ids],
    )
    conn.commit()
    return len(ids)


def _active_download(conn, *, movie_id: int | None = None,
                     episode_id: int | None = None,
                     series_id: int | None = None) -> int | None:
    clauses = ["status IN (" + ",".join("?" for _ in ACTIVE_DOWNLOAD_STATUSES) + ")"]
    params: list[Any] = list(ACTIVE_DOWNLOAD_STATUSES)
    if movie_id is not None:
        clauses.append("movie_id = ?")
        params.append(int(movie_id))
    elif episode_id is not None:
        clauses.append("episode_id = ?")
        params.append(int(episode_id))
    elif series_id is not None:
        clauses.append("series_id = ?")
        params.append(int(series_id))
    else:
        return None
    row = conn.execute(
        "SELECT id FROM downloads WHERE " + " AND ".join(clauses) +
        " ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    return int(row["id"]) if row is not None else None


def active_download_for_work(conn, work: Mapping[str, Any]) -> int | None:
    """The active download this unit already owns, if any."""
    current = work.get("current_download_id")
    if current is not None:
        row = conn.execute(
            "SELECT id, status FROM downloads WHERE id = ?", (int(current),)
        ).fetchone()
        if row is not None and row["status"] in ACTIVE_DOWNLOAD_STATUSES:
            return int(row["id"])
    payload = work.get("payload") or {}
    entity_type = str(work.get("entity_type") or "")
    if entity_type == "movie":
        return _active_download(conn, movie_id=int(work.get("entity_id") or 0))
    if entity_type == "episode":
        return _active_download(
            conn, episode_id=int(payload.get("episode_id") or work.get("entity_id") or 0)
        )
    if entity_type == "series":
        return _active_download(
            conn, series_id=int(payload.get("series_id") or work.get("entity_id") or 0)
        )
    return None


def link_active_download(conn, work: Mapping[str, Any], *,
                         now: int | None = None) -> int | None:
    """Attach an existing active download instead of starting a duplicate."""
    now = _now(now)
    active = active_download_for_work(conn, work)
    if active is not None and int(work.get("current_download_id") or 0) != active:
        conn.execute(
            "UPDATE bworker_work SET current_download_id = ?, updated_at = ? "
            "WHERE id = ?",
            (active, now, int(work["id"])),
        )
        conn.commit()
    return active


def recover_orphans(conn, *, now: int | None = None) -> dict:
    """Requeue work left ``running`` by a crash and link active downloads."""
    now = _now(now)
    rows = conn.execute(
        "SELECT * FROM bworker_work WHERE status = 'running'"
    ).fetchall()
    requeued = 0
    linked = 0
    for row in rows:
        work = normalise(row)
        assert work is not None
        download_id = active_download_for_work(conn, work)
        if download_id is not None:
            linked += 1
        message = "Interrupted by a bworker restart; queued again."
        conn.execute(
            """
            UPDATE bworker_work
            SET status = 'pending', stage = CASE WHEN stage = 'pending'
                    THEN 'pending' ELSE stage END,
                next_run_at = ?, current_download_id = COALESCE(?, current_download_id),
                started_at = NULL, finished_at = NULL, last_finished_at = ?,
                last_error = NULL, reset_requested = 0, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (now, download_id, now, now, int(work["id"])),
        )
        # Preserve the interruption in the last error only when the unit had
        # actually started; it is diagnostic, not a retry failure.
        conn.execute(
            "UPDATE bworker_work SET last_error = ? WHERE id = ?",
            (message, int(work["id"])),
        )
        requeued += 1
    conn.commit()
    return {"requeued": requeued, "linked": linked}


# ----------------------------------------------------------------- finishing
def _active_download_by_id(conn, download_id: Any) -> int | None:
    if download_id is None:
        return None
    row = conn.execute(
        "SELECT id, status FROM downloads WHERE id = ?", (int(download_id),)
    ).fetchone()
    if row is None or row["status"] not in ACTIVE_DOWNLOAD_STATUSES:
        return None
    return int(row["id"])


def finish_success(conn, work: Mapping[str, Any], outcome: Mapping[str, Any],
                   *, now: int | None = None) -> dict:
    """Record success/no-result. Returns the effective result summary."""
    now = _now(now)
    work_id = int(work["id"])
    next_run = outcome.get("next_run_at")
    try:
        next_run = int(next_run) if next_run is not None else None
    except (TypeError, ValueError):
        next_run = None
    stage = str(outcome.get("stage") or work.get("stage") or "complete")
    status = "pending" if next_run is not None else "complete"
    current = outcome.get("current_download_id",
                          work.get("current_download_id"))
    current = _active_download_by_id(conn, current)
    conn.execute(
        """
        UPDATE bworker_work
        SET status = ?, stage = ?, failure_count = 0, last_error = NULL,
            next_run_at = ?, current_download_id = ?, finished_at = ?,
            last_finished_at = ?, cursor = COALESCE(?, cursor),
            best_score = COALESCE(?, best_score),
            best_source_id = COALESCE(?, best_source_id),
            best_remote_id = COALESCE(?, best_remote_id),
            best_episode_stream_id = COALESCE(?, best_episode_stream_id),
            profile_revision = COALESCE(?, profile_revision), updated_at = ?
        WHERE id = ?
        """,
        (status, stage, next_run, current, now, now,
         outcome.get("cursor"), outcome.get("best_score"),
         outcome.get("best_source_id"), outcome.get("best_remote_id"),
         outcome.get("best_episode_stream_id"),
         outcome.get("profile_revision"), now, work_id),
    )
    conn.commit()
    return {"status": status, "stage": stage, "next_run_at": next_run,
            "failure_count": 0, "current_download_id": current}


def finish_failure(conn, work: Mapping[str, Any], error: str = "",
                   *, now: int | None = None, stage: str | None = None,
                   transient: bool = True) -> dict:
    """Record a transient failure and schedule a backoff retry.

    A profile rejection is *not* a failure; callers report it through
    :func:`finish_success` with a ``no_candidates`` stage.
    """
    now = _now(now)
    work_id = int(work["id"])
    current = get_work(conn, work_id) or dict(work)
    if not transient:
        return finish_skipped(conn, current, outcome={
            "stage": stage or "failed", "message": error,
            "disable": True,
        }, now=now)
    failure_count = int(current.get("failure_count") or 0) + 1
    infinite = bool(current.get("infinite_retry"))
    max_failures = current.get("max_failures")
    policy_row = policy(conn, str(current.get("work_type") or "")) or {}
    if not infinite and max_failures is None:
        max_failures = policy_row.get("max_failures")
        infinite = bool(policy_row.get("infinite_retry"))
    delay = int(current.get("retry_delay_seconds")
                or policy_row.get("retry_delay_seconds") or 60)
    multiplier = float(current.get("backoff_multiplier")
                       or policy_row.get("backoff_multiplier") or 2.0)
    wait = delay * (multiplier ** max(0, failure_count - 1))
    max_delay = policy_row.get("max_delay_seconds")
    if max_delay is not None:
        wait = min(wait, int(max_delay))
    wait = max(1, int(round(wait)))
    stopped = (not infinite and max_failures is not None
               and failure_count >= int(max_failures))
    status = "failed_stopped" if stopped else "retry"
    next_run = None if stopped else now + wait
    message = str(error or "Work failed")[:2000]
    conn.execute(
        """
        UPDATE bworker_work
        SET status = ?, stage = ?, failure_count = ?, next_run_at = ?,
            last_error = ?, started_at = NULL, finished_at = ?,
            last_finished_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, stage or "failed", failure_count, next_run, message,
         now, now, now, work_id),
    )
    conn.commit()
    return {
        "status": status, "stage": stage or "failed",
        "failure_count": failure_count, "next_run_at": next_run,
        "retry_delay_seconds": wait, "stopped": stopped,
    }


def finish_skipped(conn, work: Mapping[str, Any], outcome: Mapping[str, Any],
                   *, now: int | None = None) -> dict:
    """Record a clean no-op (missing entity, disabled, active download, ...)."""
    now = _now(now)
    work_id = int(work["id"])
    next_run = outcome.get("next_run_at")
    try:
        next_run = int(next_run) if next_run is not None else None
    except (TypeError, ValueError):
        next_run = None
    stage = str(outcome.get("stage") or "skipped")
    disable = bool(outcome.get("disable"))
    if disable:
        status = "disabled"
        enabled = 0
        stage = stage or "skipped"
        next_run = None
    else:
        status = "pending" if next_run is not None else "complete"
        enabled = int(work.get("enabled", 1))
    message = str(outcome.get("message") or "")[:2000] or None
    conn.execute(
        """
        UPDATE bworker_work
        SET enabled = ?, status = ?, stage = ?, next_run_at = ?,
            last_error = ?, finished_at = ?, last_finished_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (enabled, status, stage, next_run,
         message if disable else None, now, now, now, work_id),
    )
    conn.commit()
    return {"status": status, "stage": stage, "next_run_at": next_run,
            "failure_count": int(work.get("failure_count") or 0),
            "disabled": disable}


# ---------------------------------------------------------------------- state
def get_state(conn) -> dict:
    row = conn.execute("SELECT * FROM bworker_state WHERE id = 1").fetchone()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO bworker_state (id, paused, status) "
            "VALUES (1, 0, 'starting')"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM bworker_state WHERE id = 1").fetchone()
    item = dict(row) if row is not None else {
        "id": 1, "paused": 0, "current_work_id": None,
        "current_work_label": None, "started_at": None, "heartbeat_at": None,
        "status": "starting", "updated_at": None,
    }
    item["paused"] = bool(item.get("paused"))
    return item


def manual_allowed(conn) -> bool:
    """Manual acquisition is refused while the shared unit is paused."""
    return not get_state(conn)["paused"]


def set_paused(conn, paused: bool, *, now: int | None = None) -> dict:
    """Set the pause authority. Idempotent; returns ``{changed, state}``."""
    now = _now(now)
    state = get_state(conn)
    desired = bool(paused)
    if state["paused"] == desired:
        return {"changed": False, "state": state}
    if desired:
        status = "paused"
    else:
        status = "idle" if state.get("status") in (
            "paused", "starting", "idle") else str(state.get("status"))
    conn.execute(
        "UPDATE bworker_state SET paused = ?, status = ?, updated_at = ? "
        "WHERE id = 1",
        (1 if desired else 0, status, now),
    )
    conn.commit()
    return {"changed": True, "state": get_state(conn)}


def heartbeat(conn, *, status: str = "idle", current_work_id: int | None = None,
              current_work_label: str | None = None,
              now: int | None = None) -> None:
    """Update bworker_state, preserving the paused flag as authority."""
    now = _now(now)
    conn.execute(
        """
        INSERT INTO bworker_state
            (id, paused, current_work_id, current_work_label, started_at,
             heartbeat_at, status, updated_at)
        VALUES (1, 0, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            current_work_id = excluded.current_work_id,
            current_work_label = excluded.current_work_label,
            heartbeat_at = excluded.heartbeat_at,
            updated_at = excluded.updated_at,
            status = CASE
                WHEN bworker_state.paused = 1
                     AND excluded.status NOT IN ('stopped', 'error')
                    THEN 'paused'
                WHEN bworker_state.status = 'stopped'
                     AND excluded.status NOT IN ('stopped', 'error', 'starting')
                    THEN 'starting'
                ELSE excluded.status END
        """,
        (current_work_id, current_work_label, now, now, str(status), now),
    )
    conn.commit()


def mark_stopped(conn, *, now: int | None = None) -> None:
    now = _now(now)
    conn.execute(
        "UPDATE bworker_state SET status = 'stopped', current_work_id = NULL, "
        "current_work_label = NULL, heartbeat_at = ?, updated_at = ? WHERE id = 1",
        (now, now),
    )
    conn.commit()


def work_target(conn, work: Mapping[str, Any]) -> dict:
    """Human identity for the activity feed and log."""
    entity_type = str(work.get("entity_type") or "global")
    entity_id = work.get("entity_id")
    payload = work.get("payload") or {}
    label = str(work.get("work_key") or work.get("work_type") or "work")
    if entity_type == "movie":
        row = conn.execute(
            "SELECT title, year FROM movies WHERE id = ?",
            (int(entity_id or 0),),
        ).fetchone()
        if row is not None:
            label = str(row["title"] or label)
    elif entity_type == "series":
        row = conn.execute(
            "SELECT title FROM series WHERE id = ?", (int(entity_id or 0),)
        ).fetchone()
        if row is not None:
            label = str(row["title"] or label)
    elif entity_type == "episode":
        row = conn.execute(
            """
            SELECT s.title, e.season, e.episode
            FROM series_episodes e JOIN series s ON s.id = e.series_id
            WHERE e.id = ?
            """,
            (int(entity_id or 0),),
        ).fetchone()
        if row is not None:
            label = (f"{row['title']} S{int(row['season']):02d}"
                     f"E{int(row['episode']):02d}")
        else:
            season = payload.get("season")
            episode = payload.get("episode")
            if season is not None and episode is not None:
                label = f"Episode S{int(season):02d}E{int(episode):02d}"
    elif entity_type == "source":
        row = conn.execute(
            "SELECT name FROM sources WHERE id = ?", (int(entity_id or 0),)
        ).fetchone()
        if row is not None:
            label = str(row["name"] or label)
    else:
        label = str(work.get("work_type") or label)
    return {"kind": entity_type, "id": int(entity_id) if entity_id is not None else None,
            "label": label}


__all__ = [
    "ALWAYS_ENABLED",
    "WORK_TYPES",
    "WORK_TYPE_LABELS",
    "active_download_for_work",
    "all_work",
    "claim_work",
    "consume_resets",
    "counts",
    "due_exists",
    "ensure_episode_work",
    "ensure_movie_work",
    "ensure_series_work",
    "episode_initial_stage",
    "episode_work_key",
    "finish_failure",
    "finish_skipped",
    "finish_success",
    "get_state",
    "get_work",
    "get_work_by_key",
    "heartbeat",
    "link_active_download",
    "manual_allowed",
    "mark_stopped",
    "movie_initial_stage",
    "movie_work_key",
    "next_due_at",
    "normalise",
    "policy",
    "policy_enabled",
    "policies",
    "reconcile_enabled",
    "recover_orphans",
    "remove_entity_work",
    "reset_work",
    "seed_all",
    "series_work_key",
    "set_paused",
    "update_policy",
    "work_for_entity",
    "work_target",
]
