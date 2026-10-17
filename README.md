# IPTV Charts

A local IPTV playlist browser. Point it at a playlist — either a direct
**M3U URL** or **Xtream Codes** credentials — and it builds a searchable
catalog you can browse, filter and play from.

Everything runs on your own machine. There is no remote server, no account,
and nothing is uploaded anywhere.

## What works today

- **Add a playlist** as either an M3U URL or Xtream Codes credentials, with a
  **connection test** that reports what it found — account status, expiry,
  connection limits, channel estimate — before anything is saved.
- **Ingest** the whole catalog into SQLite: streaming parse, batched inserts,
  cancellable, and safe to re-run.
- **Real progress bars** — bytes against a known total, not a spinner.
- **Update** one playlist or **Update all**, with a Cancel button.
- **Staleness reminders**: a playlist not refreshed in 24h says so, with a
  one-click refresh.
- **Remove** a playlist and its downloaded catalog in one click.
- **Browse** any playlist with full-text **search**, kind tabs (Live / VOD /
  Series), a category filter, and sorting by name, recency or rating, paging
  as you scroll.
- **Play** in the browser — a plain video element, plus hls.js for HLS — or
  hand the stream to a player on your machine with one click.
- **Choose your player**: mpv, VLC (native or Flatpak), or a command of your
  own, from the settings page.
- **Copy link** for any item, or any individual episode.
- **Series** open an episode list with per-episode play, copy and player
  actions.

## Still to build

- The **info/stats page** with charts: account details, category breakdown,
  and when content was added. Account and server data is already fetched and
  stored on every sync, so this page is presentation only.

## Quick start

Two entry points, for two different jobs.

### Trying out a change — `setup.test.sh`

Runs the app directly on this machine from a virtualenv, with auto-reload.
Edit a file, save, refresh the browser. This is the fast loop.

```bash
./setup.test.sh              # start with auto-reload
./setup.test.sh --check      # run the smoke tests and exit
./setup.test.sh --no-reload  # run without the reloader
./setup.test.sh --port 9000  # different port
```

It creates the virtualenv and installs dependencies on first run, and only
reinstalls when `requirements.txt` actually changes.

### Running it for real — `setup.deploy.sh`

Builds a container image and runs it under Podman with a persistent volume.

```bash
./setup.deploy.sh              # build, deploy, open the browser
./setup.deploy.sh --build      # build the image only
./setup.deploy.sh --logs       # follow container logs
./setup.deploy.sh --status     # container, volume and bridge state
./setup.deploy.sh --stop       # stop and remove the container, keep data
./setup.deploy.sh --shell      # shell inside the running container
./setup.deploy.sh --destroy    # remove container AND delete all data
```

Overridable via environment: `IPTV_PORT`, `IPTV_IMAGE`, `IPTV_VOLUME`,
`IPTV_CONTAINER`.

## Where your data lives

**The two modes use different databases.** This is deliberate — the container
uses a Podman named volume so it survives rebuilds, while the dev mode uses
the ordinary XDG location on the host. A playlist added in one will not appear
in the other.

| | Dev mode (`setup.test.sh`) | Container (`setup.deploy.sh`) |
|---|---|---|
| Database | `~/.local/share/iptv-charts/` | volume `iptv-charts-data`, mounted at `/data` |
| Settings | `~/.config/iptv-charts/` | `/config` inside the container |
| Play queue | n/a — the player is launched directly | `$XDG_RUNTIME_DIR/iptv-charts/` |

To inspect or back up the container's database:

```bash
podman volume inspect iptv-charts-data --format '{{.Mountpoint}}'
```

The database holds your provider credentials in plain text, like any local
media player's config. Files are owned by your user and readable only by you.

## Playback

**In the browser.** Always available, no setup, no extra dependencies. A plain
`<video>` element plays what the browser can play, and hls.js (vendored locally,
so nothing is fetched at runtime) covers HLS, which no browser outside Safari
decodes on its own. Live channels are usually HLS, so they normally work; raw
MPEG-TS and MPEG-2/AC-3 streams do not, and the browser is left to say so in its
own controls rather than being covered up by an error of ours.

