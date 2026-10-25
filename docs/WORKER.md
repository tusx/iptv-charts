# The bworker: queues, stages, retries and the activity log

IPTV Charts runs as four processes in one container:

```
supervisord
├── valkey-server   127.0.0.1:6379  RAM-only activity stream
├── gunicorn        127.0.0.1:8771  Flask web process, 1 worker × 8 threads
├── bworker         no socket       every slow/provider/disk unit, one at a time
└── nginx           0.0.0.0:8770    vendored static files + reverse proxy
```

The web process renders pages, serves playback, writes queue rows and reads
their status. It never runs a provider download, ffprobe/ffmpeg, playlist
import, library scan or file rename. The bworker has no listening socket and is
the only process that performs those operations.

The split exists so a 200 MB playlist sync cannot occupy a request thread and a
web reload cannot lose queued or in-progress work. It also gives every slow
operation one place to be paused, retried and observed.

## Two sources of work

The bworker claims from two durable tables, in this order:

| Source | Owner | Shape |
|---|---|---|
| `worker_jobs` | imperative, inserted by the web process | one row per request: sync, download, manual search/grab, task, scan import |
| `bworker_work` | declarative, keyed by item and work type | one recurring/per-item row with stage, cursor, retry state, policy and reset flag |

`worker_jobs` is the older queue and keeps its handler contract. It is
deliberately claimed first: a manual search, playlist sync or download the user
just requested runs before background automatic work. Once no job is due, the
bworker claims one due `bworker_work` row.

Only one unit runs at a time. The bworker never overlaps a job with work, or
two work rows. There is no second worker and no concurrency setting.

### Imperative job kinds

| Kind | Work |
|---|---|
| `sync` | one playlist ingest (`sync.run_job`) |
| `download` | one queued HTTP/HLS download and import (`downloader.process_download`) |
| `task` | one scheduled/manual maintenance task (`scheduler.run_task`) |
| `search_movie` | automatic or manual movie matching, candidate probing and selection |
| `search_series` | series or one-episode matching |
| `movie_releases` / `episode_releases` | manual release list with ffprobe facts |
| `grab_movie` / `grab_episode` | re-probe the selected release, then enqueue the download |
| `refresh_movie` / `refresh_series` | metadata refresh from TMDB/TVDB |
| `probe_movie_candidate` / `probe_movie_candidates` | ffprobe one candidate or the candidate list |
| `scan_import` / `scan_import_all` | import reviewed scan results |

`worker_jobs` columns:

| Column | Meaning |
|---|---|
| `kind` | handler name |
| `payload` | JSON arguments, e.g. `{"source_id": 2}` |
| `status` | `queued`, `running`, `done`, `error`, `cancelled` |
| `priority` | higher first; interactive searches 55–60, syncs 20, tasks 15 |
| `dedupe_key` | active-job uniqueness (partial unique index) |
| `available_at` | future retry scheduling |
| `progress_data` | JSON sync/download phase, bytes, items |
| `result` / `error` | JSON result or user-facing failure |

`iptv_charts/jobs.py` owns enqueue, claim and finish. Claiming uses
`BEGIN IMMEDIATE`; only one process can win a row. A queued job cancelled by
the user frees its dedupe key immediately. A running sync or download observes
a cancel flag at a safe point.

## Declarative work

`iptv_charts/bworker_store.py` owns `bworker_work`, `bworker_policies`,
`bworker_state`, `bworker_events`, `bworker_scan` and `bworker_candidates`.
One work row represents one entity and one kind of background intent:

| Work type | Entity | Default policy |
|---|---|---|
| `movie_search` | movie | enabled, infinite retries, 6h delay/cap |
| `series_search` | series | enabled, infinite, 4h delay/cap |
| `episode_search` | episode | enabled, infinite, 4h delay/cap |
| `playlist_sync` | source | enabled, infinite, 5min ×2 backoff, 1h cap |
| `metadata_refresh` | — | enabled, infinite, 7d delay/cap |
| `download` | movie/episode | enabled, 3 attempts, 60s ×2, 15min cap |
| `probe` | movie/episode | enabled, 3 attempts, 5min, 5min cap |
| `library_scan` | — | enabled, 3 attempts, 15min ×2, 15min cap |
| `activity_cleanup` | — | always enabled, infinite, 60s ×2, 1h cap |

