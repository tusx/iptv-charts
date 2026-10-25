#!/usr/bin/env python3
"""UI tests for the 0.8.0 movie detail page (agent A09).

Covers the card structure, the server-rendered bworker stage card with its
Current/History tabs, the explicit stage labels, progress and score fields,
the last-15 event retention/order, the retained action controls, the manual
metadata dialog and the local-assets-only rule.

No network, no provider, no TMDB key. Fixture bworker work/events/download
rows are inserted directly into the temporary SQLite database.

Run with:  .venv/bin/python tests/movies_ui.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="iptv-movies-ui-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_TMP / "media")
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"
os.environ["IPTV_CHARTS_LOG_LEVEL"] = "CRITICAL"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from iptv_charts import db, manager_web  # noqa: E402
from iptv_charts.app import create_app  # noqa: E402

TEMPLATE = ROOT / "iptv_charts" / "templates" / "movie.html"
CSS = ROOT / "iptv_charts" / "static" / "css" / "movie_detail.css"
JS = ROOT / "iptv_charts" / "static" / "js" / "movie.js"

_REMOTE_ASSET = re.compile(
    r"(?:<link\b[^>]*\bhref|<script\b[^>]*\bsrc)\s*=\s*[\"']\s*(?:https?:)?//",
    re.IGNORECASE,
)
_REMOTE_CSS_URL = re.compile(r"(?:url\(|@import\s+)[\"']?\s*(?:https?:)?//", re.IGNORECASE)

_passed = 0
_failures: list[tuple[str, str]] = []


def check(label: str, condition, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        return
    _failures.append((label, detail))
    print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def section(name: str) -> None:
    print(f"\n{name}")


def normalise(html: str) -> str:
    """Readable page text: tags separate words, then whitespace collapses."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def make_app():
    return create_app()


APP = make_app()
CLIENT = APP.test_client()


def connect():
    return db.connect()


