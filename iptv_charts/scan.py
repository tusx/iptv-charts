"""Library scan/import: find media already on disk and match it to metadata.

A scan is deliberately a two-step operation. It proposes matches and stores
them in scan_results; the user reviews them on /scan before anything is
attached. Guessing wrong is much worse than leaving a file unmatched, because
a wrong attachment later feeds renames, upgrades and deletions.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import shutil
import time
from pathlib import Path

from . import config, db, library, matching, metadata, naming, quality

logger = logging.getLogger("iptv_charts.scan")

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".webm", ".mpg", ".mpeg",
}
AUTO_IMPORT_CONFIDENCE = 0.85


def _now() -> int:
    return int(time.time())


def _walk(root: Path):
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".")
            and name not in {".incomplete", "@eaDir"}
            and not Path(directory, name).is_symlink()
        ]
        for filename in filenames:
            if filename.startswith(".") or filename.endswith(".part"):
                continue
            path = Path(directory) / filename
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
            except (OSError, ValueError):
                logger.warning("scan: skipping a symlink outside the media library: %s", path)
                continue
            yield resolved


def _probe(path: Path) -> tuple[str | None, str | None]:
    """Return (resolution, error) using ffprobe when it is available."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"ffprobe could not run: {exc}"
    if result.returncode != 0:
        return None, "ffprobe could not read the file as video."
    try:
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        height = int(streams[0]["height"]) if streams else None
    except (TypeError, ValueError, KeyError, IndexError):
        return None, None
    return quality.resolution_from_height(height), None


def _match_library_rows(title: str, year: int | None,
                        rows: list[dict]) -> tuple[dict | None, float]:
    titles = matching.title_variants(title)
    if not titles:
        return None, 0.0
    best: tuple[dict, float] | None = None
    for candidate in rows:
        aliases = matching.aliases_from_row(candidate)
        if not aliases:
            continue
        score = max(
            (matching.similarity(source, alias)
             for source in titles for alias in aliases),
            default=0.0,
        )
        candidate_year = candidate.get("year")
        if year and candidate_year:
            difference = abs(int(year) - int(candidate_year))
            if difference == 0:
                score += 0.08
            elif difference == 1:
                score += 0.03
            else:
                score -= 0.25
        elif year and not candidate_year:
            score -= 0.02
        score = round(max(0.0, min(score, 1.0)), 4)
        if best is None or score > best[1]:
            best = (candidate, score)
    return best if best and best[1] >= AUTO_IMPORT_CONFIDENCE else (None, best[1] if best else 0.0)


def _tmdb_movie(conn, title: str, year: int | None, cache: dict) -> tuple[dict | None, float]:
    key = ("movie", title.casefold(), year)
    if key in cache:
        return cache[key]
    result = (None, 0.0)
    try:
        matches = metadata.search_movies(conn, title, year)
    except Exception as exc:
        logger.warning("scan: TMDB movie search failed for %s: %s", title, exc)
        matches = []
    titles = matching.title_variants(title)
    best = None
    for match in matches[:10]:
        aliases = matching.title_variants(match["title"], match.get("original_title"))
        score = max(
            (matching.similarity(source, alias)
             for source in titles for alias in aliases),
            default=0.0,
        )
        difference = abs(int(year) - int(match["year"])) if year and match.get("year") else 0
        if year and match.get("year"):
            score += 0.10 if difference == 0 else (0.03 if difference == 1 else -0.25)
        if best is None or score > best[1]:
            best = (match, score)
    if best and best[1] >= 0.85:
        result = best
    cache[key] = result
    return result


def _tmdb_series(conn, title: str, year: int | None, cache: dict) -> tuple[dict | None, float]:
    key = ("tv", title.casefold(), year)
    if key in cache:
        return cache[key]
    result = (None, 0.0)
    try:
        matches = metadata.search_series(conn, title, year)
    except Exception as exc:
        logger.warning("scan: TMDB series search failed for %s: %s", title, exc)
        matches = []
    titles = matching.title_variants(title)
    best = None
    for match in matches[:10]:
        aliases = matching.title_variants(match["title"], match.get("original_title"))
        score = max(
            (matching.similarity(source, alias)
             for source in titles for alias in aliases),
            default=0.0,
        )
        if year and match.get("year"):
            difference = abs(int(year) - int(match["year"]))
            score += 0.10 if difference == 0 else (0.03 if difference == 1 else -0.25)
        if best is None or score > best[1]:
            best = (match, score)
    if best and best[1] >= 0.85:
        result = best
    cache[key] = result
    return result


