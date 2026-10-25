# Weighted profiles, probing, connections and monitoring

This document describes the parts of the manager that decide **whether a
provider stream is good enough**, **when it may be downloaded**, and **how many
provider operations may run at once**. It complements the README, which covers
the user-facing workflow.

## 1. The weighted profile model

A 0.8.0 profile has two mandatory facts and six optional sections:

```json
{
  "name": "1080p",
  "resolution": "1080p",
  "resolution_weight": 10,
  "quality_options": [
    {"value": "web-dl", "label": "WEB-DL", "weight": 10},
    {"value": "bluray", "label": "BluRay", "weight": 10}
  ],
  "video_codecs": [],
  "audio_codecs": [{"value": "eac3", "label": "E-AC-3", "weight": 10}],
  "audio_languages": [{"value": "original", "label": "Original language", "weight": 5}],
  "audio_channels": [],
  "subtitle_formats": [],
  "subtitle_languages": [{"value": "eng", "label": "English", "weight": 5}],
  "target_score": 40,
  "upgrades": true
}
```

### Mandatory matching

1. **Resolution.** The candidate must use the profile's selected resolution.
   A lower resolution is eligible only when `upgrades` is enabled; a higher
   resolution, an unknown resolution, or no profile resolution is rejected.
   Source/quality is deliberately not part of a profile: provider metadata is
   frequently missing, so it must not block a release.

The resolution match sets the **base score**:

```
base_score = resolution_weight
```

The default resolution weight is 10, so the minimum score is 10. Weights are
whole numbers from **1 to 1000**.

### Optional sections

| Section | Kind | Default weight | Contribution |
|---|---|---|---|
| `video_codecs` | exclusive | 10 | The single best matching selected codec |
| `audio_codecs` | exclusive | 10 | The single best matching selected codec |
| `audio_languages` | multi | 5 | Every selected language present |
| `audio_channels` | exclusive | 5 | The single best matching channel layout |
| `subtitle_formats` | multi | 5 | Every selected format present |
| `subtitle_languages` | multi | 5 | Every selected language present |

An optional match only **adds** its weight. An unmatched optional criterion
adds zero and never rejects a candidate. A section with no selected options
contributes zero. `audio_languages` and `subtitle_languages` accept the
special `original` value and an `any language` value; language tags are folded
across ISO 639-1/2B (`en`/`eng`, `es`/`spa`, regional forms such as `pt-BR`)
by `languages.normalise()`.

`max_score` is computed as the mandatory maximum plus each optional section's
maximum:

- mandatory maximum = resolution weight;
- exclusive section maximum = highest selected option in that section;
- multi section maximum = sum of every selected option in that section.

`target_score` must be between **10** and `max_score`. It defaults to
`max_score`.

### Target and fallback

```
target_met = exact_resolution AND score >= target_score
```

A lower-resolution candidate can be eligible, can score above the target, and
still is **not** target-met. It is a fallback (`needs_upgrade = true`). A
candidate using the exact resolution is never marked as needing an upgrade.

Selection follows this order:

1. Among eligible candidates, target-met candidates always win.
2. If none meets the target, the best eligible fallback is chosen after the
   candidate pool is exhausted.
3. Ties are broken by score, exact resolution, provider match score, size and
   finally name (deterministic).
4. A candidate that fails the mandatory resolution rule is never downloaded.
   If nothing is eligible, the item stays missing/no-candidates.

A downloaded fallback is stored as `awaiting_upgrade`. The interval search can
later replace it only with a better score; a target-met file is not replaced
when upgrades are enabled. With `upgrades = false`, an existing file is never
replaced and a lower-resolution fallback is not eligible for a new item.

### Current-file scoring

The current file is re-scored from its stored `media_info` every time a
replacement decision is made. A score written by an older profile revision is
not trusted. If the current file is target-met, no replacement is considered.
If it is eligible but not target-met, only a strictly higher-scoring candidate
can replace it. If it is not eligible at all (for example its source is no
longer selected), any eligible candidate can replace it.

### Built-in profiles and the builder

Three profiles are inserted and repaired at startup:

