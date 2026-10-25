#!/usr/bin/env python3
"""Activity page UI tests: bworker card, live log, refresh control and wiring.

Run with:  .venv/bin/python tests/activity_ui.py

Dependency-free and offline. The real bworker_api blueprint is registered in a
small Flask test app (which is exactly the integration app.py performs); the
activity store is the in-memory implementation, so no Valkey is needed. Static
JavaScript is parsed with ``node --check`` when Node is available.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_BASE = Path(tempfile.mkdtemp(prefix="iptv-activity-ui-test-"))

_RESULT: list[tuple[bool, str, str]] = []


def _set_dirs(root: Path) -> None:
    os.environ["IPTV_CHARTS_DATA_DIR"] = str(root / "data")
    os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(root / "config")
    os.environ["IPTV_CHARTS_CACHE_DIR"] = str(root / "cache")
    os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(root / "media")
    os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
    os.environ.pop("IPTV_CHARTS_MOVIE_DIR", None)
    os.environ.pop("IPTV_CHARTS_SERIES_DIR", None)
    os.environ["IPTV_CHARTS_SCHEDULER"] = "0"


_set_dirs(_BASE)

from flask import Flask, g, jsonify, render_template  # noqa: E402

from iptv_charts import activity_log, bworker_api, config, db  # noqa: E402


def check(label: str, got, want) -> None:
    ok = got == want
    _RESULT.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def check_false(label: str, got) -> None:
    check(label, bool(got), False)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


REFRESH_VALUES = ["5", "10", "15", "20", "25", "30", "60",
                  "120", "180", "300", "600", "900", "1200", "1800", "3600"]


class DegradedActivityLog:
    """Mimics a Valkey outage: reads and status both fail, API must still 200."""

    backend = "unavailable"

    def recent(self, limit: int = 100) -> list[dict]:
        raise RuntimeError("valkey is down")

    def status(self) -> dict:
        raise RuntimeError("valkey is down")


def make_app() -> Flask:
    app = Flask(
        "activity-ui-test",
        template_folder=str(ROOT / "iptv_charts" / "templates"),
        static_folder=str(ROOT / "iptv_charts" / "static"),
    )
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(bworker_api.bp)

    def stub():
        return "stub"

    def page():
        return render_template("activity.html")

    def activity_feed():
        # The page's queue/history/tasks poll. The real blueprint is exercised
        # separately by its own suite; here only the no-store contract matters.
        response = jsonify({
            "ok": True, "active": [], "recent": [], "history": [],
            "tasks": [], "task_running": False,
        })
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_url_rule("/", endpoint="home", view_func=stub)
    app.add_url_rule("/activity", endpoint="manager.activity", view_func=page)
    app.add_url_rule("/api/activity", endpoint="manager.activity_feed",
                     view_func=activity_feed)
    app.add_url_rule("/storage", endpoint="storage.storage_page", view_func=stub)

    @app.context_processor
    def inject_globals():
        return {"app_name": config.APP_NAME, "app_version": "0.8.0-test"}

    @app.teardown_appcontext
    def close_connection(exception):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    return app


# --------------------------------------------------------------- page markup
def test_page_markup(client) -> None:
    section("activity page renders the bworker card and live log")

    response = client.get("/activity")
    check("page status", response.status_code, 200)
    html = response.get_data(as_text=True)

    check_true("bworker card", 'id="bworker-card"' in html)
    check_true("bworker status pill", 'id="bworker-state"' in html)
    check_true("heartbeat", 'id="bworker-heartbeat"' in html)
    check_true("pause button", 'id="bworker-pause"' in html)
    check_true("resume toggles same button", 'id="bworker-pause-label"' in html)
    check_true("reset button label", "Reset all failed attempts" in html)
    check_true("reset button target", 'id="bworker-reset"' in html)
    check_true("download folder link", 'id="download-folder-link"' in html)
    check_true("download folder route", 'href="/storage"' in html)
    check_true("download folder label", "Download folder" in html)

    for node in (
        "bworker-current-label", "bworker-current-stage",
        "bworker-current-started", "bworker-current-status",
        "bworker-progress", "bworker-progress-fill", "bworker-progress-label",
        "bw-count-work-pending", "bw-count-work-running",
        "bw-count-work-retry", "bw-count-work-stopped",
        "bw-count-work-complete", "bw-count-jobs-queued",
        "bw-count-jobs-running",
    ):
        check_true(f"current/queue node {node}", f'id="{node}"' in html)

    check_true("live log heading", "Live activity" in html)
    check_true("newest 100 copy", "newest 100" in html)
    check_true("log container", 'id="activity-log"' in html)
    check_true("log is a polite live region", 'aria-live="polite"' in html)
    check_true("log list", 'id="activity-log-list"' in html)
    check_true("degraded banner", 'id="activity-degraded"' in html)
    check_true("degraded banner starts hidden", 'id="activity-degraded" hidden' in html
               or 'hidden' in html.split('id="activity-degraded"', 1)[1][:80])

    check_true("refresh interval select", 'id="activity-refresh-interval"' in html)
    select = re.search(
        r'<select[^>]*id="activity-refresh-interval".*?</select>', html, re.S)
    check_true("refresh select present", select is not None)
    values = re.findall(r'<option value="(\d+)"', select.group(0)) if select else []
    check("refresh option values", values, REFRESH_VALUES)
    check_true("5s is the default choice",
               bool(re.search(r'<option value="5"\s+selected', html)))
    check_true("minutes are labelled", "2 min" in html and "60 min" in html)
    check_true("green refresh toggle", "activity-refresh-btn is-on" in html)
    check_true("refresh toggle starts on", 'aria-pressed="true"' in html)
    check_true("refresh countdown label", "Refresh on (5s)" in html)
    check_true("manual pause control", 'id="activity-refresh-pause"' in html)
    check_true("refresh hint", 'id="activity-refresh-hint"' in html)

    check_true("queue section", "<h2>Queue</h2>" in html)
    check_true("history section", "<h2>History</h2>" in html)
    check_true("tasks section", "<h2>Tasks</h2>" in html)
    check_true("tasks are read-only", "read-only" in html)
    check_true("tasks run in bworker", "started automatically by the bworker" in html)

    check_false("no Run now control", "Run now" in html)
    check_false("no Search missing control", "Search missing" in html)
    check_false("no task run hook", "data-run-task" in html)
    check_false("no legacy task run endpoint", "/api/tasks/" in html)


# ------------------------------------------------------------- static assets
def test_assets() -> None:
    section("static assets are local and safe")

    template = (ROOT / "iptv_charts" / "templates" / "activity.html").read_text()
    js = (ROOT / "iptv_charts" / "static" / "js" / "activity.js").read_text()
    css = (ROOT / "iptv_charts" / "static" / "css" / "activity.css").read_text()

    check_true("activity js exists", len(js) > 1000)
    check_true("activity css exists", len(css) > 1000)

    for label, text in (("template", template), ("js", js), ("css", css)):
        check_false(f"{label}: no http URLs", "http://" in text or "https://" in text)
        check_false(f"{label}: no protocol-relative URLs", "//cdn." in text)
        check_false(f"{label}: no Vue", "vue" in text.lower())
        check_false(f"{label}: no CDN", "cdn." in text.lower()
                    or "unpkg" in text.lower() or "jsdelivr" in text.lower())

    check_true("local activity css", "css/activity.css" in template)
    check_true("local manager js", "js/manager.js" in template)
    check_true("local activity js", "js/activity.js" in template)

    # API data must only ever be inserted through DOM text. Assignment patterns
    # are checked, so the explanatory comment mentioning innerHTML is fine.
    for forbidden in (".innerHTML =", ".innerHTML=", ".outerHTML =",
                      ".outerHTML=", "insertAdjacentHTML(", "document.write(",
                      "eval("):
        check_false(f"js avoids {forbidden}", forbidden in js)
    check_true("js uses textContent", ".textContent" in js)
    check_true("js rebuilds lists with DOM", "replaceChildren()" in js)

    check_true("activity endpoint",
               "/api/bworker/activity?limit=100" in js)
    check_true("status endpoint", "/api/bworker/status" in js)
    check_true("pause endpoint", "/api/bworker/pause" in js)
    check_true("resume endpoint", "/api/bworker/resume" in js)
    check_true("reset endpoint", "/api/bworker/reset" in js)
    check_true("reset all units", "all: true" in js)
    check_true("no-store requests", "cache: 'no-store'" in js)
    check_true("status and log fetched together",
               "Promise.allSettled" in js)
    check_true("running row fields", "parts.eventTag.textContent" in js
               and "parts.stageTag.textContent" in js)
    check_true("time ago rendering", "started ${shortAgo" in js)
    check_true("duration rendering", "took ${durationText" in js)
    check_true("progress rendering", "progressInfo" in js)

    check_true("countdown label", "Refresh on (${remaining}s)" in js)
    check_true("one-second tick", "setInterval(tick, 1000)" in js)
    check_true("manual pause state", "state.userPaused" in js)
    check_true("auto pause off top", "window.scrollY > 0" in js)
    check_true("auto pause log scrolled", "nodes.log.scrollTop > 0" in js)
    check_true("tab hidden pause", "Boolean(document.hidden)" in js)
    check_true("visibility resume", "visibilitychange" in js)
    check_true("effective refresh gate", "state.scrolledAway" in js
               and "state.logScrolled" in js)
    check_true("errors retried at interval",
               "Retrying every ${intervalText(state.interval)}" in js)
    check_true("non-blocking status line", "setStatus(nodes.statusLine" in js)
    check_false("no manual task trigger", "/api/tasks/" in js)
    check_false("no run-task hook", "data-run-task" in js)
    check_false("no search-missing trigger",
                "search_missing/run" in js or "'/run'" in js)

    js_values = re.search(
        r"const REFRESH_OPTIONS = \[(.*?)\];", js, re.S)
    check_true("js refresh option list", js_values is not None)
    values = re.findall(r"\d+", js_values.group(1)) if js_values else []
    check("js options match select", values, REFRESH_VALUES)

    check_true("green active refresh style",
               ".activity-refresh-btn.is-on" in css)
    check_true("log scroll container", ".activity-log" in css
               and "overflow-y: auto" in css)
    check_true("mobile breakpoint", "@media (max-width: 700px)" in css)
    check_true("css local only", "url(" not in css and "@import" not in css)
    check("css braces balance", css.count("{"), css.count("}"))

    node = shutil.which("node")
    if node:
        result = subprocess.run(
            [node, "--check", str(ROOT / "iptv_charts" / "static" / "js"
                                 / "activity.js")],
            capture_output=True, text=True, check=False,
        )
        check("node --check activity.js", result.returncode, 0)
        if result.returncode:
            print(result.stderr)
    else:
        print("  (node not found; skipping syntax check)")


# ------------------------------------------------------------- API behaviour
def test_api(client) -> None:
    section("bworker API wiring, no-store and degraded store")

    store = activity_log.MemoryActivityLog()
    activity_log.set_default_store(store)
    now = int(time.time())
    store.push(activity_log.make_entry(
        event=activity_log.EVENT_STARTED,
        status=activity_log.STATUS_RUNNING,
        work_id=7,
        work_type="playlist_sync",
        target={"kind": "source", "id": 3,
                "label": '<img src=x onerror="alert(1)">'},
        stage="fetching",
        message="Updating playlist",
        progress={"done": 2, "total": 5},
        started_at=now - 5,
    ))
    store.push(activity_log.make_entry(
        event=activity_log.EVENT_STARTED,
        status=activity_log.STATUS_RUNNING,
        work_id=8,
        work_type="movie_search",
        target={"kind": "movie", "id": 9, "label": "Movie One"},
        stage="searching",
        message="Searching providers",
        started_at=now - 2,
    ))

    response = client.get("/api/bworker/activity?limit=100")
    check("activity API status", response.status_code, 200)
    check("activity API no-store",
          response.headers.get("Cache-Control"), "no-store")
    payload = response.get_json()
    check("activity API ok", payload.get("ok"), True)
    check("newest first", payload["entries"][0]["work_id"], 8)
    check("entry count", len(payload["entries"]), 2)
    check("limit echoed", payload["limit"], 100)
    check("default refresh seconds", payload["refresh_seconds"], 5)
    check("memory store available", payload["available"], True)
    check("memory store not degraded", payload["degraded"], False)
    check("untrusted label preserved as data",
          payload["entries"][1]["target"]["label"],
          '<img src=x onerror="alert(1)">')

    response = client.get("/api/bworker/status")
    check("status API status", response.status_code, 200)
    check("status API no-store",
          response.headers.get("Cache-Control"), "no-store")
    status = response.get_json()
    for key in ("alive", "paused", "current", "queue", "heartbeat_age"):
        check_true(f"status has {key}", key in status)

    response = client.post("/api/bworker/pause")
    check("pause API status", response.status_code, 200)
    check("pause API paused", response.get_json().get("paused"), True)
    paused = client.get("/api/bworker/activity?limit=100").get_json()
    check("activity reflects pause", paused["paused"], True)
    check("activity current from SQLite", paused["current"], None)
    response = client.post("/api/bworker/resume")
    check("resume API status", response.status_code, 200)
    check("resume API resumed", response.get_json().get("paused"), False)

    response = client.post("/api/bworker/reset", json={"all": True})
    check("reset API status", response.status_code, 200)
    check_true("reset returns ok", response.get_json().get("ok"))

    # A dead activity store must not turn the page or the API into an error.
    activity_log.set_default_store(DegradedActivityLog())
    response = client.get("/api/bworker/activity?limit=100")
    check("degraded activity status", response.status_code, 200)
    degraded = response.get_json()
    check("degraded flagged", degraded["degraded"], True)
    check("degraded unavailable", degraded["available"], False)
    check("degraded entries empty", degraded["entries"], [])
    check("degraded no-store",
          response.headers.get("Cache-Control"), "no-store")
    check("page still renders during degradation",
          client.get("/activity").status_code, 200)
    check("status API survives dead log",
          client.get("/api/bworker/status").status_code, 200)


def main() -> int:
    db.init_db()
    app = make_app()
    client = app.test_client()

    test_page_markup(client)
    test_assets()
    test_api(client)

    failed = [item for item in _RESULT if not item[0]]
    print(f"\n{len(_RESULT) - len(failed)}/{len(_RESULT)} checks passed")
    if failed:
        print("\nFailures:")
        for _ok, label, detail in failed:
            print(f"  - {label}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
