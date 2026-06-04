"""Best-effort heartbeat ping — a dead-man's switch for fleet liveness.

The scheduler pings ``HEALTHCHECK_PING_URL`` after every successful adhan/iqamah
playback.  Point that URL at a free dead-man's-switch service (e.g.
healthchecks.io) or any webhook: while the unit keeps playing, the pings keep
arriving; the moment it wedges (a locked-up process that can no longer spawn
playback threads) or stops reaching its speakers, the pings stop and the
service alerts you.

This is the only signal that catches *both* failure modes we hit in the field:
a fully-dead unit (no pings) and an alive-but-not-playing unit (no successful
playback → no pings).  A mere liveness ping from the main loop would keep firing
even while playback was broken, so it is deliberately tied to playback success.

Pinging is fire-and-forget on a daemon thread with a short timeout and total
error suppression, so it can never delay or break playback.  When
``HEALTHCHECK_PING_URL`` is unset the whole module is a no-op.
"""

import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)

PING_TIMEOUT_SECONDS = 10


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
    """
    url = os.getenv("HEALTHCHECK_PING_URL", "").strip()
    if not url:
        return
    threading.Thread(target=_ping, args=(url,), daemon=True).start()
