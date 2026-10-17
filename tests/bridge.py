#!/usr/bin/env python3
"""Bridge tests: the contract between the app and scripts/player-bridge.sh.

The bridge is the only place where a line written by the app turns into a
process on the host, and it had no test at all. These drive the real script
against a temporary queue directory and a fake player that records the argv it
was handed, so what is asserted is the actual protocol rather than a description
of it.

Nothing here touches the real queue directory or launches a real player: a fake
`flatpak` that reports nothing installed keeps detection pinned to the fake mpv.

Run with:  .venv/bin/python tests/bridge.py
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "scripts" / "player-bridge.sh"

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


class Bridge:
    """The real script, running against a throwaway queue directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.queue_dir = root / "queue"
        self.queue_dir.mkdir()
        self.queue = self.queue_dir / "queue"
        self.heartbeat = self.queue_dir / "play-bridge.alive"
        self.players_file = self.queue_dir / "players.json"
        self.record = root / "played.log"
        self.log = root / "bridge.log"

        # A fake player, and a fake flatpak that claims nothing is installed.
        self.bin = root / "bin"
        self.bin.mkdir()
        self.player = self.bin / "mpv"
        self.player.write_text(
            "#!/bin/sh\n"
            "{ for arg in \"$@\"; do printf '%s\\n' \"$arg\"; done; "
            "printf -- '--end--\\n'; } >> \"$FAKE_PLAYER_LOG\"\n"
        )
        self.player.chmod(0o755)
        (self.bin / "flatpak").write_text("#!/bin/sh\nexit 1\n")
        (self.bin / "flatpak").chmod(0o755)

        self.env = {
            **os.environ,
            # The fake bin comes first; /usr/bin is still needed for python3,
            # tail, setsid and friends.
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "IPTV_CHARTS_PLAY_QUEUE_DIR": str(self.queue_dir),
            "FAKE_PLAYER_LOG": str(self.record),
        }
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.log_handle = self.log.open("w")
        self.proc = subprocess.Popen(
            [str(BRIDGE), "--foreground"],
            env=self.env,
            stdout=self.log_handle,
            stderr=self.log_handle,
            start_new_session=True,
        )

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log_handle.close()

    def wait_ready(self, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.heartbeat.exists() and self.players_file.exists():
                return True
            if self.proc and self.proc.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def send(self, **entry) -> None:
        with self.queue.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def records(self) -> list[list[str]]:
        """Every argv the fake player has been run with."""
        if not self.record.exists():
            return []
        out, current = [], []
        for line in self.record.read_text().splitlines():
            if line == "--end--":
                out.append(current)
                current = []
            else:
                current.append(line)
        return out

    def wait_for_records(self, count: int, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.records()) >= count:
                return True
            time.sleep(0.05)
        return False

    def capabilities(self) -> dict:
        return json.loads(self.players_file.read_text())

    def bridge_log(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


def main() -> int:
    if not shutil.which("bash"):
        print("bash is required for the bridge tests")
        return 1

    with tempfile.TemporaryDirectory(prefix="iptv-bridge-") as tmp:
        bridge = Bridge(Path(tmp))
        bridge.start()
        try:
            # ---------------------------------------------------- capabilities
            section("capabilities published for the app")
            check_true("heartbeat and players.json appear", bridge.wait_ready())

            caps = bridge.capabilities()
            check("players.json version", caps.get("version"), 1)
            ids = [entry["id"] for entry in caps.get("players", [])]
            check("only the installed player is listed", ids, ["native-mpv"])
            check("command is absolute, not a PATH lookup",
                  caps["players"][0]["command"][0], str(bridge.player))
            check("dialect travels with it", caps["players"][0]["flavour"], "mpv")
            check("extra args travel with it",
                  caps["players"][0]["extra_args"], ["--force-window=yes", "--idle=no"])

            # ------------------------------------------------------- argv path
            section("a request carrying its own command")
            bridge.send(
                url="http://example.com/live.ts",
                title="Some Channel",
                user_agent="UA/1.0",
                referrer="http://portal/",
                player="native-mpv",
                command=[str(bridge.player), "--title=Some Channel", "--",
                         "http://example.com/live.ts"],
            )
            check_true("it ran", bridge.wait_for_records(1))
            check("arguments arrive intact and in order", bridge.records()[0], [
                "--title=Some Channel", "--", "http://example.com/live.ts",
            ])

            # An argument containing spaces, quotes and a shell metacharacter
            # must arrive as one argument: nothing is passed through a shell.
            bridge.send(url="http://example.com/x.ts", command=[
                str(bridge.player), "--title=$(touch /tmp/pwned); rm -rf", "http://example.com/x.ts",
            ])
            check_true("a hostile argument still runs", bridge.wait_for_records(2))
            check("and arrives verbatim as one argument",
                  bridge.records()[1][0], "--title=$(touch /tmp/pwned); rm -rf")
            check_true("nothing was executed by a shell", not Path("/tmp/pwned").exists())

            # ----------------------------------------------------- legacy path
            section("a request from an app that sends no command")
            bridge.send(
                url="http://example.com/legacy.ts",
                title="Legacy",
                user_agent="UA/2.0",
                referrer="http://portal/",
            )
            check_true("it ran", bridge.wait_for_records(3))
            check("the default player's own flags were used", bridge.records()[2], [
                "--force-window=yes", "--idle=no",
                "--title=Legacy", "--user-agent=UA/2.0", "--referrer=http://portal/",
                "--", "http://example.com/legacy.ts",
            ])

            # ------------------------------------------------------ refusals
            section("refusals")
            bridge.send(url="--script=/tmp/evil.lua", title="not a url")
            bridge.send(command=["-f", "http://example.com/x.ts"])
            bridge.send(command=[])
            bridge.send(command=[str(bridge.player)] + ["x" * 9000])
            bridge.send(url="http://example.com/" + "x" * 9000, title="oversized")
            # A sentinel: the queue is handled in order, so once this has run,
            # everything above it has been dealt with.
            bridge.send(url="http://example.com/sentinel.ts", command=[
                str(bridge.player), "sentinel",
            ])
            check_true("the valid request after them ran", bridge.wait_for_records(4))
            check("nothing was launched for the refused ones",
                  [record for record in bridge.records()[3:] if record != ["sentinel"]], [])
            log = bridge.bridge_log()
            check_true("the refused address is reported",
                       "not a URL" in log)
            check_true("a leading-dash program name is reported",
                       "starts with '-'" in log)
            check_true("an oversized request is reported", "oversized" in log)
        finally:
            bridge.stop()

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = [(label, detail) for ok, label, detail in _results if not ok]

    print(f"\n{'=' * 60}")
    if failed:
        print(f"\033[31m{len(failed)} FAILED\033[0m, {passed} passed\n")
        for label, detail in failed:
            print(f"  \033[31m✗\033[0m {label}\n      {detail}")
        return 1

    print(f"\033[32mall {passed} checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
