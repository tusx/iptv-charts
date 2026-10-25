"""Weighted quality profiles, stream facts and upgrade decisions.

A 0.8.0 profile is a single mandatory resolution plus a weighted set of
quality/source options. Two things are always mandatory:

* the candidate resolution must equal the selected resolution, or be a
  *lower* resolution when ``upgrades`` is enabled;
* the candidate source/quality must match one of the selected quality options.

Those two mandatory matches form the base score (resolution weight + best
matching source weight). Every other criterion is optional and only adds
weight when it matches:

* video codec, audio codec and audio channels are exclusive sections -- only
  the best matching selected option in each section adds its weight;
* audio languages, subtitle formats and subtitle languages are multi-valued --
  every unique matching selected option adds its weight.

An unmatched optional option contributes zero; it never rejects a candidate.
A section with no selected options contributes zero. The stored ``max_score``
is the sum of the mandatory maximum and every optional section's maximum.

``target_score`` is the score needed for a candidate to count as complete, but
only when it also used the exact selected resolution. A lower-resolution
fallback can be eligible, score highly and still not be ``target_met``; it is
downloaded as a stopgap and flagged ``needs_upgrade``.

Probe facts come from :func:`probe.normalise` payloads (the same shape stored
in ``movies.media_info`` and ``stream_probes.payload``), so scoring a current
file always re-derives its score from the stored media facts instead of
trusting a score written by an older profile revision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import languages

#: Resolutions a profile may require. ``unknown`` is a candidate fact, never a
#: profile value.
RESOLUTIONS = ("2160p", "1080p", "720p", "480p", "360p")
RANK = {name: index for index, name in enumerate(RESOLUTIONS)}
CANDIDATE_RESOLUTIONS = RESOLUTIONS + ("unknown",)

#: The full ordered quality/source vocabulary. The first eight are the default
#: selections of every builtin profile (and of a new custom profile).
QUALITY_OPTIONS = (
    ("remux", "REMUX"),
    ("bluray", "BluRay"),
    ("bdrip", "BDRip"),
    ("web-dl", "WEB-DL"),
    ("webrip", "WEBRip"),
    ("hdtv", "HDTV"),
    ("sd-tv", "SD-TV"),
    ("dvd", "DVD"),
    ("unknown", "Unknown"),
    ("workprint", "Workprint"),
    ("cam", "CAM"),
    ("telesync", "Telesync"),
    ("telecine", "Telecine"),
    ("screener", "Screener"),
    ("r5", "R5"),
)
QUALITY_LABELS = dict(QUALITY_OPTIONS)
QUALITY_CHOICES = QUALITY_OPTIONS
DEFAULT_QUALITY_VALUES = (
    "remux", "bluray", "bdrip", "web-dl", "webrip", "hdtv", "sd-tv", "dvd",
)
DEFAULT_QUALITY_SELECTION = tuple(
    {"value": value, "label": QUALITY_LABELS[value], "weight": 10}
    for value in DEFAULT_QUALITY_VALUES
)
LOW_QUALITY_SOURCES = frozenset(
    {"workprint", "cam", "telesync", "telecine", "screener", "r5", "unknown"}
)

#: Compatibility aliases for older call sites and templates.
SOURCES = tuple(value for value, _label in QUALITY_OPTIONS)
SOURCE_LABELS = dict(QUALITY_LABELS)
SOURCE_CHOICES = QUALITY_OPTIONS
AUDIO_MODES = ("any", "original", "single", "multi")
SUBTITLE_MODES = ("any", "required", "none")

#: Weight defaults for option sections, used when a client omits a weight.
VIDEO_CODEC_WEIGHT = 10
AUDIO_CODEC_WEIGHT = 10
AUDIO_LANGUAGE_WEIGHT = 5
AUDIO_CHANNEL_WEIGHT = 5
SUBTITLE_FORMAT_WEIGHT = 5
SUBTITLE_LANGUAGE_WEIGHT = 5

#: Optional sections, in builder display order. ``kind`` is ``exclusive``
#: (best matching option only) or ``multi`` (every matching option).
OPTIONAL_SECTIONS = (
    {"key": "video_codecs", "label": "Video codec", "kind": "exclusive",
     "default_weight": VIDEO_CODEC_WEIGHT},
    {"key": "audio_codecs", "label": "Audio codec", "kind": "exclusive",
     "default_weight": AUDIO_CODEC_WEIGHT},
    {"key": "audio_languages", "label": "Audio language", "kind": "multi",
     "default_weight": AUDIO_LANGUAGE_WEIGHT},
    {"key": "audio_channels", "label": "Audio channels", "kind": "exclusive",
     "default_weight": AUDIO_CHANNEL_WEIGHT},
    {"key": "subtitle_formats", "label": "Subtitle format", "kind": "multi",
     "default_weight": SUBTITLE_FORMAT_WEIGHT},
    {"key": "subtitle_languages", "label": "Subtitle language", "kind": "multi",
     "default_weight": SUBTITLE_LANGUAGE_WEIGHT},
)
SECTION_KEYS = tuple(section["key"] for section in OPTIONAL_SECTIONS)
SECTION_BY_KEY = {section["key"]: section for section in OPTIONAL_SECTIONS}

VIDEO_CODEC_OPTIONS = (
    ("h264", "H.264 / x264"),
    ("hevc", "H.265 / HEVC / x265"),
    ("av1", "AV1"),
    ("vp9", "VP9"),
    ("vc1", "VC-1"),
    ("mpeg2", "MPEG-2"),
)
AUDIO_CODEC_OPTIONS = (
    ("aac", "AAC"),
    ("ac3", "AC-3 / Dolby Digital"),
    ("eac3", "E-AC-3 / Dolby Digital Plus"),
    ("dts", "DTS"),
    ("dts-hd", "DTS-HD"),
    ("truehd", "TrueHD"),
    ("atmos", "Dolby Atmos"),
    ("opus", "Opus"),
    ("flac", "FLAC"),
    ("mp3", "MP3"),
    ("pcm", "PCM"),
)
AUDIO_CHANNEL_OPTIONS = (
    ("mono", "Mono"),
    ("stereo", "Stereo"),
    ("2.0", "2.0"),
    ("5.1", "5.1"),
    ("7.1", "7.1"),
)
SUBTITLE_FORMAT_OPTIONS = (
    ("srt", "SRT"),
    ("ass", "ASS / SSA"),
    ("pgs", "PGS"),
    ("vobsub", "VobSub"),
    ("webvtt", "WebVTT"),
    ("mov_text", "mov_text"),
)
AUDIO_LANGUAGE_OPTIONS = (
    (languages.ORIGINAL, "Original language"),
    ("any", "Any language"),
) + tuple(languages.COMMON_LANGUAGES)
SUBTITLE_LANGUAGE_OPTIONS = AUDIO_LANGUAGE_OPTIONS

_SECTION_VOCABULARIES = {
    "video_codecs": dict(VIDEO_CODEC_OPTIONS),
    "audio_codecs": dict(AUDIO_CODEC_OPTIONS),
    "audio_languages": dict(AUDIO_LANGUAGE_OPTIONS),
    "audio_channels": dict(AUDIO_CHANNEL_OPTIONS),
    "subtitle_formats": dict(SUBTITLE_FORMAT_OPTIONS),
    "subtitle_languages": dict(SUBTITLE_LANGUAGE_OPTIONS),
}

#: Sentinel used internally for "a language track exists" so the ``any``
#: option can match without inventing a real language code.
_ANY = "any"
_WEIGHT_MIN = 1
_WEIGHT_MAX = 1000
_TARGET_FLOOR = 20

#: Old 0.7.0 source names mapped onto the 0.8.0 vocabulary.
_SOURCE_ALIASES = {
    "remux": "remux",
    "bluray": "bluray", "blu-ray": "bluray",
    "bdrip": "bdrip", "brrip": "bdrip", "bd-rip": "bdrip", "br-rip": "bdrip",
    "web-dl": "web-dl", "webdl": "web-dl", "web_dl": "web-dl",
    "webrip": "webrip", "web-rip": "webrip",
    "hdtv": "hdtv", "hd-tv": "hdtv",
    "sd-tv": "sd-tv", "sdtv": "sd-tv", "sd_tv": "sd-tv",
    "dvd": "dvd", "dvdrip": "dvd", "dvd-rip": "dvd",
    "unknown": "unknown",
    "workprint": "workprint",
    "cam": "cam", "hdcam": "cam", "hd-cam": "cam",
    "telesync": "telesync", "ts": "telesync", "hdts": "telesync",
    "telecine": "telecine", "tc": "telecine",
    "screener": "screener", "scr": "screener",
    "r5": "r5",
}

#: Order matters: BDrip before BluRay, E-AC-3 before AC-3, and every pattern
#: uses word-ish boundaries so "Camelot" is not a CAM.
_SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("workprint", re.compile(r"(?i)(?<![a-z0-9])work[\s._-]?print(?![a-z0-9])")),
    ("remux", re.compile(r"(?i)(?<![a-z0-9])remux(?![a-z0-9])")),
    ("bdrip", re.compile(
        r"(?i)(?<![a-z0-9])(?:bd[\s._-]?rip|br[\s._-]?rip)(?![a-z0-9])"
    )),
    ("bluray", re.compile(
        r"(?i)(?<![a-z0-9])(?:blu[\s._-]?ray|bd[\s._-]?remux|bd25|bd50)(?![a-z0-9])"
    )),
    ("web-dl", re.compile(r"(?i)(?<![a-z0-9])web[\s._-]?dl(?![a-z0-9])")),
    ("webrip", re.compile(r"(?i)(?<![a-z0-9])web[\s._-]?rip(?![a-z0-9])")),
    ("hdtv", re.compile(r"(?i)(?<![a-z0-9])hd[\s._-]?tv(?![a-z0-9])")),
    ("sd-tv", re.compile(r"(?i)(?<![a-z0-9])sd[\s._-]?tv(?![a-z0-9])")),
    ("dvd", re.compile(r"(?i)(?<![a-z0-9])dvd(?:[\s._-]?rip)?(?![a-z0-9])")),
    ("cam", re.compile(r"(?i)(?<![a-z0-9])(?:hd[\s._-]?cam|cam(?:rip)?)(?![a-z0-9])")),
    ("telesync", re.compile(
        r"(?i)(?<![a-z0-9])(?:hd[\s._-]?ts|telesync|ts)(?![a-z0-9])"
    )),
    ("telecine", re.compile(r"(?i)(?<![a-z0-9])(?:telecine|tc)(?![a-z0-9])")),
    ("screener", re.compile(r"(?i)(?<![a-z0-9])(?:screener|scr)(?![a-z0-9])")),
    ("r5", re.compile(r"(?i)(?<![a-z0-9])r5(?![a-z0-9])")),
)

#: Order matters: 4K before 1080p, and word boundaries keep "4Kings" from
#: being treated as 4K. "sd" remains a 480p alias for release names.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("2160p", re.compile(r"(?i)(?<![\w])(?:2160p|4k|uhd)(?![\w])")),
    ("1080p", re.compile(r"(?i)(?<![\w])(?:1080p|fullhd|fhd)(?![\w])")),
    ("720p", re.compile(r"(?i)(?<![\w])720p(?![\w])")),
    ("480p", re.compile(r"(?i)(?<![\w])(?:480p|576p|540p|sd)(?![\w])")),
    ("360p", re.compile(r"(?i)(?<![\w])360p(?![\w])")),
)

#: A height in pixels from ffprobe mapped back onto profile resolutions.
_HEIGHT_TIERS = ((1800, "2160p"), (1000, "1080p"), (650, "720p"), (420, "480p"))


# ------------------------------------------------------------------ detection
def parse_resolution(text: str | None) -> str:
    """Return the resolution named in a release or filename, or ``unknown``."""
    for label, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return label
    return "unknown"


def parse_source(text: str | None) -> str:
    """Return the release source named in a provider name, or ``unknown``."""
    for label, pattern in _SOURCE_PATTERNS:
        if pattern.search(text or ""):
            return label
    return "unknown"


def normalise_quality_value(value: Any) -> str | None:
    """Fold a stored/legacy source value onto the 0.8.0 vocabulary."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in QUALITY_LABELS:
        return text
    return _SOURCE_ALIASES.get(text)


