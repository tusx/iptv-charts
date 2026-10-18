"""External media player detection, configuration and launching.

Two execution modes, chosen by whether ``IPTV_CHARTS_PLAY_QUEUE`` is set:

**Direct (no queue configured).** We run on the host, so we spawn the player
ourselves. Note that mpv and VLC are commonly installed as *Flatpaks* on modern
distros -- they are on this development machine -- which means they are absent
from ``PATH``. A bare ``subprocess.Popen(["mpv", ...])`` would therefore fail
even though mpv is plainly installed, so detection probes Flatpak application
IDs as well.

**Bridged (queue configured).** We are in a container, which has no way to start
a process on the host. Instead we append the finished argv to a file shared with
``scripts/player-bridge.sh``, a small host-side watcher that runs it. Because
the container cannot see the host's software, the bridge also publishes what it
found; that file is the only honest answer to "what can play this" in bridged
mode, and our own ``shutil.which`` calls are meaningless there.

Which player gets used is a user setting, applied in :func:`resolve`; everything
usable is in :func:`available`. A command of the user's own is offered as an
extra candidate, which is what makes the feature work on a host with none of the
known players installed.

Nothing here is required for the app to work: the browser plays what it can, and
the external-player buttons hide themselves when there is nothing to launch.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db

_log = logging.getLogger("iptv_charts.player")

#: How long a player gets to die before we call it started. Long enough to
#: cover a provider that answers late: mpv's HTTP failure here arrived at
#: 1.3s, just after a shorter grace had already reported success.
START_GRACE = 2.0

#: How long the app waits for the host bridge to report on a request. The
#: bridge writes the result after START_GRACE, so this is grace plus slack.
RESULT_TIMEOUT = 4.0

#: Matches a URL inside a player's error output, so it can be redacted before
#: it reaches the app log or the UI: Xtream URLs carry the provider username
#: and password in their path.
_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"']+")

# Flatpak application IDs, most preferred first.
MPV_FLATPAK = "io.mpv.Mpv"
VLC_FLATPAK = "org.videolan.VLC"

#: The bridge rewrites this file every few seconds. If it goes stale we assume
#: the bridge is gone and hide the external-player buttons.
BRIDGE_HEARTBEAT = "play-bridge.alive"
BRIDGE_STALE_AFTER = 30.0

#: What the bridge publishes about the host it runs on.
PLAYERS_FILE = "players.json"
PLAYERS_FILE_VERSION = 1

#: What the bridge reports about the last request it handled. Without it the
#: app can only say "sent", which is what a click that never opened a player
#: used to look like.
RESULT_FILE = "play-result.json"

#: The player a bridge older than ``players.json`` stands for: it can play, it
#: just cannot say with what. Kept so upgrading the app never loses the button.
BRIDGE_PLAYER_ID = "host-bridge"

#: Ceiling on a stored custom command. Long enough for a real command line with
#: a wrapper, short enough that nothing pathological reaches the queue.
COMMAND_MAX_LENGTH = 500

#: Schemes a stream may use. Checked because the URL is handed to the player as
#: an argument, where a leading ``-`` is read as an option rather than as a
#: location -- see :func:`_checked_url`.
STREAM_SCHEMES = frozenset({
    "http", "https", "rtsp", "rtsps", "rtmp", "rtmps", "udp", "rtp", "srt",
    "mms", "mmsh",
})

#: Settings keys this module owns.
PREFERENCE_KEY = "preferred_player"
COMMAND_KEY = "player_command"

#: Every id a stored preference may name. The settings page only offers what
#: `available()` actually returned, but a save is accepted regardless -- a
#: choice made while the bridge happens to be down is still a valid choice, and
#: rejecting it would be a puzzle with no explanation.
KNOWN_PLAYER_IDS = frozenset({
    "auto", "custom", "host-bridge",
    "flatpak-mpv", "native-mpv", "flatpak-vlc", "native-vlc",
})

_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://")
_PLACEHOLDER = re.compile(r"\{(url|title|user_agent|referrer)\}")

#: Probing shells out to ``flatpak info``, far too slow to repeat per request.
_CANDIDATES: list["Player"] | None = None
_CANDIDATES_AT = 0.0
_CANDIDATES_TTL = 60.0

_queue_lock = threading.Lock()


class PlayerError(RuntimeError):
    """Playback could not be started. Callers report this to the user."""


@dataclass(frozen=True)
class Player:
    """A way to play a stream, described as an argv prefix and a flag dialect."""

    id: str
    label: str
    #: Flag dialect: 'mpv', 'vlc', 'custom' for a user-supplied template, or
    #: 'bridge' for a host bridge that decides for itself.
    flavour: str = "mpv"
    #: For 'custom' this is the template, placeholders intact.
    argv_prefix: tuple[str, ...] = field(default_factory=tuple)
    #: Extra args this player wants before the URL.
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    #: True when playback happens somewhere other than this process.
    via_bridge: bool = False

    def command_for(self, url: str, *, user_agent: str | None = None,
                    referrer: str | None = None, title: str | None = None) -> list[str]:
        """Build a complete argv for playing ``url``.

        Some providers reject the default User-Agent, and M3U playlists
        frequently carry ``#EXTVLCOPT:http-user-agent`` / ``http-referrer``
        hints. mpv ignores those directives entirely, so the caller has to
        translate them into the flags of whichever player this is -- VLC spells
        all three differently, and getting that wrong makes it exit on startup.
        """
        url = _checked_url(url)

        if self.flavour == "bridge":
            # There is no command: the bridge chooses the player itself. Only
            # an older bridge ends up here, and it gets a bare request.
            raise PlayerError(
                "The host bridge chooses the player; there is no command to show."
            )

        if self.flavour == "custom":
            return expand_command(
                self.argv_prefix, url=url, title=title or "",
                user_agent=user_agent or "", referrer=referrer or "",
            )

        argv = [*self.argv_prefix, *self.extra_args]

        if self.flavour == "vlc":
            if title:
                argv.append(f"--meta-title={title}")
            if user_agent:
                argv.append(f"--http-user-agent={user_agent}")
            if referrer:
                argv.append(f"--http-referrer={referrer}")
            argv.append(url)
            return argv

        if title:
            argv.append(f"--title={title}")
        if user_agent:
            argv.append(f"--user-agent={user_agent}")
        if referrer:
            argv.append(f"--referrer={referrer}")
        # End of options. mpv parses anything starting with '-' as a flag, so
        # without this a stored URL that begins with one is executed as an
        # option; see _checked_url for why that is not hypothetical.
        argv.append("--")
        argv.append(url)
        return argv


def is_container() -> bool:
    """True when running inside a container.

    Podman writes ``/run/.containerenv``; Docker writes ``/.dockerenv``.
    Reported by the health endpoint purely for diagnostics -- no behaviour
    depends on it.
    """
    return Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()


# ------------------------------------------------------------------- logging
def redact_url(url: str) -> str:
    """A URL safe to put in a log line: scheme, host and last path segment.

    Provider URLs carry the account name and password as path segments, and
    logs get pasted into bug reports, so the middle of the path is dropped.
    """
    scheme_end = url.find("://")
    if scheme_end < 0:
        return url
    scheme, rest = url[:scheme_end], url[scheme_end + 3:]
    host, _, path = rest.partition("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    return f"{scheme}://{host}/…/{tail}" if tail else f"{scheme}://{host}"


def _redact_text(text: str) -> str:
    """Strip anything URL-shaped out of a player's error output."""
    return _URL_IN_TEXT.sub("<url>", text)


