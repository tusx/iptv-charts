# IPTV Charts

A local IPTV playlist browser that is also a movie and series manager. Point it
at a playlist — either a direct **M3U URL** or **Xtream Codes** credentials —
and it builds a searchable catalog you can browse, filter and play from. Add
movies and series from TMDB, let the **bworker** find matching VODs in your
playlists, download them, and file them into `Movies/` and `Series/` with
Radarr/Sonarr-style names.

Everything runs on your own machine, in one container or one virtualenv. There
is no remote server, no account, no indexer, no torrent client, and no public
API for other software. Provider catalogs and metadata keys stay local.

**0.8.0 in one paragraph.** One background process, the **bworker**, owns every
slow or provider-facing unit and exposes a live activity log, pause/resume and
per-work-type retry policies. Quality profiles are now **weighted scoring
profiles** with immutable 4K, 1080p and 720p defaults and a custom builder.
Settings are split across global, IPTV, Movie and Series pages. Explore,
Import existing, manual metadata and a storage browser are built in. The
container runs nginx, gunicorn, the bworker and Valkey under supervisor, and
`download.and.deploy.sh` installs a verified release with a manual update
check. See [`docs/UPGRADING.md`](docs/UPGRADING.md) before upgrading: the
profile reset is destructive.

## Home and navigation

`/` is a three-card dashboard:

- **IPTV** — playlist and catalog totals, **Add playlist**, **Update all**,
  **Manage**, and per-playlist Browse, Edit, Update, Cancel and Remove actions.
- **Movies** — **In library**, **Monitored**, **Downloaded** and **Missing**
  counts, plus Open movies, Add movie, Calendar, Wanted and Search missing.
- **Series** — series, episode, downloaded and **missing episode** counts,
  plus Open series, Add series, Calendar, Wanted and Search missing.

The top bar is the brand and nothing else. Movies and Series carry their own
**Library / Wanted / Calendar** tabs, so a section never sends you into another
section's list. Activity is linked from the home header and from each
section's actions. The layout is responsive: cards stack and tables scroll on
a phone. Icons are served from the vendored Font Awesome build, never from a
CDN.

Settings are deliberately split rather than one long form:

| Page | Owns |
|---|---|
| `/settings` | Global metadata providers, refresh schedule, links to the section pages, quality profiles, library scan, backup and restore |
| `/playlists#iptv-settings` | External player switch, player choice and command, playlist stale-after |
| `/movies/settings` | Movie naming, default profile and availability, search interval, candidate batch, probes per iteration, policies |
| `/series/settings` | Series naming, default profile, monitor and new-season defaults, search interval, candidate batch, probes per iteration, policies |

The global page saves its fields together. Each section page saves separately,
with its own validation and live naming preview, so a bad movie template can
never roll back an IPTV player change. See
[`docs/PLAYLISTS_AND_SETTINGS.md`](docs/PLAYLISTS_AND_SETTINGS.md).

## Explore, importing and storage

**Explore** (`/explore/movies`, `/explore/series`) shows TMDB discovery lists —
trending, popular, top rated, upcoming, on the air and airing today — from a
short-lived local cache. A details dialog can add a title through the normal
add flow, and a read-only playlist lookup shows whether the local catalog
already has it. Explore never writes to the library.

**Import existing** (`/import`) walks the media folders, proposes matches and
imports only what you confirm. The same page performs **manual metadata**
search, preview and apply for a movie or series whose identity is wrong, using
TMDB and TVDB directly. Scanning is read-only; the manual metadata calls never
change provider credentials or stream identity.

**Storage** (`/storage`) browses `Movies/`, `Series/` and `.incomplete/` through
a confined tree view. You can delete a file or empty a folder after
confirmation. Deleting an active staging file cancels its download once
confirmed; a scan or import in progress refuses the operation. Symlinks are
never followed and paths cannot escape the media root.

## Playlists

- **Add a playlist** as an M3U URL or Xtream Codes credentials, with a
  **connection test** that reports account status, expiry, connection limits
  and a channel estimate before anything is saved.
- **Edit a playlist** on its own page: name, URL/server, username, password,
  user agent, referrer and connection limit. A Test connection button checks
  unsaved values. Passwords are never rendered back into the page.
