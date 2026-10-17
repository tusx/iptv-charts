#!/usr/bin/env bash
#
# player-bridge.sh -- runs on the HOST and plays streams requested by the app.
#
# Why this exists: a container cannot start a process on the host, so when the
# app runs in Podman it has no way to open mpv on your desktop. Instead it
# appends a line of JSON to a queue file inside a shared, ephemeral directory,
# and this script watches that file and launches the player.
#
# Two things travel in that line: the stream (url/title/user_agent/referrer,
# always present, so an older app still plays something) and, from the current
# app, the finished argv in `command` -- the app decides which player to use and
# with which flags, so that choice is implemented once, in one language. A line
# without `command` falls back to the first player found below.
#
# The bridge also publishes players.json next to its heartbeat: the container
# cannot see the host's software, so this file is how the settings page can
# offer the players that actually exist out here.
#
# The app checks for a heartbeat this script writes, so if the bridge is not
# running the external-player buttons hide rather than doing nothing.
#
# Usage:
#   ./scripts/player-bridge.sh              # run in the foreground
#   ./scripts/player-bridge.sh --daemon     # fork into the background
#   ./scripts/player-bridge.sh --status     # is a bridge running?
#   ./scripts/player-bridge.sh --stop       # stop a backgrounded bridge
#
# The queue directory must match what setup.deploy.sh mounts into the
# container. Override with IPTV_CHARTS_PLAY_QUEUE_DIR if you changed it.

set -euo pipefail

QUEUE_DIR="${IPTV_CHARTS_PLAY_QUEUE_DIR:-${XDG_RUNTIME_DIR:-/tmp}/iptv-charts}"
QUEUE="$QUEUE_DIR/queue"
HEARTBEAT="$QUEUE_DIR/play-bridge.alive"
PIDFILE="$QUEUE_DIR/play-bridge.pid"
PLAYERS_FILE="$QUEUE_DIR/players.json"

PLAYERS_FILE_VERSION=1

# If the queue is never drained (bridge stopped for a long time) it can grow;
# this bounds it.
MAX_QUEUE_BYTES=$((1024 * 1024))

# Bounds on one request. The app is the only writer, but "our own app wrote it"
# is not a reason to exec an unbounded argv, and it means a corrupt line is
# skipped with a log entry instead of doing something surprising.
MAX_LINE_BYTES=8192
MAX_ARGV=64
MAX_ARG_BYTES=8192

log() { printf '[player-bridge] %s\n' "$*" >&2; }

# list_players -- one line per player installed on this host:
#   id <TAB> label <TAB> flavour <TAB> extra args <TAB> command
# Both trailing fields are ours, so splitting them on spaces is safe.
# mpv and VLC are frequently installed as Flatpaks, which puts them outside
# PATH, so flatpak is probed as well. Absolute paths, so the app never has to
# guess at this shell's environment.
list_players() {
    local flatpak_bin mpv_bin vlc_bin
    flatpak_bin="$(command -v flatpak || true)"
    mpv_bin="$(command -v mpv || true)"
    vlc_bin="$(command -v vlc || true)"

    if [[ -n "$flatpak_bin" ]] && "$flatpak_bin" info io.mpv.Mpv >/dev/null 2>&1; then
        printf 'flatpak-mpv\tmpv (Flatpak)\tmpv\t--force-window=yes --idle=no\t%s run io.mpv.Mpv\n' "$flatpak_bin"
    fi
    if [[ -n "$mpv_bin" ]]; then
        printf 'native-mpv\tmpv\tmpv\t--force-window=yes --idle=no\t%s\n' "$mpv_bin"
    fi
    if [[ -n "$flatpak_bin" ]] && "$flatpak_bin" info org.videolan.VLC >/dev/null 2>&1; then
        printf 'flatpak-vlc\tVLC (Flatpak)\tvlc\t--play-and-exit\t%s run org.videolan.VLC\n' "$flatpak_bin"
    fi
    if [[ -n "$vlc_bin" ]]; then
        printf 'native-vlc\tVLC\tvlc\t--play-and-exit\t%s\n' "$vlc_bin"
    fi
}

