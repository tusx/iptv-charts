# Movie candidates, pinned versions and additional files

The movie detail page is also the candidate workspace, modelled on the
Auto-Streams movie view. It lists every catalog release that plausibly matches
the movie, shows the cached ffprobe facts for candidates that have been
probed, and lets the user pin one.

## What is shown

`movie_candidates_view` combines:

- `matching.movie_candidates` — provider VODs scored against the movie's
  title, original title, alternative titles and year;
- the latest row in `stream_probes` for each candidate identity
  `(source_id, provider_remote_id)`;
- the current quality profile's decision.

Each card shows the provider, source, resolution, match score, probe state
(`Probed`, `Probe failed`, `Not probed`), resolution/video/HDR/audio/subtitle
counts/size when probed, and the profile's reason. The cards can be filtered
to all, probed, profile-allowed, or pinned candidates.

Nothing on page render contacts a provider. **Probe** and **Probe unprobed**
enqueue worker jobs; the worker holds the playlist connection slot and writes
the result back to `stream_probes`. Re-probe is available for a cached
candidate.

## Pinning

Only a successfully probed candidate can be pinned. There are two modes:

| Mode | Effect |
|---|---|
| **Only this** | Stops other active downloads for the movie and makes this candidate the main file even when the quality profile would reject it. Future searches keep using this candidate instead of the profile. |
| **Also download** | Keeps the profile search as the main file and queues the pinned candidate as an extra version, unless the quality profile picks the same candidate. The extra is queued even when the profile rejects it; it never becomes the main file. |

The pin is stored on `movies.candidate_source_id`,
`movies.candidate_remote_id`, `movies.candidate_mode` and
`movies.candidate_name`. Clearing the pin leaves already downloaded files in
place. An “only” pin whose candidate is already the main file is not
downloaded again: `movie_needs_search` returns false for the scheduled search,
and an explicit search reports that it is already the main file. Delete the
file (or use Manual search with a query) to force a fresh download.

`select_movie_candidate` requires a successful cached probe. The “only” mode
calls `downloads.cancel_for(movie_id)` and then enqueues a main download with
the profile check bypassed. The “also” mode asks the API to enqueue a
`search_movie` job; `search_movie` runs the normal profile decision and calls
`_queue_pinned_alternate` with the chosen release, so it only adds the
alternate when it differs from the profile pick (and skips a main file that
is already that same candidate, or a main download still in flight for it).

Retries are deliberately quiet: an automatic “only” search does not repeat a
main download that failed in the last 24 hours, and an automatic “also”
search does not repeat a failed alternate for the same candidate. **Search
now** and the pin buttons pass `reason=manual`, so an explicit user action
still retries immediately.

## Additional versions

Additional downloads use `downloads.mode = 'alternate'`. Their import path
goes to `movie_alternates`, never to `movies.file_path`, and gets an
`[alternate]` suffix so it can sit beside the main file:

```
Movies/The Matrix (1999)/The Matrix (1999) [2160p].mkv
Movies/The Matrix (1999)/The Matrix (1999) [1080p] [alternate].mp4
```

The movie page lists these under **Additional versions** with a file size and
a Remove action. Removing a movie with `delete_files` removes these files as
well. They are included in backup/restore (the table is optional for backups
made before schema v4).

The extra download is independent of the main queue slot: a movie can have
one active main download and one active alternate download, but the worker
still processes them one at a time. The movie page shows and can cancel each
active download separately. An alternate never triggers the main file's
upgrade deletion. If the same candidate later becomes the main file (for
example through an upgrade or an “only” pin), the main import removes the
duplicate alternate row and file.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/movies/<id>/candidates` | candidate view + cached probe facts |
| POST | `/api/movies/<id>/candidates/probe` | probe one candidate (worker job) |
| POST | `/api/movies/<id>/candidates/probe-all` | probe unprobed/failed candidates (worker job) |
| POST | `/api/movies/<id>/candidate` | pin; `mode` = `only`/`also`, `grab` optional |
| DELETE | `/api/movies/<id>/candidate` | clear the pin |
| DELETE | `/api/movies/<id>/alternates/<id>` | remove an extra version and its file |

The movie detail page calls these endpoints and reloads; all provider work
remains in the background worker.
