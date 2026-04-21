"""Per-device playback log — append-only JSONL with age-based retention.

Every adhan/iqamah attempt writes one line per speaker:

    {"ts": "2026-04-20T05:30:02.113+00:00",
     "event": "adhan" | "iqamah" | "friday_sela",
     "prayer": "Fajr",
     "speaker": "Downstairs display",
     "ok": true,
     "elapsed_ms": 2341,
     "error": null}

Writes are serialised through a process-local lock.  The file is pruned in
place on every write (cheap: one pass over the file) so it never grows
beyond ``RETENTION_DAYS``.  Dashboard queries read the same file and filter.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_FILE = Path(os.getenv("CONFIG_DIR", "/data")) / "playback.log.jsonl"
RETENTION_DAYS = 7
# Hard ceiling to guard against runaway growth between retention passes
# (unlikely but cheap insurance).
MAX_LINES = 5000

_write_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _is_expired(entry: dict, cutoff: datetime.datetime) -> bool:
    ts = entry.get("ts")
    if not isinstance(ts, str):
        return True
    try:
        parsed = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed < cutoff


def _prune_file_locked(cutoff: datetime.datetime) -> None:
    """Rewrite LOG_FILE keeping only entries newer than cutoff.

    Called with ``_write_lock`` held.  Silently skips when the file is
    missing.  On any I/O or parse failure we log and move on — losing
    retention pruning for one cycle is far better than crashing the
    scheduler over a log file.
    """
    if not LOG_FILE.exists():
        return
    try:
        with LOG_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        logger.warning("playback_log: cannot read for prune: %s", exc)
        return

    kept: list[str] = []
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _is_expired(entry, cutoff):
            kept.append(line)

    if len(kept) > MAX_LINES:
        kept = kept[-MAX_LINES:]

    if len(kept) == len(lines):
        return

    try:
        tmp = LOG_FILE.with_suffix(LOG_FILE.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for line in kept:
                f.write(line + "\n")
        tmp.replace(LOG_FILE)
    except OSError as exc:
        logger.warning("playback_log: cannot rewrite: %s", exc)


def record(
    event: str,
    prayer: str | None,
    speaker: str,
    ok: bool,
    elapsed_seconds: float,
    error: str | None = None,
) -> None:
    """Append one entry to the playback log.

    ``event`` is one of ``"adhan" | "iqamah" | "friday_sela"``.  ``prayer``
    may be None for events that aren't tied to a specific prayer (though
    today all three are).
    """
    entry = {
        "ts": _now_iso(),
        "event": event,
        "prayer": prayer,
        "speaker": speaker,
        "ok": bool(ok),
        "elapsed_ms": int(round(elapsed_seconds * 1000)),
        "error": error,
    }
    line = json.dumps(entry, ensure_ascii=False)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=RETENTION_DAYS)
    with _write_lock:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.warning("playback_log: cannot append: %s", exc)
            return
        _prune_file_locked(cutoff)


def query(
    speaker: str | None = None,
    limit: int = 50,
    days: int = RETENTION_DAYS,
) -> list[dict]:
    """Return recent entries, newest first, optionally filtered by speaker.

    Always returns at most ``limit`` entries.  ``days`` caps how far back
    we look (defaults to the retention window).
    """
    if not LOG_FILE.exists():
        return []
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    try:
        with LOG_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        logger.warning("playback_log: cannot read for query: %s", exc)
        return []

    entries: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _is_expired(entry, cutoff):
            continue
        if speaker is not None and entry.get("speaker") != speaker:
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def purge(older_than_days: int = RETENTION_DAYS) -> int:
    """Force a retention pass; returns the number of entries removed."""
    if not LOG_FILE.exists():
        return 0
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=older_than_days)
    with _write_lock:
        try:
            with LOG_FILE.open("r", encoding="utf-8") as f:
                before = sum(1 for line in f if line.strip())
        except OSError:
            return 0
        _prune_file_locked(cutoff)
        try:
            with LOG_FILE.open("r", encoding="utf-8") as f:
                after = sum(1 for line in f if line.strip())
        except OSError:
            return 0
    return max(0, before - after)
