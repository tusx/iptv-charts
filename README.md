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

**Home**

- A three-card dashboard for **IPTV**, **Movies** and **Series**, with the
  actions that belong to each area: playlist add/update/browse/edit/remove and
  update-all on the IPTV card; open/add/calendar/wanted/search-missing on the
  library cards. Settings is no longer a directory of every feature.

**Playlists**

- **Add a playlist** as either an M3U URL or Xtream Codes credentials, with a
  **connection test** that reports what it found — account status, expiry,
  connection limits, channel estimate — before anything is saved.
- **Edit a playlist** on its own page: name, M3U URL or Xtream server,
  username, password, user agent, referrer and connection limit. A Test
  connection button checks unsaved values; changing provider identity clears
  the old catalog and queues a fresh sync. Passwords are never rendered back
  to the page.
- **Ingest** the whole catalog into SQLite: streaming parse, batched inserts,
  cancellable, and safe to re-run.
- **Real progress bars** — bytes against a known total, not a spinner.
- **Update** one playlist or **Update all**, with a Cancel button.
- **Staleness reminders**: a playlist not refreshed in 24h says so, with a
  one-click refresh.
- **Set a connection limit** when adding a playlist, or edit it on the playlist
  card. Leave it blank to use the provider's reported limit, defaulting to 1
  when the provider does not report one. The limit is enforced across syncs,
  metadata calls, probes and downloads.
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
- **Monitor** movies and individual episodes or entire seasons, with
  **quality profiles** that order resolution and source tiers and can require
  original audio or specific subtitle languages.
- **Probe before download**: ffprobe reads the provider URL first and reports
  the real resolution, video codec/HDR, every audio track and language, every
  embedded subtitle track, duration and size. The download starts only when
  those facts satisfy the quality profile. Probe results are cached per stream.
- **Automatic search** of every playlist's IPTV catalog for a matching VOD or
  provider series episode. Matching understands the way real catalogs name
  things: `ES - Un talento único [[4K-Q]-Q] (2026)`, `EN - Wonder [4K-Q] (2017)
  JULIA ROBERTS`, `GR - Supernatural [1080p]` with no year, and `[X][WatchMDH]`
  group tags.
- **Manual search** when automatic matching misses, with a query override, a
  list of every candidate release, and the probed stream facts next to each.
- **Movie candidate workspace**: every matching VOD, its cached ffprobe facts
  (resolution, video/HDR, audio and subtitle tracks, size), and the profile
  decision in one view. Probe one or all candidates without leaving the page.
- **Pin a candidate**: **Only this** overrides the profile and stops other
  attempts; **Also download** keeps the profile pick and adds the pinned
  candidate as an extra version. Pins persist and are honoured by scheduled
  searches. Additional versions are listed and removable on the movie page.
- **Built-in download queue** with progress, cancel, retry, history, editable
  **priority**, and a durable SQLite queue that survives restarts. Direct HTTP
  downloads resume; HLS playlists are remuxed with ffmpeg without transcoding.
- **Per-playlist connection limits** enforced across playlist syncs, metadata
  calls, probes and downloads. The background worker runs one job at a time,
  so a season downloads one episode after another regardless of playlist
  count.
- **Import and rename** completed files into the media folders. The default
  names are Radarr/Sonarr-style and fully editable.
- **Quality upgrades** replace an existing file when a better allowed tier
  appears and the profile's cutoff has not been reached.
- **Radarr/Sonarr-style add choices**: minimum availability for movies
  (announced, in cinemas, or released), monitor all/future/first/latest/no
  episodes for series, and what to do when a new season is discovered.
- **Section-scoped Library / Wanted / Calendar tabs** under Movies and Series.
  Movie Wanted lists only movies; Series Wanted lists only episodes; the
  calendars are scoped the same way. No global Wanted/Calendar clutter in the
  top bar.
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
- Advanced Radarr/Sonarr options that do not fit a single provider-backed
  downloader are still out of scope: score-based custom formats, delay
  profiles, tags, a mass editor, notifications, import lists, indexers and
  torrent clients. Source, audio and subtitle constraints cover the common
  "custom quality" cases; a public API for other software is also deliberately
  out of scope.

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
reinstalls when `requirements.txt` actually changes. It starts `worker.py`
beside the reloading web process; Ctrl+C stops both. Running `python run.py`
by hand starts HTTP only, so start `python worker.py` in another terminal if
you do that. In dev mode the media library defaults to
`~/Media/IPTV Charts`; override it with `IPTV_CHARTS_MEDIA_DIR=/some/path`.

