#!/usr/bin/env bash
#
# build.deploy.sh -- build the image and run it as a container.
#
# This is the deployment path: build the current working tree, create or
# replace the production container, and leave it running. Podman and Docker
# are both supported; when both are installed you are asked which to use.
# For iterating on a feature, use setup.test.sh (virtualenv, auto-reload) or
# deploy.dev.build.sh (a separate dev container); neither disturbs this one.
#
# Usage:
#   ./build.deploy.sh                 build, (re)deploy, and open the browser
#   ./build.deploy.sh --build         build the image only
#   ./build.deploy.sh --logs          follow container logs
#   ./build.deploy.sh --status        show runtime, container and volume state
#   ./build.deploy.sh --stop          stop and remove the container (keeps data)
#   ./build.deploy.sh --destroy       stop, remove container AND delete volumes
#   ./build.deploy.sh --shell         open a shell inside the running container
#   ./build.deploy.sh --runtime NAME  podman or docker; skips the prompt
#
# Environment overrides:
#   IPTV_RUNTIME        podman or docker (same as --runtime)
#   IPTV_PORT           host port to publish (default 8770)
#   IPTV_IMAGE          image name (default iptv-charts)
#   IPTV_VOLUME         data volume name (default iptv-charts-data)
#   IPTV_CONFIG_VOLUME  config volume name (default iptv-charts-config)
#   IPTV_CACHE_VOLUME   cache volume name (default iptv-charts-cache)
#   IPTV_CONTAINER      container name (default iptv-charts)
#   IPTV_MEDIA_DIR      media folder on the host (default ~/Media/IPTV Charts);
#                       an existing movie/movies and tv/series folder is used
#                       as-is, otherwise Movies/ and Series/ are created.
#                       --destroy never touches this folder.
#
# The app volume, config and cache all live in named volumes, so replacing the
# container never throws settings or cached metadata away. The media folder is
# a bind mount and is never deleted by this script.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${IPTV_IMAGE:-iptv-charts}"
VOLUME="${IPTV_VOLUME:-iptv-charts-data}"
CONFIG_VOLUME="${IPTV_CONFIG_VOLUME:-iptv-charts-config}"
CACHE_VOLUME="${IPTV_CACHE_VOLUME:-iptv-charts-cache}"
CONTAINER="${IPTV_CONTAINER:-iptv-charts}"
HOST_PORT="${IPTV_PORT:-8770}"
INTERNAL_PORT="${IPTV_INTERNAL_PORT:-8770}"

# Ephemeral IPC directory shared with the player bridge. XDG_RUNTIME_DIR is
# per-user, 0700 and cleared on logout, which is exactly right for this --
# it is not state worth keeping.
QUEUE_DIR="${IPTV_CHARTS_PLAY_QUEUE_DIR:-${XDG_RUNTIME_DIR:-/tmp}/iptv-charts}"

# Where downloads are written on the host. The container sees this folder at
# /media; the app is told which subfolders to use below.
DEFAULT_MEDIA_DIR="$HOME/Media/IPTV Charts"
MEDIA_DIR="${IPTV_MEDIA_DIR:-$DEFAULT_MEDIA_DIR}"

info()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,37p' "$0" | sed 's/^# \{0,1\}//'
}