- **Ingest** the whole catalog into SQLite: streaming parse, batched inserts,
  cancellable, safe to re-run, with byte-accurate progress.
- **Update** one playlist or **Update all**, with a Cancel button. A playlist
  older than the configured interval shows a refresh reminder.
- **Remove** a playlist and its downloaded catalog in one action. Library
  movies and series and their files are not touched.
- **Browse** any playlist with full-text search, kind tabs (Live / VOD /
  Series), category filter, sorting and incremental paging.
- **Play** in the browser with a plain video element plus vendored hls.js, or
  hand a stream to a player on your machine.
- **Copy link** for any item or episode; series open a per-episode list.

The **IPTV settings card at the bottom of `/playlists`** owns external playback
and playlist staleness. **External player is OFF by default**; browser playback
always works. While it is off, player buttons are hidden and the player fields
are disabled. The card offers mpv, VLC (native or Flatpak), or a custom command
with `{url}`, `{title}`, `{user_agent}` and `{referrer}` placeholders. **Check
command syntax** parses the command but never runs it. Nothing is passed
through a shell. In the container a supervised host-side bridge launches the
player; in dev mode the player is launched directly.

Changing an M3U URL, Xtream server, username or password is an identity change:
queued and active downloads for that source are stopped, the catalog,
categories, provider series cache, probes and account snapshot are cleared,
and a fresh sync is queued. The stored password may be reused only when the
server and username are unchanged. A playlist cannot be edited while its own
sync is queued or running.

M3U playlists do not group episodes into series, so **series management and
episode downloads require an Xtream playlist**. Movies work with both.

## Movies and series

### Adding and monitoring

**Add** opens a TMDB search. Choose the result, a quality profile, whether to
monitor it, and the initial choices:

- **Movies — download when**: `announced` searches immediately; `in cinemas`
  waits for the release date; `released` (default) also waits for TMDB's
  released status. A future movie stays monitored and appears in the calendar.
- **Series — monitor episodes**: all, future only, first season, latest season
  or none. This is the initial selection when the series is added.
- **Series — new seasons**: monitor all newly discovered episodes, future
  episodes only, or ignore them. A metadata refresh discovers episodes and
  applies this rule; specials (season 0) are never auto-monitored.
- **Queue priority**: -100 to 100, inherited by downloads. Higher values are
  claimed first.

Adding captures alternative titles and external IDs, which matters because a
provider may call a film `Un talento único` while TMDB calls it `A Unique
Talent`. Series use TMDB for search and art; TVDB supplies episode names,
numbers and air dates when configured, with TMDB as the fallback.

### Weighted quality profiles

A profile is now **one required resolution plus optional weighted criteria**.
Source/quality is not required or scored: provider catalogues frequently omit
or mislabel it, so it must never block a release. Three immutable defaults ship
with the app:

| Profile | Required resolution | Target and maximum |
|---|---|---|
| **4K** | 2160p | 10 / 10 |
| **1080p** (default) | 1080p | 10 / 10 |
| **720p** | 720p | 10 / 10 |

The resolution is worth 10 points and sets the minimum score. Built-ins are
immutable and cannot be edited or deleted; their names are reserved. The
site-wide default profile starts at **1080p**.

The **custom profile builder** lets you choose any resolution in 2160p, 1080p,
720p, 480p or 360p, assign a weight (1–1000) to the resolution, set a target
score and optionally add:

| Section | Matching |
|---|---|
| Video codec | Exclusive: the best matching selected codec adds its weight |
| Audio codec | Exclusive |
| Audio language | Multi: every matching selected language adds its weight |
| Audio channels | Exclusive |
| Subtitle format | Multi |
| Subtitle language | Multi |

A candidate must use the exact selected resolution; a lower resolution is
eligible only when upgrades are enabled. Higher and unknown resolutions are
rejected. Optional criteria add zero when unmatched and never reject a
candidate. The target must be between 10 and the computed maximum.
`target_met` requires the exact resolution and a score at least the target.
When scores tie, an exact-resolution candidate beats a lower-resolution
fallback, so switching profiles can replace a fallback even at the same score.

