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
from geolocation import detect_location  # noqa: E402
from smartthings import list_devices as st_list_devices  # noqa: E402
from adhan_scheduler import compute_prayer_times, compute_iqamah_times, validate_audio_files  # noqa: E402

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(32).hex())

AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "/audio"))
AUTH_FILE = Path(os.getenv("CONFIG_DIR", "/data")) / "auth.json"

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

    audio_files = []
    if AUDIO_DIR.exists():
        audio_files = sorted(
            f.name for f in AUDIO_DIR.iterdir() if f.suffix == ".mp3"
        )

    return render_template(
        "dashboard.html",
        config=config,
        prayer_times=times,
        iqamah_times=iqamah_times,
        prayer_names=PRAYER_NAMES,
        methods=CALCULATION_METHODS,
        audio_files=audio_files,
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
        "adhan_file",
        "fajr_adhan_file",
        "smartthings_token",
        "smartthings_device_id",
    ):
        if key in data:
            config[key] = data[key]

    if "calculation_method" in data:
        if data["calculation_method"] not in CALCULATION_METHODS:
            errors.append(f"Invalid calculation method: {data['calculation_method']}")

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
        if _validate_timezone(data["timezone"]):
            config["timezone"] = data["timezone"]
        else:
            errors.append(f"Invalid timezone: {data['timezone']}")

    if "city" in data:
        config["city"] = str(data["city"])[:100]
    if "country" in data:
        config["country"] = str(data["country"])[:100]

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


@app.route("/api/discover-speakers", methods=["POST"])
@login_required
def api_discover_speakers():
    devices = discover_chromecasts(timeout=10)
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
    audio_file = config.get("adhan_file", "adhan_makkah.mp3")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    web_port = os.getenv("WEB_PORT", "5000")
    media_url = f"http://{local_ip}:{web_port}/audio/{audio_file}"

    devices = discover_chromecasts(timeout=8)
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
        "adhan_file", "fajr_adhan_file",
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
