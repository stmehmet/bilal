"""Tests for the cast playback fan-out — timeout, abort, no-double-log."""

import threading
import time
from unittest.mock import MagicMock

import pytest

import discovery


def _make_device(name: str, *, status_after_wait=True):
    """A MagicMock shaped like a pychromecast.Chromecast.

    ``status_after_wait`` mimics whether the connection actually completed —
    set to ``None`` to force ``_play_once`` to raise TimeoutError.
    """
    dev = MagicMock()
    dev.cast_info.friendly_name = name
    dev.cast_info.cast_type = "cast"
    dev.status = "ok" if status_after_wait else None
    dev.wait = MagicMock(return_value=None)
    dev.set_volume = MagicMock()
    dev.disconnect = MagicMock()
    dev.media_controller = MagicMock()
    dev.media_controller.play_media = MagicMock()
    dev.media_controller.block_until_active = MagicMock()
    return dev


class TestPlayOnceTimeout:
    def test_raises_when_device_not_ready_after_wait(self):
        dev = _make_device("Hung", status_after_wait=False)
        with pytest.raises(TimeoutError):
            discovery._play_once(dev, "http://x/a.mp3", "audio/mpeg", 0.5)
        # set_volume / play_media must not be invoked against a dead socket
        dev.set_volume.assert_not_called()
        dev.media_controller.play_media.assert_not_called()

    def test_passes_explicit_wait_timeout(self):
        dev = _make_device("Ready")
        discovery._play_once(dev, "http://x/a.mp3", "audio/mpeg", 0.5)
        # First positional/kwarg call to wait() must carry the cap
        kwargs = dev.wait.call_args.kwargs
        assert kwargs.get("timeout") == discovery.CAST_WAIT_TIMEOUT_SECONDS


class TestPlayOnAllTimeout:
    """play_on_all must enforce PLAY_DEADLINE_SECONDS as an absolute budget,
    record a timeout for each laggard, and disconnect their casts so the
    daemon thread can't replay the adhan hours later."""

    def test_timeout_reported_and_device_disconnected(self, monkeypatch):
        # Compress the deadline so the test is fast — the behaviour we care
        # about (timeout → disconnect → no phantom log) is independent of
        # the actual number of seconds.
        monkeypatch.setattr(discovery, "PLAY_DEADLINE_SECONDS", 1)

        # Simulate a worker that never returns within the deadline.
        block = threading.Event()

        def _hang(*args, **kwargs):
            block.wait(timeout=10)  # released by the test at the end
            return True

        monkeypatch.setattr(discovery, "play_on_chromecast", _hang)

        dev = _make_device("Hung")
        results_logged: list[tuple] = []

        def on_result(name, ok, elapsed, error):
            results_logged.append((name, ok, elapsed, error))

        t0 = time.time()
        results = discovery.play_on_all(
            {"Hung": dev}, ["Hung"], "http://x/a.mp3",
            on_result=on_result,
        )
        elapsed_total = time.time() - t0

        assert results == {"Hung": False}
        assert elapsed_total < 3  # we used a 1s deadline; even with disconnect overhead
        dev.disconnect.assert_called_once()
        # The parent's timeout row is the only one logged.
        assert len(results_logged) == 1
        assert results_logged[0][:2] == ("Hung", False)
        assert results_logged[0][3] == "timeout"

        block.set()  # let the worker unwind so the daemon thread can exit

    def test_no_phantom_log_when_worker_completes_after_deadline(self, monkeypatch):
        """The headline bug: hung daemon threads would complete hours later
        and log a second 'OK' row, prompting the speaker to play the adhan
        at 3 AM.  After the deadline, on_result must never fire for that
        speaker again — even if the worker eventually returns success."""
        monkeypatch.setattr(discovery, "PLAY_DEADLINE_SECONDS", 1)

        late_done = threading.Event()
        worker_entered = threading.Event()

        def _late_success(*args, **kwargs):
            worker_entered.set()
            # Block past the deadline, then return True — the late completion
            # the bug-report screenshot was reading as a 3 AM "OK".
            time.sleep(2)
            late_done.set()
            return True

        monkeypatch.setattr(discovery, "play_on_chromecast", _late_success)

        dev = _make_device("Slow")
        on_result_calls: list[tuple] = []

        def on_result(name, ok, elapsed, error):
            on_result_calls.append((name, ok, elapsed, error))

        discovery.play_on_all(
            {"Slow": dev}, ["Slow"], "http://x/a.mp3",
            on_result=on_result,
        )

        # Wait until the worker actually finishes, then give the discarding
        # branch a beat to execute.  If the fix is wrong, on_result would
        # see a second invocation here.
        assert late_done.wait(timeout=5)
        time.sleep(0.2)

        timeouts = [c for c in on_result_calls if c[3] == "timeout"]
        non_timeouts = [c for c in on_result_calls if c[3] != "timeout"]
        assert len(timeouts) == 1, on_result_calls
        assert non_timeouts == [], (
            "Late worker completion logged a phantom result — this is the "
            "3 AM mass-replay bug"
        )

    def test_absolute_deadline_does_not_compound_across_speakers(self, monkeypatch):
        """With N hung speakers, total wall-clock must stay near the
        deadline.  Per-thread join(timeout=...) compounded to N*deadline
        and let workers stay alive long enough to fire after the next
        Nest reboot."""
        monkeypatch.setattr(discovery, "PLAY_DEADLINE_SECONDS", 1)

        block = threading.Event()

        def _hang(*args, **kwargs):
            block.wait(timeout=10)
            return True

        monkeypatch.setattr(discovery, "play_on_chromecast", _hang)

        devices = {f"S{i}": _make_device(f"S{i}") for i in range(4)}
        t0 = time.time()
        discovery.play_on_all(devices, list(devices), "http://x/a.mp3")
        elapsed = time.time() - t0
        block.set()

        # Absolute budget = 1s.  Allow generous slack for thread startup +
        # the disconnect loop, but reject anything that smells like N*1s.
        assert elapsed < 2.5, f"play_on_all took {elapsed:.2f}s — deadline is compounding"

    def test_fast_speaker_still_returns_ok(self, monkeypatch):
        """Sanity check: the abandon logic must not break the happy path."""
        dev = _make_device("Fast")

        def on_result(name, ok, elapsed, error):
            pass

        results = discovery.play_on_all(
            {"Fast": dev}, ["Fast"], "http://x/a.mp3",
            on_result=on_result,
        )
        assert results == {"Fast": True}
        dev.disconnect.assert_not_called()


