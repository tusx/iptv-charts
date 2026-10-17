"""Creating, probing and deleting playlist sources.

The probe is deliberately separate from the sync. Adding a playlist is a
commitment -- a full sync of a large catalog is minutes of downloading -- so
the UI tests the credentials first and shows what it found before anything is
written. It is also the cheapest place to catch a mistyped URL.
"""

from __future__ import annotations

import json
import sqlite3
import time
import re

import requests

from .clients import m3u, xtream

#: How much of an M3U to read when probing. Enough to confirm the header and
#: estimate the size, without downloading a 100k-line playlist just to say
#: "yes, that URL works".
PROBE_SAMPLE_BYTES = 65536

PROBE_CONNECT_TIMEOUT = 12
PROBE_READ_TIMEOUT = 25


class ProbeError(RuntimeError):
    """Raised when credentials or a URL cannot be used."""


def probe_xtream(base_url: str, username: str, password: str) -> dict:
    """Sign in and report what the account looks like."""
    try:
        client = xtream.XtreamClient(base_url, username, password)
    except xtream.XtreamError as exc:
        raise ProbeError(str(exc)) from exc

    try:
        info = client.account_info()
    except xtream.XtreamAuthError as exc:
        raise ProbeError(
            f"{exc} Check the username, password and server address."
        ) from exc
    except xtream.XtreamError as exc:
        raise ProbeError(str(exc)) from exc

    try:
        category_counts = {}
        for kind in ("live", "vod", "series"):
            try:
                category_counts[kind] = len(client.categories(kind))
            except xtream.XtreamError:
                category_counts[kind] = None
    finally:
        client.close()

    return {
        "kind": "xtream",
        "base_url": client.base_url,
        "account": {
            "status": info.status,
            "authenticated": info.authenticated,
            "is_trial": info.is_trial,
            "exp_date": info.exp_date,
            "exp_unknown": info.exp_unknown,
            "days_remaining": info.days_remaining,
            "expired": info.expired,
            "active_connections": info.active_connections,
            "max_connections": info.max_connections,
            "allowed_formats": info.allowed_formats,
        },
        "server": {
            "host": info.server_host,
            "port": info.server_port,
            "https_port": info.https_port,
            "protocol": info.server_protocol,
            "timezone": info.timezone,
            "time": info.server_time,
        },
        "category_counts": category_counts,
        "warnings": _account_warnings(info),
    }


def _account_warnings(info: xtream.AccountInfo) -> list[str]:
    warnings: list[str] = []
    if info.is_trial:
        warnings.append("This is a trial account.")
    if info.expired:
        warnings.append("This account has expired.")
    elif info.days_remaining is not None and info.days_remaining <= 7:
        warnings.append(f"Expires in {info.days_remaining} day(s).")
    if info.max_connections == 1:
        warnings.append(
            "The account allows one connection — syncing while watching may "
            "fail."
        )
    return warnings


