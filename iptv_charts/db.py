"""SQLite connection handling, schema management and bulk-load tuning.

Threading model: one connection per thread, which is SQLite's own
recommendation and avoids needing ``check_same_thread=False``. Flask requests
get a connection scoped to the request via ``flask.g``; the sync worker opens
its own. Connections are never shared across threads.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Bumped whenever schema.sql starts relying on a column that existing
#: databases do not have. New tables appear through CREATE IF NOT EXISTS;
#: columns need an explicit step below.
#:
#: v5 (0.8.0): weighted profiles, items.imdb_id, per-item overrides, file and
#: download scores, and the bworker work/state/events/policy/scan/candidate
#: tables. The legacy quality_profiles columns stay in place; old profile rows
#: are discarded and every movie/series is assigned to the 1080p builtin.
SCHEMA_VERSION = 5

#: The profile new movies/series receive when they have no explicit choice.
DEFAULT_PROFILE_KEY = "1080p"

#: Builtin profiles, in display order. The canonical data shape is owned here
#: (not in quality.py) so migration and restore agree without importing the
#: scoring engine. Insert order is separate: 1080p is inserted first so that
#: legacy code selecting "the first profile by id" still lands on the default.
BUILTIN_PROFILES = (
    {"builtin_key": "4k", "name": "4K", "resolution": "2160p"},
    {"builtin_key": "1080p", "name": "1080p", "resolution": "1080p"},
    {"builtin_key": "720p", "name": "720p", "resolution": "720p"},
)

#: Key -> spec lookup used by validation and backup restore.
BUILTIN_PROFILES_BY_KEY = {
    profile["builtin_key"]: profile for profile in BUILTIN_PROFILES
}

_BUILTIN_INSERT_ORDER = ("1080p", "4k", "720p")

#: Builtins score the resolution only. Source/quality is not part of a
#: profile any more: provider catalogues frequently omit it, so it must not
#: block a release. The minimum/target is therefore 10.
_BUILTIN_QUALITY_OPTIONS: tuple[tuple[str, str], ...] = ()

#: Settings introduced with schema v5. They are also seeded in schema.sql for
#: fresh databases; this copy lets the v5 migration and startup repair run
#: independently of schema.sql's execution order.
DEFAULT_SETTINGS = {
    "external_player_enabled": "0",
    "default_profile_key": DEFAULT_PROFILE_KEY,
    "movie_search_interval_hours": "6",
    "series_search_interval_hours": "4",
    "candidate_batch_size": "25",
    "probe_batch_per_loop": "3",
    "update_check_enabled": "1",
    "bworker_retry_delay_seconds": "60",
    "bworker_retry_backoff": "2.0",
    "bworker_retry_max_delay_seconds": "3600",
    "bworker_idle_wait_seconds": "30",
    "bworker_fast_loop_seconds": "10",
    "activity_log_cap": "500",
    "activity_log_trim_to": "100",
    "activity_refresh_seconds": "5",
}

#: (work_type, enabled, max_failures, infinite_retry, retry_delay_seconds,
#:  backoff_multiplier, max_delay_seconds)
DEFAULT_POLICIES = (
    ("movie_search", 1, None, 1, 21600, 1.0, 21600),
    ("series_search", 1, None, 1, 14400, 1.0, 14400),
    ("episode_search", 1, None, 1, 14400, 1.0, 14400),
    ("playlist_sync", 1, None, 1, 300, 2.0, 3600),
    ("metadata_refresh", 1, None, 1, 604800, 1.0, 604800),
    ("download", 1, 3, 0, 60, 2.0, 900),
    ("probe", 1, 3, 0, 300, 1.0, 300),
    ("library_scan", 1, 3, 0, 900, 2.0, 900),
    ("activity_cleanup", 1, None, 1, 60, 2.0, 3600),
)

_TASKS = ("search_missing", "refresh_metadata", "refresh_catalog", "scan_library")

# Applied to every connection that serves requests.
#
# Note there is no journal_mode here. WAL is a persistent property of the
# database file, not of a connection, so it is set once in `connect()` and
# left alone afterwards -- see the comment there for why that matters.
#
# synchronous=NORMAL is the standard WAL pairing: durable against application
# crashes, and the worst case on power loss is losing the last few
# transactions of a catalog sync, which is re-runnable anyway.
SERVING_PRAGMAS = (
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 10000",
    "PRAGMA temp_store = MEMORY",
)

# Applied only for the duration of a bulk ingest.
#
# Deliberately does NOT touch journal_mode. Switching journal mode requires
# exclusive access to the database, so with the UI holding any other
# connection open -- which it usually is -- the switch fails with "database is
# locked" and takes the whole sync down with it. Staying in WAL costs very
# little here: the expensive part of a WAL commit is the fsync, and
# synchronous=OFF already removes that.
#
# All three settings below are per-connection and can be changed freely while
# other connections are reading.
BULK_PRAGMAS = (
    "PRAGMA synchronous = OFF",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA cache_size = -131072",  # ~128 MB, in KiB as a negative value
)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a tuned connection. Creates the parent directory if needed."""
    target = path or config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target, timeout=10.0)
    conn.row_factory = sqlite3.Row

    # Set WAL only when the file is not already in it. Re-issuing the pragma
    # on every connection looks harmless but is not: switching journal mode
    # needs exclusive access, so doing it while another connection is open
    # raises "database is locked". On the common path this is a single cheap
    # read that confirms there is nothing to do.
    try:
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        # Another connection is momentarily busy. WAL is already set on any
        # database we created, so carrying on is correct.
        pass

    for pragma in SERVING_PRAGMAS:
        conn.execute(pragma)
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables, indexes, the FTS index and its triggers.

    Idempotent: every statement uses IF NOT EXISTS, so this is safe to re-run.
    ``bulk_load`` relies on that to restore the FTS triggers it dropped.
    """
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


# --------------------------------------------------------------- v5 canonical
def _builtin_criteria() -> dict:
    """The canonical criteria_json shared by all three builtin profiles.

    Builtins configure only the mandatory resolution. Optional sections start
    empty, so their maximum and target are both the resolution weight of 10.
    """
    return {
        "version": 1,
        "quality_options": [
            {"value": value, "label": label, "weight": 10}
            for value, label in _BUILTIN_QUALITY_OPTIONS
        ],
        "video_codecs": [],
        "audio_codecs": [],
        "audio_languages": [],
        "audio_channels": [],
        "subtitle_formats": [],
        "subtitle_languages": [],
    }


def _release_builtin_name(conn: sqlite3.Connection, name: str, key: str) -> None:
    """Free a builtin's canonical name without discarding user data.

    Builtin names (4K, 1080p, 720p) are reserved. A damaged or partial
    database can hold a custom row under one of those names while the
    canonical row is missing; a plain ``INSERT OR IGNORE`` would then be
    silently skipped on the name conflict and leave the builtin absent. Any
    conflicting row is renamed instead, so the insert can proceed and the
    custom profile survives under a distinct name. A builtin row that itself
    carries the wrong name is renamed too; the UPDATE by ``builtin_key`` below
    restores its canonical name on its own iteration.
    """
    rows = conn.execute(
        "SELECT id, builtin_key FROM quality_profiles WHERE name = ?", (name,)
    ).fetchall()
    for row in rows:
        if str(row["builtin_key"] or "") == key:
            continue
        base = f"{name} (custom)"
        candidate = base
        suffix = 2
        while conn.execute(
            "SELECT 1 FROM quality_profiles WHERE name = ?", (candidate,)
        ).fetchone():
            candidate = f"{base} {suffix}"
            suffix += 1
        conn.execute(
            "UPDATE quality_profiles SET name = ? WHERE id = ?",
            (candidate, int(row["id"])),
        )


def ensure_builtin_profiles(conn: sqlite3.Connection, *,
                            now: int | None = None) -> dict[str, int]:
    """Insert (or repair) the three immutable builtins and return key -> id.

    Legacy columns are populated as well, because an old ``quality.Profile``
    reader may run until the weighted engine takes over. The criteria payload
    is canonical: editing a builtin through the API is forbidden, and a
    damaged row is repaired on the next startup.

    Robust to a missing or damaged builtin row: the canonical row is inserted
    when absent and overwritten when present. A custom row occupying a
    canonical name is renamed rather than deleted (see
    ``_release_builtin_name``), so a migration/startup repair can never fail
    with a UNIQUE-name error.
    """
    now = int(now or time.time())
    criteria = _builtin_criteria()
    criteria_json = json.dumps(criteria, ensure_ascii=False, sort_keys=True)
    selected = [option["value"] for option in criteria["quality_options"]]
    specs = {spec["builtin_key"]: spec for spec in BUILTIN_PROFILES}

    for key in _BUILTIN_INSERT_ORDER:
        spec = specs[key]
        resolution = spec["resolution"]
        _release_builtin_name(conn, spec["name"], key)
        legacy_qualities = json.dumps([{
            "name": resolution,
            "resolution": resolution,
            "sources": selected,
        }])
        conn.execute(
            """
            INSERT OR IGNORE INTO quality_profiles
                (name, resolutions, cutoff, upgrades, qualities,
                 audio_mode, audio_languages, subtitle_mode, subtitle_languages,
                 probe_required, created_at, builtin_key, is_builtin,
                 resolution, resolution_weight, target_score, max_score,
                 revision, updated_at, criteria_json)
            VALUES (?, ?, ?, 1, ?, 'any', '[]', 'any', '[]', 1, ?,
                    ?, 1, ?, 10, 10, 10, 1, ?, ?)
            """,
            (
                spec["name"], json.dumps([resolution]), resolution,
                legacy_qualities, now, key, resolution, now, criteria_json,
            ),
        )
        # Keep the builtin canonical even if an old/backup path inserted a
        # row with the same key. Builtins are immutable by contract.
        conn.execute(
            """
            UPDATE quality_profiles SET
                name = ?, resolutions = ?, cutoff = ?, upgrades = 1,
                qualities = ?, audio_mode = 'any', audio_languages = '[]',
                subtitle_mode = 'any', subtitle_languages = '[]',
                probe_required = 1, is_builtin = 1, resolution = ?,
                resolution_weight = 10, target_score = 10, max_score = 10,
                revision = 1, updated_at = COALESCE(updated_at, ?),
                criteria_json = ?
            WHERE builtin_key = ?
            """,
            (
                spec["name"], json.dumps([resolution]), resolution,
                legacy_qualities, resolution, now, criteria_json, key,
            ),
        )

    rows = conn.execute(
        "SELECT id, builtin_key FROM quality_profiles WHERE builtin_key IS NOT NULL"
    ).fetchall()
    return {str(row["builtin_key"]): int(row["id"]) for row in rows}


def ensure_default_settings(conn: sqlite3.Connection) -> None:
    """Seed the 0.8.0 settings keys without overwriting user values."""
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def ensure_bworker_policies(conn: sqlite3.Connection, *,
                            now: int | None = None) -> None:
    """Seed the per-work-type retry policies without overwriting edits."""
    now = int(now or time.time())
    for row in DEFAULT_POLICIES:
        work_type, enabled, max_failures, infinite_retry, delay, backoff, max_delay = row
        conn.execute(
            """
            INSERT OR IGNORE INTO bworker_policies
                (work_type, enabled, max_failures, infinite_retry,
                 retry_delay_seconds, backoff_multiplier, max_delay_seconds,
                 updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (work_type, enabled, max_failures, infinite_retry,
             delay, backoff, max_delay, now),
        )
    # Log cleanup is the one policy that must never be switched off: it has to
    # keep trimming the Valkey log while bworker is paused.
    conn.execute(
        "UPDATE bworker_policies SET enabled = 1 WHERE work_type = 'activity_cleanup'"
    )


