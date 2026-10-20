#!/usr/bin/env python3
"""Web server entry point for IPTV Charts.

This process serves HTTP only. Playlist syncs, probing, downloads and scheduled
tasks run in the separate ``worker.py`` process; in the container both are
supervised together. Use ``setup.test.sh`` for the local loop, which starts
both.

Usage:
    python run.py                 # pick a free port and open a browser
    python run.py --no-browser    # don't open a browser
    python run.py --port 9000     # start looking for a free port at 9000
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import webbrowser

from iptv_charts import config
from iptv_charts.app import create_app


def _open_browser_later(url: str, delay: float = 0.6) -> None:
    """Open the browser just after the server starts accepting connections.

    Opening immediately races the server bind and shows a connection error on
    a first run, so we give it a moment.
    """
    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:
            # A missing browser is not fatal -- the URL is printed regardless.
            pass

    threading.Timer(delay, _open).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iptv-charts", description=__doc__)
    parser.add_argument(
        "--port", type=int, default=config.DEFAULT_PORT,
        help=f"preferred port (default {config.DEFAULT_PORT}); the first free "
             "port at or after this is used",
    )
    parser.add_argument(
        "--host", default=config.BIND_HOST,
        help="interface to bind (default loopback only)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="do not open a web browser on startup",
    )
    parser.add_argument(
        "--dev", action="store_true",
        help="use the Flask development server with auto-reload instead of waitress",
    )
    args = parser.parse_args(argv)

    app = create_app()

    port = config.find_free_port(args.port, args.host)
    config.remember_port(port)
    url = f"http://{args.host}:{port}/"

    print(f"{config.APP_NAME}")
    print(f"  data:  {config.data_dir()}")
    print(f"  url:   {url}")
    print("  stop:  Ctrl+C")
    print()
    # Flushed because stdout is block-buffered when not attached to a terminal,
    # which would otherwise hold the URL back until the process exits -- so
    # anyone running this with output redirected to a log sees nothing at all.
    sys.stdout.flush()

    if not args.no_browser:
        _open_browser_later(url)

    if args.dev:
        # Reloader is convenient for editing but must never be the default:
        # it is single-threaded by default and not intended to be left running.
        # It reloads this web process only; restart worker.py after editing
        # worker code.
        app.run(host=args.host, port=port, debug=True, threaded=True)
    else:
        import logging

        from waitress import create_server

        # Per-request logging would be noise for a single-user app, and with
        # progress polling it is a measurable amount of it. Note `_quiet=`
        # is a serve() argument only; create_server rejects it as an unknown
        # adjustment, so silence the logger instead.
        logging.getLogger("waitress").setLevel(logging.WARNING)

        # Most requests are fast, but interactive search/release/grab calls
        # wait for a worker job. Eight threads leave room for those to wait
        # without starving /api/health and the cancel endpoints; waitress is
        # pure Python and the app is single-user.
        server = create_server(app, host=args.host, port=port, threads=8)

        # Waitress does not install signal handlers of its own, so without
        # this a SIGTERM is simply ignored: `podman stop`, systemd and Ctrl+C
        # all fall through to a SIGKILL after the timeout, which truncates
        # whatever the process was doing. Closing the server explicitly makes
        # shutdown immediate and clean.
        def _shutdown(signum, _frame):
            print(f"\nReceived signal {signum}, shutting down.", flush=True)
            server.close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        server.run()

    return 0


if __name__ == "__main__":
    sys.exit(main())
