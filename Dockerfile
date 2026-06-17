# ---- Bilal Home Adhan System – Raspberry Pi optimized image ----
FROM python:3.11-slim-bookworm AS base

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for pychromecast / zeroconf / mDNS
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libavahi-compat-libdnssd-dev \
        avahi-daemon \
        dbus \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security hardening
RUN groupadd -r bilal && useradd -r -g bilal -m -s /bin/bash bilal

WORKDIR /app

# Install Python dependencies (combined to share cached layers)
COPY scheduler/requirements.txt /tmp/sched-requirements.txt
COPY web/requirements.txt /tmp/web-requirements.txt
RUN pip install --no-cache-dir \
    -r /tmp/sched-requirements.txt \
    -r /tmp/web-requirements.txt

# Fail the build loudly if a dependency's files didn't land in the image. We've
# shipped images where pip recorded a package's dist-info but the module dir was
# dropped from the arm64 layer (multi-arch QEMU + gha-cache corruption): `pip
# freeze` lists it, the runtime import explodes, and Watchtower deploys it as
# "healthy". Importing the boot-critical packages here turns that into a build
# failure instead of a fleet-wide outage.
RUN python -c "import tzlocal, pytz, apscheduler, sqlalchemy, zeroconf, requests, dotenv, flask, flask_login, flask_wtf, werkzeug, gunicorn, pychromecast, adhanpy; from apscheduler.schedulers.background import BackgroundScheduler; print('dependency import smoke test passed')"

# Copy application code
COPY scheduler/ /app/scheduler/
COPY web/ /app/web/

# Version marker — read by the web app for the dashboard footer, so a deployed
# unit reports its real version instead of "dev".
COPY VERSION /app/VERSION

# Create data and audio directories
RUN mkdir -p /data /audio && chown -R bilal:bilal /data /audio /app

# --- Scheduler target ---
FROM base AS scheduler
USER bilal
ENV PYTHONUNBUFFERED=1
ENV CONFIG_DIR=/data
ENV AUDIO_DIR=/audio
WORKDIR /app/scheduler
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "from config import load_config; load_config()" || exit 1
CMD ["python", "main.py"]

# --- Web target ---
FROM base AS web
USER bilal
ENV PYTHONUNBUFFERED=1
ENV CONFIG_DIR=/data
ENV AUDIO_DIR=/audio
ENV SCHEDULER_PATH=/app/scheduler
ENV WEB_PORT=5000
WORKDIR /app/web
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/login')" || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
