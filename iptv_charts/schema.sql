-- IPTV Charts schema.
--
-- Two deliberate omissions/changes worth knowing before editing:
--
-- 1. There is no `raw_json` column anywhere. Storing each provider's full
--    original object for a 200k-item catalog is the largest RAM and disk cost
--    this design could incur, and it buys almost nothing. We keep the fields
--    we actually display and re-fetch detail on demand.
--
-- 2. `remote_id` is TEXT, and identity is (source_id, kind, remote_id). The
--    same numeric id genuinely can appear as both a live `stream_id` and a
--    `series_id` on one panel, so the `kind` component is load-bearing, not
--    decoration.

PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- sources: one row per configured playlist
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL,
    kind                TEXT    NOT NULL CHECK (kind IN ('m3u', 'xtream')),
    created_at          INTEGER NOT NULL,

    -- Xtream credentials
    base_url            TEXT,
    username            TEXT,
    password            TEXT,
    https_port          INTEGER,

    -- M3U URL
    m3u_url             TEXT,

    -- Headers required by some providers, as a JSON object:
    -- {"user_agent": "...", "referrer": "...", "headers": {"X-Foo": "bar"}}
    request_headers     TEXT,

    -- Cached snapshot of the account/server response, for the info page.
    -- These are refreshed on every sync so the info page renders without
    -- making a live API call.
    user_info_json      TEXT,
    server_info_json    TEXT,
    account_status      TEXT,
    is_trial            INTEGER,
    exp_date            INTEGER,
    -- Set when exp_date was null/0/negative. Providers use all three to mean
    -- "no expiry recorded", and computing a countdown from them produces
    -- nonsense dates, so we record that we don't know instead.
    exp_unknown         INTEGER NOT NULL DEFAULT 0,
    active_connections  INTEGER,
    -- What the provider reports for the account. Display/account information.
    max_connections     INTEGER,
    -- What IPTV Charts is allowed to use. NULL means "use max_connections
    -- when the provider reports one, otherwise 1". Enforced across playlist
    -- syncs, metadata calls and downloads.
    connection_limit    INTEGER,
    allowed_formats     TEXT,           -- JSON array, e.g. ["m3u8","ts"]
    server_timezone     TEXT,
    server_protocol     TEXT,
    server_host         TEXT,
    server_port         INTEGER,

    -- Sync bookkeeping
    last_synced_at      INTEGER,
    last_sync_seconds   REAL,
    sync_status         TEXT    NOT NULL DEFAULT 'idle',
    sync_message        TEXT
);

-- --------------------------------------------------------------------------
-- categories
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL CHECK (kind IN ('live', 'vod', 'series')),
    remote_id   TEXT,
    name        TEXT    NOT NULL,
    -- Denormalised so the browse-page filter dropdown is one cheap query
    -- rather than a GROUP BY over the whole items table on every page load.
    item_count  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, kind, remote_id)
);

CREATE INDEX IF NOT EXISTS idx_categories_source
    ON categories (source_id, kind);

-- --------------------------------------------------------------------------
-- items: every playable thing (live channel, movie, or series)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL CHECK (kind IN ('live', 'vod', 'series')),
    remote_id       TEXT    NOT NULL,
    num             INTEGER,            -- provider display-order hint; NEVER in a URL
    name            TEXT    NOT NULL,
    category_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    logo            TEXT,
    epg_channel_id  TEXT,
    container_ext   TEXT,
    added_ts        INTEGER,            -- parsed from `added`; drives the timeline chart
    tv_archive      INTEGER,
    rating          REAL,
    plot            TEXT,
    genre           TEXT,
    duration        TEXT,
    cast_list       TEXT,
    director        TEXT,
    release_date    TEXT,
    youtube_trailer TEXT,
    tmdb_id         TEXT,
    -- IMDb identity supplied by provider metadata. Candidate resolution tries
    -- TMDB, then IMDb, then title/year because providers are inconsistent
    -- about which identifier they expose.
    imdb_id         TEXT,
    -- 1 once get_vod_info has been fetched for this row, so we only pay for
    -- the detail payload when a user actually opens the item.
    detail_loaded   INTEGER NOT NULL DEFAULT 0,
    -- Per-stream HTTP requirements from an M3U entry (#EXTVLCOPT, URL
    -- headers), as the same JSON shape as sources.request_headers. Xtream
    -- rows use the source-level headers instead.
    request_headers TEXT,
    UNIQUE (source_id, kind, remote_id)
);

