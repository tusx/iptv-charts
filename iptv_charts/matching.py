"""Matching TMDB/TVDB titles against messy IPTV catalog names.

Provider names are close to release names but not close enough to compare
literally. Real examples from a 313k-row catalog:

    ES - Un talento único [[4K-Q]-Q] (2026)
    EN - Wonder [4K-Q] (2017) JULIA ROBERTS
    FR - Anya  (2020) [1080p]
    [X][WatchMDH] Title [1080p]
    GR - Supernatural [1080p]          (no year at all)

A leading locale prefix, a quality tag, a trailing cast list, and a title that
is itself a number (``1917 (2019)``) all have to survive. The approach is:
strip only things we can recognise, score what is left against the TMDB/TVDB
title and its alternative titles, and keep manual search as the escape hatch.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping

from . import items, quality

#: Resolution words that may appear inside brackets/tags. Kept separate from
#: quality.parse_resolution because this is used to *remove* noise, including
#: tags like "4K-Q" that are not a bare quality label.
_QUALITY_WORD_RE = re.compile(
    r"(?i)(?<![\w])(?:2160p|4k|uhd|1080p|fullhd|fhd|720p|480p|576p|540p|360p)(?![\w])"
)
_QUALITY_STANDALONE_RE = re.compile(
    r"(?i)(?<![\w])(?:2160p|4k|uhd|1080p|fullhd|fhd|720p|480p|576p|540p|360p)(?![\w])"
)
#: A locale prefix such as "ES - ", "EN - ", "NF - ". Three letters covers
#: provider-specific codes; requiring the separator avoids eating "The Matrix".
_LOCALE_PREFIX_RE = re.compile(r"^\s*[A-Z]{2,3}\s*[-–—]\s+")
#: Trailing/leading year in parentheses or brackets, e.g. "(2023)" or "[2024]".
_YEAR_BRACKETED_RE = re.compile(r"[\(\[]\s*(19\d{2}|20\d{2})\s*[\)\]]")
#: A year only when it follows a separator and ends the string, e.g.
#: "- Kwalificatie 2024". A bare trailing number is left alone because it may
#: be part of the title ("Blade Runner 2049", "1917").
_YEAR_SEPARATED_RE = re.compile(r"[-–—,]\s+(19\d{2}|20\d{2})\s*$")
_EPISODE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])S(\d{1,2})\s*E(\d{1,3})(?![0-9])")
_EPISODE_ALT_RE = re.compile(r"(?<!\d)(\d{1,2})x(\d{2,3})(?!\d)")
_LANG_TAG_RE = re.compile(r"[\(\[]\s*[A-Z]{2}\s*[\)\]]")
#: A bracketed group, allowing one level of nesting for "[[4K-Q]-Q]".
_GROUP_RE = re.compile(r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]|\([^()]*\)")
_LEADING_GROUPS_RE = re.compile(r"^\s*(?:\[[^\[\]]{1,24}\]\s*)+")

_ARTICLES = ("the ", "a ", "an ")


@dataclass(frozen=True)
class ParsedName:
    """A provider release name after the recognisable noise is removed."""

    original: str
    title_variants: tuple[str, ...]
    year: int | None
    resolution: str
    source: str
    is_episode: bool
    season: int | None = None
    episode: int | None = None


def _normalise(text: str | None) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().replace("&", " and ").replace("'", " ").replace("’", " ")
    value = re.sub(r"[^\w]+|_", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _add_variants(out: list[str], value: str) -> None:
    normalized = _normalise(value)
    if not normalized:
        return
    if normalized not in out:
        out.append(normalized)
    for article in _ARTICLES:
        if normalized.startswith(article) and len(normalized) > len(article):
            stripped = normalized[len(article):]
            if stripped not in out:
                out.append(stripped)


def title_variants(*titles: Any) -> tuple[str, ...]:
    """Normalise a set of library titles (title, original, aliases)."""
    out: list[str] = []
    for entry in titles:
        if entry is None:
            continue
        if isinstance(entry, (list, tuple)):
            for value in entry:
                _add_variants(out, str(value))
        else:
            _add_variants(out, str(entry))
    return tuple(out)


def _decode_aliases(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(value) for value in raw if value]
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if isinstance(value, list):
        return [str(entry) for entry in value if entry]
    return []


def aliases_from_row(row: Mapping[str, Any]) -> list[str]:
    """The title, original title and alternative titles of a library row."""
    return title_variants(
        row["title"],
        row["original_title"] if "original_title" in row.keys() else None,
        _decode_aliases(row["alt_titles"] if "alt_titles" in row.keys() else None),
    )


def identity_match_mode(conn, row: Mapping[str, Any],
                        kind: str) -> tuple[str | None, str | None]:
    """Resolve TMDB -> IMDb -> title precedence for a library row.

    An id only wins when the catalog actually contains it. This is the single
    entry point used by both the manual candidate lists and the paged engine.
    """
    keys = row.keys() if hasattr(row, "keys") else row
    tmdb = row["tmdb_id"] if "tmdb_id" in keys else None
    imdb = row["imdb_id"] if "imdb_id" in keys else None
    return items.identity_match_mode(conn, kind, tmdb_id=tmdb, imdb_id=imdb)


def classify_catalog_row(
    row: Mapping[str, Any],
    *,
    kind: str,
    titles: Iterable[str],
    year: int | None,
    match_kind: str | None,
    min_score: float,
    identity_value: str | None = None,
) -> dict | None:
    """Decide whether one catalog row matches, and return its release facts.

    ``match_kind`` is ``tmdb``/``imdb`` for an identity scan or ``title`` for
    the fallback. Identity rows are authoritative; a movie row whose name is
    clearly an episode is still skipped because M3U playlists mix episodes
    into their VOD listing.
    """
    keys = row.keys() if hasattr(row, "keys") else row
    parsed = parse_name(str(row["name"] or ""))
    if kind == "vod" and parsed.is_episode:
        return None

    if match_kind in ("tmdb", "imdb"):
        field = "tmdb_id" if match_kind == "tmdb" else "imdb_id"
        value = row[field] if field in keys else None
        if identity_value is not None:
            normalise = (items.normalise_tmdb_id if match_kind == "tmdb"
                         else items.normalise_imdb_id)
            if normalise(value) != normalise(identity_value):
                return None
        match_score = 1.0
    else:
        match_kind = "title"
        match_score = score_name(parsed, titles, year)
        if match_score < min_score:
            return None

    return {
        "parsed_title": parsed.title_variants[0] if parsed.title_variants else "",
        "candidate_year": parsed.year,
        "resolution": parsed.resolution,
        "source": parsed.source,
        "score": match_score,
        "match_kind": match_kind,
        "match_score": match_score,
    }


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple:
    priority = {"tmdb": 0, "imdb": 1, "title": 2}
    return (
        priority.get(str(candidate.get("match_kind") or "title"), 2),
        -float(candidate.get("match_score") or candidate.get("score") or 0.0),
        quality.RANK.get(candidate.get("resolution"), len(quality.RESOLUTIONS)),
        str(candidate.get("name") or "").casefold(),
    )


def _extract_year(name: str) -> tuple[int | None, tuple[int, int] | None]:
    """Return (year, span to remove), preferring a parenthesised year."""
    matches = list(_YEAR_BRACKETED_RE.finditer(name))
    if matches:
        match = matches[-1]
        return int(match.group(1)), match.span()
    match = _YEAR_SEPARATED_RE.search(name)
    if match:
        return int(match.group(1)), match.span(1)
    return None, None


def _remove_quality_groups(text: str) -> str:
    """Remove bracketed groups that contain a quality word, repeatedly."""
    previous = None
    current = text
    while previous != current:
        previous = current

        def replace(match: re.Match[str]) -> str:
            body = match.group(0)
            if _QUALITY_WORD_RE.search(body):
                return " "
            return body

        current = _GROUP_RE.sub(replace, current)
    return current


def _strip_groups(text: str) -> str:
    """Drop leading group tags such as [X][WatchMDH], when a title remains."""
    match = _LEADING_GROUPS_RE.match(text)
    if not match:
        return text
    remainder = text[match.end():]
    return remainder if _normalise(remainder) else text


def parse_name(name: str) -> ParsedName:
    """Parse a provider release name into the pieces matching needs."""
    original = " ".join(str(name or "").split())
    working = _LOCALE_PREFIX_RE.sub("", original)

    episode_match = _EPISODE_RE.search(working) or _EPISODE_ALT_RE.search(working)
    is_episode = episode_match is not None
    season = episode = None
    if episode_match:
        season, episode = int(episode_match.group(1)), int(episode_match.group(2))
        working = working[:episode_match.start()] + " " + working[episode_match.end():]

    year, span = _extract_year(working)
    if span:
        working = working[:span[0]] + " " + working[span[1]:]

    working = _remove_quality_groups(working)
    working = _QUALITY_STANDALONE_RE.sub(" ", working)
    # Two-letter language tags like (FR) or [EN]; only uppercase, so a title
    # such as "[REC]" is left alone.
    working = _LANG_TAG_RE.sub(" ", working)
    working = re.sub(r"\s+", " ", working).strip(" -–—_")

    variants: list[str] = []
    _add_variants(variants, working)
    _add_variants(variants, _strip_groups(working))
    if not variants:
        # A name made only of tags ("[1080p]") is still a name; keep the
        # original rather than returning nothing.
        _add_variants(variants, original)

    return ParsedName(
        original=original,
        title_variants=tuple(variants),
        year=year,
        resolution=quality.parse_resolution(original),
        source=quality.parse_source(original),
        is_episode=is_episode,
        season=season,
        episode=episode,
    )


def similarity(left: str, right: str) -> float:
    """Compare two normalised titles, tolerating extra words in one."""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    ratio = SequenceMatcher(None, left, right).ratio()
    left_words = set(left.split())
    right_words = set(right.split())
    if left_words and right_words:
        shared = left_words & right_words
        union = left_words | right_words
        jaccard = len(shared) / len(union)
        coverage = len(shared) / min(len(left_words), len(right_words))
        token_score = 0.65 * jaccard + 0.35 * coverage
    else:
        token_score = 0.0

    contained = (
        min(len(left), len(right)) >= 4
        and (left in right or right in left)
    )
    return max(ratio, token_score, 0.95 if contained else 0.0)


def score_name(parsed: ParsedName, titles: Iterable[str], year: int | None) -> float:
    """Score one parsed provider name against a library item's titles."""
    candidates = tuple(titles)
    best = max(
        (similarity(candidate, title) for candidate in parsed.title_variants
         for title in candidates),
        default=0.0,
    )

    if year and parsed.year:
        difference = abs(int(year) - int(parsed.year))
        if difference == 0:
            best += 0.10
        elif difference == 1:
            best += 0.04
        else:
            best -= 0.30
    # A provider name with no year is common ("GR - Supernatural [1080p]").
    # It neither helps nor hurts; the title has to carry the match.

    return round(max(0.0, min(best, 1.0)), 4)


