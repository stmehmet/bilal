"""Core Adhan scheduler – computes daily prayer times and triggers playback."""

import datetime
import logging
import os
import socket

from adhan import adhan
from adhan.methods import (
    ISNA,
    EGYPT,
    KARACHI,
    KUWAIT,
    MWL,
    QATAR,
    SINGAPORE,
    TEHRAN,
    TURKEY,
    UMM_AL_QURA,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import (
    PRAYER_NAMES,
    load_config,
)
from discovery import discover_chromecasts, play_on_all
from smartthings import play_audio_on_device

logger = logging.getLogger(__name__)

METHOD_MAP = {
    "MuslimWorldLeague": MWL,
    "Egyptian": EGYPT,
    "Karachi": KARACHI,
    "UmmAlQura": UMM_AL_QURA,
    "Kuwait": KUWAIT,
    "Qatar": QATAR,
    "Singapore": SINGAPORE,
    "Tehran": TEHRAN,
    "Turkey": TURKEY,
    "ISNA": ISNA,
    # Aliases that map to the closest available method
    "Dubai": UMM_AL_QURA,
    "MoonsightingCommittee": MWL,
    "NorthAmerica": ISNA,
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

    tz = pytz.timezone(config.get("timezone", "UTC"))
    if date is None:
        date = datetime.datetime.now(tz).date()

    method_key = config.get("calculation_method", "ISNA")
    params = METHOD_MAP.get(method_key, ISNA)

    times = adhan(
        day=date,
        location=(config["latitude"], config["longitude"]),
        parameters=params,
    )

    # Map the adhan library keys to our prayer names
    key_map = {
        "Fajr": "fajr",
        "Sunrise": "sunrise",
        "Dhuhr": "dhuhr",
        "Asr": "asr",
        "Maghrib": "maghrib",
        "Isha": "isha",
    }

    result = {}
    for prayer, lib_key in key_map.items():
        t = times.get(lib_key)
        if t is not None:
            if t.tzinfo is None:
                t = tz.localize(t)
            result[prayer] = t

    return result


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


def trigger_adhan(prayer_name: str) -> None:
    """Called by the scheduler when it's time for a specific prayer."""
    config = load_config()

    if prayer_name in config.get("skip_prayers", []):
        logger.info("Skipping %s (disabled by user)", prayer_name)
        return

    if _is_dnd_active(config):
        logger.info("Skipping %s – Do Not Disturb is active", prayer_name)
        return

    # Determine the audio file
    if prayer_name == "Fajr":
        audio_file = config.get("fajr_adhan_file", "adhan_fajr.mp3")
    else:
        audio_file = config.get("adhan_file", "adhan_makkah.mp3")

    local_ip = _get_local_ip()
    web_port = os.getenv("WEB_PORT", "5000")
    media_url = f"http://{local_ip}:{web_port}/audio/{audio_file}"
    volume = config.get("volume", 0.5)

    logger.info("Adhan for %s – playing %s", prayer_name, media_url)

    # --- Chromecast playback ---
    speakers = config.get("speakers", {})
    enabled = [name for name, info in speakers.items() if info.get("enabled", False)]
    if enabled:
        try:
            devices = discover_chromecasts(timeout=8)
            results = play_on_all(devices, enabled, media_url, volume=volume)
            for name, ok in results.items():
                status = "success" if ok else "FAILED"
                logger.info("  %s -> %s", name, status)
        except Exception as exc:
            logger.error("Chromecast playback error: %s", exc)

    # --- SmartThings playback ---
    st_token = config.get("smartthings_token", "")
    st_device = config.get("smartthings_device_id", "")
    if st_token and st_device:
        play_audio_on_device(st_token, st_device, media_url)


class AdhanSchedulerService:
    """Manages the APScheduler instance and reschedules daily."""

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self._job_ids: list[str] = []

    def start(self) -> None:
        """Start the scheduler and set up the daily reschedule job."""
        self.scheduler.start()
        self.schedule_today()

        # Reschedule every day at midnight
        self.scheduler.add_job(
            self.schedule_today,
            CronTrigger(hour=0, minute=1),
            id="daily_reschedule",
            replace_existing=True,
        )
        logger.info("Adhan scheduler started")

    def schedule_today(self) -> None:
        """Remove old prayer jobs and schedule today's prayers."""
        for jid in self._job_ids:
            try:
                self.scheduler.remove_job(jid)
            except Exception:
                pass
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
            )
            self._job_ids.append(job_id)
            logger.info("Scheduled %s at %s", prayer, pt.strftime("%H:%M:%S"))

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