def ensure_bworker_state(conn: sqlite3.Connection) -> None:
    """Create the singleton state row if a database predates schema v5."""
    conn.execute(
        "INSERT OR IGNORE INTO bworker_state (id, paused, status) "
        "VALUES (1, 0, 'starting')"
    )


def _parse_iso_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return None


def _date_epoch(day: date) -> int:
    """Unix seconds for midnight UTC of ``day``.

    Availability/air dates are calendar dates; pinning them to UTC makes the
    stored next_run_at independent of the host timezone.
    """
    return int(datetime(day.year, day.month, day.day,
                        tzinfo=timezone.utc).timestamp())


def _movie_initial_stage(row, now: int) -> tuple[str, int]:
    """(stage, next_run_at) for a freshly seeded movie work row."""
    mode = str(row["minimum_availability"] or "released").lower()
    if mode == "announced":
        return "search", now
    release = _parse_iso_date(row["release_date"])
    today = date.fromtimestamp(now)
    if release is None:
        # No date: check back daily instead of searching a cinema recording.
        return "waiting_availability", now + 86400
    if release > today:
        return "waiting_availability", _date_epoch(release)
    if mode == "in_cinemas":
        return "search", now
    status = str(row["status"] or "").lower()
    if not status or status == "released":
        return "search", now
    return "waiting_availability", now + 86400