### Running it for real — `build.deploy.sh`

Builds a container image and runs it under Podman or Docker with persistent
volumes and a bind-mounted media folder. When both runtimes are installed you
are asked which to use; `--runtime podman|docker` (or `IPTV_RUNTIME`) skips the
prompt. Supervisor keeps nginx (the loopback-published public listener),
gunicorn (the web process), bworker (background work) and Valkey (the activity
log) running in the one container.

```bash
./build.deploy.sh              # build, deploy, open the browser
./build.deploy.sh --build      # build the image only
./build.deploy.sh --logs       # follow container logs
./build.deploy.sh --status     # container, volume, media and bridge state
./build.deploy.sh --stop       # stop and remove the container, keep data
./build.deploy.sh --shell      # shell inside the running container
./build.deploy.sh --destroy    # remove container AND delete app data
```

For a container built from the working tree without touching the production
container or its data, use `./deploy.dev.build.sh` (image
`iptv-charts-dev`, port 8771, its own volumes; `--build`, `--logs` and
`--stop` are supported).

Overridable via environment: `IPTV_RUNTIME`, `IPTV_PORT`, `IPTV_IMAGE`,
`IPTV_VOLUME`, `IPTV_CONFIG_VOLUME`, `IPTV_CACHE_VOLUME`, `IPTV_CONTAINER`,
and `IPTV_MEDIA_DIR`.

`IPTV_MEDIA_DIR` defaults to `~/Media/IPTV Charts`. If the folder already
contains a movie library (`Movies`/`movies`/`movie`) or a series library
(`Series`/`series`/`tv`/`tv shows`/`tv-shows`), that folder is used as-is
(case-insensitively); `Movies/` and `Series/` are created only when neither
exists. The whole folder is mounted at `/media` in the container, and the app
is told which subfolder to write to. `--destroy` deletes the container and the
app volumes, never the media folder.

## Where your data lives

**The two modes use different databases.** This is deliberate — the container
uses a named volume so it survives rebuilds, while the dev mode uses the
ordinary XDG location on the host. A playlist added in one will not appear in
the other.

| | Dev mode (`setup.test.sh`) | Container (`build.deploy.sh`) |
|---|---|---|
| Database | `~/.local/share/iptv-charts/` | volume `iptv-charts-data`, mounted at `/data` |
| Runtime prefs | `~/.config/iptv-charts/` | volume `iptv-charts-config`, mounted at `/config` |
| Cache | `~/.cache/iptv-charts/` | volume `iptv-charts-cache`, mounted at `/cache` |
| Media library | `~/Media/IPTV Charts/` (override with `IPTV_CHARTS_MEDIA_DIR`) | host `IPTV_MEDIA_DIR`, mounted at `/media` |
| Play queue | n/a — the player is launched directly | `$XDG_RUNTIME_DIR/iptv-charts/` |

Config and cache live in named volumes too, so replacing the container does
not silently discard runtime preferences or cached metadata. To inspect or
back up the container's database:

```bash
podman volume inspect iptv-charts-data --format '{{.Mountpoint}}'
```

The database holds provider credentials and metadata tokens in plain text,
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

`build.deploy.sh` restarts the bridge on every deploy. It is supervised: if the
bridge exits, it is started again, and `--status` finds it even if its pid file
was lost. It publishes a heartbeat and a list of the players installed on the
host, which is how the settings page can offer them. Browser playback is
unaffected if the bridge is not running.

## Movies and series

### TMDB and TVDB credentials

Open **Settings → Metadata providers** and paste the **TMDB API Read Access
Token** from TMDB's API settings. It is the JWT token that starts with `eyJ`,
not the older 32-character v3 API key. The app sends it as
`Authorization: Bearer <token>` on every TMDB request and never puts a
credential in the query string. TMDB provides movie and series search, posters,
overviews, ratings and alternative titles.