def normalise_source(value: Any, name: Any = None) -> str:
    """Normalise a release source, falling back to the release name."""
    source = normalise_quality_value(value)
    if source:
        return source
    if str(value or "").strip().lower() in {"", "unknown"} and name:
        return parse_source(str(name))
    return "unknown"


def source_label(source: str | None) -> str:
    return QUALITY_LABELS.get(str(source or "unknown").lower(), "Unknown")


def resolution_from_height(height: int | None) -> str | None:
    """Map an ffprobe frame height to a resolution; ``None`` when unusable."""
    if not height or height <= 0:
        return None
    for threshold, label in _HEIGHT_TIERS:
        if height >= threshold:
            return label
    return "360p"


def resolution_from_dimensions(width: int | None, height: int | None) -> str | None:
    """Classify a probe frame, using width for scope-aspect 4K films.

    A 3840x1600 letterboxed movie has a height below 1800 but is plainly 4K;
    looking only at the height would call it 1080p.
    """
    try:
        width = int(width or 0)
        height = int(height or 0)
    except (TypeError, ValueError):
        return None
    if width >= 3000 or height >= 1800:
        return "2160p"
    if width >= 1800 or height >= 1000:
        return "1080p"
    if width >= 1200 or height >= 650:
        return "720p"
    if width >= 700 or height >= 420:
        return "480p"
    if width or height:
        return "360p"
    return None


def normalise_resolution(value: Any) -> str | None:
    """Normalise a stored resolution, including ffprobe heights."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return resolution_from_height(int(value))
    text = str(value).strip().lower()
    if not text:
        return None
    if text in RESOLUTIONS:
        return text
    if text in {"4k", "uhd"}:
        return "2160p"
    if text == "unknown":
        return "unknown"
    if text.isdigit():
        return resolution_from_height(int(text))
    return parse_resolution(text)


def normalise_video_codec(value: Any) -> str | None:
    """Map an ffprobe/legacy video codec name onto the profile vocabulary."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if "h264" in compact or "avc" in compact:
        return "h264"
    if "hevc" in compact or "h265" in compact:
        return "hevc"
    if "av1" in compact:
        return "av1"
    if "vp9" in compact:
        return "vp9"
    if "vc1" in compact or "wmv3" in compact:
        return "vc1"
    if "mpeg2" in compact:
        return "mpeg2"
    return None


