#!/usr/bin/env python3
"""Release workflow and updater tests (0.8.0).

Everything here is offline: GitHub is replaced by a loopback fixture server
and the update client receives an injected HTTP getter. The suite covers:

* semver-ish comparison (v prefix, padding, prerelease/build suffixes);
* the update check's cache, manual refresh, disabled switch and silent
  failure modes, plus the read-only ``/api/update/status`` blueprint;
* the release workflow's YAML structure and its tag/version gate;
* the actual source-archive and checksum commands from the workflow, run
  locally against this worktree;
* ``download.and.deploy.sh`` against a fake release: checksum-before-extract,
  unsafe archive rejection, current/stale/newer/not-running prompts, forced
  deploys, runtime/port/media propagation and offline/rate-limit errors.

Run with:  .venv/bin/python tests/release.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_BASE = Path(tempfile.mkdtemp(prefix="iptv-release-test-"))
os.environ["IPTV_CHARTS_DATA_DIR"] = str(_BASE / "data")
os.environ["IPTV_CHARTS_CONFIG_DIR"] = str(_BASE / "config")
os.environ["IPTV_CHARTS_CACHE_DIR"] = str(_BASE / "cache")
os.environ.pop("IPTV_CHARTS_PLAY_QUEUE", None)
os.environ.pop("IPTV_CHARTS_UPDATE_API", None)
os.environ.pop("IPTV_CHARTS_UPDATE_TIMEOUT", None)
os.environ.pop("IPTV_CHARTS_UPDATE_CHECK", None)

import requests  # noqa: E402

from iptv_charts import db, updates  # noqa: E402

SCRIPT = ROOT / "download.and.deploy.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
REPO = "tusx/iptv-charts"

_RESULT: list[tuple[bool, str, str]] = []
_FAILED = object()


def check(label: str, got, want) -> None:
    ok = got == want
    _RESULT.append((ok, label, f"{got!r}" if ok else f"{got!r} != {want!r}"))


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def check_false(label: str, got) -> None:
    check(label, bool(got), False)


def check_in(label: str, needle, haystack) -> None:
    ok = needle in haystack
    _RESULT.append((ok, label, f"{needle!r} found" if ok else f"{needle!r} not in {haystack!r}"))


def check_not_in(label: str, needle, haystack) -> None:
    ok = needle not in haystack
    _RESULT.append((ok, label, f"{needle!r} absent" if ok else f"{needle!r} unexpectedly present"))


def section(name: str) -> None:
    print(f"\n\033[1m{name}\033[0m")


# --------------------------------------------------------------------- mini YAML
class MiniYamlError(ValueError):
    pass


def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in inner.split(",")]
    if text == "true":
        return True
    if text == "false":
        return False
    if text in {"null", "~"}:
        return None
    return text


class _MiniYaml:
    """Parse the restricted YAML subset used by release.yml.

    PyYAML is not a runtime dependency, so the suite must be able to validate
    and execute the workflow locally with the standard library alone. The
    parser supports block mappings/sequences, quoted and plain scalars, inline
    lists and literal block scalars -- everything this workflow uses.
    """

    def __init__(self, text: str):
        self.lines = text.splitlines()
        self.index = 0

    @staticmethod
    def _indent(raw: str) -> int:
        return len(raw) - len(raw.lstrip(" "))

    @staticmethod
    def _blank(raw: str) -> bool:
        stripped = raw.strip()
        return not stripped or stripped.startswith("#")

    def _peek(self):
        while self.index < len(self.lines) and self._blank(self.lines[self.index]):
            self.index += 1
        if self.index >= len(self.lines):
            return None
        raw = self.lines[self.index]
        return raw, self._indent(raw)

    def parse(self):
        value = self._node(0)
        if value is None:
            raise MiniYamlError("empty document")
        extra = self._peek()
        if extra is not None:
            raise MiniYamlError(
                f"unexpected content at line {self.index + 1}: {extra[0]!r}"
            )
        return value

    def _node(self, minimum: int):
        found = self._peek()
        if found is None:
            return None
        raw, indent = found
        if indent < minimum:
            return None
        if raw[indent:].startswith("- "):
            return self._list(indent)
        return self._mapping(indent)

    def _mapping(self, indent: int) -> dict:
        result: dict = {}
        while True:
            found = self._peek()
            if found is None:
                break
            raw, current = found
            if current < indent:
                break
            content = raw[current:]
            if current != indent or content.startswith("- "):
                raise MiniYamlError(
                    f"bad mapping indentation at line {self.index + 1}: {raw!r}"
                )
            key, separator, rest = content.partition(":")
            if not separator:
                raise MiniYamlError(
                    f"missing ':' at line {self.index + 1}: {raw!r}"
                )
            key = key.strip()
            rest = rest.strip()
            if not key:
                raise MiniYamlError(f"empty key at line {self.index + 1}")
            self.index += 1
            if rest in {"|", "|-", "|+", ">", ">-", ">+"}:
                result[key] = self._block(
                    indent,
                    folded=rest.startswith(">"),
                    chomp=rest.endswith("-"),
                )
            elif rest == "":
                child = self._node(indent + 1)
                result[key] = {} if child is None else child
            else:
                result[key] = _scalar(rest)
        return result

    def _list(self, indent: int) -> list:
        result: list = []
        while True:
            found = self._peek()
            if found is None:
                break
            raw, current = found
            if current < indent:
                break
            if current != indent:
                raise MiniYamlError(
                    f"bad sequence indentation at line {self.index + 1}: {raw!r}"
                )
            content = raw[current:]
            if not content.startswith("- "):
                break
            rest = content[2:].strip()
            self.index += 1
            if rest == "":
                result.append(self._node(indent + 2))
            elif re.match(r"^[A-Za-z0-9_.-]+:", rest) and not rest.startswith(
                ("'", '"', "[", "{")
            ):
                key, _separator, value = rest.partition(":")
                item = {key.strip(): _scalar(value)}
                child = self._node(indent + 2)
                if isinstance(child, dict):
                    item.update(child)
                elif child is not None:
                    raise MiniYamlError(
                        f"bad sequence mapping at line {self.index + 1}"
                    )
                result.append(item)
            else:
                result.append(_scalar(rest))
        return result

    def _block(self, parent_indent: int, *, folded: bool, chomp: bool) -> str:
        collected: list[str] = []
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            if raw.strip() == "":
                collected.append("")
                self.index += 1
                continue
            current = self._indent(raw)
            if current <= parent_indent:
                break
            collected.append(raw)
            self.index += 1
        while collected and collected[-1] == "":
            collected.pop()
        if not collected:
            return "" if chomp else "\n"
        base = min(self._indent(line) for line in collected if line.strip())
        stripped = [line[base:] if line.strip() else "" for line in collected]
        text = "\n".join(stripped)
        if not folded:
            pass
        if not chomp:
            text += "\n"
        return text


def load_workflow() -> tuple[dict, str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    return _MiniYaml(text).parse(), text


def release_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["release"]["steps"]


def step_by_name(workflow: dict, name: str) -> dict:
    for step in release_steps(workflow):
        if step.get("name") == name:
            return step
    raise AssertionError(f"workflow step not found: {name}")


def run_shell_script(script: str, env: dict, *, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )


def _base_env(**extra) -> dict:
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env.pop("IPTV_CHARTS_UPDATE_CHECK", None)
    env.pop("IPTV_CHARTS_UPDATE_API", None)
    env.update(extra)
    return env


# --------------------------------------------------------------------- versions
def test_versions() -> None:
    section("version comparison")
    same = [
        ("0.8.0", "0.8.0"),
        ("v0.8.0", "0.8.0"),
        ("V0.8.0", "v0.8.0"),
        ("0.8", "0.8.0"),
        ("0.8.0.0", "0.8.0"),
        ("1.0.0+build.5", "1.0.0"),
    ]
    for left, right in same:
        check(f"{left} == {right}", updates.compare_versions(left, right), 0)

    ordered = [
        ("0.7.9", "0.8.0"),
        ("0.8.0-rc.1", "0.8.0"),
        ("0.8.0-alpha", "0.8.0-beta"),
        ("0.8.0-beta.2", "0.8.0-beta.10"),
        ("0.8.0-rc.1", "0.8.0-rc.2"),
        ("1.0.0-1", "1.0.0-alpha"),
        ("0.9.0", "1.0.0"),
        ("1.9.0", "1.10.0"),
    ]
    for older, newer in ordered:
        check(f"{older} < {newer}", updates.compare_versions(older, newer), -1)
        check(f"{newer} > {older}", updates.compare_versions(newer, older), 1)

    for invalid in ("", "banana", "v", "1..0", "1.0.0-", "1.0.0+"):
        try:
            updates.parse_version(invalid)
        except ValueError:
            passed = True
        else:
            passed = False
        check(f"{invalid!r} rejected", passed, True)

    check("invalid is not newer", updates.version_is_newer("banana", "0.8.0"), False)
    check("v-prefixed newer", updates.version_is_newer("v0.8.1", "0.8.0"), True)
    check("prerelease is older", updates.version_is_newer("0.8.0-rc.1", "0.8.0"), False)
    check("release beats prerelease", updates.version_is_newer("0.8.0", "0.8.0-rc.1"), True)


# ------------------------------------------------------------------ update check
class _Response:
    def __init__(self, status: int, payload, *, json_error: bool = False):
        self.status_code = status
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


def _release_payload(tag: str = "v99.0.0") -> dict:
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "published_at": "2026-10-25T00:00:00Z",
    }


def _clear_cache(conn) -> None:
    conn.execute("DELETE FROM metadata_cache")
    conn.commit()


def test_update_check() -> None:
    section("update check")
    db.init_db()
    conn = db.connect()
    current = updates.__version__
    check("module current version", current, "0.8.0")
    check("default api base", updates.latest_release_url(),
          "https://api.github.com/repos/tusx/iptv-charts/releases/latest")

    calls: list[dict] = []

    def ok_get(url, *, headers, timeout):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return _Response(200, _release_payload("v99.0.0"))

    _clear_cache(conn)
    result = updates.check_for_update(conn, http_get=ok_get, now=1_700_000_000)
    check("update available", result["update_available"], True)
    check("latest version", result["latest"], "99.0.0")
    check("latest tag", result["tag"], "v99.0.0")
    check("current version", result["current"], current)
    check("checked_at stored", result["checked_at"], 1_700_000_000)
    check("no error", result["error"], None)
    check("not marked cached", result["cached"], False)
    check("release url", result["url"], "https://github.com/tusx/iptv-charts/releases/tag/v99.0.0")
    check_true("manual update command", "download.and.deploy.sh" in result["update_command"])
    check_true("manual command pinned to tag", "/v99.0.0/" in result["update_command"])
    check("one HTTP call", len(calls), 1)
    check("no credentials in headers", "Authorization" in calls[0]["headers"], False)
    check("user agent", calls[0]["headers"]["User-Agent"], f"IPTV-Charts/{current}")
    check("accept header", calls[0]["headers"]["Accept"], "application/vnd.github+json")
    check_true("short timeout", 0 < calls[0]["timeout"] <= 10)

    calls.clear()
    cached = updates.check_for_update(conn, http_get=ok_get, now=1_700_000_100)
    check("cache used", cached["cached"], True)
    check("cache avoids HTTP", len(calls), 0)
    check("cache keeps checked_at", cached["checked_at"], 1_700_000_000)
    check("cache still reports update", cached["update_available"], True)

    cached = updates.check_for_update(
        conn, http_get=ok_get, force=True, now=1_700_000_200
    )
    check("force bypasses cache", len(calls), 1)
    check("force refreshes checked_at", cached["checked_at"], 1_700_000_200)

    calls.clear()
    updates.check_for_update(
        conn, http_get=ok_get, now=1_700_000_200 + updates.CACHE_TTL_SECONDS + 1
    )
    check("expired cache refetches", len(calls), 1)

    _clear_cache(conn)
    same = updates.check_for_update(
        conn, http_get=lambda *a, **k: _Response(200, _release_payload(f"v{current}"))
    )
    check("same version is not an update", same["update_available"], False)
    check("same version has no command", same["update_command"], None)

    # A failed refresh keeps the last confirmed release and reports the error.
    _clear_cache(conn)
    updates.check_for_update(
        conn, http_get=lambda *a, **k: _Response(200, _release_payload("v99.0.0")),
        now=1_700_000_300,
    )

    def offline(*args, **kwargs):
        raise requests.ConnectionError("offline")

    failed = updates.check_for_update(conn, http_get=offline, force=True)
    check("offline keeps cached latest", failed["latest"], "99.0.0")
    check("offline marks cached", failed["cached"], True)
    check("offline reports error", failed["error"], updates.OFFLINE_MESSAGE)
    check("offline never raises", failed["update_available"], True)

    _clear_cache(conn)
    empty = updates.check_for_update(conn, http_get=offline, force=True)
    check("offline without cache has no latest", empty["latest"], None)
    check("offline without cache is not an update", empty["update_available"], False)
    check_true("offline error present", bool(empty["error"]))

    rate = updates.check_for_update(
        conn, http_get=lambda *a, **k: _Response(403, {"message": "rate limit"}), force=True
    )
    check("rate limit error", rate["error"], updates.RATE_LIMIT_MESSAGE)

    missing = updates.check_for_update(
        conn, http_get=lambda *a, **k: _Response(404, {}), force=True
    )
    check("404 error", missing["error"], updates.NOT_FOUND_MESSAGE)

    broken = updates.check_for_update(
        conn, http_get=lambda *a, **k: _Response(200, None, json_error=True), force=True
    )
    check("invalid json error", broken["error"], updates.INVALID_MESSAGE)

    bad_tag = updates.check_for_update(
        conn, http_get=lambda *a, **k: _Response(200, _release_payload("not-a-version")),
        force=True,
    )
    check("invalid release version", bad_tag["error"], updates.INVALID_VERSION_MESSAGE)

    # Settings/environment switches.
    _clear_cache(conn)
    db.set_setting(conn, "update_check_enabled", "0")
    calls.clear()
    disabled = updates.check_for_update(conn, http_get=ok_get, force=True)
    check("setting disables checking", disabled["enabled"], False)
    check("disabled makes no call", len(calls), 0)
    check("disabled is not an update", disabled["update_available"], False)
    db.set_setting(conn, "update_check_enabled", "1")

    os.environ["IPTV_CHARTS_UPDATE_CHECK"] = "0"
    try:
        env_disabled = updates.check_for_update(conn, http_get=ok_get, force=True)
    finally:
        os.environ.pop("IPTV_CHARTS_UPDATE_CHECK", None)
    check("env disables checking", env_disabled["enabled"], False)
    check("env disabled makes no call", len(calls), 0)

    os.environ["IPTV_CHARTS_UPDATE_CHECK"] = "1"
    try:
        check("env 1 keeps checking", updates.update_check_enabled(conn), True)
    finally:
        os.environ.pop("IPTV_CHARTS_UPDATE_CHECK", None)

    conn.close()


def test_update_endpoint() -> None:
    section("update endpoint")
    from flask import Flask

    from iptv_charts import update_web

    db.init_db()
    app = Flask("update-test")
    app.register_blueprint(update_web.bp)
    client = app.test_client()

    conn = db.connect()
    _clear_cache(conn)
    conn.close()

    os.environ["IPTV_CHARTS_UPDATE_CHECK"] = "0"
    try:
        with mock.patch.object(
            updates.requests, "get", side_effect=AssertionError("must not call")
        ):
            response = client.get("/api/update/status")
    finally:
        os.environ.pop("IPTV_CHARTS_UPDATE_CHECK", None)
    payload = response.get_json()
    check("disabled endpoint status", response.status_code, 200)
    check("disabled endpoint ok", payload["ok"], True)
    check("disabled endpoint enabled", payload["enabled"], False)
    check("disabled endpoint current", payload["current"], updates.__version__)
    check("disabled endpoint no latest", payload["latest"], None)

    conn = db.connect()
    _clear_cache(conn)
    conn.close()
    with mock.patch.object(
        updates.requests, "get", side_effect=requests.ConnectionError("offline")
    ):
        response = client.get("/api/update/status?refresh=1")
    payload = response.get_json()
    check("offline endpoint status", response.status_code, 200)
    check("offline endpoint ok", payload["ok"], True)
    check("offline endpoint no update", payload["update_available"], False)
    check("offline endpoint error", payload["error"], updates.OFFLINE_MESSAGE)
    check("offline endpoint no latest", payload["latest"], None)

    # Pre-seed the cache and make the refresh fail: the endpoint keeps the
    # confirmed release but reports the failed manual check.
    conn = db.connect()
    metadata_payload = {
        "repo": REPO,
        "tag": "v99.0.0",
        "version": "99.0.0",
        "url": f"https://github.com/{REPO}/releases/tag/v99.0.0",
        "checked_at": int(time.time()),
    }
    from iptv_charts import metadata as metadata_module

    metadata_module.cache_set(conn, updates.CACHE_KEY, metadata_payload)
    conn.close()
    with mock.patch.object(
        updates.requests, "get", side_effect=requests.ConnectionError("offline")
    ):
        cached = client.get("/api/update/status").get_json()
        refreshed = client.get("/api/update/status?refresh=1").get_json()
    check("endpoint cached latest", cached["latest"], "99.0.0")
    check("endpoint cached flag", cached["cached"], True)
    check("endpoint cached error", cached["error"], None)
    check("endpoint cached update", cached["update_available"], True)
    check("endpoint refresh failed", refreshed["error"], updates.OFFLINE_MESSAGE)
    check("endpoint refresh keeps latest", refreshed["latest"], "99.0.0")
    check("endpoint refresh marks cached", refreshed["cached"], True)


# --------------------------------------------------------------------- workflow
def test_workflow_structure() -> None:
    section("release workflow")
    check("workflow exists", WORKFLOW.is_file(), True)
    workflow, text = load_workflow()
    check("workflow parses", isinstance(workflow, dict), True)
    check("workflow name", workflow.get("name"), "release")

    triggers = workflow.get("on")
    check("tag trigger present", isinstance(triggers, dict), True)
    push = (triggers or {}).get("push") or {}
    check("push tags", push.get("tags"), ["v*"])
    check("no branch trigger", "branches" in push, False)

    check("permissions", workflow.get("permissions"), {"contents": "write"})
    check("no secret use", "secrets." in text, False)
    check_not_in("no docker push", "docker push", text)
    check_not_in("no podman push", "podman push", text)
    check_not_in("no build-push action", "build-push-action", text)
    check_not_in("no image build", "docker build", text)
    check_in("checksum command", "sha256sum", text)
    check_in("changelog notes", "CHANGELOG.md", text)
    check_in("release create", "gh release create", text)
    check_in("release update", "gh release edit", text)
    check_in("asset overwrite", "--clobber", text)
    check_in("aggregate test runner", "python tests/run_all.py", text)
    check_not_in("suites are not enumerated twice", "suites=(tests/*.py)", text)
    check_in("tag/version gate", "iptv_charts.__version__", text)
    check_in("source archive name", "iptv-charts-${version}.tar.gz", text)
    check_in("checksum file name", ".tar.gz.sha256", text)
    check_in("fetch depth", "fetch-depth: 0", text)
    check_in("archives exclude git", "--exclude='./.git'", text)
    check_in("archives exclude venv", "--exclude='./.venv'", text)
    check_in("archives exclude data", "--exclude='./data'", text)
    check_in("archives exclude cache", "--exclude='./cache'", text)

    names = [step.get("name") for step in release_steps(workflow)]
    expected = [
        "Check out the tagged source",
        "Set up Python",
        "Install dependencies",
        "Verify the tag matches the package version",
        "Run every test suite",
        "Build the source archive and SHA-256 checksum",
        "Prepare release notes from CHANGELOG",
        "Create or update the GitHub release",
    ]
    check("all release steps present", names, expected)
    order = {name: index for index, name in enumerate(names)}
    check_true(
        "version gate runs before tests",
        order["Verify the tag matches the package version"] < order["Run every test suite"],
    )
    check_true(
        "tests run before publishing",
        order["Run every test suite"] < order["Create or update the GitHub release"],
    )
    check_true(
        "archive built before publishing",
        order["Build the source archive and SHA-256 checksum"]
        < order["Create or update the GitHub release"],
    )
    uses = [step.get("uses") for step in release_steps(workflow) if step.get("uses")]
    check_true("checkout action", any(str(item).startswith("actions/checkout@") for item in uses))
    check_true("python action", any(str(item).startswith("actions/setup-python@") for item in uses))

    # Execute the real tag/version gate. The tagged commit must match
    # iptv_charts.__version__, which is also what the release asset is named
    # after.
    script = step_by_name(workflow, "Verify the tag matches the package version")["run"]
    current = __import__("iptv_charts").__version__
    good = run_shell_script(script, _base_env(TAG=f"v{current}"))
    check("current tag accepted", good.returncode, 0)
    check_in("tag acceptance logged", "matches", good.stdout)

    wrong = run_shell_script(script, _base_env(TAG="v999.0.0"))
    check("mismatched tag rejected", wrong.returncode, 1)
    check_in("tag mismatch logged", "does not match", wrong.stdout)

    no_prefix = run_shell_script(script, _base_env(TAG=current))
    check("tag without v rejected", no_prefix.returncode, 1)

    prerelease = run_shell_script(script, _base_env(TAG=f"v{current}-rc.1"))
    check("prerelease tag rejected", prerelease.returncode, 1)


def test_workflow_archive() -> None:
    section("workflow archive and checksum")
    workflow, _text = load_workflow()
    version = __import__("iptv_charts").__version__
    name = f"iptv-charts-{version}"
    temp = Path(tempfile.mkdtemp(prefix="iptv-release-archive-"))
    env = _base_env(RUNNER_TEMP=str(temp), GITHUB_WORKSPACE=str(ROOT))
    script = step_by_name(workflow, "Build the source archive and SHA-256 checksum")["run"]
    completed = run_shell_script(script, env)
    check("archive step succeeds", completed.returncode, 0)

    archive = temp / "artifacts" / f"{name}.tar.gz"
    checksum = temp / "artifacts" / f"{name}.tar.gz.sha256"
    check("archive exists", archive.is_file(), True)
    check("checksum exists", checksum.is_file(), True)
    check_true("archive non-empty", archive.stat().st_size > 1000)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_text = checksum.read_text(encoding="utf-8").strip()
    check("checksum content", checksum_text, f"{digest}  {name}.tar.gz")

    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
    check_true("archive has entries", len(names) > 10)
    check("single archive root", all(item == name or item.startswith(f"{name}/") for item in names), True)
    check_true("archive root directory present", f"{name}/" in names or name in names)
    check_true("build script included", f"{name}/build.deploy.sh" in names)
    check_true("release workflow included", f"{name}/.github/workflows/release.yml" in names)
    check_true("updater script included", f"{name}/download.and.deploy.sh" in names)
    check_true("requirements included", f"{name}/requirements.txt" in names)
    check_true("tests included", f"{name}/tests/release.py" in names)

    parts = {part for item in names for part in item.split("/")}
    for excluded in (".git", ".venv", "data", "cache", "__pycache__"):
        check(f"archive excludes {excluded}", excluded in parts, False)
    check("archive excludes .pyc", any(item.endswith(".pyc") for item in names), False)
    check("archive excludes databases", any(item.endswith((".db", ".db-wal", ".db-shm")) for item in names), False)

    # Release notes come from the CHANGELOG section for the version, with a
    # non-empty fallback while the section is still being written.
    notes_script = step_by_name(workflow, "Prepare release notes from CHANGELOG")["run"]
    notes_env = _base_env(RUNNER_TEMP=str(temp), GITHUB_WORKSPACE=str(ROOT))
    notes_run = run_shell_script(notes_script, notes_env)
    check("notes step succeeds", notes_run.returncode, 0)
    notes_file = temp / "release-notes.md"
    check("notes file exists", notes_file.is_file(), True)
    notes = notes_file.read_text(encoding="utf-8") if notes_file.is_file() else ""
    check_true("notes non-empty", bool(notes.strip()))

    import shutil

    shutil.rmtree(temp, ignore_errors=True)


# ------------------------------------------------------------ release fixtures
FAKE_BUILD = """#!/usr/bin/env bash
# Fake build.deploy.sh written by tests/release.py.
# Recognised flags (for the updater's capability probe):
# --yes --runtime --port --media-dir
set -euo pipefail
{
    printf 'args=%s\\n' "$*"
    printf 'port=%s\\n' "${IPTV_PORT:-}"
    printf 'media=%s\\n' "${IPTV_MEDIA_DIR:-}"
    printf 'runtime=%s\\n' "${IPTV_CHARTS_RUNTIME:-}"
    printf 'assume_yes=%s\\n' "${IPTV_CHARTS_ASSUME_YES:-}"
    printf 'noninteractive=%s\\n' "${IPTV_CHARTS_NONINTERACTIVE:-}"
    printf 'no_open=%s\\n' "${IPTV_CHARTS_NO_OPEN:-}"
    printf 'tree=%s\\n' "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
} > "${FAKE_BUILD_MARKER:?FAKE_BUILD_MARKER is not set}"
echo "fake build deployed"
"""

# Mirrors A15's build.deploy.sh: it accepts --runtime/--yes and reads the
# port/media/runtime from IPTV_PORT, IPTV_MEDIA_DIR and IPTV_RUNTIME. Any other
# flag is a hard error, so this catches an updater that invents options.
STRICT_BUILD = """#!/usr/bin/env bash
# Accepts only --yes and --runtime (A15 interface); other flags must not be
# invented by the updater. Port/media/runtime come from the environment.
set -euo pipefail
original_args="$*"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes) shift ;;
        --runtime) shift 2 ;;
        *) echo "unexpected flag: $1" >&2; exit 3 ;;
    esac
