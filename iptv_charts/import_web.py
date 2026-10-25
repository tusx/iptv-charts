"""Import-existing page and manual-metadata endpoints.

``/import`` reviews files that are already in the media folders but are not
tracked by the library. Scanning is read-only; metadata is fetched on demand
and the user confirms every import. The same blueprint exposes the manual
metadata search/preview/apply calls the movie and series detail pages use when
their existing identity is wrong.

The routes inherit the app-wide loopback Host/Origin guard; nothing here
weakens or repeats it. Follow the shared JSON convention: ``{ok: true, ...}``
on success and ``{ok: false, error}`` on expected failure.
"""

from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for

from . import config, db, library, manual_metadata, metadata, scan
from .clients import tmdb as tmdb_client
from .clients import tvdb as tvdb_client

logger = logging.getLogger("iptv_charts.import_web")

bp = Blueprint("import_media", __name__)

SERIES_MODES = ("all", "none", "manual", "future")


def _conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


def _fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _payload() -> dict:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    return payload


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _error(exc: Exception):
    if isinstance(exc, (scan.ImportConflict, manual_metadata.MetadataConflict)):
        return _fail(str(exc), 409)
    if isinstance(exc, LookupError):
        return _fail(str(exc), 404)
    if isinstance(exc, ValueError):
        return _fail(str(exc), 400)
    if isinstance(exc, (tmdb_client.TMDBKeyError, tvdb_client.TVDBKeyError)):
        return _fail(str(exc), 409)
    if isinstance(exc, (tmdb_client.TMDBError, tvdb_client.TVDBError,
                        metadata.MetadataError)):
        return _fail(str(exc), 502)
    logger.exception("import request failed: %s", exc)
    return _fail("The request could not be completed. See the app log.", 500)


# --------------------------------------------------------------------- page
def _profile_rows(conn) -> list[dict]:
    return [
        dict(row) for row in conn.execute(
            "SELECT id, name, builtin_key FROM quality_profiles ORDER BY id"
        )
    ]


def _default_profile_id(conn, profiles: list[dict]) -> int | None:
    key = db.get_setting(conn, "default_profile_key", "1080p") or "1080p"
    for profile in profiles:
        if profile.get("builtin_key") == key:
            return int(profile["id"])
    for profile in profiles:
        if profile.get("builtin_key") == "1080p":
            return int(profile["id"])
    return int(profiles[0]["id"]) if profiles else None


def _render_import(tab: str):
    conn = _conn()
    profiles = _profile_rows(conn)
    return render_template(
        "import_media.html",
        profiles=profiles,
        default_profile_id=_default_profile_id(conn, profiles),
        tmdb_configured=metadata.has_tmdb(conn),
        media_host_dir=config.media_host_dir(),
        movies_dir=str(config.movie_library_dir()),
        series_dir=str(config.series_library_dir()),
        series_modes=SERIES_MODES,
        initial_tab="series" if tab == "series" else "movie",
    )


@bp.get("/import")
def import_page():
    tab = "series" if request.args.get("tab") == "series" else "movie"
    return _render_import(tab)


@bp.get("/import/movies")
def import_movies_page():
    return redirect(url_for("import_media.import_page", tab="movie"))


@bp.get("/import/series")
def import_series_page():
    return redirect(url_for("import_media.import_page", tab="series"))


# -------------------------------------------------------------------- scan
@bp.get("/api/import/scan")
def scan_files():
    conn = _conn()
    kind = (request.args.get("kind") or "").strip().lower()
    try:
        items = scan.discover(conn, kind)
        return jsonify({
            "ok": True, "kind": kind, "items": items,
            "count": len(items),
            "importable": sum(1 for item in items if item.get("importable")),
        })
    except Exception as exc:
        return _error(exc)


