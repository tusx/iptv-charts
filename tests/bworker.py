#!/usr/bin/env python3
"""Standalone tests for the 0.8.0 bworker core.

Run with:  .venv/bin/python tests/bworker.py

No pytest, no network, no Valkey server. The Valkey store's trim and degraded
behaviour are exercised through the in-memory fake and a refused-port URL;
the bworker always takes an injected store so its behaviour is deterministic.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

_TMP = Path(tempfile.mkdtemp(prefix="iptv-bworker-test-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_TMP / "media")
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"
os.environ["IPTV_CHARTS_VALKEY_URL"] = "redis://127.0.0.1:1/0"
os.environ["IPTV_CHARTS_LOG_LEVEL"] = "CRITICAL"
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)

sys.path.insert(0, str(ROOT))

from iptv_charts import (  # noqa: E402
    activity_log,
    bworker,
    bworker_store as store,
    config,
    db,
    jobs,
    scheduler,
)

# Several tests deliberately trigger handled retries; keep the suite output
# focused on check results. The handlers still run and log via the module
# logger, which is simply disabled here.
logging.disable(logging.CRITICAL)

_passed = 0
_failures: list[tuple[str, str]] = []


def check(label: str, condition, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        return
    _failures.append((label, detail))
    print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


class FakeClock:
    def __init__(self) -> None:
        self.value = time.time()

    def __call__(self) -> float:
        return self.value

    def time(self) -> int:
        return int(self.value)

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class HugeActivityLog(activity_log.MemoryActivityLog):
    """Fake whose own constructor cap never trims; lets the loop cleanup run."""

    def configure(self, cap=None, trim_to=None) -> None:
        return None


def connect():
    return db.connect()


def clean_db() -> None:
    conn = connect()
    try:
        for table in (
            "worker_jobs", "bworker_events", "bworker_scan", "bworker_candidates",
            "bworker_work", "downloads", "movie_alternates",
            "series_episodes", "series", "movies", "items", "categories",
            "sources",
        ):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.execute("DELETE FROM bworker_state")
        conn.commit()
    finally:
        conn.close()


def make_worker(activity=None, clock=None, *, progress_watch=False):
    activity = (activity if activity is not None
                else activity_log.MemoryActivityLog(cap=1000, trim_to=100))
    clock = clock or FakeClock()
    runner = bworker.Bworker(activity=activity, clock=clock, wall_clock=clock,
                             progress_watch=progress_watch)
    return runner, activity, clock


def add_movie(*, title="Test Movie", monitored=1, release_date="2020-01-01",
              status="released", file_path=None, tmdb_id=None) -> int:
    conn = connect()
    try:
        if tmdb_id is None:
            tmdb_id = int(time.time() * 1000) % 2_000_000_000
            while conn.execute("SELECT 1 FROM movies WHERE tmdb_id = ?",
                               (tmdb_id,)).fetchone() is not None:
                tmdb_id += 1
        cursor = conn.execute(
            "INSERT INTO movies (tmdb_id, title, added_at, monitored, "
            "release_date, status, file_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tmdb_id, title, int(time.time()), monitored, release_date,
             status, file_path),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def add_series(*, title="Test Series", monitored=1, tmdb_id=None) -> int:
    conn = connect()
    try:
        if tmdb_id is None:
            tmdb_id = int(time.time() * 1000) % 2_000_000_000
            while conn.execute("SELECT 1 FROM series WHERE tmdb_id = ?",
                               (tmdb_id,)).fetchone() is not None:
                tmdb_id += 1
        cursor = conn.execute(
            "INSERT INTO series (tmdb_id, title, added_at, monitored) "
            "VALUES (?, ?, ?, ?)",
            (tmdb_id, title, int(time.time()), monitored),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def add_episode(series_id: int, season: int, episode: int, *,
                air_date="2020-01-01", monitored=1) -> int:
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO series_episodes (series_id, season, episode, air_date, "
            "monitored, title) VALUES (?, ?, ?, ?, ?, ?)",
            (series_id, season, episode, air_date, monitored,
             f"S{season:02d}E{episode:02d}"),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def add_download(*, movie_id=None, episode_id=None, series_id=None,
                 status="queued", reason="auto", title="Test Movie",
                 max_attempts=3) -> int:
    conn = connect()
    try:
        if movie_id is not None:
            kind, provider_kind = "movie", "vod"
        else:
            kind, provider_kind = "episode", "series"
        cursor = conn.execute(
            """
            INSERT INTO downloads
                (kind, movie_id, series_id, episode_id, source_name,
                 provider_kind, provider_remote_id, episode_stream_id,
                 container_ext, title, resolution, source, quality_label,
                 priority, mode, release_name, reason, status, attempts,
                 max_attempts, requested_at)
            VALUES (?, ?, ?, ?, 'Test playlist', ?, 'remote-1', ?, 'mkv',
                    ?, '1080p', 'web-dl', '1080p', 0, 'main', 'release',
                    ?, ?, 0, ?, ?)
            """,
            (kind, movie_id, series_id, episode_id, provider_kind,
             "stream-1" if episode_id else None, title, reason, status,
             max_attempts, int(time.time())),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def insert_work(*, work_key="test:work", work_type="movie_search",
                entity_type="global", entity_id=None, payload=None,
                stage="search", status="pending", next_run_at=None,
                enabled=1, max_failures=None, infinite_retry=0,
                retry_delay_seconds=10, backoff_multiplier=2.0,
                current_download_id=None, cursor=None, best_score=None) -> int:
    conn = connect()
    try:
        now = int(time.time())
        if next_run_at is None:
            next_run_at = now - 1
        result = conn.execute(
            """
            INSERT INTO bworker_work
                (work_key, work_type, entity_type, entity_id, payload, enabled,
                 stage, status, failure_count, max_failures, infinite_retry,
                 retry_delay_seconds, backoff_multiplier, next_run_at, cursor,
                 best_score, current_download_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (work_key, work_type, entity_type, entity_id,
             json.dumps(payload or {}), enabled, stage, status, max_failures,
             infinite_retry, retry_delay_seconds, backoff_multiplier,
             next_run_at, cursor, best_score, current_download_id, now, now),
        )
        conn.commit()
        return int(result.lastrowid)
    finally:
        conn.close()


def get_work(work_id: int) -> dict:
    conn = connect()
    try:
        row = store.get_work(conn, work_id)
        assert row is not None
        return row
    finally:
        conn.close()