done
{
    printf 'args=%s\\n' "$original_args"
    printf 'port=%s\\n' "${IPTV_PORT:-}"
    printf 'media=%s\\n' "${IPTV_MEDIA_DIR:-}"
    printf 'runtime=%s\\n' "${IPTV_RUNTIME:-}"
    printf 'tree=%s\\n' "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
} > "${FAKE_BUILD_MARKER:?FAKE_BUILD_MARKER is not set}"
echo "strict fake build deployed"
"""


def _tar_add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def make_archive(
    version: str = "9.9.9",
    *,
    include_build: bool = True,
    include_container: bool = True,
    root: str | None = None,
    extras: list[tuple] | None = None,
    build_script: str = FAKE_BUILD,
) -> bytes:
    """Build a release-shaped tar.gz, optionally with hostile members.

    An extra is ``("file", name, data)``, ``("symlink", name, target)`` or
    ``("dir", name)``.
    """
    buffer = io.BytesIO()
    top = root if root is not None else f"iptv-charts-{version}"
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        if top:
            info = tarfile.TarInfo(top)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        if include_build:
            _tar_add_bytes(tar, f"{top}/build.deploy.sh", build_script.encode(), 0o755)
        if include_container:
            _tar_add_bytes(tar, f"{top}/Containerfile", b"FROM scratch\n")
        _tar_add_bytes(
            tar,
            f"{top}/scripts/player-bridge.sh",
            b"#!/usr/bin/env bash\necho bridge\n",
            0o755,
        )
        for extra in extras or []:
            kind = extra[0]
            if kind == "file":
                _tar_add_bytes(tar, extra[1], extra[2])
            elif kind == "dir":
                info = tarfile.TarInfo(extra[1])
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "symlink":
                info = tarfile.TarInfo(extra[1])
                info.type = tarfile.SYMTYPE
                info.linkname = extra[2]
                tar.addfile(info)
            else:
                raise AssertionError(f"unknown extra kind: {kind}")
    return buffer.getvalue()


class _FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401 - quiet test server
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        fixture = self.server.fixture
        fixture.requests.append(self.path)
        base = f"http://127.0.0.1:{self.server.server_address[1]}"

        if self.path == "/api/health":
            if fixture.health_status != 200:
                self._send(fixture.health_status, b'{"alive": false}')
                return
            body = json.dumps(
                {"alive": True, "version": fixture.health_version}
            ).encode()
            self._send(200, body, "application/json")
            return

        if self.path.startswith("/repos/"):
            if fixture.api_status != 200:
                self._send(fixture.api_status, b'{"message": "test error"}', "application/json")
                return
            expected = {
                f"/repos/{REPO}/releases/latest",
                f"/repos/{REPO}/releases/tags/v{fixture.version}",
            }
            if self.path not in expected:
                self._send(404, b"{}", "application/json")
                return
            tar_name = f"iptv-charts-{fixture.version}.tar.gz"
            payload = {
                "tag_name": f"v{fixture.version}",
                "html_url": f"https://github.com/{REPO}/releases/tag/v{fixture.version}",
                "assets": [
                    {
                        "name": tar_name,
                        "browser_download_url": f"{base}/downloads/archive",
                    },
                    {
                        "name": tar_name + ".sha256",
                        "browser_download_url": f"{base}/downloads/checksum",
                    },
                ],
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return

        if self.path == "/downloads/archive":
            self._send(200, fixture.archive)
            return

        if self.path == "/downloads/checksum":
            self._send(200, fixture.checksum_text.encode())
            return

        self._send(404, b"not found", "text/plain")


class _Fixture:
    def __init__(
        self,
        *,
        version: str = "9.9.9",
        health_version: str = "9.9.9",
        health_status: int = 200,
        api_status: int = 200,
        container_running: str = "1",
        build_script: str = FAKE_BUILD,
    ):
        self.version = version
        self.health_version = health_version
        self.health_status = health_status
        self.api_status = api_status
        self.container_running = container_running
        self.requests: list[str] = []
        self.archive = make_archive(version, build_script=build_script)
        self.checksum_text = self._checksum(self.archive)
        self.workdir = Path(tempfile.mkdtemp(prefix="iptv-release-run-"))
        self.marker = self.workdir / "build-marker.txt"
        self.install_dir = self.workdir / "stable release"
        self.media_dir = self.workdir / "media root"
        self.media_dir.mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self.server.fixture = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _checksum(self, data: bytes) -> str:
        return f"{hashlib.sha256(data).hexdigest()}  iptv-charts-{self.version}.tar.gz\n"

    def set_archive(self, data: bytes, *, checksum: str | None = None) -> None:
        self.archive = data
        self.checksum_text = checksum or self._checksum(data)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextmanager
def release_fixture(**kwargs):
    fixture = _Fixture(**kwargs)
    try:
        yield fixture
    finally:
        fixture.close()


def run_updater(
    fixture: _Fixture,
    *args: str,
    env_extra: dict | None = None,
    stdin=None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    env = _base_env()
    env.pop("IPTV_PORT", None)
    env.pop("IPTV_MEDIA_DIR", None)
    env.update(
        {
            "IPTV_CHARTS_RELEASES_API": fixture.base_url,
            "IPTV_CHARTS_CONTAINER_RUNNING": fixture.container_running,
            "IPTV_CHARTS_RUNTIME": "podman",
            "IPTV_CHARTS_CONTAINER_NAME": "iptv-charts",
            "IPTV_CHARTS_HEALTH_TIMEOUT": "3",
            "IPTV_CHARTS_INSTALL_DIR": str(fixture.install_dir),
            "IPTV_PORT": str(fixture.port),
            "IPTV_MEDIA_DIR": str(fixture.media_dir),
            "FAKE_BUILD_MARKER": str(fixture.marker),
        }
    )
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=fixture.workdir,
        env=env,
        stdin=stdin if stdin is not None else subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _combined(result: subprocess.CompletedProcess) -> str:
    return (result.stdout or "") + (result.stderr or "")


def _marker(fixture: _Fixture) -> dict[str, str]:
    if not fixture.marker.is_file():
        return {}
    result = {}
    for line in fixture.marker.read_text(encoding="utf-8").splitlines():
        key, _separator, value = line.partition("=")
        result[key] = value
    return result


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_updater_current_and_stale() -> None:
    section("updater: current, stale and prompts")
    with release_fixture() as fixture:
        result = run_updater(fixture, "--yes")
        output = _combined(result)
        check("current exits zero", result.returncode, 0)
        check_in("current message", "already the latest release", output)
        check("current does not build", fixture.marker.exists(), False)
        check_true("current fetched health", "/api/health" in fixture.requests)

        forced = run_updater(fixture, "--yes", "--force")
        check("forced current deploys", forced.returncode, 0)
        check("forced current builds", fixture.marker.is_file(), True)
        marker = _marker(fixture)
        check("forced build port", marker.get("port"), str(fixture.port))
        check("forced build media", marker.get("media"), str(fixture.media_dir))
        check("forced build runtime", marker.get("runtime"), "podman")
        check("forced build noninteractive", marker.get("noninteractive"), "1")
        check("forced build assume yes", marker.get("assume_yes"), "1")
        check("forced build no open", marker.get("no_open"), "1")
        check("forced build runs from stable tree", marker.get("tree"), str(fixture.install_dir))
        check(
            "installed build script persists after exit",
            (fixture.install_dir / "build.deploy.sh").is_file(),
            True,
        )
        check(
            "player bridge script persists after exit",
            (fixture.install_dir / "scripts" / "player-bridge.sh").is_file(),
            True,
        )
        check(
            "installed bridge script is executable",
            os.access(fixture.install_dir / "scripts" / "player-bridge.sh", os.X_OK),
            True,
        )
        staging = [
            path.name
            for path in fixture.workdir.iterdir()
            if path.name.startswith(".stable release.")
        ]
        check("no install staging left behind", staging, [])
        check_true("flags passed", "--runtime podman" in marker.get("args", ""))
        check_true("port flag passed", f"--port {fixture.port}" in marker.get("args", ""))
        check_true("media flag passed", f"--media-dir {fixture.media_dir}" in marker.get("args", ""))
        check_in("final URL printed", f"http://127.0.0.1:{fixture.port}/", forced.stdout)

    with release_fixture(health_version="9.8.0") as fixture:
        result = run_updater(fixture, "--yes")
        output = _combined(result)
        check("stale update exits zero", result.returncode, 0)
        check_in("stale message shows running version", "9.8.0", output)
        check_in("stale message shows release", "9.9.9", output)
        check("stale update builds", fixture.marker.is_file(), True)
        check(
            "stale update uses stable tree",
            _marker(fixture).get("tree"),
            str(fixture.install_dir),
        )
        check(
            "stable tree survives the updater",
            (fixture.install_dir / "build.deploy.sh").is_file(),
            True,
        )
        check_true(
            "stale update downloaded archive",
            "/downloads/archive" in fixture.requests,
        )
        check_true(
            "stale update downloaded checksum",
            "/downloads/checksum" in fixture.requests,
        )

        fixture.marker.unlink()
        refused = run_updater(fixture)
        check("stale noninteractive refused", refused.returncode, 1)
        check_in("stale noninteractive reason", "--yes", _combined(refused))
        check("stale noninteractive did not build", fixture.marker.exists(), False)

    with release_fixture(health_version="9.9.10") as fixture:
        newer = run_updater(fixture, "--yes")
        check("running newer exits zero", newer.returncode, 0)
        check_in("running newer message", "newer than the latest", _combined(newer))
        check("running newer does not downgrade", fixture.marker.exists(), False)

        forced = run_updater(fixture, "--yes", "--force")
        check("forced downgrade builds", forced.returncode, 0)
        check("forced downgrade marker", fixture.marker.is_file(), True)

    with release_fixture(health_version="9.9.9-rc.1") as fixture:
        prerelease_running = run_updater(fixture, "--yes")
        check("prerelease is stale", prerelease_running.returncode, 0)
        check("prerelease updates", fixture.marker.is_file(), True)

    with release_fixture(
        version="9.9.9-rc.1", health_version="9.9.9"
    ) as fixture:
        newer_running = run_updater(fixture, "--yes")
        check("release running beats prerelease", newer_running.returncode, 0)
        check_in("no downgrade to prerelease", "newer than the latest", _combined(newer_running))
        check("no prerelease downgrade build", fixture.marker.exists(), False)


def test_updater_not_running_and_selection() -> None:
    section("updater: not running and option selection")
    with release_fixture(health_status=404, container_running="0") as fixture:
        result = run_updater(fixture, "--yes")
        output = _combined(result)
        check("not running deploys", result.returncode, 0)
        check_in("not running message", "is not running", output)
        check("not running builds", fixture.marker.is_file(), True)
        marker = _marker(fixture)
        check("not running keeps selected port", marker.get("port"), str(fixture.port))
        check("not running keeps selected media", marker.get("media"), str(fixture.media_dir))

    with release_fixture(health_status=404, container_running="0") as fixture:
        refused = run_updater(fixture)
        check("not running noninteractive refused", refused.returncode, 1)
        check("not running did not build", fixture.marker.exists(), False)

    with release_fixture(health_version="0.1.0") as fixture:
        result = run_updater(
            fixture,
            "--yes",
            "--version",
            "9.9.9",
            "--repo",
            "tusx/iptv-charts",
            "--port",
            str(fixture.port),
            "--media-dir",
            str(fixture.media_dir),
        )
        check("explicit version deploys", result.returncode, 0)
        check_true(
            "explicit version uses tag endpoint",
            f"/repos/{REPO}/releases/tags/v9.9.9" in fixture.requests,
        )
        check("explicit version builds", fixture.marker.is_file(), True)

    # A15's build.deploy.sh interface: --yes and --runtime are accepted, and
    # the port, media root and runtime are read from the environment. The
    # updater must not invent flags that the script would reject.
    with release_fixture(
        health_version="0.1.0", build_script=STRICT_BUILD
    ) as fixture:
        result = run_updater(fixture, "--yes")
        check("A15-style build succeeds", result.returncode, 0)
        marker = _marker(fixture)
        check(
            "A15-style flags only",
            marker.get("args", "").split(),
            ["--yes", "--runtime", "podman"],
        )
        check("A15-style IPTV_PORT", marker.get("port"), str(fixture.port))
        check("A15-style IPTV_MEDIA_DIR", marker.get("media"), str(fixture.media_dir))
        check("A15-style IPTV_RUNTIME", marker.get("runtime"), "podman")
        check("A15-style stable tree", marker.get("tree"), str(fixture.install_dir))


def test_updater_errors_and_safety() -> None:
    section("updater: offline, rate limit and unsafe archives")
    with release_fixture(health_version="0.1.0") as fixture:
        result = run_updater(
            fixture,
            "--yes",
            env_extra={"IPTV_CHARTS_INSTALL_DIR": "relative/install"},
        )
        check("relative install dir rejected", result.returncode, 1)
        check_in("relative install dir explained", "absolute", _combined(result).lower())
        check("relative install dir does not build", fixture.marker.exists(), False)

    offline_port = _free_port()
    with release_fixture(health_version="0.1.0") as fixture:
        result = run_updater(
            fixture,
            "--yes",
            env_extra={"IPTV_CHARTS_RELEASES_API": f"http://127.0.0.1:{offline_port}"},
        )
        check("offline exits non-zero", result.returncode, 1)
        check_in("offline message", "could not reach github", _combined(result).lower())
        check("offline does not build", fixture.marker.exists(), False)

    with release_fixture(api_status=403) as fixture:
        result = run_updater(fixture, "--yes")
        check("rate limit exits non-zero", result.returncode, 1)
        check_in("rate limit message", "rate limit", _combined(result).lower())
        check("rate limit does not build", fixture.marker.exists(), False)

    with release_fixture(api_status=404) as fixture:
        result = run_updater(fixture, "--yes")
        check("missing release exits non-zero", result.returncode, 1)
        check_in("missing release message", "no release", _combined(result).lower())
        check("missing release does not build", fixture.marker.exists(), False)

    with release_fixture(health_version="0.1.0") as fixture:
        fixture.set_archive(
            fixture.archive,
            checksum="0" * 64 + f"  iptv-charts-{fixture.version}.tar.gz\n",
        )
        result = run_updater(fixture, "--yes")
        check("checksum mismatch exits non-zero", result.returncode, 1)
        check_in("checksum mismatch message", "SHA-256 mismatch", _combined(result))
        check("checksum mismatch does not build", fixture.marker.exists(), False)

    unsafe_cases = [
        (
            "parent traversal",
            make_archive(extras=[("file", "../escape.txt", b"owned")]),
        ),
        (
            "absolute path",
            make_archive(extras=[("file", "/tmp/iptv-charts-evil", b"owned")]),
        ),
        (
            "nested traversal",
            make_archive(
                extras=[("file", "iptv-charts-9.9.9/../../escape.txt", b"owned")]
            ),
        ),
        (
            "symlink escape",
            make_archive(
                extras=[
                    ("symlink", "iptv-charts-9.9.9/link", "../../etc/passwd"),
                ]
            ),
        ),
        (
            "missing build script",
            make_archive(include_build=False),
        ),
        (
            "wrong top-level directory",
            make_archive(root="other-project"),
        ),
    ]
    for label, archive in unsafe_cases:
        with release_fixture(health_version="0.1.0") as fixture:
            fixture.set_archive(archive)
            result = run_updater(fixture, "--yes")
            check(f"{label} exits non-zero", result.returncode, 1)
            check(f"{label} does not build", fixture.marker.exists(), False)
            check(f"{label} installs nothing", fixture.install_dir.exists(), False)
            check_true(f"{label} error is explained", "error:" in _combined(result).lower())

    check("no file escaped the temp tree", (Path("/tmp") / "iptv-charts-evil").exists(), False)


# ------------------------------------------------------------------------- main
def main() -> int:
    check("updater script exists", SCRIPT.is_file(), True)
    check("updater script executable", os.access(SCRIPT, os.X_OK), True)

    test_versions()
    test_update_check()
    test_update_endpoint()
    test_workflow_structure()
    test_workflow_archive()
    test_updater_current_and_stale()
    test_updater_not_running_and_selection()
    test_updater_errors_and_safety()

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
