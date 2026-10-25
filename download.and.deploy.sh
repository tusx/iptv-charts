#!/usr/bin/env bash
#
# download.and.deploy.sh -- fetch, verify and deploy a published IPTV Charts
# release. This is the manual update path; the app never runs it by itself.
#
# What it does, in order:
#   1. Resolves the latest (or requested) release from GitHub.
#   2. Downloads the source archive and its SHA-256 checksum.
#   3. Verifies the checksum before anything is extracted.
#   4. Extracts into a fresh temporary directory, rejecting absolute paths,
#      ".." entries and links that escape the archive.
#   5. Reads the running container's version from /api/health and compares it
#      with the release (semver-ish: v prefix and prereleases are handled).
#   6. Asks before changing a running container (or before the first deploy).
#      --yes skips the question for unattended use; there is no implicit
#      auto-update.
#   7. Invokes the downloaded build.deploy.sh with the selected runtime, port
#      and media directory. The named data volume is reused, so the library
#      and credentials survive.
#
# Usage:
#   ./download.and.deploy.sh                     latest release, ask first
#   ./download.and.deploy.sh --yes               latest release, no prompt
#   ./download.and.deploy.sh --version 0.8.0     a specific release
#   ./download.and.deploy.sh --force             redeploy/downgrade anyway
#   ./download.and.deploy.sh --runtime docker --port 9000 \
#       --media-dir "/srv/IPTV Charts"
#
# Options:
#   --version VERSION   release to install (default: latest published)
#   --repo OWNER/NAME   release repository (default: tusx/iptv-charts)
#   --runtime NAME      podman or docker (default: detect)
#   --port PORT         host port to publish (default: 8770)
#   --media-dir DIR     host media root (default: ~/Media/IPTV Charts)
#   --yes, -y           do not ask for confirmation
#   --force             deploy even when the running version is current or
#                       newer than the latest release
#   --help, -h          show this help
#
# Environment overrides (also used by the offline test suite):
#   IPTV_CHARTS_REPO                same as --repo
#   IPTV_CHARTS_RELEASES_API        GitHub API base (default https://api.github.com)
#   IPTV_CHARTS_RUNTIME             same as --runtime
#   IPTV_CHARTS_CONTAINER_NAME      container name (default iptv-charts)
#   IPTV_CHARTS_CONTAINER_RUNNING   force "1" or "0" instead of inspecting
#   IPTV_CHARTS_HEALTH_TIMEOUT      seconds for the local health probe (default 2)
#   IPTV_CHARTS_INSTALL_DIR         stable source tree for build.deploy.sh
#   IPTV_PORT, IPTV_MEDIA_DIR       same as --port / --media-dir
#
# The verified release is copied to a stable install directory before
# build.deploy.sh runs. That script starts the supervised player bridge from
# its own tree, so the tree must outlive this process; a temporary directory
# would leave the bridge pointing at a deleted path.
#
# Requirements: bash, curl, tar, python3, sha256sum, and the selected
# container runtime. No GitHub token or other credential is used.

set -euo pipefail

REPO="${IPTV_CHARTS_REPO:-tusx/iptv-charts}"
VERSION=""
ASSUME_YES=0
FORCE=0
RUNTIME="${IPTV_CHARTS_RUNTIME:-}"
PORT="${IPTV_PORT:-8770}"
MEDIA_DIR="${IPTV_MEDIA_DIR:-$HOME/Media/IPTV Charts}"
API_BASE="${IPTV_CHARTS_RELEASES_API:-https://api.github.com}"
CONTAINER_NAME="${IPTV_CHARTS_CONTAINER_NAME:-iptv-charts}"
HEALTH_TIMEOUT="${IPTV_CHARTS_HEALTH_TIMEOUT:-2}"
INSTALL_DIR="${IPTV_CHARTS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/iptv-charts-release}"

