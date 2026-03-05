"""Bilal – Home Adhan System Web Dashboard."""

import json
import os
import sys
from pathlib import Path

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

# Allow importing from the scheduler package
sys.path.insert(0, os.getenv("SCHEDULER_PATH", "/app/scheduler"))

from config import (  # noqa: E402
    CALCULATION_METHODS,
    PRAYER_NAMES,
    load_config,
    save_config,
)
from discovery import discover_chromecasts  # noqa: E402
from geolocation import detect_location  # noqa: E402
from adhan_scheduler import compute_prayer_times  # noqa: E402

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


@app.route("/login", methods=["GET", "POST"])
def login():
    auth = _load_auth()
    # If no password set yet, redirect to initial setup
    if not auth.get("password_hash"):
        if request.method == "POST":
            pw = request.form.get("password", "")
            if len(pw) < 6:
                flash("Password must be at least 6 characters.", "error")
                return render_template("login.html", first_time=True)
            _save_auth({"password_hash": generate_password_hash(pw)})
            login_user(User())
            return redirect(url_for("dashboard"))
        return render_template("login.html", first_time=True)

    if request.method == "POST":
        pw = request.form.get("password", "")
        if check_password_hash(auth["password_hash"], pw):
            login_user(User())
            return redirect(url_for("dashboard"))
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
    return send_from_directory(str(AUDIO_DIR), filename)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    config = load_config()
    times = {}
    if config.get("latitude") is not None:
        times = compute_prayer_times(config)
        times = {k: v.strftime("%I:%M %p") for k, v in times.items()}

    audio_files = []
    if AUDIO_DIR.exists():
        audio_files = sorted(
            f.name for f in AUDIO_DIR.iterdir() if f.suffix == ".mp3"
        )

    return render_template(
        "dashboard.html",
        config=config,
        prayer_times=times,
        prayer_names=PRAYER_NAMES,
        methods=CALCULATION_METHODS,
        audio_files=audio_files,
    )


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
@login_required
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
@login_required
def update_config():
    config = load_config()
    data = request.get_json(silent=True) or {}

    for key in (
        "calculation_method",
        "adhan_file",
        "fajr_adhan_file",
        "smartthings_token",
        "smartthings_device_id",
    ):
        if key in data:
            config[key] = data[key]

    if "volume" in data:
        config["volume"] = max(0.0, min(1.0, float(data["volume"])))

    if "skip_prayers" in data:
        config["skip_prayers"] = [
            p for p in data["skip_prayers"] if p in PRAYER_NAMES
        ]

    if "latitude" in data and "longitude" in data:
        config["latitude"] = float(data["latitude"])
        config["longitude"] = float(data["longitude"])

    if "timezone" in data:
        config["timezone"] = data["timezone"]
    if "city" in data:
        config["city"] = data["city"]
    if "country" in data:
        config["country"] = data["country"]

    config["setup_complete"] = True
    save_config(config)
    return jsonify({"status": "ok"})


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
    config = load_config()
    speakers = config.get("speakers", {})
    for name in devices:
        if name not in speakers:
            speakers[name] = {"enabled": True}
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


@app.route("/api/prayer-times", methods=["GET"])
@login_required
def api_prayer_times():
    config = load_config()
    times = compute_prayer_times(config)
    return jsonify({k: v.isoformat() for k, v in times.items()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("WEB_PORT", "5000")), debug=False)
