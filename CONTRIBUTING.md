# Contributing

IPTV Charts is a single-user, loopback-only application. Changes should keep
that shape: one process, one SQLite database, no external services beyond the
provider and metadata APIs the user configures.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./setup.test.sh              # live dev server + worker, auto-reload
./setup.test.sh --check      # all five test suites, no network
```

The full check suite must pass before a change is merged. It uses local fixture
servers for Xtream/M3U, TMDB, TVDB, media and ffmpeg/ffprobe; it does not touch
the internet or the user's media library.

The container path additionally needs Podman and the system ffmpeg/ffprobe
package:

```bash
./build.deploy.sh --build
./build.deploy.sh
```

## What to be careful about

- **Schema changes.** `schema.sql` is all `IF NOT EXISTS`, so it cannot add a
  column to an existing table or create an index over a column that does not
  exist yet. Add columns and such indexes in `db.migrate` under a new
  `SCHEMA_VERSION`, and keep `schema.sql` usable against the previous version.
- **Provider connections.** Any new provider call must be considered against
  `connections.py`. Syncs, `get_series_info`, probes and downloads share the
  per-playlist limit; nesting acquisitions can deadlock at a limit of one.
- **Untrusted names and headers.** Provider names, metadata titles, M3U headers
  and backup contents are untrusted. Paths go through `naming`, headers through
  `streams.clean_header`, URLs are rebuilt server-side, and every process is
  started without a shell.
- **Web requests do not do background work.** New slow or provider-facing
  operation belongs in a `worker_jobs` handler in `iptv_charts/worker.py`;
  the HTTP route enqueues and, if it needs a result, polls. Do not start a
  thread in the web process or call ffprobe/ffmpeg from a request handler.
  The worker runs one job at a time, so handlers must be self-contained and
  leave durable progress/state in SQLite.
- **Library identity.** Never store an `items.id`: a playlist sync replaces
  catalog rows. Store `(source_id, kind, remote_id)` and an episode stream id.
- **No transcoding.** HLS is remuxed with `-c copy`. The app files media for
  Jellyfin; it does not implement playback processing.
- **Probe-first quality.** A quality decision should use the probe result when
  the profile requires one; do not silently fall back to the filename for
  audio/subtitle requirements.
- **Settings saves are atomic.** Add new form fields to the nested payload and
  `settings_service.py`; validate every section before writing, then commit in
  one transaction. Keep actions (tests, backups, cache clearing) separate.
- **Playlist credentials are special.** Never render a stored password. A
  blank password may only test/update an unchanged server and username. Any
  identity change must clear the old catalog/caches and queue a sync.

## Tests

| Suite | What it protects |
|---|---|
| `tests/smoke.py` | Parsers, migrations, worker queue, FTS, quality/language/probe/connection units |
| `tests/integration.py` | Ingest against the local fixture provider |
| `tests/bridge.py` | The host player queue contract |
| `tests/endtoend.py` | HTTP routes and rendered pages |
| `tests/manager.py` | TMDB/TVDB/probes, library, queue, scan, backup |

Add a focused check for each behaviour change. Failure messages in the suites
are written for a person reading CI output; keep them specific.

## Commits

Keep commits scoped to one change with a subject in the imperative mood:

```
Probe provider streams before downloads
Add per-playlist connection limits
```

Explain the *why* in the body when it is not obvious from the diff. Include
tests and documentation in the same commit unless a separate documentation
commit is genuinely clearer.
