"""Core Adhan scheduler – computes daily prayer times and triggers playback."""

import datetime
import logging
import os
import socket
import threading
import time

from zoneinfo import ZoneInfo

from adhanpy.PrayerTimes import PrayerTimes
from adhanpy.calculation.CalculationMethod import CalculationMethod
from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

import playback_log
from config import (
    AUDIO_DIR,
    PRAYER_NAMES,
    config_changed_since,
    load_config,
    save_config,
)
from discovery import (
    connect_speakers_direct,
    discover_chromecasts,
    get_device_metadata,
    play_on_all,
)

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

# How many seconds before adhan/iqamah we fire a pre-warm.  Long enough to
# absorb a serial mDNS fallback for several speakers, short enough not to
# wake devices unnecessarily early.
PREWARM_SECONDS = 90

# Pre-warmed devices older than this are discarded as stale.
PREWARM_TTL_SECONDS = 180

# Lock held for the full duration of a playback trigger.  ``_check_config_change``
# skips rescheduling while a playback is in flight so a mid-adhan save cannot
# remove a running job.
_trigger_lock = threading.Lock()

# Keyed by prayer name; populated by pre-warm, consumed by playback.
_prewarm_cache: dict[str, dict] = {}
_prewarm_cache_lock = threading.Lock()


