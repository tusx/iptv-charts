"""The built-in download queue.

Downloads are durable rows in SQLite, not in-memory jobs: a container restart
must not lose a queue, and progress must survive long enough to be inspected.
The background worker claims one download job at a time, writes to
``media/.incomplete`` (same filesystem as the library), and hands the finished
file to ``library.import_download`` for an atomic rename and the naming
templates. The web process only inserts rows and reads progress.

The provider is the download source; there is no external download client.
Direct HTTP is used for normal files, and ffmpeg for HLS playlists.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import requests

from . import config, connections, db, jobs, library, naming, player, quality, streams

logger = logging.getLogger("iptv_charts.downloads")

ACTIVE = ("queued", "downloading", "importing")
TERMINAL = ("imported", "failed", "cancelled")
CHUNK = 1024 * 1024
PROGRESS_INTERVAL = 2.0
DISK_MARGIN = 256 * 1024 * 1024
_URL_IN_TEXT_RE = re.compile(r"https?://\S+")
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


class DownloadError(RuntimeError):
    """A download failed in a way that can be retried."""


class PermanentDownloadError(RuntimeError):
    """A download failed in a way that retrying will not fix."""


class DownloadCancelled(RuntimeError):
    """The user cancelled the download."""


class DownloadInterrupted(RuntimeError):
    """The app is shutting down; keep the temp file and resume next start."""


def _now() -> int:
    return int(time.time())


def _redact(text: str | None, limit: int = 600) -> str:
    value = _URL_IN_TEXT_RE.sub("<url>", str(text or ""))
    value = " ".join(value.split())
    return value[:limit]


def _resolve(conn, download: Mapping[str, Any]) -> streams.StreamRef:
    """Rebuild the provider URL from the identity captured at grab time."""
    try:
        return streams.download_stream(conn, download)
    except streams.StreamError as exc:
        raise PermanentDownloadError(str(exc)) from exc


class DownloadManager:
    """One durable download, executed by the background worker.

    There are deliberately no worker threads here any more. The worker loop
    claims a ``worker_jobs`` row, calls ``process`` for its download id, and
    gets back an outcome. That is how a sync, a probe search and a download
    can never overlap -- the worker runs one at a time.
    """

    def __init__(self) -> None:
        # Set by a graceful worker shutdown. Unlike a user cancel, an
        # interruption keeps the row and temp file for a resume.
        self._stop = threading.Event()

    # ------------------------------------------------------------- lifecycle
    @staticmethod
    def max_attempts(conn) -> int:
        try:
            return max(1, min(10, int(db.get_setting(conn, "download_max_attempts", "3") or 3)))
        except (TypeError, ValueError):
            return 3

    @staticmethod
    def concurrency(conn) -> int:
        # Retained for the settings API and old databases. The worker processes
        # one download at a time by design; this value is informational now.
        try:
            return max(1, min(3, int(db.get_setting(conn, "download_concurrency", "1") or 1)))
        except (TypeError, ValueError):
            return 1

    def _queue_job(self, conn, download_id: int, priority: int | None = None) -> None:
        row = db.get_one(
            conn, "SELECT priority FROM downloads WHERE id = ?", (int(download_id),)
        )
        value = int(row["priority"]) if row is not None else 0
        jobs.enqueue(
            conn, "download", {"download_id": int(download_id)},
            priority=self._priority(priority if priority is not None else value),
            dedupe_key=f"download:{int(download_id)}",
        )

    def _queue_queued(self, conn) -> int:
        rows = conn.execute(
            "SELECT id, priority FROM downloads WHERE status = 'queued' ORDER BY id"
        ).fetchall()
        for row in rows:
            self._queue_job(conn, int(row["id"]), int(row["priority"] or 0))
        return len(rows)

    def recover(self) -> None:
        """Requeue rows interrupted by a restart and recreate their queue jobs."""
        self._stop.clear()
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE downloads SET status = 'queued', error = NULL, "
                "cancel_requested = 0, next_attempt_at = NULL "
                "WHERE status IN ('downloading', 'importing')"
            )
            conn.commit()
            self._queue_queued(conn)
        finally:
            conn.close()

    def ensure_started(self) -> None:
        """Queue any durable download rows that do not have an active job.

        Kept as a compatibility facade for older callers. It never starts a
        thread: the worker is a separate process in production and a test
        harness may run one in-process.
        """
        conn = db.connect()
        try:
            self._queue_queued(conn)
        finally:
            conn.close()

    def shutdown(self, timeout: float = 3.0) -> None:
        # Deliberately not cancelling rows: a clean stop leaves the row and its
        # temp file in place for the next start to resume.
        self._stop.set()

    # ------------------------------------------------------------- one download
    def process(self, download_id: int) -> dict:
        """Run one queued download to a terminal state (or a retry).

        The worker has already claimed the surrounding job. This method owns
        the provider connection slot for the duration and returns an outcome
        the worker runner can translate into a job status.
        """
        conn = db.connect()
        download_id = int(download_id)
        cancel = threading.Event()
        watcher_stop = threading.Event()
        watcher: threading.Thread | None = None
        try:
            row = db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (download_id,))
            if row is None:
                return {"status": "cancelled", "message": "The download no longer exists."}
            if row["status"] != "queued":
                if row["status"] == "cancelled":
                    return {"status": "cancelled", "message": "Cancelled"}
                return {"status": "done", "message": row["status"]}

            job = dict(row)
            connection_source = job.get("source_id")
            job["attempts"] = int(job.get("attempts") or 0) + 1
            job["status"] = "downloading"

            def watch_cancel() -> None:
                wconn = None
                try:
                    wconn = db.connect()
                    while not watcher_stop.wait(0.5):
                        current = wconn.execute(
                            "SELECT status, cancel_requested FROM downloads WHERE id = ?",
                            (download_id,),
                        ).fetchone()
                        if current is None or current["status"] == "cancelled" \
                                or current["cancel_requested"]:
                            cancel.set()
                            return
                except Exception:
                    pass
                finally:
                    if wconn is not None:
                        wconn.close()

            def mark_downloading() -> bool:
                # The status guard matters: Cancel (or a credential change)
                # can retire a row while this job was waiting for a slot.
                cursor = conn.execute(
                    "UPDATE downloads SET status = 'downloading', "
                    "started_at = COALESCE(started_at, ?), attempts = ?, "
                    "next_attempt_at = NULL, error = NULL "
                    "WHERE id = ? AND status = 'queued'",
                    (_now(), job["attempts"], download_id),
                )
                conn.commit()
                return cursor.rowcount == 1

            def unstarted_outcome() -> dict:
                current = db.get_one(
                    conn, "SELECT status FROM downloads WHERE id = ?", (download_id,)
                )
                if current is not None and current["status"] == "queued":
                    return {"status": "queued", "retry": True,
                            "available_at": _now(),
                            "message": "Could not claim the download row; retrying."}
                if current is not None and current["status"] in ("imported", "failed"):
                    return {"status": "done", "message": current["status"]}
                return {"status": "cancelled", "message": "Cancelled"}

            watcher = threading.Thread(
                target=watch_cancel, name=f"iptv-download-cancel-{download_id}",
                daemon=True,
            )
            watcher.start()

            try:
                if connection_source is not None:
                    with connections.acquire(
                        conn, int(connection_source), purpose="Download",
                        timeout=3600.0, cancel=cancel,
                    ):
                        if not mark_downloading():
                            return unstarted_outcome()
                        job["_connection_source"] = None
                        self._process(conn, job, cancel)
                else:
                    if not mark_downloading():
                        return unstarted_outcome()
                    job["_connection_source"] = None
                    self._process(conn, job, cancel)
            except connections.ConnectionCancelled:
                return {"status": "cancelled", "message": "Cancelled"}

            result = db.get_one(
                conn,
                "SELECT status, error, next_attempt_at FROM downloads WHERE id = ?",
                (download_id,),
            )
            if result is None:
                return {"status": "done", "message": "Download row removed."}
            status = str(result["status"])
            if status == "queued":
                return {
                    "status": "queued", "retry": True,
                    "available_at": int(result["next_attempt_at"] or _now()),
                    "message": result["error"] or "Retrying download.",
                }
            if status == "cancelled":
                conn.execute(
                    "UPDATE downloads SET cancel_requested = 0 WHERE id = ?", (download_id,)
                )
                conn.commit()
                return {"status": "cancelled", "message": "Cancelled"}
            if status == "failed":
                conn.execute(
                    "UPDATE downloads SET cancel_requested = 0 WHERE id = ?", (download_id,)
                )
                conn.commit()
                return {
                    "status": "done",
                    "message": f"Download failed: {result['error'] or 'unknown error'}",
                    "result": {"status": "failed"},
                }
            conn.execute(
                "UPDATE downloads SET cancel_requested = 0 WHERE id = ?", (download_id,)
            )
            conn.commit()
            return {"status": "done", "message": "Imported", "result": {"status": "imported"}}
        except connections.ConnectionBusy as exc:
            return {"status": "queued", "retry": True, "available_at": _now(),
                    "message": str(exc)}
        except Exception as exc:
            logger.exception("downloads: #%d could not be processed: %s", download_id, exc)
            try:
                conn.execute(
                    "UPDATE downloads SET status = 'queued', error = ?, "
                    "next_attempt_at = ? WHERE id = ? AND status IN "
                    "('queued', 'downloading', 'importing')",
                    (_redact(str(exc)), _now() + 60, download_id),
                )
                conn.commit()
            except Exception:
                pass
            return {
                "status": "queued", "retry": True, "available_at": _now() + 60,
                "message": _redact(str(exc)),
            }
        finally:
            watcher_stop.set()
            if watcher is not None:
                watcher.join(timeout=2.0)
            conn.close()

    # ------------------------------------------------------------------ queue
    @staticmethod
    def _json(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _priority(value: Any) -> int:
        try:
            return max(-100, min(100, int(value or 0)))
        except (TypeError, ValueError):
            return 0

    def enqueue_movie(self, conn, movie: Mapping[str, Any], release: Mapping[str, Any],
                      *, reason: str = "auto", priority: int | None = None,
                      mode: str = "main", allow_active: bool = False) -> dict:
        mode = "alternate" if str(mode).lower() == "alternate" else "main"
        movie_id = int(movie["id"])
        if mode == "main":
            if not allow_active and library.active_download(
                    conn, movie_id=movie_id, mode="main"):
                raise ValueError("A download for this movie is already active.")
        else:
            if library.active_download(conn, movie_id=movie_id, mode="alternate"):
                raise ValueError("An additional download for this movie is already active.")
            existing = db.get_one(
                conn,
                "SELECT id, file_path FROM movie_alternates "
                "WHERE movie_id = ? AND source_id = ? AND remote_id = ?",
                (movie_id, int(release["source_id"]), str(release["remote_id"])),
            )
            if existing is not None:
                raise ValueError("That candidate is already saved as an additional version.")
            same = db.get_one(
                conn,
                "SELECT id FROM downloads WHERE movie_id = ? AND mode = 'alternate' "
                "AND source_id = ? AND provider_remote_id = ? "
                "AND status IN ('queued', 'downloading', 'importing')",
                (movie_id, int(release["source_id"]), str(release["remote_id"])),
            )
            if same is not None:
                raise ValueError("That candidate is already queued as an additional version.")
        now = _now()
        probe = release.get("probe") if isinstance(release, Mapping) else None
        priority_value = (priority if priority is not None
                          else release.get("priority", movie.get("priority", 0)))
        cursor = conn.execute(
            """
            INSERT INTO downloads (
                kind, movie_id, source_id, source_name, provider_kind,
                provider_remote_id, container_ext, title, resolution, source,
                quality_label, audio_languages, subtitle_languages, media_info,
                priority, mode, release_name, reason, status, attempts, max_attempts,
                requested_at
            ) VALUES ('movie', ?, ?, ?, 'vod', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'queued', 0, ?, ?)
            """,
            (
                int(movie["id"]), int(release["source_id"]), release.get("source_name"),
                str(release["remote_id"]), release.get("container_ext"),
                movie["title"], release.get("resolution") or "unknown",
                release.get("source"), release.get("quality_label"),
                self._json(release.get("audio_languages")),
                self._json(release.get("subtitle_languages")),
                self._json(probe), self._priority(priority_value), mode,
                release.get("name"), reason, self.max_attempts(conn), now,
            ),
        )
        download_id = int(cursor.lastrowid)
        library.add_history(
            conn, "grabbed", movie["title"], kind="movie", movie_id=movie["id"],
            download_id=download_id,
            message=(
                f"{release.get('resolution') or 'unknown'} from "
                f"{release.get('source_name')}"
                + (" (additional version)" if mode == "alternate" else "")
            ),
        )
        conn.commit()
        logger.info("downloads: queued movie %s (#%d, %s)", movie["title"],
                    download_id, release.get("resolution"))
        self._queue_job(conn, download_id, priority_value)
        return dict(db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (download_id,)))

    def enqueue_episode(self, conn, series: Mapping[str, Any], episode: Mapping[str, Any],
                        candidate: Mapping[str, Any], provider_episode: Mapping[str, Any],
                        *, reason: str = "auto", priority: int | None = None) -> dict:
        if library.active_download(conn, episode_id=int(episode["id"])):
            raise ValueError("A download for this episode is already active.")
        label = (f"{series['title']} S{int(episode['season']):02d}"
                 f"E{int(episode['episode']):02d}")
        now = _now()
        probe = candidate.get("probe") if isinstance(candidate, Mapping) else None
        priority_value = (priority if priority is not None
                          else candidate.get("priority", series.get("priority", 0)))
        cursor = conn.execute(
            """
            INSERT INTO downloads (
                kind, movie_id, series_id, episode_id, source_id, source_name,
                provider_kind, provider_remote_id, episode_stream_id,
                container_ext, title, episode_title, season, episode, resolution,
                source, quality_label, audio_languages, subtitle_languages,
                media_info, priority, release_name, reason, status, attempts,
                max_attempts, requested_at
            ) VALUES ('episode', NULL, ?, ?, ?, ?, 'series', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            (
                int(series["id"]), int(episode["id"]), int(candidate["source_id"]),
                candidate.get("source_name"), str(candidate["remote_id"]),
                str(provider_episode["stream_id"]),
                provider_episode.get("container_ext") or "mp4",
                series["title"], episode.get("title"), int(episode["season"]),
                int(episode["episode"]), candidate.get("resolution") or "unknown",
                candidate.get("source"), candidate.get("quality_label"),
                self._json(candidate.get("audio_languages")),
                self._json(candidate.get("subtitle_languages")),
                self._json(probe), self._priority(priority_value),
                candidate.get("name"), reason, self.max_attempts(conn), now,
            ),
        )
        download_id = int(cursor.lastrowid)
        library.add_history(
            conn, "grabbed", label, kind="episode", series_id=series["id"],
            episode_id=episode["id"], download_id=download_id,
            message=f"{candidate.get('resolution') or 'unknown'} from {candidate.get('source_name')}",
        )
        conn.commit()
        logger.info("downloads: queued episode %s (#%d, %s)", label, download_id,
                    candidate.get("resolution"))
        self._queue_job(conn, download_id, priority_value)
        return dict(db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (download_id,)))

    def cancel_download(self, conn, download_id: int) -> dict:
        row = db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (int(download_id),))
        if row is None:
            raise LookupError("That download no longer exists.")
        if row["status"] in TERMINAL:
            raise ValueError("That download has already finished.")
        if row["status"] == "importing":
            raise ValueError("That download is already importing and can no longer be cancelled.")
        if row["status"] == "queued":
            # Nothing has started yet, so this is final immediately. Retire
            # the queue row too; otherwise it would keep the dedupe key active
            # and a later Retry could attach to a job that is about to vanish.
            conn.execute(
                "UPDATE downloads SET status = 'cancelled', finished_at = ?, "
                "error = 'Cancelled', cancel_requested = 0 WHERE id = ?",
                (_now(), int(download_id)),
            )
            library.add_history(
                conn, "cancelled", row["title"], kind=row["kind"],
                movie_id=row["movie_id"], series_id=row["series_id"],
                episode_id=row["episode_id"], download_id=row["id"],
            )
            jobs.cancel_by_key(conn, f"download:{int(download_id)}")
        else:
            # The worker owns the transfer. The flag is its only channel.
            conn.execute(
                "UPDATE downloads SET cancel_requested = 1, error = 'Cancelling…' "
                "WHERE id = ?",
                (int(download_id),),
            )
        conn.commit()
        return dict(db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (int(download_id),)))

    def cancel_for(self, conn, *, movie_id: int | None = None,
                   series_id: int | None = None, mode: str | None = None) -> None:
        clauses = ["status IN (?, ?, ?)"]
        params: list[Any] = list(ACTIVE)
        if movie_id is not None:
            clauses.append("movie_id = ?")
            params.append(int(movie_id))
        if mode in ("main", "alternate"):
            clauses.append("mode = ?")
            params.append(mode)
        if series_id is not None:
            clauses.append("series_id = ?")
            params.append(int(series_id))
        rows = conn.execute(
            f"SELECT * FROM downloads WHERE {' AND '.join(clauses)}", params
        ).fetchall()
        for row in rows:
            if row["status"] == "queued":
                conn.execute(
                    "UPDATE downloads SET status = 'cancelled', finished_at = ?, "
                    "error = 'Cancelled', cancel_requested = 0 WHERE id = ?",
                    (_now(), row["id"]),
                )
                library.add_history(
                    conn, "cancelled", row["title"], kind=row["kind"],
                    movie_id=row["movie_id"], series_id=row["series_id"],
                    episode_id=row["episode_id"], download_id=row["id"],
                )
                jobs.cancel_by_key(conn, f"download:{int(row['id'])}")
            else:
                conn.execute(
                    "UPDATE downloads SET cancel_requested = 1, error = 'Cancelling…' "
                    "WHERE id = ?",
                    (row["id"],),
                )
        conn.commit()

    def retry_download(self, conn, download_id: int) -> dict:
        row = db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (int(download_id),))
        if row is None:
            raise LookupError("That download no longer exists.")
        if row["status"] not in ("failed", "cancelled"):
            raise ValueError("Only a failed or cancelled download can be retried.")
        temp_path = row["temp_path"]
        if temp_path and not Path(temp_path).exists():
            temp_path = None
        conn.execute(
            "UPDATE downloads SET status = 'queued', attempts = 0, error = NULL, "
            "finished_at = NULL, next_attempt_at = NULL, temp_path = ?, "
            "bytes_done = 0, cancel_requested = 0 WHERE id = ?",
            (temp_path, int(download_id)),
        )
        conn.commit()
        self._queue_job(conn, int(download_id), int(row["priority"] or 0))
        return dict(db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (int(download_id),)))

    def remove_download(self, conn, download_id: int) -> None:
        row = db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (int(download_id),))
        if row is None:
            raise LookupError("That download no longer exists.")
        if row["status"] in ACTIVE:
            raise ValueError("Cancel an active download before removing it.")
        conn.execute("DELETE FROM downloads WHERE id = ?", (int(download_id),))
        conn.commit()

    # -------------------------------------------------------------- processing
    def _process(self, conn, job: dict, cancel: threading.Event) -> None:
        download_id = int(job["id"])
        connection_source = job.pop("_connection_source", None)
        temp_path: Path | None = None
        try:
            resolved = _resolve(conn, job)
            # The resolved extension can differ from the grabbed one: HLS is
            # remuxed to mkv even when the provider called it m3u8. Keep the
            # row in step so the imported filename gets the real container.
            job["container_ext"] = resolved.container_ext
            temp_path = self._temp_path(job, resolved)
            conn.execute(
                "UPDATE downloads SET container_ext = ?, temp_path = ?, "
                "status = 'downloading' WHERE id = ?",
                (resolved.container_ext, str(temp_path), download_id),
            )
            conn.commit()

            logger.info("downloads: started #%d %s (%s)", download_id, job["title"],
                        job["resolution"])
            if resolved.hls:
                total = self._download_hls(conn, job, resolved, temp_path, cancel)
            else:
                total = self._download_http(conn, job, resolved, temp_path, cancel)

            probed = _probe_resolution(temp_path)
            if probed:
                job["resolution"] = probed
                conn.execute("UPDATE downloads SET resolution = ? WHERE id = ?",
                             (probed, download_id))
                conn.commit()

            conn.execute("UPDATE downloads SET status = 'importing' WHERE id = ?",
                         (download_id,))
            conn.commit()
            final = library.import_download(conn, job, temp_path)
            conn.execute(
                "UPDATE downloads SET status = 'imported', final_path = ?, temp_path = NULL, "
                "bytes_done = COALESCE(?, bytes_done), bytes_total = COALESCE(?, bytes_total), "
                "finished_at = ?, error = NULL WHERE id = ?",
                (str(final), total, total, _now(), download_id),
            )
            conn.commit()
            logger.info("downloads: finished #%d -> %s", download_id, final)
        except DownloadInterrupted:
            self._mark_interrupted(conn, job, download_id)
        except DownloadCancelled:
            self._mark_cancelled(conn, job, download_id, temp_path)
        except PermanentDownloadError as exc:
            _delete_temp(temp_path)
            message = _redact(str(exc))
            conn.execute(
                "UPDATE downloads SET status = 'failed', finished_at = ?, error = ?, "
                "temp_path = NULL WHERE id = ?",
                (_now(), message, download_id),
            )
            library.add_history(conn, "failed", job["title"], kind=job["kind"],
                                movie_id=job["movie_id"], series_id=job["series_id"],
                                episode_id=job["episode_id"], download_id=download_id,
                                message=message)
            conn.commit()
            logger.warning("downloads: #%d failed permanently: %s", download_id, message)
        except Exception as exc:
            # A cancellation can surface as a broken read rather than as a
            # clean DownloadCancelled (requests may be blocked in iter_content
            # when the user presses Cancel). Cancellation wins over retrying.
            if cancel.is_set():
                self._mark_cancelled(conn, job, download_id, temp_path)
                return
            if self._stop.is_set():
                self._mark_interrupted(conn, job, download_id)
                return
            message = _redact(f"{type(exc).__name__}: {exc}")
            attempts = int(job.get("attempts") or 1)
            max_attempts = int(job.get("max_attempts") or 3)
            if attempts < max_attempts:
                wait = min(900, 60 * (2 ** (attempts - 1)))
                conn.execute(
                    "UPDATE downloads SET status = 'queued', next_attempt_at = ?, error = ? "
                    "WHERE id = ?",
                    (_now() + wait, message, download_id),
                )
                conn.commit()
                logger.warning("downloads: #%d failed (attempt %d/%d), retrying in %ds: %s",
                               download_id, attempts, max_attempts, wait, message)
            else:
                _delete_temp(temp_path)
                conn.execute(
                    "UPDATE downloads SET status = 'failed', finished_at = ?, error = ?, "
                    "temp_path = NULL WHERE id = ?",
                    (_now(), message, download_id),
                )
                library.add_history(conn, "failed", job["title"], kind=job["kind"],
                                    movie_id=job["movie_id"], series_id=job["series_id"],
                                    episode_id=job["episode_id"], download_id=download_id,
                                    message=message)
                conn.commit()
                logger.warning("downloads: #%d failed after %d attempts: %s",
                               download_id, attempts, message)
        finally:
            if connection_source is not None:
                connections.release(int(connection_source))

    @staticmethod
    def _mark_interrupted(conn, job: Mapping[str, Any], download_id: int) -> None:
        """Leave the row queued and the temp file in place for the next start."""
        conn.execute(
            "UPDATE downloads SET status = 'queued', next_attempt_at = NULL, "
            "error = 'Interrupted by shutdown; will resume.', "
            "attempts = MAX(0, attempts - 1) WHERE id = ?",
            (download_id,),
        )
        conn.commit()
        logger.info("downloads: interrupted #%d; it will resume on the next start",
                    download_id)

    @staticmethod
    def _mark_cancelled(conn, job: Mapping[str, Any], download_id: int,
                        temp_path: Path | None) -> None:
        _delete_temp(temp_path)
        conn.execute(
            "UPDATE downloads SET status = 'cancelled', finished_at = ?, "
            "error = 'Cancelled', temp_path = NULL WHERE id = ?",
            (_now(), download_id),
        )
        library.add_history(conn, "cancelled", job["title"], kind=job["kind"],
                            movie_id=job["movie_id"], series_id=job["series_id"],
                            episode_id=job["episode_id"], download_id=download_id)
        conn.commit()
        logger.info("downloads: cancelled #%d", download_id)

    def _temp_path(self, job: Mapping[str, Any], resolved: streams.StreamRef) -> Path:
        existing = job.get("temp_path")
        if existing:
            try:
                path = naming.confine(existing, config.media_dir())
                if path.exists():
                    return path
            except naming.TemplateError:
                pass
        suffix = "mkv" if resolved.hls else naming.safe_extension(resolved.container_ext)
        return config.incomplete_dir() / f"download-{job['id']}-{uuid.uuid4().hex}.{suffix}"

    def _progress(self, conn, download_id: int, done: int, total: int | None,
                  state: dict) -> None:
        now = time.monotonic()
        if now - state.get("last", 0.0) < PROGRESS_INTERVAL:
            return
        state["last"] = now
        conn.execute(
            "UPDATE downloads SET bytes_done = ?, bytes_total = ? WHERE id = ?",
            (int(done), int(total) if total is not None else None, download_id),
        )
        conn.commit()

    def _download_http(self, conn, job: Mapping[str, Any], resolved: streams.StreamRef,
                       temp_path: Path, cancel: threading.Event) -> int | None:
        download_id = int(job["id"])
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        existing = temp_path.stat().st_size if temp_path.exists() else 0
        headers = {"Accept-Encoding": "identity", **resolved.headers}
        state: dict = {"last": 0.0}

        # One resume attempt, then a clean restart: a server that ignores Range
        # must not append a second copy to the file.
        for use_range in ((True, False) if existing else (False,)):
            offset = existing if use_range else 0
            request_headers = dict(headers)
            if offset:
                request_headers["Range"] = f"bytes={offset}-"
            try:
                response = requests.get(
                    resolved.url, headers=request_headers, stream=True,
                    timeout=(15, 60), allow_redirects=True,
                )
            except requests.RequestException as exc:
                raise DownloadError(f"Could not reach the provider: {exc}") from exc

            try:
                if response.status_code == 416 and offset:
                    _delete_temp(temp_path)
                    existing = 0
                    continue
                if response.status_code not in (200, 206):
                    status = response.status_code
                    if status in (401, 403, 404, 410):
                        raise PermanentDownloadError(
                            f"The provider refused the stream (HTTP {status})."
                        )
                    raise DownloadError(f"The provider returned HTTP {status}.")
                content_type = (response.headers.get("Content-Type") or "").lower()
                if content_type.startswith("text/html"):
                    raise PermanentDownloadError(
                        "The provider returned a web page instead of a video."
                    )

                total: int | None = None
                if response.status_code == 206:
                    match = _CONTENT_RANGE_RE.search(response.headers.get("Content-Range", ""))
                    if match and match.group(3) != "*":
                        total = int(match.group(3))
                    elif response.headers.get("Content-Length"):
                        total = offset + int(response.headers["Content-Length"])
                elif response.headers.get("Content-Length"):
                    total = int(response.headers["Content-Length"])

                free = shutil.disk_usage(config.media_dir()).free
                required = (total + DISK_MARGIN) if total else DISK_MARGIN
                if free < required:
                    raise PermanentDownloadError(
                        "Not enough free space for this download. "
                        f"Need about {required // (1024 * 1024)} MB more."
                    )

                mode = "ab" if response.status_code == 206 and offset else "wb"
                done = offset if mode == "ab" else 0
                with temp_path.open(mode) as handle:
                    try:
                        for chunk in response.iter_content(CHUNK):
                            if self._stop.is_set():
                                raise DownloadInterrupted()
                            if cancel.is_set():
                                raise DownloadCancelled()
                            if not chunk:
                                continue
                            handle.write(chunk)
                            done += len(chunk)
                            self._progress(conn, download_id, done, total, state)
                            free = shutil.disk_usage(config.media_dir()).free
                            if free < DISK_MARGIN:
                                raise PermanentDownloadError("The disk filled up during the download.")
                    except requests.RequestException as exc:
                        if self._stop.is_set():
                            raise DownloadInterrupted() from exc
                        if cancel.is_set():
                            raise DownloadCancelled() from exc
                        raise DownloadError(f"The provider connection broke: {exc}") from exc
                self._progress(conn, download_id, done, total, {"last": 0.0})
                if total is not None and done < total:
                    raise DownloadError(
                        f"The download ended early ({done} of {total} bytes)."
                    )
                if done <= 0:
                    raise DownloadError("The provider sent an empty file.")
                return total if total is not None else done
            finally:
                response.close()

        raise DownloadError("The provider would not resume or restart the download.")

    def _download_hls(self, conn, job: Mapping[str, Any], resolved: streams.StreamRef,
                      temp_path: Path, cancel: threading.Event) -> int | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise PermanentDownloadError(
                "HLS downloads need ffmpeg, which is not installed in this image. "
                "Rebuild the container from the current Containerfile."
            )
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        if temp_path.suffix.lower() != ".mkv":
            temp_path = temp_path.with_suffix(".mkv")
        free = shutil.disk_usage(config.media_dir()).free
        if free < DISK_MARGIN:
            raise PermanentDownloadError("Not enough free space for this download.")

        command = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        ]
        command += streams.ffmpeg_header_args(resolved.headers)
        command += [
            "-protocol_whitelist", "http,https,tcp,tls,crypto",
            "-i", resolved.url,
            "-c", "copy",
            "-progress", "pipe:1", "-nostats",
            "-y", str(temp_path),
        ]

        logger.debug("downloads: ffmpeg %s", " ".join(
            player.redact_url(part) if part.startswith("http") else part
            for part in command
        ))
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise DownloadError(f"Could not start ffmpeg: {exc}") from exc

        stderr_tail: list[str] = []

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                line = line.strip()
                if line:
                    stderr_tail.append(line)
                    del stderr_tail[:-20]

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        state: dict = {"last": 0.0}
        try:
            assert process.stdout is not None
            while True:
                if self._stop.is_set():
                    _terminate(process)
                    raise DownloadInterrupted()
                if cancel.is_set():
                    _terminate(process)
                    raise DownloadCancelled()
                line = process.stdout.readline()
                if not line:
                    break
                if line.startswith("progress=") and line.strip() == "progress=end":
                    break
                if temp_path.exists():
                    self._progress(conn, int(job["id"]), temp_path.stat().st_size,
                                   None, state)
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _terminate(process)
            raise DownloadError("ffmpeg did not exit after finishing the stream.")
        finally:
            stderr_thread.join(timeout=2)
            if process.poll() is None:
                _terminate(process)

        if self._stop.is_set():
            raise DownloadInterrupted()
        if cancel.is_set():
            raise DownloadCancelled()
        if process.returncode != 0:
            message = " ".join(stderr_tail[-3:]) or f"ffmpeg exited with {process.returncode}"
            raise DownloadError(_redact(message))
        if not temp_path.exists() or temp_path.stat().st_size <= 0:
            raise DownloadError("ffmpeg produced an empty file.")
        return temp_path.stat().st_size


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


