"""Tests for the playback_log module and its HTTP surface."""

import datetime
import json
from pathlib import Path

import pytest

import playback_log


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Redirect playback_log.LOG_FILE into tmp_path so tests are hermetic."""
    target = tmp_path / "playback.log.jsonl"
    monkeypatch.setattr(playback_log, "LOG_FILE", target)
    return tmp_path


class TestRecord:
    def test_appends_jsonl_entry(self, log_dir):
        playback_log.record("adhan", "Fajr", "Downstairs", True, 2.5)
        entries = list(log_dir.glob("playback.log.jsonl"))
        assert len(entries) == 1
        lines = entries[0].read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "adhan"
        assert entry["prayer"] == "Fajr"
        assert entry["speaker"] == "Downstairs"
        assert entry["ok"] is True
        assert entry["elapsed_ms"] == 2500

    def test_appends_multiple(self, log_dir):
        for i in range(5):
            playback_log.record("iqamah", "Dhuhr", f"Speaker{i}", i % 2 == 0, 1.0)
        content = (log_dir / "playback.log.jsonl").read_text().strip().split("\n")
        assert len(content) == 5

    def test_records_error_field(self, log_dir):
        playback_log.record("adhan", "Isha", "Test", False, 45.0, error="timeout")
        entry = json.loads((log_dir / "playback.log.jsonl").read_text().strip())
        assert entry["ok"] is False
        assert entry["error"] == "timeout"


class TestQuery:
    def test_returns_empty_when_no_file(self, log_dir):
        assert playback_log.query() == []

    def test_returns_newest_first(self, log_dir):
        playback_log.record("adhan", "Fajr", "A", True, 1.0)
        playback_log.record("adhan", "Dhuhr", "A", True, 1.0)
        results = playback_log.query()
        assert len(results) == 2
        assert results[0]["prayer"] == "Dhuhr"  # Newest first
        assert results[1]["prayer"] == "Fajr"

    def test_filters_by_speaker(self, log_dir):
        playback_log.record("adhan", "Fajr", "Speaker A", True, 1.0)
        playback_log.record("adhan", "Fajr", "Speaker B", True, 1.0)
        results = playback_log.query(speaker="Speaker B")
        assert len(results) == 1
        assert results[0]["speaker"] == "Speaker B"

    def test_respects_limit(self, log_dir):
        for i in range(10):
            playback_log.record("adhan", "Fajr", f"Sp{i}", True, 1.0)
        assert len(playback_log.query(limit=3)) == 3

    def test_excludes_expired(self, log_dir):
        # Write an entry with a very old timestamp directly
        old_ts = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=30)).isoformat()
        (log_dir / "playback.log.jsonl").write_text(
            json.dumps({"ts": old_ts, "event": "adhan", "prayer": "Fajr",
                        "speaker": "X", "ok": True, "elapsed_ms": 1000, "error": None}) + "\n"
        )
        playback_log.record("adhan", "Dhuhr", "Y", True, 1.0)
        results = playback_log.query()
        assert len(results) == 1
        assert results[0]["prayer"] == "Dhuhr"

    def test_skips_malformed_lines(self, log_dir):
        (log_dir / "playback.log.jsonl").write_text(
            "not json\n"
            + json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "event": "adhan", "prayer": "Fajr", "speaker": "OK",
                "ok": True, "elapsed_ms": 1000, "error": None,
            }) + "\n"
        )
        results = playback_log.query()
        assert len(results) == 1
        assert results[0]["speaker"] == "OK"


class TestPurge:
    def test_removes_old_entries(self, log_dir):
        old_ts = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=30)).isoformat()
        entries = [
            json.dumps({"ts": old_ts, "event": "adhan", "prayer": "Fajr",
                        "speaker": "Old", "ok": True, "elapsed_ms": 1, "error": None}),
            json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "event": "adhan", "prayer": "Dhuhr", "speaker": "New",
                "ok": True, "elapsed_ms": 1, "error": None,
            }),
        ]
        (log_dir / "playback.log.jsonl").write_text("\n".join(entries) + "\n")
        removed = playback_log.purge(older_than_days=7)
        assert removed == 1
        remaining = playback_log.query()
        assert len(remaining) == 1
        assert remaining[0]["speaker"] == "New"

    def test_purge_zero_days_clears_all(self, log_dir):
        playback_log.record("adhan", "Fajr", "A", True, 1.0)
        playback_log.record("adhan", "Dhuhr", "B", True, 1.0)
        removed = playback_log.purge(older_than_days=0)
        assert removed == 2
        assert playback_log.query() == []


class TestHttpSurface:
    def test_get_returns_entries(self, logged_in_client, tmp_path, monkeypatch):
        monkeypatch.setattr(playback_log, "LOG_FILE", tmp_path / "playback.log.jsonl")
        playback_log.record("adhan", "Fajr", "Living room", True, 2.0)
        resp = logged_in_client.get("/api/playback-log")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 1
        assert data["entries"][0]["speaker"] == "Living room"

    def test_get_filters_by_speaker(self, logged_in_client, tmp_path, monkeypatch):
        monkeypatch.setattr(playback_log, "LOG_FILE", tmp_path / "playback.log.jsonl")
        playback_log.record("adhan", "Fajr", "A", True, 1.0)
        playback_log.record("adhan", "Fajr", "B", True, 1.0)
        resp = logged_in_client.get("/api/playback-log?speaker=B")
        data = resp.get_json()
        assert data["count"] == 1
        assert data["entries"][0]["speaker"] == "B"

    def test_delete_purges(self, logged_in_client, tmp_path, monkeypatch):
        monkeypatch.setattr(playback_log, "LOG_FILE", tmp_path / "playback.log.jsonl")
        playback_log.record("adhan", "Fajr", "A", True, 1.0)
        resp = logged_in_client.delete("/api/playback-log?older_than_days=0")
        assert resp.status_code == 200
        assert resp.get_json()["removed"] == 1
        assert playback_log.query() == []

    def test_requires_auth(self, app_client):
        assert app_client.get("/api/playback-log").status_code == 302