When a candidate is selected, one meeting the target always wins. Otherwise a
lower-resolution fallback may be downloaded as a stopgap and is marked
**awaiting upgrade**. With an existing file, the current file is re-scored from
its stored media facts, and it is only replaced by a higher score when the
target is not met and upgrades are enabled. This is a score model, not a fixed
best-first quality ladder. See
[`docs/QUALITY_AND_CONNECTIONS.md`](docs/QUALITY_AND_CONNECTIONS.md).

### Probing

Before a download, **ffprobe reads the provider URL** and reports the real
resolution, video codec and HDR, every audio codec/channel/language, every
embedded subtitle track, duration and size. Automatic acquisition probes at
most three candidates per iteration, one ffprobe subprocess at a time, and
chooses only from probed facts. Manual search and grab probe on demand or reuse
the cache. Probe results are cached for 24 hours by default; failures are
retried after five minutes. Probes take a playlist connection slot.

ffprobe runs without a shell, with a protocol whitelist
(`http,https,tcp,tls,crypto`), a timeout, a small analyseduration/probesize and
sanitised provider headers. It never receives a browser-supplied URL.

### Automatic search and candidate acquisition

Automatic work is declarative and per item. Search is **ID first**: a movie
uses a TMDB ID, then an IMDb ID, then title/year. Catalog lookup uses indexes
and **keyset paging** (`id > cursor`), not offsets, so a sync cannot shift the
page out from under a search.

Each bworker iteration:

1. discovers at most the configured **candidate batch** (default **25**, up to
   200) new matching releases per scope;
2. expands at most three provider series containers into episode streams;
3. probes at most the configured number of pending candidates (default **3**,
   UI-capped at 3) one at a time, checking pause between probes;
4. deduplicates candidates, records the best one in durable scan/candidate
   tables, and either queues one download or returns **need more** so the next
   iteration can continue the cursor.

A search never scans or probes the whole catalog in one request and never
re-downloads the provider identity already backing the current file. If the
profile target is not reached after the candidate pool is exhausted, the best
eligible lower-resolution fallback may be grabbed and marked awaiting upgrade;
the interval re-search can later replace it with a better score. Releases that
match no mandatory criterion are reported as no candidates.

Matching normalises diacritics, locale prefixes (`ES - ...`), quality and group
tags and cast noise, extracts year/resolution/source and scores against the
title and aliases. Identity IDs are authoritative; title matching is the
fallback. If the match is uncertain, nothing is downloaded automatically. The
app never accepts a download URL from the browser: it re-resolves the provider
item and episode server-side.

For series, each monitored episode is matched by season and episode number
against the provider's own episode list, cached for a few hours. Unaired
episodes wait. TVDB/TMDB numbering can differ from the provider's; manual
search is the escape hatch.

### Manual search and the movie candidate workspace

Manual search accepts a query override and lists plausible releases with
probe facts; **Show all candidates** in the dialog expands that to every
catalog match and lets you probe or download one explicitly. The **movie
detail page is also the candidate workspace**: every matching VOD, its cached
ffprobe facts (size, resolution, video/audio/subtitle codecs and languages),
filtering (all, probed, profile allowed, pinned) and the weighted profile
decision. Probe one/all candidates without leaving the page. Series manual
search has the same all-candidates browser, and outside-profile episode picks
require confirmation.

A probed candidate can be pinned:

- **Only this** stops other active attempts and makes this the main file even
  when the profile rejects it. Future searches keep using it.
- **Also download** keeps the profile pick as the main file and adds this
  candidate as an extra version. It never becomes the main file.

Pins persist, scheduled searches honour them, and additional versions are
listed with a size and Remove action. They import with an `[alternate]` suffix
and never replace the main file. See
[`docs/MOVIE_CANDIDATES.md`](docs/MOVIE_CANDIDATES.md).

## Downloads, naming and library

The download queue is durable SQLite. HTTP downloads resume with Range
requests, verify Content-Length and re-check the row after waiting for a
connection slot. HLS is remuxed with ffmpeg using stream copy; nothing is
transcoded. Every downloaded file is checked before import: an HTML login page
served as HTTP 200 is rejected and ffprobe must recognise it as video.

