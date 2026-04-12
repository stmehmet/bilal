"""Core Adhan scheduler – computes daily prayer times and triggers playback."""

import datetime
import logging
import os
import socket

from zoneinfo import ZoneInfo

from adhanpy.PrayerTimes import PrayerTimes
from adhanpy.calculation.CalculationMethod import CalculationMethod
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import (
    AUDIO_DIR,
    PRAYER_NAMES,
    config_changed_since,
    load_config,
)
from discovery import discover_chromecasts, play_on_all
from smartthings import play_audio_on_device

logger = logging.getLogger(__name__)

METHOD_MAP = {
    "MuslimWorldLeague": CalculationMethod.MUSLIM_WORLD_LEAGUE,
    "Egyptian": CalculationMethod.EGYPTIAN,
    "Karachi": CalculationMethod.KARACHI,
    "UmmAlQura": CalculationMethod.UMM_AL_QURA,
    "Dubai": CalculationMethod.DUBAI,
    "MoonsightingCommittee": CalculationMethod.MOON_SIGHTING_COMMITTEE,
    "NorthAmerica": CalculationMethod.NORTH_AMERICA,
    "Kuwait": CalculationMethod.KUWAIT,
    "Qatar": CalculationMethod.QATAR,
    "Singapore": CalculationMethod.SINGAPORE,
    "ISNA": CalculationMethod.NORTH_AMERICA,
    # adhanpy doesn't ship Tehran/Turkey — alias to closest angle profile
    "Tehran": CalculationMethod.MUSLIM_WORLD_LEAGUE,
    "Turkey": CalculationMethod.MUSLIM_WORLD_LEAGUE,
}


