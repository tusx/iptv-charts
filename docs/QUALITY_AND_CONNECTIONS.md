# Quality profiles, stream probing and connection limits

This document describes the parts of the manager that decide **whether a
provider stream is good enough**, **when it may be downloaded**, and **how many
provider operations may run at once**. It complements the README, which covers
the user-facing workflow.

## 1. Quality tiers

A profile is an ordered list of tiers, best first. Each tier is:

```json
{
  "name": "2160p WEB-DL",
  "resolution": "2160p",
  "sources": ["web-dl"]
}
```

- `resolution` is one of `2160p`, `1080p`, `720p`, `480p`, `unknown`.
- `sources` is empty for "any source", or a subset of `remux`, `bluray`,
  `web-dl`, `webrip`, `hdtv`, `dvdrip`, `brrip`, `hdcam`, `cam`, `ts`, `tc`,
  `scr`, `r5`, `unknown`.
- The order is the hierarchy. `name` is a display label, generated from the
  resolution and source set unless supplied by the API.

A release is matched against the first tier whose resolution and source rules
accept it. Resolution comes from the probe when available, otherwise from the
name. Source comes from the provider name: `quality.parse_source()` recognises
WEB-DL/WEBRip/BluRay/HDTV/CAM/TS/etc. with word boundaries, so titles such as
"Camelot" are not treated as CAM.

A source-restricted tier **rejects an unknown source**. If the provider name
contains no source tag, a 2160p WEB-DL tier will not accept it. Use an
unrestricted 2160p tier if that is too strict.

### Cutoff and upgrades

`cutoff` is the name of a tier (or, for legacy profiles, a resolution). It is a
stopping point, not a ceiling:

1. No file: choose the best allowed tier.
2. File at or above the cutoff tier: no upgrade.
3. File below the cutoff: allow a release in a strictly better tier, if
   upgrades are enabled.
4. File outside every tier: it is treated as upgradeable to anything the
   profile allows.

The current file's rank uses its stored resolution and source. A missing/unknown
source is allowed to match a source-restricted tier for this comparison, so old
files do not become permanently upgradeable; a known source (for example CAM)
does not get that allowance.

## 2. Audio and subtitle requirements

These are profile-wide:

| Field | Values | Meaning |
|---|---|---|
| `audio_mode` | `any`, `original`, `single`, `multi` | No constraint; require original language; exactly one audio track; at least two audio tracks or two languages. |
| `audio_languages` | ISO 639-2/B list | If non-empty, at least one of these languages must be present. |
| `subtitle_mode` | `any`, `required`, `none` | No constraint; at least one embedded subtitle track; no subtitle tracks. |
| `subtitle_languages` | ISO 639-2/B list, or `original` | If `required` and non-empty, at least one subtitle track must match one of these. |

`original` resolves to the movie/series original language from TMDB. TMDB
provides ISO 639-1 (`en`, `es`), while ffprobe commonly tags tracks with ISO
639-2/B (`eng`, `spa`); `languages.normalise()` folds both, including the
terminologic variants `fra`/`fre`, `deu`/`ger`, `zho`/`chi`, and regional forms
such as `pt-BR`.

The probe reports every audio/subtitle track. If a track has no language tag,
it cannot satisfy an original-language or specific-language requirement.
Burned-in subtitles have no stream and cannot be detected.

A profile can disable **Require a successful probe**. Then:
- resolution/source come from the provider name when the probe fails;
- any audio/subtitle requirement still forces a probe, because those facts
  cannot be inferred from the name.

## 3. Probing

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

- **No download.** ffprobe reads only what it needs to identify the streams.
- **No shell.** The argv is constructed directly; headers are sanitised and
  CR/LF is refused.
- **Provider headers** are merged from the source and per-item M3U headers,
  exactly as downloads use them.
- **Timeout** is the `probe_timeout_seconds` setting (default 30s), enforced by
  both `-rw_timeout` and a process-group kill.
