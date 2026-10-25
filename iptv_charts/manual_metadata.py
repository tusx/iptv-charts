"""Manual metadata replacement for an existing movie or series.

The movie and series detail pages keep their own edit/search controls. This
module supplies the two extra operations they need: search TMDB by a title the
user types, and replace an item's metadata identity with a chosen TMDB result.

Replacement is deliberately narrow. It changes the identity and descriptive
columns only; the local file, its probe facts, imported timestamps, download
rows, monitoring choice and quality profile all stay exactly as they were.
Series episodes are refreshed for the new identity through the existing
upsert, which keeps rows and files that disappeared from the provider rather
than deleting them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

from . import db, library, metadata

logger = logging.getLogger("iptv_charts.manual_metadata")

#: Section kind -> (table, route-friendly name).
KINDS = {"movie": "movies", "series": "series"}


class MetadataConflict(ValueError):
    """The requested TMDB identity is already attached to another item."""


def _now() -> int:
    return int(time.time())


def _json_list(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(list(value), ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def normalise_kind(value) -> str:
    kind = str(value or "").strip().lower()
    if kind == "tv":
        kind = "series"
    if kind not in KINDS:
        raise ValueError("Choose the movie or series metadata.")
    return kind


def parse_tmdb_id(value) -> int:
    try:
        tmdb_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Choose a TMDB result.") from exc
    if tmdb_id <= 0:
        raise ValueError("Choose a TMDB result.")
    return tmdb_id


def _item(conn, kind: str, item_id: int) -> dict:
    row = (library.get_movie(conn, int(item_id)) if kind == "movie"
           else library.get_series(conn, int(item_id)))
    if row is None:
        raise LookupError(
            "That movie no longer exists." if kind == "movie"
            else "That series no longer exists."
        )
    return row


def _duplicate(conn, kind: str, tmdb_id: int, item_id: int) -> dict | None:
    row = db.get_one(
        conn,
        f"SELECT id, title FROM {KINDS[kind]} WHERE tmdb_id = ? AND id <> ?",
        (int(tmdb_id), int(item_id)),
    )
    return dict(row) if row else None


def _annotate(conn, kind: str, result: dict) -> dict:
    item = dict(result)
    item["poster"] = metadata.image_url("w342", item.get("poster_path"))
    item["backdrop"] = metadata.image_url("w780", item.get("backdrop_path"))
    existing = db.get_one(
        conn, f"SELECT id, title FROM {KINDS[kind]} WHERE tmdb_id = ?",
        (int(item["tmdb_id"]),),
    )
    item["in_library"] = existing is not None
    item["library_id"] = int(existing["id"]) if existing else None
    item["library_title"] = existing["title"] if existing else None
    return item


def search(conn, kind, query: str, year: int | None = None,
           limit: int = 12) -> list[dict]:
    """TMDB search results annotated with local-library ownership."""
    kind = normalise_kind(kind)
    query = " ".join(str(query or "").split())[:120]
    if not query:
        raise ValueError("Type a title to search.")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("The year must be a number.") from exc
    results = (metadata.search_movies(conn, query, year) if kind == "movie"
               else metadata.search_series(conn, query, year))
    return [_annotate(conn, kind, result) for result in list(results)[:limit]]


def preview(conn, kind, tmdb_id) -> dict:
    """Full-ish TMDB details for one result, for the confirmation card."""
    kind = normalise_kind(kind)
    tmdb_id = parse_tmdb_id(tmdb_id)
    details = (metadata.movie_details(conn, tmdb_id) if kind == "movie"
               else metadata.series_details(conn, tmdb_id))
    item = _annotate(conn, kind, details)
    item["kind"] = kind
    return item


def _apply_movie(conn, movie: dict, tmdb_id: int) -> dict:
    details = metadata.movie_details(conn, tmdb_id, refresh=True)
    try:
        conn.execute(
            """
            UPDATE movies SET
                tmdb_id = ?, imdb_id = ?, title = ?, original_title = ?,
                original_language = ?, alt_titles = ?, year = ?, overview = ?,
                runtime = ?, genres = ?, poster_path = ?, backdrop_path = ?,
                release_date = ?, rating = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(tmdb_id), details.get("imdb_id"), details["title"],
                details.get("original_title"), details.get("original_language"),
                _json_list(details.get("alt_titles") or []),
                details.get("year"), details.get("overview"),
                details.get("runtime"),
                _json_list(details.get("genres") or []),
                details.get("poster_path"), details.get("backdrop_path"),
                details.get("release_date"), details.get("rating"),
                details.get("status"), _now(), int(movie["id"]),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise MetadataConflict(
            "That TMDB movie is already in your library."
        ) from exc
    library.add_history(
        conn, "metadata", details["title"], kind="movie", movie_id=int(movie["id"]),
        message=f"Metadata replaced from TMDB {int(tmdb_id)}.",
    )
    updated = library.get_movie(conn, int(movie["id"]))
    return {
        "kind": "movie", "movie": updated, "item": updated,
        "tmdb_id": int(tmdb_id), "episodes_refreshed": None,
    }


