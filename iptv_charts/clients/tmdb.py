"""A small TMDB API client authenticated with the API Read Access Token.

The v3 endpoints under /3 are used, but authentication is the current
application method: ``Authorization: Bearer <API Read Access Token>``. The
older 32-character v3 API key query parameter is deliberately not supported.

Only the endpoints the library needs are implemented. Responses are normalised
here so the rest of the app never has to know TMDB's field names or that a TV
show's title is called ``name`` rather than ``title``.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

DEFAULT_BASE = "https://api.themoviedb.org/3"
DEFAULT_IMAGE_BASE = "https://image.tmdb.org/t/p"
CONNECT_TIMEOUT = 12
READ_TIMEOUT = 30
DEFAULT_USER_AGENT = "IPTVCharts/0.7"

#: Sizes accepted by the local image proxy. Deliberately a whitelist rather
#: than "whatever is in the URL": the proxy must not become an SSRF gadget.
IMAGE_SIZES = frozenset({"w92", "w154", "w185", "w342", "w500", "w780", "original"})

#: A v3 API key is 32 hex characters. It is a different credential from the
#: API Read Access Token, which is a JWT and is sent as a bearer token.
_V3_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")


class TMDBError(RuntimeError):
    """Raised for any failure talking to TMDB."""


class TMDBKeyError(TMDBError):
    """No token, or TMDB rejected the configured read access token."""


def validate_read_token(value: Any) -> str:
    """Validate and return the TMDB API Read Access Token.

    TMDB's current application authentication uses the token from the account's
    API settings as ``Authorization: Bearer <token>``. It is a JWT (it starts
    with ``eyJ``), not the older 32-character v3 API key. Rejecting the v3 key
    here gives the user a precise error instead of a generic 401 from TMDB.
    """
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise TMDBKeyError(
            "Add a TMDB API Read Access Token in Settings to use the movie "
            "and series library."
        )
    if _V3_KEY_RE.fullmatch(token):
        raise TMDBKeyError(
            "That is a TMDB v3 API key. Copy the API Read Access Token from "
            "TMDB's API settings instead (it starts with 'eyJ')."
        )
    if not token.startswith("eyJ"):
        raise TMDBKeyError(
            "The TMDB API Read Access Token should start with 'eyJ'. Copy it "
            "from TMDB's API settings."
        )
    if len(token) < 40:
        raise TMDBKeyError("The TMDB API Read Access Token looks too short.")
    if len(token) > 4096:
        raise TMDBKeyError("The TMDB API Read Access Token is too long.")
    return token


def base_url() -> str:
    return os.environ.get("IPTV_CHARTS_TMDB_BASE", DEFAULT_BASE).rstrip("/")


def image_base_url() -> str:
    return os.environ.get("IPTV_CHARTS_TMDB_IMAGE_BASE", DEFAULT_IMAGE_BASE).rstrip("/")


def _year(value: Any) -> int | None:
    text = str(value or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _poster(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith("/") else None


def _alternative_titles(payload: dict) -> list[str]:
    block = payload.get("alternative_titles") or {}
    entries = block.get("titles") or block.get("results") or []
    titles = []
    for entry in entries:
        if isinstance(entry, dict):
            title = entry.get("title")
            if title:
                titles.append(str(title))
    return list(dict.fromkeys(titles))


def _genres(payload: dict) -> list[str]:
    return [
        str(entry.get("name"))
        for entry in payload.get("genres") or []
        if isinstance(entry, dict) and entry.get("name")
    ]


def normalise_summary(raw: dict, *, kind: str) -> dict:
    title = raw.get("title") if kind == "movie" else raw.get("name")
    original = raw.get("original_title") if kind == "movie" else raw.get("original_name")
    date = raw.get("release_date") if kind == "movie" else raw.get("first_air_date")
    return {
        "tmdb_id": int(raw["id"]),
        "title": str(title or original or "Untitled"),
        "original_title": str(original or title or ""),
        "original_language": raw.get("original_language"),
        "year": _year(date),
        "release_date": date or None,
        "overview": raw.get("overview") or "",
        "poster_path": _poster(raw.get("poster_path")),
        "backdrop_path": _poster(raw.get("backdrop_path")),
        "rating": raw.get("vote_average"),
        "kind": kind,
    }


def normalise_movie(raw: dict) -> dict:
    summary = normalise_summary(raw, kind="movie")
    external = raw.get("external_ids") or {}
    return {
        **summary,
        "imdb_id": external.get("imdb_id"),
        "alt_titles": _alternative_titles(raw),
        "runtime": raw.get("runtime"),
        "genres": _genres(raw),
        "status": raw.get("status"),
    }


def normalise_series(raw: dict) -> dict:
    summary = normalise_summary(raw, kind="tv")
    external = raw.get("external_ids") or {}
    return {
        **summary,
        "tvdb_id": external.get("tvdb_id"),
        "alt_titles": _alternative_titles(raw),
        "genres": _genres(raw),
        "status": raw.get("status"),
        "seasons": [
            {
                "season_number": entry.get("season_number"),
                "episode_count": entry.get("episode_count"),
                "air_date": entry.get("air_date"),
            }
            for entry in raw.get("seasons") or []
            if isinstance(entry, dict) and entry.get("season_number") is not None
        ],
    }


def normalise_episode(raw: dict, season_number: int | None = None) -> dict | None:
    number = raw.get("episode_number", raw.get("number"))
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    season = raw.get("season_number", season_number)
    try:
        season = int(season)
    except (TypeError, ValueError):
        return None
    return {
        "season": season,
        "episode": number,
        "absolute_number": raw.get("absolute_number"),
        "title": raw.get("name") or f"Episode {number}",
        "overview": raw.get("overview") or "",
        "air_date": raw.get("air_date") or None,
        "still_path": _poster(raw.get("still_path")),
    }


class TMDBClient:
    """One session, with the small amount of retry TMDB needs in practice.

    Only the API Read Access Token is supported. It is a bearer credential and
    is sent on every request; there is no ``api_key`` query parameter.
    """

    def __init__(self, read_token: str, language: str = "en-US") -> None:
        token = validate_read_token(read_token)
        self.read_token = token
        self.language = (language or "en-US").strip() or "en-US"
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        })

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "TMDBClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> dict:
        query = {"language": self.language, **params}
        url = f"{base_url()}{path}"

        for attempt in range(2):
            try:
                response = self._session.get(
                    url, params=query, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )
            except requests.RequestException as exc:
                # Do not put the exception text in the user-facing error: a
                # requests error string can contain the full URL.
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise TMDBError(
                    "Could not reach TMDB. Check the network connection and the API host."
                ) from exc

            if response.status_code == 429 and attempt == 0:
                try:
                    wait = min(10.0, max(1.0, float(response.headers.get("Retry-After", "2"))))
                except (TypeError, ValueError):
                    wait = 2.0
                response.close()
                time.sleep(wait)
                continue

            if response.status_code in (401, 403):
                response.close()
                raise TMDBKeyError(
                    "TMDB rejected the API Read Access Token. Check it in Settings."
                )
            if response.status_code == 404:
                response.close()
                raise TMDBError("TMDB has no record with that id.")
            if response.status_code >= 500 and attempt == 0:
                response.close()
                time.sleep(1.0)
                continue
            if response.status_code != 200:
                response.close()
                raise TMDBError(f"TMDB returned HTTP {response.status_code}.")

            try:
                return response.json()
            except ValueError as exc:
                raise TMDBError("TMDB returned a response that was not JSON.") from exc
            finally:
                response.close()

        raise TMDBError("TMDB request failed after a retry.")

    # ------------------------------------------------------------------ search
    def search_movies(self, query: str, year: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = int(year)
        payload = self._get("/search/movie", **params)
        return [
            normalise_summary(entry, kind="movie")
            for entry in payload.get("results") or []
            if isinstance(entry, dict) and entry.get("id")
        ]

    def search_series(self, query: str, year: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = int(year)
        payload = self._get("/search/tv", **params)
        return [
            normalise_summary(entry, kind="tv")
            for entry in payload.get("results") or []
            if isinstance(entry, dict) and entry.get("id")
        ]

    # ----------------------------------------------------------------- details
    def movie(self, tmdb_id: int) -> dict:
        raw = self._get(
            f"/movie/{int(tmdb_id)}",
            append_to_response="external_ids,alternative_titles",
        )
        return normalise_movie(raw)

    def series(self, tmdb_id: int) -> dict:
        raw = self._get(
            f"/tv/{int(tmdb_id)}",
            append_to_response="external_ids,alternative_titles",
        )
        return normalise_series(raw)

    def season(self, tmdb_id: int, season_number: int) -> list[dict]:
        raw = self._get(f"/tv/{int(tmdb_id)}/season/{int(season_number)}")
        episodes = []
        for entry in raw.get("episodes") or []:
            if isinstance(entry, dict):
                episode = normalise_episode(entry, season_number=season_number)
                if episode:
                    episodes.append(episode)
        return episodes


__all__ = [
    "IMAGE_SIZES",
    "TMDBClient",
    "TMDBError",
    "TMDBKeyError",
    "base_url",
    "image_base_url",
    "normalise_movie",
    "normalise_series",
    "normalise_summary",
    "validate_read_token",
]