def _get_local_ip() -> str | None:
    """Return the Pi's LAN IP address for serving audio files.

    Returns None on failure instead of silently falling back to
    ``127.0.0.1`` — a localhost URL would leave speakers with nothing to
    stream, producing a silent failure.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception as exc:
        logger.error("Could not determine local IP: %s", exc)
        return None
    if ip.startswith("127.") or ip == "0.0.0.0":
        logger.error("Local IP resolved to loopback (%s); skipping playback", ip)
        return None
    return ip


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


def _filter_by_schedule(
    enabled: list[str],
    speakers: dict,
    prayer_name: str | None,
    timezone: str,
    *,
    schedule_key: str,
) -> list[str]:
    """Filter enabled speakers by the given per-speaker weekday schedule.

    ``schedule_key`` is ``"schedule"`` for adhan or ``"iqamah_schedule"`` for
    iqamah.  A speaker with no entry for ``schedule_key`` falls back to the
    ``"schedule"`` value (backward compatible: iqamah inherits adhan's
    schedule until the user sets one explicitly).
    """
    if not prayer_name or not enabled:
        return enabled
    try:
        today = datetime.datetime.now(ZoneInfo(timezone)).weekday()
    except Exception:
        today = datetime.datetime.now(pytz.UTC).weekday()

    result: list[str] = []
    for name in enabled:
        info = speakers.get(name, {})
        schedule = info.get(schedule_key)
        if schedule is None and schedule_key != "schedule":
            # Inherit adhan schedule when iqamah schedule isn't set
            schedule = info.get("schedule")
        if schedule is None:
            result.append(name)
            continue
        if prayer_name not in schedule:
            result.append(name)
            continue
        days = schedule[prayer_name]
        if days is None or today in days:
            result.append(name)
        else:
            logger.info(
                "  %s skipped for %s %s (not scheduled today)",
                name, prayer_name, schedule_key.replace("_schedule", "") or "adhan",
            )
    return result


def _get_prewarmed(prayer_name: str | None) -> dict:
    """Return freshly pre-warmed devices for a prayer, or empty dict."""
    if not prayer_name:
        return {}
    with _prewarm_cache_lock:
        entry = _prewarm_cache.pop(prayer_name, None)
    if not entry:
        return {}
    if time.time() - entry["ts"] > PREWARM_TTL_SECONDS:
        logger.info("Pre-warm cache for %s is stale, discarding", prayer_name)
        return {}
    return entry.get("devices", {})


def _store_prewarm(prayer_name: str | None, devices: dict) -> None:
    if not prayer_name:
        return
    with _prewarm_cache_lock:
        _prewarm_cache[prayer_name] = {"devices": devices, "ts": time.time()}


def _persist_discovered_hosts(devices: dict, speakers_config: dict) -> bool:
    """Update saved host/port for speakers we reached via mDNS.

    Returns True if anything was persisted.  Keeping the stored host fresh
    means tomorrow's direct-connect works even if the router handed out a
    new lease overnight.
    """
    meta = get_device_metadata(devices)
    changed = False
    for name, info in meta.items():
        host = info.get("host")
        if not host:
            continue
        port = info.get("port", 8009)
        saved = speakers_config.get(name, {})
        if saved.get("host") == host and saved.get("port", 8009) == port:
            continue
        saved["host"] = host
        saved["port"] = port
        speakers_config[name] = saved
        changed = True
        logger.info("Refreshed host for %s -> %s:%d", name, host, port)
    return changed


def _resolve_devices(
    speakers_config: dict,
    enabled: list[str],
    prayer_name: str | None,
) -> dict:
    """Return connected Chromecast objects for enabled speakers.

    Tries (in order): fresh pre-warm cache, direct-connect by saved host,
    mDNS fallback.  Any new hosts discovered via mDNS are persisted back
    to config so future direct-connects hit the right IP.
    """
    devices = _get_prewarmed(prayer_name)
    if devices:
        present = [n for n in enabled if n in devices]
        if len(present) == len(enabled):
            logger.info("Using pre-warmed connections for %d speaker(s)", len(present))
            return {n: devices[n] for n in present}
        # Partial hit — keep what we have, look up the rest.
        logger.info(
            "Pre-warm had %d/%d speakers; resolving the rest",
            len(present), len(enabled),
        )
        devices = {n: devices[n] for n in present}
    else:
        devices = {}

    missing = [n for n in enabled if n not in devices]
    if missing:
        direct = connect_speakers_direct(speakers_config, missing, timeout=10)
        devices.update(direct)

    still_missing = [n for n in enabled if n not in devices]
    if still_missing:
        logger.info("  mDNS fallback for: %s", still_missing)
        discovered = discover_chromecasts(timeout=15, use_cache=False)
        newly_found = {n: discovered[n] for n in still_missing if n in discovered}
        devices.update(newly_found)

        if newly_found:
            # Persist any freshly-discovered IPs so we don't need mDNS next time
            try:
                current = load_config()
                speakers = current.get("speakers", {})
                if _persist_discovered_hosts(newly_found, speakers):
                    current["speakers"] = speakers
                    save_config(current)
            except Exception:
                logger.exception("Failed to persist refreshed speaker hosts")

    return devices


def _play_on_speakers(
    media_url: str,
    config: dict,
    event_label: str,
    prayer_name: str | None = None,
    *,
    event_type: str = "adhan",
) -> None:
    """Play audio on all enabled Chromecast speakers.

    ``event_type`` is one of ``"adhan" | "iqamah" | "friday_sela"`` and
    picks which per-speaker weekday schedule to honour.
    """
    volume = config.get("volume", 0.5)
    speakers = config.get("speakers", {})
    enabled = [name for name, info in speakers.items() if info.get("enabled", False)]

    schedule_key = "iqamah_schedule" if event_type == "iqamah" else "schedule"
    enabled = _filter_by_schedule(
        enabled, speakers, prayer_name,
        timezone=config.get("timezone", "UTC"),
        schedule_key=schedule_key,
    )

    if not enabled:
        return

    # Per-speaker volume overrides
    speaker_volumes = {
        name: speakers[name]["volume"]
        for name in enabled
        if speakers[name].get("volume") is not None
    }

    try:
        devices = _resolve_devices(speakers, enabled, prayer_name)

        def _log_result(name: str, ok: bool, elapsed: float, error: str | None) -> None:
            try:
                playback_log.record(
                    event=event_type,
                    prayer=prayer_name,
                    speaker=name,
                    ok=ok,
                    elapsed_seconds=elapsed,
                    error=error,
                )
            except Exception:
                logger.exception("Failed to record playback log entry")

        results = play_on_all(
            devices, enabled, media_url,
            volume=volume,
            speaker_volumes=speaker_volumes or None,
            on_result=_log_result,
        )
        for name, ok in results.items():
            logger.info("  %s -> %s", name, "success" if ok else "FAILED")
    except Exception as exc:
        logger.error("%s Chromecast playback error: %s", event_label, exc)


def _do_trigger(
    prayer_name: str,
    *,
    event_type: str,
    event_label: str,
    audio_file: str | None,
) -> None:
    """Shared body for adhan/iqamah/sela triggers.

    Holds ``_trigger_lock`` so the config watcher can't reschedule jobs
    out from under a firing playback.
    """
    with _trigger_lock:
        config = load_config()

        if _is_dnd_active(config):
            logger.info("Skipping %s – Do Not Disturb is active", event_label)
            return

        if event_type == "adhan":
            audio_file = _resolve_audio_file(prayer_name, config)
        # iqamah/sela pass their audio_file in already

        if not audio_file:
            logger.error("Skipping %s – no audio file available", event_label)
            return

        if not (AUDIO_DIR / audio_file).is_file():
            logger.warning("Audio file missing for %s: %s", event_label, audio_file)
            return

        local_ip = _get_local_ip()
        if local_ip is None:
            return

        web_port = os.getenv("WEB_PORT", "5000")
        media_url = f"http://{local_ip}:{web_port}/audio/{audio_file}"

        logger.info("%s – playing %s", event_label, media_url)
        _play_on_speakers(media_url, config, event_label, prayer_name=prayer_name, event_type=event_type)


def trigger_adhan(prayer_name: str) -> None:
    """Called by the scheduler when it's time for a specific prayer."""
    config = load_config()
    if prayer_name in config.get("skip_prayers", []):
        logger.info("Skipping %s (disabled by user)", prayer_name)
        return
    _do_trigger(
        prayer_name,
        event_type="adhan",
        event_label=f"Adhan ({prayer_name})",
        audio_file=None,
    )