# Write players.json atomically: the app reads this file whenever it renders a
# page, and a half-written one would read as "no players at all".
publish_players() {
    local tmp="$PLAYERS_FILE.$$"
    if list_players | python3 -c '
import json, sys

players = []
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 5:
        continue
    player_id, label, flavour, extra, command = parts
    players.append({
        "id": player_id,
        "label": label,
        "flavour": flavour,
        "extra_args": extra.split(),
        "command": command.split(),
    })

json.dump({"version": int(sys.argv[1]), "players": players}, sys.stdout)
' "$PLAYERS_FILE_VERSION" > "$tmp" 2>/dev/null; then
        mv -f "$tmp" "$PLAYERS_FILE"
    else
        rm -f "$tmp"
    fi
}

write_heartbeat() {
    date +%s > "$HEARTBEAT" 2>/dev/null || true
}

# ---------------------------------------------------------------- subcommands
case "${1:-}" in
    --status)
        if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "bridge running (pid $(cat "$PIDFILE"))"
            echo "queue:   $QUEUE"
            echo "players: $(list_players | cut -f2 | paste -sd', ' -)"
            exit 0
        fi
        echo "bridge not running"
        exit 1
        ;;
    --stop)
        if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            kill "$(cat "$PIDFILE")" 2>/dev/null || true
            rm -f "$PIDFILE"
            echo "stopped"
        else
            echo "not running"
        fi
        exit 0
        ;;
    --daemon)
        mkdir -p "$QUEUE_DIR"
        # Re-exec detached, then return control to the caller.
        setsid "$0" --foreground </dev/null >>"$QUEUE_DIR/play-bridge.log" 2>&1 &
        disown || true
        sleep 0.4
        if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "bridge started (pid $(cat "$PIDFILE"))"
            echo "log:   $QUEUE_DIR/play-bridge.log"
            exit 0
        fi
        echo "bridge failed to start; see $QUEUE_DIR/play-bridge.log" >&2
        exit 1
        ;;
    --foreground|"")
        ;;
    *)
        echo "usage: $0 [--daemon|--status|--stop]" >&2
        exit 2
        ;;
esac

