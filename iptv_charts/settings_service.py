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
            warnings.append(f"Settings saved, but download workers did not start: {exc}")

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


__all__ = ["SettingsError", "save_all"]