def activity_entries(activity, event=None, work_id=None) -> list[dict]:
    entries = activity.recent(500)
    if event is not None:
        entries = [entry for entry in entries if entry.get("event") == event]
    if work_id is not None:
        entries = [entry for entry in entries
                   if int(entry.get("work_id") or 0) == int(work_id)]
    return entries


def latest_entry(activity, **filters) -> dict | None:
    entries = activity_entries(activity, **filters)
    return entries[0] if entries else None


class override_work:
    def __init__(self, work_type: str, handler):
        self.work_type = work_type
        self.handler = handler
        self.previous = None

    def __enter__(self):
        self.previous = bworker.WORK_HANDLERS.get(self.work_type)
        bworker.WORK_HANDLERS[self.work_type] = self.handler
        return self

    def __exit__(self, *exc_info):
        if self.previous is None:
            bworker.WORK_HANDLERS.pop(self.work_type, None)
        else:
            bworker.WORK_HANDLERS[self.work_type] = self.previous
        return False


class override_job:
    def __init__(self, kind: str, handler):
        self.kind = kind
        self.handler = handler
        self.previous = None

    def __enter__(self):
        self.previous = bworker.JOB_HANDLERS.get(self.kind)
        bworker.JOB_HANDLERS[self.kind] = self.handler
        return self

    def __exit__(self, *exc_info):
        if self.previous is None:
            bworker.JOB_HANDLERS.pop(self.kind, None)
        else:
            bworker.JOB_HANDLERS[self.kind] = self.previous
        return False


# ---------------------------------------------------------------------- tests
def test_claim_order() -> None:
    clean_db()
    runner, mem, clock = make_worker()
    runner.startup()
    order: list[str] = []

    def job_handler(job, stop):
        order.append("job")
        return {"status": "done", "message": "Job done."}

    def work_handler(ctx):
        order.append("work")
        return {"status": "success", "stage": "complete", "message": "Work done."}

    with override_job("test_job", job_handler), override_work("library_scan", work_handler):
        work_id = insert_work(work_key="global:test", work_type="library_scan",
                              next_run_at=clock.time() - 1)
        conn = connect()
        try:
            jobs.enqueue(conn, "test_job", {}, priority=10)
        finally:
            conn.close()
        result = runner.step()
        check("claim order: job first", order == ["job"], str(order))
        check("claim order: first kind", result["kind"] == "job", str(result))
        check("claim order: work untouched", get_work(work_id)["status"] == "pending")
        result = runner.step()
        check("claim order: work second", order == ["job", "work"], str(order))
        check("claim order: work completed", get_work(work_id)["status"] == "complete")
    finished = activity_entries(mem, event=activity_log.EVENT_FINISHED,
                                work_id=work_id)
    check("claim order: one work activity entry", len(finished) == 1,
          str(len(finished)))
    check("claim order: work target label",
          (finished[0].get("target") or {}).get("kind") == "global",
          str(finished[0].get("target")))
    check("claim order: only one job activity entry",
          len([e for e in mem.recent(50) if e.get("work_type") == "test_job"]) == 1)


def test_due_and_idle_entries() -> None:
    clean_db()
    runner, mem, clock = make_worker()
    runner.startup()
    calls: list[int] = []

    def handler(ctx):
        calls.append(ctx.work_id)
        return {"status": "success", "stage": "complete"}

    with override_work("library_scan", handler):
        future = insert_work(work_key="future", work_type="library_scan",
                             next_run_at=clock.time() + 3600)
        result = runner.step()
        check("due: future work not claimed", result["kind"] == "idle", str(result))
        check("due: future remains pending", get_work(future)["status"] == "pending")
        first = insert_work(work_key="due-1", work_type="library_scan")
        second = insert_work(work_key="due-2", work_type="library_scan")
        result = runner.step()
        check("due: due work claimed", result["kind"] == "work", str(result))
        check("due: due work continues immediately", result["wait"] == 0.0,
              str(result["wait"]))
        check("due: has_due true", result["has_due"] is True, str(result))
        result = runner.step()
        check("due: second due work claimed", result["kind"] == "work", str(result))
        check("due: both handlers ran", len(calls) == 2, str(calls))
        # The first idle (waiting on the future row) belongs to an earlier
        # idle period, so a fresh entry after doing work is expected; a second
        # idle iteration in the same period is not.
        no_work_before = len(activity_entries(mem, event=activity_log.EVENT_NO_WORK))
        result = runner.step()
        check("due: idle after work", result["kind"] == "idle", str(result))
        check("due: idle waits", result["wait"] == 30.0, str(result["wait"]))
        no_work_after = len(activity_entries(mem, event=activity_log.EVENT_NO_WORK))
        check("due: idle period logs one no_work entry",
              no_work_after - no_work_before == 1,
              f"{no_work_before} -> {no_work_after}")
        runner.step()
        check("due: no duplicate no_work entry in the same idle period",
              len(activity_entries(mem, event=activity_log.EVENT_NO_WORK))
              == no_work_after)
        check("due: future still not claimed", get_work(future)["status"] == "pending")
        check("due: due rows complete",
              get_work(first)["status"] == "complete"
              and get_work(second)["status"] == "complete")
    entry = activity_entries(mem, event=activity_log.EVENT_NO_WORK)[0]
    required = {"id", "at", "started_at", "event", "status", "level", "work_id",
                "work_type", "target", "stage", "message", "progress",
                "attempt", "next_run_at", "finished_at", "duration"}
    check("activity: entry has contract fields", required <= set(entry), str(set(entry)))