def _folder_info(folder: Path) -> tuple[str, int | None, str]:
    parsed = matching.parse_name(folder.name)
    return folder.name, parsed.year, parsed.resolution


def _movie_candidate(conn, path: Path, root: Path, cache: dict) -> dict:
    relative = path.relative_to(root)
    if len(relative.parts) > 1:
        title, year, folder_res = _folder_info(root / relative.parts[0])
    else:
        title, year, folder_res = _folder_info(path.with_suffix(""))
    parsed = matching.parse_name(path.stem)
    if parsed.is_episode:
        return {"kind": "movie", "title": title, "year": year or parsed.year,
                "resolution": folder_res or parsed.resolution,
                "message": "Looks like an episode, not a movie.", "confidence": 0.0}
    year = year or parsed.year
    resolution = parsed.resolution if parsed.resolution != "unknown" else folder_res
    rows = library.list_movies(conn)
    matched, confidence = _match_library_rows(title, year, rows)
    if matched:
        return {"kind": "movie", "title": title, "year": year,
                "resolution": resolution, "matched_movie_id": matched["id"],
                "confidence": confidence, "message": f"Matches {matched['title']}."}
    proposed, confidence = _tmdb_movie(conn, title, year, cache)
    if proposed:
        return {"kind": "movie", "title": proposed["title"], "year": year or proposed.get("year"),
                "resolution": resolution, "tmdb_id": proposed["tmdb_id"],
                "confidence": confidence, "message": "Will be added from TMDB."}
    return {"kind": "movie", "title": title, "year": year, "resolution": resolution,
            "confidence": confidence, "message": "No confident match."}


def _series_candidate(conn, path: Path, root: Path, cache: dict) -> dict | None:
    relative = path.relative_to(root)
    if len(relative.parts) > 1:
        series_title, series_year, folder_res = _folder_info(root / relative.parts[0])
    else:
        series_title, series_year, folder_res = _folder_info(path.with_suffix(""))
    marker = matching.episode_marker(path.stem)
    if marker is None:
        return None
    season, episode = marker
    parsed = matching.parse_name(path.stem)
    resolution = parsed.resolution if parsed.resolution != "unknown" else folder_res
    series_rows = library.list_series(conn)
    matched, confidence = _match_library_rows(series_title, series_year, series_rows)
    if matched:
        row = db.get_one(
            conn,
            "SELECT * FROM series_episodes WHERE series_id = ? AND season = ? AND episode = ?",
            (matched["id"], season, episode),
        )
        return {
            "kind": "episode", "title": matched["title"], "year": matched.get("year"),
            "season": season, "episode": episode, "resolution": resolution,
            "matched_series_id": matched["id"],
            "matched_episode_id": row["id"] if row else None,
            "confidence": confidence,
            "message": (f"Matches {matched['title']} S{season:02d}E{episode:02d}."
                        if row else "Series matched, but the episode is not in the metadata yet."),
        }
    proposed, confidence = _tmdb_series(conn, series_title, series_year, cache)
    if proposed:
        return {
            "kind": "episode", "title": proposed["title"],
            "year": series_year or proposed.get("year"),
            "season": season, "episode": episode, "resolution": resolution,
            "tmdb_id": proposed["tmdb_id"], "confidence": confidence,
            "message": "Will be added from TMDB, then matched to the episode.",
        }
    return {
        "kind": "episode", "title": series_title, "year": series_year,
        "season": season, "episode": episode, "resolution": resolution,
        "confidence": confidence, "message": "No confident match.",
    }


def _reconcile_missing(conn) -> int:
    missing = 0
    for row, kind, root in _tracked_files(conn):
        if not Path(row["file_path"]).exists():
            if kind == "movie":
                conn.execute(
                    "UPDATE movies SET file_path = NULL, file_size = NULL, "
                    "resolution = NULL, imported_at = NULL WHERE id = ?", (row["id"],)
                )
                library.add_history(conn, "missing", row["title"], kind="movie",
                                    movie_id=row["id"], message=row["file_path"])
            else:
                conn.execute(
                    "UPDATE series_episodes SET file_path = NULL, file_size = NULL, "
                    "resolution = NULL, imported_at = NULL WHERE id = ?", (row["id"],)
                )
                library.add_history(
                    conn, "missing", f"{row['series_title']} S{row['season']:02d}E{row['episode']:02d}",
                    kind="episode", series_id=row["series_id"], episode_id=row["id"],
                    message=row["file_path"],
                )
            missing += 1
    conn.commit()
    return missing


