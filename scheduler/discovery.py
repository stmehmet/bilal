"""mDNS device discovery + direct-connect for Google Nest/Home speakers."""

import logging
import time
import threading

import pychromecast

logger = logging.getLogger(__name__)

# Discovery cache to avoid rescanning on every prayer time
_cache_lock = threading.Lock()
_cached_devices: dict[str, pychromecast.Chromecast] = {}
_cache_timestamp: float = 0
CACHE_TTL_SECONDS = 600  # 10 minutes (bumped from 5 — direct-connect is the fast path now)


def discover_chromecasts(timeout: int = 10, use_cache: bool = True) -> dict[str, pychromecast.Chromecast]:
    """Discover all Chromecast-compatible devices on the local network.

    Returns a mapping of friendly_name -> Chromecast object.
    Uses a cache to avoid slow mDNS scans on every prayer playback.
    """
    global _cached_devices, _cache_timestamp

    if use_cache:
        with _cache_lock:
            if _cached_devices and (time.time() - _cache_timestamp) < CACHE_TTL_SECONDS:
                logger.debug("Using cached Chromecast devices (%d devices)", len(_cached_devices))
                return _cached_devices

    logger.info("Scanning for Chromecast devices (timeout=%ds)...", timeout)
    browser = pychromecast.get_chromecasts(timeout=timeout)
    chromecasts = browser[0]
    devices = {}
    for cc in chromecasts:
        name = cc.cast_info.friendly_name
        devices[name] = cc
        cast_type = getattr(cc.cast_info, "cast_type", "cast")
        logger.info("Found device: %s (%s, type=%s)", name, cc.cast_info.model_name, cast_type)

    with _cache_lock:
        _cached_devices = devices
        _cache_timestamp = time.time()

    return devices


def connect_by_host(host: str, port: int = 8009, timeout: float = 10) -> pychromecast.Chromecast | None:
    """Connect directly to a Chromecast by IP address, skipping mDNS.

    Returns a Chromecast object on success, None on failure.
    """
    try:
        casts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=None,
            known_hosts=[host],
            timeout=timeout,
        )
        if browser:
            browser.stop_discovery()
        if casts:
            cc = casts[0]
            cc.wait(timeout=timeout)
            return cc
    except Exception as exc:
        logger.debug("Direct connect to %s:%d failed: %s", host, port, exc)
    return None


def connect_speakers_direct(
    speakers_config: dict,
    enabled_names: list[str],
    timeout: float = 10,
) -> dict[str, pychromecast.Chromecast]:
    """Connect to enabled speakers using stored host/port (no mDNS).

    Tries direct connection for each speaker that has a saved host.
    Returns a mapping of friendly_name -> Chromecast for successful connections.
    """
    devices = {}
    for name in enabled_names:
        info = speakers_config.get(name, {})
        host = info.get("host")
        port = info.get("port", 8009)
        if not host:
            continue
        cc = connect_by_host(host, port, timeout=timeout)
        if cc:
            devices[name] = cc
            logger.info("Direct connect OK: %s (%s:%d)", name, host, port)
        else:
            logger.info("Direct connect failed: %s (%s:%d), will fall back to mDNS", name, host, port)
    return devices


def get_device_metadata(chromecasts: dict[str, pychromecast.Chromecast]) -> dict[str, dict]:
    """Return display metadata (model, type, host, port) for each discovered device.

    Returns a mapping of friendly_name -> {model, is_group, host, port}.
    """
    meta = {}
    for name, cc in chromecasts.items():
        cast_type = getattr(cc.cast_info, "cast_type", "cast")
        host = None
        port = 8009
        # Extract host from cast_info
        if hasattr(cc.cast_info, "host"):
            host = cc.cast_info.host
        elif hasattr(cc.cast_info, "services") and cc.cast_info.services:
            for service in cc.cast_info.services:
                if hasattr(service, "__iter__") and len(service) >= 2:
                    host = service[1] if isinstance(service[1], str) and "." in service[1] else None
                    if host:
                        break
        if hasattr(cc.cast_info, "port"):
            port = cc.cast_info.port
        meta[name] = {
            "model": cc.cast_info.model_name,
            "is_group": cast_type == "group",
            "host": host,
            "port": port,
        }
    return meta


def play_on_chromecast(
    device: pychromecast.Chromecast,
    media_url: str,
    content_type: str = "audio/mpeg",
    volume: float = 0.5,
) -> bool:
    """Cast an audio file to a single Chromecast device.

    Args:
        device: A connected Chromecast instance.
        media_url: HTTP URL to the audio file (served by our web app).
        content_type: MIME type of the audio.
        volume: Playback volume 0.0-1.0.

    Returns True on success.
    """
    try:
        device.wait()
        device.set_volume(volume)
        mc = device.media_controller
        mc.play_media(media_url, content_type)
        mc.block_until_active(timeout=30)
        logger.info("Playing on %s", device.cast_info.friendly_name)
        return True
    except pychromecast.error.PyChromecastError as exc:
        logger.error(
            "Chromecast error on %s: %s", device.cast_info.friendly_name, exc
        )
        return False
    except (OSError, ConnectionError) as exc:
        logger.error(
            "Network error playing on %s: %s", device.cast_info.friendly_name, exc
        )
        return False


def play_on_all(
    devices: dict[str, pychromecast.Chromecast],
    enabled_names: list[str],
    media_url: str,
    volume: float = 0.5,
    speaker_volumes: dict[str, float] | None = None,
) -> dict[str, bool]:
    """Play audio on all enabled speakers in parallel.

    Args:
        devices: All discovered devices.
        enabled_names: Friendly names of speakers that should play.
        media_url: HTTP URL to the audio file.
        volume: Default playback volume (used when no per-speaker override).
        speaker_volumes: Optional per-speaker volume overrides.

    Returns a dict of device_name -> success.
    """
    results = {}
    missing = [n for n in enabled_names if n not in devices]
    for name in missing:
        logger.warning("Speaker '%s' not found on network", name)
        results[name] = False

    present = [n for n in enabled_names if n in devices]
    if not present:
        return results

    # Play on all speakers concurrently so one slow/stuck device
    # doesn't delay or block the others.
    threads: list[threading.Thread] = []
    thread_results: dict[str, bool] = {}
    lock = threading.Lock()

    def _play(name: str) -> None:
        vol = speaker_volumes.get(name, volume) if speaker_volumes else volume
        t0 = time.time()
        ok = play_on_chromecast(devices[name], media_url, volume=vol)
        elapsed = time.time() - t0
        with lock:
            thread_results[name] = ok
        if ok:
            logger.info("  %s responded in %.1fs", name, elapsed)
        else:
            logger.error("  %s FAILED after %.1fs", name, elapsed)

    for name in present:
        t = threading.Thread(target=_play, args=(name,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=45)

    # Any thread that didn't finish in time
    for name in present:
        if name not in thread_results:
            logger.error("  %s TIMED OUT (no response in 45s)", name)
            thread_results[name] = False

    results.update(thread_results)
    return results