The bworker runs one unit at a time, so a season downloads one episode after
another. The effective provider connection limit is, in order, the playlist's
**connection limit**, the provider-reported `max_connections`, or **1**. Syncs,
metadata calls, probes, downloads and browser episode requests all take slots
from one cross-process `flock` registry. Playback started outside the app
(Jellyfin, an external player) cannot be counted.

Queue rows carry a priority from -100 to 100. A download can be cancelled and
retried; progress and errors are visible on Activity and the detail pages.
Interrupted jobs and downloads are recovered at startup.

Default names:

| | Template |
|---|---|
| Movie folder | `{title} ({year})` |
| Movie file | `{title} ({year}) [{resolution}]` |
| Series folder | `{title} ({year})` |
| Season folder | `Season {season:00}` |
| Episode file | `{series} - S{season:00}E{episode:00} - {episode_title} [{resolution}]` |

All five are editable on the Movie/Series settings pages with a live preview.
File templates also accept `{source}` and `{quality}`. Names are sanitised and
confined to the media root; an unmanaged file is never overwritten and gets a
` (2)` suffix instead. Imports are an atomic rename on the same filesystem.
An upgrade deletes the previous recorded file only after the new file is in
place. A library scan detects files that have disappeared and returns them to
missing without deleting the library entry.

## Automation and the bworker

The background process is **`bworker.py`**. It claims two kinds of durable
work, always one bounded unit at a time:

- **`worker_jobs`** — imperative jobs inserted by the web process: playlist
  sync, download/import, manual search/grab, metadata refresh, scan import.
- **`bworker_work`** — declarative per-item work: movie search, series and
  episode search, probe, playlist sync, metadata refresh, download,
  library scan and activity cleanup. Each row has a stage, cursor, retry
  state, policy and pause-aware reset state.

Jobs are claimed before work. A sync, probe, download or import already in
flight is allowed to finish when you pause; the next unit does not start. The
Activity page has **Pause**, **Resume** and **Reset failed**; there are **no
run-now buttons for scheduled tasks**. Automatic work is driven by the
bworker's policies and intervals, not by clicking a queue.

Stages are visible on each item and on Activity: `pending`,
`waiting_availability`, `waiting_air`, `search`, `searching_candidates`,
`probing`, `downloading`, `verifying`, `renaming`, `moving`,
`awaiting_upgrade`, `complete`, `no_candidates`, `failed`, `paused` and
`skipped`.

Retry policy is per work type: retries use exponential backoff, are capped,
and stop after the policy's maximum (or continue indefinitely for searches and
refreshes). Failed work becomes **stopped** and can be reset. Defaults:

| Work | Attempts | Default delay / cap |
|---|---|---|
| Movie search | infinite | 6h / 6h |
| Series and episode search | infinite | 4h / 4h |
| Playlist sync | infinite | 5min ×2 / 1h |
| Metadata refresh | infinite | 7d / 7d |
| Download | 3 | 60s ×2 / 15min |
| Probe | 3 | 5min / 5min |
| Library scan | 3 | 15min ×2 / 15min |
| Activity cleanup | always on | 60s ×2 / 1h |

Search intervals are editable per section (default 6h for movies, 4h for
series) and can be overridden per item. The scheduler only enqueues global
**refresh metadata** (weekly) and **refresh playlists** (daily) maintenance;
missing-item search is bworker work, not a scheduler task.
`IPTV_CHARTS_SCHEDULER=0` disables the periodic enqueue only.

**Valkey** stores a small, newest-first activity stream. The bworker is the
only writer; the web process only reads. The list is capped at 500 entries and
trimmed to 100, with an incrementing ID so entries can update in place. If
Valkey is down, Activity shows a degraded banner and current work still comes
from SQLite; downloads, searches and syncs are never failed by a logging
problem. See [`docs/WORKER.md`](docs/WORKER.md).

## Backup and restore

**Settings → Backup and restore** exports a ZIP of the app's own state:
playlists and account snapshots, settings (including metadata tokens and player
choice), quality profiles, movies, series, episodes, downloads, additional
versions, history, scan results, task state, bworker work/policies/events. It
deliberately excludes the large IPTV catalog and its search index, provider and
metadata caches, probe results, bworker scan/candidate pages, worker queues and
the Valkey activity log. The catalog is rebuilt by a sync.