CREATE INDEX IF NOT EXISTS idx_items_source_kind ON items (source_id, kind);
CREATE INDEX IF NOT EXISTS idx_items_category    ON items (category_id);
CREATE INDEX IF NOT EXISTS idx_items_name        ON items (name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_items_added       ON items (source_id, added_ts);

-- --------------------------------------------------------------------------
-- Full-text search over the fields a user might search by.
--
-- External-content FTS5: the index stores only the inverted index and re-reads
-- the `items` row for content, which roughly halves the on-disk cost versus a
-- standalone FTS table.
-- --------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    name,
    plot,
    genre,
    cast_list,
    content='items',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);

-- Triggers keep the index correct for incremental edits (detail loads,
-- renames). Bulk sync deliberately does NOT rely on these -- see db.bulk_load,
-- which drops them, loads, then rebuilds the index once. Per-row trigger
-- maintenance during a 200k-row insert is dramatically slower than one rebuild.
CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts (rowid, name, plot, genre, cast_list)
    VALUES (new.id, new.name, new.plot, new.genre, new.cast_list);
END;

CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, name, plot, genre, cast_list)
    VALUES ('delete', old.id, old.name, old.plot, old.genre, old.cast_list);
END;

CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, name, plot, genre, cast_list)
    VALUES ('delete', old.id, old.name, old.plot, old.genre, old.cast_list);
    INSERT INTO items_fts (rowid, name, plot, genre, cast_list)
    VALUES (new.id, new.name, new.plot, new.genre, new.cast_list);
END;

-- --------------------------------------------------------------------------
-- settings: simple key/value
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('stale_after_hours', '24'),
    ('preferred_player',  'auto'),
    ('default_tab',       'all'),
    ('metadata_language', 'en-US'),
    ('movie_folder_template',  '{title} ({year})'),
    ('movie_file_template',    '{title} ({year}) [{resolution}]'),
    ('series_folder_template', '{title} ({year})'),
    ('season_folder_template', 'Season {season:00}'),
    ('episode_file_template',
     '{series} - S{season:00}E{episode:00} - {episode_title} [{resolution}]'),
    ('download_concurrency',  '1'),
    ('download_max_attempts', '3'),
    ('search_interval_hours', '6'),
    ('refresh_interval_hours','168'),
    ('catalog_refresh_hours', '24'),
    -- Media probing. Every candidate is inspected with ffprobe before it is
    -- downloaded, so the quality decision uses the real video/audio/subtitle
    -- streams rather than the provider's filename.
    ('probe_cache_hours',     '24'),
    ('probe_timeout_seconds', '30'),
    ('probe_max_candidates',  '6'),
    ('probe_search_budget',   '60'),
    -- 0.8.0 section/profile/bworker defaults. The quality-profile rows
    -- themselves are seeded by db.ensure_builtin_profiles() (migration and
    -- every startup) because an old database does not have the new columns
    -- until migration runs, and an INSERT here would fail before that.
    ('external_player_enabled', '0'),
    ('default_profile_key',     '1080p'),
    ('movie_search_interval_hours', '6'),
    ('series_search_interval_hours', '4'),
    ('candidate_batch_size',    '25'),
    ('probe_batch_per_loop',    '3'),
    ('update_check_enabled',    '1'),
    ('bworker_retry_delay_seconds',     '60'),
    ('bworker_retry_backoff',           '2.0'),
    ('bworker_retry_max_delay_seconds', '3600'),
    ('bworker_idle_wait_seconds',       '30'),
    ('bworker_fast_loop_seconds',       '10'),
    ('activity_log_cap',        '500'),
    ('activity_log_trim_to',    '100'),
    ('activity_refresh_seconds', '5');

