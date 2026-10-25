#!/usr/bin/env python3
"""Deploy-script tests for 0.8.0.

Exercises build.deploy.sh and deploy.dev.build.sh without touching a real
container runtime. Each scenario gets a temporary project root, a fake
``podman``/``docker`` that records its argv and remembers container/volume
state, plus fake ``ss``, ``curl``, ``xdg-open`` and a fake player bridge.

The fake runtime is the only thing that "builds" or "runs" anything: no image
is built, no container starts, and no real volume is created. The tests assert
runtime selection, media folder adoption, generated mounts/env, restart and
port policy, dev/production separation, and that destructive commands only
ever name the container/volumes they are supposed to.

Deliberately dependency-free (no pytest). Run with:

    .venv/bin/python tests/deploy_scripts.py
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {
    "build": ROOT / "build.deploy.sh",
    "dev": ROOT / "deploy.dev.build.sh",
}

#: Utilities the deploy scripts call. The restricted PATH is built from
#: symlinks to exactly these, so a real podman/docker in /usr/bin can never be
#: picked up by accident (this machine has both installed).
SYSTEM_UTILS = (
    "find", "grep", "sed", "seq", "cat", "cut", "du", "mkdir", "chmod",
    "sleep", "dirname", "bash",
)

_results: list[tuple[bool, str, str]] = []


def check(label: str, got, want) -> None:
    ok = got == want
    _results.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


def check_not_in(label: str, needle: str, haystack) -> None:
    _results.append((needle not in haystack, label,
                     f"{needle!r} absent" if needle not in haystack
                     else f"{needle!r} unexpectedly present"))


def check_in(label: str, needle: str, haystack) -> None:
    check_true(label, needle in haystack)


# --------------------------------------------------------------------- fakes
FAKE_RUNTIME = r'''#!{python}
import json, os, sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
log = os.environ.get("FAKE_RUNTIME_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"runtime": name, "argv": args}}) + "\n")

state = Path(os.environ["FAKE_STATE_DIR"])
containers = state / "containers"
volumes = state / "volumes"
containers.mkdir(parents=True, exist_ok=True)
volumes.mkdir(parents=True, exist_ok=True)


def present(base, item):
    return (base / item).is_file()


def mark(base, item):
    (base / item).write_text("1", encoding="utf-8")


def unmark(base, item):
    try:
        (base / item).unlink()
    except FileNotFoundError:
        pass


if args[:2] == ["container", "inspect"]:
    sys.exit(0 if len(args) > 2 and present(containers, args[2]) else 1)
if args[:2] == ["container", "exists"]:
    sys.exit(0 if len(args) > 2 and present(containers, args[2]) else 1)
if args[:1] == ["inspect"]:
    target = args[-1] if args else ""
    found = present(containers, target)
    if "-f" in args:
        print("true" if found else "false")
    sys.exit(0 if found else 1)
if args[:2] == ["volume", "inspect"]:
    sys.exit(0 if len(args) > 2 and present(volumes, args[2]) else 1)
if args[:2] == ["volume", "create"]:
    mark(volumes, args[2])
    sys.exit(0)
if args[:2] == ["volume", "rm"]:
    unmark(volumes, args[2])
    sys.exit(0)
if args[:1] == ["run"]:
    if "--name" in args:
        mark(containers, args[args.index("--name") + 1])
    sys.exit(0)
if args[:1] == ["rm"]:
    if len(args) >= 2 and args[-1] != "-f":
        unmark(containers, args[-1])
    sys.exit(0)
sys.exit(0)
'''

FAKE_SS = r'''#!/bin/bash
if [[ -n "${FAKE_SS_BUSY:-}" ]]; then
    printf 'LISTEN 0 128 127.0.0.1:%s 0.0.0.0:*\n' "$FAKE_SS_BUSY"
fi
exit 0
'''

FAKE_CURL = r'''#!/bin/bash
if [[ -n "${FAKE_CURL_FAIL:-}" ]]; then
    exit 22
fi
printf '%s\n' '{"alive":true,"status":"ok","worker":{"alive":true}}'
exit 0
'''

FAKE_XDG_OPEN = r'''#!/bin/bash
printf '%s\n' "$*" >> "${FAKE_XDG_OPEN_LOG:?}"
exit 0
'''

FAKE_BRIDGE = r'''#!/bin/bash
printf '%s\n' "$*" >> "${FAKE_BRIDGE_LOG:?}"
if [[ "${1:-}" == "--status" ]]; then
    exit "${FAKE_BRIDGE_STATUS:-0}"
fi
exit 0
'''


class Sandbox:
    """One isolated deploy scenario: temp HOME, media, project and fakes."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="iptv-deploy-test-"))
        self.fakebin = self.tmp / "fakebin"
        self.systembin = self.tmp / "systembin"
        self.state = self.tmp / "state"
        self.containers = self.state / "containers"
        self.volumes = self.state / "volumes"
        self.home = self.tmp / "home"
        self.queue = self.tmp / "queue"
        self.project = self.tmp / "project"
        for path in (self.fakebin, self.systembin, self.containers,
                     self.volumes, self.home, self.project):
            path.mkdir(parents=True, exist_ok=True)

        for util in SYSTEM_UTILS:
            found = shutil.which(util)
            if found:
                os.symlink(found, self.systembin / util)

        self.runtime_log = self.tmp / "runtime.jsonl"
        self.bridge_log = self.tmp / "bridge.log"
        self.xdg_log = self.tmp / "xdg-open.log"

        # The deploy scripts only need Containerfile to exist; the fake
        # runtime never reads it.
        (self.project / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
        for key, source in SCRIPTS.items():
            destination = self.project / source.name
            shutil.copy2(source, destination)
            destination.chmod(0o755)

        self._write_fake("ss", FAKE_SS, 0o755)
        self._write_fake("curl", FAKE_CURL, 0o755)
        self._write_fake("xdg-open", FAKE_XDG_OPEN, 0o755)
        # Readiness loops would otherwise sleep 0.5s per failed probe.
        self._write_fake("sleep", "#!/bin/bash\nexit 0\n", 0o755)

    def _write_fake(self, name: str, body: str, mode: int) -> Path:
        path = self.fakebin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(mode)
        return path

    def install_runtime(self, *names: str) -> None:
        for name in names:
            self._write_fake(name, FAKE_RUNTIME.format(python=sys.executable), 0o755)

    def install_player_bridge(self, status: int = 0) -> None:
        scripts = self.project / "scripts"
        scripts.mkdir(exist_ok=True)
        bridge = scripts / "player-bridge.sh"
        bridge.write_text(FAKE_BRIDGE, encoding="utf-8")
        bridge.chmod(0o755)
        self.bridge_status = status

    def media_root(self, dirs=(), files=()) -> Path:
        media = self.tmp / "media"
        media.mkdir(exist_ok=True)
        for name in dirs:
            (media / name).mkdir(parents=True, exist_ok=True)
        for name in files:
            path = media / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep me\n", encoding="utf-8")
        return media

    def precreate_container(self, name: str) -> None:
        (self.containers / name).write_text("1", encoding="utf-8")

    def precreate_volume(self, name: str) -> None:
        (self.volumes / name).write_text("1", encoding="utf-8")

    def _env(self, extra: dict[str, str | None]) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("IPTV_", "FAKE_"))
               and key not in ("XDG_RUNTIME_DIR", "BASH_ENV", "ENV")}
        env["PATH"] = f"{self.fakebin}:{self.systembin}"
        env["HOME"] = str(self.home)
        env["IPTV_CHARTS_PLAY_QUEUE_DIR"] = str(self.queue)
        env["FAKE_RUNTIME_LOG"] = str(self.runtime_log)
        env["FAKE_STATE_DIR"] = str(self.state)
        env["FAKE_BRIDGE_LOG"] = str(self.bridge_log)
        env["FAKE_BRIDGE_STATUS"] = str(getattr(self, "bridge_status", 0))
        env["FAKE_XDG_OPEN_LOG"] = str(self.xdg_log)
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, script: str, args=(), extra=None, pty_inputs=None,
            timeout: int = 30):
        cmd = [shutil.which("bash") or "/bin/bash",
               str(self.project / SCRIPTS[script].name), *args]
        env = self._env(extra or {})

        if pty_inputs is not None:
            code, output = self._run_pty(cmd, env, pty_inputs, timeout)
            return subprocess.CompletedProcess(cmd, code, output, "")

        return subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )

    @staticmethod
    def _run_pty(cmd, env, inputs, timeout):
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            cmd, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True,
        )
        os.close(slave)
        try:
            for text in inputs:
                try:
                    os.write(master, text.encode())
                except OSError:
                    break
            output, _ = proc.communicate(timeout=timeout)
        finally:
            os.close(master)
        return proc.returncode, output or ""

    def records(self) -> list[dict]:
        if not self.runtime_log.is_file():
            return []
        return [json.loads(line) for line in
                self.runtime_log.read_text(encoding="utf-8").splitlines() if line]

    def run_commands(self) -> list[list[str]]:
        return [record["argv"] for record in self.records()
                if record["argv"][:1] == ["run"]]

    def build_commands(self) -> list[list[str]]:
        return [record["argv"] for record in self.records()
                if record["argv"][:1] == ["build"]]

    def bridge_calls(self) -> list[str]:
        if not self.bridge_log.is_file():
            return []
        return self.bridge_log.read_text(encoding="utf-8").splitlines()

    def xdg_calls(self) -> list[str]:
        if not self.xdg_log.is_file():
            return []
        return self.xdg_log.read_text(encoding="utf-8").splitlines()


