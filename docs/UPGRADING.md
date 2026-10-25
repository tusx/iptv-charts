# Upgrading to 0.8.0

0.8.0 changes the quality-profile data model and the background process. Most
of the upgrade is automatic, but one part is deliberately destructive. Read
this page before starting 0.8.0 against an existing database.

## Before you upgrade

1. **Export a backup** from the running version
   (**Settings → Backup and restore → Download backup**). It contains
   playlists, settings, tokens, the movie/series library, download history and
   the old profile rows. A 0.8.0 install can read it, although the old profile
   rules are ignored (see below).
2. Note the quality-profile rules you rely on. If you use a custom profile,
   record its resolution, accepted sources, audio/subtitle requirements and
   upgrade preference. They cannot be reconstructed from the 0.8.0 database
   after migration.
3. If you are running the container, confirm the data volume name
   (`iptv-charts-data` by default). The upgrade must reuse it.
4. Stop starting new downloads or playlist syncs. Let any in-flight download
   finish, or cancel it, before upgrading.

Backups contain provider credentials and metadata tokens in plain text. Keep
the ZIP private.

## The profile reset is destructive

On first start, the database is migrated from schema v4 to **v5**. That
migration:

- deletes **every existing quality profile**, including custom profiles and the
  old HD/4K profiles;
- inserts the three immutable built-ins **4K**, **1080p** and **720p**;
- reassigns **every movie and every series** to the built-in **1080p**
  profile;
- discards the old ordered quality lists, stopping points, audio mode,
  subtitle mode and probe-required values.

There is **no migration of old profile rules**. Per-item profile choices are
not preserved. Library entries, files, downloads, history, playlists and
settings are kept.

After the first start:

1. Open **Movies → Settings** and **Series → Settings** and choose the default
   profile you want (the built-in default is 1080p).
2. Re-create any custom profiles with the weighted builder. Profiles are
   located from **Settings → Quality profiles**.
3. Reassign movies and series to the new profiles as needed.
4. Run a search pass or wait for the interval to re-evaluate existing files.
   Existing files are re-scored from their stored media facts.

The reset cannot be undone in place. A pre-upgrade backup can restore the old
library, but its profile rows are still ignored on import into 0.8.0; it
restores the library and assigns every item to 1080p.

## What happens to the database

- **Schema v5** adds weighted-profile columns, `items.imdb_id`, per-item search
  overrides, file/download scores and the bworker work/state/events/policy/
  scan/candidate tables.
- Migration runs automatically at startup under a file lock shared by the web
  process and the bworker. Do not start two different versions against the same
  database.
- The IPTV catalog is preserved. A playlist sync is not required just because
  of the migration, although a fresh sync can repopulate IMDb ids for ID-first
  matching.
- **Downgrading is not supported** after the migration. A 0.7.0 process cannot
  use a v5 database. To go back, restore the pre-upgrade data volume or a
  pre-upgrade backup and accept that work done after the upgrade is lost.

## Background work and old controls

The old `worker.py` process is now the **bworker**. The root `worker.py` file
still exists as a compatibility shim; `bworker.py` is the supported entry
point. In the container, supervisor starts Valkey, gunicorn, the bworker and
nginx.

- Activity shows current work, stage, progress, recent events and queue
  counts.
- **Pause** and **Resume** replace stopping a process. Pause takes effect
  between units; an in-flight download or probe is allowed to finish.
- **Reset failed** re-queues work whose retry policy is exhausted. There is no
  run-now button for scheduled tasks; use **Search missing** on the relevant
  section or **Search now** on an item when you want an explicit search.
- Interrupted work is recovered automatically. You do not need to delete a
  lock file unless the app reports that another bworker owns it while no
  bworker is running; a stale `worker.lock` is released by the kernel when its
  process exits.
- The activity log uses a Valkey sidecar in the container. If it is unavailable,
  Activity is degraded but downloads and searches continue. Running outside
  the container without Valkey is supported; SQLite remains the durable state.

The weekly metadata refresh and daily playlist refresh remain periodic tasks.
Missing-item search now runs through bworker work instead of the old scheduler
task. `IPTV_CHARTS_SCHEDULER=0` still disables only the periodic enqueue.

## Settings after the upgrade

Settings are split; the old single form no longer exists.

| Old location | New location |
|---|---|
| External player, player command | **Playlists → IPTV settings** (`/playlists#iptv-settings`) |
| Movie naming, availability, search interval | **Movies → Settings** |
| Series naming, monitor defaults, search interval | **Series → Settings** |
| Metadata providers, refresh intervals | **Settings** (global) |
| Quality profiles, library scan, backup | **Settings** (action cards) |
| Downloads/probing legacy controls | Superseded by Movie/Series search settings and bworker policies |

