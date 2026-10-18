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
RESULT_FILE="$QUEUE_DIR/play-result.json"
ERROR_FILE="$QUEUE_DIR/play-last.err"

PLAYERS_FILE_VERSION=1

# How long a player gets to die before we call it started. A bad URL, a wrong
# flag or a broken Flatpak exits in well under a second; mpv needs a moment to
# report a provider's HTTP error and quit.
PLAYER_START_GRACE=2.0

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

# Anything URL-shaped in a player's error is replaced before it leaves this
# machine's logs and the app's: provider URLs carry the account name and
# password in their path, and app logs get pasted into bug reports.
redact_error() {
    sed -E 's#[A-Za-z][A-Za-z0-9+.-]*://[^[:space:]]+#<url>#g' "$1" 2>/dev/null \
        | tail -n 3 | paste -sd' ' -
}

# Tell the app what became of one request. Written for the app to read by
# requested_at, so a stale result can never be mistaken for this request's:
# without it, a click that opened nothing still looked like a success.
write_result() {
    local requested_at="$1" player_id="$2" ok="$3" message="$4"
    local tmp="$RESULT_FILE.$$"

    if python3 - "$requested_at" "$player_id" "$ok" "$message" > "$tmp" 2>/dev/null <<'PY'
import json, sys

requested_at, player, ok, message = sys.argv[1:5]
try:
    requested_at = float(requested_at)
except ValueError:
    requested_at = 0.0
json.dump({"requested_at": requested_at, "player": player,
           "ok": ok == "true", "message": message}, sys.stdout)
PY
    then
        mv -f "$tmp" "$RESULT_FILE"
    else
        rm -f "$tmp"
    fi
}

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
        printf 'flatpak-mpv\tmpv (Flatpak)\tmpv\t--force-window=yes --idle=yes\t%s run io.mpv.Mpv\n' "$flatpak_bin"
    fi
    if [[ -n "$mpv_bin" ]]; then
        printf 'native-mpv\tmpv\tmpv\t--force-window=yes --idle=yes\t%s\n' "$mpv_bin"
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

# Take down the worker's whole process group, not just the shell: a worker
# killed outright leaves its heartbeat loop and queue reader running, and two
# readers on one queue open two players per click.
reap_worker() {
    [[ -n "${child:-}" ]] || return 0
    kill -TERM -- "-$child" 2>/dev/null || true
    child=""
}

# A pid file is a weak claim on a running process: it can be removed while the
# bridge lives (it happened), and pids get reused. When it is missing or dead,
# look for the process itself, scoped to this queue directory so a bridge for
# another directory is not mistaken for ours.
find_running_bridge() {
    local pid pattern
    # The supervisor first: it is the process --stop has to end, and the
    # foreground worker it keeps alive must not be mistaken for the whole thing.
    for pattern in "player-bridge.sh --supervise" "player-bridge.sh --foreground"; do
        for pid in $(pgrep -f "$pattern" 2>/dev/null); do
            # A worker's helpers (heartbeat, queue reader) share its command
            # line, so only the group leader counts -- that is the process
            # --stop must signal. Everything else has the worker as its pgid.
            if [[ "$pattern" == *--foreground ]] \
                    && [[ "$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" != "$pid" ]]; then
                continue
            fi
            # A bridge started without the variable is using the default
            # directory, so compare the effective value rather than the raw
            # environment; otherwise the common case never matches.
            local running_dir
            running_dir="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
                | sed -n 's/^IPTV_CHARTS_PLAY_QUEUE_DIR=//p' | head -1)"
            [[ -n "$running_dir" ]] || running_dir="${XDG_RUNTIME_DIR:-/tmp}/iptv-charts"
            if [[ "$running_dir" == "$QUEUE_DIR" ]]; then
                printf '%s\n' "$pid"
                return 0
            fi
        done
    done
    return 1
}

bridge_pid() {
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        printf '%s\n' "$pid"
        return 0
    fi
    pid="$(find_running_bridge)"
    [[ -n "$pid" ]] || return 1
    # Repair the pid file so the next reader is not misled again.
    printf '%s\n' "$pid" > "$PIDFILE"
    printf '%s\n' "$pid"
}

