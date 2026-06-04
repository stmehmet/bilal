"""mDNS device discovery + direct-connect for Google Nest/Home speakers."""

import logging
import socket
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pychromecast

logger = logging.getLogger(__name__)

# Discovery cache to avoid rescanning on every prayer time
_cache_lock = threading.Lock()
_cached_devices: dict[str, pychromecast.Chromecast] = {}
_cache_timestamp: float = 0
CACHE_TTL_SECONDS = 600  # 10 minutes (bumped from 5 — direct-connect is the fast path now)

# Group devices take 1–3s to propagate volume through the mesh; sending
# play_media immediately after set_volume can arrive mid-sync.  A small
# gap removes the race.
GROUP_VOLUME_STAGGER_SECONDS = 2.0

# Hard cap on the initial ``device.wait()`` inside a play attempt.  Without
# this, an unreachable speaker can chew through the entire per-prayer 45s
# budget on the very first call — leaving no time for the retry in
# ``play_on_chromecast`` and, worse, leaving a daemon thread blocked on a
# socket that pychromecast never times out on.  When the device finally
# recovers (Google Nest pushes overnight firmware and reboots around 3 AM)
# that queued ``play_media`` completes and the speaker blasts a stale
# adhan hours late.
CAST_WAIT_TIMEOUT_SECONDS = 15

# Absolute wall-clock budget across all speakers for one playback fan-out.
# Per-thread join timeouts compound to N*45s if every speaker hangs; an
# absolute deadline keeps the total bounded.
PLAY_DEADLINE_SECONDS = 45


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


def _safe_disconnect(device: pychromecast.Chromecast) -> None:
    """Close a Chromecast connection without blocking or raising."""
    try:
        device.disconnect(timeout=0)
    except Exception:
        pass


def connect_by_host(
    host: str,
    port: int = 8009,
    timeout: float = 10,
    expected_name: str | None = None,
) -> pychromecast.Chromecast | None:
    """Connect directly to a Chromecast by IP address, skipping mDNS.

    When ``expected_name`` is given, the device answering at ``host`` must
    report that friendly name or the connection is rejected.  This guards
    against a stale saved IP that DHCP has since handed to a *different* cast
    device — without the check we would happily blast the adhan on whatever
    speaker now lives at the old address.

    Returns a Chromecast object on success, None on failure or name mismatch.
    """
    try:
        casts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=None,
            known_hosts=[host],
            timeout=timeout,
        )
        if browser:
            browser.stop_discovery()
        if not casts:
            return None
        cc = casts[0]
        cc.wait(timeout=timeout)
        if expected_name is not None and cc.cast_info.friendly_name != expected_name:
            logger.info(
                "Saved host %s:%d now serves '%s' (wanted '%s') — treating as moved",
                host, port, cc.cast_info.friendly_name, expected_name,
            )
            _safe_disconnect(cc)
            return None
        return cc
    except Exception as exc:
        logger.debug("Direct connect to %s:%d failed: %s", host, port, exc)
    return None


def connect_speakers_direct(
    speakers_config: dict,
    enabled_names: list[str],
    timeout: float = 10,
) -> dict[str, pychromecast.Chromecast]:
    """Connect to enabled speakers using stored host/port (no mDNS), in parallel.

    Previous behaviour was serial: N speakers with unreachable hosts took N *
    timeout seconds before the mDNS fallback even started.  Now all speakers
    connect concurrently so the worst-case is a single timeout regardless of
    fleet size.
    """
    targets: list[tuple[str, str, int]] = []
    for name in enabled_names:
        info = speakers_config.get(name, {})
        host = info.get("host")
        if not host:
            continue
        port = info.get("port", 8009)
        targets.append((name, host, port))

    if not targets:
        return {}

    devices: dict[str, pychromecast.Chromecast] = {}
    max_workers = min(len(targets), 8)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="direct-cc") as pool:
        futures = {
            pool.submit(connect_by_host, host, port, timeout, name): (name, host, port)
            for name, host, port in targets
        }
        for fut in as_completed(futures):
            name, host, port = futures[fut]
            try:
                cc = fut.result()
            except Exception as exc:
                logger.info("Direct connect errored: %s (%s:%d): %s", name, host, port, exc)
                continue
            if cc:
                devices[name] = cc
                logger.info("Direct connect OK: %s (%s:%d)", name, host, port)
            else:
                logger.info("Direct connect failed: %s (%s:%d), will fall back to mDNS", name, host, port)
    return devices