def _tail(text: str, lines: int = 3) -> str:
    """The last few non-empty lines, joined -- enough to say why a player died."""
    kept = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " ".join(kept[-lines:])


# ----------------------------------------------------------------- validation
def _checked_url(url: str) -> str:
    """Return ``url`` if it is a stream URL, else raise.

    This is a security boundary rather than tidiness. The URL is the last thing
    handed to the player, and it is not always ours: an M3U item's stream URL is
    whatever the playlist said. A "URL" of ``--script=/somewhere/x.lua`` would
    be read as an option by mpv and turn a playlist into code execution on this
    machine, which matters because the container can write to the queue the
    host bridge reads. Requiring a scheme we can actually play rules that out,
    along with ``file://`` and friends.
    """
    if not isinstance(url, str) or not url:
        raise PlayerError("This item has no stream URL.")
    scheme = _SCHEME.match(url)
    if scheme is None:
        raise PlayerError(f"That stream address is not a URL: {url[:60]!r}")
    if scheme.group("scheme").lower() not in STREAM_SCHEMES:
        raise PlayerError(
            f"Refusing to play a {scheme.group('scheme')} address."
        )
    return url


def parse_command(text: str) -> tuple[str, ...]:
    """Parse a user's command template into argv tokens.

    Raises ValueError with something worth showing the user. The result is
    never passed through a shell -- it is exec'd as a list -- so quoting only
    decides where one argument ends and the next begins.
    """
    if not text or not text.strip():
        raise ValueError("The command is empty.")
    if len(text) > COMMAND_MAX_LENGTH:
        raise ValueError(
            f"The command is longer than {COMMAND_MAX_LENGTH} characters."
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("The command contains a control character.")
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"That command cannot be parsed: {exc}") from exc
    if not argv:
        raise ValueError("The command is empty.")
    if _PLACEHOLDER.search(argv[0]) or "{" in argv[0] or "}" in argv[0]:
        raise ValueError(
            "The first word is the program to run, so it cannot contain a "
            "placeholder."
        )
    return tuple(argv)


