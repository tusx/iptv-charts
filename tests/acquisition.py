#!/usr/bin/env python3
"""Standalone tests for the 0.8.0 candidate acquisition pipeline.

Run with:  .venv/bin/python tests/acquisition.py

No pytest, no network, no ffprobe and no provider calls. Stream probes and
Xtream episode listings are replaced with deterministic fakes; everything
else is the real engine, matching, scoring, scan state and downloader queue.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

_TMP = Path(tempfile.mkdtemp(prefix="iptv-acquisition-test-"))
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
    acquisition,
    bworker_store as store,
    db,
    downloader,
    items,
    jobs,
    library,
    matching,
    probe,
    quality,
)
from iptv_charts.clients import m3u, xtream  # noqa: E402

_passed = 0
_failures: list[tuple[str, str]] = []


def check(label: str, condition, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        return
    _failures.append((label, detail))
    print(f"FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def eq(label: str, got, want) -> None:
    check(label, got == want, f"{got!r} != {want!r}")


def close(label: str, got, want, tolerance: float = 1e-6) -> None:
    try:
        ok = abs(float(got) - float(want)) <= tolerance
    except (TypeError, ValueError):
        ok = False
    check(label, ok, f"{got!r} != {want!r}")


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


# --------------------------------------------------------------- environment
conn = db.connect()


def reset_runtime() -> None:
    global PROBE_CALLS, ENRICH_CALLS
    PROBE_CALLS.clear()
    ENRICH_CALLS.clear()
    PROBE_RESULTS.clear()
    PROVIDER_EPISODES.clear()


# Probe results are keyed by (source_id, provider_remote_id) for movies and
# (source_id, provider_remote_id, stream_id) for episodes.
PROBE_RESULTS: dict[tuple, dict] = {}
PROBE_CALLS: list[tuple] = []
ENRICH_CALLS: list[int] = []
PROVIDER_EPISODES: dict[tuple[int, str], list[dict]] = {}
PROVIDER_CALLS: list[tuple[int, str]] = []


def _default_payload(release, stream_id: str = "") -> dict:
    return {
        "ok": True,
        "resolution": release.get("resolution") or "1080p",
        "audio": [],
        "video": [{"codec": "h264"}],
        "size": 1_000_000,
        "probed_at": int(time.time()),
    }


def fake_probe_movie(conn, release, *, refresh=False, acquire_timeout=5.0):
    key = (int(release["source_id"]), str(release["remote_id"]))
    PROBE_CALLS.append(("movie", key))
    payload = PROBE_RESULTS.get(key) or _default_payload(release)
    return probe.apply(dict(release), payload)


def fake_probe_episode(conn, release, *, refresh=False, acquire_timeout=5.0):
    stream_id = str((release.get("provider_episode") or {}).get("stream_id") or "")
    key = (int(release["source_id"]), str(release["remote_id"]), stream_id)
    PROBE_CALLS.append(("episode", key))
    payload = PROBE_RESULTS.get(key) or _default_payload(release)
    return probe.apply(dict(release), payload)


def store_probe(source_id: int, remote_id: str, payload: dict, *,
                kind: str = "vod", stream_id: str = "") -> None:
    conn.execute(
        "INSERT INTO stream_probes (source_id, provider_kind, provider_remote_id, "
        "episode_stream_id, ok, payload, probed_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, provider_kind, provider_remote_id, episode_stream_id) "
        "DO UPDATE SET ok = excluded.ok, payload = excluded.payload, "
        "probed_at = excluded.probed_at",
        (int(source_id), kind, str(remote_id), str(stream_id),
         1 if payload.get("ok") else 0,
         json.dumps(payload, ensure_ascii=False), int(time.time())),
    )
    conn.commit()


def fake_provider_episodes(conn, source_row, remote_id, *, refresh=False):
    key = (int(source_row["id"]), str(remote_id))
    PROVIDER_CALLS.append(key)
    return [dict(entry) for entry in PROVIDER_EPISODES.get(key, [])]


def fake_enrich(conn, item, *, acquire_timeout=2.0):
    ENRICH_CALLS.append(int(item["id"]))
    return None, None


probe.probe_movie_release = fake_probe_movie
probe.probe_episode_release = fake_probe_episode
library.provider_series_episodes = fake_provider_episodes
items.enrich_item_identity = fake_enrich


# ------------------------------------------------------------------- fixtures
def add_source(kind: str = "m3u", name: str | None = None) -> int:
    name = name or f"{kind}-{time.time_ns()}"
    cursor = conn.execute(
        "INSERT INTO sources (name, kind, created_at, base_url, username, "
        "password, m3u_url, sync_status, last_synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', ?)",
        (name, kind, int(time.time()), "http://127.0.0.1:1" if kind == "xtream" else None,
         "u" if kind == "xtream" else None, "p" if kind == "xtream" else None,
         "http://127.0.0.1:1/list.m3u" if kind == "m3u" else None,
         int(time.time())),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_item(source_id: int, kind: str, remote_id: str, name: str, *,
             tmdb_id=None, imdb_id=None, container_ext="mkv",
             request_headers=None) -> int:
    cursor = conn.execute(
        "INSERT INTO items (source_id, kind, remote_id, name, container_ext, "
        "tmdb_id, imdb_id, request_headers) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (int(source_id), kind, str(remote_id), name, container_ext,
         str(tmdb_id) if tmdb_id is not None else None,
         imdb_id, json.dumps(request_headers) if request_headers else None),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _next_tmdb() -> int:
    return int(time.time_ns() % 1_000_000_000) + 10


def add_movie(*, title="Acquisition Movie", year=2020, tmdb_id=None,
              imdb_id=None, profile_id=None, file_path=None, source=None,
              main_source_id=None, main_remote_id=None, media_info=None,
              candidate_batch_size=None, probe_batch_size=None,
              monitored=1, file_score=None) -> int:
    tmdb_id = tmdb_id or _next_tmdb()
    while conn.execute("SELECT 1 FROM movies WHERE tmdb_id = ?", (int(tmdb_id),)).fetchone():
        tmdb_id += 1
    cursor = conn.execute(
        """
        INSERT INTO movies (tmdb_id, imdb_id, title, year, added_at, monitored,
                            quality_profile_id, file_path, source, main_source_id,
                            main_remote_id, media_info, file_score,
                            candidate_batch_size, probe_batch_size,
                            release_date, status, minimum_availability)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2020-01-01',
                'Released', 'announced')
        """,
        (int(tmdb_id), imdb_id, title, year, int(time.time()), int(monitored),
         profile_id, file_path, source, main_source_id, main_remote_id,
         media_info, file_score, candidate_batch_size, probe_batch_size),
    )
    movie_id = int(cursor.lastrowid)
    conn.commit()
    store.ensure_movie_work(conn, movie_id)
    return movie_id


def add_series(*, title="Acquisition Series", year=2020, tmdb_id=None,
               profile_id=None, monitored=1,
               candidate_batch_size=None, probe_batch_size=None) -> int:
    tmdb_id = tmdb_id or _next_tmdb()
    while conn.execute("SELECT 1 FROM series WHERE tmdb_id = ?", (int(tmdb_id),)).fetchone():
        tmdb_id += 1
    cursor = conn.execute(
        """
        INSERT INTO series (tmdb_id, title, year, added_at, monitored,
                            quality_profile_id, candidate_batch_size,
                            probe_batch_size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (int(tmdb_id), title, year, int(time.time()), int(monitored),
         profile_id, candidate_batch_size, probe_batch_size),
    )
    series_id = int(cursor.lastrowid)
    conn.commit()
    return series_id


