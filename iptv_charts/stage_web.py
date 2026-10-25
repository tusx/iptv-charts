"""Item stage and durable-history JSON endpoints.

Absolute routes, no blueprint prefix:

    GET /api/movies/<movie_id>/stage
    GET /api/series/<series_id>/stage
    GET /api/series/<series_id>/episodes/<episode_id>/stage
    GET /api/stages

Every response follows the existing ``{ok: true, ...}`` /
``{ok: false, error}`` convention and carries ``Cache-Control: no-store`` so
a detail page polling the stage never shows a cached transition. Expected
missing entities are 404s. Provider URLs are redacted by
``bworker_events`` before a message reaches JSON.

Registration is integration-owned; ``create_app`` needs exactly::

    from .stage_web import bp as stage_web_bp
    ...
    app.register_blueprint(stage_web_bp)

The same data is available to a server-rendered route (A09/A10) without an
HTTP round trip through the helpers in ``bworker_events``:

    from . import bworker_events
    context = bworker_events.movie_stage_context(conn, movie_id)
    context = bworker_events.series_stage_context(conn, series_id)
    context = bworker_events.episode_stage_context(conn, series_id, episode_id)

They return ``None`` when the entity is missing (or, for the episode helper,
when the episode belongs to another series). Each context has the same
``stage``/``label``/``status``/``message``/``progress``/``work``/``events``
shape as the JSON route, plus series ``episodes`` and ``counts``.
"""

from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify

from . import bworker_events, db

logger = logging.getLogger("iptv_charts.stage_web")

bp = Blueprint("stage", __name__)


def _conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": str(message)}), status


@bp.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/api/movies/<int:movie_id>/stage")
def movie_stage(movie_id: int):
    try:
        context = bworker_events.movie_stage_context(_conn(), movie_id)
    except Exception:
        logger.exception("stage API: could not read movie #%d", movie_id)
        return _fail("Could not read the movie stage.", 500)
    if context is None:
        return _fail("That movie no longer exists.", 404)
    return jsonify({"ok": True, **context})


@bp.get("/api/series/<int:series_id>/stage")
def series_stage(series_id: int):
    try:
        context = bworker_events.series_stage_context(_conn(), series_id)
    except Exception:
        logger.exception("stage API: could not read series #%d", series_id)
        return _fail("Could not read the series stage.", 500)
    if context is None:
        return _fail("That series no longer exists.", 404)
    return jsonify({"ok": True, **context})


@bp.get("/api/series/<int:series_id>/episodes/<int:episode_id>/stage")
def episode_stage(series_id: int, episode_id: int):
    try:
        context = bworker_events.episode_stage_context(
            _conn(), series_id, episode_id)
    except Exception:
        logger.exception("stage API: could not read episode #%d of series #%d",
                         episode_id, series_id)
        return _fail("Could not read the episode stage.", 500)
    if context is None:
        return _fail("That episode no longer exists in this series.", 404)
    return jsonify({"ok": True, **context})


@bp.get("/api/stages")
def stages():
    """Stage/status labels and badge hints used by the item UIs."""
    return jsonify({"ok": True, **bworker_events.stages_payload()})


__all__ = ["bp"]
