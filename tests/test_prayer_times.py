"""Tests for prayer time computation."""

import datetime

import pytest

from adhan_scheduler import compute_prayer_times, compute_iqamah_times
from config import PRAYER_NAMES


MAKKAH_CONFIG = {
    "latitude": 21.3891,
    "longitude": 39.8579,
    "timezone": "Asia/Riyadh",
    "calculation_method": "UmmAlQura",
}

LONDON_CONFIG = {
    "latitude": 51.5074,
    "longitude": -0.1278,
    "timezone": "Europe/London",
    "calculation_method": "MuslimWorldLeague",
}

FIXED_DATE = datetime.date(2024, 6, 15)


class TestComputePrayerTimes:
    def test_returns_five_prayers(self):
        times = compute_prayer_times(MAKKAH_CONFIG, FIXED_DATE)
        for prayer in PRAYER_NAMES:
            assert prayer in times, f"{prayer} missing from result"

    def test_all_times_are_timezone_aware(self):
        times = compute_prayer_times(MAKKAH_CONFIG, FIXED_DATE)
        for name, t in times.items():
            assert t.tzinfo is not None, f"{name} has no timezone"

    def test_prayers_are_in_chronological_order(self):
        times = compute_prayer_times(LONDON_CONFIG, FIXED_DATE)
        present = [p for p in PRAYER_NAMES if p in times]
        for i in range(len(present) - 1):
            assert times[present[i]] < times[present[i + 1]], (
                f"{present[i]} ({times[present[i]]}) is not before "
                f"{present[i + 1]} ({times[present[i + 1]]})"
            )

    def test_sunrise_is_between_fajr_and_dhuhr(self):
        times = compute_prayer_times(MAKKAH_CONFIG, FIXED_DATE)
        if "Sunrise" in times:
            assert times["Fajr"] < times["Sunrise"] < times["Dhuhr"]

    def test_no_location_returns_empty(self):
        config = {"latitude": None, "longitude": None}
        assert compute_prayer_times(config) == {}

    def test_defaults_to_today(self):
        times = compute_prayer_times(MAKKAH_CONFIG)
        assert len(times) >= 5

    @pytest.mark.parametrize("method", ["ISNA", "Egyptian", "Karachi", "Kuwait", "Qatar"])
    def test_various_methods(self, method):
        config = {**MAKKAH_CONFIG, "calculation_method": method}
        times = compute_prayer_times(config, FIXED_DATE)
        assert len(times) >= 5

    def test_all_times_fall_on_correct_date(self):
        import pytz
        config = MAKKAH_CONFIG
        times = compute_prayer_times(config, FIXED_DATE)
        tz = pytz.timezone(config["timezone"])
        for name, t in times.items():
            local_date = t.astimezone(tz).date()
            assert local_date == FIXED_DATE, f"{name} falls on {local_date}, expected {FIXED_DATE}"


class TestComputeIqamahTimes:
    def test_iqamah_after_adhan(self):
        prayer_times = compute_prayer_times(MAKKAH_CONFIG, FIXED_DATE)
        config = {
            **MAKKAH_CONFIG,
            "iqamah_offsets": {"Fajr": 20, "Dhuhr": 15, "Asr": 15, "Maghrib": 5, "Isha": 15},
        }
        iqamah = compute_iqamah_times(config, prayer_times)
        for prayer in PRAYER_NAMES:
            if prayer in prayer_times:
                offset = config["iqamah_offsets"][prayer]
                expected = prayer_times[prayer] + datetime.timedelta(minutes=offset)
                assert iqamah[prayer] == expected

    def test_zero_offset_equals_adhan(self):
        prayer_times = compute_prayer_times(MAKKAH_CONFIG, FIXED_DATE)
        config = {
            **MAKKAH_CONFIG,
            "iqamah_offsets": {p: 0 for p in PRAYER_NAMES},
        }
        iqamah = compute_iqamah_times(config, prayer_times)
        for prayer in PRAYER_NAMES:
            if prayer in prayer_times:
                assert iqamah[prayer] == prayer_times[prayer]

    def test_sunrise_excluded_from_iqamah(self):
        prayer_times = compute_prayer_times(MAKKAH_CONFIG, FIXED_DATE)
        config = {**MAKKAH_CONFIG, "iqamah_offsets": {p: 10 for p in PRAYER_NAMES}}
        iqamah = compute_iqamah_times(config, prayer_times)
        assert "Sunrise" not in iqamah
