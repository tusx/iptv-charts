# Home, playlists and split settings

This document covers the 0.8.0 page structure and the rules behind editing a
playlist and saving settings. Settings are owned by the section that uses them:
there is no longer one unified form that can silently change unrelated values.

## Home dashboard

`/` renders three cards:

- **IPTV** — playlist count, catalog item count and a compact row per playlist
  with Browse, Edit, Update, Cancel and Remove. The card also has Add playlist,
  Update all and Manage actions, plus the update-all progress bar.
- **Movies** — **In library**, **Monitored**, **Downloaded** and **Missing**
  counts, with Open movies, Add movie, Calendar, Wanted, Search missing and
  Settings.
- **Series** — **Series**, **Episodes**, **Downloaded** and **Missing episodes**
  counts, with Open series, Add series, Calendar, Wanted, Search missing and
  Settings.

The top bar contains only the brand; there is no global navigation menu. The
home page header links to Activity and Settings. Movies and Series each carry
their own **Library / Wanted / Calendar** tabs, so those lists are always
section-scoped. The missing metrics are actionable: **Search missing** runs an
explicit search for only the current section.

The Add movie/series links use `?add=1`; the section page opens its dialog on
load and removes the query parameter so a reload does not reopen it. The add
playlist dialog is shared between home and `/playlists` so the two cannot
drift.

The responsive layout stacks the cards and playlist rows on narrow screens;
wide tables scroll rather than forcing the page sideways. Icons come from the
vendored Font Awesome stylesheet. No page depends on a CDN or a JavaScript
build step.

## Playlist management page

`/playlists` is the full management page. It contains the detailed playlist
cards, Add playlist, Update all and the same per-playlist actions as the home
card. The shared add dialog lives in `_add_playlist_dialog.html`.

At the bottom is the **IPTV settings** card (`#iptv-settings`). It owns:

- **External player enabled** — off by default. While off, player buttons are
  hidden and the player fields are disabled. Browser playback is unaffected.
- **Player choice** — mpv, VLC (native or Flatpak), or a custom command. The
  host bridge reports which players it can see in the container.
- **Custom command** with `{url}`, `{title}`, `{user_agent}` and `{referrer}`
  placeholders. If the command never mentions `{url}`, the URL is appended.
  Nothing is passed through a shell; quotes only group arguments.
- **Check command syntax** — parses the command and returns a validation
  message. It never runs the command.
- **Playlist stale after (hours)** — 1 to 8760; the playlist card shows a
  refresh reminder after that interval.

Connection limits stay per playlist; there is no shared default that could
silently override a provider's limit. The IPTV card saves separately from the
playlist cards and from the global settings page.

## Playlist edit page

`GET /playlists/<id>/edit` renders the provider fields for the playlist's kind:

- common: name, User-Agent, Referrer, connection limit;
- M3U: playlist URL;
- Xtream: server URL, username, password.

The kind is read-only. M3U and Xtream have different identity models and stream
URL formats; changing type is a remove-and-add operation.

The stored password is never selected or rendered. Its placeholder says that a
blank value keeps it. A blank password may only be used by the Test connection
endpoint when the submitted server URL and username still match the stored
ones. Changing either requires the password again. This prevents the endpoint
from forwarding the stored password to a caller-supplied server.

### Saving and catalog identity

`PATCH /api/sources/<id>` updates the fields and returns
`catalog_changed`/`sync_queued`. Name, connection-limit and header-only changes
are cheap and do not resync. A change to the M3U URL, Xtream server URL,
username or password is an identity change:

1. Active downloads for that source are refused until cancelled.
2. Queued downloads for the source are cancelled.
3. Items, categories, `provider_series_cache` and `stream_probes` for the
   source are deleted.
4. The account snapshot (`user_info`, server fields, expiry, connections,
   formats) is cleared and `sync_status` returns to `never`.
5. A full source sync is queued.

The transaction leaves the app with an empty, honest catalog until the new
account sync succeeds, rather than showing old remote IDs against new
credentials. Library movie/series entries and their files are not touched; a
queued download that would have used the old account is marked cancelled.

A playlist cannot be edited while its own sync is queued or running (HTTP 409).
Cancel the sync first.

## Split settings ownership