def _episode_initial_stage(air_date, now: int) -> tuple[str, int]:
    """(stage, next_run_at) for a freshly seeded episode work row."""
    day = _parse_iso_date(air_date)
    if day is None:
        return "waiting_air", now + 86400
    if day > date.fromtimestamp(now):
        return "waiting_air", _date_epoch(day)
    return "search", now


def seed_bworker_work(conn: sqlite3.Connection, *, now: int | None = None) -> int:
    """Create work rows for monitored movies and episodes.

    Safe to call repeatedly: ``work_key`` is UNIQUE and every insert uses
    ``INSERT OR IGNORE``. Existing rows (including disabled ones) are never
    overwritten. Work keys are part of the public contract:
    ``movie:<movie_id>:search`` and ``episode:<episode_id>:search``.
    """
    now = int(now or time.time())
    inserted = 0

    movies = conn.execute(
        """
        SELECT id, release_date, status, minimum_availability, file_path
        FROM movies WHERE monitored = 1 ORDER BY id
        """
    ).fetchall()
    for movie in movies:
        stage, next_run = _movie_initial_stage(movie, now)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO bworker_work
                (work_key, work_type, entity_type, entity_id, payload, enabled,
                 stage, status, failure_count, max_failures, infinite_retry,
                 retry_delay_seconds, backoff_multiplier, next_run_at,
                 created_at, updated_at)
            VALUES (?, 'movie_search', 'movie', ?, ?, 1, ?, 'pending', 0,
                    NULL, 1, 21600, 1.0, ?, ?, ?)
            """,
            (
                f"movie:{int(movie['id'])}:search", int(movie["id"]),
                json.dumps({"movie_id": int(movie["id"])}),
                stage, int(next_run), now, now,
            ),
        )
        inserted += int(cursor.rowcount or 0)

    episodes = conn.execute(
        """
        SELECT e.id, e.series_id, e.season, e.episode, e.air_date, e.file_path
        FROM series_episodes e
        JOIN series s ON s.id = e.series_id
        WHERE e.monitored = 1 AND s.monitored = 1
        ORDER BY e.id
        """
    ).fetchall()
    for episode in episodes:
        stage, next_run = _episode_initial_stage(episode["air_date"], now)
        payload = {
            "series_id": int(episode["series_id"]),
            "episode_id": int(episode["id"]),
            "season": int(episode["season"]),
            "episode": int(episode["episode"]),
        }
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO bworker_work
                (work_key, work_type, entity_type, entity_id, payload, enabled,
                 stage, status, failure_count, max_failures, infinite_retry,
                 retry_delay_seconds, backoff_multiplier, next_run_at,
                 created_at, updated_at)
            VALUES (?, 'episode_search', 'episode', ?, ?, 1, ?, 'pending', 0,
                    NULL, 1, 14400, 1.0, ?, ?, ?)
            """,
            (
                f"episode:{int(episode['id'])}:search", int(episode["id"]),
                json.dumps(payload), stage, int(next_run), now, now,
            ),
        )
        inserted += int(cursor.rowcount or 0)

    return inserted


