"""Storage page and its JSON endpoints.

Registration is integration-owned; ``create_app`` needs exactly::

    from .storage_web import bp as storage_web_bp
    ...
    app.register_blueprint(storage_web_bp)

The blueprint carries both the server-rendered page (``/storage``) and the
``/api/storage/*`` endpoints. It relies on the application-wide loopback and
Origin guard in app.py for write protection, and follows the existing
``{ok: true, ...}`` / ``{ok: false, error}`` API convention.
"""

from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, render_template, request

from . import config, db, storage

logger = logging.getLogger("iptv_charts.storage_web")

bp = Blueprint("storage", __name__)


def _conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _payload() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise storage.StorageError("Expected a JSON object.")
    return payload


def _error(exc: Exception):
    if isinstance(exc, storage.StorageError):
        return _fail(str(exc), getattr(exc, "status", 400))
    if isinstance(exc, PermissionError):
        return _fail("Permission denied by the filesystem.", 403)
    logger.exception("storage request failed: %s", exc)
    return _fail("The storage request could not be completed. See the app log.", 500)


@bp.get("/storage")
def storage_page():
    return render_template(
        "storage.html",
        media_dir=str(storage.media_root()),
        media_host_dir=config.media_host_dir(),
        folders=storage.folder_options(),
    )


@bp.get("/api/storage/tree")
def storage_tree():
    try:
        payload = storage.tree(_conn(), request.args.get("folder") or None)
    except Exception as exc:  # expected errors map to their status; others log
        return _error(exc)
    response = jsonify({"ok": True, **payload})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.delete("/api/storage/file")
def storage_delete_file():
    try:
        payload = _payload()
        result = storage.delete_file(
            _conn(),
            payload.get("path"),
            confirm=payload.get("confirm") is True,
            confirm_active=payload.get("confirm_active") is True,
        )
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


@bp.post("/api/storage/empty")
def storage_empty_folder():
    try:
        payload = _payload()
        result = storage.empty_folder(
            _conn(),
            payload.get("folder"),
            confirm=payload.get("confirm") is True,
            confirm_active=payload.get("confirm_active") is True,
        )
    except Exception as exc:
        return _error(exc)
    return jsonify({"ok": True, **result})


__all__ = ["bp"]
