"""Samsung SmartThings API integration for Family Hub notifications."""

import logging

import requests

logger = logging.getLogger(__name__)

ST_API_BASE = "https://api.smartthings.com/v1"


def list_devices(token: str) -> list[dict]:
    """List all SmartThings devices."""
    if not token:
        return []
    try:
        resp = requests.get(
            f"{ST_API_BASE}/devices",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("items", [])
    except requests.RequestException as exc:
        logger.error("SmartThings device listing failed: %s", exc)
        return []


def send_notification(token: str, device_id: str, message: str) -> bool:
    """Send a notification to a SmartThings device (e.g., Family Hub fridge).

    Uses the 'notification' capability to display a message on the device.
    """
    if not token or not device_id:
        logger.warning("SmartThings not configured, skipping notification")
        return False

    try:
        resp = requests.post(
            f"{ST_API_BASE}/devices/{device_id}/commands",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "commands": [
                    {
                        "component": "main",
                        "capability": "notification",
                        "command": "sendNotification",
                        "arguments": [message],
                    }
                ]
            },
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("SmartThings notification sent to %s", device_id)
        return True
    except requests.RequestException as exc:
        logger.error("SmartThings notification failed: %s", exc)
        return False


def play_audio_on_device(token: str, device_id: str, audio_url: str) -> bool:
    """Attempt audio playback on a SmartThings device via audioNotification.

    Falls back to a text notification if the capability is not supported.
    """
    if not token or not device_id:
        return False

    try:
        resp = requests.post(
            f"{ST_API_BASE}/devices/{device_id}/commands",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "commands": [
                    {
                        "component": "main",
                        "capability": "audioNotification",
                        "command": "playTrackAndResume",
                        "arguments": [audio_url, 50],
                    }
                ]
            },
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("SmartThings audio playback started on %s", device_id)
        return True
    except requests.RequestException as exc:
        logger.warning(
            "SmartThings audio playback failed on %s: %s, "
            "falling back to notification",
            device_id,
            exc,
        )
        return send_notification(
            token, device_id, "Adhan time - please check your prayer schedule"
        )