# ----------------------------------------------------------------- argv utils
def after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def env_values(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv)
            if value == "-e" and index + 1 < len(argv)]


def volume_values(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, value in enumerate(argv)
            if value == "-v" and index + 1 < len(argv)]


def has_mount(argv: list[str], source: str, target: str) -> bool:
    prefix = f"{source}:{target}"
    return any(value == prefix or value.startswith(prefix + ":")
               for value in volume_values(argv))


def runtime_command(records: list[dict], *prefix: str) -> bool:
    return any(record["argv"][:len(prefix)] == list(prefix) for record in records)


# --------------------------------------------------------------------- tests
def test_static_guards() -> None:
    section("static guards")
    for key, path in SCRIPTS.items():
        check_true(f"{key} script exists", path.is_file())
        check_true(f"{key} script is executable", os.access(path, os.X_OK))
        text = path.read_text(encoding="utf-8")
        check_true(f"{key} uses strict shell mode",
                   bool(re.search(r"^set -euo pipefail$", text, re.MULTILINE)))
        check_not_in(f"{key} has no eval", "eval ", text)
        check_not_in(f"{key} has no unless-stopped", "unless-stopped", text)
        check_not_in(f"{key} never publishes a wildcard address", "0.0.0.0:",
                     text)
        check_in(f"{key} uses restart always", "--restart always", text)
        check_in(f"{key} supports IPTV_RUNTIME", "IPTV_RUNTIME", text)
        check_in(f"{key} supports --runtime", "--runtime", text)
        check_in(f"{key} knows podman", "podman", text)
        check_in(f"{key} knows docker", "docker", text)
        check_in(f"{key} honours IPTV_CHARTS_NO_OPEN", "IPTV_CHARTS_NO_OPEN",
                 text)
        check_in(f"{key} publishes on loopback", "127.0.0.1:${HOST_PORT}",
                 text)
        check_not_in(f"{key} does not reference setup.deploy.sh",
                     "setup.deploy.sh", text)

    build_text = SCRIPTS["build"].read_text(encoding="utf-8")
    dev_text = SCRIPTS["dev"].read_text(encoding="utf-8")
    check_in("build uses movie env var", "IPTV_CHARTS_MOVIE_DIR", build_text)
    check_in("build uses series env var", "IPTV_CHARTS_SERIES_DIR", build_text)
    check_in("dev uses movie env var", "IPTV_CHARTS_MOVIE_DIR", dev_text)
    check_in("dev uses series env var", "IPTV_CHARTS_SERIES_DIR", dev_text)

    sys.path.insert(0, str(ROOT))
    from iptv_charts import config  # noqa: E402  (path set above)

    check_in("build matches config movie env", config.MOVIE_DIR_ENV, build_text)
    check_in("build matches config series env", config.SERIES_DIR_ENV, build_text)
    for alias in config.MOVIE_FOLDER_ALIASES:
        check_in(f"build knows movie alias {alias!r}", alias, build_text)
    for alias in config.SERIES_FOLDER_ALIASES:
        check_in(f"build knows series alias {alias!r}", alias, build_text)
    check_in("build creates canonical Movies", "Movies movies movie",
             build_text)
    check_in("build creates canonical Series", "Series series tv",
             build_text)


