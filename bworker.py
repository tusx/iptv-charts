#!/usr/bin/env python3
"""Process entry point for the IPTV Charts bworker.

The real loop lives in :mod:`iptv_charts.bworker`. This file exists so the
container/supervisor can run ``python bworker.py`` exactly where it used to run
``worker.py``. ``worker.py`` remains a compatibility shim for older setup and
test scripts.
"""

from __future__ import annotations

import sys

from iptv_charts.bworker import main

if __name__ == "__main__":
    sys.exit(main())
