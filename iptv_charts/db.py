"""SQLite connection handling, schema management and bulk-load tuning.

Threading model: one connection per thread, which is SQLite's own
recommendation and avoids needing ``check_same_thread=False``. Flask requests
get a connection scoped to the request via ``flask.g``; the sync worker opens
its own. Connections are never shared across threads.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Bumped whenever schema.sql starts relying on a column that existing
#: databases do not have. New tables appear through CREATE IF NOT EXISTS;
#: columns need an explicit step below.
SCHEMA_VERSION = 2

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

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def init_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        apply_schema(conn)
        migrate(conn)
    finally:
        conn.close()


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
