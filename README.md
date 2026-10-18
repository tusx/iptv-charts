# IPTV Charts

A local IPTV playlist browser that is also a movie and series manager. Point it
at a playlist — either a direct **M3U URL** or **Xtream Codes** credentials —
and it builds a searchable catalog you can browse, filter and play from. Add
movies and series from TMDB, let it find the matching VODs in your playlists,
download them, and file them into `Movies/` and `Series/` with Radarr/Sonarr-style
names.

Everything runs on your own machine, in one container or one virtualenv. There
is no remote server, no account, no indexer, no torrent client, and no public
API for other software. Provider catalogs and metadata keys stay local.

## What works today

**Playlists**

- **Add a playlist** as either an M3U URL or Xtream Codes credentials, with a
  **connection test** that reports what it found — account status, expiry,
  connection limits, channel estimate — before anything is saved.
- **Ingest** the whole catalog into SQLite: streaming parse, batched inserts,
  cancellable, and safe to re-run.
- **Real progress bars** — bytes against a known total, not a spinner.
- **Update** one playlist or **Update all**, with a Cancel button.
- **Staleness reminders**: a playlist not refreshed in 24h says so, with a
  one-click refresh.
- **Remove** a playlist and its downloaded catalog in one click.
- **Browse** any playlist with full-text **search**, kind tabs (Live / VOD /
  Series), a category filter, and sorting by name, recency or rating, paging
  as you scroll.
- **Play** in the browser — a plain video element, plus hls.js for HLS — or
  hand the stream to a player on your machine with one click.
- **Choose your player**: mpv, VLC (native or Flatpak), or a command of your
  own, from the settings page.
- **Copy link** for any item, or any individual episode.
- **Series** open an episode list with per-episode play, copy and player
  actions.

**Movies and series**

- **Add from TMDB** with posters, overviews, ratings and alternative titles.
  Series use TMDB for search, art and overviews, and TVDB for episode names,
  numbers and air dates when a TVDB key is configured (TMDB episodes are the
  fallback).
- **Monitor** movies and individual episodes or entire seasons, and choose a
  **quality profile** (resolution tiers and a cutoff).
- **Automatic search** of every playlist's IPTV catalog for a matching VOD or
  provider series episode. Matching understands the way real catalogs name
  things: `ES - Un talento único [[4K-Q]-Q] (2026)`, `EN - Wonder [4K-Q] (2017)
  JULIA ROBERTS`, `GR - Supernatural [1080p]` with no year, and `[X][WatchMDH]`
  group tags.
- **Manual search** when automatic matching misses, with a query override and
  a list of every candidate release.
- **Built-in download queue** with progress, cancel, retry, history and a
  durable SQLite queue that survives restarts. Direct HTTP downloads resume;
  HLS playlists are remuxed with ffmpeg.
- **Import and rename** completed files into the media folders. The default
  names are Radarr/Sonarr-style and fully editable.
- **Quality upgrades** replace an existing file when a better allowed
  resolution appears and the profile's cutoff has not been reached.
- **Calendar** of episode air dates and movie release dates, and **Wanted**
  views for missing movies, aired monitored episodes, and files below cutoff.
- **Library scan**: find files already on disk, match them to TMDB, review the
  proposals, and import the ones you confirm. A scan also returns files that
  have disappeared to "missing".
- **Backup and restore** as a ZIP of everything except the huge IPTV catalog.
- **Scheduled tasks** for missing-item searches, metadata refreshes and
  playlist refreshes, plus manual runs from the Activity page.

## Still to build

- The **info/stats page** with charts: account details, category breakdown,
  and when content was added. Account and server data is already fetched and
  stored on every sync, so this page is presentation only.
- A public API for other software is deliberately out of scope, as are
  indexers, torrent clients, notifications, custom formats and lists.

## Quick start

Two entry points, for two different jobs.

### Trying out a change — `setup.test.sh`

Runs the app directly on this machine from a virtualenv, with auto-reload.
Edit a file, save, refresh the browser. This is the fast loop.