| Name | Resolution | Target / maximum |
|---|---|---|
| **4K** | 2160p | 10 / 10 |
| **1080p** (default) | 1080p | 10 / 10 |
| **720p** | 720p | 10 / 10 |

The resolution is worth 10, no optional sections are selected, and both target
and maximum are **10**. Built-ins are
immutable: edit/delete requests return HTTP 409 and the names `4K`, `1080p` and
`720p` are reserved for them. Startup repairs a missing or damaged built-in row
without deleting a custom profile that happens to use a reserved name; the
custom row is renamed instead.

Custom profiles are created, edited and deleted through the profile API and the
builder page. Validation rejects an unknown resolution, duplicate or unknown
optional options, weights outside 1–1000, a maximum below 10, and a target
outside 10–maximum. The default profile used by the add pages and import flow
is one of the built-ins and starts at 1080p; a custom profile is still
selectable per movie or series.

The legacy ordered-quality columns remain only for database compatibility and
old backup readers. They are not part of the 0.8.0 model and are not exposed as
profile features.

## 2. Probing

`probe.py` runs:

```
ffprobe -v error -print_format json -show_format -show_streams
        -protocol_whitelist http,https,tcp,tls,crypto
        -rw_timeout <timeout_us>
        -analyzeduration 5000000 -probesize 5000000
        [-headers "<Name>: <value>\r\n..."]
        -i <provider URL>
```

Properties:

- **No download.** ffprobe reads only what is needed to identify streams.
- **No shell.** argv is constructed directly; headers are sanitised and CR/LF
  is refused.
- **Provider headers** merge source and per-item M3U headers exactly as
  downloads use them.
- **Timeout** comes from `probe_timeout_seconds` (default 30s), enforced by
  `-rw_timeout` and a process-group kill.
- **Normalisation** reports resolution/width/height, video codec and HDR
  (Dolby Vision/HDR10/HLG/SDR), every audio track (codec, channels, layout,
  language, title, default), every embedded subtitle track (codec, language,
  title, default, forced), container, duration, size and bitrate.
- **Cache** is keyed by `(source_id, provider_kind, provider_remote_id,
  episode_stream_id)` in `stream_probes`. The TTL is 24 hours by default
  (`probe_cache_hours`); failed probes are retried after five minutes. Probe
  cache is derived data, excluded from backups and clearable from the API.
- **Connection accounting.** A probe takes a playlist connection slot. If none
  is free within the caller's timeout, it reports a busy error and no release
  is grabbed.
- **Probe before grab.** Automatic acquisition probes up to
  `probe_batch_per_loop` candidates (default and UI maximum **3**), one ffprobe
  subprocess at a time, and checks the bworker pause authority between probes.
  Manual search and grab probe on demand or reuse a fresh cache entry. The
  profile decision uses probe facts, so a release with unknown resolution or
  source is not accepted. There is no "skip the probe" profile option.

`probe_max_candidates` and `probe_search_budget` are legacy settings kept for
old API clients; automatic acquisition uses the per-section candidate batch and
probes-per-iteration values instead. They do not change the 0.8.0 pipeline.

## 3. Connection limits

`connections.py` enforces active provider operations per playlist with one
`flock` file per source and slot under `<data>/locks`. The kernel lock is atomic
across the web and bworker processes and is released if a process dies, so the
limit covers a browser episode request and a worker download at the same time.
A source's effective limit is:

1. `sources.connection_limit` when set;
2. `sources.max_connections` (provider-reported) when positive;
3. otherwise **1**.

Operations that acquire a slot:

- playlist syncs (one slot for the whole source sync);
- `get_series_info` fetches used by searches, grabs and the episode browser;
- ffprobe URL probes;
- downloads (held from claim through import);
- the series-episode browser API.

All operations use the same blocking `acquire`. The bworker runs one unit at a
time, so a sync, probe and download cannot overlap each other. A browser
episode request can hold a slot while the bworker waits; the worker resumes
when it is released. The UI shows active slots against the effective limit on
each playlist card. Playback started by Jellyfin, VLC or another external
player is outside the app's control and cannot be counted.

