"""Read-only HTTP surface for the advisory update check.

Registration is integration-owned; ``create_app`` needs exactly::

    from .update_web import bp as update_web_bp
    ...
    app.register_blueprint(update_web_bp)

The blueprint exposes ``GET /api/update/status`` only. It never downloads or
applies an update and it mutates nothing, so it deliberately has no POST
route. ``?refresh=1`` performs a manual check that bypasses the short cache;
a normal page load can omit it and use the cached answer.

Failures are reported inside a 200 response (``ok: true`` with a non-empty
``error``) rather than as an error status: an unreachable GitHub is not an
application failure, and the UI must stay quiet instead of showing an error
toast. The app-wide loopback/Origin guard still applies to every request.
"""

from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, request

from . import db, updates

logger = logging.getLogger("iptv_charts.update_web")

bp = Blueprint("update", __name__, url_prefix="/api/update")

_TRUE = {"1", "true", "yes", "on", "force"}


def _conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@bp.get("/status")
def update_status():
    """Return ``current``/``latest``/``update_available`` and related fields.

    ``error`` is informational and always accompanies a successful response;
    ``enabled`` is false when the user or an operator disabled checking.
    """
    force = (request.args.get("refresh") or "").strip().lower() in _TRUE
    try:
        payload = updates.check_for_update(_conn(), force=force)
    except Exception:
        # updates.check_for_update already contains its own failure handling;
        # this is a last line of defence so a page poll can never 500.
        logger.exception("update status check failed")
        payload = {
            "enabled": True,
            "current": __import__("iptv_charts").__version__,
            "latest": None,
            "tag": None,
            "update_available": False,
            "url": None,
            "checked_at": None,
            "cached": False,
            "error": updates.OFFLINE_MESSAGE,
            "update_command": None,
        }
    response = jsonify({"ok": True, **payload})
    response.headers["Cache-Control"] = "no-store"
    return response
