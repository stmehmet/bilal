"""Core Adhan scheduler – computes daily prayer times and triggers playback."""

import datetime
import ipaddress
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

import heartbeat
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
    disconnect_all,
    find_speakers_by_name,
    get_device_metadata,
    play_on_all,
    scan_network_for_speakers,
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
        disconnect_all(entry.get("devices", {}))
        return {}
    return entry.get("devices", {})


def _store_prewarm(prayer_name: str | None, devices: dict) -> None:
    if not prayer_name:
        return
    with _prewarm_cache_lock:
        previous = _prewarm_cache.get(prayer_name)
        _prewarm_cache[prayer_name] = {"devices": devices, "ts": time.time()}
    # Release a prior unconsumed pre-warm for this prayer so its worker threads
    # don't linger.  Done outside the lock — disconnect() can briefly block.
    if previous:
        disconnect_all(previous.get("devices", {}))


def _persist_discovered_hosts(devices: dict, speakers_config: dict) -> bool:
    """Update saved host/port (and backfill UUID) for speakers we reached.

    Returns True if anything was persisted.  Keeping the stored host fresh
    means tomorrow's direct-connect works even if the router handed out a new
    lease overnight.  A speaker that predates UUID capture also gets its UUID
    backfilled the first time we resolve it, so it upgrades to stable
    identity matching on the next run.
    """
    meta = get_device_metadata(devices)
    changed = False
    for name, info in meta.items():
        saved = speakers_config.get(name, {})
        entry_changed = False

        host = info.get("host")
        if host:
            port = info.get("port", 8009)
            if saved.get("host") != host or saved.get("port", 8009) != port:
                saved["host"] = host
                saved["port"] = port
                entry_changed = True
                logger.info("Refreshed host for %s -> %s:%d", name, host, port)

        new_uuid = info.get("uuid")
        if new_uuid and not saved.get("uuid"):
            saved["uuid"] = new_uuid
            entry_changed = True
            logger.info("Captured UUID for %s -> %s", name, new_uuid)

        if entry_changed:
            speakers_config[name] = saved
            changed = True
    return changed


def _candidate_hosts(speakers_config: dict, max_hosts: int = 512) -> list[str]:
    """Every IPv4 address in the /24 of each saved speaker host.

    A speaker that changed IP almost certainly stayed within the same DHCP
    subnet, so its old address tells us where to sweep.  Used only by the
    unicast-scan fallback, so the cost is paid solely when a speaker is
    otherwise unrecoverable.
    """
    nets = set()
    for info in speakers_config.values():
        host = info.get("host")
        if not host:
            continue
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            continue
        if addr.version != 4:
            continue
        nets.add(ipaddress.ip_network(f"{host}/24", strict=False))

    hosts: list[str] = []
    for net in sorted(nets, key=str):
        for ip in net.hosts():
            hosts.append(str(ip))
            if len(hosts) >= max_hosts:
                return hosts
    return hosts


def _refresh_saved_hosts(devices: dict) -> None:
    """Persist current host/port for resolved devices; carry everything else.

    Only ``host``/``port`` are touched — volume, schedules, enabled state and
    every other per-speaker setting are keyed by friendly name and ride along
    untouched.  Keeping the saved IP fresh means the next direct-connect hits
    the right address even though DHCP moved the device.
    """
    if not devices:
        return
    try:
        current = load_config()
        speakers = current.get("speakers", {})
        if _persist_discovered_hosts(devices, speakers):
            current["speakers"] = speakers
            save_config(current)
    except Exception:
        logger.exception("Failed to persist refreshed speaker hosts")


def _locate_speakers(speakers_config: dict, names: list[str]) -> dict:
    """Resolve speakers through escalating fallbacks, then persist IP/UUID.

    1. Direct-connect to the saved host (identity-verified by UUID or name).
    2. Full mDNS browse, matched by UUID (preferred) or name — handles IP
       changes and renames, and covers groups.
    3. Unicast subnet scan by name — works even when mDNS is unreliable.

    Any device found at a new address has its host/port (and UUID, if not yet
    recorded) written back to config so the fast path works next time.
    Returns friendly_name -> Chromecast.
    """
    names = list(names)
    if not names:
        return {}

    devices = connect_speakers_direct(speakers_config, names, timeout=10)

    still = [n for n in names if n not in devices]
    if still:
        identities = {
            n: {
                "uuid": speakers_config.get(n, {}).get("uuid"),
                "match_by": speakers_config.get(n, {}).get("match_by", "device"),
            }
            for n in still
        }
        logger.info("  browse fallback for: %s", still)
        devices.update(find_speakers_by_name(still, timeout=15, identities=identities))

    still = [n for n in names if n not in devices]
    if still:
        hosts = _candidate_hosts(speakers_config)
        if hosts:
            devices.update(scan_network_for_speakers(still, hosts))
        else:
            logger.warning("No saved hosts to derive a scan range for: %s", still)

    still = [n for n in names if n not in devices]
    if still:
        logger.warning("Could not locate speaker(s) by any method: %s", still)

    _refresh_saved_hosts(devices)
    return devices


