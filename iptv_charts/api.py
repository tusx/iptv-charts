"""JSON endpoints for managing playlists and watching sync progress."""

from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, request

from . import connections, db, items, player, sources, sync

bp = Blueprint("api", __name__, url_prefix="/api")

logger = logging.getLogger("iptv_charts.api")


def _conn():
    """The request-scoped connection, created on first use."""
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


#: Shown when nothing can play a stream externally. Says where to fix it, since
#: "no player" is a setting away from being solved.
_NO_PLAYER = (
    "No external player is available. Choose one in Settings, or start the "
    "player bridge if you are running in a container."
)


# ------------------------------------------------------------------- probing
@bp.post("/probe")
def probe():
    """Test credentials without committing anything to the database."""
    payload = request.get_json(silent=True) or {}
    kind = (payload.get("kind") or "").strip()

    try:
        result = sources.probe(
            kind,
            name=payload.get("name", ""),
            m3u_url=payload.get("m3u_url", ""),
            base_url=payload.get("base_url", ""),
            username=payload.get("username", ""),
            password=payload.get("password", ""),
            user_agent=payload.get("user_agent", ""),
            referrer=payload.get("referrer", ""),
        )
    except sources.ProbeError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        return _fail(f"Unexpected error while testing: {exc}")

    return jsonify({"ok": True, "result": result})


# ------------------------------------------------------------------- sources
@bp.post("/sources")
def create_source():
    """Create a playlist and start its first sync."""
    payload = request.get_json(silent=True) or {}
    kind = (payload.get("kind") or "").strip()
    conn = _conn()

    try:
        raw_limit = payload.get("connection_limit")
        connection_limit = int(raw_limit) if str(raw_limit or "").strip() else None
        source_id = sources.create(
            conn,
            kind,
            payload.get("name", ""),
            m3u_url=payload.get("m3u_url", ""),
            base_url=payload.get("base_url", ""),
            username=payload.get("username", ""),
            password=payload.get("password", ""),
            user_agent=payload.get("user_agent", ""),
            referrer=payload.get("referrer", ""),
            connection_limit=connection_limit,
        )
    except (sources.ProbeError, TypeError, ValueError) as exc:
        return _fail(str(exc))

    sync.enqueue([source_id])
    return jsonify({"ok": True, "source_id": source_id}), 201


@bp.patch("/sources/<int:source_id>")
def update_source(source_id: int):
    """Edit a playlist in place and re-sync when its identity changed."""
    conn = _conn()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _fail("Expected a JSON object.")
    existing = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (source_id,))
    if existing is None:
        return _fail("That playlist no longer exists.", 404)
    if "kind" in payload and str(payload.get("kind") or "") != existing["kind"]:
        return _fail("Remove and re-add the playlist to change its type.", 409)
    job = sync.snapshot()["jobs"].get(source_id)
    if job and job.get("state") in ("queued", "running"):
        return _fail("That playlist is syncing. Wait for it to finish before editing it.", 409)

    try:
        row, catalog_changed = sources.update(
            conn, source_id,
            name=payload.get("name") if "name" in payload else None,
            m3u_url=payload.get("m3u_url") if "m3u_url" in payload else None,
            base_url=payload.get("base_url") if "base_url" in payload else None,
            username=payload.get("username") if "username" in payload else None,
            password=str(payload.get("password")) if payload.get("password") else None,
            user_agent=payload.get("user_agent") if "user_agent" in payload else None,
            referrer=payload.get("referrer") if "referrer" in payload else None,
            connection_limit=(
                None if payload.get("connection_limit") in (None, "", 0)
                else int(payload.get("connection_limit"))
            ) if "connection_limit" in payload else ...,
        )
    except (sources.ProbeError, TypeError, ValueError) as exc:
        return _fail(str(exc))

    queued = sync.enqueue([source_id]) if catalog_changed else []
    return jsonify({
        "ok": True,
        "source": {
            "id": row["id"], "name": row["name"], "kind": row["kind"],
            "base_url": row["base_url"], "username": row["username"],
            "m3u_url": row["m3u_url"],
            "connection_limit": row["connection_limit"],
            "max_connections": row["max_connections"],
        },
        "catalog_changed": catalog_changed,
        "sync_queued": bool(queued),
    })


