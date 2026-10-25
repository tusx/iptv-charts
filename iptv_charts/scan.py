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
import sqlite3
import subprocess
import shutil
import time
from datetime import date
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
            if kind == "alternate":
                conn.execute("DELETE FROM movie_alternates WHERE id = ?", (row["id"],))
                library.add_history(
                    conn, "missing", str(row["name"] or "Alternate version"),
                    kind="movie", movie_id=row["movie_id"], message=row["file_path"],
                )
            elif kind == "movie":
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
    alternates = conn.execute(
        "SELECT id, movie_id, name, file_path FROM movie_alternates"
    ).fetchall()
    for row in alternates:
        yield row, "alternate", config.movie_library_dir()
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
    alternate_paths = set()
    for row in conn.execute("SELECT file_path FROM movie_alternates"):
        try:
            alternate_paths.add(str(Path(row["file_path"]).resolve()))
        except (OSError, TypeError):
            alternate_paths.add(str(row["file_path"]))
    for root, kind in (
        (config.movie_library_dir().resolve(), "movie"),
        (config.series_library_dir().resolve(), "episode"),
    ):
        if not root.exists():
            continue
        for path in _walk(root):
            if str(path) in alternate_paths:
                continue
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
            "main_source_id = NULL, main_remote_id = NULL, "
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


# ------------------------------------------------------- import existing page
# The /import page is a review-only view over the same discovery rules as
# ``run``. It never proposes by itself and never touches files: the page lists
# untracked files with the title/year/episode parsed from their names, the user
# fetches metadata on demand, and only an explicit confirmation attaches a
# file. ``run`` keeps its own behaviour (pending rows + automatic TMDB hints).
class ImportConflict(ValueError):
    """A file or TMDB identity is already attached to another library item."""


def _resolved(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except (OSError, TypeError):
        return str(path)


def _owned_files(conn) -> dict[str, dict]:
    """Every tracked file path, so an import can never attach a file twice."""
    owned: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT id, title, file_path FROM movies "
        "WHERE file_path IS NOT NULL AND file_path <> ''"
    ):
        owned[_resolved(row["file_path"])] = {
            "kind": "movie", "id": int(row["id"]), "title": row["title"],
        }
    for row in conn.execute(
        "SELECT a.id, a.name, a.file_path, a.movie_id, m.title "
        "FROM movie_alternates a JOIN movies m ON m.id = a.movie_id"
    ):
        owned[_resolved(row["file_path"])] = {
            "kind": "alternate", "id": int(row["id"]),
            "title": f"{row['title']} ({row['name'] or 'alternate version'})",
        }
    for row in conn.execute(
        "SELECT e.id, e.series_id, e.season, e.episode, e.file_path, "
        "s.title AS series_title FROM series_episodes e "
        "JOIN series s ON s.id = e.series_id "
        "WHERE e.file_path IS NOT NULL AND e.file_path <> ''"
    ):
        season, episode = int(row["season"]), int(row["episode"])
        owned[_resolved(row["file_path"])] = {
            "kind": "episode", "id": int(row["id"]),
            "series_id": int(row["series_id"]),
            "season": season, "episode": episode,
            "title": f"{row['series_title']} S{season:02d}E{episode:02d}",
        }
    return owned


def _ignored_paths(conn) -> set[str]:
    return {
        _resolved(row["path"])
        for row in conn.execute("SELECT path FROM scan_results WHERE status = 'ignored'")
    }


def _parsed_title(parsed: matching.ParsedName, fallback: str) -> str:
    return parsed.title_variants[0] if parsed.title_variants else fallback


