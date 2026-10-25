"""The bounded candidate acquisition engine.

One iteration does three things and returns:

1. discover at most ``candidate_batch_size`` new matches using keyset paging;
2. expand at most three series containers into episode-stream candidates;
3. probe at most ``probe_batch_per_loop`` pending candidates, one ffprobe at a
   time, checking the pause authority between probes.

It then either hands one release to the downloader or reports ``need_more`` so
the caller can continue later. A single call never scans or probes the whole
catalog, cannot select a stale catalog row, and never re-downloads the source
identity already backing the current file.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

from .. import db, items, matching, probe, quality
from .. import bworker_store as store
from . import state

DEFAULT_BATCH = 25
DEFAULT_PROBE = 3
EPISODE_EXPANSION_LIMIT = 3
TITLE_MIN_SCORE = 0.85
MANUAL_SCAN_LIMIT = 100
MANUAL_QUERY_MIN_SCORE = 0.45
MANUAL_TITLE_MIN_SCORE = 0.65
MAX_BATCH = 200
MAX_PROBE = 10
MAX_DISCOVERY_PAGES = 6

#: Detail lookups are process-lifetime best effort. Keeping the set bounded
#: prevents a catalog full of id-less rows from growing memory forever.
_ENRICH_ATTEMPTS: dict[tuple[int, str, str], float] = {}
_ENRICH_MAX = 512


# --------------------------------------------------------------------- helpers
def _now(now: int | None = None) -> int:
    return int(now if now is not None else time.time())


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    keys = row.keys() if hasattr(row, "keys") else row
    return row[key] if key in keys else default


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _setting_int(conn, key: str, default: int, *, minimum: int = 1,
                 maximum: int = MAX_BATCH) -> int:
    try:
        value = int(float(db.get_setting(conn, key, str(default)) or default))
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(value, maximum))


def _profile(conn, profile_id):
    from .. import library
    return library._profile(conn, profile_id)


def _stop_requested(conn, stop: Callable[[], bool] | None,
                    check_pause: bool) -> bool:
    if stop is not None:
        try:
            if stop():
                return True
        except Exception:
            pass
    if check_pause:
        try:
            if store.get_state(conn)["paused"]:
                return True
        except Exception:
            pass
    return False


def _search_interval_seconds(conn, entity_type: str, row: Mapping[str, Any]) -> int:
    hours = _get(row, "search_interval_hours")
    try:
        hours = float(hours) if hours is not None else None
    except (TypeError, ValueError):
        hours = None
    if hours is None:
        key = ("movie_search_interval_hours" if entity_type == "movie"
               else "series_search_interval_hours")
        fallback = 6 if entity_type == "movie" else 4
        try:
            hours = float(db.get_setting(conn, key, str(fallback)) or fallback)
        except (TypeError, ValueError):
            hours = float(fallback)
    if hours <= 0:
        return 0
    return max(60, int(hours * 3600))


def _work_for(conn, entity_type: str, entity_id: int,
              work: Mapping[str, Any] | None) -> dict | None:
    if work is not None:
        return dict(work)
    if entity_type == "movie":
        key = store.movie_work_key(entity_id)
    else:
        key = store.episode_work_key(entity_id)
    row = store.get_work_by_key(conn, key)
    if row is not None:
        return row
    if entity_type == "movie":
        store.ensure_movie_work(conn, entity_id)
    else:
        store.ensure_episode_work(conn, entity_id)
    return store.get_work_by_key(conn, key)


def _emit(on_event, **fields) -> None:
    """Best-effort per-candidate activity callback; never break acquisition."""
    if on_event is None:
        return
    try:
        on_event(fields)
    except Exception:
        pass


def _candidate_facts(payload: Mapping[str, Any]) -> str:
    bits = []
    if payload.get("resolution"):
        bits.append(str(payload["resolution"]))
    if payload.get("video_codec"):
        bits.append(str(payload["video_codec"]))
    if payload.get("audio_codecs"):
        bits.append("audio " + "/".join(payload["audio_codecs"]))
    if payload.get("subtitle_codecs"):
        bits.append("subs " + "/".join(payload["subtitle_codecs"]))
    return " · ".join(bits)


def _empty_outcome(stage: str, message: str, *, need_more: bool = False,
                   exhausted: bool = False, stopped: bool = False,
                   next_run_at: int | None = None,
                   work: Mapping[str, Any] | None = None) -> dict:
    return {
        "stage": stage,
        "cursor": None,
        "candidates": {},
        "chosen_candidate": None,
        "need_more": bool(need_more),
        "exhausted": bool(exhausted),
        "stopped": bool(stopped),
        "next_run_at": next_run_at,
        "message": message,
        "progress": {},
        "queued": False,
        "download": None,
        "release": None,
        "work_id": int(work["id"]) if work else None,
    }


def _ctx_counts(conn, scan) -> dict:
    if scan is None:
        return {}
    counts = state.scan_counts(conn, int(scan["id"]))
    counts["seen"] = int(scan.get("candidates_seen") or 0)
    counts["matched"] = int(counts.get("candidates") or 0)
    counts["probed_total"] = int(scan.get("candidates_probed") or 0)
    return counts


def _progress(scan, counts: dict) -> dict:
    if scan is None:
        return dict(counts)
    return {
        "scan_id": int(scan["id"]),
        "epoch": scan.get("epoch"),
        "fingerprint": scan.get("query_fingerprint"),
        "cursor": scan.get("cursor"),
        "exhausted": bool(scan.get("exhausted")),
        "candidate_batch": counts.get("candidates", 0),
        "pending": counts.get("pending", 0),
        "probed": counts.get("probed", 0),
        "eligible": counts.get("eligible", 0),
        "target_met": counts.get("target_met", 0),
        "candidates_seen": counts.get("seen", 0),
        "candidates_matched": counts.get("candidates_matched", 0),
        "candidates_probed": counts.get("probed_total", 0),
    }


# ------------------------------------------------------------------ discovery
def _identity_fields(row: Mapping[str, Any]) -> tuple[str | None, str | None]:
    tmdb = items.normalise_tmdb_id(_get(row, "tmdb_id"))
    imdb = items.normalise_imdb_id(_get(row, "imdb_id"))
    return tmdb, imdb


def _maybe_enrich(conn, ctx: dict, row: Mapping[str, Any]) -> bool:
    """Bounded detail-lazy identity enrichment for one title match.

    Returns True when the resolved match mode changed (the caller then stops
    this iteration and lets the next one reset the scan to ID-first).
    """
    if ctx["mode"]:
        return False
    if not (ctx["entity_tmdb"] or ctx["entity_imdb"]):
        return False
    if _get(row, "tmdb_id") or _get(row, "imdb_id"):
        return False
    key = (int(row["source_id"]), str(row["kind"]), str(row["remote_id"]))
    if key in _ENRICH_ATTEMPTS:
        return False
    if len(_ENRICH_ATTEMPTS) >= _ENRICH_MAX:
        _ENRICH_ATTEMPTS.clear()
    _ENRICH_ATTEMPTS[key] = time.time()
    items.enrich_item_identity(conn, row, acquire_timeout=1.5)
    mode, value = items.identity_match_mode(
        conn, ctx["kind"], tmdb_id=ctx["entity_tmdb"],
        imdb_id=ctx["entity_imdb"],
    )
    if mode and (mode, value) != (ctx["mode"], ctx["identity"]):
        ctx["mode"], ctx["identity"] = mode, value
        return True
    return False


def _insert_candidate(conn, ctx: dict, scan: Mapping[str, Any],
                      row: Mapping[str, Any], classified: Mapping[str, Any],
                      *, episode_stream_id: str = "",
                      details: Mapping[str, Any] | None = None,
                      now: int | None = None) -> bool:
    now = _now(now)
    provider_kind = "vod" if ctx["entity_type"] == "movie" else "series"
    remote_id = str(row["remote_id"])
    existing = conn.execute(
        "SELECT id, status FROM bworker_candidates WHERE scan_id = ? "
        "AND source_id = ? AND provider_kind = ? AND provider_remote_id = ? "
        "AND episode_stream_id = ?",
        (int(scan["id"]), int(row["source_id"]), provider_kind,
         remote_id, str(episode_stream_id)),
    ).fetchone()
    score_details = None
    if details:
        score_details = json.dumps(dict(details), ensure_ascii=False)
    if existing is not None:
        if existing["status"] in ("pending", "rejected", "failed"):
            conn.execute(
                """
                UPDATE bworker_candidates
                SET item_id = ?, release_name = ?, resolution = ?, source = ?,
                    match_kind = ?, match_score = ?,
                    score_details = COALESCE(?, score_details), updated_at = ?
                WHERE id = ?
                """,
                (int(row["id"]), row["name"], classified.get("resolution"),
                 classified.get("source"), classified.get("match_kind"),
                 float(classified.get("match_score") or 0.0), score_details,
                 now, int(existing["id"])),
            )
        return False
    conn.execute(
        """
        INSERT INTO bworker_candidates
            (scan_id, work_id, source_id, item_id, provider_kind,
             provider_remote_id, episode_stream_id, release_name, resolution,
             source, score, score_details, match_kind, match_score, eligible,
             target_met, needs_upgrade, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, 0, 0,
                'pending', ?, ?)
        """,
        (int(scan["id"]), int(scan["work_id"]), int(row["source_id"]),
         int(row["id"]), provider_kind, remote_id, str(episode_stream_id),
         row["name"], classified.get("resolution"), classified.get("source"),
         score_details, classified.get("match_kind"),
         float(classified.get("match_score") or 0.0), now, now),
    )
    return True


def _discover(conn, ctx: dict, scan: Mapping[str, Any], *,
              batch: int, stop, check_pause: bool,
              now: int | None = None) -> dict:
    result = {
        "seen": 0, "new": 0, "matched": 0,
        "cursor": scan.get("cursor"),
        "exhausted": bool(scan.get("exhausted")),
        "stopped": False, "mode_changed": False,
    }
    if scan.get("exhausted"):
        return result
    cursor = int(scan.get("cursor") or 0)
    page_limit = min(200, max(50, batch * 4))
    pages = 0
    while (result["new"] < batch and pages < MAX_DISCOVERY_PAGES
           and not result["exhausted"]):
        if _stop_requested(conn, stop, check_pause):
            result["stopped"] = True
            break
        pages += 1
        page = items.catalog_page(
            conn, ctx["kind"], after_item_id=cursor,
            tmdb_id=ctx["identity"] if ctx["mode"] == "tmdb" else None,
            imdb_id=ctx["identity"] if ctx["mode"] == "imdb" else None,
            text_queries=ctx["titles"] if not ctx["mode"] else None,
            limit=page_limit,
        )
        rows = page["items"]
        if not rows:
            result["exhausted"] = True
            break
        page_seen = 0
        page_matched = 0
        for row in rows:
            cursor = int(row["id"])
            result["seen"] += 1
            page_seen += 1
            classified = matching.classify_catalog_row(
                row,
                kind=ctx["kind"],
                titles=ctx["titles"],
                year=ctx["year"],
                match_kind=ctx["mode"] or "title",
                min_score=TITLE_MIN_SCORE,
                identity_value=ctx["identity"],
            )
            if classified is None:
                continue
            result["matched"] += 1
            page_matched += 1
            if _maybe_enrich(conn, ctx, row):
                result["mode_changed"] = True
            if _insert_candidate(conn, ctx, scan, row, classified,
                                 now=now):
                result["new"] += 1
            if result["new"] >= batch:
                break
        result["cursor"] = str(cursor)
        result["exhausted"] = bool(page["exhausted"]) and result["new"] < batch
        # Persist progress after every page so a stop or crash never loses the
        # keyset position.
        state.update_scan(
            conn, int(scan["id"]), cursor=cursor,
            exhausted=result["exhausted"],
            seen_increment=page_seen, matched_increment=page_matched,
            now=now,
        )
        if result["mode_changed"]:
            break
    state.update_scan(conn, int(scan["id"]), cursor=cursor,
                      exhausted=result["exhausted"], now=now)
    return result


# --------------------------------------------------------------- episode expansion
def _expand_containers(conn, ctx: dict, scan: Mapping[str, Any], *,
                       limit: int, stop, check_pause: bool,
                       now: int | None = None) -> dict:
    """Turn series-level matches into the target episode's stream candidates."""
    now = _now(now)
    result = {"expanded": 0, "deferred": 0, "rejected": 0,
              "stopped": False, "streams": 0}
    rows = conn.execute(
        """
        SELECT * FROM bworker_candidates
        WHERE scan_id = ? AND status = 'pending' AND episode_stream_id = ''
          AND provider_kind = 'series'
        ORDER BY CASE match_kind WHEN 'tmdb' THEN 0 WHEN 'imdb' THEN 1 ELSE 2 END,
                 match_score DESC, id
        LIMIT ?
        """,
        (int(scan["id"]), int(limit)),
    ).fetchall()
    if not rows:
        return result
    from .. import library

    for candidate in rows:
        if _stop_requested(conn, stop, check_pause):
            result["stopped"] = True
            break
        candidate = dict(candidate)
        item = db.get_one(conn, "SELECT * FROM items WHERE id = ?",
                          (int(candidate["item_id"] or 0),))
        source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?",
                            (int(candidate["source_id"]),))
        if item is None or source is None:
            conn.execute(
                "UPDATE bworker_candidates SET status = 'rejected', updated_at = ? "
                "WHERE id = ?", (now, int(candidate["id"])),
            )
            result["rejected"] += 1
            continue
        if source["kind"] != "xtream":
            conn.execute(
                "UPDATE bworker_candidates SET status = 'rejected', updated_at = ? "
                "WHERE id = ?", (now, int(candidate["id"])),
            )
            result["rejected"] += 1
            continue
        try:
            episodes = library.provider_series_episodes(
                conn, source, str(candidate["provider_remote_id"])
            )
        except Exception as exc:
            from .. import connections
            if isinstance(exc, connections.ConnectionBusy):
                result["deferred"] += 1
                break
            # A provider error is transient: leave the container pending so a
            # later iteration can try again without losing the match.
            result["deferred"] += 1
            continue
        entry = None
        for episode_entry in episodes:
            if (int(episode_entry.get("season") or 0) == ctx["season_number"]
                    and int(episode_entry.get("episode") or 0) == ctx["episode_number"]):
                entry = episode_entry
                break
        if entry is None:
            conn.execute(
                "UPDATE bworker_candidates SET status = 'rejected', updated_at = ? "
                "WHERE id = ?", (now, int(candidate["id"])),
            )
            result["rejected"] += 1
            continue
        details = {
            "provider_episode": {
                "stream_id": str(entry["stream_id"]),
                "container_ext": entry.get("container_ext") or "mp4",
                "title": entry.get("title"),
                "season": ctx["season_number"],
                "episode": ctx["episode_number"],
            },
            "provider_series": {
                "remote_id": str(candidate["provider_remote_id"]),
                "name": candidate.get("release_name"),
            },
        }
        classified = {
            "resolution": candidate.get("resolution"),
            "source": candidate.get("source"),
            "match_kind": candidate.get("match_kind"),
            "match_score": candidate.get("match_score") or 0.0,
        }
        if _insert_candidate(
            conn, ctx, scan, item, classified,
            episode_stream_id=str(entry["stream_id"]),
            details=details, now=now,
        ):
            result["streams"] += 1
        conn.execute(
            "UPDATE bworker_candidates SET status = 'rejected', updated_at = ? "
            "WHERE id = ?", (now, int(candidate["id"])),
        )
        conn.commit()
        result["expanded"] += 1
    conn.commit()
    return result