def _apply_series(conn, series: dict, tmdb_id: int) -> dict:
    details = metadata.series_details(conn, tmdb_id, refresh=True)
    try:
        conn.execute(
            """
            UPDATE series SET
                tmdb_id = ?, tvdb_id = ?, title = ?, original_title = ?,
                original_language = ?, alt_titles = ?, year = ?, overview = ?,
                genres = ?, poster_path = ?, backdrop_path = ?,
                first_air_date = ?, status = ?, rating = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(tmdb_id), details.get("tvdb_id"), details["title"],
                details.get("original_title"), details.get("original_language"),
                _json_list(details.get("alt_titles") or []),
                details.get("year"), details.get("overview"),
                _json_list(details.get("genres") or []),
                details.get("poster_path"), details.get("backdrop_path"),
                details.get("first_air_date"), details.get("status"),
                details.get("rating"), _now(), int(series["id"]),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise MetadataConflict(
            "That TMDB series is already in your library."
        ) from exc
    library.add_history(
        conn, "metadata", details["title"], kind="series",
        series_id=int(series["id"]),
        message=f"Metadata replaced from TMDB {int(tmdb_id)}.",
    )
    updated = library.get_series(conn, int(series["id"]))
    refreshed = True
    warning = None
    try:
        library.refresh_series_episodes(conn, updated, refresh=True)
    except Exception as exc:  # a provider outage must not undo the identity
        refreshed = False
        warning = (
            "The new identity was saved, but the episode list could not be "
            "refreshed. Use Refresh metadata when the provider is available."
        )
        logger.warning("manual metadata: episode refresh failed for series %s: %s",
                       series["id"], exc)
    try:
        db.seed_bworker_work(conn)
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("manual metadata: bworker seeding skipped: %s", exc)
    updated = library.get_series(conn, int(series["id"]))
    result = {
        "kind": "series", "series": updated, "item": updated,
        "tmdb_id": int(tmdb_id), "episodes_refreshed": refreshed,
    }
    if warning:
        result["warning"] = warning
    return result


def apply(conn, kind, item_id, tmdb_id) -> dict:
    """Replace an item's identity, preserving its file and download state.

    Refuses a TMDB identity that is already attached to another library item.
    Applying the identity the item already has refreshes its descriptive
    metadata without changing the id and reports ``changed=False``.
    """
    kind = normalise_kind(kind)
    tmdb_id = parse_tmdb_id(tmdb_id)
    item = _item(conn, kind, int(item_id))
    duplicate = _duplicate(conn, kind, tmdb_id, int(item["id"]))
    if duplicate is not None:
        label = "movie" if kind == "movie" else "series"
        raise MetadataConflict(
            f"That TMDB {label} is already in your library as "
            f"“{duplicate['title']}”."
        )
    changed = int(item["tmdb_id"]) != tmdb_id
    result = (_apply_movie(conn, item, tmdb_id) if kind == "movie"
              else _apply_series(conn, item, tmdb_id))
    result["changed"] = changed
    return result


__all__ = [
    "KINDS",
    "MetadataConflict",
    "apply",
    "normalise_kind",
    "parse_tmdb_id",
    "preview",
    "search",
]
