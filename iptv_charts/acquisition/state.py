"""Persistent scan/candidate state for the acquisition pipeline.

All state is derived and lives in the two tables the v5 schema already owns.
No schema changes are made here. The important invariants:

* a scan belongs to exactly one work row and one catalog epoch;
* the query fingerprint covers every input that can change what a candidate
  means (identity mode/value, titles, year, episode, profile revision);
* a fingerprint/epoch change resets the scan rather than mixing pages;
* candidates are deduplicated by the same identity tuple downloads and probes
  use: ``(source_id, provider_kind, provider_remote_id, episode_stream_id)``.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping

from .. import db


def _now(now: int | None = None) -> int:
    return int(now if now is not None else time.time())


def catalog_epoch(conn) -> str:
    """Opaque fingerprint of the provider catalog.

    ``sources.last_synced_at`` changes on a successful sync, and the maximum
    item id changes when items are deleted/reinserted. Together they catch a
    changed catalog without scanning item rows or trusting a failed sync.
    """
    sources = conn.execute(
        "SELECT id, kind, COALESCE(last_synced_at, 0) AS synced "
        "FROM sources ORDER BY id"
    ).fetchall()
    max_item = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM items").fetchone()
    parts = [f"{int(row['id'])}:{row['kind']}:{int(row['synced'])}" for row in sources]
    parts.append(f"max:{int(max_item['id']) if max_item else 0}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]


def query_fingerprint(
    *,
    entity: str,
    entity_id: int,
    kind: str,
    mode: str | None,
    identity: str | None,
    titles: list[str] | tuple[str, ...],
    year: int | None,
    profile_id: int | None,
    profile_revision: int,
    season: int | None = None,
    episode: int | None = None,
    query: str | None = None,
    version: int = 1,
) -> str:
    payload = {
        "version": int(version),
        "entity": entity,
        "entity_id": int(entity_id),
        "kind": kind,
        "mode": mode or "title",
        "identity": identity,
        "titles": list(titles),
        "year": year,
        "profile_id": profile_id,
        "profile_revision": int(profile_revision),
        "season": season,
        "episode": episode,
        "query": query,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _scan_dict(row) -> dict | None:
    return dict(row) if row is not None else None


def active_scan(conn, work_id: int) -> dict | None:
    return _scan_dict(conn.execute(
        "SELECT * FROM bworker_scan WHERE work_id = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (int(work_id),),
    ).fetchone())


def get_scan(conn, scan_id: int) -> dict | None:
    return _scan_dict(conn.execute(
        "SELECT * FROM bworker_scan WHERE id = ?", (int(scan_id),)
    ).fetchone())


def ensure_scan(conn, *, work_id: int, epoch: str, fingerprint: str,
                now: int | None = None) -> tuple[dict, bool]:
    """Reuse the matching active scan, or reset/create one.

    Returns ``(scan, reset)``. A reset deletes the old candidate rows because
    they were matched under a different query/profile identity.
    """
    now = _now(now)
    work_id = int(work_id)
    current = active_scan(conn, work_id)
    if (current is not None and current["epoch"] == str(epoch)
            and current["query_fingerprint"] == str(fingerprint)):
        # A previous iteration may have stopped between claiming and finishing
        # a probe. Such rows are pending again, never selected.
        conn.execute(
            "UPDATE bworker_candidates SET status = 'pending', updated_at = ? "
            "WHERE scan_id = ? AND status = 'probing'",
            (now, int(current["id"])),
        )
        conn.commit()
        return current, False

    if current is not None:
        conn.execute(
            "UPDATE bworker_scan SET status = 'invalidated', updated_at = ? "
            "WHERE id = ?",
            (now, int(current["id"])),
        )
        conn.execute(
            "UPDATE bworker_candidates SET status = 'rejected', updated_at = ? "
            "WHERE scan_id = ? AND status IN ('pending', 'probing', 'probed')",
            (now, int(current["id"])),
        )

    existing = conn.execute(
        "SELECT * FROM bworker_scan WHERE work_id = ? AND epoch = ? "
        "ORDER BY id DESC LIMIT 1",
        (work_id, str(epoch)),
    ).fetchone()
    if existing is not None:
        scan_id = int(existing["id"])
        conn.execute("DELETE FROM bworker_candidates WHERE scan_id = ?", (scan_id,))
        conn.execute(
            """
            UPDATE bworker_scan
            SET query_fingerprint = ?, status = 'active', cursor = NULL,
                exhausted = 0, candidates_seen = 0, candidates_matched = 0,
                matched_count = 0, candidates_probed = 0, updated_at = ?
            WHERE id = ?
            """,
            (str(fingerprint), now, scan_id),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO bworker_scan
                (work_id, epoch, query_fingerprint, status, cursor, exhausted,
                 candidates_seen, candidates_matched, matched_count,
                 candidates_probed, created_at, updated_at)
            VALUES (?, ?, ?, 'active', NULL, 0, 0, 0, 0, 0, ?, ?)
            """,
            (work_id, str(epoch), str(fingerprint), now, now),
        )
        scan_id = int(cursor.lastrowid)
    conn.commit()
    return get_scan(conn, scan_id), True


