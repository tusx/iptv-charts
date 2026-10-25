"""Inspect an IPTV stream with ffprobe before downloading it.

The provider's release name is a hint, not evidence. ffprobe can read the
first few megabytes of an HTTP/HLS stream and report the real resolution,
video codec/HDR, every audio track with its language, and every embedded
subtitle track. The downloader only starts when those facts satisfy the
quality profile.

Probing counts against the playlist's connection limit, so every probe takes
a slot through ``connections`` and results are cached per provider identity.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from typing import Any, Mapping

from . import connections, db, languages, quality, streams

logger = logging.getLogger("iptv_charts.probe")

_URL_IN_TEXT_RE = re.compile(r"https?://\S+")
_ERROR_CACHE_SECONDS = 300


class ProbeError(RuntimeError):
    """ffprobe could not inspect the stream."""


def available() -> bool:
    return shutil.which("ffprobe") is not None


def _redact(text: str | None, limit: int = 500) -> str:
    value = _URL_IN_TEXT_RE.sub("<url>", str(text or ""))
    return " ".join(value.split())[:limit]


def _setting_float(conn, key: str, default: float) -> float:
    try:
        return float(db.get_setting(conn, key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def cache_hours(conn) -> float:
    return max(0.0, min(24 * 30, _setting_float(conn, "probe_cache_hours", 24)))


def timeout_seconds(conn) -> float:
    return max(5.0, min(120.0, _setting_float(conn, "probe_timeout_seconds", 30)))


def max_candidates(conn) -> int:
    try:
        return max(1, min(20, int(db.get_setting(conn, "probe_max_candidates", "6") or 6)))
    except (TypeError, ValueError):
        return 6


def search_budget(conn) -> int:
    try:
        return max(1, min(200, int(db.get_setting(conn, "probe_search_budget", "60") or 60)))
    except (TypeError, ValueError):
        return 60


def _tags(stream: Mapping[str, Any]) -> dict[str, str]:
    raw = stream.get("tags") or {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(key).lower(): str(value) for key, value in raw.items() if value is not None}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique_codecs(tracks: Any) -> list[str]:
    if not isinstance(tracks, list):
        return []
    return list(dict.fromkeys(
        str(track["codec"])
        for track in tracks
        if isinstance(track, Mapping) and track.get("codec")
    ))


def _codec_facts(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    """Aggregate codecs for a payload, including older cached probes."""
    return {
        "video_codecs": _unique_codecs(payload.get("video")),
        "audio_codecs": _unique_codecs(payload.get("audio")),
        "subtitle_codecs": _unique_codecs(payload.get("subtitles")),
    }


def enrich(payload: Mapping[str, Any]) -> dict:
    """Return a probe payload with aggregate codec/track fields filled in.

    Older cached payloads only contain the per-track arrays. Normalising on
    read keeps the candidate cards and the download decision consistent
    without rewriting the cache.
    """
    result = dict(payload)
    if not result.get("ok"):
        return result
    for key, value in _codec_facts(result).items():
        if value and not result.get(key):
            result[key] = value
    audio = result.get("audio")
    subtitles = result.get("subtitles")
    videos = result.get("video")
    if isinstance(audio, list) and not result.get("audio_track_count"):
        result["audio_track_count"] = len(audio)
    if isinstance(subtitles, list) and not result.get("subtitle_track_count"):
        result["subtitle_track_count"] = len(subtitles)
    if isinstance(audio, list) and not result.get("audio_languages"):
        result["audio_languages"] = list(dict.fromkeys(
            track.get("language") for track in audio
            if isinstance(track, Mapping) and track.get("language")
        ))
    if isinstance(subtitles, list) and not result.get("subtitle_languages"):
        result["subtitle_languages"] = list(dict.fromkeys(
            track.get("language") for track in subtitles
            if isinstance(track, Mapping) and track.get("language")
        ))
    if isinstance(videos, list) and not result.get("video_codec"):
        result["video_codec"] = next(
            (track.get("codec") for track in videos
             if isinstance(track, Mapping) and track.get("codec")),
            None,
        )
    return result


def normalise(payload: Mapping[str, Any]) -> dict:
    """Convert ffprobe ``-show_streams -show_format`` JSON into app facts."""
    raw_streams = payload.get("streams") if isinstance(payload, Mapping) else None
    if not isinstance(raw_streams, list):
        raise ProbeError("ffprobe returned no stream list.")

    videos: list[dict] = []
    audio: list[dict] = []
    subtitles: list[dict] = []
    for stream in raw_streams:
        if not isinstance(stream, Mapping):
            continue
        codec_type = str(stream.get("codec_type") or "").lower()
        disposition = stream.get("disposition") or {}
        if not isinstance(disposition, Mapping):
            disposition = {}
        tags = _tags(stream)
        if codec_type == "video" and not disposition.get("attached_pic"):
            width = _int(stream.get("width"))
            height = _int(stream.get("height"))
            video = {
                "index": _int(stream.get("index")),
                "codec": stream.get("codec_name") or stream.get("codec_long_name"),
                "profile": stream.get("profile"),
                "width": width,
                "height": height,
                "pix_fmt": stream.get("pix_fmt"),
                "bit_rate": _int(stream.get("bit_rate")),
                "frame_rate": stream.get("avg_frame_rate"),
                "color_transfer": stream.get("color_transfer"),
                "color_primaries": stream.get("color_primaries"),
            }
            side_data = stream.get("side_data_list") or []
            hdr = "SDR"
            if any(
                "dovi" in str(entry.get("side_data_type", "")).lower()
                or "dolby vision" in str(entry.get("side_data_type", "")).lower()
                for entry in side_data if isinstance(entry, Mapping)
            ):
                hdr = "Dolby Vision"
            elif str(stream.get("color_transfer") or "").lower() == "smpte2084":
                hdr = "HDR10"
            elif str(stream.get("color_transfer") or "").lower() == "arib-std-b67":
                hdr = "HLG"
            video["hdr"] = hdr
            videos.append(video)
        elif codec_type == "audio":
            code = languages.normalise(tags.get("language"))
            audio.append({
                "index": _int(stream.get("index")),
                "codec": stream.get("codec_name") or stream.get("codec_long_name"),
                "channels": _int(stream.get("channels")),
                "channel_layout": stream.get("channel_layout"),
                "language": code,
                "language_raw": tags.get("language") or None,
                "title": tags.get("title") or None,
                "default": bool(disposition.get("default")),
            })
        elif codec_type == "subtitle":
            code = languages.normalise(tags.get("language"))
            subtitles.append({
                "index": _int(stream.get("index")),
                "codec": stream.get("codec_name") or stream.get("codec_long_name"),
                "language": code,
                "language_raw": tags.get("language") or None,
                "title": tags.get("title") or None,
                "default": bool(disposition.get("default")),
                "forced": bool(disposition.get("forced")),
            })

    if not videos:
        raise ProbeError("The stream has no video track.")

    primary = max(
        videos,
        key=lambda entry: (
            int(entry.get("width") or 0) * int(entry.get("height") or 0),
            int(entry.get("bit_rate") or 0),
        ),
    )
    resolution = quality.resolution_from_dimensions(
        primary.get("width"), primary.get("height")
    ) or "unknown"

    fmt = payload.get("format") if isinstance(payload, Mapping) else None
    if not isinstance(fmt, Mapping):
        fmt = {}
    size = _int(fmt.get("size"))
    container = str(fmt.get("format_name") or "")
    if "hls" in container.lower() or "applehttp" in container.lower():
        # ffprobe reports the size of the playlist for HLS, not the media.
        size = None
    duration = _float(fmt.get("duration"))
    audio_languages = list(dict.fromkeys(
        track["language"] for track in audio if track.get("language")
    ))
    subtitle_languages = list(dict.fromkeys(
        track["language"] for track in subtitles if track.get("language")
    ))

    return {
        "ok": True,
        "resolution": resolution,
        "width": primary.get("width"),
        "height": primary.get("height"),
        "video_codec": primary.get("codec"),
        "video_codecs": _unique_codecs(videos),
        "hdr": primary.get("hdr") or "SDR",
        "video": videos,
        "audio": audio,
        "audio_codecs": _unique_codecs(audio),
        "subtitles": subtitles,
        "subtitle_codecs": _unique_codecs(subtitles),
        "audio_languages": audio_languages,
        "subtitle_languages": subtitle_languages,
        "audio_track_count": len(audio),
        "subtitle_track_count": len(subtitles),
        "size": size,
        "duration": duration,
        "bit_rate": _int(fmt.get("bit_rate")),
        "container": container or None,
        "probed_at": int(time.time()),
    }


def _run_ffprobe(ref: streams.StreamRef, timeout: float) -> dict:
    if not available():
        raise ProbeError(
            "ffprobe is not installed, so the stream cannot be inspected before download."
        )
    timeout_us = max(5_000_000, int(timeout * 1_000_000))
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams",
        "-protocol_whitelist", "http,https,tcp,tls,crypto",
        "-rw_timeout", str(timeout_us),
        "-analyzeduration", "5000000", "-probesize", "5000000",
    ]
    cmd.extend(streams.ffmpeg_header_args(ref.headers))
    cmd.extend(["-i", ref.url])

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise ProbeError(f"Could not start ffprobe: {exc}") from exc

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        process.communicate()
        raise ProbeError(f"ffprobe timed out after {timeout:.0f}s.")

    if process.returncode != 0:
        detail = ""
        for line in reversed((stderr or "").splitlines()):
            if line.strip():
                detail = _redact(line, 300)
                break
        raise ProbeError(
            "ffprobe could not read the stream"
            + (f": {detail}" if detail else ".")
        )
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError) as exc:
        raise ProbeError("ffprobe returned a response that was not JSON.") from exc
    if not isinstance(payload, dict):
        raise ProbeError("ffprobe returned an unexpected response.")
    return payload


def cached(conn, ref: streams.StreamRef, *, refresh: bool = False) -> dict | None:
    row = db.get_one(
        conn,
        "SELECT payload, ok, probed_at FROM stream_probes "
        "WHERE source_id = ? AND provider_kind = ? AND provider_remote_id = ? "
        "AND episode_stream_id = ?",
        ref.probe_key,
    )
    if row is None:
        return None
    age = int(time.time()) - int(row["probed_at"])
    ttl = int(cache_hours(conn) * 3600)
    if not row["ok"]:
        ttl = min(ttl, _ERROR_CACHE_SECONDS)
    if refresh or age > ttl:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["cached"] = True
    return payload


def _store(conn, ref: streams.StreamRef, payload: Mapping[str, Any]) -> None:
    conn.execute(
        "INSERT INTO stream_probes "
        "(source_id, provider_kind, provider_remote_id, episode_stream_id, "
        "ok, payload, probed_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, provider_kind, provider_remote_id, episode_stream_id) "
        "DO UPDATE SET ok = excluded.ok, payload = excluded.payload, "
        "probed_at = excluded.probed_at",
        (
            ref.source_id, ref.provider_kind, ref.provider_remote_id,
            ref.episode_stream_id, 1 if payload.get("ok") else 0,
            json.dumps(payload, ensure_ascii=False), int(time.time()),
        ),
    )
    conn.commit()


def inspect(conn, ref: streams.StreamRef, *, refresh: bool = False,
            acquire_timeout: float = 5.0, purpose: str = "Stream probe") -> dict:
    """Probe one stream, using the cache unless ``refresh`` is set.

    Returns a normalised dict. A connection-limit timeout is returned as an
    error payload (``busy: True``) rather than raised, so automatic search can
    skip a candidate and try again later without failing the whole run.
    """
    if not refresh:
        hit = cached(conn, ref)
        if hit is not None:
            return hit

    if not available():
        payload = {
            "ok": False,
            "error": "ffprobe is not installed; install it (the container image includes it).",
            "probed_at": int(time.time()),
        }
        _store(conn, ref, payload)
        return payload

    timeout = timeout_seconds(conn)
    try:
        with connections.acquire(
            conn, ref.source_id, purpose=purpose, timeout=acquire_timeout
        ):
            raw = _run_ffprobe(ref, timeout)
            payload = normalise(raw)
    except connections.ConnectionBusy as exc:
        return {
            "ok": False,
            "busy": True,
            "error": str(exc),
            "probed_at": int(time.time()),
        }
    except ProbeError as exc:
        payload = {
            "ok": False,
            "error": _redact(str(exc)),
            "probed_at": int(time.time()),
        }
    except Exception as exc:  # noqa: BLE001 - probe failure must not break search
        logger.warning("probe: unexpected failure: %s", exc)
        payload = {
            "ok": False,
            "error": _redact(f"{type(exc).__name__}: {exc}"),
            "probed_at": int(time.time()),
        }
    _store(conn, ref, payload)
    return payload


def apply(release: dict, payload: Mapping[str, Any]) -> dict:
    """Attach a probe payload to a release and prefer the probed facts."""
    payload = enrich(payload)
    release["probe"] = dict(payload)
    release["probe_ok"] = bool(payload.get("ok"))
    if payload.get("ok"):
        if payload.get("resolution") and payload["resolution"] != "unknown":
            release["resolution"] = payload["resolution"]
        release["audio_languages"] = payload.get("audio_languages") or []
        release["subtitle_languages"] = payload.get("subtitle_languages") or []
        # HLS probes deliberately have no size (ffprobe would report only the
        # playlist). Keep the catalog's size in that case instead of clearing
        # a useful file-size hint.
        if payload.get("size") is not None:
            release["size"] = payload["size"]
        release["video_codec"] = payload.get("video_codec")
        release["hdr"] = payload.get("hdr")
        release["width"] = payload.get("width")
        release["height"] = payload.get("height")
        release["duration"] = payload.get("duration")
        release["bit_rate"] = payload.get("bit_rate")
        release["container"] = payload.get("container")
        release["audio_track_count"] = payload.get("audio_track_count")
        release["subtitle_track_count"] = payload.get("subtitle_track_count")
        for key, value in _codec_facts(payload).items():
            if value:
                release[key] = value
    return release


def probe_movie_release(conn, release: Mapping[str, Any], *, refresh: bool = False,
                        acquire_timeout: float = 5.0) -> dict:
    result = dict(release)
    try:
        ref = streams.movie_stream(
            conn, int(release["source_id"]), str(release["remote_id"])
        )
    except streams.StreamError as exc:
        return apply(result, {"ok": False, "error": str(exc)})
    return apply(result, inspect(
        conn, ref, refresh=refresh, acquire_timeout=acquire_timeout,
        purpose="Movie probe",
    ))


def probe_episode_release(conn, release: Mapping[str, Any], *, refresh: bool = False,
                          acquire_timeout: float = 5.0) -> dict:
    result = dict(release)
    provider_episode = release.get("provider_episode") or {}
    try:
        ref = streams.episode_stream(
            conn, int(release["source_id"]), str(release["remote_id"]),
            str(provider_episode["stream_id"]),
            provider_episode.get("container_ext"),
        )
    except (streams.StreamError, KeyError) as exc:
        return apply(result, {"ok": False, "error": str(exc)})
    return apply(result, inspect(
        conn, ref, refresh=refresh, acquire_timeout=acquire_timeout,
        purpose="Episode probe",
    ))


def clear_cache(conn, source_id: int | None = None) -> int:
    if source_id is None:
        cursor = conn.execute("DELETE FROM stream_probes")
    else:
        cursor = conn.execute(
            "DELETE FROM stream_probes WHERE source_id = ?", (int(source_id),)
        )
    conn.commit()
    return cursor.rowcount


def cache_snapshot(conn) -> list[dict]:
    return [
        dict(row) for row in conn.execute(
            "SELECT source_id, provider_kind, provider_remote_id, "
            "episode_stream_id, ok, probed_at FROM stream_probes "
            "ORDER BY probed_at DESC"
        )
    ]


__all__ = [
    "ProbeError",
    "apply",
    "available",
    "cache_hours",
    "cache_snapshot",
    "cached",
    "clear_cache",
    "enrich",
    "inspect",
    "max_candidates",
    "normalise",
    "probe_episode_release",
    "probe_movie_release",
    "search_budget",
    "timeout_seconds",
]
