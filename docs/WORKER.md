# Background worker and job queue

IPTV Charts runs as two processes in one container:

```
supervisord
├── python run.py       web server: HTTP, SQLite reads, enqueue only
└── python worker.py    one infinite loop: one job at a time
```

The split exists because playlist syncs, ffprobe searches, downloads, imports,
metadata refreshes and library scans are slow, network-bound and sometimes
disk-heavy. None of that should occupy a waitress request thread or disappear
when the web front end is reloaded. The web process still renders pages and
serves playback, but it never opens a provider download, runs ffprobe or
renames a media file.

## The queue

Queue rows live in SQLite, in `worker_jobs`:

| column | meaning |
|---|---|
| `kind` | handler name (see below) |
| `payload` | JSON arguments, e.g. `{"source_id": 2}` |
| `status` | `queued`, `running`, `done`, `error`, `cancelled` |
| `priority` | higher first; interactive searches 55–60, syncs 20, tasks 15 |
| `dedupe_key` | active-job uniqueness (a partial unique index) |
| `available_at` | retries can be scheduled for the future |
| `progress_data` | JSON: sync phase/detail/bytes/items |
| `result` / `error` | JSON result or user-facing failure |

`iptv_charts/jobs.py` owns enqueue/claim/finish. The worker uses
`BEGIN IMMEDIATE` to claim the next due row, marks it running, and records the
outcome. Only one worker exists, so exactly one job runs at a time. If the
worker is killed, the next start requeues every row left in `running`;
download rows interrupted mid-transfer are reset to `queued` and their temp
files are resumed.

`current_job_id` and a heartbeat live in `worker_state`; a lock-free
`worker.heartbeat.json` is written alongside it so a large restore holding
SQLite's writer does not make a live worker look dead. `/api/health` and
Activity expose whether a worker is actually alive, rather than showing a
healthy UI over a queue that nothing is draining. A `worker.lock` file taken
with `flock` at startup makes the one-worker rule explicit: a second worker
exits instead of racing recovery and claiming a different job.

## Job kinds

| kind | work |
|---|---|
| `sync` | one playlist ingest (`sync.run_job`) |
| `download` | one queued HTTP/HLS download and atomic import (`downloader.process_download`) |
| `task` | one scheduled/manual library task (`scheduler.run_task`) |
| `search_movie` | automatic or manual movie matching + URL probes |
| `search_series` | series or one episode matching + URL probes |
| `movie_releases` | manual movie release list with ffprobe facts |
| `episode_releases` | manual episode release list |
| `grab_movie` / `grab_episode` | re-probe the selected release, then enqueue the download |
| `refresh_movie` / `refresh_series` | metadata refresh from TMDB/TVDB |
| `probe_movie_candidate` / `probe_movie_candidates` | ffprobe one candidate or the movie candidate list |
| `scan_import` / `scan_import_all` | import reviewed scan results (may add metadata and probe files) |

Automatic search, scheduled tasks and downloads enqueue and return
immediately. Interactive endpoints (manual search, release lists, grabs and
metadata refresh) enqueue a job and poll its row while the request is open.
The HTTP thread is waiting, not doing ffprobe; the heavy work is still in the
worker process. If the worker is down, the request is still queued and the
endpoint returns a clear 503; restarting the worker runs it.

## Periodic tasks

`worker.run_forever` calls `scheduler.enqueue_due()` before claiming. That
reads `task_state` and inserts a `task` job when a periodic task is due. It is
database-only work; the actual search/refresh/scan runs as the job. If a
playlist sync is queued or running, search and metadata tasks are deferred for
five minutes so they never use a half-replaced catalog. `refresh_catalog`
queues sync jobs and returns; because syncs have a higher priority, they run
before the next search pass. A manual search/metadata task that arrives while
a sync is queued is deferred for five minutes rather than reading the catalog
being replaced. `IPTV_CHARTS_SCHEDULER=0` disables the automatic enqueue
(manual task triggers still work).

## Connection limits across processes

The per-playlist connection limit must hold across the web and worker. The
registry is now filesystem-based: one lock file per source/slot under
`<data>/locks`, held with `flock`. `flock` is atomic across processes and the
kernel releases it if a process dies, so a crashed download cannot leave a
permanent phantom slot. The web process can acquire a slot for the series
episode browser while the worker holds one for a download, and the UI counts
both. Playback started outside the app still cannot be counted.

## Running

Container:

```sh
./setup.deploy.sh
```

Supervisor starts both processes; `podman logs -f iptv-charts` interleaves
them. The web health check stays on `run.py`; `/api/health` also reports the
worker heartbeat and queue counts.

Local development:

```sh
./setup.test.sh
```

That starts `worker.py` beside the reloading web server and stops both on
Ctrl+C. If you run `python run.py` directly, start `python worker.py` in another
terminal; otherwise syncs and downloads stay queued and interactive searches
report that the worker is unavailable.

## Failure and shutdown

- A job handler exception is recorded as `error` with the original exception
  type, which the API maps back to 404/400/409/502 where appropriate.
- A download's transient failure remains in the existing retry schedule
  (60 s, then 2 min, …); the worker job is rescheduled for `next_attempt_at`.
- A queued job is marked cancelled immediately, which frees its dedupe key.
  A running download writes `downloads.cancel_requested` and a sync writes
  `worker_jobs.cancel_requested`; the worker notices from its progress loop
  and stops at a safe point. A download re-checks its row after waiting for a
  provider slot and will not start if it was retired in the meantime.
- SIGTERM stops the loop; an in-flight download is left resumable and a sync is
  interrupted, its job requeued on the next start.
- Finished jobs older than seven days are pruned when the worker is idle.
