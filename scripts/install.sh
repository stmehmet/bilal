#!/usr/bin/env bash
# -------------------------------------------------------------------
# Bilal – Installer for Raspberry Pi
#
# Usage (from a fresh Pi SSH session):
#
#   # One-time preconditions on a vanilla Pi OS Lite image:
#   sudo apt update && sudo apt install -y git curl ca-certificates
#
#   # Credentials
#   export TAILSCALE_AUTHKEY=tskey-... # reusable auth key tagged tag:bilal-fleet
#
#   # Clone the repo and run this script
#   git clone https://github.com/stmehmet/bilal.git ~/bilal
#   cd ~/bilal && ./scripts/install.sh
#
# Env vars:
#   TAILSCALE_AUTHKEY  Tailscale reusable auth key. Optional but strongly
#                      recommended for gifted units — without it you have no
#                      way to SSH in remotely.
#   INSTALL_DIR        Where to install bilal (default: $HOME/bilal).
# -------------------------------------------------------------------

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-${HOME}/bilal}"
REPO_OWNER="stmehmet"
REPO_NAME="bilal"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"

log()  { echo "[bilal-install] $*"; }
warn() { echo "[bilal-install] WARNING: $*" >&2; }
err()  { echo "[bilal-install] ERROR: $*" >&2; }

# ---------------------------------------------------------------------------
# 1. Docker
# ---------------------------------------------------------------------------
if ! command -v docker &>/dev/null; then
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    log "Docker installed. You may need to log out and back in for group changes."
fi

if ! docker compose version &>/dev/null 2>&1; then
    log "Installing Docker Compose plugin..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-compose-plugin
fi

# Use sudo for docker commands if the current session isn't in the docker
# group yet (usermod takes effect only after re-login).
if docker ps &>/dev/null; then
    DOCKER=docker
else
    DOCKER="sudo docker"
fi

# ---------------------------------------------------------------------------
# 2. Tailscale — install and join the tailnet so the maintainer can SSH in
#    remotely without port-forwarding on the recipient's router.
# ---------------------------------------------------------------------------
if [ -n "${TAILSCALE_AUTHKEY:-}" ]; then
    if ! command -v tailscale &>/dev/null; then
        log "Installing Tailscale..."
        curl -fsSL https://tailscale.com/install.sh | sh
    fi

    if sudo tailscale status &>/dev/null; then
        log "Tailscale already joined — skipping up."
    else
        MACHINE_ID=$(head -c 8 /etc/machine-id 2>/dev/null || true)
        TAILSCALE_HOSTNAME="bilal-${MACHINE_ID:-$(hostname)}"
        log "Joining Tailscale as ${TAILSCALE_HOSTNAME}..."
        sudo tailscale up \
            --authkey="$TAILSCALE_AUTHKEY" \
            --hostname="$TAILSCALE_HOSTNAME" \
            --ssh
        log "Tailscale joined. SSH via Tailscale is enabled."
    fi
else
    warn "TAILSCALE_AUTHKEY not set — skipping Tailscale bootstrap."
    warn "  For gifted units this means no remote SSH. Set the env var and re-run to enable."
fi

# ---------------------------------------------------------------------------
# 3. Clone (or update) the repo
# ---------------------------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
    log "Updating existing installation at $INSTALL_DIR..."
    cd "$INSTALL_DIR"
    git pull --ff-only
else
    log "Cloning $REPO_OWNER/$REPO_NAME into $INSTALL_DIR..."
    git clone "${REPO_URL}" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# 4. Audio files — verify, don't block
# ---------------------------------------------------------------------------
# (GHCR login is no longer needed — the repo and packages are public.)
if [ -z "$(find "$INSTALL_DIR/audio" -maxdepth 1 -name '*.mp3' -print -quit 2>/dev/null)" ]; then
    warn "No .mp3 files found in $INSTALL_DIR/audio/"
    warn "  The scheduler will start but won't have anything to play."
    warn "  scp your adhans to $INSTALL_DIR/audio/ or commit them to the repo."
fi

# ---------------------------------------------------------------------------
# 5. Generate .env with a random SECRET_KEY if missing
# ---------------------------------------------------------------------------
if [ ! -f "$INSTALL_DIR/.env" ]; then
    SECRET=$(openssl rand -hex 32)
    echo "SECRET_KEY=${SECRET}" > "$INSTALL_DIR/.env"
    log "Generated .env with a fresh SECRET_KEY."
fi

# ---------------------------------------------------------------------------
# 6. Pull images from GHCR and start the stack
# ---------------------------------------------------------------------------
log "Pulling latest images from GHCR..."
cd "$INSTALL_DIR"
$DOCKER compose pull

log "Starting bilal..."
$DOCKER compose up -d

# ---------------------------------------------------------------------------
# 7. Summary
# ---------------------------------------------------------------------------
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$LAN_IP" ]; then
    LAN_IP=$(ip route get 1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
fi

log ""
log "=========================================="
log "  Bilal is running!"
log "  LAN:       http://${LAN_IP:-<pi-ip>}:5000"
if command -v tailscale &>/dev/null && sudo tailscale status &>/dev/null; then
    TS_IP=$(sudo tailscale ip -4 2>/dev/null | head -n1 || true)
    TS_NAME=$(sudo tailscale status --json 2>/dev/null | grep -o '"HostName": *"[^"]*"' | head -n1 | sed 's/.*"\([^"]*\)"$/\1/' || true)
    [ -n "$TS_IP" ] && log "  Tailscale: http://${TS_IP}:5000"
    [ -n "$TS_NAME" ] && log "  MagicDNS:  http://${TS_NAME}:5000"
fi
log "=========================================="