def _review_movie(conn, path: Path, root: Path, rows: list[dict]) -> dict:
    relative = path.relative_to(root)
    parsed = matching.parse_name(path.stem)
    if len(relative.parts) > 1:
        folder = root / relative.parts[0]
        folder_parsed = matching.parse_name(folder.name)
        title = _parsed_title(folder_parsed, folder.name)
        year = folder_parsed.year or parsed.year
        folder_resolution = folder_parsed.resolution
    else:
        title = _parsed_title(parsed, path.stem)
        year = parsed.year
        folder_resolution = "unknown"
    resolution = parsed.resolution if parsed.resolution != "unknown" else folder_resolution
    matched, confidence = _match_library_rows(title, year, rows)
    item = {
        "kind": "movie", "title": title, "year": year,
        "season": parsed.season, "episode": parsed.episode,
        "resolution": resolution,
        "matched_movie_id": int(matched["id"]) if matched else None,
        "matched_title": matched["title"] if matched else None,
        "confidence": confidence,
        "importable": not parsed.is_episode,
        "is_episode": parsed.is_episode,
    }
    if parsed.is_episode:
        item["message"] = "Looks like a TV episode; import it from the Series tab."
    elif matched:
        item["message"] = f"Matches the existing library movie “{matched['title']}”."
    else:
        item["message"] = "Fetch metadata to choose the movie."
    return item


def _review_episode(conn, path: Path, root: Path, rows: list[dict]) -> dict:
    relative = path.relative_to(root)
    parsed = matching.parse_name(path.stem)
    if len(relative.parts) > 1:
        folder = root / relative.parts[0]
        folder_parsed = matching.parse_name(folder.name)
        title = _parsed_title(folder_parsed, folder.name)
        year = folder_parsed.year or parsed.year
        folder_resolution = folder_parsed.resolution
    else:
        title = _parsed_title(parsed, path.stem)
        year = parsed.year
        folder_resolution = "unknown"
    resolution = parsed.resolution if parsed.resolution != "unknown" else folder_resolution
    marker = matching.episode_marker(path.stem)
    season, episode = marker if marker else (None, None)
    matched, confidence = _match_library_rows(title, year, rows)
    matched_series_id = int(matched["id"]) if matched else None
    matched_episode_id = None
    if matched and marker is not None:
        row = _find_episode(conn, matched_series_id, season, episode)
        matched_episode_id = int(row["id"]) if row else None
    item = {
        "kind": "episode", "title": title, "year": year,
        "season": season, "episode": episode,
        "resolution": resolution,
        "matched_series_id": matched_series_id,
        "matched_title": matched["title"] if matched else None,
        "matched_episode_id": matched_episode_id,
        "confidence": confidence,
        "importable": marker is not None,
        "is_episode": True,
    }
    if marker is None:
        item["message"] = "The season and episode could not be read from the filename."
    elif matched and matched_episode_id:
        item["message"] = (
            f"Matches {matched['title']} S{season:02d}E{episode:02d}; "
            "the file will be attached to that episode."
        )
    elif matched:
        item["message"] = (
            f"Matches the series “{matched['title']}”, but S{season:02d}E{episode:02d} "
            "is not in its metadata yet."
        )
    else:
        item["message"] = "Fetch metadata to choose the series."
    return item


def discover(conn, kind: str) -> list[dict]:
    """List untracked video files under the movie or series root.

    This is read-only: it does not insert ``scan_results`` rows, probe files or
    call a metadata provider. ``kind`` is ``movie`` or ``series``. Files that
    are already tracked, or were previously ignored in the scan review, are
    omitted. Paths are absolute and resolved inside the configured root.
    """
    kind = str(kind or "").strip().lower()
    if kind not in {"movie", "series"}:
        raise ValueError("Choose the Movies or Series tab.")
    root = (config.movie_library_dir() if kind == "movie"
            else config.series_library_dir()).resolve()
    if not root.exists():
        return []
    rows = library.list_movies(conn) if kind == "movie" else library.list_series(conn)
    owned = _owned_files(conn)
    ignored = _ignored_paths(conn)
    found: list[dict] = []
    for path in _walk(root):
        key = _resolved(path)
        if key in owned or key in ignored:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(root)
        item = (_review_movie(conn, path, root, rows) if kind == "movie"
                else _review_episode(conn, path, root, rows))
        item.update({
            "path": str(path),
            "relative": str(relative),
            "name": path.name,
            "size": int(stat.st_size),
            "modified_at": int(stat.st_mtime),
            "tracked": False,
        })
        found.append(item)
    found.sort(key=lambda row: (
        (row.get("title") or "").casefold(),
        row.get("year") or 0,
        row.get("season") or 0,
        row.get("episode") or 0,
        row["path"],
    ))
    return found


