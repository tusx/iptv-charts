"""Resolution tiers, quality profiles, and upgrade decisions.

IPTV VOD names carry a resolution tag far more often than they carry a source
or codec label, so profiles here are deliberately simpler than Radarr's: a
profile is an ordered list of allowed resolution tiers plus a cutoff. That is
enough to express "1080p is fine, do not bother with 4K" or "take 4K when it
exists" without pretending the provider tells us BluRay from WEB-DL.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: Best first. ``unknown`` ranks below every explicit tier, because a name
#: with no quality tag is not evidence of a high-quality file.
RESOLUTIONS = ("2160p", "1080p", "720p", "480p", "unknown")
RANK = {name: index for index, name in enumerate(RESOLUTIONS)}

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


def parse_resolution(text: str | None) -> str:
    """Return the resolution named in a release or filename, or ``unknown``."""
    for label, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return label
    return "unknown"


def resolution_from_height(height: int | None) -> str | None:
    """Map an ffprobe frame height to a tier; ``None`` when it is unusable."""
    if not height or height <= 0:
        return None
    for threshold, label in _HEIGHT_TIERS:
        if height >= threshold:
            return label
    return "480p"


def decode_resolutions(raw: Any) -> list[str]:
    """Decode a profile's JSON list, keeping only known, ordered tiers."""
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


@dataclass(frozen=True)
class Profile:
    """A decoded quality profile."""

    resolutions: tuple[str, ...]
    cutoff: str | None = None
    upgrades: bool = True

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Profile":
        resolutions = tuple(decode_resolutions(row["resolutions"]))
        cutoff = (row["cutoff"] or None)
        if cutoff not in resolutions:
            cutoff = None
        return cls(
            resolutions=resolutions,
            cutoff=cutoff,
            upgrades=bool(row["upgrades"]),
        )

    def allows(self, resolution: str | None) -> bool:
        return (resolution or "unknown") in self.resolutions

    @property
    def cutoff_rank(self) -> int:
        """Rank of the cutoff; below the worst tier when unset."""
        if self.cutoff is None:
            return len(RESOLUTIONS) - 1
        return RANK[self.cutoff]

    def cutoff_met(self, resolution: str | None) -> bool:
        if self.cutoff is None:
            return False
        return RANK.get(resolution or "unknown", len(RESOLUTIONS) - 1) <= self.cutoff_rank


def choose_release(
    releases: Iterable[Mapping[str, Any]],
    profile: Profile,
    *,
    current: str | None = None,
) -> Mapping[str, Any] | None:
    """Pick the release to grab, or ``None`` when nothing should be done.

    No current file: the best allowed release wins. With a file: do nothing
    unless the profile allows upgrades and the file has not reached the
    cutoff, then take the best allowed release strictly better than what is
    already on disk. The cutoff is a stopping point, not a ceiling -- if a
    2160p release is allowed and the cutoff is 1080p, it is still the best
    release and wins.
    """
    allowed = [
        release for release in releases
        if profile.allows(release.get("resolution"))
    ]
    if not allowed:
        return None

    current_rank = RANK.get(current or "unknown", len(RESOLUTIONS) - 1)

    if current is not None:
        if not profile.upgrades or profile.cutoff_met(current):
            return None
        allowed = [
            release for release in allowed
            if RANK[release.get("resolution", "unknown")] < current_rank
        ]
        if not allowed:
            return None

    return min(
        allowed,
        key=lambda release: (
            RANK[release.get("resolution", "unknown")],
            -float(release.get("score") or 0.0),
        ),
    )


def validate_profile(
    resolutions: Any,
    cutoff: Any = None,
    upgrades: Any = True,
) -> tuple[list[str], str | None, bool]:
    """Validate user input for a profile. Raises ``ValueError`` with a reason."""
    if not isinstance(resolutions, (list, tuple)):
        raise ValueError("Choose at least one resolution.")
    cleaned = []
    for value in resolutions:
        label = str(value or "").strip().lower()
        if label not in RANK:
            raise ValueError(f"Unknown resolution {value!r}.")
        if label not in cleaned:
            cleaned.append(label)
    if not cleaned:
        raise ValueError("Choose at least one resolution.")
    cleaned = [name for name in RESOLUTIONS if name in cleaned]

    cutoff_label = str(cutoff or "").strip().lower() or None
    if cutoff_label is not None and cutoff_label not in cleaned:
        raise ValueError("The cutoff must be one of the allowed resolutions.")

    return cleaned, cutoff_label, bool(upgrades)


__all__ = [
    "Profile",
    "RANK",
    "RESOLUTIONS",
    "choose_release",
    "decode_resolutions",
    "parse_resolution",
    "resolution_from_height",
    "validate_profile",
]