# --------------------------------------------------------------------- runtime
runtime=""
runtime_override="${IPTV_RUNTIME:-}"
action="deploy"
destroy_yes=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --logs)    action="logs"; shift ;;
        --status)  action="status"; shift ;;
        --stop)    action="stop"; shift ;;
        --destroy) action="destroy"; shift ;;
        --shell)   action="shell"; shift ;;
        --build)   action="build"; shift ;;
        --yes)     destroy_yes=true; shift ;;
        --runtime)
            [[ $# -ge 2 ]] || die "--runtime needs 'podman' or 'docker'"
            runtime_override="$2"
            shift 2
            ;;
        --runtime=*)
            runtime_override="${1#*=}"
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

# Pick the container runtime once, before any subcommand runs. An explicit
# --runtime (or IPTV_RUNTIME) is authoritative and never prompts; otherwise a
# single installed runtime is used, and two installed runtimes mean a prompt.
# Without a terminal there is nobody to ask, so fail with instructions rather
# than hang.
choose_runtime() {
    local requested="$1"
    local -a available=()
    local reply
    command -v podman >/dev/null 2>&1 && available+=(podman)
    command -v docker >/dev/null 2>&1 && available+=(docker)
    [[ ${#available[@]} -gt 0 ]] \
        || die "neither podman nor docker is installed"

    if [[ -n "$requested" ]]; then
        case "$requested" in
            podman|docker) ;;
            *) die "unsupported runtime '$requested' (use podman or docker)" ;;
        esac
        command -v "$requested" >/dev/null 2>&1 \
            || die "$requested is not installed"
        runtime="$requested"
    elif [[ ${#available[@]} -eq 1 ]]; then
        runtime="${available[0]}"
    elif [[ -t 0 ]]; then
        info "both podman and docker are installed"
        printf '  1) podman\n  2) docker\n'
        read -r -p "Container runtime [1]: " reply || reply=""
        case "${reply:-1}" in
            1|podman|Podman|PODMAN) runtime=podman ;;
            2|docker|Docker|DOCKER) runtime=docker ;;
            *) die "invalid runtime selection '$reply'" ;;
        esac
    else
        die "both podman and docker are installed; choose one with --runtime podman|docker or IPTV_RUNTIME=podman|docker"
    fi
    info "container runtime: $runtime"
}

# Runtime-neutral helpers. `container inspect` and `volume inspect` work under
# both runtimes, unlike podman's `container exists` / `volume exists` verbs.
container_exists() {
    "$runtime" container inspect "$CONTAINER" >/dev/null 2>&1
}

container_running() {
    [[ "$("$runtime" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null \
        || true)" == "true" ]]
}

volume_exists() {
    "$runtime" volume inspect "$1" >/dev/null 2>&1
}

# SELinux needs an explicit relabel on bind mounts; most non-Fedora systems
# have no SELinux at all and would reject the suffix as noise.
volume_label() {
    if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled 2>/dev/null; then
        printf ':Z'
    fi
}

# Volume ownership:
#   podman --userns=keep-id maps this user onto the same uid inside the
#   container, and :U makes each named volume writable by it. Docker runs the
#   image's app user directly and a fresh named volume inherits the ownership
#   of the image directory it covers, so no extra option is needed there.
named_volume_suffix() {
    if [[ "$runtime" == "podman" ]]; then
        printf ':U'
    fi
}

# ---------------------------------------------------------------------- paths
# Normalise a user-supplied folder without eval: expand a leading "~" and
# resolve a relative path against the current directory.
normalize_dir() {
    local dir="$1"
    case "$dir" in
        "~")   dir="$HOME" ;;
        "~/"*) dir="$HOME/${dir:2}" ;;
    esac
    if [[ "$dir" != /* ]]; then
        dir="$PWD/$dir"
    fi
    while [[ "$dir" == */ && "$dir" != "/" ]]; do
        dir="${dir%/}"
    done
    printf '%s\n' "$dir"
}

# Find an existing library folder under the media root. The canonical name
# wins when it exists exactly; otherwise aliases are matched case-insensitively
# in the same order config.py uses. Prints the folder name and returns 0 when
# one exists, or prints the canonical name and returns 1 when none does.
detect_library_dir() {
    local dir_root="$1" canonical="$2"
    shift 2
    local -a dirs=()
    local entry alias name

    if [[ -d "$dir_root/$canonical" ]]; then
        printf '%s\n' "$canonical"
        return 0
    fi

    while IFS= read -r -d '' entry; do
        dirs+=("${entry##*/}")
    done < <(find "$dir_root" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)

    shopt -s nocasematch
    for alias in "$@"; do
        for name in "${dirs[@]}"; do
            if [[ "$name" == "$alias" ]]; then
                shopt -u nocasematch
                printf '%s\n' "$name"
                return 0
            fi
        done
    done
    shopt -u nocasematch

    printf '%s\n' "$canonical"
    return 1
}

