# IPTV Charts -- container image.
#
# Build and run via setup.deploy.sh, which handles the volume, the play-queue
# mount and the port publishing. Building directly works too:
#
#   podman build -t iptv-charts .
#
# Playback still happens in your browser or on the host via the player bridge,
# so no display stack belongs here. ffmpeg is installed for the downloader:
# HLS VODs are remuxed with it, and ffprobe validates completed files and
# detects their real resolution.

FROM python:3.13-slim

# -u so logs appear immediately rather than sitting in a buffer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --no-install-recommends keeps the ffmpeg dependency tree as small as the
# Debian package allows. The list is removed afterwards so the layer does not
# carry apt metadata.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: this layer only rebuilds when requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py ./
COPY iptv_charts ./iptv_charts

# The app writes only to these paths, all of which are mount points.
# Creating and chowning them here matters: a fresh named volume inherits the
# ownership of the image directory it covers, which is what makes the volume
# writable without running as root.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /config /cache /playqueue /media \
 && chown -R app:app /data /config /cache /playqueue /media

# Stated explicitly rather than inferred, so the app never has to ask whether
# it is in a container. /media is bind-mounted from the host by setup.deploy.sh.
ENV IPTV_CHARTS_DATA_DIR=/data \
    IPTV_CHARTS_CONFIG_DIR=/config \
    IPTV_CHARTS_CACHE_DIR=/cache \
    IPTV_CHARTS_PLAY_QUEUE=/playqueue/queue \
    IPTV_CHARTS_MEDIA_DIR=/media

USER app

EXPOSE 8770

# Uses python rather than curl so the image needs no extra package.
HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8770/api/health', timeout=4)" || exit 1

# 0.0.0.0 inside the container is required: a server bound to the container's
# own 127.0.0.1 would be unreachable through the published port. The host side
# is still bound to 127.0.0.1 only, so nothing is exposed to the network.
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8770", "--no-browser"]
