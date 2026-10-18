"""A small TVDB v4 client, used only for series episode metadata.

TVDB's login is an API key plus, for user-supported keys, a subscriber PIN.
The token lasts about a month; callers pass a cache callback so it survives
across requests without logging in every time. Any 401 clears it and retries
once, which is what happens when a token is revoked or the PIN changes.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

import requests

DEFAULT_BASE = "https://api4.thetvdb.com/v4"
CONNECT_TIMEOUT = 12
READ_TIMEOUT = 30
DEFAULT_USER_AGENT = "IPTVCharts/0.3"

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


class TVDBError(RuntimeError):
    """Raised for any failure talking to TVDB."""


class TVDBKeyError(TVDBError):
    """No key, or TVDB rejected the configured key/PIN."""


def base_url() -> str:
    return os.environ.get("IPTV_CHARTS_TVDB_BASE", DEFAULT_BASE).rstrip("/")


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _episode(raw: dict) -> dict | None:
    number = _to_int(raw.get("number"))
    season = _to_int(raw.get("seasonNumber"))
    if number is None or season is None:
        return None
    return {
        "season": season,
        "episode": number,
        "absolute_number": _to_int(raw.get("absoluteNumber")),
        "title": raw.get("name") or f"Episode {number}",
        "overview": raw.get("overview") or "",
        "air_date": raw.get("aired") or None,
        "tvdb_id": _to_int(raw.get("id")),
        "image": raw.get("image") or None,
        "runtime": _to_int(raw.get("runtime")),
    }


def normalise_search_entry(raw: dict) -> dict | None:
    tvdb_id = _to_int(raw.get("tvdb_id") or raw.get("id"))
    name = raw.get("name") or raw.get("extended_title") or raw.get("title")
    if not tvdb_id or not name:
        return None
    year = _to_int(raw.get("year"))
    if year is None:
        match = _YEAR_RE.search(str(raw.get("first_air_time") or ""))
        year = int(match.group(0)) if match else None
    return {
        "tvdb_id": tvdb_id,
        "title": str(name),
        "year": year,
        "overview": raw.get("overview") or "",
        "image_url": raw.get("image_url") or None,
    }


class TVDBClient:
    def __init__(
        self,
        api_key: str,
        pin: str | None = None,
        *,
        token: str | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise TVDBKeyError("No TVDB API key is configured.")
        self.api_key = key
        self.pin = (pin or "").strip() or None
        self._token = token or None
        self._on_token = on_token
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        })

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "TVDBClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _login(self) -> None:
        body: dict[str, Any] = {"apikey": self.api_key}
        if self.pin:
            body["pin"] = self.pin
        try:
            response = self._session.post(
                f"{base_url()}/login",
                json=body,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            raise TVDBError(f"Could not reach TVDB: {exc}") from exc

        if response.status_code in (401, 403):
            response.close()
            raise TVDBKeyError("TVDB rejected the API key or PIN. Check them in Settings.")
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise TVDBError(f"TVDB login returned HTTP {status}.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TVDBError("TVDB's login response was not JSON.") from exc
        finally:
            response.close()

        data = payload.get("data") if isinstance(payload, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise TVDBError("TVDB's login response did not contain a token.")
        self._token = str(token)
        if self._on_token:
            self._on_token(self._token)

    def _get(self, path: str, params: dict | None = None) -> dict:
        if not self._token:
            self._login()

        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._token}"}
            try:
                response = self._session.get(
                    f"{base_url()}{path}",
                    params=params or {},
                    headers=headers,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
            except requests.RequestException as exc:
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise TVDBError(f"Could not reach TVDB: {exc}") from exc

            if response.status_code == 401 and attempt == 0:
                response.close()
                self._token = None
                self._login()
                continue
            if response.status_code == 404:
                response.close()
                raise TVDBError("TVDB has no record with that id.")
            if response.status_code == 429 and attempt == 0:
                try:
                    wait = min(10.0, max(1.0, float(response.headers.get("Retry-After", "2"))))
                except (TypeError, ValueError):
                    wait = 2.0
                response.close()
                time.sleep(wait)
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise TVDBError(f"TVDB returned HTTP {status}.")

            try:
                return response.json()
            except ValueError as exc:
                raise TVDBError("TVDB returned a response that was not JSON.") from exc
            finally:
                response.close()

        raise TVDBError("TVDB request failed after a retry.")

    def search_series(self, query: str, limit: int = 20) -> list[dict]:
        payload = self._get("/search", {"query": query, "type": "series", "limit": limit})
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        results = []
        for entry in data:
            if isinstance(entry, dict):
                normalised = normalise_search_entry(entry)
                if normalised:
                    results.append(normalised)
        return results

    def series_episodes(self, tvdb_id: int, language: str = "eng") -> list[dict]:
        """Every episode, following pagination, with a hard page cap."""
        episodes: list[dict] = []
        seen: set[tuple[int, int]] = set()
        for page in range(100):
            payload = self._get(
                f"/series/{int(tvdb_id)}/episodes/default/{language}",
                {"page": page},
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                break
            page_episodes = data.get("episodes") or []
            if not isinstance(page_episodes, list):
                break

            for raw in page_episodes:
                if not isinstance(raw, dict):
                    continue
                episode = _episode(raw)
                if not episode:
                    continue
                key = (episode["season"], episode["episode"])
                if key not in seen:
                    seen.add(key)
                    episodes.append(episode)

            links = data.get("links") or payload.get("links") or {}
            if not isinstance(links, dict) or not links.get("next"):
                break

        episodes.sort(key=lambda entry: (entry["season"], entry["episode"]))
        return episodes


__all__ = [
    "TVDBClient",
    "TVDBError",
    "TVDBKeyError",
    "base_url",
    "normalise_search_entry",
]