def _tracked_files(conn):
    movies = conn.execute(
        "SELECT id, title, file_path FROM movies WHERE file_path IS NOT NULL"
    ).fetchall()
    for row in movies:
        yield row, "movie", config.movie_library_dir()
    episodes = conn.execute(
        "SELECT e.id, e.series_id, e.season, e.episode, e.file_path, "
        "s.title AS series_title FROM series_episodes e "
        "JOIN series s ON s.id = e.series_id WHERE e.file_path IS NOT NULL"
    ).fetchall()
    for row in episodes:
        yield row, "episode", config.series_library_dir()


def run(conn) -> str:
    """Scan both library roots, propose matches, and reconcile missing files."""
    conn.execute("DELETE FROM scan_results WHERE status = 'pending'")
    conn.commit()
    missing = _reconcile_missing(conn)

    cache: dict = {}
    proposed = inserted = 0
    existing = {
        row["path"]: row["status"]
        for row in conn.execute("SELECT path, status FROM scan_results")
    }
    for root, kind in (
        (config.movie_library_dir().resolve(), "movie"),
        (config.series_library_dir().resolve(), "episode"),
    ):
        if not root.exists():
            continue
        for path in _walk(root):
            status = existing.get(str(path))
            if status in ("imported", "ignored"):
                continue
            parsed = _movie_candidate(conn, path, root, cache) if kind == "movie" else _series_candidate(conn, path, root, cache)
            if parsed is None:
                continue
            probed, probe_error = _probe(path)
            resolution = probed or parsed.get("resolution") or "unknown"
            message = parsed.get("message") or ""
            if probe_error:
                message = f"{message} {probe_error}".strip()
            conn.execute(
                """
                INSERT INTO scan_results (
                    path, kind, title, year, season, episode, resolution, tmdb_id,
                    matched_movie_id, matched_series_id, matched_episode_id,
                    confidence, status, message, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    kind = excluded.kind, title = excluded.title, year = excluded.year,
                    season = excluded.season, episode = excluded.episode,
                    resolution = excluded.resolution, tmdb_id = excluded.tmdb_id,
                    matched_movie_id = excluded.matched_movie_id,
                    matched_series_id = excluded.matched_series_id,
                    matched_episode_id = excluded.matched_episode_id,
                    confidence = excluded.confidence, status = 'pending',
                    message = excluded.message, scanned_at = excluded.scanned_at
                """,
                (
                    str(path), parsed["kind"], parsed.get("title"), parsed.get("year"),
                    parsed.get("season"), parsed.get("episode"), resolution,
                    parsed.get("tmdb_id"), parsed.get("matched_movie_id"),
                    parsed.get("matched_series_id"), parsed.get("matched_episode_id"),
                    parsed.get("confidence") or 0.0, message, _now(),
                ),
            )
            proposed += 1
            if (parsed.get("matched_movie_id") or parsed.get("matched_series_id")
                    or parsed.get("tmdb_id")):
                inserted += 1
    conn.commit()
    logger.info("scan: %d file(s), %d with a proposed match, %d missing", proposed,
                inserted, missing)
    return (f"Scanned {proposed} file(s); {inserted} have a proposed match"
            + (f"; {missing} tracked file(s) are missing." if missing else "."))


def results(conn) -> list[dict]:
    return [
        dict(row) for row in conn.execute(
            "SELECT * FROM scan_results ORDER BY "
            "CASE status WHEN 'pending' THEN 0 WHEN 'imported' THEN 1 ELSE 2 END, "
            "confidence DESC, path"
        )
    ]


def _find_episode(conn, series_id: int, season: int, episode: int) -> dict | None:
    row = db.get_one(
        conn,
        "SELECT * FROM series_episodes WHERE series_id = ? AND season = ? AND episode = ?",
        (series_id, season, episode),
    )
    return dict(row) if row else None


