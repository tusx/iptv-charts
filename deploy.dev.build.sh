#!/usr/bin/env bash
#
# deploy.dev.build.sh -- build the working tree as a dev image and run it.
#
# This is the "real container, but not the production one" loop: same image
# recipe, same media folder detection, but its own image, container, volumes
# and port. The production container and its data volumes are never touched,
# so you can try a branch without risking the catalog you use every day.
#
# Usage:
#   ./deploy.dev.build.sh                 build, (re)deploy, and open the browser
#   ./deploy.dev.build.sh --build         build the dev image only
#   ./deploy.dev.build.sh --logs          follow dev container logs
#   ./deploy.dev.build.sh --status        show dev container and volume state
#   ./deploy.dev.build.sh --stop          stop and remove the dev container
#   ./deploy.dev.build.sh --runtime NAME  podman or docker; skips the prompt
#
# Environment overrides:
#   IPTV_RUNTIME              podman or docker (same as --runtime)
#   IPTV_DEV_PORT             host port to publish (default 8771)
#   IPTV_DEV_IMAGE            image name (default iptv-charts-dev)
#   IPTV_DEV_VOLUME           data volume (default iptv-charts-dev-data)
#   IPTV_DEV_CONFIG_VOLUME    config volume (default iptv-charts-dev-config)
#   IPTV_DEV_CACHE_VOLUME     cache volume (default iptv-charts-dev-cache)
#   IPTV_DEV_CONTAINER        container name (default iptv-charts-dev)
#   IPTV_MEDIA_DIR            media folder on the host (default
#                             ~/Media/IPTV Charts); the same movie/series
#                             detection as build.deploy.sh is used, and the
#                             dev container shares that media folder read-write.
#
# The dev container shares the host play-queue directory with production so
# the player bridge works for both. It only starts the bridge when one is not
# already running; it never restarts the production one.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${IPTV_DEV_IMAGE:-iptv-charts-dev}"
VOLUME="${IPTV_DEV_VOLUME:-iptv-charts-dev-data}"
CONFIG_VOLUME="${IPTV_DEV_CONFIG_VOLUME:-iptv-charts-dev-config}"
CACHE_VOLUME="${IPTV_DEV_CACHE_VOLUME:-iptv-charts-dev-cache}"
CONTAINER="${IPTV_DEV_CONTAINER:-iptv-charts-dev}"
HOST_PORT="${IPTV_DEV_PORT:-8771}"
INTERNAL_PORT="${IPTV_INTERNAL_PORT:-8770}"

QUEUE_DIR="${IPTV_CHARTS_PLAY_QUEUE_DIR:-${XDG_RUNTIME_DIR:-/tmp}/iptv-charts}"
DEFAULT_MEDIA_DIR="$HOME/Media/IPTV Charts"
MEDIA_DIR="${IPTV_MEDIA_DIR:-$DEFAULT_MEDIA_DIR}"

info()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '3,33p' "$0" | sed 's/^# \{0,1\}//'
}

# --------------------------------------------------------------------- runtime
runtime=""
runtime_override="${IPTV_RUNTIME:-}"
action="deploy"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --logs)    action="logs"; shift ;;
        --status)  action="status"; shift ;;
        --stop)    action="stop"; shift ;;
        --build)   action="build"; shift ;;
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

# Same selection rules as build.deploy.sh: an explicit runtime never prompts,
# one installed runtime is used directly, two installed runtimes prompt when
# there is a terminal, and fail with instructions when there is not.
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

volume_label() {
    if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled 2>/dev/null; then
        printf ':Z'
    fi
}

named_volume_suffix() {
    if [[ "$runtime" == "podman" ]]; then
        printf ':U'
    fi
}

# ---------------------------------------------------------------------- paths
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

# Mirrors config.py and build.deploy.sh: the exact canonical folder wins, then
# aliases are matched case-insensitively in declared order.
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

