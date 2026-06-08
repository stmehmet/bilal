"""Disk-space probe — surfaces a near-full SD card before it takes a unit dark.

On 2026-06-07 both field units hit 100% disk (a 53 GB unbounded Docker log) and
went silent.  Log rotation + atomic config writes fixed that root cause, but a
gap remained: the heartbeat dead-man's switch (``heartbeat.ping_success``) only
fires on *playback success*, so a disk that fills for any *other* reason — audio
uploads, journald, a future bug — keeps the unit pinging "healthy" right up to
the moment it wedges.

This module gives both the dashboard and the heartbeat a cheap, dependency-free
way to read how much room is left on the data volume, so a filling disk can be
surfaced *before* it goes dark rather than after.
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)

# The data volume (config.json, the playback log, uploaded audio) is the
# partition that actually fills in the field.  Fall back to "/" when it is
# unset or absent — e.g. running the probe outside the container — so the
# reading still reflects the disk the process lives on.
_DATA_PATH = os.getenv("CONFIG_DIR", "/data")


def _probe_path() -> str:
    """Return the path to stat: the data volume if present, else root ("/")."""
    if os.path.isdir(_DATA_PATH):
        return _DATA_PATH
    return "/"


def usage() -> dict | None:
    """Return data/root filesystem usage, or ``None`` if it can't be read.

    Keys: ``total_bytes``, ``free_bytes`` and ``free_pct`` (0–100, rounded to
    one decimal).  A ``None`` return means the probe failed and callers must
    treat that as *unknown* — never as *full* — so a transient ``stat`` hiccup
    can't, on its own, trip the dead-man's switch.
    """
    path = _probe_path()
    try:
        total, _used, free = shutil.disk_usage(path)
    except OSError as exc:
        logger.warning("diskspace: cannot stat %s: %s", path, exc)
        return None
    if total <= 0:  # defensive: avoid a divide-by-zero on a degenerate mount
        return None
    return {
        "total_bytes": total,
        "free_bytes": free,
        "free_pct": round(free / total * 100, 1),
    }