def _get_local_ip() -> str:
    """Return the Pi's LAN IP address for serving audio files."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def compute_prayer_times(config: dict, date: datetime.date | None = None) -> dict:
    """Calculate prayer times for a given date.

    Returns a dict of prayer_name -> datetime (timezone-aware).
    """
    if config["latitude"] is None or config["longitude"] is None:
        logger.error("Location not set, cannot compute prayer times")
        return {}

    tz_zi = ZoneInfo(config.get("timezone", "UTC"))
    if date is None:
        date = datetime.datetime.now(tz_zi).date()

    method = METHOD_MAP.get(
        config.get("calculation_method", "ISNA"),
        CalculationMethod.NORTH_AMERICA,
    )

    pt = PrayerTimes(
        coordinates=(config["latitude"], config["longitude"]),
        date=datetime.datetime(date.year, date.month, date.day),
        calculation_method=method,
        time_zone=tz_zi,
    )

    return {
        "Fajr": pt.fajr,
        "Sunrise": pt.sunrise,
        "Dhuhr": pt.dhuhr,
        "Asr": pt.asr,
        "Maghrib": pt.maghrib,
        "Isha": pt.isha,
    }


def compute_iqamah_times(config: dict, prayer_times: dict) -> dict:
    """Compute iqamah times by adding offset (minutes) to each prayer time.

    Returns a dict of prayer_name -> datetime (timezone-aware), only for
    the five obligatory prayers (not Sunrise).
    """
    offsets = config.get("iqamah_offsets", {})
    result = {}
    for prayer in PRAYER_NAMES:
        pt = prayer_times.get(prayer)
        if pt is None:
            continue
        offset_min = offsets.get(prayer, 0)
        result[prayer] = pt + datetime.timedelta(minutes=offset_min)
    return result


def _is_dnd_active(config: dict) -> bool:
    """Return True if the current time falls within the Do Not Disturb window."""
    if not config.get("dnd_enabled", False):
        return False
    try:
        tz = pytz.timezone(config.get("timezone", "UTC"))
        now = datetime.datetime.now(tz).time()
        start = datetime.time(*map(int, config["dnd_start"].split(":")))
        end = datetime.time(*map(int, config["dnd_end"].split(":")))
        # Handle overnight windows (e.g. 23:00 – 05:30)
        if start <= end:
            return start <= now <= end
        else:
            return now >= start or now <= end
    except Exception as exc:
        logger.warning("DND check failed: %s", exc)
        return False


def validate_audio_files(config: dict) -> list[str]:
    """Check all configured adhan audio files and return a list of missing ones."""
    files = config.get("adhan_audio_files", {}) or {}
    configured = {files.get(p) for p in PRAYER_NAMES if files.get(p)}
    missing = sorted(f for f in configured if not (AUDIO_DIR / f).is_file())
    for f in missing:
        logger.warning("Audio file missing: %s", AUDIO_DIR / f)
    return missing


def _first_available_adhan() -> str | None:
    """Return any `adhan_*.mp3` file in the audio dir, or None."""
    if not AUDIO_DIR.is_dir():
        return None
    for candidate in sorted(AUDIO_DIR.glob("adhan_*.mp3")):
        return candidate.name
    return None


def _resolve_audio_file(prayer_name: str, config: dict) -> str | None:
    """Resolve the audio file for a prayer, falling back if needed.

    Returns the filename or None if no audio file is available.
    """
    files = config.get("adhan_audio_files", {}) or {}
    audio_file = files.get(prayer_name)

    if audio_file and (AUDIO_DIR / audio_file).is_file():
        return audio_file

    if audio_file:
        logger.warning(
            "Audio file missing for %s: %s, trying fallback", prayer_name, audio_file
        )
    else:
        logger.warning("No audio file configured for %s, trying fallback", prayer_name)

    fallback = _first_available_adhan()
    if fallback:
        return fallback

    logger.error("No adhan_*.mp3 files available in %s", AUDIO_DIR)
    return None


def _play_on_speakers(
    media_url: str,
    config: dict,
    event_label: str,
    prayer_name: str | None = None,
) -> None:
    """Play audio on all enabled Chromecast speakers and SmartThings devices.

    When *prayer_name* is given, each speaker's per-prayer schedule is checked
    against today's weekday.  A missing ``schedule`` key means "all days" for
    backward compatibility with configs that predate this feature.
    """
    volume = config.get("volume", 0.5)

    # --- Chromecast playback ---
    speakers = config.get("speakers", {})
    enabled = [name for name, info in speakers.items() if info.get("enabled", False)]

    # Per-speaker schedule filtering
    if prayer_name and enabled:
        tz_name = config.get("timezone", "UTC")
        try:
            today = datetime.datetime.now(ZoneInfo(tz_name)).weekday()
        except Exception:
            today = datetime.datetime.now(pytz.UTC).weekday()
        scheduled = []
        for name in enabled:
            schedule = speakers[name].get("schedule")
            if schedule is None:
                # No schedule = play every day (backward compatible)
                scheduled.append(name)
            elif prayer_name in schedule:
                days = schedule[prayer_name]
                if days is None or today in days:
                    scheduled.append(name)
                else:
                    logger.info("  %s skipped for %s (not scheduled today)", name, prayer_name)
            else:
                # Prayer not listed in schedule = play every day
                scheduled.append(name)
        enabled = scheduled

    if enabled:
        try:
            devices = discover_chromecasts(timeout=8)
            results = play_on_all(devices, enabled, media_url, volume=volume)
            for name, ok in results.items():
                status = "success" if ok else "FAILED"
                logger.info("  %s -> %s", name, status)
        except Exception as exc:
            logger.error("%s Chromecast playback error: %s", event_label, exc)

    # --- SmartThings playback ---
    st_token = config.get("smartthings_token", "")
    st_device = config.get("smartthings_device_id", "")
    if st_token and st_device:
        play_audio_on_device(st_token, st_device, media_url)


def trigger_adhan(prayer_name: str) -> None:
    """Called by the scheduler when it's time for a specific prayer."""
    config = load_config()

    if prayer_name in config.get("skip_prayers", []):
        logger.info("Skipping %s (disabled by user)", prayer_name)
        return

    if _is_dnd_active(config):
        logger.info("Skipping %s – Do Not Disturb is active", prayer_name)
        return

    # Determine the audio file with validation
    audio_file = _resolve_audio_file(prayer_name, config)
    if audio_file is None:
        logger.error("Skipping adhan for %s – no audio file available", prayer_name)
        return

    local_ip = _get_local_ip()
    web_port = os.getenv("WEB_PORT", "5000")
    media_url = f"http://{local_ip}:{web_port}/audio/{audio_file}"

    logger.info("Adhan for %s – playing %s", prayer_name, media_url)
    _play_on_speakers(media_url, config, f"Adhan ({prayer_name})", prayer_name=prayer_name)


