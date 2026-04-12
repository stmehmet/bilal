"""Bilal – Home Adhan System Web Dashboard."""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import pytz
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

# Allow importing from the scheduler package
sys.path.insert(0, os.getenv("SCHEDULER_PATH", "/app/scheduler"))

from config import (  # noqa: E402
    CALCULATION_METHODS,
    PRAYER_NAMES,
    load_config,
    save_config,
)
from discovery import discover_chromecasts, get_device_metadata  # noqa: E402
from geolocation import detect_location, geocode_address  # noqa: E402
from smartthings import list_devices as st_list_devices  # noqa: E402
from adhan_scheduler import compute_prayer_times, compute_iqamah_times, validate_audio_files  # noqa: E402

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(32).hex())

AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "/audio"))
AUTH_FILE = Path(os.getenv("CONFIG_DIR", "/data")) / "auth.json"

# Read version from VERSION file at module level
_version_file = Path(__file__).resolve().parent.parent / "VERSION"
APP_VERSION = _version_file.read_text().strip() if _version_file.exists() else "dev"


# ---------------------------------------------------------------------------
# Audio file display labels
# ---------------------------------------------------------------------------
# Maqam names rendered with proper Turkish orthography. Filenames stay ASCII
# (clean URLs / shells / filesystems); the display layer maps each slug to
# its diacritical form.
MAQAM_LABELS: dict[str, str] = {
    "saba": "Saba",
    "ussak": "Uşşak",
    "rast": "Rast",
    "segah": "Segâh",
    "hicaz": "Hicaz",
}


def audio_display_label(filename: str) -> str:
    """Return a human-readable label for an audio file.

    Supported patterns:

      * ``adhan_<prayer>_<maqam>_<number>.mp3`` → "Saba 1", "Uşşak 2"
      * ``adhan_<prayer>_<maqam>.mp3``           → "Saba"
      * ``iqamah_<name>.mp3``                    → "Bell"

    Maqam slugs are looked up in MAQAM_LABELS for Turkish orthography.
    """
    stem = filename[:-4] if filename.endswith(".mp3") else filename
    parts = stem.split("_")

    # adhan_<prayer>_<maqam>_<number>
    if len(parts) >= 4 and parts[0] == "adhan":
        maqam_slug = parts[2]
        number = parts[3]
        maqam = MAQAM_LABELS.get(maqam_slug, maqam_slug.title())
        return f"{maqam} {number}"

    # adhan_<prayer>_<maqam> (no number)
    if len(parts) == 3 and parts[0] == "adhan":
        maqam_slug = parts[2]
        return MAQAM_LABELS.get(maqam_slug, maqam_slug.title())

    # iqamah_<name>
    if len(parts) >= 2 and parts[0] == "iqamah":
        return parts[1].title()

    # Fallback
    return " ".join(p.title() for p in parts if p)


# Valid prayer slugs (lowercase) used in filenames and filtering.
_PRAYER_SLUGS = {"fajr", "dhuhr", "asr", "maghrib", "isha"}


def _audio_file_category(filename: str) -> tuple[str, str | None]:
    """Classify an audio filename.

    Returns one of:
      * ("adhan", "<prayer>") — e.g. ("adhan", "fajr") for a per-prayer adhan
      * ("iqamah", None)      — iqamah_<name>.mp3
      * ("other", None)       — anything else
    """
    stem = filename[:-4] if filename.endswith(".mp3") else filename
    parts = stem.split("_")
    if len(parts) >= 2 and parts[0] == "adhan" and parts[1] in _PRAYER_SLUGS:
        return ("adhan", parts[1])
    if len(parts) >= 2 and parts[0] == "iqamah":
        return ("iqamah", None)
    return ("other", None)


def _build_audio_file_list() -> list[dict]:
    """Scan AUDIO_DIR and return a sorted list of {filename, label} dicts."""
    if not AUDIO_DIR.exists():
        return []
    entries = [
        {"filename": f.name, "label": audio_display_label(f.name)}
        for f in AUDIO_DIR.iterdir()
        if f.suffix == ".mp3"
    ]
    # Sort by label so the dropdowns are alphabetised by human-readable name
    entries.sort(key=lambda e: (e["label"].lower(), e["filename"]))
    return entries