def test_runtime_selection() -> None:
    section("runtime selection")

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    check("single podman exits 0", result.returncode, 0)
    check("single podman is used", sandbox.records()[0]["runtime"], "podman")
    check_in("chosen runtime is printed", "container runtime: podman",
             result.stdout)

    sandbox = Sandbox()
    sandbox.install_runtime("docker")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    check("single docker exits 0", result.returncode, 0)
    check("single docker is used", sandbox.records()[0]["runtime"], "docker")
    build_argv = sandbox.build_commands()[0]
    check_not_in("docker build has no podman --format", "--format", build_argv)

    sandbox = Sandbox()
    sandbox.install_runtime("podman", "docker")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    result = sandbox.run(
        "build", extra={"IPTV_MEDIA_DIR": str(media), "IPTV_RUNTIME": "docker"})
    check("IPTV_RUNTIME=docker exits 0", result.returncode, 0)
    check("IPTV_RUNTIME overrides the prompt", sandbox.records()[0]["runtime"],
          "docker")
    check_not_in("IPTV_RUNTIME does not prompt", "both podman and docker",
                 result.stdout)

    sandbox = Sandbox()
    sandbox.install_runtime("podman", "docker")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    result = sandbox.run(
        "build", args=("--runtime", "podman"),
        extra={"IPTV_MEDIA_DIR": str(media)})
    check("--runtime podman exits 0", result.returncode, 0)
    check("--runtime overrides the prompt", sandbox.records()[0]["runtime"],
          "podman")

    sandbox = Sandbox()
    sandbox.install_runtime("podman", "docker")
    media = sandbox.media_root()
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    check_true("both runtimes without a terminal fails", result.returncode != 0)
    check_in("noninteractive failure explains the choice",
             "both podman and docker", result.stdout + result.stderr)
    check("nothing was built", sandbox.records(), [])

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    media = sandbox.media_root()
    result = sandbox.run(
        "build", extra={"IPTV_MEDIA_DIR": str(media),
                        "IPTV_RUNTIME": "containerd"})
    check_true("unknown runtime is rejected", result.returncode != 0)
    check_in("unknown runtime names the value", "containerd",
             result.stdout + result.stderr)
    check("unknown runtime runs nothing", sandbox.records(), [])

    # The prompt itself needs a terminal, so drive the script through a pty.
    sandbox = Sandbox()
    sandbox.install_runtime("podman", "docker")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    result = sandbox.run(
        "build", extra={"IPTV_MEDIA_DIR": str(media)}, pty_inputs=["2\n"])
    check("interactive choice exits 0", result.returncode, 0)
    check_in("interactive choice prompts", "Container runtime [1]",
             result.stdout)
    check("interactive choice selects docker", sandbox.records()[0]["runtime"],
          "docker")


