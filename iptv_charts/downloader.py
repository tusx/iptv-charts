"""The built-in download queue.

Downloads are durable rows in SQLite, not in-memory jobs: a container restart
must not lose a queue, and progress must survive long enough to be inspected.
Workers claim rows atomically, write to ``media/.incomplete`` (same filesystem
as the library), and hand the finished file to ``library.import_download`` for
an atomic rename and the naming templates.

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

from . import config, connections, db, library, naming, player, quality, streams

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
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[int, threading.Thread] = {}
        self._cancels: dict[int, threading.Event] = {}
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
        try:
            return max(1, min(3, int(db.get_setting(conn, "download_concurrency", "1") or 1)))
        except (TypeError, ValueError):
            return 1

    def recover(self) -> None:
        """Requeue rows interrupted by a restart, then start the workers."""
        conn = db.connect()
        try:
            conn.execute(
                "UPDATE downloads SET status = 'queued', error = NULL, "
                "next_attempt_at = NULL WHERE status IN ('downloading', 'importing')"
            )
            conn.commit()
        finally:
            conn.close()
        self.ensure_started()

    def ensure_started(self) -> None:
        conn = db.connect()
        try:
            allowed = self.concurrency(conn)
        finally:
            conn.close()
        with self._lock:
            for index in range(allowed):
                thread = self._workers.get(index)
                if thread is not None and thread.is_alive():
                    continue
                thread = threading.Thread(
                    target=self._worker, args=(index,), name=f"iptv-download-{index + 1}",
                    daemon=True,
                )
                self._workers[index] = thread
                thread.start()

    def _worker_exit(self, index: int) -> None:
        with self._lock:
            if self._workers.get(index) is threading.current_thread():
                self._workers.pop(index, None)

    def shutdown(self, timeout: float = 3.0) -> None:
        # Deliberately not setting the cancel events: a clean stop should leave
        # the row and its temp file in place so the next start can resume,
        # unlike a user cancel, which is final.
        self._stop.set()
        with self._lock:
            threads = list(self._workers.values())
        for thread in threads:
            thread.join(timeout=timeout)

    def _worker(self, index: int) -> None:
        conn = None
        try:
            conn = db.connect()
            while not self._stop.is_set():
                allowed = self.concurrency(conn)
                if index >= allowed:
                    break
                job = self._claim(conn)
                if job is None:
                    time.sleep(1.0)
                    continue
                self._process(conn, job)
        except Exception as exc:  # never let one worker take the queue down
            logger.exception("download worker stopped: %s", exc)
        finally:
            if conn is not None:
                conn.close()
            self._worker_exit(index)

    def _claim(self, conn) -> dict | None:
        """Claim the highest-priority row whose playlist has a free slot.

        The connection slot is acquired before the row is marked downloading
        and held until ``_process`` finishes. When the playlist's limit is
        reached, a lower-priority row from another playlist may run; that is
        deliberate, because waiting for one full download would leave a
        second provider idle.
        """
        acquired_source: int | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM downloads WHERE status = 'queued' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY priority DESC, requested_at, id LIMIT 50",
                (_now(),),
            ).fetchall()
            row = None
            limits: dict[int, int] = {}
            for candidate in rows:
                source_id = candidate["source_id"]
                if source_id is None:
                    # Let it fail with a clear permanent error; no provider
                    # call is made for it, so no slot is needed.
                    row = candidate
                    break
                source_id = int(source_id)
                limit = limits.get(source_id)
                if limit is None:
                    limit = connections.effective_limit(conn, source_id)
                    limits[source_id] = limit
                if connections.try_acquire(source_id, limit=limit):
                    acquired_source = source_id
                    row = candidate
                    break
            if row is None:
                conn.commit()
                return None
            attempts = int(row["attempts"]) + 1
            cursor = conn.execute(
                "UPDATE downloads SET status = 'downloading', "
                "started_at = COALESCE(started_at, ?), attempts = ?, "
                "next_attempt_at = NULL, error = NULL "
                "WHERE id = ? AND status = 'queued'",
                (_now(), attempts, row["id"]),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                if acquired_source is not None:
                    connections.release(acquired_source)
                return None
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            if acquired_source is not None:
                connections.release(acquired_source)
            return None
        job = dict(row)
        job["attempts"] = attempts
        job["status"] = "downloading"
        job["_connection_source"] = acquired_source
        return job

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
                      *, reason: str = "auto", priority: int | None = None) -> dict:
        if library.active_download(conn, movie_id=int(movie["id"])):
            raise ValueError("A download for this movie is already active.")
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
                priority, release_name, reason, status, attempts, max_attempts,
                requested_at
            ) VALUES ('movie', ?, ?, ?, 'vod', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'queued', 0, ?, ?)
            """,
            (
                int(movie["id"]), int(release["source_id"]), release.get("source_name"),
                str(release["remote_id"]), release.get("container_ext"),
                movie["title"], release.get("resolution") or "unknown",
                release.get("source"), release.get("quality_label"),
                self._json(release.get("audio_languages")),
                self._json(release.get("subtitle_languages")),
                self._json(probe), self._priority(priority_value),
                release.get("name"), reason, self.max_attempts(conn), now,
            ),
        )
        download_id = int(cursor.lastrowid)
        library.add_history(
            conn, "grabbed", movie["title"], kind="movie", movie_id=movie["id"],
            download_id=download_id,
            message=f"{release.get('resolution') or 'unknown'} from {release.get('source_name')}",
        )
        conn.commit()
        logger.info("downloads: queued movie %s (#%d, %s)", movie["title"],
                    download_id, release.get("resolution"))
        self.ensure_started()
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
        self.ensure_started()
        return dict(db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (download_id,)))

    def cancel_download(self, conn, download_id: int) -> dict:
        row = db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (int(download_id),))
        if row is None:
            raise LookupError("That download no longer exists.")
        if row["status"] in TERMINAL:
            raise ValueError("That download has already finished.")
        with self._lock:
            event = self._cancels.get(int(download_id))
        if event is not None:
            event.set()
            conn.execute(
                "UPDATE downloads SET error = 'Cancelling…' WHERE id = ?", (int(download_id),)
            )
        else:
            conn.execute(
                "UPDATE downloads SET status = 'cancelled', finished_at = ?, "
                "error = 'Cancelled' WHERE id = ?", (_now(), int(download_id))
            )
            library.add_history(
                conn, "cancelled", row["title"], kind=row["kind"],
                movie_id=row["movie_id"], series_id=row["series_id"],
                episode_id=row["episode_id"], download_id=row["id"],
            )
        conn.commit()
        return dict(db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (int(download_id),)))

    def cancel_for(self, conn, *, movie_id: int | None = None,
                   series_id: int | None = None) -> None:
        clauses = ["status IN (?, ?, ?)"]
        params: list[Any] = list(ACTIVE)
        if movie_id is not None:
            clauses.append("movie_id = ?")
            params.append(int(movie_id))
        if series_id is not None:
            clauses.append("series_id = ?")
            params.append(int(series_id))
        rows = conn.execute(
            f"SELECT * FROM downloads WHERE {' AND '.join(clauses)}", params
        ).fetchall()
        for row in rows:
            with self._lock:
                event = self._cancels.get(int(row["id"]))
            if event is not None:
                event.set()
            else:
                conn.execute(
                    "UPDATE downloads SET status = 'cancelled', finished_at = ?, "
                    "error = 'Cancelled' WHERE id = ?", (_now(), row["id"])
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
            "finished_at = NULL, next_attempt_at = NULL, temp_path = ?, bytes_done = 0 "
            "WHERE id = ?",
            (temp_path, int(download_id)),
        )
        conn.commit()
        self.ensure_started()
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
    def _process(self, conn, job: dict) -> None:
        download_id = int(job["id"])
        connection_source = job.pop("_connection_source", None)
        cancel = threading.Event()
        with self._lock:
            self._cancels[download_id] = cancel
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
            with self._lock:
                self._cancels.pop(download_id, None)

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


def ensure_started() -> None:
    _manager.ensure_started()


def shutdown(timeout: float = 3.0) -> None:
    _manager.shutdown(timeout)


def enqueue_movie(conn, movie, release, *, reason: str = "auto") -> dict:
    return _manager.enqueue_movie(conn, movie, release, reason=reason)


def enqueue_episode(conn, series, episode, candidate, provider_episode,
                    *, reason: str = "auto") -> dict:
    return _manager.enqueue_episode(conn, series, episode, candidate,
                                    provider_episode, reason=reason)


def cancel_download(conn, download_id: int) -> dict:
    return _manager.cancel_download(conn, download_id)


def cancel_for(conn, *, movie_id=None, series_id=None) -> None:
    _manager.cancel_for(conn, movie_id=movie_id, series_id=series_id)


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
    "remove_download",
    "retry_download",
    "shutdown",
    "snapshot",
    "start",
]