def test_pause_resume() -> None:
    clean_db()
    runner, mem, clock = make_worker()
    runner.startup()
    result = store.set_paused(connect(), True, now=clock.time())
    check("pause: first request changes", result["changed"] is True)
    result = store.set_paused(connect(), True, now=clock.time())
    check("pause: repeated request is idempotent", result["changed"] is False)
    result = runner.step()
    check("pause: loop reports paused", result["kind"] == "paused", str(result))
    check("pause: loop sleeps briefly", 0 < result["wait"] <= 5, str(result["wait"]))
    runner.step()
    paused_entries = activity_entries(mem, event=activity_log.EVENT_PAUSED)
    check("pause: exactly one paused entry", len(paused_entries) == 1,
          str(len(paused_entries)))

    calls: list[int] = []

    def handler(ctx):
        calls.append(ctx.work_id)
        return {"status": "success", "stage": "complete"}

    with override_work("library_scan", handler):
        work_id = insert_work(work_key="paused-work", work_type="library_scan")
        conn = connect()
        try:
            jobs.enqueue(conn, "test_job", {})
        finally:
            conn.close()
        runner.step()
        check("pause: no work claimed while paused", calls == [], str(calls))
        check("pause: work row untouched", get_work(work_id)["status"] == "pending")
        check("pause: queued job untouched",
              jobs.counts(connect())["queued"] == 1)

        # A new process/loop sees the persisted pause and writes no duplicate.
        restarted, _, _ = make_worker(activity=mem, clock=clock)
        restarted.startup()
        restarted.step()
        check("pause: persists across restart",
              len(activity_entries(mem, event=activity_log.EVENT_PAUSED)) == 1,
              str(len(activity_entries(mem, event=activity_log.EVENT_PAUSED))))
        check("pause: restart does not claim work", calls == [], str(calls))

        conn = connect()
        try:
            resumed = store.set_paused(conn, False, now=clock.time())
            check("resume: request changes", resumed["changed"] is True)
            check("resume: repeated request is idempotent",
                  store.set_paused(conn, False, now=clock.time())["changed"] is False)
            check("pause: manual acquisition refused while paused was true",
                  store.manual_allowed(conn) is True)
            # The queued job was only there to prove paused loops claim
            # nothing. Drop it so the next iteration exercises the work path.
            conn.execute("DELETE FROM worker_jobs")
            conn.commit()
        finally:
            conn.close()
        runner.step()
        resumed_entries = activity_entries(mem, event=activity_log.EVENT_RESUMED)
        check("resume: exactly one resumed entry", len(resumed_entries) == 1,
              str(len(resumed_entries)))
        # The resumed loop runs the due work/job in the same iteration.
        check("resume: work resumes", len(calls) == 1, str(calls))
        runner.step()
        check("resume: no duplicate resumed entry",
              len(activity_entries(mem, event=activity_log.EVENT_RESUMED)) == 1)

        store.set_paused(connect(), True, now=clock.time())
        runner.step()
        check("pause: second transition writes a second entry",
              len(activity_entries(mem, event=activity_log.EVENT_PAUSED)) == 2)
        store.set_paused(connect(), False, now=clock.time())
        runner.step()
        check("resume: second transition writes a second entry",
              len(activity_entries(mem, event=activity_log.EVENT_RESUMED)) == 2)

    # Cleanup must keep running while paused, even while the loop sleeps.
    clean_db()
    big = HugeActivityLog(cap=10000, trim_to=5000)
    runner2, _, clock2 = make_worker(activity=big)
    runner2.startup()
    for index in range(500):
        big.push(activity_log.make_entry(
            event="test", status="running", message=f"entry {index}",
            at=index + 1,
        ))
    check("cleanup: fake holds all 500 before trim", len(big) >= 500, str(len(big)))
    store.set_paused(connect(), True, now=clock2.time())
    clock2.advance(10)
    runner2.step()
    check("cleanup: paused loop trimmed the log", len(big) <= 101, str(len(big)))
    newest = big.recent(10)
    check("cleanup: newest entries survive", newest[0]["event"] == activity_log.EVENT_PAUSED,
          str(newest[0].get("event")))
    check("cleanup: one paused entry after trim",
          len(activity_entries(big, event=activity_log.EVENT_PAUSED)) == 1,
          str(len(activity_entries(big, event=activity_log.EVENT_PAUSED))))


def test_retry_and_reset() -> None:
    clean_db()
    runner, mem, clock = make_worker()
    runner.startup()
    attempts: list[int] = []

    def failing(ctx):
        attempts.append(ctx.work_id)
        raise RuntimeError("provider hiccup")

    with override_work("library_scan", failing):
        work_id = insert_work(work_key="retry-work", work_type="library_scan",
                              max_failures=2, infinite_retry=0,
                              retry_delay_seconds=10, backoff_multiplier=2.0)
        runner.step()
        row = get_work(work_id)
        check("retry: first failure increments", row["failure_count"] == 1,
              str(row["failure_count"]))
        check("retry: first failure schedules retry", row["status"] == "retry",
              str(row["status"]))
        check("retry: base delay applied",
              int(row["next_run_at"]) == clock.time() + 10,
              f"{row['next_run_at']} vs {clock.time() + 10}")
        retry_entry = latest_entry(mem, work_id=work_id)
        check("retry: activity status retry",
              retry_entry is not None and retry_entry["status"] == activity_log.STATUS_RETRY,
              str(retry_entry and retry_entry.get("status")))
        clock.advance(10)
        runner.step()
        row = get_work(work_id)
        check("retry: second failure increments", row["failure_count"] == 2,
              str(row["failure_count"]))
        check("retry: policy limit stops the unit",
              row["status"] == "failed_stopped", str(row["status"]))
        check("retry: stopped unit has no next run", row["next_run_at"] is None)
        stopped_entry = latest_entry(mem, work_id=work_id)
        check("retry: failed_stopped activity is failure",
              stopped_entry is not None and stopped_entry["status"] ==
              activity_log.STATUS_FAILURE, str(stopped_entry and stopped_entry.get("status")))
        result = runner.step()
        check("retry: failed_stopped is not claimed", result["kind"] == "idle",
              str(result))

        conn = connect()
        try:
            conn.execute("UPDATE bworker_work SET cursor = '{\"offset\": 4}', "
                         "best_score = 88 WHERE id = ?", (work_id,))
            conn.commit()
            reset = store.reset_work(conn, work_id=work_id, now=clock.time())
        finally:
            conn.close()
        row = get_work(work_id)
        check("reset: unit updated", reset["updated"] == 1, str(reset))
        check("reset: failure count cleared", row["failure_count"] == 0)
        check("reset: status pending", row["status"] == "pending")
        check("reset: stage pending", row["stage"] == "pending")
        check("reset: cursor cleared", row["cursor"] is None, str(row["cursor"]))
        check("reset: best candidate cleared", row["best_score"] is None)
        check("reset: next run is now", int(row["next_run_at"]) == clock.time())

        def succeeding(ctx):
            return {"status": "success", "stage": "complete", "message": "ok"}

        bworker.WORK_HANDLERS["library_scan"] = succeeding
        runner.step()
        check("reset: reset unit runs again", get_work(work_id)["status"] == "complete")

    # Infinite retry: policy/work flags keep it retrying with capped backoff.
    with override_work("movie_search", failing):
        infinite_id = insert_work(work_key="infinite", work_type="movie_search",
                                  entity_type="global", infinite_retry=1,
                                  max_failures=None,
                                  retry_delay_seconds=100,
                                  backoff_multiplier=2.0)
        runner.step()
        row = get_work(infinite_id)
        check("retry: infinite stays retry", row["status"] == "retry")
        first_next = int(row["next_run_at"])
        clock.advance(100)
        runner.step()
        row = get_work(infinite_id)
        check("retry: infinite second failure counts", row["failure_count"] == 2)
        check("retry: infinite backoff multiplies",
              int(row["next_run_at"]) == first_next + 200,
              f"{row['next_run_at']} vs {first_next + 200}")
        check("retry: infinite still retry", row["status"] == "retry")

    # A profile rejection is a normal no-result, not a failure.
    with override_work("library_scan", lambda ctx: {
            "status": "success", "stage": "no_candidates",
            "message": "No allowed release passed the profile.",
            "next_run_at": clock.time() + 3600}):
        rejection_id = insert_work(work_key="rejection",
                                   work_type="library_scan")
        runner.step()
        row = get_work(rejection_id)
        check("rejection: failure count stays zero", row["failure_count"] == 0,
              str(row["failure_count"]))
        check("rejection: unit is scheduled normally", row["status"] == "pending")
        check("rejection: stage no_candidates", row["stage"] == "no_candidates",
              str(row["stage"]))

    # Reset requested while a row is running is consumed at the loop boundary.
    running_id = insert_work(work_key="running-reset", work_type="library_scan",
                             status="running")
    conn = connect()
    try:
        deferred = store.reset_work(conn, work_id=running_id, now=clock.time())
        check("reset: running unit defers", deferred["reset_requested"] == 1,
              str(deferred))
        check("reset: running unit stays running",
              get_work(running_id)["status"] == "running")
        consumed = store.consume_resets(conn, now=clock.time())
    finally:
        conn.close()
    row = get_work(running_id)
    check("reset: running reset consumed at loop boundary", consumed == 1)
    check("reset: running unit becomes pending", row["status"] == "pending")
    check("reset: running unit cleared", row["failure_count"] == 0)


