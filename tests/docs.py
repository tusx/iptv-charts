#!/usr/bin/env python3
"""Static consistency checks for the 0.8.0 documentation.

This suite does not render the app. It reads the version, README, changelog,
top-level docs and dependency comments and checks that the release story is
present and internally consistent:

* the package version and a 0.8.0 changelog section agree;
* the changelog records the destructive profile reset and the new workflows;
* README covers the bworker, weighted defaults, split settings, Explore /
  Import / Storage, container topology, verified updates and backup format;
* user documentation contains no stale repository name, Flatpak build notes,
  or legacy quality-tier/cutoff claims that contradict weighted profiles;
* every local documentation link resolves to an existing file.

Run with:  .venv/bin/python tests/docs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"

CHECKS = 0
FAILURES: list[str] = []


def check(ok: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILURES.append(label)


def load(path: Path) -> str:
    rel = path.relative_to(ROOT)
    check(path.is_file(), f"{rel} exists")
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        check(False, f"{rel} is readable UTF-8 ({exc})")
        return ""


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def contains(text: str, terms: list[str], label: str) -> None:
    haystack = _squash(text)
    missing = [term for term in terms if _squash(term) not in haystack]
    check(not missing, f"{label} is missing: {', '.join(missing)}")


def contains_ci(text: str, terms: list[str], label: str) -> None:
    haystack = _squash(text).casefold()
    missing = [term for term in terms if _squash(term).casefold() not in haystack]
    check(not missing, f"{label} is missing: {', '.join(missing)}")


def matches(text: str, pattern: str, label: str, *,
            ignore_case: bool = True) -> None:
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    check(re.search(pattern, text, flags) is not None,
          f"{label} (no match for {pattern!r})")


def offenses(texts: dict[str, str],
             patterns: list[tuple[str, str]]) -> list[str]:
    found: list[str] = []
    for rel, text in texts.items():
        for pattern, description in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                line = text.count("\n", 0, match.start()) + 1
                found.append(
                    f"{rel}:{line} {description} ({match.group(0)!r})")
    return found


# --------------------------------------------------------------------- files
README = load(ROOT / "README.md")
CHANGELOG = load(ROOT / "CHANGELOG.md")
REQUIREMENTS = load(ROOT / "requirements.txt")

REQUIRED_DOCS = [
    "PLAYLISTS_AND_SETTINGS.md",
    "QUALITY_AND_CONNECTIONS.md",
    "MOVIE_CANDIDATES.md",
    "WORKER.md",
    "METADATA_PROVIDERS.md",
    "RELEASING.md",
    "UPGRADING.md",
]
DOC_TEXTS: dict[str, str] = {}
for path in sorted(DOCS_DIR.glob("*.md")):
    DOC_TEXTS[path.name] = load(path)
for name in REQUIRED_DOCS:
    check(name in DOC_TEXTS, f"docs/{name} exists")

WORKER = DOC_TEXTS.get("WORKER.md", "")
QUALITY = DOC_TEXTS.get("QUALITY_AND_CONNECTIONS.md", "")
PLAYLISTS = DOC_TEXTS.get("PLAYLISTS_AND_SETTINGS.md", "")
CANDIDATES = DOC_TEXTS.get("MOVIE_CANDIDATES.md", "")
METADATA = DOC_TEXTS.get("METADATA_PROVIDERS.md", "")
RELEASING = DOC_TEXTS.get("RELEASING.md", "")
UPGRADING = DOC_TEXTS.get("UPGRADING.md", "")

# ------------------------------------------------------------------- version
init_text = load(ROOT / "iptv_charts" / "__init__.py")
version_match = re.search(
    r"""^__version__\s*=\s*["']([^"']+)["']""", init_text, re.MULTILINE)
version = version_match.group(1) if version_match else None
check(version == "0.8.0",
      f"iptv_charts.__version__ is 0.8.0 (got {version!r})")

check("0.8.0" in README, "README identifies the 0.8.0 release")
changelog_match = re.search(
    r"(?ms)^## 0\.8\.0\b.*?(?=^## |\Z)", CHANGELOG)
check(changelog_match is not None,
      "CHANGELOG.md has a '## 0.8.0' section")
release_notes = changelog_match.group(0) if changelog_match else ""
contains(release_notes,
         ["### Added", "### Changed", "### Removed", "### Fixed",
          "### Upgrade notes"],
         "CHANGELOG 0.8.0 section headings")
contains_ci(release_notes,
            ["bworker", "weighted", "immutable", "1080p", "format v2",
             "download.and.deploy.sh"],
            "CHANGELOG 0.8.0 feature notes")
matches(release_notes, r"profile reset is destructive",
        "CHANGELOG 0.8.0 marks the profile reset destructive")
matches(release_notes,
        r"deletes every existing (?:quality )?profile",
        "CHANGELOG 0.8.0 says migration deletes old profiles")
matches(release_notes,
        r"assigns every movie and (?:every )?series to .{0,40}1080p",
        "CHANGELOG 0.8.0 says items fall back to 1080p")
check("iptv-info" not in release_notes.casefold(),
      "CHANGELOG 0.8.0 has no stale iptv-info repository name")

# -------------------------------------------------------------------- README
contains_ci(README,
            ["bworker", "worker_jobs", "bworker_work",
             "pause", "resume", "reset failed", "retr", "backoff",
             "no run-now"],
            "README bworker controls")
contains_ci(README,
            ["weighted quality profiles", "weighted scoring profiles",
             "custom profile builder", "4K", "1080p", "720p", "immutable",
             "target", "maximum", "target_met"],
            "README weighted profile story")
matches(README,
        r"external player[^\n]{0,60}off by default",
        "README says external player is off by default")
contains(README,
         ["/settings", "/playlists#iptv-settings", "/movies/settings",
          "/series/settings"],
         "README split settings routes")
matches(README, r"settings are deliberately split",
        "README explains why settings are split")
contains_ci(README,
            ["missing", "explore", "import existing", "manual metadata",
             "storage", "library / wanted / calendar", "top bar is the brand",
             "responsive"],
            "README home/navigation and library tools")
contains_ci(README,
            ["nginx", "gunicorn", "valkey", "supervisord", "four processes",
             "restart always", "loopback", "no authentication"],
            "README container and security story")
contains(README,
         ["build.deploy.sh", "deploy.dev.build.sh",
          "download.and.deploy.sh"],
         "README deployment scripts")
contains_ci(README,
            ["download.and.deploy.sh", "sha-256", "checksum",
             "/api/update/status", "update check", "manual update"],
            "README update story")
contains_ci(README,
            ["format is **v2**", "v1 backup", "profile reset is destructive",
             "schema", "v5"],
            "README upgrade and backup story")
check("CDN" in README, "README states the no-CDN design constraint")
check("tusx/iptv-charts" in README,
      "README points at the tusx/iptv-charts release repository")
for name in REQUIRED_DOCS:
    check(f"](docs/{name})" in README,
          f"README links docs/{name}")

# --------------------------------------------------------------- doc content
contains(WORKER,
         ["worker_jobs", "bworker_work", "bworker_state",
          "bworker_policies", "bworker_events", "worker.lock",
          "iptv-charts:activity", "heartbeat", "stage", "backoff_multiplier",
          "claim", "Only one unit runs at a time", "bounded"],
         "docs/WORKER.md covers the bworker contract")
matches(WORKER, r"web process[^\n]{0,80}(?:only reads|never appends)",
        "docs/WORKER.md says only the bworker writes activity")

contains_ci(QUALITY,
            ["weighted", "base score", "resolution", "target_met", "max_score",
             "target_score", "upgrades", "awaiting_upgrade",
             "current file is re-scored", "probe_batch_per_loop",
             "candidate_batch_size", "source/quality"],
            "docs/QUALITY_AND_CONNECTIONS.md covers weighted scoring")
contains_ci(QUALITY,
            ["metadata", "movie", "series", "settings"],
            "docs/QUALITY_AND_CONNECTIONS.md points at settings ownership")

contains_ci(PLAYLISTS,
            ["split settings", "/settings", "/playlists#iptv-settings",
             "/movies/settings", "/series/settings", "external player",
             "off by default", "atomic"],
            "docs/PLAYLISTS_AND_SETTINGS.md covers page and settings ownership")

contains_ci(CANDIDATES,
            ["identity first", "tmdb", "imdb", "keyset", "id >",
             "candidate_batch_size", "default **25**", "maximum 200",
             "at most **3**", "probe_batch_per_loop", "one ffprobe",
             "continuation", "bworker_candidates", "bworker_scan"],
            "docs/MOVIE_CANDIDATES.md covers ID-first bounded acquisition")

contains_ci(METADATA,
            ["/trending/movie", "/movie/popular", "/movie/top_rated",
             "/movie/upcoming", "/movie/now_playing", "/trending/tv",
             "/tv/popular", "/tv/top_rated", "/tv/on_the_air",
             "/tv/airing_today", "15 minutes", "media_type",
             "external_ids", "cdn"],
            "docs/METADATA_PROVIDERS.md covers the Explore feeds")

contains_ci(RELEASING,
            ["v0.8.0", "/api/update/status", "download.and.deploy.sh",
             "sha-256", "six-hour"],
            "docs/RELEASING.md covers the release and update surfaces")
matches(RELEASING, r"0\.8\.0 does \*\*not\*\* add an update card",
        "docs/RELEASING.md says 0.8.0 has no update UI")

contains_ci(UPGRADING,
            ["profile reset is destructive", "every existing quality profile",
             "1080p", "schema v4", "v5", "backup",
             "downgrading is not supported", "pause the bworker",
             "external player is off by default"],
            "docs/UPGRADING.md covers the destructive upgrade")
matches(UPGRADING, r"format\s+\*\*v1\*\* backups remain readable",
        "docs/UPGRADING.md describes v1 backup compatibility")

# --------------------------------------------------------- stale references
all_docs = {"README.md": README, "CHANGELOG.md": CHANGELOG,
            "requirements.txt": REQUIREMENTS}
all_docs.update({f"docs/{name}": text for name, text in DOC_TEXTS.items()})

old_repo = offenses(all_docs, [(r"\biptv-info\b", "old repository name")])
check(not old_repo,
      "no stale iptv-info repository references: " + ", ".join(old_repo[:5]))

flatpak = offenses(all_docs, [
    (r"flatpak-pip-generator", "old Flatpak pip generator"),
    (r"flatpak/", "Flatpak build path"),
    (r"flatpak\s+build", "Flatpak build instruction"),
    (r"flatpak[- ]based\s+build", "Flatpak-based build"),
    (r"vendored\s+into\s+the\s+flatpak", "vendoring into Flatpak"),
])
check(not flatpak,
      "no stale Flatpak build instructions: " + ", ".join(flatpak[:5]))
check("flatpak" not in REQUIREMENTS.casefold(),
      "requirements.txt has no Flatpak build note")
contains(REQUIREMENTS, ["gunicorn", "redis"],
         "requirements.txt records the container worker dependencies")

# Historical changelog entries legitimately describe the removed model, so
# the legacy vocabulary check covers current user docs only.
user_docs = {"README.md": README}
user_docs.update({f"docs/{name}": text for name, text in DOC_TEXTS.items()})
legacy = offenses(user_docs, [
    (r"\bcutoff\b", "legacy cutoff claim"),
    (r"\baudio_mode\b", "legacy audio_mode field"),
    (r"\bquality\s+tiers?\b", "legacy quality-tier claim"),
    (r"\btiers?\s+of\s+quality\b", "legacy quality-tier claim"),
])
check(not legacy,
      "user docs have no quality-tier/cutoff claims that contradict "
      "weighted profiles: " + ", ".join(legacy[:5]))

# ---------------------------------------------------------------- link check
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
for rel, text in all_docs.items():
    if not rel.endswith(".md"):
        continue
    base = ROOT / rel
    for raw_target in LINK_RE.findall(text):
        target = raw_target.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            continue  # http:, https:, mailto:, ...
        if target.startswith("#"):
            continue
        path_part = unquote(target.split("#", 1)[0].split("?", 1)[0])
        if not path_part:
            continue
        resolved = (ROOT / path_part.lstrip("/")
                    if path_part.startswith("/") else base.parent / path_part)
        check(resolved.exists(),
              f"{rel} link target exists: {target}")

seen_refs: set[str] = set()
for match in re.finditer(r"docs/[A-Za-z0-9_./-]+\.md", README):
    rel_path = match.group(0)
    if rel_path in seen_refs:
        continue
    seen_refs.add(rel_path)
    check((ROOT / rel_path).is_file(),
          f"README doc reference exists: {rel_path}")


def main() -> int:
    if FAILURES:
        print(f"docs: {len(FAILURES)} of {CHECKS} checks failed")
        for failure in FAILURES:
            print(f"  FAIL: {failure}")
        return 1
    print(f"docs: {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