def test_deploy_arguments() -> None:
    section("deploy arguments (podman)")
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    check("deploy exits 0", result.returncode, 0)

    records = sandbox.records()
    build_argv = sandbox.build_commands()[0]
    check_in("podman build requests docker format", "--format", build_argv)
    argv = sandbox.run_commands()[0]

    check("run names the production container", after(argv, "--name"),
          "iptv-charts")
    check("restart policy is always", after(argv, "--restart"), "always")
    check("publishes only loopback", after(argv, "-p"),
          "127.0.0.1:8770:8770")
    check("image is last", argv[-1], "iptv-charts")
    check_true("data volume is mounted", has_mount(argv, "iptv-charts-data", "/data"))
    check_true("config volume is mounted",
               has_mount(argv, "iptv-charts-config", "/config"))
    check_true("cache volume is mounted",
               has_mount(argv, "iptv-charts-cache", "/cache"))
    check_true("queue is mounted", has_mount(argv, str(sandbox.queue), "/playqueue"))
    check_true("media is mounted", has_mount(argv, str(media), "/media"))
    check_in("data volume gets podman :U", "iptv-charts-data:/data:U",
             volume_values(argv))
    check_in("config volume gets podman :U", "iptv-charts-config:/config:U",
             volume_values(argv))
    check_in("cache volume gets podman :U", "iptv-charts-cache:/cache:U",
             volume_values(argv))
    check_in("podman keeps the host uid", "--userns=keep-id", argv)

    envs = env_values(argv)
    check_in("host media path is passed for display",
             f"IPTV_CHARTS_MEDIA_HOST_DIR={media}", envs)
    check_in("movie folder env is relative to the media root",
             "IPTV_CHARTS_MOVIE_DIR=Movies", envs)
    check_in("series folder env is relative to the media root",
             "IPTV_CHARTS_SERIES_DIR=Series", envs)
    check_true("canonical movie folder created", (media / "Movies").is_dir())
    check_true("canonical series folder created", (media / "Series").is_dir())

    created_volumes = {record["argv"][2] for record in records
                       if record["argv"][:2] == ["volume", "create"]}
    check("three volumes are created", created_volumes,
          {"iptv-charts-data", "iptv-charts-config", "iptv-charts-cache"})
    check("normal deploy never removes a volume",
          [record["argv"] for record in records
           if record["argv"][:2] == ["volume", "rm"]], [])

    check_in("player bridge is stopped first", "--stop",
             sandbox.bridge_calls())
    check_in("player bridge is restarted", "--daemon", sandbox.bridge_calls())
    check_in("URL is printed", f"http://127.0.0.1:8770/", result.stdout)
    check_in("runtime is printed after readiness", "Runtime:   podman",
             result.stdout)
    check_in("port is printed", "Port:      8770", result.stdout)
    check_in("media path is printed", f"Media:     {media}", result.stdout)
    check_not_in("no non-loopback bind in output", "0.0.0.0", result.stdout)
    check("browser opens the loopback URL by default", sandbox.xdg_calls(),
          ["http://127.0.0.1:8770/"])

    # The release updater sets IPTV_CHARTS_NO_OPEN for unattended deploys.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    result = sandbox.run(
        "build", extra={"IPTV_MEDIA_DIR": str(media),
                        "IPTV_CHARTS_NO_OPEN": "1"})
    check("no-open deploy exits 0", result.returncode, 0)
    check("no-open deploy still prints the URL",
          "http://127.0.0.1:8770/" in result.stdout, True)
    check("IPTV_CHARTS_NO_OPEN suppresses the browser", sandbox.xdg_calls(), [])


