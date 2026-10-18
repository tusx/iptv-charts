"""Naming templates, safe path components, and path confinement.

Metadata is attacker-controlled from the app's point of view: a provider name
or a TMDB title can contain slashes, control characters, or a reserved device
name, and a backup can supply a template. Everything that becomes a path goes
through here, and every final path is checked to stay inside the media root.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

#: Defaults match the seeded settings values; the constants are the fallback
#: when a setting is missing or malformed.
DEFAULT_MOVIE_FOLDER = "{title} ({year})"
DEFAULT_MOVIE_FILE = "{title} ({year}) [{resolution}]"
DEFAULT_SERIES_FOLDER = "{title} ({year})"
DEFAULT_SEASON_FOLDER = "Season {season:00}"
DEFAULT_EPISODE_FILE = (
    "{series} - S{season:00}E{episode:00} - {episode_title} [{resolution}]"
)

MOVIE_FOLDER_TOKENS = frozenset({"title", "year"})
MOVIE_FILE_TOKENS = frozenset(
    {"title", "year", "resolution", "source", "quality"}
)
SERIES_FOLDER_TOKENS = frozenset({"title", "year"})
SEASON_FOLDER_TOKENS = frozenset({"season"})
EPISODE_FILE_TOKENS = frozenset(
    {"series", "season", "episode", "episode_title", "resolution", "source", "quality"}
)

_TOKEN_RE = re.compile(r"\{([a-z_]+)(?::(0+))?\}")
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_SPACE_RE = re.compile(r"\s+")

#: Windows refuses these as file names, and a library intended for a media
#: player may well be read from a Windows machine or a Samba share.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

#: 255 bytes is the usual filesystem limit; leave room for a suffix.
MAX_COMPONENT_BYTES = 200


class TemplateError(ValueError):
    """A user-supplied naming template is not usable."""


def validate_template(template: str, allowed: frozenset[str]) -> str:
    """Check one template and return it stripped, or raise ``TemplateError``.

    Templates describe one path component, so a separator is rejected rather
    than silently flattened: a user who writes ``{title}/{year}`` has asked
    for something the app does not do, and guessing would hide that.
    """
    text = str(template or "").strip()
    if not text:
        raise TemplateError("A naming template cannot be empty.")
    if len(text) > 200:
        raise TemplateError("A naming template is limited to 200 characters.")
    if "/" in text or "\\" in text:
        raise TemplateError("A naming template describes one folder or file name; use separate templates for subfolders.")
    if ".." in text:
        raise TemplateError("A naming template cannot contain '..'.")
    if any(ord(char) < 32 for char in text):
        raise TemplateError("A naming template cannot contain control characters.")

    for match in _TOKEN_RE.finditer(text):
        if match.group(1) not in allowed:
            known = ", ".join("{" + token + "}" for token in sorted(allowed))
            raise TemplateError(
                f"Unknown placeholder {{{match.group(1)}}}. "
                f"This template supports: {known}."
            )

    leftover = _TOKEN_RE.sub("", text)
    if "{" in leftover or "}" in leftover:
        raise TemplateError("Unbalanced or malformed placeholder in the template.")
    return text


def validate_template_set(templates: dict[str, str], allowed: dict[str, frozenset[str]]) -> dict[str, str]:
    """Validate a named group of templates in one call."""
    return {
        name: validate_template(templates.get(name, ""), allowed[name])
        for name in allowed
    }


def render(template: str, values: dict[str, object], allowed: frozenset[str] | None = None) -> str:
    """Substitute placeholders in one pass.

    One pass matters: a movie titled ``{url}`` must not be able to make a
    second round of substitution happen. Values are inserted as text, never
    rescanned.
    """
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if allowed is not None and token not in allowed:
            raise TemplateError(f"Unknown placeholder {{{token}}}.")
        value = values.get(token)
        if value is None or value == "":
            return ""
        if match.group(2):
            try:
                return str(int(value)).zfill(len(match.group(2)))
            except (TypeError, ValueError):
                return ""
        return str(value)

    return _TOKEN_RE.sub(replace, template)


def _trim_bytes(value: str, limit: int = MAX_COMPONENT_BYTES) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", "ignore").rstrip(" .")


def _remove_empty_groups(value: str) -> str:
    """Drop ``()``/``[]`` left behind when an optional value is missing."""
    previous = None
    current = value
    while previous != current:
        previous = current
        current = re.sub(r"\(\s*\)", "", current)
        current = re.sub(r"\[\s*\]", "", current)
        current = _SPACE_RE.sub(" ", current)
    return current.strip(" -–—")


def sanitize_component(text: object, fallback: str = "Unknown") -> str:
    """Turn arbitrary metadata into one safe folder or file name."""
    value = unicodedata.normalize("NFC", str(text or ""))
    value = _ILLEGAL_RE.sub("", value)
    value = _SPACE_RE.sub(" ", value).strip(" .")
    if value.lower() in _RESERVED_NAMES:
        value = f"_{value}"
    value = _trim_bytes(value)
    value = value.rstrip(" .")
    if not value or value in {".", ".."}:
        return fallback
    return value


def render_component(
    template: str,
    values: dict[str, object],
    allowed: frozenset[str],
    *,
    fallback: str = "Unknown",
) -> str:
    """Render a template and make the result a safe path component."""
    return sanitize_component(_remove_empty_groups(render(template, values, allowed)), fallback)


def safe_extension(value: object, fallback: str = "mp4") -> str:
    """Return a lowercase alphanumeric extension without the leading dot."""
    text = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    return text[:8] or fallback


def unique_path(path: Path) -> Path:
    """Return ``path`` or a numbered variant so an unmanaged file is not clobbered."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for number in range(2, 1000):
        candidate = path.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
    raise TemplateError(f"Could not find an unused name near {path.name!r}.")


def confine(path: Path | str, root: Path | str) -> Path:
    """Resolve *path* and require it to live under *root*.

    ``resolve()`` follows symlinks, so a link inside the media tree that
    points elsewhere is rejected rather than followed during a delete.
    """
    root_path = Path(root).resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise TemplateError(
            f"Refusing to use a path outside the media library: {path!s}"
        ) from exc
    return target


__all__ = [
    "DEFAULT_EPISODE_FILE",
    "DEFAULT_MOVIE_FILE",
    "DEFAULT_MOVIE_FOLDER",
    "DEFAULT_SEASON_FOLDER",
    "DEFAULT_SERIES_FOLDER",
    "EPISODE_FILE_TOKENS",
    "MOVIE_FILE_TOKENS",
    "MOVIE_FOLDER_TOKENS",
    "SEASON_FOLDER_TOKENS",
    "SERIES_FOLDER_TOKENS",
    "TemplateError",
    "confine",
    "render",
    "render_component",
    "safe_extension",
    "sanitize_component",
    "unique_path",
    "validate_template",
    "validate_template_set",
]
