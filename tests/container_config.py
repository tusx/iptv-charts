#!/usr/bin/env python3
"""Static validation of the 0.8.0 container stack.

No container runtime is required: this suite reads the image definition and
the supervised service configs and asserts the contracts the rest of the app
relies on:

* a pinned nginx:alpine base and the expected apk/venv dependency layers;
* application code copied after dependencies;
* four supervised programs in order (valkey -> gunicorn -> bworker -> nginx),
  each restartable and logging to the container's stdout/stderr;
* nginx serving local static files, proxying with the original Host header,
  a body limit above the 64 MiB backup cap and timeouts above the longest
  synchronous wait;
* point-in-time Valkey with no persistence;
* a gunicorn timeout above 900s and a gunicorn entry point that starts no
  background threads;
* a stdlib-only HEALTHCHECK that requires app+ bworker health but only
  reports Valkey availability;
* no CDN references in the image config or the frontend assets.

Run with either the project venv or the system Python:

    .venv/bin/python tests/container_config.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKS = 0
FAILURES: list[str] = []

BACKUP_LIMIT_MB = 64
LONGEST_SYNC_WAIT_S = 3600
CDN_HOSTS = (
    "cdnjs.cloudflare.com",
    "cdn.jsdelivr.net",
    "unpkg.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "stackpath.bootstrapcdn.com",
    "use.fontawesome.com",
    "bootstrapcdn.com",
)


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def check(condition: object, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)


def logical_instructions(text: str) -> list[str]:
    """Dockerfile instructions with backslash continuations joined."""
    instructions: list[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.rstrip()
        continued = stripped.endswith("\\")
        piece = stripped[:-1] if continued else raw
        if pending:
            pending += piece
        else:
            pending = piece
        if not continued:
            instructions.append(pending)
            pending = ""
    if pending:
        instructions.append(pending)
    return instructions


def ini_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.fullmatch(r"\[([^\]]+)\]", line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current is not None and line and not line.startswith((";", "#")):
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def ini_value(block: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.*?)\s*$", block)
    return match.group(1) if match else None


def brace_block(text: str, header: str) -> str:
    """The body of an nginx block whose opening line starts with *header*."""
    start = text.index(header)
    opening = text.index("{", start)
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1:index]
    raise AssertionError(f"unterminated nginx block: {header}")


# --------------------------------------------------------------------- inputs
containerfile = read("Containerfile")
supervisor = read("supervisord.conf")
nginx = read("nginx.conf")
valkey = read("valkey.conf")
wsgi = read("wsgi.py")
containerignore = read(".containerignore")
requirements = read("requirements.txt")

instructions = [
    line for line in logical_instructions(containerfile)
    if line.strip() and not line.lstrip().startswith("#")
]
instruction_text = "\n".join(instructions)


def instructions_with(*needles: str) -> list[str]:
    return [line for line in instructions
            if all(needle in line for needle in needles)]


# ------------------------------------------------------------- base image/apk
from_lines = [line.strip() for line in instructions
              if line.lstrip().upper().startswith("FROM ")]
check(len(from_lines) == 1, "Containerfile must have exactly one FROM")
base_image = from_lines[0].split()[1] if from_lines else ""
check(
    re.search(r"nginx:\d+\.\d+(?:\.\d+)?-alpine$", base_image) is not None,
    f"base image must be a pinned nginx alpine tag, got {base_image!r}",
)
check(":latest" not in base_image, "base image must not use :latest")

apk_text = "\n".join(line for line in instructions if "apk add" in line)
for package in ("python3", "py3-pip", "ffmpeg", "supervisor", "valkey"):
    check(package in apk_text, f"apk must install {package}")
check("apk del .build-deps" in instruction_text,
      "temporary build dependencies must be removed in the same layer")
check("python3 -m venv /opt/venv" in instruction_text,
      "Python dependencies must live in /opt/venv (PEP 668)")
check("--break-system-packages" not in instruction_text,
      "the system Python guard must not be bypassed")
check("/opt/venv/bin/pip install" in instruction_text,
      "requirements must be installed into the venv")

# ------------------------------------------------------- dependency/code order
pip_lines = instructions_with("pip install", "requirements.txt")
copy_lines = [i for i, line in enumerate(instructions)
              if line.lstrip().upper().startswith("COPY ")]
app_copy_lines = [i for i, line in enumerate(instructions)
                  if line.lstrip().upper().startswith("COPY ")
                  and (". /app" in line or "iptv_charts" in line)]
check(len(pip_lines) == 1, "expected one pip install layer for requirements.txt")
check(len(app_copy_lines) >= 1, "application code must be COPYed into /app")
if pip_lines and app_copy_lines:
    pip_index = instructions.index(pip_lines[0])
    check(min(app_copy_lines) > pip_index,
          "application code must be copied after dependencies are installed")
    check(max(copy_lines) == max(app_copy_lines),
          "the application COPY must be the last COPY instruction")

check("WORKDIR /app" in instruction_text, "WORKDIR /app is required")
check("useradd" in instruction_text or "adduser" in instruction_text,
      "an unprivileged app user must be created")
check(re.search(r"(?m)^USER\s+app\s*$", instruction_text) is not None,
      "the container must run as the app user")
for path in ("/data", "/config", "/cache", "/playqueue", "/media"):
    check(path in instruction_text, f"writable path {path} must be created")
for variable, value in {
    "IPTV_CHARTS_DATA_DIR": "/data",
    "IPTV_CHARTS_CONFIG_DIR": "/config",
    "IPTV_CHARTS_CACHE_DIR": "/cache",
    "IPTV_CHARTS_PLAY_QUEUE": "/playqueue/queue",
    "IPTV_CHARTS_MEDIA_DIR": "/media",
}.items():
    check(re.search(rf"{variable}={re.escape(value)}\b", instruction_text) is not None,
          f"{variable} must be set to {value}")
check(re.search(r"(?m)^EXPOSE\s+8770\s*$", instruction_text) is not None,
      "EXPOSE 8770 is required")
check('CMD ["supervisord"' in instruction_text
      and "/app/supervisord.conf" in instruction_text,
      "CMD must run supervisord with the shipped config")

# ------------------------------------------------------------ supervisor units
sections = ini_sections(supervisor)
programs = {
    name.split("program:", 1)[1]: body
    for name, body in sections.items() if name.startswith("program:")
}
check(set(programs) == {"valkey-server", "gunicorn", "bworker", "nginx"},
      f"supervisor programs must be valkey-server/gunicorn/bworker/nginx, got "
      f"{sorted(programs)}")
check(ini_value(sections.get("supervisord", ""), "nodaemon") == "true",
      "supervisord must run in the foreground")
for name, body in programs.items():
    for key in ("autorestart", "stopasgroup", "killasgroup"):
        check(ini_value(body, key) == "true", f"{name}: {key}=true is required")
    check(ini_value(body, "stdout_logfile") == "/dev/stdout",
          f"{name}: stdout must go to the container's stdout")
    check(ini_value(body, "stderr_logfile") == "/dev/stderr",
          f"{name}: stderr must go to the container's stderr")

priorities = {}
for name, body in programs.items():
    raw = ini_value(body, "priority")
    check(raw is not None and raw.isdigit(), f"{name}: numeric priority required")
    priorities[name] = int(raw) if raw and raw.isdigit() else 0
check(priorities.get("valkey-server", 0) < priorities.get("gunicorn", 0)
      < priorities.get("bworker", 0) < priorities.get("nginx", 0),
      "start order must be valkey -> gunicorn -> bworker -> nginx")

valkey_command = ini_value(programs.get("valkey-server", ""), "command") or ""
check("/usr/bin/valkey-server" in valkey_command
      and "/app/valkey.conf" in valkey_command,
      "valkey-server must use the shipped config")

gunicorn_command = ini_value(programs.get("gunicorn", ""), "command") or ""
check("--bind 127.0.0.1:8771" in gunicorn_command,
      "gunicorn must bind loopback 8771")
check("--workers 1" in gunicorn_command, "gunicorn must run one worker")
check("--worker-class gthread" in gunicorn_command,
      "gunicorn must use the threaded worker class")
check("--threads 8" in gunicorn_command, "gunicorn must run eight threads")
check("wsgi:app" in gunicorn_command, "gunicorn must load the wsgi entry point")
timeout_match = re.search(r"--timeout\s+(\d+)", gunicorn_command)
gunicorn_timeout = int(timeout_match.group(1)) if timeout_match else 0
check(gunicorn_timeout > 900, "gunicorn timeout must exceed 900s")
check(gunicorn_timeout > LONGEST_SYNC_WAIT_S,
      "gunicorn timeout must exceed the 3600s synchronous wait")
check(re.search(r"--graceful-timeout\s+30\b", gunicorn_command) is not None,
      "gunicorn graceful timeout must be 30s")

bworker_command = ini_value(programs.get("bworker", ""), "command") or ""
check("bworker.py" in bworker_command, "the bworker program must run bworker.py")
nginx_command = ini_value(programs.get("nginx", ""), "command") or ""
check("/usr/sbin/nginx" in nginx_command and "/app/nginx.conf" in nginx_command,
      "nginx must run with the shipped config")

# --------------------------------------------------------------------- valkey
check(re.search(r"(?m)^bind\s+127\.0\.0\.1\s*$", valkey) is not None,
      "valkey must bind 127.0.0.1 only")
check(re.search(r"(?m)^port\s+6379\s*$", valkey) is not None,
      "valkey must listen on 6379")
check(re.search(r'(?m)^save\s+""\s*$', valkey) is not None,
      'valkey persistence must be disabled with save ""')
check(re.search(r"(?m)^save\s+\d", valkey) is None,
      "no timed RDB snapshot may be configured")
check(re.search(r"(?m)^appendonly\s+no\s*$", valkey) is not None,
      "valkey AOF must be disabled")
check(re.search(r"(?m)^daemonize\s+no\s*$", valkey) is not None,
      "valkey must stay in the foreground under supervisor")
check(re.search(r"(?m)^maxmemory\s+16mb\s*$", valkey) is not None,
      "valkey maxmemory must be small (16mb)")
check(re.search(r"(?m)^maxmemory-policy\s+noeviction\s*$", valkey) is not None,
      "valkey must use noeviction so the activity cap owns eviction")

# ---------------------------------------------------------------------- nginx
check("daemon off;" in nginx, "nginx must run with daemon off")
check(re.search(r"(?m)^\s*listen\s+0\.0\.0\.0:8770\s*;", nginx) is not None,
      "nginx must listen on 0.0.0.0:8770")
static_block = brace_block(nginx, "location /static/")
check("/app/iptv_charts/static/" in static_block,
      "nginx must serve the vendored static directory locally")
check("alias" in static_block, "the static location must use alias")

main_proxy = brace_block(nginx, "location / {")
check("proxy_pass http://iptv_charts_app" in main_proxy,
      "nginx must proxy all non-static routes to gunicorn")
check("127.0.0.1:8771" in nginx, "the gunicorn upstream must be loopback 8771")
check("proxy_set_header Host $http_host;" in main_proxy,
      "nginx must preserve the original Host header for the Flask guard")
check("X-Forwarded-For" in main_proxy and "X-Real-IP" in main_proxy,
      "nginx must forward the client address headers")
for directive in ("proxy_send_timeout", "proxy_read_timeout"):
    match = re.search(rf"{directive}\s+(\d+)s", main_proxy)
    value = int(match.group(1)) if match else 0
    check(value > LONGEST_SYNC_WAIT_S,
          f"{directive} must exceed the 3600s synchronous wait")

body_match = re.search(r"client_max_body_size\s+(\d+)m", nginx)
body_limit = int(body_match.group(1)) if body_match else 0
check(body_limit > BACKUP_LIMIT_MB,
      "client_max_body_size must exceed the 64 MiB backup limit")

health_block = brace_block(nginx, "location = /api/health {")
check("proxy_pass http://iptv_charts_app" in health_block,
      "/api/health must be exposed through the nginx proxy")

urls = re.findall(r"https?://([^\s;\"']+)", nginx)
allowed_hosts = {"iptv_charts_app", "127.0.0.1:8771"}
check(all(url in allowed_hosts for url in urls),
      f"nginx must not proxy to external hosts: {urls}")

# ----------------------------------------------------------------------- wsgi
check("from iptv_charts.app import create_app" in wsgi,
      "wsgi.py must import create_app from the app package")
check(re.search(r"(?m)^app\s*=\s*create_app\(\)\s*$", wsgi) is not None,
      "wsgi.py must expose app = create_app()")
check(len(re.findall(r"create_app\(\)", wsgi)) == 1,
      "create_app() must be called exactly once at import")
for forbidden in ("threading", "Thread(", "start_background(", "run.py"):
    check(forbidden not in wsgi,
          f"wsgi.py must not start background work ({forbidden!r} found)")

# ----------------------------------------------------------------- healthcheck
health_lines = [line for line in instructions
                if line.lstrip().upper().startswith("HEALTHCHECK")]
check(len(health_lines) == 1, "Containerfile must define exactly one HEALTHCHECK")
health = health_lines[0] if health_lines else ""
check("python" in health, "the HEALTHCHECK must use Python")
check("curl" not in health and "wget" not in health,
      "the HEALTHCHECK must not depend on curl/wget")
check("/api/health" in health, "the HEALTHCHECK must call /api/health")
check("worker" in health and "alive" in health,
      "the HEALTHCHECK must require a live bworker")
check(health.count("'status'") >= 1 and "'ok'" in health,
      "the HEALTHCHECK must require app status ok")
check("valkey" in health.lower() and "6379" in health,
      "the HEALTHCHECK must probe Valkey")
check("PONG" in health, "the Valkey probe must actually ping the server")
check("degraded" in health.lower() or "unavailable" in health.lower(),
      "Valkey availability must be reported as a degraded condition")
check("&&" not in health,
      "the Valkey probe must not gate the app probe (no shell &&)")
check("; python -c" in health,
      "the app probe must run even when the Valkey probe fails")
check(health.index("valkey") < health.index("api/health"),
      "Valkey availability must be reported before the app probe")
for stdlib_module in ("json", "urllib.request", "socket"):
    check(stdlib_module in health,
          f"the HEALTHCHECK must use only the standard library ({stdlib_module})")
check("import redis" not in health,
      "the HEALTHCHECK must not import the third-party redis client")

# ---------------------------------------------------------- requirements/no CDN
check(re.search(r"(?m)^gunicorn[<>=]", requirements) is not None,
      "requirements.txt must include gunicorn")
for preserved in ("Flask", "requests", "ijson", "waitress"):
    check(re.search(rf"(?m)^{preserved}\b", requirements) is not None,
          f"requirements.txt must keep {preserved}")

for path in ("Containerfile", "nginx.conf", "supervisord.conf", "wsgi.py"):
    text = read(path).lower()
    for host in CDN_HOSTS:
        check(host not in text, f"{path} must not reference CDN host {host}")

asset_files = [
    *sorted((ROOT / "iptv_charts" / "templates").rglob("*.html")),
    *sorted((ROOT / "iptv_charts" / "static").rglob("*.js")),
    *sorted((ROOT / "iptv_charts" / "static").rglob("*.css")),
]
# Only resource-loading attributes count; ordinary <a href> links to TMDB
# documentation are plain navigation, not runtime asset loads.
remote_asset_patterns = (
    r"<script[^>]+src\s*=\s*[\"']https?://",
    r"<link[^>]+href\s*=\s*[\"']https?://",
    r"<img[^>]+src\s*=\s*[\"']https?://",
    r"<source[^>]+src\s*=\s*[\"']https?://",
    r"@import\s+url\(\s*[\"']?https?://",
    r"url\(\s*[\"']?https?://",
)
remote_assets: list[str] = []
for path in asset_files:
    text = path.read_text(encoding="utf-8", errors="replace")
    if any(re.search(pattern, text, re.IGNORECASE)
           for pattern in remote_asset_patterns):
        remote_assets.append(f"{path.relative_to(ROOT)} (remote asset URL)")
    for host in CDN_HOSTS:
        if host in text:
            remote_assets.append(f"{path.relative_to(ROOT)} ({host})")
check(not remote_assets,
      "frontend assets must be vendored locally, not loaded from a CDN: "
      + ", ".join(sorted(set(remote_assets))[:8]))

# -------------------------------------------------------------- ignore context
for pattern in (".git/", ".venv/", "__pycache__", "tests/", "scripts/"):
    check(pattern in containerignore,
          f".containerignore must exclude {pattern}")
check(re.search(r"(?m)^/?iptv_charts/?\s*$", containerignore) is None,
      ".containerignore must not exclude the application package")


def main() -> int:
    if FAILURES:
        print(f"container_config: {len(FAILURES)} of {CHECKS} checks failed")
        for failure in FAILURES:
            print(f"  FAIL: {failure}")
        return 1
    print(f"container_config: {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
