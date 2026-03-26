"""Shared configuration for the Adhan scheduler."""

import json
import os
from pathlib import Path

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
    "adhan_file": "adhan_makkah.mp3",
    "fajr_adhan_file": "adhan_fajr.mp3",
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


def load_config() -> dict:
    """Load configuration from disk, merging with defaults."""
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                stored = json.load(f)
            config.update(stored)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config: dict) -> None:
    """Persist configuration to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
