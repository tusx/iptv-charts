"""JSON API for the movie/series manager.

The routes follow the existing API's conventions: ``{ok: true, …}`` on
success, ``{ok: false, error}`` on expected failure, JSON bodies for writes,
and the app-wide loopback/origin guard from app.py.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import time

from flask import Blueprint, Response, g, jsonify, request, send_file

from . import (backup, connections, db, downloader, jobs, library, metadata,
               naming, probe, quality, scan, scheduler, settings_service)
from .clients import tmdb as tmdb_client
from .clients import tvdb as tvdb_client

logger = logging.getLogger("iptv_charts.manager_api")

bp = Blueprint("manager_api", __name__)


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


def _error(exc: Exception):
    if isinstance(exc, jobs.JobFailed):
        # The message came from the worker, which ran the same library code
        # the web process used to run. Preserve its error classification so a
        # missing movie is still 404 and a rejected profile is still 400.
        status = {
            "LookupError": 404,
            "ValueError": 400,
            "ConnectionBusy": 409,
            "TMDBKeyError": 409,
            "TVDBKeyError": 409,
            "TMDBError": 502,
            "TVDBError": 502,
            "MetadataError": 502,
        }.get(exc.error_type, 500)
        message = str(exc)
        if exc.error_type and message.startswith(f"{exc.error_type}: "):
            # The worker stores "ValueError: <sentence>" for diagnostics; the
            # old synchronous API showed the sentence to the user.
            message = message[len(exc.error_type) + 2:]
        return _fail(message, status)
    if isinstance(exc, (jobs.WorkerUnavailable, jobs.JobTimeout)):
        return _fail(str(exc), 503)
    if isinstance(exc, jobs.JobCancelled):
        return _fail(str(exc), 409)
    if isinstance(exc, LookupError):
        return _fail(str(exc), 404)
    if isinstance(exc, ValueError):
        return _fail(str(exc), 400)
    if isinstance(exc, (tmdb_client.TMDBKeyError, tvdb_client.TVDBKeyError,
                        connections.ConnectionBusy)):
        return _fail(str(exc), 409)
    if isinstance(exc, (tmdb_client.TMDBError, tvdb_client.TVDBError,
                        metadata.MetadataError)):
        return _fail(str(exc), 502)
    logger.exception("manager api failed: %s", exc)
    return _fail("The request could not be completed. See the app log.", 500)


def _bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------- metadata
@bp.get("/metadata/movie/search")
def search_movie_metadata():
    conn = _conn()
    query = (request.args.get("q") or "").strip()[:120]
    year = _int(request.args.get("year"))
    if not query:
        return _fail("Type a movie title to search.")
    try:
        return jsonify({"ok": True, "results": metadata.search_movies(conn, query, year)})
    except Exception as exc:
        return _error(exc)


@bp.get("/metadata/series/search")
def search_series_metadata():
    conn = _conn()
    query = (request.args.get("q") or "").strip()[:120]
    year = _int(request.args.get("year"))
    if not query:
        return _fail("Type a series title to search.")
    try:
        return jsonify({"ok": True, "results": metadata.search_series(conn, query, year)})
    except Exception as exc:
        return _error(exc)


@bp.post("/metadata/test")
def test_metadata():
    conn = _conn()
    try:
        payload = _payload()
        service = str(payload.get("service") or "").strip().lower()
        if service == "tmdb":
            with metadata.tmdb_client(conn) as client:
                client.search_movies("test")
            return jsonify({"ok": True, "message": "TMDB accepted the API Read Access Token."})
        if service == "tvdb":
            with metadata.tvdb_client(conn) as client:
                client.search_series("test")
            return jsonify({"ok": True, "message": "TVDB login succeeded."})
        return _fail("Choose tmdb or tvdb.")
    except Exception as exc:
        return _error(exc)


@bp.get("/images/tmdb/<size>/<path:path>")
def tmdb_image(size: str, path: str):
    try:
        data, content_type = metadata.fetch_image(size, path)
    except metadata.MetadataError as exc:
        return _fail(str(exc), 404)
    except Exception:
        logger.exception("image proxy failed")
        return _fail("The image could not be fetched.", 502)
    response = Response(data, mimetype=content_type)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


# --------------------------------------------------------------------- movies
@bp.post("/movies")
def add_movie():
    conn = _conn()
    try:
        payload = _payload()
        tmdb_id = _int(payload.get("tmdb_id"))
        if not tmdb_id:
            return _fail("Choose a movie first.")
        search_now = _bool(payload.get("search"), True)
        movie = library.add_movie(
            conn, tmdb_id,
            profile_id=_int(payload.get("quality_profile_id")),
            monitored=_bool(payload.get("monitored"), True),
            minimum_availability=str(
                payload.get("minimum_availability") or "released"
            ),
            priority=_int(payload.get("priority")) or 0,
        )
        if search_now:
            # The add is already durable. A search failure must not undo it;
            # that is the same rule the old in-process search had.
            try:
                jobs.submit_and_wait(
                    "search_movie", {"movie_id": movie["id"], "reason": "add"},
                    priority=55, dedupe_key=f"search_movie:{movie['id']}:add",
                    timeout=600.0,
                )
            except Exception as exc:
                logger.warning("initial search for %s failed: %s", movie["title"], exc)
            movie = library.get_movie(conn, movie["id"])
        return jsonify({"ok": True, "movie": movie}), 201
    except Exception as exc:
        return _error(exc)


@bp.get("/movies/<int:movie_id>")
def movie_detail(movie_id: int):
    conn = _conn()
    try:
        movie = library.get_movie(conn, movie_id)
        if movie is None:
            return _fail("That movie no longer exists.", 404)
        history = [dict(row) for row in conn.execute(
            "SELECT * FROM history WHERE movie_id = ? ORDER BY id DESC LIMIT 50",
            (movie_id,),
        )]
        candidates = library.movie_candidates_view(conn, movie)
        downloads = [dict(row) for row in conn.execute(
            "SELECT * FROM downloads WHERE movie_id = ? "
            "AND status IN ('queued', 'downloading', 'importing') "
            "ORDER BY CASE mode WHEN 'main' THEN 0 ELSE 1 END, id",
            (movie_id,),
        )]
        return jsonify({
            "ok": True, "movie": movie,
            "status": library.movie_status(conn, movie),
            "download": library.active_download(conn, movie_id=movie_id),
            "downloads": downloads,
            "candidates": candidates,
            "alternates": library.movie_alternates(conn, movie_id),
            "history": history,
        })
    except Exception as exc:
        return _error(exc)


@bp.patch("/movies/<int:movie_id>")
def edit_movie(movie_id: int):
    conn = _conn()
    try:
        payload = _payload()
        movie = library.update_movie(
            conn, movie_id,
            monitored=_bool(payload.get("monitored")) if "monitored" in payload else None,
            profile_id=_int(payload.get("quality_profile_id"))
            if "quality_profile_id" in payload else None,
            minimum_availability=(
                str(payload.get("minimum_availability"))
                if "minimum_availability" in payload else None
            ),
            priority=_int(payload.get("priority"))
            if "priority" in payload else None,
        )
        return jsonify({"ok": True, "movie": movie})
    except Exception as exc:
        return _error(exc)


@bp.delete("/movies/<int:movie_id>")
def delete_movie(movie_id: int):
    conn = _conn()
    try:
        payload = request.get_json(silent=True)
        delete_files = _bool(payload.get("delete_files")) if isinstance(payload, dict) else False
        library.remove_movie(conn, movie_id, delete_files=delete_files)
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(exc)


@bp.post("/movies/<int:movie_id>/refresh")
def refresh_movie(movie_id: int):
    conn = _conn()
    try:
        result = jobs.submit_and_wait(
            "refresh_movie", {"movie_id": movie_id},
            priority=55, dedupe_key=f"refresh_movie:{movie_id}", timeout=300.0,
        )
        return jsonify({"ok": True, **(result or {})})
    except Exception as exc:
        return _error(exc)


@bp.post("/movies/<int:movie_id>/search")
def search_movie(movie_id: int):
    conn = _conn()
    try:
        movie = library.get_movie(conn, movie_id)
        if movie is None:
            return _fail("That movie no longer exists.", 404)
        result = jobs.submit_and_wait(
            "search_movie", {"movie_id": movie_id, "reason": "manual"},
            priority=60, dedupe_key=f"search_movie:{movie_id}:manual",
            timeout=600.0,
        ) or {}
        return jsonify({"ok": True, **result, "movie": library.get_movie(conn, movie_id)})
    except Exception as exc:
        return _error(exc)


@bp.get("/movies/<int:movie_id>/releases")
def movie_releases(movie_id: int):
    conn = _conn()
    try:
        if library.get_movie(conn, movie_id) is None:
            return _fail("That movie no longer exists.", 404)
        query = (request.args.get("q") or "").strip()[:120] or None
        key = f"movie_releases:{movie_id}:{query or ''}"
        result = jobs.submit_and_wait(
            "movie_releases", {"movie_id": movie_id, "query": query},
            priority=60, dedupe_key=key, timeout=600.0,
        ) or {}
        return jsonify({"ok": True, "releases": result.get("releases") or []})
    except Exception as exc:
        return _error(exc)


@bp.get("/movies/<int:movie_id>/candidates")
def movie_candidates(movie_id: int):
    conn = _conn()
    try:
        movie = library.get_movie(conn, movie_id)
        if movie is None:
            return _fail("That movie no longer exists.", 404)
        view = library.movie_candidates_view(conn, movie)
        return jsonify({"ok": True, **view, "movie": movie})
    except Exception as exc:
        return _error(exc)


@bp.post("/movies/<int:movie_id>/candidates/probe")
def probe_movie_candidate(movie_id: int):
    conn = _conn()
    try:
        payload = _payload()
        source_id = _int(payload.get("source_id"))
        remote_id = str(payload.get("remote_id") or "").strip()
        if not source_id or not remote_id:
            return _fail("Choose a candidate first.")
        result = jobs.submit_and_wait(
            "probe_movie_candidate", {
                "movie_id": movie_id, "source_id": source_id,
                "remote_id": remote_id, "refresh": _bool(payload.get("refresh")),
            },
            priority=65,
            dedupe_key=f"probe_movie:{movie_id}:{source_id}:{remote_id}",
            timeout=300.0,
        )
        return jsonify({"ok": True, "candidate": result})
    except Exception as exc:
        return _error(exc)


@bp.post("/movies/<int:movie_id>/candidates/probe-all")
def probe_movie_candidates(movie_id: int):
    conn = _conn()
    try:
        payload = request.get_json(silent=True)
        refresh = _bool(payload.get("refresh")) if isinstance(payload, dict) else False
        result = jobs.submit_and_wait(
            "probe_movie_candidates", {"movie_id": movie_id, "refresh": refresh},
            priority=50, dedupe_key=f"probe_movies:{movie_id}", timeout=3600.0,
        )
        return jsonify({"ok": True, **(result or {})})
    except Exception as exc:
        return _error(exc)


@bp.post("/movies/<int:movie_id>/candidate")
def select_movie_candidate(movie_id: int):
    conn = _conn()
    try:
        payload = _payload()
        source_id = _int(payload.get("source_id"))
        remote_id = str(payload.get("remote_id") or "").strip()
        mode = str(payload.get("mode") or "").strip().lower()
        if not source_id or not remote_id:
            return _fail("Choose a candidate first.")
        if mode not in ("only", "also"):
            return _fail("Choose whether to use only this version or download it too.")
        grab = _bool(payload.get("grab"), True)
        result = library.select_movie_candidate(
            conn, movie_id, source_id, remote_id, mode, grab=grab,
        )
        search_queued = False
        if result.pop("search_needed", False):
            job = jobs.enqueue(
                conn, "search_movie",
                {"movie_id": movie_id, "reason": "selection"},
                priority=55,
                # Selecting a different candidate while a search is running
                # needs its own follow-up; the running job may have read the
                # previous pin already.
                dedupe_key=(
                    f"search_movie:{movie_id}:selection:{source_id}:{remote_id}"
                ),
            )
            search_queued = bool(job.get("_created"))
        alternate = result.get("download") if mode == "also" else None
        main_download = result.get("download") if mode == "only" else None
        return jsonify({
            "ok": True,
            "movie": result.get("movie"),
            "candidate": result.get("candidate"),
            "download": main_download,
            "alternate": alternate,
            "search_queued": search_queued,
            "message": (
                "Pinned candidate. Search queued; it will add this version unless "
                "the quality profile picks the same candidate."
                if mode == "also" else
                "Pinned candidate; other candidates were overridden and this "
                "version was queued."
            ),
        })
    except Exception as exc:
        return _error(exc)


@bp.delete("/movies/<int:movie_id>/candidate")
def clear_movie_candidate(movie_id: int):
    conn = _conn()
    try:
        return jsonify({"ok": True, "movie": library.clear_movie_candidate(conn, movie_id)})
    except Exception as exc:
        return _error(exc)


@bp.delete("/movies/<int:movie_id>/alternates/<int:alternate_id>")
def delete_movie_alternate(movie_id: int, alternate_id: int):
    conn = _conn()
    try:
        library.remove_movie_alternate(conn, movie_id, alternate_id)
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(exc)


@bp.post("/movies/<int:movie_id>/grab")
def grab_movie(movie_id: int):
    conn = _conn()
    try:
        payload = _payload()
        source_id = _int(payload.get("source_id"))
        remote_id = str(payload.get("remote_id") or "").strip()
        if not source_id or not remote_id:
            return _fail("Choose a release first.")
        result = jobs.submit_and_wait(
            "grab_movie", {
                "movie_id": movie_id, "source_id": source_id, "remote_id": remote_id,
            },
            priority=55,
            dedupe_key=f"grab_movie:{movie_id}:{source_id}:{remote_id}",
            timeout=600.0,
        ) or {}
        return jsonify({"ok": True, **(result or {})})
    except Exception as exc:
        return _error(exc)


# --------------------------------------------------------------------- series
@bp.post("/series")
def add_series():
    conn = _conn()
    try:
        payload = _payload()
        tmdb_id = _int(payload.get("tmdb_id"))
        if not tmdb_id:
            return _fail("Choose a series first.")
        search_now = _bool(payload.get("search"), True)
        series = library.add_series(
            conn, tmdb_id,
            profile_id=_int(payload.get("quality_profile_id")),
            monitored=_bool(payload.get("monitored"), True),
            monitor_mode=str(payload.get("monitor_mode") or "all"),
            monitor_new_seasons=str(payload.get("monitor_new_seasons") or "all"),
            priority=_int(payload.get("priority")) or 0,
        )
        if search_now:
            try:
                jobs.submit_and_wait(
                    "search_series", {"series_id": series["id"], "reason": "add"},
                    priority=55, dedupe_key=f"search_series:{series['id']}:add",
                    timeout=900.0,
                )
            except Exception as exc:
                logger.warning("initial search for %s failed: %s", series["title"], exc)
            series = library.get_series(conn, series["id"])
        return jsonify({"ok": True, "series": series}), 201
    except Exception as exc:
        return _error(exc)


@bp.get("/series/<int:series_id>")
def series_detail(series_id: int):
    conn = _conn()
    try:
        series = library.get_series(conn, series_id)
        if series is None:
            return _fail("That series no longer exists.", 404)
        episodes = library.series_episodes(conn, series_id)
        history = [dict(row) for row in conn.execute(
            "SELECT * FROM history WHERE series_id = ? ORDER BY id DESC LIMIT 50",
            (series_id,),
        )]
        return jsonify({
            "ok": True, "series": series, "episodes": episodes, "history": history,
            "downloads": [dict(row) for row in conn.execute(
                "SELECT * FROM downloads WHERE series_id = ? AND status IN (?, ?, ?)",
                (series_id, *downloader.ACTIVE),
            )],
        })
    except Exception as exc:
        return _error(exc)


@bp.patch("/series/<int:series_id>")
def edit_series(series_id: int):
    conn = _conn()
    try:
        payload = _payload()
        series = library.update_series(
            conn, series_id,
            monitored=_bool(payload.get("monitored")) if "monitored" in payload else None,
            profile_id=_int(payload.get("quality_profile_id"))
            if "quality_profile_id" in payload else None,
            monitor_new_seasons=(
                str(payload.get("monitor_new_seasons"))
                if "monitor_new_seasons" in payload else None
            ),
            priority=_int(payload.get("priority"))
            if "priority" in payload else None,
        )
        return jsonify({"ok": True, "series": series})
    except Exception as exc:
        return _error(exc)


@bp.delete("/series/<int:series_id>")
def delete_series(series_id: int):
    conn = _conn()
    try:
        payload = request.get_json(silent=True)
        delete_files = _bool(payload.get("delete_files")) if isinstance(payload, dict) else False
        library.remove_series(conn, series_id, delete_files=delete_files)
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(exc)


@bp.post("/series/<int:series_id>/refresh")
def refresh_series(series_id: int):
    conn = _conn()
    try:
        result = jobs.submit_and_wait(
            "refresh_series", {"series_id": series_id},
            priority=55, dedupe_key=f"refresh_series:{series_id}", timeout=600.0,
        )
        return jsonify({"ok": True, **(result or {})})
    except Exception as exc:
        return _error(exc)


@bp.post("/series/<int:series_id>/search")
def search_series(series_id: int):
    conn = _conn()
    try:
        series = library.get_series(conn, series_id)
        if series is None:
            return _fail("That series no longer exists.", 404)
        result = jobs.submit_and_wait(
            "search_series", {"series_id": series_id, "reason": "manual"},
            priority=60, dedupe_key=f"search_series:{series_id}:manual",
            timeout=900.0,
        ) or {}
        return jsonify({"ok": True, **result, "series": library.get_series(conn, series_id)})
    except Exception as exc:
        return _error(exc)


@bp.patch("/series/<int:series_id>/seasons/<int:season>/monitor")
def monitor_season(series_id: int, season: int):
    conn = _conn()
    try:
        payload = _payload()
        library.set_season_monitored(conn, series_id, season, _bool(payload.get("monitored")))
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(exc)


@bp.patch("/series/<int:series_id>/episodes/<int:episode_id>")
def monitor_episode(series_id: int, episode_id: int):
    conn = _conn()
    try:
        payload = _payload()
        episode = library.get_episode(conn, episode_id)
        if episode is None or int(episode["series_id"]) != int(series_id):
            return _fail("That episode no longer exists.", 404)
        return jsonify({
            "ok": True,
            "episode": library.set_episode_monitored(
                conn, episode_id, _bool(payload.get("monitored"))
            ),
        })
    except Exception as exc:
        return _error(exc)


@bp.post("/series/<int:series_id>/episodes/<int:episode_id>/search")
def search_episode(series_id: int, episode_id: int):
    conn = _conn()
    try:
        series = library.get_series(conn, series_id)
        if series is None:
            return _fail("That series no longer exists.", 404)
        result = jobs.submit_and_wait(
            "search_series", {
                "series_id": series_id, "episode_id": episode_id,
                "reason": "manual",
            },
            priority=60,
            dedupe_key=f"search_series:{series_id}:episode:{episode_id}",
            timeout=600.0,
        ) or {}
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return _error(exc)


@bp.get("/series/<int:series_id>/episodes/<int:episode_id>/releases")
def episode_releases(series_id: int, episode_id: int):
    conn = _conn()
    try:
        query = (request.args.get("q") or "").strip()[:120] or None
        result = jobs.submit_and_wait(
            "episode_releases", {
                "series_id": series_id, "episode_id": episode_id, "query": query,
            },
            priority=60,
            dedupe_key=f"episode_releases:{series_id}:{episode_id}:{query or ''}",
            timeout=600.0,
        ) or {}
        return jsonify({"ok": True, "releases": result.get("releases") or []})
    except Exception as exc:
        return _error(exc)


@bp.post("/series/<int:series_id>/episodes/<int:episode_id>/grab")
def grab_episode(series_id: int, episode_id: int):
    conn = _conn()
    try:
        payload = _payload()
        source_id = _int(payload.get("source_id"))
        provider_remote_id = str(payload.get("provider_remote_id") or "").strip()
        stream_id = str(payload.get("stream_id") or "").strip()
        if not source_id or not provider_remote_id or not stream_id:
            return _fail("Choose a release first.")
        result = jobs.submit_and_wait(
            "grab_episode", {
                "series_id": series_id, "episode_id": episode_id,
                "source_id": source_id,
                "provider_remote_id": provider_remote_id,
                "stream_id": stream_id,
            },
            priority=55,
            dedupe_key=(
                f"grab_episode:{series_id}:{episode_id}:{source_id}:"
                f"{provider_remote_id}:{stream_id}"
            ),
            timeout=600.0,
        ) or {}
        return jsonify({"ok": True, **(result or {})})
    except Exception as exc:
        return _error(exc)


# ------------------------------------------------------------------- activity
@bp.get("/activity")
def activity():
    conn = _conn()
    try:
        snapshot = downloader.snapshot(conn)
        snapshot["tasks"] = scheduler.snapshot(conn)
        snapshot["task_running"] = scheduler.is_running()
        snapshot["worker"] = jobs.snapshot(conn, limit=20)
        snapshot["worker_status"] = jobs.worker_status(conn)
        snapshot["job_counts"] = jobs.counts(conn)
        snapshot["ok"] = True
        response = jsonify(snapshot)
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        return _error(exc)


@bp.post("/downloads/<int:download_id>/cancel")
def cancel_download(download_id: int):
    conn = _conn()
    try:
        return jsonify({"ok": True, "download": downloader.cancel_download(conn, download_id)})
    except Exception as exc:
        return _error(exc)


@bp.post("/downloads/<int:download_id>/retry")
def retry_download(download_id: int):
    conn = _conn()
    try:
        return jsonify({"ok": True, "download": downloader.retry_download(conn, download_id)})
    except Exception as exc:
        return _error(exc)


@bp.patch("/downloads/<int:download_id>")
def edit_download(download_id: int):
    conn = _conn()
    try:
        payload = _payload()
        row = db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (download_id,))
        if row is None:
            return _fail("That download no longer exists.", 404)
        if "priority" in payload:
            priority = max(-100, min(100, int(payload.get("priority") or 0)))
            conn.execute("UPDATE downloads SET priority = ? WHERE id = ?",
                         (priority, download_id))
            # The worker claims the download job by its own priority; keep the
            # queue's ordering in step with the row the Activity page edits.
            conn.execute(
                "UPDATE worker_jobs SET priority = ? WHERE kind = 'download' "
                "AND dedupe_key = ? AND status IN ('queued', 'running')",
                (priority, f"download:{download_id}"),
            )
            conn.commit()
        return jsonify({
            "ok": True,
            "download": dict(db.get_one(
                conn, "SELECT * FROM downloads WHERE id = ?", (download_id,)
            )),
        })
    except Exception as exc:
        return _error(exc)


@bp.delete("/downloads/<int:download_id>")
def remove_download(download_id: int):
    conn = _conn()
    try:
        downloader.remove_download(conn, download_id)
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(exc)


@bp.post("/probes/clear")
def clear_probes():
    conn = _conn()
    try:
        payload = _payload()
        source_id = _int(payload.get("source_id"))
        return jsonify({"ok": True, "cleared": probe.clear_cache(conn, source_id)})
    except Exception as exc:
        return _error(exc)


@bp.get("/tasks")
def tasks():
    conn = _conn()
    return jsonify({
        "ok": True,
        "tasks": scheduler.snapshot(conn),
        "running": scheduler.is_running(),
    })


@bp.post("/tasks/<name>/run")
def run_task(name: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    scope = str(payload.get("scope") or "all")
    try:
        if not scheduler.trigger(name, scope):
            return _fail("A task is already running. Wait for it to finish.", 409)
        return jsonify({"ok": True, "started": name, "scope": scope})
    except ValueError as exc:
        return _fail(str(exc), 404)


# ----------------------------------------------------------------------- scan
@bp.get("/scan/results")
def scan_results():
    conn = _conn()
    return jsonify({"ok": True, "results": scan.results(conn)})


@bp.post("/scan/results/<int:result_id>/import")
def scan_import(result_id: int):
    try:
        result = jobs.submit_and_wait(
            "scan_import", {"result_id": result_id},
            priority=45, dedupe_key=f"scan_import:{result_id}", timeout=600.0,
        ) or {}
        return jsonify({"ok": True, **(result or {})})
    except Exception as exc:
        return _error(exc)


@bp.post("/scan/results/<int:result_id>/ignore")
def scan_ignore(result_id: int):
    conn = _conn()
    try:
        scan.ignore_result(conn, result_id)
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(exc)


@bp.post("/scan/import-all")
def scan_import_all():
    try:
        result = jobs.submit_and_wait(
            "scan_import_all", {}, priority=45,
            dedupe_key="scan_import_all", timeout=900.0,
        ) or {}
        return jsonify({"ok": True, **(result or {})})
    except Exception as exc:
        return _error(exc)


# ------------------------------------------------------------------- profiles
def _profile_values(payload: dict) -> dict:
    raw_qualities = payload.get("qualities")
    if raw_qualities is None:
        raw_qualities = payload.get("resolutions")
    return quality.validate_profile(
        raw_qualities,
        payload.get("cutoff"),
        _bool(payload.get("upgrades"), True),
        audio_mode=payload.get("audio_mode", "any"),
        audio_languages=payload.get("audio_languages"),
        subtitle_mode=payload.get("subtitle_mode", "any"),
        subtitle_languages=payload.get("subtitle_languages"),
        probe_required=_bool(payload.get("probe_required"), True),
    )


def _store_profile(conn, profile_id: int, values: dict) -> None:
    resolutions = list(dict.fromkeys(
        tier["resolution"] for tier in values["qualities"]
    ))
    conn.execute(
        "UPDATE quality_profiles SET qualities = ?, resolutions = ?, cutoff = ?, "
        "upgrades = ?, audio_mode = ?, audio_languages = ?, subtitle_mode = ?, "
        "subtitle_languages = ?, probe_required = ? WHERE id = ?",
        (
            json.dumps(values["qualities"]), json.dumps(resolutions),
            values["cutoff"], 1 if values["upgrades"] else 0,
            values["audio_mode"], json.dumps(values["audio_languages"]),
            values["subtitle_mode"], json.dumps(values["subtitle_languages"]),
            1 if values["probe_required"] else 0, int(profile_id),
        ),
    )


@bp.get("/quality-profiles")
def quality_profiles():
    conn = _conn()
    rows = [quality.profile_summary(row) for row in conn.execute(
        "SELECT * FROM quality_profiles ORDER BY id"
    )]
    return jsonify({"ok": True, "profiles": rows})


@bp.post("/quality-profiles")
def create_profile():
    conn = _conn()
    try:
        payload = _payload()
        name = str(payload.get("name") or "").strip()[:80]
        if not name:
            return _fail("Give the profile a name.")
        values = _profile_values(payload)
        resolutions = list(dict.fromkeys(
            tier["resolution"] for tier in values["qualities"]
        ))
        try:
            cursor = conn.execute(
                "INSERT INTO quality_profiles "
                "(name, resolutions, cutoff, upgrades, qualities, audio_mode, "
                "audio_languages, subtitle_mode, subtitle_languages, probe_required, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name, json.dumps(resolutions), values["cutoff"],
                    1 if values["upgrades"] else 0,
                    json.dumps(values["qualities"]), values["audio_mode"],
                    json.dumps(values["audio_languages"]), values["subtitle_mode"],
                    json.dumps(values["subtitle_languages"]),
                    1 if values["probe_required"] else 0, int(time.time()),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return _fail("A profile with that name already exists.", 409)
        row = db.get_one(conn, "SELECT * FROM quality_profiles WHERE id = ?",
                         (cursor.lastrowid,))
        return jsonify({"ok": True, "profile": quality.profile_summary(row)}), 201
    except Exception as exc:
        return _error(exc)


@bp.patch("/quality-profiles/<int:profile_id>")
def edit_profile(profile_id: int):
    conn = _conn()
    try:
        payload = _payload()
        row = db.get_one(conn, "SELECT * FROM quality_profiles WHERE id = ?", (profile_id,))
        if row is None:
            return _fail("That profile no longer exists.", 404)
        current = quality.profile_summary(row)
        name = str(payload.get("name") or row["name"]).strip()[:80]
        values = _profile_values({
            "qualities": payload.get("qualities", current["qualities"]),
            "cutoff": payload.get("cutoff", current["cutoff"]),
            "upgrades": payload.get("upgrades", current["upgrades"]),
            "audio_mode": payload.get("audio_mode", current["audio_mode"]),
            "audio_languages": payload.get("audio_languages", current["audio_languages"]),
            "subtitle_mode": payload.get("subtitle_mode", current["subtitle_mode"]),
            "subtitle_languages": payload.get(
                "subtitle_languages", current["subtitle_languages"]
            ),
            "probe_required": payload.get("probe_required", current["probe_required"]),
        })
        try:
            conn.execute("UPDATE quality_profiles SET name = ? WHERE id = ?",
                         (name, profile_id))
            _store_profile(conn, profile_id, values)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return _fail("A profile with that name already exists.", 409)
        updated = db.get_one(conn, "SELECT * FROM quality_profiles WHERE id = ?",
                             (profile_id,))
        return jsonify({"ok": True, "profile": quality.profile_summary(updated)})
    except Exception as exc:
        return _error(exc)


@bp.delete("/quality-profiles/<int:profile_id>")
def delete_profile(profile_id: int):
    conn = _conn()
    row = db.get_one(conn, "SELECT * FROM quality_profiles WHERE id = ?", (profile_id,))
    if row is None:
        return _fail("That profile no longer exists.", 404)
    used = db.get_one(
        conn,
        "SELECT (SELECT COUNT(*) FROM movies WHERE quality_profile_id = ?) + "
        "(SELECT COUNT(*) FROM series WHERE quality_profile_id = ?) AS n",
        (profile_id, profile_id),
    )["n"]
    if used:
        return _fail("That profile is in use. Reassign its movies and series first.", 409)
    total = db.get_one(conn, "SELECT COUNT(*) AS n FROM quality_profiles")["n"]
    if total <= 1:
        return _fail("Keep at least one quality profile.", 409)
    conn.execute("DELETE FROM quality_profiles WHERE id = ?", (profile_id,))
    conn.commit()
    return jsonify({"ok": True})


# ------------------------------------------------------------------- settings
@bp.post("/settings/save")
def save_all_settings():
    """Validate and save every settings section in one transaction."""
    conn = _conn()
    try:
        payload = _payload()
        return jsonify(settings_service.save_all(conn, payload))
    except settings_service.SettingsError as exc:
        return jsonify({
            "ok": False,
            "error": str(exc),
            "section": exc.section,
        }), 400
    except Exception as exc:
        return _error(exc)


def _save_setting(conn, key: str, value: str) -> None:
    db.set_setting(conn, key, value)


def _valid_language(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$", value))


@bp.post("/settings/metadata")
def save_metadata_settings():
    conn = _conn()
    try:
        payload = _payload()
        if _bool(payload.get("tmdb_clear")):
            _save_setting(conn, metadata.TMDB_KEY, "")
            _save_setting(conn, metadata.TMDB_LEGACY_KEY, "")
            logger.info("settings: tmdb read token cleared")
        elif "tmdb_read_token" in payload or "tmdb_api_key" in payload:
            raw_token = payload.get("tmdb_read_token", payload.get("tmdb_api_key"))
            if not str(raw_token or "").strip():
                _save_setting(conn, metadata.TMDB_KEY, "")
                _save_setting(conn, metadata.TMDB_LEGACY_KEY, "")
                logger.info("settings: tmdb read token cleared")
            else:
                try:
                    token = tmdb_client.validate_read_token(raw_token)
                except tmdb_client.TMDBKeyError as exc:
                    return _fail(str(exc))
                _save_setting(conn, metadata.TMDB_KEY, token)
                _save_setting(conn, metadata.TMDB_LEGACY_KEY, "")
                logger.info("settings: tmdb read token set")
        for field, setting in (
            ("tvdb_api_key", metadata.TVDB_KEY),
            ("tvdb_pin", metadata.TVDB_PIN),
        ):
            if field not in payload:
                continue
            value = str(payload.get(field) or "").strip()
            if len(value) > 300 or any(ord(char) < 32 for char in value):
                return _fail(f"{field} is not valid.")
            _save_setting(conn, setting, value)
            logger.info("settings: %s %s", field, "set" if value else "cleared")
        if "language" in payload or "metadata_language" in payload:
            language = str(payload.get("language", payload.get("metadata_language")) or "").strip()
            if not _valid_language(language):
                return _fail("Language must look like en-US or es-ES.")
            _save_setting(conn, metadata.LANGUAGE, language)
        return jsonify({
            "ok": True,
            "tmdb": metadata.has_tmdb(conn),
            "tvdb": metadata.has_tvdb(conn),
            "language": metadata.language(conn),
        })
    except Exception as exc:
        return _error(exc)


@bp.post("/settings/media")
def save_media_settings():
    conn = _conn()
    try:
        payload = _payload()
        entries = {
            "movie_folder_template": (naming.MOVIE_FOLDER_TOKENS, naming.DEFAULT_MOVIE_FOLDER),
            "movie_file_template": (naming.MOVIE_FILE_TOKENS, naming.DEFAULT_MOVIE_FILE),
            "series_folder_template": (naming.SERIES_FOLDER_TOKENS, naming.DEFAULT_SERIES_FOLDER),
            "season_folder_template": (naming.SEASON_FOLDER_TOKENS, naming.DEFAULT_SEASON_FOLDER),
            "episode_file_template": (naming.EPISODE_FILE_TOKENS, naming.DEFAULT_EPISODE_FILE),
        }
        for key, (allowed, default) in entries.items():
            if key not in payload:
                continue
            try:
                value = naming.validate_template(str(payload.get(key) or ""), allowed)
            except naming.TemplateError as exc:
                return _fail(str(exc))
            _save_setting(conn, key, value)
        movie_values = {
            "title": "Example Movie", "year": 2024, "resolution": "1080p",
        }
        episode_values = {
            "series": "Example Series", "year": 2024, "season": 1, "episode": 2,
            "episode_title": "Pilot", "resolution": "1080p",
        }
        return jsonify({
            "ok": True,
            "preview": {
                "movie_folder": naming.render_component(
                    db.get_setting(conn, "movie_folder_template", naming.DEFAULT_MOVIE_FOLDER),
                    movie_values, naming.MOVIE_FOLDER_TOKENS),
                "movie_file": naming.render_component(
                    db.get_setting(conn, "movie_file_template", naming.DEFAULT_MOVIE_FILE),
                    movie_values, naming.MOVIE_FILE_TOKENS),
                "episode_file": naming.render_component(
                    db.get_setting(conn, "episode_file_template", naming.DEFAULT_EPISODE_FILE),
                    episode_values, naming.EPISODE_FILE_TOKENS),
            },
        })
    except Exception as exc:
        return _error(exc)


@bp.post("/settings/downloads")
def save_download_settings():
    conn = _conn()
    try:
        payload = _payload()
        if "concurrency" in payload:
            concurrency = _int(payload.get("concurrency"))
            if concurrency is None or not 1 <= concurrency <= 3:
                return _fail("Concurrent downloads must be between 1 and 3.")
            _save_setting(conn, "download_concurrency", str(concurrency))
            downloader.ensure_started()
        if "max_attempts" in payload:
            attempts = _int(payload.get("max_attempts"))
            if attempts is None or not 1 <= attempts <= 10:
                return _fail("Retries must be between 1 and 10.")
            _save_setting(conn, "download_max_attempts", str(attempts))
        numeric = {
            "probe_cache_hours": (0, 720),
            "probe_timeout_seconds": (5, 120),
            "probe_max_candidates": (1, 20),
            "probe_search_budget": (1, 200),
        }
        for key, (minimum, maximum) in numeric.items():
            if key not in payload:
                continue
            value = _int(payload.get(key))
            if value is None or not minimum <= value <= maximum:
                return _fail(f"{key} must be between {minimum} and {maximum}.")
            _save_setting(conn, key, str(value))
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(exc)


@bp.post("/settings/scheduler")
def save_scheduler_settings():
    conn = _conn()
    try:
        payload = _payload()
        limits = {
            "search_interval_hours": (1, 8760),
            "refresh_interval_hours": (1, 8760),
            "catalog_refresh_hours": (0, 8760),
        }
        for key, (minimum, maximum) in limits.items():
            if key not in payload:
                continue
            try:
                value = float(payload.get(key))
            except (TypeError, ValueError):
                return _fail(f"{key} must be a number of hours.")
            if not minimum <= value <= maximum:
                return _fail(f"{key} must be between {minimum} and {maximum}.")
            _save_setting(conn, key, str(value))
        return jsonify({"ok": True})
    except Exception as exc:
        return _error(exc)


# --------------------------------------------------------------------- backup
@bp.post("/backup/export")
def export_backup():
    conn = _conn()
    try:
        data = backup.export_zip(conn)
        name = f"iptv-charts-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        logger.info("backup: exported %d bytes", len(data))
        return send_file(
            io.BytesIO(data), mimetype="application/zip",
            as_attachment=True, download_name=name,
        )
    except Exception as exc:
        return _error(exc)


@bp.post("/backup/restore")
def restore_backup():
    conn = _conn()
    if request.content_length and request.content_length > backup.MAX_ZIP_BYTES:
        return _fail("That backup file is too large.", 413)
    data = request.get_data(cache=False)
    if not data:
        return _fail("Choose a backup ZIP first.")
    try:
        result = backup.restore(conn, data)
        return jsonify({"ok": True, **result})
    except backup.RestoreBusy as exc:
        return _fail(str(exc), 409)
    except ValueError as exc:
        return _fail(str(exc), 400)
    except Exception as exc:
        return _error(exc)


__all__ = ["bp"]