The format is **v2** in 0.8.0. A v1 backup is still readable: its old profile
rules are ignored, the three built-in profiles are restored from code, and all
restored movies and series are assigned to the **1080p** default, matching the
v4 → v5 migration.

Restore validates the manifest, format, schema version, table names, row
counts, JSON shapes and size limits, then replaces the app tables in one
transaction. It refuses while a playlist sync, scheduler task, queued/running
job or active download exists. A **paused bworker is safe**: pause finishes the
current unit, then restore can proceed. Restore re-seeds bworker work, clears
derived candidate/scan state and re-enqueues playlist syncs and queued
downloads. Paths and naming templates are re-validated against the current
media root.

**The ZIP contains provider credentials and metadata tokens in plain text.**
Store it accordingly.

## Running it

Two entry points, for two different jobs.

### Trying out a change — `setup.test.sh`

Runs the app directly from a virtualenv with auto-reload. It creates `.venv`,
installs `requirements.txt` only when it changes, starts the bworker beside the
reloading web process, and stops both on Ctrl+C.

```bash
./setup.test.sh              # start with auto-reload
./setup.test.sh --check      # run the full test matrix and exit
./setup.test.sh --no-reload  # run without the reloader
./setup.test.sh --port 9000  # different port
```

The script launches `worker.py`, which in 0.8.0 is a compatibility shim for the
bworker. Running `python run.py` by hand starts HTTP only; start
`python bworker.py` in another terminal if you do that. Dev media defaults to
`~/Media/IPTV Charts`; override with `IPTV_CHARTS_MEDIA_DIR`.

### Running it for real — `build.deploy.sh`

Builds a container image and runs it under Podman or Docker with named volumes,
a bind-mounted media folder, a host play-queue mount and `--restart always`.
When both runtimes are installed you are asked which to use; `--runtime
podman|docker` or `IPTV_RUNTIME` skips the prompt.

```bash
./build.deploy.sh              # build, deploy, open the browser
./build.deploy.sh --build      # build the image only
./build.deploy.sh --logs       # follow container logs
./build.deploy.sh --status     # container, volume, media and bridge state
./build.deploy.sh --stop       # stop and remove the container, keep data
./build.deploy.sh --shell      # shell inside the running container
./build.deploy.sh --destroy    # remove the container AND app volumes
```

The published port defaults to **8770** and is bound to `127.0.0.1` only.
`--destroy` never deletes the media folder.

For iterating on the working tree without touching production, use
**`deploy.dev.build.sh`**. It builds image `iptv-charts-dev`, runs container
`iptv-charts-dev` on port **8771**, uses separate named volumes, shares the
media folder and play queue, and supports `--build`, `--logs`, `--status` and
`--stop`. It never restarts or replaces the production container.

Environment overrides for the production script include `IPTV_RUNTIME`,
`IPTV_PORT`, `IPTV_IMAGE`, `IPTV_VOLUME`, `IPTV_CONFIG_VOLUME`,
`IPTV_CACHE_VOLUME`, `IPTV_CONTAINER` and `IPTV_MEDIA_DIR`. The dev script uses
the `IPTV_DEV_*` equivalents. Media detection is unchanged: if the folder
already contains `Movies`/`movies`/`movie` or
`Series`/`series`/`tv`/`tv shows`/`tv-shows`, that folder is used as-is;
otherwise `Movies/` and `Series/` are created. The folder is mounted at
`/media`; `--destroy` leaves it alone.

### Container topology

The image is based on `nginx:1.29.8-alpine` and runs four processes under
supervisor as uid 1000:

| Process | Bind | Role |
|---|---|---|
| `valkey-server` | `127.0.0.1:6379` | RAM-only activity stream |
| `gunicorn` | `127.0.0.1:8771` | Flask web process, one worker and 8 threads, timeout 3900s, `wsgi:app` |
| `bworker` | no socket | the only process that runs provider work, ffprobe/ffmpeg and imports |
| `nginx` | `0.0.0.0:8770` | vendored static files and reverse proxy to gunicorn |

