"""Quality tiers, stream requirements and upgrade decisions.

A profile is an ordered list of quality tiers, best first. A tier combines a
resolution with an optional set of release sources:

    [{"name": "4K WEB-DL", "resolution": "2160p", "sources": ["web-dl"]},
     {"name": "1080p WEB-DL", "resolution": "1080p", "sources": ["web-dl"]}]

The order is the user's hierarchy. A tier with an empty source list accepts
any source. Release sources come from the provider name (WEB-DL, WEBRip,
BluRay, CAM, ...). Audio and subtitle requirements come from the ffprobe
result for the stream, not from the name, so a profile can demand original
audio or English subtitles without guessing.

A cutoff is a stopping point, not a ceiling: if a lower-ranked tier is the
cutoff and a better allowed release exists, the better one still wins.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import languages

#: Best first. ``unknown`` ranks below every explicit tier, because a name
#: with no quality tag is not evidence of a high-quality file.
RESOLUTIONS = ("2160p", "1080p", "720p", "480p", "unknown")
RANK = {name: index for index, name in enumerate(RESOLUTIONS)}

#: Highest quality first. The UI presents these as source checkboxes.
SOURCES = (
    "remux", "bluray", "web-dl", "webrip", "hdtv", "dvdrip", "brrip",
    "hdcam", "cam", "ts", "tc", "scr", "r5", "unknown",
)
SOURCE_LABELS = {
    "remux": "REMUX", "bluray": "BluRay", "web-dl": "WEB-DL",
    "webrip": "WEBRip", "hdtv": "HDTV", "dvdrip": "DVDRip", "brrip": "BRRip",
    "hdcam": "HDCAM", "cam": "CAM", "ts": "TS", "tc": "TC",
    "scr": "SCR", "r5": "R5", "unknown": "Unknown",
}
SOURCE_CHOICES = tuple((source, SOURCE_LABELS[source]) for source in SOURCES)
LOW_QUALITY_SOURCES = frozenset({"hdcam", "cam", "ts", "tc", "scr", "r5"})
AUDIO_MODES = ("any", "original", "single", "multi")
SUBTITLE_MODES = ("any", "required", "none")

#: Order matters: REMUX before BluRay, WEB-DL before WEBRip, HDCAM before CAM,
#: and every pattern uses word-ish boundaries so titles such as "CAM" in
#: "Camelot" are not mistaken for a source.
_SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remux", re.compile(r"(?i)(?<![a-z0-9])remux(?![a-z0-9])")),
    ("bluray", re.compile(
        r"(?i)(?<![a-z0-9])(?:blu[\s._-]?ray|bd[\s._-]?rip|bd[\s._-]?remux|bd25|bd50)(?![a-z0-9])"
    )),
    ("web-dl", re.compile(r"(?i)(?<![a-z0-9])web[\s._-]?dl(?![a-z0-9])")),
    ("webrip", re.compile(r"(?i)(?<![a-z0-9])web[\s._-]?rip(?![a-z0-9])")),
    ("hdtv", re.compile(r"(?i)(?<![a-z0-9])hd[\s._-]?tv(?![a-z0-9])")),
    ("dvdrip", re.compile(r"(?i)(?<![a-z0-9])dvd[\s._-]?rip(?![a-z0-9])")),
    ("brrip", re.compile(r"(?i)(?<![a-z0-9])br[\s._-]?rip(?![a-z0-9])")),
    ("hdcam", re.compile(r"(?i)(?<![a-z0-9])hd[\s._-]?cam(?![a-z0-9])")),
    ("cam", re.compile(r"(?i)(?<![a-z0-9])cam(?:rip)?(?![a-z0-9])")),
    ("ts", re.compile(r"(?i)(?<![a-z0-9])(?:hd[\s._-]?ts|telesync|ts)(?![a-z0-9])")),
    ("tc", re.compile(r"(?i)(?<![a-z0-9])(?:telecine|tc)(?![a-z0-9])")),
    ("scr", re.compile(r"(?i)(?<![a-z0-9])(?:screener|scr)(?![a-z0-9])")),
    ("r5", re.compile(r"(?i)(?<![a-z0-9])r5(?![a-z0-9])")),
)

#: Order matters: 4K must be tested before 1080p, and a word boundary keeps
#: "4K" from matching inside a title like "4Kings".
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("2160p", re.compile(r"(?i)(?<![\w])(?:2160p|4k|uhd)(?![\w])")),
    ("1080p", re.compile(r"(?i)(?<![\w])(?:1080p|fullhd|fhd)(?![\w])")),
    ("720p", re.compile(r"(?i)(?<![\w])720p(?![\w])")),
    ("480p", re.compile(r"(?i)(?<![\w])(?:480p|576p|540p|360p|sd)(?![\w])")),
)

#: A height in pixels from ffprobe mapped back onto the tiers above.
_HEIGHT_TIERS = ((1800, "2160p"), (1000, "1080p"), (650, "720p"))


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


def source_label(source: str | None) -> str:
    return SOURCE_LABELS.get(source or "unknown", "Unknown")


def resolution_from_height(height: int | None) -> str | None:
    """Map an ffprobe frame height to a tier; ``None`` when it is unusable."""
    if not height or height <= 0:
        return None
    for threshold, label in _HEIGHT_TIERS:
        if height >= threshold:
            return label
    return "480p"


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
    if width or height:
        return "480p"
    return None


def decode_resolutions(raw: Any) -> list[str]:
    """Decode a legacy profile resolution list, keeping known ordered tiers."""
    values = raw
    if isinstance(raw, str):
        try:
            values = json.loads(raw or "[]")
        except (TypeError, ValueError):
            values = []
    if not isinstance(values, (list, tuple)):
        return []
    seen = {str(value).strip().lower() for value in values}
    return [name for name in RESOLUTIONS if name in seen]


# --------------------------------------------------------------------- tiers
@dataclass(frozen=True)
class Tier:
    """One ordered quality step: a resolution and optional source set."""

    name: str
    resolution: str
    sources: tuple[str, ...] = ()

    def matches(self, release: Mapping[str, Any], *, allow_unknown_source: bool = False) -> bool:
        if release.get("resolution") != self.resolution:
            return False
        if not self.sources:
            return True
        source = str(release.get("source") or "unknown").lower()
        if source == "unknown" and allow_unknown_source:
            return True
        return source in self.sources

    def to_dict(self) -> dict:
        return {"name": self.name, "resolution": self.resolution,
                "sources": list(self.sources)}

    @property
    def label(self) -> str:
        return self.name or _tier_name(self.resolution, self.sources)


def _tier_name(resolution: str, sources: Iterable[str]) -> str:
    labels = [SOURCE_LABELS.get(source, source) for source in sources]
    return f"{resolution} {'/'.join(labels)}" if labels else resolution


def _decode_tiers(raw: Any, fallback_resolutions: Any = None) -> tuple[Tier, ...]:
    values = raw
    if isinstance(raw, str):
        try:
            values = json.loads(raw or "[]")
        except (TypeError, ValueError):
            values = []
    tiers: list[Tier] = []
    if isinstance(values, (list, tuple)):
        for entry in values:
            if isinstance(entry, str):
                resolution = entry.strip().lower()
                if resolution in RANK:
                    tiers.append(Tier(resolution, resolution))
                continue
            if not isinstance(entry, Mapping):
                continue
            resolution = str(entry.get("resolution") or "").strip().lower()
            if resolution not in RANK:
                continue
            raw_sources = entry.get("sources") or []
            if isinstance(raw_sources, str):
                raw_sources = [raw_sources]
            sources = []
            for source in raw_sources if isinstance(raw_sources, (list, tuple)) else []:
                label = str(source or "").strip().lower()
                if label in SOURCE_LABELS and label not in sources:
                    sources.append(label)
            sources = tuple(source for source in SOURCES if source in sources)
            name = str(entry.get("name") or "").strip()[:80]
            tiers.append(Tier(name or _tier_name(resolution, sources), resolution, sources))
    if not tiers:
        for resolution in decode_resolutions(fallback_resolutions):
            tiers.append(Tier(resolution, resolution))
    # De-duplicate exact tiers, preserving order.
    seen = set()
    unique = []
    for tier in tiers:
        key = (tier.resolution, tier.sources)
        if key not in seen:
            seen.add(key)
            unique.append(tier)
    return tuple(unique)


def decode_qualities(raw: Any, fallback_resolutions: Any = None) -> list[dict]:
    """Return tiers as JSON-ready dictionaries (used by the API/UI)."""
    return [tier.to_dict() for tier in _decode_tiers(raw, fallback_resolutions)]


def _release_view(release: Any) -> dict:
    if isinstance(release, str):
        return {"resolution": release}
    if isinstance(release, Mapping):
        return dict(release)
    return {}


def _probe_ok(release: Mapping[str, Any]) -> bool:
    probe = release.get("probe")
    return isinstance(probe, Mapping) and bool(probe.get("ok"))


def _audio_tracks(release: Mapping[str, Any]) -> list[dict]:
    probe = release.get("probe") or {}
    tracks = probe.get("audio") if isinstance(probe, Mapping) else None
    return [track for track in tracks or [] if isinstance(track, Mapping)]


def _subtitle_tracks(release: Mapping[str, Any]) -> list[dict]:
    probe = release.get("probe") or {}
    tracks = probe.get("subtitles") if isinstance(probe, Mapping) else None
    return [track for track in tracks or [] if isinstance(track, Mapping)]


def _langs(release: Mapping[str, Any], key: str, tracks: list[dict]) -> list[str]:
    values = release.get(key)
    if values is None:
        probe = release.get("probe") or {}
        values = probe.get(key) if isinstance(probe, Mapping) else None
    if values:
        return languages.normalise_many(values)
    return languages.normalise_many([track.get("language") for track in tracks])


@dataclass(frozen=True)
class Profile:
    """A decoded quality profile."""

    tiers: tuple[Tier, ...]
    cutoff_index: int | None = None
    upgrades: bool = True
    audio_mode: str = "any"
    audio_languages: tuple[str, ...] = ()
    subtitle_mode: str = "any"
    subtitle_languages: tuple[str, ...] = ()
    probe_required: bool = True

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Profile":
        def get(key, default=None):
            keys = row.keys() if hasattr(row, "keys") else row
            return row[key] if key in keys else default

        tiers = _decode_tiers(get("qualities"), get("resolutions"))
        cutoff_raw = str(get("cutoff") or "").strip()
        cutoff_index = None
        if cutoff_raw:
            # A cutoff may be a tier label or a bare resolution (legacy).
            for index, tier in enumerate(tiers):
                if tier.name.casefold() == cutoff_raw.casefold():
                    cutoff_index = index
                    break
            if cutoff_index is None:
                for index, tier in enumerate(tiers):
                    if tier.resolution == cutoff_raw.lower():
                        cutoff_index = index
                        break
        audio_mode = str(get("audio_mode") or "any").lower()
        if audio_mode not in AUDIO_MODES:
            audio_mode = "any"
        subtitle_mode = str(get("subtitle_mode") or "any").lower()
        if subtitle_mode not in SUBTITLE_MODES:
            subtitle_mode = "any"
        subtitle_languages = []
        for value in decode_language_list(get("subtitle_languages")):
            if str(value).strip().lower() == languages.ORIGINAL:
                code = languages.ORIGINAL
            else:
                code = languages.normalise(value)
            if code and code not in subtitle_languages:
                subtitle_languages.append(code)
        return cls(
            tiers=tiers,
            cutoff_index=cutoff_index,
            upgrades=bool(get("upgrades", True)),
            audio_mode=audio_mode,
            audio_languages=tuple(languages.normalise_many(get("audio_languages"))),
            subtitle_mode=subtitle_mode,
            subtitle_languages=tuple(subtitle_languages),
            probe_required=bool(get("probe_required", True)),
        )

    # ------------------------------------------------------------- matching
    def rank(self, release: Any, *, allow_unknown_source: bool = False) -> int | None:
        """Index of the best matching tier, lower is better, or ``None``."""
        view = _release_view(release)
        for index, tier in enumerate(self.tiers):
            if tier.matches(view, allow_unknown_source=allow_unknown_source):
                return index
        return None

    @property
    def cutoff_rank(self) -> int | None:
        return self.cutoff_index

    def cutoff_met(self, release: Any, *, allow_unknown_source: bool = True) -> bool:
        if self.cutoff_index is None:
            return False
        rank = self.rank(release, allow_unknown_source=allow_unknown_source)
        return rank is not None and rank <= self.cutoff_index

    def allows(self, release: Any, original_language: Any = None) -> bool:
        return self.check(release, original_language)[0]

    def check(self, release: Any, original_language: Any = None) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for one release.

        Source and resolution come from the provider name and the probe;
        audio/subtitle requirements are verified against the probe result.
        """
        view = _release_view(release)
        if not self.tiers:
            return False, "The quality profile has no tiers."
        rank = self.rank(view)
        if rank is None:
            resolution = view.get("resolution") or "unknown"
            source = source_label(view.get("source"))
            # Distinguish "this resolution is not allowed" from "the source
            # is not allowed for this resolution" in the user's language.
            same_resolution = any(tier.resolution == resolution for tier in self.tiers)
            if same_resolution:
                return False, f"{source} is not an allowed source for {resolution}."
            return False, f"{resolution} is not in the quality profile."

        # A profile that asks for audio/subtitle guarantees, or marks probing
        # as required, cannot make a decision from a failed probe.
        needs_probe = (
            self.probe_required
            or self.audio_mode != "any"
            or bool(self.audio_languages)
            or self.subtitle_mode != "any"
            or bool(self.subtitle_languages)
        )
        probe = view.get("probe")
        if not _probe_ok(view):
            if needs_probe:
                error = ""
                if isinstance(probe, Mapping):
                    error = str(probe.get("error") or "")
                return False, (
                    "Stream probe required but unavailable"
                    + (f": {error}" if error else ".")
                )
        else:
            audio_tracks = _audio_tracks(view)
            subtitle_tracks = _subtitle_tracks(view)
            audio_langs = _langs(view, "audio_languages", audio_tracks)
            subtitle_langs = _langs(view, "subtitle_languages", subtitle_tracks)

            if self.audio_mode == "single" and len(audio_tracks) != 1:
                return False, f"Profile requires one audio track; found {len(audio_tracks)}."
            if self.audio_mode == "multi" and (
                len(audio_tracks) < 2 and len(set(audio_langs)) < 2
            ):
                return False, "Profile requires multiple audio tracks/languages."
            if self.audio_mode == "original":
                original = languages.normalise(original_language)
                if not original:
                    return False, "The original language is unknown; refresh metadata."
                if original not in audio_langs:
                    return False, (
                        f"Profile requires original audio ({languages.display(original)}); "
                        f"found {', '.join(languages.labels(audio_langs)) or 'no tagged audio'}."
                    )
            required_audio = languages.resolve_many(self.audio_languages, original_language)
            if required_audio and not languages.matches(required_audio, audio_langs):
                return False, (
                    "Profile requires audio in "
                    f"{', '.join(languages.labels(required_audio))}; found "
                    f"{', '.join(languages.labels(audio_langs)) or 'none'}."
                )

            if self.subtitle_mode == "none" and subtitle_tracks:
                return False, "Profile does not allow subtitle tracks."
            if self.subtitle_mode == "required" and not subtitle_tracks:
                return False, "Profile requires subtitles; the stream has none."
            if (
                self.subtitle_mode == "required"
                and languages.ORIGINAL in self.subtitle_languages
                and not languages.normalise(original_language)
            ):
                return False, "The original language is unknown; refresh metadata."
            required_subs = languages.resolve_many(
                self.subtitle_languages, original_language
            )
            if self.subtitle_mode == "required" and required_subs and not languages.matches(
                required_subs, subtitle_langs
            ):
                return False, (
                    "Profile requires subtitles in "
                    f"{', '.join(languages.labels(required_subs))}; found "
                    f"{', '.join(languages.labels(subtitle_langs)) or 'none'}."
                )
        return True, ""

    def cutoff_met_for(self, release: Any) -> bool:
        return self.cutoff_met(release)