A **TVDB v4 API key** is optional. When present, episode names, numbers and air
dates come from TVDB; if the key is missing or TVDB is unreachable, the app
falls back to TMDB season data. A subscriber PIN can be supplied if your TVDB
key needs one.

Tokens/keys are stored in the app database and are included in backups. They
are shown as "configured / not configured" and are never echoed back into the
page. Tests can run fully offline with `IPTV_CHARTS_TMDB_BASE` and
`IPTV_CHARTS_TVDB_BASE` pointed at a fixture server.

### Adding, monitoring and searching

On **Movies** or **Series**, use **Add**. A title search runs against TMDB;
choose the right result, a quality profile, whether it should be monitored, and
whether to search immediately. Adding also captures alternative titles, which
matters because a provider may call a film `Un talento único` while TMDB's
English title is `A Unique Talent`.

The add dialog also carries the Radarr/Sonarr-style choices:

- **Movies — download when**: `announced` searches immediately; `in cinemas`
  waits for the TMDB release date; `released` (the default) also waits for the
  status to say it is released. A future movie stays monitored but is not
  searched, and the detail page says why.
- **Series — monitor episodes**: all, upcoming only, first season, latest
  season, or none. "Upcoming only" monitors unaired/unknown-date episodes and
  leaves already-released episodes unmonitored.
- **Series — when a new season appears**: monitor all new episodes, monitor
  future episodes only, or ignore new seasons. A metadata refresh discovers new
  episodes and applies this rule; it does not download them until they air and
  the search task runs.
- **Queue priority**: a number from -100 to 100. Higher values are claimed
  first; the priority can also be changed from the Activity queue and the
  detail pages.

The source restrictions on a quality tier are read from the provider release
name (WEB-DL, WEBRip, BluRay, HDTV, CAM, …). Audio and subtitle requirements
come from the stream probe, not the name.

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

### Quality profiles, probes and upgrades

**Settings → Quality profiles** (or the profiles link) edits profiles. A
profile is an **ordered hierarchy of quality tiers**, best first. Each tier is
a resolution plus an optional set of allowed sources:

- `2160p WEB-DL` — only 4K WEB-DL, never a CAM or another source;
- `1080p WEB-DL`;
- `1080p` — any source at 1080p;
- `720p`;
- `480p`;
- `unknown`.

Tiers can be added, removed and moved up or down; the order is the quality
hierarchy. The cutoff is a tier name, and acts as a stopping point, not a
ceiling. If a better allowed tier exists, it still wins, so "upgrade until
1080p WEB-DL" does not prevent a 2160p WEB-DL release from being chosen. An
upgrades switch disables replacements entirely.

Each profile also has:

- **Audio**: any, original-language audio, a single audio track, or multiple
  audio tracks. Optionally, one of a chosen set of languages must also be
  present.
- **Subtitles**: any, required, or no subtitle tracks. A required profile can
  name languages (English, Hindi, …) or `original`.
- **Require a successful probe**: on by default. If ffprobe cannot read the
  stream, the release is reported as unverified instead of being guessed at.
  Turning this off falls back to the provider's name for resolution/source
  while still using the probe when it succeeds.

The probe determines the real facts: ffprobe reads the URL, not the downloaded
file. It reports video codec, HDR, width/height, audio codecs/channels and
language tags, embedded subtitle tracks and language tags, duration, container
and size. Results are cached (24 hours by default; editable under Downloads)
and count against the playlist's connection limit.

Two profiles are seeded: **HD** (1080p/720p/480p/unknown, cutoff 1080p) and
**4K** (2160p/1080p/720p, cutoff 2160p). Existing profiles from older versions
are migrated to resolution-only tiers and probe-required on. The add dialog
lets you choose a profile, so picking a quality is a single choice.

A profile that is in use cannot be deleted until its movies and series are
reassigned, and at least one profile always remains.

### Downloads, connection limits and naming

The built-in queue lives in SQLite. A separate background worker process
(`worker.py`, supervised beside the web server) runs **one job at a time**:
playlist syncs, probe/search passes, downloads/imports, scheduled tasks,
metadata refreshes and scans. The web process only writes queue rows and reads
their progress, so a slow provider cannot tie up an HTTP thread. The
**playlist's own connection limit** is still the hard limit: a sync, a
metadata call, a probe and a download all take a slot from the same per-source
registry, now shared across both processes with `flock` lock files. The
effective limit is, in order:

1. the **connection limit** set on the playlist (add dialog or the card on the
   Playlists page);
2. the provider's reported `max_connections`;
3. **1** when neither is known.

The worker serialises everything, so a queued season downloads one episode
after another and a playlist sync cannot overlap a download. Interactive
searches are queued ahead of downloads, but they still wait for any job already
running; that is the deliberate price of the single-worker design. Browser
episode requests and playback take short-lived slots from the same limit while
the worker is busy.

Queue rows carry a **priority** (-100 to 100, default 0); higher priorities are
claimed first. A download can be cancelled, retried after failure, and
inspected in **Activity**, where the priority can be edited. The same page
shows the worker heartbeat and recent queue jobs. Interrupted jobs and
downloads are requeued on restart.

- **Direct files** are streamed to `media/.incomplete/` with Range resume,
  Content-Length checks, progress persisted to the database, and a disk-space
  preflight.
- **HLS** (`.m3u8`) is remuxed with ffmpeg using stream copy (`-c copy`). No
  transcoding is done anywhere; Jellyfin or any other player handles playback.
  The container image installs ffmpeg; in dev mode it is optional, but HLS
  downloads require it.
- A downloaded file is checked before import: an HTML login/error page served
  with HTTP 200 is rejected, and ffprobe must recognise the file as video. The
  URL probe has already made the quality decision; the local probe is a final
  integrity/type check.
- Imports are an atomic rename on the same filesystem, followed by a database
  update and a history event. The probed source, quality tier, audio/subtitle
  languages and media facts are stored on the library row. An upgrade deletes
  the previous recorded file only after the new one is in place.

Default names:

| | Template |
|---|---|
| Movie folder | `{title} ({year})` |
| Movie file | `{title} ({year}) [{resolution}]` |
| Series folder | `{title} ({year})` |
| Season folder | `Season {season:00}` |
| Episode file | `{series} - S{season:00}E{episode:00} - {episode_title} [{resolution}]` |

All five are editable in **Settings → Media management**, with a live preview.
File templates also accept `{source}` and `{quality}` (the matched tier label).
Tokens are `{title}`, `{series}`, `{year}`, `{season}`, `{episode}`,
`{episode_title}`, `{resolution}`, `{source}` and `{quality}`; `{season:00}`
and `{episode:00}` pad the number. A template describes one path component, so
a separator is refused.
Names are sanitised (illegal characters, reserved device names, length) and
every final path is confined to the media root. Unmanaged files are never
overwritten; a ` (2)` suffix is used instead.

### Automation, calendar and wanted

The worker loop enqueues due tasks and runs them as jobs, one at a time.
`IPTV_CHARTS_SCHEDULER=0` disables the automatic enqueue (manual task runs
still work):

- **Search missing** — every 6 hours by default. Monitored missing movies and
  aired, monitored episodes are matched again; files below cutoff are upgraded
  when upgrades are enabled.
- **Refresh metadata** — weekly by default. Refreshes TMDB/TVDB data and episode
  lists. Episodes discovered for the first time follow the series' **monitor
  new seasons** rule (all, future only, or ignore). This is Sonarr's new-season
  announcement behaviour without a notification channel.
- **Refresh playlists** — daily by default (`0` disables it). Playlists older
  than the interval are synced so new VODs become available to search. This is
  the equivalent of Radarr's indexer refresh.
- **Scan library** — manual only.

All four can be run from **Activity**, which also shows the queue, recent
failures, history and the next scheduled run. **Movies → Calendar** shows movie
release dates, **Series → Calendar** shows episode air dates, and the matching
**Wanted** tabs list missing movies or missing episodes respectively.

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
tables/settings.json         including metadata tokens and player choice
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
transaction. It refuses while any worker job (sync, search, download, task or
scan import) is queued or running. Restored file paths and naming templates are
re-validated; paths outside the current media root are cleared. Restored queued
downloads get fresh worker jobs, and playlist syncs are queued after the
restore, because the catalog is not in the ZIP.

**The ZIP contains provider credentials and metadata tokens in plain text.** Keep it
private.

## Settings

