"""Tests for per-speaker adhan + iqamah schedules and SmartThings cleanup."""

import json

import pytest

import config as cfg


class TestDeprecatedConfigKeys:
    def test_smartthings_keys_stripped_on_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
        # Simulate a legacy config left on disk by an older deployed unit
        (tmp_path / "config.json").write_text(json.dumps({
            "smartthings_token": "xyz",
            "smartthings_device_id": "abc",
            "latitude": 30.0,
            "longitude": -97.0,
        }))
        loaded = cfg.load_config()
        assert "smartthings_token" not in loaded
        assert "smartthings_device_id" not in loaded
        # Real values survive
        assert loaded["latitude"] == 30.0
        # And the strip is persisted so subsequent loads don't re-run it
        on_disk = json.loads((tmp_path / "config.json").read_text())
        assert "smartthings_token" not in on_disk


class TestIqamahSchedule:
    def test_saves_iqamah_schedule_field(self, logged_in_client, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
        cfg.save_config({
            "speakers": {"Main": {"enabled": True}},
        })
        resp = logged_in_client.post("/api/speakers", json={
            "Main": {"iqamah_schedule": {"Fajr": [0, 1, 2, 3, 4], "Dhuhr": None}},
        })
        assert resp.status_code == 200
        stored = cfg.load_config()["speakers"]["Main"]
        assert stored["iqamah_schedule"]["Fajr"] == [0, 1, 2, 3, 4]
        assert stored["iqamah_schedule"]["Dhuhr"] is None

    def test_adhan_and_iqamah_schedules_are_independent(self, logged_in_client, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
        cfg.save_config({"speakers": {"Main": {"enabled": True}}})
        # Set adhan schedule to weekdays only, iqamah schedule to weekends only
        logged_in_client.post("/api/speakers", json={
            "Main": {
                "schedule": {"Fajr": [0, 1, 2, 3, 4]},
                "iqamah_schedule": {"Fajr": [5, 6]},
            },
        })
        stored = cfg.load_config()["speakers"]["Main"]
        assert stored["schedule"]["Fajr"] == [0, 1, 2, 3, 4]
        assert stored["iqamah_schedule"]["Fajr"] == [5, 6]

    def test_clearing_iqamah_schedule_removes_key(self, logged_in_client, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
        cfg.save_config({
            "speakers": {"Main": {
                "enabled": True,
                "iqamah_schedule": {"Fajr": [5, 6]},
            }},
        })
        logged_in_client.post("/api/speakers", json={"Main": {"iqamah_schedule": None}})
        stored = cfg.load_config()["speakers"]["Main"]
        assert "iqamah_schedule" not in stored

    def test_apply_all_respects_kind(self, logged_in_client, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
        cfg.save_config({
            "speakers": {
                "A": {"enabled": True},
                "B": {"enabled": True},
            },
        })
        logged_in_client.post("/api/speakers/schedule/apply-all", json={
            "schedule": {"Fajr": [4]},
            "kind": "iqamah",
        })
        speakers = cfg.load_config()["speakers"]
        assert speakers["A"]["iqamah_schedule"]["Fajr"] == [4]
        assert speakers["B"]["iqamah_schedule"]["Fajr"] == [4]
        # Adhan schedule untouched
        assert "schedule" not in speakers["A"]

    def test_apply_all_defaults_to_adhan_kind(self, logged_in_client, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
        cfg.save_config({"speakers": {"A": {"enabled": True}}})
        logged_in_client.post("/api/speakers/schedule/apply-all", json={
            "schedule": {"Fajr": [1, 2]},
        })
        stored = cfg.load_config()["speakers"]["A"]
        assert stored["schedule"]["Fajr"] == [1, 2]
        assert "iqamah_schedule" not in stored


class TestScheduleFilter:
    """_filter_by_schedule honours schedule_key and falls back correctly."""

    def test_iqamah_inherits_adhan_when_unset(self, monkeypatch):
        import datetime
        import adhan_scheduler
        # Force "today" to Wednesday (weekday=2)
        fake_now = datetime.datetime(2026, 4, 22, 12, 0, tzinfo=datetime.timezone.utc)
        monkeypatch.setattr(
            adhan_scheduler.datetime, "datetime",
            type("dt", (), {
                "now": staticmethod(lambda tz=None: fake_now.astimezone(tz) if tz else fake_now),
            })(),
        )
        speakers = {
            "A": {"schedule": {"Fajr": [0, 1, 2]}},  # Mon/Tue/Wed
            "B": {"schedule": {"Fajr": [5, 6]}},     # weekends only
        }
        # No iqamah_schedule set: both fall back to adhan schedule
        result = adhan_scheduler._filter_by_schedule(
            ["A", "B"], speakers, "Fajr",
            timezone="UTC", schedule_key="iqamah_schedule",
        )
        assert result == ["A"]

    def test_iqamah_overrides_adhan_when_set(self, monkeypatch):
        import datetime
        import adhan_scheduler
        fake_now = datetime.datetime(2026, 4, 25, 12, 0, tzinfo=datetime.timezone.utc)  # Saturday
        monkeypatch.setattr(
            adhan_scheduler.datetime, "datetime",
            type("dt", (), {
                "now": staticmethod(lambda tz=None: fake_now.astimezone(tz) if tz else fake_now),
            })(),
        )
        speakers = {
            "A": {
                "schedule": {"Fajr": [0, 1, 2, 3, 4]},   # weekdays only for adhan
                "iqamah_schedule": {"Fajr": [5, 6]},     # weekends only for iqamah
            },
        }
        # Saturday: adhan would skip, iqamah plays
        assert adhan_scheduler._filter_by_schedule(
            ["A"], speakers, "Fajr", timezone="UTC", schedule_key="schedule",
        ) == []
        assert adhan_scheduler._filter_by_schedule(
            ["A"], speakers, "Fajr", timezone="UTC", schedule_key="iqamah_schedule",
        ) == ["A"]


class TestLocalIPValidation:
    def test_loopback_returns_none(self, monkeypatch):
        import adhan_scheduler
        import socket

        class FakeSocket:
            def __init__(self, *a, **kw): pass
            def settimeout(self, *a): pass
            def connect(self, *a): pass
            def getsockname(self): return ("127.0.0.1", 0)
            def close(self): pass

        monkeypatch.setattr(socket, "socket", FakeSocket)
        assert adhan_scheduler._get_local_ip() is None

    def test_valid_ip_returned(self, monkeypatch):
        import adhan_scheduler
        import socket

        class FakeSocket:
            def __init__(self, *a, **kw): pass
            def settimeout(self, *a): pass
            def connect(self, *a): pass
            def getsockname(self): return ("192.168.1.50", 0)
            def close(self): pass

        monkeypatch.setattr(socket, "socket", FakeSocket)
        assert adhan_scheduler._get_local_ip() == "192.168.1.50"

    def test_socket_failure_returns_none(self, monkeypatch):
        import adhan_scheduler
        import socket

        class BrokenSocket:
            def __init__(self, *a, **kw): raise OSError("no network")

        monkeypatch.setattr(socket, "socket", BrokenSocket)
        assert adhan_scheduler._get_local_ip() is None