def expand_command(template: tuple[str, ...], *, url: str, title: str = "",
                   user_agent: str = "", referrer: str = "") -> list[str]:
    """Expand a command template into a ready-to-run argv.

    Placeholders are replaced in a single pass, so a value that itself looks
    like a placeholder (a title of ``{url}``) is never substituted again. A
    token whose placeholder has no value is dropped whole: ``--user-agent=``
    with nothing after it is worse than no flag at all.

    The first token is copied through untouched -- it is the program name, and
    `parse_command` rejects placeholders in it -- so the executable can never be
    dropped or rewritten by data.
    """
    values = {"url": url, "title": title, "user_agent": user_agent, "referrer": referrer}
    argv = [template[0]]

    for token in template[1:]:
        names = _PLACEHOLDER.findall(token)
        if names and any(not values[name] for name in names):
            continue
        argv.append(_PLACEHOLDER.sub(lambda match: values[match.group(1)], token))

    # A command that never mentions {url} almost certainly wants it appended.
    if not any("{url}" in token for token in template[1:]):
        argv.append(url)
    return argv


# --------------------------------------------------------------------- bridge
def _queue_file(name: str) -> Path | None:
    """A file in the shared queue directory, or None when not bridged."""
    queue = config.play_queue_path()
    if queue is None:
        return None
    return queue.parent / name


def bridge_is_alive() -> bool:
    """True when a bridge has touched its heartbeat recently."""
    heartbeat = _queue_file(BRIDGE_HEARTBEAT)
    if heartbeat is None:
        return False
    try:
        return (time.time() - heartbeat.stat().st_mtime) < BRIDGE_STALE_AFTER
    except OSError:
        return False


