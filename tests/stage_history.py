#!/usr/bin/env python3
"""Standalone tests for item stages and durable bworker history (A04).

Run with:  .venv/bin/python tests/stage_history.py

Covers the event recorder and its newest-15 retention, transaction-safe
recording, all five entity types, the stage/status vocabulary, the JSON
blueprint shapes and 404s, the server-rendered query helpers, URL redaction
and concurrent writers/pruners. No network, no Valkey and no bworker loop.
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

_TMP = Path(tempfile.mkdtemp(prefix="iptv-stage-test-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_TMP / "media")
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"
os.environ["IPTV_CHARTS_VALKEY_URL"] = "redis://127.0.0.1:1/0"
os.environ["IPTV_CHARTS_LOG_LEVEL"] = "CRITICAL"
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)

sys.path.insert(0, str(ROOT))

from iptv_charts import bworker_events as events  # noqa: E402
from iptv_charts import db, stage_web  # noqa: E402

# Expected recording failures (closed connection, bad work id) are logged by
# the module; keep the suite output about checks only.
logging.disable(logging.CRITICAL)

_passed = 0
_failures: list[tuple[str, str]] = []
_tmdb_counter = [800_000]


def check(label: str, condition, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        return
    _failures.append((label, detail))
    print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def connect():
    return db.connect()


def clean_db() -> None:
    conn = connect()
    try:
        for table in (
            "worker_jobs", "bworker_events", "bworker_scan",
            "bworker_candidates", "bworker_work", "downloads",
            "movie_alternates", "series_episodes", "series", "movies",
            "items", "categories", "sources",
        ):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()
    finally:
        conn.close()


def _tmdb() -> int:
    _tmdb_counter[0] += 1
    return _tmdb_counter[0]


def add_movie(*, title="Stage Movie", monitored=1, release_date="2020-01-01",
              status="released", file_path=None) -> int:
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO movies (tmdb_id, title, added_at, monitored, "
            "release_date, status, file_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_tmdb(), title, int(time.time()), monitored, release_date,
             status, file_path),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def add_series(*, title="Stage Series", monitored=1) -> int:
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO series (tmdb_id, title, added_at, monitored) "
            "VALUES (?, ?, ?, ?)",
            (_tmdb(), title, int(time.time()), monitored),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def add_episode(series_id: int, season: int, episode: int, *,
                air_date="2020-01-01", monitored=1, title=None,
                file_path=None) -> int:
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO series_episodes "
            "(series_id, season, episode, title, air_date, monitored, "
            " file_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(series_id), int(season), int(episode), title, air_date,
             int(monitored), file_path),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def insert_work(*, work_key: str, work_type: str, entity_type: str,
                entity_id: int | None, stage="pending", status="pending",
                best_score=None, current_download_id=None,
                next_run_at=None) -> int:
    now = int(time.time())
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO bworker_work (work_key, work_type, entity_type, "
            "entity_id, payload, enabled, stage, status, best_score, "
            "current_download_id, next_run_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (work_key, work_type, entity_type, entity_id,
             json.dumps({"entity_id": entity_id}), stage, status, best_score,
             current_download_id, next_run_at, now, now),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def add_download(*, movie_id=None, series_id=None, episode_id=None,
                 status="downloading", bytes_done=50, bytes_total=100) -> int:
    kind = "movie" if movie_id is not None else "episode"
    provider_kind = "vod" if movie_id is not None else "series"
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO downloads (kind, movie_id, series_id, episode_id, "
            "source_name, provider_kind, provider_remote_id, title, mode, "
            "status, bytes_done, bytes_total, requested_at, score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'main', ?, ?, ?, ?, 20)",
            (kind, movie_id, series_id, episode_id,
             "Provider https://secret.example/api?token=SECRET", provider_kind,
             "provider-remote-SECRET", "Release.mkv", status, bytes_done,
             bytes_total, int(time.time())),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def event_count(conn, entity_type, entity_id) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) AS n FROM bworker_events "
        "WHERE entity_type = ? AND entity_id IS ?",
        (entity_type, entity_id),
    ).fetchone()["n"])


def make_client():
    from flask import Flask, g

    app = Flask(__name__)
    app.register_blueprint(stage_web.bp)

    @app.teardown_appcontext
    def _close(_exception):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    return app.test_client()


# --------------------------------------------------------------- vocabulary
def test_stage_vocabulary() -> None:
    allowed_badges = {"secondary", "info", "primary", "warning", "success",
                      "danger"}
    for stage in events.STAGES:
        meta = events.stage_meta(stage)
        check(f"vocab: {stage} has label", bool(meta["label"]), stage)
        check(f"vocab: {stage} badge", meta["badge"] in allowed_badges,
              meta["badge"])
        check(f"vocab: {stage} known", meta["known"] is True)
    check("vocab: canonical stage count", len(events.STAGES) == 16,
          str(len(events.STAGES)))
    check("vocab: downloading label",
          events.stage_label("downloading") == "Downloading")
    check("vocab: searching_candidates label",
          events.stage_label("searching_candidates") == "Searching candidates")
    unknown = events.stage_meta("custom_stage")
    check("vocab: unknown stage is prettified", unknown["label"] == "Custom Stage")
    check("vocab: unknown stage marked unknown", unknown["known"] is False)
    check("vocab: empty stage label", events.stage_label(None) == "Unknown")

    for status in events.EVENT_STATUSES:
        meta = events.event_status_meta(status)
        check(f"vocab: event {status} has label", bool(meta["label"]))
        check(f"vocab: event {status} badge", meta["badge"] in allowed_badges)
    check("vocab: event succeeded label",
          events.event_status_label("succeeded") == "Succeeded")
    check("vocab: work failed_stopped label",
          events.work_status_label("failed_stopped")
          == "Stopped after failures")
    check("vocab: work running badge",
          events.work_status_meta("running")["badge"] == "primary")
    check("vocab: combined status honours event first",
          events.status_label("failed") == "Failed")

    payload = events.stages_payload()
    check("vocab: payload stages", len(payload["stages"]) == len(events.STAGES))
    check("vocab: payload labels",
          payload["stage_labels"]["searching_candidates"]
          == "Searching candidates")
    check("vocab: payload event statuses",
          {row["value"] for row in payload["event_statuses"]}
          == set(events.EVENT_STATUSES))
    check("vocab: payload work statuses",
          {row["value"] for row in payload["work_statuses"]}
          == set(events.WORK_STATUSES))


# ----------------------------------------------------------------- recorder
def test_recorder_retention() -> None:
    clean_db()
    conn = connect()
    try:
        recorded = True
        for index in range(20):
            ok = events.record(
                conn, work_id=None, work_type="movie_search",
                entity_type="movie", entity_id=101, stage="downloading",
                status="progress", message=f"movie event {index}",
                progress={"done": index + 1, "total": 20}, now=1000 + index,
            )
            recorded = recorded and ok
        check("retention: every insert succeeded", recorded)
        rows = events.recent(conn, "movie", 101)
        check("retention: movie keeps 15", len(rows) == 15, str(len(rows)))
        check("retention: newest first",
              [row["message"] for row in rows]
              == [f"movie event {index}" for index in range(19, 4, -1)])
        check("retention: table holds 15", event_count(conn, "movie", 101) == 15)
        check("retention: progress decoded", rows[0]["progress"]
              == {"done": 20, "total": 20}, str(rows[0]["progress"]))
        check("retention: stage label", rows[0]["stage_label"] == "Downloading")
        check("retention: status label",
              rows[0]["status_label"] == "In progress")
        check("retention: limit newest slice",
              [row["message"] for row in events.recent(conn, "movie", 101,
                                                       limit=3)]
              == ["movie event 19", "movie event 18", "movie event 17"])
        check("retention: prune is idempotent",
              events.prune(conn, "movie", 101) == 0)

        for index in range(18):
            events.record(conn, work_type="movie_search", entity_type="movie",
                          entity_id=102, stage="probing",
                          status="progress", message=f"other {index}")
        check("retention: second entity keeps 15",
              event_count(conn, "movie", 102) == 15)
        check("retention: first entity unchanged",
              event_count(conn, "movie", 101) == 15)

        for index in range(16):
            events.record(conn, work_type="playlist_sync",
                          entity_type="source", entity_id=7, stage="search",
                          status="info", message=f"source {index}")
        check("retention: source keeps 15",
              event_count(conn, "source", 7) == 15)
        check("retention: source query",
              len(events.recent(conn, "source", 7)) == 15)

        for index in range(17):
            events.record(conn, work_type="playlist_sync",
                          entity_type="global", entity_id=None,
                          stage="search", status="progress",
                          message=f"global {index}")
        global_rows = events.recent(conn, "global")
        check("retention: global keeps 15", len(global_rows) == 15)
        check("retention: global entity_id is null",
              all(row["entity_id"] is None for row in global_rows))
        check("retention: global newest",
              global_rows[0]["message"] == "global 16")

        work_id = insert_work(
            work_key="movie:101:search", work_type="movie_search",
            entity_type="movie", entity_id=101)
        events.record(conn, work_id=work_id, work_type="movie_search",
                      entity_type="movie", entity_id=101,
                      stage="downloading", status="progress",
                      message="linked")
        linked = events.recent(conn, "movie", 101)[0]
        check("retention: work id retained", linked["work_id"] == work_id,
              str(linked["work_id"]))

        check("retention: invalid entity type rejected",
              events.record(conn, entity_type="bogus", entity_id=1,
                            stage="search", status="info") is False)
        check("retention: invalid status rejected",
              events.record(conn, entity_type="movie", entity_id=1,
                            stage="search", status="bogus") is False)
        check("retention: invalid entity id rejected",
              events.record(conn, entity_type="movie", entity_id="nope",
                            stage="search", status="info") is False)
        try:
            events.recent(conn, "bogus", 1)
            raised = False
        except ValueError:
            raised = True
        check("retention: recent rejects unknown entity type", raised)
    finally:
        conn.close()


def test_transaction_safety_and_failure() -> None:
    clean_db()
    movie_id = add_movie(title="Transaction Movie")

    # Standalone recording is durable across connections.
    conn = connect()
    try:
        check("tx: standalone insert",
              events.record(conn, work_type="movie_search",
                            entity_type="movie", entity_id=movie_id,
                            stage="searching_candidates", status="progress",
                            message="standalone"))
    finally:
        conn.close()
    conn = connect()
    try:
        check("tx: standalone durable",
              len(events.recent(conn, "movie", movie_id)) == 1)
    finally:
        conn.close()

    # Calling record inside a caller transaction must not commit that
    # transaction behind the caller's back.
    conn = connect()
    try:
        conn.execute("UPDATE movies SET title = ? WHERE id = ?",
                     ("Changed in transaction", movie_id))
        check("tx: caller transaction open", conn.in_transaction)
        ok = events.record(conn, work_type="movie_search",
                           entity_type="movie", entity_id=movie_id,
                           stage="downloading", status="progress",
                           message="joined transaction")
        check("tx: joined insert succeeds", ok)
        check("tx: caller transaction still open", conn.in_transaction)
        check("tx: event visible inside caller transaction",
              any(row["message"] == "joined transaction"
                  for row in events.recent(conn, "movie", movie_id)))
        conn.rollback()
        check("tx: caller rollback discards event",
              all(row["message"] != "joined transaction"
                  for row in events.recent(conn, "movie", movie_id)))
        title = conn.execute("SELECT title FROM movies WHERE id = ?",
                             (movie_id,)).fetchone()["title"]
        check("tx: caller rollback preserved", title == "Transaction Movie")
    finally:
        conn.close()

    # A closed connection is reported as False, never raised.
    broken = connect()
    broken.close()
    result = events.record(broken, work_type="movie_search",
                           entity_type="movie", entity_id=movie_id,
                           stage="downloading", status="progress",
                           message="closed")
    check("failure: closed connection returns False", result is False)

    def fake_bounded_work() -> str:
        closed = connect()
        closed.close()
        try:
            events.record(closed, work_type="movie_search",
                          entity_type="movie", entity_id=movie_id,
                          stage="downloading", status="failed",
                          message="never raises")
        except Exception:
            return "raised"
        return "finished"

    check("failure: recording cannot raise into work",
          fake_bounded_work() == "finished")

    # A work row deleted between claim and history write falls back to a NULL
    # reference rather than losing the event.
    conn = connect()
    try:
        ok = events.record(conn, work_id=999_999, work_type="movie_search",
                           entity_type="movie", entity_id=movie_id,
                           stage="searching_candidates", status="progress",
                           message="orphaned work link")
        check("failure: invalid work id still records", ok)
        row = events.recent(conn, "movie", movie_id)[0]
        check("failure: invalid work id becomes NULL",
              row["work_id"] is None, str(row["work_id"]))
    finally:
        conn.close()


def test_record_helpers() -> None:
    clean_db()
    movie_id = add_movie(title="Helper Movie")
    series_id = add_series(title="Helper Series")
    episode_id = add_episode(series_id, 1, 1)
    work_id = insert_work(
        work_key=f"movie:{movie_id}:search", work_type="movie_search",
        entity_type="movie", entity_id=movie_id)
    conn = connect()
    try:
        ok = events.record_for_work(
            conn,
            {"id": work_id, "work_id": work_id, "work_type": "movie_search",
             "entity_type": "movie", "entity_id": movie_id},
            "downloading", "progress", "Downloading", {"done": 1, "total": 2},
        )
        row = events.recent(conn, "movie", movie_id)[0]
        check("helpers: record_for_work", ok and row["work_id"] == work_id)
        check("helpers: progress stored", row["progress"] == {"done": 1,
                                                              "total": 2})

        check("helpers: movie stage",
              events.record_movie_stage(conn, movie_id,
                                        "searching_candidates", "progress",
                                        "Looking for a release"))
        check("helpers: movie stage visible",
              events.recent(conn, "movie", movie_id)[0]["stage"]
              == "searching_candidates")

        check("helpers: series stage",
              events.record_series_stage(conn, series_id, "search", "info",
                                         "Series scan"))
        check("helpers: series stage visible",
              events.recent(conn, "series", series_id)[0]["stage"] == "search")

        check("helpers: episode stage",
              events.record_episode_stage(conn, episode_id, "probing",
                                          "progress", "Probe"))
        check("helpers: episode stage visible",
              events.recent(conn, "episode", episode_id)[0]["stage"]
              == "probing")

        events.record_movie_stage(conn, movie_id, "custom_stage", "info",
                                  "Custom stage")
        check("helpers: unknown stage label fallback",
              events.recent(conn, "movie", movie_id)[0]["stage_label"]
              == "Custom Stage")

        events.record_movie_stage(
            conn, movie_id, "downloading", "failed",
            "Failed at https://user:pass@provider.example/file?token=SECRET")
        redacted = events.recent(conn, "movie", movie_id)[0]["message"]
        check("helpers: URL redacted",
              "[redacted-url]" in redacted and "http" not in redacted.lower(),
              redacted)
        check("helpers: URL token redacted", "SECRET" not in redacted)
        check("helpers: redaction helper",
              events.redact_text("see http://x/y") == "see [redacted-url]")

        # Text without a URL is untouched, so history stays readable.
        check("helpers: plain text untouched",
              events.redact_text("No candidates found") == "No candidates found")
    finally:
        conn.close()


# --------------------------------------------------------------- concurrency
def test_concurrent_writes() -> None:
    clean_db()
    entity_id = 777
    workers = 6
    per_worker = 5
    barrier = threading.Barrier(workers)
    errors: list[str] = []

    def worker(index: int) -> None:
        conn = connect()
        try:
            barrier.wait(timeout=15)
            for step in range(per_worker):
                ok = events.record(
                    conn, work_type="movie_search", entity_type="movie",
                    entity_id=entity_id, stage="downloading",
                    status="progress", message=f"w{index}-{step}",
                    progress={"done": step, "total": per_worker},
                )
                if not ok:
                    errors.append(f"worker {index} step {step} returned False")
        except Exception as exc:  # pragma: no cover - failure detail
            errors.append(f"worker {index}: {exc!r}")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    check("concurrency: no writer errors", not errors, str(errors[:3]))
    conn = connect()
    try:
        count = event_count(conn, "movie", entity_id)
        check("concurrency: pruned to 15", count == 15, str(count))
        rows = events.recent(conn, "movie", entity_id)
        check("concurrency: all newest 15 present", len(rows) == 15)
        check("concurrency: messages unique",
              len({row["message"] for row in rows}) == 15)
    finally:
        conn.close()

    # Pruners running against active writers must not remove current rows or
    # raise; the final state is still exactly the newest 15.
    barrier2 = threading.Barrier(6)
    errors2: list[str] = []

    def writer2(index: int) -> None:
        conn = connect()
        try:
            barrier2.wait(timeout=15)
            for step in range(5):
                if not events.record(
                        conn, work_type="movie_search", entity_type="movie",
                        entity_id=entity_id, stage="verifying",
                        status="progress", message=f"writer2-{index}-{step}"):
                    errors2.append(f"writer2 {index} returned False")
        except Exception as exc:  # pragma: no cover - failure detail
            errors2.append(f"writer2 {index}: {exc!r}")
        finally:
            conn.close()

    def pruner(index: int) -> None:
        conn = connect()
        try:
            barrier2.wait(timeout=15)
            for _ in range(20):
                events.prune(conn, "movie", entity_id)
        except Exception as exc:  # pragma: no cover - failure detail
            errors2.append(f"pruner {index}: {exc!r}")
        finally:
            conn.close()

    threads = [threading.Thread(target=writer2, args=(index,))
               for index in range(3)]
    threads += [threading.Thread(target=pruner, args=(index,))
                for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    check("concurrency: writers and pruners do not raise", not errors2,
          str(errors2[:3]))
    conn = connect()
    try:
        count = event_count(conn, "movie", entity_id)
        check("concurrency: final count is 15", count == 15, str(count))
    finally:
        conn.close()


# -------------------------------------------------------------- movie + API
def test_movie_context_and_api() -> None:
    clean_db()
    movie_id = add_movie(title="Staged Movie")
    download_id = add_download(movie_id=movie_id, bytes_done=50,
                               bytes_total=100)
    work_id = insert_work(
        work_key=f"movie:{movie_id}:search", work_type="movie_search",
        entity_type="movie", entity_id=movie_id, stage="downloading",
        status="running", best_score=20,
        current_download_id=download_id,
    )
    conn = connect()
    try:
        for index in range(17):
            message = f"progress {index}"
            if index == 16:
                message += " https://user:pass@provider.example/x?token=SECRET"
            events.record(
                conn, work_id=work_id, work_type="movie_search",
                entity_type="movie", entity_id=movie_id,
                stage="downloading" if index > 5 else "searching_candidates",
                status="progress", message=message,
                progress={"done": index, "total": 17}, now=2000 + index,
            )
        context = events.movie_stage_context(conn, movie_id)
        check("movie ctx: exists", context is not None)
        context = context or {}
        check("movie ctx: stage", context.get("stage") == "downloading")
        check("movie ctx: label", context.get("label") == "Downloading")
        check("movie ctx: status", context.get("status") == "running")
        check("movie ctx: status source", context.get("status_source") == "work")
        check("movie ctx: work id",
              (context.get("work") or {}).get("id") == work_id)
        check("movie ctx: best score", context.get("best_score") == 20)
        check("movie ctx: current download",
              (context.get("download") or {}).get("id") == download_id)
        check("movie ctx: download percent",
              (context.get("download") or {}).get("percent") == 50.0)
        check("movie ctx: events capped", len(context.get("events") or []) == 15)
        check("movie ctx: progress",
              context.get("progress") == {"done": 16, "total": 17},
              str(context.get("progress")))
        check("movie ctx: newest event first",
              (context.get("events") or [{}])[0].get("message", "")
              .startswith("progress 16"))
        serialised = json.dumps(context)
        check("movie ctx: no provider secret", "SECRET" not in serialised)
        check("movie ctx: no provider URL",
              "provider.example" not in serialised)
        check("movie ctx: no provider identity fields",
              "provider_remote_id" not in serialised
              and "source_name" not in serialised
              and "temp_path" not in serialised)
        check("movie ctx: missing entity", events.movie_stage_context(conn,
                                                                     999_999)
              is None)
    finally:
        conn.close()

    client = make_client()
    response = client.get(f"/api/movies/{movie_id}/stage")
    payload = response.get_json()
    check("movie api: 200", response.status_code == 200,
          str(response.status_code))
    check("movie api: ok", payload.get("ok") is True)
    required = {"ok", "stage", "label", "status", "message", "progress",
                "work", "events"}
    check("movie api: response shape", required <= set(payload),
          str(required - set(payload)))
    check("movie api: stage", payload.get("stage") == "downloading")
    check("movie api: label", payload.get("label") == "Downloading")
    check("movie api: work best score",
          (payload.get("work") or {}).get("best_score") == 20)
    check("movie api: download", (payload.get("download") or {}).get("id")
          == download_id)
    check("movie api: history 15", len(payload.get("events") or []) == 15)
    check("movie api: event labels",
          (payload.get("events") or [{}])[0].get("stage_label")
          == "Downloading")
    check("movie api: no-store header",
          response.headers.get("Cache-Control") == "no-store")
    body = response.get_data(as_text=True)
    check("movie api: no provider secret", "SECRET" not in body)
    check("movie api: no provider URL", "provider.example" not in body)

    missing = client.get("/api/movies/999999/stage")
    missing_payload = missing.get_json()
    check("movie api: 404", missing.status_code == 404,
          str(missing.status_code))
    check("movie api: 404 ok false", missing_payload.get("ok") is False)
    check("movie api: 404 error", bool(missing_payload.get("error")))


# ------------------------------------------------------ series/episode + API
def test_series_and_episode_context_and_api() -> None:
    clean_db()
    series_id = add_series(title="Staged Series")
    other_series_id = add_series(title="Other Series")
    episode1 = add_episode(series_id, 1, 1, air_date="2020-01-01",
                           file_path="/library/s01e01.mkv")
    episode2 = add_episode(series_id, 1, 2, air_date="2020-01-02")
    episode3 = add_episode(series_id, 2, 1, air_date="2999-01-01")
    episode4 = add_episode(series_id, 2, 2, air_date="2020-01-03",
                           monitored=0)
    download_id = add_download(series_id=series_id, episode_id=episode2,
                               bytes_done=25, bytes_total=100)
    work1 = insert_work(
        work_key=f"episode:{episode1}:search", work_type="episode_search",
        entity_type="episode", entity_id=episode1, stage="complete",
        status="complete")
    work2 = insert_work(
        work_key=f"episode:{episode2}:search", work_type="episode_search",
        entity_type="episode", entity_id=episode2, stage="downloading",
        status="running", best_score=20, current_download_id=download_id)
    work3 = insert_work(
        work_key=f"episode:{episode3}:search", work_type="episode_search",
        entity_type="episode", entity_id=episode3, stage="waiting_air",
        status="pending")
    insert_work(
        work_key=f"episode:{episode4}:search", work_type="episode_search",
        entity_type="episode", entity_id=episode4, stage="skipped",
        status="disabled")
    series_work = insert_work(
        work_key=f"series:{series_id}:search", work_type="series_search",
        entity_type="series", entity_id=series_id, stage="search",
        status="complete")

    conn = connect()
    try:
        events.record_episode_stage(conn, episode1, "complete", "succeeded",
                                    "Episode imported", work_id=work1)
        events.record_episode_stage(conn, episode3, "waiting_air", "info",
                                    "Airs later", work_id=work3)
        for index in range(20):
            events.record_episode_stage(
                conn, episode2, "downloading", "progress",
                f"episode2 progress {index}",
                {"done": index, "total": 20}, work_id=work2)
        events.record_series_stage(conn, series_id, "search", "succeeded",
                                   "Series search finished",
                                   work_id=series_work)

        context = events.series_stage_context(conn, series_id)
        check("series ctx: exists", context is not None)
        context = context or {}
        check("series ctx: aggregate stage",
              context.get("stage") == "downloading")
        check("series ctx: aggregate status", context.get("status") == "running")
        check("series ctx: status source",
              context.get("status_source") == "work")
        check("series ctx: progress counts",
              context.get("progress") == {"done": 1, "total": 3},
              str(context.get("progress")))
        counts = context.get("counts") or {}
        check("series ctx: counts episodes", counts.get("episodes") == 4)
        check("series ctx: counts monitored",
              counts.get("monitored_episodes") == 3)
        check("series ctx: counts downloaded",
              counts.get("downloaded_episodes") == 1)
        check("series ctx: work counts",
              counts.get("works", {}).get("running") == 1)
        check("series ctx: representative work",
              (context.get("work") or {}).get("id") == work2)
        check("series ctx: representative download",
              (context.get("download") or {}).get("id") == download_id)
        check("series ctx: series-level works",
              len(context.get("works") or []) == 1)
        check("series ctx: merged events capped",
              len(context.get("events") or []) == 15)
        check("series ctx: merged newest is series-level",
              (context.get("events") or [{}])[0].get("entity_type") == "series")
        episode_events = [row for row in (context.get("events") or [])
                          if row.get("entity_type") == "episode"]
        check("series ctx: episode events annotated",
              bool(episode_events)
              and episode_events[0].get("season") == 1
              and episode_events[0].get("episode") == 2,
              str(episode_events[0] if episode_events else None))

        episode_rows = {row["id"]: row for row in context.get("episodes") or []}
        check("series ctx: all episodes present", len(episode_rows) == 4)
        check("series ctx: downloaded episode stage",
              episode_rows[episode1]["stage"] == "complete")
        check("series ctx: downloaded flag",
              episode_rows[episode1]["downloaded"] is True)
        check("series ctx: running episode stage",
              episode_rows[episode2]["stage"] == "downloading"
              and episode_rows[episode2]["status"] == "running")
        check("series ctx: waiting episode stage",
              episode_rows[episode3]["stage"] == "waiting_air"
              and episode_rows[episode3]["status"] == "pending")
        check("series ctx: unmonitored episode skipped",
              episode_rows[episode4]["stage"] == "skipped"
              and episode_rows[episode4]["status"] == "disabled")
        check("series ctx: episode last event",
              (episode_rows[episode2].get("last_event") or {}).get("stage")
              == "downloading")
        check("series ctx: episode progress",
              episode_rows[episode2].get("progress")
              == {"done": 19, "total": 20},
              str(episode_rows[episode2].get("progress")))
        check("series ctx: missing entity",
              events.series_stage_context(conn, 999_999) is None)

        episode_context = events.episode_stage_context(conn, series_id,
                                                       episode2)
        check("episode ctx: exists", episode_context is not None)
        episode_context = episode_context or {}
        check("episode ctx: stage", episode_context.get("stage")
              == "downloading")
        check("episode ctx: entity label",
              (episode_context.get("entity") or {}).get("label")
              == "Staged Series S01E02")
        check("episode ctx: work", (episode_context.get("work") or {}).get("id")
              == work2)
        check("episode ctx: history capped",
              len(episode_context.get("events") or []) == 15)
        check("episode ctx: wrong series rejected",
              events.episode_stage_context(conn, other_series_id, episode2)
              is None)
        check("episode ctx: missing episode rejected",
              events.episode_stage_context(conn, series_id, 999_999) is None)
    finally:
        conn.close()

    client = make_client()
    response = client.get(f"/api/series/{series_id}/stage")
    payload = response.get_json()
    check("series api: 200", response.status_code == 200,
          str(response.status_code))
    check("series api: ok", payload.get("ok") is True)
    required = {"ok", "stage", "label", "status", "message", "progress",
                "work", "events"}
    check("series api: response shape", required <= set(payload),
          str(required - set(payload)))
    check("series api: aggregate stage", payload.get("stage") == "downloading")
    check("series api: episodes list", len(payload.get("episodes") or []) == 4)
    check("series api: counts", (payload.get("counts") or {})
          .get("monitored_episodes") == 3)
    check("series api: merged history",
          len(payload.get("events") or []) == 15)
    body = response.get_data(as_text=True)
    check("series api: no provider secret", "SECRET" not in body)
    check("series api: no-store",
          response.headers.get("Cache-Control") == "no-store")

    missing = client.get("/api/series/999999/stage")
    check("series api: 404", missing.status_code == 404,
          str(missing.status_code))
    check("series api: 404 ok false", missing.get_json().get("ok") is False)

    response = client.get(f"/api/series/{series_id}/episodes/{episode2}/stage")
    payload = response.get_json()
    check("episode api: 200", response.status_code == 200,
          str(response.status_code))
    check("episode api: ok", payload.get("ok") is True)
    check("episode api: stage", payload.get("stage") == "downloading")
    check("episode api: entity label",
          (payload.get("entity") or {}).get("label")
          == "Staged Series S01E02")

    wrong_series = client.get(
        f"/api/series/{other_series_id}/episodes/{episode2}/stage")
    check("episode api: wrong series 404", wrong_series.status_code == 404,
          str(wrong_series.status_code))
    missing_episode = client.get(
        f"/api/series/{series_id}/episodes/999999/stage")
    check("episode api: missing 404", missing_episode.status_code == 404,
          str(missing_episode.status_code))

    response = client.get("/api/stages")
    payload = response.get_json()
    check("stages api: 200", response.status_code == 200)
    check("stages api: ok", payload.get("ok") is True)
    check("stages api: downloading label",
          "downloading" in {row["value"] for row in payload.get("stages") or []})
    check("stages api: event statuses",
          "succeeded" in {row["value"]
                          for row in payload.get("event_statuses") or []})
    check("stages api: work statuses",
          "running" in {row["value"]
                        for row in payload.get("work_statuses") or []})
    check("stages api: label map",
          (payload.get("stage_labels") or {}).get("searching_candidates")
          == "Searching candidates")


# ------------------------------------------------------------ query coverage
def test_all_entity_queries() -> None:
    clean_db()
    movie_id = add_movie(title="Query Movie")
    series_id = add_series(title="Query Series")
    episode_id = add_episode(series_id, 1, 1)
    conn = connect()
    try:
        events.record_movie_stage(conn, movie_id, "search", "info", "movie")
        events.record_series_stage(conn, series_id, "search", "info", "series")
        events.record_episode_stage(conn, episode_id, "probing", "progress",
                                    "episode")
        events.record(conn, work_type="playlist_sync", entity_type="source",
                      entity_id=42, stage="search", status="info",
                      message="source")
        events.record(conn, work_type="playlist_sync", entity_type="global",
                      entity_id=None, stage="search", status="info",
                      message="global")
        for entity_type, entity_id, marker in (
            ("movie", movie_id, "movie"),
            ("series", series_id, "series"),
            ("episode", episode_id, "episode"),
            ("source", 42, "source"),
            ("global", None, "global"),
        ):
            rows = events.recent(conn, entity_type, entity_id)
            check(f"queries: {entity_type} history", len(rows) == 1,
                  str(len(rows)))
            check(f"queries: {entity_type} message",
                  rows[0]["message"] == marker)
            check(f"queries: {entity_type} labels",
                  bool(rows[0]["stage_label"]) and bool(rows[0]["status_label"]))
    finally:
        conn.close()


# ------------------------------------------------------------ page helpers
def test_page_helpers_and_derivation() -> None:
    clean_db()
    conn = connect()
    try:
        check("helpers: missing movie", events.movie_stage_context(
            conn, 123_456) is None)
        check("helpers: missing series", events.series_stage_context(
            conn, 123_456) is None)
        check("helpers: missing episode", events.episode_stage_context(
            conn, 123_456, 654_321) is None)

        future_movie = add_movie(title="Future Movie", release_date="2999-01-01")
        context = events.movie_stage_context(conn, future_movie) or {}
        check("helpers: future movie waits",
              context.get("stage") == "waiting_availability",
              str(context.get("stage")))
        check("helpers: future movie has no work",
              context.get("work") is None and context.get("events") == [])
        check("helpers: future movie status",
              context.get("status") == "pending")

        downloaded_movie = add_movie(title="Downloaded Movie",
                                     file_path="/library/movie.mkv")
        context = events.movie_stage_context(conn, downloaded_movie) or {}
        check("helpers: downloaded movie complete",
              context.get("stage") == "complete")
        check("helpers: downloaded movie status",
              context.get("status") == "complete")

        series_id = add_series(title="Derived Series")
        add_episode(series_id, 1, 1, air_date="2020-01-01")
        context = events.series_stage_context(conn, series_id) or {}
        check("helpers: derived series pending",
              context.get("stage") == "pending",
              str(context.get("stage")))
        check("helpers: derived series progress",
              context.get("progress") == {"done": 0, "total": 1})

        unmonitored = add_series(title="Unmonitored Series", monitored=0)
        add_episode(unmonitored, 1, 1)
        context = events.series_stage_context(conn, unmonitored) or {}
        check("helpers: unmonitored series skipped",
              context.get("stage") == "skipped")
        check("helpers: unmonitored series disabled",
              context.get("status") == "disabled")
    finally:
        conn.close()


# ------------------------------------------------------------------- runner
def run_section(name: str, func) -> None:
    print(f"\n== {name} ==")
    try:
        func()
    except Exception:
        _failures.append((name, "exception"))
        traceback.print_exc()


def main() -> int:
    db.init_db()
    sections = [
        ("stage vocabulary", test_stage_vocabulary),
        ("recorder retention", test_recorder_retention),
        ("transaction safety and failures",
         test_transaction_safety_and_failure),
        ("record helpers and redaction", test_record_helpers),
        ("concurrent writers and pruners", test_concurrent_writes),
        ("movie context and API", test_movie_context_and_api),
        ("series and episode context and API",
         test_series_and_episode_context_and_api),
        ("all entity queries", test_all_entity_queries),
        ("page helpers and derivation", test_page_helpers_and_derivation),
    ]
    for name, func in sections:
        run_section(name, func)
    total = _passed + len(_failures)
    print(f"\n{'=' * 60}")
    print(f"stage/history tests: {_passed}/{total} checks passed")
    if _failures:
        for label, detail in _failures:
            print(f"  FAILED: {label}" + (f" ({detail})" if detail else ""))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