def test_docker_arguments() -> None:
    section("deploy arguments (docker)")
    sandbox = Sandbox()
    sandbox.install_runtime("docker")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    result = sandbox.run(
        "build", extra={"IPTV_MEDIA_DIR": str(media), "IPTV_RUNTIME": "docker"})
    check("docker deploy exits 0", result.returncode, 0)
    argv = sandbox.run_commands()[0]
    check("docker uses the docker runtime", sandbox.records()[0]["runtime"],
          "docker")
    check("docker restart policy is always", after(argv, "--restart"), "always")
    check("docker publishes loopback only", after(argv, "-p"),
          "127.0.0.1:8770:8770")
    check_not_in("docker has no podman user namespace", "--userns=keep-id",
                 argv)
    check_in("docker data mount has no :U", "iptv-charts-data:/data",
             volume_values(argv))
    check_not_in("docker does not add :U", "iptv-charts-data:/data:U",
                 volume_values(argv))


def test_media_detection() -> None:
    section("media folder detection")

    # Aliases: never create Movies/Series beside an existing library.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.install_player_bridge()
    media = sandbox.media_root(
        dirs=("movies", "TV Shows", "Series"),
        files=("movies/keep.mkv", "TV Shows/keep.mkv"))
    # Series/ exists canonically and must win over the TV Shows alias.
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    check("alias deploy exits 0", result.returncode, 0)
    envs = env_values(sandbox.run_commands()[0])
    check_in("existing lowercase movies is adopted",
             "IPTV_CHARTS_MOVIE_DIR=movies", envs)
    check_in("canonical Series wins over alias",
             "IPTV_CHARTS_SERIES_DIR=Series", envs)
    check_not_in("Movies is not created beside movies", "Movies",
                 [p.name for p in media.iterdir()])
    check_true("alias media file is untouched", (media / "movies/keep.mkv").is_file())
    check_true("alias series file is untouched", (media / "TV Shows/keep.mkv").is_file())

    # tv/tv-shows aliases and case-insensitive names.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    media = sandbox.media_root(dirs=("MOVIES", "tv-shows"))
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    check("case-insensitive deploy exits 0", result.returncode, 0)
    envs = env_values(sandbox.run_commands()[0])
    check_in("case variant MOVIES is adopted", "IPTV_CHARTS_MOVIE_DIR=MOVIES",
             envs)
    check_in("tv-shows alias is adopted",
             "IPTV_CHARTS_SERIES_DIR=tv-shows", envs)
    check_not_in("no canonical folders appear", "Movies",
                 [p.name for p in media.iterdir()])
    check_not_in("no canonical series appears", "Series",
                 [p.name for p in media.iterdir()])

    # Alias order matches config.py: series beats tv.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    media = sandbox.media_root(dirs=("tv", "series", "movie"))
    sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    envs = env_values(sandbox.run_commands()[0])
    check_in("series alias wins in declared order",
             "IPTV_CHARTS_SERIES_DIR=series", envs)
    check_in("singular movie alias is adopted",
             "IPTV_CHARTS_MOVIE_DIR=movie", envs)

    # Canonical preference when both spellings exist.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    media = sandbox.media_root(dirs=("Movies", "movies", "Series", "tv"))
    sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    envs = env_values(sandbox.run_commands()[0])
    check_in("canonical Movies wins", "IPTV_CHARTS_MOVIE_DIR=Movies", envs)
    check_in("canonical Series wins", "IPTV_CHARTS_SERIES_DIR=Series", envs)

    # Tilde and relative paths are normalised without eval.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": "~/Tilde Media"})
    check("tilde media path exits 0", result.returncode, 0)
    argv = sandbox.run_commands()[0]
    check_true("tilde is expanded",
               has_mount(argv, str(sandbox.home / "Tilde Media"), "/media"))
    check_true("tilde folders created",
               (sandbox.home / "Tilde Media/Movies").is_dir())

    # Interactive prompt with the documented default, then a custom answer.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    chosen = sandbox.tmp / "Prompted Media"
    result = sandbox.run("build", pty_inputs=[f"{chosen}\n"])
    check("prompted media path exits 0", result.returncode, 0)
    check_in("media prompt offers the default", "Media folder [",
             result.stdout)
    check_true("prompted media path is mounted",
               has_mount(sandbox.run_commands()[0], str(chosen), "/media"))

    # Unattended default: ~/Media/IPTV Charts.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    result = sandbox.run("build")
    check("default media deploy exits 0", result.returncode, 0)
    default_media = sandbox.home / "Media" / "IPTV Charts"
    check_true("default media root is used",
               has_mount(sandbox.run_commands()[0], str(default_media), "/media"))
    check_true("default Movies folder is created",
               (default_media / "Movies").is_dir())
    check_true("default Series folder is created",
               (default_media / "Series").is_dir())


