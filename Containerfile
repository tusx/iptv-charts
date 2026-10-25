# IPTV Charts -- single-container image.
#
# The public process is nginx (port 8770). It serves the vendored static files
# itself and proxies everything else to gunicorn on loopback. Supervisor keeps
# the four processes alive:
#
#   valkey-server  127.0.0.1:6379  RAM-only activity log
#   gunicorn       127.0.0.1:8771  Flask web process (one worker, 8 threads)
#   bworker        no socket       the only process that runs heavy work
#   nginx          0.0.0.0:8770    static files + reverse proxy
#
# Playback happens in the browser or the host-side player bridge, so there is
# no display stack here. ffmpeg is installed for the downloader: HLS VODs are
# remuxed with it, and ffprobe inspects provider URLs before a download so
# quality/audio/subtitle requirements use the real stream facts. Nothing is
# transcoded.
#
# Build and run via the deploy scripts, which handle the volume, play-queue
# mount and loopback-only port publishing. Building directly works too:
#
#   podman build -t iptv-charts .

# Pinned to an exact nginx/alpine release: a moving `nginx:alpine` tag would
# make image builds change underneath the release. Fully qualified so Podman
# setups without a default registry still resolve it.
FROM docker.io/library/nginx:1.29.8-alpine

# -u so logs appear immediately rather than sitting in a buffer.
# /opt/venv avoids PEP 668's externally-managed-environment restriction on
# Alpine's system Python without disarming the system-wide guard.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Runtime packages. ffmpeg ships both ffmpeg and ffprobe; valkey provides the
# server; supervisor runs the four processes. ca-certificates covers
# provider/TMDB HTTPS independently of certifi.
RUN apk add --no-cache \
        ca-certificates \
        ffmpeg \
        py3-pip \
        python3 \
        supervisor \
        valkey \
 && python3 -m venv /opt/venv

# Dependencies first: this layer only rebuilds when requirements.txt changes.
# The virtual package holds compiler headers only for the duration of the pip
# install and is removed in the same layer, so it never reaches the final
# image. It exists because a package without a musllinux wheel must be built
# from source.
COPY requirements.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .build-deps build-base python3-dev \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && apk del .build-deps \
 && rm -f /tmp/requirements.txt

# The app writes only to these paths. Existing files in a volume inherit the
# ownership of the image directory they cover, so chowning them here is what
# makes a fresh named volume writable by the unprivileged app user. /playqueue
# is the directory; the app appends its request file at /playqueue/queue.
RUN addgroup -g 1000 app \
 && adduser -D -u 1000 -G app -h /home/app -s /sbin/nologin app \
 && mkdir -p /data /config /cache /playqueue /media /home/app \
 && chown -R app:app /data /config /cache /playqueue /media /home/app

WORKDIR /app

# Application code LAST, after every dependency layer above. A source change
# therefore rebuilds only this layer and never re-runs pip or apk.
COPY . /app

# Stated explicitly rather than inferred, so the app never has to ask whether
# it is in a container. /media and /playqueue are bind-mounted by the deploy
# scripts; /data is a named volume.
ENV IPTV_CHARTS_DATA_DIR=/data \
    IPTV_CHARTS_CONFIG_DIR=/config \
    IPTV_CHARTS_CACHE_DIR=/cache \
    IPTV_CHARTS_PLAY_QUEUE=/playqueue/queue \
    IPTV_CHARTS_MEDIA_DIR=/media \
    IPTV_CHARTS_VALKEY_URL=redis://127.0.0.1:6379/0

# The web, worker and nginx processes are unprivileged. nginx binds 8770
# (>1024) and uses only /tmp for pid/temp files; the deploy scripts publish
# the port on the host's loopback only.
USER app

EXPOSE 8770

# Health = the Flask app answers through nginx (status ok) and the bworker
# heartbeat is fresh. A paused bworker is still alive; only a stopped or stale
# one fails. Valkey is probed independently with the standard library and its
# availability is reported, but a degraded activity log must not take the
# whole container down: the two probes are separated by `;`, so only the app
# probe's exit status decides the health check. No curl/wget package needed.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1',6379)); s.sendall(b'PING\r\n'); p=s.recv(16); s.close(); print('valkey: ' + ('available' if p.startswith(b'+PONG') else 'unexpected response (activity log degraded)'))" 2>/dev/null || echo "valkey: unavailable (activity log degraded)"; python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4)); w=d.get('worker') or {}; ok=d.get('status') == 'ok' and bool(w.get('alive')); print('health: app=%s bworker=%s alive=%s' % (d.get('status'), w.get('status'), bool(w.get('alive')))); raise SystemExit(0 if ok else 1)"

# Supervisor is PID 1 inside the container; supervisord.conf starts nginx last
# so the proxy appears only after gunicorn and the bworker have been launched.
CMD ["supervisord", "-c", "/app/supervisord.conf"]
