# Bilal – Home Adhan System

A production-ready, "giftable" Adhan system for Raspberry Pi 4 that automatically plays the call to prayer on Google Nest/Home speakers and Samsung SmartThings devices.

> **🏛 Understanding how it works?** Read [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — components, data flows, and the design decisions behind them (including why the web and scheduler containers run with host networking, why the scheduler uses an in-memory jobstore, and why we picked Tailscale + Watchtower + Nominatim).
>
> **📖 Gifting a Pi?** Follow the full step-by-step [Deployment Runbook](./docs/DEPLOYMENT-RUNBOOK.md) — it captures every gotcha from the first real-hardware deployment so you don't make the same mistakes twice.
>
> **🚀 Planning a DietPi swap?** See [DietPi Migration Plan](./docs/DIETPI-MIGRATION.md) for known differences and validation checklist.

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

- Raspberry Pi 4, Pi Zero 2W, or Pi 3B+ with Raspberry Pi OS Lite (64-bit)
- Docker and Docker Compose installed
- Adhan `.mp3` files placed in the `audio/` directory

### Installation

From a fresh Pi SSH session:

```bash
# Preconditions: git + curl + ca-certificates
sudo apt update && sudo apt install -y git curl ca-certificates

# Optional but recommended for remote access to gifted units
export TAILSCALE_AUTHKEY=tskey-auth-xxx

# Clone and install
git clone https://github.com/stmehmet/bilal.git ~/bilal
cd ~/bilal && ./scripts/install.sh
```

The installer will:

1. Install Docker + Compose plugin
2. Install Tailscale and join the tailnet (with `--ssh` for remote access)
3. Clone the repo to `~/bilal`
4. Generate a `.env` with a random `SECRET_KEY`
5. `docker compose pull && docker compose up -d`

If you've already cloned the repo, just run the installer directly:

```bash
cd ~/bilal
TAILSCALE_AUTHKEY=tskey-auth-xxx ./scripts/install.sh
```

### Access the Dashboard

Open `http://<pi-ip-address>:5000` in your browser. On first visit, you'll be asked to create a password.

## Gifting a Pi

The end-to-end recipe for assembling a gift unit:

1. **Flash the SD card** with Raspberry Pi OS 64-bit Lite using Raspberry Pi Imager. In advanced options, preconfigure:
   - Hostname: `bilal-<location>` (e.g. `bilal-home`)
   - Username / password
   - Your own public SSH key (optional — Tailscale SSH is the primary remote path)
   - Locale + timezone
2. **Mint a Tailscale auth key** on your workstation:
   - `TAILSCALE_AUTHKEY` — reusable, pre-authorized auth key tagged `tag:bilal-fleet` (one key works for the whole fleet). Create at https://login.tailscale.com/admin/settings/keys.
3. **Boot the Pi** and either:
   - Plug it into ethernet, or
   - Let the captive portal come up: connect your phone to the `Bilal-Setup` hotspot (password `bilal1234`), open `http://192.168.4.1:5000`, and enter the recipient's WiFi credentials.
4. **SSH in and run the installer**:
   ```bash
   ssh <user>@bilal-<name>.local
   export TAILSCALE_AUTHKEY=tskey-auth-xxx
   git clone https://github.com/stmehmet/bilal.git ~/bilal
   cd ~/bilal && ./scripts/install.sh
   ```
5. **Verify remote access** — the Pi should appear in your Tailscale admin at https://login.tailscale.com/admin/machines as `bilal-<machine-id-prefix>`. From your laptop, anywhere:
   ```bash
   tailscale ssh bilal-<machine-id-prefix>
   ```
   The dashboard is also reachable via MagicDNS: `http://bilal-<machine-id-prefix>:5000`.
6. **Harden** (optional but recommended before gifting):
   ```bash
   sudo ~/bilal/scripts/harden.sh
   ```
   Tailscale SSH continues to work because it bypasses OpenSSH password auth entirely.
7. **Hand it off.** Watchtower on the Pi pulls new images from GHCR within an hour of every merge to `main`, so you can ship fixes to the whole fleet without touching the hardware.

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

[PolyForm Noncommercial 1.0.0](./LICENSE) — free for personal and non-commercial use. Commercial use requires written permission from the author.
