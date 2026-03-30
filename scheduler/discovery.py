"""mDNS device discovery for Google Nest/Home speakers."""

import logging
import time

import pychromecast

logger = logging.getLogger(__name__)


def discover_chromecasts(timeout: int = 10) -> dict[str, pychromecast.Chromecast]:
    """Discover all Chromecast-compatible devices on the local network.

    Returns a mapping of friendly_name -> Chromecast object.
    """
    logger.info("Scanning for Chromecast devices (timeout=%ds)...", timeout)
    browser = pychromecast.get_chromecasts(timeout=timeout)
    chromecasts = browser[0]
    devices = {}
    for cc in chromecasts:
        name = cc.cast_info.friendly_name
        devices[name] = cc
        cast_type = getattr(cc.cast_info, "cast_type", "cast")
        logger.info("Found device: %s (%s, type=%s)", name, cc.cast_info.model_name, cast_type)
    return devices


def get_device_metadata(chromecasts: dict[str, pychromecast.Chromecast]) -> dict[str, dict]:
    """Return display metadata (model, type) for each discovered device.

    Returns a mapping of friendly_name -> {model, is_group}.
    """
    meta = {}
    for name, cc in chromecasts.items():
        cast_type = getattr(cc.cast_info, "cast_type", "cast")
        meta[name] = {
            "model": cc.cast_info.model_name,
            "is_group": cast_type == "group",
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
) -> dict[str, bool]:
    """Play audio on all enabled speakers.

    Args:
        devices: All discovered devices.
        enabled_names: Friendly names of speakers that should play.
        media_url: HTTP URL to the audio file.
        volume: Playback volume.

    Returns a dict of device_name -> success.
    """
    results = {}
    for name in enabled_names:
        if name in devices:
            results[name] = play_on_chromecast(devices[name], media_url, volume=volume)
        else:
            logger.warning("Speaker '%s' not found on network", name)
            results[name] = False
    return results
