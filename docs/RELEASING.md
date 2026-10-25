# Releasing IPTV Charts

The release flow is deliberately manual at the two points that matter: a
maintainer pushes a version tag, and a user chooses to run the updater. Nothing
in the application downloads or replaces itself.

## Version and tag convention

- The version lives in `iptv_charts/__init__.py` (`__version__`).
- Release tags are `v<version>` — this release is `v0.8.0`.
- The tag must match `__version__` exactly (including the `v`). The release
  workflow refuses to publish otherwise.
- `CHANGELOG.md` holds the human-readable section for the version; the release
  workflow copies that section into the GitHub release notes. A heading such as
  `## 0.8.0 — 2026-10-25` is matched by its first token.

## What `.github/workflows/release.yml` does

Triggered only by pushing a `v*` tag. It runs one job with `contents: write`:

1. Checks out the tagged commit and installs `requirements.txt`.
2. Verifies `v<__version__>` equals `github.ref_name`.
3. Runs every `tests/*.py` suite (except `tests/fixture_server.py`, which is a
   fixture process, not a suite).
4. Builds `iptv-charts-<version>.tar.gz` with a single
   `iptv-charts-<version>/` root, excluding `.git`, `.venv`, `data`, `cache`,
   `dist`, `__pycache__`, `*.pyc`, `*.egg-info` and SQLite files.
5. Writes `iptv-charts-<version>.tar.gz.sha256` in GNU `sha256sum` format.
6. Extracts the version's section from `CHANGELOG.md` into the release notes
   (falling back to a one-line title when the section is not written yet).
7. Creates the GitHub release, or updates it and overwrites the two assets when
   the tag already has a release (`gh release upload --clobber`). Re-running a
   failed workflow is therefore safe.

The workflow publishes **no container images**. Deployment builds the image on
the user's machine from the verified source archive.

### Cutting a release

1. Bump `iptv_charts/__init__.py` and add the `CHANGELOG.md` section.
2. Run `./setup.test.sh --check` and every standalone suite, or at minimum:
   `.venv/bin/python tests/release.py`
3. Commit on the release branch and merge to `main` after review.
4. Tag the commit and push only the tag:

   ```sh
   git tag -a v0.8.0 -m "IPTV Charts 0.8.0"
   git push origin v0.8.0
   ```

5. Watch the `release` workflow. It fails early if the tag and version differ,
   any suite fails, or the archive/checksum cannot be produced.

### Validating the workflow locally

`tests/release.py` parses `release.yml` with a standard-library YAML subset
parser (PyYAML is not a runtime dependency), asserts the permissions and step
order, executes the real tag/version gate for good and bad tags, and runs the
real archive/checksum and release-notes shell steps in a temporary directory.
It verifies archive layout/exclusions and that the checksum matches. No
external network is used.

```sh
.venv/bin/python tests/release.py
# release: 253/253 checks passed
```

## Manual update path: `download.and.deploy.sh`

The script is shipped in the source archive and can also be fetched from the
tagged source:

```sh
curl -fsSL https://raw.githubusercontent.com/tusx/iptv-charts/v0.8.0/download.and.deploy.sh \
  -o download.and.deploy.sh
bash download.and.deploy.sh
```

It resolves the latest release through the public GitHub API (no token, a
`User-Agent`, short timeouts), downloads the source archive and checksum, and
**verifies SHA-256 before extracting anything**. Extraction happens in a fresh
`mktemp -d` directory and rejects absolute paths, `..` components, special
files and symlink/hardlink targets that escape the archive. The downloaded
tree is only executed after the checksum and layout checks pass.

It then asks the running container for its version at
`http://127.0.0.1:<port>/api/health` and compares it with the release using a
semver-ish comparison (optional `v` prefix, numeric components, prereleases).

| Running state | Default behaviour |
|---|---|
| Same version | Says it is current and exits 0. `--force` redeploys. |
| Release newer | Shows both versions and asks for confirmation. |
| Running version newer | Refuses to downgrade. `--force` overrides. |
| Container not running / unhealthy | Asks to deploy. |
| No terminal and no `--yes` | Refuses to change anything. |

Only after confirmation (or `--yes`) does it copy the verified tree to a
stable install directory and invoke that copy's `build.deploy.sh`. The stable
path is required because the downloaded build script launches the supervised
player bridge from its own tree; deleting the temporary extraction would leave
the bridge pointing at a removed filesystem path. The default is
`${XDG_DATA_HOME:-~/.local/share}/iptv-charts-release`; override it with
`IPTV_CHARTS_INSTALL_DIR` for a system-wide install. The release tree is code
only — `build.deploy.sh` reuses the named data volume, so playlists,
credentials, settings and the database survive; the media directory and host
port are passed through and preserved.