def _confined_media_path(raw_path, root: Path) -> Path:
    """Resolve a review path and require it to stay inside the given root."""
    if not raw_path:
        raise ValueError("Choose a file to import.")
    try:
        path = naming.confine(Path(str(raw_path)), root)
    except naming.TemplateError as exc:
        raise ValueError(str(exc)) from exc
    if not path.exists() or not path.is_file():
        raise ValueError("The file no longer exists on disk.")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("That file is not a supported video.")
    return path


def _profile_id(conn, profile_id) -> int:
    if profile_id in (None, ""):
        resolved = library.default_profile_id(conn)
        if resolved is None:
            raise ValueError("Create a quality profile in Settings first.")
        return int(resolved)
    try:
        resolved = int(profile_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Choose a valid quality profile.") from exc
    if db.get_one(conn, "SELECT id FROM quality_profiles WHERE id = ?", (resolved,)) is None:
        raise ValueError("That quality profile does not exist.")
    return resolved


def _media_resolution(path: Path, fallback: str | None) -> str:
    probed, _error = _probe(path)
    value = probed or fallback or "unknown"
    return value if value != "unknown" else "unknown"


def _seed_bworker(conn) -> int:
    """Create bworker work for every monitored item.

    The foundation owns this helper; calling it here keeps a freshly imported
    item immediately pickable by bworker. A database that predates the bworker
    tables (or a worktree without the foundation) still imports successfully.
    """
    try:
        return int(db.seed_bworker_work(conn) or 0)
    except AttributeError:
        logger.info("scan: bworker work seeding is unavailable in this build")
        return 0
    except sqlite3.Error as exc:
        logger.warning("scan: bworker work seeding skipped: %s", exc)
        return 0


def _mark_imported(conn, *, path: Path, kind: str, title: str | None,
                   year: int | None, season: int | None, episode: int | None,
                   resolution: str | None, tmdb_id: int | None,
                   matched_movie_id: int | None, matched_series_id: int | None,
                   matched_episode_id: int | None) -> None:
    conn.execute(
        """
        INSERT INTO scan_results (
            path, kind, title, year, season, episode, resolution, tmdb_id,
            matched_movie_id, matched_series_id, matched_episode_id,
            confidence, status, message, scanned_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'imported', ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            kind = excluded.kind, title = excluded.title, year = excluded.year,
            season = excluded.season, episode = excluded.episode,
            resolution = excluded.resolution, tmdb_id = excluded.tmdb_id,
            matched_movie_id = excluded.matched_movie_id,
            matched_series_id = excluded.matched_series_id,
            matched_episode_id = excluded.matched_episode_id,
            confidence = excluded.confidence, status = 'imported',
            message = excluded.message, scanned_at = excluded.scanned_at
        """,
        (
            str(path), kind, title, year, season, episode, resolution, tmdb_id,
            matched_movie_id, matched_series_id, matched_episode_id,
            "Imported from the Import existing page.", _now(),
        ),
    )


def _finished(conn, *, path: Path, kind: str, title: str, year: int | None,
              season: int | None, episode: int | None, resolution: str | None,
              tmdb_id: int | None, matched_movie_id: int | None,
              matched_series_id: int | None, matched_episode_id: int | None,
              item_id: int, extra: dict | None = None) -> dict:
    _mark_imported(
        conn, path=path, kind=kind, title=title, year=year, season=season,
        episode=episode, resolution=resolution, tmdb_id=tmdb_id,
        matched_movie_id=matched_movie_id, matched_series_id=matched_series_id,
        matched_episode_id=matched_episode_id,
    )
    seeded = _seed_bworker(conn)
    conn.commit()
    result = {
        "path": str(path), "kind": kind, "title": title,
        "resolution": resolution, "tmdb_id": tmdb_id,
        "work_seeded": seeded,
    }
    result.update(extra or {})
    return result


def _import_movie(conn, path: Path, *, tmdb_id: int | None,
                  matched_movie_id: int | None, profile_id: int | None,
                  resolution: str | None) -> dict:
    key = _resolved(path)
    owner = _owned_files(conn).get(key)
    target = None
    if tmdb_id:
        # An explicitly selected identity must never fall back to a stale
        # local match; otherwise a new TMDB choice could attach the file to
        # an unrelated existing row and leave the identity unchanged.
        target = db.get_one(conn, "SELECT * FROM movies WHERE tmdb_id = ?",
                            (int(tmdb_id),))
    elif matched_movie_id:
        target = db.get_one(conn, "SELECT * FROM movies WHERE id = ?",
                            (int(matched_movie_id),))
    if owner is not None:
        same_movie = False
        if tmdb_id:
            # An explicit TMDB choice wins over a stale local match.
            same_movie = (target is not None and owner["kind"] == "movie"
                          and owner["id"] == int(target["id"]))
        elif matched_movie_id:
            same_movie = owner["kind"] == "movie" and owner["id"] == int(matched_movie_id)
        if same_movie:
            movie = library.get_movie(conn, owner["id"])
            conn.execute("UPDATE movies SET monitored = 1 WHERE id = ?", (movie["id"],))
            if profile_id:
                conn.execute("UPDATE movies SET quality_profile_id = ? WHERE id = ?",
                             (int(profile_id), movie["id"]))
            return _finished(
                conn, path=path, kind="movie", title=movie["title"],
                year=movie.get("year"), season=None, episode=None,
                resolution=movie.get("resolution") or resolution, tmdb_id=movie["tmdb_id"],
                matched_movie_id=movie["id"], matched_series_id=None,
                matched_episode_id=None, item_id=movie["id"],
                extra={"movie_id": movie["id"], "already_imported": True,
                       "monitored": True},
            )
        raise ImportConflict(f"That file is already attached to “{owner['title']}”.")

    if target is None:
        if not tmdb_id:
            raise ValueError("Choose the movie metadata for this file.")
        target = library.add_movie(conn, int(tmdb_id), profile_id=profile_id,
                                   monitored=True)
    movie = library.get_movie(conn, int(target["id"]))
    if movie.get("file_path") and _resolved(movie["file_path"]) != key:
        raise ImportConflict(
            f"“{movie['title']}” already has a different file attached."
        )
    if profile_id:
        conn.execute("UPDATE movies SET quality_profile_id = ? WHERE id = ?",
                     (int(profile_id), movie["id"]))
    final_resolution = _media_resolution(path, resolution)
    now = _now()
    conn.execute(
        "UPDATE movies SET file_path = ?, file_size = ?, resolution = ?, "
        "main_source_id = NULL, main_remote_id = NULL, monitored = 1, "
        "imported_at = ?, updated_at = ? WHERE id = ?",
        (str(path), path.stat().st_size, final_resolution, now, now, movie["id"]),
    )
    library.add_history(conn, "imported", movie["title"], kind="movie",
                        movie_id=movie["id"], message="import existing")
    return _finished(
        conn, path=path, kind="movie", title=movie["title"], year=movie.get("year"),
        season=None, episode=None, resolution=final_resolution,
        tmdb_id=movie["tmdb_id"], matched_movie_id=movie["id"],
        matched_series_id=None, matched_episode_id=None, item_id=movie["id"],
        extra={"movie_id": movie["id"], "already_imported": False,
               "monitored": True},
    )


def _episode_monitor_value(episode: dict, mode: str,
                           seasons: set[int] | None) -> bool:
    season = int(episode["season"])
    if season == 0:
        return False
    if mode == "all":
        return True
    if mode == "none":
        return False
    if mode == "future":
        air_date = str(episode.get("air_date") or "")[:10]
        return not air_date or air_date >= date.today().isoformat()
    if mode == "manual":
        return season in (seasons or set())
    raise ValueError("Unknown series monitoring mode.")


def _apply_series_mode(conn, series_id: int, mode: str,
                       seasons: set[int] | None) -> None:
    mode = str(mode or "all").strip().lower()
    if mode not in {"all", "none", "manual", "future"}:
        raise ValueError("Choose all, none, manual seasons or future episodes.")
    if mode == "manual" and not seasons:
        raise ValueError("Choose at least one season to monitor.")
    episodes = library.series_episodes(conn, series_id)
    for episode in episodes:
        monitored = _episode_monitor_value(episode, mode, seasons)
        conn.execute("UPDATE series_episodes SET monitored = ? WHERE id = ?",
                     (1 if monitored else 0, episode["id"]))


def _import_episode(conn, path: Path, *, tmdb_id: int | None,
                    matched_series_id: int | None, profile_id: int | None,
                    resolution: str | None, season: int | None,
                    episode: int | None, series_mode: str,
                    seasons: set[int] | None) -> dict:
    marker = matching.episode_marker(path.stem)
    if marker is None:
        raise ValueError(
            "The season and episode could not be read from the filename."
        )
    parsed_season, parsed_episode = marker
    season = int(season) if season is not None else parsed_season
    episode = int(episode) if episode is not None else parsed_episode
    key = _resolved(path)
    owner = _owned_files(conn).get(key)
    target = None
    if tmdb_id:
        # As for movies: the explicit TMDB choice is authoritative and must
        # not silently use a stale local series match instead.
        target = db.get_one(conn, "SELECT * FROM series WHERE tmdb_id = ?",
                            (int(tmdb_id),))
    elif matched_series_id:
        target = db.get_one(conn, "SELECT * FROM series WHERE id = ?",
                            (int(matched_series_id),))
    if owner is not None:
        same_series = False
        if target is not None and owner["kind"] == "episode":
            same_series = int(owner["series_id"]) == int(target["id"])
        elif matched_series_id and owner["kind"] == "episode":
            same_series = int(owner["series_id"]) == int(matched_series_id)
        if (same_series and owner["kind"] == "episode"
                and owner["season"] == season and owner["episode"] == episode):
            series = library.get_series(conn, owner["series_id"])
            conn.execute("UPDATE series SET monitored = 1 WHERE id = ?", (series["id"],))
            if profile_id:
                conn.execute("UPDATE series SET quality_profile_id = ? WHERE id = ?",
                             (int(profile_id), series["id"]))
            _apply_series_mode(conn, series["id"], series_mode, seasons)
            return _finished(
                conn, path=path, kind="episode", title=series["title"],
                year=series.get("year"), season=season, episode=episode,
                resolution=series_episodes_resolution(conn, owner["id"]) or resolution,
                tmdb_id=series["tmdb_id"], matched_movie_id=None,
                matched_series_id=series["id"], matched_episode_id=owner["id"],
                item_id=owner["id"],
                extra={"series_id": series["id"], "episode_id": owner["id"],
                       "already_imported": True, "monitored": True},
            )
        raise ImportConflict(f"That file is already attached to “{owner['title']}”.")

    if target is None:
        if not tmdb_id:
            raise ValueError("Choose the series metadata for this file.")
        target = library.add_series(
            conn, int(tmdb_id), profile_id=profile_id, monitored=True,
            monitor_mode="none", monitor_new_seasons="all",
        )
    series = library.get_series(conn, int(target["id"]))
    if profile_id:
        conn.execute("UPDATE series SET quality_profile_id = ?, monitored = 1 "
                     "WHERE id = ?", (int(profile_id), series["id"]))
    row = _find_episode(conn, series["id"], season, episode)
    if row is None:
        # A newly added or stale series may not have this episode yet.
        library.refresh_series_episodes(conn, series)
        row = _find_episode(conn, series["id"], season, episode)
    if row is None:
        raise ValueError(
            f"The series has no S{season:02d}E{episode:02d} episode in its metadata."
        )
    if row.get("file_path") and _resolved(row["file_path"]) != key:
        raise ImportConflict(
            f"{series['title']} S{season:02d}E{episode:02d} already has a "
            "different file attached."
        )
    final_resolution = _media_resolution(path, resolution)
    conn.execute(
        "UPDATE series_episodes SET file_path = ?, file_size = ?, resolution = ?, "
        "imported_at = ? WHERE id = ?",
        (str(path), path.stat().st_size, final_resolution, _now(), row["id"]),
    )
    _apply_series_mode(conn, series["id"], series_mode, seasons)
    title = f"{series['title']} S{season:02d}E{episode:02d}"
    library.add_history(conn, "imported", title, kind="episode",
                        series_id=series["id"], episode_id=row["id"],
                        message="import existing")
    return _finished(
        conn, path=path, kind="episode", title=series["title"],
        year=series.get("year"), season=season, episode=episode,
        resolution=final_resolution, tmdb_id=series["tmdb_id"],
        matched_movie_id=None, matched_series_id=series["id"],
        matched_episode_id=row["id"], item_id=row["id"],
        extra={"series_id": series["id"], "episode_id": row["id"],
               "already_imported": False, "monitored": True},
    )


def series_episodes_resolution(conn, episode_id: int) -> str | None:
    row = db.get_one(conn, "SELECT resolution FROM series_episodes WHERE id = ?",
                     (int(episode_id),))
    return row["resolution"] if row else None


def import_untracked(conn, *, path, kind: str, tmdb_id: int | None = None,
                     matched_movie_id: int | None = None,
                     matched_series_id: int | None = None,
                     title: str | None = None, year: int | None = None,
                     season: int | None = None, episode: int | None = None,
                     resolution: str | None = None,
                     profile_id: int | None = None,
                     series_mode: str = "all",
                     seasons: list[int] | tuple[int, ...] | set[int] | None = None
                     ) -> dict:
    """Attach one review file to the library without moving it.

    ``kind`` is the discovered file kind: ``movie`` or ``episode``. A movie is
    confined to the movie root and an episode to the series root. The selected
    quality profile defaults to the library default (the 1080p builtin).

    Duplicates are refused: a path that is already tracked, or a TMDB identity
    already attached to a different file, raises :class:`ImportConflict`.
    Importing the same file for the same identity again is idempotent and is
    reported with ``already_imported=True``.
    """
    kind = str(kind or "").strip().lower()
    if kind == "series":
        kind = "episode"
    if kind not in {"movie", "episode"}:
        raise ValueError("Choose a movie or episode to import.")
    tmdb_id = _optional_int(tmdb_id, "TMDB id")
    matched_movie_id = _optional_int(matched_movie_id, "movie id")
    matched_series_id = _optional_int(matched_series_id, "series id")
    season = _optional_int(season, "season")
    episode = _optional_int(episode, "episode")
    year = _optional_int(year, "year")
    normalized_seasons = {
        int(value) for value in (seasons or [])
        if str(value).strip() not in {"", "None"}
    }
    series_mode = str(series_mode or "all").strip().lower()
    if series_mode not in {"all", "none", "manual", "future"}:
        raise ValueError("Choose all, none, manual seasons or future episodes.")
    if (kind == "episode" and series_mode == "manual"
            and not normalized_seasons):
        # Validate before library rows are created: a rejected form must not
        # leave a half-imported series behind.
        raise ValueError("Choose at least one season to monitor.")
    resolved_profile = _profile_id(conn, profile_id)
    if kind == "movie":
        if season is not None or episode is not None:
            raise ValueError("A movie file cannot have a season or episode.")
        root = config.movie_library_dir()
        target = _confined_media_path(path, root)
        parsed = matching.parse_name(target.stem)
        if parsed.is_episode:
            raise ValueError(
                "That file looks like a TV episode; import it from the Series tab."
            )
        return _import_movie(
            conn, target, tmdb_id=tmdb_id, matched_movie_id=matched_movie_id,
            profile_id=resolved_profile, resolution=resolution,
        )
    root = config.series_library_dir()
    target = _confined_media_path(path, root)
    return _import_episode(
        conn, target, tmdb_id=tmdb_id, matched_series_id=matched_series_id,
        profile_id=resolved_profile, resolution=resolution, season=season,
        episode=episode, series_mode=series_mode, seasons=normalized_seasons,
    )


def _optional_int(value, label: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {label}.") from exc


__all__ = [
    "AUTO_IMPORT_CONFIDENCE",
    "ImportConflict",
    "discover",
    "ignore_result",
    "import_all",
    "import_result",
    "import_untracked",
    "results",
    "run",
]