@bp.post("/sources/<int:source_id>/probe")
def probe_source(source_id: int):
    """Test an edit form without saving it.

    A blank password reuses the stored one only while the submitted server and
    username match the stored values. If either changed, the password must be
    entered again; otherwise this endpoint would be a way to send the stored
    password to a caller-supplied server.
    """
    conn = _conn()
    row = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (source_id,))
    if row is None:
        return _fail("That playlist no longer exists.", 404)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _fail("Expected a JSON object.")
    headers = sources.request_headers(row)
    user_agent = (
        payload.get("user_agent") if "user_agent" in payload
        else headers.get("user_agent", "")
    )
    referrer = (
        payload.get("referrer") if "referrer" in payload
        else headers.get("referrer", "")
    )

    if row["kind"] == "xtream":
        proposed_base = payload.get("base_url") or row["base_url"] or ""
        proposed_user = payload.get("username") or row["username"] or ""
        supplied_password = str(payload.get("password") or "")
        if not supplied_password and (
            proposed_base != (row["base_url"] or "")
            or proposed_user != (row["username"] or "")
        ):
            return _fail(
                "Enter the password again when testing a different server URL "
                "or username."
            )

    try:
        if row["kind"] == "xtream":
            result = sources.probe(
                "xtream",
                name=payload.get("name", row["name"]),
                base_url=proposed_base,
                username=proposed_user,
                password=supplied_password or row["password"] or "",
                user_agent=user_agent or "",
                referrer=referrer or "",
                headers=headers.get("headers"),
            )
        else:
            result = sources.probe(
                "m3u",
                name=payload.get("name", row["name"]),
                m3u_url=payload.get("m3u_url") or row["m3u_url"] or "",
                user_agent=user_agent or "",
                referrer=referrer or "",
            )
    except sources.ProbeError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        return _fail(f"Unexpected error while testing: {exc}")
    return jsonify({"ok": True, "result": result})


@bp.post("/sources/<int:source_id>/sync")
def resync(source_id: int):
    conn = _conn()
    row = db.get_one(conn, "SELECT id FROM sources WHERE id = ?", (source_id,))
    if row is None:
        return _fail("That playlist no longer exists.", 404)

    queued = sync.enqueue([source_id])
    if not queued:
        return jsonify({"ok": True, "already_running": True})
    return jsonify({"ok": True, "queued": queued})


@bp.post("/sources/sync-all")
def sync_all():
    queued = sync.enqueue_all()
    return jsonify({"ok": True, "queued": queued})


@bp.post("/sources/<int:source_id>/cancel")
def cancel(source_id: int):
    if sync.cancel(source_id):
        return jsonify({"ok": True})
    return _fail("That playlist is not currently syncing.", 409)


@bp.post("/sync/cancel-all")
def cancel_all():
    return jsonify({"ok": True, "cancelled": sync.cancel_all()})


@bp.delete("/sources/<int:source_id>")
def delete_source(source_id: int):
    conn = _conn()
    if sources.delete(conn, source_id):
        return jsonify({"ok": True})
    return _fail("That playlist no longer exists.", 404)


# -------------------------------------------------------------------- status
@bp.get("/sync/status")
def sync_status():
    """Current sync progress.

    Polled only while something is running, so an idle app costs nothing.
    ``Cache-Control: no-store`` because a cached progress report is worse than
    no report at all.
    """
    response = jsonify({"ok": True, **sync.snapshot()})
    response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------- items
def _load_item(conn, item_id: int):
    item = db.get_one(conn, "SELECT * FROM items WHERE id = ?", (item_id,))
    if item is None:
        return None, None
    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (item["source_id"],))
    return item, source


def _resolve_url(source, item, *, prefer_hls: bool) -> str:
    """Full stream URL for an item, resolving series to an episode.

    A series row holds a series id, which is not itself playable -- the real
    stream id lives on an episode and only comes back from get_series_info.
    """
    if item["kind"] == "series":
        episode_id, extension = items.resolve_series_episode(source, item["remote_id"])
        return items.build_episode_url(source, episode_id, extension)
    return items.build_stream_url(source, item, prefer_hls=prefer_hls)


@bp.get("/sources/<int:source_id>/items")
def list_items(source_id: int):
    """Paged, searchable item listing for the browse page."""
    conn = _conn()
    if db.get_one(conn, "SELECT id FROM sources WHERE id = ?", (source_id,)) is None:
        return _fail("That playlist no longer exists.", 404)

    args = request.args
    category = args.get("category", "").strip()

    page = items.query_items(
        conn,
        source_id,
        text=args.get("q", ""),
        kind=args.get("kind", ""),
        category_id=int(category) if category.isdigit() else None,
        sort=args.get("sort", items.DEFAULT_SORT),
        limit=int(args.get("limit", 60) or 60),
        offset=int(args.get("offset", 0) or 0),
    )

    source = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (source_id,))
    media_type_map = {}

    for entry in page["items"]:
        # Copying a URL needs it up front. Series are the exception: resolving
        # one costs an extra API call, so it is left to the play moment.
        try:
            url = items.build_stream_url(source, entry, prefer_hls=True)
            entry["stream_url"] = url
            media_type_map[entry["id"]] = items.guess_media_type(url)
        except items.PlaybackUnavailable:
            entry["stream_url"] = None
        entry["media_type"] = media_type_map.get(entry["id"])

    return jsonify({"ok": True, **page})