# ---------------------------------------------------------------- selection
def choose_release(
    releases: Iterable[Mapping[str, Any]],
    profile: Profile,
    *,
    current: Any = None,
    original_language: Any = None,
) -> Mapping[str, Any] | None:
    """Pick the release to grab, or ``None`` when nothing should be done.

    No current file: the best allowed release wins. With a file: do nothing
    unless the profile allows upgrades and the file has not reached the
    cutoff, then take an allowed release in a better tier. The cutoff is a
    stopping point, not a ceiling.
    """
    allowed = [
        release for release in releases
        if profile.check(release, original_language)[0]
    ]
    if not allowed:
        return None

    current_view = _release_view(current)
    if current:
        if not profile.upgrades or profile.cutoff_met(current_view):
            return None
        current_rank = profile.rank(current_view, allow_unknown_source=True)
        if current_rank is None:
            # A file outside every tier is still upgradeable to anything the
            # profile allows; the user can decide in the profile.
            current_rank = len(profile.tiers)
        allowed = [
            release for release in allowed
            if (profile.rank(release) is not None
                and profile.rank(release) < current_rank)
        ]
        if not allowed:
            return None

    def sort_key(release: Mapping[str, Any]) -> tuple:
        rank = profile.rank(release)
        probe = release.get("probe") if isinstance(release.get("probe"), Mapping) else {}
        size = probe.get("size") or release.get("size") or 0
        return (
            rank if rank is not None else len(profile.tiers),
            RANK.get(release.get("resolution", "unknown"), len(RESOLUTIONS)),
            -float(release.get("score") or 0.0),
            -int(size or 0),
            str(release.get("name") or "").casefold(),
        )

    return min(allowed, key=sort_key)