def _resolve_devices(
    speakers_config: dict,
    enabled: list[str],
    prayer_name: str | None,
) -> dict:
    """Return connected Chromecast objects for enabled speakers.

    Uses fresh pre-warm connections when available, otherwise locates each
    missing speaker by name (direct-connect -> mDNS -> unicast scan) and
    persists any refreshed IPs.
    """
    devices = _get_prewarmed(prayer_name)
    if devices:
        present = [n for n in enabled if n in devices]
        if len(present) == len(enabled):
            logger.info("Using pre-warmed connections for %d speaker(s)", len(present))
            return {n: devices[n] for n in present}
        # Partial hit — keep what we have, look up the rest.  Release the
        # pre-warmed devices we won't use so their worker threads don't linger.
        logger.info(
            "Pre-warm had %d/%d speakers; resolving the rest",
            len(present), len(enabled),
        )
        disconnect_all({n: cc for n, cc in devices.items() if n not in present})
        devices = {n: devices[n] for n in present}
    else:
        devices = {}

    missing = [n for n in enabled if n not in devices]
    if missing:
        devices.update(_locate_speakers(speakers_config, missing))

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

    devices: dict = {}
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
        # Heartbeat: a successful playback proves the unit is alive AND reaching
        # its speakers. Pinging only on success is what lets the dead-man's
        # switch catch a wedged process as well as unreachable speakers.
        if any(results.values()):
            heartbeat.ping_success()
    except Exception as exc:
        logger.error("%s Chromecast playback error: %s", event_label, exc)
    finally:
        # Release every connection opened for this playback.  Holding them keeps
        # pychromecast worker threads alive long after the adhan; when one later
        # loses its (already-stopped) mDNS browser it spins in a reconnect loop
        # that floods the logs — the storm that filled a Pi's disk to 100%.
        disconnect_all(devices)


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

    devices = _locate_speakers(speakers, enabled)

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
        #
        # Scheduler creation is deferred to start() so the timezone can be
        # pinned to the user's configured timezone instead of the host OS
        # default — see start() for why this matters.
        self.scheduler: BackgroundScheduler | None = None
        self._job_ids: list[str] = []
        self._last_config_check: float = 0.0
        self._scheduler_tz_name: str = "UTC"

    def _resolve_tz(self, config: dict) -> tuple[object, str]:
        """Return (tzinfo, name) from config, falling back to UTC on error."""
        name = config.get("timezone", "UTC")
        try:
            return pytz.timezone(name), name
        except Exception as exc:
            logger.warning(
                "Invalid configured timezone %r: %s — falling back to UTC", name, exc,
            )
            return pytz.UTC, "UTC"

    def _install_recurring_jobs(self, tz) -> None:
        """(Re)install the daily-reschedule and log-purge crons in the given tz.

        Both crons must fire at a wall-clock time in the user's local zone.
        The daily reschedule in particular HAS to run a few minutes after
        local midnight: if it instead fires hours later, prayers earlier in
        the day are already past `now` and get silently dropped from
        schedule_today().
        """
        self.scheduler.add_job(
            self.schedule_today,
            CronTrigger(hour=0, minute=1, timezone=tz),
            id="daily_reschedule",
            replace_existing=True,
        )
        self.scheduler.add_job(
            playback_log.purge,
            CronTrigger(hour=3, minute=17, timezone=tz),
            id="playback_log_purge",
            replace_existing=True,
        )

    def start(self) -> None:
        """Start the scheduler and set up the daily reschedule job."""
        config = load_config()
        tz, tz_name = self._resolve_tz(config)
        self._scheduler_tz_name = tz_name

        # Pin APScheduler to the configured timezone. Without this it falls
        # back to the host OS timezone (tzlocal), which on a fresh Pi image
        # is UTC — making the daily 00:01 cron fire hours before local
        # midnight and silently drop prayers that have already passed.
        self.scheduler = BackgroundScheduler(timezone=tz)
        self.scheduler.add_listener(self._on_misfire, EVENT_JOB_MISSED)

        self.scheduler.start()
        missing = validate_audio_files(config)
        if missing:
            logger.warning("Missing audio files at startup: %s", missing)
        self.schedule_today()
        self._last_config_check = time.time()

        self._install_recurring_jobs(tz)

        # Check for config changes every 30 seconds
        self.scheduler.add_job(
            self._check_config_change,
            "interval",
            seconds=30,
            id="config_watcher",
            replace_existing=True,
        )

        logger.info("Adhan scheduler started (timezone=%s)", tz_name)

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
                config = load_config()
                new_tz, new_tz_name = self._resolve_tz(config)
                if new_tz_name != self._scheduler_tz_name:
                    logger.info(
                        "Timezone changed: %s -> %s, updating scheduler",
                        self._scheduler_tz_name, new_tz_name,
                    )
                    # Update default tz for newly-added jobs and re-pin the
                    # recurring crons so they fire at local-midnight again.
                    self.scheduler.configure(timezone=new_tz)
                    self._install_recurring_jobs(new_tz)
                    self._scheduler_tz_name = new_tz_name
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

        # Discard any pre-warmed devices left from yesterday — disconnecting
        # them first so their socket-worker threads don't outlive the cache.
        with _prewarm_cache_lock:
            leftover = list(_prewarm_cache.values())
            _prewarm_cache.clear()
        for entry in leftover:
            disconnect_all(entry.get("devices", {}))

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
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