Policy fields are `enabled`, `max_failures`, `infinite_retry`,
`retry_delay_seconds`, `backoff_multiplier` and `max_delay_seconds`. The
Activity policy editor updates all rows except `activity_cleanup`, which cannot
be disabled because it must keep trimming the Valkey stream while work is
paused. **Reset failed** in the UI clears only rows in `failed_stopped`; the
generic `POST /api/bworker/reset` accepts `work_id`, `work_key`, `entity_type`
or neither (all units).

`work_key` is unique per item/type. Work rows are seeded for monitored
movies/episodes by the startup/migration pass and are reconciled idempotently;
the add/edit/monitor paths use `ensure_movie_work`,
`ensure_episode_work` and `ensure_series_work` to create or re-enable a row as
soon as monitoring changes. Unmonitoring or deleting an entity disables or
removes its rows rather than losing history.

### Stages

`bworker_work.stage` is user-visible on the item and Activity:

`pending`, `waiting_availability`, `waiting_air`, `search`, `searching_candidates`,
`probing`, `downloading`, `verifying`, `renaming`, `moving`, `awaiting_upgrade`,
`complete`, `no_candidates`, `failed`, `paused`, `skipped`.

A newly monitored movie whose release date is in the future starts at
`waiting_availability` with `next_run_at` at midnight UTC on that date. A future
episode starts at `waiting_air`. Otherwise the initial stage is `search`.
Stage changes are recorded in `bworker_events` with a timestamp, message and
progress payload, so a detail page can show the recent history rather than only
the current value.

## Claiming

`claim_work` selects a row in `BEGIN IMMEDIATE` where:

- `enabled = 1`;
- `status` is `pending` or `retry`;
- `COALESCE(next_run_at, 0) <= now`;
- the policy is enabled, except that `activity_cleanup` always runs.

It orders by `next_run_at`, then id, marks the row `running`, records
`started_at`/`last_started_at`, clears `last_error` and consumes
`reset_requested`. A crash leaves `running` rows; startup recovery requeues
them, links any still-active download and records that it was interrupted.

## Success, failure and retry

`finish_success` resets `failure_count`, stores the new stage/cursor/best
candidate and sets:

- `status = pending` with `next_run_at` when the work recurs;
- `status = complete` with no next run when it is finished.

A profile rejection is not a failure. It is a successful unit that reports
`no_candidates` (or `awaiting_upgrade` for a fallback) so a later interval can
try again without incrementing a failure counter.

`finish_failure` increments `failure_count` and computes:

```
wait = retry_delay_seconds × backoff_multiplier^(failure_count - 1)
wait = min(wait, max_delay_seconds)
```

With infinite retry, the row becomes `retry` and is due after `wait`. Without
infinite retry, once `failure_count >= max_failures` it becomes
`failed_stopped`, has no next run and keeps `last_error` for the UI. A
non-transient failure can disable the row through `finish_skipped`.

`finish_skipped` is a clean no-op: a missing entity, an unmonitored item or an
already-active download. It either re-schedules with `next_run_at`, completes,
or disables the row.

## Pause, resume and reset

`bworker_state` is the single pause authority, stored in SQLite so the web and
bworker agree across restarts.

- **Pause** takes effect **between units**. The current download/probe/sync is
  allowed to finish; no new unit starts. The bworker polls the state once a
  second (`PAUSE_POLL_SECONDS = 1.0`) while paused.
- A paused bworker still writes heartbeats and reports healthy. Manual
  acquisition endpoints are refused while paused.