def trigger_iqamah(prayer_name: str) -> None:
    """Called by the scheduler when it's time for iqamah."""
    config = load_config()
    if not config.get("iqamah_enabled", False):
        return
    if prayer_name in config.get("skip_prayers", []):
        logger.info("Skipping iqamah for %s (disabled by user)", prayer_name)
        return
    audio_file = config.get("iqamah_audio_file", "iqamah_bell.mp3")
    _do_trigger(
        prayer_name,
        event_type="iqamah",
        event_label=f"Iqamah ({prayer_name})",
        audio_file=audio_file,
    )


def trigger_friday_sela() -> None:
    """Called by the scheduler before Friday Dhuhr to play the Sela."""
    config = load_config()
    if not config.get("friday_sela_enabled", False):
        return
    audio_file = config.get("friday_sela_audio_file", "sela_cuma_huseyni_1.mp3")
    _do_trigger(
        "Dhuhr",
        event_type="friday_sela",
        event_label="Friday Sela",
        audio_file=audio_file,
    )


def _prewarm_speakers(prayer_name: str | None = None) -> None:
    """Connect to all enabled speakers to wake them from sleep.

    Scheduled ``PREWARM_SECONDS`` before each prayer.  Stores the resulting
    Chromecast objects so the playback trigger can reuse them without
    re-running direct-connect + mDNS.
    """
    config = load_config()
    speakers = config.get("speakers", {})
    enabled = [n for n, info in speakers.items() if info.get("enabled", False)]
    if not enabled:
        return

    logger.info("Pre-warming %d speaker(s) for %s...", len(enabled), prayer_name or "next event")

    devices = connect_speakers_direct(speakers, enabled, timeout=10)

    missing = [n for n in enabled if n not in devices]
    if missing:
        discovered = discover_chromecasts(timeout=15, use_cache=False)
        newly_found = {n: discovered[n] for n in missing if n in discovered}
        devices.update(newly_found)
        if newly_found:
            try:
                current = load_config()
                speakers_cur = current.get("speakers", {})
                if _persist_discovered_hosts(newly_found, speakers_cur):
                    current["speakers"] = speakers_cur
                    save_config(current)
            except Exception:
                logger.exception("Failed to persist refreshed speaker hosts during pre-warm")

    _store_prewarm(prayer_name, devices)
    logger.info(
        "Pre-warm complete: %d/%d speakers ready for %s",
        len(devices), len(enabled), prayer_name or "next event",
    )


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
        self.scheduler.add_listener(self._on_misfire, EVENT_JOB_MISSED)

    def start(self) -> None:
        """Start the scheduler and set up the daily reschedule job."""
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

        # Prune the playback log daily so it never grows unbounded
        self.scheduler.add_job(
            playback_log.purge,
            CronTrigger(hour=3, minute=17),
            id="playback_log_purge",
            replace_existing=True,
        )

        logger.info("Adhan scheduler started")

    def _on_misfire(self, event) -> None:
        """APScheduler fires this when a job missed its run_date within grace.

        With a 60s grace we expect zero misfires in normal operation; any
        misfire is a real signal worth surfacing.
        """
        logger.warning(
            "Job %s missed its scheduled time of %s — check system load / clock drift",
            event.job_id, event.scheduled_run_time,
        )

    def _check_config_change(self) -> None:
        """Reschedule prayers if config has been updated.

        Skipped while a playback trigger is holding ``_trigger_lock`` so we
        never rip jobs out of the scheduler mid-fire.
        """
        try:
            if not config_changed_since(self._last_config_check):
                return
            if not _trigger_lock.acquire(blocking=False):
                logger.debug("Config change seen but a playback is in flight; retrying later")
                return
            try:
                logger.info("Config change detected, rescheduling prayers")
                self.schedule_today()
                self._last_config_check = time.time()
            finally:
                _trigger_lock.release()
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

        # Discard any pre-warmed devices left from yesterday.
        with _prewarm_cache_lock:
            _prewarm_cache.clear()

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

            prewarm_time = pt - datetime.timedelta(seconds=PREWARM_SECONDS)
            if prewarm_time > now:
                pw_id = f"prewarm_{prayer}"
                self.scheduler.add_job(
                    _prewarm_speakers,
                    "date",
                    run_date=prewarm_time,
                    args=[prayer],
                    id=pw_id,
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                self._job_ids.append(pw_id)

            job_id = f"adhan_{prayer}"
            self.scheduler.add_job(
                trigger_adhan,
                "date",
                run_date=pt,
                args=[prayer],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=60,
            )
            self._job_ids.append(job_id)
            logger.info(
                "Scheduled %s at %s (pre-warm at %s, %ds before)",
                prayer, pt.strftime("%H:%M:%S"),
                prewarm_time.strftime("%H:%M:%S"), PREWARM_SECONDS,
            )

        if config.get("iqamah_enabled", False):
            iqamah_times = compute_iqamah_times(config, times)
            for prayer in PRAYER_NAMES:
                iq_time = iqamah_times.get(prayer)
                if iq_time is None or iq_time <= now:
                    continue

                iq_prewarm_time = iq_time - datetime.timedelta(seconds=PREWARM_SECONDS)
                if iq_prewarm_time > now:
                    pw_id = f"prewarm_iqamah_{prayer}"
                    self.scheduler.add_job(
                        _prewarm_speakers,
                        "date",
                        run_date=iq_prewarm_time,
                        args=[prayer],
                        id=pw_id,
                        replace_existing=True,
                        misfire_grace_time=60,
                    )
                    self._job_ids.append(pw_id)

                job_id = f"iqamah_{prayer}"
                self.scheduler.add_job(
                    trigger_iqamah,
                    "date",
                    run_date=iq_time,
                    args=[prayer],
                    id=job_id,
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                self._job_ids.append(job_id)
                logger.info("Scheduled iqamah %s at %s", prayer, iq_time.strftime("%H:%M:%S"))

        if config.get("friday_sela_enabled", False) and now.weekday() == 4:
            dhuhr_time = times.get("Dhuhr")
            offset = config.get("friday_sela_offset", 45)
            if dhuhr_time:
                sela_time = dhuhr_time - datetime.timedelta(minutes=offset)
                if sela_time > now and sela_time < dhuhr_time:
                    pw_time = sela_time - datetime.timedelta(seconds=PREWARM_SECONDS)
                    if pw_time > now:
                        pw_id = "prewarm_friday_sela"
                        self.scheduler.add_job(
                            _prewarm_speakers,
                            "date",
                            run_date=pw_time,
                            args=["Dhuhr"],
                            id=pw_id,
                            replace_existing=True,
                            misfire_grace_time=60,
                        )
                        self._job_ids.append(pw_id)

                    job_id = "friday_sela"
                    self.scheduler.add_job(
                        trigger_friday_sela,
                        "date",
                        run_date=sela_time,
                        id=job_id,
                        replace_existing=True,
                        misfire_grace_time=60,
                    )
                    self._job_ids.append(job_id)
                    logger.info(
                        "Scheduled Friday Sela at %s (%d min before Dhuhr)",
                        sela_time.strftime("%H:%M:%S"),
                        offset,
                    )

    def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