The health check requires the app status `ok` **and** a live bworker
heartbeat. A paused bworker is alive; only a stopped or stale one fails.
Valkey is probed separately and its failure is reported as a degraded activity
log, not as an unhealthy app. The container includes ffmpeg/ffprobe and runs
with restart `always`.

### Updating

**In-app update check.** `GET /api/update/status` checks the public GitHub
release for `tusx/iptv-charts` once every six hours and returns the current and
latest versions, availability, release URL and the manual update command. It is
advisory: nothing downloads or replaces itself. A manual check uses
`?refresh=1`. Disable it with the `update_check_enabled=0` setting or the
`IPTV_CHARTS_UPDATE_CHECK=0` environment variable. Failures return a normal
200 with an `error` field, so a page poll never breaks.

**Manual update — `download.and.deploy.sh`.** The script resolves the latest
release (or `--version`), downloads the source archive and SHA-256 file,
**verifies the checksum before extracting anything**, safely rejects absolute
paths, `..`, special files and escaping links, compares versions with the
running container, asks for confirmation, installs the verified tree at
`${XDG_DATA_HOME:-~/.local/share}/iptv-charts-release`, and invokes that tree's
`build.deploy.sh`. Named volumes are reused, so a code update keeps playlists,
settings and the database. The media folder and host port are preserved.
`--yes`, `--force`, `--runtime`, `--port`, `--media-dir`, `--repo` and
`--version` are supported. Offline or rate-limited runs exit non-zero without
touching the deployment. See [`docs/RELEASING.md`](docs/RELEASING.md).

## Where your data lives

**The two development modes use different databases.** This is deliberate.

| | Dev mode (`setup.test.sh`) | Container (`build.deploy.sh`) |
|---|---|---|
| Database | `~/.local/share/iptv-charts/` | named volume `iptv-charts-data` at `/data` |
| Runtime prefs | `~/.config/iptv-charts/` | `iptv-charts-config` at `/config` |
| Cache | `~/.cache/iptv-charts/` | `iptv-charts-cache` at `/cache` |
| Media library | `~/Media/IPTV Charts/` | host `IPTV_MEDIA_DIR` at `/media` |
| Play queue | direct launch | `$XDG_RUNTIME_DIR/iptv-charts/` at `/playqueue` |

Config and cache are named volumes too, so replacing the container does not
silently discard preferences or metadata. The database holds provider
credentials and metadata tokens in plain text. Back it up with the app's
backup action; a raw `catalog.db` contains the whole provider catalog and is
mostly regenerable.

## Security

The server binds **loopback only**. In the container the published port is
bound to `127.0.0.1` on the host and nginx is the only public listener.

There is deliberately **no authentication** — it is a single-user local app.
Two checks stand in for one:

- the request's `Host` header must name loopback, which rejects DNS-rebinding
  pages that resolve another hostname to `127.0.0.1`;
- state-changing requests must not come from another site: a cross-site
  `Sec-Fetch-Site`, or an `Origin` naming another host, is refused. This
  includes backup restore and storage deletion.

Stream URLs and custom player commands are untrusted. Download URLs must be
`http`/`https` and are rebuilt from provider identity, never accepted from the
browser. Player commands run without a shell; the host bridge re-checks what
the app sends. Images come only from the TMDB image host through a validated
local proxy. ffprobe uses a protocol whitelist, timeout, sanitised headers and
no shell.

The playlist edit page never renders the password. A blank password may only
test/update an unchanged server and username, so the stored credential cannot
be forwarded to a caller-supplied server. Paths are confined to the media
root; symlinks that escape it are skipped; naming templates are validated when
saved and again on restore; a downloaded file is deleted only when its path is
the recorded one inside the library. All responses refuse framing
(`X-Frame-Options: DENY` and CSP `frame-ancestors 'none'`).

Because there is no token, any web page you have open can still *read* from
`http://127.0.0.1:<port>/`, and can issue writes that browsers classify as
same-site. That is an accepted single-user trade-off. Do not expose this app
beyond loopback without adding authentication.

## Design constraints

**Light on RAM.** A Xtream `get_vod_streams` response can be 200 MB of JSON
covering 200,000 items. The sync spools it to disk, parses incrementally with
ijson and inserts in batches, so peak memory stays flat. The catalog does not
store raw JSON per item. Downloads stream to disk and update progress in place.

