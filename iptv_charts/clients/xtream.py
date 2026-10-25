"""Client for the Xtream Codes ``player_api.php`` HTTP API.

There is no official specification -- the original vendor is defunct and every
implementation is reverse-engineered. The behaviour encoded here comes from
several independent client libraries and mock servers, plus quirks observed
against real panels.

The notable traps, all of which are handled below:

* ``auth == 1`` is the **only** reliable login signal. ``status`` can report
  "Active" while ``auth`` is 0.
* ``exp_date`` may be ``null``, ``"0"``, or negative, all meaning "no expiry
  recorded" -- not "expired in 1970".
* Credentials travel in the query string, so following a redirect to a login
  page silently loses them. Redirects are disabled.
* Servers routinely return an HTML error page instead of JSON. Parsing that
  blindly produces a confusing traceback instead of a useful message.
* ``stream_id`` goes in the URL; ``num`` is a display-order hint and is
  genuinely a different number. For episodes the field is ``id``.
* Numeric fields arrive as strings, sometimes as ints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse, urlunparse

import requests

from ..errors import SyncCancelled
from .streaming import (
    CountingReader,
    open_maybe_gzip,
    peek_looks_like_object,
    spool_response,
)

# Kept as a private alias so existing callers and tests referring to the old
# name continue to work after the move into streaming.py.
_CountingReader = CountingReader

# ijson's C backend is roughly 14x faster than the pure-Python one (about
# 100 MB/s vs 7 MB/s). On a 200 MB response that is the difference between a
# couple of seconds and half a minute of pegged CPU, which matters for an app
# whose stated goal is staying light. Fail loudly at import rather than
# silently crawling.
try:
    from ijson.backends import yajl2_c as _ijson_backend  # noqa: F401
    IJSON_BACKEND = "yajl2_c"
except ImportError:  # pragma: no cover - depends on how ijson was installed
    import ijson as _ijson_backend  # type: ignore[no-redef]
    IJSON_BACKEND = "python"

#: Xtream panels are PHP applications that are frequently slow to enumerate
#: large catalogs. The read timeout is generous for that reason; the connect
#: timeout stays short so a dead host fails fast.
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 300

DEFAULT_USER_AGENT = "IPTVCharts/0.1"

#: Values providers use to mean "not set" in metadata fields.
_PLACEHOLDERS = {"", "-", "n/a", "na", "null", "none", "0"}


class XtreamError(RuntimeError):
    """Raised for any failure talking to the panel."""


class XtreamAuthError(XtreamError):
    """Raised specifically when the panel rejects the credentials."""


@dataclass
class AccountInfo:
    """Normalised ``user_info`` + ``server_info``."""

    authenticated: bool = False
    status: str | None = None
    is_trial: bool = False
    exp_date: int | None = None
    exp_unknown: bool = True
    active_connections: int | None = None
    max_connections: int | None = None
    created_at: int | None = None
    allowed_formats: list[str] = field(default_factory=list)

    server_host: str | None = None
    server_port: int | None = None
    https_port: int | None = None
    server_protocol: str | None = None
    timezone: str | None = None
    server_time: str | None = None
    raw_user_info: dict = field(default_factory=dict)
    raw_server_info: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        """True only when we know an expiry and it has passed.

        Deliberately False when the expiry is unknown: treating "no expiry
        recorded" as expired would lock users out of working accounts.
        """
        if self.exp_unknown or not self.exp_date:
            return False
        import time

        return int(self.exp_date) < int(time.time())

    @property
    def days_remaining(self) -> int | None:
        if self.exp_unknown or not self.exp_date:
            return None
        import time

        return (int(self.exp_date) - int(time.time())) // 86400


# --------------------------------------------------------------------- helpers
def _to_int(value: Any) -> int | None:
    """Best-effort int conversion; panels use strings, ints and null freely."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    """Xtream booleans arrive as 1, "1", true, "true"."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes")


