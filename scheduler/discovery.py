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


def _cast_uuid(cast_info) -> str | None:
    """Return a device's UUID as a string, or None if it has none.

    The UUID is the only stable identifier: it survives DHCP IP changes *and*
    Google Home renames, and it stays constant for cast groups even though
    their port is ephemeral.  Friendly name is the fallback when no UUID is on
    record yet.
    """
    uuid = getattr(cast_info, "uuid", None)
    return str(uuid) if uuid else None


def connect_by_host(
    host: str,
    port: int = 8009,
    timeout: float = 10,
    expected_name: str | None = None,
    expected_uuid: str | None = None,
) -> pychromecast.Chromecast | None:
    """Connect directly to a Chromecast by IP address, skipping mDNS.

    Identity is verified before the device is accepted, so a stale saved IP that
    DHCP has since handed to a *different* cast device never receives the adhan.
    When ``expected_uuid`` is given it wins (UUIDs are stable across IP changes
    and renames); otherwise ``expected_name`` is checked.

    Returns a Chromecast object on success, None on failure or identity mismatch.
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
        if expected_uuid:
            got = _cast_uuid(cc.cast_info)
            if got != str(expected_uuid):
                logger.info(
                    "Saved host %s:%d serves uuid %s (wanted %s) — treating as moved",
                    host, port, got, expected_uuid,
                )
                _safe_disconnect(cc)
                return None
        elif expected_name is not None and cc.cast_info.friendly_name != expected_name:
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
    targets: list[tuple[str, str, int, str | None]] = []
    for name in enabled_names:
        info = speakers_config.get(name, {})
        host = info.get("host")
        if not host:
            continue
        port = info.get("port", 8009)
        # Verify by UUID when this slot is pinned to a specific device; by name
        # when it should follow whatever device currently advertises that name.
        expected_uuid = info.get("uuid") if info.get("match_by", "device") == "device" else None
        targets.append((name, host, port, expected_uuid))

    if not targets:
        return {}

    devices: dict[str, pychromecast.Chromecast] = {}
    max_workers = min(len(targets), 8)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="direct-cc") as pool:
        futures = {
            pool.submit(connect_by_host, host, port, timeout, name, expected_uuid): (name, host, port)
            for name, host, port, expected_uuid in targets
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
                logger.info("Direct connect failed: %s (%s:%d), will fall back to browse", name, host, port)
    return devices


def find_speakers_by_name(
    names: list[str],
    timeout: float = 15,
    tries: int = 2,
    retry_wait: float = 2.0,
    identities: dict | None = None,
) -> dict[str, pychromecast.Chromecast]:
    """Locate speakers via a full mDNS browse, matching by UUID or friendly name.

    A broad ``get_chromecasts()`` browse is dramatically more reliable than the
    targeted ``get_listed_chromecasts(friendly_names=...)`` / ``known_hosts=...``
    discovery, which on real networks intermittently returns nothing for devices
    a full browse finds and connects to in well under a second — and which can't
    recover cast *groups* (their port is ephemeral; only the browse re-finds
    them).  This is the primary self-heal path when a speaker's address changes.

    ``identities`` optionally maps a wanted name to
    ``{"uuid": str|None, "match_by": "device"|"name"}`` so a speaker can be
    re-found by its stable UUID (survives IP changes *and* Google Home renames)
    instead of its current friendly name.  Without it, matching is by name.

    Every browsed device we don't keep is disconnected so its background
    socket-client thread doesn't leak — unbounded accumulation of those threads
    is what eventually wedged a long-running scheduler with "can't start new
    thread".  ``tries``/``retry_wait`` are accepted for call compatibility.

    Returns friendly_name -> Chromecast for the names that were found.
    """
    names = [n for n in names if n]
    if not names:
        return {}
    identities = identities or {}

    # Split the wanted speakers into UUID matchers (preferred) and name matchers.
    want_by_uuid: dict[str, str] = {}
    want_by_name: dict[str, str] = {}
    for n in names:
        ident = identities.get(n, {})
        uuid = ident.get("uuid")
        if ident.get("match_by", "device") == "device" and uuid:
            want_by_uuid[str(uuid)] = n
        else:
            want_by_name[n] = n

    logger.info("mDNS browse to locate %d speaker(s): %s", len(names), names)
    try:
        chromecasts, browser = pychromecast.get_chromecasts(timeout=timeout)
    except Exception as exc:
        logger.info("mDNS browse failed: %s", exc)
        return {}

    found: dict[str, pychromecast.Chromecast] = {}
    try:
        for cc in chromecasts:
            info = cc.cast_info
            target = None
            cc_uuid = _cast_uuid(info)
            if cc_uuid and cc_uuid in want_by_uuid and want_by_uuid[cc_uuid] not in found:
                target = want_by_uuid[cc_uuid]
            elif info.friendly_name in want_by_name and want_by_name[info.friendly_name] not in found:
                target = want_by_name[info.friendly_name]
            if target is None:
                _safe_disconnect(cc)  # not wanted — release its socket thread
                continue
            try:
                cc.wait(timeout=timeout)
            except Exception:
                logger.debug("Found '%s' via browse but it never became ready", target)
                _safe_disconnect(cc)
                continue
            found[target] = cc
            logger.info(
                "Located '%s' via browse at %s:%s",
                target, getattr(info, "host", None), getattr(info, "port", None),
            )
    finally:
        if browser:
            try:
                browser.stop_discovery()
            except Exception:
                pass
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
            "uuid": _cast_uuid(cc.cast_info),
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
