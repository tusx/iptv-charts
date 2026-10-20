#!/usr/bin/env bash
#
# setup.deploy.sh -- build the image and run it as a Podman container.
#
# This is the deployment path. For iterating on a feature, use
# setup.test.sh instead, which runs from a virtualenv with auto-reload and is
# far faster. Build a container when you want the real thing.
#
# Usage:
#   ./setup.deploy.sh              build, (re)deploy, and open the browser
#   ./setup.deploy.sh --build      build the image only
#   ./setup.deploy.sh --logs       follow container logs
#   ./setup.deploy.sh --status     show container and volume state
#   ./setup.deploy.sh --stop       stop and remove the container (keeps data)
#   ./setup.deploy.sh --destroy    stop, remove container AND delete the volume
#   ./setup.deploy.sh --shell      open a shell inside the running container
#
# Environment overrides:
#   IPTV_PORT        host port to publish (default 8770)
#   IPTV_IMAGE       image name (default iptv-charts)
#   IPTV_VOLUME      volume name (default iptv-charts-data)
#   IPTV_CONTAINER   container name (default iptv-charts)
#   IPTV_MEDIA_DIR   media folder on the host (default ~/Media/IPTV Charts);
#                    Movies/ and Series/ are created inside it and mounted at
#                    /media. --destroy never touches this folder.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE="${IPTV_IMAGE:-iptv-charts}"
VOLUME="${IPTV_VOLUME:-iptv-charts-data}"
CONTAINER="${IPTV_CONTAINER:-iptv-charts}"
HOST_PORT="${IPTV_PORT:-8770}"
INTERNAL_PORT=8770

# Ephemeral IPC directory shared with the player bridge. XDG_RUNTIME_DIR is
# per-user, 0700 and cleared on logout, which is exactly right for this --
# it is not state worth keeping.
QUEUE_DIR="${IPTV_CHARTS_PLAY_QUEUE_DIR:-${XDG_RUNTIME_DIR:-/tmp}/iptv-charts}"

# Where downloads are written on the host. Movies/ and Series/ live inside it;
# the container sees the whole folder at /media.
MEDIA_DIR="${IPTV_MEDIA_DIR:-$HOME/Media/IPTV Charts}"

info()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require_podman() {
    command -v podman >/dev/null || die "podman is not installed"
}

# SELinux needs an explicit relabel on bind mounts; most non-Fedora systems
# have no SELinux at all and would reject the suffix as noise.
volume_label() {
    if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled 2>/dev/null; then
        printf ':Z'
    fi
}

# ------------------------------------------------------------------ subcommands
case "${1:-}" in
    --logs)
        require_podman
        exec podman logs -f "$CONTAINER"
        ;;
    --status)
        require_podman
        printf 'container: '
        if podman container exists "$CONTAINER" 2>/dev/null; then
            podman ps -a --filter "name=^${CONTAINER}$" \
                --format '{{.Status}}  (image {{.Image}})'
        else
            echo "not created"
        fi
        printf 'volume:    '
        if podman volume exists "$VOLUME" 2>/dev/null; then
            mountpoint="$(podman volume inspect "$VOLUME" --format '{{.Mountpoint}}')"
            size="$(du -sh "$mountpoint" 2>/dev/null | cut -f1 || echo '?')"
            echo "$VOLUME  ($size at $mountpoint)"
        else
            echo "not created"
        fi
        printf 'url:       http://127.0.0.1:%s/\n' "$HOST_PORT"
        printf 'media:     %s\n' "$MEDIA_DIR"
        printf 'bridge:    '
        if [[ -f "$QUEUE_DIR/play-bridge.pid" ]] \
           && kill -0 "$(cat "$QUEUE_DIR/play-bridge.pid")" 2>/dev/null; then
            echo "running (pid $(cat "$QUEUE_DIR/play-bridge.pid"))"
        else
            echo "not running -- ./scripts/player-bridge.sh --daemon"
        fi
        exit 0
        ;;
    --stop)
        require_podman
        if podman container exists "$CONTAINER" 2>/dev/null; then
            info "stopping and removing $CONTAINER (volume kept)"
            podman rm -f "$CONTAINER" >/dev/null
        else
            info "$CONTAINER is not running"
        fi
        exit 0
        ;;
    --destroy)
        require_podman
        if [[ "${2:-}" != "--yes" ]]; then
            echo "This deletes container '$CONTAINER' AND volume '$VOLUME'."
            echo "Your entire catalog -- every playlist and all credentials -- will be gone."
            echo
            read -r -p "Type 'destroy' to confirm: " reply
            [[ "$reply" == "destroy" ]] || { echo "cancelled"; exit 1; }
        fi
        podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
        podman volume rm "$VOLUME" >/dev/null 2>&1 || true
        info "container and volume removed"
        exit 0
        ;;
    --shell)
        require_podman
        exec podman exec -it "$CONTAINER" /bin/bash
        ;;
    --build)
        BUILD_ONLY=true
        ;;
    "")
        BUILD_ONLY=false
        ;;
    *)
        sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'
        exit 2
        ;;