def test_dev_isolation() -> None:
    section("dev container isolation")
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.install_player_bridge(status=0)
    media = sandbox.media_root()
    result = sandbox.run("dev", extra={"IPTV_MEDIA_DIR": str(media)})
    check("dev deploy exits 0", result.returncode, 0)

    argv = sandbox.run_commands()[0]
    check("dev container name", after(argv, "--name"), "iptv-charts-dev")
    check("dev restart policy is always", after(argv, "--restart"), "always")
    check("dev port is 8771 on loopback", after(argv, "-p"),
          "127.0.0.1:8771:8770")
    check("dev image is last", argv[-1], "iptv-charts-dev")
    check_true("dev data volume", has_mount(argv, "iptv-charts-dev-data", "/data"))
    check_true("dev config volume",
               has_mount(argv, "iptv-charts-dev-config", "/config"))
    check_true("dev cache volume",
               has_mount(argv, "iptv-charts-dev-cache", "/cache"))
    check_in("dev media env is passed",
             f"IPTV_CHARTS_MEDIA_HOST_DIR={media}", env_values(argv))

    # Production names must never appear as a container/volume/image token.
    forbidden = {"iptv-charts", "iptv-charts-data", "iptv-charts-config",
                 "iptv-charts-cache"}
    check("no production names in dev args", forbidden & set(argv), set())
    for record in sandbox.records():
        check("dev never names production in any command",
              forbidden & set(record["argv"]), set())

    check_in("dev URL is printed", "http://127.0.0.1:8771/", result.stdout)
    check_in("dev runtime is printed", "Runtime:   podman", result.stdout)

    # An already-running bridge must not be restarted by a dev deploy.
    check_not_in("dev leaves a running bridge alone", "--daemon",
                 sandbox.bridge_calls())
    check_in("dev checks bridge status", "--status", sandbox.bridge_calls())

    # A missing bridge is started for the dev container.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.install_player_bridge(status=1)
    media = sandbox.media_root()
    sandbox.run("dev", extra={"IPTV_MEDIA_DIR": str(media)})
    check_in("dev starts a missing bridge", "--daemon", sandbox.bridge_calls())

    # --build only builds, and --stop touches only the dev container.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    media = sandbox.media_root()
    result = sandbox.run("dev", args=("--build",),
                         extra={"IPTV_MEDIA_DIR": str(media)})
    check("dev --build exits 0", result.returncode, 0)
    check("dev --build builds once", len(sandbox.build_commands()), 1)
    check("dev --build does not run", sandbox.run_commands(), [])
    check_true("dev --build leaves media alone", not (media / "Movies").exists())

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.precreate_container("iptv-charts-dev")
    sandbox.precreate_container("iptv-charts")
    result = sandbox.run("dev", args=("--stop",))
    check("dev --stop exits 0", result.returncode, 0)
    rm_commands = [record["argv"] for record in sandbox.records()
                   if record["argv"][:1] == ["rm"]]
    check("dev --stop removes the dev container", rm_commands,
          [["rm", "-f", "iptv-charts-dev"]])
    check_true("dev --stop leaves production running",
               (sandbox.containers / "iptv-charts").is_file())

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.precreate_container("iptv-charts-dev")
    result = sandbox.run("dev", args=("--logs",))
    check("dev --logs exits 0", result.returncode, 0)
    check("dev --logs follows the dev container",
          sandbox.records()[-1]["argv"], ["logs", "-f", "iptv-charts-dev"])

    sandbox = Sandbox()
    sandbox.install_runtime("docker")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    sandbox.run("dev", extra={"IPTV_MEDIA_DIR": str(media),
                              "IPTV_RUNTIME": "docker"})
    argv = sandbox.run_commands()[0]
    check("dev docker restart is always", after(argv, "--restart"), "always")
    check_not_in("dev docker has no userns", "--userns=keep-id", argv)
    check_not_in("dev docker has no :U", "iptv-charts-dev-data:/data:U",
                 volume_values(argv))


