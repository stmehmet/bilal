# Bilal – Home Adhan System

A production-ready, "giftable" Adhan system for Raspberry Pi 4 that automatically plays the call to prayer on Google Nest/Home speakers and Samsung SmartThings devices.

## Features

- **Automatic prayer time calculation** using the `adhan` library with 13+ calculation methods
- **Chromecast/Google Nest discovery** via mDNS – plays `.mp3` files from local storage
- **Samsung SmartThings integration** for Family Hub fridges (audio + notifications)
- **IP-based auto-location** on first boot (ipapi / ipinfo fallback)
- **Captive Portal** – creates a WiFi hotspot if no network is found
- **Mobile-responsive dashboard** (Flask + Tailwind CSS) with password protection
- **Dockerized** with multi-arch CI/CD (ARM64 for Pi, AMD64 for dev)
- **Auto-updates** via Watchtower pulling from GitHub Container Registry (GHCR)
- **Security hardened** – non-root containers, SSH lockdown, UFW firewall

## Architecture

```
┌────────────────────────────────────────────────────┐
│  Raspberry Pi 4                                    │
│                                                    │
│  ┌──────────────┐  ┌────────────────┐              │
│  │  bilal-web   │  │ bilal-scheduler│              │
│  │  (Flask/     │  │ (APScheduler + │              │
│  │   Gunicorn)  │  │  pychromecast) │              │
│  │  :5000       │  │  host network  │              │
│  └──────┬───────┘  └───────┬────────┘              │
│         │   Shared /data volume    │               │
│         └──────────┬───────────────┘               │
│                    │                               │
│  ┌─────────────────┴──────────────────┐            │
│  │         Watchtower                 │            │
│  │      (auto-pull from GHCR)         │            │
│  └────────────────────────────────────┘            │
└────────────────────────────────────────────────────┘
         │                        │
    ┌────┴─────┐          ┌──────┴────────┐
    │ Google   │          │  SmartThings  │
    │ Nest/Home│          │  Family Hub   │
    └──────────┘          └───────────────┘
```

## Quick Start

### Prerequisites

- Raspberry Pi 4 (or Zero 2 W) with Raspberry Pi OS (64-bit)
- Docker and Docker Compose installed
- Adhan `.mp3` files placed in the `audio/` directory

### Installation

```bash
# Clone the repository
git clone https://github.com/stmehmet/bilal.git
cd bilal

# Add your adhan audio files
cp /path/to/adhan_makkah.mp3 audio/
cp /path/to/adhan_fajr.mp3 audio/

# Generate a secret key
echo "SECRET_KEY=$(openssl rand -hex 32)" > .env

# Build and start
docker compose up -d --build
```

Or use the one-line installer:

```bash
curl -sSL https://raw.githubusercontent.com/stmehmet/bilal/main/scripts/install.sh | bash
```

### Access the Dashboard

Open `http://<pi-ip-address>:5000` in your browser. On first visit, you'll be asked to create a password.

## Configuration

### Dashboard Features

| Feature | Description |
|---------|-------------|
| **Prayer Times** | Shows today's 5 prayer times with the selected calculation method |
| **Skip Prayers** | Toggle individual prayers on/off |
| **Calculation Method** | Choose from ISNA, MWL, Egyptian, UmmAlQura, and more |
| **Audio Selection** | Pick different `.mp3` files for regular and Fajr adhans |
| **Volume Control** | Adjust playback volume (0-100%) |
| **Speaker Discovery** | Scan network for Google Nest/Home devices |
| **Test on Speaker** | Play a test adhan on any discovered speaker |
| **Preview** | Listen to the adhan in your browser |
| **SmartThings** | Configure Samsung Family Hub integration |

### Captive Portal (First Boot WiFi Setup)

If the Pi can't connect to any known WiFi network:

1. It creates a hotspot called **Bilal-Setup** (password: `bilal1234`)
2. Connect your phone to this hotspot
3. Open `http://192.168.4.1:5000` to configure WiFi credentials

```bash
# Manual hotspot control
./scripts/captive-portal.sh auto      # Auto-detect or start hotspot
./scripts/captive-portal.sh hotspot   # Force start hotspot
./scripts/captive-portal.sh stop      # Stop hotspot
```

## GitHub Actions CI/CD Setup

The included workflow (`.github/workflows/build-push.yml`) builds multi-arch images and pushes them to the GitHub Container Registry (GHCR). No external secrets are required — the workflow authenticates with the built-in `GITHUB_TOKEN`.

### How It Works

1. Push to `main` or create a version tag (`v1.0.0`)
2. Tests run; on success, GitHub Actions builds `linux/amd64` + `linux/arm64` images
3. Images are pushed to `ghcr.io/stmehmet/bilal-web` and `ghcr.io/stmehmet/bilal-scheduler`
4. Watchtower on the Pi automatically pulls the new images within 1 hour

### Pi authentication (private packages)

If the repo is private, GHCR packages are private by default. Authenticate the Pi once so Docker (and Watchtower) can pull:

1. Create a GitHub fine-grained personal access token with **Contents: Read** + **Packages: Read** scoped to this repo.
2. On the Pi:
   ```bash
   echo <PAT> | docker login ghcr.io -u stmehmet --password-stdin
   ```
   This writes `~/.docker/config.json`, which Watchtower reads.

Alternatively, mark each package as public from its GitHub package settings page — source stays private while artifacts become publicly pullable.

## Security Hardening

Run the hardening script on your Pi:

```bash
sudo ./scripts/harden.sh
```

This will:
- Lock the default `pi` user password
- Disable SSH password authentication (use SSH keys instead)
- Install automatic security updates
- Configure UFW firewall (allow ports 22 and 5000 only)

## Project Structure

```
bilal/
├── audio/                  # Place .mp3 adhan files here
├── scheduler/
│   ├── main.py             # Scheduler entry point
│   ├── adhan_scheduler.py  # Prayer time computation & job scheduling
│   ├── config.py           # Shared configuration (JSON on disk)
│   ├── discovery.py        # Chromecast mDNS discovery & playback
│   ├── geolocation.py      # IP-based location detection
│   ├── smartthings.py      # Samsung SmartThings API integration
│   └── requirements.txt
├── web/
│   ├── app.py              # Flask web dashboard
│   ├── templates/
│   │   ├── dashboard.html  # Main dashboard UI
│   │   └── login.html      # Authentication page
│   └── requirements.txt
├── scripts/
│   ├── captive-portal.sh   # WiFi hotspot setup
│   ├── harden.sh           # Pi security hardening
│   └── install.sh          # One-line installer
├── .github/workflows/
│   └── build-push.yml      # Multi-arch CI/CD pipeline
├── docker-compose.yml      # Service orchestration
├── Dockerfile              # Multi-stage build (web + scheduler)
└── .env.example            # Environment template
```

## License

MIT
