"""Paths, ports and runtime settings.

Two ways to locate the writable directories, in priority order:

1. An explicit ``IPTV_CHARTS_*_DIR`` environment variable. A container sets
   these to its mount points, so paths inside the container are stated rather
   than inferred.
2. The XDG base-directory spec.

The point of the ordering is that the code never asks *where it is running* --
it is told where to write. Nothing here branches on container-vs-host.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

APP_NAME = "IPTV Charts"
APP_SLUG = "iptv-charts"
APP_ID = "io.github.tusx.IptvCharts"

# Port 8770 was chosen after finding 8081, 8088 and 8384 already occupied on
# the development machine. We still probe, because that says nothing about
# any other machine.
DEFAULT_PORT = 8770
PORT_PROBE_RANGE = 50

# Only ever bind loopback. This app holds provider credentials and has no
# authentication of its own, so it must never be reachable from the network.
BIND_HOST = "127.0.0.1"


def _resolve_dir(override_var: str, xdg_var: str, fallback: Path) -> Path:
    """An explicit override wins outright; otherwise follow XDG.

    Note the override replaces the whole path rather than having the app slug
    appended, so ``IPTV_CHARTS_DATA_DIR=/data`` really does mean ``/data``.
    """
    override = os.environ.get(override_var)
    path = Path(override) if override else _xdg_dir(xdg_var, fallback)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _xdg_dir(env_var: str, fallback: Path) -> Path:
    raw = os.environ.get(env_var)
    base = Path(raw) if raw else fallback
    return base / APP_SLUG


def data_dir() -> Path:
    """Directory for the database and other persistent state."""
    return _resolve_dir(
        "IPTV_CHARTS_DATA_DIR", "XDG_DATA_HOME", Path.home() / ".local" / "share"
    )


def config_dir() -> Path:
    """Directory for small runtime preferences."""
    return _resolve_dir(
        "IPTV_CHARTS_CONFIG_DIR", "XDG_CONFIG_HOME", Path.home() / ".config"
    )


def cache_dir() -> Path:
    """Directory for disposable data such as downloaded poster images."""
    return _resolve_dir(
        "IPTV_CHARTS_CACHE_DIR", "XDG_CACHE_HOME", Path.home() / ".cache"
    )


def play_queue_path() -> Path | None:
    """Where to hand off playback requests, when running as a container.

    A container has no way to start a process on the host, so instead of
    launching a player it appends a request to a file shared with a small
    host-side watcher (scripts/player-bridge.sh). Returns None when unset,
    which means "launch the player directly" -- the normal case on a host.
    """
    raw = os.environ.get("IPTV_CHARTS_PLAY_QUEUE")
    return Path(raw) if raw else None


def media_dir() -> Path:
    """Root of the downloaded media library.

    In the container this is ``/media``, bind-mounted from a host folder by
    build.deploy.sh. In dev mode it defaults to ``~/Media/IPTV Charts`` so a
    download still lands somewhere sensible without extra configuration.
    """
    raw = os.environ.get("IPTV_CHARTS_MEDIA_DIR")
    # Deliberately not created here: settings and health should be safe to
    # read without touching the user's home directory. The library helpers
    # below create the folders when something is actually written.
    return Path(raw) if raw else Path.home() / "Media" / "IPTV Charts"


def media_host_dir() -> str:
    """The media path as the user knows it on the host, for display."""
    return os.environ.get("IPTV_CHARTS_MEDIA_HOST_DIR") or str(media_dir())


#: Optional names or paths for the two library folders. An absolute value is
#: used as-is; a relative value is resolved under ``media_dir()``. The defaults
#: stay ``Movies``/``Series``, but an existing folder with one of the aliases
#: below is adopted instead, so a pre-existing ``movies`` or ``TV Shows``
#: library does not end up next to an empty default folder.
MOVIE_DIR_ENV = "IPTV_CHARTS_MOVIE_DIR"
SERIES_DIR_ENV = "IPTV_CHARTS_SERIES_DIR"
MOVIE_FOLDER_ALIASES = ("movies", "movie")
SERIES_FOLDER_ALIASES = ("series", "tv", "tv shows", "tv-shows")


def _library_override(var: str) -> Path | None:
    raw = os.environ.get(var)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else media_dir() / path


def _detect_library_dir(root: Path, aliases: tuple[str, ...],
                        canonical: str) -> Path | None:
    """Find an existing library folder under *root*, case-insensitively.

    The canonical name wins when it exists, then the aliases in their declared
    order. Only real directories count: a file called ``movies`` is not a
    library. Missing/unreadable roots simply mean "not detected".
    """
    candidate = root / canonical
    try:
        if candidate.is_dir():
            return candidate
    except OSError:
        return None
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    directories = {
        entry.name.casefold(): entry
        for entry in entries
        if not entry.is_symlink() and entry.is_dir()
    }
    for alias in aliases:
        found = directories.get(alias.casefold())
        if found is not None:
            return found
    return None


def detect_movie_library_dir(root: Path | None = None) -> Path | None:
    """The existing movie folder under the media root, if there is one."""
    return _detect_library_dir(root or media_dir(), MOVIE_FOLDER_ALIASES, "Movies")


def detect_series_library_dir(root: Path | None = None) -> Path | None:
    """The existing series folder under the media root, if there is one."""
    return _detect_library_dir(root or media_dir(), SERIES_FOLDER_ALIASES, "Series")


def movie_library_dir() -> Path:
    path = (_library_override(MOVIE_DIR_ENV)
            or detect_movie_library_dir()
            or media_dir() / "Movies")
    path.mkdir(parents=True, exist_ok=True)
    return path


def series_library_dir() -> Path:
    path = (_library_override(SERIES_DIR_ENV)
            or detect_series_library_dir()
            or media_dir() / "Series")
    path.mkdir(parents=True, exist_ok=True)
    return path


def incomplete_dir() -> Path:
    """Staging area on the same filesystem as the library, for atomic imports."""
    path = media_dir() / ".incomplete"
    path.mkdir(parents=True, exist_ok=True)
    return path


def spool_dir() -> Path:
    """Scratch space for spooling large API responses to disk.

    Deliberately not ``/tmp``: inside the Flatpak sandbox the filesystem is
    tmpfs-backed, so a 200 MB spool would consume RAM -- precisely what this
    app is trying to avoid. The XDG cache directory is real disk in both the
    venv and the sandbox.
    """
    path = cache_dir() / "spool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "catalog.db"


def _runtime_prefs_path() -> Path:
    return config_dir() / "runtime.json"


def load_runtime_prefs() -> dict:
    """Small JSON blob for things needed before the database can be opened."""
    try:
        with open(_runtime_prefs_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_runtime_prefs(prefs: dict) -> None:
    try:
        with open(_runtime_prefs_path(), "w", encoding="utf-8") as handle:
            json.dump(prefs, handle, indent=2)
    except OSError:
        # Preferences are a convenience; never let them break startup.
        pass


def port_is_available(port: int, host: str = BIND_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Without SO_REUSEADDR a socket left in TIME_WAIT from a recent run
        # would look occupied and push us onto a different port every restart.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(preferred: int = DEFAULT_PORT, host: str = BIND_HOST) -> int:
    """Return the first free port at or after ``preferred``.

    Reuses the previously chosen port when it is still free, so restarts keep
    a stable URL for bookmarks.
    """
    # Order matters: the caller's explicit preference wins, and the remembered
    # port is only a fallback. Checking the remembered port first would make
    # `--port 9000` silently land back on a previously used port instead.
    candidates = [preferred]
    remembered = load_runtime_prefs().get("last_port")
    if isinstance(remembered, int) and remembered > 0 and remembered != preferred:
        candidates.append(remembered)
    candidates.extend(range(preferred + 1, preferred + PORT_PROBE_RANGE))

    for port in candidates:
        if port_is_available(port, host):
            return port

    raise RuntimeError(
        f"No free TCP port found in {preferred}-{preferred + PORT_PROBE_RANGE}. "
        "Something is occupying the whole range."
    )


def remember_port(port: int) -> None:
    prefs = load_runtime_prefs()
    prefs["last_port"] = port
    save_runtime_prefs(prefs)
