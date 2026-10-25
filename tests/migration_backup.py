#!/usr/bin/env python3
"""Migration, builtin-profile, backup and restore tests for schema v5.

Run with:  .venv/bin/python tests/migration_backup.py

Deliberately dependency-free (no pytest) and offline, matching tests/smoke.py.
Every scenario gets its own temporary data/config/cache/media roots, and the
IPTV_CHARTS_* variables are set before importing the application so a restore
that reopens the database through config.db_path() sees the same file.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_BASE = Path(tempfile.mkdtemp(prefix="iptv-migration-test-"))


def _set_dirs(root: Path) -> None:
    os.environ["IPTV_CHARTS_DATA_DIR"] = str(root / "data")
    os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(root / "config")
    os.environ["IPTV_CHARTS_CACHE_DIR"] = str(root / "cache")
    os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(root / "media")
    os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
    os.environ["IPTV_CHARTS_SCHEDULER"] = "0"


_set_dirs(_BASE)

from iptv_charts import backup, config, db  # noqa: E402

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def raises(fn, *args, exc: type[BaseException] = Exception, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except exc:
        return "raised"
    return "returned"


@contextmanager
def workspace(prefix: str = "iptv-migration-"):
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        _set_dirs(Path(tmp))
        yield Path(tmp)


# --------------------------------------------------------------------- helpers
def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _dicts(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params)]


def _builtin_map(conn) -> dict[str, dict]:
    rows = _dicts(
        conn, "SELECT * FROM quality_profiles WHERE builtin_key IS NOT NULL"
    )
    return {str(row["builtin_key"]): row for row in rows}


def _make_archive(tables: dict[str, list[dict]], *,
                  format_version: int, schema_version: int) -> bytes:
    manifest = {
        "format": backup.FORMAT,
        "format_version": format_version,
        "schema_version": schema_version,
        "created_at": int(time.time()),
        "tables": {table: len(rows) for table, rows in tables.items()},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for table, rows in tables.items():
            archive.writestr(f"tables/{table}.json", json.dumps(rows))
    return buffer.getvalue()


def _empty_tables() -> dict[str, list[dict]]:
    return {table: [] for table in backup.TABLES}


def _v1_tables() -> dict[str, list[dict]]:
    """The tables a format-v1 / schema-v4 archive contains.

    Profiles use the 0.7.0 tier shape; there are no bworker files at all.
    """
    tables = _empty_tables()
    for table in ("bworker_work", "bworker_policies", "bworker_events"):
        tables.pop(table)
    tables["sources"] = [{
        "id": 1, "name": "Old playlist", "kind": "m3u", "created_at": 1_600_000_000,
        "m3u_url": "http://example.invalid/list.m3u",
        "sync_status": "idle", "sync_message": None,
    }]
    tables["quality_profiles"] = [{
        "id": 7,
        "name": "4K",
        "resolutions": json.dumps(["2160p", "1080p"]),
        "cutoff": "2160p",
        "upgrades": 1,
        "qualities": json.dumps([{
            "name": "2160p", "resolution": "2160p", "sources": [],
        }]),
        "audio_mode": "any",
        "audio_languages": "[]",
        "subtitle_mode": "any",
        "subtitle_languages": "[]",
        "probe_required": 1,
        "created_at": 1_600_000_000,
    }]
    tables["movies"] = [{
        "id": 1,
        "tmdb_id": 100,
        "title": "Old Movie",
        "year": 2020,
        "release_date": "2020-01-01",
        "status": "Released",
        "monitored": 1,
        "minimum_availability": "released",
        "quality_profile_id": 7,
        "added_at": 1_600_000_000,
    }]
    tables["settings"] = [{"key": "metadata_language", "value": "fr-FR"}]
    return tables


# ---------------------------------------------------------------- fresh schema
def test_fresh_v5() -> None:
    section("fresh schema v5")
    with workspace():
        db.init_db()
        conn = db.connect()
        try:
            check("user_version is 5",
                  db.get_one(conn, "PRAGMA user_version")[0], 5)
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("bworker_work", "bworker_state", "bworker_events",
                          "bworker_policies", "bworker_scan",
                          "bworker_candidates", "worker_jobs"):
                check_true(f"fresh schema has {table}", table in tables)

            builtins = _builtin_map(conn)
            check("exactly three builtins", sorted(builtins), ["1080p", "4k", "720p"])
            check("builtin display names",
                  {key: row["name"] for key, row in builtins.items()},
                  {"1080p": "1080p", "4k": "4K", "720p": "720p"})
            check("builtin resolutions",
                  {key: row["resolution"] for key, row in builtins.items()},
                  {"1080p": "1080p", "4k": "2160p", "720p": "720p"})
            check("builtins are marked immutable",
                  {row["is_builtin"] for row in builtins.values()}, {1})
            check("builtins have upgrades on",
                  {row["upgrades"] for row in builtins.values()}, {1})
            check("builtins target and max are 20",
                  {(row["target_score"], row["max_score"])
                   for row in builtins.values()}, {(20, 20)})
            check("builtin resolution weight is 10",
                  {row["resolution_weight"] for row in builtins.values()}, {10})
            for key, row in builtins.items():
                criteria = json.loads(row["criteria_json"])
                check(f"{key} has eight source choices",
                      len(criteria["quality_options"]), 8)
                check(f"{key} leaves optional sections empty",
                      [section_name for section_name in (
                          "video_codecs", "audio_codecs", "audio_languages",
                          "audio_channels", "subtitle_formats",
                          "subtitle_languages")
                       if criteria[section_name]], [])

            check("default profile setting is 1080p",
                  db.get_setting(conn, "default_profile_key"), "1080p")
            missing = [key for key in db.DEFAULT_SETTINGS
                       if db.get_setting(conn, key) is None]
            check("all v5 settings seeded", missing, [])
            policy_types = {row["work_type"] for row in conn.execute(
                "SELECT work_type FROM bworker_policies")}
            check("all policies seeded", policy_types,
                  {row[0] for row in db.DEFAULT_POLICIES})
            check("activity cleanup is always enabled",
                  db.get_one(conn, "SELECT enabled FROM bworker_policies "
                                   "WHERE work_type = 'activity_cleanup'")[0], 1)
            check("bworker state singleton exists",
                  db.get_one(conn, "SELECT COUNT(*) FROM bworker_state")[0], 1)
            check("no work for an empty library",
                  db.get_one(conn, "SELECT COUNT(*) FROM bworker_work")[0], 0)

            lifecycle = {
                "id", "work_key", "work_type", "entity_type", "entity_id",
                "payload", "enabled", "stage", "status", "failure_count",
                "max_failures", "infinite_retry", "retry_delay_seconds",
                "backoff_multiplier", "next_run_at", "cursor", "best_score",
                "best_source_id", "best_remote_id", "best_episode_stream_id",
                "current_download_id", "profile_revision", "reset_requested",
                "last_error", "started_at", "finished_at", "last_started_at",
                "last_finished_at", "created_at", "updated_at",
            }
            check("bworker_work lifecycle columns", lifecycle <= _columns(
                conn, "bworker_work"), True)
        finally:
            conn.close()

        # apply_schema runs before migrate on every startup, so a database
        # that lost a table introduced in v5 must get it back.
        conn = db.connect()
        conn.execute("DROP TABLE bworker_events")
        conn.commit()
        conn.close()
        db.init_db()
        conn = db.connect()
        try:
            check_true("init_db recreates a missing new table",
                       "bworker_events" in {row["name"] for row in conn.execute(
                           "SELECT name FROM sqlite_master WHERE type='table'")})
            check("builtin repair is idempotent",
                  db.get_one(conn, "SELECT COUNT(*) FROM quality_profiles")[0], 3)
        finally:
            conn.close()


# ---------------------------------------------------------------- v4 migration
_V4_SCHEMA = """
CREATE TABLE quality_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    resolutions TEXT NOT NULL,
    cutoff TEXT,
    upgrades INTEGER NOT NULL DEFAULT 1,
    qualities TEXT,
    audio_mode TEXT NOT NULL DEFAULT 'any'
        CHECK (audio_mode IN ('any', 'original', 'multi', 'single')),
    audio_languages TEXT,
    subtitle_mode TEXT NOT NULL DEFAULT 'any'
        CHECK (subtitle_mode IN ('any', 'required', 'none')),
    subtitle_languages TEXT,
    probe_required INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    original_title TEXT,
    original_language TEXT,
    alt_titles TEXT,
    year INTEGER,
    overview TEXT,
    runtime INTEGER,
    genres TEXT,
    poster_path TEXT,
    backdrop_path TEXT,
    release_date TEXT,
    rating REAL,
    status TEXT,
    added_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER,
    monitored INTEGER NOT NULL DEFAULT 1,
    minimum_availability TEXT NOT NULL DEFAULT 'released',
    priority INTEGER NOT NULL DEFAULT 0,
    quality_profile_id INTEGER REFERENCES quality_profiles(id) ON DELETE SET NULL,
    file_path TEXT,
    file_size INTEGER,
    resolution TEXT,
    source TEXT,
    quality_label TEXT,
    audio_languages TEXT,
    subtitle_languages TEXT,
    media_info TEXT,
    imported_at INTEGER,
    last_search_at INTEGER,
    search_message TEXT,
    candidate_source_id INTEGER,
    candidate_remote_id TEXT,
    candidate_mode TEXT,
    candidate_name TEXT,
    candidate_selected_at INTEGER,
    main_source_id INTEGER,
    main_remote_id TEXT
);
CREATE TABLE series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id INTEGER NOT NULL UNIQUE,
    tvdb_id INTEGER,
    title TEXT NOT NULL,
    original_title TEXT,
    original_language TEXT,
    alt_titles TEXT,
    year INTEGER,
    overview TEXT,
    genres TEXT,
    poster_path TEXT,
    backdrop_path TEXT,
    first_air_date TEXT,
    status TEXT,
    rating REAL,
    added_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER,
    monitored INTEGER NOT NULL DEFAULT 1,
    monitor_new_seasons TEXT NOT NULL DEFAULT 'all'
        CHECK (monitor_new_seasons IN ('all', 'future', 'none')),
    priority INTEGER NOT NULL DEFAULT 0,
    quality_profile_id INTEGER REFERENCES quality_profiles(id) ON DELETE SET NULL,
    last_refreshed_at INTEGER,
    last_search_at INTEGER,
    search_message TEXT
);
CREATE TABLE series_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    absolute_number INTEGER,
    title TEXT,
    overview TEXT,
    air_date TEXT,
    tvdb_id INTEGER,
    monitored INTEGER NOT NULL DEFAULT 1,
    file_path TEXT,
    file_size INTEGER,
    resolution TEXT,
    source TEXT,
    quality_label TEXT,
    audio_languages TEXT,
    subtitle_languages TEXT,
    media_info TEXT,
    imported_at INTEGER,
    last_search_at INTEGER,
    UNIQUE (series_id, season, episode)
);
"""


def _make_v4_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V4_SCHEMA)
        conn.execute(
            "INSERT INTO quality_profiles "
            "(id, name, resolutions, cutoff, upgrades, created_at) "
            "VALUES (1, 'HD', ?, '1080p', 1, 0)",
            (json.dumps(["1080p", "720p", "480p"]),),
        )
        # Deliberately uses a builtin's future name: the migration must delete
        # old profiles before inserting the canonical builtins.
        conn.execute(
            "INSERT INTO quality_profiles "
            "(id, name, resolutions, cutoff, upgrades, created_at) "
            "VALUES (2, '4K', ?, '2160p', 1, 0)",
            (json.dumps(["2160p", "1080p"]),),
        )
        conn.execute(
            "INSERT INTO movies (id, tmdb_id, title, release_date, status, "
            "monitored, minimum_availability, quality_profile_id, added_at) "
            "VALUES (1, 101, 'Legacy HD Movie', '2020-01-01', 'Released', 1, "
            "'released', 1, 0)"
        )
        conn.execute(
            "INSERT INTO movies (id, tmdb_id, title, release_date, status, "
            "monitored, minimum_availability, quality_profile_id, added_at) "
            "VALUES (2, 102, 'Legacy Unassigned Movie', '2020-01-01', "
            "'Released', 1, 'released', NULL, 0)"
        )
        conn.execute(
            "INSERT INTO series (id, tmdb_id, title, monitored, "
            "quality_profile_id, added_at) "
            "VALUES (1, 201, 'Legacy 4K Series', 1, 2, 0)"
        )
        conn.execute(
            "INSERT INTO series_episodes (id, series_id, season, episode, "
            "air_date, monitored) VALUES (1, 1, 1, 1, '2020-01-02', 1)"
        )
        conn.execute(
            "INSERT INTO series_episodes (id, series_id, season, episode, "
            "air_date, monitored) VALUES (2, 1, 1, 2, '2099-01-02', 1)"
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
    finally:
        conn.close()


def test_v4_migration() -> None:
    section("v4 -> v5 migration")
    with workspace():
        path = config.db_path()
        _make_v4_database(path)
        db.init_db()
        conn = db.connect()
        try:
            check("user_version upgraded",
                  db.get_one(conn, "PRAGMA user_version")[0], 5)
            builtins = _builtin_map(conn)
            check("old profiles discarded, builtins installed",
                  sorted(builtins), ["1080p", "4k", "720p"])
            check("old profile names are gone",
                  [row["name"] for row in conn.execute(
                      "SELECT name FROM quality_profiles ORDER BY id")],
                  ["1080p", "4K", "720p"])
            default_id = builtins["1080p"]["id"]
            check("movie with old profile moved to 1080p",
                  db.get_one(conn, "SELECT quality_profile_id FROM movies "
                                   "WHERE id = 1")[0], default_id)
            check("movie with no profile moved to 1080p",
                  db.get_one(conn, "SELECT quality_profile_id FROM movies "
                                   "WHERE id = 2")[0], default_id)
            check("series moved to 1080p",
                  db.get_one(conn, "SELECT quality_profile_id FROM series "
                                   "WHERE id = 1")[0], default_id)
            check("default setting remains 1080p",
                  db.get_setting(conn, "default_profile_key"), "1080p")

            all_ids = {row["id"] for row in conn.execute(
                "SELECT id FROM quality_profiles")}
            check("every profile reference is valid",
                  {row["quality_profile_id"] for row in conn.execute(
                      "SELECT quality_profile_id FROM movies")} <= all_ids, True)

            work = {row["work_key"]: row for row in _dicts(
                conn, "SELECT * FROM bworker_work")}
            check("work seeded for monitored movie and episodes",
                  sorted(work), ["episode:1:search", "episode:2:search",
                                 "movie:1:search", "movie:2:search"])
            check("available movie starts in search",
                  work["movie:1:search"]["stage"], "search")
            check("aired episode starts in search",
                  work["episode:1:search"]["stage"], "search")
            check("future episode waits for air",
                  work["episode:2:search"]["stage"], "waiting_air")
            check_true("future episode is scheduled later",
                       work["episode:2:search"]["next_run_at"] > int(time.time()))
            check("seeded work starts pending",
                  {row["status"] for row in work.values()}, {"pending"})

            before = db.get_one(conn, "SELECT COUNT(*) FROM bworker_work")[0]
            db.seed_bworker_work(conn, now=int(time.time()))
            db.seed_bworker_work(conn, now=int(time.time()))
            check("work seeding is idempotent",
                  db.get_one(conn, "SELECT COUNT(*) FROM bworker_work")[0], before)
            conn.commit()
        finally:
            conn.close()


def test_builtin_repair() -> None:
    section("builtin immutability and repair")
    with workspace():
        db.init_db()
        conn = db.connect()
        try:
            # Damage a builtin: wrong name, resolution, weights and criteria.
            conn.execute(
                "UPDATE quality_profiles SET name = 'Hacked 4K', "
                "resolution = '720p', resolution_weight = 1, target_score = 1, "
                "max_score = 1, criteria_json = '{}' WHERE builtin_key = '4k'"
            )
            # A custom profile occupies a canonical name; deleting the 1080p
            # builtin must still recreate it without losing the custom row.
            conn.execute("DELETE FROM quality_profiles WHERE builtin_key = '1080p'")
            conn.execute(
                "INSERT INTO quality_profiles "
                "(name, resolutions, created_at, is_builtin, resolution, "
                " resolution_weight, target_score, revision, criteria_json) "
                "VALUES ('1080p', '[]', 0, 0, '1080p', 10, 20, 1, '{}')"
            )
            conn.commit()
        finally:
            conn.close()

        db.init_db()
        conn = db.connect()
        try:
            builtins = _builtin_map(conn)
            check("all builtins survive repair",
                  sorted(builtins), ["1080p", "4k", "720p"])
            four_k = builtins["4k"]
            check("damaged builtin name repaired", four_k["name"], "4K")
            check("damaged builtin resolution repaired",
                  four_k["resolution"], "2160p")
            check("damaged builtin weights repaired",
                  (four_k["resolution_weight"], four_k["target_score"],
                   four_k["max_score"]), (10, 20, 20))
            check("damaged builtin criteria repaired",
                  len(json.loads(four_k["criteria_json"])["quality_options"]), 8)
            names = [row["name"] for row in conn.execute(
                "SELECT name FROM quality_profiles ORDER BY id")]
            check("canonical names are unique", len(names), len(set(names)))
            check("custom row preserved under a new name",
                  "1080p (custom)" in names, True)
            check("default builtin still 1080p",
                  db.get_one(conn, "SELECT id FROM quality_profiles "
                                   "WHERE builtin_key = '1080p'")[0],
                  builtins["1080p"]["id"])
        finally:
            conn.close()


# -------------------------------------------------------------------- restores
def _insert_source(conn, source_id: int = 1) -> None:
    conn.execute(
        "INSERT INTO sources (id, name, kind, created_at, sync_status) "
        "VALUES (?, 'Playlist', 'm3u', 0, 'idle')",
        (source_id,),
    )


def test_v1_restore() -> None:
    section("v1 backup restore to v5")
    with workspace():
        db.init_db()
        conn = db.connect()
        try:
            _insert_source(conn, 1)
            conn.execute(
                "INSERT INTO stream_probes (source_id, provider_kind, "
                "provider_remote_id, ok, payload, probed_at) "
                "VALUES (1, 'vod', '900', 1, '{}', 0)"
            )
            conn.execute(
                "INSERT INTO provider_series_cache (source_id, remote_id, "
                "payload, fetched_at) VALUES (1, '88', '{}', 0)"
            )
            conn.commit()
        finally:
            conn.close()

        data = _make_archive(_v1_tables(), format_version=1, schema_version=4)
        conn = db.connect()
        try:
            result = backup.restore(conn, data)
            check("v1 restore format reported", result["format_version"], 1)
            check("v1 restore needs a sync", result["needs_sync"], True)
            check("v1 restore queues the archived source",
                  result["sync_queued"], [1])
            check("v1 restore reports the 1080p default",
                  result["profile_default"], "1080p")

            builtins = _builtin_map(conn)
            check("v1 old profiles are not migrated",
                  sorted(builtins), ["1080p", "4k", "720p"])
            check("v1 restore creates exactly three profiles",
                  db.get_one(conn, "SELECT COUNT(*) FROM quality_profiles")[0], 3)
            check("v1 movie assigned to 1080p",
                  db.get_one(conn, "SELECT quality_profile_id FROM movies "
                                   "WHERE id = 1")[0], builtins["1080p"]["id"])
            check("restored settings preserved",
                  db.get_setting(conn, "metadata_language"), "fr-FR")
            check("v5 settings defaulted after restore",
                  db.get_setting(conn, "candidate_batch_size"), "25")
            check("restored default profile is a builtin key",
                  db.get_setting(conn, "default_profile_key"), "1080p")
            check("stream probes cleared",
                  db.get_one(conn, "SELECT COUNT(*) FROM stream_probes")[0], 0)
            check("provider series cache cleared",
                  db.get_one(conn,
                             "SELECT COUNT(*) FROM provider_series_cache")[0], 0)
            check("restored source present",
                  db.get_one(conn, "SELECT COUNT(*) FROM sources")[0], 1)
            check("restore queues a catalog sync job",
                  db.get_one(conn, "SELECT COUNT(*) FROM worker_jobs "
                                   "WHERE kind = 'sync' AND status = 'queued'")[0], 1)

            work = _dicts(conn, "SELECT * FROM bworker_work")
            check("work seeded from the restored library",
                  [row["work_key"] for row in work], ["movie:1:search"])
            check("restored work starts pending",
                  (work[0]["status"], work[0]["stage"], work[0]["failure_count"]),
                  ("pending", "search", 0))
        finally:
            conn.close()


def _insert_custom_profile(conn, *, name: str = "Custom 4K") -> int:
    criteria = {
        "version": 1,
        "quality_options": [
            {"value": "remux", "label": "REMUX", "weight": 10},
        ],
        "video_codecs": [{"value": "x265", "label": "x265", "weight": 5}],
        "audio_codecs": [],
        "audio_languages": [],
        "audio_channels": [],
        "subtitle_formats": [],
        "subtitle_languages": [],
    }
    now = int(time.time())
    cursor = conn.execute(
        "INSERT INTO quality_profiles "
        "(name, resolutions, cutoff, upgrades, qualities, audio_mode, "
        " audio_languages, subtitle_mode, subtitle_languages, probe_required, "
        " created_at, builtin_key, is_builtin, resolution, resolution_weight, "
        " target_score, max_score, revision, updated_at, criteria_json) "
        "VALUES (?, ?, '2160p', 1, NULL, 'any', '[]', 'any', '[]', 1, ?, "
        "        NULL, 0, '2160p', 10, 25, 35, 3, ?, ?)",
        (name, json.dumps(["2160p"]), now, now, json.dumps(criteria)),
    )
    return int(cursor.lastrowid)


def test_v2_roundtrip() -> None:
    section("v2 backup round-trip (custom profile, work reset, policies)")
    with workspace():
        db.init_db()
        conn = db.connect()
        try:
            movie_id = int(conn.execute(
                "INSERT INTO movies (tmdb_id, title, release_date, status, "
                "monitored, minimum_availability, added_at) "
                "VALUES (300, 'Round Trip Movie', '2020-01-01', 'Released', 1, "
                "'released', 0)"
            ).lastrowid)
            profile_id = _insert_custom_profile(conn)
            conn.execute(
                "UPDATE movies SET quality_profile_id = ? WHERE id = ?",
                (profile_id, movie_id),
            )
            db.set_setting(conn, "default_profile_key", "720p")
            db.set_setting(conn, "activity_refresh_seconds", "15")
            conn.execute(
                "UPDATE bworker_policies SET retry_delay_seconds = 99, "
                "max_failures = 7 WHERE work_type = 'download'"
            )
            db.seed_bworker_work(conn, now=int(time.time()))
            work = conn.execute(
                "SELECT * FROM bworker_work WHERE work_key = ?",
                (f"movie:{movie_id}:search",),
            ).fetchone()
            work_id = int(work["id"])
            conn.execute(
                "UPDATE bworker_work SET stage = 'probing', status = 'retry', "
                "failure_count = 2, last_error = 'boom', "
                "cursor = '{\"epoch\":\"old\",\"offset\":25}', best_score = 25, "
                "best_remote_id = 'abc', profile_revision = 3, "
                "reset_requested = 1, started_at = ?, finished_at = ?, "
                "last_started_at = ?, last_finished_at = ?, "
                "next_run_at = ? WHERE id = ?",
                (100, 110, 100, 110, 50, work_id),
            )
            conn.execute(
                "INSERT INTO bworker_events (work_id, work_type, entity_type, "
                "entity_id, stage, status, message, progress, created_at) "
                "VALUES (?, 'movie_search', 'movie', ?, 'searching', "
                "'progress', 'looked', '{}', 123)",
                (work_id, movie_id),
            )
            conn.commit()

            data = backup.export_zip(conn)
            archive = zipfile.ZipFile(io.BytesIO(data))
            manifest = json.loads(archive.read("manifest.json"))
            names = set(archive.namelist())
        finally:
            conn.close()

        check("export writes format v2", manifest["format_version"], 2)
        check("export records schema v5", manifest["schema_version"], 5)
        check_true("export archives bworker_work",
                   "tables/bworker_work.json" in names)
        check_true("export archives bworker_policies",
                   "tables/bworker_policies.json" in names)
        check_true("export archives bworker_events",
                   "tables/bworker_events.json" in names)
        check("export excludes worker_jobs", "tables/worker_jobs.json" in names,
              False)
        check("export excludes stream_probes", "tables/stream_probes.json" in names,
              False)

        conn = db.connect()
        try:
            conn.execute("UPDATE bworker_policies SET retry_delay_seconds = 5, "
                         "max_failures = 1 WHERE work_type = 'download'")
            conn.execute("UPDATE bworker_work SET status = 'retry', "
                         "failure_count = 9, cursor = 'stale' WHERE id = ?",
                         (work_id,))
            db.set_setting(conn, "activity_refresh_seconds", "5")
            conn.commit()
            restore_started = int(time.time())
            result = backup.restore(conn, data)
        finally:
            conn.close()

        check("v2 restore reports counts",
              result["counts"]["bworker_work"], 1)
        conn = db.connect()
        try:
            custom = conn.execute(
                "SELECT * FROM quality_profiles WHERE name = 'Custom 4K'"
            ).fetchone()
            check_true("custom profile restored", custom is not None)
            if custom is not None:
                check("custom profile score data preserved",
                      (custom["resolution"], custom["target_score"],
                       custom["max_score"], custom["revision"]),
                      ("2160p", 25, 35, 3))
                check("custom profile criteria preserved",
                      len(json.loads(custom["criteria_json"])["video_codecs"]), 1)
                check("movie points at the restored custom profile",
                      db.get_one(conn, "SELECT quality_profile_id FROM movies "
                                       "WHERE id = ?", (movie_id,))[0],
                      custom["id"])
            builtins = _builtin_map(conn)
            check("builtins restored from code",
                  sorted(builtins), ["1080p", "4k", "720p"])

            check("default profile setting restored",
                  db.get_setting(conn, "default_profile_key"), "720p")
            check("user setting restored",
                  db.get_setting(conn, "activity_refresh_seconds"), "15")

            policy = conn.execute(
                "SELECT * FROM bworker_policies WHERE work_type = 'download'"
            ).fetchone()
            check("policy restored without a PK conflict",
                  (policy["retry_delay_seconds"], policy["max_failures"]),
                  (99, 7))
            check("all policy rows present",
                  db.get_one(conn, "SELECT COUNT(*) FROM bworker_policies")[0],
                  len(db.DEFAULT_POLICIES))

            restored_work = conn.execute(
                "SELECT * FROM bworker_work WHERE work_key = ?",
                (f"movie:{movie_id}:search",),
            ).fetchone()
            check_true("work row restored", restored_work is not None)
            if restored_work is not None:
                check("work id stable for event history",
                      restored_work["id"], work_id)
                check("work lifecycle reset",
                      (restored_work["status"], restored_work["stage"],
                       restored_work["failure_count"], restored_work["cursor"],
                       restored_work["best_score"], restored_work["last_error"],
                       restored_work["started_at"], restored_work["finished_at"],
                       restored_work["reset_requested"]),
                      ("pending", "pending", 0, None, None, None, None, None, 0))
                check("work keeps retry policy copy",
                      (restored_work["infinite_retry"],
                       restored_work["retry_delay_seconds"],
                       restored_work["backoff_multiplier"]),
                      (1, 21600, 1.0))
                check_true("work scheduled immediately",
                           restored_work["next_run_at"] >= restore_started)
            check("no duplicate work rows",
                  db.get_one(conn, "SELECT COUNT(*) FROM bworker_work")[0], 1)

            event = conn.execute(
                "SELECT * FROM bworker_events WHERE message = 'looked'"
            ).fetchone()
            check_true("event history restored", event is not None)
            if event is not None:
                check("event still links to its work row",
                      event["work_id"], work_id)
            check("bworker state reset to idle",
                  conn.execute("SELECT status FROM bworker_state WHERE id = 1")
                  .fetchone()[0], "idle")
        finally:
            conn.close()


def test_missing_new_tables() -> None:
    section("missing new-table compatibility")
    with workspace():
        db.init_db()

        # A format-v2 archive written by a pre-v5 build has no bworker files.
        # The manifest schema version says they are absent by design, so the
        # restore treats them as empty rather than rejecting the archive.
        tables = _v1_tables()
        tables["movies"] = []
        tables["settings"] = []
        data = _make_archive(tables, format_version=2, schema_version=4)
        conn = db.connect()
        try:
            result = backup.restore(conn, data)
            check("pre-v5 v2 archive accepted", result["format_version"], 2)
            check("pre-v5 v2 archive restores builtins",
                  sorted(_builtin_map(conn)), ["1080p", "4k", "720p"])
        finally:
            conn.close()

    with workspace():
        db.init_db()
        # The same missing file in a v5 archive is corruption: the version
        # contract says every v5 table must be present.
        tables = _empty_tables()
        tables.pop("bworker_work")
        data = _make_archive(tables, format_version=2, schema_version=5)
        conn = db.connect()
        try:
            check("v5 archive missing a new table is rejected",
                  raises(backup.restore, conn, data, exc=ValueError), "raised")
        finally:
            conn.close()


def test_newer_schema_rejected() -> None:
    section("newer schema rejection")
    with workspace():
        db.init_db()
        tables = _empty_tables()
        data = _make_archive(tables, format_version=2, schema_version=6)
        conn = db.connect()
        try:
            check("newer schema version is rejected",
                  raises(backup.restore, conn, data, exc=ValueError), "raised")
            check("unchanged database after rejection",
                  db.get_one(conn, "PRAGMA user_version")[0], 5)
        finally:
            conn.close()

    with workspace():
        db.init_db()
        data = _make_archive(_empty_tables(), format_version=3,
                             schema_version=5)
        conn = db.connect()
        try:
            check("unknown backup format is rejected",
                  raises(backup.restore, conn, data, exc=ValueError), "raised")
        finally:
            conn.close()


def test_restore_busy() -> None:
    section("restore requires a paused bworker")
    with workspace():
        db.init_db()
        conn = db.connect()
        try:
            data = backup.export_zip(conn)
            now = int(time.time())
            conn.execute(
                "UPDATE bworker_state SET paused = 0, status = 'idle', "
                "heartbeat_at = ?, current_work_id = NULL WHERE id = 1",
                (now,),
            )
            conn.commit()
            check("running bworker blocks restore",
                  raises(backup.restore, conn, data,
                         exc=backup.RestoreBusy), "raised")
            check("database untouched after refusal",
                  db.get_one(conn, "PRAGMA user_version")[0], 5)

            conn.execute(
                "UPDATE bworker_state SET paused = 1, status = 'paused', "
                "heartbeat_at = ? WHERE id = 1",
                (now,),
            )
            conn.commit()
            result = backup.restore(conn, data)
            check("paused bworker allows restore",
                  result["counts"]["quality_profiles"], 3)
            check("restored state keeps bworker paused",
                  db.get_one(conn, "SELECT paused FROM bworker_state")[0], 1)
        finally:
            conn.close()


def main() -> int:
    test_fresh_v5()
    test_v4_migration()
    test_builtin_repair()
    test_v1_restore()
    test_v2_roundtrip()
    test_missing_new_tables()
    test_newer_schema_rejected()
    test_restore_busy()

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = [(label, detail) for ok, label, detail in _results if not ok]

    print(f"\n{'=' * 60}")
    if failed:
        print(f"\033[31m{len(failed)} FAILED\033[0m, {passed} passed\n")
        for label, detail in failed:
            print(f"  \033[31m✗\033[0m {label}\n      {detail}")
        return 1

    print(f"\033[32mall {passed} checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