class TestPlayOnChromecastRetriesTimeout:
    def test_timeout_error_triggers_retry(self, monkeypatch):
        """A TimeoutError on the first attempt must be caught and retried —
        the bug pattern was a stuck device.wait() with no recovery path."""
        dev = _make_device("Flaky")
        calls = []

        def _play_once(device, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("wait timed out")
            return True

        monkeypatch.setattr(discovery, "_play_once", _play_once)
        # Shrink the retry sleep so the test isn't slow
        monkeypatch.setattr(discovery.time, "sleep", lambda *_: None)

        ok = discovery.play_on_chromecast(dev, "http://x/a.mp3")
        assert ok is True
        assert len(calls) == 2


class TestDisconnectAll:
    """Disconnecting devices after use is what prevents the pychromecast
    socket-worker reconnect storm that floods logs and fills the disk."""

    def test_disconnects_every_device(self):
        d1 = _make_device("A")
        d2 = _make_device("B")
        discovery.disconnect_all({"A": d1, "B": d2})
        d1.disconnect.assert_called_once()
        d2.disconnect.assert_called_once()

    def test_swallows_disconnect_errors(self):
        bad = _make_device("Bad")
        bad.disconnect.side_effect = RuntimeError("boom")
        discovery.disconnect_all({"Bad": bad})  # must not raise

    def test_empty_map_is_noop(self):
        discovery.disconnect_all({})  # must not raise


class TestPlayOnSpeakersReleasesConnections:
    """``_play_on_speakers`` must disconnect every device it opened — leaving
    them connected is what let worker threads accumulate into the 53GB-log storm.
    """

    def _config(self):
        return {
            "volume": 0.5,
            "timezone": "UTC",
            "speakers": {"Office": {"enabled": True}},
        }

    def test_disconnects_after_successful_play(self, monkeypatch):
        import adhan_scheduler as sch

        dev = _make_device("Office")
        monkeypatch.setattr(sch, "_filter_by_schedule", lambda enabled, *a, **k: enabled)
        monkeypatch.setattr(sch, "_resolve_devices", lambda *a, **k: {"Office": dev})
        monkeypatch.setattr(sch, "play_on_all", lambda *a, **k: {"Office": True})
        monkeypatch.setattr(sch.heartbeat, "ping_success", lambda *a, **k: None)

        sch._play_on_speakers("http://x/a.mp3", self._config(), "Test", "Dhuhr")
        dev.disconnect.assert_called_once()

    def test_disconnects_even_when_play_raises(self, monkeypatch):
        import adhan_scheduler as sch

        dev = _make_device("Office")
        monkeypatch.setattr(sch, "_filter_by_schedule", lambda enabled, *a, **k: enabled)
        monkeypatch.setattr(sch, "_resolve_devices", lambda *a, **k: {"Office": dev})

        def boom(*_a, **_k):
            raise RuntimeError("cast exploded")

        monkeypatch.setattr(sch, "play_on_all", boom)

        # Should swallow the error (logged) AND still disconnect in the finally.
        sch._play_on_speakers("http://x/a.mp3", self._config(), "Test", "Dhuhr")
        dev.disconnect.assert_called_once()