# --------------------------------------------------------------- validation
def _validate_source_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part for part in raw.replace(";", ",").split(",") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Sources must be a list.")
    sources = []
    for value in raw:
        label = str(value or "").strip().lower()
        if label not in SOURCE_LABELS:
            raise ValueError(f"Unknown source {value!r}.")
        if label not in sources:
            sources.append(label)
    return [source for source in SOURCES if source in sources]


def validate_profile(
    qualities: Any,
    cutoff: Any = None,
    upgrades: Any = True,
    *,
    audio_mode: Any = "any",
    audio_languages: Any = None,
    subtitle_mode: Any = "any",
    subtitle_languages: Any = None,
    probe_required: Any = True,
) -> dict:
    """Validate a profile payload. Raises ``ValueError`` with a reason."""
    tiers = _decode_tiers(qualities)
    if not tiers:
        raise ValueError("Add at least one quality tier.")
    # Rebuild each tier through validation so the stored JSON is canonical.
    cleaned = []
    for tier in tiers:
        sources = _validate_source_list(tier.sources)
        cleaned.append({
            "name": tier.label[:80],
            "resolution": tier.resolution,
            "sources": sources,
        })

    cutoff_raw = str(cutoff or "").strip()
    cutoff_label = None
    if cutoff_raw:
        for tier in cleaned:
            if tier["name"].casefold() == cutoff_raw.casefold():
                cutoff_label = tier["name"]
                break
        if cutoff_label is None:
            for tier in cleaned:
                if tier["resolution"] == cutoff_raw.lower():
                    cutoff_label = tier["name"]
                    break
        if cutoff_label is None:
            raise ValueError("The cutoff must be one of the quality tiers.")

    audio_mode = str(audio_mode or "any").strip().lower()
    if audio_mode not in AUDIO_MODES:
        raise ValueError("Unknown audio requirement.")
    subtitle_mode = str(subtitle_mode or "any").strip().lower()
    if subtitle_mode not in SUBTITLE_MODES:
        raise ValueError("Unknown subtitle requirement.")
    subtitle_values = subtitle_languages
    if isinstance(subtitle_values, str):
        subtitle_values = [
            part for part in subtitle_values.replace(";", ",").split(",") if part.strip()
        ]
    if not isinstance(subtitle_values, (list, tuple)):
        subtitle_values = []
    subtitle_codes = []
    for value in subtitle_values:
        if str(value).strip().lower() == languages.ORIGINAL:
            label = languages.ORIGINAL
        else:
            label = languages.normalise(value)
        if label and label not in subtitle_codes:
            subtitle_codes.append(label)
    return {
        "qualities": cleaned,
        "cutoff": cutoff_label,
        "upgrades": bool(upgrades),
        "audio_mode": audio_mode,
        "audio_languages": languages.normalise_many(audio_languages),
        "subtitle_mode": subtitle_mode,
        "subtitle_languages": subtitle_codes,
        "probe_required": bool(probe_required),
    }


