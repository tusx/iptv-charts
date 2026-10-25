#!/usr/bin/env python3
"""Run every test suite in a deterministic order and summarise the matrix.

Each suite runs in its own process: several of them install a temporary data
directory, fake player/ffprobe binaries or a local fixture server at import
time, and sharing an interpreter would leak that state between suites.

Usage:
    .venv/bin/python tests/run_all.py            # everything
    .venv/bin/python tests/run_all.py smoke.py   # only the named suites
    .venv/bin/python tests/run_all.py --list     # show the order and exit
    .venv/bin/python tests/run_all.py --fail-fast

``fixture_server.py`` is support code, not a suite, and ``run_all.py`` itself
is excluded. Every other ``tests/*.py`` file is run, including files added
after this runner was written (they run alphabetically at the end).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

#: Files that are importable support modules rather than standalone suites.
SUPPORT = frozenset({"fixture_server.py", "harness.py", "run_all.py"})

#: Fast, dependency-free checks first; suites that start servers, workers or
#: real fixture downloads last. Anything not listed runs alphabetically after
#: these, so a new suite is never silently skipped.
ORDER = (
    "smoke.py",
    "docs.py",
    "container_config.py",
    "deploy_scripts.py",
    "profiles.py",
    "migration_backup.py",
    "bworker.py",
    "stage_history.py",
    "storage.py",
    "explore.py",
    "import_media.py",
    "ui_shell.py",
    "settings_sections.py",
    "activity_ui.py",
    "movies_ui.py",
    "series_ui.py",
    "acquisition.py",
    "release.py",
    "integration.py",
    "bridge.py",
    "endtoend.py",
    "manager.py",
)


def suites() -> list[str]:
    present = {
        path.name for path in HERE.glob("*.py") if path.name not in SUPPORT
    }
    ordered = [name for name in ORDER if name in present]
    ordered.extend(sorted(present.difference(ordered)))
    return ordered


def _banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--list" in args:
        for name in suites():
            print(name)
        return 0
    fail_fast = "--fail-fast" in args
    requested = [arg for arg in args if not arg.startswith("-")]
    available = suites()
    if requested:
        unknown = [name for name in requested if name not in available]
        if unknown:
            print(f"Unknown test suite(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"Available: {', '.join(available)}", file=sys.stderr)
            return 2
        selected = [name for name in available if name in set(requested)]
    else:
        selected = available
    if not selected:
        print("No test suites found.", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    results: list[tuple[str, bool, float]] = []
    started_all = time.monotonic()

    for index, name in enumerate(selected, start=1):
        _banner(f"[{index}/{len(selected)}] {name}")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, str(HERE / name)],
                cwd=str(ROOT),
                env=env,
                check=False,
            )
            ok = completed.returncode == 0
            detail = f"exit {completed.returncode}"
        except OSError as exc:
            ok = False
            detail = f"could not run: {exc}"
            print(f"error: {detail}", file=sys.stderr)
        elapsed = time.monotonic() - started
        results.append((name, ok, elapsed))
        print(f"\n{name}: {'PASS' if ok else 'FAIL'} ({elapsed:.1f}s)",
              flush=True)
        if not ok and fail_fast:
            break

    _banner("test matrix summary")
    width = max(len(name) for name, _ok, _seconds in results)
    for name, ok, elapsed in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {elapsed:7.1f}s")
    passed = sum(1 for _name, ok, _seconds in results if ok)
    failed = [name for name, ok, _seconds in results if not ok]
    total = time.monotonic() - started_all
    print(
        f"\n{passed}/{len(results)} suites passed in {total:.1f}s"
        + (f"; failed: {', '.join(failed)}" if failed else "")
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
