# Candidate acquisition, pins and additional versions

Movie and series search has two paths that share the same scoring engine:

- **automatic acquisition** — a bounded, durable pipeline that discovers
  candidates ID-first, probes a few per iteration and hands one release to the
  downloader;
- **manual search and the movie candidate workspace** — a bounded list of
  matching releases with probe facts, profile decisions and pins.

Nothing on a detail page contacts a provider during render. Probe and search
actions enqueue work; the bworker holds the connection slot and writes results
back to SQLite.

## Identity first

A library row carries a TMDB ID and, for movies, an IMDb ID. Candidate lookup
uses the highest identity that **actually exists in the local catalog**:

1. **TMDB ID**, if at least one matching catalog row carries it;
2. **IMDb ID**, if at least one matching catalog row carries it;
3. **title/year/alternative titles**, scored when neither id has a match.

An id that the provider never supplied cannot win just because the library row
has it. `matching.identity_match_mode` is the single entry point for both the
automatic engine and manual lists.

`items.identity_catalog` selects exact TMDB/IMDb rows with a bounded
`ORDER BY id LIMIT 50..500`, never OFFSET. The paged engine uses
`items.catalog_page`, whose every selector is keyset-based:

```sql
WHERE kind = ? AND id > ? [AND tmdb_id = ? | imdb_id = ?]
ORDER BY id LIMIT ?
```

Identity rows are authoritative. A title fallback must reach the minimum title
score (0.85 for automatic discovery, 0.45 for a manual list unless the user
typed a query). A provider VOD row whose parsed name is clearly an episode is
skipped even when its identity matches, because M3U playlists mix episodes into
their VOD listing.

## The automatic pipeline

Each iteration is one bounded bworker unit:

1. **Discover** at most `candidate_batch_size` new matches (default **25**,
   maximum 200). Page size is `batch × 4`, clamped to 50–200, with at most six
   pages. Progress is committed after every page, so a stop or crash keeps the
   keyset cursor. Discovery stops early once the batch is full or the catalog
   is exhausted.
2. **Expand** at most **3** provider series containers into the target
   episode's stream candidates. Deferred containers are recorded for a later
   iteration.
3. **Probe** at most `probe_batch_per_loop` pending candidates (default **3**,
   UI-capped at 3), one ffprobe subprocess at a time, checking the pause
   authority between probes. Results and profile scores are written to
   `bworker_candidates`.
4. **Select**:
   - a target-met candidate always wins;
   - if none exists and the candidate pool is exhausted, the best eligible
     lower-resolution fallback may be chosen and marked `awaiting_upgrade`;
   - the provider identity already backing the current file is excluded;
   - a current target-met file blocks replacement; a current eligible file
     blocks equal/lower scores; `upgrades = false` blocks replacement entirely.
5. **Hand off** one release to the downloader or ask for another iteration.

The continuation mechanism is a queued `search_movie`/`search_series` job with
`reason=auto` and a dedupe token derived from the scan id, cursor, counts and
available time. A continuation already queued for the same token is not
duplicated, and a running continuation can queue its successor. When a
provider detail supplies identification ids mid-scan, the engine records
`mode_changed`, stops the current pass and starts an ID-first rescan.

### Durable state

| Table | Contents |
|---|---|
| `bworker_scan` | work id, item scope, query fingerprint, catalog epoch, cursor, counts, exhausted flag, status |
| `bworker_candidates` | provider identity, episode stream id, name, parsed resolution/source, match kind/score, probe state, profile eligibility/score/target/needs-upgrade, status |

The scan is pinned to a **catalog epoch**; a playlist sync bumps the epoch and
invalidates stale candidates. A fingerprint covers identity mode/value,
titles/year, profile id/revision, season/episode and query, so changing the
profile, item identity or query starts a fresh scan. Duplicate provider
identities are collapsed. The engine never selects an item row that no longer
exists or belongs to a different source.

### Automatic selection and retries

A candidate that fails the mandatory resolution or source rule is not
downloaded. A release that matches but misses the target stays in the pool for
a later comparison. After the pool is exhausted, the best eligible fallback is
used only when there is no current file or the current file is not eligible;
a current eligible file must be beaten by a strictly higher score.

A profile rejection is a successful iteration, not a retry failure: the work
records `no_candidates` or `awaiting_upgrade` and waits for the search
interval. If a download for the same item failed within 24 hours, automatic
work skips the immediate retry and reports the reason; an explicit manual
search/pin still retries. A provider slot that is busy defers the next
iteration instead of grabbing unverified.

