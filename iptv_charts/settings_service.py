"""Validate and save every editable setting in one transaction.

The settings page has one form and one Save button. Validation happens for all
sections first; only then are values written, in one SQLite transaction. A bad
template or interval therefore cannot leave the half of the form that happened
to be processed first silently saved.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from . import db, downloader, metadata, naming, player
from .clients import tmdb as tmdb_client

LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")

MEDIA_TEMPLATES = {
    "movie_folder_template": (naming.MOVIE_FOLDER_TOKENS, naming.DEFAULT_MOVIE_FOLDER),
    "movie_file_template": (naming.MOVIE_FILE_TOKENS, naming.DEFAULT_MOVIE_FILE),
    "series_folder_template": (naming.SERIES_FOLDER_TOKENS, naming.DEFAULT_SERIES_FOLDER),
    "season_folder_template": (naming.SEASON_FOLDER_TOKENS, naming.DEFAULT_SEASON_FOLDER),
    "episode_file_template": (naming.EPISODE_FILE_TOKENS, naming.DEFAULT_EPISODE_FILE),
}

#: The section pages own these subsets. Keeping them next to MEDIA_TEMPLATES
#: means template validation cannot drift between the global and section saves.
MOVIE_TEMPLATE_KEYS = ("movie_folder_template", "movie_file_template")
SERIES_TEMPLATE_KEYS = (
    "series_folder_template",
    "season_folder_template",
    "episode_file_template",
)

#: Section defaults and search-tuning bounds. Search intervals are floats in
#: hours (the UI allows half hours); batch/probe counts are integers because
#: they count candidates in one bworker iteration, which the contract caps at 3.
MOVIE_DEFAULT_AVAILABILITY = ("announced", "in_cinemas", "released")
SERIES_MONITOR_MODES = ("all", "future", "first", "latest", "none")
SERIES_NEW_SEASONS_MODES = ("all", "future", "none")
SEARCH_LIMITS = {
    "movie_search_interval_hours": ("hours", 1.0, 8760.0),
    "series_search_interval_hours": ("hours", 1.0, 8760.0),
}
CANDIDATE_BATCH_SIZE = ("candidate batch size", 1, 200)
PROBE_BATCH_PER_LOOP = ("probes per iteration", 1, 3)
STALE_AFTER_HOURS = ("stale after hours", 1, 8760)

DOWNLOADS_NUMERIC = {
    "concurrency": ("download_concurrency", 1, 3),
    "max_attempts": ("download_max_attempts", 1, 10),
    "probe_cache_hours": ("probe_cache_hours", 0, 720),
    "probe_timeout_seconds": ("probe_timeout_seconds", 5, 120),
    "probe_max_candidates": ("probe_max_candidates", 1, 20),
    "probe_search_budget": ("probe_search_budget", 1, 200),
}

SCHEDULER_LIMITS = {
    "search_interval_hours": (1, 8760),
    "refresh_interval_hours": (1, 8760),
    "catalog_refresh_hours": (0, 8760),
}


class SettingsError(ValueError):
    """One section's input is bad; the section name helps the UI focus it."""

    def __init__(self, message: str, section: str) -> None:
        super().__init__(message)
        self.section = section


def _section(payload: Mapping[str, Any], name: str) -> dict:
    value = payload.get(name)
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _secret(value: Any, field: str, section: str) -> str:
    text = str(value or "").strip()
    if len(text) > 300 or any(ord(char) < 32 for char in text):
        raise SettingsError(f"{field.replace('_', ' ').title()} is not valid.", section)
    return text


