"""Querying catalog items and turning them into playable URLs.

The interesting parts here are search and URL construction.

**Search** runs against the FTS5 index, which tokenises text into words. That
is what makes it fast over hundreds of thousands of rows, but it also means a
raw user string is not a valid query -- `"`, `-` and `*` all have meaning, and
a bare word only matches whole tokens. So input is tokenised and rebuilt as a
quoted prefix query, which is both safe and closer to what people expect
(typing `matr` finds `The Matrix`).

**Why CROSS JOIN.** This one is worth stating plainly, because the obvious
query is catastrophically slow and nothing about it looks wrong:

    SELECT ... FROM items_fts f JOIN items i ON i.id = f.rowid
    WHERE items_fts MATCH ? AND i.source_id = ?

Against a real 313k-item catalog that took **20 seconds**. The planner chose
to drive from `items` -- scanning every row of the source via an index -- and
probe the FTS index once per row, i.e. 313,247 probes to find 866 matches.
CROSS JOIN removes the planner's freedom to reorder, pinning the FTS match as
the outer loop, which turns it into 866 primary-key lookups. The same query
then runs in 3 ms.

The cost is that the join condition has to move into the WHERE clause, since
CROSS JOIN takes no ON clause.

**URLs** differ by source type. An M3U item's `remote_id` *is* its URL, while
an Xtream item's is an opaque id that has to be reassembled into a path. The
one genuine trap is that a series is not playable on its own: the catalog
stores the series id, but playback needs an episode id from a separate call.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Mapping
from urllib.parse import quote

from . import db

#: Matches unicode word characters. Everything else is treated as a separator,
#: which is what keeps arbitrary punctuation out of the FTS query.
_WORD_RE = re.compile(r"\w+", re.UNICODE)

SORTS = {
    "name": "i.name COLLATE NOCASE ASC",
    "newest": "(i.added_ts IS NULL) ASC, i.added_ts DESC, i.name COLLATE NOCASE ASC",
    "rating": "(i.rating IS NULL) ASC, i.rating DESC, i.name COLLATE NOCASE ASC",
}
DEFAULT_SORT = "name"


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 prefix query.

    Returns None when there is nothing usable, so callers can skip the MATCH
    clause entirely rather than searching for an empty string.
    """
    tokens = _WORD_RE.findall(text or "")
    if not tokens:
        return None
    # Quote each token so punctuation cannot be read as FTS syntax, and append
    # * so a partial word matches: "matr" should find "The Matrix".
    return " ".join(f'"{token}"*' for token in tokens)


