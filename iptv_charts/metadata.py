"""Metadata providers, caching, and the local image proxy.

TMDB is the primary source for movies and series. TVDB supplies episode
metadata when a key is configured; when it is missing or fails, TMDB seasons
are used instead so the library still works.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from . import config, db, matching
from .clients import tmdb, tvdb

logger = logging.getLogger("iptv_charts.metadata")

#: The current setting name. The old name is still read so an existing JWT
#: saved by an earlier version keeps working; a v3 API key is rejected by the
#: TMDB client because the app now uses bearer-token authentication.
TMDB_KEY = "tmdb_read_token"
TMDB_LEGACY_KEY = "tmdb_api_key"
TVDB_KEY = "tvdb_api_key"
TVDB_PIN = "tvdb_pin"
LANGUAGE = "metadata_language"
DEFAULT_LANGUAGE = "en-US"

SEARCH_TTL = 15 * 60
DETAILS_TTL = 6 * 60 * 60
TVDB_TOKEN_KEY = "tvdb:token"

IMAGE_PATH_RE = re.compile(r"^/[A-Za-z0-9._-]+\.(?:jpe?g|png|webp)$", re.IGNORECASE)
IMAGE_MAX_BYTES = 15 * 1024 * 1024


class MetadataError(RuntimeError):
    """A provider is missing or returned something unusable."""


# --------------------------------------------------------------------- settings
def _setting(conn, key: str, default: str = "") -> str:
    try:
        return (db.get_setting(conn, key, default) or default).strip()
    except Exception:
        return default


def language(conn) -> str:
    return _setting(conn, LANGUAGE, DEFAULT_LANGUAGE) or DEFAULT_LANGUAGE


def tmdb_token(conn) -> str:
    """Return the current token, falling back to the pre-0.5 setting name."""
    token = _setting(conn, TMDB_KEY)
    if token:
        return token
    return _setting(conn, TMDB_LEGACY_KEY)


def has_tmdb(conn) -> bool:
    try:
        tmdb.validate_read_token(tmdb_token(conn))
    except tmdb.TMDBKeyError:
        return False
    return True


def has_tvdb(conn) -> bool:
    return bool(_setting(conn, TVDB_KEY))


def tmdb_client(conn) -> tmdb.TMDBClient:
    token = tmdb_token(conn)
    if not token:
        raise tmdb.TMDBKeyError(
            "Add a TMDB API Read Access Token in Settings to use the movie "
            "and series library."
        )
    return tmdb.TMDBClient(token, language(conn))


def tvdb_client(conn) -> tvdb.TVDBClient:
    key = _setting(conn, TVDB_KEY)
    if not key:
        raise tvdb.TVDBKeyError(
            "Add a TVDB API key in Settings to use TVDB episode data."
        )
    return tvdb.TVDBClient(
        key,
        _setting(conn, TVDB_PIN) or None,
        token=_tvdb_token(conn),
        on_token=lambda token: _save_tvdb_token(conn, token),
    )


# ------------------------------------------------------------------------ cache
def cache_get(conn, key: str, max_age: int | None = None) -> Any:
    row = db.get_one(
        conn, "SELECT payload, fetched_at FROM metadata_cache WHERE key = ?", (key,)
    )
    if row is None:
        return None
    if max_age is not None and int(time.time()) - int(row["fetched_at"]) > max_age:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def cache_set(conn, key: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO metadata_cache (key, payload, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, "
        "fetched_at = excluded.fetched_at",
        (key, json.dumps(payload, ensure_ascii=False), int(time.time())),
    )
    conn.commit()


def _search_key(service: str, kind: str, query: str, year: int | None, lang: str) -> str:
    return f"{service}:{kind}:search:{lang}:{year or ''}:{query.casefold()}"


def clear_cache(conn) -> None:
    conn.execute("DELETE FROM metadata_cache")
    conn.commit()


# ----------------------------------------------------------------------- TMDB
def search_movies(conn, query: str, year: int | None = None) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    key = _search_key("tmdb", "movie", query, year, language(conn))
    cached = cache_get(conn, key, SEARCH_TTL)
    if cached is not None:
        return cached
    with tmdb_client(conn) as client:
        results = client.search_movies(query, year)
    cache_set(conn, key, results)
    return results


def search_series(conn, query: str, year: int | None = None) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    key = _search_key("tmdb", "tv", query, year, language(conn))
    cached = cache_get(conn, key, SEARCH_TTL)
    if cached is not None:
        return cached
    with tmdb_client(conn) as client:
        results = client.search_series(query, year)
    cache_set(conn, key, results)
    return results


def movie_details(conn, tmdb_id: int, *, refresh: bool = False) -> dict:
    key = f"tmdb:movie:{int(tmdb_id)}:{language(conn)}"
    cached = None if refresh else cache_get(conn, key, DETAILS_TTL)
    if cached is not None:
        return cached
    with tmdb_client(conn) as client:
        details = client.movie(tmdb_id)
    cache_set(conn, key, details)
    return details


def series_details(conn, tmdb_id: int, *, refresh: bool = False) -> dict:
    key = f"tmdb:tv:{int(tmdb_id)}:{language(conn)}"
    cached = None if refresh else cache_get(conn, key, DETAILS_TTL)
    if cached is not None:
        return cached
    with tmdb_client(conn) as client:
        details = client.series(tmdb_id)
    cache_set(conn, key, details)
    return details


# ----------------------------------------------------------------------- TVDB
def _tvdb_token(conn) -> str | None:
    cached = cache_get(conn, TVDB_TOKEN_KEY)
    if isinstance(cached, dict):
        token = cached.get("token")
        return str(token) if token else None
    return None


def _save_tvdb_token(conn, token: str) -> None:
    cache_set(conn, TVDB_TOKEN_KEY, {"token": token})


def find_tvdb_id(conn, series_row) -> int | None:
    """Resolve a TVDB series id, preferably from TMDB's external ids."""
    existing = series_row["tvdb_id"] if "tvdb_id" in series_row.keys() else None
    if existing:
        return int(existing)
    if not has_tvdb(conn):
        return None

    titles = matching.aliases_from_row(series_row)
    year = series_row["year"] if "year" in series_row.keys() else None
    try:
        with tvdb_client(conn) as client:
            results = client.search_series(
                str(series_row["title"]), year=int(year) if year else None
            )
    except tvdb.TVDBError as exc:
        logger.warning("TVDB search failed: %s", exc)
        return None

    best: tuple[float, int] | None = None
    for result in results:
        candidate_titles = matching.title_variants(result.get("title"))
        if not candidate_titles:
            continue
        name_score = max(
            (matching.similarity(title, candidate_titles[0]) for title in titles),
            default=0.0,
        )
        if year and result.get("year"):
            difference = abs(int(year) - int(result["year"]))
            if difference > 1:
                name_score -= 0.25
            elif difference == 1:
                name_score -= 0.05
        if name_score >= 0.75 and (best is None or name_score > best[0]):
            best = (name_score, int(result["tvdb_id"]))
    return best[1] if best else None


