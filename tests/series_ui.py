#!/usr/bin/env python3
"""Standalone UI/contract tests for the 0.8.0 series detail page.

Run with:  .venv/bin/python tests/series_ui.py

No pytest, no network and no browser. The Flask test client renders the
server-side page, the A04 stage endpoint is compared with the rendered polling
signature, and the HTTP contracts used by the page's actions are exercised
against temporary fixtures. bworker_events is then disabled to prove the
direct-SQL fallback renders the same stage vocabulary.
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

_TMP = Path(tempfile.mkdtemp(prefix="iptv-series-ui-test-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_TMP / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_TMP / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_TMP / "cache")
os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(_TMP / "media")
os.environ["IPTV_CHARTS_SCHEDULER"] = "0"
os.environ["IPTV_CHARTS_LOG_LEVEL"] = "CRITICAL"
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)

sys.path.insert(0, str(ROOT))

from iptv_charts import db, manager_web, manual_metadata, quality  # noqa: E402
from iptv_charts.app import create_app  # noqa: E402

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


def contains(label: str, haystack: str, needle: str) -> None:
    check(label, needle in haystack, f"missing {needle!r}")


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(value or "")).strip()


def strip_tags(value: str) -> str:
    return normalize(re.sub(r"<[^>]+>", " ", value or ""))


def episode_stage_label(page: str, stage: str) -> str:
    pattern = re.compile(
        r'<span[^>]*\bdata-episode-stage="' + re.escape(stage)
        + r'"[^>]*>(.*?)</span>',
        re.S,
    )
    match = pattern.search(page)
    return strip_tags(match.group(1)) if match else ""


STAGES = (
    ("pending", "pending"),
    ("waiting_availability", "pending"),
    ("waiting_air", "pending"),
    ("search", "pending"),
    ("searching_candidates", "running"),
    ("probing", "running"),
    ("downloading", "running"),
    ("verifying", "running"),
    ("renaming", "running"),
    ("moving", "running"),
    ("awaiting_upgrade", "pending"),
    ("complete", "complete"),
    ("no_candidates", "retry"),
    ("failed", "failed_stopped"),
    ("paused", "disabled"),
    ("skipped", "disabled"),
)

STAGE_LABELS = {
    "pending": "Pending",
    "waiting_availability": "Waiting for availability",
    "waiting_air": "Waiting for air",
    "search": "Searching",
    "searching_candidates": "Searching candidates",
    "probing": "Probing",
    "downloading": "Downloading",
    "verifying": "Verifying",
    "renaming": "Renaming",
    "moving": "Moving",
    "awaiting_upgrade": "Awaiting upgrade",
    "complete": "Complete",
    "no_candidates": "No candidates",
    "failed": "Failed",
    "paused": "Paused",
    "skipped": "Skipped",
}

STAGE_HINTS = (
    "waiting for availability",
    "waiting for the episode to air",
    "awaiting the search",
    "Scanning the catalog",
    "Probing candidate streams",
    "Downloading the chosen release",
    "Verifying the downloaded file",
    "Renaming and importing the file",
    "Moving the file into the library",
    "awaiting a better release",
    "profile target met",
    "No eligible candidate matched the profile",
    "reset the work to retry",
    "Skipped / unmonitored",
    "Queued for the next bworker pass",
)

NOW = int(time.time())
MEDIA = Path(os.environ["IPTV_CHARTS_MEDIA_DIR"])


def media_file(series_title: str, season: int, episode: int) -> str:
    folder = MEDIA / "shows" / series_title
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"S{season:02d}E{episode:02d}.mkv"
    path.write_bytes(b"not a real video, just a fixture")
    return str(path)


def add_series(conn, *, title, status, monitored=1, profile_id=None,
               year=2020) -> int:
    conn.execute(
        "INSERT INTO series (tmdb_id, title, year, status, monitored, "
        "monitor_new_seasons, quality_profile_id, priority, added_at) "
        "VALUES (?, ?, ?, ?, ?, 'all', ?, 0, ?)",
        (abs(hash(title)) % 900000 + 1000, title, year, status, monitored,
         profile_id, NOW),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def add_episode(conn, series_id: int, season: int, episode: int, *,
                title=None, air_date="2020-01-01", monitored=1,
                file_path=None, file_score=None, overview=None,
                resolution=None, quality_label=None, audio=None,
                subtitles=None, media_info=None) -> int:
    conn.execute(
        "INSERT INTO series_episodes (series_id, season, episode, title, "
        "overview, air_date, monitored, file_path, file_score, resolution, "
        "source, quality_label, audio_languages, subtitle_languages, "
        "media_info) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            series_id, season, episode, title or f"Episode {episode}", overview,
            air_date, monitored, file_path, file_score, resolution,
            "web-dl" if quality_label else None, quality_label,
            audio, subtitles, media_info,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def add_work(conn, *, key: str, entity_type: str, entity_id: int, stage: str,
             status: str, next_run_at=None, best_score=None, last_error=None,
             work_type=None) -> int:
    conn.execute(
        "INSERT INTO bworker_work (work_key, work_type, entity_type, "
        "entity_id, stage, status, next_run_at, best_score, last_error, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key, work_type or f"{entity_type}_search", entity_type,
            entity_id, stage, status, next_run_at, best_score, last_error,
            NOW, NOW,
        ),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def add_download(conn, *, series_id: int, episode_id: int, title: str,
                 done: int, total: int, remote: str) -> int:
    conn.execute(
        "INSERT INTO downloads (kind, series_id, episode_id, source_name, "
        "provider_kind, provider_remote_id, title, season, episode, status, "
        "bytes_done, bytes_total, requested_at, started_at) "
        "VALUES ('episode', ?, ?, 'Test Playlist', 'series', ?, ?, 1, 1, "
        "'downloading', ?, ?, ?, ?)",
        (series_id, episode_id, remote, title, done, total, NOW, NOW),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def add_event(conn, *, entity_type: str, entity_id: int, stage: str,
              status: str, message: str, created_at: int,
              progress: str | None = None) -> None:
    conn.execute(
        "INSERT INTO bworker_events (work_id, work_type, entity_type, "
        "entity_id, stage, status, message, progress, created_at) "
        "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"{entity_type}_search", entity_type, entity_id, stage, status,
         message, progress, created_at),
    )


def signature_for(data: dict) -> str:
    """The exact signature computed by series_detail.js."""
    events = data.get("events") or []
    progress = data.get("progress") or {}
    done = progress.get("done")
    total = progress.get("total")
    return "|".join((
        str(data.get("stage") or "pending"),
        str(data.get("status") or ""),
        str(data.get("message") or ""),
        "" if data.get("best_score") is None else str(data.get("best_score")),
        f"{'' if done is None else done}/{'' if total is None else total}",
        str(events[0].get("id")) if events else "",
    ))


def main() -> int:
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()
    conn = db.connect()

    profile_row = conn.execute(
        "SELECT * FROM quality_profiles WHERE builtin_key = '1080p'"
    ).fetchone()
    check("seeded 1080p profile exists", profile_row is not None)
    profile = quality.profile_summary(dict(profile_row))
    profile_id = int(profile["id"])
    target = int(profile.get("target_score") or 20)
    max_score = int(profile.get("max_score") or target)
    check("seeded profile supports upgrades", bool(profile.get("upgrades")))
    below_score = max(0, target - 1)
    complete_score = max(target, 1)

    # ------------------------------------------------------------ main series
    main_id = add_series(
        conn, title="Stage Show", status="Returning Series",
        profile_id=profile_id,
    )
    stage_episodes: dict[str, int] = {}
    complete_file = ""
    upgrade_file = ""
    download_id = 0
    for index, (stage, status) in enumerate(STAGES):
        season = 1 if index < 8 else 2
        episode = index + 1 if season == 1 else index - 8 + 1
        file_path = None
        file_score = None
        tech = {}
        overview = None
        if stage == "complete":
            file_path = media_file("Stage Show", season, episode)
            complete_file = file_path
            file_score = complete_score
            tech = {
                "resolution": "1080p",
                "quality_label": "WEB-DL",
                "audio": '["eng"]',
                "subtitles": '["eng"]',
                "media_info": '{"video_codec": "h264"}',
            }
            overview = "The complete episode overview tooltip."
        elif stage == "awaiting_upgrade":
            file_path = media_file("Stage Show", season, episode)
            upgrade_file = file_path
            file_score = below_score
        episode_id = add_episode(
            conn, main_id, season, episode,
            title=f"Stage {index + 1:02d}",
            air_date="2020-01-01",
            file_path=file_path,
            file_score=file_score,
            overview=overview,
            **tech,
        )
        stage_episodes[stage] = episode_id
        next_run = NOW + 120 if status in ("pending", "retry") else (
            NOW if status == "running" else None)
        add_work(
            conn, key=f"episode:{episode_id}", entity_type="episode",
            entity_id=episode_id, stage=stage, status=status,
            next_run_at=next_run,
            best_score=90 if stage == "probing" else None,
            last_error="No stream responded" if stage == "failed" else None,
        )
        if stage == "downloading":
            download_id = add_download(
                conn, series_id=main_id, episode_id=episode_id,
                title="Stage Show S01E07", done=500, total=1000,
                remote="fixture-download-main",
            )

    # Twenty events; A04 and the SQL fallback must expose the newest fifteen.
    for index in range(1, 21):
        entity_type = "series" if index % 3 == 0 else "episode"
        entity_id = main_id if entity_type == "series" else stage_episodes["search"]
        add_event(
            conn, entity_type=entity_type, entity_id=entity_id,
            stage="searching_candidates" if index % 2 else "probing",
            status="succeeded", message=f"Event {index:02d}",
            created_at=NOW - (21 - index),
            progress='{"done": 1, "total": 4}' if index == 20 else None,
        )

    # --------------------------------------------------------- derived series
    derived_id = add_series(
        conn, title="Derived Show", status="In Production",
        profile_id=profile_id,
    )
    derived_future = add_episode(
        conn, derived_id, 1, 1, title="Future", air_date="2099-01-01",
        overview="The future episode overview.",
    )
    derived_missing = add_episode(
        conn, derived_id, 1, 2, title="Aired Missing", air_date="2020-01-01",
    )
    derived_unmonitored = add_episode(
        conn, derived_id, 1, 3, title="Unmonitored", air_date="2020-01-01",
        monitored=0,
    )
    derived_upgrade = add_episode(
        conn, derived_id, 1, 4, title="Below Profile",
        air_date="2020-01-01",
        file_path=media_file("Derived Show", 1, 4),
        file_score=below_score,
    )
    derived_downloading = add_episode(
        conn, derived_id, 1, 5, title="Downloading", air_date="2020-01-01",
    )
    derived_complete = add_episode(
        conn, derived_id, 1, 6, title="Complete", air_date="2020-01-01",
        file_path=media_file("Derived Show", 1, 6),
        file_score=complete_score,
        resolution="1080p", quality_label="WEB-DL",
        audio='["eng"]', subtitles='["eng"]',
        media_info='{"video_codec": "h264"}',
    )
    add_download(
        conn, series_id=derived_id, episode_id=derived_downloading,
        title="Derived Show S01E05", done=250, total=1000,
        remote="fixture-download-derived",
    )

    # ------------------------------------------------------- ended / canceled
    ended_id = add_series(conn, title="Ended Show", status="Ended")
    canceled_id = add_series(conn, title="Canceled Show", status="Canceled")
    conn.commit()

    # ---------------------------------------------------------------- render
    response = client.get(f"/series/{main_id}")
    check("series detail returns 200", response.status_code == 200,
          str(response.status_code))
    page = response.get_data(as_text=True)
    text = normalize(page)
    body = html_lib.unescape(page)

    contains("page title", page, "Stage Show")
    contains("returning series status", page, "Returning Series")
    contains("airing label", page, "airing-airing")
    contains("airing text", text, "Airing")
    check("dark theme attribute", 'data-theme="dark"' in page)
    check("no Vue runtime", "vue" not in page.lower())

    response = client.get(
        f"/api/series/{main_id}/episodes/{stage_episodes['search']}"
        "/candidates?q=Stage&limit=10",
    )
    candidates_payload = response.get_json() or {}
    check("episode candidate browser returns JSON",
          response.status_code == 200, str(response.status_code))
    check("episode candidate browser reports ok",
          candidates_payload.get("ok"), True)
    check("episode candidate browser returns a list",
          isinstance(candidates_payload.get("candidates"), list), True)
    check("episode candidate browser echoes the query",
          candidates_payload.get("query"), "Stage")

    # Vendored assets only.
    contains("bootstrap css link", page,
             '/static/vendor/bootstrap/css/bootstrap.min.css')
    contains("bootstrap js link", page,
             '/static/vendor/bootstrap/js/bootstrap.bundle.min.js')
    contains("font awesome local link", page,
             '/static/vendor/fontawesome/css/all.min.css')
    check("bootstrap css file exists",
          (ROOT / "iptv_charts/static/vendor/bootstrap/css/bootstrap.min.css").is_file())
    check("bootstrap js file exists",
          (ROOT / "iptv_charts/static/vendor/bootstrap/js/bootstrap.bundle.min.js").is_file())
    check("no external http asset", "http://" not in body and "https://" not in body)
    for external in ("cdn.", "unpkg", "jsdelivr", "cdnjs"):
        check(f"no {external}", external not in body.lower())

    # Existing ids and data attributes are preserved.
    for element_id in (
        "series-search-now", "series-refresh", "series-delete",
        "series-action-status", "series-edit-form", "series-monitored-edit",
        "series-profile-edit", "series-new-seasons-edit",
        "series-priority-edit", "series-edit-status", "episode-search-dialog",
        "episode-release-query", "episode-release-status",
        "episode-release-list", "episode-all-candidates",
        "series-delete-dialog", "series-delete-files",
        "series-delete-confirm",
    ):
        contains(f"id {element_id}", page, f'id="{element_id}"')
    for marker in (
        "data-season-monitor=", "data-episode-monitor=",
        "data-episode-search=", "data-episode-manual=",
    ):
        contains(f"marker {marker}", page, marker)

    # New stage, metadata and file controls.
    for element_id in (
        "series-stage-card", "series-stage-refresh",
        "series-stage-tab-current", "series-stage-tab-history",
        "series-stage-current", "series-stage-history",
        "series-manual-metadata", "series-metadata-dialog",
        "series-metadata-query", "series-metadata-year",
        "series-metadata-search", "series-metadata-status",
        "series-metadata-results", "episode-remove-dialog",
        "episode-remove-label", "episode-remove-confirm",
    ):
        contains(f"new id {element_id}", page, f'id="{element_id}"')
    refresh_at = page.find('id="series-refresh"')
    metadata_at = page.find('id="series-manual-metadata"')
    delete_at = page.find('id="series-delete"')
    check("manual metadata sits beside refresh",
          -1 < refresh_at < metadata_at < delete_at,
          f"{refresh_at}, {metadata_at}, {delete_at}")
    contains("metadata prefill", page, 'id="series-metadata-query" value="Stage Show"')
    contains("metadata year prefill", page, 'id="series-metadata-year" min="1888"')

    # Bootstrap accordion markup, local and usable without custom JS toggles.
    check("two season accordions",
          page.count('data-bs-toggle="collapse"') == 2,
          str(page.count('data-bs-toggle="collapse"')))
    contains("accordion container", page, 'class="accordion series-accordions"')
    contains("accordion parent", page, 'data-bs-parent="#series-seasons"')
    contains("first accordion open", page, "accordion-collapse collapse show")
    contains("aria-expanded true", page, 'aria-expanded="true"')
    contains("aria-expanded false", page, 'aria-expanded="false"')
    contains("season 1 controls", page, 'data-season-monitor="1"')
    contains("season 2 controls", page, 'data-season-monitor="2"')
    contains("season 1 search", page, 'data-season-search="1"')
    contains("season 2 search", page, 'data-season-search="2"')
    check("all season monitors checked",
          page.count('data-season-monitor="1"\n                               checked') == 1
          or bool(re.search(r'data-season-monitor="1"\s+checked', page)))
    check("sixteen episode rows", page.count('class="episode-row"') == 16,
          str(page.count('class="episode-row"')))
    check("sixteen episode monitors", page.count("data-episode-monitor=") == 16,
          str(page.count("data-episode-monitor=")))
    check("sixteen episode searches", page.count("data-episode-search=") == 16,
          str(page.count("data-episode-search=")))
    check("sixteen episode manual buttons", page.count("data-episode-manual=") == 16,
          str(page.count("data-episode-manual=")))

    # Current/History tabs are server-rendered with the history hidden.
    contains("current tab", page, 'data-stage-tab="current"')
    contains("history tab", page, 'data-stage-tab="history"')
    contains("current panel", page, 'data-stage-panel="current"')
    contains("history panel", page, 'data-stage-panel="history"')
    check("history panel initially hidden",
          bool(re.search(r'data-stage-panel="history"[^>]*hidden', page)))
    check("current tab selected", 'aria-selected="true"' in page)

    # Aggregate stage and counts.
    contains("aggregate stage attr", page, 'data-stage="')
    contains("aggregate running status", page, 'data-stage-status="running"')
    check("bworker now sentence",
          bool(re.search(r"(?:Moving|Downloading|Renaming|Verifying|Probing) now\.", text)),
          "missing an active-stage 'now' sentence")
    contains("downloaded count", text, "Episodes downloaded")
    contains("downloaded over monitored", text, "<strong>2 / 16</strong>")
    contains("missing count", text, "Missing")
    check("missing metric is 14",
          bool(re.search(r"Missing\s*</span>\s*<strong>14</strong>", text)), text[:200])
    check("unaired metric is 0",
          bool(re.search(r"Unaired\s*</span>\s*<strong>0</strong>", text)), text[:200])
    contains("quality profile metric", text, "1080p")
    contains("profile target", text, f"Target {target}")
    contains("best candidate score metric", text, "Best candidate score")
    contains("next bworker pass metric", text, "Next bworker pass")

    # Every canonical stage is present with its promised label.
    for stage, label in STAGE_LABELS.items():
        actual = episode_stage_label(page, stage)
        check(f"stage label {stage}", actual == label,
              f"got {actual!r}, expected {label!r}")
    for hint in STAGE_HINTS:
        contains(f"stage hint {hint}", text, hint)

    # Episode metadata (title, air date, overview tooltip, tech tracks).
    contains("episode title", text, "Stage 01")
    contains("air date", text, "2020-01-01")
    contains("overview tooltip", page, "The complete episode overview tooltip.")
    contains("resolution metadata", text, "1080p")
    contains("quality metadata", text, "WEB-DL")
    contains("audio metadata", text, "audio eng")
    contains("subtitle metadata", text, "subs eng")
    check("download progress rendered", 'role="progressbar"' in page)
    check("download aria progress", bool(re.search(r'aria-valuenow="[\d.]+"', page)))

    # Recent history: newest first, capped at fifteen.
    history_at = page.find('id="series-stage-history"')
    history_page = page[history_at:]
    event_ids = [int(value) for value in
                 re.findall(r'data-event-id="(\d+)"', history_page)]
    check("history has fifteen events", len(event_ids) == 15, str(event_ids))
    check("history newest first", event_ids == sorted(event_ids, reverse=True),
          str(event_ids))
    check("history keeps newest id", event_ids and event_ids[0] == 20,
          str(event_ids[:1]))
    check("history drops older events", event_ids and min(event_ids) == 6,
          str(event_ids[-1:]))
    contains("newest event message", history_page, "Event 20")
    check("oldest dropped event absent", "Event 05" not in history_page)
    contains("series event label", text, "Series")
    contains("episode event code", text, "S01E04")

    # The polling signature rendered by the server must equal the one the JS
    # computes from /api/series/<id>/stage, otherwise the page would reload
    # on every poll forever.
    api = client.get(f"/api/series/{main_id}/stage")
    check("stage API returns 200", api.status_code == 200, str(api.status_code))
    stage_data = api.get_json() or {}
    check("stage API ok", stage_data.get("ok") is True)
    signature_match = re.search(r'data-stage-signature="([^"]*)"', page)
    check("rendered signature present", signature_match is not None)
    server_signature = html_lib.unescape(signature_match.group(1)) if signature_match else ""
    check("polling signature matches API",
          server_signature == signature_for(stage_data),
          f"{server_signature!r} != {signature_for(stage_data)!r}")
    contains("polling url", page, 'window.SERIES_STAGE_URL = "/api/series/')

    # Remove-file controls carry a media-root-relative key.
    complete_key = "shows/Stage Show/S02E04.mkv"
    upgrade_key = "shows/Stage Show/S02E03.mkv"
    contains("remove file button", page, "data-episode-remove-file=")
    contains("complete file key", page, f'data-file-key="{complete_key}"')
    contains("upgrade file key", page, f'data-file-key="{upgrade_key}"')
    check("two remove file controls",
          page.count("data-episode-remove-file=") == 2,
          str(page.count("data-episode-remove-file=")))

    # ------------------------------------------------------- derived series
    derived_response = client.get(f"/series/{derived_id}")
    check("derived series returns 200", derived_response.status_code == 200)
    derived_page = derived_response.get_data(as_text=True)
    derived_text = normalize(derived_page)
    contains("derived airing", derived_page, "airing-upcoming")
    contains("derived status label", derived_text, "In Production")
    for stage, label in {
        "waiting_air": "Waiting for air",
        "search": "Searching",
        "skipped": "Skipped",
        "awaiting_upgrade": "Awaiting upgrade",
        "downloading": "Downloading",
        "complete": "Complete",
    }.items():
        actual = episode_stage_label(derived_page, stage)
        check(f"derived stage {stage}", actual == label, f"got {actual!r}")
    contains("derived search hint", derived_text, "Aired — awaiting the search")
    contains("derived upgrade hint", derived_text, "awaiting a better release")
    contains("derived future overview", derived_page,
             "The future episode overview.")
    check("derived progress bar", 'role="progressbar"' in derived_page)
    check("derived downloaded over monitored",
          "<strong>2 / 5</strong>" in derived_text, derived_text[:300])
    check("derived missing is 2",
          bool(re.search(r"Missing\s*</span>\s*<strong>2</strong>", derived_text)))
    check("derived unaired is 1",
          bool(re.search(r"Unaired\s*</span>\s*<strong>1</strong>", derived_text)))
    contains("derived empty history", derived_text,
             "No bworker events recorded for this series yet")
    contains("derived tech audio", derived_text, "audio eng")
    contains("derived remove file", derived_page, 'data-file-key="shows/Derived Show/S01E04.mkv"')
    check("derived complete derived from file", 'data-episode-stage="complete"' in derived_page)
    check("derived rows", derived_page.count('class="episode-row"') == 6,
          str(derived_page.count('class="episode-row"')))

    # ------------------------------------------------------- ended/canceled
    ended_page = client.get(f"/series/{ended_id}").get_data(as_text=True)
    canceled_page = client.get(f"/series/{canceled_id}").get_data(as_text=True)
    contains("ended airing", ended_page, "airing-ended")
    contains("ended label", normalize(ended_page), ">Ended</span>")
    contains("canceled label", normalize(canceled_page), ">Canceled</span>")
    contains("ended empty episodes", ended_page, "No episodes have been loaded")
    check("ended page has no accordion", "data-bs-toggle=\"collapse\"" not in ended_page)

    # ------------------------------------------------------------- fallback
    original_events_module = manager_web.bworker_events
    manager_web.bworker_events = None
    try:
        fallback_response = client.get(f"/series/{main_id}")
        fallback_html = fallback_response.get_data(as_text=True)
        fallback_text = normalize(fallback_html)
        check("fallback returns 200", fallback_response.status_code == 200,
              str(fallback_response.status_code))
        for stage, label in STAGE_LABELS.items():
            actual = episode_stage_label(fallback_html, stage)
            check(f"fallback stage {stage}", actual == label, f"got {actual!r}")
        fallback_ids = [int(value) for value in
                        re.findall(r'data-event-id="(\d+)"', fallback_html)]
        check("fallback history cap", len(fallback_ids) == 15, str(fallback_ids))
        check("fallback history newest first",
              fallback_ids == sorted(fallback_ids, reverse=True))
        contains("fallback aggregate stage", fallback_html, 'data-stage="')
        contains("fallback remove file", fallback_html, complete_key)
        contains("fallback profile", fallback_text, "1080p")
    finally:
        manager_web.bworker_events = original_events_module

    # --------------------------------------------------------------- paused
    conn.execute("UPDATE bworker_state SET paused = 1 WHERE id = 1")
    conn.commit()
    paused_page = client.get(f"/series/{main_id}").get_data(as_text=True)
    check("paused banner rendered",
          'id="series-stage-paused"' in paused_page)
    contains("paused explanation", normalize(paused_page), "Bworker is paused.")
    contains("paused bworker sentence", normalize(paused_page),
             "this series waits until it resumes")
    conn.execute("UPDATE bworker_state SET paused = 0 WHERE id = 1")
    conn.commit()

    # ------------------------------------------------------ manual metadata
    captured: dict = {}
    original_search = manual_metadata.search
    original_apply = manual_metadata.apply

    def fake_search(conn_arg, kind, query, year=None):
        captured["search"] = (kind, query, year)
        return [{
            "tmdb_id": 4242, "title": "New Show", "original_title": "New Show",
            "year": 2021, "release_date": "2021-01-01", "overview": "New.",
            "poster": None, "kind": "tv", "in_library": False,
            "library_id": None, "library_title": None,
        }]

    def fake_apply(conn_arg, kind, item_id, tmdb_id):
        captured["apply"] = (kind, int(item_id), int(tmdb_id))
        return {
            "kind": kind, "tmdb_id": int(tmdb_id),
            "series": {"id": int(item_id), "title": "New Show"},
            "item": {"id": int(item_id), "title": "New Show"},
            "episodes_refreshed": 16,
            "warning": "Downloaded files were kept.",
        }

    try:
        manual_metadata.search = fake_search
        search_response = client.get(
            f"/api/import/metadata/series/{main_id}/search",
            query_string={"q": "New Show", "year": 2021},
        )
        search_payload = search_response.get_json() or {}
        check("metadata search status", search_response.status_code == 200,
              str(search_response.status_code))
        check("metadata search ok", search_payload.get("ok") is True)
        check("metadata search result", len(search_payload.get("results") or []) == 1)
        check("metadata search args", captured.get("search") == ("series", "New Show", 2021),
              str(captured.get("search")))

        manual_metadata.apply = fake_apply
        apply_response = client.post(
            f"/api/import/metadata/series/{main_id}", json={"tmdb_id": 4242})
        apply_payload = apply_response.get_json() or {}
        check("metadata apply status", apply_response.status_code == 200,
              str(apply_response.status_code))
        check("metadata apply ok", apply_payload.get("ok") is True)
        check("metadata apply warning", apply_payload.get("warning") ==
              "Downloaded files were kept.")
        check("metadata apply args", captured.get("apply") == ("series", main_id, 4242),
              str(captured.get("apply")))
    finally:
        manual_metadata.search = original_search
        manual_metadata.apply = original_apply

    # ------------------------------------------------------- remove a file
    storage_rule = next(
        (rule for rule in app.url_map.iter_rules()
         if rule.rule == "/api/storage/file" and "DELETE" in rule.methods),
        None,
    )
    check("storage delete endpoint exists", storage_rule is not None)
    delete_response = client.delete(
        "/api/storage/file",
        json={"path": complete_key, "confirm": True, "confirm_active": True},
    )
    delete_payload = delete_response.get_json() or {}
    check("storage delete status", delete_response.status_code == 200,
          f"{delete_response.status_code} {delete_payload}")
    check("storage delete ok", delete_payload.get("ok") is True)
    check("storage reconciled episode",
          int((delete_payload.get("library") or {}).get("episodes") or 0) == 1,
          str(delete_payload.get("library")))
    check("file removed from disk", not Path(complete_file).exists())
    row = conn.execute(
        "SELECT file_path FROM series_episodes WHERE id = ?",
        (stage_episodes["complete"],),
    ).fetchone()
    check("episode row marked missing", row is not None and row["file_path"] is None,
          str(dict(row) if row else None))
    after_delete = client.get(f"/series/{main_id}").get_data(as_text=True)
    check("removed file key gone from page", complete_key not in after_delete)
    check("other file key remains", upgrade_key in after_delete)

    # --------------------------------------------------- static JS/CSS checks
    js = (ROOT / "iptv_charts/static/js/series_detail.js").read_text()
    css = (ROOT / "iptv_charts/static/css/series_detail.css").read_text()
    contains("js stage url", js, "window.SERIES_STAGE_URL")
    contains("js stage signature", js, "stageSignature")
    contains("js polling", js, "setInterval")
    contains("js season search", js, "data-season-search")
    contains("js season endpoint", js,
             "/api/series/${seriesId}/episodes/${episodeId}/search")
    contains("js metadata search endpoint", js,
             "/api/import/metadata/series/${seriesId}/search")
    contains("js metadata apply endpoint", js,
             "/api/import/metadata/series/${seriesId}")
    contains("js storage delete", js, "/api/storage/file")
    contains("js bworker reset", js, "/api/bworker/reset")
    contains("js remove file control", js, "data-episode-remove-file")
    contains("js all candidates endpoint", js,
             "/episodes/${targetEpisode}/candidates")
    contains("js manual episode override", js, "force: manualMode && !release.allowed")
    contains("js episode probe endpoint", js, "candidates/probe")
    contains("js renders probe media facts", js, "mediaFacts(release)")
    check("js has no broken task call", "/api/tasks/" not in js)
    check("js has no external URL", "http://" not in js and "https://" not in js)
    contains("css accordion", css, ".series-accordions")
    contains("css bootstrap reset", css, "--bs-body-bg")
    contains("css episode row", css, ".episode-row")
    contains("css stage badge", css, ".stage-badge")
    contains("css mobile breakpoint", css, "@media (max-width: 760px)")
    check("css has no external URL", "http://" not in css and "https://" not in css)

    conn.close()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s) did not pass; {_passed} passed.")
        return 1
    print(f"OK: {_passed} checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