-- --------------------------------------------------------------------------
-- Media library and automation
--
-- Library rows are keyed by TMDB/TVDB identity, never by items.id: a catalog
-- sync deletes and re-inserts items with fresh ids. Grabs record the provider
-- identity (source_id, kind, remote_id) and re-resolve the URL when the
-- download starts.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quality_profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    -- Legacy resolution list, retained so old databases and clients can read
    -- a profile. New decisions use `qualities` below.
    resolutions TEXT    NOT NULL,
    cutoff      TEXT,
    upgrades    INTEGER NOT NULL DEFAULT 1,
    -- Ordered quality tiers, best first. Each tier is
    -- {"name":"4K WEB-DL","resolution":"2160p","sources":["web-dl"]}.
    -- An empty source list means any source. Source restrictions are taken
    -- from the release name; audio/subtitle restrictions come from ffprobe.
    qualities   TEXT,
    audio_mode  TEXT    NOT NULL DEFAULT 'any'
        CHECK (audio_mode IN ('any', 'original', 'multi', 'single')),
    -- JSON array of ISO 639-2/B language codes. When set, at least one audio
    -- track must match one of them.
    audio_languages TEXT,
    subtitle_mode TEXT NOT NULL DEFAULT 'any'
        CHECK (subtitle_mode IN ('any', 'required', 'none')),
    -- JSON array of ISO 639-2/B codes; the token "original" resolves to the
    -- movie/series original language from metadata.
    subtitle_languages TEXT,
    -- When 1 (the default), automatic grabs require a successful ffprobe of
    -- the stream. A failed probe is reported instead of guessed around.
    probe_required INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL,

    -- ------------------------------------------------------------------
    -- Schema v5 weighted profiles.
    --
    -- The columns above are the legacy 0.7.0 tier model. They are retained
    -- (and populated for builtins) so an old database can be read before the
    -- v5 migration and so ALTER TABLE has stable NOT NULL columns. New code
    -- reads the fields below; criteria_json is the forward-compatible
    -- payload documented in docs/implementation/0.8.0-contracts.md.
    -- ------------------------------------------------------------------
    -- Immutable defaults: '4k', '1080p', '720p'. NULL for custom profiles.
    builtin_key       TEXT,
    is_builtin        INTEGER NOT NULL DEFAULT 0
        CHECK (is_builtin IN (0, 1)),
    -- The single mandatory resolution. NULL only on legacy rows that have
    -- not been migrated; the v5 migration replaces every such row.
    resolution        TEXT
        CHECK (resolution IS NULL OR
               resolution IN ('2160p', '1080p', '720p', '480p', '360p')),
    resolution_weight INTEGER NOT NULL DEFAULT 10,
    -- target_score is the old plan's min_score: minimum score for target_met.
    target_score      INTEGER NOT NULL DEFAULT 20,
    -- Computed maximum score; stored so the API/UI can show it without
    -- re-deriving the algebra. NULL for a legacy row until saved.
    max_score         INTEGER,
    revision          INTEGER NOT NULL DEFAULT 1,
    updated_at        INTEGER,
    -- JSON: quality_options, video_codecs, audio_codecs, audio_languages,
    -- audio_channels, subtitle_formats, subtitle_languages.
    criteria_json     TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS movies (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id            INTEGER NOT NULL UNIQUE,
    imdb_id            TEXT,
    title              TEXT    NOT NULL,
    original_title     TEXT,
    original_language  TEXT,            -- ISO 639-2/B, from TMDB
    -- JSON array of alternative titles, used to match localised provider names.
    alt_titles         TEXT,
    year               INTEGER,
    overview           TEXT,
    runtime            INTEGER,
    genres             TEXT,            -- JSON array
    poster_path        TEXT,            -- TMDB path, served through the image proxy
    backdrop_path      TEXT,
    release_date       TEXT,
    rating             REAL,
    status             TEXT,            -- TMDB release status
    added_at           INTEGER NOT NULL,
    updated_at         INTEGER,
    monitored          INTEGER NOT NULL DEFAULT 1,
    -- Radarr-style minimum availability: announced | in_cinemas | released.
    minimum_availability TEXT NOT NULL DEFAULT 'released',
    priority           INTEGER NOT NULL DEFAULT 0,
    quality_profile_id INTEGER REFERENCES quality_profiles(id) ON DELETE SET NULL,
    file_path          TEXT,
    file_size          INTEGER,
    resolution         TEXT,
    source             TEXT,            -- parsed release source, e.g. web-dl
    quality_label      TEXT,            -- matched quality tier at import
    audio_languages    TEXT,            -- JSON array from the stream probe
    subtitle_languages TEXT,            -- JSON array from the stream probe
    media_info         TEXT,            -- normalised ffprobe payload
    imported_at        INTEGER,
    last_search_at     INTEGER,
    search_message     TEXT,
    -- One manually pinned provider candidate. `only` uses it instead of the
    -- quality profile; `also` downloads it as an additional version while the
    -- profile pick remains the main library file.
    candidate_source_id   INTEGER,
    candidate_remote_id   TEXT,
    candidate_mode        TEXT CHECK (candidate_mode IN ('only', 'also')),
    candidate_name        TEXT,
    candidate_selected_at INTEGER,
    -- Provider identity of the current main file, so an "also" pin of the
    -- same candidate is not downloaded twice.
    main_source_id        INTEGER,
    main_remote_id        TEXT,
    -- Schema v5: score of the current file against its profile at import or
    -- last evaluation. Used for upgrade decisions; NULL means unscored.
    file_score            INTEGER,
    -- Per-item overrides of the section defaults. NULL means "use the
    -- section setting" (candidate_batch_size/probe_batch_per_loop/
    -- movie_search_interval_hours).
    candidate_batch_size  INTEGER,
    probe_batch_size      INTEGER,
    search_interval_hours REAL
);

