# Bilal Deployment Runbook

Step-by-step guide for taking a Raspberry Pi from bare hardware to a fully-configured, gift-ready Bilal unit. Written after the first real-hardware deployment so every non-obvious gotcha is captured.

> **Target audience:** you (future self) preparing another Pi for a family member, or the next maintainer picking this project up cold.

---

## Table of contents

1. [Prerequisites & credentials](#1-prerequisites--credentials)
2. [Tailscale ACL — one-time tailnet setup](#2-tailscale-acl--one-time-tailnet-setup)
3. [Flash the SD card](#3-flash-the-sd-card)
4. [First boot & find the Pi's IP](#4-first-boot--find-the-pis-ip)
5. [Run the installer](#5-run-the-installer)
6. [Post-install fixes](#6-post-install-fixes)
7. [First-time dashboard setup](#7-first-time-dashboard-setup)
8. [Verification](#8-verification)
9. [Preparing a gift unit for a different location](#9-preparing-a-gift-unit-for-a-different-location)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites & credentials

### Hardware

| Item | Notes |
|------|-------|
| Raspberry Pi 4, Pi Zero 2W, or Pi 3B+ | 1 GB+ RAM. Pi 5 also works. Pi Zero 2W ($15) is the recommended fleet unit. |
| microSD card, **16 GB minimum** | Docker + images alone needs ~4 GB. **4 GB cards fail with "No space left on device" during the Tailscale install** — don't try to save money here. 32 GB or 64 GB is safest. |
| Power supply (USB-C, 5V/3A) | Official Pi supply is ideal; underpowered supplies cause cryptic filesystem errors. |
| Ethernet cable (optional) | Only needed for first-boot before WiFi joins; can skip if you preconfigure WiFi in Pi Imager. |

### Workstation tools

- **Raspberry Pi Imager** (https://www.raspberrypi.com/software/)
- A **terminal with ssh + tailscale CLI** (Mac: Tailscale app installed, or `brew install tailscale`)
- An **SSH keypair** in `~/.ssh/id_ed25519[.pub]`. If you don't have one:
  ```bash
  ssh-keygen -t ed25519 -C "you@example.com" -f ~/.ssh/id_ed25519
  ```

### Tailscale auth key (`TAILSCALE_AUTHKEY`)

1. Go to https://login.tailscale.com/admin/settings/keys
2. Click **Generate auth key**
3. Toggle **Reusable** (so one key can be used across all gift units)
4. Toggle **Pre-approved**
5. Add **Tags**: `tag:bilal-fleet`
6. Copy the `tskey-auth-...` string.

---

## 2. Tailscale ACL — one-time tailnet setup

Before you flash the first gift unit, your Tailscale ACL needs to allow admin SSH into `tag:bilal-fleet`. Tailscale's default `ssh` block uses `autogroup:self` as destination, but tagged devices have no owner so `autogroup:self` never matches — you'd see `access denied by tailnet policy` when trying to `tailscale ssh` in.

Edit the ACL at https://login.tailscale.com/admin/acls to this (or merge with your existing rules):

```hujson
{
    "grants": [
        {
            "src": ["*"],
            "dst": ["*"],
            "ip":  ["*"],
        },
    ],
    "ssh": [
        // Default: members can SSH into their own (non-tagged) devices in check mode.
        {
            "action": "check",
            "src":    ["autogroup:member"],
            "dst":    ["autogroup:self"],
            "users":  ["autogroup:nonroot", "root"],
        },
        // Bilal gift-fleet: admins can SSH into any Pi in the fleet without re-auth.
        {
            "action": "accept",
            "src":    ["autogroup:admin"],
            "dst":    ["tag:bilal-fleet"],
            "users":  ["bilal", "root"],
        },
    ],
    "tagOwners": {
        "tag:bilal-fleet": ["autogroup:admin"],
    },
}
```

Click **Save**. This is a one-time step that applies to every future Bilal unit in the tailnet.

---

## 3. Flash the SD card

Open **Raspberry Pi Imager**, click **Edit Settings**, then:

### Operating System

- **Raspberry Pi OS Lite (64-bit)** — Debian Bookworm or Trixie. Lite because we don't need a desktop, 64-bit because Docker images are built for `linux/arm64`.

### Storage

- Select the inserted SD card.

### Advanced settings (gear icon)

| Field | Value |
|-------|-------|
| Set hostname | `bilal-<location>` — e.g. `bilal-dev`, `bilal-home`, `bilal-office` |
| Set username and password | Username: `bilal` (matches the ACL and docs throughout). Password: anything; you'll rarely type it because Tailscale SSH bypasses OpenSSH. |
| Configure wireless LAN | Enter **your own WiFi** so the Pi joins the network on first boot. You'll add the recipient's network later via `nmcli`. |
| Wireless LAN country | Your country code |
| Set locale / timezone | Your local settings (you can change timezone later per-location) |
| Enable SSH | Use public-key authentication only, paste contents of `~/.ssh/id_ed25519.pub` |

Click **Save** and write the image to the card. This takes ~5–10 minutes.

---

## 4. First boot & find the Pi's IP

1. Insert the SD card into the Pi.
2. Plug in power. The green ACT LED will blink heavily for the first 60–90 seconds while the rootfs auto-expands and cloud-init applies your settings.
3. Wait for the Pi to join WiFi. On Google WiFi / Nest WiFi networks, you may see a **new device notification in the Google Home app** (`bilal-<name>` joined). That's the confirmation.

### Finding the IP

Preferred:

```bash
ping -c 3 bilal-<name>.local
```

If mDNS is blocked (Google WiFi mesh sometimes drops `_workstation._tcp` announcements between mesh nodes), fall back to:

- **Google Home app** → Wi-Fi → Devices → find `bilal-<name>` → note the IP
- Your router's DHCP lease table (login page of the gateway)
- `arp -a | grep -i 'b8:27:eb\|dc:a6:32\|e4:5f:01'` — common Pi MAC prefixes

Once you have the IP, confirm you can reach it over OpenSSH (not Tailscale yet — Tailscale isn't installed):

```bash
ssh bilal@<lan-ip>
# You should land at: bilal@bilal-<name>:~ $
```

---

## 5. Run the installer

Still in the SSH session from the previous step.

```bash
# Preconditions — git and curl may or may not be preinstalled on Pi OS Lite
sudo apt update && sudo apt install -y git curl ca-certificates

# Optional but recommended for remote access
export TAILSCALE_AUTHKEY=tskey-auth-your-reusable-key

# Clone the repo and run the installer
git clone https://github.com/stmehmet/bilal.git ~/bilal
cd ~/bilal && ./scripts/install.sh
```

The installer runs 7 steps:

1. Install Docker Engine + Compose plugin (takes ~3–5 min on a Pi 4, longer on Pi Zero 2W)
2. Install Tailscale
3. `tailscale up --authkey <key> --ssh --hostname=bilal-<machine-id>`
4. Clone the repo to `~/bilal` (skipped because you already cloned it)
5. Verify `audio/` has at least one mp3 file
6. Generate `.env` with a random `SECRET_KEY`
7. `docker compose pull && docker compose up -d`

Expected final output:

```
[bilal-install] ==========================================
[bilal-install] Bilal is running!
[bilal-install] LAN:       http://<lan-ip>:5000
[bilal-install] Tailscale: http://<tailscale-ip>:5000
[bilal-install] MagicDNS:  http://bilal-<machine-id>:5000
[bilal-install] ==========================================
```

Note the MagicDNS hostname — you'll use it for every future remote access.

---

## 6. Post-install fixes

Two one-time cleanups before the system is actually usable.

### 6.1. Enable cgroup memory (optional but recommended)

Pi OS Lite disables cgroup memory accounting by default, which means the `memory: 256M` limits in `docker-compose.yml` are silently ignored. To enable them:

```bash
sudo sed -i 's/$/ cgroup_memory=1 cgroup_enable=memory/' /boot/firmware/cmdline.txt
sudo reboot
```

After the reboot (~30s), `tailscale ssh bilal@bilal-<machine-id>` back in and continue.

---

## 7. First-time dashboard setup

From your Mac browser, open:

```
http://bilal-<machine-id>:5000
```

Tailscale's MagicDNS resolves this regardless of whether the Pi is on your LAN or somewhere else on the internet.

1. **Create admin password** (first-visit only). Pick something strong — at least 8 characters. This gates the dashboard forever after.
2. **Location** — three paths, in order of preference:
   - **Auto-detect from IP** (primary): clicks once, reads your public IP, fills lat/lon/city/country/timezone. Good for local setup.
   - **Wrong location? Look up by address** (fallback when IP is wrong — VPN, Tailscale exit node, or ISP geolocating you to another city): expand the collapsible, type a free-text address (e.g. a city name), click **Look Up**. Status line shows the resolved city/country/timezone/coordinates. Uses OpenStreetMap's Nominatim + timeapi.io. Free, no API keys.
   - **Manual coordinates** (for when both fail, or you want exact control): type lat/lon in the numeric fields. Also fill in the City, Country, and Timezone text fields so the dashboard header matches. _Leaving these blank means the old values persist — this was a bug fixed in commit `c71306e`._
3. **Calculation Method** — pick `ISNA` (North America default), `UmmAlQura` (Makkah), `MuslimWorldLeague` (Europe default), or whatever your community uses.
4. **Per-prayer adhan audio** — each vakit dropdown is filtered to show only the recordings that match that prayer's traditional Ottoman maqam:
   - Fajr: Saba 1, Saba 2
   - Dhuhr: Uşşak 1, Uşşak 2
   - Asr: Rast 1, Rast 2
   - Maghrib: Segâh 1, Segâh 2
   - Isha: Hicaz 1, Hicaz 2
5. **Volume** — start at 50% and adjust after the first real adhan plays.
6. **Click Save Settings.** Page reloads with the new prayer times.

### 7.1. Speakers

1. Click **Discover Speakers**. The scheduler runs an mDNS scan via `pychromecast` and returns every Nest Mini / Google Home / Chromecast-capable device on the LAN (discovery requires the containers to be on `network_mode: host` — both web and scheduler are, see section 10).
2. Check the boxes next to the speakers you want the adhan to play on.
3. Click **Test** on any speaker to play a short adhan preview and verify playback works.
4. Click **Save Settings** again.

### 7.2. Optional: iqamah

Currently commented out from the UX but the plumbing is all in place. If you add `iqamah_<name>.mp3` files to `audio/`, the dashboard will pick them up in the iqamah dropdown.

---

## 8. Verification

Before shipping a gift unit, walk this checklist:

### 8.1. Core services healthy

```bash
tailscale ssh bilal@bilal-<machine-id>
cd ~/bilal && docker compose ps
```

All three containers should show `Up X seconds (healthy)`:

```
NAME               STATUS
bilal-web          Up 5 minutes (healthy)
bilal-scheduler    Up 5 minutes (healthy)
bilal-watchtower   Up 5 minutes (healthy)
```

If scheduler shows `Restarting`, check `docker compose logs scheduler`. See [Troubleshooting](#10-troubleshooting).

### 8.2. Scheduler is actually scheduling

```bash
docker compose logs scheduler | grep "Scheduled" | tail -5
```

Expected: 5 lines, one per prayer, like:

```
[adhan_scheduler] INFO: Scheduled Fajr at 05:23:00
[adhan_scheduler] INFO: Scheduled Dhuhr at 13:10:00
[adhan_scheduler] INFO: Scheduled Asr at 16:49:00
[adhan_scheduler] INFO: Scheduled Maghrib at 19:39:00
[adhan_scheduler] INFO: Scheduled Isha at 20:55:00
```

**No traceback** should appear in the logs. If you see `TypeError: Schedulers cannot be serialized`, you're on a pre-PR-#14 image — pull latest.

### 8.3. Watchtower auto-update proven end-to-end

```bash
docker compose logs watchtower | tail -20
```

You should eventually see a line like:

```
time="...Z" level=info msg="Update session completed" failed=0 notify=no scanned=3 updated=N
```

— where `scanned=3` (web, scheduler, watchtower) and `updated=N` is 0 if nothing changed or ≥2 if the main branch had new images. The moment you see a non-zero `updated` count, the gift-fleet model is proven: a merge to `main` → GHA multi-arch build → GHCR `:latest` push → Watchtower auto-pull → rolling restart, all without touching the Pi.

### 8.4. Test a real prayer

Easiest: set one prayer time to 2 minutes in the future via the dashboard, save, wait. When it fires, you'll hear adhan on the selected speakers and see:

```
docker compose logs scheduler | grep "Adhan for"
# [adhan_scheduler] INFO: Adhan for Dhuhr – playing http://192.168.86.234:5000/audio/adhan_dhuhr_ussak_2.mp3
```

### 8.5. Reboot survival

Power-cycle the Pi and wait 90 seconds. Reconnect via Tailscale SSH and verify `docker compose ps` shows all three containers back up healthy. This catches cases where something depends on a manual state that didn't persist.

---

## 9. Preparing a gift unit for a different location

The Pi is ready for your house. To prep it for the recipient:

### 9.1. Add their WiFi network

You need the recipient's SSID and password. From your Tailscale SSH session on the Pi:

```bash
sudo nmcli connection add \
  type wifi \
  con-name "<RECIPIENT_SSID>" \
  ifname wlan0 \
  ssid "<RECIPIENT_SSID>" \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "<RECIPIENT_PASSWORD>" \
  connection.autoconnect yes \
  connection.autoconnect-priority 10

# Verify the profile saved — should show a block starting with connection.id
sudo nmcli connection show "<RECIPIENT_SSID>"
```

NetworkManager auto-tries saved profiles in priority order, **but only those whose SSID is actually visible in the current scan**. So at your house, this new profile is skipped (their SSID isn't in range), and the Pi stays on your WiFi. When the Pi boots at their house, your SSID isn't in range, and NM falls through to their profile.

**UTF-8 SSIDs (e.g. Turkish characters):** nmcli handles UTF-8 natively. Double-quotes in bash preserve the bytes intact. If the name doesn't round-trip cleanly, install `xxd` and retry using the hex form:

```bash
sudo apt install -y xxd
echo -n "<SSID>" | xxd -p
# Then: wifi-sec.ssid-hex <hex-string>
```

### 9.2. Update the location

Open the dashboard at `http://bilal-<machine-id>:5000` from your Mac (works via Tailscale regardless of which LAN the Pi is on). Use **"Wrong location? Look up by address"** and type the recipient's city. Click **Look Up**, verify the lat/lon/city/country/timezone auto-populate, then **Save Settings**.

The scheduler's config-watcher picks up the change within 30 seconds and re-schedules today's remaining prayers with the new coordinates.

### 9.3. Verify the new location's prayer times look right

The header and Prayer Times table should now reflect the destination city. Fajr should be a morning time local to the destination, Dhuhr around noon, Isha late evening. If anything looks obviously wrong (e.g. all times at 3 AM), the timezone didn't save correctly — re-check the Timezone field and save again.

### 9.4. Note what the recipient should do on arrival

Include a note like this with the Pi:

```
Bilal — Setup Instructions

  1. Plug in power
  2. Wait 60–90 seconds for it to boot
  3. Find the Pi's IP address:
     - Google Home app → Wi-Fi → Devices, or
     - Your router's admin page
  4. Open http://<that-ip>:5000 in any browser
     on the same WiFi
  5. Log in with the password the maintainer gave you
  6. Click "Discover Speakers", check the boxes
     next to your Nest / Google Home devices
  7. Click Save — prayers play automatically
     at each vakit time

  If anything is weird, text the maintainer — they can log in
  remotely via Tailscale and fix it without you
  touching anything.
```

### 9.5. Ship it

The Pi can travel with the SD card installed. On arrival, the recipient plugs it in, NetworkManager joins their WiFi, Tailscale reconnects automatically (tailnet is location-independent), and you can SSH in from your Mac via `tailscale ssh bilal@bilal-<machine-id>` to verify everything is healthy from anywhere.

---

## 10. Troubleshooting

### Forgot the dashboard password

The dashboard password is stored as a bcrypt hash in `/data/auth.json` inside the Docker volume. Deleting this file resets the password — the next browser visit will prompt for a new one.

**Quick reset via SSH:**

```bash
tailscale ssh bilal@bilal-<hostname>
~/bilal/scripts/reset-password.sh
```

**Manual reset:**

```bash
docker exec bilal-web rm -f /data/auth.json
```

Then open the dashboard in your browser — you'll see the "Create admin password" screen.

### Other issues

### `Err: No space left on device` during Tailscale install

**Cause:** The SD card was too small (4 GB). Docker install alone consumed ~3 GB. The rootfs auto-expansion was fine; the card is physically too small.

**Check:** `lsblk` — if `mmcblk0` reports `3.7G`, the card is 4 GB. You need 16 GB minimum, 32 GB+ is safer.

**Fix:** Reflash to a larger card. All installer work is idempotent.

### `Tailscale SSH enabled, but access controls don't allow anyone to access this device`

**Cause:** Tailscale's default `ssh` block uses `autogroup:self` as destination, which never matches tagged devices (tagged devices have no owner).

**Fix:** Add an explicit `accept` rule for `tag:bilal-fleet` in the ACL. See [section 2](#2-tailscale-acl--one-time-tailnet-setup).

### `No ED25519 host key is known for bilal-... Host key verification failed`

**Cause:** Tailscale SSH on macOS delegates to the system `ssh` binary via ProxyCommand. Strict host-key checking kicks in on first connection, or there's a stale entry from a previous install of the same hostname.

**Fix:** On the Mac,

```bash
ssh-keygen -R bilal-<machine-id>
ssh-keygen -R <tailscale-ip>
tailscale ssh --accept-new bilal@bilal-<machine-id>
```

### `bilal-scheduler` crash loop: `TypeError: Schedulers cannot be serialized`

**Cause:** An old bug where `AdhanSchedulerService` used `SQLAlchemyJobStore`, which serializes every job for persistence. The `daily_reschedule` and `config_watcher` jobs target bound methods (`self.schedule_today`, `self._check_config_change`); serializing a bound method also serializes its `self`, which holds `self.scheduler`, which APScheduler explicitly refuses.

**Fix:** Fixed in PR #14 — switched to the default in-memory jobstore. If you still see this, pull latest. Jobs are rebuilt from current config on every startup, so persistence was never needed.

### `bilal-watchtower` crash loop: `client version 1.25 is too old. Minimum supported API version is 1.40`

**Cause:** `containrrr/watchtower` is abandoned and ships an old moby client that speaks Docker API v1.25. Docker Engine 25+ refuses anything below v1.40.

**Fix:** Fixed in PR #14 — switched to `nickfedor/watchtower:latest`, the actively maintained fork. Drop-in replacement. Now uses API v1.51 cleanly with Docker Engine 29.

### `Discover Speakers` returns an empty list even though Nest speakers are on the same WiFi

**Cause:** Docker's default bridge network does **not** forward multicast traffic. `pychromecast` relies on mDNS on `224.0.0.251:5353` to find Chromecast devices. If the `web` or `scheduler` containers are on a bridge network, the scan silently returns nothing.

**Fix:** Both `web` and `scheduler` must run with `network_mode: host`. Fixed in PR #15. See the `docker-compose.yml` comments.

### `[Errno 2] No such file or directory: 'nmcli'` in the WiFi card

**Cause:** `nmcli` is installed on the host but not in the containers, so the WiFi management endpoints can't shell out to it.

**Current behavior:** The UI shows a friendly fallback message telling users to SSH in and run `sudo nmcli device wifi list` for WiFi management. This is intentional — running nmcli inside a container requires NetworkManager + dbus + root + capabilities, which is out of scope for now.

### Prayer times update correctly but the city/timezone in the header don't

**Cause:** Save Settings used to only send `latitude`, `longitude`, and `calculation_method`. City and timezone stayed at the old values.

**Fix:** Fixed in commit `c71306e` — the Location section now has visible City, Country, and Timezone text fields that are always sent on save. Auto-detect and address lookup populate them automatically.

### Address lookup returns 404 for specific street addresses

**Cause:** Nominatim's free-form search (`q=...`) works well for city/state/country but sometimes misses when you include a specific street number. Different abbreviations (`St` vs `Street`, `Ave` vs `Avenue`) and apartment suffixes can throw it off.

**Fix:** Enhanced in the follow-up PR (branch `feat/geocode-street-fallback`) — now cascades through progressively shorter queries: full address → drop street number → drop street → city/state/country only. Prayer time calculation doesn't need building-level precision (arc-seconds make a sub-second difference), so falling back to the neighborhood coordinates is fine.

### Watchtower countdown in the logs looks frozen

**Cause:** The "Next scheduled run: ... in N minutes" line in the logs is a **static log entry** printed at container startup. It never updates. It's a historical announcement, not a live countdown.

**Fix:** Compute the actual time remaining yourself:

```bash
# Replace with the UTC target from the log
echo "$(( $(date -d '2026-04-11 09:18:32 UTC' +%s) - $(date +%s) ))s remaining"
```

Or tail `docker compose logs -f watchtower` and wait for the real run to fire — you'll see `Checking containers for updated images` → `Found new image` → `Started new container` → `Update session completed`.

---

## Appendix A: What proved the gift-fleet model works

From our first real-hardware deployment (2026-04-11):

```
09:18:36  Found new image ghcr.io/stmehmet/bilal-scheduler:latest
09:18:40  Found new image ghcr.io/stmehmet/bilal-web:latest
09:18:41  Stopping bilal-scheduler (SIGTERM, 30s grace)
09:18:41  Stopping bilal-web (SIGTERM, 30s grace)
09:18:42  Started new bilal-web
09:18:43  Started new bilal-scheduler
09:18:44  Update session completed: failed=0 scanned=3 updated=2
```

**8 seconds of downtime** for the full rolling restart. Zero manual intervention.

That's the whole value proposition: push a fix to `main`, wait an hour, and every Bilal unit in the fleet updates itself. You can ship the Pi to another country with confidence.

## Appendix B: Key open follow-ups

- [ ] **DietPi migration**: see [`DIETPI-MIGRATION.md`](./DIETPI-MIGRATION.md)
- [ ] **Iqamah audio**: add iqamah audio files so users can pick an iqamah sound
- [ ] **In-container WiFi management**: wire NetworkManager dbus + nmcli into the web container so the WiFi tab works without SSH
- [ ] **Street-number geocoding**: merge the `feat/geocode-street-fallback` PR after testing
