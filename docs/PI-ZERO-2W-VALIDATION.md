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
| SD card | 32 GB |

---

## Go / no-go criteria

### 1. Containers start successfully

**Criterion:** `docker compose ps` shows bilal-web, bilal-scheduler, bilal-watchtower all in `Up` state.

**Result:** PASS

```
NAME               SERVICE      STATUS
bilal-scheduler    scheduler    Up (healthy)
bilal-watchtower   watchtower   Up (healthy)
bilal-web          web          Up (healthy)
```

### 2. Idle RAM headroom

**Criterion:** `free -h` shows >50 MiB of available memory after all containers stabilise.

**Result:** PASS (166 MiB available after Gunicorn worker reduction)

```
               total        used        free      shared  buff/cache   available
Mem:           416Mi       249Mi        91Mi       120Ki       135Mi       166Mi
Swap:          415Mi       130Mi       285Mi
```

```
CONTAINER ID   NAME               CPU %     MEM USAGE / LIMIT     MEM %     NET I/O         BLOCK I/O         PIDS
6f97734d6f92   bilal-scheduler    0.00%     6.004MiB / 416.1MiB   1.44%     0B / 0B         30.6MB / 25.6MB   3
2d60668b2e39   bilal-web          0.03%     21.03MiB / 416.1MiB   5.05%     0B / 0B         66.2MB / 80.8MB   4
1aac729ab1f8   bilal-watchtower   0.00%     1.309MiB / 416.1MiB   0.31%     7.95kB / 126B   40.1MB / 4.89MB   9
```

**Analysis:**

| Component | Memory |
|---|---|
| OS + system | ~221 MiB |
| bilal-web | 21 MiB |
| bilal-scheduler | 6 MiB |
| bilal-watchtower | 1.3 MiB |
| **Total containers** | **28 MiB** |
| **Total used** | **249 MiB** |
| **Available** | **166 MiB** |

**Comparison to Pi 4B 4GB:**
- Pi 4B: containers 165 MiB (2 Gunicorn workers) / 3300 MiB available
- Zero 2W: containers 28 MiB (1 Gunicorn worker + 2 threads) / 166 MiB available

### 3. Dashboard load time

**Criterion:** Dashboard loads at `http://<hostname>:5000` in under 5 seconds.

**Result:** PASS

### 4. Speaker discovery time

**Criterion:** `/api/discover-speakers` completes in under 15 seconds.

**Result:** PASS — speakers discovered and playback tested successfully

### 5. Test playback latency

**Criterion:** "Test on Speaker" fires audible playback on a Nest device within 5 seconds.

**Result:** PASS

### 6. Watchtower auto-update without OOM

**Criterion:** Watchtower pull + restart cycle completes with `failed=0` and no OOM kill.

**Result:** PASS — multiple update cycles observed with `failed=0`

---

## Issues encountered

1. **`git clone` connection reset** — WiFi dropped mid-clone on first attempt. Retry succeeded. The Zero 2W's 2.4 GHz radio can be flaky during large transfers. Moving closer to the router or using ethernet (via OTG adapter) helps.

2. **cgroup memory accounting disabled** — `docker stats` showed `0B / 0B` for memory until `cgroup_memory=1 cgroup_enable=memory` was added to `/boot/firmware/cmdline.txt` and the Pi was rebooted. This is a Pi OS default, same fix as documented in the deployment runbook.

3. **Swap pressure with 2 Gunicorn workers** — With the default `--workers 2`, swap usage was 155 MiB. Reducing to `--workers 1 --threads 2` via `.env` dropped swap to 130 MiB and freed ~25 MiB of RAM.

---

## Verdict

**Overall: PASS**

**Gunicorn worker reduction needed?** YES

Recommended config (add to `.env` on the Pi):
```bash
GUNICORN_CMD_ARGS=--workers 1 --threads 2
```

---

## Conclusion

The Pi Zero 2W is a viable fleet unit at $15. The existing Docker stack runs with zero code changes. Total container memory is 28 MiB with 1 Gunicorn worker, leaving 166 MiB available. Swap usage is present but manageable at 130 MiB. All core functions — dashboard, speaker discovery, playback, Watchtower auto-updates — work correctly.

**Recommended fleet configuration:** Pi Zero 2W + 32 GB microSD + micro-USB 5V 2.5A PSU. Total cost per unit: ~$25-30 including accessories.