def _enqueue(argv: list[str] | None, *, url: str, title: str | None,
             user_agent: str | None, referrer: str | None,
             player_id: str) -> float:
    """Append a playback request for the host-side bridge to pick up.

    The four legacy fields are always written: they cost nothing and they are
    what an older bridge, which knows nothing about ``command``, plays instead
    of dropping the request on the floor. ``argv`` is None when the bridge is
    the one choosing the player, which is what every bridge did before the
    field existed.
    """
    queue = config.play_queue_path()
    if queue is None:
        raise PlayerError("No play queue is configured.")

    queue.parent.mkdir(parents=True, exist_ok=True)

    requested_at = time.time()
    request = {
        "url": url,
        "title": title,
        "user_agent": user_agent,
        "referrer": referrer,
        "requested_at": requested_at,
        "player": player_id,
        "command": list(argv) if argv is not None else None,
    }
    line = json.dumps(request, ensure_ascii=False) + "\n"

    # O_APPEND writes below PIPE_BUF are atomic on Linux; longer lines are
    # serialised by this lock, which is what actually protects the several
    # waitress threads writing here.
    with _queue_lock:
        with open(queue, "a", encoding="utf-8") as handle:
            handle.write(line)
    return requested_at


def _await_play_result(requested_at: float, *, timeout: float = RESULT_TIMEOUT) -> dict | None:
    """Wait for the bridge to say how the request it was given went.

    The bridge is a separate process on the host; this is the only channel back.
    A bridge older than the result file simply never answers, which the caller
    treats as "sent, outcome unknown" rather than as a failure.
    """
    path = _queue_file(RESULT_FILE)
    if path is None:
        return None

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = None
        if isinstance(result, dict):
            try:
                matches = abs(float(result.get("requested_at", -1)) - requested_at) < 0.001
            except (TypeError, ValueError):
                matches = False
            if matches:
                return result
        time.sleep(0.1)
    return None