def probe_m3u(url: str, user_agent: str | None = None,
              referrer: str | None = None) -> dict:
    """Fetch the head of a playlist and report what it appears to be."""
    url = (url or "").strip()
    if not url:
        raise ProbeError("Enter a playlist URL.")
    if "://" not in url:
        url = "http://" + url

    # Ask for the bytes as they are. With gzip in play, `iter_content` hands
    # back decompressed data while Content-Length counts compressed bytes, so
    # comparing the two to decide whether the sample covered the whole file
    # gives exactly the wrong answer -- the same trap the sync's progress
    # accounting exists to avoid.
    headers = {
        "User-Agent": user_agent or m3u.DEFAULT_USER_AGENT,
        "Accept-Encoding": "identity",
    }
    if referrer:
        headers["Referer"] = referrer

    try:
        response = requests.get(
            url, headers=headers, stream=True,
            timeout=(PROBE_CONNECT_TIMEOUT, PROBE_READ_TIMEOUT),
        )
    except requests.RequestException as exc:
        raise ProbeError(f"Could not reach that URL: {exc}") from exc

    try:
        if response.status_code != 200:
            raise ProbeError(f"The URL returned HTTP {response.status_code}.")

        sample = b""
        for piece in response.iter_content(8192):
            sample += piece
            if len(sample) >= PROBE_SAMPLE_BYTES:
                break

        total = response.headers.get("Content-Length")
        content_type = response.headers.get("Content-Type", "")

        # If the server compressed anyway, the length is not comparable to the
        # decompressed sample we hold, so report no total rather than a wrong
        # one.
        if response.headers.get("Content-Encoding"):
            total = None
    finally:
        response.close()

    if not sample:
        raise ProbeError("That URL returned no content.")

    text = m3u.decode(sample)

    if not text.lstrip("﻿ \t\r\n").startswith("#EXTM3U"):
        snippet = text[:120].strip().replace("\n", " ")
        raise ProbeError(
            "That URL does not look like an M3U playlist — it does not start "
            f"with #EXTM3U. It begins with: {snippet!r}"
        )

    if m3u.looks_like_hls(text):
        raise ProbeError(
            "That is an HLS media playlist for a single stream, not a channel "
            "list. Use the provider's playlist URL instead."
        )

    channels = sum(1 for line in text.splitlines() if line.startswith("#EXTINF:"))
    groups: set[str] = set()
    epg_url = None

    for line in text.splitlines():
        if line.startswith("#EXTM3U") and epg_url is None:
            match = re.search(r'(?:url-tvg|x-tvg-url|tvg-url)="([^"]+)"', line)
            if match:
                epg_url = match.group(1)
        if "group-title=" in line:
            match = re.search(r'group-title="([^"]*)"', line)
            if match and match.group(1):
                groups.add(match.group(1).split(";")[0])

    truncated = total is None or int(total) > len(sample)

    return {
        "kind": "m3u",
        "url": url,
        "sampled_bytes": len(sample),
        "total_bytes": int(total) if total and total.isdigit() else None,
        "content_type": content_type or None,
        "channels_sampled": channels,
        # A real estimate, clearly labelled as one: extrapolating from the
        # sample is honest for a file but explicitly not for a chunked
        # response with no length.
        "channels_estimate": (
            int(channels * int(total) / len(sample))
            if total and total.isdigit() and len(sample) and int(total) > len(sample)
            else None
        ),
        "sample_covers_whole_file": not truncated,
        "groups_sampled": sorted(groups)[:25],
        "group_count_sampled": len(groups),
        "epg_url": epg_url,
        "warnings": [],
    }


def probe(kind: str, *, name: str = "", m3u_url: str = "", base_url: str = "",
          username: str = "", password: str = "", user_agent: str = "",
          referrer: str = "") -> dict:
    if kind == "xtream":
        result = probe_xtream(base_url, username, password)
    elif kind == "m3u":
        result = probe_m3u(m3u_url, user_agent or None, referrer or None)
    else:
        raise ProbeError(f"Unknown playlist type {kind!r}.")
    result["name"] = name.strip() or _default_name(result)
    return result


def _default_name(result: dict) -> str:
    from urllib.parse import urlparse

    raw = result.get("base_url") or result.get("url") or ""
    host = urlparse(raw).hostname or "playlist"
    return host


def create(conn: sqlite3.Connection, kind: str, name: str, *, m3u_url: str = "",
           base_url: str = "", username: str = "", password: str = "",
           user_agent: str = "", referrer: str = "") -> int:
    """Insert a source and return its id."""
    if kind not in ("m3u", "xtream"):
        raise ProbeError(f"Unknown playlist type {kind!r}.")

    headers = {}
    if user_agent:
        headers["user_agent"] = user_agent
    if referrer:
        headers["referrer"] = referrer

    normalised_base = None
    play_url = None

    if kind == "xtream":
        normalised_base = xtream.normalise_base_url(base_url)
        if not username.strip() or not password.strip():
            raise ProbeError("Username and password are required.")
    else:
        play_url = (m3u_url or "").strip()
        if not play_url:
            raise ProbeError("A playlist URL is required.")
        if "://" not in play_url:
            play_url = "http://" + play_url

    cursor = conn.execute(
        """
        INSERT INTO sources (name, kind, created_at, base_url, username, password,
                             m3u_url, request_headers, sync_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'never')
        """,
        (
            name.strip() or "Untitled playlist",
            kind,
            int(time.time()),
            normalised_base,
            username.strip() or None,
            password.strip() or None,
            play_url,
            json.dumps(headers) if headers else None,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def delete(conn: sqlite3.Connection, source_id: int) -> bool:
    """Remove a source; items and categories cascade away with it."""
    cursor = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()
    return cursor.rowcount > 0


def request_headers(source_row) -> dict:
    try:
        return json.loads(source_row["request_headers"] or "{}")
    except (ValueError, TypeError):
        return {}


__all__ = [
    "ProbeError",
    "create",
    "delete",
    "probe",
    "probe_m3u",
    "probe_xtream",
    "request_headers",
]