# --------------------------------------------------------------------- launch
# Run an argv, detached, with no shell anywhere: every element is one argument,
# exactly as the app built it.
launch_argv() {
    local -a cmd=( "$@" )

    (( ${#cmd[@]} )) || return 1
    if [[ -z "${cmd[0]}" || "${cmd[0]}" == -* ]]; then
        # setsid would otherwise eat a leading '-' as one of its own options.
        log "refusing: the program name starts with '-'"
        return 1
    fi
    if ! command -v -- "${cmd[0]}" >/dev/null 2>&1; then
        # Not fatal: this script's PATH is whatever started it, which is not
        # necessarily the PATH the app assumed.
        log "warning: ${cmd[0]} is not on this PATH; running it anyway"
    fi

    log "playing: ${cmd[-1]}"
    setsid -- "${cmd[@]}" >/dev/null 2>&1 < /dev/null &
}

# The fallback for a request with no `command`: play it with the first player
# found here, in that player's own flag dialect.
default_cmd=()
default_extra=()
default_flavour=""

pick_default() {
    local id label flavour extra command
    while IFS=$'\t' read -r id label flavour extra command; do
        [[ -n "$id" ]] || continue
        default_flavour="$flavour"
        read -r -a default_extra <<< "$extra"
        read -r -a default_cmd <<< "$command"
        return 0
    done < <(list_players)
    return 1
}

launch_legacy() {
    local url="$1" title="$2" user_agent="$3" referrer="$4"
    local -a args

    [[ -n "$url" ]] || return 1

    # The same rule the app applies, and the reason it exists: the address is
    # the last argument, so one beginning with '-' would be read as an option
    # by the player. An M3U playlist's items are not ours to trust.
    if [[ ! "$url" =~ ^[A-Za-z][A-Za-z0-9+.-]*:// ]]; then
        log "refusing a request whose address is not a URL"
        return 1
    fi

    if (( ${#default_cmd[@]} == 0 )); then
        log "no player on this host to run that request with"
        return 1
    fi

    args=( "${default_extra[@]}" )
    if [[ "$default_flavour" == "vlc" ]]; then
        [[ -n "$title" ]] && args+=( "--meta-title=$title" )
        [[ -n "$user_agent" ]] && args+=( "--http-user-agent=$user_agent" )
        [[ -n "$referrer" ]] && args+=( "--http-referrer=$referrer" )
    else
        [[ -n "$title" ]] && args+=( "--title=$title" )
        # Some providers reject the default User-Agent; M3U playlists often
        # carry #EXTVLCOPT hints that mpv does not read on its own.
        [[ -n "$user_agent" ]] && args+=( "--user-agent=$user_agent" )
        [[ -n "$referrer" ]] && args+=( "--referrer=$referrer" )
        args+=( -- )   # end of options; the url is a location, not a flag
    fi

    launch_argv "${default_cmd[@]}" "${args[@]}" "$url"
}

# ------------------------------------------------------------------ main loop
mkdir -p "$QUEUE_DIR"
echo $$ > "$PIDFILE"

publish_players

if pick_default; then
    log "default player: ${default_cmd[*]}"
else
    # Not fatal any more: a request that names its own command needs nothing
    # installed here, and the app hides the button on its own when this file
    # says there is nothing to run.
    log "no mpv or vlc found on this host -- a custom command will still run"
fi
log "queue: $QUEUE"

# Truncate a pathologically large queue rather than replaying thousands of
# stale requests, but otherwise leave it alone so nothing is lost.
if [[ -f "$QUEUE" ]] && (( $(stat -c %s "$QUEUE" 2>/dev/null || echo 0) > MAX_QUEUE_BYTES )); then
    log "queue exceeded ${MAX_QUEUE_BYTES} bytes; discarding backlog"
    : > "$QUEUE"
fi
touch "$QUEUE"

# Heartbeat, so the app knows the bridge is there. The player list is
# republished on a slower tick, so software installed after this started shows
# up without restarting the bridge.
(
    ticks=0
    while true; do
        write_heartbeat
        if (( ticks % 6 == 0 )); then publish_players; fi
        ticks=$((ticks + 1))
        sleep 5
    done
) &
heartbeat_pid=$!

cleanup() {
    kill "$heartbeat_pid" 2>/dev/null || true
    rm -f "$PIDFILE" "$HEARTBEAT" "$PLAYERS_FILE"
}
trap cleanup EXIT INT TERM

# -n0: only lines appended from now on, so old requests are not replayed.
tail -f -n0 "$QUEUE" | while IFS= read -r line; do
    [[ -z "$line" ]] && continue

    # Bounded before it is parsed: `read` has no limit of its own, and a command
    # array makes long lines routine rather than exotic.
    if (( ${#line} > MAX_LINE_BYTES )); then
        log "ignoring an oversized request (${#line} bytes)"
        continue
    fi

    # Decoded with python3: JSON in pure bash is not worth the bugs. Emits
    # NUL-separated fields starting with a tag -- NUL is the only byte an argv
    # element cannot contain, so it is the one separator a value cannot forge.
    fields=()
    while IFS= read -r -d '' field; do
        fields+=("$field")
    done < <(printf '%s' "$line" | python3 -c '
import json, sys

MAX_ARGV, MAX_ARG = 64, 8192

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(data, dict):
    sys.exit(0)


def clean(value):
    # NUL cannot reach execve and a newline would break the record, so both are
    # neutralised here rather than smuggled through the field separator.
    text = str(value if value is not None else "")
    for char in ("\x00", "\n", "\r", "\t"):
        text = text.replace(char, " ")
    return text


command = data.get("command")
if (isinstance(command, list) and 0 < len(command) <= MAX_ARGV
        and all(isinstance(part, str) for part in command)):
    argv = [clean(part) for part in command]
    if all(part and len(part.encode()) <= MAX_ARG for part in argv):
        sys.stdout.write("\0".join(["argv", *argv]) + "\0")
        sys.exit(0)

sys.stdout.write("\0".join([
    "legacy",
    clean(data.get("url")),
    clean(data.get("title")),
    clean(data.get("user_agent")),
    clean(data.get("referrer")),
]) + "\0")
' 2>/dev/null) || true

    (( ${#fields[@]} )) || continue

    case "${fields[0]}" in
        argv)
            launch_argv "${fields[@]:1}" || continue
            ;;
        legacy)
            launch_legacy "${fields[1]:-}" "${fields[2]:-}" "${fields[3]:-}" "${fields[4]:-}" || continue
            ;;
        *)
            log "ignoring an unreadable request"
            ;;
    esac
done