# -------------------------------------------------------------------- probing
def _movie_release(conn, candidate: Mapping[str, Any], item: Mapping[str, Any],
                   source: Mapping[str, Any]) -> dict:
    parsed = matching.parse_name(str(item.get("name") or ""))
    return {
        "id": int(item["id"]),
        "source_id": int(candidate["source_id"]),
        "source_name": source["name"],
        "source_kind": source["kind"],
        "remote_id": str(candidate.get("provider_remote_id")
                         or candidate.get("remote_id")),
        "name": item.get("name"),
        "container_ext": item.get("container_ext"),
        "request_headers": item.get("request_headers"),
        "resolution": candidate.get("resolution") or parsed.resolution,
        "source": candidate.get("source") or parsed.source,
        "score": float(candidate.get("match_score") or 0.0),
        "match_kind": candidate.get("match_kind"),
        "match_score": float(candidate.get("match_score") or 0.0),
    }


def _episode_release(conn, candidate: Mapping[str, Any], item: Mapping[str, Any],
                     source: Mapping[str, Any], ctx: dict) -> dict:
    details = candidate.get("score_details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except (TypeError, ValueError):
            details = None
    provider = (details or {}).get("provider_episode") or {}
    provider_episode = {
        "stream_id": str(candidate["episode_stream_id"]),
        "container_ext": provider.get("container_ext") or "mp4",
        "title": provider.get("title") or candidate.get("release_name"),
        "season": ctx["season_number"],
        "episode": ctx["episode_number"],
    }
    parsed = matching.parse_name(str(candidate.get("release_name") or ""))
    return {
        "id": int(item["id"]),
        "source_id": int(candidate["source_id"]),
        "source_name": source["name"],
        "source_kind": source["kind"],
        "remote_id": str(candidate["provider_remote_id"]),
        "name": candidate.get("release_name"),
        "request_headers": item.get("request_headers"),
        "resolution": candidate.get("resolution") or parsed.resolution,
        "source": candidate.get("source") or parsed.source,
        "score": float(candidate.get("match_score") or 0.0),
        "match_kind": candidate.get("match_kind"),
        "match_score": float(candidate.get("match_score") or 0.0),
        "provider_episode": provider_episode,
    }


def _probe_pending(conn, ctx: dict, scan: Mapping[str, Any], profile,
                   *, limit: int, stop, check_pause: bool,
                   acquire_timeout: float, now: int | None = None,
                   on_event=None) -> dict:
    now = _now(now)
    result = {"attempted": 0, "completed": 0, "busy": False,
              "stopped": False, "best": None, "reasons": []}
    rows = conn.execute(
        """
        SELECT * FROM bworker_candidates
        WHERE scan_id = ? AND status = 'pending'
          AND (provider_kind = 'vod' OR episode_stream_id != '')
        ORDER BY CASE match_kind WHEN 'tmdb' THEN 0 WHEN 'imdb' THEN 1 ELSE 2 END,
                 match_score DESC, score DESC, id
        LIMIT ?
        """,
        (int(scan["id"]), int(limit)),
    ).fetchall()
    for row in rows:
        if _stop_requested(conn, stop, check_pause):
            result["stopped"] = True
            break
        candidate = dict(row)
        result["attempted"] += 1
        item = db.get_one(conn, "SELECT * FROM items WHERE id = ?",
                          (int(candidate["item_id"] or 0),))
        source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?",
                            (int(candidate["source_id"]),))
        # Release builders and probe fakes use mapping access.
        item = dict(item) if item is not None else None
        source = dict(source) if source is not None else None
        expected_kind = "vod" if ctx["entity_type"] == "movie" else "series"
        candidate_name = str(candidate.get("release_name") or
                             candidate.get("provider_remote_id") or "candidate")
        if (item is None or source is None
                or int(item["source_id"]) != int(candidate["source_id"])
                or str(item["kind"]) != expected_kind
                or str(item["remote_id"]) != str(candidate["provider_remote_id"])):
            reason = "The catalog item is no longer available or has changed."
            conn.execute(
                "UPDATE bworker_candidates SET status = 'rejected', "
                "score_details = ?, updated_at = ? WHERE id = ?",
                (json.dumps({"reason": reason}, ensure_ascii=False),
                 now, int(candidate["id"])),
            )
            conn.commit()
            _emit(on_event, event="probe_skipped", status="skipped",
                  name=candidate_name, reason=reason,
                  message=f"Skipped “{candidate_name}”: {reason}",
                  candidate_id=int(candidate["id"]))
            continue
        _emit(on_event, event="probe_started", status="running",
              name=candidate_name,
              message=f"Checking candidate “{candidate_name}”",
              candidate_id=int(candidate["id"]))
        if ctx["entity_type"] == "movie":
            release = _movie_release(conn, candidate, item, source)
            probed = probe.probe_movie_release(
                conn, release, acquire_timeout=acquire_timeout
            )
        else:
            release = _episode_release(conn, candidate, item, source, ctx)
            probed = probe.probe_episode_release(
                conn, release, acquire_timeout=acquire_timeout
            )
        payload = probed.get("probe") or {}
        if payload.get("busy"):
            # Connection-limit contention is not a candidate failure.
            reason = "The playlist connection is busy; it stays queued."
            conn.execute(
                "UPDATE bworker_candidates SET status = 'pending', updated_at = ? "
                "WHERE id = ?", (now, int(candidate["id"])),
            )
            conn.commit()
            _emit(on_event, event="probe_skipped", status="skipped",
                  name=candidate_name, reason=reason,
                  message=f"Deferred “{candidate_name}”: {reason}",
                  candidate_id=int(candidate["id"]))
            result["busy"] = True
            break
        evaluation = profile.evaluate(probed, ctx["original_language"])
        details = dict(evaluation)
        if not payload.get("ok") and payload.get("error"):
            details["probe_error"] = str(payload["error"])
        if not evaluation.get("eligible") and evaluation.get("reason"):
            details["skip_reason"] = str(evaluation["reason"])
        if ctx["entity_type"] == "episode":
            prior = candidate.get("score_details")
            if isinstance(prior, str):
                try:
                    prior = json.loads(prior)
                except (TypeError, ValueError):
                    prior = None
            if isinstance(prior, dict):
                for key in ("provider_episode", "provider_series"):
                    if prior.get(key):
                        details[key] = prior[key]
        status = "probed" if payload.get("ok") else "failed"
        conn.execute(
            """
            UPDATE bworker_candidates
            SET score = ?, score_details = ?, eligible = ?, target_met = ?,
                needs_upgrade = ?, resolution = ?, source = ?, status = ?,
                probed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(evaluation["score"]), json.dumps(details, ensure_ascii=False),
                1 if evaluation["eligible"] else 0,
                1 if evaluation["target_met"] else 0,
                1 if evaluation["needs_upgrade"] else 0,
                evaluation.get("resolution") or candidate.get("resolution"),
                evaluation.get("source") or candidate.get("source"),
                status, now, now, int(candidate["id"]),
            ),
        )
        conn.commit()
        result["completed"] += 1
        facts = _candidate_facts(payload)
        if evaluation.get("reason"):
            result["reasons"].append(str(evaluation["reason"]))
        if evaluation["eligible"] and payload.get("ok"):
            _emit(
                on_event, event="probe_finished", status="success",
                name=candidate_name, reason="", facts=facts,
                message=(f"Probed “{candidate_name}”: {facts or 'facts unavailable'}"
                         f" · score {evaluation.get('score') or 0}"
                         + (" · needs upgrade" if evaluation.get("needs_upgrade") else "")),
                candidate_id=int(candidate["id"]),
            )
        elif payload.get("ok"):
            reason = str(evaluation.get("reason") or "Outside the quality profile.")
            _emit(on_event, event="probe_skipped", status="skipped",
                  name=candidate_name, reason=reason, facts=facts,
                  message=f"Skipped “{candidate_name}”: {reason}",
                  candidate_id=int(candidate["id"]))
        else:
            reason = str(payload.get("error") or "ffprobe could not read the stream.")
            _emit(on_event, event="probe_failed", status="failure",
                  name=candidate_name, reason=reason,
                  message=f"Probe failed for “{candidate_name}”: {reason}",
                  candidate_id=int(candidate["id"]))
        if evaluation["eligible"]:
            best = result["best"]
            if best is None or int(evaluation["score"]) > int(best["score"]):
                result["best"] = {
                    "id": int(candidate["id"]),
                    "source_id": int(candidate["source_id"]),
                    "provider_remote_id": str(candidate["provider_remote_id"]),
                    "episode_stream_id": str(candidate["episode_stream_id"]),
                    "score": int(evaluation["score"]),
                    "target_met": bool(evaluation["target_met"]),
                }
    if len(rows) >= int(limit):
        remaining = conn.execute(
            "SELECT id, release_name FROM bworker_candidates "
            "WHERE scan_id = ? AND status = 'pending' ORDER BY id LIMIT 50",
            (int(scan["id"]),),
        ).fetchall()
        reason = (f"Not probed in this cycle: the probe batch limit ({int(limit)}) "
                  "was reached; it will be retried on the next cycle.")
        for row in remaining:
            name = str(row["release_name"] or "candidate")
            _emit(on_event, event="probe_skipped", status="skipped",
                  name=name, reason=reason,
                  message=f"Deferred “{name}”: {reason}",
                  candidate_id=int(row["id"]))
    if result["completed"]:
        state.update_scan(conn, int(scan["id"]),
                          probed_increment=result["completed"], now=now)
    return result


# ------------------------------------------------------------------- selection
def _current_movie_identity(conn, movie: Mapping[str, Any]):
    if not movie.get("file_path"):
        return None
    source_id = _int(movie.get("main_source_id"))
    remote_id = movie.get("main_remote_id")
    if source_id and remote_id:
        return source_id, str(remote_id)
    row = db.get_one(
        conn,
        "SELECT source_id, provider_remote_id FROM downloads "
        "WHERE movie_id = ? AND kind = 'movie' AND mode = 'main' "
        "AND status = 'imported' AND final_path = ? AND source_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (int(movie["id"]), str(movie["file_path"])),
    )
    if row is not None and row["source_id"] and row["provider_remote_id"]:
        return int(row["source_id"]), str(row["provider_remote_id"])
    return None


def _current_episode_identity(conn, episode: Mapping[str, Any]):
    if not episode.get("file_path"):
        return None
    row = db.get_one(
        conn,
        "SELECT source_id, provider_remote_id, episode_stream_id FROM downloads "
        "WHERE episode_id = ? AND status = 'imported' AND source_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (int(episode["id"]),),
    )
    if row is None or not row["source_id"]:
        return None
    return (int(row["source_id"]), str(row["provider_remote_id"]),
            str(row["episode_stream_id"] or ""))


def _same_identity(ctx: dict, candidate: Mapping[str, Any],
                   identity) -> bool:
    if identity is None:
        return False
    remote_id = candidate.get("provider_remote_id", candidate.get("remote_id"))
    if remote_id is None:
        return False
    if ctx["entity_type"] == "movie":
        return (int(candidate["source_id"]) == int(identity[0])
                and str(remote_id) == str(identity[1]))
    return (
        int(candidate["source_id"]) == int(identity[0])
        and str(remote_id) == str(identity[1])
        and str(candidate.get("episode_stream_id") or "") == str(identity[2])
    )


def _validate_candidate(conn, ctx: dict, candidate: Mapping[str, Any]) -> str:
    """Return ``ok``, ``stale`` or ``busy``."""
    item = db.get_one(conn, "SELECT * FROM items WHERE id = ?",
                      (int(candidate["item_id"] or 0),))
    expected_kind = "vod" if ctx["entity_type"] == "movie" else "series"
    if (item is None
            or int(item["source_id"]) != int(candidate["source_id"])
            or str(item["kind"]) != expected_kind
            or str(item["remote_id"]) != str(candidate["provider_remote_id"])):
        return "stale"
    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?",
                        (int(candidate["source_id"]),))
    if source is None:
        return "stale"
    if ctx["entity_type"] == "episode":
        if not candidate.get("episode_stream_id"):
            return "stale"
        from .. import library, connections
        try:
            episodes = library.provider_series_episodes(
                conn, source, str(candidate["provider_remote_id"])
            )
        except connections.ConnectionBusy:
            return "busy"
        except Exception:
            # Provider trouble is not proof the stream is gone.
            return "busy"
        for entry in episodes:
            if (int(entry.get("season") or 0) == ctx["season_number"]
                    and int(entry.get("episode") or 0) == ctx["episode_number"]):
                if str(entry.get("stream_id")) != str(candidate["episode_stream_id"]):
                    return "stale"
                return "ok"
        return "stale"
    return "ok"


def _select(conn, ctx: dict, scan: Mapping[str, Any], profile,
            counts: Mapping[str, Any], *, allow_fallback: bool) -> dict:
    result = {"candidate": None, "evaluation": None, "message": "",
              "blocked": False}
    current = ctx["entity"]
    has_file = bool(_get(current, "file_path"))
    identity = ctx["current_identity"]
    current_eval = None
    if has_file:
        if not profile.upgrades:
            result["message"] = (
                "The current file is kept because upgrades are disabled."
            )
            return result
        current_eval = profile.evaluate(current, ctx["original_language"])
        if current_eval["target_met"]:
            result["message"] = "The current file already meets the quality target."
            return result

    target_phase = True
    while True:
        if target_phase:
            rows = conn.execute(
                """
                SELECT * FROM bworker_candidates
                WHERE scan_id = ? AND eligible = 1 AND target_met = 1
                  AND status IN ('probed', 'selected')
                ORDER BY score DESC, id
                LIMIT 25
                """,
                (int(scan["id"]),),
            ).fetchall()
        elif allow_fallback:
            rows = conn.execute(
                """
                SELECT * FROM bworker_candidates
                WHERE scan_id = ? AND eligible = 1
                  AND status IN ('probed', 'selected')
                ORDER BY score DESC, id
                LIMIT 25
                """,
                (int(scan["id"]),),
            ).fetchall()
        else:
            rows = []
        for row in rows:
            candidate = dict(row)
            if _same_identity(ctx, candidate, identity):
                continue
            if has_file and current_eval["eligible"]:
                if int(candidate.get("score") or 0) <= int(current_eval["score"]):
                    continue
            status = _validate_candidate(conn, ctx, candidate)
            if status == "stale":
                conn.execute(
                    "UPDATE bworker_candidates SET status = 'rejected', updated_at = ? "
                    "WHERE id = ?", (_now(), int(candidate["id"])),
                )
                conn.commit()
                continue
            if status == "busy":
                result["blocked"] = True
                result["message"] = (
                    "The playlist is busy; the next iteration will verify the "
                    "candidate before downloading."
                )
                return result
            try:
                details = json.loads(candidate.get("score_details") or "null")
            except (TypeError, ValueError):
                details = None
            result["candidate"] = candidate
            result["evaluation"] = details if isinstance(details, dict) else {}
            return result
        if not target_phase:
            break
        target_phase = False
        if not allow_fallback:
            break
    return result


# --------------------------------------------------------------------- handoff
def _download_active(conn, ctx: dict) -> dict | None:
    if ctx["entity_type"] == "movie":
        return db.get_one(
            conn,
            "SELECT * FROM downloads WHERE movie_id = ? "
            "AND status IN ('queued', 'downloading', 'importing') "
            "AND mode = 'main' ORDER BY id DESC LIMIT 1",
            (int(ctx["entity"]["id"]),),
        )
    return db.get_one(
        conn,
        "SELECT * FROM downloads WHERE episode_id = ? "
        "AND status IN ('queued', 'downloading', 'importing') "
        "ORDER BY id DESC LIMIT 1",
        (int(ctx["entity"]["id"]),),
    )


def _recent_failure(conn, ctx: dict) -> dict | None:
    cutoff = _now() - 24 * 3600
    if ctx["entity_type"] == "movie":
        return db.get_one(
            conn,
            "SELECT * FROM downloads WHERE movie_id = ? AND status = 'failed' "
            "AND requested_at >= ? AND mode = 'main' ORDER BY id DESC LIMIT 1",
            (int(ctx["entity"]["id"]), cutoff),
        )
    return db.get_one(
        conn,
        "SELECT * FROM downloads WHERE episode_id = ? AND status = 'failed' "
        "AND requested_at >= ? ORDER BY id DESC LIMIT 1",
        (int(ctx["entity"]["id"]), cutoff),
    )


def annotate_download(conn, download_id: int, evaluation: Mapping[str, Any], *,
                      max_attempts: int | None = None) -> dict:
    conn.execute(
        "UPDATE downloads SET score = ?, score_details = ?, "
        "max_attempts = COALESCE(?, max_attempts) WHERE id = ?",
        (
            int(evaluation.get("score") or 0),
            json.dumps(dict(evaluation), ensure_ascii=False),
            int(max_attempts) if max_attempts is not None else None,
            int(download_id),
        ),
    )
    conn.commit()
    row = db.get_one(conn, "SELECT * FROM downloads WHERE id = ?",
                     (int(download_id),))
    return dict(row) if row is not None else {}


def _cached_probe_payload(conn, candidate: Mapping[str, Any]) -> dict | None:
    row = db.get_one(
        conn,
        "SELECT payload FROM stream_probes WHERE source_id = ? "
        "AND provider_kind = ? AND provider_remote_id = ? "
        "AND episode_stream_id = ?",
        (
            int(candidate["source_id"]),
            str(candidate.get("provider_kind") or "vod"),
            str(candidate["provider_remote_id"]),
            str(candidate.get("episode_stream_id") or ""),
        ),
    )
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("ok") else None


def _handoff(conn, ctx: dict, profile, candidate: Mapping[str, Any],
             evaluation: Mapping[str, Any] | None, *,
             now: int | None = None) -> dict:
    now = _now(now)
    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?",
                        (int(candidate["source_id"]),))
    item = db.get_one(conn, "SELECT * FROM items WHERE id = ?",
                      (int(candidate["item_id"] or 0),))
    if source is None or item is None:
        return {"error": "The candidate is no longer in the playlist."}
    source = dict(source)
    item = dict(item)
    cached = _cached_probe_payload(conn, candidate)
    if ctx["entity_type"] == "movie":
        release = _movie_release(conn, candidate, item, source)
    else:
        release = _episode_release(conn, candidate, item, source, ctx)
    if cached is not None:
        release = probe.apply(release, cached)
    # The stored evaluation is the one the candidate was selected by; do not
    # silently re-score without its probe facts if the cache was cleared.
    evaluation = dict(evaluation or {})
    if not evaluation.get("eligible"):
        evaluation = profile.evaluate(release, ctx["original_language"])
    if not evaluation.get("eligible"):
        return {"error": str(evaluation.get("reason") or "Not eligible.")}
    release["resolution"] = evaluation.get("resolution") or release.get("resolution")
    release["source"] = evaluation.get("source") or release.get("source")
    release["quality_label"] = _label(evaluation)
    release.pop("request_headers", None)
    from .. import downloader

    try:
        if ctx["entity_type"] == "movie":
            download = downloader.enqueue_movie(
                conn, ctx["entity"], release,
                reason="upgrade" if ctx["entity"].get("file_path") else "auto",
            )
        else:
            provider_episode = release.get("provider_episode") or {}
            if not provider_episode.get("stream_id"):
                return {"error": "The provider episode is no longer available."}
            download = downloader.enqueue_episode(
                conn, ctx["series"], ctx["episode"], release, provider_episode,
                reason="upgrade" if ctx["episode"].get("file_path") else "auto",
            )
    except (ValueError, LookupError) as exc:
        return {"error": str(exc)}
    annotate_download(conn, int(download["id"]), evaluation, max_attempts=1)
    # enqueue_* returns the row as it was before the annotation; keep the
    # returned mapping truthful for the API response and continuation jobs.
    download["score"] = int(evaluation["score"])
    download["score_details"] = json.dumps(dict(evaluation), ensure_ascii=False)
    download["max_attempts"] = 1
    conn.execute(
        "UPDATE bworker_candidates SET status = 'selected', score = ?, "
        "score_details = ?, eligible = 1, target_met = ?, needs_upgrade = ?, "
        "updated_at = ? WHERE id = ?",
        (
            int(evaluation["score"]),
            json.dumps(dict(evaluation), ensure_ascii=False),
            1 if evaluation["target_met"] else 0,
            1 if evaluation["needs_upgrade"] else 0,
            now, int(candidate["id"]),
        ),
    )
    conn.commit()
    return {"download": download, "release": release, "evaluation": evaluation}


def _label(evaluation: Mapping[str, Any]) -> str:
    resolution = str(evaluation.get("resolution") or "unknown")
    source = str(evaluation.get("source") or "unknown")
    if source in ("", "unknown", "none"):
        return resolution
    try:
        source_name = quality.source_label(source)
    except Exception:
        source_name = source
    return f"{resolution} {source_name}".strip()


def _continuation(conn, ctx: dict, scan: Mapping[str, Any], *,
                  available_at: int | None = None) -> None:
    """Queue one more bounded iteration without editing bworker.py.

    The worker's job handlers already call ``library.search_movie`` /
    ``search_series`` with ``reason="auto"``, so a queued auto job is the
    integration hook. The token changes as the scan advances; this both avoids
    duplicate chains and lets a running continuation enqueue its successor.
    """
    from .. import jobs
    counts = state.scan_counts(conn, int(scan["id"]))
    token = ":".join(str(value) for value in (
        int(scan["id"]), scan.get("cursor") or "0",
        counts.get("candidates", 0), counts.get("probed", 0),
        counts.get("pending", 0), int(available_at or 0),
    ))
    if ctx["entity_type"] == "movie":
        payload = {"movie_id": int(ctx["entity"]["id"]), "reason": "auto",
                   "acquisition": True}
        kind = "search_movie"
        key = f"acquisition:movie:{int(ctx['entity']['id'])}:{token}"
    else:
        payload = {"series_id": int(ctx["series"]["id"]),
                   "episode_id": int(ctx["episode"]["id"]),
                   "reason": "auto", "acquisition": True}
        kind = "search_series"
        key = f"acquisition:episode:{int(ctx['episode']['id'])}:{token}"
    jobs.enqueue(conn, kind, payload, priority=5, dedupe_key=key,
                 available_at=available_at)


# ----------------------------------------------------------------------- context
def _movie_context(conn, movie: Mapping[str, Any], *,
                   query: str | None = None) -> dict:
    titles = list(matching.aliases_from_row(movie))
    year = _int(_get(movie, "year"))
    profile = _profile(conn, _get(movie, "quality_profile_id"))
    entity_tmdb, entity_imdb = _identity_fields(movie)
    if query:
        mode, identity = None, None
    else:
        mode, identity = items.identity_match_mode(
            conn, "vod", tmdb_id=entity_tmdb, imdb_id=entity_imdb
        )
    return {
        "entity_type": "movie",
        "entity": dict(movie),
        "series": None,
        "episode": None,
        "kind": "vod",
        "titles": titles,
        "year": year,
        "profile": profile,
        "original_language": _get(movie, "original_language"),
        "entity_tmdb": entity_tmdb,
        "entity_imdb": entity_imdb,
        "mode": mode,
        "identity": identity,
        "query": None,
    }


def _episode_context(conn, series: Mapping[str, Any],
                     episode: Mapping[str, Any]) -> dict:
    titles = list(matching.aliases_from_row(series))
    year = _int(_get(series, "year"))
    profile = _profile(conn, _get(series, "quality_profile_id"))
    entity_tmdb, entity_imdb = _identity_fields(series)
    mode, identity = items.identity_match_mode(
        conn, "series", tmdb_id=entity_tmdb, imdb_id=entity_imdb
    )
    return {
        "entity_type": "episode",
        "entity": dict(episode),
        "series": dict(series),
        "episode": dict(episode),
        "kind": "series",
        "titles": titles,
        "year": year,
        "profile": profile,
        "original_language": _get(series, "original_language"),
        "entity_tmdb": entity_tmdb,
        "entity_imdb": entity_imdb,
        "mode": mode,
        "identity": identity,
        "query": None,
        "season_number": int(episode["season"]),
        "episode_number": int(episode["episode"]),
        "episode_id": int(episode["id"]),
    }


def _fingerprint(ctx: dict) -> str:
    if ctx["entity_type"] == "movie":
        entity_id = int(ctx["entity"]["id"])
        season = episode = None
    else:
        entity_id = int(ctx["entity"]["id"])
        season = ctx["season_number"]
        episode = ctx["episode_number"]
    return state.query_fingerprint(
        entity=ctx["entity_type"], entity_id=entity_id, kind=ctx["kind"],
        mode=ctx["mode"], identity=ctx["identity"],
        titles=ctx["titles"], year=ctx["year"],
        profile_id=ctx["profile"].profile_id,
        profile_revision=int(ctx["profile"].revision or 1),
        season=season, episode=episode, query=ctx["query"],
    )


# -------------------------------------------------------------------- automatic
def _run(conn, ctx: dict, *, work: Mapping[str, Any] | None,
         reason: str, stop, batch_size: int | None,
         probe_limit: int | None, acquire_timeout: float,
         now: int | None, on_event=None) -> dict:
    now = _now(now)
    profile = ctx["profile"]
    entity_type = ctx["entity_type"]
    entity_id = int(ctx["entity"]["id"])

    if entity_type == "movie":
        monitored = bool(ctx["entity"].get("monitored"))
    else:
        monitored = (bool(ctx["series"].get("monitored"))
                     and bool(ctx["episode"].get("monitored")))
    work = _work_for(conn, entity_type, entity_id, work)
    if not monitored:
        return _empty_outcome("skipped", "This item is not monitored.", work=work)
    if work is None or not work.get("enabled"):
        return _empty_outcome("skipped", "Automatic search is disabled.", work=work)

    active = _download_active(conn, ctx)
    if active is not None:
        return _empty_outcome(
            "downloading", "A download for this item is already active.",
            next_run_at=now + 60, work=work,
        )

    batch = batch_size or _int(
        _get(ctx["entity"], "candidate_batch_size")
        or _get(ctx.get("series"), "candidate_batch_size")
    ) or _setting_int(conn, "candidate_batch_size", DEFAULT_BATCH,
                      minimum=1, maximum=MAX_BATCH)
    batch = max(1, min(int(batch), MAX_BATCH))
    probe_limit = probe_limit or _int(
        _get(ctx["entity"], "probe_batch_size")
        or _get(ctx.get("series"), "probe_batch_size")
    ) or _setting_int(conn, "probe_batch_per_loop", DEFAULT_PROBE,
                      minimum=1, maximum=MAX_PROBE)
    probe_limit = max(1, min(int(probe_limit), MAX_PROBE))

    epoch = state.catalog_epoch(conn)
    fingerprint = _fingerprint(ctx)
    scan, _reset = state.ensure_scan(
        conn, work_id=int(work["id"]), epoch=epoch,
        fingerprint=fingerprint, now=now,
    )
    state.invalidate_stale_candidates(conn, int(scan["id"]), now=now)

    counts_before = state.scan_counts(conn, int(scan["id"]))
    discovery = _discover(
        conn, ctx, scan, batch=batch, stop=stop,
        check_pause=(reason == "auto"), now=now,
    )
    scan = state.get_scan(conn, int(scan["id"])) or scan
    expansion = {"expanded": 0, "deferred": 0, "rejected": 0,
                 "stopped": False, "streams": 0}
    if entity_type == "episode" and not discovery["mode_changed"]:
        expansion = _expand_containers(
            conn, ctx, scan, limit=EPISODE_EXPANSION_LIMIT, stop=stop,
            check_pause=(reason == "auto"), now=now,
        )
    probing = {"attempted": 0, "completed": 0, "busy": False,
               "stopped": False, "best": None, "reasons": []}
    if not discovery["stopped"] and not expansion["stopped"] \
            and not discovery["mode_changed"]:
        probing = _probe_pending(
            conn, ctx, scan, profile, limit=probe_limit, stop=stop,
            check_pause=(reason == "auto"),
            acquire_timeout=acquire_timeout, now=now, on_event=on_event,
        )
    scan = state.get_scan(conn, int(scan["id"])) or scan
    state.invalidate_stale_candidates(conn, int(scan["id"]), now=now)
    counts = state.scan_counts(conn, int(scan["id"]))
    counts["seen"] = int(scan.get("candidates_seen") or 0)
    counts["matched"] = int(scan.get("candidates_matched") or 0)
    counts["probed_total"] = int(scan.get("candidates_probed") or 0)

    stopped = (discovery["stopped"] or expansion["stopped"]
               or probing["stopped"])
    mode_changed = discovery["mode_changed"]

    identity = (_current_movie_identity(conn, ctx["entity"])
                if entity_type == "movie"
                else _current_episode_identity(conn, ctx["episode"]))
    ctx["current_identity"] = identity

    pool_exhausted = bool(scan.get("exhausted")) and counts["pending"] == 0
    selection = {"candidate": None, "evaluation": None, "message": "",
                 "blocked": False}
    if not stopped and not mode_changed:
        selection = _select(
            conn, ctx, scan, profile, counts,
            allow_fallback=pool_exhausted,
        )

    best = probing.get("best")
    next_run = None
    stage = "searching_candidates"
    need_more = False
    message = ""

    if mode_changed:
        message = ("A provider detail supplied identity ids; the next iteration "
                   "will rescan ID-first.")
        need_more = True
        next_run = now + 1
        stage = "searching_candidates"
    elif stopped:
        message = "Search paused before the iteration finished."
        next_run = now + 60
        stage = "paused"
    elif selection["candidate"] is not None:
        if reason in {"auto", "add"} and _recent_failure(conn, ctx):
            message = ("The last attempt failed recently; skipping this "
                       "automatic retry.")
            stage = "awaiting_upgrade" if _get(ctx["entity"], "file_path") else "no_candidates"
            next_run = now + _search_interval_seconds(
                conn, entity_type, ctx["series"] if entity_type == "episode"
                else ctx["entity"])
        else:
            handoff = _handoff(conn, ctx, profile, selection["candidate"],
                               selection.get("evaluation"), now=now)
            if handoff.get("error"):
                message = str(handoff["error"])
                stage = "no_candidates"
            else:
                download = handoff["download"]
                stage = "downloading"
                message = (
                    f"Queued {handoff['release'].get('quality_label') or 'release'} "
                    f"from {handoff['release'].get('source_name')}."
                )
                next_run = now + 60
                state.record_work_progress(
                    conn, int(work["id"]), cursor=scan.get("cursor"),
                    profile_revision=int(profile.revision or 1), best=best,
                    stage=stage, next_run_at=next_run,
                    current_download_id=int(download["id"]), now=now,
                )
                outcome = _empty_outcome(
                    stage, message, need_more=False,
                    exhausted=bool(scan.get("exhausted")), next_run_at=next_run,
                    work=work,
                )
                outcome.update({
                    "cursor": scan.get("cursor"),
                    "candidates": counts,
                    "chosen_candidate": _public_candidate(selection["candidate"]),
                    "progress": _progress(scan, counts),
                    "queued": True,
                    "download": download,
                    "release": handoff["release"],
                    "stage": stage,
                    "message": message,
                })
                if scan.get("exhausted"):
                    state.set_scan_status(conn, int(scan["id"]), "complete", now=now)
                return outcome
    elif (pool_exhausted and not selection["blocked"]):
        if _get(ctx["entity"], "file_path"):
            stage = "awaiting_upgrade"
            message = selection["message"] or (
                "No better release found; keeping the current file."
            )
        else:
            stage = "no_candidates"
            message = selection["message"] or (
                "No eligible release found in the catalog."
            )
        next_run = now + _search_interval_seconds(
            conn, entity_type,
            ctx["series"] if entity_type == "episode" else ctx["entity"])
        if scan.get("exhausted"):
            state.set_scan_status(conn, int(scan["id"]), "complete", now=now)
    else:
        if selection.get("message"):
            message = selection["message"]
        elif discovery["new"] or probing["completed"] or expansion["expanded"]:
            message = (
                f"Scanning: {counts['seen']} catalog entries checked, "
                f"{counts['candidates']} candidates, "
                f"{counts['probed_total']} probed."
            )
        else:
            message = "No further match in this catalog page."
        need_more = True
        if selection["blocked"] or probing["busy"] or expansion["deferred"]:
            next_run = now + 300
        else:
            next_run = now + 1
        stage = "probing" if probing["completed"] else "searching_candidates"

    state.record_work_progress(
        conn, int(work["id"]), cursor=scan.get("cursor"),
        profile_revision=int(profile.revision or 1), best=best, now=now,
    )
    if need_more:
        _continuation(conn, ctx, scan, available_at=next_run)
    outcome = _empty_outcome(
        stage, message, need_more=need_more,
        exhausted=bool(scan.get("exhausted")), stopped=stopped,
        next_run_at=next_run, work=work,
    )
    outcome.update({
        "cursor": scan.get("cursor"),
        "candidates": counts,
        "chosen_candidate": None,
        "progress": _progress(scan, counts),
        "batch_size": batch,
        "probe_limit": probe_limit,
    })
    return outcome


def _public_candidate(candidate: Mapping[str, Any]) -> dict:
    return {
        "id": int(candidate["id"]),
        "item_id": candidate.get("item_id"),
        "source_id": int(candidate["source_id"]),
        "provider_kind": candidate.get("provider_kind"),
        "provider_remote_id": candidate.get("provider_remote_id"),
        "episode_stream_id": candidate.get("episode_stream_id"),
        "match_kind": candidate.get("match_kind"),
        "match_score": candidate.get("match_score"),
        "score": candidate.get("score"),
        "resolution": candidate.get("resolution"),
        "source": candidate.get("source"),
        "status": candidate.get("status"),
    }


def run_movie_iteration(conn, movie: Mapping[str, Any], *, reason: str = "auto",
                        work: Mapping[str, Any] | None = None,
                        stop: Callable[[], bool] | None = None,
                        batch_size: int | None = None,
                        probe_limit: int | None = None,
                        acquire_timeout: float = 5.0,
                        now: int | None = None,
                        on_event=None) -> dict:
    ctx = _movie_context(conn, movie)
    return _run(conn, ctx, work=work, reason=reason, stop=stop,
                batch_size=batch_size, probe_limit=probe_limit,
                acquire_timeout=acquire_timeout, now=now, on_event=on_event)


def run_episode_iteration(conn, series: Mapping[str, Any],
                          episode: Mapping[str, Any], *,
                          reason: str = "auto",
                          work: Mapping[str, Any] | None = None,
                          stop: Callable[[], bool] | None = None,
                          batch_size: int | None = None,
                          probe_limit: int | None = None,
                          acquire_timeout: float = 5.0,
                          now: int | None = None,
                          on_event=None) -> dict:
    ctx = _episode_context(conn, series, episode)
    return _run(conn, ctx, work=work, reason=reason, stop=stop,
                batch_size=batch_size, probe_limit=probe_limit,
                acquire_timeout=acquire_timeout, now=now, on_event=on_event)


# ----------------------------------------------------------------------- manual
def bounded_movie_search(conn, movie: Mapping[str, Any], *,
                         query: str | None = None,
                         reason: str = "manual",
                         min_score: float | None = None,
                         scan_limit: int = MANUAL_SCAN_LIMIT,
                         probe_limit: int | None = None,
                         acquire_timeout: float = 30.0) -> dict:
    """Bounded synchronous candidate search for manual/API paths.

    It uses the same ID-first classification and keyset paging, but keeps no
    bworker scan state: manual search is a one-shot bounded operation.
    """
    ctx = _movie_context(conn, movie, query=query)
    ctx["query"] = query
    titles = ctx["titles"]
    if query:
        threshold = MANUAL_QUERY_MIN_SCORE if min_score is None else float(min_score)
        mode, identity = None, None
    else:
        threshold = MANUAL_TITLE_MIN_SCORE if min_score is None else float(min_score)
        mode, identity = ctx["mode"], ctx["identity"]

    found: list[dict] = []
    cursor = 0
    pages = 0
    while len(found) < scan_limit and pages < MAX_DISCOVERY_PAGES:
        pages += 1
        page = items.catalog_page(
            conn, "vod", after_item_id=cursor,
            tmdb_id=identity if mode == "tmdb" else None,
            imdb_id=identity if mode == "imdb" else None,
            text_queries=([query] if query else titles) if not mode else None,
            limit=min(200, max(50, scan_limit)),
        )
        rows = page["items"]
        if not rows:
            break
        for row in rows:
            cursor = int(row["id"])
            classified = matching.classify_catalog_row(
                row, kind="vod", titles=titles, year=ctx["year"],
                match_kind=mode or "title", min_score=threshold,
                identity_value=identity,
            )
            if classified is not None and len(found) < scan_limit:
                found.append({**row, **classified})
        if page["exhausted"]:
            break
    found.sort(key=lambda candidate: (
        {"tmdb": 0, "imdb": 1, "title": 2}.get(
            str(candidate.get("match_kind") or "title"), 2),
        -float(candidate.get("match_score") or 0.0),
        quality.RANK.get(candidate.get("resolution"), len(quality.RESOLUTIONS)),
        str(candidate.get("name") or "").casefold(),
    ))
    limit = probe_limit or probe.max_candidates(conn)
    limit = max(1, min(int(limit), max(1, probe.max_candidates(conn))))
    identity_current = _current_movie_identity(conn, movie)
    releases: list[dict] = []
    budget = [probe.search_budget(conn)]
    for candidate in found[:limit]:
        if budget[0] <= 0:
            break
        item = db.get_one(conn, "SELECT * FROM items WHERE id = ?",
                          (int(candidate["id"]),))
        source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?",
                            (int(candidate["source_id"]),))
        if item is None or source is None:
            continue
        item = dict(item)
        source = dict(source)
        if (_same_identity(ctx, candidate, identity_current)):
            continue
        release = _movie_release(conn, candidate, item, source)
        release = probe.probe_movie_release(
            conn, release, acquire_timeout=acquire_timeout
        )
        budget[0] -= 1
        evaluation = ctx["profile"].evaluate(release, ctx["original_language"])
        release["quality_label"] = (
            _label(evaluation) if evaluation["eligible"] else None
        )
        release["allowed"] = bool(evaluation["eligible"])
        release["reason"] = str(evaluation.get("reason") or "")
        release["target_met"] = bool(evaluation.get("target_met"))
        release["needs_upgrade"] = bool(evaluation.get("needs_upgrade"))
        release["profile_score"] = int(evaluation.get("score") or 0)
        # Per-stream headers can carry tokens and are never part of the
        # manual/API release shape (library.manual_movie_releases strips the
        # same field).
        release.pop("request_headers", None)
        releases.append(release)
    choice = quality.choose_release(
        releases, ctx["profile"],
        current=movie if movie.get("file_path") else None,
        original_language=ctx["original_language"],
    )
    if choice is not None:
        choice = dict(choice)
        choice.pop("request_headers", None)
        message = (
            f"Found {choice.get('resolution') or 'unknown'} "
            f"{choice.get('source') or ''} from {choice.get('source_name')}."
        ).replace("  ", " ")
    elif not found:
        message = "No matching VOD found in your playlists."
    elif not releases:
        message = "No candidate could be probed in this search."
    else:
        reasons = [entry.get("reason") for entry in releases if entry.get("reason")]
        message = "No allowed release available"
        if reasons:
            message += f": {reasons[0]}"
    return {
        "choice": choice,
        "release": choice,
        "message": message,
        "candidates": found,
        "releases": releases,
        "scanned": len(found),
        "probed": len(releases),
        "match_mode": mode or "title",
    }


# ------------------------------------------------------------------- read API
def candidate_rows(conn, work_id: int | None = None,
                   scan_id: int | None = None, statuses=None,
                   limit: int = 100) -> list[dict]:
    return state.candidate_rows(conn, work_id=work_id, scan_id=scan_id,
                                statuses=statuses, limit=limit)


def scan_snapshot(conn, work_id: int) -> dict:
    return state.scan_snapshot(conn, work_id)


__all__ = [
    "annotate_download",
    "bounded_movie_search",
    "candidate_rows",
    "run_episode_iteration",
    "run_movie_iteration",
    "scan_snapshot",
]
