"""Resolve a library item or provider episode into a downloadable stream.

A stream is always rebuilt from the stored provider identity at use time; no
provider URL is ever persisted in the library. The same resolver is used by
the downloader and by the media probe, so both see identical headers,
container extension and HLS classification.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import db, items, naming

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class StreamError(RuntimeError):
    """A provider stream cannot be rebuilt."""


@dataclass(frozen=True)
class StreamRef:
    url: str
    headers: dict[str, str]
    container_ext: str
    hls: bool
    source_id: int
    provider_kind: str          # vod | series
    provider_remote_id: str
    episode_stream_id: str = ""

    @property
    def probe_key(self) -> tuple[int, str, str, str]:
        return (
            int(self.source_id), self.provider_kind,
            str(self.provider_remote_id), self.episode_stream_id,
        )


def _json_headers(row: Mapping[str, Any] | None) -> dict:
    if not row:
        return {}
    try:
        value = json.loads(row["request_headers"] or "{}")
        return value if isinstance(value, dict) else {}
    except (KeyError, TypeError, ValueError):
        return {}


def clean_header(name: Any, value: Any) -> tuple[str, str] | None:
    name_text = str(name or "").strip()
    value_text = str(value or "").strip()
    if not name_text or not _HEADER_NAME_RE.match(name_text):
        return None
    if not value_text or any(char in value_text for char in "\r\n\x00"):
        return None
    return name_text, value_text[:4096]


def effective_headers(source_row, item_row=None) -> dict[str, str]:
    """Merge source and per-item headers, with the same trust rules.

    Playlist-provided header values are untrusted input, especially when they
    are eventually passed to ffmpeg. Names must be HTTP tokens and values may
    not contain CR/LF or NUL.
    """
    source = _json_headers(source_row)
    item = _json_headers(item_row)
    user_agent = item.get("user_agent") or source.get("user_agent") or "IPTVCharts/0.3"
    referrer = item.get("referrer") or source.get("referrer")
    headers: dict[str, str] = {}
    if user_agent:
        cleaned = clean_header("User-Agent", user_agent)
        if cleaned:
            headers[cleaned[0]] = cleaned[1]
    if referrer:
        cleaned = clean_header("Referer", referrer)
        if cleaned:
            headers[cleaned[0]] = cleaned[1]
    for block in (source.get("headers"), item.get("headers")):
        if not isinstance(block, dict):
            continue
        for name, value in block.items():
            cleaned = clean_header(name, value)
            if cleaned and cleaned[0].lower() not in {
                "user-agent", "referer", "referrer"
            }:
                headers[cleaned[0]] = cleaned[1]
    return headers


def ffmpeg_headers(headers: Mapping[str, str]) -> str | None:
    if not headers:
        return None
    lines = [f"{name}: {value}" for name, value in headers.items()]
    return "\r\n".join(lines) + "\r\n"


def ffmpeg_header_args(headers: Mapping[str, str]) -> list[str]:
    """Build ffmpeg/ffprobe input arguments without duplicating User-Agent.

    ffmpeg has a dedicated ``-user_agent`` option; leaving the same header in
    ``-headers`` as well can make the HTTP layer send two User-Agent lines,
    which some panels reject. Everything else goes through ``-headers``.
    """
    user_agent = None
    rest: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() == "user-agent":
            user_agent = value
        else:
            rest[name] = value
    arguments: list[str] = []
    if user_agent:
        arguments.extend(["-user_agent", user_agent])
    block = ffmpeg_headers(rest)
    if block:
        arguments.extend(["-headers", block])
    return arguments


def extension_from_url(url: str) -> str:
    from pathlib import Path

    suffix = Path(urlsplit(url).path).suffix.lower().lstrip(".")
    return naming.safe_extension(suffix, "mp4") if suffix else "mp4"


def _validate_url(url: str) -> None:
    scheme = (urlsplit(url).scheme or "").lower()
    if scheme not in ("http", "https"):
        raise StreamError(
            f"Only http/https streams can be downloaded, not {scheme or 'this URL'}."
        )


def _hls(url: str) -> bool:
    return items.guess_media_type(url) == "hls"


def movie_stream(conn, source_id: int, remote_id: str) -> StreamRef:
    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (int(source_id),))
    if source is None:
        raise StreamError("The playlist this release came from no longer exists.")
    source = dict(source)
    item = db.get_one(
        conn,
        "SELECT * FROM items WHERE source_id = ? AND kind = 'vod' AND remote_id = ?",
        (int(source_id), str(remote_id)),
    )
    if item is None:
        raise StreamError(
            "That VOD is no longer in the playlist. Update the playlist and try again."
        )
    item = dict(item)
    try:
        url = items.build_stream_url(source, item, prefer_hls=False)
    except items.PlaybackUnavailable as exc:
        raise StreamError(str(exc)) from exc
    _validate_url(url)
    extension = naming.safe_extension(
        item.get("container_ext") or extension_from_url(url)
    )
    hls = _hls(url)
    return StreamRef(
        url=url,
        headers=effective_headers(source, item),
        container_ext="mkv" if hls else extension,
        hls=hls,
        source_id=int(source_id),
        provider_kind="vod",
        provider_remote_id=str(remote_id),
    )


def episode_stream(conn, source_id: int, provider_remote_id: str, stream_id: str,
                   container_ext: str | None = None) -> StreamRef:
    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (int(source_id),))
    if source is None:
        raise StreamError("The playlist this episode came from no longer exists.")
    source = dict(source)
    if source["kind"] != "xtream":
        raise StreamError("Episode downloads need an Xtream playlist.")
    extension = naming.safe_extension(container_ext, "mp4")
    try:
        url = items.build_episode_url(source, str(stream_id), extension)
    except items.PlaybackUnavailable as exc:
        raise StreamError(str(exc)) from exc
    _validate_url(url)
    hls = _hls(url)
    return StreamRef(
        url=url,
        headers=effective_headers(source),
        container_ext="mkv" if hls else extension,
        hls=hls,
        source_id=int(source_id),
        provider_kind="series",
        provider_remote_id=str(provider_remote_id),
        episode_stream_id=str(stream_id),
    )


def download_stream(conn, download: Mapping[str, Any]) -> StreamRef:
    """Rebuild the stream for a queue row."""
    source_id = download.get("source_id")
    if source_id is None:
        raise StreamError("The playlist this was grabbed from no longer exists.")
    if download["kind"] == "movie":
        return movie_stream(conn, int(source_id), str(download["provider_remote_id"]))
    return episode_stream(
        conn, int(source_id), str(download["provider_remote_id"]),
        str(download.get("episode_stream_id") or ""),
        download.get("container_ext"),
    )


__all__ = [
    "StreamError",
    "StreamRef",
    "clean_header",
    "download_stream",
    "effective_headers",
    "episode_stream",
    "extension_from_url",
    "ffmpeg_header_args",
    "ffmpeg_headers",
    "movie_stream",
]