def decode_language_list(raw: Any) -> list[str]:
    values = raw
    if isinstance(raw, str):
        try:
            values = json.loads(raw or "[]")
        except (TypeError, ValueError):
            values = []
    if not isinstance(values, (list, tuple)):
        return []
    return [str(value) for value in values if value is not None]


def profile_dict(profile: Profile) -> dict:
    return {
        "qualities": [tier.to_dict() for tier in profile.tiers],
        "cutoff_index": profile.cutoff_index,
        "cutoff": (
            profile.tiers[profile.cutoff_index].name
            if profile.cutoff_index is not None
            and 0 <= profile.cutoff_index < len(profile.tiers)
            else None
        ),
        "upgrades": profile.upgrades,
        "audio_mode": profile.audio_mode,
        "audio_languages": list(profile.audio_languages),
        "subtitle_mode": profile.subtitle_mode,
        "subtitle_languages": list(profile.subtitle_languages),
        "probe_required": profile.probe_required,
    }


def profile_summary(row: Mapping[str, Any]) -> dict:
    """A row as JSON-ready profile data for the API and templates."""
    def get(key, default=None):
        keys = row.keys() if hasattr(row, "keys") else row
        return row[key] if key in keys else default

    profile = Profile.from_row(row)
    data = dict(row) if hasattr(row, "keys") else dict(row)
    data.update(profile_dict(profile))
    data["resolutions"] = list(dict.fromkeys(
        tier.resolution for tier in profile.tiers
    ))
    data["audio_languages"] = decode_language_list(get("audio_languages"))
    data["subtitle_languages"] = decode_language_list(get("subtitle_languages"))
    data["upgrades"] = profile.upgrades
    data["probe_required"] = profile.probe_required
    return data


__all__ = [
    "AUDIO_MODES",
    "LOW_QUALITY_SOURCES",
    "Profile",
    "RANK",
    "RESOLUTIONS",
    "SOURCE_CHOICES",
    "SOURCE_LABELS",
    "SOURCES",
    "SUBTITLE_MODES",
    "Tier",
    "choose_release",
    "decode_language_list",
    "decode_qualities",
    "decode_resolutions",
    "parse_resolution",
    "parse_source",
    "profile_dict",
    "profile_summary",
    "resolution_from_dimensions",
    "resolution_from_height",
    "source_label",
    "validate_profile",
]