| Page | Fields | Save behaviour |
|---|---|---|
| `/settings` | TMDB token, metadata language, TVDB key/PIN, refresh intervals | One atomic **Save all settings** for global fields |
| `/playlists#iptv-settings` | External player switch/choice/command, stale-after | Independent IPTV save |
| `/movies/settings` | Movie folder/file naming, default profile, default availability, search interval, candidate batch, probes, policies | Independent Movie save + live naming preview |
| `/series/settings` | Series/season/episode naming, default profile, monitor mode, new-season policy, search interval, candidate batch, probes, policies | Independent Series save + live naming preview |

`/settings` also has section link cards to the three pages, a quality-profile
entry, library scan and backup/restore. Those are actions or links, not part of
the global form. The global form validates every supplied field before writing,
then upserts them in one `BEGIN IMMEDIATE` transaction; an invalid naming value
or interval cannot leave another global field half-saved.

Section pages validate and save only their own keys. The UI sends JSON to
`POST /api/settings/sections/movies` or `.../series`; a preview endpoint
renders the naming templates without writing. A failed field returns a section
error and leaves the previous values untouched. IPTV settings use
`GET/POST /api/settings/sections/iptv` plus
`POST /api/settings/sections/iptv/command-check`.

### Global settings details

- A blank metadata key is omitted by the UI and keeps the stored value.
- `tmdb_clear` removes the stored TMDB read token; `tvdb_clear` removes the
  TVDB key and PIN and the cached TVDB token.
- Metadata test buttons test **saved** keys, not unsaved input.
- The TMDB field expects the API Read Access Token (the JWT beginning `eyJ`),
  not the older 32-character v3 key.
- Refresh metadata accepts 1–8760 hours (step 0.5). Refresh playlists accepts
  0–8760; `0` disables the periodic catalog refresh.
- Tokens are shown as configured/not configured and are never echoed into the
  page.

### Movie and Series settings details

- **Default quality profile** offers the three built-ins. A custom profile can
  still be chosen per item, but the add default itself is a built-in. The
  default starts at 1080p.
- **Movie default availability** is announced, in cinemas or released.
- **Series default monitor mode** is all, future, first season, latest season
  or none. **Default new-season policy** is all, future or none. Monitor mode
  applies at add time; the new-season policy applies to later metadata
  discoveries, and specials are never auto-monitored.
- **Search interval** accepts 1–8760 hours (step 0.5) and is overridden by a
  per-item value. Search repeats until the profile target is met.
- **Candidate batch size** accepts 1–200 and defaults to 25 new candidates per
  scope per bworker iteration.
- **Probes per iteration** accepts 1–3 and defaults to 3, one ffprobe
  subprocess at a time. The cap keeps a search from monopolising a provider
  connection.
- Naming previews render all templates for the section; a template describes
  one path component and a path separator is rejected.

### Policies

The shared policy editor appears on both Movie and Series settings and writes
through `PATCH /api/settings/sections/policies`. It can enable/disable a work
type, choose infinite retries or a maximum attempt count, and set the retry
delay, backoff multiplier and maximum delay. **Activity cleanup is always
enabled and locked**: it must keep trimming the Valkey stream even while the
bworker is paused.

The **Reset failed** action in the policy editor targets only rows in
`failed_stopped`; it does not erase cursors or completed work. The broader
bworker reset on Activity can reset one unit or all units.

### Compatibility

The legacy section endpoints (`/api/settings`,
`/api/settings/metadata|media|downloads|scheduler`) remain available for older
clients. The current pages use the global atomic endpoint or the section
endpoints; no current page writes the legacy shape. Settings such as
`probe_max_candidates`, `probe_search_budget` and `download_concurrency` are
retained as compatibility values and do not alter the 0.8.0 acquisition
pipeline.

## Related pages

- **Explore** (`/explore/movies`, `/explore/series`) is read-only discovery and
  playlist lookup; see the README.
- **Import existing** (`/import`) scans, reviews and imports local files and
  exposes manual metadata search/preview/apply. It cannot overwrite an active
  download or follow a link outside the media root.
- **Storage** (`/storage`) browses and deletes within `Movies/`, `Series/` and
  `.incomplete/`. Active staging deletion requires confirmation and cancels its
  download; scan/import is refused while running.