- **Resume** returns the loop to normal claiming. Pause/resume endpoints only
  write SQLite; they do not signal or kill a process.
- **Reset** on an idle/retrying/failed row sets it to `pending` immediately and
  clears failure count, cursor, best candidate and profile revision. Reset on a
  **running** row sets `reset_requested = 1`; the bworker consumes it at the
  next loop boundary, after the current bounded unit. A reset never interrupts
  an in-flight download.
- **Reset failed** in Activity resets only stopped rows. The API can reset one
  work row, one entity or all units.

## Startup and the loop

`Bworker.run_forever()`:

1. calls `db.init_db()` (migration is serialised with the web process by a
   file lock in `db.py`);
2. takes `<data>/worker.lock` with `flock(LOCK_EX | LOCK_NB)` and exits if
   another bworker owns it;
3. runs `startup()`: requeue orphaned jobs/work, mark interrupted activity
   entries, seed and reconcile work, reconcile the pause log, recover
   downloads, write the first heartbeat;
4. starts a heartbeat thread that writes every **5 seconds**;
5. repeats `step()` until SIGTERM/SIGINT, waiting on the stop event so shutdown
   is prompt.

`step()`:

1. Runs housekeeping: recover orphaned jobs, consume pending resets, prune
   activity and old jobs.
2. Reads `bworker_state`. If paused, records `paused`/heartbeat and returns a
   one-second wait. Otherwise it calls `scheduler.enqueue_due()`.
3. Claims one `worker_jobs` row. If none, claims one `bworker_work` row.
4. Runs the claimed unit and records its outcome and activity entries.
5. Checks whether due jobs/work remain. If so, the loop runs again
   immediately. If no work is due and the iteration was fast (≤
   `bworker_fast_loop_seconds`, default 10s), it sleeps
   `bworker_idle_wait_seconds` (default 30s). A slow iteration does not add
   another idle wait.
6. Writes an idle heartbeat and repeats.

The no-work activity entry is written once per idle transition, not on every
idle poll. Settings keys `bworker_idle_wait_seconds`, `bworker_fast_loop_seconds`,
`bworker_retry_delay_seconds`, `bworker_retry_backoff`,
`bworker_retry_max_delay_seconds`, `activity_log_cap` and
`activity_log_trim_to` are seeded with defaults; the policy values usually
override the global retry values for automatic work.

## Periodic maintenance

`scheduler.enqueue_due()` is database-only and runs before claiming. It reads
`task_state` and inserts a `task` job when due:

- **Refresh metadata** — weekly by default (`refresh_interval_hours`, default
  168). Refreshes TMDB/TVDB data, discovers episodes and applies the series'
  new-season rule.
- **Refresh playlists** — daily by default (`catalog_refresh_hours`, default
  24, `0` disables). Queues sync jobs for stale playlists.

`search_missing` is intentionally not in `TASK_NAMES`; its old task-state row
may remain for display but it is never queued. Missing-item search is
declarative bworker work. The policy mapping is `refresh_catalog` →
`playlist_sync` and `refresh_metadata` → `metadata_refresh`; a disabled policy
suppresses the task while leaving it due so re-enabling queues it next pass.
Metadata refresh is deferred while a sync is queued or running so it cannot
read a half-replaced catalog. Task priority is 15.

`IPTV_CHARTS_SCHEDULER=0` (or `false`/`no`/`off`) disables only this periodic
enqueue. Claimed jobs and work continue, and manual maintenance can still be
queued through the API.

## Activity log in Valkey

The activity log is **not** in SQLite. It is a short-lived UI stream:

```
iptv-charts:activity      list, newest at the tail (RPUSH)
iptv-charts:activity:id   monotonically increasing id (INCR)
```

`activity_log.py` pushes with one Lua script that assigns the id, appends and,
when the list reaches the cap, trims it to `trim_to`. The defaults are cap
**500**, trim **100**. Counter-based ids let a running entry be updated in place
when it finishes. Event names are `started`, `finished`, `no_work`, `paused`,
`resumed` and `interrupted`; statuses are `running`, `success`, `failure`,
`skipped`, `retry`, `interrupted`, `paused` and `idle`.