def profile_row(builtin_key: str = "1080p") -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM quality_profiles WHERE builtin_key = ?",
            (builtin_key,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def add_movie(title: str = "Stage Fixture Movie", **overrides) -> int:
    now = int(time.time())
    fields = {
        "tmdb_id": 990001,
        "title": title,
        "added_at": now,
        "monitored": 1,
        "release_date": "2020-01-01",
        "status": "Released",
        "quality_profile_id": profile_row()["id"],
        "overview": "A fixture movie for the detail page tests.",
    }
    fields.update(overrides)
    conn = connect()
    try:
        conn.execute("DELETE FROM movies WHERE tmdb_id = ?", (fields["tmdb_id"],))
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = conn.execute(
            f"INSERT INTO movies ({columns}) VALUES ({marks})",
            tuple(fields.values()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def clear_stage(movie_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM bworker_events WHERE entity_type = 'movie' AND entity_id = ?",
            (movie_id,),
        )
        conn.execute(
            "DELETE FROM bworker_work WHERE entity_type = 'movie' AND entity_id = ?",
            (movie_id,),
        )
        conn.execute("DELETE FROM downloads WHERE movie_id = ?", (movie_id,))
        conn.execute("DELETE FROM movie_alternates WHERE movie_id = ?", (movie_id,))
        conn.execute("DELETE FROM history WHERE movie_id = ?", (movie_id,))
        conn.execute(
            "UPDATE bworker_state SET paused = 0, current_work_id = NULL, "
            "current_work_label = NULL WHERE id = 1"
        )
        conn.commit()
    finally:
        conn.close()


def set_work(movie_id: int, stage: str, status: str, *,
             message: str | None = None, best_score: int | None = None,
             failure_count: int = 0, last_error: str | None = None,
             next_run_at: int | None = None) -> int:
    now = int(time.time())
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM bworker_work WHERE entity_type = 'movie' AND entity_id = ?",
            (movie_id,),
        )
        cursor = conn.execute(
            "INSERT INTO bworker_work (work_key, work_type, entity_type, "
            "entity_id, payload, enabled, stage, status, failure_count, "
            "next_run_at, best_score, last_error, started_at, updated_at, "
            "created_at) VALUES (?, 'movie_search', 'movie', ?, '{}', 1, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?)",
            (
                f"movie:{movie_id}:search", movie_id, stage, status,
                failure_count, next_run_at, best_score, last_error,
                now - 3600, now - 60, now,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def add_event(movie_id: int, stage: str, status: str, message: str, *,
              progress: str | None = None, created_at: int | None = None,
              work_id: int | None = None) -> int:
    now = int(created_at if created_at is not None else time.time())
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO bworker_events (work_id, work_type, entity_type, "
            "entity_id, stage, status, message, progress, started_at, "
            "finished_at, created_at) VALUES (?, 'movie_search', 'movie', ?, "
            "?, ?, ?, ?, ?, NULL, ?)",
            (work_id, movie_id, stage, status, message, progress,
             now - 30, now),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def add_download(movie_id: int, done: int = 50, total: int = 100) -> int:
    now = int(time.time())
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO downloads (kind, movie_id, provider_kind, "
            "provider_remote_id, title, release_name, status, bytes_done, "
            "bytes_total, requested_at, started_at) VALUES ('movie', ?, 'vod', "
            "'fixture-remote', 'Fixture release', 'Fixture.Release.1080p', "
            "'downloading', ?, ?, ?, ?)",
            (movie_id, done, total, now, now),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def render(movie_id: int) -> str:
    response = CLIENT.get(f"/movies/{movie_id}")
    check(f"GET /movies/{movie_id} succeeds", response.status_code == 200,
          f"status {response.status_code}")
    return response.get_data(as_text=True)


def history_panel(html: str) -> str:
    if 'data-stage-panel="history"' not in html:
        return ""
    return html.split('data-stage-panel="history"', 1)[1].split("</section>", 1)[0]


# --------------------------------------------------------------------- tests
def test_cards_and_controls(movie_id: int) -> None:
    section("card structure and retained action controls")
    clear_stage(movie_id)
    set_work(movie_id, "searching_candidates", "running",
             message="Searching the catalog", best_score=20)
    add_event(movie_id, "searching_candidates", "started",
              "Search started")
    page = render(movie_id)
    text = normalise(page)

    card_ids = (
        "movie-stage-card", "candidate-section", "movie-file-card",
        "movie-alternates-card", "movie-edit-card", "movie-history-card",
    )
    for card_id in card_ids:
        check(f"card #{card_id} is rendered", f'id="{card_id}"' in page)

    check("top card is labelled Current stage", "Current stage" in text)
    check("Current/History tabs are server-rendered",
          page.count("data-stage-tab=") == 2
          and "data-stage-tab=\"current\"" in page
          and "data-stage-tab=\"history\"" in page)
    check("current panel is visible and history panel starts hidden",
          'data-stage-panel="current"' in page
          and 'data-stage-panel="history"' in page
          and re.search(r'data-stage-panel="history"[^>]*\bhidden\b', page)
          is not None)

    controls = (
        'id="movie-search-now"', 'id="movie-manual-search"',
        'id="movie-refresh"', 'id="movie-delete"',
        'id="movie-manual-search-section"', 'id="movie-show-all"',
        'id="movie-show-all-section"', 'id="movie-refind"',
        'id="movie-reprobe-all"',
        'id="candidate-filters"', 'id="movie-edit-form"',
        'id="movie-monitored-edit"', 'id="movie-profile-edit"',
        'id="movie-availability-edit"', 'id="movie-priority-edit"',
        'id="movie-stage-refresh"', 'id="movie-all-candidates"',
    )
    for control in controls:
        check(f"control {control} is present", control in page)

    template = TEMPLATE.read_text(encoding="utf-8")
    for attribute in (
        "data-probe-source", "data-pin-source", "data-pin-mode",
        "data-cancel-download", "data-delete-alternate",
        "data-candidate-filter", 'id="candidate-clear"',
    ):
        check(f"template keeps {attribute}", attribute in template)

    check("manual metadata button and dialog are present",
          'id="movie-manual-metadata"' in page
          and 'id="movie-metadata-dialog"' in page
          and 'id="movie-metadata-query"' in page
          and 'id="movie-metadata-year"' in page
          and 'id="movie-metadata-search"' in page
          and 'id="movie-metadata-results"' in page)
    js = JS.read_text(encoding="utf-8")
    check("manual metadata JS calls A12 endpoints",
          "/api/import/metadata/movies/${movieId}/search" in js
          and "post(`/api/import/metadata/movies/${movieId}`" in js)
    check("manual metadata copy promises file/download preservation",
          "file, download state, quality profile" in page)
    check("manual search exposes the all-candidates browser",
          'id="movie-all-candidates"' in template
          and "/api/movies/${movieId}/candidates" in js
          and "probe_if_needed" in js)
    check("candidate actions are available on the page",
          'id="movie-refind"' in template
          and 'id="movie-reprobe-all"' in template
          and 'id="movie-remove-refind"' in template
          and "/api/movies/${movieId}/refind" in js
          and "/remove-and-refind" in js)
    check("manual search waits for a query",
          "Type at least two characters" in template
          and "query.length >= 2" in js
          and "fa-spinner" in js)
    check("candidate cards render the full ffprobe facts",
          "Audio codecs" in template and "Subtitle codecs" in template
          and "mediaFacts(release)" in js)


def test_server_rendered_stage_and_score(movie_id: int) -> None:
    section("server-rendered stage, score and target/max")
    clear_stage(movie_id)
    profile = profile_row()
    set_work(movie_id, "probing", "running",
             message="Probing candidate 2/25", best_score=27,
             next_run_at=int(time.time()) + 3600)
    add_event(movie_id, "probing", "progress", "Probing candidate 2/25",
              progress='{"done": 2, "total": 25}')
    page = render(movie_id)
    text = normalise(page)

    check("best score is rendered", "Best score 27" in text)
    check("target/max comes from the selected profile",
          f"Target / max {profile['target_score']} / {profile['max_score']}" in text,
          f"target={profile['target_score']} max={profile['max_score']}")
    check("profile name is rendered", profile["name"] in text)
    check("batch/probe progress is rendered", "2 / 25" in text)
    check("started and updated relative times are rendered",
          "Started " in text and "Updated " in text)
    check("next run hint is rendered", "in 1 hour" in text)
    check("stage message is server-rendered", "Probing candidate 2/25" in page)
    check("no blank stage placeholder is needed",
          'data-stage="probing"' in page)
    check("history panel carries the durable event on first paint",
          "Probing candidate 2/25" in history_panel(page))


def test_explicit_stage_labels(movie_id: int) -> None:
    section("explicit bworker stage labels")
    cases = (
        ("waiting_availability", "pending", None, "Waiting for availability"),
        ("search", "running", None, "Searching"),
        ("searching_candidates", "running", None, "Searching candidates"),
        ("probing", "running", None, "Probing"),
        ("verifying", "running", None, "Verifying"),
        ("renaming", "running", None, "Renaming"),
        ("moving", "running", None, "Moving"),
        ("awaiting_upgrade", "pending", None, "Awaiting upgrade"),
        ("complete", "complete", None, "Complete"),
        ("no_candidates", "pending", None, "No candidates"),
        ("skipped", "disabled", None, "Skipped"),
    )
    for stage, status, message, label in cases:
        clear_stage(movie_id)
        set_work(movie_id, stage, status, message=message)
        text = normalise(render(movie_id))
        check(f"stage {stage} shows {label!r}", label in text)

    clear_stage(movie_id)
    set_work(movie_id, "searching_candidates", "failed_stopped",
             message="Every source failed",
             failure_count=3, last_error="Provider unavailable")
    text = normalise(render(movie_id))
    check("failed-stopped work shows Failed", "Failed" in text)
    check("failed-stopped work shows the unit status", "Stopped after failures" in text)
    check("failed-stopped work shows the failure count", "3 failed attempts" in text)

    clear_stage(movie_id)
    set_work(movie_id, "failed", "retry", message="Attempt failed")
    text = normalise(render(movie_id))
    check("stage failed shows Failed", "Failed" in text)

    clear_stage(movie_id)
    set_work(movie_id, "searching_candidates", "pending")
    conn = connect()
    try:
        conn.execute("UPDATE bworker_state SET paused = 1 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()
    text = normalise(render(movie_id))
    check("paused bworker shows Paused", "Paused" in text)
    check("paused bworker explains the pause", "bworker is paused" in text.lower())
    conn = connect()
    try:
        conn.execute("UPDATE bworker_state SET paused = 0 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


def test_downloading_progress(movie_id: int) -> None:
    section("downloading with percent")
    clear_stage(movie_id)
    download_id = add_download(movie_id, done=512, total=1024)
    set_work(movie_id, "downloading", "running", message="Downloading release",
             best_score=20)
    conn = connect()
    try:
        conn.execute(
            "UPDATE bworker_work SET current_download_id = ? "
            "WHERE entity_type = 'movie' AND entity_id = ?",
            (download_id, movie_id),
        )
        conn.commit()
    finally:
        conn.close()
    page = render(movie_id)
    text = normalise(page)
    check("downloading stage label is rendered", "Downloading" in text)
    check("download percent is rendered", "50%" in text)
    check("download byte totals are rendered", "512 B / 1 KiB" in text)
    check("download percentage bar is server-rendered", "stage-progress-bar" in page)
    check("cancel-download control is present",
          f'data-cancel-download="{download_id}"' in page)


def test_history_cap_and_order(movie_id: int) -> None:
    section("durable history: newest first, capped at 15")
    clear_stage(movie_id)
    set_work(movie_id, "searching_candidates", "running")
    now = int(time.time())
    for index in range(18):
        add_event(
            movie_id, "searching_candidates",
            "succeeded" if index % 2 else "progress",
            f"fixture-event-{index:02d}",
            progress='{"done": %d, "total": 25}' % (index + 1),
            created_at=now - (18 - index) * 60,
        )
    page = render(movie_id)
    panel = history_panel(page)
    check("history panel keeps exactly 15 events",
          panel.count("data-history-event") == 15,
          f"count={panel.count('data-history-event')}")
    check("newest event is first",
          panel.index("fixture-event-17") < panel.index("fixture-event-16"))
    check("newest retained event precedes the oldest retained event",
          panel.index("fixture-event-17") < panel.index("fixture-event-03"))
    check("the three oldest events were dropped",
          all(f"fixture-event-{index:02d}" not in panel for index in (0, 1, 2)))
    check("history status badges are rendered",
          "Succeeded" in panel and "In progress" in panel)
    check("history relative times are rendered", "ago" in panel)

    context = manager_web.movie_stage_summary(connect(), movie_id)
    check("stage summary helper exposes a compact badge",
          context is not None and context["label"] == "Searching candidates",
          repr(context))


def test_derived_states_and_fallback(movie_id: int) -> None:
    section("derived states and direct-SQL fallback")
    clear_stage(movie_id)
    text = normalise(render(movie_id))
    check("no work row falls back to the initial stage", "Pending" in text)

    conn = connect()
    try:
        conn.execute(
            "UPDATE movies SET release_date = ? WHERE id = ?",
            ((time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))),
             movie_id),
        )
        conn.commit()
    finally:
        conn.close()
    text = normalise(render(movie_id))
    check("future release derives waiting availability",
          "Waiting for availability" in text)

    conn = connect()
    try:
        conn.execute(
            "UPDATE movies SET release_date = '2020-01-01', file_path = ? "
            "WHERE id = ?", ("/tmp/fixture-movie.mkv", movie_id),
        )
        conn.commit()
    finally:
        conn.close()
    text = normalise(render(movie_id))
    check("existing file derives Complete", "Complete" in text)
    conn = connect()
    try:
        conn.execute("UPDATE movies SET file_path = NULL WHERE id = ?", (movie_id,))
        conn.commit()
    finally:
        conn.close()

    # Simulate a checkout without A04's module: the direct SQL path must
    # still render a stage and its durable events.
    clear_stage(movie_id)
    set_work(movie_id, "renaming", "running", message="Renaming fixture")
    add_event(movie_id, "renaming", "started", "Rename started")
    original = manager_web.bworker_events
    manager_web.bworker_events = None
    try:
        page = render(movie_id)
        text = normalise(page)
        check("fallback path renders the stage", "Renaming" in text)
        check("fallback path renders durable events",
              "Rename started" in history_panel(page))
    finally:
        manager_web.bworker_events = original


def test_assets_and_responsive_css(movie_id: int) -> None:
    section("local assets, no CDN, responsive dark-theme CSS")
    page = render(movie_id)
    template = TEMPLATE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    check("movie detail CSS is local",
          "/static/css/movie_detail.css" in page and CSS.is_file())
    check("movie detail JS is local",
          "/static/js/movie.js" in page and JS.is_file())
    check("movie page template loads no remote asset",
          not _REMOTE_ASSET.search(template)
          and not _REMOTE_CSS_URL.search(template))
    check("rendered page loads no remote asset",
          not _REMOTE_ASSET.search(page)
          and not _REMOTE_CSS_URL.search(page))
    check("movie detail CSS has no remote URL",
          "http://" not in css and "https://" not in css)
    check("movie detail CSS uses the app dark-theme tokens",
          "var(--bg" in css and "var(--fg" in css and "var(--accent)" in css)
    check("movie detail CSS has a mobile breakpoint",
          "@media (max-width: 860px)" in css
          and "@media (max-width: 480px)" in css)
    check("movie detail CSS stacks the card grid on mobile",
          ".movie-detail-grid { grid-template-columns: 1fr; }" in css)
    js = JS.read_text(encoding="utf-8")
    check("stage tabs are interactive without a framework",
          "activateStageTab" in js and "data-stage-tab" in js)
    check("stage polling is conditional on the JSON endpoint",
          "window.MOVIE_STAGE_URL" in js and "stageSignature" in js)
    check("movie page still uses the vendored icon macro",
          "{% from \"_macros.html\" import" in template and "icon(" in template)


def test_candidate_browser_api(movie_id: int) -> None:
    section("candidate browser API")
    response = CLIENT.get(
        f"/api/movies/{movie_id}/candidates?q=stage&limit=25",
    )
    check("candidate browser returns JSON", response.status_code == 200,
          str(response.status_code))
    payload = response.get_json() or {}
    check("candidate browser reports ok", payload.get("ok"), True)
    check("candidate browser returns a list",
          isinstance(payload.get("candidates"), list), True)
    check("candidate browser echoes the query", payload.get("query"), "stage")
    check("candidate browser reports a total",
          isinstance(payload.get("total"), int), True)
    response = CLIENT.post(f"/api/movies/{movie_id}/refind")
    check("candidate refind endpoint returns JSON", response.status_code == 200,
          str(response.status_code))
    check("candidate refind reports ok", (response.get_json() or {}).get("ok"), True)
    response = CLIENT.post(f"/api/movies/{movie_id}/remove-and-refind")
    check("remove-and-refind endpoint returns JSON",
          response.status_code == 200, str(response.status_code))


def test_candidate_checks(movie_id: int) -> None:
    section("candidate check reasons")
    conn = connect()
    try:
        now = int(time.time())
        source_id = int(conn.execute(
            "INSERT INTO sources (name, kind, created_at) VALUES (?, 'm3u', ?)",
            ("Checks fixture", now),
        ).lastrowid)
        conn.execute(
            "DELETE FROM bworker_work WHERE entity_type = 'movie' AND entity_id = ?",
            (movie_id,),
        )
        conn.execute(
            "INSERT INTO bworker_work (work_key, work_type, entity_type, "
            "entity_id, payload, enabled, stage, status, failure_count, "
            "created_at, updated_at) VALUES (?, 'movie_search', 'movie', ?, "
            "'{}', 1, 'searching_candidates', 'running', 0, ?, ?)",
            (f"movie:{movie_id}:search", movie_id, now, now),
        )
        work_id = int(conn.execute(
            "SELECT id FROM bworker_work WHERE work_key = ?",
            (f"movie:{movie_id}:search",),
        ).fetchone()["id"])
        conn.execute(
            "INSERT INTO bworker_scan (work_id, epoch, query_fingerprint, "
            "status, cursor, exhausted, candidates_seen, candidates_matched, "
            "matched_count, candidates_probed, created_at, updated_at) "
            "VALUES (?, 'epoch', 'fingerprint', 'active', NULL, 0, 25, 25, "
            "25, 3, ?, ?)", (work_id, now, now),
        )
        scan_id = int(conn.execute(
            "SELECT id FROM bworker_scan WHERE work_id = ?", (work_id,),
        ).fetchone()["id"])
        conn.execute(
            "INSERT INTO bworker_candidates (scan_id, work_id, source_id, "
            "provider_kind, provider_remote_id, release_name, status, "
            "created_at, updated_at) VALUES (?, ?, ?, 'vod', 'fixture-1', ?, "
            "'pending', ?, ?)",
            (scan_id, work_id, source_id, "Deferred Fixture 1080p", now, now),
        )
        conn.commit()
    finally:
        conn.close()
    page = render(movie_id)
    check("candidate checks section renders",
          'id="movie-candidate-checks"' in page, True)
    check("candidate check explains a deferred candidate",
          "Deferred Fixture 1080p" in page
          and "probe batch limit" in page, True)
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM bworker_work WHERE entity_type = 'movie' AND entity_id = ?",
            (movie_id,),
        )
        conn.execute("DELETE FROM sources WHERE name = 'Checks fixture'")
        conn.commit()
    finally:
        conn.close()


def test_missing_movie() -> None:
    section("missing movie")
    response = CLIENT.get("/movies/999999")
    check("unknown movie returns 404", response.status_code == 404,
          f"status {response.status_code}")


def main() -> int:
    movie_id = add_movie()
    try:
        test_cards_and_controls(movie_id)
        test_server_rendered_stage_and_score(movie_id)
        test_explicit_stage_labels(movie_id)
        test_downloading_progress(movie_id)
        test_history_cap_and_order(movie_id)
        test_derived_states_and_fallback(movie_id)
        test_assets_and_responsive_css(movie_id)
        test_candidate_browser_api(movie_id)
        test_candidate_checks(movie_id)
        test_missing_movie()
    finally:
        clear_stage(movie_id)
        conn = connect()
        try:
            conn.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
            conn.commit()
        finally:
            conn.close()

    total = _passed + len(_failures)
    print(f"\n{_passed}/{total} checks passed")
    if _failures:
        print("\nFailures:")
        for label, detail in _failures:
            print(f"  - {label}" + (f"  [{detail}]" if detail else ""))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