def _clean(value: Any) -> str | None:
    """Drop placeholder noise so templates don't render 'N/A' everywhere."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _PLACEHOLDERS else text


def normalise_base_url(raw: str) -> str:
    """Turn whatever the user pasted into a scheme://host[:port][/path] base.

    Users paste every variation: with a trailing slash, with
    ``/player_api.php?...`` still attached, with a subpath, with surrounding
    whitespace.
    """
    text = (raw or "").strip()
    if not text:
        raise XtreamError("Server URL is empty.")

    if "://" not in text:
        text = "http://" + text

    parsed = urlparse(text)

    if parsed.scheme not in ("http", "https"):
        raise XtreamError(f"Unsupported URL scheme {parsed.scheme!r}; use http or https.")
    if not parsed.netloc:
        raise XtreamError("Server URL has no host.")
    if parsed.username or parsed.password:
        raise XtreamError(
            "Don't put credentials in the URL -- enter them in the username "
            "and password fields instead."
        )

    path = parsed.path.rstrip("/")
    for suffix in ("/player_api.php", "/panel_api.php", "/get.php", "/xmltv.php"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            break

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")




class XtreamClient:
    """Talks to one Xtream panel."""

    def __init__(self, base_url: str, username: str, password: str,
                 user_agent: str | None = None,
                 referrer: str | None = None,
                 headers: dict | None = None) -> None:
        self.base_url = normalise_base_url(base_url)
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.user_agent = user_agent or DEFAULT_USER_AGENT

        if not self.username or not self.password:
            raise XtreamError("Username and password are required.")

        self._session = requests.Session()
        self._session.headers["User-Agent"] = self.user_agent
        if referrer:
            self._session.headers["Referer"] = str(referrer)
        # Playlist-supplied headers are untrusted. requests itself rejects
        # newlines, but skip malformed entries explicitly so a bad playlist
        # cannot take the whole sync down.
        for name, value in (headers or {}).items():
            if not isinstance(name, str) or not name.strip():
                continue
            text = str(value)
            if any(char in name for char in "\r\n:") or any(char in text for char in "\r\n"):
                continue
            self._session.headers[name.strip()] = text

    # ------------------------------------------------------------------ http
    def _api_url(self) -> str:
        return f"{self.base_url}/player_api.php"

    def _params(self, **extra: Any) -> dict[str, Any]:
        params = {"username": self.username, "password": self.password}
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _open(self, **extra: Any) -> requests.Response:
        """Issue a request, validating that we got JSON-ish content back."""
        try:
            response = self._session.get(
                self._api_url(),
                params=self._params(**extra),
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                stream=True,
                # Credentials live in the query string; a redirect to a login
                # page would drop them and look like an auth failure.
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise XtreamError(f"Could not reach {self.base_url}: {exc}") from exc

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "?")
            response.close()
            raise XtreamError(
                f"Server redirected to {location}. The API path is probably "
                "wrong, or the panel wants a different port."
            )
        if response.status_code != 200:
            response.close()
            raise XtreamError(
                f"Server returned HTTP {response.status_code} for {self._api_url()}"
            )

        return response

    def _get_json(self, **extra: Any) -> Any:
        """Fetch and parse a (small) JSON response."""
        response = self._open(**extra)
        try:
            raw = response.content
        finally:
            response.close()

        text = raw.lstrip()
        if not text:
            raise XtreamError(
                "Server returned an empty response. Check the server URL and port."
            )
        if not text.startswith((b"{", b"[")):
            # Panels serve Cloudflare pages, nginx error pages, and PHP fatal
            # errors with a 200 status surprisingly often.
            snippet = raw[:160].decode("utf-8", errors="replace").strip()
            raise XtreamError(
                "Server returned a non-JSON response where the API was "
                f"expected -- the panel may be down or misconfigured. "
                f"Starts with: {snippet!r}"
            )

        try:
            return json.loads(raw)
        except ValueError as exc:
            raise XtreamError(f"Could not parse the server's response as JSON: {exc}") from exc

    def _spool(self, response: requests.Response, on_progress=None,
               cancel=None) -> tuple[Path, str | None, int]:
        """Drain a response body to disk with exact byte progress.

        See ``clients/streaming.py`` for why this exists rather than parsing
        straight off the socket.
        """
        return spool_response(
            response, on_progress=on_progress, cancel=cancel,
            prefix="xtream-", suffix=".json",
        )

    def _parse_spool(self, path: Path, encoding: str | None, on_progress=None,
                     cancel=None) -> Iterator[dict]:
        """Yield objects from a spooled JSON array, one at a time.

        This is the piece that keeps peak memory flat. A ``get_vod_streams``
        body can be 200 MB; ``json.loads`` on that materialises roughly a
        gigabyte of Python objects, whereas ijson hands us one dict at a time
        so we only ever hold one item plus one insert batch.
        """
        size = path.stat().st_size
        counted = CountingReader(
            open(path, "rb"), total=size, on_progress=on_progress, cancel=cancel
        )
        stream = open_maybe_gzip(path, encoding, counted)

        try:
            # use_float avoids building a Decimal per numeric field, which is
            # both slower and higher-memory across hundreds of thousands of
            # objects -- and would not bind to a REAL column anyway.
            for item in _ijson_backend.items(stream, "item", use_float=True):
                if isinstance(item, dict):
                    yield item
        except SyncCancelled:
            raise
        except Exception as exc:
            raise XtreamError(
                f"Server returned malformed JSON while listing streams: {exc}"
            ) from exc
        finally:
            counted.publish_final()
            counted.close()

    def _check_array_shape(self, path: Path, encoding: str | None) -> None:
        """Fail loudly when a panel returns an object instead of an array."""
        snippet = peek_looks_like_object(path, encoding)
        if snippet is None:
            return  # an array (or empty) -- the normal, healthy case

        raise XtreamError(
            "Expected a list of streams but the server returned an object. "
            "The panel may be reporting an error, or may not support this "
            f"endpoint. Response starts with: {snippet!r}"
        )

    def _iter_json_array(self, on_progress=None, on_phase=None, cancel=None,
                         **extra: Any) -> Iterator[dict]:
        """Fetch a top-level JSON array and yield its objects one at a time.

        Two phases so that both halves of the progress bar have a real
        denominator: spool the bytes (against ``Content-Length``), then parse
        the file (against its size). ``on_phase`` is told which one is running
        so the UI can label it honestly rather than showing one ambiguous bar.
        """
        if on_phase:
            on_phase("download")

        response = self._open(**extra)
        try:
            path, encoding, written = self._spool(response, on_progress, cancel)
        finally:
            response.close()

        try:
            self._check_array_shape(path, encoding)
            if on_phase:
                on_phase("parse")
            yield from self._parse_spool(path, encoding, on_progress, cancel)
        finally:
            path.unlink(missing_ok=True)

    # --------------------------------------------------------------- account
    def account_info(self) -> AccountInfo:
        """Authenticate and return normalised account + server details."""
        payload = self._get_json()

        if not isinstance(payload, dict):
            raise XtreamError("Unexpected response shape from the server.")

        user_info = payload.get("user_info") or {}
        server_info = payload.get("server_info") or {}

        if not user_info:
            raise XtreamAuthError(
                "The server did not return account information. Check the "
                "server URL, username and password."
            )

        # auth == 1 is the only trustworthy success signal.
        authenticated = _truthy(user_info.get("auth"))

        info = AccountInfo(
            authenticated=authenticated,
            status=_clean(user_info.get("status")),
            is_trial=_truthy(user_info.get("is_trial")),
            active_connections=_to_int(user_info.get("active_cons")),
            max_connections=_to_int(user_info.get("max_connections")),
            created_at=_to_int(user_info.get("created_at")),
            server_host=_clean(server_info.get("url")),
            server_port=_to_int(server_info.get("port")),
            https_port=_to_int(server_info.get("https_port")),
            server_protocol=_clean(server_info.get("server_protocol")),
            timezone=_clean(server_info.get("timezone")),
            server_time=_clean(server_info.get("time_now")),
            raw_user_info=user_info,
            raw_server_info=server_info,
        )

        formats = user_info.get("allowed_output_formats")
        if isinstance(formats, list):
            info.allowed_formats = [str(f).strip() for f in formats if str(f).strip()]
        elif isinstance(formats, str) and formats.strip():
            info.allowed_formats = [f.strip() for f in formats.split(",") if f.strip()]

        exp_raw = user_info.get("exp_date")
        exp = _to_int(exp_raw)
        # null / 0 / negative all mean "no expiry recorded". Recording that
        # explicitly stops the UI inventing a 1970 expiry date.
        if exp is None or exp <= 0:
            info.exp_unknown = True
            info.exp_date = None
        else:
            info.exp_unknown = False
            info.exp_date = exp

        if not authenticated:
            raise XtreamAuthError(
                "The server rejected these credentials"
                + (f" (account status: {info.status})" if info.status else "")
                + "."
            )

        return info

    # ------------------------------------------------------------ categories
    def categories(self, kind: str) -> list[dict]:
        """Return categories for 'live', 'vod' or 'series'."""
        action = {
            "live": "get_live_categories",
            "vod": "get_vod_categories",
            "series": "get_series_categories",
        }[kind]

        payload = self._get_json(action=action)
        if not isinstance(payload, list):
            return []

        result = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            result.append({
                # IDs are inconsistent across panels; normalise to str so the
                # database key is stable.
                "remote_id": _clean(entry.get("category_id")),
                "name": _clean(entry.get("category_name")) or "Unnamed",
                "parent_id": _clean(entry.get("parent_id")),
            })
        return result

    # --------------------------------------------------------------- streams
    def streams(self, kind: str, category_id: str | None = None,
                on_progress=None, on_phase=None, cancel=None) -> Iterator[dict]:
        """Stream raw stream objects for 'live', 'vod' or 'series'."""
        action = {
            "live": "get_live_streams",
            "vod": "get_vod_streams",
            "series": "get_series",
        }[kind]

        yield from self._iter_json_array(
            on_progress=on_progress,
            on_phase=on_phase,
            cancel=cancel,
            action=action,
            category_id=category_id,
        )

    # ---------------------------------------------------------------- detail
    def vod_info(self, vod_id: str) -> dict:
        """Full metadata for one movie. Fetched lazily, on first open."""
        payload = self._get_json(action="get_vod_info", vod_id=vod_id)
        return payload if isinstance(payload, dict) else {}

    def series_info(self, series_id: str) -> dict:
        """Seasons and episodes for one series.

        Note ``episodes`` is keyed by season number as a *string*, not an
        array, and a series' own ``seasons`` list can be empty even when
        episodes exist -- so callers must render from ``episodes``.
        """
        payload = self._get_json(action="get_series_info", series_id=series_id)
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ urls
    def stream_url(self, kind: str, stream_id: str, container_ext: str | None = None,
                   prefer_hls: bool = False) -> str:
        """Build a playable URL.

        ``stream_id`` is the provider's real identifier. It is NOT ``num``,
        which is only a display-order hint and produces a 404 if used here.

        For episodes the caller must pass the episode's ``id`` as ``stream_id``.
        """
        base = self.base_url
        if kind == "live":
            extension = "m3u8" if prefer_hls else "ts"
            return f"{base}/live/{self.username}/{self.password}/{stream_id}.{extension}"
        if kind == "vod":
            extension = container_ext or "mp4"
            return f"{base}/movie/{self.username}/{self.password}/{stream_id}.{extension}"
        if kind == "series":
            extension = container_ext or "mp4"
            return f"{base}/series/{self.username}/{self.password}/{stream_id}.{extension}"
        raise XtreamError(f"Unknown stream kind {kind!r}")

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "XtreamClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _identity_id(value: Any) -> str | None:
    """Return an unambiguous id, or None for placeholders and junk."""
    text = _clean(value)
    return text.strip() if text else None


def _tmdb_id(value: Any) -> str | None:
    text = _identity_id(value)
    return text if text and text.isdigit() and int(text) > 0 else None


def _imdb_id(value: Any) -> str | None:
    text = _identity_id(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered.startswith("tt") and lowered[2:].isdigit() and len(lowered) >= 7:
        return lowered
    return None


def _payload_identity(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    tmdb = None
    for key in ("tmdb_id", "tmdb", "tmdbid", "themoviedb_id"):
        tmdb = _tmdb_id(payload.get(key))
        if tmdb:
            break
    imdb = None
    for key in ("imdb_id", "imdb", "imdbid"):
        imdb = _imdb_id(payload.get(key))
        if imdb:
            break
    return tmdb, imdb


def detail_identity(payload: dict) -> tuple[str | None, str | None]:
    """TMDB/IMDb ids from a get_vod_info/get_series_info response.

    Panels variously place them in ``info``, ``movie_data`` or at the top
    level. Only explicit ids are accepted; nothing is derived from a title.
    """
    if not isinstance(payload, dict):
        return None, None
    tmdb, imdb = _payload_identity(payload)
    for key in ("info", "movie_data", "series_data"):
        found_tmdb, found_imdb = _payload_identity(payload.get(key))
        tmdb = tmdb or found_tmdb
        imdb = imdb or found_imdb
    return tmdb, imdb


def normalise_stream(kind: str, raw: dict) -> dict:
    """Map a raw panel stream object onto our item columns.

    Handles the field-placement differences between live, VOD and series, and
    the ``stream_id`` vs ``num`` vs ``id`` distinction.
    """
    if kind == "live":
        remote_id = _clean(raw.get("stream_id"))
        name = _clean(raw.get("name"))
        logo = _clean(raw.get("stream_icon"))
        container = None
    elif kind == "vod":
        remote_id = _clean(raw.get("stream_id"))
        name = _clean(raw.get("name"))
        logo = _clean(raw.get("stream_icon"))
        container = _clean(raw.get("container_extension"))
    else:  # series
        # Series use `series_id`, and it is only ever used to fetch details --
        # never in a playback URL.
        remote_id = _clean(raw.get("series_id"))
        name = _clean(raw.get("name"))
        logo = _clean(raw.get("cover")) or _clean(raw.get("stream_icon"))
        container = None

    tmdb_id, imdb_id = _payload_identity(raw)
    # Some panels nest the ids one level down even in the list payload.
    for key in ("info", "movie_data", "series_data"):
        nested_tmdb, nested_imdb = _payload_identity(raw.get(key))
        tmdb_id = tmdb_id or nested_tmdb
        imdb_id = imdb_id or nested_imdb

    return {
        "remote_id": remote_id,
        "name": name or "",
        "num": _to_int(raw.get("num")),
        "logo": logo,
        "epg_channel_id": _clean(raw.get("epg_channel_id")),
        "container_ext": container,
        # `added` is documented as a string but real panels send ints too.
        "added_ts": _to_int(raw.get("added")),
        "tv_archive": _to_int(raw.get("tv_archive")),
        "category_remote_id": _clean(raw.get("category_id")),
        "rating": None,  # only present in detail responses
        "plot": None,
        "genre": None,
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
    }


__all__ = [
    "AccountInfo",
    "XtreamAuthError",
    "XtreamClient",
    "XtreamError",
    "detail_identity",
    "normalise_base_url",
    "normalise_stream",
]