def episodes_for_series(conn, series_row, *, refresh: bool = False) -> tuple[list[dict], str, int | None]:
    """Episodes for a library series, from TVDB when possible.

    Returns ``(episodes, source, tvdb_id)`` where source is ``"tvdb"`` or
    ``"tmdb"``. A TVDB outage is logged and degrades to TMDB rather than
    breaking the series page.
    """
    tvdb_id = find_tvdb_id(conn, series_row) if has_tvdb(conn) else None
    if tvdb_id:
        try:
            with tvdb_client(conn) as client:
                episodes = client.series_episodes(tvdb_id)
            if episodes:
                return episodes, "tvdb", tvdb_id
            logger.warning("TVDB returned no episodes for series %s; falling back", tvdb_id)
        except tvdb.TVDBKeyError as exc:
            logger.warning("TVDB not usable: %s", exc)
        except tvdb.TVDBError as exc:
            logger.warning("TVDB episode fetch failed: %s", exc)

    # TMDB fallback. The season list comes from the series details, and each
    # season is one extra request; that is acceptable for an add/refresh.
    details = series_details(conn, series_row["tmdb_id"], refresh=refresh)
    episodes: list[dict] = []
    with tmdb_client(conn) as client:
        for season in (details.get("seasons") or [])[:80]:
            number = season.get("season_number")
            if number is None:
                continue
            key = f"tmdb:season:{int(series_row['tmdb_id'])}:{int(number)}:{language(conn)}"
            cached = None if refresh else cache_get(conn, key, DETAILS_TTL)
            if cached is None:
                cached = client.season(int(series_row["tmdb_id"]), int(number))
                cache_set(conn, key, cached)
            episodes.extend(cached)
    return episodes, "tmdb", tvdb_id


