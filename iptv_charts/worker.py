"""Compatibility shim for the old worker module.

0.8.0 renamed the process to bworker. Existing setup scripts, test suites and
imports that still say ``iptv_charts.worker`` keep working by re-exporting the
bworker loop and its registries. New code should import
:mod:`iptv_charts.bworker` directly.
"""

from __future__ import annotations

from .bworker import (
    HANDLERS,
    JOB_HANDLERS,
    WORK_HANDLERS,
    Bworker,
    WorkContext,
    main,
    register_job_handler,
    register_work_handler,
    run_forever,
    start_background,
    status,
    stop_background,
)

__all__ = [
    "HANDLERS",
    "JOB_HANDLERS",
    "WORK_HANDLERS",
    "Bworker",
    "WorkContext",
    "main",
    "register_job_handler",
    "register_work_handler",
    "run_forever",
    "start_background",
    "status",
    "stop_background",
]
