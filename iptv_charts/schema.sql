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
    max_connections     INTEGER,
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
    ('default_tab',       'all');