```bash
./setup.test.sh              # start with auto-reload
./setup.test.sh --check      # run all five test suites and exit
./setup.test.sh --no-reload  # run without the reloader
./setup.test.sh --port 9000  # different port
```

It creates the virtualenv and installs dependencies on first run, and only
reinstalls when `requirements.txt` actually changes. In dev mode the media
library defaults to `~/Media/IPTV Charts`; override it with
`IPTV_CHARTS_MEDIA_DIR=/some/path`.

### Running it for real — `setup.deploy.sh`

Builds a container image and runs it under Podman with a persistent volume and
a bind-mounted media folder.

```bash
./setup.deploy.sh              # build, deploy, open the browser
./setup.deploy.sh --build      # build the image only
./setup.deploy.sh --logs       # follow container logs
./setup.deploy.sh --status     # container, volume, media and bridge state
./setup.deploy.sh --stop       # stop and remove the container, keep data
./setup.deploy.sh --shell      # shell inside the running container
./setup.deploy.sh --destroy    # remove container AND delete app data
```

Overridable via environment: `IPTV_PORT`, `IPTV_IMAGE`, `IPTV_VOLUME`,
`IPTV_CONTAINER`, and `IPTV_MEDIA_DIR`.

`IPTV_MEDIA_DIR` defaults to `~/Media/IPTV Charts`. The deploy script creates
`Movies/` and `Series/` inside it and mounts the whole folder at `/media` in
the container. `--destroy` deletes the container and the app volume, never the
media folder.

## Where your data lives

**The two modes use different databases.** This is deliberate — the container
uses a Podman named volume so it survives rebuilds, while the dev mode uses
the ordinary XDG location on the host. A playlist added in one will not appear
in the other.

| | Dev mode (`setup.test.sh`) | Container (`setup.deploy.sh`) |
|---|---|---|
| Database | `~/.local/share/iptv-charts/` | volume `iptv-charts-data`, mounted at `/data` |
| Runtime prefs | `~/.config/iptv-charts/` | `/config` inside the container |
| Media library | `~/Media/IPTV Charts/` (override with `IPTV_CHARTS_MEDIA_DIR`) | host `IPTV_MEDIA_DIR`, mounted at `/media` |
| Play queue | n/a — the player is launched directly | `$XDG_RUNTIME_DIR/iptv-charts/` |

To inspect or back up the container's database:

```bash
podman volume inspect iptv-charts-data --format '{{.Mountpoint}}'
```

The database holds provider credentials and metadata API keys in plain text,
like any local media manager's config. Files are owned by your user and
readable only by you.

## Playback

**In the browser.** Always available, no setup, no extra dependencies. A plain
`<video>` element plays what the browser can play, and hls.js (vendored locally,
so nothing is fetched at runtime) covers HLS, which no browser outside Safari
decodes on its own. Live channels are usually HLS, so they normally work; raw
MPEG-TS and MPEG-2/AC-3 streams do not, and the browser is left to say so in its
own controls rather than being covered up by an error of ours.

**In a player on your machine.** Every item and every episode has an
"External player" button that hands the stream to whatever you have chosen on
the settings page. In the container, a small host-side watcher runs the player:

```bash
./scripts/player-bridge.sh --daemon   # start it in the background
./scripts/player-bridge.sh --status   # is it running? which players does it see?
./scripts/player-bridge.sh --stop     # stop it
```

`setup.deploy.sh` restarts the bridge on every deploy. It is supervised: if the
worker exits, it is started again, and `--status` finds it even if its pid file
was lost. It publishes a heartbeat and a list of the players installed on the
host, which is how the settings page can offer them. Browser playback is
unaffected if the bridge is not running.

## Movies and series

### TMDB and TVDB keys

Open **Settings → Metadata providers** and paste a TMDB API key. A v3 API key
or a v4 read access token both work. TMDB provides movie and series search,
posters, overviews, ratings and alternative titles.