def _like_pattern(text: str) -> str:
    """Escape LIKE wildcards so a literal % does not match everything."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def query_items(conn: sqlite3.Connection, source_id: int, *, text: str = "",
                kind: str = "", category_id: int | None = None,
                sort: str = DEFAULT_SORT, limit: int = 60,
                offset: int = 0) -> dict:
    """Return one page of items plus the total matching count."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    clauses: list[str] = []
    params: list = []

    # Build the MATCH clause first: parameters bind in the order their
    # placeholders appear, and MATCH sits at the front of the WHERE.
    fts = fts_query(text) if text else None
    if fts:
        # CROSS JOIN, not JOIN: it stops the planner driving from `items` and
        # probing the FTS index once per row. See the module docstring -- the
        # difference against a 313k catalog is 20 seconds versus 3 ms. The
        # join condition has to live in the WHERE clause because CROSS JOIN
        # does not accept ON.
        table = "items_fts f CROSS JOIN items i"
        clauses.append("items_fts MATCH ?")
        params.append(fts)
        clauses.append("i.id = f.rowid")
    else:
        table = "items i"
        if text:
            # No usable tokens (punctuation only, say) -- fall back to a
            # substring match rather than returning nothing.
            clauses.append("i.name LIKE ? ESCAPE '\\'")
            params.append(_like_pattern(text))

    clauses.append("i.source_id = ?")
    params.append(source_id)

    if kind in ("live", "vod", "series"):
        clauses.append("i.kind = ?")
        params.append(kind)

    if category_id:
        clauses.append("i.category_id = ?")
        params.append(category_id)

    where = " AND ".join(clauses)
    order = SORTS.get(sort, SORTS[DEFAULT_SORT])

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params
    ).fetchone()["n"]

    rows = conn.execute(
        f"""
        SELECT i.id, i.kind, i.remote_id, i.name, i.logo, i.category_id,
               i.container_ext, i.added_ts, i.rating, i.epg_channel_id,
               i.tv_archive, i.duration, i.plot, i.genre, i.release_date,
               c.name AS category_name
        FROM {table}
        LEFT JOIN categories c ON c.id = i.category_id
        WHERE {where}
        ORDER BY {order}
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()

    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


#: Real catalogs are not tidy. One measured sample had 1,388 categories, which
#: is unusable as a dropdown and pointless to render -- the long tail holds a
#: handful of items each and is far better served by the search box. The list
#: is ordered by size, so a cap keeps the ones people actually filter by.
DEFAULT_CATEGORY_LIMIT = 300


def search_catalog(conn: sqlite3.Connection, kind: str, text: str, *,
                   limit: int = 200) -> list[dict]:
    """Search the whole catalog for one kind, across every playlist.

    Used by the library's release matcher: a movie is not tied to the playlist
    it happens to be browsed from, so the per-source query above is the wrong
    shape. The CROSS JOIN is the same planner-defeating trick described in the
    module docstring; without it, a title search over 300k rows takes seconds.
    """
    if kind not in ("live", "vod", "series"):
        raise ValueError(f"Unknown item kind {kind!r}.")
    limit = max(1, min(int(limit), 500))
    fts = fts_query(text)
    if not fts:
        return []

    params: list = [fts, kind]
    rows = conn.execute(
        """
        SELECT i.id, i.source_id, i.kind, i.remote_id, i.name, i.logo,
               i.container_ext, i.request_headers, i.added_ts,
               s.name AS source_name, s.kind AS source_kind
        FROM items_fts f
        CROSS JOIN items i
        CROSS JOIN sources s
        WHERE items_fts MATCH ?
          AND i.id = f.rowid
          AND i.kind = ?
          AND s.id = i.source_id
        ORDER BY i.name COLLATE NOCASE
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------- identities
#: Provider payloads are inconsistent: ids arrive as ints, numeric strings,
#: empty placeholders or URLs. Only an unambiguous value is accepted -- an
#: invented identity is worse than no identity because it silently wins over
#: title matching.
_TMDB_RE = re.compile(r"^\d{1,12}$")
_IMDB_RE = re.compile(r"^(tt\d{5,12})$", re.IGNORECASE)


def normalise_tmdb_id(value) -> str | None:
    text = str(value or "").strip()
    if not _TMDB_RE.match(text) or int(text) <= 0:
        return None
    return text


def normalise_imdb_id(value) -> str | None:
    match = _IMDB_RE.match(str(value or "").strip())
    return match.group(1).lower() if match else None


def identity_from_payload(payload) -> tuple[str | None, str | None]:
    """Pull TMDB/IMDb ids out of a provider list or detail object.

    Handles the shapes seen in the wild (``tmdb_id``/``tmdb``/``imdb_id``)
    without attempting to derive an id from a title or a URL.
    """
    if not hasattr(payload, "get"):
        return None, None
    tmdb = None
    for key in ("tmdb_id", "tmdb", "tmdbid", "themoviedb_id"):
        tmdb = normalise_tmdb_id(payload.get(key))
        if tmdb:
            break
    imdb = None
    for key in ("imdb_id", "imdb", "imdbid"):
        imdb = normalise_imdb_id(payload.get(key))
        if imdb:
            break
    return tmdb, imdb


def identity_match_mode(conn: sqlite3.Connection, kind: str, *,
                        tmdb_id=None, imdb_id=None,
                        source_id: int | None = None) -> tuple[str | None, str | None]:
    """Resolve the highest-precedence identity that actually exists.

    TMDB wins over IMDb, and IMDb wins over a title search. An id is only
    chosen when at least one catalog row carries it, so a movie with ids the
    provider never supplied still falls back to titles.
    """
    if kind not in ("live", "vod", "series"):
        raise ValueError(f"Unknown item kind {kind!r}.")
    tmdb = normalise_tmdb_id(tmdb_id)
    imdb = normalise_imdb_id(imdb_id)
    clauses = ["kind = ?"]
    params: list = [kind]
    if source_id is not None:
        clauses.append("source_id = ?")
        params.append(int(source_id))
    where = " AND ".join(clauses)
    if tmdb:
        row = conn.execute(
            f"SELECT 1 FROM items WHERE {where} AND tmdb_id = ? LIMIT 1",
            [*params, tmdb],
        ).fetchone()
        if row is not None:
            return "tmdb", tmdb
    if imdb:
        row = conn.execute(
            f"SELECT 1 FROM items WHERE {where} AND imdb_id = ? LIMIT 1",
            [*params, imdb],
        ).fetchone()
        if row is not None:
            return "imdb", imdb
    return None, None


_CATALOG_COLUMNS = (
    "i.id, i.source_id, i.kind, i.remote_id, i.name, i.logo, "
    "i.container_ext, i.request_headers, i.added_ts, i.tmdb_id, i.imdb_id, "
    "s.name AS source_name, s.kind AS source_kind"
)


def identity_catalog(conn: sqlite3.Connection, kind: str, match_kind: str,
                     value: str, *, limit: int = 200,
                     source_id: int | None = None) -> list[dict]:
    """Catalog rows with an exact provider id (no OFFSET, bounded)."""
    if match_kind not in ("tmdb", "imdb"):
        raise ValueError(f"Unknown identity match kind {match_kind!r}.")
    column = "i.tmdb_id" if match_kind == "tmdb" else "i.imdb_id"
    params: list = [kind, str(value)]
    clause = ""
    if source_id is not None:
        clause = " AND i.source_id = ?"
        params.append(int(source_id))
    rows = conn.execute(
        f"""
        SELECT {_CATALOG_COLUMNS}
        FROM items i CROSS JOIN sources s
        WHERE i.kind = ? AND {column} = ? AND s.id = i.source_id{clause}
        ORDER BY i.id
        LIMIT ?
        """,
        [*params, max(1, min(int(limit), 500))],
    ).fetchall()
    return [dict(row) for row in rows]


def catalog_page(conn: sqlite3.Connection, kind: str, *,
                 after_item_id: int | None = None,
                 tmdb_id=None, imdb_id=None,
                 text_queries: list[str] | tuple[str, ...] | None = None,
                 source_id: int | None = None,
                 limit: int = 100) -> dict:
    """One keyset page of the whole catalog for the acquisition pipeline.

    Every selector uses ``i.id > cursor ORDER BY i.id``; OFFSET never appears.
    Identity selectors and title/FTS selectors may be combined, but the engine
    uses one family at a time so ID matches can win outright. Rows are merged
    and deduplicated by item id, then truncated to ``limit``.
    """
    if kind not in ("live", "vod", "series"):
        raise ValueError(f"Unknown item kind {kind!r}.")
    limit = max(1, min(int(limit), 500))
    cursor = max(0, int(after_item_id or 0))
    rows_by_id: dict[int, dict] = {}
    selectors = 0
    short_selectors = 0

    def collect(sql: str, params: list) -> bool:
        nonlocal selectors, short_selectors
        selectors += 1
        found = conn.execute(sql, params).fetchall()
        if len(found) < limit:
            short_selectors += 1
        for row in found:
            rows_by_id.setdefault(int(row["id"]), dict(row))
        return len(found)

    base_params = [kind, cursor]
    source_clause = ""
    if source_id is not None:
        source_clause = " AND i.source_id = ?"
    tmdb = normalise_tmdb_id(tmdb_id)
    if tmdb:
        params = [*base_params, *( [int(source_id)] if source_id is not None else []), tmdb, limit]
        collect(
            f"""
            SELECT {_CATALOG_COLUMNS}
            FROM items i CROSS JOIN sources s
            WHERE i.kind = ? AND i.id > ?{source_clause}
              AND i.tmdb_id = ? AND s.id = i.source_id
            ORDER BY i.id LIMIT ?
            """,
            params,
        )
    imdb = normalise_imdb_id(imdb_id)
    if imdb:
        params = [*base_params, *( [int(source_id)] if source_id is not None else []), imdb, limit]
        collect(
            f"""
            SELECT {_CATALOG_COLUMNS}
            FROM items i CROSS JOIN sources s
            WHERE i.kind = ? AND i.id > ?{source_clause}
              AND i.imdb_id = ? AND s.id = i.source_id
            ORDER BY i.id LIMIT ?
            """,
            params,
        )

    for text in text_queries or ():
        fts = fts_query(str(text or ""))
        if not fts:
            continue
        params = [fts, kind, cursor]
        if source_id is not None:
            params.append(int(source_id))
        params.append(limit)
        collect(
            f"""
            SELECT {_CATALOG_COLUMNS}
            FROM items_fts f CROSS JOIN items i CROSS JOIN sources s
            WHERE items_fts MATCH ? AND i.id = f.rowid AND i.kind = ?
              AND i.id > ?{source_clause} AND s.id = i.source_id
            ORDER BY i.id LIMIT ?
            """,
            params,
        )

    ordered = [rows_by_id[key] for key in sorted(rows_by_id)][:limit]
    next_cursor = int(ordered[-1]["id"]) if ordered else (str(cursor) if cursor else None)
    exhausted = not rows_by_id or (
        short_selectors == selectors and len(rows_by_id) < limit
    )
    if not selectors:
        exhausted = True
    return {
        "items": ordered,
        "cursor": str(next_cursor) if next_cursor is not None else None,
        "exhausted": exhausted,
        "limit": limit,
        "selectors": selectors,
    }


def apply_detail_identity(conn: sqlite3.Connection, item_id: int, *,
                          tmdb_id=None, imdb_id=None) -> bool:
    """Fill missing identity columns from a detail payload. Never overwrites."""
    tmdb = normalise_tmdb_id(tmdb_id)
    imdb = normalise_imdb_id(imdb_id)
    if not tmdb and not imdb:
        return False
    row = conn.execute(
        "SELECT tmdb_id, imdb_id FROM items WHERE id = ?", (int(item_id),)
    ).fetchone()
    if row is None:
        return False
    current_tmdb = normalise_tmdb_id(row["tmdb_id"])
    current_imdb = normalise_imdb_id(row["imdb_id"])
    new_tmdb = current_tmdb or tmdb
    new_imdb = current_imdb or imdb
    if new_tmdb == current_tmdb and new_imdb == current_imdb:
        return False
    conn.execute(
        "UPDATE items SET tmdb_id = ?, imdb_id = ? WHERE id = ?",
        (new_tmdb, new_imdb, int(item_id)),
    )
    conn.commit()
    return True


def enrich_item_identity(conn: sqlite3.Connection, item: Mapping[str, Any], *,
                         acquire_timeout: float = 2.0) -> tuple[str | None, str | None]:
    """Best-effort detail lookup for one Xtream item missing ids.

    Called at most once per item per process by the acquisition pipeline. The
    connection-limit acquire is short and a busy playlist simply leaves the
    item as it was; enrichment must never fail a search.
    """
    from . import connections
    from .clients import xtream

    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?",
                        (int(item["source_id"]),))
    if source is None or source["kind"] != "xtream":
        return None, None
    kind = str(item["kind"])
    if kind not in ("vod", "series"):
        return None, None
    tmdb = normalise_tmdb_id(item.get("tmdb_id"))
    imdb = normalise_imdb_id(item.get("imdb_id"))
    if tmdb and imdb:
        return tmdb, imdb
    try:
        extra = json.loads(source["request_headers"] or "{}")
    except (TypeError, ValueError):
        extra = {}
    try:
        with connections.acquire(
            conn, int(item["source_id"]), purpose="Identity metadata",
            timeout=acquire_timeout,
        ):
            client = xtream.XtreamClient(
                source["base_url"], source["username"], source["password"],
                user_agent=extra.get("user_agent"),
                referrer=extra.get("referrer"),
                headers=extra.get("headers"),
            )
            try:
                if kind == "vod":
                    payload = client.vod_info(str(item["remote_id"]))
                else:
                    payload = client.series_info(str(item["remote_id"]))
            finally:
                client.close()
    except connections.ConnectionBusy:
        return None, None
    except Exception:
        return None, None
    found_tmdb, found_imdb = xtream.detail_identity(payload)
    apply_detail_identity(conn, int(item["id"]), tmdb_id=found_tmdb,
                          imdb_id=found_imdb)
    return found_tmdb or tmdb, found_imdb or imdb


