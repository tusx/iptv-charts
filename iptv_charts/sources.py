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


def normalise_playlist_url(value: str, *, label: str = "Playlist URL") -> str:
    """Require an http(s) URL and add http:// when the scheme was omitted."""
    from urllib.parse import urlsplit

    text = str(value or "").strip()
    if not text:
        raise ProbeError(f"{label} is required.")
    if "://" not in text:
        text = "http://" + text
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ProbeError(f"{label} must use http:// or https://.")
    if not parsed.hostname:
        raise ProbeError(f"{label} does not contain a server host.")
    return text


def _optional_header(value: str | None, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > 4096 or any(char in text for char in "\r\n\x00"):
        raise ProbeError(f"{label} contains characters that are not allowed in a header.")
    return text


def probe_xtream(base_url: str, username: str, password: str, *,
                 user_agent: str | None = None, referrer: str | None = None,
                 headers: dict | None = None) -> dict:
    """Sign in and report what the account looks like."""
    try:
        client = xtream.XtreamClient(
            base_url, username, password,
            user_agent=user_agent,
            referrer=referrer,
            headers=headers,
        )
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
    url = normalise_playlist_url(url, label="Playlist URL")

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
          referrer: str = "", headers: dict | None = None) -> dict:
    if kind == "xtream":
        result = probe_xtream(
            base_url, username, password,
            user_agent=user_agent or None,
            referrer=referrer or None,
            headers=headers if isinstance(headers, dict) else None,
        )
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
           user_agent: str = "", referrer: str = "",
           connection_limit: int | None = None) -> int:
    """Insert a source and return its id."""
    if kind not in ("m3u", "xtream"):
        raise ProbeError(f"Unknown playlist type {kind!r}.")
    if connection_limit is not None:
        try:
            connection_limit = int(connection_limit)
        except (TypeError, ValueError):
            raise ProbeError("The connection limit must be a whole number.")
        if connection_limit < 1:
            raise ProbeError("The connection limit must be at least 1.")
        connection_limit = min(connection_limit, 32)

    headers = {}
    if user_agent:
        headers["user_agent"] = _optional_header(user_agent, "User-Agent")
    if referrer:
        headers["referrer"] = _optional_header(referrer, "Referrer")

    normalised_base = None
    play_url = None

    if kind == "xtream":
        try:
            normalised_base = xtream.normalise_base_url(base_url)
        except xtream.XtreamError as exc:
            raise ProbeError(str(exc)) from exc
        if not username.strip() or not password.strip():
            raise ProbeError("Username and password are required.")
    else:
        play_url = normalise_playlist_url(m3u_url)

    cursor = conn.execute(
        """
        INSERT INTO sources (name, kind, created_at, base_url, username, password,
                             m3u_url, request_headers, connection_limit, sync_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'never')
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
            connection_limit,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update(conn: sqlite3.Connection, source_id: int, *,
           name: str | None = None,
           m3u_url: str | None = None,
           base_url: str | None = None,
           username: str | None = None,
           password: str | None = None,
           user_agent: str | None = None,
           referrer: str | None = None,
           connection_limit: int | None | object = ...) -> tuple[dict, bool]:
    """Update a playlist in place.

    The playlist kind is immutable: M3U and Xtream have different identity
    models, so changing between them is a remove-and-add operation rather than
    an edit. Everything that identifies the provider account can change.

    Blank ``password`` means "keep the stored password". The edit page never
    renders the password, so sending an empty field must not wipe it.

    Returns ``(row, catalog_changed)``. When identity/credentials changed, the
    caller must queue a sync; existing catalog rows are left in place until
    that sync finishes, because the sync replaces them transactionally.
    """
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise ProbeError("That playlist no longer exists.")
    kind = row["kind"]
    fields: list[str] = []
    params: list = []
    catalog_changed = False

    if name is not None:
        cleaned = name.strip()[:120] or "Untitled playlist"
        if cleaned != row["name"]:
            fields.append("name = ?")
            params.append(cleaned)

    if kind == "xtream":
        new_base = row["base_url"]
        new_username = row["username"] or ""
        if base_url is not None:
            try:
                normalised = xtream.normalise_base_url(base_url)
            except xtream.XtreamError as exc:
                raise ProbeError(str(exc)) from exc
            if normalised != row["base_url"]:
                fields.append("base_url = ?")
                params.append(normalised)
                new_base = normalised
                catalog_changed = True
        if username is not None:
            cleaned = username.strip()
            if not cleaned:
                raise ProbeError("Username is required.")
            if cleaned != (row["username"] or ""):
                fields.append("username = ?")
                params.append(cleaned)
                new_username = cleaned
                catalog_changed = True

        new_password = password.strip() if password is not None else ""
        if new_password and new_password != (row["password"] or ""):
            fields.append("password = ?")
            params.append(new_password)
            catalog_changed = True
        if (
            new_base != row["base_url"]
            or new_username != (row["username"] or "")
        ) and not new_password:
            raise ProbeError(
                "Enter the password again when changing the server URL or username."
            )
        if not new_username or not (new_password or row["password"]):
            raise ProbeError("Username and password are required.")
    else:
        if m3u_url is not None:
            play_url = normalise_playlist_url(m3u_url)
            if play_url != (row["m3u_url"] or ""):
                fields.append("m3u_url = ?")
                params.append(play_url)
                catalog_changed = True

    headers = request_headers(row)
    headers_changed = False
    if user_agent is not None:
        cleaned = _optional_header(user_agent, "User-Agent")
        if cleaned != (headers.get("user_agent") or ""):
            headers_changed = True
        if cleaned:
            headers["user_agent"] = cleaned
        else:
            headers.pop("user_agent", None)
    if referrer is not None:
        cleaned = _optional_header(referrer, "Referrer")
        if cleaned != (headers.get("referrer") or ""):
            headers_changed = True
        if cleaned:
            headers["referrer"] = cleaned
        else:
            headers.pop("referrer", None)
    if headers_changed:
        fields.append("request_headers = ?")
        params.append(json.dumps(headers) if headers else None)

    if connection_limit is not ...:
        if connection_limit is None:
            fields.append("connection_limit = NULL")
        else:
            try:
                limit = int(connection_limit)
            except (TypeError, ValueError):
                raise ProbeError("The connection limit must be a whole number.")
            if limit < 1:
                raise ProbeError("The connection limit must be at least 1.")
            fields.append("connection_limit = ?")
            params.append(min(limit, 32))

    if catalog_changed:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM downloads WHERE source_id = ? "
            "AND status IN ('downloading', 'importing')",
            (int(source_id),),
        ).fetchone()["n"]
        if active:
            raise ProbeError(
                "A download from this playlist is running. Cancel it before "
                "changing the server, username or password."
            )
        # Queued rows would resolve against the new account and fetch the
        # wrong stream, so retire them with a clear reason.
        conn.execute(
            "UPDATE downloads SET status = 'cancelled', finished_at = ?, "
            "error = 'Playlist credentials changed' "
            "WHERE source_id = ? AND status = 'queued'",
            (int(time.time()), int(source_id)),
        )

    if fields:
        params.append(int(source_id))
        conn.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id = ?", params)
        if catalog_changed:
            # The old catalog belongs to a different account/server. Stream
            # URLs are rebuilt from the current credentials at play time, so
            # keeping old remote ids under new credentials is worse than an
            # empty catalog until the sync finishes.
            conn.execute("DELETE FROM items WHERE source_id = ?", (int(source_id),))
            conn.execute("DELETE FROM categories WHERE source_id = ?", (int(source_id),))
            conn.execute(
                "DELETE FROM provider_series_cache WHERE source_id = ?", (int(source_id),)
            )
            conn.execute(
                "DELETE FROM stream_probes WHERE source_id = ?", (int(source_id),)
            )
            conn.execute(
                "UPDATE sources SET user_info_json = NULL, server_info_json = NULL, "
                "account_status = NULL, is_trial = NULL, exp_date = NULL, "
                "exp_unknown = 0, active_connections = NULL, max_connections = NULL, "
                "allowed_formats = NULL, server_timezone = NULL, "
                "server_protocol = NULL, server_host = NULL, server_port = NULL, "
                "last_synced_at = NULL, last_sync_seconds = NULL, "
                "sync_status = 'never', "
                "sync_message = 'Playlist details changed; sync queued.' "
                "WHERE id = ?",
                (int(source_id),),
            )
        conn.commit()
    current = dict(conn.execute(
        "SELECT * FROM sources WHERE id = ?", (source_id,)
    ).fetchone())
    return current, catalog_changed


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
    "update",
]