def scan_counts(conn, scan_id: int) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS candidates,
            SUM(CASE WHEN status IN ('pending', 'probing') THEN 1 ELSE 0 END)
                AS pending,
            SUM(CASE WHEN status = 'probed' THEN 1 ELSE 0 END) AS probed,
            SUM(CASE WHEN status = 'selected' THEN 1 ELSE 0 END) AS selected,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status = 'pending' AND episode_stream_id = ''
                     AND provider_kind = 'series'
                     THEN 1 ELSE 0 END) AS pending_containers,
            COALESCE(MAX(score), 0) AS best_score,
            SUM(CASE WHEN eligible = 1 THEN 1 ELSE 0 END) AS eligible,
            SUM(CASE WHEN target_met = 1 THEN 1 ELSE 0 END) AS target_met
        FROM bworker_candidates WHERE scan_id = ?
        """,
        (int(scan_id),),
    ).fetchone()
    result = {key: int(row[key] or 0) for key in row.keys()}
    result["matched_count"] = result["candidates"]
    return result


def scan_snapshot(conn, work_id: int) -> dict:
    """Diagnostic view: the newest scan for a work row and its candidates."""
    scan = _scan_dict(conn.execute(
        "SELECT * FROM bworker_scan WHERE work_id = ? ORDER BY id DESC LIMIT 1",
        (int(work_id),),
    ).fetchone())
    if scan is None:
        return {"scan": None, "counts": {}, "candidates": []}
    return {
        "scan": scan,
        "counts": scan_counts(conn, int(scan["id"])),
        "candidates": candidate_rows(conn, work_id=int(work_id),
                                     scan_id=int(scan["id"]), limit=200),
    }


def candidate_rows(conn, *, work_id: int | None = None,
                   scan_id: int | None = None, statuses=None,
                   limit: int = 100) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if work_id is not None:
        clauses.append("work_id = ?")
        params.append(int(work_id))
    if scan_id is not None:
        clauses.append("scan_id = ?")
        params.append(int(scan_id))
    status_list = list(statuses or ())
    if status_list:
        clauses.append("status IN (" + ",".join("?" for _ in status_list) + ")")
        params.extend(str(value) for value in status_list)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT * FROM bworker_candidates{where}
        ORDER BY CASE match_kind WHEN 'tmdb' THEN 0 WHEN 'imdb' THEN 1 ELSE 2 END,
                 match_score DESC, score DESC, id
        LIMIT ?
        """,
        [*params, max(1, min(int(limit), 500))],
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["score_details"] = json.loads(item.get("score_details") or "null")
        except (TypeError, ValueError):
            pass
        result.append(item)
    return result


def update_scan(conn, scan_id: int, *, cursor=None, exhausted: bool | None = None,
                seen_increment: int = 0, matched_increment: int = 0,
                probed_increment: int = 0, now: int | None = None) -> dict:
    now = _now(now)
    assignments = ["updated_at = ?"]
    params: list[Any] = [now]
    if cursor is not None:
        assignments.append("cursor = ?")
        params.append(str(cursor))
    if exhausted is not None:
        assignments.append("exhausted = ?")
        params.append(1 if exhausted else 0)
    if seen_increment:
        assignments.append("candidates_seen = candidates_seen + ?")
        params.append(int(seen_increment))
    if matched_increment:
        assignments.append("candidates_matched = candidates_matched + ?")
        params.append(int(matched_increment))
    if probed_increment:
        assignments.append("candidates_probed = candidates_probed + ?")
        params.append(int(probed_increment))
    params.append(int(scan_id))
    conn.execute(
        f"UPDATE bworker_scan SET {', '.join(assignments)} WHERE id = ?", params
    )
    counts = conn.execute(
        "SELECT COUNT(*) AS n FROM bworker_candidates WHERE scan_id = ?",
        (int(scan_id),),
    ).fetchone()
    conn.execute(
        "UPDATE bworker_scan SET matched_count = ? WHERE id = ?",
        (int(counts["n"]) if counts else 0, int(scan_id)),
    )
    conn.commit()
    return get_scan(conn, scan_id) or {}