def test_idle_timing() -> None:
    clean_db()
    runner, mem, clock = make_worker()
    runner.startup()
    delays: list[float] = []

    def timed(ctx):
        delays.append(0)
        clock.advance(15 if len(delays) == 2 else 2)
        return {"status": "success", "stage": "complete"}

    with override_work("library_scan", timed):
        result = runner.step()
        check("timing: first idle waits 30s", result["wait"] == 30.0,
              str(result["wait"]))
        insert_work(work_key="quick", work_type="library_scan")
        result = runner.step()
        check("timing: quick unit then waits", result["wait"] == 30.0,
              str(result["wait"]))
        insert_work(work_key="slow", work_type="library_scan")
        result = runner.step()
        check("timing: slow unit skips the wait once", result["wait"] == 0.0,
              str(result["wait"]))
        result = runner.step()
        check("timing: next idle waits again", result["wait"] == 30.0,
              str(result["wait"]))
        insert_work(work_key="due-a", work_type="library_scan")
        insert_work(work_key="due-b", work_type="library_scan")
        result = runner.step()
        check("timing: due work is immediate", result["has_due"] is True
              and result["wait"] == 0.0, str(result))


def test_orphan_recovery_and_link() -> None:
    clean_db()
    runner, mem, clock = make_worker()
    runner.startup()
    movie_id = add_movie(title="Orphan Movie")
    download_id = add_download(movie_id=movie_id, status="queued")
    store.ensure_movie_work(connect(), movie_id, now=clock.time())
    conn = connect()
    try:
        # Pretend we crashed mid-unit: work and job are running, activity says
        # running, and the download row is queued but not linked.
        conn.execute("UPDATE bworker_work SET status = 'running', "
                     "current_download_id = NULL WHERE entity_id = ? AND "
                     "entity_type = 'movie'", (movie_id,))
        conn.commit()
        jobs.enqueue(conn, "task", {"name": "refresh_metadata"})
        conn.execute("UPDATE worker_jobs SET status = 'running'")
        conn.commit()
    finally:
        conn.close()
    mem.push(activity_log.make_entry(
        event=activity_log.EVENT_STARTED, status=activity_log.STATUS_RUNNING,
        work_id=1, work_type="movie_search", stage="searching_candidates",
        message="Searching \"Orphan Movie\"",
    ))
    restarted, _, _ = make_worker(activity=mem, clock=clock)
    restarted.startup()
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM bworker_work WHERE entity_id = ?",
                           (movie_id,)).fetchone()
        job_row = conn.execute("SELECT status FROM worker_jobs").fetchone()
    finally:
        conn.close()
    check("orphan: work requeued", row["status"] == "pending", str(row["status"]))
    check("orphan: active download linked",
          int(row["current_download_id"] or 0) == download_id,
          str(row["current_download_id"]))
    check("orphan: worker job requeued", job_row["status"] == "queued",
          str(job_row["status"]))
    interrupted = [entry for entry in mem.recent(50)
                   if entry.get("work_id") == 1
                   and entry.get("status") == activity_log.STATUS_INTERRUPTED]
    check("orphan: running activity marked interrupted", len(interrupted) == 1,
          str(len(interrupted)))

    # The default movie handler links the active download instead of searching
    # again or creating a second download row.
    clean_db()
    runner2, mem2, clock2 = make_worker()
    runner2.startup()
    movie2 = add_movie(title="Linked Movie")
    download2 = add_download(movie_id=movie2, status="queued")
    store.ensure_movie_work(connect(), movie2, now=clock2.time())
    result = runner2.step()
    check("link: work unit waits on active download", result["kind"] == "work",
          str(result))
    row = connect().execute(
        "SELECT * FROM bworker_work WHERE entity_id = ?", (movie2,)
    ).fetchone()
    check("link: work records the existing download",
          int(row["current_download_id"] or 0) == download2,
          str(row["current_download_id"]))
    check("link: stage downloading", row["stage"] == "downloading",
          str(row["stage"]))
    check("link: unit scheduled to poll", row["status"] == "pending", str(row["status"]))
    conn = connect()
    try:
        downloads = conn.execute("SELECT COUNT(*) AS n FROM downloads").fetchone()["n"]
    finally:
        conn.close()
    check("link: no duplicate download row", downloads == 1, str(downloads))
    entry = latest_entry(mem2, work_id=int(row["id"]))
    check("link: activity target names the movie",
          (entry.get("target") or {}).get("label") == "Linked Movie",
          str(entry and entry.get("target")))


