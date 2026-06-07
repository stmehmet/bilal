"""Tests for the heartbeat dead-man's switch and its wiring into playback."""

import pytest

import heartbeat
import adhan_scheduler as sched


@pytest.fixture(autouse=True)
def _healthy_disk_by_default(monkeypatch):
    """Default every heartbeat test to a healthy disk.

    ping_success() now suppresses the ping when the data volume is critically
    full, so without this a test machine that happened to be near-full would
    spuriously fail the ping tests.  The disk-guard tests override this.
    """
    monkeypatch.setattr(
        heartbeat.diskspace, "usage",
        lambda: {"total_bytes": 10**12, "free_bytes": 8 * 10**11, "free_pct": 80.0},
    )


class _SyncThread:
    """Run the thread target synchronously so the ping is observable in-test."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        if self._target:
            self._target(*self._args)


class _CountingThread:
    """Count how many threads get started without running anything."""

    started = 0

    def __init__(self, *a, **k):
        pass

    def start(self):
        type(self).started += 1


class TestHeartbeatPing:
    def test_noop_when_url_unset(self, monkeypatch):
        monkeypatch.delenv("HEALTHCHECK_PING_URL", raising=False)
        _CountingThread.started = 0
        monkeypatch.setattr(heartbeat.threading, "Thread", _CountingThread)
        heartbeat.ping_success()
        assert _CountingThread.started == 0  # never even spawns a thread

    def test_blank_url_is_noop(self, monkeypatch):
        monkeypatch.setenv("HEALTHCHECK_PING_URL", "   ")
        _CountingThread.started = 0
        monkeypatch.setattr(heartbeat.threading, "Thread", _CountingThread)
        heartbeat.ping_success()
        assert _CountingThread.started == 0

    def test_pings_configured_url(self, monkeypatch):
        monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc.example/abc")
        monkeypatch.setattr(heartbeat.threading, "Thread", _SyncThread)
        seen = {}
        monkeypatch.setattr(
            heartbeat.requests, "get",
            lambda url, timeout=None: seen.update(url=url, timeout=timeout),
        )
        heartbeat.ping_success()
        assert seen["url"] == "https://hc.example/abc"
        assert seen["timeout"] == heartbeat.PING_TIMEOUT_SECONDS

    def test_ping_swallows_network_errors(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(heartbeat.requests, "get", _boom)
        heartbeat._ping("https://hc.example/abc")  # must not raise


class TestPlaybackHeartbeatWiring:
    def _cfg(self):
        return {
            "volume": 0.5,
            "timezone": "UTC",
            "speakers": {"Living Room": {"enabled": True}},
        }

    def test_pings_on_successful_playback(self, monkeypatch):
        monkeypatch.setattr(sched, "_resolve_devices",
                            lambda speakers, enabled, prayer: {"Living Room": object()})
        monkeypatch.setattr(sched, "play_on_all",
                            lambda devices, enabled, *a, **k: {"Living Room": True})
        pinged = {"n": 0}
        monkeypatch.setattr(sched.heartbeat, "ping_success",
                            lambda: pinged.__setitem__("n", pinged["n"] + 1))
        sched._play_on_speakers("http://x/a.mp3", self._cfg(), "Adhan (Fajr)", prayer_name="Fajr")
        assert pinged["n"] == 1

    def test_no_ping_when_all_speakers_fail(self, monkeypatch):
        monkeypatch.setattr(sched, "_resolve_devices", lambda *a, **k: {})
        monkeypatch.setattr(sched, "play_on_all",
                            lambda devices, enabled, *a, **k: {n: False for n in enabled})
        pinged = {"n": 0}
        monkeypatch.setattr(sched.heartbeat, "ping_success",
                            lambda: pinged.__setitem__("n", pinged["n"] + 1))
        sched._play_on_speakers("http://x/a.mp3", self._cfg(), "Adhan (Fajr)", prayer_name="Fajr")
        assert pinged["n"] == 0


class TestHeartbeatDiskGuard:
    """A critically-full disk must suppress the ping so the switch can fire."""

    @staticmethod
    def _usage(free_pct, free_bytes):
        return {"total_bytes": 10**12, "free_pct": free_pct, "free_bytes": free_bytes}

    def test_low_by_percentage(self):
        assert heartbeat._is_critically_low(self._usage(4.9, 10**12)) is True

    def test_low_by_absolute_bytes(self):
        # Healthy percentage but under the 500 MB floor (large-volume guard).
        assert heartbeat._is_critically_low(self._usage(50.0, 100 * 1024 * 1024)) is True

    def test_healthy_disk_is_not_low(self):
        assert heartbeat._is_critically_low(self._usage(50.0, 10**12)) is False

    def test_threshold_boundary_is_not_low(self):
        # Exactly at the floors counts as healthy (strict less-than).
        assert heartbeat._is_critically_low(
            self._usage(heartbeat.CRITICAL_FREE_PCT, heartbeat.CRITICAL_FREE_BYTES)
        ) is False

    def test_unknown_usage_is_not_low(self):
        # A failed probe must never trip the switch on its own.
        assert heartbeat._is_critically_low(None) is False

    def test_suppresses_ping_when_disk_low(self, monkeypatch):
        monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc.example/abc")
        monkeypatch.setattr(heartbeat.diskspace, "usage",
                            lambda: self._usage(2.0, 50 * 1024 * 1024))
        _CountingThread.started = 0
        monkeypatch.setattr(heartbeat.threading, "Thread", _CountingThread)
        heartbeat.ping_success()
        assert _CountingThread.started == 0  # no ping → dead-man's switch fires

    def test_pings_when_disk_healthy(self, monkeypatch):
        monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc.example/abc")
        monkeypatch.setattr(heartbeat.diskspace, "usage",
                            lambda: self._usage(80.0, 10**12))
        _CountingThread.started = 0
        monkeypatch.setattr(heartbeat.threading, "Thread", _CountingThread)
        heartbeat.ping_success()
        assert _CountingThread.started == 1

    def test_pings_when_usage_unknown(self, monkeypatch):
        monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc.example/abc")
        monkeypatch.setattr(heartbeat.diskspace, "usage", lambda: None)
        _CountingThread.started = 0
        monkeypatch.setattr(heartbeat.threading, "Thread", _CountingThread)
        heartbeat.ping_success()
        assert _CountingThread.started == 1

    def test_logs_warning_when_disk_low(self, monkeypatch, caplog):
        monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc.example/abc")
        monkeypatch.setattr(heartbeat.diskspace, "usage",
                            lambda: self._usage(2.0, 50 * 1024 * 1024))
        monkeypatch.setattr(heartbeat.threading, "Thread", _CountingThread)
        with caplog.at_level("WARNING"):
            heartbeat.ping_success()
        assert "Disk critically low" in caplog.text