def _collect(conn, payload: Mapping[str, Any]) -> tuple[dict[str, str], dict]:
    """Return (setting values, response details) without writing anything."""
    values: dict[str, str] = {}
    details: dict[str, Any] = {"warnings": [], "clear_tvdb_token": False}

    # --------------------------------------------------------------- player
    section = _section(payload, "player")
    command_text = None
    if "player_command" in section:
        command_text = str(section.get("player_command") or "").strip()
        if command_text:
            try:
                player.parse_command(command_text)
            except ValueError as exc:
                raise SettingsError(str(exc), "player") from exc
        values[player.COMMAND_KEY] = command_text

    preferred = None
    if "preferred_player" in section:
        preferred = str(section.get("preferred_player") or "").strip()
        known = player.KNOWN_PLAYER_IDS | {
            found.id for found in player.available(conn, refresh=True)
        }
        if preferred not in known:
            raise SettingsError("That player is not available.", "player")
        if preferred == "custom":
            if command_text is not None:
                ready = bool(command_text)
            else:
                ready = player.custom_player(conn) is not None
            if not ready:
                raise SettingsError(
                    "Give a command first — there is nothing to run yet.", "player"
                )
        values[player.PREFERENCE_KEY] = preferred

    # ------------------------------------------------------------- metadata
    section = _section(payload, "metadata")
    tmdb_clear = bool(section.get("tmdb_clear"))
    tvdb_clear = bool(section.get("tvdb_clear"))
    if tmdb_clear:
        values[metadata.TMDB_KEY] = ""
        values[metadata.TMDB_LEGACY_KEY] = ""
    elif "tmdb_read_token" in section or "tmdb_api_key" in section:
        raw_token = section.get("tmdb_read_token", section.get("tmdb_api_key"))
        try:
            token = tmdb_client.validate_read_token(raw_token)
        except tmdb_client.TMDBKeyError as exc:
            raise SettingsError(str(exc), "metadata") from exc
        values[metadata.TMDB_KEY] = token
        # Do not let an old saved key resurface through the compatibility
        # fallback after a new token was saved.
        values[metadata.TMDB_LEGACY_KEY] = ""
    if tvdb_clear:
        values[metadata.TVDB_KEY] = ""
        values[metadata.TVDB_PIN] = ""
    else:
        if "tvdb_api_key" in section:
            values[metadata.TVDB_KEY] = _secret(
                section.get("tvdb_api_key"), "TVDB API key", "metadata"
            )
        if "tvdb_pin" in section:
            values[metadata.TVDB_PIN] = _secret(
                section.get("tvdb_pin"), "TVDB PIN", "metadata"
            )
    language = None
    if "language" in section or "metadata_language" in section:
        language = str(
            section.get("language", section.get("metadata_language")) or ""
        ).strip()
        if not LANGUAGE_RE.match(language):
            raise SettingsError("Language must look like en-US or es-ES.", "metadata")
        values[metadata.LANGUAGE] = language
    details["clear_tvdb_token"] = bool(
        tvdb_clear or metadata.TVDB_KEY in values or metadata.TVDB_PIN in values
    )

    # ---------------------------------------------------------------- media
    section = _section(payload, "media")
    for key, (allowed, _default) in MEDIA_TEMPLATES.items():
        if key not in section:
            continue
        try:
            values[key] = naming.validate_template(str(section.get(key) or ""), allowed)
        except naming.TemplateError as exc:
            raise SettingsError(str(exc), "media") from exc

    # ------------------------------------------------------------ downloads
    section = _section(payload, "downloads")
    for key, (setting_key, minimum, maximum) in DOWNLOADS_NUMERIC.items():
        if key not in section:
            continue
        value = _int(section.get(key))
        if value is None or not minimum <= value <= maximum:
            raise SettingsError(
                f"{key.replace('_', ' ')} must be between {minimum} and {maximum}.",
                "downloads",
            )
        values[setting_key] = str(value)

    # ------------------------------------------------------------- scheduler
    section = _section(payload, "scheduler")
    for key, (minimum, maximum) in SCHEDULER_LIMITS.items():
        if key not in section:
            continue
        try:
            value = float(section.get(key))
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{key.replace('_', ' ')} must be a number of hours.",
                                "scheduler") from exc
        if not minimum <= value <= maximum:
            raise SettingsError(
                f"{key.replace('_', ' ')} must be between {minimum} and {maximum}.",
                "scheduler",
            )
        values[key] = str(value)

    details["values"] = values
    details["player_changed"] = bool(
        player.COMMAND_KEY in values or player.PREFERENCE_KEY in values
    )
    return values, details


def _preview(conn, values: Mapping[str, str]) -> dict:
    def template(key: str, default: str) -> str:
        return values.get(key) or db.get_setting(conn, key, default) or default

    movie_values = {
        "title": "Example Movie", "year": 2024, "resolution": "1080p",
        "source": "WEB-DL", "quality": "1080p WEB-DL",
    }
    episode_values = {
        "series": "Example Series", "year": 2024, "season": 1, "episode": 2,
        "episode_title": "Pilot", "resolution": "1080p",
        "source": "WEB-DL", "quality": "1080p WEB-DL",
    }
    return {
        "movie_folder": naming.render_component(
            template("movie_folder_template", naming.DEFAULT_MOVIE_FOLDER),
            movie_values, naming.MOVIE_FOLDER_TOKENS,
        ),
        "movie_file": naming.render_component(
            template("movie_file_template", naming.DEFAULT_MOVIE_FILE),
            movie_values, naming.MOVIE_FILE_TOKENS,
        ),
        "episode_file": naming.render_component(
            template("episode_file_template", naming.DEFAULT_EPISODE_FILE),
            episode_values, naming.EPISODE_FILE_TOKENS,
        ),
    }


