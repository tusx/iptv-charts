"""Flask application factory and page routes."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import Flask, abort, g, render_template, request

from . import (bworker, config, connections, db, items, jobs, metadata, naming,
               player, sources, sync)
from .api import bp as api_bp
from .bworker_api import bp as bworker_api_bp
from .explore_web import bp as explore_web_bp
from .import_web import bp as import_web_bp
from .manager_api import bp as manager_api_bp
from .manager_web import bp as manager_web_bp
from .settings_sections import bp as settings_sections_bp
from .stage_web import bp as stage_web_bp
from .storage_web import bp as storage_web_bp
from .update_web import bp as update_web_bp

#: Hostnames that mean "this machine". Used for both the Host header and the
#: Origin of writes.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def configure_logging() -> logging.Logger:
    """Send the app's own log to stdout, where the container runtime collects it.

    Its own logger, with propagation off: the root logger is shared with
    waitress, and its per-request lines would bury the handful of actions that
    are worth reading. ``IPTV_CHARTS_LOG_LEVEL`` turns the volume up without a
    code change (DEBUG adds the exact argv handed to a player).
    """
    logger = logging.getLogger("iptv_charts")
    logger.setLevel(getattr(
        logging, os.environ.get("IPTV_CHARTS_LOG_LEVEL", "INFO").upper(), logging.INFO
    ))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(module)s: %(message)s",
            "%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = logging.getLogger("iptv_charts.app")

# Columns the sources list needs. Kept explicit rather than SELECT * so that
# adding a column to the schema never silently changes what a template sees.
_SOURCE_LIST_SQL = """
SELECT
    s.id, s.name, s.kind, s.created_at, s.last_synced_at, s.sync_status,
    s.sync_message, s.account_status, s.exp_date, s.exp_unknown, s.is_trial,
    s.active_connections, s.max_connections, s.connection_limit, s.server_timezone,
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
    configure_logging()
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
    app.register_blueprint(manager_api_bp, url_prefix="/api")
    app.register_blueprint(bworker_api_bp)
    app.register_blueprint(manager_web_bp)
    app.register_blueprint(explore_web_bp)
    app.register_blueprint(import_web_bp)
    app.register_blueprint(storage_web_bp)
    app.register_blueprint(settings_sections_bp)
    app.register_blueprint(stage_web_bp)
    app.register_blueprint(update_web_bp)

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
        g.request_started = time.perf_counter()

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
    def home():
        conn = get_conn()
        sources_rows = db.get_all(conn, _SOURCE_LIST_SQL)
        movies = db.get_one(
            conn,
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(file_path IS NULL), 0) AS missing, "
            "COALESCE(SUM(file_path IS NOT NULL), 0) AS downloaded, "
            "COALESCE(SUM(monitored = 1), 0) AS monitored FROM movies",
        )
        series = db.get_one(conn, "SELECT COUNT(*) AS total, "
                            "COALESCE(SUM(monitored = 1), 0) AS monitored FROM series")
        episodes = db.get_one(
            conn,
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(file_path IS NOT NULL), 0) AS downloaded, "
            "COALESCE(SUM(air_date IS NOT NULL AND air_date <= date('now') "
            "AND file_path IS NULL), 0) AS missing FROM series_episodes",
        )
        return render_template(
            "home.html",
            sources=sources_rows,
            sync_state=sync.snapshot(),
            connection_counts=connections.snapshot(),
            playlist_count=len(sources_rows),
            catalog_items=sum(
                (row["live_count"] or 0) + (row["vod_count"] or 0)
                + (row["series_count"] or 0)
                for row in sources_rows
            ),
            movie_counts=dict(movies),
            series_counts={
                "total": series["total"],
                "monitored": series["monitored"],
                "episodes": episodes["total"],
                "downloaded_episodes": episodes["downloaded"],
                "missing_episodes": episodes["missing"],
            },
            tmdb_configured=metadata.has_tmdb(conn),
        )

    @app.route("/playlists")
    def sources_list():
        conn = get_conn()
        rows = db.get_all(conn, _SOURCE_LIST_SQL)
        # Rendered into the page so a reload mid-sync shows progress
        # immediately, rather than waiting for the first poll to come back.
        return render_template(
            "sources.html", sources=rows, sync_state=sync.snapshot(),
            connection_counts=connections.snapshot(),
        )

    @app.route("/playlists/<int:source_id>/edit")
    def edit_source(source_id: int):
        conn = get_conn()
        source = db.get_one(
            conn,
            "SELECT id, name, kind, base_url, username, m3u_url, "
            "request_headers, connection_limit, max_connections, "
            "account_status, exp_date, exp_unknown, is_trial, "
            "last_synced_at, sync_status, sync_message "
            "FROM sources WHERE id = ?",
            (source_id,),
        )
        if source is None:
            abort(404, description="That playlist no longer exists.")
        return render_template(
            "source_edit.html",
            source=dict(source),
            headers=sources.request_headers(source),
            counts=items.counts_by_kind(conn, source_id),
            connection_counts=connections.snapshot(),
        )

    @app.route("/api/health")
    def health():
        conn = get_conn()
        found = player.resolve(conn)
        bworker_health = bworker.status(conn)
        activity = bworker_health.get("activity") or {}
        return {
            # A paused bworker is alive on purpose; a dead one is not healthy,
            # because syncs, downloads and automatic searches would sit forever.
            "status": "ok" if bworker_health.get("alive") else "error",
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
            # The UI can be perfectly healthy while no background worker is
            # running; surface that instead of leaving syncs queued forever.
            "worker": jobs.worker_status(conn),
            "worker_jobs": jobs.counts(conn),
            "bworker": {
                "alive": bool(bworker_health.get("alive")),
                "paused": bool(bworker_health.get("paused")),
                "status": bworker_health.get("status"),
                "heartbeat_age": bworker_health.get("heartbeat_age"),
                "current_work_id": bworker_health.get("current_work_id"),
                "current_work_label": bworker_health.get("current_work_label"),
                "current": bworker_health.get("current"),
            },
            "activity": {
                "available": bool(activity.get("available")),
                "degraded": bool(activity.get("degraded", True)),
                "backend": activity.get("backend"),
                "error": activity.get("error"),
            },
            "bworker_queue": bworker_health.get("queue"),
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
        """Player, metadata, media, download and scheduler settings.

        The player list is probed here rather than cached from the last page
        render: this is the one page where being a minute out of date is a
        bug rather than a detail.
        """
        conn = get_conn()
        command = db.get_setting(conn, player.COMMAND_KEY, "") or ""
        available = player.available(conn, refresh=True)

        def setting(key: str, default: str) -> str:
            return db.get_setting(conn, key, default) or default

        templates = {
            "movie_folder": setting("movie_folder_template", naming.DEFAULT_MOVIE_FOLDER),
            "movie_file": setting("movie_file_template", naming.DEFAULT_MOVIE_FILE),
            "series_folder": setting("series_folder_template", naming.DEFAULT_SERIES_FOLDER),
            "season_folder": setting("season_folder_template", naming.DEFAULT_SEASON_FOLDER),
            "episode_file": setting("episode_file_template", naming.DEFAULT_EPISODE_FILE),
        }
        return render_template(
            "settings.html",
            # The custom command is offered as its own radio below, so it is
            # kept out of the detected list rather than listed twice.
            players=[found for found in available if found.id != "custom"],
            preferred=db.get_setting(conn, player.PREFERENCE_KEY, "auto") or "auto",
            command=command,
            custom_ready=bool(command.strip()),
            bridged=config.play_queue_path() is not None,
            bridge_alive=player.bridge_is_alive(),
            # The placeholder player means the bridge is running but is too old
            # to say which players it has -- worth saying, because restarting it
            # is what makes the choice real.
            bridge_outdated=any(found.id == player.BRIDGE_PLAYER_ID
                                for found in available),
            tmdb_configured=metadata.has_tmdb(conn),
            tvdb_configured=metadata.has_tvdb(conn),
            metadata_language=metadata.language(conn),
            media_dir=str(config.media_dir()),
            media_host_dir=config.media_host_dir(),
            media_dir_configured=bool(os.environ.get("IPTV_CHARTS_MEDIA_DIR")),
            templates=templates,
            download_concurrency=setting("download_concurrency", "1"),
            download_max_attempts=setting("download_max_attempts", "3"),
            search_interval_hours=setting("search_interval_hours", "6"),
            refresh_interval_hours=setting("refresh_interval_hours", "168"),
            catalog_refresh_hours=setting("catalog_refresh_hours", "24"),
            ffmpeg_available=bool(shutil.which("ffmpeg")),
            ffprobe_available=bool(shutil.which("ffprobe")),
            probe_cache_hours=setting("probe_cache_hours", "24"),
            probe_timeout_seconds=setting("probe_timeout_seconds", "30"),
            probe_max_candidates=setting("probe_max_candidates", "6"),
            probe_search_budget=setting("probe_search_budget", "60"),
            profile_count=db.get_one(
                conn, "SELECT COUNT(*) AS n FROM quality_profiles"
            )["n"],
        )

    # Playback has no page of its own: the browse page opens the player in a
    # dialog, so that playing something does not cost the user their place in
    # the list. See static/js/playback.js.

    @app.after_request
    def _log_action(response):
        """One line per action, not per request.

        Reads are left out (the sync poll runs every second, and page loads are
        not actions), so what lands in the container log is what someone
        actually did: the writes, and anything that failed.
        """
        # The app has no authentication and can display provider URLs/usernames
        # on the edit page. Refuse framing so another page cannot clickjack a
        # local user into editing or deleting a playlist.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy", "frame-ancestors 'none'"
        )
        if request.method == "GET" and response.status_code < 400:
            return response
        elapsed = (time.perf_counter() - getattr(g, "request_started", 0)) * 1000
        logger.info("%s %s -> %d (%.0f ms)", request.method, request.path,
                    response.status_code, elapsed)
        return response

    logger.info("starting %s", __import__("iptv_charts").__version__)
    logger.info("data: %s", config.data_dir())
    queue = config.play_queue_path()
    if queue is None:
        logger.info("playback: direct (players are started by this process)")
    else:
        logger.info("playback: bridged (queue %s, bridge %s)", queue,
                    "running" if player.bridge_is_alive() else "NOT running")
    # Outside any request, so the connection needs an explicit context: the
    # teardown handler closes it again as soon as the block ends.
    with app.app_context():
        found = player.resolve(get_conn())
    logger.info("player: %s", found.label if found else "none available")

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
