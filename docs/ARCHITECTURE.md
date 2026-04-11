# Bilal — Architecture & Design Decisions

Technical reference for the system's components, data flows, and the design choices behind them. Read this if you're picking the project up cold, evaluating a change that spans multiple components, or trying to understand *why* something is the way it is.

For deployment mechanics, see [`DEPLOYMENT-RUNBOOK.md`](./DEPLOYMENT-RUNBOOK.md). For the DietPi-specific variant, see [`DIETPI-MIGRATION.md`](./DIETPI-MIGRATION.md).

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Components](#2-components)
3. [Data flows](#3-data-flows)
4. [Design decisions (ADR-lite)](#4-design-decisions-adr-lite)
5. [Security model](#5-security-model)
6. [Network topology](#6-network-topology)
7. [Technology stack](#7-technology-stack)
8. [Repository layout](#8-repository-layout)

---

## 1. System overview

Bilal is a **single-purpose appliance** that runs on a Raspberry Pi inside a private home, computes daily Islamic prayer times from the occupant's location, and plays the call-to-prayer (adhan) audio through Google Nest / Google Home / Chromecast speakers at the correct local moment. It has a small web dashboard for configuration and is designed to be **gifted**: the maintainer flashes a Pi, hands it to a family member, and can continue to push bug fixes and features remotely without the recipient ever touching the hardware.

The "gift-fleet model" is the organising principle. Every design choice has to survive the question:

> *What happens when this Pi is 2000 km away from me, behind a WiFi router I don't control, and something needs to change?*

Answering that question well is why we use Tailscale, Watchtower, container auto-updates, and a shared config volume. It's also why we avoid anything that requires interactive SSH on the recipient's network.

### High-level diagram

```
┌──────────────────────── Recipient's home ────────────────────────┐
│                                                                  │
│   ┌──────────── Raspberry Pi 4 / 5 ───────────────┐              │
│   │                                                │              │
│   │   Host OS (Pi OS Lite 64-bit)                  │              │
│   │   ├── NetworkManager  (WiFi, autoconnect)      │              │
│   │   ├── Tailscale       (tailnet membership)     │              │
│   │   └── Docker Engine   (container runtime)      │              │
│   │                                                │              │
│   │   ┌──── docker compose (host network) ────┐   │              │
│   │   │                                        │   │              │
│   │   │  ┌──────────┐    ┌────────────────┐   │   │              │
│   │   │  │bilal-web │    │bilal-scheduler │   │   │              │
│   │   │  │Flask+    │    │APScheduler +   │   │   │              │
│   │   │  │Gunicorn  │    │pychromecast    │   │   │              │
│   │   │  │:5000     │    │                │   │   │              │
│   │   │  └────┬─────┘    └──────┬─────────┘   │   │              │
│   │   │       │                 │              │  │              │
│   │   │       └───┬─────────────┘              │  │              │
│   │   │           │  bilal-data volume         │  │              │
│   │   │           │  /data/config.json         │  │              │
│   │   │           │  /data/auth.json           │  │              │
│   │   │                                        │   │              │
│   │   │  ┌─────────────────────────────────┐  │   │              │
│   │   │  │  bilal-watchtower                │  │   │              │
│   │   │  │  (polls GHCR hourly)             │  │   │              │
│   │   │  └─────────────────────────────────┘  │   │              │
│   │   └────────────────────────────────────────┘   │              │
│   │                                                │              │
│   └────────────────────────────────────────────────┘              │
│                         │                                        │
│        LAN (WPA2/WPA3)  │         mDNS                           │
│                         │        ┌─────────────┐                  │
│                         └────────│ Nest Mini    │                 │
│                                  │ Google Home  │                 │
│                                  │ Nest Hub     │                 │
│                                  └─────────────┘                  │
└──────────────────────────────────────────────────────────────────┘
         │                                      │
         │ Tailscale mesh (WireGuard)           │ Watchtower HTTPS poll
         │                                      │
         ▼                                      ▼
┌───────────────────┐                   ┌──────────────────┐
│ Maintainer's Mac  │                   │ ghcr.io          │
│ (tailscale ssh,   │                   │ stmehmet/        │
│  dashboard via    │                   │  bilal-web       │
│  MagicDNS)        │                   │  bilal-scheduler │
└───────────────────┘                   └──────────────────┘
                                                 ▲
                                                 │ GHA multi-arch build
                                                 │ on every push to main
                                                 │
                                        ┌─────────────────┐
                                        │ GitHub Actions  │
                                        │ stmehmet/bilal  │
                                        └─────────────────┘
```

---

## 2. Components

### 2.1. `bilal-web` (Flask dashboard, Gunicorn WSGI)

**Role:** Single-page dashboard for configuration and status. Serves audio files to Chromecast devices over HTTP.

**Tech:**
- Python 3.11, Flask, Flask-Login, Gunicorn
- Jinja2 templates (`web/templates/dashboard.html`, `login.html`)
- Tailwind CSS via CDN (`cdn.tailwindcss.com`), Phosphor icons via CDN
- Aref Ruqaa Google Font for the "Bilal" brand title

**Ports:**
- `5000` — HTTP dashboard + `/audio/<filename>` media endpoint (host network)

**State on disk:**
- `/data/config.json` — all dashboard settings
- `/data/auth.json` — bcrypt password hash (first-boot wizard)

**Key endpoints (see `web/app.py`):**

| Path | Method | Purpose |
|---|---|---|
| `/` | GET | Dashboard (renders `dashboard.html`) |
| `/login` | GET/POST | First-time password creation + sign-in |
| `/audio/<filename>` | GET | Serves `.mp3` files from `AUDIO_DIR` to Chromecast devices. Path-traversal hardened. |
| `/api/config` | GET/POST | Read or update `config.json` (validated) |
| `/api/detect-location` | POST | IP-based auto-detect via `ipapi.co` / `ipinfo.io` / `ip-api.com` fallback |
| `/api/geocode` | POST | Address-based forward geocoding via Nominatim + timeapi.io |
| `/api/discover-speakers` | POST | mDNS scan via `pychromecast` for Chromecast devices |
| `/api/speakers` | POST | Toggle individual speakers enabled/disabled |
| `/api/test-speaker` | POST | Plays a 1-shot test on the given speaker |
| `/api/audio/validate` | GET | Returns the list of configured audio files that don't exist on disk |
| `/api/prayer-times` | GET | Today's prayer times as ISO strings |
| `/api/wifi/*` | GET/POST | NetworkManager passthrough (degrades gracefully with 503 in-container) |

**Runs as:** non-root `bilal` user inside the container (Dockerfile `USER bilal`).

### 2.2. `bilal-scheduler` (APScheduler + pychromecast)

**Role:** Computes the day's prayer times from `config.json`, schedules a job for each vakit, and at each trigger time sends a Chromecast `LOAD` command to every enabled speaker.

**Tech:**
- Python 3.11, APScheduler (in-memory jobstore), adhanpy, pychromecast, pytz
- Host network mode (required for mDNS multicast)

**State on disk:**
- Reads `/data/config.json` on startup and re-reads whenever the file changes (checked every 30s by `_check_config_change`)
- **Does not** persist scheduler state — jobs are rebuilt from current config on every startup

**Lifecycle:**
1. Start up, read config
2. Compute today's 5 prayer times via `adhanpy.PrayerTimes(coordinates=..., calculation_method=...)`
3. Register one APScheduler job per upcoming prayer using a `DateTrigger` at the computed time
4. Register a daily reschedule job at 00:01 (recomputes tomorrow's times)
5. Register a config-watcher job on a 30-second interval that triggers a full reschedule if `config.json`'s mtime changed
6. Block in the APScheduler event loop until shutdown

**Job handler:** `trigger_adhan(prayer_name: str)` resolves the configured audio file, looks up enabled speakers + SmartThings targets, and dispatches playback.

**Runs as:** non-root `bilal` user inside the container.

### 2.3. `bilal-watchtower` (GHCR auto-update poller)

**Role:** Polls `ghcr.io/stmehmet/bilal-web:latest` and `ghcr.io/stmehmet/bilal-scheduler:latest` once an hour. On new image digest, stops the container, pulls the new image, starts a fresh container, removes the old image. This is how maintainer fixes reach gift units with zero manual intervention.

**Tech:** Go binary from `nickfedor/watchtower` (actively maintained fork of abandoned `containrrr/watchtower`).

**Configuration:**
- `WATCHTOWER_POLL_INTERVAL=3600` (1 hour)
- `WATCHTOWER_CLEANUP=true` (removes old image layers after update)
- `WATCHTOWER_LABEL_ENABLE=true` (only updates containers with `com.centurylinklabs.watchtower.enable=true` label)

**Auth:** Reads `/root/.docker/config.json` (mounted read-only) to authenticate to GHCR for private image pulls.

**Observed real-world update cycle:** 8 seconds of total downtime for a rolling restart of both bilal containers (measured on Pi 4B 4GB over WiFi).

### 2.4. Shared `bilal-data` volume

A Docker named volume mounted at `/data` in both `bilal-web` and `bilal-scheduler`. Contains:

- `config.json` — all user settings (location, calculation method, speakers, audio files, iqamah offsets, DND window, etc.)
- `auth.json` — bcrypt hash of the dashboard admin password
- `scheduler_jobs.db` — deprecated, not created by current code (used to hold the SQL jobstore before PR #14)

Using a named Docker volume instead of a bind mount means the data survives container recreation but can still be inspected from the host via `docker volume inspect bilal-data`. The config schema is intentionally flat JSON (not SQL) so it can be read and edited with any tool, including the `docker exec bilal-web python3 -c "..."` one-liners documented in the runbook.

### 2.5. Audio files (`./audio` bind mount)

A bind mount from the host's `~bilal/bilal/audio/` directory to `/audio` inside both containers, **read-only**. Audio files are baked into the Docker image during build AND also accessible from the host for adding new recordings via SCP / git pull.

File naming convention: `adhan_<prayer>_<muezzin>[_<maqam>].mp3`

- `<prayer>` — one of `fajr`, `dhuhr`, `asr`, `maghrib`, `isha`
- `<muezzin>` — camelCase slug, e.g. `rec1`, `rec2`
- `<maqam>` — optional, e.g. `saba`, `ussak`, `rast`, `segah`, `hicaz`

The `audio_display_label()` parser in `web/app.py` maps slugs to their Turkish orthography (`Recording 1`, `Uşşak`, etc.) via the `MUEZZIN_LABELS` and `MAQAM_LABELS` dictionaries. Unknown slugs fall through to a camelCase→Title Case conversion.

### 2.6. Host OS services (Pi OS Lite)

These run outside Docker, on the Raspberry Pi's host OS:

- **NetworkManager** — WiFi connection management. Multiple saved profiles with `autoconnect=true` allow a single Pi to travel between networks (runbook §9.1).
- **Tailscale** — Tailnet membership + Tailscale SSH. Installed as a system service via `tailscale up --authkey ... --ssh --hostname=bilal-<machine-id>` during `scripts/install.sh`. Provides the sole remote-access path; OpenSSH is still present but is not the primary channel.
- **Docker Engine** — containerd-based, installed via Docker's convenience script in `install.sh`.
- **systemd-journald** — log persistence (on DietPi this defaults to tmpfs, see migration doc).

---

## 3. Data flows

### 3.1. First-time setup flow

```
Fresh Pi boot
  │
  ▼
[Pi OS Lite first-boot]
  ├── cloud-init applies hostname, user, SSH key, WiFi from Pi Imager
  └── Rootfs auto-expands to fill SD card
  │
  ▼
[Maintainer SSHs in with classic GH PAT + Tailscale auth key]
  │
  ▼
[scripts/install.sh — 8 steps]
  1. apt install docker-ce + docker-compose-plugin
  2. Install Tailscale, `tailscale up --ssh --authkey=...`
  3. Clone repo (skipped if already cloned)
  4. `docker login ghcr.io` with GH_PAT
  5. Verify audio/ has at least one mp3
  6. Generate .env with random SECRET_KEY
  7. `docker compose pull` (pulls private images over authed GHCR)
  8. `docker compose up -d`
  │
  ▼
[First-boot dashboard visit at http://bilal-<id>:5000]
  │
  ▼
[Login page detects empty auth.json → "Create admin password"]
  │
  ▼
[Dashboard renders with empty config]
  ├── Auto-detect from IP (primary)
  ├── Look up by address (fallback)
  └── Manual coordinates (expert)
  │
  ▼
[User clicks Save Settings]
  │
  ▼
[/api/config POST → save_config() writes config.json]
  │
  ▼
[bilal-scheduler's config-watcher (30s interval) picks up the change]
  │
  ▼
[schedule_today() recomputes prayer times, registers new jobs]
  │
  ▼
[At each vakit time, trigger_adhan() fires]
```

### 3.2. Adhan playback flow

```
APScheduler wake-up at prayer time
  │
  ▼
trigger_adhan(prayer_name)
  ├── load_config()  # re-reads config.json  
  ├── Resolve audio file: config["adhan_audio_files"][prayer_name]
  ├── Get LAN IP (socket.gethostbyname or `ip route`)
  └── Build media URL: http://<lan-ip>:5000/audio/<filename>
  │
  ▼
For each enabled speaker in config["speakers"]:
  │
  ▼
pychromecast.get_chromecast_from_cast_info(speaker_info)
  ├── Opens TLS socket to <nest-ip>:8009
  ├── Cast protocol LAUNCH default media receiver
  └── Cast protocol LOAD with media URL + autoplay
  │
  ▼
Nest speaker fetches http://<lan-ip>:5000/audio/<filename>
  │
  ▼
bilal-web serves the file (send_from_directory, path-traversal hardened)
  │
  ▼
Speaker decodes and plays the adhan through its speaker
```

**Critical:** the adhan audio bytes **never** leave the LAN. Bilal just tells Nest *"go fetch this URL"*; Nest does the fetching and decoding locally. That's why Tailscale isn't in this path — the Pi and the Nest are both on the same WiFi, and the Nest wouldn't know how to reach a Tailscale IP.

### 3.3. Config save propagation

```
User edits field → clicks Save Settings
  │
  ▼
Frontend POST /api/config with entire payload
  │
  ▼
update_config() in web/app.py:
  ├── Validates each field (coordinate range, tz string, calculation method, etc.)
  ├── Merges into loaded config dict
  └── save_config() writes /data/config.json atomically
  │
  ▼
File mtime changes
  │
  ▼
(within 30 seconds)
  │
  ▼
bilal-scheduler's config_watcher job fires
  ├── config_changed_since(self._last_config_check) returns True
  └── schedule_today() is called
      ├── Remove all existing adhan jobs
      └── Recompute + re-register for the new config
  │
  ▼
Logged: "Config change detected, rescheduling prayers"
```

This is why the dashboard feels "live" — no explicit reload button needed. The 30-second polling is a tradeoff: lower latency would mean more file-system churn; higher latency would frustrate users during initial setup.

### 3.4. Auto-update flow (the gift-fleet value prop)

```
Maintainer pushes a commit to main
  │
  ▼
GitHub Actions (.github/workflows/build-push.yml)
  ├── Run tests (pytest)
  ├── Set up QEMU + Buildx
  ├── Build linux/amd64 + linux/arm64 images
  └── Push to ghcr.io/stmehmet/bilal-web:latest + bilal-scheduler:latest
  │
  ▼ (~3-5 minutes later)
New :latest digests live on GHCR
  │
  ▼ (within 1 hour)
bilal-watchtower wakes up (hourly interval from container start)
  │
  ▼
For each labelled container (web, scheduler, watchtower):
  ├── Check digest of :latest on GHCR vs running image
  ├── If different → pull new image
  ├── Stop old container (SIGTERM, 30s grace)
  ├── Start new container with same name, volumes, env
  └── Remove old image layers (WATCHTOWER_CLEANUP=true)
  │
  ▼
Log: "Update session completed: scanned=3 updated=N failed=0"
```

**Measured in real deployment:** 8 seconds of total downtime for both bilal containers rolling restart simultaneously. Web dashboard becomes unreachable for those 8 seconds; a prayer scheduled exactly during that window would miss. In practice, prayers are once every few hours and the 1-in-10800 odds of a collision are acceptable.

### 3.5. Remote access flow

```
Maintainer opens http://bilal-4ec1d7c1:5000 on their Mac
  │
  ▼
macOS DNS resolution
  ├── System checks /etc/resolver/ts.net → points at 100.100.100.100
  └── Tailscale daemon resolves MagicDNS name → Pi's tailscale IP
  │
  ▼
HTTP request goes out the Mac's tailscale0 interface
  │
  ▼
Tailscale encrypts with WireGuard + routes via DERP relay or direct
  ├── Usually direct UDP:41641 between endpoints
  └── Falls back to DERP (Tailscale's relay mesh) if NAT is strict
  │
  ▼
Request arrives on Pi's tailscale0 interface
  │
  ▼
Pi's host kernel routes to 0.0.0.0:5000 (bilal-web on host network)
  │
  ▼
Flask responds, response follows the same path back
```

**Critical:** Tailscale is an overlay; the recipient's router has no idea any of this is happening. No port forwarding, no DDNS, no firewall holes punched. Tailnet is its own firewall — anyone not logged into the tailnet simply cannot resolve the hostname or reach the IP.

---

## 4. Design decisions (ADR-lite)

Each decision follows a compact Context / Decision / Consequences / Alternatives structure. These document *why* things are the way they are so future maintainers don't revisit settled questions.

### 4.1. Docker containers instead of native Python services

**Context:** We need scheduler + web + auto-update + reproducible environment on a Pi that might be maintained by someone who has never touched Python.

**Decision:** Run everything in Docker containers managed by `docker-compose.yml`. Build multi-arch images in CI and push to GHCR.

**Consequences:**
- ✅ Installer becomes `docker compose up -d` after one-time Docker install
- ✅ Auto-updates are trivial (pull new image, restart)
- ✅ Python dependencies are frozen per-image, no venv drift
- ✅ Non-root container user adds a thin security layer
- ✅ Watchtower exists specifically for this pattern
- ❌ ~100 MB Docker daemon overhead on a resource-constrained board
- ❌ Host networking means the container isolation benefit is partial
- ❌ Pi OS Lite default cgroup memory disabled means memory limits are silently ignored

**Alternatives considered:** systemd units with virtualenvs per service. Simpler install footprint but no auto-update mechanism, harder to roll back, and the "every host has a different Python version" problem surfaces.

### 4.2. Host networking for `bilal-web` and `bilal-scheduler`

**Context:** `pychromecast` discovers Nest speakers via mDNS multicast on `224.0.0.251:5353`. Docker's default bridge network **does not forward multicast traffic**, so a bridged container will always see zero speakers regardless of what's on the WiFi.

**Decision:** Both `web` and `scheduler` run with `network_mode: host`. Bridge networks are removed from `docker-compose.yml` entirely.

**Consequences:**
- ✅ mDNS scanning works
- ✅ `/audio/<filename>` is served directly on the host's port 5000
- ✅ Scheduler can make outbound TLS connections to port 8009 on Nest devices
- ❌ No port isolation between containers
- ❌ If two containers wanted to bind port 5000, they'd collide
- ⚠️ This effectively means every process in web and scheduler runs on the host network namespace — acceptable for a single-purpose appliance but would not fly on a shared host

**Alternatives considered:**
1. **macvlan network** — allows multicast but requires host interface in promiscuous mode and doesn't work on WiFi interfaces without additional setup.
2. **Running an mDNS reflector** (like `avahi-daemon` in reflector mode) on the host and keeping containers on bridge — adds operational complexity and still requires opening ports.
3. **Running only the scheduler on host** and having web make an RPC call to it for discovery — doubles the surface area (two services need to expose APIs to each other) for no benefit.

Host networking wins by simplicity. Documented in `docker-compose.yml` comments.

### 4.3. In-memory APScheduler jobstore instead of SQL persistence

**Context:** An earlier version used `SQLAlchemyJobStore` to persist scheduled jobs across restarts. This caused `bilal-scheduler` to crash on startup with `TypeError: Schedulers cannot be serialized`.

**Root cause:** `SQLAlchemyJobStore` serializes every job it stores. The daily reschedule job and the config-watcher job target *bound methods* (`self.schedule_today`, `self._check_config_change`) on `AdhanSchedulerService`. Serializing a bound method also serializes its `self`, which holds `self.scheduler`, which APScheduler's base `Scheduler.__getstate__()` explicitly refuses with that exact error.

**Decision:** Drop `SQLAlchemyJobStore`. Use APScheduler's default `MemoryJobStore`. On every container start, `start()` calls `schedule_today()` which recomputes all prayer times from the current `config.json` and registers fresh jobs.

**Consequences:**
- ✅ No startup crash
- ✅ Simpler code (no SQL dependency in scheduler)
- ✅ Jobs are always consistent with the current config — no stale "yesterday's prayer" jobs
- ❌ A prayer that fires exactly during a container restart might miss (the container is down when the scheduled time passes). In practice this is ~8 seconds per update cycle per day, so the odds of missing a prayer are negligible.

**Alternatives considered:**
1. **Keep SQL but use module-level function references** — would work but requires rewriting every bound method call as a free function, losing the encapsulation of `AdhanSchedulerService`.
2. **Keep SQL and skip the bound-method jobs** — leaves the daily reschedule and config watcher unpersisted anyway, so there's no win.

The persistent store wasn't buying us anything. This is documented in the `__init__` comment of `AdhanSchedulerService`.

### 4.4. Watchtower fork (`nickfedor/watchtower`) instead of `containrrr/watchtower`

**Context:** The originally-chosen `containrrr/watchtower` image crashed with `client version 1.25 is too old. Minimum supported API version is 1.40`.

**Root cause:** `containrrr/watchtower` has been effectively abandoned since ~2022 and still ships an old moby client that speaks Docker Engine API v1.25. Docker Engine 25+ refuses API versions below 1.40.

**Decision:** Switch to `nickfedor/watchtower`, the actively maintained community fork. Drop-in replacement.

**Consequences:**
- ✅ Speaks modern Docker API (v1.51 on our Pi 4 running Docker 29)
- ✅ Same labels, same env vars, same volume mounts — no config changes
- ✅ Actively maintained, so future Docker API changes should be supported
- ❌ Slightly larger image (~8 MB vs ~6 MB — irrelevant)

**Alternatives considered:** Writing a cron job that runs `docker compose pull && docker compose up -d` hourly. Simpler but loses the zero-downtime rolling restart behaviour and requires external monitoring.

### 4.5. Tailscale for remote access

**Context:** The recipient doesn't want to — and shouldn't have to — reconfigure their home router for port forwarding. The maintainer needs remote SSH and remote HTTP access to the dashboard for debugging and future updates.

**Decision:** Install Tailscale as a host service during `install.sh`. Use `tailscale up --ssh` to enable Tailscale SSH (bypasses OpenSSH, authenticates against tailnet identity). Gift units are tagged `tag:bilal-fleet` so the maintainer's tailnet ACL can grant them admin access without promoting them to full devices.

**Consequences:**
- ✅ Works behind any NAT, any router, any ISP — no configuration required on the recipient's side
- ✅ Tailnet membership itself acts as a firewall — gift units are not reachable from the public internet
- ✅ MagicDNS (`bilal-<machine-id>`) is stable across IP changes — the maintainer's Mac always reaches the Pi at the same hostname regardless of which WiFi the Pi is on
- ✅ Tailscale SSH handles host key management, so no `~/.ssh/known_hosts` churn when a Pi is reflashed
- ❌ Adds an external dependency (Tailscale must stay up and honour free-tier limits)
- ❌ Adds a ~40 MB daemon on the host
- ❌ Uses ~2% CPU on the Pi 4 at idle (measured)

**Alternatives considered:**
1. **Dynamic DNS + port forwarding** — requires router config on the recipient's side, brittle when they get a new router, and exposes OpenSSH to the public internet.
2. **ZeroTier** — similar to Tailscale, comparable trade-offs. Tailscale chosen for better Mac/Linux tooling and Tailscale SSH specifically.
3. **WireGuard directly** — more config, no MagicDNS, no SSH identity integration.
4. **Cloudflare Tunnel** — requires a Cloudflare account, ties the project to Cloudflare's pricing changes.

### 4.6. Shared `bilal-data` volume with flat JSON config

**Context:** Both `web` and `scheduler` need to read the user's configuration. `web` needs to write it when the user saves settings. `scheduler` needs to detect the change and reschedule.

**Decision:** A single Docker named volume `bilal-data` mounted at `/data` in both containers. All persistent state lives in `/data/config.json` as flat JSON.

**Consequences:**
- ✅ Simple — one file, one format, inspectable with any JSON tool
- ✅ `docker exec bilal-web python3 -c "..."` one-liners work for surgical debugging (we used this to clean up stale speakers)
- ✅ Config watcher is a trivial mtime check every 30s
- ✅ No schema migration infrastructure needed
- ❌ No transactional integrity — a crash mid-write could corrupt the file
- ❌ No schema validation at the file level (only validated at the `/api/config` boundary)
- ❌ Concurrent writes from both containers would race (mitigated because only `web` writes)

**Alternatives considered:**
1. **SQLite** — would give us transactions and a schema, but adds a dependency and makes `docker exec` debugging harder.
2. **Redis/Postgres** — massive overkill for a single-user single-host appliance.

Flat JSON is the right fit for this size of deployment.

### 4.7. Nominatim + timeapi.io for address-based location lookup

**Context:** The original IP-based auto-detect works but sometimes returns the wrong city when the user is on a VPN, Tailscale exit node, or when their ISP geolocates them to another city. Users needed a fallback to enter a known address.

**Decision:** Add a `/api/geocode` endpoint that calls OpenStreetMap's Nominatim (free, no API key, respects rate limits) to convert an address to coordinates, then a second call to timeapi.io (free, no key) to resolve IANA timezone from those coordinates.

**Consequences:**
- ✅ No API key management
- ✅ No cost
- ✅ Nominatim is comprehensive worldwide (entire OSM dataset)
- ✅ Privacy-respecting (no tracking, no account required)
- ❌ Nominatim's free tier has a 1 req/sec rate limit (trivial for our usage)
- ❌ Nominatim's `User-Agent` policy requires identifying the app (done)
- ❌ Free-form search sometimes misses specific street addresses (fixed in PR #17 with a fallback cascade)

**Alternatives considered:**
1. **Google Maps Geocoding API** — reliable but requires API key, costs money, has quota.
2. **Mapbox** — same, requires API key.
3. **Offline geocoding with a library like `geopy`** — would require bundling geographic data or a massive database.
4. **Structured Nominatim queries** (`street=X&city=Y&country=Z` instead of `q=...`) — more reliable but requires the user to fill in multiple fields.

The free-form + cascade-on-failure approach (PR #17) gives us the best UX: one field, usually works, gracefully degrades on failure.

### 4.8. Filename-encoded maqam/muezzin metadata with Turkish display

**Context:** The audio collection includes recordings in five traditional Ottoman maqams (Saba, Uşşak, Rast, Segâh, Hicaz), one per vakit, from multiple muezzins. We need to show these in the UI with proper Turkish orthography (Recording 1, Recording 2) without adding a database layer.

**Decision:** Encode metadata directly in the filename: `adhan_<prayer>_<muezzin>_<maqam>.mp3`. Keep filenames ASCII so they work cleanly in URLs and shell commands. At render time, parse the filename with `audio_display_label()` in `web/app.py` and look up Turkish labels via `MUEZZIN_LABELS` and `MAQAM_LABELS` dicts.

**Consequences:**
- ✅ No database
- ✅ Adding new recordings is `git add audio/adhan_... && commit` — no config changes needed
- ✅ Turkish diacritics render correctly in the UI while filenames stay portable
- ✅ Unknown muezzins or maqams gracefully fall back to camelCase→Title Case
- ✅ The per-prayer filter (PR #16) uses filename parsing directly — no extra metadata needed
- ❌ If someone adds a file with an unusual naming scheme, the label defaults to the filename stem
- ❌ No support for multiple languages yet (labels are hardcoded to Turkish transliterations)

**Alternatives considered:**
1. **Side-car JSON metadata file per audio** — more flexible but requires parallel maintenance of filenames and metadata, easy to forget.
2. **Database table** — overkill for ~10 audio files.
3. **ID3 tags in the mp3 itself** — works but adds mutagen/eyed3 dependency and makes files harder to regenerate.

Filename encoding is the right amount of structure for this scale.

### 4.9. First-time password stored as bcrypt hash in `auth.json`

**Context:** The dashboard must be gated by a password (gift recipients should be the only ones who can change settings on their unit), but we don't want a user-account database or any external identity provider.

**Decision:** On first visit, prompt for a password, hash it with Werkzeug's `generate_password_hash` (bcrypt under the hood), store the hash in `/data/auth.json`. On subsequent visits, verify via `check_password_hash`. Session stored in a Flask-Login cookie signed by `SECRET_KEY` from `.env`.

**Consequences:**
- ✅ Zero external dependencies
- ✅ No user management UI needed (single admin user)
- ✅ Password recovery is "SSH in and delete `/data/auth.json`, then hit the dashboard for a fresh setup"
- ❌ No multi-user support (fine for gift appliance)
- ❌ `SECRET_KEY` rotation invalidates all sessions (acceptable on a single-user device)

**Alternatives considered:**
1. **OAuth/Google Sign-In** — overkill, requires account setup, breaks offline use.
2. **Passkey/WebAuthn** — would be nice but adds significant complexity.
3. **No password** — considered for a while; rejected because the dashboard is reachable over LAN to anyone on the WiFi.

### 4.10. Push directly to `main` for solo-maintained repo

**Context:** This project has a single maintainer. PR workflow overhead (branch → push → open PR → wait for CI → merge → delete branch) is pure friction for solo hotfixes.

**Decision:** For small fixes under ~50 lines, push directly to `main`. Use PRs for larger features or anything that should be reviewable later. Watchtower picks up both flows identically.

**Consequences:**
- ✅ Fast iteration for small fixes (the city/timezone save fix was a single commit pushed to main)
- ✅ Still use PRs for things like the geocode cascade (PR #17) where review is valuable
- ❌ Loses CI signal on the change before it hits main — we rely on local tests instead
- ❌ No formal code review step — fine for a solo project, would need to change if a collaborator joins

This is a project-phase decision, not a permanent one. When a second maintainer joins, flip to PR-required.

---

## 5. Security model

### Threat model

Bilal is a **single-user appliance in a private home**. We're defending against:

1. **Random scanners on the recipient's WiFi** finding the dashboard and poking at it
2. **Malicious guests** on the WiFi (same attack surface, different motivation)
3. **The internet at large** if the router is ever misconfigured with port forwarding
4. **Credential leakage** from the maintainer's machine (GH_PAT, Tailscale auth key)

We are **not** defending against:
- Physical access to the SD card (anyone who can pull the card can read `/data/config.json`)
- Arbitrary code execution inside a container (host network mode means escape would be serious)
- Long-lived compromised containers (we rely on Watchtower to push patches quickly)

### Defense layers

1. **Tailnet membership as the outermost firewall** — the Pi's Tailscale IP is unreachable from the public internet. Only devices logged into the maintainer's tailnet can resolve `bilal-<machine-id>` or reach `100.83.161.42`.

2. **Dashboard password** — bcrypt-hashed, stored in `/data/auth.json`. Required to access every `/api/*` endpoint except `/login`. Brute force protected via in-memory rate limiter (5 attempts per 5 minutes per IP).

3. **Path-traversal hardening on `/audio/<filename>`** — rejects filenames with `/`, `\`, or any extension other than `.mp3`. Served via `send_from_directory` which does its own validation.

4. **Input validation on `/api/config`** — coordinate ranges, valid IANA timezones, whitelisted calculation methods, bounded string lengths on city/country fields.

5. **Container non-root user** — both web and scheduler run as `bilal` (UID ~999), not root. Limits blast radius if a Flask endpoint is compromised.

6. **GHCR authentication with least-privilege PAT** — the classic PAT used for `docker login ghcr.io` has `repo` + `read:packages` scopes only. No write access to the repo from the Pi. (TODO: migrate to a dedicated machine-user PAT before shipping to more recipients.)

7. **Tailscale ACL tagging** — gift units are tagged `tag:bilal-fleet` and the ACL only grants `autogroup:admin` SSH access to them. If a recipient ever joined the tailnet themselves, they still couldn't SSH into each other's units.

### Known gaps and accepted risks

- `/root/.docker/config.json` contains the GHCR PAT in base64. Anyone with host root on the Pi can read it. Mitigated by: tailnet-only access + Tailscale SSH requiring admin tag grant.
- `SECRET_KEY` is generated once per install in `.env` and persists on the bind mount. If the volume is compromised, all Flask sessions on that Pi can be forged. Mitigated by: session only grants dashboard access, which is already gated by the password.
- The Chromecast cast protocol is unauthenticated on the LAN. Any device on the WiFi can push audio to a Nest speaker. This is a property of Chromecast, not Bilal.
- Watchtower pulls any new digest published under the `:latest` tag. A compromise of the GHCR push credential would push malicious images to the entire fleet. Mitigated by: `GITHUB_TOKEN` in GHA is scoped per-workflow, never persisted, and can't be used to push without the workflow running.

---

## 6. Network topology

```
┌────────────── Public internet ──────────────┐
│                                             │
│  ghcr.io            GitHub                  │
│  (pulls only)       (pushes via GHA)        │
│                                             │
└─────────┬────────────────────┬──────────────┘
          │                    │
          │ HTTPS              │ HTTPS (only from GHA runner)
          │                    │
┌─────────▼────────────────────▼──────────────┐
│                                             │
│          Tailscale control plane            │
│          (authentication + DERP relay)      │
│                                             │
└─────────┬────────────────────┬──────────────┘
          │                    │
          │ WireGuard overlay  │
          │                    │
┌─────────▼────────┐     ┌─────▼─────────┐
│                  │     │                │
│   Maintainer     │     │   Gift Pi      │
│   Mac            │     │   in recipient │
│   100.x.x.x      │     │   home         │
│                  │     │   100.x.x.x    │
│                  │     │                │
└──────────────────┘     └───┬────────────┘
                             │
                     ┌───────┼───────────────┐
                     │   Recipient's LAN     │
                     │   (192.168.x.x)       │
                     │       │               │
                     │       │               │
                     │  ┌────┴───────┐       │
                     │  │ Nest Mini  │       │
                     │  │ Google Home│       │
                     │  │ Nest Hub   │       │
                     │  └────────────┘       │
                     │                       │
                     └───────────────────────┘
```

**Three distinct network planes:**

1. **Public internet** — used only for GHCR image pulls (inbound to Pi) and GitHub webhook/push (inbound to GHA runners, never to Pi)
2. **Tailnet overlay (WireGuard)** — used for all maintainer ↔ Pi traffic (SSH, HTTP dashboard, log inspection). Not routable from outside the tailnet.
3. **Recipient's LAN** — used for Pi ↔ Nest speakers (mDNS + Chromecast LOAD + HTTP audio serving). Never crosses out of the LAN.

The separation is deliberate: the maintainer never needs to touch the recipient's LAN, and the Nest speakers never need to be exposed beyond the LAN.

---

## 7. Technology stack

| Layer | Choice | Rationale |
|---|---|---|
| Host OS | Raspberry Pi OS Lite 64-bit (Debian Bookworm/Trixie) | Official, stable, supports Docker cleanly, has NetworkManager, ample community knowledge |
| Container runtime | Docker Engine + Compose plugin | Standard, well-supported, works with Watchtower |
| Reverse proxy | none | Single-tenant appliance, no need for TLS termination |
| WSGI server | Gunicorn | Production-grade, handles worker recycling, avoids Flask dev server |
| Web framework | Flask | Lightweight, mature, excellent Jinja2 templating, Flask-Login for auth |
| Frontend framework | Tailwind CSS via CDN | No build step, no npm, readable HTML templates |
| Icon library | Phosphor Icons via CDN | Modern, extensive, MIT licensed |
| Font | Aref Ruqaa via Google Fonts | Arabic-style Latin font for the "Bilal" brand title, loads once |
| Prayer time library | `adhanpy` | Pure Python, 13+ calculation methods, actively maintained |
| Chromecast library | `pychromecast` | De facto standard, mDNS discovery + Cast protocol LOAD |
| Scheduler | APScheduler (in-memory jobstore) | Solid Python scheduler, no external deps, supports cron + interval + date triggers |
| Auto-update | `nickfedor/watchtower` | Active fork of abandoned containrrr, supports Docker API v1.40+ |
| Remote access | Tailscale + Tailscale SSH | Zero-config mesh VPN, MagicDNS, no router reconfig needed |
| Geocoding | OpenStreetMap Nominatim | Free, no API key, worldwide coverage, privacy-respecting |
| Timezone-from-coords | timeapi.io | Free, no API key, IANA names |
| CI/CD | GitHub Actions (buildx multi-arch) | Integrates with GHCR, free for public/private repos on free tier |
| Image registry | GHCR (ghcr.io/stmehmet) | Tied to repo, uses `GITHUB_TOKEN` in CI, classic PAT on Pi for pulls |
| State persistence | Flat JSON on Docker named volume | Simple, inspectable, no schema migration |
| Password hashing | Werkzeug `generate_password_hash` (bcrypt) | Standard, well-tested, no extra deps |

---

## 8. Repository layout

```
bilal/
├── README.md                     # Project intro + quick start
├── SESSION-NOTES.md              # Local scratch (gitignored)
├── VERSION                       # Version string read by web/app.py
├── .env.example                  # Template for SECRET_KEY etc
├── .gitignore                    # See standards at ~/coding/.claude/rules/
├── .claudeignore                 # Excludes lockfiles, audio bin, .venv
│
├── audio/                        # MP3 files, bind-mounted into containers
│   └── adhan_<prayer>_<muezzin>_<maqam>.mp3
│
├── docs/
│   ├── ARCHITECTURE.md           # This file
│   ├── DEPLOYMENT-RUNBOOK.md     # Step-by-step gift unit bring-up
│   └── DIETPI-MIGRATION.md       # Future DietPi swap plan
│
├── scheduler/                    # bilal-scheduler container source
│   ├── main.py                   # Entry point — instantiates AdhanSchedulerService
│   ├── adhan_scheduler.py        # Core: compute_prayer_times, schedule_today, trigger_adhan
│   ├── config.py                 # Load/save/watch config.json
│   ├── discovery.py              # pychromecast scanning + Cast protocol LOAD
│   ├── geolocation.py            # IP detect + Nominatim address lookup
│   ├── smartthings.py            # Samsung Family Hub integration (optional)
│   └── requirements.txt
│
├── web/                          # bilal-web container source
│   ├── app.py                    # Flask app, all /api/* endpoints
│   ├── static/
│   │   └── minaret-100.png       # Favicon + dashboard logo
│   ├── templates/
│   │   ├── dashboard.html        # Main UI
│   │   └── login.html            # First-boot password + sign-in
│   └── requirements.txt
│
├── scripts/
│   ├── install.sh                # One-shot Pi installer (Docker + Tailscale + compose)
│   ├── captive-portal.sh         # WiFi hotspot for first-boot setup
│   └── harden.sh                 # Optional: lock down SSH, UFW, unattended-upgrades
│
├── tests/                        # pytest suite
│   ├── conftest.py               # Fixtures (logged_in_client, sample_config)
│   ├── test_config.py
│   ├── test_prayer_times.py
│   ├── test_validation.py
│   └── test_web.py
│
├── .github/workflows/
│   └── build-push.yml            # Multi-arch buildx → GHCR
│
├── docker-compose.yml            # Orchestrates web + scheduler + watchtower
└── Dockerfile                    # Multi-stage: base → scheduler target / web target
```

### Key files to understand before making changes

| File | When to read it |
|---|---|
| `docker-compose.yml` | Before any deployment change — comments explain host networking and watchtower fork |
| `scheduler/adhan_scheduler.py` | Before touching scheduling logic — comments explain the in-memory jobstore choice |
| `web/app.py` | Before touching config handling, auth, or geocoding |
| `web/templates/dashboard.html` | Before touching the UI |
| `scheduler/config.py` | Before changing the config schema or default values |
| `docs/DEPLOYMENT-RUNBOOK.md` | Before deploying a new unit |
| This file | Before making a change that spans multiple components |