**Only the bworker writes the log.** The web process only reads through
`GET /api/bworker/activity`; it never appends, even for pause/resume — the
bworker writes those entries at the next loop boundary. Every log method
catches every error, connect/read timeouts are
**0.25s**, and after a failure the client backs off for **10s**. A missing or
hung Valkey therefore degrades the Activity page; it never fails a download,
sync, probe or search. `GET /api/bworker/activity` returns 200 with a degraded
flag when the store is unavailable. Current work and counts still come from
SQLite.

The bworker's cleanup unit is always enabled. It clears old finished jobs
(older than seven days) and trims the activity log, including while paused.

## Connection limits across processes

The per-playlist connection limit is enforced by `iptv_charts/connections.py`
with one `flock` file per source/slot under `<data>/locks`. `flock` is atomic
across processes and the kernel releases it on process death, so a crashed
download cannot leave a phantom slot.

The effective limit is:

1. `sources.connection_limit` when set;
2. `sources.max_connections` when positive;
3. otherwise **1**.

Operations taking a slot: playlist syncs (one for the whole sync),
`get_series_info` lookups, ffprobe URL probes, downloads (claim through import)
and the series-episode browser API. The bworker's single-unit rule means a
sync, probe and download cannot overlap each other; a browser request can hold
a slot while the bworker waits. Playback started by Jellyfin or an external
player is outside the app's control.

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/bworker/status` | alive/paused, current unit/stage/progress, queue/work counts, heartbeat |
| POST | `/api/bworker/pause` | pause between units |
| POST | `/api/bworker/resume` | resume claiming |
| GET | `/api/bworker/activity` | newest activity entries; degraded 200 when Valkey is down |
| GET | `/api/bworker/policies` | policy rows and labels |
| PATCH | `/api/bworker/policies` | update enabled/attempts/delay/backoff/cap |
| POST | `/api/bworker/reset` | reset one work id/key/entity, or all units |

Status includes `alive` (heartbeat fresh), `paused`, `current` work, `counts`
(pending/running/retry/stopped/complete, queued/running jobs) and `activity`
availability. `/api/health` reports the bworker `worker` block and returns an
error status if the heartbeat is stale, but a paused bworker counts as alive.

## Running and deployment

Container: supervisor starts Valkey, gunicorn, the bworker and nginx in that
priority order. `podman logs -f iptv-charts` interleaves them. The bworker has
no socket; health is the app through nginx plus the bworker heartbeat.

Local development: `./setup.test.sh` starts `worker.py` (a compatibility shim
for `bworker.py`) beside the reloading web server and stops both on Ctrl+C.
If you run `python run.py` directly, also run `python bworker.py`; otherwise
syncs, searches and downloads stay queued and interactive searches report that
the bworker is unavailable.

Direct run without supervisor:

```sh
python run.py          # web only
python bworker.py      # background work, one process
```

## Failure and shutdown

- A handler exception becomes `error`/failure with its original message. The
  API maps expected types back to 404/400/409/502.
- A sync interrupted by shutdown is requeued on the next start. A download is
  left resumable; orphan recovery links it back to its work row.
- A running download writes `downloads.cancel_requested`; a sync writes
  `worker_jobs.cancel_requested`. The bworker checks these from its progress
  loop and stops at a safe point. A download re-checks its row after waiting
  for a provider slot and will not start if it was retired meanwhile.
- SIGTERM sets the stop event. The bworker finishes the current bounded unit (or
  stops at the next safe point), writes `stopped` heartbeats and releases the
  file lock. Supervisor then kills the process group after its stop window
  (30s).
- A second bworker exits rather than racing recovery and claiming a different
  unit.
- The web process can restart independently; queued work and progress live in
  SQLite. The bworker can restart independently; the web shows its stale
  heartbeat until it returns.