def normalise_audio_codec(value: Any, *, detail: Any = None) -> str | None:
    """Map an ffprobe/legacy audio codec onto the profile vocabulary.

    ``detail`` is checked for DTS-HD/Atmos markers that ffprobe reports in a
    profile or title rather than in ``codec_name``.
    """
    text = str(value or "").strip().lower()
    combined = f"{text} {str(detail or '').lower()}"
    compact = re.sub(r"[^a-z0-9]+", "", combined)
    if "atmos" in compact:
        return "atmos"
    if "eac3" in compact or "eac3" in text or "ddp" in compact:
        return "eac3"
    if "truehd" in compact:
        return "truehd"
    if "dtshd" in compact or "dtshd" in text:
        return "dts-hd"
    if "ac3" in compact:
        return "ac3"
    if "dts" in compact:
        return "dts"
    if "aac" in compact:
        return "aac"
    if "opus" in compact:
        return "opus"
    if "flac" in compact:
        return "flac"
    if "mp3" in compact or "mpeg audio" in combined:
        return "mp3"
    if "pcm" in compact:
        return "pcm"
    return None


def normalise_channels(channels: Any, layout: Any = None) -> frozenset[str]:
    """Map an ffprobe channel count/layout onto channel option values."""
    found: set[str] = set()
    try:
        count = int(channels)
    except (TypeError, ValueError):
        count = 0
    if count == 1:
        found.add("mono")
    elif count == 2:
        found.update({"stereo", "2.0"})
    elif count >= 8:
        found.add("7.1")
    elif count >= 6:
        found.add("5.1")
    text = str(layout or "").strip().lower()
    if "mono" in text:
        found.add("mono")
    if "stereo" in text:
        found.update({"stereo", "2.0"})
    for value in ("2.0", "5.1", "6.1", "7.1", "3.0", "4.0", "5.0"):
        if value in text:
            if value in {"2.0", "5.1", "7.1"}:
                found.add(value)
    return frozenset(found)


def normalise_subtitle_format(value: Any) -> str | None:
    """Map an ffprobe/legacy subtitle codec onto the profile vocabulary."""
    text = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if not compact:
        return None
    if "subrip" in compact or compact in {"srt", "subsrt"}:
        return "srt"
    if compact in {"ass", "ssa"} or "ass" == compact or "ssa" == compact:
        return "ass"
    if "pgs" in compact:
        return "pgs"
    if "vobsub" in compact or "dvdsub" in compact or "dvdsubtitle" in compact:
        return "vobsub"
    if "webvtt" in compact or "vtt" == compact:
        return "webvtt"
    if "movtext" in compact or "tx3g" in compact:
        return "mov_text"
    return None


def decode_resolutions(raw: Any) -> list[str]:
    """Decode a legacy resolution list, keeping known ordered tiers."""
    values = _load_json(raw)
    if not isinstance(values, (list, tuple)):
        return []
    seen = {str(value).strip().lower() for value in values}
    return [name for name in RESOLUTIONS if name in seen]


def decode_language_list(raw: Any) -> list[str]:
    values = _load_json(raw)
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value) for value in values if value is not None]


def _load_json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw or "null")
        except (TypeError, ValueError):
            return None
    return raw


# --------------------------------------------------------------- probe facts
def _probe_payload(release: Mapping[str, Any]) -> dict:
    """Return a usable probe/media-info payload for a candidate or file."""
    for key in ("probe", "media_info"):
        raw = release.get(key)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = None
        if isinstance(raw, Mapping) and raw:
            if raw.get("ok") is False:
                continue
            return dict(raw)
    return {}


def _track_list(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = payload.get(key) if isinstance(payload, Mapping) else None
    return [entry for entry in value or [] if isinstance(entry, Mapping)]


def _string_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part for part in raw.replace(";", ",").split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(entry) for entry in raw if entry is not None]
    return []


def release_facts(release: Any, original_language: Any = None) -> dict:
    """Normalise a candidate release or stored file into scoring facts.

    ``release`` accepts the shapes used by matching/probe (``resolution``,
    ``source``, ``probe``, ``name``) and a stored movie/episode row
    (``media_info``, ``source``, ``file_size``).
    """
    if isinstance(release, str):
        view: Mapping[str, Any] = {"resolution": release}
    elif isinstance(release, Mapping):
        view = release
    elif hasattr(release, "keys"):
        view = dict(release)  # sqlite3.Row
    else:
        view = {}

    payload = _probe_payload(view)
    usable = bool(payload)

    resolution = None
    if usable:
        resolution = normalise_resolution(payload.get("resolution"))
        if resolution in (None, "unknown"):
            resolution = resolution_from_dimensions(
                payload.get("width"), payload.get("height")
            )
    if resolution in (None, "unknown"):
        resolution = normalise_resolution(view.get("resolution"))
    if resolution in (None, "unknown") and view.get("name"):
        resolution = parse_resolution(str(view.get("name")))
    if resolution is None:
        resolution = "unknown"

    source = normalise_source(view.get("source"), view.get("name"))
    if source == "unknown" and payload.get("source"):
        source = normalise_source(payload.get("source"))

    video_codecs: set[str] = set()
    audio_codecs: set[str] = set()
    audio_languages: set[str] = set()
    audio_channels: set[str] = set()
    subtitle_formats: set[str] = set()
    subtitle_languages: set[str] = set()

    if view.get("video_codec"):
        value = normalise_video_codec(view.get("video_codec"))
        if value:
            video_codecs.add(value)

    for track in _track_list(payload, "video"):
        value = normalise_video_codec(track.get("codec"))
        if value:
            video_codecs.add(value)

    audio_tracks = _track_list(payload, "audio")
    for track in audio_tracks:
        detail = " ".join(
            str(track.get(key) or "")
            for key in ("title", "profile", "codec_long_name")
        )
        value = normalise_audio_codec(track.get("codec"))
        if value:
            audio_codecs.add(value)
        # Atmos is a layer on TrueHD or E-AC-3, so expose both the base codec
        # and the Atmos marker; a profile may select either. DTS-HD is reported
        # by ffprobe as codec_name "dts" plus a profile/title marker.
        detailed_codec = normalise_audio_codec(track.get("codec"), detail=detail)
        if detailed_codec == "atmos":
            audio_codecs.add("atmos")
        elif detailed_codec == "dts-hd":
            audio_codecs.add("dts-hd")
        audio_channels.update(normalise_channels(
            track.get("channels"), track.get("channel_layout")
        ))
        code = languages.normalise(track.get("language"))
        if code:
            audio_languages.add(code)
    for value in _string_values(view.get("audio_codecs")):
        codec = normalise_audio_codec(value)
        if codec:
            audio_codecs.add(codec)
    for value in _string_values(view.get("audio_channels")):
        audio_channels.update(normalise_channels(None, value))
    for code in languages.normalise_many(view.get("audio_languages")):
        audio_languages.add(code)
    for code in languages.normalise_many(payload.get("audio_languages")):
        audio_languages.add(code)

    subtitle_tracks = _track_list(payload, "subtitles")
    for track in subtitle_tracks:
        value = normalise_subtitle_format(track.get("codec"))
        if value:
            subtitle_formats.add(value)
        code = languages.normalise(track.get("language"))
        if code:
            subtitle_languages.add(code)
    for value in _string_values(view.get("subtitle_formats")):
        fmt = normalise_subtitle_format(value)
        if fmt:
            subtitle_formats.add(fmt)
    for code in languages.normalise_many(view.get("subtitle_languages")):
        subtitle_languages.add(code)
    for code in languages.normalise_many(payload.get("subtitle_languages")):
        subtitle_languages.add(code)

    has_audio = bool(audio_tracks or audio_languages or audio_codecs)
    has_subtitles = bool(
        subtitle_tracks or subtitle_languages or subtitle_formats
    )
    if has_audio:
        audio_languages.add(_ANY)
    if has_subtitles:
        subtitle_languages.add(_ANY)

    return {
        "resolution": resolution,
        "source": source,
        "original_language": languages.normalise(
            original_language if original_language is not None
            else view.get("original_language")
        ),
        "video_codecs": frozenset(video_codecs),
        "audio_codecs": frozenset(audio_codecs),
        "audio_languages": frozenset(audio_languages),
        "audio_channels": frozenset(audio_channels),
        "subtitle_formats": frozenset(subtitle_formats),
        "subtitle_languages": frozenset(subtitle_languages),
        "size": _release_size(view, payload),
    }


