"""Section-owned settings pages and APIs (0.8.0 settings split).

The global settings page keeps only metadata, refresh, backup/restore,
profiles and scan. External playback and playlist defaults belong to IPTV,
movie naming/search defaults to Movies, and series naming/monitor defaults to
Series. This module owns those pages plus their save endpoints.

Registration is intentionally *not* in ``app.py`` (integration-owned). The
integration change is two lines::

    from .settings_sections import bp as settings_sections_bp
    app.register_blueprint(settings_sections_bp)

Every write here is JSON-only and relies on the app-wide loopback/Origin guard
installed by ``create_app``. Values are validated together before a single
``BEGIN IMMEDIATE`` upsert, so a bad field cannot partially save a page.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from flask import Blueprint, g, jsonify, render_template, request

from . import (bworker_store as store, config, db, player, settings_service)

logger = logging.getLogger("iptv_charts.settings_sections")

bp = Blueprint("settings_sections", __name__)


# --------------------------------------------------------------------- common
def _conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _payload() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    return payload


def _fail(message: str, status: int = 400, section: str | None = None):
    payload: dict[str, Any] = {"ok": False, "error": str(message)}
    if section:
        payload["section"] = section
    return jsonify(payload), status


def _setting(conn, key: str, default: str) -> str:
    return str(db.get_setting(conn, key, default) or default)


def _builtin_profiles(conn) -> list[dict]:
    """The three immutable builtins, in builder order, for a default picker."""
    present = {
        str(row["builtin_key"])
        for row in conn.execute(
            "SELECT builtin_key FROM quality_profiles WHERE builtin_key IS NOT NULL"
        ).fetchall()
    }
    return [
        {"key": spec["builtin_key"], "name": spec["name"],
         "resolution": spec["resolution"]}
        for spec in db.BUILTIN_PROFILES
        if spec["builtin_key"] in present
    ]


def _policies(conn) -> list[dict]:
    """Every policy row in the work-type display order (not alphabetical)."""
    rows = {row["work_type"]: row for row in store.policies(conn)}
    ordered: list[dict] = []
    for work_type in store.WORK_TYPES:
        row = rows.get(work_type)
        if row is not None:
            ordered.append(row)
    # A database that somehow lacks a canonical row still gets an editor row
    # from the fallback instead of hiding the work type.
    for work_type, row in rows.items():
        if row not in ordered:
            ordered.append(row)
    return ordered


def _policies_context(conn) -> dict:
    return {
        "policies": _policies(conn),
        "policies_url": "/api/settings/sections/policies",
        "policies_reset_url": "/api/settings/sections/policies/reset",
    }


def _section_header(section: str) -> dict:
    if section == "movies":
        return {
            "title": "Movie settings",
            "subtitle": "Folder and file naming, add defaults and movie search tuning.",
            "library_dir": str(config.movie_library_dir()),
            "library_label": "Movies folder",
        }
    return {
        "title": "Series settings",
        "subtitle": "Folder, season and episode naming, monitor defaults and series search tuning.",
        "library_dir": str(config.series_library_dir()),
        "library_label": "Series folder",
    }


# ------------------------------------------------------------------------ IPTV
def _iptv_snapshot(conn) -> dict:
    available = player.available(conn, refresh=True)
    command = _setting(conn, player.COMMAND_KEY, "")
    return {
        "external_player_enabled": player.enabled(conn),
        "preferred_player": _setting(conn, player.PREFERENCE_KEY, "auto"),
        "player_command": command,
        "custom_ready": bool(command.strip()),
        "players": [
            {"id": found.id, "label": found.label, "flavour": found.flavour}
            for found in available if found.id != "custom"
        ],
        "bridged": config.play_queue_path() is not None,
        "bridge_alive": player.bridge_is_alive(),
        "bridge_outdated": any(found.id == player.BRIDGE_PLAYER_ID
                               for found in available),
        "stale_after_hours": _setting(conn, "stale_after_hours", "24"),
    }


@bp.get("/api/settings/sections/iptv")
def get_iptv_settings():
    conn = _conn()
    try:
        payload = _iptv_snapshot(conn)
        payload["ok"] = True
        return jsonify(payload)
    except Exception as exc:  # noqa: BLE001 - report, never 500 the page silently
        logger.exception("IPTV settings read failed")
        return _fail(f"Could not read IPTV settings: {exc}", 500, "player")


@bp.post("/api/settings/sections/iptv")
def save_iptv_settings():
    conn = _conn()
    try:
        return jsonify(settings_service.save_iptv(conn, _payload()))
    except settings_service.SettingsError as exc:
        return _fail(str(exc), 400, exc.section)
    except ValueError as exc:
        return _fail(str(exc), 400, "player")
    except Exception as exc:  # noqa: BLE001
        logger.exception("IPTV settings save failed")
        return _fail(f"Could not save IPTV settings: {exc}", 500, "player")


@bp.post("/api/settings/sections/iptv/command-check")
def check_iptv_command():
    """Validate custom-command syntax without running anything."""
    try:
        payload = _payload()
        command = str(payload.get("player_command") or "").strip()
        if not command:
            return _fail("Enter a command to check.", 400, "player")
        if len(command) > player.COMMAND_MAX_LENGTH:
            return _fail(
                f"The custom command is limited to {player.COMMAND_MAX_LENGTH} characters.",
                400, "player",
            )
        argv = player.parse_command(command)
        return jsonify({
            "ok": True,
            "argv": list(argv),
            "message": "Command syntax is valid. Nothing was run.",
        })
    except ValueError as exc:
        return _fail(str(exc), 400, "player")


# ---------------------------------------------------------------------- movies
@bp.get("/movies/settings")
def movies_settings():
    conn = _conn()
    return render_template(
        "movies_settings.html",
        section="movies",
        header=_section_header("movies"),
        profiles=_builtin_profiles(conn),
        default_profile_key=_setting(conn, "default_profile_key", "1080p"),
        default_minimum_availability=_setting(
            conn, "default_minimum_availability", "released"
        ),
        templates={
            "movie_folder_template": _setting(
                conn, "movie_folder_template", settings_service.MEDIA_TEMPLATES[
                    "movie_folder_template"][1]
            ),
            "movie_file_template": _setting(
                conn, "movie_file_template", settings_service.MEDIA_TEMPLATES[
                    "movie_file_template"][1]
            ),
        },
        preview=settings_service.preview_movies(conn),
        movie_search_interval_hours=_setting(
            conn, "movie_search_interval_hours", "6"
        ),
        candidate_batch_size=_setting(conn, "candidate_batch_size", "25"),
        probe_batch_per_loop=_setting(conn, "probe_batch_per_loop", "3"),
        save_url="/api/settings/sections/movies",
        preview_url="/api/settings/sections/movies/preview",
        **_policies_context(conn),
    )


@bp.post("/api/settings/sections/movies")
def save_movie_settings():
    conn = _conn()
    try:
        return jsonify(settings_service.save_movies(conn, _payload()))
    except settings_service.SettingsError as exc:
        return _fail(str(exc), 400, exc.section)
    except ValueError as exc:
        return _fail(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Movie settings save failed")
        return _fail(f"Could not save movie settings: {exc}", 500)


@bp.post("/api/settings/sections/movies/preview")
def preview_movie_settings():
    conn = _conn()
    try:
        payload = _payload()
        templates = settings_service.validate_naming(
            "movies", payload.get("templates") or {}
        )
        return jsonify({
            "ok": True,
            "preview": settings_service.preview_movies(conn, templates),
        })
    except settings_service.SettingsError as exc:
        return _fail(str(exc), 400, exc.section)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Movie naming preview failed")
        return _fail(f"Could not render the preview: {exc}", 500)


# ---------------------------------------------------------------------- series
@bp.get("/series/settings")
def series_settings():
    conn = _conn()
    return render_template(
        "series_settings.html",
        section="series",
        header=_section_header("series"),
        profiles=_builtin_profiles(conn),
        default_profile_key=_setting(conn, "default_profile_key", "1080p"),
        default_monitor_mode=_setting(conn, "default_monitor_mode", "all"),
        default_monitor_new_seasons=_setting(
            conn, "default_monitor_new_seasons", "all"
        ),
        templates={
            "series_folder_template": _setting(
                conn, "series_folder_template", settings_service.MEDIA_TEMPLATES[
                    "series_folder_template"][1]
            ),
            "season_folder_template": _setting(
                conn, "season_folder_template", settings_service.MEDIA_TEMPLATES[
                    "season_folder_template"][1]
            ),
            "episode_file_template": _setting(
                conn, "episode_file_template", settings_service.MEDIA_TEMPLATES[
                    "episode_file_template"][1]
            ),
        },
        preview=settings_service.preview_series(conn),
        series_search_interval_hours=_setting(
            conn, "series_search_interval_hours", "4"
        ),
        candidate_batch_size=_setting(conn, "candidate_batch_size", "25"),
        probe_batch_per_loop=_setting(conn, "probe_batch_per_loop", "3"),
        save_url="/api/settings/sections/series",
        preview_url="/api/settings/sections/series/preview",
        **_policies_context(conn),
    )


@bp.post("/api/settings/sections/series")
def save_series_settings():
    conn = _conn()
    try:
        return jsonify(settings_service.save_series(conn, _payload()))
    except settings_service.SettingsError as exc:
        return _fail(str(exc), 400, exc.section)
    except ValueError as exc:
        return _fail(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Series settings save failed")
        return _fail(f"Could not save series settings: {exc}", 500)


@bp.post("/api/settings/sections/series/preview")
def preview_series_settings():
    conn = _conn()
    try:
        payload = _payload()
        templates = settings_service.validate_naming(
            "series", payload.get("templates") or {}
        )
        return jsonify({
            "ok": True,
            "preview": settings_service.preview_series(conn, templates),
        })
    except settings_service.SettingsError as exc:
        return _fail(str(exc), 400, exc.section)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Series naming preview failed")
        return _fail(f"Could not render the preview: {exc}", 500)


# --------------------------------------------------------------- task policies
_POLICY_FIELDS = {
    "enabled", "max_failures", "infinite_retry", "retry_delay_seconds",
    "backoff_multiplier", "max_delay_seconds",
}


def _validate_policy_items(conn, payload: Mapping[str, Any]) -> list[dict]:
    """Validate every policy change before touching any row.

    ``bworker_store.update_policy`` writes immediately, so a partially invalid
    batch would otherwise leave earlier rows saved. This dry pass applies the
    same rules to all entries first; only then are the writes performed.
    """
    raw_items = payload.get("policies")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Provide a non-empty list of policies.")

    known = {row["work_type"]: row for row in store.policies(conn)}
    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping) or not raw.get("work_type"):
            raise ValueError("Every policy entry needs a work type.")
        work_type = str(raw["work_type"])
        row = known.get(work_type)
        if row is None:
            raise ValueError(f"Unknown work type: {work_type}")
        changes = raw.get("changes")
        if not isinstance(changes, Mapping):
            changes = {key: value for key, value in raw.items()
                       if key != "work_type"}
        changes = dict(changes)
        unknown = set(changes) - _POLICY_FIELDS
        if unknown:
            raise ValueError(
                "Unsupported policy field(s): " + ", ".join(sorted(unknown))
            )
        if not changes:
            continue

        def integer(name: str, minimum: int, maximum: int | None = None,
                    *, nullable: bool = False) -> None:
            if name not in changes:
                return
            value = changes[name]
            if value is None:
                if nullable:
                    return
                raise ValueError(f"{name} cannot be null.")
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be an integer.") from None
            if number < minimum:
                raise ValueError(f"{name} must be at least {minimum}.")
            if maximum is not None and number > maximum:
                raise ValueError(f"{name} must be at most {maximum}.")

        if "enabled" in changes:
            try:
                enabled = settings_service._bool(  # same strict parser as settings
                    changes["enabled"], "enabled", "policies"
                )
            except settings_service.SettingsError as exc:
                raise ValueError(str(exc)) from exc
            if row.get("always_enabled") and not enabled:
                raise ValueError("activity_cleanup cannot be disabled.")
            changes["enabled"] = enabled
        if "infinite_retry" in changes:
            try:
                changes["infinite_retry"] = settings_service._bool(
                    changes["infinite_retry"], "infinite retry", "policies"
                )
            except settings_service.SettingsError as exc:
                raise ValueError(str(exc)) from exc
        integer("max_failures", 1, nullable=True)
        integer("retry_delay_seconds", 1)
        integer("max_delay_seconds", 1)
        if "backoff_multiplier" in changes:
            try:
                backoff = float(changes["backoff_multiplier"])
            except (TypeError, ValueError):
                raise ValueError("backoff_multiplier must be a number.") from None
            if not 1.0 <= backoff <= 10.0:
                raise ValueError("backoff_multiplier must be between 1.0 and 10.0.")
            changes["backoff_multiplier"] = backoff
        delay = int(changes.get("retry_delay_seconds",
                                row.get("retry_delay_seconds") or 60))
        max_delay = changes.get("max_delay_seconds",
                                row.get("max_delay_seconds"))
        if max_delay is not None and int(max_delay) < delay:
            raise ValueError(
                "max_delay_seconds cannot be less than retry_delay_seconds."
            )
        items.append({"work_type": work_type, "changes": changes})
    if not items:
        raise ValueError("No policy changes were provided.")
    return items


@bp.patch("/api/settings/sections/policies")
def save_task_policies():
    conn = _conn()
    try:
        items = _validate_policy_items(conn, _payload())
        for item in items:
            store.update_policy(conn, item["work_type"], item["changes"])
        return jsonify({
            "ok": True,
            "policies": _policies(conn),
            "updated": len(items),
        })
    except ValueError as exc:
        return _fail(str(exc), 400, "policies")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Task policy save failed")
        return _fail(f"Could not save task policies: {exc}", 500, "policies")


@bp.post("/api/settings/sections/policies/reset")
def reset_failed_work():
    """Reset only ``failed_stopped`` units. Pending/completed work is untouched."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id FROM bworker_work WHERE status = 'failed_stopped' ORDER BY id"
        ).fetchall()
        reset = 0
        deferred = 0
        for row in rows:
            result = store.reset_work(conn, work_id=int(row["id"]))
            reset += int(result.get("updated") or 0)
            deferred += int(result.get("reset_requested") or 0)
        return jsonify({
            "ok": True,
            "reset": reset,
            "reset_requested": deferred,
            "message": (f"Reset {reset} failed unit"
                        f"{'' if reset == 1 else 's'}."),
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("Task reset failed")
        return _fail(f"Could not reset failed work: {exc}", 500, "policies")


__all__ = ["bp"]