def cancel_obsolete_search_jobs(conn: sqlite3.Connection, *,
                                now: int | None = None) -> int:
    """Cancel queued 0.7.0 search jobs that bworker now owns.

    Only queued search/search-task rows are affected. Playlist syncs,
    downloads, metadata refreshes, probes and library scans continue untouched.
    Running handlers are left to finish; their results are still valid.
    """
    now = int(now or time.time())
    cancelled = 0
    rows = conn.execute(
        "SELECT id, kind, payload FROM worker_jobs WHERE status = 'queued'"
    ).fetchall()
    for row in rows:
        kind = str(row["kind"] or "")
        obsolete = kind in {"search_movie", "search_series"}
        if kind == "task":
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            obsolete = isinstance(payload, dict) and payload.get("name") == "search_missing"
        if not obsolete:
            continue
        cursor = conn.execute(
            """
            UPDATE worker_jobs SET status = 'cancelled', finished_at = ?,
                   cancel_requested = 1,
                   message = 'Superseded by bworker in the 0.8.0 upgrade'
            WHERE id = ? AND status = 'queued'
            """,
            (now, int(row["id"])),
        )
        cancelled += int(cursor.rowcount or 0)
    return cancelled


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to ``SCHEMA_VERSION``.

    ``schema.sql`` is intentionally all ``IF NOT EXISTS``, so it can only add
    tables and indexes. Columns on tables that already exist need a real
    migration, and this is where those live. Kept out of ``apply_schema``
    because ``bulk_load`` re-runs that on every sync to restore the FTS
    triggers.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    if version < 1:
        if "request_headers" not in _columns(conn, "items"):
            conn.execute("ALTER TABLE items ADD COLUMN request_headers TEXT")

        # Seeded once rather than in schema.sql: a user who deletes a profile
        # should not find it re-created on the next restart.
        now = int(time.time())
        for name, resolutions, cutoff in (
            ("HD", ["1080p", "720p", "480p", "unknown"], "1080p"),
            ("4K", ["2160p", "1080p", "720p"], "2160p"),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO quality_profiles "
                "(name, resolutions, cutoff, upgrades, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (name, json.dumps(resolutions), cutoff, now),
            )
        for name in _TASKS:
            conn.execute("INSERT OR IGNORE INTO task_state (name) VALUES (?)", (name,))

    if version < 2:
        # Stream probing, source/audio/subtitle quality requirements, per-source
        # connection limits, queue priority and Sonarr-style new-season rules.
        # Every column is added conditionally: a fresh database already has
        # them from schema.sql, an existing one needs the ALTER.
        additions = (
            ("sources", "connection_limit", "INTEGER"),
            ("quality_profiles", "qualities", "TEXT"),
            ("quality_profiles", "audio_mode", "TEXT NOT NULL DEFAULT 'any'"),
            ("quality_profiles", "audio_languages", "TEXT"),
            ("quality_profiles", "subtitle_mode", "TEXT NOT NULL DEFAULT 'any'"),
            ("quality_profiles", "subtitle_languages", "TEXT"),
            ("quality_profiles", "probe_required", "INTEGER NOT NULL DEFAULT 1"),
            ("movies", "original_language", "TEXT"),
            ("movies", "status", "TEXT"),
            ("movies", "minimum_availability", "TEXT NOT NULL DEFAULT 'released'"),
            ("movies", "priority", "INTEGER NOT NULL DEFAULT 0"),
            ("movies", "source", "TEXT"),
            ("movies", "quality_label", "TEXT"),
            ("movies", "audio_languages", "TEXT"),
            ("movies", "subtitle_languages", "TEXT"),
            ("movies", "media_info", "TEXT"),
            ("series", "original_language", "TEXT"),
            ("series", "monitor_new_seasons", "TEXT NOT NULL DEFAULT 'all'"),
            ("series", "priority", "INTEGER NOT NULL DEFAULT 0"),
            ("series_episodes", "source", "TEXT"),
            ("series_episodes", "quality_label", "TEXT"),
            ("series_episodes", "audio_languages", "TEXT"),
            ("series_episodes", "subtitle_languages", "TEXT"),
            ("series_episodes", "media_info", "TEXT"),
            ("downloads", "priority", "INTEGER NOT NULL DEFAULT 0"),
            ("downloads", "source", "TEXT"),
            ("downloads", "quality_label", "TEXT"),
            ("downloads", "audio_languages", "TEXT"),
            ("downloads", "subtitle_languages", "TEXT"),
            ("downloads", "media_info", "TEXT"),
        )
        for table, column, definition in additions:
            if column not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        # An index on a column that did not exist before the ALTERs above.
        # This cannot live in schema.sql: that script runs before migration on
        # an old database, where the column is still missing.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_queue "
            "ON downloads (status, priority DESC, next_attempt_at, id)"
        )

        # Movies added before minimum availability existed should not suddenly
        # stop being searched just because TMDB has no release date for them.
        conn.execute(
            "UPDATE movies SET minimum_availability = 'announced' "
            "WHERE (release_date IS NULL OR release_date = '') "
            "AND minimum_availability = 'released'"
        )

        # Existing profiles only had resolutions. Turn each into an ordered
        # tier with no source restriction so decisions keep working until the
        # user edits the profile.
        rows = conn.execute(
            "SELECT id, resolutions FROM quality_profiles WHERE qualities IS NULL"
        ).fetchall()
        for row in rows:
            try:
                resolutions = json.loads(row["resolutions"] or "[]")
            except (TypeError, ValueError):
                resolutions = []
            tiers = [
                {"name": str(resolution), "resolution": str(resolution), "sources": []}
                for resolution in resolutions
                if isinstance(resolution, str)
            ]
            conn.execute(
                "UPDATE quality_profiles SET qualities = ? WHERE id = ?",
                (json.dumps(tiers), row["id"]),
            )

    if version < 3:
        # The web process sets this flag; the worker owns the transfer and
        # checks the flag while it runs. A column rather than an in-memory
        # event because the two are separate processes.
        if "cancel_requested" not in _columns(conn, "downloads"):
            conn.execute(
                "ALTER TABLE downloads ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
            )

    if version < 4:
        # Manual candidate pin and additional movie versions. The alternate
        # table itself is created by schema.sql; existing downloads are main.
        movie_columns = _columns(conn, "movies")
        additions = (
            ("candidate_source_id", "INTEGER"),
            ("candidate_remote_id", "TEXT"),
            ("candidate_mode", "TEXT"),
            ("candidate_name", "TEXT"),
            ("candidate_selected_at", "INTEGER"),
            ("main_source_id", "INTEGER"),
            ("main_remote_id", "TEXT"),
        )
        for column, definition in additions:
            if column not in movie_columns:
                conn.execute(f"ALTER TABLE movies ADD COLUMN {column} {definition}")
        if "mode" not in _columns(conn, "downloads"):
            conn.execute(
                "ALTER TABLE downloads ADD COLUMN mode TEXT NOT NULL DEFAULT 'main'"
            )
        # Files downloaded before v4 have no stored provider identity. Recover
        # it from the imported download that produced the current main file so
        # the candidate page cannot re-download the same stream as an extra.
        conn.execute(
            """
            UPDATE movies SET
                main_source_id = COALESCE(main_source_id, (
                    SELECT d.source_id FROM downloads d
                    WHERE d.movie_id = movies.id AND d.kind = 'movie'
                      AND d.mode = 'main' AND d.status = 'imported'
                      AND d.source_id IS NOT NULL AND d.final_path = movies.file_path
                    ORDER BY d.id DESC LIMIT 1
                )),
                main_remote_id = COALESCE(main_remote_id, (
                    SELECT d.provider_remote_id FROM downloads d
                    WHERE d.movie_id = movies.id AND d.kind = 'movie'
                      AND d.mode = 'main' AND d.status = 'imported'
                      AND d.source_id IS NOT NULL AND d.final_path = movies.file_path
                    ORDER BY d.id DESC LIMIT 1
                ))
            WHERE file_path IS NOT NULL AND file_path <> ''
              AND (main_source_id IS NULL OR main_remote_id IS NULL)
            """
        )

    if version < 5:
        # ---------------------------------------------------------------
        # 0.8.0: weighted profiles, item/download scores and bworker.
        #
        # guard every column: a fresh database already has them from
        # schema.sql, while a v4 database needs the ALTER. New tables are
        # created by schema.sql before this runs.
        # ---------------------------------------------------------------
        now = int(time.time())
        additions = (
            ("items", "imdb_id", "TEXT"),
            ("quality_profiles", "builtin_key", "TEXT"),
            ("quality_profiles", "is_builtin", "INTEGER NOT NULL DEFAULT 0"),
            ("quality_profiles", "resolution", "TEXT"),
            ("quality_profiles", "resolution_weight", "INTEGER NOT NULL DEFAULT 10"),
            ("quality_profiles", "target_score", "INTEGER NOT NULL DEFAULT 10"),
            ("quality_profiles", "max_score", "INTEGER"),
            ("quality_profiles", "revision", "INTEGER NOT NULL DEFAULT 1"),
            ("quality_profiles", "updated_at", "INTEGER"),
            ("quality_profiles", "criteria_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("movies", "file_score", "INTEGER"),
            ("movies", "candidate_batch_size", "INTEGER"),
            ("movies", "probe_batch_size", "INTEGER"),
            ("movies", "search_interval_hours", "REAL"),
            ("series", "candidate_batch_size", "INTEGER"),
            ("series", "probe_batch_size", "INTEGER"),
            ("series", "search_interval_hours", "REAL"),
            ("series_episodes", "file_score", "INTEGER"),
            ("downloads", "score", "INTEGER"),
            ("downloads", "score_details", "TEXT"),
        )
        for table, column, definition in additions:
            if column not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        # Defensive: if a partial v5 run created bworker_work before these
        # lifecycle columns were added to schema.sql, add them here. A normal
        # v4 -> v5 run gets the complete table from schema.sql already.
        work_additions = (
            ("best_score", "INTEGER"),
            ("best_source_id", "INTEGER"),
            ("best_remote_id", "TEXT"),
            ("best_episode_stream_id", "TEXT"),
            ("current_download_id", "INTEGER"),
            ("profile_revision", "INTEGER"),
            ("reset_requested", "INTEGER NOT NULL DEFAULT 0"),
            ("last_started_at", "INTEGER"),
            ("last_finished_at", "INTEGER"),
        )
        if _columns(conn, "bworker_work"):
            for column, definition in work_additions:
                if column not in _columns(conn, "bworker_work"):
                    conn.execute(
                        f"ALTER TABLE bworker_work ADD COLUMN {column} {definition}"
                    )

        # Same defensive rule for the A03 scan/candidate identity fields. A
        # normal v4 -> v5 upgrade gets them from schema.sql; this only repairs
        # a database created by a partial v5 build.
        scan_additions = (
            ("query_fingerprint", "TEXT"),
            ("exhausted", "INTEGER NOT NULL DEFAULT 0"),
            ("matched_count", "INTEGER NOT NULL DEFAULT 0"),
        )
        if _columns(conn, "bworker_scan"):
            for column, definition in scan_additions:
                if column not in _columns(conn, "bworker_scan"):
                    conn.execute(
                        f"ALTER TABLE bworker_scan ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bworker_scan_fingerprint "
                "ON bworker_scan (work_id, query_fingerprint, id DESC)"
            )
        candidate_additions = (
            ("item_id", "INTEGER REFERENCES items(id) ON DELETE SET NULL"),
            ("match_kind", "TEXT"),
            ("match_score", "REAL"),
        )
        if _columns(conn, "bworker_candidates"):
            for column, definition in candidate_additions:
                if column not in _columns(conn, "bworker_candidates"):
                    conn.execute(
                        f"ALTER TABLE bworker_candidates ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bworker_candidates_item "
                "ON bworker_candidates (item_id, status, score DESC, id)"
            )

        # Candidate resolution is ID-first, so identity lookups must not scan
        # the whole catalogue.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_kind_tmdb ON items (kind, tmdb_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_kind_imdb ON items (kind, imdb_id)"
        )
        # One row per builtin key; custom profiles have NULL and are exempt.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_quality_profiles_builtin "
            "ON quality_profiles (builtin_key) WHERE builtin_key IS NOT NULL"
        )

        # Old domain profiles are intentionally not migrated. Delete them
        # before inserting builtins because quality_profiles.name is UNIQUE and
        # a 0.7.0 profile may well be called "4K" or "1080p". Movie/series
        # identity is preserved; only their profile reference is reset.
        conn.execute(
            "DELETE FROM quality_profiles WHERE is_builtin = 0 OR builtin_key IS NULL"
        )
        builtin_ids = ensure_builtin_profiles(conn, now=now)
        default_id = builtin_ids[DEFAULT_PROFILE_KEY]
        conn.execute(
            """
            UPDATE movies SET quality_profile_id = ?
            WHERE quality_profile_id IS NULL
               OR quality_profile_id NOT IN (
                   SELECT id FROM quality_profiles WHERE is_builtin = 1)
            """,
            (default_id,),
        )
        conn.execute(
            """
            UPDATE series SET quality_profile_id = ?
            WHERE quality_profile_id IS NULL
               OR quality_profile_id NOT IN (
                   SELECT id FROM quality_profiles WHERE is_builtin = 1)
            """,
            (default_id,),
        )

        ensure_default_settings(conn)
        ensure_bworker_policies(conn, now=now)
        ensure_bworker_state(conn)
        # Work rows are seeded only for monitored items and never duplicated:
        # bworker_work.work_key is UNIQUE and the insert uses OR IGNORE.
        seed_bworker_work(conn, now=now)
        cancel_obsolete_search_jobs(conn, now=now)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def init_db(path: Path | None = None) -> None:
    """Create or migrate the database, serialising concurrent startups.

    The web server and the background worker start together in the container
    and both call this. ``CREATE TABLE IF NOT EXISTS`` is safe to race, but an
    ``ALTER TABLE`` in a migration is not: the second process would see the
    column appear between its check and its own ALTER. An advisory file lock
    around the whole operation makes first-run startup deterministic.
    """
    target = path or config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / ".iptv-charts-init.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        conn = connect(target)
        try:
            apply_schema(conn)
            migrate(conn)
            # These are cheap INSERT OR IGNORE repairs, not migrations. They
            # keep the immutable builtins, defaults and singleton state row
            # present even if an early v5 database was created before the
            # schema was finalised.
            ensure_default_settings(conn)
            ensure_bworker_policies(conn)
            ensure_bworker_state(conn)
            ensure_builtin_profiles(conn)
            conn.commit()
        finally:
            conn.close()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def bulk_load(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Context manager wrapping a large ingest.

    Drops the FTS triggers for the duration, then rebuilds the whole index in
    one pass and restores the triggers. For a 200k-row catalog this is far
    faster than letting the triggers maintain the index row by row, and it
    keeps peak memory flat rather than accumulating index writes inside the
    same transaction as the data.
    """
    for trigger in ("items_fts_ai", "items_fts_ad", "items_fts_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.commit()

    for pragma in BULK_PRAGMAS:
        conn.execute(pragma)

    try:
        yield conn
    finally:
        # Rebuild the index in a single pass over the content table.
        conn.execute("INSERT INTO items_fts (items_fts) VALUES ('rebuild')")
        conn.commit()

        # Restore serving pragmas.
        for pragma in SERVING_PRAGMAS:
            conn.execute(pragma)
        conn.commit()

        # Re-create the triggers. schema.sql is entirely IF NOT EXISTS, so
        # running it again only restores what we dropped.
        apply_schema(conn)


def get_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def get_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = get_one(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
