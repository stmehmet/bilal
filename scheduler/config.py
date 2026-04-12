"""Shared configuration for the Adhan scheduler."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

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
    "smartthings_token": "",
    "smartthings_device_id": "",
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
        except OSError as exc:
            logger.error("Cannot read config file %s: %s", CONFIG_FILE, exc)
    if _migrate_audio_filenames(config):
        save_config(config)
    return config


def save_config(config: dict) -> None:
    """Persist configuration to disk and signal watchers."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    # Touch a signal file so the scheduler can detect config changes
    _signal_file = CONFIG_DIR / ".config_changed"
    _signal_file.write_text(str(os.getpid()))


def config_changed_since(last_check: float) -> bool:
    """Return True if config has been modified since last_check timestamp."""
    signal_file = CONFIG_DIR / ".config_changed"
    if not signal_file.exists():
        return False
    return signal_file.stat().st_mtime > last_check
