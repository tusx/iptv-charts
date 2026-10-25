"""Read-only media tree and confirmed destructive storage operations.

The storage view is intentionally narrow: it can see the configured media
root's three known folders (the movie library, the series library and
``.incomplete``) and nothing else. Every path that arrives from the browser is
treated as untrusted: it must be a relative key built from ordinary path
components, every component is checked with ``lstat`` so a symlink is never
followed, and the final resolved path must still live under the media root.

Deleting a library file clears the matching database row in the same
transaction that records the library-side change, while keeping the movie,
series or episode entry itself. Deleting an active staging file is a separate,
explicit confirmation because it also cancels the download that owns it. An
import in progress cannot be cancelled and is always refused.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat as stat_module
import time
from pathlib import Path
from typing import Any, Mapping

from . import config

logger = logging.getLogger("iptv_charts.storage")

#: Folder keys are the only roots the destructive endpoints accept. The value
#: is resolved through config, so an environment override still works.
FOLDER_KEYS = ("movies", "series", "incomplete")
FOLDER_LABELS = {"movies": "Movies", "series": "Series", "incomplete": ".incomplete"}

#: Statuses that still own a file on disk.
ACTIVE_STATUSES = ("queued", "downloading", "importing")
TERMINAL_STATUSES = ("imported", "failed", "cancelled")

#: How long to wait for a cancelled download to stop before deleting anyway.
#: On POSIX an unlink of an open file is safe; the wait is there to let an
#: import (which cannot be cancelled) be detected first.
CANCEL_WAIT_SECONDS = 2.0


class StorageError(ValueError):
    """A storage request was refused for an expected reason."""

    status = 400


class StorageNotFound(StorageError):
    status = 404


class StorageForbidden(StorageError):
    status = 403


class StorageConflict(StorageError):
    status = 409


# --------------------------------------------------------------------- paths
def media_root() -> Path:
    return Path(config.media_dir())


def _protected_roots() -> list[Path]:
    """Directories the storage view must never touch or overlap."""
    roots: list[Path] = []
    for resolver in (config.data_dir, config.config_dir, config.cache_dir):
        try:
            roots.append(resolver().resolve())
        except OSError:
            continue
    return roots


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _assert_not_protected(target: Path) -> None:
    for protected in _protected_roots():
        if _overlaps(target, protected):
            raise StorageForbidden(
                "Refusing a path that overlaps the app's data, configuration "
                "or cache directory."
            )


def folder_path(folder_key: str) -> Path:
    """Resolve one known library folder and prove it stays under the root."""
    if folder_key not in FOLDER_KEYS:
        raise StorageError("Unknown storage folder.")
    try:
        if folder_key == "movies":
            path = config.movie_library_dir()
        elif folder_key == "series":
            path = config.series_library_dir()
        else:
            path = config.incomplete_dir()
    except OSError as exc:
        raise StorageError(
            f"The {FOLDER_LABELS[folder_key]} folder could not be created: "
            f"{exc.strerror or exc}"
        ) from exc

    root = media_root()
    try:
        root_resolved = root.resolve()
        resolved = path.resolve()
    except OSError as exc:
        raise StorageError("The media folder could not be resolved.") from exc

    if resolved == root_resolved:
        raise StorageForbidden("Refusing to operate on the media root itself.")
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise StorageForbidden(
            f"The configured {FOLDER_LABELS[folder_key]} folder is outside the media root."
        ) from exc
    _assert_not_protected(resolved)
    if not resolved.exists():
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"The {FOLDER_LABELS[folder_key]} folder could not be created: "
                f"{exc.strerror or exc}"
            ) from exc
    if not resolved.is_dir():
        raise StorageError(
            f"The configured {FOLDER_LABELS[folder_key]} path is not a folder."
        )
    return resolved


def folder_options() -> list[dict[str, Any]]:
    """UI metadata for the root selector; never raises for a bad override."""
    options: list[dict[str, Any]] = []
    for key in FOLDER_KEYS:
        try:
            path = folder_path(key)
        except StorageError as exc:
            options.append({
                "key": key,
                "label": FOLDER_LABELS[key],
                "path": None,
                "exists": False,
                "error": str(exc),
            })
            continue
        options.append({
            "key": key,
            "label": path.name,
            "path": _relative_inside(path, media_root()),
            "exists": True,
            "error": None,
        })
    return options


def _norm_abs(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.normpath(os.path.abspath(str(value))))


def _real_key(value: str | os.PathLike[str]) -> Path:
    """A stable key for matching a database path to a directory entry."""
    return Path(os.path.normpath(os.path.realpath(str(value))))


def _relative_inside(value: str | os.PathLike[str], root: Path) -> str | None:
    try:
        return _real_key(value).relative_to(_real_key(root)).as_posix()
    except ValueError:
        return None


def _is_under(value: str | os.PathLike[str], base: Path) -> bool:
    """Lexical containment; deliberately does not follow symlinks."""
    child = _norm_abs(value)
    parent = _norm_abs(base)
    if child == parent:
        return False
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _relative_parts(rel_key: object) -> tuple[str, ...]:
    """Validate a browser-supplied path key before it touches the filesystem."""
    if not isinstance(rel_key, str) or not rel_key:
        raise StorageError("A file path is required.")
    if "\x00" in rel_key:
        raise StorageError("Invalid file path.")
    if rel_key.startswith("/") or "\\" in rel_key:
        raise StorageError("Refusing a path outside the media library.")
    parts = rel_key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise StorageError("Refusing a path outside the media library.")
    return tuple(parts)


def _confined_path(rel_key: object) -> Path:
    """Turn a relative key into a resolved path, rejecting every escape."""
    parts = _relative_parts(rel_key)
    root = media_root()
    try:
        root_resolved = root.resolve()
    except OSError as exc:
        raise StorageError("The media root could not be resolved.") from exc

    candidate = root
    for part in parts:
        candidate = candidate / part
        try:
            info = candidate.lstat()
        except FileNotFoundError as exc:
            raise StorageNotFound("That file or folder no longer exists.") from exc
        except OSError as exc:
            raise StorageError(
                f"Could not access that path: {exc.strerror or exc}"
            ) from exc
        if stat_module.S_ISLNK(info.st_mode):
            raise StorageForbidden("Refusing to follow a symbolic link.")

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise StorageError("That path could not be resolved.") from exc
    if resolved == root_resolved:
        raise StorageForbidden("Refusing to operate on the media root itself.")
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise StorageForbidden("Refusing a path outside the media library.") from exc
    return resolved


# ------------------------------------------------------------- active markers
def _active_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    rows = conn.execute(
        "SELECT id, kind, movie_id, episode_id, title, status, mode, "
        "temp_path, final_path, bytes_done, bytes_total, cancel_requested "
        f"FROM downloads WHERE status IN ({placeholders}) ORDER BY id",
        ACTIVE_STATUSES,
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        total = item.get("bytes_total")
        done = item.get("bytes_done") or 0
        item["fraction"] = (
            max(0.0, min(1.0, float(done) / float(total))) if total else None
        )
        out.append(item)
    return out


def _active_index(rows: list[dict[str, Any]]) -> dict[Path, dict[str, Any]]:
    index: dict[Path, dict[str, Any]] = {}
    for item in rows:
        marker = {
            "id": int(item["id"]),
            "status": item["status"],
            "title": item.get("title"),
            "kind": item.get("kind"),
            "mode": item.get("mode"),
            "fraction": item.get("fraction"),
            "bytes_done": item.get("bytes_done"),
            "bytes_total": item.get("bytes_total"),
            "cancel_requested": bool(item.get("cancel_requested")),
        }
        for column, path_kind in (("temp_path", "temp"), ("final_path", "final")):
            raw = item.get(column)
            if not raw:
                continue
            index.setdefault(
                _real_key(raw), {**marker, "path_kind": path_kind}
            )
    return index


def _active_for_target(conn: sqlite3.Connection, target: Path) -> list[dict[str, Any]]:
    key = _real_key(target)
    return [
        row for row in _active_rows(conn)
        if any(row.get(column) and _real_key(row[column]) == key
               for column in ("temp_path", "final_path"))
    ]


def _active_under(conn: sqlite3.Connection, base: Path) -> list[dict[str, Any]]:
    return [
        row for row in _active_rows(conn)
        if any(row.get(column) and _is_under(row[column], base)
               for column in ("temp_path", "final_path"))
    ]


# -------------------------------------------------------------------- the tree
def _scan_directory(
    base: Path,
    rel_prefix: str,
    active_index: Mapping[Path, dict[str, Any]],
    errors: list[str],
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Recursively list *base* without ever following a symlink."""
    children: list[dict[str, Any]] = []
    total_size = 0
    file_count = 0
    dir_count = 0

    try:
        with os.scandir(base) as scan:
            entries = list(scan)
    except PermissionError:
        errors.append(f"{rel_prefix or base.name}: permission denied")
        return children, total_size, file_count, dir_count
    except OSError as exc:
        errors.append(f"{rel_prefix or base.name}: {exc.strerror or exc}")
        return children, total_size, file_count, dir_count

    for entry in entries:
        rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            errors.append(f"{rel}: {exc.strerror or exc}")
            children.append({
                "name": entry.name,
                "path": rel,
                "kind": "unreadable",
                "size": 0,
                "modified": None,
                "active": None,
                "error": exc.strerror or str(exc),
            })
            continue

        modified = int(info.st_mtime)
        if entry.is_symlink():
            children.append({
                "name": entry.name,
                "path": rel,
                "kind": "symlink",
                "size": int(info.st_size),
                "modified": modified,
                "active": None,
                "error": None,
            })
            continue

        if entry.is_dir(follow_symlinks=False):
            sub_children, sub_size, sub_files, sub_dirs = _scan_directory(
                base / entry.name, rel, active_index, errors
            )
            children.append({
                "name": entry.name,
                "path": rel,
                "kind": "dir",
                "size": sub_size,
                "modified": modified,
                "file_count": sub_files,
                "dir_count": sub_dirs,
                "active": None,
                "error": None,
                "children": sub_children,
            })
            total_size += sub_size
            file_count += sub_files
            dir_count += 1 + sub_dirs
            continue

        if entry.is_file(follow_symlinks=False):
            size = int(info.st_size)
            children.append({
                "name": entry.name,
                "path": rel,
                "kind": "file",
                "size": size,
                "modified": modified,
                "active": active_index.get(_real_key(entry.path)),
                "error": None,
            })
            total_size += size
            file_count += 1
            continue

        children.append({
            "name": entry.name,
            "path": rel,
            "kind": "other",
            "size": 0,
            "modified": modified,
            "active": None,
            "error": None,
        })

    # Folders first, then files, each case-insensitively sorted. The UI keeps
    # this order so a refresh does not reshuffle the tree.
    children.sort(key=lambda node: (node["kind"] != "dir", node["name"].casefold()))
    return children, total_size, file_count, dir_count


