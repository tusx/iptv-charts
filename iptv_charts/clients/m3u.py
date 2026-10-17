"""Streaming parser for extended M3U playlists.

There is no formal specification for the IPTV dialect of M3U; the de-facto
reference is Kodi's pvr.iptvsimple. The rules encoded here come from that
reference plus bugs observed in real playlists.

Design notes:

* **Streaming, always.** Providers ship playlists with tens to hundreds of
  thousands of entries. Materialising them as Python objects costs hundreds of
  megabytes, so this parser consumes an iterable of lines and yields one
  channel at a time; the caller decides what to keep.

* **Quote-aware comma splitting.** ``#EXTINF`` separates its attribute list
  from the display name with a comma, but commas also appear *inside* quoted
  attribute values::

      #EXTINF:-1 tvg-name="The Good, the Bad and the Ugly" group-title="Movies",The Good, the Bad and the Ugly

  Splitting on the first comma is a genuine, frequently-shipped bug and
  truncates the name to " the Bad and the Ugly". We scan for the first comma
  that is not inside quotes.

* **BOM tolerance.** A leading U+FEFF would otherwise stop the ``#EXTM3U``
  check from matching and cause a perfectly valid playlist to be rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlparse

import requests

from .streaming import iter_text_lines, spool_response

BOM = "﻿"

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 300
DEFAULT_USER_AGENT = "IPTVCharts/0.1"

#: Extensions that mean "this is a file to download and watch", as opposed to
#: a live stream. Used to classify M3U entries, which carry no explicit type.
VOD_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v",
    ".mpg", ".mpeg", ".webm", ".ts.vod",
})

#: Audio-only entries. Treated as live for our purposes -- they are continuous
#: streams, and they have no poster art or plot to show.
AUDIO_EXTENSIONS = frozenset({".mp3", ".aac", ".flac", ".ogg", ".m4a", ".wav"})


class M3UError(RuntimeError):
    """Raised when a playlist cannot be fetched or is not a channel list."""


def classify_kind(url: str, radio: bool = False) -> str:
    """Decide whether an M3U entry is a live channel or an on-demand item.

    M3U has no type field, so this is a heuristic on the URL extension. It is
    deliberately biased towards "live": a misclassified live channel merely
    sorts oddly, whereas calling a live channel VOD would offer it a seek bar
    that cannot work.
    """
    if radio:
        return "live"
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in VOD_EXTENSIONS:
        return "vod"
    return "live"

#: Any line starting with this marks a *real* HLS playlist rather than a
#: channel list. Both formats begin with ``#EXTM3U``, so this is the only
#: reliable discriminator.
HLS_TAG_PREFIX = "#EXT-X-"

# key="value" | key='value' | key=value
_ATTR_RE = re.compile(
    r"""([A-Za-z0-9_.\-]+)      # key
        \s*=\s*
        (?:
            "((?:[^"\\]|\\.)*)"   # "double quoted"
          | '((?:[^'\\]|\\.)*)'   # 'single quoted'
          | ([^\s,]+)             # bare
        )""",
    re.VERBOSE,
)

# Attribute names vary between playlist generators. Normalise the aliases we
# know about onto one spelling each.
_ATTR_ALIASES = {
    "tvg_id": "tvg-id",
    "tvg_name": "tvg-name",
    "tvg_logo": "tvg-logo",
    "tvg_chno": "tvg-chno",
    "logo": "tvg-logo",
    "group": "group-title",
    "group_title": "group-title",
    "channel-id": "tvg-id",
}


@dataclass(slots=True)
class Channel:
    """One entry from a playlist."""

    name: str
    url: str
    group: str | None = None
    tvg_id: str | None = None
    tvg_name: str | None = None
    logo: str | None = None
    tvg_chno: str | None = None
    duration: str | None = None
    radio: bool = False

    # Per-stream HTTP requirements. Many providers 403 without these, and mpv
    # ignores VLC's #EXTVLCOPT directives, so we extract them here and the
    # player module translates them into real mpv flags.
    user_agent: str | None = None
    referrer: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    #: Everything else, preserved so nothing is silently lost.
    raw_attrs: dict[str, str] = field(default_factory=dict)


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\'", "'")


def split_extinf(line: str) -> tuple[str | None, dict[str, str], str]:
    """Split an ``#EXTINF`` line into (duration, attributes, display name).

    The split point is the first comma that is not inside a quoted attribute
    value -- see the module docstring for why this matters.
    """
    body = line[len("#EXTINF:"):] if line.startswith("#EXTINF:") else line

    head, sep, name = _split_outside_quotes(body)
    if sep is None:
        # No unquoted comma at all: malformed, but treat it all as attributes.
        head, name = body, ""

    duration = None
    attrs: dict[str, str] = {}

    stripped = head.strip()
    if stripped:
        first, _, rest = stripped.partition(" ")
        # The duration token is numeric (often -1); if it isn't, the line has
        # no duration and everything is attributes.
        if re.fullmatch(r"-?\d+(\.\d+)?", first):
            duration = first
            attr_text = rest
        else:
            attr_text = stripped

        for match in _ATTR_RE.finditer(attr_text):
            key = match.group(1).lower()
            value = match.group(2) or match.group(3) or match.group(4) or ""
            key = _ATTR_ALIASES.get(key, key)
            attrs[key] = _unescape(value)

    return duration, attrs, name.strip()


def _split_outside_quotes(text: str) -> tuple[str, str | None, str]:
    """Split at the first comma that is not within quotes.

    Returns (before, separator, after); separator is None when no such comma
    exists.
    """
    quote: str | None = None
    index = 0
    length = len(text)

    while index < length:
        char = text[index]

        if char == "\\" and index + 1 < length:
            index += 2  # skip the escaped character
            continue

        if quote is not None:
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char == ",":
            return text[:index], ",", text[index + 1:]

        index += 1

    return text, None, ""


def _split_url_suffix(url: str) -> tuple[str, dict[str, str]]:
    """Handle Kodi's ``url|Header=value&Header2=value`` extension.

    Non-standard headers are written with a leading ``!``.
    """
    if "|" not in url:
        return url, {}

    base, _, suffix = url.partition("|")
    params: dict[str, str] = {}
    for pair in suffix.split("&"):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        params[key.strip().lstrip("!").lower()] = value.strip()
    return base, params


def parse_lines(lines: Iterable[str]) -> Iterator[Channel]:
    """Yield Channels from an iterable of text lines.

    Consumers should feed this a line iterator rather than a materialised list
    so that peak memory stays independent of playlist size.
    """
    pending: dict[str, str] | None = None
    pending_duration: str | None = None
    pending_groups: list[str] = []
    pending_headers: dict[str, str] = {}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # Strip a BOM wherever it landed on the first meaningful line.
        if line.startswith(BOM):
            line = line[len(BOM):]
            if not line:
                continue

        if line.startswith("#EXTINF:"):
            duration, attrs, name = split_extinf(line)
            pending = attrs
            pending_duration = duration
            pending_groups = []
            pending_headers = {}

            # A missing display name falls back to tvg-name, which is what
            # most players do.
            if not name:
                name = attrs.get("tvg-name", "")
            pending["__name__"] = name
            continue

        if line.startswith("#EXTGRP:"):
            # Groups from #EXTGRP union with group-title rather than replacing.
            value = line[len("#EXTGRP:"):].strip()
            if value:
                pending_groups.extend(g for g in value.split(";") if g)
            continue

        if line.startswith("#EXTVLCOPT:"):
            # VLC-specific options. mpv ignores these entirely, so we pull out
            # the ones that matter and translate them later.
            option = line[len("#EXTVLCOPT:"):].strip()
            key, _, value = option.partition("=")
            key = key.strip().lower()
            value = value.strip()
            if key in ("http-user-agent", "user-agent"):
                pending_headers["user_agent"] = value
            elif key in ("http-referrer", "http-referer", "referrer"):
                pending_headers["referrer"] = value
            elif key == "http-header" and ":" in value:
                header_name, _, header_value = value.partition(":")
                pending_headers.setdefault("headers", {})
                pending_headers["headers"][header_name.strip()] = header_value.strip()  # type: ignore[index]
            continue

        if line.startswith("#"):
            # #KODIPROP, #EXTM3U and the HLS tags carry no per-channel data.
            continue

        # Anything else is a URL, which completes the current entry.
        if pending is None:
            # A bare URL with no #EXTINF. Real playlists do contain these; use
            # the filename as a last-resort name rather than dropping it.
            url, suffix = _split_url_suffix(line)
            yield Channel(
                name=url.rsplit("/", 1)[-1] or url,
                url=url,
                user_agent=suffix.get("user-agent"),
                referrer=suffix.get("referrer") or suffix.get("referer"),
            )
            continue

        url, suffix = _split_url_suffix(line)

        groups = [g.strip() for g in (pending.get("group-title") or "").split(";") if g.strip()]
        for extra in pending_groups:
            if extra not in groups:
                groups.append(extra)

        headers = dict(pending_headers.get("headers", {}))  # type: ignore[arg-type]
        for key, value in suffix.items():
            if key == "user-agent":
                pending_headers["user_agent"] = value
            elif key in ("referrer", "referer"):
                pending_headers["referrer"] = value
            else:
                headers[key] = value

        channel = Channel(
            name=pending.pop("__name__", "") or url,
            url=url,
            group=";".join(groups) if groups else None,
            tvg_id=pending.get("tvg-id") or None,
            tvg_name=pending.get("tvg-name") or None,
            logo=pending.get("tvg-logo") or None,
            tvg_chno=pending.get("tvg-chno") or None,
            duration=pending_duration,
            radio=(pending.get("radio", "").lower() == "true"),
            user_agent=pending_headers.get("user_agent"),
            referrer=pending_headers.get("referrer"),
            headers=headers,
            raw_attrs=pending,
        )
        yield channel

        pending = None
        pending_duration = None
        pending_groups = []
        pending_headers = {}


def parse_text(text: str) -> Iterator[Channel]:
    """Convenience wrapper for small playlists already held in memory."""
    return parse_lines(text.splitlines())


def looks_like_hls(text_head: str) -> bool:
    """True when the content is a real HLS playlist, not a channel list.

    A handful of lines is enough to decide: ``#EXT-X-`` tags only ever appear
    in genuine HLS.
    """
    for line in text_head.splitlines()[:20]:
        if line.strip().startswith(HLS_TAG_PREFIX):
            return True
    return False


def fetch_lines(url: str, *, user_agent: str | None = None,
                referrer: str | None = None, headers: dict[str, str] | None = None,
                on_progress=None, on_phase=None, cancel=None,
                timeout: tuple[int, int] = (CONNECT_TIMEOUT, READ_TIMEOUT)) -> Iterator[str]:
    """Fetch an M3U and yield its lines without ever holding it in memory.

    Spools to disk first for the same reason the Xtream client does: it gives
    an honest byte denominator for progress, and keeps a 100k-line playlist
    from costing the size of the file in RAM.
    """
    request_headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    if referrer:
        request_headers["Referer"] = referrer
    if headers:
        request_headers.update(headers)

    if on_phase:
        on_phase("download")

    try:
        response = requests.get(
            url, headers=request_headers, stream=True, timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise M3UError(f"Could not fetch the playlist: {exc}") from exc

    try:
        if response.status_code != 200:
            raise M3UError(
                f"The playlist URL returned HTTP {response.status_code}."
            )

        path, encoding, written = spool_response(
            response, on_progress=on_progress, cancel=cancel,
            prefix="m3u-", suffix=".m3u",
        )
    finally:
        response.close()

    if written == 0:
        path.unlink(missing_ok=True)
        raise M3UError("The playlist URL returned an empty response.")

    try:
        if on_phase:
            on_phase("parse")
        yield from iter_text_lines(path, encoding, on_progress=on_progress, cancel=cancel)
    finally:
        path.unlink(missing_ok=True)


def decode(raw: bytes) -> str:
    """Decode playlist bytes, tolerating the encodings seen in the wild."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Never fail a sync over a few bad bytes.
    return raw.decode("utf-8", errors="replace")


__all__ = [
    "BOM",
    "HLS_TAG_PREFIX",
    "Channel",
    "decode",
    "looks_like_hls",
    "parse_lines",
    "parse_text",
    "split_extinf",
]