def find_speakers_by_name(
    names: list[str],
    timeout: float = 15,
    tries: int = 2,
    retry_wait: float = 2.0,
) -> dict[str, pychromecast.Chromecast]:
    """Locate specific speakers by friendly name via mDNS.

    Unlike ``discover_chromecasts`` (which grabs whatever turns up in a fixed
    window), this actively hunts for the named devices and keeps retrying until
    it finds them or ``timeout`` elapses.  This is the primary self-heal path
    when a speaker's IP changes: the friendly name is stable, the address is
    not.  Works for groups as well as individual speakers.

    Returns friendly_name -> Chromecast for the names that were found.
    """
    names = [n for n in names if n]
    if not names:
        return {}
    logger.info("mDNS lookup by name for: %s", names)
    try:
        casts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=list(names),
            tries=tries,
            retry_wait=retry_wait,
            timeout=timeout,
        )
        if browser:
            browser.stop_discovery()
    except Exception as exc:
        logger.info("mDNS name lookup failed: %s", exc)
        return {}

    wanted = set(names)
    found: dict[str, pychromecast.Chromecast] = {}
    for cc in casts:
        name = cc.cast_info.friendly_name
        if name not in wanted or name in found:
            continue
        try:
            cc.wait(timeout=timeout)
        except Exception:
            logger.debug("Found '%s' via mDNS but it never became ready", name)
            _safe_disconnect(cc)
            continue
        found[name] = cc
        logger.info("Located '%s' via mDNS at %s:%d", name, cc.cast_info.host, cc.cast_info.port)
    return found