**Library rows never reference a catalog item id.** A sync replaces `items`
with new ids, so a grab records `(source_id, kind, remote_id)` and the episode
stream id; the downloader re-resolves the URL when the download starts. If the
provider removed the item, the download fails clearly instead of fetching the
wrong stream.

**Catalog matching is best-effort.** Provider names contain locale prefixes,
quality tags, cast names and missing years. The matcher strips recognised
noise, uses IDs first and title/alias scoring second. Uncertainty leaves the
item missing rather than downloading the wrong film.

**Syncing must not disturb reading.** WAL mode keeps browse requests working
during a sync and prevents half-written rows being visible. A sync is
re-runnable; a cancelled re-sync can leave a partial catalog until the next
successful sync.

**Provider connections are a hard limit.** The per-source `flock` registry is
shared by the web process and bworker and released by the kernel if a process
dies. The bworker's single-unit rule prevents a sync, probe and download from
overlapping; a browser episode request can still hold a slot briefly.

**Schema changes need a migration.** `schema.sql` is `IF NOT EXISTS` for new
tables; columns use `db.migrate` with `PRAGMA user_version`. Version 5 adds
weighted profiles, `items.imdb_id`, per-item search overrides, file/download
scores and the bworker work/state/event/policy/scan/candidate tables. Existing
databases migrate at startup under a file lock. The v4 → v5 profile reset is
destructive — read [`docs/UPGRADING.md`](docs/UPGRADING.md).

**Search has to be written carefully.** A naive FTS join with a source filter
measured 20 seconds against a 313k-item catalog; a `CROSS JOIN` pins the FTS
match as the outer loop and runs the same query in 3 ms. A regression test
asserts the query plan. Candidate paging adds keyset indexes for TMDB/IMDb
identity so ID-first search does not scan the catalog.

## Documentation

- [`docs/UPGRADING.md`](docs/UPGRADING.md) — required reading before upgrading:
  the profile reset, backup compatibility and safe restore.
- [`docs/QUALITY_AND_CONNECTIONS.md`](docs/QUALITY_AND_CONNECTIONS.md) —
  weighted scoring, probes, connection limits, monitoring and retries.
- [`docs/PLAYLISTS_AND_SETTINGS.md`](docs/PLAYLISTS_AND_SETTINGS.md) — home,
  playlist identity and the split settings ownership.
- [`docs/MOVIE_CANDIDATES.md`](docs/MOVIE_CANDIDATES.md) — ID-first automatic
  acquisition, the manual candidate workspace, pins and extra versions.
- [`docs/WORKER.md`](docs/WORKER.md) — the bworker, its two queues, stages,
  retry policies, Valkey activity log and loop timing.
- [`docs/METADATA_PROVIDERS.md`](docs/METADATA_PROVIDERS.md) — TMDB read-token
  authentication, discovery feeds and the TVDB v4 contract.
- [`docs/RELEASING.md`](docs/RELEASING.md) — release workflow, updater
  verification and the in-app update API.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development workflow and invariants.
- [`LICENSE`](LICENSE) — MIT.

## Development

```bash
./setup.test.sh --check
```

That runs the full offline matrix through `tests/run_all.py`: every standalone
suite under `tests/` in a deterministic order, each in its own process. The
original core suites are `tests/smoke.py` (parsers, schema, migrations, queue,
FTS, quality profiles, naming, probes, connections, request guards),
`tests/integration.py` (real ingest against the local fixture provider),
`tests/bridge.py` (real host bridge protocol), `tests/endtoend.py` (HTTP API and
rendered pages via Flask's test client) and `tests/manager.py` (fake
TMDB/TVDB/probes, matching, downloads/imports, scan, backup). The same runner
also covers docs, profiles, bworker, settings sections, migration/backup,
Explore, Import, storage, activity UI, stage history, container config and
deploy/release scripts. CI and the release workflow run the same matrix through
the aggregate runner:

```bash
.venv/bin/python tests/run_all.py
```

No suite touches the internet. `tests/fixture_server.py` can also be run
standalone, which is how the container is exercised end to end.

## Project layout

