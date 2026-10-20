#!/usr/bin/env python3
"""Background worker entry point for IPTV Charts.

This is the second process in the container. It owns every slow, provider
facing or disk-writing operation the app performs: playlist syncs, ffprobe
candidate searches, downloads/imports, scheduled searches, metadata refreshes
and library scans. The web server only queues jobs and reads their status.

Run it beside the web server:

    python worker.py

The container supervises this process; ``setup.test.sh`` starts it for the
local development loop. The worker is safe to stop at any time -- interrupted
jobs are durable rows in SQLite and are requeued on the next start.
"""

from __future__ import annotations

import sys

from iptv_charts.worker import main

if __name__ == "__main__":
    sys.exit(main())