def test_safety_and_persistence() -> None:
    section("safety and persistence")

    # Normal replacement removes the container but never a volume.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.install_player_bridge()
    media = sandbox.media_root(files=("keep.txt",))
    sandbox.precreate_container("iptv-charts")
    for volume in ("iptv-charts-data", "iptv-charts-config", "iptv-charts-cache"):
        sandbox.precreate_volume(volume)
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media)})
    check("replace exits 0", result.returncode, 0)
    runtime_commands = [record["argv"] for record in sandbox.records()]
    check("existing container is removed",
          [argv for argv in runtime_commands if argv[:1] == ["rm"]],
          [["rm", "-f", "iptv-charts"]])
    check("volumes are never removed on replace",
          [argv for argv in runtime_commands
           if argv[:2] in (["volume", "rm"], ["volume", "prune"])], [])
    check("existing volumes are reused, not recreated",
          [argv for argv in runtime_commands if argv[:2] == ["volume", "create"]],
          [])
    check_true("media survives replacement", (media / "keep.txt").is_file())

    # A foreign listener on the port stops the deploy before a run.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    media = sandbox.media_root()
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media),
                                          "FAKE_SS_BUSY": "8770"})
    check_true("foreign port holder fails", result.returncode != 0)
    check_in("port conflict is explained", "already in use",
             result.stdout + result.stderr)
    check("no container was started", sandbox.run_commands(), [])

    # Our own running container may be replaced while it holds the port.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.install_player_bridge()
    media = sandbox.media_root()
    sandbox.precreate_container("iptv-charts")
    result = sandbox.run("build", extra={"IPTV_MEDIA_DIR": str(media),
                                          "FAKE_SS_BUSY": "8770"})
    check("own container replacement is allowed", result.returncode, 0)
    check("own container is rebuilt", len(sandbox.run_commands()), 1)

    # --destroy removes exactly the three production volumes.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    media = sandbox.media_root(files=("keep.txt", "movies/keep.mkv"))
    sandbox.precreate_container("iptv-charts")
    sandbox.precreate_container("iptv-charts-dev")
    for volume in ("iptv-charts-data", "iptv-charts-config",
                   "iptv-charts-cache", "iptv-charts-dev-data"):
        sandbox.precreate_volume(volume)
    result = sandbox.run("build", args=("--destroy", "--yes"),
                         extra={"IPTV_MEDIA_DIR": str(media)})
    check("destroy exits 0", result.returncode, 0)
    commands = [record["argv"] for record in sandbox.records()]
    volume_removals = [argv[2] for argv in commands
                       if argv[:2] == ["volume", "rm"]]
    check("destroy removes exactly the production volumes", volume_removals,
          ["iptv-charts-data", "iptv-charts-config", "iptv-charts-cache"])
    check("destroy removes the production container",
          [argv for argv in commands if argv[:1] == ["rm"]],
          [["rm", "-f", "iptv-charts"]])
    check_true("destroy leaves media untouched", (media / "keep.txt").is_file())
    check_true("destroy leaves movie media untouched",
               (media / "movies/keep.mkv").is_file())
    check_true("destroy never names the media path",
               all(str(media) not in " ".join(argv) for argv in commands))

    # Noninteractive destroy without --yes refuses before removing anything.
    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.precreate_container("iptv-charts")
    sandbox.precreate_volume("iptv-charts-data")
    result = sandbox.run("build", args=("--destroy",))
    check_true("unconfirmed destroy fails", result.returncode != 0)
    check_in("unconfirmed destroy explains --yes", "--yes",
             result.stdout + result.stderr)
    check("unconfirmed destroy removes nothing",
          [argv for argv in (record["argv"] for record in sandbox.records())
           if argv[:1] in (["rm"], ["volume"])], [])