def _catalog_rows(
    conn,
    kind: str,
    titles: Iterable[str],
    *,
    query_override: str | None,
    limit: int,
) -> list[dict]:
    queries: list[str] = []
    if query_override:
        queries.append(str(query_override))
    else:
        # Searching with the library's own title is more reliable than using
        # the provider name: the provider may add a locale prefix or cast.
        for title in list(titles)[:4]:
            if title:
                queries.append(title)

    rows: dict[tuple[int, str], dict] = {}
    for query in queries:
        for row in items.search_catalog(conn, kind, query, limit=limit):
            key = (int(row["source_id"]), str(row["remote_id"]))
            rows.setdefault(key, row)
    return list(rows.values())


def movie_candidates(
    conn,
    movie_row: Mapping[str, Any],
    *,
    query_override: str | None = None,
    min_score: float = 0.45,
    limit: int = 200,
) -> list[dict]:
    """Provider VODs that correspond to one library movie.

    A free-text query keeps the user's intent and searches titles. Otherwise
    TMDB is tried, then IMDb, and only when neither exists in the catalog does
    the title/year/alternative-title fallback run.
    """
    titles = aliases_from_row(movie_row)
    year = movie_row["year"] if "year" in movie_row.keys() else None
    mode, value = (None, None)
    if not query_override:
        mode, value = identity_match_mode(conn, movie_row, "vod")
    if mode:
        rows = items.identity_catalog(conn, "vod", mode, str(value), limit=limit)
    else:
        rows = _catalog_rows(conn, "vod", titles,
                             query_override=query_override, limit=limit)
    out: list[dict] = []
    for row in rows:
        classified = classify_catalog_row(
            row, kind="vod", titles=titles, year=year,
            match_kind=mode or "title", min_score=min_score,
            identity_value=value,
        )
        if classified is None:
            continue
        out.append({**row, **classified})
    out.sort(key=_candidate_sort_key)
    return out


