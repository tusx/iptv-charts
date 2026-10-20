# Changelog

All notable changes to IPTV Charts are recorded here. The project follows
semantic-ish versioning while the app is pre-1.0: the minor version changes
when the library schema or user-visible workflow changes.

## 0.6.0 — 2026-10-20

### Added

- **A separate background worker and durable job queue.** The container now
  runs `run.py` (HTTP only) and `worker.py` (one infinite loop) under
  supervisor. Playlist syncs, candidate searches/ffprobe, downloads/imports,
  scheduled tasks, metadata refreshes and scans are `worker_jobs` rows in
  SQLite. The web process only enqueues and polls; it no longer starts sync,
  download or scheduler threads.
- Job priority and dedupe: a sync/download/search already queued or running is
  not duplicated; interactive searches run ahead of scheduled work. The
  Activity page shows the worker heartbeat and recent jobs.
- Progress survives a reload and is visible across processes: sync phase,
  bytes, items and messages are written to `worker_jobs` and `sources`, and a
  download keeps using its existing row.
- Interrupted jobs are requeued on startup; interrupted downloads resume from
  their temp file. The worker can be stopped at any time without losing the
  queue.

### Changed

- **Per-playlist connection slots are enforced across the two processes with
  `flock` lock files** under the data directory, not an in-memory dict. The
  provider limit now covers a browser episode request while the worker is
  downloading, and a crashed process cannot leave a stale slot.
- **One job at a time** replaces the previous 1–3 download threads. A season
  downloads one episode after another regardless of playlist count; the
  concurrency selector was removed from Settings. Queue priority, retry,
  cancellation and per-playlist limits remain.
- Manual search/release/grab/refresh endpoints keep their synchronous
  responses, but the request thread now waits on a worker job instead of
  running ffprobe itself.
- Schema version 3 adds `worker_jobs`, `worker_state` and
  `downloads.cancel_requested`; the web and worker serialise first-run
  migrations with a file lock.
- `setup.test.sh` starts the worker beside the development server. The
  container uses `supervisord.conf`; `run.py` serves HTTP only.
- Test coverage updated to 654 checks, including queue dedupe/claim/recovery.

## 0.5.0 — 2026-10-20

### Changed

- **TMDB now uses only the API Read Access Token**, sent as
  `Authorization: Bearer <token>` on every request. The legacy 32-character v3
  API key query parameter is no longer supported; saving one is rejected with
  a clear message. The Settings field, API validation and docs now ask for the
  read token.
- A JWT saved under the old `tmdb_api_key` setting is still read for
  compatibility. Saving a new token or clearing the credential clears the old
  key so the fallback cannot resurface it.
- **TVDB v4 client aligned with the official OpenAPI spec and official Python
  client**: series search accepts the documented `year` filter; episode
  pagination reads top-level `links.next` page numbers (with string/URL
  tolerance and loop protection); the translated episode endpoint falls back
  to the untranslated endpoint whose schema explicitly includes
  `data.episodes`; bearer auth and login caching remain unchanged.
- Tests assert TMDB bearer auth and no `api_key` query, TVDB bearer auth,
  TVDB page-number pagination, v3 key rejection and legacy JWT fallback.
- Test coverage updated to 632 checks.

## 0.4.0 — 2026-10-18

### Added

- **Home dashboard** with three cards — IPTV, Movies and Series. The IPTV card
  keeps the per-playlist Update/Browse/Edit/Remove actions and Update all/Add
  playlist buttons; the library cards expose Open, Add, Calendar, Wanted and
  Search missing actions directly.
- **Playlist edit page** at `/playlists/<id>/edit`. It edits the display name,
  M3U URL or Xtream server/username/password, user agent/referrer and
  connection limit, with a Test connection button that can use the stored
  password for an unchanged account. Provider changes clear the old catalog
  and derived caches, reset the account snapshot and queue a sync.
- **Unified settings form** with one Save button. Player, metadata, naming,
  downloads/probing and automation are validated into memory and written in a
  single SQLite transaction; a bad value in any section saves nothing. Action
  buttons (provider tests, probe-cache clear, backup/restore) remain separate.