def _tcp_port_open(host: str, port: int, timeout: float) -> bool:
    """Return True if a TCP connection to host:port completes within timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan_network_for_speakers(
    names: list[str],
    candidate_hosts: list[str],
    port: int = 8009,
    probe_timeout: float = 0.6,
    connect_timeout: float = 6,
    max_workers: int = 64,
) -> dict[str, pychromecast.Chromecast]:
    """Find speakers by name via a unicast sweep — no multicast/mDNS required.

    Last-resort self-heal for networks where mDNS is unreliable (WiFi multicast
    filtering, IGMP snooping, mesh routers).  Phase 1 fast-probes every
    candidate host for an open cast port; phase 2 connects to the handful that
    answer and matches them by friendly name.

    Only finds individual speakers, which use the stable :8009 port.  Cast
    *groups* use an ephemeral port and can only be recovered via mDNS, so they
    are intentionally out of scope here.

    Returns friendly_name -> Chromecast for the names that were found.
    """
    wanted = {n for n in names if n}
    if not wanted or not candidate_hosts:
        return {}
    logger.warning(
        "Unicast-scanning %d host(s) on :%d to recover %s (mDNS-independent fallback)",
        len(candidate_hosts), port, sorted(wanted),
    )

    open_hosts: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scan") as pool:
        futures = {pool.submit(_tcp_port_open, h, port, probe_timeout): h for h in candidate_hosts}
        for fut in as_completed(futures):
            try:
                if fut.result():
                    open_hosts.append(futures[fut])
            except Exception:
                pass
    logger.info("Scan: %d host(s) have :%d open", len(open_hosts), port)

    found: dict[str, pychromecast.Chromecast] = {}
    for host in open_hosts:
        if not wanted:
            break
        cc = connect_by_host(host, port, timeout=connect_timeout)
        if cc is None:
            continue
        name = cc.cast_info.friendly_name
        if name in wanted:
            found[name] = cc
            wanted.discard(name)
            logger.info("Recovered '%s' at %s:%d via unicast scan", name, host, port)
        else:
            _safe_disconnect(cc)
    return found


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


def _is_group_device(device: pychromecast.Chromecast) -> bool:
    return getattr(device.cast_info, "cast_type", "cast") == "group"


def _play_once(
    device: pychromecast.Chromecast,
    media_url: str,
    content_type: str,
    volume: float,
) -> bool:
    device.wait(timeout=CAST_WAIT_TIMEOUT_SECONDS)
    if device.status is None:
        # wait() returns silently after timeout; status stays None when the
        # connection never completed.  Raise so play_on_chromecast retries
        # or moves on instead of pushing volume/play_media at a dead socket.
        raise TimeoutError(
            f"{device.cast_info.friendly_name}: not ready after "
            f"{CAST_WAIT_TIMEOUT_SECONDS}s"
        )
    device.set_volume(volume)
    # Groups sync volume across their members for a couple of seconds; sending
    # play_media immediately after set_volume can arrive mid-sync.  A small
    # gap removes the race.
    if _is_group_device(device):
        time.sleep(GROUP_VOLUME_STAGGER_SECONDS)
    mc = device.media_controller
    mc.play_media(media_url, content_type)
    mc.block_until_active(timeout=30)
    return True


def play_on_chromecast(
    device: pychromecast.Chromecast,
    media_url: str,
    content_type: str = "audio/mpeg",
    volume: float = 0.5,
    retries: int = 1,
) -> bool:
    """Cast an audio file to a single Chromecast device with one automatic retry.

    Transient TCP hiccups, brief device unreachability, and Google group-mesh
    syncs produce flaky first attempts; the retry is cheap insurance against
    a completely missed adhan.
    """
    name = device.cast_info.friendly_name
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            _play_once(device, media_url, content_type, volume)
            if attempt > 0:
                logger.info("Playing on %s (succeeded on retry %d)", name, attempt)
            else:
                logger.info("Playing on %s", name)
            return True
        except pychromecast.error.PyChromecastError as exc:
            last_exc = exc
            logger.warning(
                "Chromecast error on %s (attempt %d/%d): %s",
                name, attempt + 1, retries + 1, exc,
            )
        except (OSError, ConnectionError, TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "Network error on %s (attempt %d/%d): %s",
                name, attempt + 1, retries + 1, exc,
            )
        if attempt < retries:
            time.sleep(3)

    logger.error("%s FAILED after %d attempt(s): %s", name, retries + 1, last_exc)
    return False


def play_on_all(
    devices: dict[str, pychromecast.Chromecast],
    enabled_names: list[str],
    media_url: str,
    volume: float = 0.5,
    speaker_volumes: dict[str, float] | None = None,
    on_result: "callable | None" = None,
) -> dict[str, bool]:
    """Play audio on all enabled speakers in parallel.

    Args:
        devices: All discovered devices.
        enabled_names: Friendly names of speakers that should play.
        media_url: HTTP URL to the audio file.
        volume: Default playback volume (used when no per-speaker override).
        speaker_volumes: Optional per-speaker volume overrides.
        on_result: Optional callback ``fn(name, ok, elapsed_seconds, error)``
            invoked once per speaker so callers can record per-device metrics
            without waiting for the whole fan-out.

    Returns a dict of device_name -> success.
    """
    results: dict[str, bool] = {}
    missing = [n for n in enabled_names if n not in devices]
    for name in missing:
        logger.warning("Speaker '%s' not found on network", name)
        results[name] = False
        if on_result is not None:
            try:
                on_result(name, False, 0.0, "not_found")
            except Exception:
                logger.exception("on_result callback raised")

    present = [n for n in enabled_names if n in devices]
    if not present:
        return results

    threads: list[threading.Thread] = []
    thread_results: dict[str, bool] = {}
    abandoned: set[str] = set()
    lock = threading.Lock()

    def _play(name: str) -> None:
        vol = speaker_volumes.get(name, volume) if speaker_volumes else volume
        t0 = time.time()
        error: str | None = None
        try:
            ok = play_on_chromecast(devices[name], media_url, volume=vol)
        except Exception as exc:
            ok = False
            error = str(exc)
            logger.exception("Unexpected error playing on %s", name)
        elapsed = time.time() - t0
        with lock:
            if name in abandoned:
                # Parent already logged a timeout and disconnected the cast for
                # this speaker.  Logging again would produce a phantom "OK"
                # entry hours after the fact when the underlying pychromecast
                # call finally unblocks — exactly the 3 AM mass-replay we're
                # trying to kill.
                logger.warning(
                    "  %s completed %.1fs after the %ds deadline (ok=%s); "
                    "discarding result",
                    name, elapsed, PLAY_DEADLINE_SECONDS, ok,
                )
                return
            thread_results[name] = ok
        if ok:
            logger.info("  %s responded in %.1fs", name, elapsed)
        else:
            logger.error("  %s FAILED after %.1fs", name, elapsed)
        if on_result is not None:
            try:
                on_result(name, ok, elapsed, error)
            except Exception:
                logger.exception("on_result callback raised")

    for name in present:
        t = threading.Thread(target=_play, args=(name,), daemon=True)
        threads.append(t)
        t.start()

    # Single absolute deadline rather than a per-thread join(timeout=45):
    # the latter compounds to N*45s in the worst case, which is what kept
    # daemon threads alive long enough to fire at 3 AM.
    deadline = time.time() + PLAY_DEADLINE_SECONDS
    for t in threads:
        remaining = max(0.0, deadline - time.time())
        t.join(timeout=remaining)

    for name in present:
        with lock:
            if name in thread_results:
                continue
            abandoned.add(name)
        logger.error("  %s TIMED OUT (no response in %ds)", name, PLAY_DEADLINE_SECONDS)
        # Disconnecting closes the socket the worker is blocked on, so
        # pychromecast errors out instead of completing the cast later.
        # ``timeout=0`` avoids blocking this thread on the disconnect itself.
        try:
            devices[name].disconnect(timeout=0)
        except Exception:
            logger.exception("Failed to disconnect %s after timeout", name)
        thread_results[name] = False
        if on_result is not None:
            try:
                on_result(name, False, float(PLAY_DEADLINE_SECONDS), "timeout")
            except Exception:
                logger.exception("on_result callback raised")

    results.update(thread_results)
    return results