### Interplay with the bworker

The engine writes stage/cursor/best-candidate progress onto the claimed
`bworker_work` row and records item events. A paused bworker checks the pause
authority between discovery pages and probes, persists the cursor and returns
`paused`, then continues from the cursor after resume. A stop (SIGTERM) leaves
the work pending; the next start recovers it. The single-unit rule means a
candidate probe cannot run while a playlist sync or download is active.

The acquisition engine is a module (`iptv_charts/acquisition/`) with a small
public API (`run_movie_iteration`, `run_episode_iteration`,
`bounded_movie_search`, `annotate_download`, diagnostics helpers). It is
deliberately decoupled from the bworker loop so the pipeline can be tested
directly; the bworker's search handlers invoke it and execute its continuation
jobs.

## Manual search

Manual search accepts a query override. A movie candidate list normally uses
identity lookup; a typed query switches to title matching so the user's intent
wins. `matching.movie_candidates` returns at most 200 rows, sorted by identity
precedence, match score, preferred resolution and name.

The movie detail page is also the **candidate workspace**. It combines:

- provider VODs from `matching.movie_candidates`;
- the latest `stream_probes` row for each `(source_id, provider_remote_id)`;
- the current weighted profile's decision for each candidate.

Each card shows provider, source, resolution, match type/score, probe state
(`Probed`, `Probe failed`, `Not probed`), video/HDR, audio and subtitle counts,
size and the profile score/target/needs-upgrade reason. Cards can be filtered
to all, probed, profile-allowed or pinned candidates. **Probe** and **Probe
unprobed** enqueue jobs; re-probe reuses or refreshes the cache.

## Pinning

Only a successfully probed candidate can be pinned:

| Mode | Effect |
|---|---|
| **Only this** | Stops other active attempts for the movie and makes this candidate the main file even when the profile rejects it. Future searches keep using it instead of the profile pick. |
| **Also download** | Keeps the profile pick as the main file and queues this candidate as an extra version. It never becomes the main file, and it is skipped when the profile picks the same candidate. |

The pin is stored on `movies.candidate_source_id`,
`movies.candidate_remote_id`, `movies.candidate_mode` and
`movies.candidate_name`. Clearing it leaves downloaded files in place. An
**Only this** pin whose candidate is already the main file is not downloaded
again; delete the file or use a manual query to force a fresh download.

`select_movie_candidate` requires a successful cached probe. **Only this**
cancels other active downloads for the movie and queues a main download with
the profile check bypassed. **Also download** runs the normal search/selection
and adds the pinned candidate as an alternate only when it differs from the
profile pick.

Automatic retries stay quiet: an automatic pin search does not repeat a main
download that failed in the last 24 hours, and an automatic extra search does
not repeat a failed alternate for the same candidate. The explicit pin buttons
and **Search now** pass `reason=manual`, so they retry immediately.

## Additional versions

Alternate downloads use `downloads.mode = 'alternate'`. They import into
`movie_alternates`, never into `movies.file_path`, with an `[alternate]` suffix:

```
Movies/The Matrix (1999)/The Matrix (1999) [2160p].mkv
Movies/The Matrix (1999)/The Matrix (1999) [1080p] [alternate].mp4
```

The movie page lists them under **Additional versions** with a file size and
Remove action. Removing a movie with `delete_files` removes these files too.
They are included in backup/restore (optional for backups made before schema
v4). An alternate never triggers deletion of the main file's upgrade. If the
same candidate later becomes the main file, the main import folds away the
duplicate alternate row and file.

A movie can have one active main and one active alternate download, but the
bworker still processes them one unit at a time. The movie page shows and
cancels each active download separately. The scan stage and recent events are
rendered from `bworker_events`.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/movies/<id>/candidates` | candidate view + cached probe facts |
| POST | `/api/movies/<id>/candidates/probe` | probe one candidate (worker job) |
| POST | `/api/movies/<id>/candidates/probe-all` | probe unprobed/failed candidates |
| POST | `/api/movies/<id>/candidate` | pin; `mode` = `only`/`also`, `grab` optional |
| DELETE | `/api/movies/<id>/candidate` | clear the pin |
| DELETE | `/api/movies/<id>/alternates/<id>` | remove an extra version and its file |

Manual provider work remains in the background queue; the detail page polls the
job and reloads the candidate view. Automatic acquisition state is visible
through the item stage endpoints and Activity, not by exposing provider URLs.
