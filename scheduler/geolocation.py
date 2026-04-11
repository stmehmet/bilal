"""Geolocation helpers — IP-based auto-detect and address-based lookup."""

import logging

import requests

logger = logging.getLogger(__name__)

# Nominatim (OpenStreetMap) public endpoint. Requires a descriptive
# User-Agent per their usage policy; rate-limited to 1 req/sec which is
# more than enough for our occasional lookups.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "bilal-adhan-system/1.0 (https://github.com/stmehmet/bilal)"

# Free timezone-from-coordinates lookup. No API key required.
TIMEAPI_TZ_URL = "https://timeapi.io/api/timezone/coordinate"

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


def _timezone_for_coords(lat: float, lon: float) -> str | None:
    """Return the IANA timezone string for the given coordinates, or None.

    Uses the free timeapi.io endpoint. Failures are logged and swallowed so
    the caller can fall back to keeping the previously-configured timezone.
    """
    try:
        resp = requests.get(
            TIMEAPI_TZ_URL,
            params={"latitude": lat, "longitude": lon},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        tz = data.get("timeZone")
        if isinstance(tz, str) and tz:
            return tz
        logger.warning("timeapi.io returned no timeZone for %s,%s: %s", lat, lon, data)
    except requests.RequestException as exc:
        logger.warning("Timezone lookup for %s,%s failed: %s", lat, lon, exc)
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Timezone lookup parse error for %s,%s: %s", lat, lon, exc)
    return None


def geocode_address(address: str) -> dict | None:
    """Resolve a street address or place name to a location dict.

    Uses OpenStreetMap's Nominatim service for the forward-geocoding step,
    then a second request to timeapi.io for the IANA timezone at those
    coordinates.

    Returns the same shape as `detect_location()` on success:
        {
          "latitude": float,
          "longitude": float,
          "city": str,
          "country": str,
          "timezone": str,  # may be "UTC" if timezone lookup failed
        }
    or None if the address could not be resolved.
    """
    address = (address or "").strip()
    if not address:
        return None
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": address,
                "format": "json",
                "limit": 1,
                "addressdetails": 1,
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        logger.warning("Nominatim request failed for %r: %s", address, exc)
        return None
    except ValueError as exc:
        logger.warning("Nominatim returned invalid JSON for %r: %s", address, exc)
        return None

    if not results:
        logger.info("Nominatim: no results for %r", address)
        return None

    hit = results[0]
    try:
        lat = float(hit["lat"])
        lon = float(hit["lon"])
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Nominatim result missing lat/lon for %r: %s", address, exc)
        return None

    # Nominatim's `address` object has city/town/village/hamlet depending on
    # the size of the place. Fall through in preference order.
    addr = hit.get("address", {}) or {}
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("hamlet")
        or addr.get("suburb")
        or addr.get("municipality")
        or addr.get("county")
        or "Unknown"
    )
    country = addr.get("country") or "Unknown"

    timezone = _timezone_for_coords(lat, lon) or "UTC"

    return {
        "latitude": lat,
        "longitude": lon,
        "city": city,
        "country": country,
        "timezone": timezone,
    }