def _release_size(view: Mapping[str, Any], payload: Mapping[str, Any]) -> int:
    for source in (payload.get("size"), view.get("size"), view.get("file_size")):
        try:
            value = int(source)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


# -------------------------------------------------------------------- options
@dataclass(frozen=True)
class Option:
    """One selected weighted option in a profile section."""

    value: str
    label: str
    weight: int = 10

    def to_dict(self) -> dict:
        return {"value": self.value, "label": self.label, "weight": self.weight}

    @property
    def display(self) -> str:
        return self.label or self.value


#: Compatibility name for callers that used the old tier structure.
@dataclass(frozen=True)
class Tier:
    """A display-only tier view of one quality option.

    The 0.8.0 engine has no tier list; this exists so candidate labels such as
    ``library._tier_label`` keep working without a parallel data model.
    """

    name: str
    resolution: str | None = None
    sources: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "resolution": self.resolution,
            "sources": list(self.sources),
        }

    @property
    def label(self) -> str:
        return self.name


def _language_option_value(value: str, vocabulary: Mapping[str, str]) -> str | None:
    """Canonicalise a language option, accepting legacy two-letter aliases."""
    if value in vocabulary:
        return value
    code = languages.normalise(value)
    return code if code in vocabulary else None


def _option_value(section: str, value: Any,
                  vocabulary: Mapping[str, str]) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if section in {"audio_languages", "subtitle_languages"}:
        return _language_option_value(text, vocabulary)
    return text if text in vocabulary else None


def _options_from_raw(raw: Any, vocabulary: Mapping[str, str],
                      default_weight: int, *,
                      section: str = "") -> tuple[Option, ...]:
    """Decode a criteria/row list into canonical Option objects."""
    values = raw
    if isinstance(raw, str):
        parsed = _load_json(raw)
        values = parsed if parsed is not None else [
            part for part in raw.replace(";", ",").split(",") if part.strip()
        ]
    if isinstance(values, Mapping):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return ()
    out: list[Option] = []
    seen: set[str] = set()
    for entry in values:
        if isinstance(entry, Mapping):
            raw_value = entry.get("value")
            weight = entry.get("weight", default_weight)
            label = str(entry.get("label") or "").strip()
        else:
            raw_value = entry
            weight = default_weight
            label = ""
        value = _option_value(section, raw_value, vocabulary)
        if not value or value in seen:
            continue
        try:
            weight = int(weight)
        except (TypeError, ValueError):
            weight = default_weight
        if not _WEIGHT_MIN <= weight <= _WEIGHT_MAX:
            weight = default_weight
        seen.add(value)
        out.append(Option(value, label or vocabulary[value], weight))
    return tuple(out)


def _section_options(row: Mapping[str, Any], criteria: Mapping[str, Any],
                     key: str) -> tuple[Option, ...]:
    spec = SECTION_BY_KEY[key]
    vocabulary = _SECTION_VOCABULARIES[key]
    if key in criteria:
        raw = criteria.get(key)
    else:
        # A partially migrated row may still have the legacy JSON columns.
        keys = row.keys() if hasattr(row, "keys") else row
        try:
            raw = row[key] if key in keys else None
        except (IndexError, KeyError, TypeError):
            raw = None
        if isinstance(raw, str):
            raw = _load_json(raw)
    return _options_from_raw(
        raw, vocabulary, int(spec["default_weight"]), section=key
    )