def _delete_temp(path: Path | None) -> None:
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _probe_resolution(path: Path) -> str | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        raise PermanentDownloadError(
            "The downloaded file is not a playable video."
        )
    try:
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        width = int(streams[0]["width"]) if streams else None
        height = int(streams[0]["height"]) if streams else None
    except (ValueError, KeyError, TypeError, IndexError):
        return None
    return quality.resolution_from_dimensions(width, height)


# ------------------------------------------------------------------ facade
_manager = DownloadManager()


def start() -> None:
    _manager.recover()


def recover() -> None:
    """Requeue interrupted rows and recreate their queue jobs (worker startup)."""
    _manager.recover()


def ensure_started() -> None:
    _manager.ensure_started()


def process_download(download_id: int) -> dict:
    """Execute one queued download. Used by the background worker only."""
    return _manager.process(int(download_id))


def shutdown(timeout: float = 3.0) -> None:
    _manager.shutdown(timeout)


def enqueue_movie(conn, movie, release, *, reason: str = "auto",
                  mode: str = "main", allow_active: bool = False) -> dict:
    return _manager.enqueue_movie(
        conn, movie, release, reason=reason, mode=mode, allow_active=allow_active
    )


def enqueue_episode(conn, series, episode, candidate, provider_episode,
                    *, reason: str = "auto") -> dict:
    return _manager.enqueue_episode(conn, series, episode, candidate,
                                    provider_episode, reason=reason)


