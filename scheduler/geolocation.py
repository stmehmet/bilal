"""IP-based geolocation for automatic location detection."""

import logging

import requests

logger = logging.getLogger(__name__)

PROVIDERS = [
    {
        "url": "https://ipapi.co/json/",
        "lat": "latitude",
        "lon": "longitude",
        "city": "city",
        "country": "country_name",
        "tz": "timezone",
    },
    {
        "url": "https://ipinfo.io/json",
        "lat": "loc",  # "lat,lon" format
        "lon": "loc",
        "city": "city",
        "country": "country",
        "tz": "timezone",
    },
    {
        "url": "http://ip-api.com/json/?fields=lat,lon,city,country,timezone",
        "lat": "lat",
        "lon": "lon",
        "city": "city",
        "country": "country",
        "tz": "timezone",
    },
]


def detect_location() -> dict | None:
    """Detect location from public IP using multiple providers.

    Returns a dict with latitude, longitude, city, country, timezone
    or None on failure.
    """
    for provider in PROVIDERS:
        try:
            resp = requests.get(provider["url"], timeout=10)
            resp.raise_for_status()
            data = resp.json()

            lat = data.get(provider["lat"])
            lon = data.get(provider["lon"])

            # ipinfo returns "lat,lon" as a single field
            if isinstance(lat, str) and "," in lat:
                parts = lat.split(",")
                lat, lon = float(parts[0]), float(parts[1])
            else:
                lat, lon = float(lat), float(lon)

            return {
                "latitude": lat,
                "longitude": lon,
                "city": data.get(provider["city"], "Unknown"),
                "country": data.get(provider["country"], "Unknown"),
                "timezone": data.get(provider["tz"], "UTC"),
            }
        except requests.RequestException as exc:
            logger.warning("Geolocation request to %s failed: %s", provider["url"], exc)
            continue
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Geolocation parse error from %s: %s", provider["url"], exc)
            continue

    logger.error("All geolocation providers failed")
    return None
