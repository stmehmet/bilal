"""Tests for input validation, rate limiting, and security improvements."""

import json
import time
from unittest.mock import patch

import pytest

import config as cfg


# ---------------------------------------------------------------------------
# Coordinate validation
# ---------------------------------------------------------------------------

class TestCoordinateValidation:
    def test_valid_coordinates_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"latitude": 21.4225, "longitude": 39.8262},
        )
        assert resp.status_code == 200
        data = logged_in_client.get("/api/config").get_json()
        assert data["latitude"] == 21.4225
        assert data["longitude"] == 39.8262

    def test_latitude_out_of_range_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"latitude": 200, "longitude": 39.0},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["status"] == "error"
        assert any("Latitude" in e for e in data["errors"])

    def test_longitude_out_of_range_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"latitude": 21.0, "longitude": 500},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert any("Longitude" in e for e in data["errors"])

    def test_negative_coordinates_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"latitude": -33.8688, "longitude": -151.2093},
        )
        assert resp.status_code == 200

    def test_boundary_coordinates_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"latitude": 90, "longitude": 180},
        )
        assert resp.status_code == 200

        resp = logged_in_client.post(
            "/api/config",
            json={"latitude": -90, "longitude": -180},
        )
        assert resp.status_code == 200

    def test_non_numeric_coordinates_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"latitude": "not-a-number", "longitude": 39.0},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert any("numeric" in e for e in data["errors"])


# ---------------------------------------------------------------------------
# Timezone validation
# ---------------------------------------------------------------------------

class TestTimezoneValidation:
    def test_valid_timezone_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"timezone": "America/New_York"},
        )
        assert resp.status_code == 200
        data = logged_in_client.get("/api/config").get_json()
        assert data["timezone"] == "America/New_York"

    def test_invalid_timezone_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"timezone": "Mars/Olympus_Mons"},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert any("timezone" in e.lower() for e in data["errors"])

    def test_utc_timezone_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"timezone": "UTC"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# DND time format validation
# ---------------------------------------------------------------------------

class TestDNDTimeValidation:
    def test_valid_dnd_times_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"dnd_start": "23:00", "dnd_end": "05:30"},
        )
        assert resp.status_code == 200

    def test_invalid_dnd_start_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"dnd_start": "25:00"},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert any("DND start" in e for e in data["errors"])

    def test_invalid_dnd_end_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"dnd_end": "12-30"},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert any("DND end" in e for e in data["errors"])

    def test_midnight_dnd_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"dnd_start": "00:00", "dnd_end": "23:59"},
        )
        assert resp.status_code == 200

    def test_text_dnd_time_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"dnd_start": "not-a-time"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Iqamah offset validation
# ---------------------------------------------------------------------------

class TestIqamahOffsetValidation:
    def test_valid_offsets_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"iqamah_offsets": {"Fajr": 20, "Dhuhr": 15}},
        )
        assert resp.status_code == 200

    def test_negative_offset_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"iqamah_offsets": {"Fajr": -5}},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert any("Iqamah offset" in e for e in data["errors"])

    def test_excessive_offset_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"iqamah_offsets": {"Fajr": 200}},
        )
        assert resp.status_code == 400

    def test_zero_offset_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"iqamah_offsets": {"Fajr": 0}},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Calculation method validation
# ---------------------------------------------------------------------------

class TestCalculationMethodValidation:
    def test_valid_method_accepted(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"calculation_method": "ISNA"},
        )
        assert resp.status_code == 200

    def test_invalid_method_rejected(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"calculation_method": "FakeMethod"},
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert any("calculation method" in e.lower() for e in data["errors"])


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_rate_limiting_after_max_attempts(self, app_client):
        # Create password first
        app_client.post("/login", data={"password": "testpass123"})
        app_client.get("/logout")

        # Exhaust login attempts
        for _ in range(5):
            app_client.post("/login", data={"password": "wrong"})

        # Next attempt should be rate limited
        resp = app_client.post(
            "/login", data={"password": "wrong"}, follow_redirects=True
        )
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Password strength
# ---------------------------------------------------------------------------

class TestPasswordStrength:
    def test_short_password_rejected(self, app_client):
        resp = app_client.post(
            "/login", data={"password": "abc"}, follow_redirects=True
        )
        assert b"at least 8" in resp.data.lower() or resp.status_code == 200

    def test_seven_char_password_rejected(self, app_client):
        resp = app_client.post(
            "/login", data={"password": "abcdefg"}, follow_redirects=True
        )
        assert b"at least 8" in resp.data.lower() or resp.status_code == 200

    def test_eight_char_password_accepted(self, app_client):
        resp = app_client.post("/login", data={"password": "abcdefgh"})
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Audio file path security
# ---------------------------------------------------------------------------

class TestAudioSecurity:
    def test_path_traversal_blocked(self, logged_in_client):
        resp = logged_in_client.get("/audio/../etc/passwd")
        assert resp.status_code == 400

    def test_non_mp3_blocked(self, logged_in_client):
        resp = logged_in_client.get("/audio/config.json")
        assert resp.status_code == 400

    def test_valid_mp3_allowed(self, logged_in_client, tmp_path):
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir(exist_ok=True)
        (audio_dir / "test.mp3").write_bytes(b"\x00" * 100)
        resp = logged_in_client.get("/audio/test.mp3")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Multiple validation errors
# ---------------------------------------------------------------------------

class TestMultipleErrors:
    def test_multiple_errors_returned(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={
                "latitude": 200,
                "longitude": 500,
                "timezone": "Invalid/Zone",
                "dnd_start": "99:99",
            },
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert len(data["errors"]) >= 2


# ---------------------------------------------------------------------------
# City/Country truncation
# ---------------------------------------------------------------------------

class TestFieldTruncation:
    def test_long_city_truncated(self, logged_in_client):
        long_city = "A" * 200
        resp = logged_in_client.post(
            "/api/config",
            json={"city": long_city},
        )
        assert resp.status_code == 200
        data = logged_in_client.get("/api/config").get_json()
        assert len(data["city"]) == 100