def test_valkey_behaviour() -> None:
    memory = activity_log.MemoryActivityLog(cap=500, trim_to=100)
    for index in range(500):
        memory.push(activity_log.make_entry(
            event="test", status="running", message=f"entry {index}"))
    entries = memory.recent(500)
    check("valkey trim: cap 500 trims to 100", len(entries) == 100, str(len(entries)))
    check("valkey trim: keeps the newest entries", entries[0]["id"] == 500
          and entries[-1]["id"] == 401, f"{entries[0]['id']}..{entries[-1]['id']}")
    check("valkey trim: push assigned increasing ids",
          [entry["id"] for entry in entries] == list(range(500, 400, -1)))
    updated = memory.update(500, {"status": "finished", "message": "done"})
    check("valkey update: true for existing id", updated is True)
    check("valkey update: patch applied", memory.recent(1)[0]["status"] == "finished")
    check("valkey update: false for missing id", memory.update(9999, {}) is False)

    degraded = activity_log.ValkeyActivityLog(
        "redis://127.0.0.1:1/0", connect_timeout=0.05, socket_timeout=0.05,
        offline_retry=30.0,
    )
    status = degraded.status()
    check("valkey degraded: status reports unavailable",
          status["available"] is False and status["degraded"] is True,
          str(status))
    check("valkey degraded: error is recorded",
          bool(status.get("error")), str(status.get("error")))
    check("valkey degraded: url is masked", "***" not in (status.get("url") or "")
          or status.get("url", "").startswith("redis://127.0.0.1:1"),
          str(status.get("url")))
    check("valkey degraded: push returns None",
          degraded.push(activity_log.make_entry(event="x", status="running")) is None)
    check("valkey degraded: recent is empty", degraded.recent(10) == [])
    check("valkey degraded: update is false", degraded.update(1, {}) is False)
    check("valkey degraded: trim returns None", degraded.trim(500, 100) is None)
    check("valkey degraded: interrupt returns zero",
          degraded.mark_running_interrupted() == 0)

    # A work unit must still run and finish with a dead activity store.
    clean_db()
    runner, _, clock = make_worker(activity=degraded)
    runner.startup()

    def handler(ctx):
        ctx.set_stage("searching_candidates", "still working")
        return {"status": "success", "stage": "complete", "message": "done"}

    with override_work("library_scan", handler):
        work_id = insert_work(work_key="degraded-work",
                              work_type="library_scan")
        result = runner.step()
    check("valkey degraded: work still completes",
          compare_status(work_id, "complete"), str(get_work(work_id)["status"]))
    check("valkey degraded: loop did not fail", result["kind"] == "work", str(result))


def compare_status(work_id: int, status: str) -> bool:
    return get_work(work_id)["status"] == status


def test_ensure_helpers() -> None:
    clean_db()
    past_movie = add_movie(title="Past Movie", release_date="2020-01-01")
    future_movie = add_movie(title="Future Movie", release_date="2099-01-01")
    conn = connect()
    try:
        created = store.ensure_movie_work(conn, past_movie)
        again = store.ensure_movie_work(conn, past_movie)
        future_created = store.ensure_movie_work(conn, future_movie)
        rows = {row["work_key"]: row for row in conn.execute(
            "SELECT * FROM bworker_work WHERE entity_type = 'movie'")}
    finally:
        conn.close()
    check("ensure: movie work created", created is True and again is False)
    check("ensure: movie work key", store.movie_work_key(past_movie) in rows)
    check("ensure: released movie searches now",
          rows[store.movie_work_key(past_movie)]["stage"] == "search",
          str(rows[store.movie_work_key(past_movie)]["stage"]))
    check("ensure: future movie waits for availability",
          rows[store.movie_work_key(future_movie)]["stage"] == "waiting_availability",
          str(rows[store.movie_work_key(future_movie)]["stage"]))
    check("ensure: future movie scheduled after release",
          int(rows[store.movie_work_key(future_movie)]["next_run_at"]) > time.time())

    conn = connect()
    try:
        conn.execute("UPDATE movies SET monitored = 0 WHERE id = ?", (past_movie,))
        conn.commit()
        store.ensure_movie_work(conn, past_movie)
        disabled = store.get_work_by_key(conn, store.movie_work_key(past_movie))
        conn.execute("UPDATE movies SET monitored = 1 WHERE id = ?", (past_movie,))
        conn.commit()
        store.ensure_movie_work(conn, past_movie)
        reenabled = store.get_work_by_key(conn, store.movie_work_key(past_movie))
    finally:
        conn.close()
    check("ensure: unmonitored movie disabled",
          disabled["enabled"] is False and disabled["status"] == "disabled")
    check("ensure: remonitored movie re-enabled",
          reenabled["enabled"] is True and reenabled["status"] == "pending")

    series_id = add_series(title="Test Series")
    past_episode = add_episode(series_id, 1, 1, air_date="2020-01-01")
    future_episode = add_episode(series_id, 1, 2, air_date="2099-01-01")
    off_episode = add_episode(series_id, 1, 3, monitored=0)
    conn = connect()
    try:
        created_episodes = store.ensure_series_work(conn, series_id)
        rows = {row["entity_id"]: row for row in conn.execute(
            "SELECT * FROM bworker_work WHERE entity_type = 'episode'")}
    finally:
        conn.close()
    check("ensure: series creates episode rows", created_episodes == 3,
          str(created_episodes))
    check("ensure: aired episode searches",
          rows[past_episode]["stage"] == "search", str(rows[past_episode]["stage"]))
    check("ensure: future episode waits for air",
          rows[future_episode]["stage"] == "waiting_air")
    check("ensure: unmonitored episode disabled",
          rows[off_episode]["enabled"] == 0 and rows[off_episode]["status"] == "disabled")

    conn = connect()
    try:
        conn.execute("UPDATE series SET monitored = 0 WHERE id = ?", (series_id,))
        conn.commit()
        created_after = store.ensure_series_work(conn, series_id)
        disabled_count = conn.execute(
            "SELECT COUNT(*) AS n FROM bworker_work WHERE entity_type = 'episode' "
            "AND enabled = 0").fetchone()["n"]
    finally:
        conn.close()
    check("ensure: unmonitored series creates nothing", created_after == 0)
    check("ensure: unmonitored series disables all episodes", disabled_count == 3,
          str(disabled_count))

    conn = connect()
    try:
        removed = store.remove_entity_work(conn, "series", series_id)
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM bworker_work WHERE entity_type = 'episode'"
        ).fetchone()["n"]
        removed_movie = store.remove_entity_work(conn, "movie", past_movie)
    finally:
        conn.close()
    check("ensure: removing series removes episode work",
          removed >= 3 and remaining == 0, f"{removed}/{remaining}")
    check("ensure: removing movie removes its work", removed_movie == 1,
          str(removed_movie))


