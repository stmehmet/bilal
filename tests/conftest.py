"""Pytest configuration – add scheduler and web to sys.path, mock unavailable libs."""
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Mock external hardware libraries that may not be installable in CI/test
# ---------------------------------------------------------------------------
for mod_name in ("adhan", "adhan.methods", "pychromecast", "zeroconf"):
    if mod_name not in sys.modules:
        fake = types.ModuleType(mod_name)
        if mod_name == "adhan":
            fake.adhan = MagicMock(return_value={})
        elif mod_name == "adhan.methods":
            for attr in (
                "ISNA", "EGYPT", "KARACHI", "KUWAIT", "MWL",
                "QATAR", "SINGAPORE", "TEHRAN", "TURKEY", "UMM_AL_QURA",
            ):
                setattr(fake, attr, MagicMock())
        elif mod_name == "pychromecast":
            fake.Chromecast = MagicMock
            fake.get_chromecasts = MagicMock(return_value=([], None))
        sys.modules[mod_name] = fake

sys.path.insert(0, str(Path(__file__).parent.parent / "scheduler"))
sys.path.insert(0, str(Path(__file__).parent.parent / "web"))


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """Create a Flask test client with all paths pointing to tmp_path."""
    import config as cfg

    # Redirect scheduler config to tmp_path
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "AUDIO_DIR", tmp_path / "audio")

    # Import app after patching config
    import app as web_app

    monkeypatch.setattr(web_app, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(web_app, "AUDIO_DIR", tmp_path / "audio")
    (tmp_path / "audio").mkdir(exist_ok=True)

    web_app.app.config["TESTING"] = True
    web_app.app.config["WTF_CSRF_ENABLED"] = False

    with web_app.app.test_client() as client:
        yield client


@pytest.fixture()
def logged_in_client(app_client, tmp_path):
    """A test client that has already set up a password and logged in."""
    # Create password
    app_client.post("/login", data={"password": "testpass123"})
    # Login
    app_client.post("/login", data={"password": "testpass123"})
    return app_client


@pytest.fixture()
def sample_config():
    """Factory providing a valid config with Makkah coordinates."""
    return {
        "latitude": 21.4225,
        "longitude": 39.8262,
        "timezone": "Asia/Riyadh",
        "city": "Makkah",
        "country": "SA",
        "calculation_method": "UmmAlQura",
        "volume": 0.5,
        "setup_complete": True,
        "skip_prayers": [],
        "speakers": {},
        "adhan_file": "adhan_makkah.mp3",
        "fajr_adhan_file": "adhan_fajr.mp3",
    }