CREATE INDEX IF NOT EXISTS idx_movies_title ON movies (title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS series (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tmdb_id            INTEGER NOT NULL UNIQUE,
    tvdb_id            INTEGER,
    title              TEXT    NOT NULL,
    original_title     TEXT,
    original_language  TEXT,            -- ISO 639-2/B, from TMDB
    alt_titles         TEXT,
    year               INTEGER,
    overview           TEXT,
    genres             TEXT,
    poster_path        TEXT,
    backdrop_path      TEXT,
    first_air_date     TEXT,
    status             TEXT,
    rating             REAL,
    added_at           INTEGER NOT NULL,
    updated_at         INTEGER,
    monitored          INTEGER NOT NULL DEFAULT 1,
    -- Sonarr's "monitor new seasons": all | future | none. Used when a
    -- metadata refresh discovers episodes that were not in the library yet.
    monitor_new_seasons TEXT NOT NULL DEFAULT 'all'
        CHECK (monitor_new_seasons IN ('all', 'future', 'none')),
    priority           INTEGER NOT NULL DEFAULT 0,
    quality_profile_id INTEGER REFERENCES quality_profiles(id) ON DELETE SET NULL,
    last_refreshed_at  INTEGER,
    last_search_at     INTEGER,
    search_message     TEXT,
    -- Schema v5 per-item overrides; NULL means use the series section
    -- setting.
    candidate_batch_size  INTEGER,
    probe_batch_size      INTEGER,
    search_interval_hours REAL
);

CREATE INDEX IF NOT EXISTS idx_series_title ON series (title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS series_episodes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id      INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    season         INTEGER NOT NULL,
    episode        INTEGER NOT NULL,
    absolute_number INTEGER,
    title          TEXT,
    overview       TEXT,
    air_date       TEXT,                -- YYYY-MM-DD
    tvdb_id        INTEGER,
    monitored      INTEGER NOT NULL DEFAULT 1,
    file_path      TEXT,
    file_size      INTEGER,
    resolution     TEXT,
    source         TEXT,
    quality_label  TEXT,
    audio_languages TEXT,
    subtitle_languages TEXT,
    media_info     TEXT,
    imported_at    INTEGER,
    last_search_at INTEGER,
    -- Schema v5: score of the current file against its series profile.
    file_score     INTEGER,
    UNIQUE (series_id, season, episode)
);

CREATE INDEX IF NOT EXISTS idx_episodes_series
    ON series_episodes (series_id, season, episode);
CREATE INDEX IF NOT EXISTS idx_episodes_air_date
    ON series_episodes (air_date);

CREATE TABLE IF NOT EXISTS downloads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT    NOT NULL CHECK (kind IN ('movie', 'episode')),
    movie_id          INTEGER REFERENCES movies(id) ON DELETE SET NULL,
    series_id         INTEGER REFERENCES series(id) ON DELETE SET NULL,
    episode_id        INTEGER REFERENCES series_episodes(id) ON DELETE SET NULL,
    source_id         INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    source_name       TEXT,
    provider_kind     TEXT    NOT NULL CHECK (provider_kind IN ('vod', 'series')),
    provider_remote_id TEXT   NOT NULL,
    episode_stream_id TEXT,
    container_ext     TEXT,
    title             TEXT    NOT NULL,
    episode_title     TEXT,
    season            INTEGER,
    episode           INTEGER,
    resolution        TEXT,
    source            TEXT,
    quality_label     TEXT,
    audio_languages   TEXT,
    subtitle_languages TEXT,
    media_info        TEXT,
    priority          INTEGER NOT NULL DEFAULT 0,
    -- main: the file tracked in movies.file_path. alternate: an extra version
    -- stored in movie_alternates and never allowed to replace the main file.
    mode              TEXT    NOT NULL DEFAULT 'main',
    release_name      TEXT,
    reason            TEXT,             -- add | auto | manual | upgrade | retry
    status            TEXT    NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued','downloading','importing','imported',
                          'failed','cancelled')),
    bytes_done        INTEGER NOT NULL DEFAULT 0,
    bytes_total       INTEGER,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    error             TEXT,
    temp_path         TEXT,
    final_path        TEXT,
    -- Set by the web process when the user presses Cancel. The worker owns the
    -- actual download and notices this flag, either before it starts or from
    -- its progress loop. SQLite is the only channel between the two processes.
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    requested_at      INTEGER NOT NULL,
    started_at        INTEGER,
    finished_at       INTEGER,
    next_attempt_at   INTEGER,
    -- Schema v5 profile score for the grabbed candidate. score_details holds
    -- the per-criterion breakdown as JSON (see the contracts document).
    score             INTEGER,
    score_details     TEXT
);