**In a player on your machine.** Every item and every episode has an
"External player" button that hands the stream to whatever you have chosen on
the settings page. How the hand-off happens depends on how the app is running:

- **Dev mode** — the app spawns the player itself, probing for a Flatpak
  (`io.mpv.Mpv`, `org.videolan.VLC`) and then for a native `mpv` or `vlc` on
  `PATH`. Flatpak comes first on purpose: mpv and VLC are very often installed
  as Flatpaks, and are then missing from `PATH` entirely.

- **Container** — a container cannot start a process on the host, so the app
  writes the request to a shared file and a small host-side watcher runs it:

  ```bash
  ./scripts/player-bridge.sh --daemon   # start it in the background
  ./scripts/player-bridge.sh --status   # is it running? which players does it see?
  ./scripts/player-bridge.sh --stop     # stop it
  ```

  `setup.deploy.sh` starts the bridge for you. It publishes a heartbeat and a
  list of the players installed on the host, which is how the settings page can
  offer them — the container cannot see the host's software itself. If the
  bridge is not running there is no heartbeat, and the buttons hide rather than
  doing nothing. Browser playback is unaffected either way.

  The bridge must be restarted after upgrading the app, so it learns the
  request format's `command` field; until then it falls back to playing with the
  first player it found, which is how it behaved before.

## Settings

`/settings` (also in the top bar) decides what the "External player" button
does:

- **A detected player** — mpv or VLC, native or Flatpak, whichever are
  installed. In container mode the list is what the bridge reported from the
  host.
- **Automatic** — the first one available, falling back to your custom command
  if nothing else is installed.
- **A custom command** — anything you like, with placeholders:

  | Placeholder | Becomes |
  |---|---|
  | `{url}` | the resolved stream address |
  | `{title}` | the item or episode name |
  | `{user_agent}` | the provider's User-Agent hint, when the playlist has one |
  | `{referrer}` | likewise, the Referer hint |

  For example:

  ```
  mpv --title={title} --user-agent={user_agent} --referrer={referrer} {url}
  ```

  The URL is appended at the end if the command never mentions it, and a flag
  whose placeholder has no value is dropped rather than passed empty. Nothing
  goes through a shell — the command is split into arguments and executed
  directly — so quotes only decide where one argument ends, and no quoting is
  needed around a value that happens to contain spaces.

**A custom command runs on the host, with your privileges, exactly as written.**
The app is loopback-only and refuses writes that come from another site, but the
command itself is not sandboxed in any way; treat it as you would a line in your
own shell profile. It is stored in the app's database (`catalog.db`).

## Design constraints

Two things shape most of the decisions here.

**It has to stay light on RAM.** Provider catalogs are large — a Xtream
`get_vod_streams` response can be 200 MB of JSON covering 200,000 items.
Loading that naively peaks at about a gigabyte of Python objects. Instead the
sync spools the response to disk, parses it incrementally with ijson's C
backend, and inserts in batches, so peak memory stays flat regardless of
catalog size. The catalog also deliberately does **not** store the provider's
raw JSON per item, which would be the single largest cost in the design.

One subtlety worth knowing if you touch the sync code: HTTP responses are
usually gzipped, and `requests` decompresses them transparently. Counting the
bytes it hands back against the compressed `Content-Length` makes the progress
bar hit 100% at roughly 10% of the download. That is why the code spools raw
bytes to disk first, so both phases have honest denominators.

**Failures must be visible.** A panel returning an HTML error page, an empty
body, `auth: 0` with `status: "Active"`, or a JSON object where an array was
expected are all handled explicitly — several of these would otherwise look
like success while importing nothing.

**Syncing must not disturb reading.** The catalog is loaded inside a single
transaction in WAL mode, so a browse request during a sync sees either the old
catalog or the new one, never a half-written mix. Cancelling rolls the whole
transaction back, leaving the previous catalog untouched.

One consequence worth knowing if you touch the bulk-load code: switching
SQLite's `journal_mode` requires *exclusive* access to the database, so doing
it during a load breaks the moment the UI holds any other connection open —
which it usually does. The bulk path therefore changes only per-connection
settings (`synchronous`, `cache_size`, `temp_store`) and stays in WAL.