esac

require_podman

# ------------------------------------------------------------------------ build
info "building image $IMAGE"
# --format docker is required: the default OCI format silently ignores
# HEALTHCHECK, which would leave the container with no health reporting at all.
podman build --format docker -t "$IMAGE" -f "$root/Containerfile" "$root"

if [[ "${BUILD_ONLY:-false}" == "true" ]]; then
    info "image built. Run ./setup.deploy.sh to deploy it."
    exit 0
fi

# ----------------------------------------------------------------- host port
# The port being busy is expected when we are about to replace our own running
# container -- --replace frees it a moment later. Only a *foreign* holder is a
# genuine conflict, so check for that rather than failing on any listener.
our_container_running=false
if podman container exists "$CONTAINER" 2>/dev/null \
   && [[ "$(podman inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]]; then
    our_container_running=true
fi

if ! $our_container_running \
   && command -v ss >/dev/null 2>&1 \
   && ss -ltn 2>/dev/null | grep -qE ":${HOST_PORT}\b"; then
    die "host port $HOST_PORT is already in use by another process. Re-run with IPTV_PORT=<other>."
fi

# ---------------------------------------------------------------------- volume
if podman volume exists "$VOLUME" 2>/dev/null; then
    info "reusing existing volume $VOLUME"
else
    info "creating volume $VOLUME"
    podman volume create "$VOLUME" >/dev/null
fi

# --------------------------------------------------------------- play queue
mkdir -p "$QUEUE_DIR"
chmod 700 "$QUEUE_DIR"

# --------------------------------------------------------------------- media
# The app writes Movies/ and Series/ here. Creating the folders up front makes
# the first import land in the expected place, and keeps the bind mount from
# being created root-owned by podman.
mkdir -p "$MEDIA_DIR/Movies" "$MEDIA_DIR/Series"

# --------------------------------------------------------------------- deploy
if podman container exists "$CONTAINER" 2>/dev/null; then
    info "replacing existing container $CONTAINER"
else
    info "creating container $CONTAINER"
fi

# --userns=keep-id maps this user onto the same uid inside the container, so
#   files written to the volume are owned by you and remain readable on the
#   host. The :U on the data volume then makes the volume writable by that uid.
# -p binds the host side to 127.0.0.1 only. The container listens on 0.0.0.0
#   (it has to, or the published port could not reach it), but nothing is
#   exposed beyond loopback, and the app's own Host-header check still applies.
podman run -d \
    --name "$CONTAINER" \
    --replace \
    --userns=keep-id \
    --restart unless-stopped \
    -p "127.0.0.1:${HOST_PORT}:${INTERNAL_PORT}" \
    -v "${VOLUME}:/data:U" \
    -v "${QUEUE_DIR}:/playqueue$(volume_label)" \
    -v "${MEDIA_DIR}:/media$(volume_label)" \
    -e "IPTV_CHARTS_MEDIA_HOST_DIR=${MEDIA_DIR}" \
    --label "io.github.tusx.iptv-charts=true" \
    "$IMAGE" >/dev/null

# --------------------------------------------------------------- player bridge
# Started BEFORE the readiness probe on purpose. The app caches player
# detection for 10 seconds, so if the bridge comes up after the first probe
# the external-player button stays hidden until that cache expires.
#
# Every deploy restarts it, whether or not one was already running: the bridge
# decides what a player click actually runs, so a bridge left over from an
# older build would keep answering with the old behaviour. It is supervised, so
# it also comes back by itself if the worker dies.
if [[ -x "$root/scripts/player-bridge.sh" ]]; then
    info "restarting player bridge on the host"
    "$root/scripts/player-bridge.sh" --stop >/dev/null 2>&1 || true
    "$root/scripts/player-bridge.sh" --daemon || \
        warn "player bridge failed to start; the in-browser player still works"
fi

# ------------------------------------------------------------------ readiness
info "waiting for the app to answer"
ready=false
for _ in $(seq 1 40); do
    health="$(curl -fsS --max-time 2 "http://127.0.0.1:${HOST_PORT}/api/health" 2>/dev/null || true)"
    if [[ -n "$health" ]] && grep -q '"alive": true' <<<"$health"; then
        ready=true
        break
    fi
    if ! podman container exists "$CONTAINER" 2>/dev/null \
       || [[ "$(podman inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]]; then
        break
    fi
    sleep 0.5
done

if ! $ready; then
    warn "the app did not become healthy in time. Recent logs:"
    podman logs --tail 25 "$CONTAINER" >&2 || true
    exit 1
fi

url="http://127.0.0.1:${HOST_PORT}/"
info "running at $url"

if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 &
fi

cat <<EOF

  Container: $CONTAINER   (podman logs -f $CONTAINER)
  Volume:    $VOLUME      (persists across rebuilds)
  Media:     $MEDIA_DIR   (Movies/ and Series/; never deleted by --destroy)
  URL:       $url
  Bridge:    ./scripts/player-bridge.sh --status

  Stop:      ./setup.deploy.sh --stop
  Wipe data: ./setup.deploy.sh --destroy

EOF