# ---------------------------------------------------------- section helpers
def _mapping(payload: Mapping[str, Any], name: str, section: str) -> dict:
    """Return one nested section object, or an empty one when omitted."""
    value = payload.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SettingsError(f"{name.title()} settings must be an object.", section)
    return dict(value)


def _bool(value: Any, field: str, section: str) -> bool:
    """Strict boolean parsing. ``"maybe"`` is an error, not a truthy value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value if value is not None else "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(
        f"{field.replace('_', ' ').title()} must be true or false.", section
    )


def _bounded_int(value: Any, label: str, section: str, minimum: int,
                 maximum: int) -> int:
    number = _int(value)
    if number is None or not minimum <= number <= maximum:
        raise SettingsError(
            f"{label.replace('_', ' ').title()} must be between "
            f"{minimum} and {maximum}.",
            section,
        )
    return number


def _bounded_float(value: Any, label: str, section: str, minimum: float,
                   maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(
            f"{label.replace('_', ' ').title()} must be a number.", section
        ) from exc
    if not minimum <= number <= maximum:
        raise SettingsError(
            f"{label.replace('_', ' ').title()} must be between "
            f"{minimum:g} and {maximum:g}.",
            section,
        )
    return number


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _write_values(conn, values: Mapping[str, str]) -> None:
    """Upsert one section's validated values in a single transaction."""
    if not values:
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(values.items()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _collect_templates(values: dict[str, str], section_payload: Mapping[str, Any],
                       keys: tuple[str, ...], section: str) -> None:
    for key in keys:
        if key not in section_payload:
            continue
        allowed, _default = MEDIA_TEMPLATES[key]
        try:
            values[key] = naming.validate_template(
                str(section_payload.get(key) or ""), allowed
            )
        except naming.TemplateError as exc:
            raise SettingsError(str(exc), section) from exc


def _profile_key(conn, value: Any) -> str:
    key = str(value or "").strip()
    known = {
        str(row["builtin_key"])
        for row in conn.execute(
            "SELECT builtin_key FROM quality_profiles WHERE builtin_key IS NOT NULL"
        ).fetchall()
    }
    if not known:
        known = {spec["builtin_key"] for spec in db.BUILTIN_PROFILES}
    if key not in known:
        raise SettingsError("Choose an existing built-in quality profile.", "defaults")
    return key


def _template_setting(conn, overrides: Mapping[str, str], key: str,
                      default: str) -> str:
    return (overrides.get(key) or db.get_setting(conn, key, default) or default)


def validate_naming(section: str, templates: Mapping[str, Any]) -> dict[str, str]:
    """Validate one section's naming templates without saving them.

    The live preview endpoint uses this, so typing an invalid placeholder shows
    the same error the save would produce and cannot write anything by itself.
    """
    if section not in {"movies", "series"}:
        raise SettingsError("Unknown settings section.", "naming")
    if not isinstance(templates, Mapping):
        raise SettingsError("Expected a templates object.", "naming")
    keys = MOVIE_TEMPLATE_KEYS if section == "movies" else SERIES_TEMPLATE_KEYS
    values: dict[str, str] = {}
    _collect_templates(values, templates, keys, "naming")
    return values


def preview_movies(conn, overrides: Mapping[str, str] | None = None) -> dict:
    """Render the movie naming templates with a stable example."""
    values = dict(overrides or {})
    movie_values = {
        "title": "Example Movie", "year": 2024, "resolution": "1080p",
        "source": "WEB-DL", "quality": "1080p WEB-DL",
    }
    return {
        "movie_folder": naming.render_component(
            _template_setting(conn, values, "movie_folder_template",
                              naming.DEFAULT_MOVIE_FOLDER),
            movie_values, naming.MOVIE_FOLDER_TOKENS,
        ),
        "movie_file": naming.render_component(
            _template_setting(conn, values, "movie_file_template",
                              naming.DEFAULT_MOVIE_FILE),
            movie_values, naming.MOVIE_FILE_TOKENS,
        ),
    }


def preview_series(conn, overrides: Mapping[str, str] | None = None) -> dict:
    """Render the series/season/episode naming templates with a stable example."""
    values = dict(overrides or {})
    episode_values = {
        "series": "Example Series", "year": 2024, "season": 1, "episode": 2,
        "episode_title": "Pilot", "resolution": "1080p",
        "source": "WEB-DL", "quality": "1080p WEB-DL",
    }
    folder_values = {"title": "Example Series", "year": 2024}
    return {
        "series_folder": naming.render_component(
            _template_setting(conn, values, "series_folder_template",
                              naming.DEFAULT_SERIES_FOLDER),
            folder_values, naming.SERIES_FOLDER_TOKENS,
        ),
        "season_folder": naming.render_component(
            _template_setting(conn, values, "season_folder_template",
                              naming.DEFAULT_SEASON_FOLDER),
            episode_values, naming.SEASON_FOLDER_TOKENS,
        ),
        "episode_file": naming.render_component(
            _template_setting(conn, values, "episode_file_template",
                              naming.DEFAULT_EPISODE_FILE),
            episode_values, naming.EPISODE_FILE_TOKENS,
        ),
    }


def _collect_default_profile(conn, defaults: Mapping[str, Any],
                             values: dict[str, str]) -> None:
    if "default_profile_key" in defaults:
        values["default_profile_key"] = _profile_key(
            conn, defaults.get("default_profile_key")
        )


def _collect_search(search: Mapping[str, Any], values: dict[str, str],
                    interval_key: str, section: str) -> None:
    if interval_key in search:
        _label, minimum, maximum = SEARCH_LIMITS[interval_key]
        values[interval_key] = _number_text(_bounded_float(
            search.get(interval_key), interval_key, section, minimum, maximum
        ))
    for key, (label, minimum, maximum) in (
        ("candidate_batch_size", CANDIDATE_BATCH_SIZE),
        ("probe_batch_per_loop", PROBE_BATCH_PER_LOOP),
    ):
        if key in search:
            values[key] = str(_bounded_int(
                search.get(key), label, section, minimum, maximum
            ))


def save_iptv(conn, payload: Mapping[str, Any]) -> dict:
    """Save external-player and playlist-default settings.

    Validation happens for both groups before any write, so a bad command
    cannot leave a changed stale-after value behind. The external-player gate
    is stored separately and read by :func:`player.resolve` everywhere.
    """
    if not isinstance(payload, Mapping):
        raise SettingsError("Expected a JSON object.", "settings")
    player_section = _mapping(payload, "player", "player")
    playlist_section = _mapping(payload, "playlist", "playlist")
    values: dict[str, str] = {}

    current_command = db.get_setting(conn, player.COMMAND_KEY, "") or ""
    command = current_command
    if "player_command" in player_section:
        command = str(player_section.get("player_command") or "").strip()
        if len(command) > player.COMMAND_MAX_LENGTH:
            raise SettingsError(
                f"The custom command is limited to {player.COMMAND_MAX_LENGTH} characters.",
                "player",
            )
        if command:
            try:
                player.parse_command(command)
            except ValueError as exc:
                raise SettingsError(str(exc), "player") from exc
        values[player.COMMAND_KEY] = command

    enabled = player.enabled(conn)
    if "enabled" in player_section:
        enabled = _bool(player_section.get("enabled"), "enabled", "player")
        values[player.EXTERNAL_PLAYER_ENABLED_KEY] = "1" if enabled else "0"

    preferred = db.get_setting(conn, player.PREFERENCE_KEY, "auto") or "auto"
    if "preferred_player" in player_section:
        preferred = str(player_section.get("preferred_player") or "").strip()
        known = player.KNOWN_PLAYER_IDS | {
            found.id for found in player.available(conn, refresh=True)
        }
        if preferred not in known:
            raise SettingsError("That player is not available.", "player")
        values[player.PREFERENCE_KEY] = preferred
    # A custom choice with no command is harmless while playback is off, but
    # it would leave an enabled player resolving to nothing. Reject only when
    # the save is switching external playback on.
    if enabled and preferred == "custom" and not command.strip():
        raise SettingsError(
            "Give a command first — there is nothing to run yet.", "player"
        )

    if "stale_after_hours" in playlist_section:
        label, minimum, maximum = STALE_AFTER_HOURS
        values["stale_after_hours"] = str(_bounded_int(
            playlist_section.get("stale_after_hours"), label, "playlist",
            minimum, maximum,
        ))

    _write_values(conn, values)
    chosen = player.resolve(conn, refresh=True) if enabled else None
    example = None
    if chosen is not None and chosen.flavour != "bridge":
        try:
            example = chosen.command_for(
                "http://example.com/stream.ts", title="Example stream",
            )
        except player.PlayerError as exc:
            example = None
            warnings = [f"Player preview unavailable: {exc}"]
        else:
            warnings = []
    else:
        warnings = []
    return {
        "ok": True,
        "saved": sorted(values),
        "external_player_enabled": bool(enabled),
        "player": {"id": chosen.id, "label": chosen.label} if chosen else None,
        "command": example,
        "warnings": warnings,
    }


def save_movies(conn, payload: Mapping[str, Any]) -> dict:
    """Save movie naming, defaults and search-tuning settings."""
    if not isinstance(payload, Mapping):
        raise SettingsError("Expected a JSON object.", "settings")
    values: dict[str, str] = {}
    _collect_templates(
        values, _mapping(payload, "naming", "naming"),
        MOVIE_TEMPLATE_KEYS, "naming",
    )

    defaults = _mapping(payload, "defaults", "defaults")
    _collect_default_profile(conn, defaults, values)
    if "default_minimum_availability" in defaults:
        availability = str(
            defaults.get("default_minimum_availability") or ""
        ).strip().lower()
        if availability not in MOVIE_DEFAULT_AVAILABILITY:
            raise SettingsError(
                "Minimum availability must be announced, in_cinemas or released.",
                "defaults",
            )
        values["default_minimum_availability"] = availability

    _collect_search(
        _mapping(payload, "search", "search"), values,
        "movie_search_interval_hours", "search",
    )
    _write_values(conn, values)
    return {
        "ok": True,
        "saved": sorted(values),
        "preview": preview_movies(conn),
    }


def save_series(conn, payload: Mapping[str, Any]) -> dict:
    """Save series naming, monitor defaults and search-tuning settings."""
    if not isinstance(payload, Mapping):
        raise SettingsError("Expected a JSON object.", "settings")
    values: dict[str, str] = {}
    _collect_templates(
        values, _mapping(payload, "naming", "naming"),
        SERIES_TEMPLATE_KEYS, "naming",
    )

    defaults = _mapping(payload, "defaults", "defaults")
    _collect_default_profile(conn, defaults, values)
    if "default_monitor_mode" in defaults:
        mode = str(defaults.get("default_monitor_mode") or "").strip().lower()
        if mode not in SERIES_MONITOR_MODES:
            raise SettingsError("Unknown monitor option.", "defaults")
        values["default_monitor_mode"] = mode
    if "default_monitor_new_seasons" in defaults:
        mode = str(
            defaults.get("default_monitor_new_seasons") or ""
        ).strip().lower()
        if mode not in SERIES_NEW_SEASONS_MODES:
            raise SettingsError(
                "New-season monitoring must be all, future or none.", "defaults"
            )
        values["default_monitor_new_seasons"] = mode

    _collect_search(
        _mapping(payload, "search", "search"), values,
        "series_search_interval_hours", "search",
    )
    _write_values(conn, values)
    return {
        "ok": True,
        "saved": sorted(values),
        "preview": preview_series(conn),
    }


def save_all(conn, payload: Mapping[str, Any]) -> dict:
    """Validate everything, write atomically, then run side effects."""
    if not isinstance(payload, Mapping):
        raise SettingsError("Expected a JSON object.", "settings")
    values, details = _collect(conn, payload)

    started = False
    try:
        if values:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                list(values.items()),
            )
            if details["clear_tvdb_token"]:
                conn.execute(
                    "DELETE FROM metadata_cache WHERE key = ?", (metadata.TVDB_TOKEN_KEY,)
                )
            conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Side effects after commit: a failure here must not make the caller think
    # the settings were not stored.
    warnings: list[str] = []
    if "download_concurrency" in values:
        try:
            downloader.ensure_started()
            started = True
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Settings saved, but the download queue could not be refreshed: {exc}")

    chosen = player.resolve(conn, refresh=True)
    example = None
    if chosen is not None and chosen.flavour != "bridge":
        try:
            example = chosen.command_for(
                "http://example.com/stream.ts", title="Example stream",
            )
        except player.PlayerError as exc:
            warnings.append(f"Player preview unavailable: {exc}")

    return {
        "ok": True,
        "player": {"id": chosen.id, "label": chosen.label} if chosen else None,
        "command": example,
        "metadata": {
            "tmdb": metadata.has_tmdb(conn),
            "tvdb": metadata.has_tvdb(conn),
            "language": metadata.language(conn),
        },
        "media": {"preview": _preview(conn, {})},
        "saved": sorted(values),
        "workers_started": started,
        "warnings": warnings,
    }


__all__ = [
    "SettingsError",
    "preview_movies",
    "preview_series",
    "save_all",
    "save_iptv",
    "save_movies",
    "save_series",
    "validate_naming",
]
