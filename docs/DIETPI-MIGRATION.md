# DietPi Migration Plan

Forward-looking notes for swapping Raspberry Pi OS Lite → **DietPi** as the base image for Bilal gift units.

> **Status:** planning / not yet attempted. This document captures the known differences between Pi OS Lite and DietPi that will affect the installer, documents the expected benefits, and lists the validation steps needed before making DietPi the default.

---

## Why DietPi?

[DietPi](https://dietpi.com/) is a minimal Debian-based distribution optimised for single-board computers. Relative to Raspberry Pi OS Lite, it promises:

| Metric | Pi OS Lite (Trixie) | DietPi | Notes |
|--------|---------------------|--------|-------|
| Image size (SD card footprint) | ~2.2 GB after install | ~400–600 MB | Matters for 16 GB cards; every freed GB is more room for image cache + logs |
| RAM at idle | ~250 MB | ~80–120 MB | More headroom for scheduler + web + watchtower |
| Boot time to SSH ready | ~45–60 s | ~20–30 s | Faster gift-unit bring-up when flashing many cards |
| Package selection | Fixed by Debian | Curated via `dietpi-software` | Docker can be installed via one command |
| Maintenance model | Debian-managed | DietPi's own `dietpi-update` + optional unattended upgrades | Need to decide which wins |

For a gift fleet where we care about:

- minimal attack surface (fewer packages = fewer CVEs to patch)
- fast first-boot (less frustration when flashing a new unit)
- predictable resource headroom (Pi 4 2GB is our target and we want 200 MB+ free)

…DietPi is an obvious fit. The cost is learning its quirks.

---

## Known differences that affect the installer

This is the list of things that will break or need rewriting when we run `scripts/install.sh` on a fresh DietPi image instead of Pi OS Lite.

### 1. Package installation conventions

**Pi OS Lite** uses stock `apt-get install -y docker.io` or Docker's convenience script.

**DietPi** has its own package index via `dietpi-software`. Docker installs cleanly with:

```bash
sudo /boot/dietpi/dietpi-software install 134    # Docker
```

The `dietpi-software` tool handles post-install daemon configuration, user group additions, and firewall rules that the convenience script doesn't. **Option:** our installer can detect `dietpi-software` existence and use it, falling back to the convenience script otherwise. Something like:

```bash
if [ -x /boot/dietpi/dietpi-software ]; then
    sudo /boot/dietpi/dietpi-software install 134 162    # Docker + Docker Compose plugin
else
    curl -fsSL https://get.docker.com | sudo sh
    sudo apt install -y docker-compose-plugin
fi
```

**Action item:** test `install.sh` on a fresh DietPi. Add the detection branch if needed.

### 2. Network management — NetworkManager may not be the default

This is the biggest potential gotcha. **Pi OS Lite** switched to NetworkManager in Bookworm and the `nmcli` commands in our runbook (Section 9.1) assume NM is running.

**DietPi** traditionally uses `/etc/network/interfaces` with `wpa_supplicant.conf`, managed via the `dietpi-config` text UI. NetworkManager can be installed via `dietpi-software install 147` but it's not default.

**Implications:**

- The `sudo nmcli connection add ...` command in [DEPLOYMENT-RUNBOOK.md § 9.1](./DEPLOYMENT-RUNBOOK.md#91-add-their-wifi-network) won't work out of the box
- Multi-network WiFi autoswitching (home network → recipient's network) requires NetworkManager or a comparable manager
- The captive-portal.sh script likely assumes `nmcli` too

**Options:**

1. **Install NetworkManager via `dietpi-software`** during our installer run, then keep the existing nmcli-based flow. Simplest, but adds ~30 MB to the image size and partially defeats the "minimal" point of DietPi.
2. **Rewrite WiFi setup against `wpa_supplicant`**: add multiple `network={}` blocks with different `priority=` values in `wpa_supplicant.conf`. This is the classic Debian approach; works on both systems.
3. **Use `dietpi-config` non-interactively**: DietPi supports scripted config via `dietpi-config WiFi`. Worth investigating if there's a stable CLI API for adding a second network.

**Action item:** pick one of these. (1) is pragmatic for a quick migration; (2) is the long-term correct answer because it removes a distro-specific dependency from our installer.

### 3. cgroup memory accounting

**Pi OS Lite** requires a manual edit to `/boot/firmware/cmdline.txt` to enable cgroup memory (Section 6.2 of the runbook).

**DietPi** — worth verifying, but based on community reports, cgroup memory is **already enabled** by default in recent versions. If so, Section 6.2 becomes a no-op on DietPi.

**Action item:** `cat /proc/cgroups | grep memory` on a fresh DietPi after first boot. If the `enabled` column is `1`, we can drop that step from the runbook on DietPi.

### 4. Firewall — `dietpi-software` vs UFW

**Pi OS Lite** uses UFW (our `harden.sh` script assumes this).

**DietPi** ships with a minimal iptables config and can install UFW via `dietpi-software install 171`. Our `harden.sh` will need the same install-or-detect branching as Docker.

**Action item:** generalise `harden.sh` or write a DietPi-specific variant.

### 5. systemd journal retention

**Pi OS Lite** keeps `/var/log/journal/` by default and rotates ~1 GB.

**DietPi** is aggressive about log reduction: `/var/log/` is a tmpfs by default, which means logs don't survive reboots. **This matters for us** because the scheduler's APScheduler timing issues and the Watchtower auto-update history are exactly the things we read from logs when debugging remotely.

**Options:**

- Accept the trade-off (fast boots, no log persistence)
- Disable DietPi's RAMlog via `dietpi-config` → Log System → set to "File-based"
- Ship persistent logs to a file in `/data/` explicitly

**Action item:** pick a logging strategy before the first DietPi gift unit ships.

### 6. `avahi-daemon` for mDNS

**Pi OS Lite** ships `avahi-daemon` running by default, which means `bilal-<name>.local` resolves on the LAN immediately after first boot.

**DietPi** does **not** ship avahi by default. Without it:

- You cannot `ssh bilal@bilal-<name>.local` during first boot — you need the LAN IP from the router or Google Home app
- `pychromecast`'s mDNS scanning inside the containers is unaffected (it does its own mDNS; avahi is only for host-level name resolution)

**Action item:** add `sudo dietpi-software install 34` (Avahi) to the installer if we want the `.local` name to work during bring-up. Or accept that users need to find the IP a different way.

### 7. `/boot/firmware/cmdline.txt` vs `/boot/cmdline.txt`

**Pi OS Lite Bookworm/Trixie** uses `/boot/firmware/cmdline.txt`.

**DietPi** historically used `/boot/cmdline.txt` but recent versions have aligned with the Pi OS path. **Verify** before assuming either path in scripts. A portable pattern:

```bash
for path in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [ -f "$path" ]; then CMDLINE="$path"; break; fi
done
```

**Action item:** make `harden.sh` and the cgroup-memory step path-agnostic.

---

## Validation checklist (before declaring DietPi the default)

Run through this on a fresh DietPi image on a real Pi:

- [ ] Flash DietPi image to SD card, boot, complete first-run dialog
- [ ] SSH in over ethernet or the LAN IP
- [ ] Run `scripts/install.sh` — capture every line that fails or prompts
- [ ] Confirm `docker compose ps` shows all 3 containers healthy after install
- [ ] Run the full [verification checklist](./DEPLOYMENT-RUNBOOK.md#8-verification) from the runbook
- [ ] Trigger a real prayer adhan on a Nest speaker
- [ ] Force a Watchtower update cycle and verify it completes with `failed=0`
- [ ] Reboot, confirm everything comes back healthy
- [ ] Add a second WiFi network (the whole point of the gift-fleet model) — test both the nmcli path and the wpa_supplicant path
- [ ] Compare boot time, RAM usage, and disk usage against Pi OS Lite baseline
- [ ] Document every tweak needed in an updated runbook
- [ ] Ship one gift unit with DietPi for a 30-day stability test before converting the whole fleet

---

## Recommended migration path

**Don't flip the switch** until we have:

1. A test run of `install.sh` on DietPi that completes without human intervention
2. A decision on the NetworkManager vs wpa_supplicant question (section 2 above)
3. 30 days of uptime on at least one gift unit running DietPi

Until then, **Pi OS Lite stays the default** and DietPi is an experimental alternative. The runbook in [DEPLOYMENT-RUNBOOK.md](./DEPLOYMENT-RUNBOOK.md) assumes Pi OS Lite throughout.

---

## References

- DietPi homepage: https://dietpi.com/
- `dietpi-software` package index: https://dietpi.com/docs/software/
- DietPi vs Raspberry Pi OS comparison: https://dietpi.com/#features
- NetworkManager on DietPi: https://dietpi.com/docs/software/network/#networkmanager
- Cgroup memory on ARM: https://forums.raspberrypi.com/viewtopic.php?t=203128