CREATE INDEX IF NOT EXISTS idx_downloads_status
    ON downloads (status, next_attempt_at);
-- idx_downloads_queue is created in the version 2 migration: it references a
-- column that existing databases do not have yet, and schema.sql runs before
-- migrations.
CREATE INDEX IF NOT EXISTS idx_downloads_movie ON downloads (movie_id);
CREATE INDEX IF NOT EXISTS idx_downloads_episode ON downloads (episode_id);

-- Additional manual movie versions. The main `movies.file_path` stays the
-- profile-selected file; these are extra downloads pinned by the user and are
-- named with an [alternate] suffix so both can live in the same folder.
CREATE TABLE IF NOT EXISTS movie_alternates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id           INTEGER NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
    download_id        INTEGER REFERENCES downloads(id) ON DELETE SET NULL,
    source_id          INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    remote_id          TEXT    NOT NULL,
    name               TEXT,
    file_path          TEXT    NOT NULL,
    file_size          INTEGER,
    resolution         TEXT,
    source             TEXT,
    quality_label      TEXT,
    audio_languages    TEXT,
    subtitle_languages TEXT,
    media_info         TEXT,
    imported_at        INTEGER NOT NULL,
    UNIQUE (movie_id, source_id, remote_id)
);

CREATE INDEX IF NOT EXISTS idx_movie_alternates_movie
    ON movie_alternates (movie_id);


CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT    NOT NULL,       -- grabbed | imported | upgraded | failed | cancelled | deleted
    kind        TEXT,
    movie_id    INTEGER,
    series_id   INTEGER,
    episode_id  INTEGER,
    download_id INTEGER,
    title       TEXT    NOT NULL,
    message     TEXT,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_created ON history (created_at DESC);

CREATE TABLE IF NOT EXISTS scan_results (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    path               TEXT    NOT NULL UNIQUE,
    kind               TEXT    NOT NULL CHECK (kind IN ('movie', 'episode')),
    title              TEXT,
    year               INTEGER,
    season             INTEGER,
    episode            INTEGER,
    resolution         TEXT,
    tmdb_id            INTEGER,
    matched_movie_id   INTEGER REFERENCES movies(id) ON DELETE SET NULL,
    matched_series_id  INTEGER REFERENCES series(id) ON DELETE SET NULL,
    matched_episode_id INTEGER REFERENCES series_episodes(id) ON DELETE SET NULL,
    confidence         REAL,
    status             TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','imported','ignored')),
    message            TEXT,
    scanned_at         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_status ON scan_results (status);

-- Small JSON response cache for TMDB/TVDB and expiring tokens. Derived data;
-- excluded from backups and safe to delete at any time.
CREATE TABLE IF NOT EXISTS metadata_cache (
    key        TEXT    PRIMARY KEY,
    payload    TEXT    NOT NULL,
    fetched_at INTEGER NOT NULL
);

-- Normalised get_series_info responses. Also derived: it can be re-fetched.
CREATE TABLE IF NOT EXISTS provider_series_cache (
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    remote_id  TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (source_id, remote_id)
);

-- ffprobe results for provider streams, keyed by the same stable identity as
-- a download. Derived data: safe to clear, excluded from backups.
CREATE TABLE IF NOT EXISTS stream_probes (
    source_id          INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    provider_kind      TEXT    NOT NULL CHECK (provider_kind IN ('vod', 'series')),
    provider_remote_id TEXT    NOT NULL,
    episode_stream_id  TEXT    NOT NULL DEFAULT '',
    ok                 INTEGER NOT NULL,
    payload            TEXT    NOT NULL,
    probed_at          INTEGER NOT NULL,
    PRIMARY KEY (source_id, provider_kind, provider_remote_id, episode_stream_id)
);

CREATE TABLE IF NOT EXISTS task_state (
    name          TEXT PRIMARY KEY,
    last_started  INTEGER,
    last_finished INTEGER,
    status        TEXT    NOT NULL DEFAULT 'idle',
    message       TEXT,
    next_run      INTEGER
);

-- --------------------------------------------------------------------------
-- Background worker queue
--
-- The web process only inserts rows here and reads their status. A separate
-- worker process claims exactly one row at a time and runs its handler; that
-- worker is the only process that talks to providers, runs ffprobe/ffmpeg or
-- writes media files. Keeping the queue in SQLite means a restart cannot lose
-- a requested sync, search or download, and both processes see the same
-- progress without IPC.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS worker_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,
    payload         TEXT    NOT NULL DEFAULT '{}',
    -- queued | running | done | error | cancelled
    status          TEXT    NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'error', 'cancelled')),
    priority        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    -- Progress for the UI. `message` is the short human line; `progress_data`
    -- is a JSON object with job-specific fields (phase, detail, byte counts).
    message         TEXT,
    progress_data   TEXT,
    result          TEXT,
    error           TEXT,
    error_type      TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    -- Unix seconds. A retry can be scheduled for the future without holding
    -- the worker open.
    available_at    INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    started_at      INTEGER,
    finished_at     INTEGER,
    -- Active jobs only. The partial unique index below makes concurrent
    -- enqueues of the same sync/download/search collapse to one row.
    dedupe_key      TEXT
);