# ------------------------------------------------------------------ detection
def _flatpak_app_installed(app_id: str) -> bool:
    """True when flatpak knows about ``app_id``.

    Needed because mpv/VLC are frequently installed as Flatpaks and therefore
    absent from PATH; `which mpv` alone gives a false negative.
    """
    if not shutil.which("flatpak"):
        return False
    try:
        result = subprocess.run(
            ["flatpak", "info", app_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _local_candidates() -> list[Player]:
    """Players installed on this machine, in preference order."""
    found: list[Player] = []

    if _flatpak_app_installed(MPV_FLATPAK):
        found.append(Player(
            id="flatpak-mpv", label="mpv (Flatpak)", flavour="mpv",
            argv_prefix=("flatpak", "run", MPV_FLATPAK),
            extra_args=("--force-window=yes", "--idle=yes"),
        ))
    if native_mpv := shutil.which("mpv"):
        found.append(Player(
            id="native-mpv", label="mpv", flavour="mpv",
            argv_prefix=(native_mpv,),
            extra_args=("--force-window=yes", "--idle=yes"),
        ))
    if _flatpak_app_installed(VLC_FLATPAK):
        found.append(Player(
            id="flatpak-vlc", label="VLC (Flatpak)", flavour="vlc",
            argv_prefix=("flatpak", "run", VLC_FLATPAK),
            extra_args=("--play-and-exit",),
        ))
    if native_vlc := shutil.which("vlc"):
        found.append(Player(
            id="native-vlc", label="VLC", flavour="vlc",
            argv_prefix=(native_vlc,),
            extra_args=("--play-and-exit",),
        ))
    return found


def _host_candidates() -> list[Player] | None:
    """What the host bridge said it can run, or None when it cannot say.

    Only meaningful in bridged mode. Every field is treated as untrusted: this
    file is written by another process, so a malformed or truncated one has to
    degrade rather than reach a page render as an exception.

    None and [] mean different things and must not be conflated. None is "no
    usable report" -- a bridge older than this file, or one that has not
    written it yet -- and the caller keeps the plain bridge player for it. []
    is a current bridge saying it found nothing installed, which must hide the
    button rather than offer one that cannot work.
    """
    path = _queue_file(PLAYERS_FILE)
    if path is None:
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != PLAYERS_FILE_VERSION:
        return None

    found: list[Player] = []
    for entry in raw.get("players") or []:
        if not isinstance(entry, dict):
            continue
        player_id = entry.get("id")
        label = entry.get("label")
        command = entry.get("command")
        flavour = entry.get("flavour", "mpv")
        if not isinstance(player_id, str) or not isinstance(label, str):
            continue
        if not isinstance(command, list) or not command:
            continue
        if not all(isinstance(part, str) for part in command):
            continue
        if flavour not in ("mpv", "vlc", "custom"):
            flavour = "mpv"
        extra = entry.get("extra_args")
        extra_args = (tuple(part for part in extra if isinstance(part, str))
                      if isinstance(extra, list) else ())
        found.append(Player(
            id=player_id, label=label, flavour=flavour,
            argv_prefix=tuple(command), extra_args=extra_args, via_bridge=True,
        ))
    return found


def _candidates(*, refresh: bool = False) -> list[Player]:
    """Cached candidate list.

    Liveness is deliberately outside the cache: it is a single ``stat``, and
    caching it is what makes a bridge that just died keep offering a button.
    """
    global _CANDIDATES, _CANDIDATES_AT

    bridged = config.play_queue_path() is not None
    if bridged and not bridge_is_alive():
        return []

    now = time.monotonic()
    if _CANDIDATES is None or refresh or (now - _CANDIDATES_AT) > _CANDIDATES_TTL:
        candidates = _host_candidates() if bridged else _local_candidates()
        if candidates is None:
            # An older bridge: it will play the request with the player it
            # found at startup, it just cannot name it. Keeping one button is
            # the difference between "still works" and "the button vanished
            # when the app was upgraded".
            candidates = [Player(
                id=BRIDGE_PLAYER_ID,
                label="External player (host)",
                flavour="bridge",
                via_bridge=True,
            )]
        _CANDIDATES = candidates
        _CANDIDATES_AT = now
    return _CANDIDATES


def _setting(conn, key: str, default: str = "") -> str:
    """A settings read that cannot fail.

    These values reach every page render through the template context, so a
    missing table or an unreadable row must not be able to 500 the app -- that
    would take out the settings page needed to fix it.
    """
    if conn is None:
        return default
    try:
        return db.get_setting(conn, key, default) or default
    except Exception:  # noqa: BLE001 - see the docstring
        return default


def custom_player(conn, *, bridged: bool | None = None) -> Player | None:
    """The user's own command as a player, when it parses."""
    text = _setting(conn, COMMAND_KEY, "")
    if not text.strip():
        return None
    try:
        template = parse_command(text)
    except ValueError:
        # Stored before it was validated, or hand-edited in the database. The
        # settings page shows the problem; nothing else should break over it.
        return None
    if bridged is None:
        bridged = config.play_queue_path() is not None
    return Player(
        id="custom", label="Custom command", flavour="custom",
        argv_prefix=template, via_bridge=bridged,
    )


def available(conn, *, refresh: bool = False) -> list[Player]:
    """Every player that could be used, in preference order.

    Detected players come first and the custom command last, so "auto" keeps
    meaning "the obvious one on this machine" until the user says otherwise.
    """
    players = list(_candidates(refresh=refresh))
    custom = custom_player(conn)
    if custom is not None:
        players.append(custom)
    return players


def resolve(conn, *, refresh: bool = False) -> Player | None:
    """The player to use, honouring the user's preference.

    Never raises: it is called while rendering every page, so anything
    unreadable or malformed has to degrade to a sane default instead.
    """
    players = available(conn, refresh=refresh)
    if not players:
        return None

    preferred = _setting(conn, PREFERENCE_KEY, "auto")
    if preferred and preferred != "auto":
        for candidate in players:
            if candidate.id == preferred:
                return candidate
        # Configured but gone -- uninstalled, or the host stopped offering it.
        # Falling through to the first candidate beats a button that does
        # nothing; the settings page shows what is actually available.
    return players[0]


def _wait_for_exit(process: subprocess.Popen, seconds: float) -> int | None:
    """The exit status if the process died within ``seconds``, else None."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = process.poll()
        if status is not None:
            return status
        time.sleep(0.05)
    return None


# -------------------------------------------------------------------- launch
def launch(url: str, *, user_agent: str | None = None, referrer: str | None = None,
           title: str | None = None, player: Player) -> Player:
    """Start playback of ``url`` in ``player``.

    ``player`` is required on purpose: a default that detects for itself is how
    the UI ends up naming one player and launching another. Callers get theirs
    from :func:`resolve`.

    Detachment note, learned the hard way: ``start_new_session=True`` (setsid)
    is what lets the player outlive this process. Without it the player
    inherits our session and can be killed when the app exits. We deliberately
    do not pass ``--no-terminal``, because that makes mpv skip installing its
    SIGTERM handler, which would make a later graceful stop impossible; stdio
    is redirected to DEVNULL here, which achieves the same quieting without
    the side effect.
    """
    if player is None:
        raise PlayerError(
            "No external media player is configured. Choose one in Settings, or "
            "start the player bridge if you are running in a container."
        )

    url = _checked_url(url)

    if player.via_bridge:
        # A bridge that cannot report its players runs the request itself, with
        # the player it chose at startup -- that is the only kind of bridge
        # that produces a 'bridge' flavour, and it must not be sent an argv it
        # would refuse to run.
        old_bridge = player.flavour == "bridge"
        argv = (None if old_bridge
                else player.command_for(url, user_agent=user_agent,
                                        referrer=referrer, title=title))
        requested_at = _enqueue(argv, url=url, title=title,
                                user_agent=user_agent, referrer=referrer,
                                player_id=player.id)
        if old_bridge:
            # Nothing to wait for: a bridge that predates the result file is
            # silent about outcomes, and pretending otherwise just adds delay.
            return player

        result = _await_play_result(requested_at)
        if result is None:
            _log.warning("bridge reported nothing for %s; assuming it started",
                         redact_url(url))
            return player
        if not result.get("ok"):
            message = str(result.get("message") or "").strip()
            raise PlayerError(
                f"{player.label} could not play that stream"
                + (f": {message}" if message else "")
            )
        return player

    argv = player.command_for(url, user_agent=user_agent, referrer=referrer, title=title)
    _log.debug("starting %s: %s", player.label, " ".join(
        redact_url(part) if part == url else part for part in argv
    ))

    # The player's own output is what explains a failure -- "HTTP error 500",
    # "cannot open display", a flag it did not understand. Discarding it (as
    # /dev/null did) turns every one of those into "nothing happened". mpv in
    # particular writes its diagnostics to stdout, so both streams are kept.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as error_output:
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=error_output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PlayerError(f"Could not start {player.label}: {exc}") from exc

        status = _wait_for_exit(process, START_GRACE)
        if status is None:
            return player

        error_output.seek(0)
        message = _redact_text(_tail(error_output.read()))
    raise PlayerError(
        f"{player.label} exited immediately (status {status})"
        + (f": {message}" if message else ". Check the command on the settings page.")
    )


__all__ = [
    "BRIDGE_HEARTBEAT",
    "BRIDGE_PLAYER_ID",
    "BRIDGE_STALE_AFTER",
    "COMMAND_KEY",
    "COMMAND_MAX_LENGTH",
    "KNOWN_PLAYER_IDS",
    "PLAYERS_FILE",
    "PLAYERS_FILE_VERSION",
    "PREFERENCE_KEY",
    "RESULT_FILE",
    "START_GRACE",
    "Player",
    "PlayerError",
    "available",
    "bridge_is_alive",
    "custom_player",
    "expand_command",
    "is_container",
    "launch",
    "parse_command",
    "redact_url",
    "resolve",
]