# --------------------------------------------------------------------- images
def image_url(size: str, path: str | None) -> str | None:
    """Local proxy URL for a TMDB poster path, or None when there is none."""
    if not path or size not in tmdb.IMAGE_SIZES:
        return None
    cleaned = str(path).lstrip("/")
    return f"/api/images/tmdb/{size}/{cleaned}"


def _image_cache_path(size: str, path: str) -> Path:
    digest = hashlib.sha256(f"{size}:{path}".encode("utf-8")).hexdigest()
    suffix = Path(path).suffix.lower() or ".jpg"
    directory = config.cache_dir() / "images"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{digest}{suffix}"


def fetch_image(size: str, path: str) -> tuple[bytes, str]:
    """Fetch one TMDB image through a strict allowlist and cache it locally.

    ``size`` and ``path`` come from a URL, so both are validated here. The
    request only ever goes to the configured TMDB image host and a redirect
    that leaves that host is refused.
    """
    if size not in tmdb.IMAGE_SIZES:
        raise MetadataError("Unknown image size.")
    path = "/" + str(path or "").lstrip("/")
    if not IMAGE_PATH_RE.match(path):
        raise MetadataError("Invalid image path.")

    cache_path = _image_cache_path(size, path)
    content_type = mimetypes.guess_type(path)[0] or "image/jpeg"
    if cache_path.exists():
        return cache_path.read_bytes(), content_type

    base = tmdb.image_base_url()
    url = f"{base}/{size}{path}"
    base_netloc = urlsplit(base).netloc
    try:
        response = requests.get(
            url,
            timeout=(12, 30),
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": tmdb.DEFAULT_USER_AGENT},
        )
    except requests.RequestException as exc:
        raise MetadataError(f"Could not fetch the image: {exc}") from exc

    try:
        if response.status_code != 200:
            raise MetadataError(f"The image server returned HTTP {response.status_code}.")
        if urlsplit(response.url).netloc != base_netloc:
            raise MetadataError("The image server redirected to another host.")
        if not (response.headers.get("Content-Type") or "").lower().startswith("image/"):
            raise MetadataError("The image server did not return an image.")

        total = 0
        chunks: list[bytes] = []
        for chunk in response.iter_content(128 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > IMAGE_MAX_BYTES:
                raise MetadataError("The image is too large.")
            chunks.append(chunk)
    finally:
        response.close()

    data = b"".join(chunks)
    temporary = cache_path.with_suffix(cache_path.suffix + ".part")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, cache_path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return data, content_type


__all__ = [
    "LANGUAGE",
    "MetadataError",
    "TMDB_KEY",
    "TMDB_LEGACY_KEY",
    "TVDB_KEY",
    "TVDB_PIN",
    "cache_get",
    "cache_set",
    "clear_cache",
    "episodes_for_series",
    "fetch_image",
    "find_tvdb_id",
    "has_tmdb",
    "has_tvdb",
    "image_url",
    "language",
    "movie_details",
    "search_movies",
    "search_series",
    "series_details",
    "tmdb_client",
    "tmdb_token",
    "tvdb_client",
]