# -------------------------------------------------------------------- profile
@dataclass(frozen=True)
class Profile:
    """A decoded weighted quality profile."""

    resolution: str | None = None
    resolution_weight: int = 10
    quality_options: tuple[Option, ...] = ()
    video_codecs: tuple[Option, ...] = ()
    audio_codecs: tuple[Option, ...] = ()
    audio_languages: tuple[Option, ...] = ()
    audio_channels: tuple[Option, ...] = ()
    subtitle_formats: tuple[Option, ...] = ()
    subtitle_languages: tuple[Option, ...] = ()
    target_score: int | None = None
    upgrades: bool = True
    name: str = ""
    profile_id: int | None = None
    builtin_key: str | None = None
    is_builtin: bool = False
    revision: int = 1

    def __post_init__(self) -> None:
        if self.target_score is None:
            object.__setattr__(self, "target_score", self.max_score)
        if self.resolution:
            value = normalise_resolution(self.resolution)
            object.__setattr__(self, "resolution", value if value in RESOLUTIONS else None)

    # ----------------------------------------------------------- serialisation
    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Profile":
        def get(key, default=None):
            keys = row.keys() if hasattr(row, "keys") else row
            try:
                return row[key] if key in keys else default
            except (IndexError, KeyError):
                return default

        criteria = _load_json(get("criteria_json"))
        if not isinstance(criteria, Mapping):
            criteria = {}
        if not isinstance(criteria, dict):
            criteria = dict(criteria)

        resolution = get("resolution")
        if resolution not in RESOLUTIONS:
            # Pre-v5 rows only carry a legacy tier list. The v5 migration
            # discards them, but a best-effort read keeps old backups usable.
            legacy = _load_json(get("qualities"))
            if isinstance(legacy, list):
                for entry in legacy:
                    if isinstance(entry, Mapping):
                        candidate = normalise_resolution(entry.get("resolution"))
                        if candidate in RESOLUTIONS:
                            resolution = candidate
                            break
            if resolution not in RESOLUTIONS:
                for value in decode_resolutions(get("resolutions")):
                    resolution = value
                    break
        resolution = normalise_resolution(resolution)
        if resolution not in RESOLUTIONS:
            resolution = None

        quality_raw = criteria.get("quality_options")
        if quality_raw is None:
            quality_raw = get("qualities")
        quality_options = _options_from_raw(
            _quality_values_from_legacy(quality_raw), QUALITY_LABELS, 10
        )
        if not quality_options and get("resolution") is None:
            quality_options = _options_from_raw(
                DEFAULT_QUALITY_SELECTION, QUALITY_LABELS, 10
            )

        try:
            resolution_weight = int(get("resolution_weight") or 10)
        except (TypeError, ValueError):
            resolution_weight = 10

        target_raw = get("target_score")
        try:
            target_score = int(target_raw) if target_raw is not None else None
        except (TypeError, ValueError):
            target_score = None
        if not target_score or target_score < 0:
            target_score = None

        return cls(
            resolution=resolution,
            resolution_weight=resolution_weight,
            quality_options=quality_options,
            video_codecs=_section_options(row, criteria, "video_codecs"),
            audio_codecs=_section_options(row, criteria, "audio_codecs"),
            audio_languages=_section_options(row, criteria, "audio_languages"),
            audio_channels=_section_options(row, criteria, "audio_channels"),
            subtitle_formats=_section_options(row, criteria, "subtitle_formats"),
            subtitle_languages=_section_options(row, criteria, "subtitle_languages"),
            target_score=target_score,
            upgrades=bool(get("upgrades", True)),
            name=str(get("name") or ""),
            profile_id=_safe_int(get("id")),
            builtin_key=get("builtin_key"),
            is_builtin=bool(get("is_builtin")),
            revision=_safe_int(get("revision")) or 1,
        )

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> "Profile":
        """Build a canonical profile from a :func:`validate_profile` result."""
        return cls(
            resolution=values.get("resolution"),
            resolution_weight=int(values.get("resolution_weight", 10)),
            quality_options=_options_from_mapping(
                values.get("quality_options"), QUALITY_LABELS, 10
            ),
            video_codecs=_options_from_mapping(
                values.get("video_codecs"), _SECTION_VOCABULARIES["video_codecs"],
                VIDEO_CODEC_WEIGHT,
            ),
            audio_codecs=_options_from_mapping(
                values.get("audio_codecs"), _SECTION_VOCABULARIES["audio_codecs"],
                AUDIO_CODEC_WEIGHT,
            ),
            audio_languages=_options_from_mapping(
                values.get("audio_languages"),
                _SECTION_VOCABULARIES["audio_languages"], AUDIO_LANGUAGE_WEIGHT,
            ),
            audio_channels=_options_from_mapping(
                values.get("audio_channels"),
                _SECTION_VOCABULARIES["audio_channels"], AUDIO_CHANNEL_WEIGHT,
            ),
            subtitle_formats=_options_from_mapping(
                values.get("subtitle_formats"),
                _SECTION_VOCABULARIES["subtitle_formats"], SUBTITLE_FORMAT_WEIGHT,
            ),
            subtitle_languages=_options_from_mapping(
                values.get("subtitle_languages"),
                _SECTION_VOCABULARIES["subtitle_languages"],
                SUBTITLE_LANGUAGE_WEIGHT,
            ),
            target_score=values.get("target_score"),
            upgrades=bool(values.get("upgrades", True)),
            name=str(values.get("name") or ""),
        )

    def to_payload(self) -> dict:
        """The editable JSON payload for this profile."""
        return {
            "name": self.name,
            "resolution": self.resolution,
            "resolution_weight": self.resolution_weight,
            "quality_options": [option.to_dict() for option in self.quality_options],
            "video_codecs": [option.to_dict() for option in self.video_codecs],
            "audio_codecs": [option.to_dict() for option in self.audio_codecs],
            "audio_languages": [option.to_dict() for option in self.audio_languages],
            "audio_channels": [option.to_dict() for option in self.audio_channels],
            "subtitle_formats": [
                option.to_dict() for option in self.subtitle_formats
            ],
            "subtitle_languages": [
                option.to_dict() for option in self.subtitle_languages
            ],
            "target_score": self.target_score,
            "upgrades": self.upgrades,
        }

    # ----------------------------------------------------------------- maxima
    def _section_values(self, key: str) -> tuple[Option, ...]:
        return getattr(self, key)

    @property
    def mandatory_max_score(self) -> int:
        source_max = max(
            (option.weight for option in self.quality_options), default=0
        )
        return max(0, int(self.resolution_weight)) + source_max

    @property
    def max_score(self) -> int:
        total = self.mandatory_max_score
        for spec in OPTIONAL_SECTIONS:
            options = self._section_values(spec["key"])
            if not options:
                continue
            if spec["kind"] == "exclusive":
                total += max(option.weight for option in options)
            else:
                total += sum(option.weight for option in options)
        return total

    @property
    def tier(self) -> Tier | None:
        """Compatibility view used by old templates/tests."""
        return (self.tiers[0] if self.tiers else None)

    @property
    def tiers(self) -> tuple[Tier, ...]:
        """Display-only tiers, one per selected quality option."""
        out = []
        for option in self.quality_options:
            out.append(Tier(
                _tier_name(self.resolution, (option.value,)),
                self.resolution,
                (option.value,),
            ))
        return tuple(out)

    @property
    def cutoff_index(self) -> None:
        return None

    @property
    def cutoff_rank(self) -> None:
        return None

    # --------------------------------------------------------------- matching
    def _best_quality_option(self, facts: Mapping[str, Any]) -> Option | None:
        source = facts.get("source")
        best = None
        for index, option in enumerate(self.quality_options):
            if option.value != source:
                continue
            if best is None or option.weight > best[0]:
                best = (option.weight, index, option)
        return best[2] if best else None

    def rank(self, release: Any, *, allow_unknown_source: bool = False) -> int | None:
        """Compatibility helper: index of the best matching quality option."""
        facts = release_facts(release)
        source = facts.get("source")
        for index, option in enumerate(self.quality_options):
            if option.value == source:
                return index
        if source in (None, "", "unknown") and allow_unknown_source:
            if self.quality_options:
                return max(
                    range(len(self.quality_options)),
                    key=lambda index: self.quality_options[index].weight,
                )
        return None

    def evaluate(self, release: Any, original_language: Any = None) -> dict:
        """Score one release against this profile.

        Returns a JSON-ready dictionary with ``eligible``, ``score``,
        ``target_met`` and the per-section contribution details.
        """
        facts = release_facts(release, original_language)
        resolution = facts["resolution"]
        exact = bool(
            self.resolution and resolution in RESOLUTIONS
            and resolution == self.resolution
        )
        lower = bool(
            self.resolution and resolution in RESOLUTIONS
            and RANK.get(resolution, -1) > RANK.get(self.resolution, -1)
        )
        higher = bool(
            self.resolution and resolution in RESOLUTIONS
            and RANK.get(resolution, -1) < RANK.get(self.resolution, -1)
        )

        reason = ""
        resolution_ok = False
        if not self.resolution:
            reason = "This quality profile has no resolution."
        elif resolution not in RESOLUTIONS:
            reason = "The release resolution is unknown."
        elif exact:
            resolution_ok = True
        elif lower and self.upgrades:
            resolution_ok = True
        elif lower:
            reason = (
                f"{resolution} is lower than {self.resolution}; "
                "enable upgrades to allow a lower-resolution fallback."
            )
        elif higher:
            reason = (
                f"{resolution} is higher than this profile's {self.resolution}."
            )
        else:
            reason = f"{resolution} is not in this quality profile."

        source_option = self._best_quality_option(facts)
        source = facts.get("source")
        if not reason:
            if not self.quality_options:
                reason = "This profile does not select a quality/source option."
            elif source_option is None:
                if source in (None, "", "unknown"):
                    reason = "The release source is unknown."
                else:
                    reason = (
                        f"{source_label(source)} is not a selected "
                        "quality/source option."
                    )

        eligible = bool(resolution_ok and source_option is not None and not reason)
        if not eligible:
            return {
                "eligible": False,
                "reason": reason or "This release does not match the profile.",
                "score": 0,
                "base_score": 0,
                "target_score": int(self.target_score or 0),
                "max_score": self.max_score,
                "target_met": False,
                "needs_upgrade": False,
                "exact_resolution": exact,
                "resolution": resolution,
                "source": source,
                "source_weight": 0,
                "components": [],
                "section_scores": {},
                "section_maxima": {},
                "original_language": facts.get("original_language"),
                "size": facts.get("size", 0),
            }

        base_score = int(self.resolution_weight) + int(source_option.weight)
        components: list[dict] = [
            {
                "section": "resolution",
                "kind": "mandatory",
                "value": resolution,
                "label": resolution,
                "weight": int(self.resolution_weight),
            },
            {
                "section": "quality",
                "kind": "mandatory",
                "value": source_option.value,
                "label": source_option.label,
                "weight": int(source_option.weight),
            },
        ]
        section_scores: dict[str, int] = {}
        section_maxima: dict[str, int] = {}
        optional_score = 0
        original = facts.get("original_language")
        for spec in OPTIONAL_SECTIONS:
            key = spec["key"]
            options = self._section_values(key)
            facts_for = facts.get(key) or frozenset()
            matched = [
                option for option in options
                if _option_matches(key, option, facts_for, original)
            ]
            contribution = 0
            if spec["kind"] == "exclusive":
                if matched:
                    best = max(
                        enumerate(matched),
                        key=lambda pair: (pair[1].weight, -pair[0]),
                    )[1]
                    contribution = int(best.weight)
                    components.append({
                        "section": key,
                        "kind": "exclusive",
                        "value": best.value,
                        "label": best.label,
                        "weight": int(best.weight),
                    })
            else:
                for option in matched:
                    contribution += int(option.weight)
                    components.append({
                        "section": key,
                        "kind": "multi",
                        "value": option.value,
                        "label": option.label,
                        "weight": int(option.weight),
                    })
            section_scores[key] = contribution
            optional_score += contribution
            if spec["kind"] == "exclusive":
                section_maxima[key] = (
                    max((option.weight for option in options), default=0)
                )
            else:
                section_maxima[key] = sum(option.weight for option in options)

        score = base_score + optional_score
        target = int(self.target_score or 0)
        return {
            "eligible": True,
            "reason": "",
            "score": score,
            "base_score": base_score,
            "target_score": target,
            "max_score": self.max_score,
            "target_met": bool(score >= target and exact),
            "needs_upgrade": not exact,
            "exact_resolution": exact,
            "resolution": resolution,
            "source": source,
            "source_weight": int(source_option.weight),
            "components": components,
            "section_scores": section_scores,
            "section_maxima": section_maxima,
            "original_language": original,
            "size": facts.get("size", 0),
        }

    def score(self, release: Any, original_language: Any = None) -> int:
        return int(self.evaluate(release, original_language)["score"])

    def allows(self, release: Any, original_language: Any = None) -> bool:
        return bool(self.evaluate(release, original_language)["eligible"])

    def check(self, release: Any, original_language: Any = None) -> tuple[bool, str]:
        evaluation = self.evaluate(release, original_language)
        return bool(evaluation["eligible"]), str(evaluation["reason"])

    def cutoff_met(self, current: Any = None) -> bool:
        """Compatibility helper: whether the current file is target-complete.

        The score is always re-derived from the stored media facts and the
        current profile, never read from a stale ``file_score`` column.
        """
        if current is None:
            return False
        return bool(self.evaluate(_current_view(current))["target_met"])

    def cutoff_met_for(self, release: Any) -> bool:
        return self.cutoff_met(release)