def _build_audio_files_by_prayer() -> dict[str, list[dict]]:
    """Return per-prayer adhan dropdown options.

    Each prayer (Fajr, Dhuhr, Asr, Maghrib, Isha) gets a list of
    {filename, label} entries for the adhan files whose filename prayer
    segment matches. This drives the per-prayer dropdowns so Fajr only
    shows Saba maqam recitations, Dhuhr only shows Uşşak, etc.

    Prayers with no matching files get an empty list; the template shows
    a "(no audio files)" placeholder option in that case.
    """
    result: dict[str, list[dict]] = {
        "Fajr": [], "Dhuhr": [], "Asr": [], "Maghrib": [], "Isha": [],
    }
    if not AUDIO_DIR.exists():
        return result
    slug_to_prayer = {
        "fajr": "Fajr", "dhuhr": "Dhuhr", "asr": "Asr",
        "maghrib": "Maghrib", "isha": "Isha",
    }
    for f in AUDIO_DIR.iterdir():
        if f.suffix != ".mp3":
            continue
        category, prayer_slug = _audio_file_category(f.name)
        if category != "adhan" or prayer_slug is None:
            continue
        prayer = slug_to_prayer[prayer_slug]
        result[prayer].append(
            {"filename": f.name, "label": audio_display_label(f.name)}
        )
    for prayer in result:
        result[prayer].sort(key=lambda e: (e["label"].lower(), e["filename"]))
    return result



# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class User(UserMixin):
    def __init__(self, uid: str = "admin"):
        self.id = uid


@login_manager.user_loader
def load_user(uid):
    return User(uid)


def _load_auth() -> dict:
    if AUTH_FILE.exists():
        with open(AUTH_FILE) as f:
            return json.load(f)
    return {}


def _save_auth(data: dict) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUTH_FILE, "w") as f:
        json.dump(data, f)