info()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,56p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            [[ $# -ge 2 ]] || die "--version needs a value"
            VERSION="${2#v}"; VERSION="${VERSION#V}"; shift 2 ;;
        --repo)
            [[ $# -ge 2 ]] || die "--repo needs OWNER/NAME"
            REPO="$2"; shift 2 ;;
        --runtime)
            [[ $# -ge 2 ]] || die "--runtime needs podman or docker"
            RUNTIME="$2"; shift 2 ;;
        --port)
            [[ $# -ge 2 ]] || die "--port needs a number"
            PORT="$2"; shift 2 ;;
        --media-dir|--media)
            [[ $# -ge 2 ]] || die "--media-dir needs a path"
            MEDIA_DIR="$2"; shift 2 ;;
        --yes|-y)
            ASSUME_YES=1; shift ;;
        --force)
            FORCE=1; shift ;;
        --help|-h)
            usage; exit 0 ;;
        *)
            usage
            exit 2 ;;
    esac
done

[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "invalid repository: $REPO"
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) \
    || die "invalid port: $PORT"
[[ -n "$MEDIA_DIR" ]] || die "the media directory cannot be empty"
[[ "$INSTALL_DIR" == /* ]] || die "the install directory must be an absolute path"
[[ "$INSTALL_DIR" != "/" && "$INSTALL_DIR" != "$HOME" ]] \
    || die "refusing to use $INSTALL_DIR as the install directory"
if [[ -n "$VERSION" ]]; then
    [[ "$VERSION" =~ ^[0-9A-Za-z][0-9A-Za-z._+-]*$ ]] \
        || die "invalid version: $VERSION"
fi
if [[ -n "$RUNTIME" ]]; then
    case "$RUNTIME" in
        podman|docker) ;;
        *) die "--runtime must be podman or docker" ;;
    esac
fi

for command in curl tar python3 sha256sum mktemp; do
    command -v "$command" >/dev/null 2>&1 || die "$command is required"
done

work="$(mktemp -d "${TMPDIR:-/tmp}/iptv-charts-update.XXXXXX")"
cleanup() { rm -rf -- "$work"; }
trap cleanup EXIT

# --------------------------------------------------------------------- release
api="${API_BASE%/}"
if [[ -n "$VERSION" ]]; then
    api_url="${api}/repos/${REPO}/releases/tags/v${VERSION}"
else
    api_url="${api}/repos/${REPO}/releases/latest"
fi

info "looking up the release at ${api_url}"
release_json="${work}/release.json"
http_code=""
if ! http_code="$(curl -sS --connect-timeout 8 --max-time 25 \
        -A "IPTV-Charts-Updater" \
        -H "Accept: application/vnd.github+json" \
        -o "$release_json" -w '%{http_code}' "$api_url" 2>"${work}/curl.err")"; then
    detail="$(head -n 1 "${work}/curl.err" 2>/dev/null || true)"
    die "could not reach GitHub to look up the release. Check your connection and try again.${detail:+ (${detail})}"
fi
case "$http_code" in
    200) ;;
    403|429)
        die "GitHub's release API rate limit was reached. Try again later, or download the release manually from https://github.com/${REPO}/releases." ;;
    404)
        die "no release was found for ${REPO}${VERSION:+ version ${VERSION}}. Check --repo/--version and try again." ;;
    *)
        die "GitHub returned HTTP ${http_code} for ${api_url}." ;;
esac

release_fields="$(
    python3 -I - "$release_json" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    raise SystemExit(1)

if not isinstance(data, dict):
    raise SystemExit(1)
tag = data.get("tag_name")
if not isinstance(tag, str) or not tag.strip():
    raise SystemExit(1)
tag = tag.strip()
version = tag[1:] if tag[:1] in "vV" else tag
if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}", version):
    raise SystemExit(1)

assets = {}
for item in data.get("assets") or []:
    if not isinstance(item, dict):
        continue
    name = item.get("name")
    url = item.get("browser_download_url")
    if isinstance(name, str) and isinstance(url, str) \
            and url.startswith(("https://", "http://")):
        assets[name] = url

tar_name = f"iptv-charts-{version}.tar.gz"
sha_name = tar_name + ".sha256"
if tar_name not in assets or sha_name not in assets:
    raise SystemExit(1)

print(tag)
print(version)
print(assets[tar_name])
print(assets[sha_name])
PY
)" || die "GitHub returned a release response this updater could not understand (missing ${VERSION:+v${VERSION} }assets?)."

mapfile -t fields <<<"$release_fields"
tag="${fields[0]}"
release_version="${fields[1]}"
tar_url="${fields[2]}"
sha_url="${fields[3]}"
if [[ -n "$VERSION" && "$release_version" != "$VERSION" ]]; then
    die "requested version ${VERSION}, but GitHub returned ${release_version}."
fi

info "release ${tag} (version ${release_version})"

# -------------------------------------------------------------------- download
tar_file="${work}/iptv-charts-${release_version}.tar.gz"
sha_file="${work}/iptv-charts-${release_version}.tar.gz.sha256"
info "downloading ${tar_file##*/}"
curl -fsSL --connect-timeout 10 --max-time 600 -o "$tar_file" "$tar_url" \
    || die "could not download the release archive from GitHub."
curl -fsSL --connect-timeout 10 --max-time 60 -o "$sha_file" "$sha_url" \
    || die "could not download the release checksum from GitHub."
[[ -s "$tar_file" ]] || die "the downloaded release archive is empty"

# Verify BEFORE extraction. Nothing from the archive has been executed or
# even unpacked at this point.
expected_sha="$(
    python3 -I - "$sha_file" <<'PY'
import re
import sys

try:
    text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
except OSError:
    raise SystemExit(1)
match = re.search(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{64})(?![0-9A-Fa-f])", text)
if match is None:
    raise SystemExit(1)
print(match.group(1).lower())
PY
)" || die "the release checksum file is not a valid SHA-256 checksum."
actual_sha="$(sha256sum "$tar_file")"
actual_sha="${actual_sha%% *}"
if [[ "$actual_sha" != "$expected_sha" ]]; then
    die "SHA-256 mismatch for ${tar_file##*/}: expected ${expected_sha}, got ${actual_sha}. Refusing to extract."
fi
info "SHA-256 verified"

# -------------------------------------------------------------------- extract
extract_dir="${work}/source"
mkdir -p "$extract_dir"
python3 -I - "$tar_file" "$extract_dir" <<'PY' || die "the release archive was rejected as unsafe or malformed."
import posixpath
import sys
import tarfile


def reject(message):
    print(f"archive rejected: {message}", file=sys.stderr)
    raise SystemExit(1)


archive, destination = sys.argv[1], sys.argv[2]
try:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            reject("empty archive")
        for member in members:
            name = member.name.replace("\\", "/")
            if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
                reject(f"absolute path {member.name!r}")
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if ".." in parts:
                reject(f"parent-directory entry {member.name!r}")
            if member.isdev() or member.isfifo():
                reject(f"special file {member.name!r}")
            if member.issym() or member.islnk():
                target = member.linkname.replace("\\", "/")
                if target.startswith("/"):
                    reject(f"absolute link target in {member.name!r}")
                if ".." in [part for part in target.split("/") if part not in ("", ".")]:
                    reject(f"escaping link target in {member.name!r}")
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(name), target)
                )
                if resolved == ".." or resolved.startswith("../"):
                    reject(f"link escapes the archive in {member.name!r}")
        kwargs = {}
        if hasattr(tarfile, "data_filter"):
            kwargs["filter"] = "data"
        bundle.extractall(destination, members=members, **kwargs)