# The media root comes from IPTV_MEDIA_DIR when set; otherwise prompt with the
# usual default when there is a terminal to ask, and fall back to the default
# for unattended runs.
choose_media_dir() {
    local reply=""
    if [[ -z "${IPTV_MEDIA_DIR:-}" && -t 0 ]]; then
        read -r -p "Media folder [$DEFAULT_MEDIA_DIR]: " reply || reply=""
    fi
    MEDIA_DIR="$(normalize_dir "${reply:-${IPTV_MEDIA_DIR:-$DEFAULT_MEDIA_DIR}}")"
    [[ -n "$MEDIA_DIR" ]] || die "the media folder path is empty"
}

# Resolve the two library folders. Existing folders (including case variants
# and the tv/tv shows/tv-shows aliases) are adopted, never moved or renamed.
# Canonical folders are created only when nothing usable exists.
choose_library_dirs() {
    mkdir -p "$MEDIA_DIR"

    if MOVIE_DIR_NAME="$(detect_library_dir "$MEDIA_DIR" Movies movies movie)"; then
        info "using existing movie folder $MEDIA_DIR/$MOVIE_DIR_NAME"
    else
        info "creating movie folder $MEDIA_DIR/$MOVIE_DIR_NAME"
        mkdir -p "$MEDIA_DIR/$MOVIE_DIR_NAME"
    fi

    if SERIES_DIR_NAME="$(detect_library_dir "$MEDIA_DIR" Series series tv "tv shows" tv-shows)"; then
        info "using existing series folder $MEDIA_DIR/$SERIES_DIR_NAME"
    else
        info "creating series folder $MEDIA_DIR/$SERIES_DIR_NAME"
        mkdir -p "$MEDIA_DIR/$SERIES_DIR_NAME"
    fi
}

# ---------------------------------------------------------------------- build
build_image() {
    info "building image $IMAGE with $runtime"
    local -a build_args=(build)
    # --format docker is required under podman: the default OCI format silently
    # ignores HEALTHCHECK, which would leave the container with no health
    # reporting at all. Docker has no such flag.
    if [[ "$runtime" == "podman" ]]; then
        build_args+=(--format docker)
    fi
    build_args+=(-t "$IMAGE" -f "$root/Containerfile" "$root")
    "$runtime" "${build_args[@]}"
}

# --------------------------------------------------------------------- volumes
ensure_volume() {
    local name="$1"
    if volume_exists "$name"; then
        info "reusing existing volume $name"
    else
        info "creating volume $name"
        "$runtime" volume create "$name" >/dev/null
    fi
}

# --------------------------------------------------------------- player bridge
# Started BEFORE the readiness probe on purpose. The app caches player
# detection for 10 seconds, so if the bridge comes up after the first probe
# the external-player button stays hidden until that cache expires.
#
# Every deploy restarts it, whether or not one was already running: the bridge
# decides what a player click actually runs, so a bridge left over from an
# older build would keep answering with the old behaviour. It is supervised, so
# it also comes back by itself if the worker dies.
start_player_bridge() {
    if [[ -x "$root/scripts/player-bridge.sh" ]]; then
        info "restarting player bridge on the host"
        "$root/scripts/player-bridge.sh" --stop >/dev/null 2>&1 || true
        "$root/scripts/player-bridge.sh" --daemon || \
            warn "player bridge failed to start; the in-browser player still works"
    fi
}

