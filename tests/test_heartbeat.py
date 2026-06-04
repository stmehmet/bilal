"""Tests for the heartbeat dead-man's switch and its wiring into playback."""

import heartbeat
import adhan_scheduler as sched


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
