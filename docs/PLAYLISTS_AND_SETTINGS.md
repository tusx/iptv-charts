# Playlists, the home dashboard and unified settings

This document covers the 0.4 UI structure and the rules behind editing a
playlist.

## Home dashboard

`/` renders three cards:

- **IPTV** — playlist count/catalog totals, Add playlist, Update all,
  Manage, and one compact row per playlist with Browse, Edit, Update, Cancel
  and Remove.
- **Movies** — library counts and Open, Add movie, Calendar, Wanted,
  Search missing.
- **Series** — series/episode counts and Open, Add series, Calendar, Wanted,
  Search missing.

The full playlist management page remains at `/playlists`. It contains the
existing detailed cards, the shared add-playlist dialog and the same actions.
The add dialog is a shared template (`_add_playlist_dialog.html`) so the home
and `/playlists` pages cannot drift apart.

The Add movie/series links use `?add=1`; the respective page opens its dialog
on load and then removes the query parameter so a reload does not reopen it.

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

## Unified settings

`/settings` is one form with one **Save all settings** button. The sections are:

1. External player
2. Custom command
3. Metadata providers
4. Media management
5. Downloads and probing
6. Automation

`POST /api/settings/save` accepts a nested JSON object:

```json
{
  "player": {"preferred_player": "auto", "player_command": ""},
  "metadata": {"language": "en-US", "tmdb_clear": false, "tvdb_clear": false},
  "media": {"movie_file_template": "{title} ({year}) [{resolution}]"},
  "downloads": {"concurrency": 1, "max_attempts": 3,
                 "probe_cache_hours": 24, "probe_timeout_seconds": 30,
                 "probe_max_candidates": 6, "probe_search_budget": 60},
  "scheduler": {"search_interval_hours": 6,
                 "refresh_interval_hours": 168,
                 "catalog_refresh_hours": 24}
}
```

Keys are optional. Validation happens for every supplied section before any
write. The values are then upserted in one `BEGIN IMMEDIATE` transaction.
Errors return HTTP 400 with `{ok:false, error, section}`; the UI scrolls to the
failing section. No section is partially saved.

Special rules:

- A blank metadata key is omitted by the UI and keeps the stored value.
- `tmdb_clear` clears the TMDB key; `tvdb_clear` clears the TVDB key and PIN.
  Clearing or changing TVDB credentials also removes the cached TVDB token.
- The metadata test buttons test **saved** keys, not unsaved input.
- The player command is validated before the preference; a custom preference
  requires a non-empty command.
- Media templates and the probe/download/scheduler ranges have the same
  validation as their old section endpoints.
- The old section endpoints (`/api/settings`,
  `/api/settings/metadata|media|downloads|scheduler`) remain available for
  compatibility; the page uses only the atomic endpoint.
- Backup/restore, quality profiles, library scan, provider tests and
  probe-cache clearing are actions, not settings, and stay outside the form.
