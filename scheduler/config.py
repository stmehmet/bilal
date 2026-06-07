"""Shared configuration for the Adhan scheduler."""

import json
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfigWriteError(OSError):
    """Raised when config.json cannot be persisted (full disk / read-only FS).

    Subclasses ``OSError`` so existing ``except OSError`` handlers still catch
    it, but is specific enough that the web layer can turn it into a clear
    "couldn't write to disk" message instead of a generic 500.
    """

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/data"))
CONFIG_FILE = CONFIG_DIR / "config.json"
AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "/audio"))

DEFAULT_CONFIG = {
    "latitude": None,
    "longitude": None,
    "timezone": "UTC",
    "city": "Unknown",
    "country": "Unknown",
    "calculation_method": "ISNA",
    "skip_prayers": [],
    "speakers": {},
    # Per-prayer adhan audio files. Keys must match PRAYER_NAMES.
    # Each prayer is traditionally recited in a specific Ottoman maqam:
    # Saba (Fajr), Uşşak (Dhuhr), Rast (Asr), Segâh (Maghrib), Hicaz (Isha).
    "adhan_audio_files": {
        "Fajr": "adhan_fajr_saba_2.mp3",
        "Dhuhr": "adhan_dhuhr_ussak_2.mp3",
        "Asr": "adhan_asr_rast_2.mp3",
        "Maghrib": "adhan_maghrib_segah_2.mp3",
        "Isha": "adhan_isha_hicaz_2.mp3",
    },
    "volume": 0.5,
    "setup_complete": False,
    # Iqamah offsets in minutes after adhan
    "iqamah_offsets": {"Fajr": 20, "Dhuhr": 15, "Asr": 15, "Maghrib": 5, "Isha": 15},
    # Iqamah audio notification
    "iqamah_enabled": False,
    "iqamah_audio_file": "iqamah_bell.mp3",
    # Friday Sela — plays before Jummah (Friday Dhuhr) only
    "friday_sela_enabled": False,
    "friday_sela_audio_file": "sela_cuma_huseyni_1.mp3",
    "friday_sela_offset": 45,  # minutes before Dhuhr
    # Do Not Disturb (mute adhan during these hours)
    "dnd_enabled": False,
    "dnd_start": "23:00",
    "dnd_end": "05:30",
}

CALCULATION_METHODS = [
    "MuslimWorldLeague",
    "Egyptian",
    "Karachi",
    "UmmAlQura",
    "Dubai",
    "MoonsightingCommittee",
    "NorthAmerica",
    "Kuwait",
    "Qatar",
    "Singapore",
    "Tehran",
    "Turkey",
    "ISNA",
]

PRAYER_NAMES = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]

# Known maqam slugs used in the current naming scheme.
_KNOWN_MAQAMS = {"saba", "ussak", "rast", "segah", "hicaz"}


def _migrate_audio_filenames(config: dict) -> bool:
    """Migrate old audio filenames to the current naming scheme.

    Detects filenames that use the old ``adhan_<prayer>_<tag>_<maqam>.mp3``
    layout (where maqam is the 4th segment) and rewrites them to the current
    ``adhan_<prayer>_<maqam>_1.mp3`` layout.  The user can then pick the
    correct variant from the dashboard dropdown.

    Returns True if any filename was changed.
    """
    files = config.get("adhan_audio_files", {})
    changed = False
    for prayer, filename in list(files.items()):
        if not filename.endswith(".mp3"):
            continue
        parts = filename[:-4].split("_")  # strip .mp3, split
        # Current format: adhan_<prayer>_<maqam>_<number> — nothing to do
        if len(parts) == 4 and parts[2] in _KNOWN_MAQAMS and parts[3].isdigit():
            continue
        # Old format: adhan_<prayer>_<tag>_<maqam> where maqam is in position 3
        if len(parts) == 4 and parts[3] in _KNOWN_MAQAMS and parts[2] not in _KNOWN_MAQAMS:
            new_name = f"adhan_{parts[1]}_{parts[3]}_1.mp3"
            files[prayer] = new_name
            changed = True
    if changed:
        logger.info("Migrated audio filenames to new naming scheme")
    return changed


_DEPRECATED_KEYS = ("smartthings_token", "smartthings_device_id")


def _strip_deprecated_keys(config: dict) -> bool:
    """Remove keys that belong to features we no longer ship (e.g. SmartThings).

    Returns True if anything was stripped, so the caller knows to persist.
    """
    changed = False
    for key in _DEPRECATED_KEYS:
        if key in config:
            del config[key]
            changed = True
    if changed:
        logger.info("Removed deprecated config keys: %s", ", ".join(_DEPRECATED_KEYS))
    return changed


def _quarantine_corrupt_config() -> None:
    """Preserve a corrupt config.json instead of silently losing it.

    Copies the bad file to ``config.json.corrupt`` (once — an existing backup is
    never overwritten, so the *first* corruption is the one kept for inspection).
    We deliberately do NOT delete or overwrite config.json here: the caller falls
    back to in-memory defaults for this run, and the next ``save_config`` replaces
    it atomically.  The point is to make corruption loud and recoverable rather
    than a silent reset to defaults (which is how a unit goes dark unnoticed).
    """
    try:
        backup = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".corrupt")
        if not backup.exists():
            shutil.copy2(CONFIG_FILE, backup)
            logger.error(
                "Backed up corrupt config to %s; running on in-memory defaults "
                "until a valid config is saved",
                backup,
            )
    except OSError as exc:
        logger.warning("Could not back up corrupt config: %s", exc)


def load_config() -> dict:
    """Load configuration from disk, merging with defaults."""
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                stored = json.load(f)
            config.update(stored)
        except json.JSONDecodeError as exc:
            logger.error("Corrupt config file %s: %s", CONFIG_FILE, exc)
            _quarantine_corrupt_config()
        except OSError as exc:
            logger.error("Cannot read config file %s: %s", CONFIG_FILE, exc)
    migrated = _migrate_audio_filenames(config)
    stripped = _strip_deprecated_keys(config)
    if migrated or stripped:
        save_config(config)
    return config


def save_config(config: dict) -> None:
    """Persist configuration to disk atomically and signal watchers.

    Writes to a temp file in the same directory, fsyncs, then ``os.replace``s it
    over the live file — an atomic rename, so a crash or a full disk can never
    leave a half-written or truncated config.json behind.  (Before this, an
    ENOSPC mid-write truncated config.json to 0 bytes; the unit then silently
    fell back to defaults and lost every user setting.)

    Raises ``ConfigWriteError`` if the file cannot be written.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    except OSError as exc:
        # Don't leave the partial temp file occupying space on an already-full disk.
        try:
            tmp.unlink()
        except OSError:
            pass
        logger.error("Could not write config file %s: %s", CONFIG_FILE, exc)
        raise ConfigWriteError(str(exc)) from exc
    # Touch a signal file so the scheduler can detect config changes.  A failure
    # here is non-fatal — the config itself is already safely persisted.
    try:
        signal_file = CONFIG_DIR / ".config_changed"
        signal_file.write_text(str(os.getpid()))
    except OSError as exc:
        logger.warning("Could not update config-changed signal file: %s", exc)


def config_changed_since(last_check: float) -> bool:
    """Return True if config has been modified since last_check timestamp."""
    signal_file = CONFIG_DIR / ".config_changed"
    if not signal_file.exists():
        return False
    return signal_file.stat().st_mtime > last_check