@bp.get("/sources/<int:source_id>/categories")
def list_categories(source_id: int):
    conn = _conn()
    requested = request.args.get("limit", "").strip()
    limit = int(requested) if requested.isdigit() else items.DEFAULT_CATEGORY_LIMIT

    return jsonify({
        "ok": True,
        **items.categories_for(conn, source_id, request.args.get("kind", ""), limit),
        "counts": items.counts_by_kind(conn, source_id),
    })


@bp.get("/items/<int:item_id>/stream")
def item_stream(item_id: int):
    """The playable URL for one item, used by the player and the Copy button.

    Also carries the fields the browse listing leaves out, because the player
    shows an item's details and has nowhere else to get them from. In practice
    cast and director are only ever set by providers that fill them in; the
    player renders whatever is there and hides the rest.
    """
    conn = _conn()
    item, source = _load_item(conn, item_id)
    if item is None:
        return _fail("That item no longer exists.", 404)

    try:
        url = _resolve_url(source, item, prefer_hls=True)
    except items.PlaybackUnavailable as exc:
        return _fail(str(exc), 409)

    return jsonify({
        "ok": True,
        "url": url,
        "name": item["name"],
        "kind": item["kind"],
        "media_type": items.guess_media_type(url),
        "logo": item["logo"],
        "plot": item["plot"],
        "genre": item["genre"],
        "duration": item["duration"],
        "rating": item["rating"],
        "cast_list": item["cast_list"],
        "director": item["director"],
        "release_date": item["release_date"],
        "category": db.get_one(
            conn, "SELECT name FROM categories WHERE id = ?", (item["category_id"],)
        )["name"] if item["category_id"] else None,
    })


@bp.get("/items/<int:item_id>/episodes")
def item_episodes(item_id: int):
    """Seasons and episodes for a series item.

    Fetched only when a series is opened, since it costs a provider API call
    and most browsing never gets this far.
    """
    conn = _conn()
    item, source = _load_item(conn, item_id)
    if item is None:
        return _fail("That item no longer exists.", 404)
    if item["kind"] != "series":
        return _fail("That item is not a series.", 400)
    if source["kind"] != "xtream":
        return _fail("Episode listings are only available for Xtream playlists.", 400)

    try:
        with connections.acquire(
            conn, source["id"], purpose="Episode browser", timeout=5.0
        ):
            seasons = items.series_episodes(source, item["remote_id"])
    except connections.ConnectionBusy as exc:
        return _fail(str(exc), 409)
    except items.PlaybackUnavailable as exc:
        return _fail(str(exc), 409)
    except Exception as exc:  # noqa: BLE001 - provider error, shown verbatim
        return _fail(f"Could not load episodes: {exc}", 502)

    return jsonify({"ok": True, "seasons": seasons})


@bp.post("/items/<int:item_id>/episodes/<episode_id>/play")
def play_episode(item_id: int, episode_id: str):
    """Play one specific episode of a series.

    The episode id is checked against the series' own episode list rather than
    being pasted into a URL directly. Accepting a URL from the client here
    would let any page make the app hand an arbitrary address to the player.
    """
    conn = _conn()
    item, source = _load_item(conn, item_id)
    if item is None:
        return _fail("That item no longer exists.", 404)
    if item["kind"] != "series":
        return _fail("That item is not a series.", 400)

    chosen = player.resolve(_conn())
    if chosen is None:
        return _fail(_NO_PLAYER, 409)

    try:
        with connections.acquire(
            conn, source["id"], purpose="Episode browser", timeout=5.0
        ):
            seasons = items.series_episodes(source, item["remote_id"])
    except connections.ConnectionBusy as exc:
        return _fail(str(exc), 409)
    except items.PlaybackUnavailable as exc:
        return _fail(str(exc), 409)
    except Exception as exc:  # noqa: BLE001 - provider error, shown verbatim
        return _fail(f"Could not load episodes: {exc}", 502)

    match = next(
        (episode for season in seasons for episode in season["episodes"]
         if episode["id"] == str(episode_id)),
        None,
    )
    if match is None:
        return _fail("That episode is not part of this series.", 404)

    headers = sources.request_headers(source)
    title = f"{item['name']} — {match['title']}"
    logger.info('play episode #%d "%s" via %s', item_id, title, chosen.label)
    try:
        launched = player.launch(
            match["url"],
            user_agent=headers.get("user_agent"),
            referrer=headers.get("referrer"),
            title=title,
            player=chosen,
        )
    except RuntimeError as exc:
        logger.warning("play episode #%d failed: %s", item_id, exc)
        return _fail(str(exc), 502)

    return jsonify({"ok": True, "player": launched.label})