def add_episode(series_id: int, season=1, episode=1, *, monitored=1,
                air_date="2020-01-01", file_path=None, media_info=None) -> int:
    cursor = conn.execute(
        "INSERT INTO series_episodes (series_id, season, episode, air_date, "
        "monitored, title, file_path, media_info) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (int(series_id), season, episode, air_date, int(monitored),
         f"S{season:02d}E{episode:02d}", file_path, media_info),
    )
    episode_id = int(cursor.lastrowid)
    conn.commit()
    store.ensure_episode_work(conn, episode_id)
    return episode_id


def add_profile(name: str, *, resolution="1080p", resolution_weight=10,
                quality_options=(("web-dl", "WEB-DL", 10),),
                audio_codecs=(), target=None, upgrades=True) -> int:
    payload = {
        "name": name,
        "resolution": resolution,
        "resolution_weight": resolution_weight,
        "quality_options": [
            {"value": value, "label": label, "weight": weight}
            for value, label, weight in quality_options
        ],
        "audio_codecs": [
            {"value": value, "label": label, "weight": weight}
            for value, label, weight in audio_codecs
        ],
        "upgrades": upgrades,
    }
    if target is not None:
        payload["target_score"] = target
    values = quality.validate_profile(payload)
    now = int(time.time())
    cursor = conn.execute(
        """
        INSERT INTO quality_profiles
            (name, resolutions, cutoff, upgrades, qualities, audio_mode,
             audio_languages, subtitle_mode, subtitle_languages, probe_required,
             created_at, resolution, resolution_weight, target_score, max_score,
             revision, updated_at, criteria_json)
        VALUES (?, ?, NULL, ?, ?, 'any', '[]', 'any', '[]', 1, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (name, json.dumps([resolution]), 1 if upgrades else 0,
         json.dumps(quality.legacy_quality_list(values)),
         now, values["resolution"], values["resolution_weight"],
         values["target_score"], values["max_score"], now, values["criteria_json"]),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_movie(movie_id: int):
    return library.get_movie(conn, movie_id)


def get_series(series_id: int):
    return library.get_series(conn, series_id)


def get_episode(episode_id: int):
    return library.get_episode(conn, episode_id)


def probe_audio(codec="eac3", title=None, channels=6, language="eng") -> dict:
    return {"codec": codec, "title": title or codec, "channels": channels,
            "language": language}


def probe_payload(resolution="1080p", *, audio=None, ok=True, source="web-dl"):
    return {
        "ok": ok,
        "resolution": resolution if ok else "unknown",
        "audio": list(audio or []),
        "video": [{"codec": "h264"}],
        "source": source,
        "size": 1_000_000,
        "probed_at": int(time.time()),
        **({} if ok else {"error": "probe failed"}),
    }


def scan_rows(work_id: int) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM bworker_scan WHERE work_id = ? ORDER BY id", (int(work_id),)
    )]


def candidate_rows_for(work_id: int) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM bworker_candidates WHERE work_id = ? ORDER BY id",
        (int(work_id),),
    )]


def downloads_for(*, movie_id=None, episode_id=None) -> list[dict]:
    if movie_id is not None:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE movie_id = ? ORDER BY id",
            (int(movie_id),),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE episode_id = ? ORDER BY id",
            (int(episode_id),),
        )
    return [dict(row) for row in rows]


def continuation_jobs(prefix: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM worker_jobs WHERE kind IN ('search_movie','search_series') "
        "AND dedupe_key LIKE ? ORDER BY id", (prefix + "%",)
    )]


# ===========================================================================
# 1. Catalog identity
# ===========================================================================
def test_identity_precedence() -> None:
    section("Catalog identity: TMDB -> IMDb -> title, never invented")
    source = add_source("m3u", "identity-source")
    movie = add_movie(title="Identity Movie", year=2020, tmdb_id=501,
                      imdb_id="tt0000501")
    tmdb_item = add_item(source, "vod", "tmdb-row", "Wrong Provider Name A",
                         tmdb_id="501")
    imdb_item = add_item(source, "vod", "imdb-row", "Wrong Provider Name B",
                         imdb_id="tt0000501")
    candidates = matching.movie_candidates(conn, get_movie(movie))
    eq("TMDB wins when both ids exist", [(c["id"], c["match_kind"]) for c in candidates],
       [(tmdb_item, "tmdb")])

    movie2 = add_movie(title="IMDb Only", year=2021, tmdb_id=502,
                       imdb_id="tt0000502")
    imdb_only = add_item(source, "vod", "imdb-only", "Completely Different",
                         imdb_id="tt0000502")
    candidates2 = matching.movie_candidates(conn, get_movie(movie2))
    eq("IMDb is the second precedence", [(c["id"], c["match_kind"]) for c in candidates2],
       [(imdb_only, "imdb")])

    movie3 = add_movie(title="Title Fallback", year=2022, tmdb_id=503,
                       imdb_id="tt0000503")
    title_item = add_item(source, "vod", "title-row",
                          "Title Fallback (2022) 1080p WEB-DL")
    candidates3 = matching.movie_candidates(conn, get_movie(movie3))
    eq("Title/year fallback when no catalog id exists",
       [(c["id"], c["match_kind"]) for c in candidates3],
       [(title_item, "title")])

    eq("identity_from_payload rejects junk",
       items.identity_from_payload({"tmdb_id": "abc", "imdb_id": "nope"}),
       (None, None))
    eq("normalise_tmdb_id rejects zero", items.normalise_tmdb_id("0"), None)
    eq("normalise_imdb_id lowercases",
       items.normalise_imdb_id("TT0133093"), "tt0133093")
    eq("normalise_imdb_id rejects arbitrary tvg-id",
       items.normalise_imdb_id("BBCOne.uk"), None)

    eq("Xtream detail ids from nested info",
       xtream.detail_identity({"info": {"tmdb_id": "603",
                                        "imdb_id": "tt0133093"}}),
       ("603", "tt0133093"))
    normalised = xtream.normalise_stream(
        "vod", {"stream_id": 9, "name": "VOD", "container_extension": "mkv",
                "movie_data": {"tmdb_id": "42", "imdb_id": "tt0000042"}})
    eq("Xtream list payload carries ids",
       (normalised["tmdb_id"], normalised["imdb_id"]), ("42", "tt0000042"))

    channels = list(m3u.parse_text(
        '#EXTM3U\n#EXTINF:-1 tvg-id="tt0133093" tmdb-id="603",The Matrix\n'
        "http://example.test/matrix.mkv\n"
        '#EXTINF:-1 tvg-id="BBCOne.uk",BBC One\nhttp://example.test/bbc\n'
    ))
    eq("M3U IMDb id from tvg-id", channels[0].imdb_id, "tt0133093")
    eq("M3U TMDB id attribute", channels[0].tmdb_id, "603")
    eq("M3U non-IMDb tvg-id is not an id", channels[1].imdb_id, None)
    eq("M3U missing tmdb stays missing", channels[1].tmdb_id, None)


def test_detail_lazy_enrichment() -> None:
    section("Catalog identity: detail-lazy enrichment never derives ids")
    source = add_source("xtream", "enrich-source")
    movie = add_movie(title="Enriched Movie", year=2019, tmdb_id=601,
                      imdb_id="tt0000601")
    item_id = add_item(source, "vod", "enrich-row",
                       "Enriched Movie (2019) 1080p WEB-DL")
    calls = []

    def enriching(conn, item, *, acquire_timeout=2.0):
        calls.append(int(item["id"]))
        items.apply_detail_identity(conn, int(item["id"]), tmdb_id="601")
        return "601", None

    items.enrich_item_identity = enriching
    try:
        first = acquisition.run_movie_iteration(
            conn, get_movie(movie), reason="auto", batch_size=5, probe_limit=1,
            now=1_000_000,
        )
        check("Enrichment requested for a title match", calls == [item_id],
              repr(calls))
        check("Identity mode change stops the iteration",
              first["need_more"] and first["chosen_candidate"] is None,
              repr(first.get("stage")))
        row = conn.execute("SELECT tmdb_id FROM items WHERE id = ?", (item_id,)).fetchone()
        eq("Enrichment fills the item id", row["tmdb_id"], "601")
        second = acquisition.run_movie_iteration(
            conn, get_movie(movie), reason="auto", batch_size=5, probe_limit=1,
            now=1_000_010,
        )
        candidates = candidate_rows_for(int(second["work_id"]))
        eq("Rescan is ID-first after enrichment",
           sorted({row["match_kind"] for row in candidates}), ["tmdb"])
        check("Enrichment is not repeated for the same item",
              calls == [item_id], repr(calls))
    finally:
        items.enrich_item_identity = fake_enrich


# ===========================================================================
# 2. Keyset paging and deduplication
# ===========================================================================
def test_keyset_paging() -> None:
    section("Candidate paging: keyset only, disjoint pages, dedupe")
    source = add_source("m3u", "keyset-source")
    for index in range(7):
        add_item(source, "vod", f"keyset-{index}",
                 f"Keyset Movie (2020) {index} 1080p WEB-DL", tmdb_id="777")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        first = items.catalog_page(conn, "vod", tmdb_id="777", limit=3)
        second = items.catalog_page(
            conn, "vod", tmdb_id="777",
            after_item_id=int(first["cursor"]), limit=3,
        )
    finally:
        conn.set_trace_callback(None)
    first_ids = [row["id"] for row in first["items"]]
    second_ids = [row["id"] for row in second["items"]]
    eq("First keyset page size", len(first_ids), 3)
    check("Pages do not overlap", not (set(first_ids) & set(second_ids)),
          f"{first_ids} / {second_ids}")
    check("Cursor advances", int(second["cursor"]) > int(first["cursor"]))
    check("No OFFSET in catalog paging",
          not any("OFFSET" in sql.upper() for sql in statements),
          str([sql for sql in statements if "OFFSET" in sql.upper()]))

    statements.clear()
    conn.set_trace_callback(statements.append)
    try:
        items.identity_catalog(conn, "vod", "tmdb", "777", limit=2)
    finally:
        conn.set_trace_callback(None)
    check("No OFFSET in identity lookup",
          not any("OFFSET" in sql.upper() for sql in statements))

    movie = add_movie(title="Dedupe Movie", year=2020, tmdb_id=778,
                      imdb_id="tt0000778")
    shared = add_item(source, "vod", "dedupe-one",
                      "Dedupe Movie (2020) 1080p WEB-DL", tmdb_id="778")
    add_item(source, "vod", "dedupe-two",
             "Dedupe Movie (2020) 720p WEB-DL", tmdb_id="778")
    # Two alternative titles both point at the same first catalog row.
    conn.execute("UPDATE movies SET alt_titles = ? WHERE id = ?",
                 (json.dumps(["Dedupe Movie", "Dedupe Film"]), movie))
    conn.commit()
    first_run = acquisition.run_movie_iteration(conn, get_movie(movie),
                                                batch_size=10, probe_limit=1,
                                                now=2_000_000)
    rows = [row for row in candidate_rows_for(int(first_run["work_id"]))
            if row["item_id"] == shared]
    eq("Duplicate selectors dedupe to one candidate", len(rows), 1)
    total = first_run["candidates"]["candidates"]
    acquisition.run_movie_iteration(conn, get_movie(movie), batch_size=10,
                                    probe_limit=1, now=2_000_010)
    eq("Re-running a page does not duplicate candidates",
       len(candidate_rows_for(int(first_run["work_id"]))), total)


# ===========================================================================
# 3. Batch and probe limits
# ===========================================================================
def test_batch_and_probe_limits() -> None:
    section("Iteration bounds: 25 discoveries and 3 probes")
    source = add_source("m3u", "bounds-source")
    movie = add_movie(title="Bounds Movie", year=2020, tmdb_id=801)
    reset_runtime()
    for index in range(30):
        add_item(source, "vod", f"bounds-{index}",
                 f"Bounds Movie (2020) 720p WEB-DL {index}", tmdb_id="801")
        PROBE_RESULTS[(source, str(index))] = probe_payload("720p")
    first = acquisition.run_movie_iteration(conn, get_movie(movie),
                                            now=3_000_000)
    eq("Default discovery batch is 25", first["candidates"]["candidates"], 25)
    check("At most three probes in one iteration", len(PROBE_CALLS) <= 3,
          str(len(PROBE_CALLS)))
    check("Discovery batch is reported in progress",
          first["progress"]["candidate_batch"] == 25,
          repr(first["progress"]))
    check("More work is signalled", first["need_more"] is True)
    check("A continuation job is queued",
          bool(continuation_jobs(f"acquisition:movie:{movie}:")),
          "no continuation job")

    second = acquisition.run_movie_iteration(conn, get_movie(movie),
                                             now=3_000_010)
    eq("Second iteration discovers the remaining five",
       second["candidates"]["candidates"], 30)
    check("Probe ceiling holds on the second iteration", len(PROBE_CALLS) <= 6,
          str(len(PROBE_CALLS)))
    remotes = [row["provider_remote_id"] for row in
               candidate_rows_for(int(second["work_id"]))]
    eq("Candidate identity remains unique", len(remotes), len(set(remotes)))

    source2 = add_source("m3u", "override-source")
    override_movie = add_movie(title="Override Movie", year=2021, tmdb_id=802,
                               candidate_batch_size=2, probe_batch_size=1)
    for index in range(5):
        add_item(source2, "vod", f"override-{index}",
                 f"Override Movie (2021) 1080p WEB-DL {index}", tmdb_id="802")
    reset_runtime()
    override = acquisition.run_movie_iteration(conn, get_movie(override_movie),
                                               now=3_000_020)
    eq("Per-item candidate batch override", override["candidates"]["candidates"], 2)
    eq("Per-item probe batch override", len(PROBE_CALLS), 1)


# ===========================================================================
# 4. Epoch and fingerprint invalidation
# ===========================================================================
def test_epoch_and_fingerprint() -> None:
    section("Scan lifecycle: epoch, fingerprint and profile revision")
    source = add_source("m3u", "epoch-source")
    profile = add_profile("Epoch Profile")
    movie = add_movie(title="Epoch Movie", year=2020, tmdb_id=901,
                      profile_id=profile)
    reset_runtime()
    for index in range(4):
        add_item(source, "vod", f"epoch-{index}",
                 f"Epoch Movie (2020) 720p WEB-DL {index}", tmdb_id="901")
        PROBE_RESULTS[(source, f"epoch-{index}")] = probe_payload("720p")
    first = acquisition.run_movie_iteration(conn, get_movie(movie),
                                            batch_size=2, now=4_000_000)
    work_id = int(first["work_id"])
    scans = scan_rows(work_id)
    eq("One scan created", len(scans), 1)
    scan_id = int(scans[0]["id"])
    old_epoch = scans[0]["epoch"]
    eq("Candidates discovered before invalidation", first["candidates"]["candidates"], 2)

    # A provider sync changes the epoch.
    conn.execute("UPDATE sources SET last_synced_at = ? WHERE id = ?",
                 (int(time.time()) + 60, source))
    conn.commit()
    second = acquisition.run_movie_iteration(conn, get_movie(movie),
                                             batch_size=2, now=4_000_010)
    scans = scan_rows(work_id)
    active = [row for row in scans if row["status"] == "active"]
    eq("New catalogue epoch starts a new active scan", len(active), 1)
    eq("Old scan is invalidated", scans[0]["status"], "invalidated")
    check("Epoch changed", active[0]["epoch"] != old_epoch)
    new_scan_id = int(active[0]["id"])
    check("New scan id", new_scan_id != scan_id)
    new_rows = [row for row in candidate_rows_for(work_id)
                if int(row["scan_id"]) == new_scan_id]
    old_rows = [row for row in candidate_rows_for(work_id)
                if int(row["scan_id"]) == scan_id]
    eq("Candidates are scoped to the new epoch", len(new_rows), 2)
    check("Old epoch candidates are all retired",
          old_rows and all(row["status"] == "rejected" for row in old_rows),
          repr([(row["status"], row["scan_id"]) for row in old_rows]))

    # A profile revision bumps the fingerprint.
    fingerprint_before = active[0]["query_fingerprint"]
    conn.execute("UPDATE quality_profiles SET revision = revision + 1 WHERE id = ?",
                 (profile,))
    conn.commit()
    third = acquisition.run_movie_iteration(conn, get_movie(movie),
                                            batch_size=2, now=4_000_020)
    active = [row for row in scan_rows(work_id) if row["status"] == "active"]
    eq("Profile revision keeps one active scan", len(active), 1)
    eq("Fingerprint reset reuses the epoch scan", int(active[0]["id"]), new_scan_id)
    check("Fingerprint changed", active[0]["query_fingerprint"] != fingerprint_before)
    reset_rows = [row for row in candidate_rows_for(work_id)
                  if int(row["scan_id"]) == new_scan_id]
    eq("Reset scan recycled the previous epoch candidate ids",
       len(reset_rows), third["candidates"]["candidates"])
    eq("Reset scan started a fresh pool of two", len(reset_rows), 2)
    check("Recycled candidates are pending or probed",
          all(row["status"] in ("pending", "probing", "probed")
              for row in reset_rows),
          repr([row["status"] for row in reset_rows]))


# ===========================================================================
# 5. Scoring and selection
# ===========================================================================
def _audio_profile(name: str, *, target: int, upgrades=True) -> int:
    return add_profile(
        name,
        quality_options=(("web-dl", "WEB-DL", 10),),
        audio_codecs=(("atmos", "Dolby Atmos", 10), ("ac3", "AC-3", 5)),
        target=target, upgrades=upgrades,
    )


def test_target_over_fallback() -> None:
    section("Selection: target_met wins over an earlier fallback")
    source = add_source("m3u", "target-source")
    profile = add_profile(
        "Strict Target", quality_options=(("web-dl", "WEB-DL", 10),),
        audio_codecs=(("atmos", "Dolby Atmos", 10),), target=30,
    )
    movie = add_movie(title="Target Movie", year=2020, tmdb_id=1001,
                      profile_id=profile)
    fallback = add_item(source, "vod", "target-fallback",
                        "Target Movie (2020) 720p WEB-DL", tmdb_id="1001")
    plain = add_item(source, "vod", "target-plain",
                     "Target Movie (2020) 1080p WEB-DL", tmdb_id="1001")
    best = add_item(source, "vod", "target-best",
                    "Target Movie (2020) 1080p WEB-DL", tmdb_id="1001")
    reset_runtime()
    PROBE_RESULTS[(source, "target-fallback")] = probe_payload(
        "720p", audio=[probe_audio("eac3", "Atmos")])
    PROBE_RESULTS[(source, "target-plain")] = probe_payload(
        "1080p", audio=[probe_audio("eac3")])
    PROBE_RESULTS[(source, "target-best")] = probe_payload(
        "1080p", audio=[probe_audio("eac3", "Atmos")])
    outcome = acquisition.run_movie_iteration(conn, get_movie(movie),
                                              now=5_000_000)
    check("A target release is queued", outcome["queued"] is True,
          repr(outcome.get("message")))
    download = outcome["download"]
    eq("Target beats fallback even when fallback was discovered first",
       download["provider_remote_id"], "target-best")
    details = json.loads(download["score_details"])
    eq("Download score details record target_met", details["target_met"], True)
    eq("Target score", details["score"], 30)
    eq("Target exact resolution", details["exact_resolution"], True)
    eq("Auto download max_attempts", download["max_attempts"], 1)
    eq("Auto download reason", download["reason"], "auto")
    chosen = [row for row in candidate_rows_for(int(outcome["work_id"]))
              if row["status"] == "selected"]
    eq("Chosen candidate recorded", len(chosen), 1)
    eq("Chosen candidate id", int(chosen[0]["item_id"]), best)
    eq("Unchosen candidates remain probed",
       sorted(row["status"] for row in candidate_rows_for(int(outcome["work_id"]))
              if row["item_id"] in (fallback, plain)),
       ["probed", "probed"])


def test_fallback_only_after_exhaustion() -> None:
    section("Selection: fallback waits for the candidate pool to exhaust")
    source = add_source("m3u", "fallback-source")
    profile = add_profile("Fallback Profile", upgrades=True,
                          quality_options=(("web-dl", "WEB-DL", 10),),
                          target=20)
    movie = add_movie(title="Fallback Movie", year=2020, tmdb_id=1101,
                      profile_id=profile)
    reset_runtime()
    count = 60
    for index in range(count):
        add_item(source, "vod", f"fallback-{index}",
                 f"Fallback Movie (2020) 720p WEB-DL {index}", tmdb_id="1101")
    reset_runtime()
    first = acquisition.run_movie_iteration(conn, get_movie(movie),
                                            now=6_000_000)
    check("No fallback while candidates remain pending",
          first["queued"] is False and first["chosen_candidate"] is None,
          repr(first.get("message")))
    outcome = first
    for step in range(30):
        outcome = acquisition.run_movie_iteration(
            conn, get_movie(movie), now=6_000_010 + step,
        )
        if outcome["queued"]:
            break
    check("Fallback selected after exhaustion", outcome["queued"] is True,
          repr(outcome.get("message")))
    if outcome["queued"]:
        eq("Fallback resolution", outcome["download"]["resolution"], "720p")
        details = json.loads(outcome["download"]["score_details"])
        eq("Fallback is marked for upgrade", details["needs_upgrade"], True)
        eq("Fallback is not target_met", details["target_met"], False)
        eq("Fallback only after every candidate was probed",
           outcome["candidates"]["pending"], 0)


def test_highest_score() -> None:
    section("Selection: highest score wins among target candidates")
    source = add_source("m3u", "highest-source")
    profile = add_profile(
        "Highest Profile", audio_codecs=(("atmos", "Dolby Atmos", 10),
                                          ("ac3", "AC-3", 5)), target=20,
    )
    movie = add_movie(title="Highest Movie", year=2020, tmdb_id=1201,
                      profile_id=profile)
    add_item(source, "vod", "highest-ac3",
             "Highest Movie (2020) 1080p WEB-DL", tmdb_id="1201")
    add_item(source, "vod", "highest-atmos",
             "Highest Movie (2020) 1080p WEB-DL", tmdb_id="1201")
    reset_runtime()
    PROBE_RESULTS[(source, "highest-ac3")] = probe_payload(
        "1080p", audio=[probe_audio("ac3")])
    PROBE_RESULTS[(source, "highest-atmos")] = probe_payload(
        "1080p", audio=[probe_audio("eac3", "Atmos")])
    outcome = acquisition.run_movie_iteration(conn, get_movie(movie),
                                              probe_limit=3, now=7_000_000)
    check("Highest-scoring target is queued", outcome["queued"] is True,
          repr(outcome.get("message")))
    if outcome["queued"]:
        eq("Highest score chosen", outcome["download"]["provider_remote_id"],
           "highest-atmos")
        eq("Chosen score", json.loads(outcome["download"]["score_details"])["score"], 30)


def test_never_redownload_or_downgrade() -> None:
    section("Selection: current identity, target and upgrade-only-on-better")
    source = add_source("m3u", "current-source")
    profile = _audio_profile("Upgrade Profile", target=30)
    current_media = json.dumps(probe_payload(
        "1080p", audio=[probe_audio("eac3", "Atmos")]))
    movie = add_movie(title="Current Movie", year=2020, tmdb_id=1301,
                      profile_id=profile, file_path="/media/movie.mkv",
                      main_source_id=source, main_remote_id="current-best",
                      media_info=current_media)
    add_item(source, "vod", "current-best",
             "Current Movie (2020) 1080p WEB-DL", tmdb_id="1301")
    reset_runtime()
    PROBE_RESULTS[(source, "current-best")] = probe_payload(
        "1080p", audio=[probe_audio("eac3", "Atmos")])
    outcome = acquisition.run_movie_iteration(conn, get_movie(movie),
                                              now=8_000_000)
    eq("Current target_met identity is never selected",
       downloads_for(movie_id=movie), [])
    check("Current target keeps the file", outcome["stage"] == "awaiting_upgrade",
          repr(outcome.get("stage")))

    # A better candidate than the current eligible file is an upgrade.
    movie2 = add_movie(title="Upgrade Movie", year=2020, tmdb_id=1302,
                       profile_id=profile, file_path="/media/upgrade.mkv",
                       main_source_id=source, main_remote_id="old-file",
                       media_info=json.dumps(probe_payload(
                           "1080p", audio=[probe_audio("ac3")])))
    add_item(source, "vod", "old-file",
             "Upgrade Movie (2020) 1080p WEB-DL", tmdb_id="1302")
    add_item(source, "vod", "equal-file",
             "Upgrade Movie (2020) 1080p WEB-DL", tmdb_id="1302")
    reset_runtime()
    PROBE_RESULTS[(source, "old-file")] = probe_payload(
        "1080p", audio=[probe_audio("ac3")])
    PROBE_RESULTS[(source, "equal-file")] = probe_payload(
        "1080p", audio=[probe_audio("ac3")])
    equal = acquisition.run_movie_iteration(conn, get_movie(movie2), now=8_000_010)
    check("Equal score is not an upgrade",
          equal["queued"] is False and not downloads_for(movie_id=movie2),
          repr(equal.get("message")))

    add_item(source, "vod", "better-file",
             "Upgrade Movie (2020) 1080p WEB-DL", tmdb_id="1302")
    PROBE_RESULTS[(source, "better-file")] = probe_payload(
        "1080p", audio=[probe_audio("eac3", "Atmos")])
    best = acquisition.run_movie_iteration(conn, get_movie(movie2), now=8_000_020)
    check("Strictly better score upgrades", best["queued"] is True,
          repr(best.get("message")))
    if best["queued"]:
        eq("Upgrade reason", best["download"]["reason"], "upgrade")
        eq("Upgraded candidate", best["download"]["provider_remote_id"], "better-file")

    # Upgrades disabled: even a lower-scoring current file is kept.
    movie3 = add_movie(title="Locked Movie", year=2020, tmdb_id=1303,
                       profile_id=add_profile("Locked Profile", upgrades=False,
                                              target=20),
                       file_path="/media/locked.mkv", main_source_id=source,
                       main_remote_id="locked-old",
                       media_info=json.dumps(probe_payload(
                           "1080p", audio=[probe_audio("ac3")])))
    add_item(source, "vod", "locked-old",
             "Locked Movie (2020) 1080p WEB-DL", tmdb_id="1303")
    add_item(source, "vod", "locked-better",
             "Locked Movie (2020) 1080p WEB-DL", tmdb_id="1303")
    reset_runtime()
    PROBE_RESULTS[(source, "locked-old")] = probe_payload(
        "1080p", audio=[probe_audio("ac3")])
    PROBE_RESULTS[(source, "locked-better")] = probe_payload(
        "1080p", audio=[probe_audio("eac3", "Atmos")])
    locked = acquisition.run_movie_iteration(conn, get_movie(movie3), now=8_000_030)
    check("Upgrades disabled keeps the current file",
          locked["queued"] is False and not downloads_for(movie_id=movie3),
          repr(locked.get("message")))


def test_stale_candidates() -> None:
    section("Selection: stale catalog rows are rejected")
    source = add_source("m3u", "stale-source")
    profile = add_profile("Stale Profile", target=30,
                          audio_codecs=(("atmos", "Dolby Atmos", 10),))
    movie = add_movie(title="Stale Movie", year=2020, tmdb_id=1401,
                      profile_id=profile, file_path="/media/stale.mkv",
                      main_source_id=source, main_remote_id="already-current",
                      media_info=json.dumps(probe_payload(
                          "1080p", audio=[probe_audio("eac3", "Atmos")])))
    add_item(source, "vod", "stale-one",
             "Stale Movie (2020) 1080p WEB-DL", tmdb_id="1401")
    add_item(source, "vod", "stale-two",
             "Stale Movie (2020) 1080p WEB-DL", tmdb_id="1401")
    reset_runtime()
    PROBE_RESULTS[(source, "stale-one")] = probe_payload(
        "1080p", audio=[probe_audio("eac3", "Atmos")])
    PROBE_RESULTS[(source, "stale-two")] = probe_payload(
        "1080p", audio=[probe_audio("eac3", "Atmos")])
    first = acquisition.run_movie_iteration(conn, get_movie(movie),
                                            probe_limit=1, now=9_000_000)
    work_id = int(first["work_id"])
    candidate = next(row for row in candidate_rows_for(work_id)
                     if row["provider_remote_id"] == "stale-one")
    conn.execute("DELETE FROM items WHERE id = ?", (int(candidate["item_id"]),))
    conn.commit()
    second = acquisition.run_movie_iteration(conn, get_movie(movie), now=9_000_010)
    stale = conn.execute(
        "SELECT status FROM bworker_candidates WHERE id = ?",
        (int(candidate["id"]),),
    ).fetchone()
    eq("Deleted item candidate is rejected", stale["status"], "rejected")
    eq("No stale candidate is downloaded", downloads_for(movie_id=movie), [])
    eq("Current target file is kept after invalidation", second["stage"],
       "awaiting_upgrade")


# ===========================================================================
# 6. Episodes
# ===========================================================================
def test_episode_pipeline() -> None:
    section("Episodes: container expansion, target episode, handoff")
    source = add_source("xtream", "episode-source")
    series = add_series(title="Episode Series", tmdb_id=1501)
    episode_two = add_episode(series, 1, 2)
    add_item(source, "series", "provider-series-1",
             "Episode Series (2020) 1080p WEB-DL", tmdb_id="1501")
    reset_runtime()
    PROVIDER_EPISODES[(source, "provider-series-1")] = [
        {"stream_id": "11", "season": 1, "episode": 1, "container_ext": "mkv",
         "title": "Pilot"},
        {"stream_id": "22", "season": 1, "episode": 2, "container_ext": "mkv",
         "title": "Second"},
    ]
    PROBE_RESULTS[(source, "provider-series-1", "22")] = probe_payload(
        "1080p", audio=[probe_audio("eac3")])
    outcome = acquisition.run_episode_iteration(
        conn, get_series(series), get_episode(episode_two), now=10_000_000,
    )
    check("Episode download queued", outcome["queued"] is True,
          repr(outcome.get("message")))
    if outcome["queued"]:
        download = outcome["download"]
        eq("Correct provider stream", str(download["episode_stream_id"]), "22")
        eq("Correct season", int(download["season"]), 1)
        eq("Correct episode", int(download["episode"]), 2)
        eq("Episode max_attempts", int(download["max_attempts"]), 1)
        eq("Episode download reason", download["reason"], "auto")
        eq("Probed only the target stream",
           [call for call in PROBE_CALLS if call[0] == "episode"],
           [("episode", (source, "provider-series-1", "22"))])
        details = json.loads(download["score_details"])
        eq("Episode score details target_met", details["target_met"], True)
    candidates = candidate_rows_for(int(outcome["work_id"]))
    statuses = {(row["episode_stream_id"], row["status"]) for row in candidates}
    check("Container is expanded and retired",
          ("", "rejected") in statuses, repr(statuses))
    check("Episode stream is selected", ("22", "selected") in statuses,
          repr(statuses))
    work = conn.execute("SELECT * FROM bworker_work WHERE id = ?",
                        (int(outcome["work_id"]),)).fetchone()
    eq("Work best source recorded", int(work["best_source_id"]), source)
    eq("Work best episode stream recorded", work["best_episode_stream_id"], "22")
    check("Work profile revision recorded", work["profile_revision"] is not None)


def test_episode_library_hook() -> None:
    section("Episodes: library.search_series auto hook")
    source = add_source("xtream", "episode-hook-source")
    series = add_series(title="Hook Series", tmdb_id=1601)
    episode = add_episode(series, 1, 1)
    add_item(source, "series", "hook-series",
             "Hook Series (2020) 1080p WEB-DL", tmdb_id="1601")
    reset_runtime()
    PROVIDER_EPISODES[(source, "hook-series")] = [
        {"stream_id": "31", "season": 1, "episode": 1, "container_ext": "mkv",
         "title": "One"},
    ]
    PROBE_RESULTS[(source, "hook-series", "31")] = probe_payload("1080p")
    result = library.search_series(conn, get_series(series),
                                   episode_id=episode, reason="auto")
    eq("Library episode hook reports one queue", result["queued"], 1)
    check("Library episode hook returns the download", bool(result.get("download")))
    row = conn.execute("SELECT * FROM series WHERE id = ?", (series,)).fetchone()
    eq("Series search message stored", row["search_message"],
       result["message"])


# ===========================================================================
# 7. Manual paths
# ===========================================================================
def test_manual_search_and_grab() -> None:
    section("Manual paths: bounded search, probe, profile and score")
    source = add_source("m3u", "manual-source")
    profile = add_profile("Manual Profile", resolution="2160p",
                          quality_options=(("bluray", "BluRay", 10),),
                          upgrades=False)
    movie = add_movie(title="Manual Movie", year=2020, tmdb_id=1701,
                      profile_id=profile)
    eligible = add_item(source, "vod", "manual-4k",
                        "Manual Movie (2020) 2160p BluRay", tmdb_id="1701",
                        request_headers={"Authorization": "secret"})
    ineligible = add_item(source, "vod", "manual-1080",
                          "Manual Movie (2020) 1080p WEB-DL", tmdb_id="1701")
    reset_runtime()
    PROBE_RESULTS[(source, "manual-4k")] = probe_payload("2160p")
    PROBE_RESULTS[(source, "manual-1080")] = probe_payload("1080p")
    search = acquisition.bounded_movie_search(conn, get_movie(movie))
    eq("Bounded search probes matching releases", len(search["releases"]), 2)
    eq("Bounded search chooses the allowed release",
       search["choice"]["remote_id"], "manual-4k")
    eq("Bounded search exposes match mode", search["match_mode"], "tmdb")
    allowed = {row["remote_id"]: row["allowed"] for row in search["releases"]}
    eq("Ineligible release is marked disallowed", allowed["manual-1080"], False)
    check("Request headers are removed from manual releases",
          all("request_headers" not in row for row in search["releases"]))

    reset_runtime()
    PROBE_RESULTS[(source, "manual-4k")] = probe_payload("2160p")
    PROBE_RESULTS[(source, "manual-1080")] = probe_payload("1080p")
    result = library.search_movie(conn, get_movie(movie), reason="manual")
    check("Manual search queues a download", result["queued"] is True)
    download = result["download"]
    eq("Manual search chose the allowed release",
       download["provider_remote_id"], "manual-4k")
    eq("Manual download keeps downloader max_attempts", download["max_attempts"], 3)
    details = json.loads(download["score_details"])
    eq("Manual download has score details", details["eligible"], True)
    check("Manual result does not leak headers",
          "request_headers" not in result["release"])

    releases = library.manual_movie_releases(conn, movie)
    allowed = {row["remote_id"]: row["allowed"] for row in releases}
    eq("Manual release list probes the eligible release", allowed["manual-4k"], True)
    eq("Manual release list rejects the ineligible release",
       allowed["manual-1080"], False)

    conn.execute("DELETE FROM downloads WHERE movie_id = ?", (movie,))
    conn.execute("DELETE FROM worker_jobs WHERE payload LIKE ?",
                 (f'%"movie_id": {movie}%',))
    conn.commit()
    try:
        library.grab_movie(conn, movie, source, "manual-1080")
    except ValueError as exc:
        check("Manual grab enforces the profile", "not allowed" in str(exc).lower(),
              str(exc))
    else:
        check("Manual grab enforces the profile", False, "did not raise")
    grab = library.grab_movie(conn, movie, source, "manual-4k")
    eq("Manual grab queues the eligible release", grab["provider_remote_id"],
       "manual-4k")
    eq("Manual grab annotates the score",
       json.loads(grab["score_details"])["eligible"], True)

    # ------------------------------------------------ manual candidate browser
    facts = probe_payload(
        "2160p",
        audio=[{"codec": "eac3", "language": "eng"},
               {"codec": "aac", "language": "spa"}],
    )
    facts["subtitles"] = [
        {"codec": "subrip", "language": "eng"},
        {"codec": "hdmv_pgs_subtitle", "language": "spa"},
    ]
    facts["audio_languages"] = ["eng", "spa"]
    facts["subtitle_languages"] = ["eng", "spa"]
    conn.execute(
        "DELETE FROM stream_probes WHERE source_id = ? AND provider_remote_id IN "
        "('manual-4k', 'manual-1080')", (source,),
    )
    conn.commit()
    store_probe(source, "manual-4k", facts)
    view = library.movie_candidates_view(
        conn, get_movie(movie), limit=10, query="Manual Movie",
    )
    eq("Candidate browser returns every matching movie", view["total"], 2)
    probed = next(row for row in view["candidates"] if row["probe_ok"])
    eq("Candidate browser reads size", probed["size"], 1_000_000)
    eq("Candidate browser reads resolution", probed["resolution"], "2160p")
    eq("Candidate browser reads video codec", probed["video_codecs"], ["h264"])
    eq("Candidate browser reads audio codecs", probed["audio_codecs"],
       ["eac3", "aac"])
    eq("Candidate browser reads subtitle codecs", probed["subtitle_codecs"],
       ["subrip", "hdmv_pgs_subtitle"])
    eq("Candidate browser reads audio languages", probed["audio_languages"],
       ["eng", "spa"])
    eq("Candidate browser reads subtitle languages", probed["subtitle_languages"],
       ["eng", "spa"])

    # Identity-only matching is right for the detail workspace, but "all
    # candidates" must also reveal a same-title release that has no TMDB id.
    add_item(source, "vod", "manual-title-only",
             "Manual Movie (2020) 1080p WEBRip")
    identity_view = library.movie_candidates_view(
        conn, get_movie(movie), limit=10,
    )
    eq("Identity workspace keeps the precise match set", identity_view["total"], 2)
    broad_view = library.movie_candidates_view(
        conn, get_movie(movie), limit=10, all_matches=True,
    )
    eq("All candidates include title-only releases", broad_view["total"], 3)
    check("All candidates expose the title-only release",
          any(row["remote_id"] == "manual-title-only"
              for row in broad_view["candidates"]))

    # A candidate the automatic search never reached can be selected in the
    # browser; the selection probes it first and refuses if ffprobe fails.
    conn.execute("DELETE FROM stream_probes WHERE source_id = ?", (source,))
    conn.commit()
    original_probe = probe.probe_movie_release

    def storing_probe(conn_arg, release, **kwargs):
        result = fake_probe_movie(conn_arg, release, **kwargs)
        store_probe(source, "manual-1080", result.get("probe") or {})
        return result

    probe.probe_movie_release = storing_probe
    try:
        selected = library.select_movie_candidate(
            conn, movie, source, "manual-1080", "only", grab=False,
            probe_if_needed=True,
        )
    finally:
        probe.probe_movie_release = original_probe
    check("Manual selection probes an unprobed candidate",
          selected["candidate"]["probe_ok"])
    eq("Manual selection keeps the probed facts",
       selected["candidate"]["resolution"], "1080p")
    conn.execute("UPDATE movies SET candidate_source_id = NULL, "
                 "candidate_remote_id = NULL, candidate_mode = NULL, "
                 "candidate_name = NULL, candidate_selected_at = NULL WHERE id = ?",
                 (movie,))
    conn.commit()


def test_manual_episode_paths() -> None:
    section("Manual paths: episode releases and grab")
    source = add_source("xtream", "manual-episode-source")
    profile = add_profile("Manual Episode Profile", resolution="2160p",
                          quality_options=(("bluray", "BluRay", 10),),
                          upgrades=False)
    series = add_series(title="Manual Episode Series", tmdb_id=1801,
                        profile_id=profile)
    episode = add_episode(series, 1, 1)
    add_item(source, "series", "manual-series",
             "Manual Episode Series (2020) 2160p BluRay", tmdb_id="1801")
    reset_runtime()
    PROVIDER_EPISODES[(source, "manual-series")] = [
        {"stream_id": "41", "season": 1, "episode": 1, "container_ext": "mkv",
         "title": "One"},
    ]
    PROBE_RESULTS[(source, "manual-series", "41")] = probe_payload("1080p")
    releases = library.manual_episode_releases(conn, series, episode)
    eq("Manual episode release is listed", len(releases), 1)
    eq("Manual episode release is rejected by the 4K profile",
       releases[0]["allowed"], False)
    try:
        library.grab_episode(conn, series, episode, source, "manual-series", "41")
    except ValueError as exc:
        check("Manual episode grab enforces the profile",
              "not allowed" in str(exc).lower(), str(exc))
    else:
        check("Manual episode grab enforces the profile", False, "did not raise")

    episode_facts = probe_payload(
        "1080p",
        audio=[{"codec": "aac", "language": "eng"}],
    )
    episode_facts["subtitles"] = [{"codec": "subrip", "language": "eng"}]
    episode_facts["audio_languages"] = ["eng"]
    episode_facts["subtitle_languages"] = ["eng"]
    store_probe(source, "manual-series", episode_facts, kind="series",
                stream_id="41")
    view = library.episode_candidates_view(
        conn, get_series(series), get_episode(episode), limit=10,
    )
    eq("Episode browser lists every provider episode", view["total"], 1)
    candidate = view["candidates"][0]
    check("Episode browser reads the cached probe", candidate["probe_ok"])
    eq("Episode browser reads file size", candidate["size"], 1_000_000)
    eq("Episode browser reads resolution", candidate["resolution"], "1080p")
    eq("Episode browser reads video codec", candidate["video_codecs"], ["h264"])
    eq("Episode browser reads audio codec", candidate["audio_codecs"], ["aac"])
    eq("Episode browser reads audio language", candidate["audio_languages"],
       ["eng"])
    eq("Episode browser reads subtitle codec", candidate["subtitle_codecs"],
       ["subrip"])
    eq("Episode browser reads subtitle language",
       candidate["subtitle_languages"], ["eng"])
    probe_result = library.probe_episode_candidate(
        conn, series, episode, source, "manual-series", "41",
    )
    check("Episode candidate probe returns facts", probe_result["probe"]["ok"])
    forced = library.grab_episode(
        conn, series, episode, source, "manual-series", "41", force=True,
    )
    eq("Forced episode grab is labelled as a manual override",
       forced["reason"], "manual-override")


# ===========================================================================
# main
# ===========================================================================
def main() -> int:
    db.init_db()
    global conn
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    conn = db.connect()
    tests = [
        test_identity_precedence,
        test_detail_lazy_enrichment,
        test_keyset_paging,
        test_batch_and_probe_limits,
        test_epoch_and_fingerprint,
        test_target_over_fallback,
        test_fallback_only_after_exhaustion,
        test_highest_score,
        test_never_redownload_or_downgrade,
        test_stale_candidates,
        test_episode_pipeline,
        test_episode_library_hook,
        test_manual_search_and_grab,
        test_manual_episode_paths,
    ]
    for test in tests:
        try:
            test()
        except Exception:
            print(f"FAIL  {test.__name__} raised")
            traceback.print_exc()
            _failures.append((test.__name__, "exception"))
    print(f"\n{_passed} checks passed, {len(_failures)} failed")
    for label, detail in _failures:
        print(f"  FAIL  {label}" + (f": {detail}" if detail else ""))
    if _failures:
        print(f"\nTemp data: {_TMP}")
    try:
        conn.close()
    except Exception:
        pass
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