# ---------------------------------------------------------------- readiness
wait_until_ready() {
    info "waiting for the app to answer"
    local ready=false health
    for _ in $(seq 1 40); do
        health="$(curl -fsS --max-time 2 "http://127.0.0.1:${HOST_PORT}/api/health" 2>/dev/null || true)"
        if [[ -n "$health" ]] \
           && grep -Eq '"alive"[[:space:]]*:[[:space:]]*true' <<<"$health"; then
            ready=true
            break
        fi
        if ! container_exists || ! container_running; then
            break
        fi
        sleep 0.5
    done

    if ! $ready; then
        warn "the app did not become healthy in time. Recent logs:"
        "$runtime" logs --tail 25 "$CONTAINER" >&2 || true
        exit 1
    fi
}

open_browser() {
    local url="$1"
    # Automation (the release updater) sets this: a browser tab popping up on
    # an unattended box is at best noise.
    [[ "${IPTV_CHARTS_NO_OPEN:-}" == "1" ]] && return 0
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 &
    fi
}

# ------------------------------------------------------------------- actions
deploy() {
    # The port being busy is expected when we are about to replace our own
    # running container -- removing it frees the port a moment later. Only a
    # *foreign* holder is a genuine conflict, so check for that rather than
    # failing on any listener.
    local our_container_running=false
    if container_exists && container_running; then
        our_container_running=true
    fi
    if ! $our_container_running \
       && command -v ss >/dev/null 2>&1 \
       && ss -ltn 2>/dev/null | grep -qE ":${HOST_PORT}\b"; then
        die "host port $HOST_PORT is already in use by another process. Re-run with IPTV_PORT=<other>."
    fi

    choose_media_dir
    choose_library_dirs

    ensure_volume "$VOLUME"
    ensure_volume "$CONFIG_VOLUME"
    ensure_volume "$CACHE_VOLUME"

    mkdir -p "$QUEUE_DIR"
    chmod 700 "$QUEUE_DIR"

    if container_exists; then
        info "replacing existing container $CONTAINER"
        "$runtime" rm -f "$CONTAINER" >/dev/null
    else
        info "creating container $CONTAINER"
    fi

    local volume_suffix
    volume_suffix="$(named_volume_suffix)"

    local -a run_args=(run -d
        --name "$CONTAINER"
        --restart always
        -p "127.0.0.1:${HOST_PORT}:${INTERNAL_PORT}"
        -v "${VOLUME}:/data${volume_suffix}"
        -v "${CONFIG_VOLUME}:/config${volume_suffix}"
        -v "${CACHE_VOLUME}:/cache${volume_suffix}"
        -v "${QUEUE_DIR}:/playqueue$(volume_label)"
        -v "${MEDIA_DIR}:/media$(volume_label)"
        -e "IPTV_CHARTS_MEDIA_HOST_DIR=${MEDIA_DIR}"
        -e "IPTV_CHARTS_MOVIE_DIR=${MOVIE_DIR_NAME}"
        -e "IPTV_CHARTS_SERIES_DIR=${SERIES_DIR_NAME}"
        --label "io.github.tusx.iptv-charts=true")

    # --userns=keep-id maps this user onto the same uid inside the container,
    # so files written to the named volumes are owned by you and remain
    # readable on the host. It is podman-only.
    if [[ "$runtime" == "podman" ]]; then
        run_args+=(--userns=keep-id)
    fi

    # -p binds the host side to 127.0.0.1 only. The container listens on
    # 0.0.0.0 (it has to, or the published port could not reach it), but
    # nothing is exposed beyond loopback, and the app's own Host-header check
    # still applies.
    "$runtime" "${run_args[@]}" "$IMAGE" >/dev/null

    start_player_bridge
    wait_until_ready

    local url="http://127.0.0.1:${HOST_PORT}/"
    info "running at $url"
    open_browser "$url"

    cat <<EOF

  Runtime:   $runtime
  Container: $CONTAINER   ($runtime logs -f $CONTAINER)
  Port:      $HOST_PORT   (127.0.0.1 only)
  Volumes:   $VOLUME (data), $CONFIG_VOLUME (config), $CACHE_VOLUME (cache)
  Media:     $MEDIA_DIR
  Movies:    $MEDIA_DIR/$MOVIE_DIR_NAME
  Series:    $MEDIA_DIR/$SERIES_DIR_NAME
  URL:       $url
  Bridge:    ./scripts/player-bridge.sh --status

  Stop:      ./build.deploy.sh --stop
  Wipe data: ./build.deploy.sh --destroy

EOF
}