def test_api() -> None:
    from flask import Flask
    from iptv_charts.bworker_api import bp

    clean_db()
    fake = activity_log.MemoryActivityLog(cap=500, trim_to=100)
    activity_log.set_default_store(fake)
    app = Flask(__name__)
    app.register_blueprint(bp)
    client = app.test_client()
    try:
        response = client.get("/api/bworker/status")
        payload = response.get_json()
        check("api: status 200", response.status_code == 200, str(response.status_code))
        check("api: status ok", payload.get("ok") is True)
        check("api: status keys",
              {"alive", "paused", "status", "current", "activity", "queue"}
              <= set(payload), str(set(payload)))
        response = client.post("/api/bworker/pause")
        payload = response.get_json()
        check("api: pause 200", response.status_code == 200, str(response.status_code))
        check("api: pause sets paused", payload.get("paused") is True)
        check("api: pause first time changed", payload.get("changed") is True)
        response = client.post("/api/bworker/pause")
        check("api: pause idempotent", response.get_json().get("changed") is False)
        check("api: manual acquisition refused while paused",
              store.manual_allowed(connect()) is False)

        work_id = insert_work(work_key="api-work", work_type="movie_search",
                              entity_type="movie", entity_id=None,
                              stage="probing", status="running")
        conn = connect()
        try:
            store.heartbeat(conn, status="running", current_work_id=work_id,
                            current_work_label="API Movie")
        finally:
            conn.close()
        fake.push(activity_log.make_entry(
            event=activity_log.EVENT_FINISHED, status=activity_log.STATUS_SUCCESS,
            work_id=work_id, work_type="movie_search", stage="complete",
            message="first", target={"kind": "movie", "id": 1, "label": "API Movie"},
        ))
        fake.push(activity_log.make_entry(
            event=activity_log.EVENT_FINISHED, status=activity_log.STATUS_FAILURE,
            work_id=work_id, work_type="movie_search", stage="failed",
            message="second", target={"kind": "movie", "id": 1, "label": "API Movie"},
        ))
        response = client.get("/api/bworker/activity?limit=1")
        payload = response.get_json()
        check("api: activity 200", response.status_code == 200)
        check("api: activity ok", payload.get("ok") is True)
        check("api: activity reports paused", payload.get("paused") is True)
        check("api: activity has entries", len(payload.get("entries") or []) == 1)
        check("api: activity newest first",
              payload["entries"][0]["message"] == "second",
              str(payload["entries"][0] if payload.get("entries") else None))
        check("api: activity reports current work",
              (payload.get("current") or {}).get("work_id") == work_id,
              str(payload.get("current")))
        check("api: activity availability",
              payload.get("available") is True and payload.get("degraded") is False)
        check("api: activity no-store header",
              response.headers.get("Cache-Control") == "no-store",
              str(response.headers.get("Cache-Control")))
        response = client.get(f"/api/bworker/activity?since_id={payload['newest_id']}")
        check("api: since_id returns nothing newer",
              response.get_json().get("entries") == [], str(response.get_json()))

        response = client.post("/api/bworker/resume")
        check("api: resume 200", response.status_code == 200)
        check("api: resume clears paused", response.get_json().get("paused") is False)

        response = client.get("/api/bworker/policies")
        payload = response.get_json()
        policies = {row["work_type"]: row for row in payload.get("policies", [])}
        check("api: policies list", len(policies) == 9, str(len(policies)))
        check("api: activity cleanup always enabled",
              policies["activity_cleanup"]["always_enabled"] is True)

        response = client.patch("/api/bworker/policies", json={
            "work_type": "download",
            "changes": {"enabled": False, "max_failures": 5},
        })
        payload = response.get_json()
        download_policy = next(row for row in payload["policies"]
                               if row["work_type"] == "download")
        check("api: policy patch 200", response.status_code == 200)
        check("api: policy disabled", download_policy["enabled"] is False)
        check("api: policy max_failures updated", download_policy["max_failures"] == 5)
        check("api: policy updated_at present", download_policy["updated_at"] is not None)
        response = client.patch("/api/bworker/policies", json={
            "work_type": "download",
            "changes": {"enabled": True, "max_failures": 3},
        })
        check("api: policy restored", response.status_code == 200)
        response = client.patch("/api/bworker/policies", json={
            "work_type": "activity_cleanup", "changes": {"enabled": False}})
        check("api: activity cleanup cannot be disabled",
              response.status_code == 400 and response.get_json().get("ok") is False)
        response = client.patch("/api/bworker/policies", json={
            "work_type": "nope", "changes": {"enabled": True}})
        check("api: unknown policy rejected", response.status_code == 400)
        response = client.patch("/api/bworker/policies", json={
            "work_type": "download", "changes": {"bogus": 1}})
        check("api: unknown policy field rejected", response.status_code == 400)

        response = client.post("/api/bworker/reset", json={})
        check("api: empty reset rejected", response.status_code == 400)
        response = client.post("/api/bworker/reset", json={"work_id": 999999})
        check("api: unknown work reset is 404", response.status_code == 404)
        reset_id = insert_work(work_key="api-reset", work_type="library_scan",
                               cursor="{\"offset\": 2}", best_score=50)
        response = client.post("/api/bworker/reset", json={"work_id": reset_id})
        payload = response.get_json()
        check("api: reset work 200", response.status_code == 200)
        check("api: reset reports updated", payload.get("updated") == 1, str(payload))
        row = get_work(reset_id)
        check("api: reset clears cursor/best",
              row["cursor"] is None and row["best_score"] is None)
        response = client.post("/api/bworker/reset", json={"all": True})
        check("api: reset all 200", response.status_code == 200)
        check("api: reset all updated a row",
              response.get_json().get("updated", 0) >= 1)
        response = client.post("/api/bworker/reset", json={"work_key": "api-reset"})
        check("api: reset by work_key", response.status_code == 200, str(response.get_json()))

        # Degraded activity: still 200, page renders current from SQLite.
        class DeadStore:
            backend = "dead"
            def recent(self, limit=100): return []
            def status(self): return {"backend": "dead", "available": False,
                                      "degraded": True, "error": "connection refused",
                                      "cap": 500, "trim_to": 100}
        activity_log.set_default_store(DeadStore())
        response = client.get("/api/bworker/activity")
        payload = response.get_json()
        check("api: degraded activity still 200", response.status_code == 200)
        check("api: degraded activity flagged",
              payload.get("available") is False and payload.get("degraded") is True)
        check("api: degraded activity error surfaced", bool(payload.get("error")))
        check("api: degraded activity keeps current",
              (payload.get("current") or {}).get("work_id") == work_id,
              str(payload.get("current")))
    finally:
        activity_log.set_default_store(fake)
        # Restore the policy row for later suites in the same process.
        conn = connect()
        try:
            store.update_policy(conn, "download", {"enabled": True,
                                                   "max_failures": 3})
        finally:
            conn.close()
        activity_log.reset_default_store()


