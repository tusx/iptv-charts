#!/usr/bin/env python3
"""Storage view tests: tree, confinement, cancellation and reconciliation.

Run with:  .venv/bin/python tests/storage.py

Dependency-free and offline, matching the other suites. Each scenario gets its
own temporary data/config/cache/media roots; the IPTV_CHARTS_* variables are
set before importing the application so config lookups see the same paths.
The Flask blueprint is registered in a small test app, which is exactly what
integration needs to do in app.py (no app.py edit is required here).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_BASE = Path(tempfile.mkdtemp(prefix="iptv-storage-test-"))

_RESULT: list[tuple[bool, str, str]] = []


def _set_dirs(root: Path) -> None:
    os.environ["IPTV_CHARTS_DATA_DIR"] = str(root / "data")
    os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(root / "config")
    os.environ["IPTV_CHARTS_CACHE_DIR"] = str(root / "cache")
    os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(root / "media")
    os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
    os.environ.pop("IPTV_CHARTS_MOVIE_DIR", None)
    os.environ.pop("IPTV_CHARTS_SERIES_DIR", None)
    os.environ["IPTV_CHARTS_SCHEDULER"] = "0"


_set_dirs(_BASE)

from flask import Flask  # noqa: E402

from iptv_charts import config, db, storage, storage_web  # noqa: E402


def check(label: str, got, want) -> None:
    ok = got == want
    _RESULT.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def check_false(label: str, got) -> None:
    check(label, bool(got), False)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def raises(label: str, exc_type: type[BaseException], contains: str | None = None):
    """Decorator-like helper used as: raises("x", StorageError) and call."""
    def run(fn):
        try:
            fn()
        except exc_type as exc:
            check_true(label, True)
            if contains:
                check_true(f"{label} message", contains in str(exc))
            return exc
        except BaseException as exc:  # noqa: BLE001 - test reports the mismatch
            check(label, f"raised {type(exc).__name__}: {exc}", exc_type.__name__)
            return exc
        check(label, "returned normally", exc_type.__name__)
        return None
    return run


@contextmanager
def workspace(prefix: str = "iptv-storage-"):
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        _set_dirs(Path(tmp))
        db.init_db()
        conn = db.connect()
        try:
            yield Path(tmp), conn
        finally:
            conn.close()


def write(path: Path, content: bytes = b"data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def add_movie(conn, *, tmdb_id: int = 1, title: str = "Movie One",
              file_path: str | None = None, size: int | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO movies (tmdb_id, title, added_at, file_path, file_size, "
        "resolution, imported_at, file_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tmdb_id, title, int(time.time()), file_path, size,
         "1080p" if file_path else None, int(time.time()) if file_path else None,
         20 if file_path else None),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_episode(conn, *, tmdb_id: int = 100, title: str = "Show",
                file_path: str | None = None, size: int | None = None) -> tuple[int, int]:
    cursor = conn.execute(
        "INSERT INTO series (tmdb_id, title, added_at) VALUES (?, ?, ?)",
        (tmdb_id, title, int(time.time())),
    )
    series_id = int(cursor.lastrowid)
    cursor = conn.execute(
        "INSERT INTO series_episodes (series_id, season, episode, title, "
        "file_path, file_size, resolution, imported_at, file_score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (series_id, 1, 2, "Pilot", file_path, size,
         "1080p" if file_path else None, int(time.time()) if file_path else None,
         20 if file_path else None),
    )
    conn.commit()
    return series_id, int(cursor.lastrowid)


def add_alternate(conn, movie_id: int, file_path: str) -> int:
    cursor = conn.execute(
        "INSERT INTO movie_alternates (movie_id, remote_id, name, file_path, "
        "imported_at) VALUES (?, ?, ?, ?, ?)",
        (movie_id, "remote-1", "Alternate version", file_path, int(time.time())),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_download(conn, *, temp_path: str | None = None,
                 final_path: str | None = None, status: str = "downloading",
                 title: str = "Movie One", movie_id: int | None = None,
                 bytes_done: int = 0, bytes_total: int | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO downloads (kind, movie_id, provider_kind, "
        "provider_remote_id, title, status, temp_path, final_path, "
        "bytes_done, bytes_total, requested_at) "
        "VALUES (?, ?, 'vod', 'remote-1', ?, ?, ?, ?, ?, ?, ?)",
        ("movie", movie_id, title, status, temp_path, final_path,
         bytes_done, bytes_total, int(time.time())),
    )
    conn.commit()
    return int(cursor.lastrowid)


def download_row(conn, download_id: int) -> dict | None:
    row = db.get_one(conn, "SELECT * FROM downloads WHERE id = ?", (download_id,))
    return dict(row) if row else None


def find_node(tree: dict, rel: str) -> dict | None:
    def walk(node):
        if node.get("path") == rel:
            return node
        for child in node.get("children") or []:
            found = walk(child)
            if found:
                return found
        return None

    for folder in tree["folders"]:
        found = walk(folder)
        if found:
            return found
    return None


def folder_node(tree: dict, key: str) -> dict:
    return next(folder for folder in tree["folders"] if folder["key"] == key)


def make_app() -> Flask:
    app = Flask(
        "storage-test",
        template_folder=str(ROOT / "iptv_charts" / "templates"),
        static_folder=str(ROOT / "iptv_charts" / "static"),
    )
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(storage_web.bp)

    def stub():
        return "stub"

    # The storage template extends base.html, so the test app needs the
    # endpoints that base navigation and the page links call.
    app.add_url_rule("/", endpoint="home", view_func=stub)
    app.add_url_rule("/playlists", endpoint="sources_list", view_func=stub)
    app.add_url_rule("/movies", endpoint="manager.movies", view_func=stub)
    app.add_url_rule("/series", endpoint="manager.series", view_func=stub)
    app.add_url_rule("/activity", endpoint="manager.activity", view_func=stub)
    app.add_url_rule("/settings", endpoint="settings", view_func=stub)

    @app.context_processor
    def inject_globals():
        return {"app_name": config.APP_NAME, "app_version": "0.8.0-test"}

    @app.teardown_appcontext
    def close_connection(exception):
        from flask import g

        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    return app


# ---------------------------------------------------------- config detection
def test_config_detection() -> None:
    section("media folder detection")

    with workspace() as (tmp, _conn):
        media = config.media_dir()
        (media / "movies").mkdir(parents=True)
        (media / "TV Shows").mkdir()
        check("detect lowercase movies", config.movie_library_dir(), media / "movies")
        check("detect TV Shows", config.series_library_dir(), media / "TV Shows")
        check("detect helper returns movie path",
              config.detect_movie_library_dir(), media / "movies")
        check("detect helper returns series path",
              config.detect_series_library_dir(), media / "TV Shows")

    with workspace() as (tmp, _conn):
        media = config.media_dir()
        (media / "MOVIES").mkdir(parents=True)
        check("detection is case-insensitive", config.movie_library_dir(), media / "MOVIES")

    with workspace() as (tmp, _conn):
        media = config.media_dir()
        (media / "movie").mkdir(parents=True)
        (media / "tv").mkdir()
        check("detect singular movie", config.movie_library_dir(), media / "movie")
        check("detect tv alias", config.series_library_dir(), media / "tv")

    with workspace() as (tmp, _conn):
        media = config.media_dir()
        (media / "Movies").mkdir(parents=True)
        (media / "movies").mkdir()
        check("canonical Movies wins over lowercase",
              config.movie_library_dir(), media / "Movies")

    with workspace() as (tmp, _conn):
        media = config.media_dir()
        # Neither folder exists: the defaults are created, exactly as before.
        check("default movie folder created",
              config.movie_library_dir(), media / "Movies")
        check_true("default movie folder exists", (media / "Movies").is_dir())
        check("default series folder created",
              config.series_library_dir(), media / "Series")
        check("incomplete folder", config.incomplete_dir(), media / ".incomplete")

    with workspace() as (tmp, _conn):
        media = config.media_dir()
        os.environ["IPTV_CHARTS_MOVIE_DIR"] = "Custom Movies"
        os.environ["IPTV_CHARTS_SERIES_DIR"] = "Custom Shows"
        check("relative movie override",
              config.movie_library_dir(), media / "Custom Movies")
        check("relative series override",
              config.series_library_dir(), media / "Custom Shows")
        os.environ.pop("IPTV_CHARTS_MOVIE_DIR")
        os.environ.pop("IPTV_CHARTS_SERIES_DIR")

    with workspace() as (tmp, _conn):
        absolute = tmp / "absolute-movies"
        os.environ["IPTV_CHARTS_MOVIE_DIR"] = str(absolute)
        check("absolute movie override", config.movie_library_dir(), absolute)
        check_true("absolute override created", absolute.is_dir())
        os.environ.pop("IPTV_CHARTS_MOVIE_DIR")


# ---------------------------------------------------------------- tree tests
def test_tree() -> None:
    section("tree: nesting, sizes, times and active markers")

    with workspace() as (tmp, conn):
        movies = config.movie_library_dir()
        series = config.series_library_dir()
        incomplete = config.incomplete_dir()

        movie_dir = movies / "Movie One"
        movie_file = write(movie_dir / "Movie One.mkv", b"123456789012")
        os.utime(movie_file, (1_000_000_000, 1_000_000_000))
        os.utime(movie_dir, (1_000_000_000, 1_000_000_000))
        loose = write(movies / "loose.txt", b"abc")
        episode = write(series / "Show" / "Season 01" / "S01E01.mkv", b"1234567")
        partial = write(incomplete / "job.part", b"12345")

        temp_download = add_download(
            conn, temp_path=str(partial), status="downloading",
            bytes_done=2, bytes_total=10,
        )
        final_download = add_download(
            conn, final_path=str(loose), status="importing",
        )

        tree = storage.tree(conn)
        check("root", tree["root"], str(config.media_dir()))
        check("folder order", [f["key"] for f in tree["folders"]],
              ["movies", "series", "incomplete"])
        check("movie label", folder_node(tree, "movies")["label"], "Movies")
        check("incomplete label",
              folder_node(tree, "incomplete")["label"], ".incomplete")

        node = find_node(tree, "Movies/Movie One")
        check("nested dir kind", node["kind"], "dir")
        check("nested dir size", node["size"], 12)
        check("nested dir file count", node["file_count"], 1)
        check("nested dir modified", node["modified"], 1_000_000_000)

        node = find_node(tree, "Movies/Movie One/Movie One.mkv")
        check("file kind", node["kind"], "file")
        check("file size", node["size"], 12)
        check("file modified", node["modified"], 1_000_000_000)
        check("file has no active marker", node["active"], None)

        node = find_node(tree, "Movies/loose.txt")
        check("final_path marker id", node["active"]["id"], final_download)
        check("final_path marker status", node["active"]["status"], "importing")

        node = find_node(tree, "Movies")
        check("movies folder size", node["size"], 15)
        check("movies folder files", node["file_count"], 2)
        check("movies folder dirs", node["dir_count"], 1)
        check("movies active count", node["active_count"], 1)

        node = find_node(tree, "Series/Show/Season 01/S01E01.mkv")
        check("episode size", node["size"], 7)

        node = find_node(tree, ".incomplete/job.part")
        check("temp marker id", node["active"]["id"], temp_download)
        check("temp marker path kind", node["active"]["path_kind"], "temp")
        check("temp marker fraction", node["active"]["fraction"], 0.2)

        check("totals files", tree["totals"]["files"], 4)
        check("totals dirs", tree["totals"]["dirs"], 3)
        check("totals size", tree["totals"]["size"], 12 + 3 + 7 + 5)
        check("totals active", tree["totals"]["active"], 2)
        check("active download summaries", len(tree["active_downloads"]), 2)
        check("tree errors", tree["errors"], [])

        check("selected all by default", tree["selected"], "all")
        filtered = storage.tree(conn, "movies")
        check("selected folder echoed", filtered["selected"], "movies")
        raises("unknown folder filter", storage.StorageError, "Unknown")(
            lambda: storage.tree(conn, "../movies")
        )


# ------------------------------------------------------- confinement/traversal
def test_confinement() -> None:
    section("confinement: traversal, absolute paths and directories")

    with workspace() as (tmp, conn):
        movies = config.movie_library_dir()
        safe = write(movies / "safe.txt", b"safe")
        outside = write(tmp / "outside.txt", b"outside")

        raises("parent traversal", storage.StorageError, "outside")(
            lambda: storage.delete_file(conn, "../outside.txt", confirm=True)
        )
        check_true("parent traversal left outside intact", outside.exists())

        raises("nested traversal", storage.StorageError, "outside")(
            lambda: storage.delete_file(
                conn, "Movies/../../outside.txt", confirm=True)
        )
        raises("dot component", storage.StorageError, "outside")(
            lambda: storage.delete_file(conn, "Movies/./safe.txt", confirm=True)
        )
        raises("absolute path", storage.StorageError, "outside")(
            lambda: storage.delete_file(conn, "/etc/passwd", confirm=True)
        )
        raises("backslash separator", storage.StorageError, "outside")(
            lambda: storage.delete_file(
                conn, "Movies\\..\\outside.txt", confirm=True)
        )
        raises("empty path", storage.StorageError)(
            lambda: storage.delete_file(conn, "", confirm=True)
        )
        raises("null byte", storage.StorageError)(
            lambda: storage.delete_file(conn, "Movies/\x00safe.txt", confirm=True)
        )

        raises("confirmation is mandatory", storage.StorageError, "confirm")(
            lambda: storage.delete_file(conn, "Movies/safe.txt")
        )
        check_true("unconfirmed delete left the file", safe.exists())

        raises("directories are refused by the file endpoint",
               storage.StorageError, "folder")(
            lambda: storage.delete_file(conn, "Movies", confirm=True)
        )
        check_true("movies folder still exists", movies.is_dir())
        check_true("outside copy still exists", outside.exists())


# ------------------------------------------------------------------- symlinks
def test_symlinks() -> None:
    section("symlinks: never followed")

    with workspace() as (tmp, conn):
        movies = config.movie_library_dir()
        config.series_library_dir()
        config.incomplete_dir()
        outside_dir = tmp / "outside"
        secret = write(outside_dir / "secret.mkv", b"secret")
        link_dir = movies / "escape"
        link_dir.symlink_to(outside_dir, target_is_directory=True)
        link_file = movies / "link.mkv"
        link_file.symlink_to(secret)

        tree = storage.tree(conn)
        node = find_node(tree, "Movies/escape")
        check("symlink dir kind", node["kind"], "symlink")
        check("symlink dir has no children", node.get("children"), None)
        check("outside file not listed", find_node(tree, "Movies/escape/secret.mkv"), None)
        node = find_node(tree, "Movies/link.mkv")
        check("symlink file kind", node["kind"], "symlink")

        raises("deleting a symlink is refused", storage.StorageForbidden, "symbolic")(
            lambda: storage.delete_file(conn, "Movies/link.mkv", confirm=True)
        )
        raises("walking through a symlink is refused",
               storage.StorageForbidden, "symbolic")(
            lambda: storage.delete_file(
                conn, "Movies/escape/secret.mkv", confirm=True)
        )
        check_true("outside secret intact", secret.exists())

        result = storage.empty_folder(conn, "movies", confirm=True)
        check("symlinks removed by empty", result["deleted_symlinks"], 2)
        check_false("link dir removed", os.path.lexists(link_dir))
        check_false("link file removed", os.path.lexists(link_file))
        check_true("outside secret still intact", secret.exists())
        check_true("movies folder kept", movies.is_dir())

    # A symlinked library folder itself must not be used as a tree root.
    with workspace() as (tmp, conn):
        media = config.media_dir()
        media.mkdir(parents=True)
        outside = tmp / "outside-movies"
        outside.mkdir()
        write(outside / "hidden.mkv", b"hidden")
        (media / "Movies").symlink_to(outside, target_is_directory=True)

        tree = storage.tree(conn)
        node = folder_node(tree, "movies")
        check_true("symlinked library folder reports an error",
                   "outside the media root" in (node["error"] or ""))
        raises("empty refuses a symlinked library folder",
               storage.StorageForbidden, "outside the media root")(
            lambda: storage.empty_folder(conn, "movies", confirm=True)
        )
        check_true("outside target intact", (outside / "hidden.mkv").exists())


# --------------------------------------------------- active confirmation/cancel
def test_active_downloads() -> None:
    section("active files: confirmation, cancellation and imports")

    with workspace() as (tmp, conn):
        incomplete = config.incomplete_dir()
        partial = write(incomplete / "queued.part", b"queued")
        download_id = add_download(
            conn, temp_path=str(partial), status="queued", bytes_done=1,
            bytes_total=10,
        )

        raises("active delete needs the second confirmation",
               storage.StorageConflict, "active download")(
            lambda: storage.delete_file(
                conn, ".incomplete/queued.part", confirm=True)
        )
        check_true("unconfirmed active file remains", partial.exists())
        check("unconfirmed active download unchanged",
              download_row(conn, download_id)["status"], "queued")

        result = storage.delete_file(
            conn, ".incomplete/queued.part", confirm=True, confirm_active=True,
        )
        check("queued download cancelled", result["cancelled_downloads"], [download_id])
        check_false("active file deleted", partial.exists())
        check("cancelled row status",
              download_row(conn, download_id)["status"], "cancelled")

    with workspace() as (tmp, conn):
        incomplete = config.incomplete_dir()
        partial = write(incomplete / "busy.part", b"busy")
        download_id = add_download(
            conn, temp_path=str(partial), status="downloading",
        )
        result = storage.delete_file(
            conn, ".incomplete/busy.part", confirm=True, confirm_active=True,
            cancel_wait=0,
        )
        check("downloading file deleted after cancel", partial.exists(), False)
        check("downloading cancel requested",
              download_row(conn, download_id)["cancel_requested"], 1)
        check_true("still-stopping warning", "warning" in result)

    with workspace() as (tmp, conn):
        incomplete = config.incomplete_dir()
        partial = write(incomplete / "importing.part", b"importing")
        download_id = add_download(
            conn, temp_path=str(partial), status="importing",
        )
        raises("importing file is refused", storage.StorageConflict, "import")(
            lambda: storage.delete_file(
                conn, ".incomplete/importing.part",
                confirm=True, confirm_active=True,
            )
        )
        check_true("importing file remains", partial.exists())
        check("importing row untouched",
              download_row(conn, download_id)["status"], "importing")

    with workspace() as (tmp, conn):
        incomplete = config.incomplete_dir()
        partial = write(incomplete / "final.part", b"final")
        download_id = add_download(
            conn, final_path=str(partial), status="importing",
        )
        raises("importing final_path is refused", storage.StorageConflict, "import")(
            lambda: storage.delete_file(
                conn, ".incomplete/final.part",
                confirm=True, confirm_active=True,
            )
        )
        check("importing final row untouched",
              download_row(conn, download_id)["status"], "importing")


# ------------------------------------------------------- database reconciliation
def test_reconciliation() -> None:
    section("database reconciliation keeps library entries")

    with workspace() as (tmp, conn):
        movies = config.movie_library_dir()
        series = config.series_library_dir()
        config.incomplete_dir()

        movie_file = write(movies / "Movie One.mkv", b"movie")
        movie_id = add_movie(conn, file_path=str(movie_file), size=movie_file.stat().st_size)
        other_movie_id = add_movie(
            conn, tmdb_id=2, title="Other Movie",
            file_path=str(write(movies / "Other.mkv", b"other")),
            size=5,
        )
        alternate_file = write(movies / "Movie One (alternate).mkv", b"alt")
        add_alternate(conn, movie_id, str(alternate_file))
        series_id, episode_id = add_episode(
            conn, file_path=str(write(
                series / "Show" / "S01E02.mkv", b"episode")),
        )

        storage.delete_file(conn, "Movies/Movie One.mkv", confirm=True)
        row = dict(db.get_one(conn, "SELECT * FROM movies WHERE id = ?", (movie_id,)))
        check("movie entry kept", row["id"], movie_id)
        check("movie file_path cleared", row["file_path"], None)
        check("movie file_size cleared", row["file_size"], None)
        check("movie file_score cleared", row["file_score"], None)
        history = db.get_one(
            conn, "SELECT * FROM history WHERE event = 'deleted' AND movie_id = ? "
            "ORDER BY id DESC LIMIT 1", (movie_id,),
        )
        check("movie deletion history", history["title"], "Movie One")
        other = db.get_one(conn, "SELECT * FROM movies WHERE id = ?", (other_movie_id,))
        check_true("unrelated movie untouched", other["file_path"] is not None)

        storage.delete_file(
            conn, "Series/Show/S01E02.mkv", confirm=True)
        episode = dict(db.get_one(
            conn, "SELECT * FROM series_episodes WHERE id = ?", (episode_id,)))
        check("episode entry kept", episode["id"], episode_id)
        check("episode file_path cleared", episode["file_path"], None)
        episode_history = db.get_one(
            conn, "SELECT * FROM history WHERE event = 'deleted' "
            "AND episode_id = ? ORDER BY id DESC LIMIT 1", (episode_id,),
        )
        check("episode history label", episode_history["title"], "Show S01E02")

        storage.delete_file(
            conn, "Movies/Movie One (alternate).mkv", confirm=True)
        check("alternate row removed", db.get_one(
            conn, "SELECT COUNT(*) AS n FROM movie_alternates"
        )["n"], 0)
        check_true("movie entry still exists after alternate delete",
                   db.get_one(conn, "SELECT id FROM movies WHERE id = ?",
                              (movie_id,)) is not None)

        # A file with no database row is still deletable.
        untracked = write(movies / "untracked.mkv", b"u")
        result = storage.delete_file(conn, "Movies/untracked.mkv", confirm=True)
        check("untracked file removed", untracked.exists(), False)
        check("untracked library counts", result["library"],
              {"movies": 0, "episodes": 0, "alternates": 0})


# ------------------------------------------------------------------ empty folder
def test_empty_folder() -> None:
    section("empty folder: confirmation, reconciliation and root safety")

    with workspace() as (tmp, conn):
        movies = config.movie_library_dir()
        series = config.series_library_dir()
        incomplete = config.incomplete_dir()

        nested = write(movies / "Movie One" / "Movie.mkv", b"movie")
        loose = write(movies / "loose.txt", b"loose")
        movie_id = add_movie(conn, file_path=str(nested), size=5)
        alternate_id = add_alternate(conn, movie_id, str(loose))
        series_file = write(series / "Show" / "S01E01.mkv", b"episode")
        _series_id, episode_id = add_episode(conn, file_path=str(series_file))
        leftover = write(incomplete / "old.part", b"old")
        outside = write(tmp / "outside.txt", b"outside")
        data_sentinel = write(config.data_dir() / "keep.txt", b"keep")
        config_sentinel = write(config.config_dir() / "keep.json", b"{}")

        # A link inside Movies must be unlinked, never followed.
        outside_dir = tmp / "outside-dir"
        write(outside_dir / "secret.mkv", b"secret")
        (movies / "escape").symlink_to(outside_dir, target_is_directory=True)

        raises("empty requires confirmation", storage.StorageError, "confirm")(
            lambda: storage.empty_folder(conn, "movies")
        )
        check_true("unconfirmed empty left files", nested.exists())

        raises("empty only accepts known folders", storage.StorageError, "folder")(
            lambda: storage.empty_folder(conn, "../media", confirm=True)
        )
        raises("empty never targets the root key", storage.StorageError, "folder")(
            lambda: storage.empty_folder(conn, "all", confirm=True)
        )

        result = storage.empty_folder(conn, "movies", confirm=True)
        check("empty file count", result["deleted_files"], 2)
        check("empty symlink count", result["deleted_symlinks"], 1)
        check("empty library movie cleared", result["library"]["movies"], 1)
        check("empty library alternate cleared", result["library"]["alternates"], 1)
        check_false("nested movie removed", nested.exists())
        check_false("loose file removed", loose.exists())
        check_true("movies folder itself kept", movies.is_dir())
        check("movies folder is empty", list(movies.iterdir()), [])
        check_true("outside link target intact", (outside_dir / "secret.mkv").exists())
        check_true("sibling outside file intact", outside.exists())
        check_true("series file untouched", series_file.exists())
        check_true("data sentinel untouched", data_sentinel.exists())
        check_true("config sentinel untouched", config_sentinel.exists())

        movie = dict(db.get_one(conn, "SELECT * FROM movies WHERE id = ?", (movie_id,)))
        check("emptied movie file_path cleared", movie["file_path"], None)
        check("emptied alternate row gone", db.get_one(
            conn, "SELECT COUNT(*) AS n FROM movie_alternates WHERE id = ?",
            (alternate_id,))["n"], 0)
        check_true("emptied episode untouched", db.get_one(
            conn, "SELECT file_path FROM series_episodes WHERE id = ?",
            (episode_id,))["file_path"] is not None)
        check_true("incomplete file untouched", leftover.exists())
        check_true("media root still exists", config.media_dir().is_dir())

        result = storage.empty_folder(conn, "series", confirm=True)
        check("series empty reconciled episodes", result["library"]["episodes"], 1)
        check("series episode path cleared", db.get_one(
            conn, "SELECT file_path FROM series_episodes WHERE id = ?",
            (episode_id,))["file_path"], None)

        # Emptying .incomplete while a download is active needs the second flag.
        active = write(incomplete / "active.part", b"active")
        download_id = add_download(
            conn, temp_path=str(active), status="queued")
        raises("incomplete with active download needs confirmation",
               storage.StorageConflict, "active downloads")(
            lambda: storage.empty_folder(conn, "incomplete", confirm=True)
        )
        check_true("active download file remains", active.exists())
        result = storage.empty_folder(
            conn, "incomplete", confirm=True, confirm_active=True)
        check("empty cancelled the download",
              result["cancelled_downloads"], [download_id])
        check_false("active staging file removed", active.exists())
        check("active download marked cancelled",
              download_row(conn, download_id)["status"], "cancelled")
        check_true("incomplete folder kept", incomplete.is_dir())


def test_protected_paths() -> None:
    section("protected data, config and database paths")

    with tempfile.TemporaryDirectory(prefix="iptv-storage-protected-") as tmp:
        base = Path(tmp)
        # Deliberately put the app's writable directories *inside* the media
        # root, which is the only way a configured library folder could
        # overlap them. The storage view must refuse that configuration.
        os.environ["IPTV_CHARTS_MEDIA_DIR"] = str(base)
        os.environ["IPTV_CHARTS_DATA_DIR"] = str(base / "data")
        os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(base / "config")
        os.environ["IPTV_CHARTS_CACHE_DIR"] = str(base / "cache")
        os.environ["IPTV_CHARTS_SCHEDULER"] = "0"
        os.environ.pop("IPTV_CHARTS_MOVIE_DIR", None)
        os.environ.pop("IPTV_CHARTS_SERIES_DIR", None)
        db.init_db()
        conn = db.connect()
        try:
            os.environ["IPTV_CHARTS_MOVIE_DIR"] = str(base / "data")
            raises("library folder overlapping data is refused",
                   storage.StorageForbidden, "overlap")(
                lambda: storage.folder_path("movies")
            )
            raises("empty refuses a folder overlapping data",
                   storage.StorageForbidden, "overlap")(
                lambda: storage.empty_folder(conn, "movies", confirm=True)
            )
            raises("database file deletion is refused",
                   storage.StorageForbidden, "overlap")(
                lambda: storage.delete_file(conn, "data/catalog.db", confirm=True)
            )
            check_true("database file still exists", config.db_path().exists())
        finally:
            os.environ.pop("IPTV_CHARTS_MOVIE_DIR", None)
            conn.close()


# --------------------------------------------------------------------- HTTP API
def test_api() -> None:
    section("blueprint: page and JSON endpoints")

    with workspace() as (tmp, conn):
        movies = config.movie_library_dir()
        config.series_library_dir()
        config.incomplete_dir()
        tracked = write(movies / "tracked.mkv", b"tracked")
        movie_id = add_movie(conn, file_path=str(tracked), size=7)
        disposable = write(movies / "disposable.mkv", b"disposable")
        active = write(config.incomplete_dir() / "active.part", b"active")
        download_id = add_download(
            conn, temp_path=str(active), status="queued", movie_id=movie_id)

        app = make_app()
        client = app.test_client()

        page = client.get("/storage")
        check("GET /storage status", page.status_code, 200)
        body = page.data
        check_true("page has title", b"<h1>Storage</h1>" in body)
        check_true("page has root selector", b'id="storage-root"' in body)
        check_true("page has tree container", b'id="storage-tree"' in body)
        check_true("page has delete dialog", b'id="storage-delete-dialog"' in body)
        check_true("page loads local css", b"css/storage.css" in body)
        check_true("page loads local js", b"js/storage.js" in body)
        check_true("page has home link", b'href="/"' in body)
        check_true("page has activity link", b'href="/activity"' in body)
        check_false("page has no external asset", b"https://cdn" in body)

        response = client.get("/api/storage/tree")
        check("GET tree status", response.status_code, 200)
        payload = response.get_json()
        check("tree ok", payload["ok"], True)
        check("tree folder count", len(payload["folders"]), 3)
        check("tree active marker",
              find_node(payload, ".incomplete/active.part")["active"]["id"],
              download_id)
        check("tree cache header", response.headers.get("Cache-Control"), "no-store")

        response = client.get("/api/storage/tree?folder=movies")
        check("tree folder filter status", response.status_code, 200)
        response = client.get("/api/storage/tree?folder=../etc")
        check("tree invalid folder status", response.status_code, 400)

        response = client.delete("/api/storage/file", json={
            "path": "Movies/tracked.mkv"})
        check("delete without confirmation status", response.status_code, 400)
        check("delete without confirmation error",
              response.get_json()["ok"], False)
        check_true("unconfirmed file remains", tracked.exists())

        response = client.delete("/api/storage/file", json={
            "path": "../outside.mkv", "confirm": True})
        check("delete traversal status", response.status_code, 400)

        response = client.delete("/api/storage/file")
        check("delete without a body status", response.status_code, 400)

        response = client.delete("/api/storage/file", json={
            "path": "Movies/tracked.mkv", "confirm": True})
        check("confirmed delete status", response.status_code, 200)
        payload = response.get_json()
        check("confirmed delete ok", payload["ok"], True)
        check("confirmed delete reconciled movie", payload["library"]["movies"], 1)
        check_false("confirmed delete removed the file", tracked.exists())
        check("confirmed delete cleared db", db.get_one(
            conn, "SELECT file_path FROM movies WHERE id = ?", (movie_id,)
        )["file_path"], None)
        check_true("disposable file untouched", disposable.exists())

        response = client.delete("/api/storage/file", json={
            "path": ".incomplete/active.part", "confirm": True})
        check("active delete without flag status", response.status_code, 409)
        check_true("active file remains", active.exists())

        response = client.delete("/api/storage/file", json={
            "path": ".incomplete/active.part", "confirm": True,
            "confirm_active": True})
        check("active delete with flag status", response.status_code, 200)
        check_false("active file removed", active.exists())
        check("active delete cancelled row", download_row(conn, download_id)["status"],
              "cancelled")

        response = client.post("/api/storage/empty", json={"folder": "movies"})
        check("empty without confirmation status", response.status_code, 400)
        check_true("empty without confirmation kept file", disposable.exists())

        response = client.post("/api/storage/empty", json={
            "folder": "../media", "confirm": True})
        check("empty unknown folder status", response.status_code, 400)

        response = client.post("/api/storage/empty", json={
            "folder": "movies", "confirm": True})
        check("empty status", response.status_code, 200)
        payload = response.get_json()
        check("empty ok", payload["ok"], True)
        check("empty deleted disposable", payload["deleted_files"], 1)
        check("empty kept the folder", movies.is_dir(), True)
        check("empty folder contents", list(movies.iterdir()), [])


def main() -> int:
    test_config_detection()
    test_tree()
    test_confinement()
    test_symlinks()
    test_active_downloads()
    test_reconciliation()
    test_empty_folder()
    test_protected_paths()
    test_api()

    failed = [item for item in _RESULT if not item[0]]
    print(f"\n{len(_RESULT) - len(failed)}/{len(_RESULT)} checks passed")
    if failed:
        print("\nFailures:")
        for _ok, label, detail in failed:
            print(f"  - {label}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
