#!/usr/bin/env python3
"""Compatibility entry point for the old worker process name.

0.8.0 renamed the background process to bworker, and the supported entry
point is now ``python bworker.py``. This file stays so existing scripts that
still launch ``worker.py`` keep working; it starts exactly the same loop.

The bworker owns every slow, provider-facing or disk-writing operation the app
performs: playlist syncs, ffprobe candidate searches, downloads/imports,
scheduled searches, metadata refreshes and library scans. The web server only
queues jobs and reads their status. It is safe to stop at any time -- running
work is durable in SQLite and is requeued on the next start.
"""

from __future__ import annotations

import sys

from iptv_charts.bworker import main

if __name__ == "__main__":
    sys.exit(main())