def test_policy_toggle_and_scheduler() -> None:
    clean_db()
    runner, mem, clock = make_worker()
    runner.startup()
    calls: list[int] = []

    def handler(ctx):
        calls.append(ctx.work_id)
        return {"status": "success", "stage": "complete"}

    with override_work("library_scan", handler):
        work_id = insert_work(work_key="policy-work", work_type="library_scan")
        conn = connect()
        try:
            store.update_policy(conn, "library_scan", {"enabled": False})
        finally:
            conn.close()
        result = runner.step()
        check("policy: disabled work type is not claimed",
              calls == [] and result["kind"] == "idle", str(result))
        conn = connect()
        try:
            store.update_policy(conn, "library_scan", {"enabled": True})
        finally:
            conn.close()
        runner.step()
        check("policy: re-enabled work is claimed", len(calls) == 1, str(calls))
        check("policy: work complete", get_work(work_id)["status"] == "complete")

    conn = connect()
    try:
        raised = False
        try:
            store.update_policy(conn, "activity_cleanup", {"enabled": False})
        except ValueError:
            raised = True
        check("policy: activity cleanup is always enabled", raised)
        check("policy: activity cleanup reports enabled",
              store.policy_enabled(conn, "activity_cleanup") is True)
    finally:
        conn.close()

    # The old scheduler search loop is retired, but maintenance tasks remain.
    clean_db()
    os.environ["IPTV_CHARTS_SCHEDULER"] = "1"
    try:
        conn = connect()
        try:
            scheduler._ensure_state(conn)
            now = int(time.time())
            conn.execute("UPDATE task_state SET next_run = ? WHERE name = "
                         "'refresh_metadata'", (now - 1,))
            conn.execute("UPDATE task_state SET next_run = ? WHERE name = "
                         "'refresh_catalog'", (now + 3600,))
            conn.execute("UPDATE task_state SET next_run = ? WHERE name = "
                         "'search_missing'", (now - 1,))
            conn.commit()
        finally:
            conn.close()
        queued = scheduler.enqueue_due()
        conn = connect()
        try:
            task_jobs = conn.execute(
                "SELECT payload FROM worker_jobs WHERE kind = 'task'"
            ).fetchall()
            payloads = [json.loads(row["payload"]) for row in task_jobs]
            names = [item.get("name") for item in payloads]
        finally:
            conn.close()
        check("scheduler: maintenance task queued", queued == 1, str(queued))
        check("scheduler: metadata refresh queued", names == ["refresh_metadata"],
              str(names))
        check("scheduler: no search_missing job", "search_missing" not in names)
        raised = False
        try:
            scheduler.trigger("search_missing")
        except ValueError:
            raised = True
        check("scheduler: search_missing trigger retired", raised)
        result = scheduler.run_task("search_missing")
        check("scheduler: stale search_missing job is a no-op",
              result["status"] == "done", str(result))
        check("scheduler: no new search job added",
              "search_missing" not in [
                  json.loads(row["payload"]).get("name")
                  for row in connect().execute(
                      "SELECT payload FROM worker_jobs WHERE kind = 'task'")
              ])
        conn = connect()
        try:
            store.update_policy(conn, "metadata_refresh", {"enabled": False})
            conn.execute("UPDATE task_state SET next_run = ? WHERE name = "
                         "'refresh_metadata'", (now - 1,))
            conn.commit()
        finally:
            conn.close()
        check("scheduler: policy disables metadata refresh",
              scheduler.enqueue_due() == 0)
        conn = connect()
        try:
            store.update_policy(conn, "metadata_refresh", {"enabled": True})
        finally:
            conn.close()
    finally:
        os.environ["IPTV_CHARTS_SCHEDULER"] = "0"


def test_status_and_compat() -> None:
    clean_db()
    fake = activity_log.MemoryActivityLog()
    runner, _, clock = make_worker(activity=fake)
    runner.startup()
    conn = connect()
    try:
        payload = bworker.status(conn, activity=fake)
    finally:
        conn.close()
    check("status: alive after startup", payload["alive"] is True, str(payload))
    check("status: not paused by default", payload["paused"] is False)
    check("status: activity available", payload["activity"]["available"] is True)
    check("status: queue reported",
          "worker_jobs" in payload["queue"] and "work" in payload["queue"])
    store.set_paused(connect(), True, now=clock.time())
    conn = connect()
    try:
        paused_payload = bworker.status(conn, activity=fake)
    finally:
        conn.close()
    check("status: paused is still alive", paused_payload["alive"] is True)
    check("status: paused reported", paused_payload["paused"] is True)
    check("status: paused status text", paused_payload["status"] == "paused")

    # A dead process: stale heartbeat and stopped status fail health.
    conn = connect()
    try:
        old = int(time.time()) - 120
        conn.execute("UPDATE bworker_state SET heartbeat_at = ?, status = 'stopped' "
                     "WHERE id = 1", (old,))
        conn.commit()
    finally:
        conn.close()
    try:
        Path(config.data_dir() / "worker.heartbeat.json").unlink()
    except OSError:
        pass
    conn = connect()
    try:
        dead_payload = bworker.status(conn, activity=fake)
    finally:
        conn.close()
    check("status: dead bworker is not alive", dead_payload["alive"] is False,
          str(dead_payload))
    check("status: dead heartbeat age", (dead_payload["heartbeat_age"] or 0) >= 120,
          str(dead_payload["heartbeat_age"]))

    import iptv_charts.worker as legacy
    check("compat: worker shim exports bworker main",
          legacy.main is bworker.main)
    check("compat: worker shim exports background helpers",
          legacy.start_background is bworker.start_background
          and legacy.stop_background is bworker.stop_background)
    check("compat: worker shim exports handlers",
          legacy.HANDLERS is bworker.HANDLERS)
    expected_jobs = {
        "sync", "download", "task", "search_movie", "search_series",
        "movie_releases", "episode_releases", "grab_movie", "grab_episode",
        "refresh_movie", "refresh_series", "probe_movie_candidate",
        "probe_movie_candidates", "scan_import", "scan_import_all",
    }
    check("compat: legacy job handlers registered",
          expected_jobs <= set(bworker.JOB_HANDLERS),
          str(expected_jobs - set(bworker.JOB_HANDLERS)))
    expected_work = {
        "movie_search", "series_search", "episode_search", "playlist_sync",
        "metadata_refresh", "download", "probe", "library_scan",
        "activity_cleanup",
    }
    check("compat: work handlers registered",
          expected_work <= set(bworker.WORK_HANDLERS),
          str(expected_work - set(bworker.WORK_HANDLERS)))
    check("compat: root bworker entry module exists",
          (ROOT / "bworker.py").exists() and (ROOT / "worker.py").exists())


