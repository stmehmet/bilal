"""Tests for the disk-space probe used by the dashboard and the heartbeat."""

import diskspace


def _du(total, free):
    """Build a ``shutil.disk_usage``-style 3-tuple ``(total, used, free)``."""
    return (total, total - free, free)


class TestDiskUsage:
    def test_returns_total_free_and_pct(self, monkeypatch):
        monkeypatch.setattr(diskspace.shutil, "disk_usage", lambda p: _du(1000, 250))
        assert diskspace.usage() == {
            "total_bytes": 1000,
            "free_bytes": 250,
            "free_pct": 25.0,
        }

    def test_pct_rounds_to_one_decimal(self, monkeypatch):
        monkeypatch.setattr(diskspace.shutil, "disk_usage", lambda p: _du(3, 1))
        assert diskspace.usage()["free_pct"] == 33.3  # 33.333… → 33.3

    def test_none_when_stat_fails(self, monkeypatch):
        def _boom(p):
            raise OSError("no such filesystem")

        monkeypatch.setattr(diskspace.shutil, "disk_usage", _boom)
        assert diskspace.usage() is None

    def test_none_on_degenerate_zero_total(self, monkeypatch):
        # A zero-total reading would divide-by-zero; treat it as unreadable.
        monkeypatch.setattr(diskspace.shutil, "disk_usage", lambda p: _du(0, 0))
        assert diskspace.usage() is None

    def test_probes_data_volume_when_present(self, monkeypatch):
        monkeypatch.setattr(diskspace.os.path, "isdir", lambda p: True)
        assert diskspace._probe_path() == diskspace._DATA_PATH

    def test_falls_back_to_root_when_data_volume_absent(self, monkeypatch):
        monkeypatch.setattr(diskspace.os.path, "isdir", lambda p: False)
        assert diskspace._probe_path() == "/"