A **TVDB v4 API key** is optional. When present, episode names, numbers and air
dates come from TVDB; if the key is missing or TVDB is unreachable, the app
falls back to TMDB season data. A subscriber PIN can be supplied if your TVDB
key needs one.

Keys are stored in the app database and are included in backups. They are
shown as "configured / not configured" and are never echoed back into the page.
Tests can run fully offline with `IPTV_CHARTS_TMDB_BASE` and
`IPTV_CHARTS_TVDB_BASE` pointed at a fixture server.

### Adding, monitoring and searching

On **Movies** or **Series**, use **Add**. A title search runs against TMDB;
choose the right result, a quality profile, whether it should be monitored, and
whether to search immediately. Adding also captures alternative titles, which
matters because a provider may call a film `Un talento único` while TMDB's
English title is `A Unique Talent`.

Automatic search looks across every playlist in the catalog, not just one. It
normalises names (diacritics, leading `ES -` style locale prefixes, quality
tags, group tags, cast noise), extracts a release year and resolution, and
scores the result against the TMDB/TVDB title and its aliases. A release is
only grabbed automatically at a high confidence; everything else remains
available through **Manual search**, where a different query can be typed and
any candidate can be grabbed. The app never accepts a URL from the browser for
a download — it re-resolves the provider item and episode server-side.

For series, each missing monitored episode is matched by season and episode
number against the provider's own episode list. The provider's
`get_series_info` response is cached for a few hours. Episodes that have not
aired yet are not searched. A **Search missing** button and a scheduled task
cover the rest.

M3U playlists do not group episodes into series, so **series management and
episode downloads require an Xtream playlist**. Movies work with both. TVDB
and TMDB numbering can differ from a provider's numbering; when it does,
manual search is the escape hatch.

### Quality profiles and upgrades

**Settings → Quality profiles** (or the profiles link) edits profiles. Each
profile has:

- an ordered list of allowed resolutions: `2160p`, `1080p`, `720p`, `480p`,
  `unknown`;
- an optional cutoff — once the file is at or above it, no further upgrades;
- an upgrades switch.

Two profiles are seeded: **HD** (1080p/720p/480p/unknown, cutoff 1080p) and
**4K** (2160p/1080p/720p, cutoff 2160p). The add dialog lets you choose one,
so picking a resolution is a single choice.

A profile that is in use cannot be deleted until its movies and series are
reassigned, and at least one profile always remains.

### Downloads and naming

The built-in queue lives in SQLite and is processed by 1–3 worker threads
(default 1, to respect provider connection limits). It pauses new jobs while a
playlist sync is running. A download can be cancelled, retried after failure,
and inspected in **Activity**.

- **Direct files** are streamed to `media/.incomplete/` with Range resume,
  Content-Length checks, progress persisted to the database, and a disk-space
  preflight.
- **HLS** (`.m3u8`) is remuxed with ffmpeg. The container image installs
  ffmpeg; in dev mode it is optional, but HLS downloads require it.
- A downloaded file is checked before import: an HTML login/error page served
  with HTTP 200 is rejected, and ffprobe (when available) must recognise the
  file as video. Its probed frame height becomes the stored resolution.
- Imports are an atomic rename on the same filesystem, followed by a database
  update and a history event. An upgrade deletes the previous recorded file
  only after the new one is in place.

Default names:

| | Template |
|---|---|
| Movie folder | `{title} ({year})` |
| Movie file | `{title} ({year}) [{resolution}]` |
| Series folder | `{title} ({year})` |
| Season folder | `Season {season:00}` |
| Episode file | `{series} - S{season:00}E{episode:00} - {episode_title} [{resolution}]` |

All five are editable in **Settings → Media management**, with a live preview.
Tokens are `{title}`, `{series}`, `{year}`, `{season}`, `{episode}`,
`{episode_title}` and `{resolution}`; `{season:00}` and `{episode:00}` pad the
number. A template describes one path component, so a separator is refused.
Names are sanitised (illegal characters, reserved device names, length) and
every final path is confined to the media root. Unmanaged files are never
overwritten; a ` (2)` suffix is used instead.