```
Usage:
  ./download.and.deploy.sh
  ./download.and.deploy.sh --yes
  ./download.and.deploy.sh --version 0.8.0
  ./download.and.deploy.sh --force
  ./download.and.deploy.sh --runtime docker --port 9000 --media-dir "/srv/IPTV Charts"
```

Options: `--version`, `--repo OWNER/NAME`, `--runtime podman|docker`,
`--port`, `--media-dir`, `--yes`, `--force`.

Offline and rate-limited machines get a clear one-line error and exit
non-zero; nothing is extracted and no deployment is touched. When both podman
and docker are installed the script prefers the runtime that owns the
container, otherwise podman; `--runtime` always wins.

### Contract with `build.deploy.sh`

`download.and.deploy.sh` invokes the downloaded script noninteractively. It
passes flags only when the downloaded script mentions them, and always exports
the environment equivalents so either style works:

- flags: `--yes`, `--runtime <name>`, `--port <port>`, `--media-dir <path>`
- environment: `IPTV_PORT`, `IPTV_MEDIA_DIR`, `IPTV_CHARTS_RUNTIME`,
  `IPTV_RUNTIME`, `IPTV_CONTAINER`, `IPTV_CHARTS_INSTALL_DIR`,
  `IPTV_CHARTS_ASSUME_YES=1`, `IPTV_CHARTS_NONINTERACTIVE=1`,
  `IPTV_CHARTS_NO_OPEN=1`
- stdin is `/dev/null`, so an unexpected prompt fails rather than hanging.

The downloaded tree is installed at `IPTV_CHARTS_INSTALL_DIR` before
`build.deploy.sh` runs, and every later update replaces that tree in place.
The build script can therefore start `scripts/player-bridge.sh` from the
stable path, and the supervisor keeps working after the updater exits.

`build.deploy.sh` is responsible for reusing the named volume and printing the
final URL. `download.and.deploy.sh` prints it again as the final line.

### Update-script test overrides

The offline test suite (`tests/release.py`) uses these undocumented-for-users
variables; they are also useful for a local mirror:

| Variable | Purpose |
|---|---|
| `IPTV_CHARTS_RELEASES_API` | API base, e.g. `http://127.0.0.1:8899` |
| `IPTV_CHARTS_CONTAINER_RUNNING` | `1`/`0` instead of runtime inspection |
| `IPTV_CHARTS_RUNTIME` | force podman/docker without a prompt |
| `IPTV_CHARTS_CONTAINER_NAME` | container name (default `iptv-charts`) |
| `IPTV_CHARTS_HEALTH_TIMEOUT` | health probe timeout in seconds |
| `IPTV_CHARTS_REPO`, `IPTV_PORT`, `IPTV_MEDIA_DIR` | same as the flags |

## In-app update check

`GET /api/update/status` is read-only and returns
`{ok, enabled, current, latest, tag, update_available, url, checked_at,
cached, error, update_command}`. It never downloads or applies anything.
`?refresh=1` bypasses the six-hour cache for a manual check; a normal page load
can use the cached answer. Failures are a 200 response with `error` set, so a
page poll never raises or shows an unrelated error.

Disable checking with either:

- Settings key `update_check_enabled=0` (DB default is `1`), or
- environment `IPTV_CHARTS_UPDATE_CHECK=0` (wins over the setting).

The check sends no credentials or token and uses `IPTV-Charts/<version>` as its
user agent. It uses `metadata_cache` under the key `update:latest-release`;
after a failed refresh it keeps the last confirmed release in the result and
reports the error. `IPTV_CHARTS_UPDATE_API` and `IPTV_CHARTS_UPDATE_TIMEOUT`
exist for tests/mirrors only.

### Where the check is exposed

The update blueprint is registered in `iptv_charts/app.py` after the other
feature blueprints, so `GET /api/update/status` is available in 0.8.0. The
response is the only integration surface: it is read-only and returns the
`latest`, `url` and `update_command` fields described above.

0.8.0 does **not** add an update card or banner to the rendered UI. Users can
call the endpoint directly (or read `update_command` from it) and run
`download.and.deploy.sh`; the README documents that manual path. A future UI
card would only need to fetch the same endpoint with `refresh=1` and display
`update_available`, `latest`, `url` and `update_command`; it must not apply an
update from the browser. No CDN, Vue or build step is involved. `error` may be
shown as muted helper text on a manual check; on automatic page loads it should
stay hidden.

## Tests

```sh
.venv/bin/python tests/release.py
```

The suite is offline and path-isolated. It covers version comparison edge
cases, update-check caching/disable/failure, the status endpoint, workflow
structure and tag gating, the real archive/checksum steps, and the updater
script against a fake release (checksum mismatch, path traversal, absolute
paths, escaping symlinks, missing layout, current/stale/newer/not-running
prompts, `--yes`, `--force`, offline and rate-limit errors).