def categories_for(conn: sqlite3.Connection, source_id: int,
                   kind: str = "", limit: int | None = DEFAULT_CATEGORY_LIMIT) -> dict:
    """Categories for the filter dropdown, optionally scoped to one kind.

    Returns the categories plus the true total, so the UI can say when it is
    showing a subset.
    """
    params: list = [source_id]
    where = "source_id = ? AND item_count > 0"

    if kind in ("live", "vod", "series"):
        where += " AND kind = ?"
        params.append(kind)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM categories WHERE {where}", params
    ).fetchone()["n"]

    sql = (
        "SELECT id, kind, name, item_count FROM categories "
        f"WHERE {where} ORDER BY item_count DESC, name COLLATE NOCASE"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()
    return {
        "categories": [dict(row) for row in rows],
        "total": total,
        "shown": len(rows),
    }


def counts_by_kind(conn: sqlite3.Connection, source_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM items WHERE source_id = ? GROUP BY kind",
        (source_id,),
    ).fetchall()
    counts = {row["kind"]: row["n"] for row in rows}
    counts["all"] = sum(counts.values())
    return counts


# ------------------------------------------------------------------ playback
class PlaybackUnavailable(RuntimeError):
    """Raised when an item cannot be turned into a stream URL."""


def allowed_formats(source_row) -> list[str]:
    try:
        formats = json.loads(source_row["allowed_formats"] or "[]")
        return formats if isinstance(formats, list) else []
    except (ValueError, TypeError):
        return []


def build_stream_url(source_row, item_row, *, prefer_hls: bool = True) -> str:
    """Assemble a playable URL for one item.

    For M3U sources the stored remote id already is the URL. For Xtream it has
    to be rebuilt, and the live extension depends on which output formats the
    account permits.
    """
    remote_id = item_row["remote_id"]
    if not remote_id:
        raise PlaybackUnavailable("This item has no stream id.")

    if source_row["kind"] == "m3u":
        return remote_id

    base = (source_row["base_url"] or "").rstrip("/")
    if not base:
        raise PlaybackUnavailable("This playlist has no server URL configured.")

    # Credentials can contain characters that are meaningful in a path, so
    # percent-encode them rather than pasting them in raw.
    user = quote(source_row["username"] or "", safe="")
    password = quote(source_row["password"] or "", safe="")
    kind = item_row["kind"]

    if kind == "live":
        formats = allowed_formats(source_row)
        # Prefer HLS for in-browser playback -- it is the only live format the
        # browser can be taught to play. Raw MPEG-TS is what the external
        # player is for.
        use_hls = prefer_hls and (not formats or "m3u8" in formats)
        extension = "m3u8" if use_hls else "ts"
        return f"{base}/live/{user}/{password}/{remote_id}.{extension}"

    if kind == "vod":
        extension = item_row["container_ext"] or "mp4"
        return f"{base}/movie/{user}/{password}/{remote_id}.{extension}"

    if kind == "series":
        # A series id is not playable. Callers must resolve an episode first
        # via resolve_series_episode().
        raise PlaybackUnavailable(
            "This is a series. Choose an episode to play."
        )

    raise PlaybackUnavailable(f"Unknown item kind {kind!r}.")


def guess_media_type(url: str) -> str:
    """Classify a URL for the browser player: hls | mpegts | file."""
    lowered = url.split("?")[0].lower()
    if lowered.endswith(".m3u8") or lowered.endswith(".m3u"):
        return "hls"
    if lowered.endswith(".ts"):
        return "mpegts"
    return "file"


def _season_sort_key(value):
    """Numeric seasons first, in order; anything else after them, alphabetically."""
    try:
        return (0, int(value), "")
    except (TypeError, ValueError):
        return (1, 0, str(value))


def _episode_number(entry: dict) -> int:
    try:
        return int(entry.get("episode_num") or 0)
    except (TypeError, ValueError):
        return 0


def series_episodes(source_row, series_id: str) -> list[dict]:
    """All episodes of a series, ordered, with playable URLs attached.

    Episodes come back from get_series_info keyed by season number *as a
    string*, not as a list, and the accompanying `seasons` array can be empty
    even when episodes exist -- so this reads the episodes mapping directly
    rather than trusting `seasons`.
    """
    from .clients import xtream

    try:
        extra = json.loads(source_row["request_headers"] or "{}")
    except (TypeError, ValueError):
        extra = {}
    client = xtream.XtreamClient(
        source_row["base_url"], source_row["username"], source_row["password"],
        user_agent=extra.get("user_agent"),
        referrer=extra.get("referrer"),
        headers=extra.get("headers"),
    )
    try:
        payload = client.series_info(series_id)
    finally:
        client.close()

    raw = payload.get("episodes") or {}
    if not isinstance(raw, dict) or not raw:
        raise PlaybackUnavailable("This series has no episodes listed.")

    seasons: list[dict] = []
    for season in sorted(raw, key=_season_sort_key):
        entries = raw[season]
        if not isinstance(entries, list):
            continue

        episodes = []
        for entry in sorted(entries, key=_episode_number):
            episode_id = entry.get("id")
            if not episode_id:
                continue
            # Note the field is `id`, not `stream_id`: for episodes the id
            # really is the stream id used in the URL.
            extension = entry.get("container_extension") or "mp4"
            episodes.append({
                "id": str(episode_id),
                "number": _episode_number(entry),
                "title": entry.get("title") or f"Episode {_episode_number(entry)}",
                "duration": (entry.get("info") or {}).get("duration"),
                "url": build_episode_url(source_row, str(episode_id), extension),
            })

        if episodes:
            seasons.append({"season": season, "episodes": episodes})

    if not seasons:
        raise PlaybackUnavailable("This series has no playable episodes.")
    return seasons


def resolve_series_episode(source_row, series_id: str) -> tuple[str, str]:
    """Return (episode_id, container_extension) for the first episode."""
    seasons = series_episodes(source_row, series_id)
    first = seasons[0]["episodes"][0]
    extension = first["url"].rsplit(".", 1)[-1]
    return first["id"], extension


def build_episode_url(source_row, episode_id: str, container_ext: str) -> str:
    base = (source_row["base_url"] or "").rstrip("/")
    user = quote(source_row["username"] or "", safe="")
    password = quote(source_row["password"] or "", safe="")
    return f"{base}/series/{user}/{password}/{episode_id}.{container_ext}"


__all__ = [
    "DEFAULT_SORT",
    "PlaybackUnavailable",
    "SORTS",
    "apply_detail_identity",
    "build_episode_url",
    "build_stream_url",
    "catalog_page",
    "categories_for",
    "counts_by_kind",
    "enrich_item_identity",
    "fts_query",
    "guess_media_type",
    "identity_catalog",
    "identity_from_payload",
    "identity_match_mode",
    "normalise_imdb_id",
    "normalise_tmdb_id",
    "query_items",
    "resolve_series_episode",
    "search_catalog",
    "series_episodes",
]