@bp.post("/api/import/confirm")
def confirm_import():
    """Attach reviewed files to the library; one entry, or a small batch.

    A single-entry request reports a conflict/traversal/provider failure as
    its HTTP status, which is what a row-level Import button expects. A batch
    processes every entry and reports per-entry failures without aborting the
    rest.
    """
    conn = _conn()
    try:
        payload = _payload()
        raw_items = payload.get("items")
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list) or not raw_items:
            return _fail("Choose at least one file to import.")
        if len(raw_items) > 500:
            return _fail("Import at most 500 files at a time.")
        default_profile = _int(payload.get("quality_profile_id"))
        default_mode = str(payload.get("series_mode") or "all").strip().lower()
        default_seasons = payload.get("seasons") or []
        if default_mode not in SERIES_MODES:
            return _fail("Choose all, none, manual seasons or future episodes.")
        if not isinstance(default_seasons, (list, tuple)):
            return _fail("Seasons must be a list of season numbers.")

        imported: list[dict] = []
        failed: list[dict] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                failure = {"path": None, "error": "Each item must be an object."}
                failed.append(failure)
                continue
            try:
                result = scan.import_untracked(
                    conn,
                    path=entry.get("path"),
                    kind=entry.get("kind"),
                    tmdb_id=entry.get("tmdb_id"),
                    matched_movie_id=entry.get("matched_movie_id"),
                    matched_series_id=entry.get("matched_series_id"),
                    title=entry.get("title"),
                    year=entry.get("year"),
                    season=entry.get("season"),
                    episode=entry.get("episode"),
                    resolution=entry.get("resolution"),
                    profile_id=(
                        _int(entry.get("quality_profile_id"))
                        if entry.get("quality_profile_id") not in (None, "")
                        else default_profile
                    ),
                    series_mode=str(
                        entry.get("series_mode") or default_mode
                    ).strip().lower(),
                    seasons=entry.get("seasons") or default_seasons,
                )
                imported.append(result)
            except Exception as exc:
                if len(raw_items) == 1:
                    return _error(exc)
                logger.warning("import: %s failed: %s", entry.get("path"), exc)
                failed.append({
                    "path": entry.get("path"),
                    "title": entry.get("title"),
                    "error": str(exc),
                })

        response = {
            "ok": True,
            "imported": imported,
            "failed": failed,
            "summary": {"imported": len(imported), "failed": len(failed)},
        }
        if len(imported) == 1 and not failed:
            response["item"] = imported[0]
        return jsonify(response)
    except Exception as exc:
        return _error(exc)


# ------------------------------------------------- manual metadata (detail UI)
@bp.get("/api/import/metadata/search")
def metadata_search():
    conn = _conn()
    try:
        kind = manual_metadata.normalise_kind(request.args.get("kind"))
        query = (request.args.get("q") or "").strip()[:120]
        year = _int(request.args.get("year"))
        results = manual_metadata.search(conn, kind, query, year)
        return jsonify({"ok": True, "kind": kind, "results": results})
    except Exception as exc:
        return _error(exc)


@bp.get("/api/import/metadata/preview")
def metadata_preview():
    conn = _conn()
    try:
        kind = manual_metadata.normalise_kind(request.args.get("kind"))
        tmdb_id = manual_metadata.parse_tmdb_id(request.args.get("tmdb_id"))
        return jsonify({
            "ok": True, "kind": kind,
            "match": manual_metadata.preview(conn, kind, tmdb_id),
        })
    except Exception as exc:
        return _error(exc)


@bp.get("/api/import/metadata/movies/<int:movie_id>/search")
def movie_metadata_search(movie_id: int):
    conn = _conn()
    try:
        movie = library.get_movie(conn, movie_id)
        if movie is None:
            return _fail("That movie no longer exists.", 404)
        query = (request.args.get("q") or "").strip()[:120]
        year = _int(request.args.get("year"))
        results = manual_metadata.search(conn, "movie", query, year)
        return jsonify({"ok": True, "movie": movie, "results": results})
    except Exception as exc:
        return _error(exc)


@bp.get("/api/import/metadata/series/<int:series_id>/search")
def series_metadata_search(series_id: int):
    conn = _conn()
    try:
        series = library.get_series(conn, series_id)
        if series is None:
            return _fail("That series no longer exists.", 404)
        query = (request.args.get("q") or "").strip()[:120]
        year = _int(request.args.get("year"))
        results = manual_metadata.search(conn, "series", query, year)
        return jsonify({"ok": True, "series": series, "results": results})
    except Exception as exc:
        return _error(exc)


@bp.post("/api/import/metadata/movies/<int:movie_id>")
def apply_movie_metadata(movie_id: int):
    conn = _conn()
    try:
        payload = _payload()
        result = manual_metadata.apply(
            conn, "movie", movie_id, payload.get("tmdb_id")
        )
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return _error(exc)


@bp.post("/api/import/metadata/series/<int:series_id>")
def apply_series_metadata(series_id: int):
    conn = _conn()
    try:
        payload = _payload()
        result = manual_metadata.apply(
            conn, "series", series_id, payload.get("tmdb_id")
        )
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return _error(exc)


__all__ = ["bp"]
