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

# Copy application code
COPY scheduler/ /app/scheduler/
COPY web/ /app/web/

# Create data and audio directories
RUN mkdir -p /data /audio && chown -R bilal:bilal /data /audio /app

# --- Scheduler target ---
FROM base AS scheduler
USER bilal
ENV PYTHONUNBUFFERED=1
ENV CONFIG_DIR=/data
ENV AUDIO_DIR=/audio
WORKDIR /app/scheduler
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
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
