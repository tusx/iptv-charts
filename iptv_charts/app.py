"""Flask application factory and page routes."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, abort, g, render_template, request

from . import config, db, items, player, sync
from .api import bp as api_bp

#: Hostnames that mean "this machine". Used for both the Host header and the
#: Origin of writes.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})

# Columns the sources list needs. Kept explicit rather than SELECT * so that
# adding a column to the schema never silently changes what a template sees.
_SOURCE_LIST_SQL = """
SELECT
    s.id, s.name, s.kind, s.created_at, s.last_synced_at, s.sync_status,
    s.sync_message, s.account_status, s.exp_date, s.exp_unknown, s.is_trial,
    s.active_connections, s.max_connections, s.server_timezone,
    (SELECT COUNT(*) FROM items i
      WHERE i.source_id = s.id AND i.kind = 'live')   AS live_count,
    (SELECT COUNT(*) FROM items i
      WHERE i.source_id = s.id AND i.kind = 'vod')    AS vod_count,
    (SELECT COUNT(*) FROM items i
      WHERE i.source_id = s.id AND i.kind = 'series') AS series_count,
    (SELECT COUNT(*) FROM categories c
      WHERE c.source_id = s.id)                       AS category_count
FROM sources s
ORDER BY s.created_at DESC
"""


def create_app() -> Flask:
    app = Flask(__name__)

    # Only used for flash messages. Persisted so a restart mid-flow doesn't
    # invalidate anything the user was in the middle of.
    prefs = config.load_runtime_prefs()
    secret = prefs.get("secret_key")
    if not secret:
        secret = os.urandom(32).hex()
        prefs["secret_key"] = secret
        config.save_runtime_prefs(prefs)
    app.secret_key = secret

    db.init_db()
    app.register_blueprint(api_bp)

    @app.before_request
    def _guard_request():
        """Reject requests that did not arrive over loopback, from this app.

        This is not authentication -- there is no token to get in the way of
        loading the app. It simply refuses a ``Host`` header naming something
        other than loopback, which is what a DNS-rebinding attempt looks like:
        a page on evil.example.com resolving to 127.0.0.1 and talking to us
        under its own hostname. Genuine local use always says 127.0.0.1 or
        localhost, so this costs nothing in normal operation.
        """
        host = (request.host or "").rsplit(":", 1)[0].strip("[]")
        if host not in LOOPBACK_NAMES:
            abort(403, description="Requests must target loopback.")

        # The Host check stops a rebind, not a page on another site posting to
        # us: that request carries the loopback Host, and the browser sends it
        # without asking us first. Writes therefore also have to come from
        # something we served. Reads are left alone -- they change nothing, and
        # a cross-site read is already blocked by the same-origin policy.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return

        if request.headers.get("Sec-Fetch-Site", "") in {"cross-site", "same-site"}:
            abort(403, description="Cross-site requests are not accepted.")
        origin = request.headers.get("Origin")
        if origin:
            # The port varies with whatever was free at startup, so only the
            # host is compared. A sandboxed frame sends "null", which parses to
            # no host and is refused.
            if (urlsplit(origin).hostname or "") not in LOOPBACK_NAMES:
                abort(403, description="Requests must come from this app.")

    # ---------------------------------------------------------------- database
    def get_conn() -> sqlite3.Connection:
        if "db" not in g:
            g.db = db.connect()
        return g.db

    @app.teardown_appcontext
    def _close_conn(exception: BaseException | None) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # ------------------------------------------------------------ template ctx
    @app.context_processor
    def inject_globals() -> dict:
        found = player.resolve(get_conn())
        return {
            "app_name": config.APP_NAME,
            "app_version": __import__("iptv_charts").__version__,
            "now": int(time.time()),
            "staleness_hours": _staleness_hours(get_conn()),
            "external_player": found,
            "containerized": player.is_container(),
            "format_age": format_age,
            "format_expiry": format_expiry,
        }

    # ------------------------------------------------------------------ routes
    @app.route("/")
    def sources_list():
        conn = get_conn()
        rows = db.get_all(conn, _SOURCE_LIST_SQL)
        # Rendered into the page so a reload mid-sync shows progress
        # immediately, rather than waiting for the first poll to come back.
        return render_template(
            "sources.html", sources=rows, sync_state=sync.snapshot()
        )

    @app.route("/api/health")
    def health():
        conn = get_conn()
        found = player.resolve(conn)
        return {
            "status": "ok",
            "version": __import__("iptv_charts").__version__,
            "data_dir": str(config.data_dir()),
            "db_path": str(config.db_path()),
            "db_bytes": config.db_path().stat().st_size if config.db_path().exists() else 0,
            "sources": db.get_one(conn, "SELECT COUNT(*) AS n FROM sources")["n"],
            "items": db.get_one(conn, "SELECT COUNT(*) AS n FROM items")["n"],
            "sqlite_version": sqlite3.sqlite_version,
            "containerized": player.is_container(),
            "play_queue": str(config.play_queue_path() or ""),
            "player": {"id": found.id, "label": found.label} if found else None,
        }

    @app.route("/sources/<int:source_id>/browse")
    def browse(source_id: int):
        conn = get_conn()
        source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (source_id,))
        if source is None:
            abort(404, description="That playlist no longer exists.")

        # The category dropdown is built client-side (it has to be rebuilt per
        # kind tab), so only the kind counts are rendered into the page.
        return render_template(
            "browse.html",
            source=source,
            counts=items.counts_by_kind(conn, source_id),
        )

    @app.route("/settings")
    def settings():
        """Where the external player is chosen.

        The player list is probed here rather than cached from the last page
        render: this is the one page where being a minute out of date is a
        bug rather than a detail.
        """
        conn = get_conn()
        command = db.get_setting(conn, player.COMMAND_KEY, "") or ""
        return render_template(
            "settings.html",
            # The custom command is offered as its own radio below, so it is
            # kept out of the detected list rather than listed twice.
            players=[found for found in player.available(conn, refresh=True)
                     if found.id != "custom"],
            preferred=db.get_setting(conn, player.PREFERENCE_KEY, "auto") or "auto",
            command=command,
            custom_ready=bool(command.strip()),
            bridged=config.play_queue_path() is not None,
            bridge_alive=player.bridge_is_alive(),
        )

    # Playback has no page of its own: the browse page opens the player in a
    # dialog, so that playing something does not cost the user their place in
    # the list. See static/js/playback.js.

    return app


# --------------------------------------------------------------------- helpers
def _staleness_hours(conn: sqlite3.Connection) -> int:
    try:
        return int(db.get_setting(conn, "stale_after_hours", "24") or 24)
    except (TypeError, ValueError):
        return 24


def format_age(timestamp: int | None) -> str:
    """Human-readable age of a unix timestamp, e.g. '3 days ago'."""
    if not timestamp:
        return "never"
    delta = int(time.time()) - int(timestamp)
    if delta < 0:
        return "just now"
    if delta < 90:
        return "just now"
    for limit, divisor, unit in (
        (3600, 60, "minute"),
        (86400, 3600, "hour"),
        (2592000, 86400, "day"),
        (31536000, 2592000, "month"),
    ):
        if delta < limit:
            value = delta // divisor
            return f"{value} {unit}{'s' if value != 1 else ''} ago"
    years = delta // 31536000
    return f"{years} year{'s' if years != 1 else ''} ago"


def format_expiry(exp_date: int | None, exp_unknown: bool) -> dict:
    """Interpret an Xtream ``exp_date``.

    Providers use null, 0 and negative values interchangeably to mean "no
    expiry recorded". Computing a countdown from any of them yields a
    confident, wrong date, so we report that we don't know instead.
    """
    if exp_unknown or not exp_date or int(exp_date) <= 0:
        return {"known": False, "label": "No expiry set", "days": None, "expired": False}

    exp = int(exp_date)
    days = (exp - int(time.time())) // 86400
    label = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d")
    return {"known": True, "label": label, "days": days, "expired": days < 0}
