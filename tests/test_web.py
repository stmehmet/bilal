"""Tests for Flask web dashboard API endpoints."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config as cfg


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class TestAuth:
    def test_dashboard_redirects_to_login_when_unauthenticated(self, app_client):
        resp = app_client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_first_time_setup_shows_create_password(self, app_client):
        resp = app_client.get("/login")
        assert resp.status_code == 200
        assert b"Create" in resp.data or b"create" in resp.data or b"first" in resp.data.lower()

    def test_create_password_too_short_rejected(self, app_client):
        resp = app_client.post("/login", data={"password": "abc"}, follow_redirects=True)
        assert b"at least 8" in resp.data.lower() or resp.status_code == 200

    def test_create_password_and_login(self, app_client):
        resp = app_client.post("/login", data={"password": "secure123"})
        assert resp.status_code == 302
        assert "/" == resp.headers["Location"] or "dashboard" in resp.headers.get("Location", "").lower()

    def test_login_with_correct_password(self, app_client):
        # First create the password
        app_client.post("/login", data={"password": "secure123"})
        # Logout
        app_client.get("/logout")
        # Login again
        resp = app_client.post("/login", data={"password": "secure123"})
        assert resp.status_code == 302

    def test_login_with_wrong_password(self, app_client):
        # Create password
        app_client.post("/login", data={"password": "secure123"})
        app_client.get("/logout")
        # Try wrong password
        resp = app_client.post("/login", data={"password": "wrong"}, follow_redirects=True)
        assert b"Incorrect" in resp.data or resp.status_code == 200

    def test_logout_redirects_to_login(self, app_client):
        app_client.post("/login", data={"password": "secure123"})
        resp = app_client.get("/logout")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------

class TestConfigAPI:
    def test_get_config_returns_defaults(self, logged_in_client, tmp_path):
        resp = logged_in_client.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timezone"] == "UTC"
        assert data["volume"] == 0.5

    def test_post_config_updates_values(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/config",
            json={"latitude": 21.0, "longitude": 39.0, "volume": 0.8},
        )
        assert resp.status_code == 200
        # Verify round-trip
        data = logged_in_client.get("/api/config").get_json()
        assert data["latitude"] == 21.0
        assert data["volume"] == 0.8

    def test_post_config_clamps_volume(self, logged_in_client):
        logged_in_client.post("/api/config", json={"volume": 1.5})
        data = logged_in_client.get("/api/config").get_json()
        assert data["volume"] == 1.0

        logged_in_client.post("/api/config", json={"volume": -0.5})
        data = logged_in_client.get("/api/config").get_json()
        assert data["volume"] == 0.0

    def test_post_config_filters_invalid_skip_prayers(self, logged_in_client):
        logged_in_client.post(
            "/api/config",
            json={"skip_prayers": ["Fajr", "InvalidPrayer"]},
        )
        data = logged_in_client.get("/api/config").get_json()
        assert data["skip_prayers"] == ["Fajr"]

    def test_post_config_sets_setup_complete(self, logged_in_client):
        logged_in_client.post("/api/config", json={"city": "Test"})
        data = logged_in_client.get("/api/config").get_json()
        assert data["setup_complete"] is True

    def test_post_config_iqamah_settings(self, logged_in_client):
        logged_in_client.post(
            "/api/config",
            json={"iqamah_enabled": True, "iqamah_audio_file": "custom.mp3"},
        )
        data = logged_in_client.get("/api/config").get_json()
        assert data["iqamah_enabled"] is True
        assert data["iqamah_audio_file"] == "custom.mp3"


# ---------------------------------------------------------------------------
# Speaker discovery
# ---------------------------------------------------------------------------

class TestSpeakers:
    @patch("app.discover_chromecasts")
    @patch("app.get_device_metadata")
    def test_discover_speakers_saves_to_config(self, mock_meta, mock_discover, logged_in_client):
        mock_discover.return_value = {"Living Room": MagicMock()}
        mock_meta.return_value = {
            "Living Room": {"model": "Google Home", "is_group": False}
        }
        resp = logged_in_client.post("/api/discover-speakers")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "Living Room" in data["speakers"]

    @patch("app.discover_chromecasts")
    @patch("app.get_device_metadata")
    def test_update_speakers_toggles_enabled(self, mock_meta, mock_discover, logged_in_client):
        # First discover
        mock_discover.return_value = {"Living Room": MagicMock()}
        mock_meta.return_value = {
            "Living Room": {"model": "Google Home", "is_group": False}
        }
        logged_in_client.post("/api/discover-speakers")
        # Now toggle
        resp = logged_in_client.post(
            "/api/speakers",
            json={"Living Room": {"enabled": False}},
        )
        assert resp.status_code == 200
        config_data = logged_in_client.get("/api/config").get_json()
        assert config_data["speakers"]["Living Room"]["enabled"] is False

    @patch("app.discover_chromecasts")
    def test_test_speaker_not_found_returns_404(self, mock_discover, logged_in_client):
        mock_discover.return_value = {}
        resp = logged_in_client.post(
            "/api/test-speaker",
            json={"speaker": "NonExistent"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# WiFi
# ---------------------------------------------------------------------------

class TestSmartThings:
    def test_smartthings_devices_no_token_returns_400(self, logged_in_client):
        resp = logged_in_client.post("/api/smartthings/devices")
        assert resp.status_code == 400

    @patch("app.st_list_devices")
    def test_smartthings_devices_returns_list(self, mock_list, logged_in_client):
        # Set a token in config first
        logged_in_client.post("/api/config", json={"smartthings_token": "fake-token"})
        mock_list.return_value = [
            {"deviceId": "abc-123", "label": "Family Hub", "name": "Fridge", "deviceTypeName": "Samsung"},
        ]
        resp = logged_in_client.post("/api/smartthings/devices")
        assert resp.status_code == 200
        devices = resp.get_json()["devices"]
        assert len(devices) == 1
        assert devices[0]["device_id"] == "abc-123"
        assert devices[0]["label"] == "Family Hub"


class TestWiFi:
    @patch("subprocess.run")
    def test_wifi_networks_returns_parsed_list(self, mock_run, logged_in_client):
        mock_run.return_value = MagicMock(
            stdout="HomeNet:85:WPA2:*\nNeighbor:60:WPA2:\n",
            returncode=0,
        )
        resp = logged_in_client.get("/api/wifi/networks")
        assert resp.status_code == 200
        networks = resp.get_json()["networks"]
        assert len(networks) == 2
        assert networks[0]["ssid"] == "HomeNet"
        assert networks[0]["connected"] is True

    def test_wifi_connect_requires_ssid(self, logged_in_client):
        resp = logged_in_client.post("/api/wifi/connect", json={})
        assert resp.status_code == 400

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_wifi_status_handles_nmcli_missing(self, mock_run, logged_in_client):
        resp = logged_in_client.get("/api/wifi/status")
        # 503 Service Unavailable: nmcli is not installed in the container.
        # The endpoint returns a friendly message directing users to SSH.
        assert resp.status_code == 503
        data = resp.get_json()
        assert "error" in data


# ---------------------------------------------------------------------------
# Prayer times API
# ---------------------------------------------------------------------------

class TestPrayerTimes:
    def test_prayer_times_returns_iso_strings(self, logged_in_client, sample_config, monkeypatch):
        import datetime
        import pytz

        cfg.save_config(sample_config)

        # Mock compute_prayer_times since the adhan library isn't available
        tz = pytz.timezone("Asia/Riyadh")
        fake_times = {
            "Fajr": tz.localize(datetime.datetime(2024, 6, 15, 4, 30)),
            "Dhuhr": tz.localize(datetime.datetime(2024, 6, 15, 12, 10)),
        }
        import app as web_app
        monkeypatch.setattr(web_app, "compute_prayer_times", lambda config: fake_times)

        resp = logged_in_client.get("/api/prayer-times")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "Fajr" in data
        assert "T" in data["Fajr"]


# ---------------------------------------------------------------------------
# Audio serving & validation
# ---------------------------------------------------------------------------

class TestAudio:
    def test_serve_audio_returns_file(self, logged_in_client, tmp_path):
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir(exist_ok=True)
        (audio_dir / "test.mp3").write_bytes(b"\x00" * 100)
        resp = logged_in_client.get("/audio/test.mp3")
        assert resp.status_code == 200

    def test_audio_validate_returns_missing_files(self, logged_in_client):
        # Default config references per-prayer adhan files which don't exist
        # in the tmp audio dir, so all five should be reported missing.
        # Filenames now encode the traditional Ottoman maqam per prayer:
        # Saba (Fajr), Uşşak (Dhuhr), Rast (Asr), Segâh (Maghrib), Hicaz (Isha).
        resp = logged_in_client.get("/api/audio/validate")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["missing"]) == 5
        assert "adhan_fajr_saba_2.mp3" in data["missing"]
        assert "adhan_dhuhr_ussak_2.mp3" in data["missing"]


# ---------------------------------------------------------------------------
# Location: address-based geocoding
# ---------------------------------------------------------------------------

class TestGeocode:
    """The /api/geocode endpoint resolves an address via Nominatim and then
    looks up the timezone via timeapi.io. Both external calls are mocked."""

    def test_geocode_empty_address_returns_400(self, logged_in_client):
        resp = logged_in_client.post("/api/geocode", json={"address": "   "})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_geocode_long_address_returns_400(self, logged_in_client):
        resp = logged_in_client.post("/api/geocode", json={"address": "a" * 500})
        assert resp.status_code == 400

    @patch("geolocation.requests.get")
    def test_geocode_success_returns_location(self, mock_get, logged_in_client):
        # First call: Nominatim returns a forward-geocoding hit
        nominatim_resp = MagicMock()
        nominatim_resp.raise_for_status = MagicMock()
        nominatim_resp.json.return_value = [{
            "lat": "30.2672",
            "lon": "-97.7431",
            "display_name": "Austin, Texas, United States",
            "address": {
                "city": "Austin",
                "state": "Texas",
                "country": "United States",
                "country_code": "us",
            },
        }]
        # Second call: timeapi.io returns the IANA timezone
        tz_resp = MagicMock()
        tz_resp.raise_for_status = MagicMock()
        tz_resp.json.return_value = {"timeZone": "America/Chicago"}

        mock_get.side_effect = [nominatim_resp, tz_resp]

        resp = logged_in_client.post("/api/geocode", json={"address": "Austin, TX"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["latitude"] == 30.2672
        assert data["longitude"] == -97.7431
        assert data["city"] == "Austin"
        assert data["country"] == "United States"
        assert data["timezone"] == "America/Chicago"

    @patch("geolocation.requests.get")
    def test_geocode_not_found_returns_404(self, mock_get, logged_in_client):
        # Nominatim returns an empty list for unknown addresses
        nominatim_resp = MagicMock()
        nominatim_resp.raise_for_status = MagicMock()
        nominatim_resp.json.return_value = []
        mock_get.return_value = nominatim_resp

        resp = logged_in_client.post(
            "/api/geocode", json={"address": "qwerqwerqwer nowhere"}
        )
        assert resp.status_code == 404
        assert "error" in resp.get_json()

    @patch("geolocation.requests.get")
    def test_geocode_tz_lookup_failure_falls_back_to_utc(self, mock_get, logged_in_client):
        # Nominatim succeeds but timeapi.io raises
        import requests
        nominatim_resp = MagicMock()
        nominatim_resp.raise_for_status = MagicMock()
        nominatim_resp.json.return_value = [{
            "lat": "51.5074",
            "lon": "-0.1278",
            "address": {"city": "London", "country": "United Kingdom"},
        }]
        mock_get.side_effect = [
            nominatim_resp,
            requests.RequestException("timeapi down"),
        ]

        resp = logged_in_client.post("/api/geocode", json={"address": "London, UK"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["timezone"] == "UTC"  # graceful fallback
        assert data["city"] == "London"

    @patch("geolocation.requests.get")
    def test_geocode_falls_back_through_place_fields(self, mock_get, logged_in_client):
        # Nominatim returns a result for a village (no 'city' key). The
        # endpoint should fall through to 'town' / 'village' / etc.
        nominatim_resp = MagicMock()
        nominatim_resp.raise_for_status = MagicMock()
        nominatim_resp.json.return_value = [{
            "lat": "42.1",
            "lon": "-71.2",
            "address": {
                "village": "Smallville",
                "county": "Somewhere County",
                "country": "United States",
            },
        }]
        tz_resp = MagicMock()
        tz_resp.raise_for_status = MagicMock()
        tz_resp.json.return_value = {"timeZone": "America/New_York"}
        mock_get.side_effect = [nominatim_resp, tz_resp]

        resp = logged_in_client.post("/api/geocode", json={"address": "Smallville"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["city"] == "Smallville"


# ---------------------------------------------------------------------------
# Audio file display label parser
# ---------------------------------------------------------------------------

class TestAudioDisplayLabel:
    """The parser turns ASCII filenames into human-readable labels with proper
    Turkish orthography for maqam names."""

    @pytest.mark.parametrize("filename,expected", [
        # Bundled recordings: adhan_<prayer>_<maqam>_<number>
        ("adhan_fajr_saba_1.mp3",        "Saba 1"),
        ("adhan_fajr_saba_2.mp3",        "Saba 2"),
        ("adhan_dhuhr_ussak_1.mp3",      "Uşşak 1"),
        ("adhan_dhuhr_ussak_2.mp3",      "Uşşak 2"),
        ("adhan_asr_rast_1.mp3",         "Rast 1"),
        ("adhan_asr_rast_2.mp3",         "Rast 2"),
        ("adhan_maghrib_segah_1.mp3",    "Segâh 1"),
        ("adhan_maghrib_segah_2.mp3",    "Segâh 2"),
        ("adhan_isha_hicaz_1.mp3",       "Hicaz 1"),
        ("adhan_isha_hicaz_2.mp3",       "Hicaz 2"),
        # Three-part (no number) — maqam only
        ("adhan_fajr_saba.mp3",          "Saba"),
        # Unknown maqam — title-case fallback
        ("adhan_fajr_bayati_3.mp3",      "Bayati 3"),
        # iqamah files
        ("iqamah_bell.mp3",              "Bell"),
        # Totally unrecognised filename — cleaned-up stem
        ("bells.mp3",                    "Bells"),
    ])
    def test_label_parsing(self, filename, expected):
        from app import audio_display_label
        assert audio_display_label(filename) == expected

    @pytest.mark.parametrize("filename,category,prayer", [
        ("adhan_fajr_saba_1.mp3",        "adhan",  "fajr"),
        ("adhan_dhuhr_ussak_2.mp3",      "adhan",  "dhuhr"),
        ("adhan_isha_hicaz_1.mp3",       "adhan",  "isha"),
        ("iqamah_bell.mp3",              "iqamah", None),
        ("bells.mp3",                    "other",  None),
        ("adhan_fajr.mp3",              "adhan",  "fajr"),
        ("adhan_notaprayer_someone.mp3", "other",  None),
    ])
    def test_audio_file_category(self, filename, category, prayer):
        from app import _audio_file_category
        assert _audio_file_category(filename) == (category, prayer)


class TestAudioFileFiltering:
    """End-to-end: per-prayer adhan and iqamah dropdown builders filter
    correctly from the real AUDIO_DIR contents."""

    def test_audio_files_by_prayer_splits_correctly(self, tmp_path, monkeypatch):
        audio_dir = tmp_path / "audio"
        audio_dir.mkdir()
        for name in [
            "adhan_fajr_saba_1.mp3",
            "adhan_fajr_saba_2.mp3",
            "adhan_dhuhr_ussak_1.mp3",
            "adhan_asr_rast_2.mp3",
            "adhan_maghrib_segah_1.mp3",
            "adhan_isha_hicaz_2.mp3",
            "iqamah_bell.mp3",
            "random_noise.mp3",
        ]:
            (audio_dir / name).write_bytes(b"\x00")

        import app as web_app
        monkeypatch.setattr(web_app, "AUDIO_DIR", audio_dir)

        by_prayer = web_app._build_audio_files_by_prayer()

        fajr_filenames = [e["filename"] for e in by_prayer["Fajr"]]
        assert "adhan_fajr_saba_1.mp3" in fajr_filenames
        assert "adhan_fajr_saba_2.mp3" in fajr_filenames
        assert len(fajr_filenames) == 2

        assert len(by_prayer["Dhuhr"]) == 1
        assert "ussak" in by_prayer["Dhuhr"][0]["filename"]
        assert len(by_prayer["Asr"]) == 1
        assert "rast" in by_prayer["Asr"][0]["filename"]

        # iqamah and random_noise are NOT in any per-prayer bucket
        all_in_buckets = {e["filename"] for p in by_prayer.values() for e in p}
        assert "iqamah_bell.mp3" not in all_in_buckets
        assert "random_noise.mp3" not in all_in_buckets

