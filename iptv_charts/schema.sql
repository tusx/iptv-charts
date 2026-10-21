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
    ('probe_search_budget',   '60');

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
    created_at  INTEGER NOT NULL
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
    main_remote_id        TEXT
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
    search_message     TEXT
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
    next_attempt_at   INTEGER
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