def cancel_download(conn, download_id: int) -> dict:
    return _manager.cancel_download(conn, download_id)


def cancel_for(conn, *, movie_id=None, series_id=None, mode=None) -> None:
    _manager.cancel_for(conn, movie_id=movie_id, series_id=series_id, mode=mode)


def retry_download(conn, download_id: int) -> dict:
    return _manager.retry_download(conn, download_id)


def remove_download(conn, download_id: int) -> None:
    _manager.remove_download(conn, download_id)


def snapshot(conn) -> dict:
    rows = conn.execute(
        "SELECT * FROM downloads WHERE status IN (?, ?, ?) "
        "ORDER BY requested_at DESC, id DESC",
        ACTIVE,
    ).fetchall()
    active = []
    for row in rows:
        item = dict(row)
        total = item.get("bytes_total")
        done = item.get("bytes_done") or 0
        item["fraction"] = (done / total) if total else None
        active.append(item)
    recent_rows = conn.execute(
        "SELECT * FROM downloads WHERE status IN ('failed', 'cancelled') "
        "ORDER BY id DESC LIMIT 50"
    ).fetchall()
    history_rows = conn.execute(
        "SELECT * FROM history ORDER BY id DESC LIMIT 150"
    ).fetchall()
    return {
        "active": active,
        "recent": [dict(row) for row in recent_rows],
        "history": [dict(row) for row in history_rows],
        "queued": sum(1 for item in active if item["status"] == "queued"),
        "downloading": sum(1 for item in active if item["status"] == "downloading"),
    }


__all__ = [
    "DownloadManager",
    "cancel_download",
    "cancel_for",
    "ensure_started",
    "enqueue_episode",
    "enqueue_movie",
    "process_download",
    "recover",
    "remove_download",
    "retry_download",
    "shutdown",
    "snapshot",
    "start",
]