def import_result(conn, result_id: int) -> dict:
    result = db.get_one(conn, "SELECT * FROM scan_results WHERE id = ?", (int(result_id),))
    if result is None:
        raise LookupError("That scan result no longer exists.")
    if result["status"] != "pending":
        raise ValueError("That scan result has already been handled.")
    path = Path(result["path"])
    try:
        path = naming.confine(path, config.media_dir())
    except naming.TemplateError as exc:
        raise ValueError(str(exc)) from exc
    if not path.exists():
        raise ValueError("The file no longer exists on disk.")

    if result["kind"] == "movie":
        movie = None
        if result["matched_movie_id"]:
            movie = library.get_movie(conn, result["matched_movie_id"])
        elif result["tmdb_id"]:
            # A previous import in the same batch may already have added it.
            existing = db.get_one(conn, "SELECT * FROM movies WHERE tmdb_id = ?",
                                  (int(result["tmdb_id"]),))
            movie = dict(existing) if existing else library.add_movie(
                conn, result["tmdb_id"],
            )
        if movie is None:
            raise ValueError("There is no confirmed movie for this file.")
        if movie.get("file_path") and Path(movie["file_path"]).resolve() != path.resolve():
            conn.execute(
                "UPDATE scan_results SET status = 'ignored', message = ? WHERE id = ?",
                ("Movie already has a different file.", result["id"]),
            )
            conn.commit()
            raise ValueError("That movie already has a different file.")
        conn.execute(
            "UPDATE movies SET file_path = ?, file_size = ?, resolution = ?, "
            "imported_at = ?, updated_at = ? WHERE id = ?",
            (str(path), path.stat().st_size, result["resolution"], _now(), _now(), movie["id"]),
        )
        library.add_history(conn, "imported", movie["title"], kind="movie",
                            movie_id=movie["id"], message="library scan")
        title = movie["title"]
    else:
        series = None
        if result["matched_series_id"]:
            series = library.get_series(conn, result["matched_series_id"])
        elif result["tmdb_id"]:
            existing = db.get_one(conn, "SELECT * FROM series WHERE tmdb_id = ?",
                                  (int(result["tmdb_id"]),))
            series = dict(existing) if existing else library.add_series(
                conn, result["tmdb_id"],
            )
        if series is None:
            raise ValueError("There is no confirmed series for this file.")
        episode = _find_episode(conn, series["id"], result["season"], result["episode"])
        if episode is None:
            # A newly added or stale series may not have episodes yet.
            library.refresh_series_episodes(conn, series)
            episode = _find_episode(conn, series["id"], result["season"], result["episode"])
        if episode is None:
            raise ValueError("The series has no matching episode in its metadata.")
        if episode.get("file_path") and Path(episode["file_path"]).resolve() != path.resolve():
            conn.execute(
                "UPDATE scan_results SET status = 'ignored', message = ? WHERE id = ?",
                ("Episode already has a different file.", result["id"]),
            )
            conn.commit()
            raise ValueError("That episode already has a different file.")
        conn.execute(
            "UPDATE series_episodes SET file_path = ?, file_size = ?, resolution = ?, "
            "imported_at = ? WHERE id = ?",
            (str(path), path.stat().st_size, result["resolution"], _now(), episode["id"]),
        )
        title = f"{series['title']} S{int(result['season']):02d}E{int(result['episode']):02d}"
        library.add_history(conn, "imported", title, kind="episode",
                            series_id=series["id"], episode_id=episode["id"],
                            message="library scan")
    conn.execute(
        "UPDATE scan_results SET status = 'imported', message = 'Imported into the library.', "
        "scanned_at = ? WHERE id = ?",
        (_now(), result["id"]),
    )
    conn.commit()
    logger.info("scan: imported %s", title)
    return {"title": title, "path": str(path)}


def ignore_result(conn, result_id: int) -> None:
    row = db.get_one(conn, "SELECT * FROM scan_results WHERE id = ?", (int(result_id),))
    if row is None:
        raise LookupError("That scan result no longer exists.")
    conn.execute("UPDATE scan_results SET status = 'ignored' WHERE id = ?", (int(result_id),))
    conn.commit()


def import_all(conn) -> dict:
    rows = conn.execute(
        "SELECT id FROM scan_results WHERE status = 'pending' AND confidence >= ? "
        "AND (matched_movie_id IS NOT NULL OR matched_series_id IS NOT NULL "
        "OR tmdb_id IS NOT NULL) ORDER BY confidence DESC",
        (AUTO_IMPORT_CONFIDENCE,),
    ).fetchall()
    imported = 0
    failed = 0
    for row in rows:
        try:
            import_result(conn, row["id"])
            imported += 1
        except Exception as exc:
            failed += 1
            logger.warning("scan: import of result %s failed: %s", row["id"], exc)
    return {"imported": imported, "failed": failed}


__all__ = [
    "AUTO_IMPORT_CONFIDENCE",
    "ignore_result",
    "import_all",
    "import_result",
    "results",
    "run",
]