CREATE INDEX IF NOT EXISTS idx_worker_jobs_queue
    ON worker_jobs (status, priority DESC, available_at, id);

-- SQLite supports partial indexes, and they express the dedupe rule exactly:
-- a key may repeat after the previous job reached a terminal state.
CREATE UNIQUE INDEX IF NOT EXISTS idx_worker_jobs_dedupe
    ON worker_jobs (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');

-- One row. The web health check uses heartbeat_at to tell whether a worker is
-- actually running, rather than presenting a healthy UI over a dead queue.
CREATE TABLE IF NOT EXISTS worker_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    pid           INTEGER,
    started_at    INTEGER,
    heartbeat_at  INTEGER,
    current_job_id INTEGER,
    status        TEXT NOT NULL DEFAULT 'starting'
);

-- --------------------------------------------------------------------------
-- bworker: declarative automatic work (schema v5)
--
-- worker_jobs above is the imperative, durable execution queue. bworker_work
-- is the declarative schedule: one row per recurring/per-item unit. bworker
-- materialises due rows into worker_jobs and maps the job outcome back onto
-- the work row. The web process may read both, but only bworker claims jobs
-- or advances work.
--
-- entity_id is deliberately not a foreign key: work is polymorphic across
-- movies, series, episodes, sources and global tasks. Writers remove work when
-- the entity is deleted; bworker treats a missing entity as a clean no-op.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bworker_work (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Stable, human-readable identity. Convention is documented in
    -- docs/implementation/0.8.0-contracts.md; the UNIQUE constraint is what
    -- makes duplicate seeding impossible.
    work_key            TEXT    NOT NULL UNIQUE,
    work_type           TEXT    NOT NULL,
    entity_type         TEXT    NOT NULL
        CHECK (entity_type IN ('movie', 'series', 'episode', 'source', 'global')),
    entity_id           INTEGER,
    payload             TEXT    NOT NULL DEFAULT '{}',
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    -- The current per-unit stage. Stage names are per work type; see the
    -- contracts document. 'pending' is the reset/initial value.
    stage               TEXT    NOT NULL DEFAULT 'pending',
    -- pending | running | retry | failed_stopped | disabled | complete
    status              TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'retry', 'failed_stopped',
                          'disabled', 'complete')),
    failure_count       INTEGER NOT NULL DEFAULT 0,
    -- NULL with infinite_retry=1 means retry forever. A finite max_failures
    -- stops this unit (failed_stopped) without stopping other work.
    max_failures        INTEGER,
    infinite_retry      INTEGER NOT NULL DEFAULT 0 CHECK (infinite_retry IN (0, 1)),
    retry_delay_seconds INTEGER NOT NULL DEFAULT 60,
    backoff_multiplier  REAL    NOT NULL DEFAULT 2.0,
    next_run_at         INTEGER,
    -- Candidate-pipeline cursor JSON: {"epoch": "...", "offset": n,
    -- "batch": n, ...}. Opaque to everything except the pipeline.
    cursor              TEXT,
    -- Snapshot of the candidate the current search has chosen. Scores are
    -- integers from the profile algebra; identity columns mirror downloads.
    best_score          INTEGER,
    best_source_id      INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    best_remote_id      TEXT,
    best_episode_stream_id TEXT,
    -- Download currently owned by this work unit (download stage).
    current_download_id INTEGER REFERENCES downloads(id) ON DELETE SET NULL,
    -- Profile revision the current scan/cursor was scored against. A profile
    -- edit bumps revision and invalidates the scan.
    profile_revision    INTEGER,
    -- Web sets this to ask bworker to clear failure state/reset the unit;
    -- bworker consumes it at the loop boundary.
    reset_requested     INTEGER NOT NULL DEFAULT 0 CHECK (reset_requested IN (0, 1)),
    last_error          TEXT,
    -- Current attempt bounds; last_* are the previous attempt's summary.
    started_at          INTEGER,
    finished_at         INTEGER,
    last_started_at     INTEGER,
    last_finished_at    INTEGER,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);

