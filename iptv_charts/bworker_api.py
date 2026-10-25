"""HTTP surface for the bworker: status, pause/resume, activity and policies.

Reads are cheap: SQLite for current work/policies, Valkey for the activity
list. The activity log being unavailable is reported as ``degraded`` with a
200 response -- the page must still render current work from SQLite and must
not spin or error because an in-memory sidecar is restarting.

``POST /pause`` and ``POST /resume`` write only the SQLite pause authority.
The bworker owns activity writes, so pausing from the web does not append a
"paused" entry here; the loop writes exactly one after the current bounded
unit finishes.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from . import activity_log, db
from . import bworker as bworker_runtime
from . import bworker_store as store

logger = logging.getLogger("iptv_charts.bworker_api")

bp = Blueprint("bworker_api", __name__, url_prefix="/api/bworker")


def _error(message: str, status: int = 400):
    return jsonify({"ok": False, "error": str(message)}), status


def _json_object() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _current(conn, state: Mapping[str, Any]) -> dict | None:
    work_id = state.get("current_work_id")
    if not work_id:
        if state.get("current_work_label"):
            return {
                "work_id": None,
                "work_type": None,
                "label": state.get("current_work_label"),
                "target": None,
                "stage": None,
                "status": "paused" if state.get("paused") else "running",
                "started_at": state.get("started_at"),
            }
        return None
    work = store.get_work(conn, int(work_id))
    if work is None:
        return {
            "work_id": int(work_id),
            "work_type": None,
            "label": state.get("current_work_label"),
            "target": None,
            "stage": None,
            "status": "paused" if state.get("paused") else "running",
            "started_at": state.get("started_at"),
        }
    target = store.work_target(conn, work)
    return {
        "work_id": int(work["id"]),
        "work_type": work.get("work_type"),
        "label": state.get("current_work_label") or target.get("label"),
        "target": target,
        "stage": work.get("stage"),
        "status": work.get("status"),
        "started_at": work.get("started_at"),
    }


def _activity_limits(conn) -> tuple[int, int]:
    def _as_int(key: str, default: int) -> int:
        try:
            return int(float(db.get_setting(conn, key, str(default)) or default))
        except (TypeError, ValueError):
            return default

    cap = max(10, _as_int("activity_log_cap", activity_log.DEFAULT_CAP))
    trim_to = max(1, min(_as_int("activity_log_trim_to",
                                 activity_log.DEFAULT_TRIM_TO), cap - 1))
    return cap, trim_to


@bp.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/status")
def status():
    conn = db.connect()
    try:
        payload = bworker_runtime.status(
            conn, activity=activity_log.default_store())
        payload["ok"] = True
        return jsonify(payload)
    except Exception as exc:
        logger.exception("bworker API: status failed")
        return _error(f"Could not read bworker status: {exc}", 500)
    finally:
        conn.close()


@bp.post("/pause")
def pause():
    conn = db.connect()
    try:
        result = store.set_paused(conn, True)
        state = result["state"]
        return jsonify({
            "ok": True,
            "paused": bool(state["paused"]),
            "changed": bool(result["changed"]),
            "status": state.get("status"),
            "current_work_id": state.get("current_work_id"),
            "current_work_label": state.get("current_work_label"),
            "heartbeat_at": state.get("heartbeat_at"),
            "message": ("Bworker paused; the current unit will finish."
                        if result["changed"] else "Bworker is already paused."),
        })
    except Exception as exc:
        logger.exception("bworker API: pause failed")
        return _error(f"Could not pause the bworker: {exc}", 500)
    finally:
        conn.close()


@bp.post("/resume")
def resume():
    conn = db.connect()
    try:
        result = store.set_paused(conn, False)
        state = result["state"]
        return jsonify({
            "ok": True,
            "paused": bool(state["paused"]),
            "changed": bool(result["changed"]),
            "status": state.get("status"),
            "heartbeat_at": state.get("heartbeat_at"),
            "message": ("Bworker resumed." if result["changed"]
                        else "Bworker is already running."),
        })
    except Exception as exc:
        logger.exception("bworker API: resume failed")
        return _error(f"Could not resume the bworker: {exc}", 500)
    finally:
        conn.close()


@bp.get("/activity")
def activity():
    conn = db.connect()
    try:
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        since_raw = request.args.get("since_id")
        try:
            since_id = int(since_raw) if since_raw not in (None, "") else None
        except (TypeError, ValueError):
            since_id = None
        activity_store = activity_log.default_store()
        fetch = activity_log.DEFAULT_CAP if since_id is not None else limit
        try:
            entries = list(activity_store.recent(fetch))
        except Exception:
            entries = []
        if since_id is not None:
            entries = [entry for entry in entries
                       if int(entry.get("id") or 0) > since_id]
            entries = entries[:limit]
        try:
            activity_status = activity_store.status()
        except Exception as exc:
            activity_status = {
                "backend": "unknown", "available": False, "degraded": True,
                "error": f"{type(exc).__name__}: {exc}",
                "cap": activity_log.DEFAULT_CAP,
                "trim_to": activity_log.DEFAULT_TRIM_TO,
            }
        state = store.get_state(conn)
        cap, trim_to = _activity_limits(conn)
        available = bool(activity_status.get("available"))
        try:
            refresh = int(float(db.get_setting(
                conn, "activity_refresh_seconds", "5") or 5))
        except (TypeError, ValueError):
            refresh = 5
        newest = 0
        if entries:
            newest = max(int(entry.get("id") or 0) for entry in entries)
        payload = {
            "ok": True,
            "paused": bool(state["paused"]),
            "status": ("paused" if state["paused"]
                       else str(state.get("status") or "missing")),
            "current": _current(conn, state),
            "entries": entries,
            "newest_id": newest or (since_id or 0),
            "since_id": since_id,
            "limit": limit,
            "cap": cap,
            "trim_to": trim_to,
            "refresh_seconds": max(1, refresh),
            "available": available,
            "degraded": not available,
            "error": activity_status.get("error"),
            "backend": activity_status.get("backend"),
        }
        return jsonify(payload)
    except Exception as exc:
        logger.exception("bworker API: activity failed")
        return _error(f"Could not read the activity log: {exc}", 500)
    finally:
        conn.close()


@bp.get("/policies")
def get_policies():
    conn = db.connect()
    try:
        return jsonify({"ok": True, "policies": store.policies(conn)})
    except Exception as exc:
        logger.exception("bworker API: policies read failed")
        return _error(f"Could not read bworker policies: {exc}", 500)
    finally:
        conn.close()


def _policy_items(payload: Mapping[str, Any]) -> list[dict]:
    if isinstance(payload.get("policies"), list):
        raw_items = payload["policies"]
    elif payload.get("work_type"):
        work_type = payload["work_type"]
        if isinstance(payload.get("changes"), Mapping):
            changes = dict(payload["changes"])
        else:
            changes = {key: value for key, value in payload.items()
                       if key not in ("work_type", "changes")}
        raw_items = [{"work_type": work_type, "changes": changes}]
    elif payload and all(isinstance(value, Mapping) for value in payload.values()):
        # {"movie_search": {"enabled": false}, ...}
        raw_items = [{"work_type": key, "changes": dict(value)}
                     for key, value in payload.items()]
    else:
        raw_items = []
    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping) or not raw.get("work_type"):
            raise ValueError("Every policy entry needs a work_type.")
        changes = raw.get("changes")
        if not isinstance(changes, Mapping):
            changes = {key: value for key, value in raw.items()
                       if key != "work_type"}
        items.append({"work_type": str(raw["work_type"]),
                      "changes": dict(changes)})
    if not items:
        raise ValueError("No policies were provided.")
    return items


@bp.patch("/policies")
def patch_policies():
    payload = _json_object()
    conn = db.connect()
    try:
        items = _policy_items(payload)
        for item in items:
            store.update_policy(conn, item["work_type"], item["changes"])
        return jsonify({"ok": True, "policies": store.policies(conn)})
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        logger.exception("bworker API: policy update failed")
        return _error(f"Could not update bworker policies: {exc}", 500)
    finally:
        conn.close()


@bp.post("/reset")
def reset():
    payload = _json_object()
    conn = db.connect()
    try:
        if not payload:
            return _error("Specify a work_id, an entity, work_key, or all=true.")
        work_id = payload.get("work_id")
        work_key = payload.get("work_key")
        entity_type = payload.get("entity_type") or payload.get("kind")
        entity_id = payload.get("entity_id")
        if entity_id is None and payload.get("id") is not None:
            entity_id = payload.get("id")
        reset_all = bool(payload.get("all"))
        if work_key and work_id is None:
            work = store.get_work_by_key(conn, str(work_key))
            if work is None:
                return _error("That work unit does not exist.", 404)
            work_id = int(work["id"])
        if work_id is not None:
            try:
                work_id = int(work_id)
            except (TypeError, ValueError):
                return _error("work_id must be an integer.")
            if store.get_work(conn, work_id) is None:
                return _error("That work unit does not exist.", 404)
        elif entity_type is not None:
            if str(entity_type) not in ("movie", "series", "episode", "source",
                                        "global"):
                return _error("Unknown entity type.")
            if entity_id is not None:
                try:
                    entity_id = int(entity_id)
                except (TypeError, ValueError):
                    return _error("entity_id must be an integer.")
        elif not reset_all:
            return _error("Specify work_id, work_key, entity_type/entity_id, "
                          "or all=true.")
        result = store.reset_work(
            conn,
            work_id=int(work_id) if work_id is not None else None,
            entity_type=str(entity_type) if entity_type is not None else None,
            entity_id=(int(entity_id) if entity_id is not None else None),
        )
        if work_id is not None and not result.get("updated"):
            return _error("That work unit does not exist.", 404)
        state = store.get_state(conn)
        return jsonify({
            "ok": True,
            "paused": bool(state["paused"]),
            "updated": int(result.get("updated") or 0),
            "reset_requested": int(result.get("reset_requested") or 0),
            "work_ids": result.get("work_ids") or [],
        })
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        logger.exception("bworker API: reset failed")
        return _error(f"Could not reset work: {exc}", 500)
    finally:
        conn.close()


__all__ = ["bp"]