def _folder_node(
    key: str,
    active_rows: list[dict[str, Any]],
    active_index: Mapping[Path, dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    try:
        path = folder_path(key)
    except StorageError as exc:
        return {
            "key": key,
            "label": FOLDER_LABELS[key],
            "path": None,
            "exists": False,
            "size": 0,
            "file_count": 0,
            "dir_count": 0,
            "active_count": 0,
            "children": [],
            "error": str(exc),
        }

    children, total_size, file_count, dir_count = _scan_directory(
        path, _relative_inside(path, media_root()) or path.name, active_index, errors
    )
    active_count = sum(
        1 for row in active_rows
        if any(row.get(column) and _is_under(row[column], path)
               for column in ("temp_path", "final_path"))
    )
    return {
        "key": key,
        "label": path.name,
        "path": _relative_inside(path, media_root()),
        "exists": True,
        "size": total_size,
        "file_count": file_count,
        "dir_count": dir_count,
        "active_count": active_count,
        "children": children,
        "error": None,
    }


def _download_summary(row: Mapping[str, Any], root: Path) -> dict[str, Any]:
    paths = []
    for column in ("temp_path", "final_path"):
        raw = row.get(column)
        if raw and _relative_inside(raw, root) is not None:
            paths.append(_relative_inside(raw, root))
    return {
        "id": int(row["id"]),
        "status": row["status"],
        "title": row.get("title"),
        "kind": row.get("kind"),
        "mode": row.get("mode"),
        "fraction": row.get("fraction"),
        "bytes_done": row.get("bytes_done"),
        "bytes_total": row.get("bytes_total"),
        "cancel_requested": bool(row.get("cancel_requested")),
        "paths": paths,
    }


def tree(conn: sqlite3.Connection, folder: str | None = None) -> dict[str, Any]:
    """Describe the whole media root. Read-only and safe to call at any time."""
    if folder not in (None, "", "all") and folder not in FOLDER_KEYS:
        raise StorageError("Unknown storage folder.")

    root = media_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(
            f"The media root could not be created: {exc.strerror or exc}"
        ) from exc

    active_rows = _active_rows(conn)
    active_index = _active_index(active_rows)
    errors: list[str] = []
    folders = [
        _folder_node(key, active_rows, active_index, errors) for key in FOLDER_KEYS
    ]
    totals = {
        "size": sum(node["size"] for node in folders),
        "files": sum(node["file_count"] for node in folders),
        "dirs": sum(node["dir_count"] for node in folders),
        "active": sum(node["active_count"] for node in folders),
    }
    return {
        "root": str(root),
        "root_display": config.media_host_dir(),
        "selected": folder if folder in FOLDER_KEYS else "all",
        "folders": folders,
        "totals": totals,
        "active_downloads": [_download_summary(row, root) for row in active_rows],
        "errors": errors,
        "generated_at": int(time.time()),
    }


# ------------------------------------------------------------- cancellation
def _wait_for_cancel(
    conn: sqlite3.Connection, download_ids: list[int], wait: float
) -> bool:
    """Wait for cancelled downloads to leave active status.

    Returns True when they are all terminal, False when the timeout expired
    while a transfer was still stopping. Raises when one of them has entered
    the import phase, because an import cannot be cancelled safely.
    """
    ids = [int(value) for value in download_ids]
    if not ids:
        return True
    placeholders = ", ".join("?" for _ in ids)
    deadline = time.monotonic() + max(0.0, float(wait))
    while True:
        rows = conn.execute(
            f"SELECT id, status FROM downloads WHERE id IN ({placeholders})", ids
        ).fetchall()
        statuses = {int(row["id"]): row["status"] for row in rows}
        if any(status == "importing" for status in statuses.values()):
            raise StorageConflict(
                "A download started importing while it was being cancelled. "
                "The file was left in place; try again when the import finishes."
            )
        if all(status in TERMINAL_STATUSES for status in statuses.values()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _cancel_for_deletion(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    cancel_wait: float,
) -> tuple[list[int], str | None]:
    from . import downloader

    if any(row.get("status") == "importing" for row in rows):
        raise StorageConflict(
            "That file belongs to a download that is being imported. "
            "Wait for the import to finish before deleting it."
        )
    cancelled: list[int] = []
    for row in rows:
        try:
            downloader.cancel_download(conn, int(row["id"]))
        except LookupError:
            continue
        except ValueError as exc:
            message = str(exc)
            if "importing" in message.lower():
                raise StorageConflict(message) from exc
            if "already finished" in message.lower():
                continue
            raise StorageConflict(message) from exc
        cancelled.append(int(row["id"]))

    warning = None
    if cancelled and not _wait_for_cancel(conn, cancelled, cancel_wait):
        warning = (
            "The download was asked to cancel but was still stopping. "
            "Its partial file will be removed and the worker will mark it cancelled."
        )
    return cancelled, warning


# ---------------------------------------------------------------- database
def _insert_history(
    conn: sqlite3.Connection,
    *,
    event: str,
    kind: str | None,
    title: str,
    movie_id: int | None = None,
    series_id: int | None = None,
    episode_id: int | None = None,
    message: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO history (event, kind, movie_id, series_id, episode_id, "
        "title, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event, kind, movie_id, series_id, episode_id, title, message, int(time.time())),
    )


#: Columns that describe a file rather than the library entry, cleared when the
#: file is deleted. This mirrors the library scan's "missing file" repair.
_FILE_COLUMNS = (
    "file_path = NULL, file_size = NULL, resolution = NULL, imported_at = NULL, "
    "file_score = NULL"
)


def _reconcile_deleted(conn: sqlite3.Connection, target: Path) -> dict[str, int]:
    """Clear the tracked file row matching *target*, keeping the entry."""
    target_str = str(target)
    result = {"movies": 0, "episodes": 0, "alternates": 0}
    try:
        with conn:
            for row in conn.execute(
                "SELECT id, title FROM movies WHERE file_path = ?", (target_str,)
            ).fetchall():
                conn.execute(
                    f"UPDATE movies SET {_FILE_COLUMNS} WHERE id = ?", (row["id"],)
                )
                _insert_history(
                    conn, event="deleted", kind="movie", title=row["title"],
                    movie_id=row["id"], message="file deleted from storage",
                )
                result["movies"] += 1

            for row in conn.execute(
                "SELECT e.id, e.series_id, e.season, e.episode, "
                "s.title AS series_title FROM series_episodes e "
                "JOIN series s ON s.id = e.series_id WHERE e.file_path = ?",
                (target_str,),
            ).fetchall():
                conn.execute(
                    f"UPDATE series_episodes SET {_FILE_COLUMNS} WHERE id = ?",
                    (row["id"],),
                )
                _insert_history(
                    conn, event="deleted", kind="episode",
                    title=f"{row['series_title']} S{row['season']:02d}E{row['episode']:02d}",
                    series_id=row["series_id"], episode_id=row["id"],
                    message="file deleted from storage",
                )
                result["episodes"] += 1

            for row in conn.execute(
                "SELECT id, movie_id, name FROM movie_alternates WHERE file_path = ?",
                (target_str,),
            ).fetchall():
                conn.execute(
                    "DELETE FROM movie_alternates WHERE id = ?", (row["id"],)
                )
                _insert_history(
                    conn, event="deleted", kind="movie",
                    title=str(row["name"] or "Alternate version"),
                    movie_id=row["movie_id"], message="file deleted from storage",
                )
                result["alternates"] += 1
    except sqlite3.Error as exc:
        logger.exception("storage: database reconciliation failed for %s", target)
        raise StorageError(
            "The file was removed, but the library database could not be updated. "
            "Run Scan library to reconcile it."
        ) from exc
    return result


def _reconcile_under(conn: sqlite3.Connection, base: Path) -> dict[str, int]:
    """Clear tracked files under *base* that no longer exist.

    Only rows whose path is gone are touched. A permission error during the
    empty operation therefore leaves the still-present file tracked rather
    than producing an inconsistent database.
    """
    result = {"movies": 0, "episodes": 0, "alternates": 0}
    prefix = str(base) + os.sep
    like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        with conn:
            for row in conn.execute(
                "SELECT id, title, file_path FROM movies "
                "WHERE file_path LIKE ? ESCAPE '\\'", (like,)
            ).fetchall():
                if not _is_under(row["file_path"], base):
                    continue
                if os.path.lexists(row["file_path"]):
                    continue
                conn.execute(
                    f"UPDATE movies SET {_FILE_COLUMNS} WHERE id = ?", (row["id"],)
                )
                result["movies"] += 1

            for row in conn.execute(
                "SELECT e.id, e.file_path FROM series_episodes e "
                "WHERE e.file_path LIKE ? ESCAPE '\\'", (like,)
            ).fetchall():
                if not _is_under(row["file_path"], base):
                    continue
                if os.path.lexists(row["file_path"]):
                    continue
                conn.execute(
                    f"UPDATE series_episodes SET {_FILE_COLUMNS} WHERE id = ?",
                    (row["id"],),
                )
                result["episodes"] += 1

            for row in conn.execute(
                "SELECT id, file_path FROM movie_alternates "
                "WHERE file_path LIKE ? ESCAPE '\\'", (like,)
            ).fetchall():
                if not _is_under(row["file_path"], base):
                    continue
                if os.path.lexists(row["file_path"]):
                    continue
                conn.execute(
                    "DELETE FROM movie_alternates WHERE id = ?", (row["id"],)
                )
                result["alternates"] += 1

            if any(result.values()):
                _insert_history(
                    conn, event="deleted", kind="storage", title=base.name,
                    message=(
                        f"Emptied {base.name}: cleared {result['movies']} movie, "
                        f"{result['episodes']} episode and "
                        f"{result['alternates']} alternate file link(s)."
                    ),
                )
    except sqlite3.Error as exc:
        logger.exception("storage: database reconciliation failed for %s", base)
        raise StorageError(
            "Files were removed, but the library database could not be updated. "
            "Run Scan library to reconcile it."
        ) from exc
    return result


# ----------------------------------------------------------------- delete
def delete_file(
    conn: sqlite3.Connection,
    rel_key: object,
    *,
    confirm: bool = False,
    confirm_active: bool = False,
    cancel_wait: float = CANCEL_WAIT_SECONDS,
) -> dict[str, Any]:
    """Delete one regular file, with explicit confirmation as a precondition.

    ``confirm_active`` is a second flag because deleting a staging file has a
    side effect: the owning download is cancelled. Directories are refused by
    this endpoint; the folder-level operation is :func:`empty_folder`.
    """
    if confirm is not True:
        raise StorageError("Deleting a file requires explicit confirmation.")
    target = _confined_path(rel_key)
    _assert_not_protected(target)
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise StorageNotFound("That file no longer exists.") from exc
    except OSError as exc:
        raise StorageError(
            f"That file could not be inspected: {exc.strerror or exc}"
        ) from exc
    if stat_module.S_ISLNK(info.st_mode):
        raise StorageForbidden("Refusing to follow a symbolic link.")
    if stat_module.S_ISDIR(info.st_mode):
        raise StorageError("That is a folder. Use Empty folder to remove its contents.")
    if not stat_module.S_ISREG(info.st_mode):
        raise StorageError("Only regular files can be deleted.")

    active = _active_for_target(conn, target)
    cancelled: list[int] = []
    warning = None
    if active:
        if not confirm_active:
            raise StorageConflict(
                "That file belongs to an active download. Deleting it will cancel "
                "the download; confirm the active-file deletion explicitly."
            )
        cancelled, warning = _cancel_for_deletion(conn, active, cancel_wait)

    # Re-check immediately before unlinking. The path was already checked with
    # lstat, but this narrows the window in which an entry could be swapped for
    # a symlink. unlink() itself never follows a final symlink.
    try:
        current = target.lstat()
    except FileNotFoundError as exc:
        raise StorageNotFound("That file no longer exists.") from exc
    if stat_module.S_ISLNK(current.st_mode):
        raise StorageForbidden("Refusing to follow a symbolic link.")
    if not stat_module.S_ISREG(current.st_mode):
        raise StorageError("Only regular files can be deleted.")
    size = int(current.st_size)

    try:
        target.unlink()
    except FileNotFoundError as exc:
        raise StorageNotFound("That file no longer exists.") from exc
    except PermissionError as exc:
        raise StorageForbidden(
            f"Permission denied deleting {target.name}."
        ) from exc
    except OSError as exc:
        raise StorageError(
            f"{target.name} could not be deleted: {exc.strerror or exc}"
        ) from exc

    library = _reconcile_deleted(conn, target)
    logger.info("storage: deleted %s (%d bytes)", target, size)
    return {
        "path": rel_key if isinstance(rel_key, str) else str(rel_key),
        "size": size,
        "cancelled_downloads": cancelled,
        "library": library,
        **({"warning": warning} if warning else {}),
    }


# ------------------------------------------------------------- empty folder
def _remove_contents(directory: Path, stats: dict[str, Any]) -> None:
    """Remove everything inside *directory* but keep the directory itself."""
    try:
        with os.scandir(directory) as scan:
            entries = list(scan)
    except PermissionError:
        stats["errors"].append(f"{directory}: permission denied")
        return
    except OSError as exc:
        stats["errors"].append(f"{directory}: {exc.strerror or exc}")
        return

    for entry in entries:
        try:
            if entry.is_symlink():
                # Unlinking a link never touches its target.
                os.unlink(entry.path)
                stats["symlinks"] += 1
            elif entry.is_dir(follow_symlinks=False):
                _remove_contents(Path(entry.path), stats)
                os.rmdir(entry.path)
                stats["dirs"] += 1
            elif entry.is_file(follow_symlinks=False):
                size = int(entry.stat(follow_symlinks=False).st_size)
                os.unlink(entry.path)
                stats["files"] += 1
                stats["bytes"] += size
            else:
                stats["errors"].append(
                    f"{entry.path}: not a regular file; skipped"
                )
        except FileNotFoundError:
            continue
        except PermissionError:
            stats["errors"].append(f"{entry.path}: permission denied")
        except OSError as exc:
            stats["errors"].append(f"{entry.path}: {exc.strerror or exc}")


def empty_folder(
    conn: sqlite3.Connection,
    folder_key: object,
    *,
    confirm: bool = False,
    confirm_active: bool = False,
    cancel_wait: float = CANCEL_WAIT_SECONDS,
) -> dict[str, Any]:
    """Remove the contents of one known folder; never the folder or root."""
    if not isinstance(folder_key, str) or folder_key not in FOLDER_KEYS:
        raise StorageError("Unknown storage folder.")
    if confirm is not True:
        raise StorageError("Emptying a folder requires explicit confirmation.")

    base = folder_path(folder_key)
    active = _active_under(conn, base)
    cancelled: list[int] = []
    warning = None
    if active:
        if any(row.get("status") == "importing" for row in active):
            raise StorageConflict(
                "A download in this folder is being imported. "
                "Wait for the import to finish before emptying the folder."
            )
        if not confirm_active:
            raise StorageConflict(
                "This folder contains active downloads. Emptying it will cancel "
                "them; confirm the active-file deletion explicitly."
            )
        cancelled, warning = _cancel_for_deletion(conn, active, cancel_wait)

    stats: dict[str, Any] = {
        "files": 0, "dirs": 0, "bytes": 0, "symlinks": 0, "errors": [],
    }
    _remove_contents(base, stats)
    library = _reconcile_under(conn, base)
    logger.info(
        "storage: emptied %s (%d files, %d dirs, %d bytes)",
        base, stats["files"], stats["dirs"], stats["bytes"],
    )
    result: dict[str, Any] = {
        "folder": folder_key,
        "label": base.name,
        "path": _relative_inside(base, media_root()),
        "deleted_files": stats["files"],
        "deleted_dirs": stats["dirs"],
        "deleted_symlinks": stats["symlinks"],
        "bytes_freed": stats["bytes"],
        "cancelled_downloads": cancelled,
        "library": library,
        "errors": stats["errors"],
    }
    if warning:
        result["warning"] = warning
    return result


__all__ = [
    "ACTIVE_STATUSES",
    "CANCEL_WAIT_SECONDS",
    "FOLDER_KEYS",
    "FOLDER_LABELS",
    "StorageConflict",
    "StorageError",
    "StorageForbidden",
    "StorageNotFound",
    "delete_file",
    "empty_folder",
    "folder_options",
    "folder_path",
    "media_root",
    "tree",
]