`/settings` (also in the top bar) is one form with **one Save all settings
button**. Every section is validated before anything is written, and the whole
save is one SQLite transaction — an invalid naming template or interval cannot
leave the other sections half-saved.

- **External player** and a **custom command** with `{url}`, `{title}`,
  `{user_agent}` and `{referrer}` placeholders. A custom command runs on the
  host with your privileges, exactly as written; nothing is passed through a
  shell.
- **Metadata providers**: TMDB API Read Access Token, TVDB key, optional PIN,
  language, and connection tests that use the stored credentials.
- **Media management**: media paths and the five naming templates with a
  preview.
- **Downloads and probing**: attempts, probe cache/timeout/candidate/budget
  controls, a probe-cache clear action, and ffmpeg/ffprobe availability. The
  worker is deliberately single-job; there is no concurrency setting.
- **Automation**: search, metadata and playlist-refresh intervals.

Quality profiles, library scan, and backup/restore are action cards on the
same page outside the settings form.

### Logs

`podman logs -f iptv-charts` (or `./build.deploy.sh --logs`; with Docker, use
`docker logs -f iptv-charts`) shows startup, the
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
**Provider connections are a shared, enforced limit.** A playlist's
connection limit is not advisory: playlist syncs, `get_series_info` metadata
calls, ffprobe stream probes and downloads all acquire a slot from one
per-source registry. The registry is a set of `flock` files under the data
directory, so it spans the web and worker processes and the kernel releases a
crashed process's slots automatically. The worker runs one job at a time, and
a browser episode request briefly takes a slot too. When the limit is 1, a
queued season is downloaded one episode at a time. Playback launched outside
the app (Jellyfin, an external player) cannot be accounted for; the app can
only enforce what it opens itself.

**Schema changes need a migration.** `schema.sql` is all `IF NOT EXISTS`, which
adds new tables but cannot add a column to an existing table. `db.migrate`
uses `PRAGMA user_version` for that; version 1 adds per-stream M3U headers and
seeds quality profiles/task state, version 2 adds stream probes, the
quality-tier/source/audio/subtitle fields, per-playlist connection limits,
queue priority, movie availability and new-season monitoring, and version 3
adds the durable worker queue/state plus the cross-process download cancel
flag. Existing databases are upgraded at startup; web and worker serialise the
first-run migration with a file lock.

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

## Documentation

- [`docs/QUALITY_AND_CONNECTIONS.md`](docs/QUALITY_AND_CONNECTIONS.md) — how
  quality tiers, probes, audio/subtitle checks, connection limits, priority
  and monitoring rules work.
- [`docs/PLAYLISTS_AND_SETTINGS.md`](docs/PLAYLISTS_AND_SETTINGS.md) — home
  dashboard, playlist editing/catalog identity, and the atomic settings save.
- [`docs/METADATA_PROVIDERS.md`](docs/METADATA_PROVIDERS.md) — TMDB read-token
  authentication and the TVDB v4 login/search/episode/pagination contract.
- [`docs/WORKER.md`](docs/WORKER.md) — the durable one-job-at-a-time worker,
  its queue, connection locks, recovery and deployment.
- [`docs/MOVIE_CANDIDATES.md`](docs/MOVIE_CANDIDATES.md) — the candidate
  workspace, pin modes and additional movie versions.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development workflow and invariants.
- [`LICENSE`](LICENSE) — MIT.

## Development

```bash
./setup.test.sh --check
```

Runs five suites, 689 checks, none of which touch the internet:

| Suite | Covers |
|---|---|
| `tests/smoke.py` | Parsers, schema, migrations, worker queue claim/dedupe/recovery, FTS, streaming, player commands, quality/naming/matching, languages, probes, connection limits, request guards |
| `tests/integration.py` | Real ingest against a local fixture provider |
| `tests/bridge.py` | The queue protocol, against the real host bridge and a fake player |
| `tests/endtoend.py` | The HTTP API and rendered pages, via Flask's test client |
| `tests/manager.py` | Fake TMDB/TVDB/probes, catalog matching, probe-first quality, audio/subtitle constraints, connection limits, priority, monitor modes, downloads/imports, upgrades, cancel/retry, scan, backup/restore |

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
run.py               web entry point (waitress; --dev for the Flask reloader)
worker.py            background worker entry point (one job at a time)
supervisord.conf     container process supervision (web + worker)
setup.test.sh        live dev build (starts both processes)
build.deploy.sh      container build and deploy (Podman or Docker)
deploy.dev.build.sh  working-tree container on a separate port/volume
Containerfile        image definition (includes ffmpeg for downloads)
iptv_charts/
  app.py             Flask factory, page routes, request guards
  api.py             JSON endpoints: probe, create, sync, status, settings
  manager_api.py      library, metadata, queue, task, scan, backup, profile API
  manager_web.py      Movies/Series with their Wanted and Calendar sections, Activity, Scan, Profiles
  settings_service.py atomic validation/save for the unified settings form
  config.py          path resolution, port selection, media folders
  db.py              SQLite connections, schema, migrations, bulk-load tuning
  jobs.py            durable worker queue: enqueue, claim, progress, recovery
  worker.py          worker loop, handlers and heartbeat
  sync.py            the ingest engine, durable progress, cancellation
  sources.py         source CRUD and credential probing
  items.py           catalog search and stream URL construction
  player.py          which player to use, how to run it, host bridge client
  metadata.py        provider selection, cache, image proxy
  matching.py        provider name normalisation, source and release scoring
  quality.py         quality tiers, source/audio/subtitle checks, cutoff decisions
  languages.py       ISO 639-1/2B folding for metadata and stream tags
  connections.py     cross-process per-playlist slots (flock files)
  streams.py         provider identity -> URL, headers, container, HLS
  probe.py           ffprobe on the URL, normalised stream facts and cache
  naming.py          templates, sanitisation, path confinement
  library.py         movies/series CRUD, episode sync, search/grab, import
  downloader.py      durable download rows, HTTP/HLS transfers, recovery
  scheduler.py       periodic task scheduling and task handlers
  scan.py            library scan/review/import
  backup.py          logical ZIP export and restore
  clients/
    m3u.py           streaming M3U parser and fetcher
    xtream.py        Xtream Codes API client
    tmdb.py          TMDB client and response normalisation
    tvdb.py          TVDB v4 login and episode client
    streaming.py     shared spool/progress primitives
  templates/         Jinja templates (home, playlist edit, unified settings, library)
  static/            CSS, JS, vendored hls.js
docs/
  WORKER.md          queue, worker loop, durability and deployment
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
tokens, so it must never be reachable from the network. In the container case
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
control characters before being handed to requests or ffmpeg. ffprobe is run
with a protocol whitelist (`http,https,tcp,tls,crypto`), a timeout, header
sanitisation and no shell; it never receives a browser-supplied URL.

The playlist edit page never renders the stored password. A blank password can
only test/update an unchanged server URL and username; changing either
requires re-entering it, so the stored credential cannot be sent to a
caller-supplied server. Changing a playlist's identity clears its catalog,
provider caches, probes, account snapshot and queued downloads before the
resync. All responses also refuse framing (`X-Frame-Options: DENY` and CSP
`frame-ancestors 'none'`).

Paths are confined to the media root. Naming templates are validated when
saved and again when restored from a backup; metadata titles are sanitised;
symlinks that escape the media root are skipped by the scanner; a downloaded
file is only deleted if its path is the one recorded in the database and is
inside the media library.

**Backups contain secrets.** They are loopback-only downloads, but the ZIP
itself contains provider credentials and metadata tokens. Store it accordingly.

Worth knowing: because there is no token, any web page you have open can still
*read* from `http://127.0.0.1:<port>/`, and can issue writes that browsers
classify as same-site. That is an accepted trade-off here, not an oversight. If
you ever expose this beyond loopback, that calculus changes and a token should
come back.

## Requirements

- Python 3.11+ (developed against 3.13)
- Podman, for the container path only
- `mpv` or `vlc`, entirely optional
- `ffmpeg` for HLS remuxing and `ffprobe` for pre-download stream inspection;
  the container image installs both. In dev mode, direct files still download
  without them, but probe-required profiles and HLS need them.
- A TMDB API Read Access Token for Movies and Series; a TVDB v4 API key is optional

On Debian/Ubuntu, if `python3 -m venv` fails with `No module named 'ensurepip'`:

```bash
sudo apt install python3-venv python3-full
```

## License

MIT
