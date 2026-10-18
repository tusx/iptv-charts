#!/usr/bin/env bash
#
# setup.test.sh -- live development build.
#
# Runs the app directly on this machine from a virtualenv, with auto-reload.
# This is the fast loop for trying out a feature before building a container:
# edit a file, save, and the browser picks it up on refresh. No image build,
# no container, no restart.
#
# Usage:
#   ./setup.test.sh                 # start with auto-reload
#   ./setup.test.sh --check         # run the smoke tests and exit
#   ./setup.test.sh --no-reload     # run without the reloader
#   ./setup.test.sh --port 9000     # use a different port
#   ./setup.test.sh --open          # also open a browser tab on startup
#
# A browser is NOT opened by default -- during development the tab is usually
# already open and reloading it yourself is less disruptive than a new one
# appearing on every restart. The URL is printed on startup either way.
#
# Data lives in ~/.local/share/iptv-charts, which is SEPARATE from the
# container's named volume. See the README before assuming a playlist added
# here will show up in the container, or vice versa.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv="$root/.venv"
stamp="$venv/.requirements.sha256"

check_only=false
use_reload=true
open_browser=false
run_args=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)      check_only=true; shift ;;
        # Auto-reload is the default here -- it is the whole point of the
        # live dev loop -- so this flag turns it *off*.
        --no-reload)  use_reload=false; shift ;;
        --open)       open_browser=true; shift ;;
        --help|-h)
            sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)            run_args+=("$1"); shift ;;
    esac
done

# --------------------------------------------------------------- environment
if ! python3 -c "import venv" 2>/dev/null; then
    echo "error: python3-venv is not installed" >&2
    echo "       run: sudo apt install python3-venv python3-full" >&2
    exit 1
fi

if [[ ! -x "$venv/bin/python" ]]; then
    echo "==> creating virtualenv at .venv"
    python3 -m venv "$venv"
fi

# Only reinstall when requirements.txt actually changed -- pip is slow enough
# that doing it on every run would defeat the point of a fast loop.
fingerprint="$(sha256sum "$root/requirements.txt" | cut -d' ' -f1)"
if [[ ! -f "$stamp" ]] || [[ "$(cat "$stamp")" != "$fingerprint" ]]; then
    echo "==> installing dependencies"
    "$venv/bin/python" -m pip install --quiet --upgrade pip
    "$venv/bin/python" -m pip install --quiet -r "$root/requirements.txt"
    printf '%s' "$fingerprint" > "$stamp"
else
    echo "==> dependencies up to date"
fi

# Catch syntax errors before the server does.
if ! "$venv/bin/python" -m compileall -q "$root/iptv_charts" "$root/run.py"; then
    echo "error: syntax errors above" >&2
    exit 1
fi

# --------------------------------------------------------------------- checks
if $check_only; then
    echo "==> smoke tests (no network)"
    "$venv/bin/python" "$root/tests/smoke.py" || exit 1

    echo
    echo "==> integration tests (local fixture server)"
    "$venv/bin/python" "$root/tests/integration.py" || exit 1

    echo
    echo "==> bridge tests (queue protocol, against the real script)"
    "$venv/bin/python" "$root/tests/bridge.py" || exit 1

    echo
    echo "==> end-to-end tests (HTTP API + rendered pages)"
    "$venv/bin/python" "$root/tests/endtoend.py" || exit 1

    echo
    echo "==> manager tests (library, downloads, scan, backup)"
    exec "$venv/bin/python" "$root/tests/manager.py"
fi

# ----------------------------------------------------------------------- run
if $use_reload; then
    echo "==> starting development server (auto-reload on; Ctrl+C to stop)"
    run_args+=(--dev)
else
    echo "==> starting server without auto-reload (Ctrl+C to stop)"
fi

# Opt-in rather than opt-out: during development the tab is normally already
# open, and a fresh one on every restart is just noise.
if ! $open_browser; then
    run_args+=(--no-browser)
fi

exec "$venv/bin/python" "$root/run.py" "${run_args[@]}"