def _option_matches(section: str, option: Option,
                    facts: Iterable[str], original_language: Any) -> bool:
    value = option.value
    if section in {"audio_languages", "subtitle_languages"}:
        if value == _ANY:
            return _ANY in facts
        if value == languages.ORIGINAL:
            original = languages.normalise(original_language)
            return bool(original and original in facts)
    return value in facts


def _options_from_mapping(raw: Any, vocabulary: Mapping[str, str],
                          default_weight: int) -> tuple[Option, ...]:
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    out = []
    for entry in raw:
        if isinstance(entry, Mapping):
            value = str(entry.get("value") or "").strip().lower()
            label = str(entry.get("label") or "").strip() or vocabulary.get(value, "")
            try:
                weight = int(entry.get("weight", default_weight))
            except (TypeError, ValueError):
                weight = default_weight
        else:
            value = str(entry or "").strip().lower()
            label = vocabulary.get(value, "")
            weight = default_weight
        if value in vocabulary:
            out.append(Option(value, label, weight))
    return tuple(out)


def _quality_values_from_legacy(raw: Any) -> Any:
    """Turn old tier dicts into a ``{value,label,weight}`` option list."""
    values = raw
    if isinstance(raw, str):
        values = _load_json(raw)
    if isinstance(values, Mapping):
        return values
    if not isinstance(values, (list, tuple)):
        return []
    if any(isinstance(entry, str) for entry in values):
        return values
    out = []
    for entry in values:
        if not isinstance(entry, Mapping):
            continue
        if "value" in entry and "sources" not in entry:
            # Already a 0.8.0 weighted option; keep its weight and label.
            out.append(dict(entry))
            continue
        sources = entry.get("sources")
        if isinstance(sources, str):
            sources = [sources]
        if not sources:
            # An empty legacy source list meant "any source"; the closest
            # 0.8.0 equivalent is the default selected set.
            out.extend(DEFAULT_QUALITY_SELECTION)
            continue
        for source in sources:
            value = normalise_quality_value(source)
            if value:
                out.append({"value": value, "label": QUALITY_LABELS[value],
                            "weight": 10})
    return out


def _tier_name(resolution: str | None, sources: Iterable[str]) -> str:
    labels = [source_label(source) for source in sources]
    prefix = resolution or ""
    if labels:
        return f"{prefix} {'/'.join(labels)}".strip()
    return prefix


def _current_view(current: Any) -> Any:
    """Expand a stored movie/episode row into a scorable release view."""
    if current is None:
        return None
    if isinstance(current, str):
        return {"resolution": current}
    if isinstance(current, Mapping):
        view = dict(current)
    elif hasattr(current, "keys"):
        view = dict(current)
    else:
        return current
    media = view.get("media_info")
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except (TypeError, ValueError):
            media = None
    if isinstance(media, Mapping) and media:
        view.setdefault("probe", media)
        if normalise_resolution(view.get("resolution")) in (None, "unknown"):
            view["resolution"] = media.get("resolution")
        if not view.get("source") and media.get("source"):
            view["source"] = media.get("source")
    if view.get("file_size") and not view.get("size"):
        view["size"] = view["file_size"]
    return view


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ selection
def evaluate_release(release: Any, profile: Profile,
                     original_language: Any = None) -> dict:
    """Score one release against ``profile`` (module-level convenience)."""
    return profile.evaluate(release, original_language)


