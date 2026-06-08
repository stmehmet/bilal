"""Tests for configuration load/save and DND logic."""

import datetime
import json

import pytest

import config as cfg
from adhan_scheduler import _is_dnd_active


# ---------------------------------------------------------------------------
# Config loading / saving
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        cfg.CONFIG_FILE = tmp_path / "config.json"
        result = cfg.load_config()
        assert result["timezone"] == "UTC"
        assert result["volume"] == 0.5
        assert result["setup_complete"] is False
        assert result["latitude"] is None

    def test_merges_stored_values_with_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        cfg.CONFIG_FILE = tmp_path / "config.json"
        cfg.CONFIG_DIR = tmp_path
        cfg.save_config({"latitude": 21.0, "longitude": 39.0})
        result = cfg.load_config()
        assert result["latitude"] == 21.0
        assert result["longitude"] == 39.0
        # Defaults for unset keys
        assert result["timezone"] == "UTC"
        assert result["volume"] == 0.5

    def test_handles_corrupt_json_gracefully(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        cfg.CONFIG_FILE = tmp_path / "config.json"
        (tmp_path / "config.json").write_text("NOT_VALID_JSON")
        result = cfg.load_config()
        # Should fall back to defaults
        assert result["timezone"] == "UTC"

    def test_corrupt_config_is_quarantined_not_lost(self, tmp_path, monkeypatch):
        """A corrupt config must be preserved for inspection, not silently reset.

        Silently falling back to defaults is how a unit goes dark unnoticed; the
        bad file should be kept so the failure is loud and recoverable.
        """
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        cfg.CONFIG_FILE = tmp_path / "config.json"
        (tmp_path / "config.json").write_text("NOT_VALID_JSON{{{")
        result = cfg.load_config()
        assert result["timezone"] == "UTC"  # still usable on defaults
        backup = tmp_path / "config.json.corrupt"
        assert backup.exists()
        assert backup.read_text() == "NOT_VALID_JSON{{{"

    def test_empty_config_file_falls_back_to_defaults(self, tmp_path, monkeypatch):
        """A 0-byte config.json (exactly what an ENOSPC truncation produced on a
        live unit) must not crash load_config."""
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        cfg.CONFIG_FILE = tmp_path / "config.json"
        (tmp_path / "config.json").write_text("")
        result = cfg.load_config()
        assert result["latitude"] is None
        assert result["timezone"] == "UTC"


class TestSaveConfig:
    def test_persists_and_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        cfg.CONFIG_DIR = tmp_path
        cfg.CONFIG_FILE = tmp_path / "config.json"
        data = {"latitude": 51.5, "longitude": -0.1, "volume": 0.8}
        cfg.save_config(data)
        raw = json.loads((tmp_path / "config.json").read_text())
        assert raw["latitude"] == 51.5
        assert raw["volume"] == 0.8

    def test_creates_missing_directory(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "dir"
        monkeypatch.setenv("CONFIG_DIR", str(nested))
        cfg.CONFIG_DIR = nested
        cfg.CONFIG_FILE = nested / "config.json"
        cfg.save_config({"foo": "bar"})
        assert (nested / "config.json").exists()

    def test_no_temp_file_left_after_success(self, tmp_path):
        cfg.CONFIG_DIR = tmp_path
        cfg.CONFIG_FILE = tmp_path / "config.json"
        cfg.save_config({"latitude": 5.0})
        # The atomic-rename temp must not linger after a normal write.
        assert not (tmp_path / "config.json.tmp").exists()
        assert json.loads((tmp_path / "config.json").read_text())["latitude"] == 5.0

    def test_failed_write_preserves_existing_config(self, tmp_path, monkeypatch):
        """The regression test for the outage: a write that fails mid-flight
        (ENOSPC) must NOT destroy the config already on disk."""
        cfg.CONFIG_DIR = tmp_path
        cfg.CONFIG_FILE = tmp_path / "config.json"
        cfg.save_config({"latitude": 1.0, "city": "Original"})

        def enospc(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(cfg.json, "dump", enospc)
        with pytest.raises(cfg.ConfigWriteError):
            cfg.save_config({"latitude": 2.0, "city": "Replacement"})

        # Original content fully intact — not truncated to 0 bytes.
        raw = json.loads((tmp_path / "config.json").read_text())
        assert raw["city"] == "Original"
        assert raw["latitude"] == 1.0

    def test_failed_write_leaves_no_temp_file(self, tmp_path, monkeypatch):
        cfg.CONFIG_DIR = tmp_path
        cfg.CONFIG_FILE = tmp_path / "config.json"

        def enospc(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(cfg.json, "dump", enospc)
        with pytest.raises(cfg.ConfigWriteError):
            cfg.save_config({"latitude": 2.0})
        assert not (tmp_path / "config.json.tmp").exists()

    def test_config_write_error_is_oserror(self):
        # Existing ``except OSError`` handlers (e.g. in the scheduler) must still
        # catch it, so the scheduler doesn't crash-loop on a save failure.
        assert issubclass(cfg.ConfigWriteError, OSError)


# ---------------------------------------------------------------------------
# Do Not Disturb logic
# ---------------------------------------------------------------------------

def _dnd_config(enabled, start, end, tz="UTC"):
    return {"dnd_enabled": enabled, "dnd_start": start, "dnd_end": end, "timezone": tz}


class TestDNDCheck:
    def test_dnd_disabled_always_false(self):
        c = _dnd_config(False, "23:00", "05:00")
        assert _is_dnd_active(c) is False

    def test_within_same_day_window(self, monkeypatch):
        # DND 10:00–12:00, current time 11:00
        c = _dnd_config(True, "10:00", "12:00")
        import adhan_scheduler
        fake_now = datetime.datetime(2024, 1, 15, 11, 0, tzinfo=datetime.timezone.utc)
        monkeypatch.setattr(adhan_scheduler.datetime, "datetime",
                            type("dt", (), {"now": staticmethod(lambda tz=None: fake_now.astimezone(tz) if tz else fake_now)})())
        assert _is_dnd_active(c) is True

    def test_outside_same_day_window(self, monkeypatch):
        # DND 10:00–12:00, current time 13:00
        c = _dnd_config(True, "10:00", "12:00")
        import adhan_scheduler
        fake_now = datetime.datetime(2024, 1, 15, 13, 0, tzinfo=datetime.timezone.utc)
        monkeypatch.setattr(adhan_scheduler.datetime, "datetime",
                            type("dt", (), {"now": staticmethod(lambda tz=None: fake_now.astimezone(tz) if tz else fake_now)})())
        assert _is_dnd_active(c) is False

    def test_overnight_window_after_start(self, monkeypatch):
        # DND 23:00–05:00, current time 23:30
        c = _dnd_config(True, "23:00", "05:00")
        import adhan_scheduler
        fake_now = datetime.datetime(2024, 1, 15, 23, 30, tzinfo=datetime.timezone.utc)
        monkeypatch.setattr(adhan_scheduler.datetime, "datetime",
                            type("dt", (), {"now": staticmethod(lambda tz=None: fake_now.astimezone(tz) if tz else fake_now)})())
        assert _is_dnd_active(c) is True

    def test_overnight_window_before_end(self, monkeypatch):
        # DND 23:00–05:00, current time 03:00
        c = _dnd_config(True, "23:00", "05:00")
        import adhan_scheduler
        fake_now = datetime.datetime(2024, 1, 15, 3, 0, tzinfo=datetime.timezone.utc)
        monkeypatch.setattr(adhan_scheduler.datetime, "datetime",
                            type("dt", (), {"now": staticmethod(lambda tz=None: fake_now.astimezone(tz) if tz else fake_now)})())
        assert _is_dnd_active(c) is True

    def test_bad_time_format_returns_false(self):
        c = _dnd_config(True, "not-a-time", "also-bad")
        assert _is_dnd_active(c) is False