## 4. Candidate acquisition limits

Automatic acquisition is bounded by section settings and per-item overrides:

| Setting | Default | Meaning |
|---|---|---|
| `candidate_batch_size` | 25 | New matching releases discovered per scope per iteration; maximum 200 |
| `probe_batch_per_loop` | 3 | Pending candidates probed per iteration; one at a time |
| `movie_search_interval_hours` | 6 | Re-search interval for a movie |
| `series_search_interval_hours` | 4 | Re-search interval for a series/episode |

Up to three provider series containers are expanded into episode streams per
iteration. Candidate discovery uses keyset paging (`id > cursor`) with a
bounded page size and at most six pages per iteration; it does not use OFFSET.
A per-item `search_interval_hours` override wins over the section setting.
Setting an interval to `0` disables re-search for that item/section.

The engine persists the cursor, query fingerprint, catalog epoch, best
candidate and scan/candidate pages in `bworker_scan`/`bworker_candidates`. A
catalog sync bumps the epoch so stale item ids cannot be selected. Duplicate
provider identities are collapsed, and the provider identity already backing
the current file is never downloaded again. A manual search is synchronous from
the caller's point of view but still bounded and uses the same scoring rules.

## 5. Retries and failure

Automatic work retries are owned by the bworker policy for its work type, not
by the download row:

- movie/series/episode search: infinite retries, delay/cap 6h/4h;
- download: 3 attempts, 60s with ×2 backoff, 15min cap;
- probe: 3 attempts, 5min, 5min cap;
- playlist sync: infinite, 5min ×2, 1h cap;
- metadata refresh: infinite, 7d, 7d cap;
- library scan: 3 attempts, 15min ×2, 15min cap.

A profile rejection is not a retry failure. It produces `no_candidates` (or
`awaiting_upgrade` when a fallback was grabbed) and waits for the next search
interval. A transient handler error increments the failure count and schedules
the policy's backoff. When attempts are exhausted the work becomes
`failed_stopped`, keeps the error for the UI, and can be reset from Activity.
Manual download rows retain their own durable retry schedule
(`download_max_attempts`, default 3); automatic downloads pass one attempt and
let the bworker policy decide when to try again.

`download_concurrency` is a legacy value. The bworker is single-unit by design;
there is no UI concurrency control.

## 6. Monitoring and availability

- **Movies** store `minimum_availability`:
  - `announced` — search immediately;
  - `in_cinemas` — wait until `release_date` <= today;
  - `released` — wait for the date and for TMDB status "Released" when present.
  A future movie stays monitored and appears in the Movies calendar; its bworker
  work starts at `waiting_availability`.
- **Series** store `monitor_new_seasons`:
  - `all` — newly discovered episodes are monitored;
  - `future` — only episodes whose air date is today/future or unknown;
  - `none` — newly discovered episodes are not monitored.
  Season 0 (specials) is never auto-monitored. Existing monitored flags are not
  overwritten by a metadata refresh.
- **Add mode** (`monitor_mode`) sets the initial episode flags: all, future,
  first season, latest season or none. It is not a permanent rule;
  `monitor_new_seasons` governs later discoveries.
- A future episode starts its work at `waiting_air`; when the air date arrives
  it moves to search.

## 7. Settings ownership

| Key | Default | Where |
|---|---|---|
| `probe_cache_hours` | 24 | Stored setting; probe cache TTL |
| `probe_timeout_seconds` | 30 | Stored setting; ffprobe timeout |
| `candidate_batch_size` | 25 | Movie and Series settings |
| `probe_batch_per_loop` | 3 | Movie and Series settings |
| `movie_search_interval_hours` | 6 | Movie settings; per-movie override |
| `series_search_interval_hours` | 4 | Series settings; per-series override |
| `download_max_attempts` | 3 | Manual download retry count |
| bworker policy values | see table above | Movie, Series and global policy editor |
| `probe_max_candidates` | 6 | Legacy compatibility only |
| `probe_search_budget` | 60 | Legacy compatibility only |
| `download_concurrency` | 1 | Legacy; no effect on the single-unit bworker |