choose_media_dir() {
    local reply=""
    if [[ -z "${IPTV_MEDIA_DIR:-}" && -t 0 ]]; then
        read -r -p "Media folder [$DEFAULT_MEDIA_DIR]: " reply || reply=""
    fi
    MEDIA_DIR="$(normalize_dir "${reply:-${IPTV_MEDIA_DIR:-$DEFAULT_MEDIA_DIR}}")"
    [[ -n "$MEDIA_DIR" ]] || die "the media folder path is empty"
}

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
    info "building dev image $IMAGE with $runtime"
    local -a build_args=(build)
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
        info "reusing existing dev volume $name"
    else
        info "creating dev volume $name"
        "$runtime" volume create "$name" >/dev/null
    fi
}

# --------------------------------------------------------------- player bridge
# Unlike the production deploy, this does not restart a bridge that is already
# running: a dev deploy must not interrupt playback for the production app. It
# only starts one when the host has none, so the external-player toggle works.
ensure_player_bridge() {
    if [[ -x "$root/scripts/player-bridge.sh" ]]; then
        if ! "$root/scripts/player-bridge.sh" --status >/dev/null 2>&1; then
            info "starting player bridge on the host"
            "$root/scripts/player-bridge.sh" --daemon || \
                warn "player bridge failed to start; the in-browser player still works"
        fi
    fi
}

# ------------------------------------------------------------------ readiness
wait_until_ready() {
    info "waiting for the dev app to answer"
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
        warn "the dev app did not become healthy in time. Recent logs:"
        "$runtime" logs --tail 25 "$CONTAINER" >&2 || true
        exit 1
    fi
}

open_browser() {
    local url="$1"
    [[ "${IPTV_CHARTS_NO_OPEN:-}" == "1" ]] && return 0
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 &
    fi
}

# ------------------------------------------------------------------- actions
deploy() {
    local our_container_running=false
    if container_exists && container_running; then
        our_container_running=true
    fi
    if ! $our_container_running \
       && command -v ss >/dev/null 2>&1 \
       && ss -ltn 2>/dev/null | grep -qE ":${HOST_PORT}\b"; then
        die "host port $HOST_PORT is already in use by another process. Re-run with IPTV_DEV_PORT=<other>."
    fi

    choose_media_dir
    choose_library_dirs

    ensure_volume "$VOLUME"
    ensure_volume "$CONFIG_VOLUME"
    ensure_volume "$CACHE_VOLUME"

    mkdir -p "$QUEUE_DIR"
    chmod 700 "$QUEUE_DIR"

    if container_exists; then
        info "replacing existing dev container $CONTAINER"
        "$runtime" rm -f "$CONTAINER" >/dev/null
    else
        info "creating dev container $CONTAINER"
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
        --label "io.github.tusx.iptv-charts=true"
        --label "io.github.tusx.iptv-charts.dev=true")

    if [[ "$runtime" == "podman" ]]; then
        run_args+=(--userns=keep-id)
    fi

    "$runtime" "${run_args[@]}" "$IMAGE" >/dev/null

    ensure_player_bridge
    wait_until_ready

    local url="http://127.0.0.1:${HOST_PORT}/"
    info "dev container running at $url"
    open_browser "$url"

    cat <<EOF

  Runtime:   $runtime
  Container: $CONTAINER   ($runtime logs -f $CONTAINER)
  Image:     $IMAGE
  Port:      $HOST_PORT   (127.0.0.1 only)
  Volumes:   $VOLUME (data), $CONFIG_VOLUME (config), $CACHE_VOLUME (cache)
  Media:     $MEDIA_DIR
  Movies:    $MEDIA_DIR/$MOVIE_DIR_NAME
  Series:    $MEDIA_DIR/$SERIES_DIR_NAME
  URL:       $url
  Bridge:    ./scripts/player-bridge.sh --status

  This is separate from the production container/volumes; --stop only
  removes $CONTAINER.
  Stop:      ./deploy.dev.build.sh --stop

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
}

stop_container() {
    if container_exists; then
        info "stopping and removing $CONTAINER (dev volumes kept)"
        "$runtime" rm -f "$CONTAINER" >/dev/null
    else
        info "$CONTAINER is not running"
    fi
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
    build)
        build_image
        info "dev image built. Run ./deploy.dev.build.sh to deploy it."
        exit 0
        ;;
    deploy)
        build_image
        deploy
        ;;
esac