```
run.py               web entry point (waitress; --dev for the Flask reloader)
bworker.py           background bworker entry point (one bounded unit at a time)
worker.py            compatibility shim that starts bworker.py
wsgi.py              gunicorn entry point (create_app)
supervisord.conf     container process supervision (Valkey, gunicorn, bworker, nginx)
nginx.conf           static file server and reverse proxy
valkey.conf          RAM-only activity store
setup.test.sh        live dev build (starts web + bworker)
build.deploy.sh      production container build and deploy (Podman or Docker)
deploy.dev.build.sh  working-tree container on separate port and volumes
download.and.deploy.sh  verified release download and manual update
Containerfile        image definition (nginx base, ffmpeg, Valkey, supervisor)
iptv_charts/
  app.py             Flask factory, page routes, request guards
  api.py             playlist and item JSON endpoints
  manager_api.py     library, metadata, queue, task, scan, backup, profile API
  manager_web.py     Movies/Series tabs, Activity, Scan, Profiles, detail pages
  explore_web.py     Explore Movies/Series and read-only playlist lookup
  import_web.py      Import existing, scan review, manual metadata
  storage_web.py     storage tree and confined file deletion
  settings_sections.py  split IPTV/Movie/Series section pages and APIs
  settings_service.py   global atomic settings validation and save
  stage_web.py       durable item stage/history APIs
  updates.py         GitHub release check, six-hour cache, semver compare
  update_web.py      /api/update/status
  bworker.py         bworker loop, job/work handlers and heartbeat
  bworker_store.py   declarative work, policies, stages, retries and pause
  bworker_events.py  per-item stage/history events
  bworker_api.py     status, pause/resume, activity, policies, reset
  activity_log.py    Valkey-backed activity stream (degraded-safe)
  acquisition/       bounded ID-first candidate discovery, probing and selection
  jobs.py            imperative durable queue: enqueue, claim, progress, recovery
  scheduler.py       periodic maintenance tasks and handlers
  config.py          path resolution, port selection, media folders
  db.py              SQLite connections, schema, migrations, bulk-load tuning
  sync.py            the ingest engine, durable progress, cancellation
  sources.py         source CRUD and credential probing
  items.py           catalog search, identity lookup, keyset paging, stream URLs
  player.py          player selection/launch and host bridge client
  metadata.py        provider selection, caching, image proxy, Explore feeds
  matching.py        provider name normalisation, identity and release scoring
  quality.py         weighted profile scoring, validation and candidate choice
  languages.py       ISO 639-1/2B folding for metadata and stream tags
  connections.py     cross-process per-playlist slots (flock files)
  streams.py         provider identity -> URL, headers, container, HLS
  probe.py           ffprobe on the URL, normalised facts and cache
  naming.py          templates, sanitisation, path confinement
  library.py         movies/series CRUD, episode sync, search/grab, import
  manual_metadata.py manual TMDB/TVDB search, preview and apply
  downloader.py      durable download rows, HTTP/HLS transfers, recovery
  scan.py            library scan/review/import
  storage.py         confined media tree, deletion and reconciliation
  backup.py          format-v2 ZIP export and v1/v2 restore
  clients/           m3u, xtream, tmdb, tvdb and streaming primitives
  templates/         Jinja templates (home, playlists, library, settings, tools)
  static/            CSS, JS, vendored Font Awesome/bootstrap/hls.js
docs/                user and implementation documentation
scripts/             host-side player bridge
tests/               offline suites, including docs consistency checks
```

## Requirements

- Python 3.11+ (developed against 3.13)
- Podman or Docker, for the container path only
- `mpv` or `vlc`, entirely optional
- `ffmpeg`/`ffprobe`: the container installs both; HLS downloads and
  probe-driven selection need them. Direct-file downloads still work without
  them in dev mode, but automatic acquisition relies on probes.
- A TMDB API Read Access Token for Movies and Series; a TVDB v4 API key is
  optional
- Valkey is bundled in the container. Outside it the activity log degrades
  gracefully; SQLite remains the durable source of truth.

On Debian/Ubuntu, if `python3 -m venv` fails with `No module named 'ensurepip'`:

```bash
sudo apt install python3-venv python3-full
```

## License

MIT