def trigger_iqamah(prayer_name: str) -> None:
    """Called by the scheduler when it's time for iqamah."""
    config = load_config()

    if not config.get("iqamah_enabled", False):
        return

    if prayer_name in config.get("skip_prayers", []):
        logger.info("Skipping iqamah for %s (disabled by user)", prayer_name)
        return

    if _is_dnd_active(config):
        logger.info("Skipping iqamah for %s – Do Not Disturb is active", prayer_name)
        return

    audio_file = config.get("iqamah_audio_file", "iqamah_bell.mp3")
    if not (AUDIO_DIR / audio_file).is_file():
        logger.warning("Iqamah audio file missing: %s, skipping", audio_file)
        return

    local_ip = _get_local_ip()
    web_port = os.getenv("WEB_PORT", "5000")
    media_url = f"http://{local_ip}:{web_port}/audio/{audio_file}"

    logger.info("Iqamah for %s – playing %s", prayer_name, media_url)
    _play_on_speakers(media_url, config, f"Iqamah ({prayer_name})", prayer_name=prayer_name)


class AdhanSchedulerService:
    """Manages the APScheduler instance and reschedules daily."""

    def __init__(self):
        # Use the default in-memory jobstore. Persistence is not needed here:
        # every startup calls schedule_today(), which recomputes prayer times
        # and re-registers all jobs for the day. A SQL jobstore would also
        # require pickling bound methods (schedule_today, _check_config_change),
        # which APScheduler refuses because they hold a reference to the
        # scheduler itself and cannot be serialized.
        self.scheduler = BackgroundScheduler()
        self._job_ids: list[str] = []
        self._last_config_check: float = 0.0

    def start(self) -> None:
        """Start the scheduler and set up the daily reschedule job."""
        import time
        self.scheduler.start()
        # Warn about missing audio files at startup
        config = load_config()
        missing = validate_audio_files(config)
        if missing:
            logger.warning("Missing audio files at startup: %s", missing)
        self.schedule_today()
        self._last_config_check = time.time()

        # Reschedule every day at midnight
        self.scheduler.add_job(
            self.schedule_today,
            CronTrigger(hour=0, minute=1),
            id="daily_reschedule",
            replace_existing=True,
        )

        # Check for config changes every 30 seconds
        self.scheduler.add_job(
            self._check_config_change,
            "interval",
            seconds=30,
            id="config_watcher",
            replace_existing=True,
        )
        logger.info("Adhan scheduler started")

    def _check_config_change(self) -> None:
        """Reschedule prayers if config has been updated."""
        import time
        try:
            if config_changed_since(self._last_config_check):
                logger.info("Config change detected, rescheduling prayers")
                self.schedule_today()
                self._last_config_check = time.time()
        except Exception as exc:
            logger.error("Error checking config change: %s", exc)

    def schedule_today(self) -> None:
        """Remove old prayer jobs and schedule today's prayers."""
        for jid in self._job_ids:
            try:
                self.scheduler.remove_job(jid)
            except Exception as exc:
                logger.debug("Could not remove job %s: %s", jid, exc)
        self._job_ids.clear()

        config = load_config()
        if not config.get("setup_complete"):
            logger.info("Setup not complete, skipping scheduling")
            return

        times = compute_prayer_times(config)
        tz = pytz.timezone(config.get("timezone", "UTC"))
        now = datetime.datetime.now(tz)

        for prayer in PRAYER_NAMES:
            pt = times.get(prayer)
            if pt is None:
                continue
            if pt <= now:
                logger.debug("Skipping %s (already passed at %s)", prayer, pt)
                continue

            job_id = f"adhan_{prayer}"
            self.scheduler.add_job(
                trigger_adhan,
                "date",
                run_date=pt,
                args=[prayer],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            self._job_ids.append(job_id)
            logger.info("Scheduled %s at %s", prayer, pt.strftime("%H:%M:%S"))

        # --- Iqamah jobs ---
        if config.get("iqamah_enabled", False):
            iqamah_times = compute_iqamah_times(config, times)
            for prayer in PRAYER_NAMES:
                iq_time = iqamah_times.get(prayer)
                if iq_time is None or iq_time <= now:
                    continue
                job_id = f"iqamah_{prayer}"
                self.scheduler.add_job(
                    trigger_iqamah,
                    "date",
                    run_date=iq_time,
                    args=[prayer],
                    id=job_id,
                    replace_existing=True,
                    misfire_grace_time=300,
                )
                self._job_ids.append(job_id)
                logger.info("Scheduled iqamah %s at %s", prayer, iq_time.strftime("%H:%M:%S"))

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