### Automation, calendar and wanted

The scheduler runs inside the app process (started by `run.py`; disabled with
`IPTV_CHARTS_SCHEDULER=0`), with one task at a time:

- **Search missing** — every 6 hours by default. Monitored missing movies and
  aired, monitored episodes are matched again; files below cutoff are upgraded
  when upgrades are enabled.
- **Refresh metadata** — weekly by default. Refreshes TMDB/TVDB data and episode
  lists. New episodes inherit the series' monitoring.
- **Refresh playlists** — daily by default (`0` disables it). Playlists older
  than the interval are synced so new VODs become available to search. This is
  the equivalent of Radarr's indexer refresh.
- **Scan library** — manual only.

All four can be run from **Activity**, which also shows the queue, recent
failures, history and the next scheduled run. **Calendar** shows episodes and
movie release dates by day; **Wanted** lists missing movies, missing aired
episodes and files below cutoff.

### Library scan

**Activity → Library scan** (or **Settings → Library scan**) walks
`Movies/` and `Series/`, skips partial downloads and symlinks that escape the
root, parses titles, years, `SxxEyy` markers and resolutions, and probes files
with ffprobe when available. It proposes matches using existing library rows
first, then TMDB; nothing is imported without review. **Import** creates the
library entry if needed and attaches the existing file without moving it.
Low-confidence files stay unmatched.

A scan also reconciles the other direction: if a tracked file has disappeared,
the item is returned to "missing" and a history event records the path. The
library entry is not silently deleted.

## Backup and restore

**Settings → Backup and restore** exports a ZIP containing:

```
manifest.json
tables/sources.json          playlist credentials and account snapshots
tables/settings.json         including metadata API keys and player choice
tables/quality_profiles.json
tables/movies.json
tables/series.json
tables/series_episodes.json
tables/downloads.json
tables/history.json
tables/scan_results.json
tables/task_state.json
```

It deliberately **excludes** `items`, `categories` and the search index (the
IPTV catalog, which is large and rebuilt by a sync), plus metadata/provider
caches and images. Restore validates the manifest, schema version, table names,
row counts, JSON shapes and size limits, then replaces the app tables in one
transaction. It refuses while a sync, task or download is active. Restored file
paths and naming templates are re-validated; paths outside the current media
root are cleared. Playlist syncs are queued after the restore, because the
catalog is not in the ZIP.

**The ZIP contains provider credentials and API keys in plain text.** Keep it
private.

## Settings

`/settings` (also in the top bar) has cards for:

- **External player** and a **custom command** with `{url}`, `{title}`,
  `{user_agent}` and `{referrer}` placeholders. A custom command runs on the
  host with your privileges, exactly as written; nothing is passed through a
  shell.
- **Metadata providers**: TMDB and TVDB keys, optional PIN, language, and
  connection tests.
- **Quality profiles** (link to the editor).
- **Media management**: media paths and the five naming templates.
- **Downloads**: concurrency, attempts, and whether ffmpeg is available.
- **Automation**: search, metadata and playlist-refresh intervals.
- **Library scan** and **Backup and restore**.

### Logs

`podman logs -f iptv-charts` (or `./setup.deploy.sh --logs`) shows startup, the
mode it is in, every write, every failure, playback requests, queue events,
download starts and imports, task runs, and metadata failures. Reads are left
out on purpose — the sync poll runs every second and would bury everything
else. `IPTV_CHARTS_LOG_LEVEL=DEBUG` adds the exact argv handed to a player and
the ffmpeg command line (with credentials in URLs redacted).

The host bridge keeps its own log next to the queue:
`$XDG_RUNTIME_DIR/iptv-charts/play-bridge.log`.

## Design constraints