def test_reset_all() -> None:
    clean_db()
    first = insert_work(work_key="all-1", work_type="library_scan",
                        cursor="x", best_score=1)
    second = insert_work(work_key="all-2", work_type="library_scan")
    conn = connect()
    try:
        conn.execute("UPDATE bworker_work SET failure_count = 4, status = 'failed_stopped'")
        conn.commit()
        result = store.reset_work(conn)
        rows = conn.execute("SELECT * FROM bworker_work").fetchall()
    finally:
        conn.close()
    check("reset all: both units updated", result["updated"] == 2, str(result))
    check("reset all: failure counts cleared",
          all(row["failure_count"] == 0 for row in rows))
    check("reset all: statuses pending",
          all(row["status"] == "pending" for row in rows))
    check("reset all: next runs now",
          all(int(row["next_run_at"]) >= int(time.time()) - 5 for row in rows))


def run_section(name: str, func) -> None:
    print(f"\n== {name} ==")
    try:
        func()
    except Exception:
        _failures.append((name, "exception"))
        traceback.print_exc()


def test_progress_watcher() -> None:
    # Regression: the watcher used to assign self._stop = Event(), shadowing
    # Thread._stop(). Python 3.12's Thread.join() calls that method internally,
    # so every completed job crashed the bworker loop instead of returning to
    # idle.
    activity = activity_log.MemoryActivityLog()
    watcher = bworker._JobProgressWatcher(
        connection_factory=db.connect,
        job_id=999_999,
        activity_id=0,
        activity=activity,
        interval=0.01,
        attempt=1,
        parent_stop=threading.Event(),
    )
    watcher.start()
    time.sleep(0.03)
    watcher.stop()
    try:
        watcher.join(timeout=2.0)
        joined = True
        error = ""
    except Exception as exc:
        joined = False
        error = f"{type(exc).__name__}: {exc}"
    check("progress watcher joins without shadowing Thread._stop", joined, error)
    check("progress watcher stopped after join", not watcher.is_alive())


def test_candidate_activity() -> None:
    conn = db.connect()
    try:
        cursor = conn.execute(
            "INSERT INTO movies (tmdb_id, title, added_at) VALUES (?, ?, ?)",
            (987001, "Candidate Activity Movie", int(time.time())),
        )
        movie_id = int(cursor.lastrowid)
        conn.commit()
        store.ensure_movie_work(conn, movie_id)
        work = store.get_work_by_key(conn, f"movie:{movie_id}:search")
        activity = activity_log.MemoryActivityLog()
        runner = bworker.Bworker(activity=activity, stop=threading.Event())
        ctx = bworker.WorkContext(
            runner=runner, conn=conn, work=work, activity_id=None,
            stop=runner.stop,
        )
        ctx.candidate_event({
            "event": "probe_started", "candidate_id": 7,
            "message": "Checking candidate “Fixture 1080p”",
        })
        entries = activity.recent(5)
        running = next((entry for entry in entries
                        if entry.get("status") == "running"), None)
        check("candidate activity shows a running probe", running is not None)
        check("candidate activity names the movie target",
              running and running.get("target", {}).get("label")
              == "Candidate Activity Movie",
              repr(running))
        ctx.candidate_event({
            "event": "probe_skipped", "status": "skipped",
            "candidate_id": 7,
            "message": "Skipped “Fixture 1080p”: resolution unknown",
        })
        entries = activity.recent(5)
        finished = next((entry for entry in entries
                         if entry.get("candidate_id") == 7
                         or entry.get("status") == "skipped"), None)
        # The in-place update keeps one entry, not a second finished row.
        check("candidate probe result updates the same entry",
              finished and finished.get("status") == "skipped",
              repr(entries))
        check("candidate skip reason is in the feed",
              finished and "resolution unknown" in str(finished.get("message")),
              repr(finished))
        check("candidate skip has a finished timestamp",
              finished and finished.get("finished_at") is not None,
              repr(finished))
    finally:
        conn.close()


def main() -> int:
    db.init_db()
    sections = [
        ("claim order", test_claim_order),
        ("progress watcher", test_progress_watcher),
        ("due and idle entries", test_due_and_idle_entries),
        ("pause and resume", test_pause_resume),
        ("retry and reset", test_retry_and_reset),
        ("idle timing", test_idle_timing),
        ("orphan recovery and download linking", test_orphan_recovery_and_link),
        ("Valkey trim and degraded mode", test_valkey_behaviour),
        ("work seeding helpers", test_ensure_helpers),
        ("bworker API", test_api),
        ("policy toggles and scheduler", test_policy_toggle_and_scheduler),
        ("status and compatibility", test_status_and_compat),
        ("reset all", test_reset_all),
        ("candidate activity", test_candidate_activity),
    ]
    for name, func in sections:
        run_section(name, func)
    total = _passed + len(_failures)
    print(f"\n{'=' * 60}")
    print(f"bworker tests: {_passed}/{total} checks passed")
    if _failures:
        for label, detail in _failures:
            print(f"  FAILED: {label}" + (f" ({detail})" if detail else ""))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