def _candidate_sort_key(release: Any, evaluation: Mapping[str, Any]) -> tuple:
    """Highest score wins; ties use the frozen contract's preference order."""
    view = release if isinstance(release, Mapping) else {}
    return (
        -int(evaluation.get("score") or 0),
        0 if evaluation.get("exact_resolution") else 1,
        -int(evaluation.get("source_weight") or 0),
        -_float(view.get("score")),
        -int(evaluation.get("size") or 0),
        str(view.get("name") or "").casefold(),
    )


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def choose_release(
    releases: Iterable[Any],
    profile: Profile,
    *,
    current: Any = None,
    original_language: Any = None,
) -> Any | None:
    """Pick the release to grab, or ``None`` when nothing should be done.

    ``target_met`` candidates always win over fallbacks. With a current file,
    no upgrade happens when it already meets the target; otherwise a replacement
    is only chosen when it scores higher than the current file (or the current
    file is not eligible at all). The current score is recomputed from its
    stored media facts rather than read from a stale column.
    """
    eligible: list[tuple[Any, dict]] = []
    for release in releases or []:
        evaluation = profile.evaluate(release, original_language)
        if evaluation["eligible"]:
            eligible.append((release, evaluation))
    if not eligible:
        return None

    targets = [pair for pair in eligible if pair[1]["target_met"]]
    pool = targets or eligible
    best_release, best_evaluation = min(
        pool, key=lambda pair: _candidate_sort_key(pair[0], pair[1])
    )

    if current:
        if not profile.upgrades:
            return None
        current_evaluation = profile.evaluate(
            _current_view(current), original_language
        )
        if current_evaluation["target_met"]:
            return None
        if current_evaluation["eligible"]:
            if best_evaluation["score"] <= current_evaluation["score"]:
                return None
    return best_release


# --------------------------------------------------------------- validation
def _weight(value: Any, default: int, label: str = "Weight") -> int:
    if value is None or value == "":
        return int(default)
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number between 1 and 1000.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number between 1 and 1000.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label} must be a whole number between 1 and 1000.")
    if not _WEIGHT_MIN <= number <= _WEIGHT_MAX:
        raise ValueError(f"{label} must be between 1 and 1000.")
    return number


def _option_entries(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, str):
        values = _load_json(raw)
        if isinstance(values, (list, tuple)):
            return list(values)
        if isinstance(values, Mapping):
            return [values]
        return [
            part for part in raw.replace(";", ",").split(",") if part.strip()
        ]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    raise ValueError("Options must be a list.")


def _validate_options(raw: Any, vocabulary: Mapping[str, str],
                      default_weight: int, section: str,
                      key: str = "") -> list[dict]:
    options: list[dict] = []
    seen: set[str] = set()
    for entry in _option_entries(raw):
        if isinstance(entry, Mapping):
            raw_value = entry.get("value")
            label = str(entry.get("label") or "").strip()
            weight = _weight(entry.get("weight"), default_weight,
                             f"{section} weight")
        else:
            raw_value = entry
            label = ""
            weight = int(default_weight)
        if not str(raw_value or "").strip():
            raise ValueError(f"Choose a {section} value.")
        value = _option_value(key, raw_value, vocabulary)
        if value is None:
            raise ValueError(f"Unknown {section} value {raw_value!r}.")
        if value in seen:
            raise ValueError(
                f"Duplicate {section} option {vocabulary[value]!r}."
            )
        seen.add(value)
        options.append({
            "value": value,
            "label": (label or vocabulary[value])[:80],
            "weight": weight,
        })
    return options


def _target_value(raw: Any, max_score: int) -> int:
    if max_score < _TARGET_FLOOR:
        raise ValueError(
            "The profile's maximum score must be at least 20; "
            "increase the resolution, source or optional weights."
        )
    if raw is None or raw == "":
        return max_score
    if isinstance(raw, bool):
        raise ValueError(f"Target score must be between 20 and {max_score}.")
    try:
        target = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Target score must be between 20 and {max_score}.")
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(f"Target score must be between 20 and {max_score}.")
    if not _TARGET_FLOOR <= target <= max_score:
        raise ValueError(f"Target score must be between 20 and {max_score}.")
    return target


def criteria_from_values(values: Mapping[str, Any]) -> dict:
    """The canonical ``criteria_json`` payload for validated profile values."""
    criteria = {"version": 1}
    for key in ("quality_options", *SECTION_KEYS):
        criteria[key] = [dict(option) for option in values.get(key) or []]
    return criteria


def legacy_quality_list(values: Mapping[str, Any]) -> list[dict]:
    """Legacy ``qualities`` JSON so old readers still see tier-like rows."""
    resolution = values.get("resolution")
    out = []
    for option in values.get("quality_options") or []:
        out.append({
            "name": _tier_name(resolution, (option["value"],)),
            "resolution": resolution,
            "sources": [option["value"]],
        })
    return out


def validate_profile(payload: Mapping[str, Any] | None = None, **overrides: Any) -> dict:
    """Validate a builder/API profile payload and return canonical values.

    Raises :class:`ValueError` with a user-facing reason. Unknown resolutions,
    unknown/duplicate options, weights outside 1..1000 and a target outside
    ``20..max_score`` are all rejected.
    """
    if payload is None:
        data: dict[str, Any] = {}
    elif isinstance(payload, Mapping):
        data = dict(payload)
    else:
        # Tolerate the old positional tier-list signature far enough to fail
        # with a useful ValueError instead of a TypeError.
        data = {"quality_options": payload}
    data.update(overrides)

    resolution = normalise_resolution(data.get("resolution"))
    if resolution is None:
        raise ValueError("Choose a resolution.")
    if resolution not in RESOLUTIONS:
        raise ValueError(f"Unknown resolution {data.get('resolution')!r}.")

    resolution_weight = _weight(
        data.get("resolution_weight"), 10, "Resolution weight"
    )
    quality_options = _validate_options(
        data.get("quality_options"), QUALITY_LABELS, 10, "quality/source"
    )
    if not quality_options:
        raise ValueError("Select at least one quality/source option.")

    values: dict[str, Any] = {
        "name": str(data.get("name") or "").strip()[:80],
        "resolution": resolution,
        "resolution_weight": resolution_weight,
        "quality_options": quality_options,
    }
    for spec in OPTIONAL_SECTIONS:
        key = spec["key"]
        values[key] = _validate_options(
            data.get(key), _SECTION_VOCABULARIES[key],
            int(spec["default_weight"]), spec["label"].lower(), key=key,
        )

    max_score = _compute_max_score(values)
    target_raw = data.get("target_score")
    if target_raw is None:
        target_raw = data.get("min_score")
    values["target_score"] = _target_value(target_raw, max_score)
    values["max_score"] = max_score
    values["upgrades"] = _truthy(data.get("upgrades"), True)
    values["criteria"] = criteria_from_values(values)
    values["criteria_json"] = json.dumps(
        values["criteria"], ensure_ascii=False, sort_keys=True
    )
    return values


