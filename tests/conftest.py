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
_mock_modules = {
    "pychromecast": lambda m: (
        setattr(m, "Chromecast", MagicMock),
        setattr(m, "get_chromecasts", MagicMock(return_value=([], None))),
        setattr(m, "error", types.ModuleType("pychromecast.error")),
    ),
    "pychromecast.error": lambda m: setattr(m, "PyChromecastError", type("PyChromecastError", (Exception,), {})),
    "zeroconf": lambda m: None,
    "apscheduler": lambda m: None,
    "apscheduler.jobstores": lambda m: None,
    "apscheduler.jobstores.sqlalchemy": lambda m: setattr(m, "SQLAlchemyJobStore", MagicMock),
    "apscheduler.schedulers": lambda m: None,
    "apscheduler.schedulers.background": lambda m: setattr(m, "BackgroundScheduler", MagicMock),
    "apscheduler.triggers": lambda m: None,
    "apscheduler.triggers.cron": lambda m: setattr(m, "CronTrigger", MagicMock),
    "sqlalchemy": lambda m: None,
}
for mod_name, setup_fn in _mock_modules.items():
    if mod_name not in sys.modules:
        fake = types.ModuleType(mod_name)
        setup_fn(fake)
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

    # Clear rate limiter state between tests
    web_app._login_attempts.clear()

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
        "adhan_audio_files": {
            "Fajr": "adhan_fajr_rec2_saba.mp3",
            "Dhuhr": "adhan_dhuhr_rec2_ussak.mp3",
            "Asr": "adhan_asr_rec2_rast.mp3",
            "Maghrib": "adhan_maghrib_rec2_segah.mp3",
            "Isha": "adhan_isha_rec2_hicaz.mp3",
        },
    }