- Management pages moved under `/playlists`; `/` is now the dashboard. The
  existing playlist dialog is shared by both pages.
- `settings_service.py` for atomic settings validation and saving.

### Security

- Playlist passwords are never rendered into the edit page.
- A blank password may only test/update an unchanged server URL and username;
  changing either requires re-entering the password, preventing a caller from
  sending the stored password to a different server.
- Changing provider identity clears items, categories, provider series caches,
  stream probes, the account snapshot and queued downloads for that source.
- Every response now sends `X-Frame-Options: DENY` and a
  `Content-Security-Policy: frame-ancestors 'none'` header.
- Test coverage increased to 613 checks across the five offline suites.

## 0.3.0 — 2026-10-18

### Added

- **Pre-download stream probing.** ffprobe reads a provider URL and reports
  real resolution, video codec/HDR, audio tracks and languages, embedded
  subtitle tracks and languages, duration, container and size. Results are
  cached per provider stream. Automatic and manual searches show the probed
  facts; nothing is downloaded until the stream satisfies the profile.
- **Custom quality hierarchies.** A profile is now an ordered list of tiers,
  each combining a resolution with an optional source set (`WEB-DL`, `WEBRip`,
  `BluRay`, `HDTV`, `CAM`, …). Tiers can be reordered, and a cutoff is a tier
  rather than only a resolution.
- **Audio and subtitle requirements.** Profiles can require original-language
  audio, a single or multiple audio tracks, one of a chosen set of audio
  languages, required subtitles in chosen languages (including the original
  language), or no subtitle tracks. Language tags are normalised between TMDB
  (ISO 639-1) and ffprobe (ISO 639-2/B).
- **Per-playlist connection limits.** A playlist can carry an explicit limit;
  otherwise the provider-reported maximum is used, falling back to one. The
  limit is enforced across playlist syncs, `get_series_info` calls, probes and
  downloads, including a fairness rule so a waiting sync/probe cannot be
  starved by a busy download queue. A season downloads one episode at a time
  at a limit of one.
- **Queue priority.** Movies, series and individual queue rows have a priority
  (-100 to 100); the download workers claim the highest priority first. The
  queue priority is editable in Activity.
- **Radarr/Sonarr add choices.** Movies have minimum availability (announced,
  in cinemas, released). Series have a monitor mode (all, upcoming, first
  season, latest season, none) and a "monitor new seasons" rule (all, future,
  none). New episodes discovered by metadata refreshes follow that rule.
- **`{source}` and `{quality}` naming tokens** for file templates.

### Changed

- Schema version 2 adds `stream_probes` and the quality/audio/subtitle,
  connection-limit, priority, availability and monitor-rule columns. Existing
  profiles are migrated to resolution-only tiers with probing required.
- The downloader no longer pauses all work during a sync. It skips a playlist
  whose connection slots are in use while other playlists continue.
- Manual search and automatic search return profile decisions with a readable
  reason; manual grabs are rejected when the probe or profile does not allow
  the release.
- Downloads store the probe facts and the matched quality tier, and imports
  copy source/quality/audio/subtitle/media facts onto the library row.
- No transcoding is performed. HLS is remuxed with `-c copy`; Jellyfin or
  another player handles playback.
- Backup/restore understands the new schema; probe caches are excluded like
  the other derived caches.
- Documentation, version and test coverage updated (546 checks).

## 0.2.0 — 2026-10-18

### Added

- Movies and Series pages backed by TMDB, with optional TVDB episode data and
  a TMDB fallback.
- Automatic and manual matching of catalog VODs and Xtream provider episodes.
- Quality profiles with resolution tiers, cutoff and upgrades.
- A durable download queue with HTTP Range resume, HLS remuxing, progress,
  cancel, retry and history.
- Radarr/Sonarr-style naming and atomic imports into `Movies/` and `Series/`.
- Calendar, Wanted, Activity, library scan/review/import, backup/restore.
- Scheduler for missing searches, metadata refresh and playlist refresh.
- Container media bind mount and ffmpeg in the image.

## 0.1.0 — earlier

- Local IPTV playlist browser: M3U/Xtream ingest, SQLite catalog, FTS search,
  charts, browser playback, external player launch and host player bridge.