def series_candidates(
    conn,
    series_row: Mapping[str, Any],
    *,
    query_override: str | None = None,
    min_score: float = 0.45,
    limit: int = 200,
) -> list[dict]:
    """Provider series rows that plausibly correspond to one library series."""
    titles = aliases_from_row(series_row)
    year = series_row["year"] if "year" in series_row.keys() else None
    mode, value = (None, None)
    if not query_override:
        mode, value = identity_match_mode(conn, series_row, "series")
    if mode:
        rows = items.identity_catalog(conn, "series", mode, str(value), limit=limit)
    else:
        rows = _catalog_rows(conn, "series", titles,
                             query_override=query_override, limit=limit)
    out: list[dict] = []
    for row in rows:
        classified = classify_catalog_row(
            row, kind="series", titles=titles, year=year,
            match_kind=mode or "title", min_score=min_score,
            identity_value=value,
        )
        if classified is None:
            continue
        out.append({**row, **classified})
    out.sort(key=_candidate_sort_key)
    return out


def episode_marker(name: str) -> tuple[int, int] | None:
    """Extract SxxEyy (or 1x02) from a provider episode or filename."""
    match = _EPISODE_RE.search(name or "") or _EPISODE_ALT_RE.search(name or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


__all__ = [
    "ParsedName",
    "aliases_from_row",
    "classify_catalog_row",
    "episode_marker",
    "identity_match_mode",
    "movie_candidates",
    "parse_name",
    "score_name",
    "series_candidates",
    "similarity",
    "title_variants",
]