**It has to stay light on RAM.** Provider catalogs are large — a Xtream
`get_vod_streams` response can be 200 MB of JSON covering 200,000 items.
Loading that naively peaks at about a gigabyte of Python objects. Instead the
sync spools the response to disk, parses it incrementally with ijson's C
backend, and inserts in batches, so peak memory stays flat regardless of
catalog size. The catalog also deliberately does **not** store the provider's
raw JSON per item, which would be the single largest cost in the design.
Media downloads stream to disk and update progress in place; they never sit in
memory.

**Library rows never reference a catalog item id.** A playlist sync deletes and
re-inserts `items` with new AUTOINCREMENT ids. A library grab therefore records
`(source_id, kind, remote_id)` and the episode stream id, and the downloader
re-resolves the URL when the download starts. If the provider removed the item,
the download fails with a clear message instead of fetching the wrong stream.

**Catalog matching is best-effort by design.** IPTV names are not release
names: they contain locale prefixes, quality tags, cast names and no year at
all. The matcher strips only recognised noise, uses TMDB alternative titles,
and scores what remains; a threshold separates automatic grabs from manual
suggestions. When it is unsure, the item stays missing rather than downloading
the wrong film.

**Failures must be visible.** A panel returning an HTML error page, an empty
body, `auth: 0` with `status: "Active"`, or a JSON object where an array was
expected are all handled explicitly. A download that dies says why in the
queue and in the log; a provider page is not saved as a video.

**Syncing must not disturb reading.** The catalog is written in WAL mode, so a
browse request during a sync never blocks it and never sees a half-written
row. A sync is re-runnable; the app does not claim an atomic catalog swap, and
a cancelled re-sync can leave a partial catalog until the next successful sync.
The download queue deliberately pauses while a sync runs: provider accounts
often limit concurrent connections, and one worker draining the queue already
uses one. The scheduler defers searches during a sync for the same reason.

**Schema changes need a migration.** `schema.sql` is all `IF NOT EXISTS`, which
adds new tables but cannot add a column to an existing table. `db.migrate`
uses `PRAGMA user_version` for that; version 1 adds per-stream M3U headers,
seeds quality profiles and task state. Existing databases are upgraded at
startup.

**Search has to be written carefully.** The natural way to search an FTS index
with a filter —

```sql
FROM items_fts f JOIN items i ON i.id = f.rowid
WHERE items_fts MATCH ? AND i.source_id = ?
```

— measured **20 seconds** against a 313k-item catalog, because SQLite chose to
scan every row of the source and probe the FTS index once per row: 313,247
probes to find 866 matches. Writing it as a `CROSS JOIN` removes the planner's
ability to reorder, pinning the FTS match as the outer loop, and the same query
runs in **3 ms**. There is a regression test asserting the query plan. The
library matcher uses the same shape without the source filter.

## Development

```bash
./setup.test.sh --check
```

Runs five suites, ~450 checks, none of which touch the internet:

| Suite | Covers |
|---|---|
| `tests/smoke.py` | Parsers, schema, migrations, FTS, streaming, player commands, quality/naming/matching, request guards |
| `tests/integration.py` | Real ingest against a local fixture provider |
| `tests/bridge.py` | The queue protocol, against the real host bridge and a fake player |
| `tests/endtoend.py` | The HTTP API and rendered pages, via Flask's test client |
| `tests/manager.py` | Fake TMDB/TVDB, catalog matching, downloads/imports, upgrades, cancel/retry, scan, backup/restore |

They exist to pin down the specific traps this codebase is built around:
quoted commas in M3U titles, diacritic-folded search, the `stream_id` vs `num`
distinction, progress accounting under gzip, the silent-zero-items failure when
a panel returns an object instead of an array, cancellation leaving the
previous catalog intact, resolution/cutoff decisions, and the provider/TMDB
naming shapes that real catalogs contain.

`tests/fixture_server.py` runs the fake provider standalone, which is how the
container is exercised end to end.

## Project layout