- **Normalisation** reports:
  - `resolution`, width/height, codec, HDR (Dolby Vision/HDR10/HLG/SDR);
  - audio tracks: codec, channels, layout, language, title, default;
  - subtitle tracks: codec, language, title, default, forced;
  - container, duration, size, bitrate.
- **Cache** keyed by `(source_id, provider_kind, provider_remote_id,
  episode_stream_id)` in `stream_probes`. The default TTL is 24 hours; failed
  probes are retried after 5 minutes. The cache is derived data, excluded from
  backups and clearable in Settings.
- **Connection accounting.** A probe takes a playlist connection slot. If none
  is free within the caller's timeout (5s for automatic search, 30s for manual
  search/grab), the probe reports a busy error and the release is not grabbed.
- **Probe first.** Automatic search probes candidates up to
  `probe_max_candidates` (default 6) and spends at most `probe_search_budget`
  (default 60) probes per run. The profile decision uses the probe facts. A
  manual grab probes the selected release again (or reuses the cache) and
  refuses it if the profile rejects it.

## 4. Connection limits

`connections.py` enforces active provider operations per playlist with one
`flock` file per source and slot under `<data>/locks`. The kernel lock is
atomic across the web and worker processes and is released if either process
dies, so the limit applies to a browser episode request and a worker download
at the same time. A source's effective limit is:

1. `sources.connection_limit` if set,
2. `sources.max_connections` (provider-reported) if positive,
3. otherwise **1**.

Operations that acquire a slot:

- playlist syncs (one slot for the whole source sync);
- `get_series_info` fetches used by searches and grabs;
- ffprobe URL probes;
- downloads (the slot is held from claim through import);
- the series-episode browser API.

All operations use the same blocking `acquire`; the background worker runs one
job at a time, so a sync, a probe/search and a download can never overlap. The
waiting case is therefore short: a browser episode call may hold a slot while
the worker waits, and the worker resumes when it is released. A source whose
slots are full is retried after the current browser request finishes.

The kernel cannot see playback started by Jellyfin or an external player;
those connections are outside the app's control. The UI counts the app's
active slots next to the effective limit on each playlist card.

## 5. Queue priority and ordering

`downloads.priority` defaults to 0, is validated to -100..100, and is inherited
from the movie/series when a grab is queued. The worker's queue is ordered by
job priority, then id; a download job's priority mirrors its `downloads` row.
Interactive searches use 55–60, playlist syncs 20, scheduled tasks 15, and
downloads their own -100..100 value, so a queued sync/search is not starved by
an endless download queue. Priority edits are available in Activity and the
API; the scheduler does not override them.

## 6. Monitoring and availability

- **Movies** store `minimum_availability`:
  - `announced` — search immediately;
  - `in_cinemas` — wait until `release_date` <= today;
  - `released` — wait for the date and for TMDB status "Released" when a status
    is present.
  A future movie remains monitored and appears in Calendar, but automatic
  search reports that it is not available yet.
- **Series** store `monitor_new_seasons`:
  - `all` — newly discovered episodes are monitored;
  - `future` — only episodes whose air date is today/future or unknown;
  - `none` — newly discovered episodes are not monitored.
  Season 0 (specials) is never auto-monitored. Existing monitored flags are
  never overwritten by a metadata refresh.
- **Add mode** (`monitor_mode`) sets the initial episode flags: all, future,
  first season, latest season, or none. It is not a permanent rule; the
  `monitor_new_seasons` setting governs later discoveries.

## 7. Relevant settings

Stored in `settings` and editable in the UI:

| Key | Default | Purpose |
|---|---|---|
| `probe_cache_hours` | 24 | Probe cache TTL; 0 disables cache hits. |
| `probe_timeout_seconds` | 30 | ffprobe wall-clock timeout. |
| `probe_max_candidates` | 6 | Candidates probed per search. |
| `probe_search_budget` | 60 | Probe budget per automatic/series search run. |
| `download_concurrency` | 1 | Legacy value; the worker is single-job by design. |
| `download_max_attempts` | 3 | Transient download attempts. |