**External player is OFF by default** in 0.8.0. Player buttons are hidden and
the player fields disabled until you enable it in the IPTV settings card. The
browser player is unaffected.

Naming tokens are unchanged (`{title}`, `{year}`, `{series}`, `{season}`,
`{episode}`, `{episode_title}`, `{resolution}`, `{source}`, `{quality}`).
Templates are validated when saved. A template saved by an older version is
re-validated on first use; if it is refused, correct it on the Movie/Series
settings page.

## Backups and restore

0.8.0 writes **format v2** backups. They include bworker work, policies and
events in addition to playlists, settings, profile criteria, library, downloads,
alternates, history, scan results and task state. They still exclude the IPTV
catalog, probe cache, metadata caches, bworker scan/candidate pages, imperative
worker queues and the Valkey log.

Format **v1** backups remain readable:

- old profile rows are ignored;
- the three built-ins are restored from code;
- every restored movie and series is assigned to the 1080p default;
- tables introduced later (`movie_alternates`, bworker work/policies/events)
  are optional when the archive's schema predates them.

Restore procedure:

1. **Pause the bworker** on Activity and wait for the current unit to finish.
2. Confirm there is no running playlist sync, scheduler task, queued job or
   active download. Restore refuses in those cases.
3. Restore the ZIP from **Settings → Backup and restore**.
4. Review the restored settings, default profile and item assignments.
5. Resume the bworker. Restore re-seeds work and re-enqueues playlist syncs
   and queued downloads; the catalog itself must sync again.
6. Run **Search missing** or wait for the intervals if you want an immediate
   library re-check.

A paused bworker is explicitly safe. A running bworker, an active download, an
active sync and queued work are refusal conditions because replacing tables
under them would mix old cursor state with a new library.

## Container upgrades

Use one of the supported paths:

- `./build.deploy.sh` from the new source tree — rebuilds the image, creates a
  replacement container with `--restart always`, and reuses the named data,
  config and cache volumes and the media bind mount.
- `./download.and.deploy.sh` — resolves a published release, verifies the
  archive SHA-256 **before extracting**, validates the archive layout, installs
  to a stable directory and invokes its `build.deploy.sh`. `--version`, `--yes`,
  `--force`, `--runtime`, `--port` and `--media-dir` are available.
- `./deploy.dev.build.sh` — a separate dev image/container on port 8771 with
  its own volumes. It never upgrades or disturbs production.

Do **not** delete the data volume to "clean up" an upgrade. It contains the
database and settings; deleting it is equivalent to a fresh install.
`build.deploy.sh --destroy` removes the container and app volumes (not the
media folder), so it is not an upgrade command.

The in-app update check is advisory and must not be confused with automatic
updates: `GET /api/update/status` checks the GitHub release for
`tusx/iptv-charts` every six hours, and nothing downloads or replaces itself.
Disable it with the `update_check_enabled=0` setting or
`IPTV_CHARTS_UPDATE_CHECK=0`.

## Container topology and resource notes

The public listener is nginx, which serves vendored static files and proxies to
gunicorn on loopback. The bworker has no socket; Valkey is RAM-only. The health
check requires the app and a live bworker heartbeat. A paused bworker is alive,
so pausing during an upgrade or restore does not make the container unhealthy.

The image includes ffmpeg and ffprobe. Automatic acquisition relies on probes
to establish resolution and source facts; running dev mode without ffprobe
limits candidate selection even though direct downloads may still work.

Valkey is not durable state. If it restarts, the activity history is empty; no
work, queue rows, stages, profiles or downloads are lost. SQLite in the data
volume remains the source of truth.

## Verification after upgrading

1. Open `/api/health` and confirm the app status is `ok`, the bworker is
   `alive`, and (in the container) the activity store is available.
2. Open Activity and confirm the bworker is idle or working, with a fresh
   heartbeat.
3. Open Settings and check metadata tokens show as configured.
4. Open Movies and Series settings and confirm the default profile, naming
   previews and search intervals.
5. Check a sample of movie/series detail pages and their new stage cards.
6. Confirm external player is intentionally off or re-enable it.
7. Run one playlist update and one explicit **Search missing** to exercise the
   new path.
8. Export a fresh format-v2 backup.

If something fails, keep the container logs (`./build.deploy.sh --logs`) and
the data volume intact, then restore the pre-upgrade backup or volume. Do not
delete the volume while diagnosing; the migration is not reversible in place.
