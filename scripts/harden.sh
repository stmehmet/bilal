#!/usr/bin/env bash
# -------------------------------------------------------------------
# Bilal – Raspberry Pi Hardening Script
#
# Run once during initial setup to secure the Pi.
# -------------------------------------------------------------------

set -euo pipefail

log() { echo "[bilal-harden] $*"; }

# 1. Disable default 'pi' user password login (force key-based SSH)
if id pi &>/dev/null; then
    log "Locking default 'pi' user password..."
    passwd -l pi
fi

# 2. Disable password authentication for SSH
SSHD_CONFIG="/etc/ssh/sshd_config"
if [ -f "$SSHD_CONFIG" ]; then
    log "Disabling SSH password authentication..."
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG"
    sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' "$SSHD_CONFIG"
    systemctl restart sshd 2>/dev/null || true
fi

# 3. Enable automatic security updates
if command -v apt-get &>/dev/null; then
    log "Installing unattended-upgrades..."
    apt-get update -qq
    apt-get install -y -qq unattended-upgrades
    dpkg-reconfigure -plow unattended-upgrades
fi

# 4. Set up UFW firewall – allow only the web dashboard and SSH
if command -v ufw &>/dev/null; then
    log "Configuring UFW firewall..."
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp     # SSH
    ufw allow 5000/tcp   # Bilal web dashboard
    ufw --force enable
fi

log "Hardening complete."