# ---------------------------------------------------------------- subcommands
case "${1:-}" in
    --status)
        if pid="$(bridge_pid)"; then
            echo "bridge running (pid $pid)"
            echo "queue:   $QUEUE"
            echo "players: $(list_players | cut -f2 | paste -sd', ' -)"
            exit 0
        fi
        echo "bridge not running"
        exit 1
        ;;
    --stop)
        if pid="$(bridge_pid)"; then
            kill "$pid" 2>/dev/null || true
            # The shell is blocked reading the queue, and a signal does not
            # interrupt that read, so the trap would never run. Ending the
            # reader gives it EOF, which lets it finish and clean up after
            # itself. Players are already detached and are not touched.
            pkill -f -- "tail -f -n0 $QUEUE" 2>/dev/null || true
            for _ in $(seq 1 30); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.1
            done
            if kill -0 "$pid" 2>/dev/null; then
                kill -9 "$pid" 2>/dev/null || true
                echo "stopped (had to force it)"
            else
                echo "stopped"
            fi
            rm -f "$PIDFILE" "$HEARTBEAT" "$PLAYERS_FILE" "$RESULT_FILE" "$ERROR_FILE"
        else
            echo "not running"
        fi
        exit 0
        ;;
    --supervise)
        # Keeps the worker alive: if it exits -- a crash, an (unexpected)
        # logout leftover, anything -- it is started again. Started from the
        # user's session by --daemon, so the graphical environment mpv needs
        # is inherited rather than guessed at by a service manager.
        echo $$ > "$PIDFILE"
        child=""
        # Job control gives each worker its own process group, so whatever it
        # left behind (heartbeat, queue reader, tail) can be taken down with
        # it. Without that, a killed worker's reader keeps tailing the queue
        # and the next worker competes with it.
        set -m
        trap 'reap_worker; rm -f "$PIDFILE"; exit 0' TERM INT
        while true; do
            IPTV_CHARTS_BRIDGE_SUPERVISED=1 "$0" --foreground &
            child=$!
            wait "$child" || true
            reap_worker
            sleep 3
        done
        ;;
    --daemon)
        mkdir -p "$QUEUE_DIR"
        if pid="$(bridge_pid)"; then
            # Idempotent on purpose: a deploy calls this every time, and a
            # second supervisor would open a second player per click.
            echo "bridge already running (pid $pid)"
            exit 0
        fi
        # Re-exec detached, then return control to the caller.
        setsid "$0" --supervise </dev/null >>"$QUEUE_DIR/play-bridge.log" 2>&1 &
        disown || true
        sleep 0.6
        if pid="$(bridge_pid)"; then
            echo "bridge started (pid $pid, restarts automatically)"
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
# exactly as the app built it. The player's first second of output is kept so a
# failure can be explained instead of swallowed.
launch_argv() {
    local requested_at="$1" player_id="$2"
    shift 2
    local -a cmd=( "$@" )

    if (( ${#cmd[@]} == 0 )); then
        write_result "$requested_at" "$player_id" false "empty command"
        log "refusing: the request has an empty command"
        return 1
    fi
    if [[ -z "${cmd[0]}" || "${cmd[0]}" == -* ]]; then
        # setsid would otherwise eat a leading '-' as one of its own options.
        write_result "$requested_at" "$player_id" false \
            "the program name starts with '-'"
        log "refusing: the program name starts with '-'"
        return 1
    fi
    if ! command -v -- "${cmd[0]}" >/dev/null 2>&1; then
        # Not fatal: this script's PATH is whatever started it, which is not
        # necessarily the PATH the app assumed.
        log "warning: ${cmd[0]} is not on this PATH; running it anyway"
    fi

    : > "$ERROR_FILE"
    log "playing: ${cmd[-1]}"
    # Both streams: mpv writes its diagnostics to stdout, not stderr, so
    # capturing only stderr is how a failed launch ended up with no explanation.
    setsid -- "${cmd[@]}" >"$ERROR_FILE" 2>&1 < /dev/null &
    local player_pid=$!

    sleep "$PLAYER_START_GRACE"
    if kill -0 "$player_pid" 2>/dev/null; then
        write_result "$requested_at" "$player_id" true ""
        log "started (pid $player_pid)"
        return 0
    fi

    local status=0
    wait "$player_pid" 2>/dev/null || status=$?
    local message
    message="$(redact_error "$ERROR_FILE")"
    write_result "$requested_at" "$player_id" false "$message"
    log "failed (status $status): ${message:-no output from the player}"
    return 1
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
    local requested_at="${5:-0}" player_id="${6:-unknown}"
    local -a args

    if [[ -z "$url" ]]; then
        write_result "$requested_at" "$player_id" false "the request has no address"
        return 1
    fi

    # The same rule the app applies, and the reason it exists: the address is
    # the last argument, so one beginning with '-' would be read as an option
    # by the player. An M3U playlist's items are not ours to trust.
    if [[ ! "$url" =~ ^[A-Za-z][A-Za-z0-9+.-]*:// ]]; then
        write_result "$requested_at" "$player_id" false \
            "the address is not a URL"
        log "refusing a request whose address is not a URL"
        return 1
    fi

    if (( ${#default_cmd[@]} == 0 )); then
        write_result "$requested_at" "$player_id" false \
            "no player is installed on the host"
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

    launch_argv "$requested_at" "$player_id" "${default_cmd[@]}" "${args[@]}" "$url"
}

# ------------------------------------------------------------------ main loop
mkdir -p "$QUEUE_DIR"
# The supervisor owns the pid file when there is one: a worker that rewrote it
# would point --stop at itself and leave the supervisor restarting it.
if [[ "${IPTV_CHARTS_BRIDGE_SUPERVISED:-}" != "1" ]]; then
    echo $$ > "$PIDFILE"
fi

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
    kill "$heartbeat_pid" "${READER_PID:-}" 2>/dev/null || true
    # The reader is a wrapper around tail; the tail itself has to go too.
    [[ -n "${READER_PID:-}" ]] && pkill -P "$READER_PID" 2>/dev/null || true
    # A supervised worker does not own the pid file -- removing it here would
    # make --status lose a bridge the supervisor is about to restart.
    if [[ "${IPTV_CHARTS_BRIDGE_SUPERVISED:-}" != "1" ]]; then
        rm -f "$PIDFILE"
    fi
    rm -f "$HEARTBEAT" "$PLAYERS_FILE" "$RESULT_FILE" "$ERROR_FILE"
}
trap cleanup EXIT INT TERM

# -n0: only lines appended from now on, so old requests are not replayed.
#
# The reader is a coprocess rather than `tail -f | while`: with a pipeline in
# the foreground bash defers the trap until it finishes, and `tail -f` never
# does, so SIGTERM was ignored and `--stop` left the bridge running. Several
# orphaned bridges then all tailed the same queue and opened a player each.
coproc QUEUE_TAIL { tail -f -n0 "$QUEUE"; }
READER_PID="$QUEUE_TAIL_PID"
reader_fd="${QUEUE_TAIL[0]}"

while IFS= read -r line <&"$reader_fd"; do
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


# The app stamps every request; the result it gets back carries the same
# stamp, so an old result can never be read as the current request.
requested_at = data.get("requested_at")
try:
    stamp = str(float(requested_at))
except (TypeError, ValueError):
    stamp = "0"

player_id = clean(data.get("player")) or "unknown"

command = data.get("command")
if (isinstance(command, list) and 0 < len(command) <= MAX_ARGV
        and all(isinstance(part, str) for part in command)):
    argv = [clean(part) for part in command]
    if all(part and len(part.encode()) <= MAX_ARG for part in argv):
        sys.stdout.write("\0".join(["argv", stamp, player_id, *argv]) + "\0")
        sys.exit(0)

sys.stdout.write("\0".join([
    "legacy",
    clean(data.get("url")),
    clean(data.get("title")),
    clean(data.get("user_agent")),
    clean(data.get("referrer")),
    stamp,
    player_id,
]) + "\0")
' 2>/dev/null) || true

    (( ${#fields[@]} )) || continue

    case "${fields[0]}" in
        argv)
            # fields: tag, requested_at, player, argv...
            launch_argv "${fields[1]:-0}" "${fields[2]:-unknown}" \
                "${fields[@]:3}" || continue
            ;;
        legacy)
            # fields: tag, url, title, user_agent, referrer, requested_at, player
            launch_legacy "${fields[1]:-}" "${fields[2]:-}" "${fields[3]:-}" \
                "${fields[4]:-}" "${fields[5]:-0}" "${fields[6]:-unknown}" || continue
            ;;
        *)
            log "ignoring an unreadable request"
            ;;
    esac
done