@bp.post("/items/<int:item_id>/play")
def play_item(item_id: int):
    """Hand an item to the external player.

    Prefers MPEG-TS over HLS here, unlike the browser player: some panels
    serve a valid-looking HLS manifest whose first segment 404s, and mpv
    handles raw transport streams natively anyway.
    """
    conn = _conn()
    item, source = _load_item(conn, item_id)
    if item is None:
        return _fail("That item no longer exists.", 404)

    chosen = player.resolve(_conn())
    if chosen is None:
        return _fail(_NO_PLAYER, 409)

    try:
        url = _resolve_url(source, item, prefer_hls=False)
    except items.PlaybackUnavailable as exc:
        return _fail(str(exc), 409)
    except Exception as exc:  # noqa: BLE001 - provider error, shown verbatim
        return _fail(f"Could not resolve this stream: {exc}", 502)

    headers = sources.request_headers(source)
    logger.info('play #%d "%s" via %s', item_id, item["name"], chosen.label)
    try:
        launched = player.launch(
            url,
            user_agent=headers.get("user_agent"),
            referrer=headers.get("referrer"),
            title=item["name"],
            player=chosen,
        )
    except RuntimeError as exc:
        logger.warning("play #%d failed: %s", item_id, exc)
        return _fail(str(exc), 502)

    return jsonify({"ok": True, "player": launched.label})


# ------------------------------------------------------------------ settings
@bp.post("/settings")
def save_settings():
    """Save the external-player choice.

    JSON only, and deliberately so: a form-encoded POST is a "simple request"
    that any website can aim at loopback without a preflight, and this endpoint
    decides what command the host runs. The origin guard in app.py is the other
    half of that; this half is never reading the value from a form or a query
    string, either of which a cross-site request can populate.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _fail("Expected a JSON object.")

    conn = _conn()

    # The command first: it is what "custom" refers to, so a request that sets
    # both must not be judged against the old value.
    if "player_command" in payload:
        text = str(payload["player_command"] or "").strip()
        if text:
            try:
                player.parse_command(text)
            except ValueError as exc:
                logger.warning("settings rejected: %s", exc)
                return _fail(str(exc))
        db.set_setting(conn, player.COMMAND_KEY, text)
        logger.info("settings: player_command %s", "set" if text else "cleared")

    if "preferred_player" in payload:
        preferred = str(payload["preferred_player"] or "").strip()
        known = player.KNOWN_PLAYER_IDS | {found.id for found in
                                           player.available(conn, refresh=True)}
        if preferred not in known:
            logger.warning("settings rejected: unknown player %r", preferred)
            return _fail("That player is not available.")
        if preferred == "custom" and player.custom_player(conn) is None:
            logger.warning("settings rejected: custom chosen without a command")
            return _fail("Give a command first — there is nothing to run yet.")
        db.set_setting(conn, player.PREFERENCE_KEY, preferred)
        logger.info("settings: preferred_player=%s", preferred)

    chosen = player.resolve(conn, refresh=True)

    # Show the user what their choice actually runs, rather than leaving them to
    # find out by clicking Play. A bridge-chosen player has no command to show;
    # that is not a failure.
    example = None
    if chosen is not None and chosen.flavour != "bridge":
        try:
            example = chosen.command_for(
                "http://example.com/stream.ts", title="Example stream",
            )
        except player.PlayerError as exc:
            return _fail(str(exc))

    return jsonify({
        "ok": True,
        "player": {"id": chosen.id, "label": chosen.label} if chosen else None,
        "command": example,
    })


@bp.get("/sources/<int:source_id>")
def source_detail(source_id: int):
    conn = _conn()
    row = db.get_one(conn, "SELECT * FROM sources WHERE id = ?", (source_id,))
    if row is None:
        return _fail("That playlist no longer exists.", 404)

    counts = {}
    for kind in sync.KINDS:
        counts[kind] = db.get_one(
            conn,
            "SELECT COUNT(*) AS n FROM items WHERE source_id = ? AND kind = ?",
            (source_id, kind),
        )["n"]

    return jsonify({
        "ok": True,
        "source": {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "last_synced_at": row["last_synced_at"],
            "sync_status": row["sync_status"],
            "sync_message": row["sync_message"],
            "counts": counts,
        },
    })
