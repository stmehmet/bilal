#!/usr/bin/env bash
# -------------------------------------------------------------------
# Bilal – One-Line Installer for Raspberry Pi
#
# Usage (from a fresh Pi SSH session):
#
#   export GH_PAT=ghp_xxx              # fine-grained PAT: Contents:Read + Packages:Read
#   export TAILSCALE_AUTHKEY=tskey-... # reusable auth key tagged tag:bilal-fleet
#   curl -sSL https://raw.githubusercontent.com/stmehmet/bilal/main/scripts/install.sh | bash
#
# Or, if you've already cloned the repo manually:
#
#   cd ~/bilal && TAILSCALE_AUTHKEY=tskey-... ./scripts/install.sh
#
# Env vars:
#   GH_PAT             GitHub PAT for cloning the private repo and pulling
#                      private GHCR images. Required if repo isn't already
#                      cloned at $INSTALL_DIR.
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
    if [ -z "${GH_PAT:-}" ]; then
        err "GH_PAT not set and no existing install at $INSTALL_DIR."
        err "  The $REPO_OWNER/$REPO_NAME repo is private. Create a fine-grained PAT at"
        err "  https://github.com/settings/personal-access-tokens/new with"
        err "  'Contents: Read' and 'Packages: Read' scoped to $REPO_OWNER/$REPO_NAME,"
        err "  then: export GH_PAT=ghp_... and re-run."
        exit 1
    fi
    log "Cloning $REPO_OWNER/$REPO_NAME into $INSTALL_DIR..."
    git clone "https://${REPO_OWNER}:${GH_PAT}@github.com/${REPO_OWNER}/${REPO_NAME}.git" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# 4. Log in to GHCR so docker-compose and Watchtower can pull private images
# ---------------------------------------------------------------------------
if [ -n "${GH_PAT:-}" ]; then
    if [ ! -f "${HOME}/.docker/config.json" ] \
       || ! grep -q "ghcr.io" "${HOME}/.docker/config.json" 2>/dev/null; then
        log "Logging in to ghcr.io..."
        echo "$GH_PAT" | $DOCKER login ghcr.io -u "$REPO_OWNER" --password-stdin
    else
        log "ghcr.io login already present in ~/.docker/config.json."
    fi
else
    warn "GH_PAT not set — skipping GHCR login."
    warn "  If the package is private, docker compose pull will fail on the next step."
fi

# ---------------------------------------------------------------------------
# 5. Audio files — verify, don't block
# ---------------------------------------------------------------------------
if [ -z "$(find "$INSTALL_DIR/audio" -maxdepth 1 -name '*.mp3' -print -quit 2>/dev/null)" ]; then
    warn "No .mp3 files found in $INSTALL_DIR/audio/"
    warn "  The scheduler will start but won't have anything to play."
    warn "  scp your adhans to $INSTALL_DIR/audio/ or commit them to the repo."
fi

# ---------------------------------------------------------------------------
# 6. Generate .env with a random SECRET_KEY if missing
# ---------------------------------------------------------------------------
if [ ! -f "$INSTALL_DIR/.env" ]; then
    SECRET=$(openssl rand -hex 32)
    echo "SECRET_KEY=${SECRET}" > "$INSTALL_DIR/.env"
    log "Generated .env with a fresh SECRET_KEY."
fi

# ---------------------------------------------------------------------------
# 7. Pull images from GHCR and start the stack
# ---------------------------------------------------------------------------
log "Pulling latest images from GHCR..."
cd "$INSTALL_DIR"
$DOCKER compose pull

log "Starting bilal..."
$DOCKER compose up -d

# ---------------------------------------------------------------------------
# 8. Summary
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