def _compute_max_score(values: Mapping[str, Any]) -> int:
    resolution_weight = int(values.get("resolution_weight") or 0)
    quality = values.get("quality_options") or []
    total = resolution_weight + max(
        (int(option["weight"]) for option in quality), default=0
    )
    for spec in OPTIONAL_SECTIONS:
        options = values.get(spec["key"]) or []
        if not options:
            continue
        if spec["kind"] == "exclusive":
            total += max(int(option["weight"]) for option in options)
        else:
            total += sum(int(option["weight"]) for option in options)
    return total


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


# ------------------------------------------------------- summaries / builder
def profile_dict(profile: Profile) -> dict:
    """JSON-ready profile fields, including legacy display fields."""
    data = {
        "id": profile.profile_id,
        "name": profile.name,
        "builtin_key": profile.builtin_key,
        "is_builtin": profile.is_builtin,
        "revision": profile.revision,
        "resolution": profile.resolution,
        "resolution_weight": profile.resolution_weight,
        "quality_options": [option.to_dict() for option in profile.quality_options],
        "video_codecs": [option.to_dict() for option in profile.video_codecs],
        "audio_codecs": [option.to_dict() for option in profile.audio_codecs],
        "audio_languages": [
            option.to_dict() for option in profile.audio_languages
        ],
        "audio_channels": [option.to_dict() for option in profile.audio_channels],
        "subtitle_formats": [
            option.to_dict() for option in profile.subtitle_formats
        ],
        "subtitle_languages": [
            option.to_dict() for option in profile.subtitle_languages
        ],
        "target_score": int(profile.target_score or 0),
        "min_score": int(profile.target_score or 0),
        "max_score": profile.max_score,
        "upgrades": profile.upgrades,
        # Legacy fields retained for templates and older API clients.
        "qualities": [tier.to_dict() for tier in profile.tiers],
        "cutoff": None,
        "cutoff_index": None,
        "audio_mode": "any",
        "subtitle_mode": "any",
        "probe_required": True,
        "resolutions": [profile.resolution] if profile.resolution else [],
    }
    return data


def profile_summary(row: Mapping[str, Any], default_key: str | None = None) -> dict:
    """A row as JSON-ready profile data for the API and templates."""
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        data = dict(row) if hasattr(row, "keys") else {}
    profile = Profile.from_row(row)
    data.update(profile_dict(profile))
    data["criteria"] = _load_json(data.get("criteria_json")) or {}
    data["is_default"] = bool(
        default_key and profile.builtin_key == default_key
    )
    return data


def builder_schema() -> dict:
    """Everything the profile builder UI needs to render its controls."""
    sections = []
    for spec in OPTIONAL_SECTIONS:
        sections.append({
            "key": spec["key"],
            "label": spec["label"],
            "kind": spec["kind"],
            "default_weight": spec["default_weight"],
            "options": [
                {"value": value, "label": label}
                for value, label in _SECTION_VOCABULARIES[spec["key"]].items()
            ],
        })
    return {
        "resolutions": list(RESOLUTIONS),
        "quality_options": [
            {"value": value, "label": label, "weight": 10}
            for value, label in QUALITY_OPTIONS
        ],
        "default_quality_options": [
            dict(option) for option in DEFAULT_QUALITY_SELECTION
        ],
        "sections": sections,
        "target_floor": _TARGET_FLOOR,
        "weight_min": _WEIGHT_MIN,
        "weight_max": _WEIGHT_MAX,
    }


def find_default_profile(profiles: Iterable[Any], default_key: str = "1080p") -> Any:
    """Return the profile matching ``default_key`` (or the 1080p fallback).

    ``profiles`` may be summary dictionaries or :class:`Profile` objects. This
    is the helper every add/import dialog uses to preselect the default.
    """
    items = list(profiles or [])
    for key in (default_key, "1080p"):
        if not key:
            continue
        for profile in items:
            if _profile_key(profile) == key:
                return profile
    for profile in items:
        if _profile_builtin(profile):
            return profile
    return items[0] if items else None


def _profile_key(profile: Any) -> str | None:
    if isinstance(profile, Mapping):
        return profile.get("builtin_key")
    return getattr(profile, "builtin_key", None)


def _profile_builtin(profile: Any) -> bool:
    if isinstance(profile, Mapping):
        return bool(profile.get("is_builtin"))
    return bool(getattr(profile, "is_builtin", False))


__all__ = [
    "AUDIO_CHANNEL_OPTIONS",
    "AUDIO_CHANNEL_WEIGHT",
    "AUDIO_CODEC_OPTIONS",
    "AUDIO_CODEC_WEIGHT",
    "AUDIO_LANGUAGE_OPTIONS",
    "AUDIO_LANGUAGE_WEIGHT",
    "AUDIO_MODES",
    "CANDIDATE_RESOLUTIONS",
    "DEFAULT_QUALITY_SELECTION",
    "DEFAULT_QUALITY_VALUES",
    "LOW_QUALITY_SOURCES",
    "OPTIONAL_SECTIONS",
    "Option",
    "Profile",
    "QUALITY_CHOICES",
    "QUALITY_LABELS",
    "QUALITY_OPTIONS",
    "RANK",
    "RESOLUTIONS",
    "SECTION_KEYS",
    "SOURCES",
    "SOURCE_CHOICES",
    "SOURCE_LABELS",
    "SUBTITLE_FORMAT_OPTIONS",
    "SUBTITLE_FORMAT_WEIGHT",
    "SUBTITLE_LANGUAGE_OPTIONS",
    "SUBTITLE_LANGUAGE_WEIGHT",
    "SUBTITLE_MODES",
    "Tier",
    "VIDEO_CODEC_OPTIONS",
    "VIDEO_CODEC_WEIGHT",
    "builder_schema",
    "choose_release",
    "criteria_from_values",
    "decode_language_list",
    "decode_qualities",
    "decode_resolutions",
    "evaluate_release",
    "find_default_profile",
    "legacy_quality_list",
    "normalise_audio_codec",
    "normalise_channels",
    "normalise_quality_value",
    "normalise_resolution",
    "normalise_source",
    "normalise_subtitle_format",
    "normalise_video_codec",
    "parse_resolution",
    "parse_source",
    "profile_dict",
    "profile_summary",
    "release_facts",
    "resolution_from_dimensions",
    "resolution_from_height",
    "source_label",
    "validate_profile",
]


def decode_qualities(raw: Any, fallback_resolutions: Any = None) -> list[dict]:
    """Return legacy tier dictionaries (kept for old callers)."""
    values = _load_json(raw)
    tiers: list[dict] = []
    if isinstance(values, (list, tuple)):
        for entry in values:
            if isinstance(entry, Mapping):
                resolution = normalise_resolution(entry.get("resolution"))
                if resolution not in RESOLUTIONS:
                    continue
                sources = entry.get("sources") or []
                if isinstance(sources, str):
                    sources = [sources]
                values_in = [
                    value for value in (
                        normalise_quality_value(source) for source in sources
                    ) if value
                ]
                tiers.append({
                    "name": str(entry.get("name") or _tier_name(
                        resolution, values_in))[:80],
                    "resolution": resolution,
                    "sources": values_in,
                })
    if not tiers:
        for resolution in decode_resolutions(fallback_resolutions):
            tiers.append({"name": resolution, "resolution": resolution,
                          "sources": []})
    return tiers