show_status() {
    MEDIA_DIR="$(normalize_dir "${IPTV_MEDIA_DIR:-$DEFAULT_MEDIA_DIR}")"
    printf 'runtime:   %s\n' "$runtime"
    printf 'container: '
    if container_exists; then
        "$runtime" inspect -f '{{.State.Status}}  (image {{.Image}})' "$CONTAINER" \
            2>/dev/null || echo "present"
    else
        echo "not created"
    fi

    local volume mountpoint size
    for volume in "$VOLUME" "$CONFIG_VOLUME" "$CACHE_VOLUME"; do
        printf 'volume:    '
        if volume_exists "$volume"; then
            mountpoint="$("$runtime" volume inspect "$volume" \
                --format '{{.Mountpoint}}' 2>/dev/null || true)"
            size="$(du -sh "$mountpoint" 2>/dev/null | cut -f1 || true)"
            echo "$volume  (${size:-?} at ${mountpoint:-unknown})"
        else
            echo "$volume  not created"
        fi
    done

    printf 'port:      %s (127.0.0.1 only)\n' "$HOST_PORT"
    printf 'media:     %s\n' "$MEDIA_DIR"
    printf 'url:       http://127.0.0.1:%s/\n' "$HOST_PORT"
    printf 'bridge:    '
    if [[ -f "$QUEUE_DIR/play-bridge.pid" ]] \
       && kill -0 "$(cat "$QUEUE_DIR/play-bridge.pid")" 2>/dev/null; then
        echo "running (pid $(cat "$QUEUE_DIR/play-bridge.pid"))"
    else
        echo "not running -- ./scripts/player-bridge.sh --daemon"
    fi
}

stop_container() {
    if container_exists; then
        info "stopping and removing $CONTAINER (volumes kept)"
        "$runtime" rm -f "$CONTAINER" >/dev/null
    else
        info "$CONTAINER is not running"
    fi
}

destroy() {
    local reply=""
    if ! $destroy_yes; then
        [[ -t 0 ]] \
            || die "refusing to destroy without confirmation; pass --destroy --yes"
        echo "This deletes container '$CONTAINER' AND volumes"
        echo "'$VOLUME', '$CONFIG_VOLUME' and '$CACHE_VOLUME'."
        echo "Your entire catalog -- every playlist and all credentials -- will be gone."
        echo "The media folder ($MEDIA_DIR) is NOT touched."
        echo
        read -r -p "Type 'destroy' to confirm: " reply
        [[ "$reply" == "destroy" ]] || { echo "cancelled"; exit 1; }
    fi
    "$runtime" rm -f "$CONTAINER" >/dev/null 2>&1 || true
    local volume
    for volume in "$VOLUME" "$CONFIG_VOLUME" "$CACHE_VOLUME"; do
        "$runtime" volume rm "$volume" >/dev/null 2>&1 || true
    done
    info "container and volumes removed; media folder untouched: $MEDIA_DIR"
}

# ------------------------------------------------------------------------ main
choose_runtime "$runtime_override"

case "$action" in
    logs)
        exec "$runtime" logs -f "$CONTAINER"
        ;;
    status)
        show_status
        exit 0
        ;;
    stop)
        stop_container
        exit 0
        ;;
    destroy)
        destroy
        exit 0
        ;;
    shell)
        exec "$runtime" exec -it "$CONTAINER" /bin/sh
        ;;
    build)
        build_image
        info "image built. Run ./build.deploy.sh to deploy it."
        exit 0
        ;;
    deploy)
        build_image
        deploy
        ;;
esac