```
run.py               entry point (waitress; --dev for the Flask reloader)
setup.test.sh        live dev build
setup.deploy.sh      container build and deploy
Containerfile        image definition (includes ffmpeg for downloads)
iptv_charts/
  app.py             Flask factory, page routes, request guards
  api.py             JSON endpoints: probe, create, sync, status, settings
  manager_api.py      library, metadata, queue, task, scan, backup, profile API
  manager_web.py      Movies, Series, Calendar, Wanted, Activity, Scan, Profiles pages
  config.py          path resolution, port selection, media folders
  db.py              SQLite connections, schema, migrations, bulk-load tuning
  sync.py            the ingest engine, progress state, cancellation
  sources.py         source CRUD and credential probing
  items.py           catalog search and stream URL construction
  player.py          which player to use, how to run it, host bridge client
  metadata.py        provider selection, cache, image proxy
  matching.py        provider name normalisation and release scoring
  quality.py         resolution tiers, profiles, cutoff decisions
  naming.py          templates, sanitisation, path confinement
  library.py         movies/series CRUD, episode sync, search/grab, import
  downloader.py      durable queue, HTTP/HLS downloads, progress, recovery
  scheduler.py       periodic tasks and manual task runs
  scan.py            library scan/review/import
  backup.py          logical ZIP export and restore
  clients/
    m3u.py           streaming M3U parser and fetcher
    xtream.py        Xtream Codes API client
    tmdb.py          TMDB client and response normalisation
    tvdb.py          TVDB v4 login and episode client
    streaming.py     shared spool/progress primitives
  templates/         Jinja templates
  static/            CSS, JS, vendored hls.js
scripts/
  player-bridge.sh   host-side watcher that plays streams for the container
tests/
  smoke.py           offline unit checks
  integration.py     ingest against a local fixture provider
  endtoend.py        HTTP API and rendered pages
  manager.py         library/download/scan/backup flows against fake providers
  bridge.py          the app/bridge queue contract
  fixture_server.py  the fixture provider, runnable standalone
```

## Security notes

The server binds **loopback only**. It stores provider credentials and metadata
API keys, so it must never be reachable from the network. In the container case
the published port is likewise bound to `127.0.0.1` on the host.

There is deliberately **no authentication** — it is a single-user local app,
and a login step or token would only get in the way. Two checks stand in for
one. A request's `Host` header must name loopback, which rejects a page on
another site resolving to `127.0.0.1` and talking to the app under its own
hostname (DNS rebinding). And anything that changes state must come from
something the app served: a cross-site `Sec-Fetch-Site`, or an `Origin` naming
another host, is refused. The backup restore endpoint accepts a raw ZIP but is
still a write, so the same guard applies.

Stream URLs from an M3U playlist and custom player commands are treated as
untrusted. Download URLs must be `http` or `https` and are rebuilt from the
provider identity, not accepted from the browser; player commands run without
a shell; the host bridge re-checks what the app sends it. Downloads and image
requests never become a generic URL fetcher: images only come from the TMDB
image host through a validated path, and provider headers are stripped of
control characters before being handed to requests or ffmpeg.

Paths are confined to the media root. Naming templates are validated when
saved and again when restored from a backup; metadata titles are sanitised;
symlinks that escape the media root are skipped by the scanner; a downloaded
file is only deleted if its path is the one recorded in the database and is
inside the media library.

**Backups contain secrets.** They are loopback-only downloads, but the ZIP
itself contains provider credentials and API keys. Store it accordingly.

Worth knowing: because there is no token, any web page you have open can still
*read* from `http://127.0.0.1:<port>/`, and can issue writes that browsers
classify as same-site. That is an accepted trade-off here, not an oversight. If
you ever expose this beyond loopback, that calculus changes and a token should
come back.

## Requirements

- Python 3.11+ (developed against 3.13)
- Podman, for the container path only
- `mpv` or `vlc`, entirely optional
- `ffmpeg` for HLS downloads and ffprobe resolution checks; the container image
  installs it, while dev mode works without it for direct files
- A TMDB API key for Movies and Series; a TVDB API key is optional

On Debian/Ubuntu, if `python3 -m venv` fails with `No module named 'ensurepip'`:

```bash
sudo apt install python3-venv python3-full
```

## License

MIT