except (tarfile.TarError, OSError) as exc:
    reject(str(exc))
PY

source_root="${extract_dir}/iptv-charts-${release_version}"
[[ -d "$source_root" ]] \
    || die "the archive does not contain the expected top-level directory iptv-charts-${release_version}/."
[[ -f "$source_root/build.deploy.sh" && ! -L "$source_root/build.deploy.sh" ]] \
    || die "the archive does not contain a regular build.deploy.sh."
[[ -f "$source_root/Containerfile" ]] \
    || die "the archive does not contain a Containerfile."

# --------------------------------------------------------------- runtime/state
select_runtime() {
    if [[ -n "$RUNTIME" ]]; then
        if [[ -z "${IPTV_CHARTS_CONTAINER_RUNNING:-}" ]] \
           && ! command -v "$RUNTIME" >/dev/null 2>&1; then
            die "$RUNTIME is not installed"
        fi
        return
    fi
    local choices=()
    command -v podman >/dev/null 2>&1 && choices+=(podman)
    command -v docker >/dev/null 2>&1 && choices+=(docker)
    if (( ${#choices[@]} == 0 )); then
        die "neither podman nor docker is installed"
    elif (( ${#choices[@]} == 1 )); then
        RUNTIME="${choices[0]}"
    else
        # Both are installed. Prefer the one that already owns our container;
        # otherwise choose podman (the project's primary runtime) rather than
        # blocking a --yes run on a prompt.
        local candidate
        for candidate in "${choices[@]}"; do
            if [[ "$("$candidate" inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
                RUNTIME="$candidate"
                break
            fi
        done
        if [[ -z "$RUNTIME" ]]; then
            RUNTIME=podman
            warn "both podman and docker are installed; using ${RUNTIME} (override with --runtime)"
        fi
    fi
}

container_running() {
    if [[ -n "${IPTV_CHARTS_CONTAINER_RUNNING:-}" ]]; then
        [[ "${IPTV_CHARTS_CONTAINER_RUNNING}" == "1" ]]
        return
    fi
    command -v "$RUNTIME" >/dev/null 2>&1 || return 1
    [[ "$("$RUNTIME" inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]
}

health_version() {
    curl -fsS --connect-timeout 1 --max-time "$HEALTH_TIMEOUT" \
        "http://127.0.0.1:${PORT}/api/health" 2>/dev/null \
        | python3 -I -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
version = data.get("version") if isinstance(data, dict) else None
if not isinstance(version, str) or not version.strip():
    raise SystemExit(1)
print(version.strip())
' 2>/dev/null || true
}

compare_versions() {
    python3 -I - "$1" "$2" <<'PY'
import re
import sys


def parse(value):
    match = re.fullmatch(
        r"[vV]?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?",
        value.strip(),
    )
    if match is None:
        return None
    core = tuple(int(part) for part in match.group(1).split("."))
    prerelease = []
    if match.group(2):
        for part in match.group(2).split("."):
            if part.isdigit():
                prerelease.append((0, int(part), ""))
            else:
                prerelease.append((1, 0, part))
    return core, tuple(prerelease)


left = parse(sys.argv[1])
right = parse(sys.argv[2])
if left is None or right is None:
    print("invalid")
    raise SystemExit(0)

width = max(len(left[0]), len(right[0]))
left_core = left[0] + (0,) * (width - len(left[0]))
right_core = right[0] + (0,) * (width - len(right[0]))
if left_core != right_core:
    print("newer" if left_core > right_core else "older")
    raise SystemExit(0)

left_pre, right_pre = left[1], right[1]
if left_pre and not right_pre:
    print("older")
elif not left_pre and right_pre:
    print("newer")
elif left_pre != right_pre:
    for left_id, right_id in zip(left_pre, right_pre):
        if left_id != right_id:
            print("newer" if left_id > right_id else "older")
            raise SystemExit(0)
    print("newer" if len(left_pre) > len(right_pre) else "older")
else:
    print("same")
PY
}

select_runtime
running=0
container_running && running=1
current_version="$(health_version)"
comparison="missing"
if [[ -n "$current_version" ]]; then
    comparison="$(compare_versions "$release_version" "$current_version")"
    if [[ "$comparison" == "invalid" ]]; then
        warn "could not compare ${release_version} with the health version ${current_version}; treating it as different"
    fi
fi

# ---------------------------------------------------------------- prompt/guard
if [[ "$comparison" == "same" && $FORCE -eq 0 ]]; then
    info "IPTV Charts ${current_version} is already the latest release."
    info "Use --force to redeploy it anyway."
    exit 0
fi
if [[ "$comparison" == "older" && $FORCE -eq 0 ]]; then
    info "the running version (${current_version}) is newer than the latest published release (${release_version})."
    info "not downgrading; use --force to deploy ${release_version} anyway."
    exit 0
fi

if [[ -n "$current_version" ]]; then
    message="IPTV Charts ${current_version} is running; ${release_version} is available."
elif (( running == 1 )); then
    message="the ${CONTAINER_NAME} container is running but did not answer the health check; deploy ${release_version}?"
else
    message="IPTV Charts is not running on port ${PORT}; deploy ${release_version}?"
fi

if (( ASSUME_YES == 0 )); then
    if [[ ! -t 0 ]]; then
        die "refusing to change the deployment without confirmation (no terminal). Re-run with --yes."
    fi
    read -r -p "${message} [y/N] " answer
    case "${answer,,}" in
        y|yes) ;;
        *) info "cancelled"; exit 0 ;;
    esac
else
    info "$message"
fi

# Install the verified tree at a stable path. build.deploy.sh launches the
# supervised player bridge from this tree, so it must not be the temporary
# extraction directory that this script deletes on exit. The swap is same-
# filesystem (the staging directory is a sibling of the target); the target is
# moved aside first so a failed copy cannot leave a half-written release.
install_release() {
    local source="$1" target="$2"
    local parent tmp old
    parent="$(dirname "$target")"
    tmp="${parent}/.$(basename "$target").new.$$"
    old="${parent}/.$(basename "$target").old.$$"
    mkdir -p "$parent"
    rm -rf -- "$tmp" "$old"
    if ! cp -a "$source/." "$tmp"; then
        rm -rf -- "$tmp"
        die "could not copy the verified release into ${target}."
    fi
    if [[ -e "$target" ]]; then
        mv "$target" "$old"
    fi
    if ! mv "$tmp" "$target"; then
        if [[ -e "$old" ]]; then
            mv "$old" "$target" 2>/dev/null || true
        fi
        rm -rf -- "$tmp"
        die "could not install the release into ${target}."
    fi
    rm -rf -- "$old"
}

# --------------------------------------------------------------------- deploy
info "installing ${tag} into ${INSTALL_DIR}"
install_release "$source_root" "$INSTALL_DIR"
build_script="${INSTALL_DIR}/build.deploy.sh"
[[ -f "$build_script" && ! -L "$build_script" ]] \
    || die "the installed release is missing build.deploy.sh."
supports_flag() {
    grep -qE -- "(^|[^A-Za-z0-9_-])${1}([^A-Za-z0-9_-]|$)" "$build_script"
}

build_args=()
supports_flag --yes && build_args+=(--yes)
supports_flag --runtime && build_args+=(--runtime "$RUNTIME")
supports_flag --port && build_args+=(--port "$PORT")
if supports_flag --media-dir; then
    build_args+=(--media-dir "$MEDIA_DIR")
elif supports_flag --media; then
    build_args+=(--media "$MEDIA_DIR")
fi

# Environment equivalents cover build.deploy.sh versions from the same
# release that read configuration only from the environment. The named data
# volume is reused by that script, so playlists, credentials and settings are
# preserved; only the container is replaced.
export IPTV_PORT="$PORT"
export IPTV_MEDIA_DIR="$MEDIA_DIR"
export IPTV_CHARTS_RUNTIME="$RUNTIME"
export IPTV_RUNTIME="$RUNTIME"
export IPTV_CONTAINER="$CONTAINER_NAME"
export IPTV_CHARTS_ASSUME_YES=1
export IPTV_CHARTS_NONINTERACTIVE=1
export IPTV_CHARTS_NO_OPEN=1

info "deploying with ${RUNTIME} on port ${PORT} (media: ${MEDIA_DIR})"
if ! bash "$build_script" "${build_args[@]}" </dev/null; then
    die "build.deploy.sh failed. The release tree was temporary; the existing container and data volume were not removed."
fi

url="http://127.0.0.1:${PORT}/"
deployed_version="$(health_version)"
if [[ -n "$deployed_version" ]]; then
    info "health reports version ${deployed_version}"
fi
info "IPTV Charts is deployed at ${url}"

cat <<EOF

  Release: ${tag}
  URL:     ${url}
  Source:  ${INSTALL_DIR}
  Data:    the existing named volume was reused (library and settings kept)
  Media:   ${MEDIA_DIR}

EOF
