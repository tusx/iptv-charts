"""Advisory update check against GitHub Releases.

The check is deliberately one-way: it fetches the latest release metadata,
compares it with :data:`iptv_charts.__version__` and returns a plain dict. It
never downloads, extracts or installs anything, and the web process never
acts on the answer by itself. Updating is a manual command the user runs
(``download.and.deploy.sh``); the UI only shows that a release is available.

Failure behaviour is part of the contract:

* no credentials are sent and the request has a short timeout;
* one successful answer is cached in ``metadata_cache`` so a page render does
  not hit GitHub repeatedly (and an offline machine still sees the last
  known release);
* every failure is converted into an ``error`` string in the result -- this
  module never raises into a template render;
* the check can be disabled with the ``update_check_enabled=0`` setting or
  ``IPTV_CHARTS_UPDATE_CHECK=0``.

The public result always has the same keys::

    {
        "enabled": bool,
        "current": "0.8.0",
        "latest": "0.8.1" | None,
        "tag": "v0.8.1" | None,
        "update_available": bool,
        "url": "https://github.com/..." | None,
        "checked_at": 1700000000 | None,
        "cached": bool,
        "error": "..." | None,
        "update_command": "curl -fsSL ..." | None,
    }
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable

import requests

from . import __version__, metadata

logger = logging.getLogger("iptv_charts.updates")

#: The canonical project. Kept as a constant rather than a setting: an update
#: banner that can be pointed at an arbitrary repository is a phishing surface.
DEFAULT_REPO = "tusx/iptv-charts"

#: GitHub's public API. Tests and air-gapped mirrors can override it with
#: ``IPTV_CHARTS_UPDATE_API``; the app itself never changes it.
DEFAULT_API_BASE = "https://api.github.com"

CACHE_KEY = "update:latest-release"
CACHE_TTL_SECONDS = 6 * 60 * 60
TIMEOUT_SECONDS = 4.0
ENABLED_SETTING = "update_check_enabled"
DISABLE_ENV = "IPTV_CHARTS_UPDATE_CHECK"
TIMEOUT_ENV = "IPTV_CHARTS_UPDATE_TIMEOUT"
API_ENV = "IPTV_CHARTS_UPDATE_API"

RATE_LIMIT_MESSAGE = (
    "GitHub's release API rate limit was reached. The update check will try "
    "again later; the app is unaffected."
)
OFFLINE_MESSAGE = (
    "Could not reach GitHub to check for updates. The app is unaffected."
)
NOT_FOUND_MESSAGE = "No published release was found on GitHub."
INVALID_MESSAGE = "GitHub returned an update response this app could not read."
INVALID_VERSION_MESSAGE = "GitHub returned an unexpected release version."

_VERSION_RE = re.compile(
    r"^[vV]?(?P<core>\d+(?:\.\d+)*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


class UpdateCheckError(RuntimeError):
    """A checked, user-safe reason the update lookup could not complete."""


# --------------------------------------------------------------------- versions
def _identifier(part: str) -> tuple[int, int, str]:
    """Sort key for one prerelease identifier.

    Numeric identifiers sort before non-numeric ones, as semver requires.
    Tuples keep the comparison total; the third element is only a tie-breaker
    for the (never both non-zero) numeric/text slots.
    """
    if part.isdigit():
        return (0, int(part), "")
    return (1, 0, part)


def parse_version(value: str) -> tuple[tuple[int, ...], tuple[tuple[int, int, str], ...]]:
    """Parse a semver-ish version into a comparable key.

    Accepts an optional ``v`` prefix, one to four numeric components
    (``0.8`` and ``0.8.0`` compare equal) and prerelease/build suffixes.
    Raises :class:`ValueError` for text that is not version-like.
    """
    if not isinstance(value, str):
        raise ValueError(f"not a version: {value!r}")
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"not a version: {value!r}")
    core = tuple(int(part) for part in match.group("core").split("."))
    prerelease = match.group("pre")
    identifiers: tuple[tuple[int, int, str], ...] = ()
    if prerelease:
        identifiers = tuple(_identifier(part) for part in prerelease.split("."))
    return core, identifiers


def compare_versions(left: str, right: str) -> int:
    """Return -1, 0 or 1 for ``left`` older/equal/newer than ``right``."""
    left_core, left_pre = parse_version(left)
    right_core, right_pre = parse_version(right)

    width = max(len(left_core), len(right_core))
    left_padded = left_core + (0,) * (width - len(left_core))
    right_padded = right_core + (0,) * (width - len(right_core))
    if left_padded != right_padded:
        return -1 if left_padded < right_padded else 1

    # A final release is newer than any prerelease of the same core version.
    if left_pre != right_pre:
        if not left_pre:
            return 1
        if not right_pre:
            return -1
        for left_id, right_id in zip(left_pre, right_pre):
            if left_id != right_id:
                return -1 if left_id < right_id else 1
        return -1 if len(left_pre) < len(right_pre) else 1
    return 0


def version_is_newer(candidate: str, current: str) -> bool:
    """Best-effort ``candidate > current`` that never raises.

    A malformed version is not an update; the UI must stay silent rather than
    show a banner based on unparseable input.
    """
    try:
        return compare_versions(candidate, current) > 0
    except (TypeError, ValueError):
        return False


def sort_key(value: str) -> tuple[tuple[int, ...], tuple[tuple[int, int, str], ...]]:
    """Expose the comparable key for callers that want to sort releases."""
    return parse_version(value)


# ---------------------------------------------------------------------- settings
def _truthy(value: Any) -> bool:
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def update_check_enabled(conn) -> bool:
    """Whether checking is allowed at all.

    The environment switch wins so an operator can disable outbound calls
    without touching the database; the settings key is the UI-facing control.
    """
    if not _truthy(os.environ.get(DISABLE_ENV, "1")):
        return False
    try:
        from . import db

        value = db.get_setting(conn, ENABLED_SETTING, "1")
    except Exception:  # a missing/locked DB must not break page rendering
        logger.debug("could not read %s", ENABLED_SETTING, exc_info=True)
        return True
    return _truthy(value if value is not None else "1")


def _api_base() -> str:
    return (os.environ.get(API_ENV) or DEFAULT_API_BASE).rstrip("/")


def _timeout() -> float:
    raw = os.environ.get(TIMEOUT_ENV)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return TIMEOUT_SECONDS


# ------------------------------------------------------------------------- cache
def _read_cache(conn, repo: str) -> dict | None:
    try:
        payload = metadata.cache_get(conn, CACHE_KEY)
    except Exception:
        logger.debug("could not read the update cache", exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("repo") not in (None, repo):
        return None
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        return None
    checked_at = payload.get("checked_at")
    if not isinstance(checked_at, int):
        return None
    return payload


def _write_cache(conn, payload: dict) -> None:
    try:
        metadata.cache_set(conn, CACHE_KEY, payload)
    except Exception:
        # A cache write is an optimisation; never fail the check over it.
        logger.debug("could not write the update cache", exc_info=True)


def cached_release(conn, repo: str = DEFAULT_REPO) -> dict | None:
    """Return the last cached release metadata, without any age limit."""
    return _read_cache(conn, repo)


# -------------------------------------------------------------------------- HTTP
def _default_http_get(url: str, *, headers: dict, timeout: float):
    return requests.get(url, headers=headers, timeout=timeout)


def latest_release_url(repo: str = DEFAULT_REPO) -> str:
    return f"{_api_base()}/repos/{repo}/releases/latest"


def fetch_latest_release(
    repo: str = DEFAULT_REPO,
    *,
    http_get: Callable[..., Any] | None = None,
    timeout: float | None = None,
) -> dict:
    """Fetch and normalise ``GET /repos/<repo>/releases/latest``.

    The returned dict is the subset the app uses, not the raw GitHub payload.
    Only ``http_get`` (tests) or ``requests`` (production) is used; there is no
    auth header and no redirect to a configurable host.
    """
    getter = http_get or _default_http_get
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"IPTV-Charts/{__version__}",
    }
    try:
        response = getter(
            latest_release_url(repo), headers=headers, timeout=timeout or _timeout()
        )
    except requests.Timeout as exc:
        raise UpdateCheckError(OFFLINE_MESSAGE) from exc
    except requests.RequestException as exc:
        raise UpdateCheckError(OFFLINE_MESSAGE) from exc
    except Exception as exc:  # injected test clients and odd HTTP stacks
        raise UpdateCheckError(OFFLINE_MESSAGE) from exc

    status = int(getattr(response, "status_code", 200))
    if status in (403, 429):
        raise UpdateCheckError(RATE_LIMIT_MESSAGE)
    if status == 404:
        raise UpdateCheckError(NOT_FOUND_MESSAGE)
    if status >= 400:
        raise UpdateCheckError(INVALID_MESSAGE)

    try:
        data = response.json()
    except Exception as exc:
        raise UpdateCheckError(INVALID_MESSAGE) from exc
    if not isinstance(data, dict):
        raise UpdateCheckError(INVALID_MESSAGE)

    tag = str(data.get("tag_name") or "").strip()
    try:
        parse_version(tag)
    except ValueError as exc:
        raise UpdateCheckError(INVALID_VERSION_MESSAGE) from exc
    version = tag[1:] if tag[:1] in {"v", "V"} else tag

    html_url = str(data.get("html_url") or "").strip()
    if not html_url:
        html_url = f"https://github.com/{repo}/releases/tag/{tag}"
    elif not html_url.startswith(("https://", "http://")):
        raise UpdateCheckError(INVALID_MESSAGE)

    return {
        "repo": repo,
        "tag": tag,
        "version": version,
        "url": html_url,
        "published_at": data.get("published_at"),
    }


# ----------------------------------------------------------------------- results
def _update_command(repo: str, tag: str) -> str:
    """The manual command the banner may show. Nothing here is auto-run."""
    script_url = f"https://raw.githubusercontent.com/{repo}/{tag}/download.and.deploy.sh"
    return (
        f"curl -fsSL {script_url} -o download.and.deploy.sh && "
        "bash download.and.deploy.sh"
    )


def _result(
    *,
    enabled: bool,
    current: str,
    release: dict | None = None,
    cached: bool = False,
    error: str | None = None,
) -> dict:
    latest = release.get("version") if release else None
    tag = release.get("tag") if release else None
    available = bool(latest and version_is_newer(latest, current))
    return {
        "enabled": enabled,
        "current": current,
        "latest": latest,
        "tag": tag,
        "update_available": available,
        "url": release.get("url") if release else None,
        "checked_at": release.get("checked_at") if release else None,
        "cached": cached,
        "error": error,
        "update_command": (
            _update_command(release.get("repo") or DEFAULT_REPO, tag)
            if available and tag
            else None
        ),
    }


def check_for_update(
    conn,
    *,
    force: bool = False,
    now: int | None = None,
    repo: str = DEFAULT_REPO,
    http_get: Callable[..., Any] | None = None,
) -> dict:
    """Return the update status; never raises.

    A fresh cached answer is used unless ``force`` (the manual refresh
    button). If a refresh fails, the last cached answer is returned together
    with ``error`` so the page can keep showing a release that was already
    confirmed without pretending the network call succeeded.
    """
    current = __version__
    now = int(now if now is not None else time.time())

    try:
        enabled = update_check_enabled(conn)
    except Exception:
        logger.debug("update enabled check failed", exc_info=True)
        enabled = True

    if not enabled:
        return _result(enabled=False, current=current)

    try:
        cached = _read_cache(conn, repo)
    except Exception:
        cached = None

    if cached is not None and not force:
        age = now - int(cached["checked_at"])
        if 0 <= age <= CACHE_TTL_SECONDS:
            return _result(
                enabled=True, current=current, release=cached, cached=True
            )

    try:
        release = fetch_latest_release(repo, http_get=http_get)
    except UpdateCheckError as exc:
        return _result(
            enabled=True,
            current=current,
            release=cached,
            cached=cached is not None,
            error=str(exc),
        )
    except Exception as exc:  # defensive: this module must never propagate
        logger.debug("update check failed", exc_info=True)
        return _result(
            enabled=True,
            current=current,
            release=cached,
            cached=cached is not None,
            error=OFFLINE_MESSAGE,
        )

    release["checked_at"] = now
    _write_cache(conn, release)
    return _result(enabled=True, current=current, release=release)
