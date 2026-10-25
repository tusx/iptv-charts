"""Gunicorn entry point for IPTV Charts.

Gunicorn imports this module in each worker and calls :func:`create_app` once.
The web process only serves HTTP and enqueues jobs; bworker is a separate
supervised process and the only one that runs provider calls, ffprobe/ffmpeg
or imports. Nothing here may start a background thread, or every web worker
would race to become a second bworker.

Run locally with::

    gunicorn --bind 127.0.0.1:8771 --worker-class gthread --threads 8 wsgi:app

The container supervises nginx (public :8770), this gunicorn process
(loopback :8771), bworker and valkey-server.
"""

from __future__ import annotations

from iptv_charts.app import create_app

app = create_app()
