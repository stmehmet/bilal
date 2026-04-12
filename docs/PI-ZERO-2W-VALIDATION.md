# Bilal — Pi Zero 2W Validation

Hardware validation results for running the full Bilal stack (bilal-web + bilal-scheduler + bilal-watchtower) on a Raspberry Pi Zero 2W (RP3A0, quad-core Cortex-A53 @ 1 GHz, 512 MB RAM, ARM64).

**Goal:** Confirm that the Pi Zero 2W is a viable $15 fleet unit using the existing Docker stack with zero code changes.

---

## Hardware under test

| | Value |
|---|---|
| Board | Raspberry Pi Zero 2W |
| SoC | RP3A0 (BCM2710A1) |
| CPU | 4× Cortex-A53 @ 1 GHz (ARMv8-A / ARM64) |
| RAM | 512 MB LPDDR2 |
| WiFi | 2.4 GHz 802.11b/g/n |
| Power | Micro-USB, 5V 2.5A recommended |
| Data | Micro-USB OTG |
| OS | Raspberry Pi OS Lite 64-bit (Bookworm) |
| SD card | ___ GB (fill in) |

---

## Go / no-go criteria

### 1. Containers start successfully

**Criterion:** `docker compose ps` shows bilal-web, bilal-scheduler, bilal-watchtower all in `Up` state.

**Result:** ___ PASS / FAIL

```
# Paste `docker compose ps` output here
```

### 2. Idle RAM headroom

**Criterion:** `free -h` shows >50 MiB of available memory after all containers stabilise (2-3 minutes post-startup).

**Result:** ___ PASS / FAIL

```
# Paste `free -h` output here
```

```
# Paste `docker stats --no-stream` output here
```

**Analysis:**

| Component | Memory |
|---|---|
| OS baseline | ___ MiB |
| bilal-web | ___ MiB |
| bilal-scheduler | ___ MiB |
| bilal-watchtower | ___ MiB |
| **Total used** | **___ MiB** |
| **Available** | **___ MiB** |

**Comparison to Pi 4B 4GB (Austin, 2026-04-11):**
- Austin: OS 235 MiB + containers 165 MiB = 400 MiB used / 3300 MiB available
- Zero 2W: OS ___ MiB + containers ___ MiB = ___ MiB used / ___ MiB available

### 3. Dashboard load time

**Criterion:** Dashboard loads at `http://<hostname>.local:5000` in under 5 seconds on first visit.

**Result:** ___ PASS / FAIL

**Measured:** ___ seconds

### 4. Speaker discovery time

**Criterion:** `/api/discover-speakers` completes in under 15 seconds.

**Result:** ___ PASS / FAIL

**Measured:** ___ seconds, ___ speakers found

### 5. Test playback latency

**Criterion:** "Test on Speaker" fires audible playback on a Nest device within 5 seconds of clicking.

**Result:** ___ PASS / FAIL

**Measured:** ___ seconds to first audible sound

### 6. Watchtower auto-update without OOM

**Criterion:** Watchtower pull + restart cycle completes with `failed=0` and no OOM kill.

**Result:** ___ PASS / FAIL

```
# Paste relevant watchtower log lines here
```

**Peak RAM during update:**

```
# If you caught a `free -h` or `docker stats` during the update, paste here
```

### 7. 24-hour soak test

**Criterion:** All 5 daily prayers fire within ±5 s of scheduled time over a 24-hour period. No container restarts, no OOM kills, no memory growth.

**Result:** ___ PASS / FAIL

**Prayers verified:**

| Prayer | Scheduled | Actual | Delta |
|---|---|---|---|
| Fajr | | | |
| Dhuhr | | | |
| Asr | | | |
| Maghrib | | | |
| Isha | | | |

**Memory after 24h:**

```
# Paste `free -h` and `docker stats --no-stream` here
```

---

## Issues encountered

_List any problems and their solutions here._

1. ...

---

## Verdict

**Overall: ___ PASS / FAIL**

**Gunicorn worker reduction needed?** ___ YES / NO

If YES, recommended config:
```bash
# Add to .env on the Pi:
GUNICORN_CMD_ARGS=--workers 1 --threads 2
```

Or add to `docker-compose.yml` web service environment section.

---

## Conclusion

_Final recommendation on Pi Zero 2W as fleet hardware._
