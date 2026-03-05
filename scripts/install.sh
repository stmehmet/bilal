#!/usr/bin/env bash
# -------------------------------------------------------------------
# Bilal – One-Line Installer for Raspberry Pi
#
# Usage:  curl -sSL https://raw.githubusercontent.com/<user>/bilal/main/scripts/install.sh | bash
# -------------------------------------------------------------------

set -euo pipefail

INSTALL_DIR="${HOME}/bilal"
REPO_URL="https://github.com/<your-username>/bilal.git"

log() { echo "[bilal-install] $*"; }

# 1. Install Docker if not present
if ! command -v docker &>/dev/null; then
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    log "Docker installed. You may need to log out and back in for group changes."
fi

# 2. Install Docker Compose plugin if not present
if ! docker compose version &>/dev/null; then
    log "Installing Docker Compose plugin..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-compose-plugin
fi

# 3. Clone the repository
if [ -d "$INSTALL_DIR" ]; then
    log "Updating existing installation..."
    cd "$INSTALL_DIR" && git pull
else
    log "Cloning Bilal..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 4. Create audio directory and add placeholder files
mkdir -p "$INSTALL_DIR/audio"
if [ ! -f "$INSTALL_DIR/audio/adhan_makkah.mp3" ]; then
    log "NOTE: Place your .mp3 adhan files in $INSTALL_DIR/audio/"
    log "  Expected files: adhan_makkah.mp3, adhan_fajr.mp3"
fi

# 5. Generate a random secret key
if [ ! -f "$INSTALL_DIR/.env" ]; then
    SECRET=$(openssl rand -hex 32)
    echo "SECRET_KEY=${SECRET}" > "$INSTALL_DIR/.env"
    log "Generated .env with SECRET_KEY"
fi

# 6. Build and start
log "Building and starting Bilal..."
cd "$INSTALL_DIR"
docker compose up -d --build

log ""
log "=========================================="
log "  Bilal is running!"
log "  Dashboard: http://$(hostname -I | awk '{print $1}'):5000"
log "=========================================="