# Simple in-memory rate limiter for login attempts
_login_attempts: dict[str, list[float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300  # 5 minutes


def _is_rate_limited(ip: str) -> bool:
    """Return True if the IP has exceeded login attempt limits."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Remove old attempts outside the window
    attempts = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


@app.route("/login", methods=["GET", "POST"])
def login():
    auth = _load_auth()
    client_ip = request.remote_addr or "unknown"

    # If no password set yet, redirect to initial setup
    if not auth.get("password_hash"):
        if request.method == "POST":
            pw = request.form.get("password", "")
            if len(pw) < 8:
                flash("Password must be at least 8 characters.", "error")
                return render_template("login.html", first_time=True)
            _save_auth({"password_hash": generate_password_hash(pw)})
            login_user(User())
            logger.info("Initial password created from %s", client_ip)
            return redirect(url_for("dashboard"))
        return render_template("login.html", first_time=True)

    if request.method == "POST":
        if _is_rate_limited(client_ip):
            logger.warning("Login rate limited for %s", client_ip)
            flash("Too many login attempts. Please try again later.", "error")
            return render_template("login.html", first_time=False), 429

        pw = request.form.get("password", "")
        if check_password_hash(auth["password_hash"], pw):
            _login_attempts.pop(client_ip, None)  # Clear on success
            login_user(User())
            logger.info("Successful login from %s", client_ip)
            return redirect(url_for("dashboard"))
        _record_login_attempt(client_ip)
        logger.warning("Failed login attempt from %s", client_ip)
        flash("Incorrect password.", "error")
    return render_template("login.html", first_time=False)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Audio file serving (for Chromecast playback)
# ---------------------------------------------------------------------------
@app.route("/audio/<path:filename>")
def serve_audio(filename):
    # Only serve .mp3 files to prevent path traversal or unexpected file access
    if not filename.endswith(".mp3") or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid audio file"}), 400
    return send_from_directory(str(AUDIO_DIR), filename)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    config = load_config()
    raw_times = {}
    times = {}
    iqamah_times = {}
    next_prayer = None

    if config.get("latitude") is not None:
        import datetime
        raw_times = compute_prayer_times(config)
        tz = pytz.timezone(config.get("timezone", "UTC"))
        now = datetime.datetime.now(tz)

        # Format for display
        times = {k: v.strftime("%I:%M %p") for k, v in raw_times.items()}

        # Iqamah times (only for the five prayers, not Sunrise)
        iq_raw = compute_iqamah_times(config, raw_times)
        iqamah_times = {k: v.strftime("%I:%M %p") for k, v in iq_raw.items()}

        # Next prayer countdown (ISO string for JS)
        for prayer in PRAYER_NAMES:
            pt = raw_times.get(prayer)
            if pt and pt > now:
                next_prayer = {"name": prayer, "iso": pt.isoformat()}
                break

    audio_files = _build_audio_file_list()
    audio_files_by_prayer = _build_audio_files_by_prayer()

    return render_template(
        "dashboard.html",
        config=config,
        prayer_times=times,
        iqamah_times=iqamah_times,
        prayer_names=PRAYER_NAMES,
        methods=CALCULATION_METHODS,
        audio_files=audio_files,
        audio_files_by_prayer=audio_files_by_prayer,
        next_prayer=next_prayer,
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
@login_required
def get_config():
    return jsonify(load_config())


def _validate_time_format(value: str) -> bool:
    """Validate HH:MM time format."""
    return bool(re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value))


def _validate_coordinate(lat: float, lon: float) -> str | None:
    """Return an error message if coordinates are out of bounds, else None."""
    if not (-90 <= lat <= 90):
        return f"Latitude must be between -90 and 90, got {lat}"
    if not (-180 <= lon <= 180):
        return f"Longitude must be between -180 and 180, got {lon}"
    return None


def _validate_timezone(tz_name: str) -> bool:
    """Check if the timezone string is valid."""
    return tz_name in pytz.all_timezones


@app.route("/api/config", methods=["POST"])
@login_required
def update_config():
    config = load_config()
    data = request.get_json(silent=True) or {}
    errors = []

    for key in (
        "calculation_method",
        "smartthings_token",
        "smartthings_device_id",
    ):
        if key in data:
            config[key] = data[key]

    if "calculation_method" in data:
        if data["calculation_method"] not in CALCULATION_METHODS:
            errors.append(f"Invalid calculation method: {data['calculation_method']}")

    # Per-prayer adhan audio files
    if "adhan_audio_files" in data:
        raw = data["adhan_audio_files"]
        if not isinstance(raw, dict):
            errors.append("adhan_audio_files must be an object keyed by prayer name")
        else:
            files = config.get("adhan_audio_files", {}) or {}
            for prayer, filename in raw.items():
                if prayer not in PRAYER_NAMES:
                    errors.append(f"Unknown prayer: {prayer}")
                    continue
                if not isinstance(filename, str) or not filename.endswith(".mp3"):
                    errors.append(f"Audio file for {prayer} must be a .mp3 filename")
                    continue
                if "/" in filename or "\\" in filename:
                    errors.append(f"Audio file for {prayer} must not contain path separators")
                    continue
                files[prayer] = filename
            config["adhan_audio_files"] = files

    if "volume" in data:
        config["volume"] = max(0.0, min(1.0, float(data["volume"])))

    if "skip_prayers" in data:
        config["skip_prayers"] = [
            p for p in data["skip_prayers"] if p in PRAYER_NAMES
        ]

    if "latitude" in data and "longitude" in data:
        try:
            lat = float(data["latitude"])
            lon = float(data["longitude"])
            coord_err = _validate_coordinate(lat, lon)
            if coord_err:
                errors.append(coord_err)
            else:
                config["latitude"] = lat
                config["longitude"] = lon
        except (TypeError, ValueError):
            errors.append("Latitude and longitude must be numeric")

    if "timezone" in data:
        tz_value = str(data["timezone"]).strip()
        if tz_value:
            if _validate_timezone(tz_value):
                config["timezone"] = tz_value
            else:
                errors.append(f"Invalid timezone: {tz_value}")
        # Empty string means "leave it alone" — the frontend sends "" when
        # the user hasn't filled in the timezone field.

    if "city" in data:
        city_value = str(data["city"]).strip()[:100]
        if city_value:
            config["city"] = city_value
    if "country" in data:
        country_value = str(data["country"]).strip()[:100]
        if country_value:
            config["country"] = country_value

    # Iqamah offsets
    if "iqamah_offsets" in data:
        offsets = config.get("iqamah_offsets", {})
        for prayer in PRAYER_NAMES:
            if prayer in data["iqamah_offsets"]:
                val = int(data["iqamah_offsets"][prayer])
                if val < 0 or val > 120:
                    errors.append(f"Iqamah offset for {prayer} must be 0-120 minutes")
                else:
                    offsets[prayer] = val
        config["iqamah_offsets"] = offsets

    # Iqamah notifications
    if "iqamah_enabled" in data:
        config["iqamah_enabled"] = bool(data["iqamah_enabled"])
    if "iqamah_audio_file" in data:
        config["iqamah_audio_file"] = data["iqamah_audio_file"]

    # Do Not Disturb
    if "dnd_enabled" in data:
        config["dnd_enabled"] = bool(data["dnd_enabled"])
    if "dnd_start" in data:
        if _validate_time_format(data["dnd_start"]):
            config["dnd_start"] = data["dnd_start"]
        else:
            errors.append(f"Invalid DND start time format: {data['dnd_start']} (expected HH:MM)")
    if "dnd_end" in data:
        if _validate_time_format(data["dnd_end"]):
            config["dnd_end"] = data["dnd_end"]
        else:
            errors.append(f"Invalid DND end time format: {data['dnd_end']} (expected HH:MM)")

    if errors:
        return jsonify({"status": "error", "errors": errors}), 400

    config["setup_complete"] = True
    save_config(config)
    return jsonify({"status": "ok"})


@app.route("/api/audio/validate", methods=["GET"])
@login_required
def api_audio_validate():
    """Return a list of configured audio files that are missing from disk."""
    config = load_config()
    missing = validate_audio_files(config)
    return jsonify({"missing": missing})


@app.route("/api/detect-location", methods=["POST"])
@login_required
def api_detect_location():
    loc = detect_location()
    if loc:
        config = load_config()
        config.update(loc)
        save_config(config)
        return jsonify(loc)
    return jsonify({"error": "Location detection failed"}), 500


@app.route("/api/geocode", methods=["POST"])
@login_required
def api_geocode():
    """Look up a street address or place name via OpenStreetMap Nominatim.

    Does NOT persist the result to config — the client populates the form
    fields so the user can review and click Save Settings themselves.
    """
    data = request.get_json(silent=True) or {}
    address = (data.get("address") or "").strip()
    if not address:
        return jsonify({"error": "Address is required"}), 400
    if len(address) > 200:
        return jsonify({"error": "Address is too long"}), 400
    loc = geocode_address(address)
    if loc is None:
        return jsonify({
            "error": (
                "Could not find that address. "
                "Try a simpler form like 'Austin, TX' or enter coordinates manually."
            )
        }), 404
    return jsonify(loc)


@app.route("/api/discover-speakers", methods=["POST"])
@login_required
def api_discover_speakers():
    devices = discover_chromecasts(timeout=10, use_cache=False)
    meta = get_device_metadata(devices)
    config = load_config()
    speakers = config.get("speakers", {})
    for name, info in meta.items():
        if name not in speakers:
            speakers[name] = {"enabled": True, "is_group": info["is_group"], "model": info["model"]}
        else:
            # Update metadata on re-scan
            speakers[name]["is_group"] = info["is_group"]
            speakers[name]["model"] = info["model"]
    config["speakers"] = speakers
    save_config(config)
    return jsonify({"speakers": list(speakers.keys())})


@app.route("/api/speakers", methods=["POST"])
@login_required
def api_update_speakers():
    data = request.get_json(silent=True) or {}
    config = load_config()
    speakers = config.get("speakers", {})
    for name, info in data.items():
        if name in speakers:
            speakers[name]["enabled"] = bool(info.get("enabled", True))
    config["speakers"] = speakers
    save_config(config)
    return jsonify({"status": "ok"})


@app.route("/api/test-speaker", methods=["POST"])
@login_required
def api_test_speaker():
    """Play a short test on a specific speaker."""
    import socket

    from discovery import play_on_chromecast

    data = request.get_json(silent=True) or {}
    speaker_name = data.get("speaker", "")
    config = load_config()
    # Use the Dhuhr file as the default test sample (any weekday adhan works)
    audio_files = config.get("adhan_audio_files", {}) or {}
    audio_file = audio_files.get("Dhuhr") or next(
        (audio_files[p] for p in PRAYER_NAMES if audio_files.get(p)),
        None,
    )
    if not audio_file:
        return jsonify({"error": "No adhan audio file configured"}), 400

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    web_port = os.getenv("WEB_PORT", "5000")
    media_url = f"http://{local_ip}:{web_port}/audio/{audio_file}"

    devices = discover_chromecasts(timeout=8, use_cache=False)
    if speaker_name in devices:
        ok = play_on_chromecast(
            devices[speaker_name], media_url, volume=config.get("volume", 0.5)
        )
        return jsonify({"status": "ok" if ok else "failed"})
    return jsonify({"error": f"Speaker '{speaker_name}' not found"}), 404


@app.route("/api/smartthings/devices", methods=["POST"])
@login_required
def api_smartthings_devices():
    """List SmartThings devices using the configured token."""
    config = load_config()
    token = config.get("smartthings_token", "")
    if not token:
        return jsonify({"error": "SmartThings token not configured"}), 400
    devices = st_list_devices(token)
    result = [
        {
            "device_id": d.get("deviceId", ""),
            "label": d.get("label", d.get("name", "Unknown")),
            "type": d.get("deviceTypeName", ""),
        }
        for d in devices
    ]
    return jsonify({"devices": result})


@app.route("/api/prayer-times", methods=["GET"])
@login_required
def api_prayer_times():
    config = load_config()
    times = compute_prayer_times(config)
    return jsonify({k: v.isoformat() for k, v in times.items()})


# ---------------------------------------------------------------------------
# WiFi configuration
# ---------------------------------------------------------------------------
@app.route("/api/wifi/networks", methods=["GET"])
@login_required
def api_wifi_networks():
    """List available WiFi networks via nmcli."""
    import subprocess
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list"],
            capture_output=True, text=True, timeout=15,
        )
        networks = []
        seen = set()
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            ssid, signal, security, in_use = parts[0], parts[1], parts[2], parts[3]
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            networks.append({
                "ssid": ssid,
                "signal": int(signal) if signal.isdigit() else 0,
                "security": security or "Open",
                "connected": in_use.strip() == "*",
            })
        networks.sort(key=lambda n: -n["signal"])
        return jsonify({"networks": networks})
    except FileNotFoundError:
        return jsonify({"error": "nmcli not available on this system"}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/wifi/connect", methods=["POST"])
@login_required
def api_wifi_connect():
    """Connect to a WiFi network."""
    import subprocess
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "").strip()
    if not ssid:
        return jsonify({"error": "SSID required"}), 400
    # Sanitize SSID: reject control characters and excessive length
    if len(ssid) > 32 or any(ord(c) < 32 for c in ssid):
        return jsonify({"error": "Invalid SSID"}), 400
    try:
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return jsonify({"status": "connected", "ssid": ssid})
        return jsonify({"error": result.stderr.strip() or "Connection failed"}), 500
    except FileNotFoundError:
        return jsonify({"error": "nmcli not available on this system"}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/wifi/status", methods=["GET"])
@login_required
def api_wifi_status():
    """Return current WiFi connection info."""
    import subprocess
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[1] == "wifi":
                return jsonify({
                    "device": parts[0],
                    "state": parts[2],
                    "connection": parts[3],
                })
        return jsonify({"state": "unknown"})
    except FileNotFoundError:
        # nmcli is on the host, not in this container. WiFi management from
        # the dashboard requires NetworkManager + dbus access which is not
        # wired up yet. Fall back to a friendly message; users can still
        # change networks via SSH.
        return jsonify({"error": "WiFi management not available in container. Use SSH: sudo nmcli device wifi list"}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/wifi/hotspot", methods=["POST"])
@login_required
def api_wifi_hotspot():
    """Start or stop the WiFi setup hotspot."""
    import subprocess

    data = request.get_json(silent=True) or {}
    action = data.get("action", "start")
    script = Path(__file__).resolve().parent.parent / "scripts" / "captive-portal.sh"

    if not script.exists():
        return jsonify({"error": "Captive portal script not found"}), 404

    try:
        if action == "start":
            cmd = ["bash", str(script), "hotspot"]
        elif action == "stop":
            cmd = ["bash", str(script), "stop"]
        else:
            return jsonify({"error": "Invalid action, use 'start' or 'stop'"}), 400

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return jsonify({"status": "ok", "action": action})
        return jsonify({"error": result.stderr.strip() or "Command failed"}), 500
    except FileNotFoundError:
        return jsonify({"error": "bash not available"}), 503
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Hotspot command timed out"}), 504
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# System status & maintenance
# ---------------------------------------------------------------------------
@app.route("/api/status", methods=["GET"])
@login_required
def api_status():
    """Return system health information for monitoring."""
    import datetime

    config = load_config()
    audio_missing = validate_audio_files(config)

    status = {
        "version": APP_VERSION,
        "setup_complete": config.get("setup_complete", False),
        "location_configured": config.get("latitude") is not None,
        "city": config.get("city", "Unknown"),
        "timezone": config.get("timezone", "UTC"),
        "calculation_method": config.get("calculation_method", "ISNA"),
        "speakers_count": len(config.get("speakers", {})),
        "speakers_enabled": sum(
            1 for s in config.get("speakers", {}).values() if s.get("enabled")
        ),
        "audio_files_missing": audio_missing,
        "dnd_enabled": config.get("dnd_enabled", False),
        "iqamah_enabled": config.get("iqamah_enabled", False),
        "smartthings_configured": bool(config.get("smartthings_token")),
        "server_time": datetime.datetime.now(
            pytz.timezone(config.get("timezone", "UTC"))
        ).isoformat(),
    }

    # Add next prayer info if location is configured
    if config.get("latitude") is not None:
        times = compute_prayer_times(config)
        tz = pytz.timezone(config.get("timezone", "UTC"))
        now = datetime.datetime.now(tz)
        for prayer in PRAYER_NAMES:
            pt = times.get(prayer)
            if pt and pt > now:
                delta = pt - now
                status["next_prayer"] = prayer
                status["next_prayer_time"] = pt.isoformat()
                status["next_prayer_minutes"] = int(delta.total_seconds() / 60)
                break

    return jsonify(status)


@app.route("/api/config/export", methods=["GET"])
@login_required
def api_config_export():
    """Export configuration as a downloadable JSON file (excludes secrets)."""
    config = load_config()
    # Redact sensitive fields
    safe_config = {k: v for k, v in config.items() if k != "smartthings_token"}
    if config.get("smartthings_token"):
        safe_config["smartthings_token"] = "***redacted***"
    response = jsonify(safe_config)
    response.headers["Content-Disposition"] = "attachment; filename=bilal-config.json"
    return response


@app.route("/api/config/import", methods=["POST"])
@login_required
def api_config_import():
    """Import configuration from a JSON payload (merges with current config)."""
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    # Only allow importing safe keys
    safe_keys = {
        "latitude", "longitude", "timezone", "city", "country",
        "calculation_method", "volume", "skip_prayers",
        "adhan_audio_files",
        "iqamah_offsets", "iqamah_enabled", "iqamah_audio_file",
        "dnd_enabled", "dnd_start", "dnd_end",
    }
    config = load_config()
    imported = 0
    for key in safe_keys:
        if key in data:
            config[key] = data[key]
            imported += 1

    save_config(config)
    return jsonify({"status": "ok", "keys_imported": imported})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("WEB_PORT", "5000")), debug=False)