-- Due-work scan: only non-running, enabled rows with a due time are candidates.
CREATE INDEX IF NOT EXISTS idx_bworker_work_due
    ON bworker_work (status, next_run_at)
    WHERE status IN ('pending', 'retry');
CREATE INDEX IF NOT EXISTS idx_bworker_work_enabled_due
    ON bworker_work (enabled, status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_bworker_work_entity
    ON bworker_work (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_bworker_work_type
    ON bworker_work (work_type, status);

-- Per-item durable history. Writers prune to the newest 15 rows per
-- (entity_type, entity_id) in the same transaction as the insert.
CREATE TABLE IF NOT EXISTS bworker_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- SET NULL: deleting a work row must not erase the item's history.
    work_id     INTEGER REFERENCES bworker_work(id) ON DELETE SET NULL,
    work_type   TEXT    NOT NULL,
    entity_type TEXT    NOT NULL
        CHECK (entity_type IN ('movie', 'series', 'episode', 'source', 'global')),
    entity_id   INTEGER,
    stage       TEXT    NOT NULL,
    -- started | progress | succeeded | failed | skipped | info
    status      TEXT    NOT NULL,
    message     TEXT,
    progress    TEXT,
    started_at  INTEGER,
    finished_at INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bworker_events_entity
    ON bworker_events (entity_type, entity_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_bworker_events_work
    ON bworker_events (work_id, id DESC);

-- Singleton bworker runtime state. The paused flag is the authority; a pause
-- request finishes the current unit before bworker stops claiming new work.
CREATE TABLE IF NOT EXISTS bworker_state (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    paused             INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    current_work_id    INTEGER REFERENCES bworker_work(id) ON DELETE SET NULL,
    current_work_label TEXT,
    started_at         INTEGER,
    heartbeat_at       INTEGER,
    -- starting | idle | running | paused | stopped | error
    status             TEXT    NOT NULL DEFAULT 'starting',
    updated_at         INTEGER
);

INSERT OR IGNORE INTO bworker_state (id, paused, status) VALUES (1, 0, 'starting');

-- Per-work-type retry/toggle policy. bworker_work carries a copy for a unit
-- that started under an older policy; new units copy the policy here.
-- activity_cleanup is always enabled and is never paused.
CREATE TABLE IF NOT EXISTS bworker_policies (
    work_type           TEXT    PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    max_failures        INTEGER,
    infinite_retry      INTEGER NOT NULL DEFAULT 0 CHECK (infinite_retry IN (0, 1)),
    retry_delay_seconds INTEGER NOT NULL DEFAULT 60,
    backoff_multiplier  REAL    NOT NULL DEFAULT 2.0,
    max_delay_seconds   INTEGER,
    updated_at          INTEGER
);

INSERT OR IGNORE INTO bworker_policies
    (work_type, enabled, max_failures, infinite_retry,
     retry_delay_seconds, backoff_multiplier, max_delay_seconds)
VALUES
    ('movie_search',     1, NULL, 1, 21600,  1.0, 21600),
    ('series_search',    1, NULL, 1, 14400,  1.0, 14400),
    ('episode_search',   1, NULL, 1, 14400,  1.0, 14400),
    ('playlist_sync',    1, NULL, 1,   300,  2.0,  3600),
    ('metadata_refresh', 1, NULL, 1, 604800, 1.0, 604800),
    ('download',         1,    3, 0,    60,  2.0,   900),
    ('probe',            1,    3, 0,   300,  1.0,   300),
    ('library_scan',     1,    3, 0,   900,  2.0,   900),
    ('activity_cleanup', 1, NULL, 1,    60,  2.0,  3600);

-- One catalogue scan session per work/epoch. epoch is opaque and changes when
-- the underlying provider catalogue changes; a changed epoch invalidates the
-- old scan so paging can never mix two catalogues.
CREATE TABLE IF NOT EXISTS bworker_scan (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id            INTEGER NOT NULL REFERENCES bworker_work(id) ON DELETE CASCADE,
    epoch              TEXT    NOT NULL,
    -- Fingerprint of the query/title identity inputs that produced this scan.
    -- When the title, year, season/episode identity or search query changes,
    -- the scan is invalidated and a new epoch/fingerprint is started instead
    -- of paging results from the old query.
    query_fingerprint  TEXT,
    status             TEXT    NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'complete', 'invalidated')),
    cursor             TEXT,
    -- exhausted=1 means source paging reached the end for this epoch. It is
    -- distinct from complete: a scan may stop early (batch limit) without
    -- exhausting the provider result set.
    exhausted          INTEGER NOT NULL DEFAULT 0 CHECK (exhausted IN (0, 1)),
    candidates_seen    INTEGER NOT NULL DEFAULT 0,
    -- Provider entries whose parsed title matched the target identity,
    -- before deduplication.
    candidates_matched INTEGER NOT NULL DEFAULT 0,
    -- Deduplicated candidate rows retained in bworker_candidates for this
    -- scan. A03 updates this on every upsert; it is the count used to decide
    -- whether another page is needed.
    matched_count      INTEGER NOT NULL DEFAULT 0,
    candidates_probed  INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    UNIQUE (work_id, epoch)
);

CREATE INDEX IF NOT EXISTS idx_bworker_scan_work
    ON bworker_scan (work_id, status);
-- idx_bworker_scan_fingerprint is created by the v5 migration, after it has
-- added query_fingerprint to databases created by a partial v5 build. An
-- index on a column that does not exist yet would make schema.sql fail before
-- the migration can repair the table.

-- Candidates discovered while paging the catalogue. Derived data: excluded
-- from backups and safe to rebuild. Identity mirrors a download/probe:
-- (source_id, provider_kind, provider_remote_id, episode_stream_id).
CREATE TABLE IF NOT EXISTS bworker_candidates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id            INTEGER NOT NULL REFERENCES bworker_scan(id) ON DELETE CASCADE,
    work_id            INTEGER NOT NULL REFERENCES bworker_work(id) ON DELETE CASCADE,
    source_id          INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    -- Catalogue item the candidate was matched against, when known. Kept
    -- nullable because a cached catalogue entry can disappear between paging
    -- and matching; ON DELETE SET NULL preserves the candidate record.
    item_id            INTEGER REFERENCES items(id) ON DELETE SET NULL,
    provider_kind      TEXT    NOT NULL CHECK (provider_kind IN ('vod', 'series')),
    provider_remote_id TEXT    NOT NULL,
    episode_stream_id  TEXT    NOT NULL DEFAULT '',
    release_name       TEXT,
    resolution         TEXT,
    source             TEXT,
    score              INTEGER,
    score_details      TEXT,
    -- How item_id was matched and its confidence; NULL when no identity match.
    match_kind         TEXT CHECK (match_kind IS NULL OR
        match_kind IN ('tmdb', 'imdb', 'title')),
    match_score        REAL,
    eligible           INTEGER NOT NULL DEFAULT 0 CHECK (eligible IN (0, 1)),
    target_met         INTEGER NOT NULL DEFAULT 0 CHECK (target_met IN (0, 1)),
    needs_upgrade      INTEGER NOT NULL DEFAULT 0 CHECK (needs_upgrade IN (0, 1)),
    -- pending | probing | probed | selected | rejected | failed
    status             TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'probing', 'probed', 'selected',
                          'rejected', 'failed')),
    probed_at          INTEGER,
    created_at         INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    UNIQUE (scan_id, source_id, provider_kind,
            provider_remote_id, episode_stream_id)
);

-- Candidate paging: the next pending row and the best-scored finished row.
CREATE INDEX IF NOT EXISTS idx_bworker_candidates_scan
    ON bworker_candidates (scan_id, status, score DESC, id);
CREATE INDEX IF NOT EXISTS idx_bworker_candidates_work
    ON bworker_candidates (work_id, status, score DESC, id);
-- idx_bworker_candidates_item is created by the v5 migration for the same
-- schema.sql-before-migration reason as idx_bworker_scan_fingerprint.
