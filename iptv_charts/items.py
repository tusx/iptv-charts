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
from urllib.parse import quote

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

    client = xtream.XtreamClient(
        source_row["base_url"], source_row["username"], source_row["password"]
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
    "build_episode_url",
    "build_stream_url",
    "categories_for",
    "counts_by_kind",
    "fts_query",
    "guess_media_type",
    "query_items",
    "resolve_series_episode",
    "series_episodes",
]