def set_scan_status(conn, scan_id: int, status: str,
                    now: int | None = None) -> None:
    if status not in ("active", "complete", "invalidated"):
        raise ValueError(f"Unknown scan status {status!r}.")
    conn.execute(
        "UPDATE bworker_scan SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(now), int(scan_id)),
    )
    conn.commit()


def invalidate_stale_candidates(conn, scan_id: int, now: int | None = None) -> int:
    """Reject candidates whose catalog item vanished or points elsewhere.

    The FK is ``ON DELETE SET NULL``, so a sync that deletes and reinserts
    items leaves candidate rows behind with ``item_id IS NULL``. They must
    never be probed or selected.
    """
    now = _now(now)
    rows = conn.execute(
        """
        SELECT c.id, c.item_id, c.source_id, c.provider_kind,
               c.provider_remote_id,
               i.id AS live_id, i.source_id AS live_source,
               i.kind AS live_kind, i.remote_id AS live_remote
        FROM bworker_candidates c
        LEFT JOIN items i ON i.id = c.item_id
        WHERE c.scan_id = ? AND c.status IN ('pending', 'probing', 'probed')
        """,
        (int(scan_id),),
    ).fetchall()
    stale: list[int] = []
    for row in rows:
        expected_kind = "vod" if row["provider_kind"] == "vod" else "series"
        if (row["live_id"] is None
                or int(row["live_source"]) != int(row["source_id"])
                or str(row["live_kind"]) != expected_kind
                or str(row["live_remote"]) != str(row["provider_remote_id"])):
            stale.append(int(row["id"]))
    if stale:
        conn.execute(
            "UPDATE bworker_candidates SET status = 'rejected', updated_at = ? "
            "WHERE id IN (" + ",".join("?" for _ in stale) + ")",
            [now, *stale],
        )
        conn.commit()
    return len(stale)


def record_work_progress(conn, work_id: int, *, cursor, profile_revision: int,
                         best: Mapping[str, Any] | None = None,
                         stage: str | None = None,
                         next_run_at: int | None = None,
                         current_download_id: int | None = None,
                         now: int | None = None) -> None:
    """Write engine-owned fields the bworker's COALESCE leave intact."""
    now = _now(now)
    assignments = ["cursor = ?", "profile_revision = ?", "updated_at = ?"]
    params: list[Any] = [str(cursor) if cursor is not None else None,
                         int(profile_revision), now]
    if best is not None:
        assignments.extend([
            "best_score = CASE WHEN best_score IS NULL OR best_score < ? "
            "THEN ? ELSE best_score END",
            "best_source_id = CASE WHEN best_score IS NULL OR best_score <= ? "
            "THEN ? ELSE best_source_id END",
            "best_remote_id = CASE WHEN best_score IS NULL OR best_score <= ? "
            "THEN ? ELSE best_remote_id END",
            "best_episode_stream_id = CASE WHEN best_score IS NULL OR best_score <= ? "
            "THEN ? ELSE best_episode_stream_id END",
        ])
        score = int(best.get("score") or 0)
        params.extend([
            score, score,
            score, int(best.get("source_id") or 0),
            score, str(best.get("provider_remote_id") or best.get("remote_id") or ""),
            score, str(best.get("episode_stream_id") or ""),
        ])
    if stage:
        assignments.append("stage = ?")
        params.append(str(stage))
    if next_run_at is not None:
        assignments.append("next_run_at = ?")
        params.append(int(next_run_at))
    if current_download_id is not None:
        assignments.append("current_download_id = ?")
        params.append(int(current_download_id))
    params.append(int(work_id))
    conn.execute(
        f"UPDATE bworker_work SET {', '.join(assignments)} WHERE id = ?", params
    )
    conn.commit()


__all__ = [
    "active_scan",
    "candidate_rows",
    "catalog_epoch",
    "ensure_scan",
    "get_scan",
    "invalidate_stale_candidates",
    "query_fingerprint",
    "record_work_progress",
    "scan_counts",
    "scan_snapshot",
    "set_scan_status",
    "update_scan",
]