**Search has to be written carefully.** The natural way to search an FTS index
with a filter —

```sql
FROM items_fts f JOIN items i ON i.id = f.rowid
WHERE items_fts MATCH ? AND i.source_id = ?
```

— measured **20 seconds** against a 313k-item catalog, because SQLite chose to
scan every row of the source and probe the FTS index once per row: 313,247
probes to find 866 matches. Writing it as a `CROSS JOIN` removes the planner's
ability to reorder, pinning the FTS match as the outer loop, and the same query
runs in **3 ms**. There is a regression test asserting the query plan, because
nothing about the slow version looks wrong.

## Development

```bash
./setup.test.sh --check
```

Runs four suites, ~240 checks, none of which touch the internet:

| Suite | Covers |
|---|---|
| `tests/smoke.py` | Parsers, schema, FTS, streaming, player commands, request guards |
| `tests/integration.py` | Real ingest against a local fixture provider |
| `tests/endtoend.py` | The HTTP API and rendered pages, via Flask's test client |
| `tests/bridge.py` | The queue protocol, against the real host bridge and a fake player |

They exist to pin down the specific traps this codebase is built around:
quoted commas in M3U titles, diacritic-folded search, the `stream_id` vs `num`
distinction, progress accounting under gzip, the silent-zero-items failure when
a panel returns an object instead of an array, and cancellation leaving the
previous catalog intact.

`tests/fixture_server.py` runs the fake provider standalone, which is how the
container is exercised end to end.

## Project layout

```
run.py               entry point (waitress; --dev for the Flask reloader)
setup.test.sh        live dev build
setup.deploy.sh      container build and deploy
Containerfile        image definition
iptv_charts/
  app.py             Flask factory, page routes, request guards
  api.py             JSON endpoints: probe, create, sync, status, settings
  config.py          path resolution, port selection
  db.py              SQLite connections, schema, bulk-load tuning
  sync.py            the ingest engine, progress state, cancellation
  sources.py         source CRUD and credential probing
  player.py          which player to use, how to run it, host bridge client
  clients/
    m3u.py           streaming M3U parser and fetcher
    xtream.py        Xtream Codes API client
    streaming.py     shared spool/progress primitives
  templates/         Jinja templates
  static/            CSS, JS, vendored hls.js
scripts/
  player-bridge.sh   host-side watcher that plays streams for the container
tests/
  smoke.py           offline unit checks
  integration.py     ingest against a local fixture provider
  endtoend.py        HTTP API and rendered pages
  bridge.py          the app/bridge queue contract
  fixture_server.py  the fixture provider, runnable standalone
```

## Security notes

The server binds **loopback only**. It stores provider credentials, so it must
never be reachable from the network. In the container case the published port
is likewise bound to `127.0.0.1` on the host.

There is deliberately **no authentication** — it is a single-user local app,
and a login step or token would only get in the way. Two checks stand in for
one. A request's `Host` header must name loopback, which rejects a page on
another site resolving to `127.0.0.1` and talking to the app under its own
hostname (DNS rebinding). And anything that changes state must come from
something the app served: a cross-site `Sec-Fetch-Site`, or an `Origin` naming
another host, is refused.

The second check is not decoration. A browser will happily send a cross-site
`POST` to loopback without asking anyone first, and one of those writes decides
which command runs on your machine when you press play — as does the stream URL
itself, since an M3U playlist supplies it. Both are therefore treated as
untrusted: addresses must be real stream URLs (which is also what stops a
"URL" like `--script=…` from being read as an option by the player), commands
are executed without a shell, and the host bridge re-checks what the app sends
it rather than trusting it.

Worth knowing: because there is no token, any web page you have open can still
*read* from `http://127.0.0.1:<port>/`, and can issue writes that browsers
classify as same-site. That is an accepted trade-off here, not an oversight. If
you ever expose this beyond loopback, that calculus changes and a token should
come back.

## Requirements

- Python 3.11+ (developed against 3.13)
- Podman, for the container path only
- `mpv` or `vlc`, entirely optional

On Debian/Ubuntu, if `python3 -m venv` fails with `No module named 'ensurepip'`:

```bash
sudo apt install python3-venv python3-full
```

## License

MIT