def test_subcommands_and_usage() -> None:
    section("subcommands and usage")

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    result = sandbox.run("build", args=("--status",))
    check("status exits 0", result.returncode, 0)
    check_in("status shows runtime", "runtime:   podman", result.stdout)
    check_in("status shows missing container", "not created", result.stdout)
    check_in("status shows URL", "http://127.0.0.1:8770/", result.stdout)

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.precreate_container("iptv-charts")
    result = sandbox.run("build", args=("--logs",))
    check("logs exits 0", result.returncode, 0)
    check("logs follows the production container",
          sandbox.records()[-1]["argv"], ["logs", "-f", "iptv-charts"])

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    sandbox.precreate_container("iptv-charts")
    result = sandbox.run("build", args=("--shell",))
    check("shell exits 0", result.returncode, 0)
    check("shell execs into the container", sandbox.records()[-1]["argv"],
          ["exec", "-it", "iptv-charts", "/bin/sh"])

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    result = sandbox.run("build", args=("--build",))
    check("build-only exits 0", result.returncode, 0)
    check("build-only builds once", len(sandbox.build_commands()), 1)
    check("build-only does not run", sandbox.run_commands(), [])
    check_in("build-only points at the deploy script",
             "./build.deploy.sh", result.stdout)

    sandbox = Sandbox()
    result = sandbox.run("build", args=("--help",))
    check("help works without a runtime", result.returncode, 0)
    check_in("help shows usage", "Usage:", result.stdout)
    check("help runs no runtime", sandbox.records(), [])

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    result = sandbox.run("build", args=("--runtime",))
    check_true("missing --runtime value fails", result.returncode != 0)
    check_in("missing --runtime value is explained", "--runtime needs",
             result.stdout + result.stderr)

    sandbox = Sandbox()
    sandbox.install_runtime("podman")
    result = sandbox.run("build", args=("--nonsense",))
    check_true("unknown flag fails", result.returncode != 0)
    check("unknown flag runs nothing", sandbox.records(), [])


# ----------------------------------------------------------------------- main
def main() -> int:
    test_static_guards()
    test_runtime_selection()
    test_deploy_arguments()
    test_docker_arguments()
    test_media_detection()
    test_dev_isolation()
    test_safety_and_persistence()
    test_subcommands_and_usage()

    failed = [item for item in _results if not item[0]]
    for ok, label, detail in _results:
        if not ok:
            print(f"  \033[31mFAIL\033[0m {label}: {detail}")
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
