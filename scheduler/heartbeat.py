"""Best-effort heartbeat ping — a dead-man's switch for fleet liveness.

The scheduler pings ``HEALTHCHECK_PING_URL`` after every successful adhan/iqamah
playback.  Point that URL at a free dead-man's-switch service (e.g.
healthchecks.io) or any webhook: while the unit keeps playing, the pings keep
arriving; the moment it wedges (a locked-up process that can no longer spawn
playback threads) or stops reaching its speakers, the pings stop and the
service alerts you.

This is the signal that catches the failure modes we hit in the field: a
fully-dead unit (no pings), an alive-but-not-playing unit (no successful
playback → no pings), and — via the disk guard below — an alive-and-playing
unit whose data volume is nearly full and about to go dark.  A mere liveness
ping from the main loop would keep firing even while playback was broken, so it
is deliberately tied to playback success.

Pinging is fire-and-forget on a daemon thread with a short timeout and total
error suppression, so it can never delay or break playback.  When
``HEALTHCHECK_PING_URL`` is unset no ping is sent — though a critically-low
disk is still logged, so the warning survives even on an unmonitored unit.
"""

import logging
import os
import threading

import requests

import diskspace

logger = logging.getLogger(__name__)

PING_TIMEOUT_SECONDS = 10

# A unit whose data volume is this close to full is about to go dark: once the
# disk hits 100% the config can't be saved, the playback log can't append, and
# the next restart can wipe state (the 2026-06-07 outage).  When free space
# drops below *either* of these floors we log a WARNING and deliberately skip
# the success ping so the dead-man's switch fires.
#
# Tradeoff: suppressing the ping makes a still-playing unit look dead to the
# healthcheck service — which is exactly what we want, because a near-full disk
# is an emergency even while adhan still plays, and we'd rather be paged a
# little early than discover the unit silently dead after the disk filled. The
# floors are intentionally low (5% / 500 MB) so this only trips when the disk
# is genuinely close to full, not on normal day-to-day churn. The two are
# OR'd so the absolute floor still protects very large volumes (where 5% is
# many GB) and the percentage still protects very small ones.
CRITICAL_FREE_PCT = 5.0
CRITICAL_FREE_BYTES = 500 * 1024 * 1024  # 500 MB


def _is_critically_low(usage: dict | None) -> bool:
    """Whether a disk-usage reading means the data volume is dangerously full.

    A ``None`` reading (the probe failed) is treated as *not* low: an unknown
    disk must never trip the dead-man's switch on its own.
    """
    if usage is None:
        return False
    return (
        usage["free_pct"] < CRITICAL_FREE_PCT
        or usage["free_bytes"] < CRITICAL_FREE_BYTES
    )


def _ping(url: str) -> None:
    try:
        requests.get(url, timeout=PING_TIMEOUT_SECONDS)
        logger.debug("Heartbeat ping sent")
    except Exception as exc:
        # A failed heartbeat must never affect playback — log and move on.
        logger.debug("Heartbeat ping failed: %s", exc)


def ping_success() -> None:
    """Signal a successful playback, resetting the dead-man's-switch timer.

    No-op when ``HEALTHCHECK_PING_URL`` is unset.  Fire-and-forget: the HTTP call runs
    on a daemon thread so a slow or unreachable endpoint never blocks the
    scheduler.

    Disk guard: a successful ping normally proves the unit is alive *and*
    playing, but a unit whose data volume is critically full is about to go
    dark even while playback still works.  In that case we log a WARNING and
    skip the ping so the dead-man's switch surfaces the problem *before* the
    disk hits 100% (see ``CRITICAL_FREE_PCT``).
    """
    usage = diskspace.usage()
    if _is_critically_low(usage):
        logger.warning(
            "Disk critically low (%.1f%% / %.0f MB free) — suppressing heartbeat "
            "so the dead-man's switch fires before the unit goes dark",
            usage["free_pct"],
            usage["free_bytes"] / (1024 * 1024),
        )
        return

    url = os.getenv("HEALTHCHECK_PING_URL", "").strip()
    if not url:
        return
    threading.Thread(target=_ping, args=(url,), daemon=True).start()
