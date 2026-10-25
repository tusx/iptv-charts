"""Bounded candidate acquisition for the 0.8.0 bworker.

The public API is deliberately small:

``run_movie_iteration`` / ``run_episode_iteration``
    One bounded automatic iteration: discover a batch, probe at most a few
    candidates, then either hand off one download or report that more work is
    needed. State lives in ``bworker_scan``/``bworker_candidates``.

``bounded_movie_search``
    The synchronous, non-persistent wrapper used by manual search. It scans a
    bounded number of matches and probes a bounded number of releases.

``annotate_download``
    Records the profile score on an enqueued download.

``scan_snapshot`` / ``candidate_rows``
    Read helpers for tests and diagnostics.
"""

from .engine import (
    annotate_download,
    bounded_movie_search,
    candidate_rows,
    run_episode_iteration,
    run_movie_iteration,
    scan_snapshot,
)

__all__ = [
    "annotate_download",
    "bounded_movie_search",
    "candidate_rows",
    "run_episode_iteration",
    "run_movie_iteration",
    "scan_snapshot",
]
