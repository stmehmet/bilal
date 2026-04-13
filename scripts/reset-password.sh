#!/usr/bin/env bash
# -------------------------------------------------------------------
# Bilal – Password Reset
#
# Deletes the dashboard password so the next browser visit prompts
# for a new one. Run this via Tailscale SSH when locked out:
#
#   tailscale ssh bilal@bilal-<hostname>
#   ~/bilal/scripts/reset-password.sh
#
# -------------------------------------------------------------------

set -euo pipefail

CONFIG_DIR="${CONFIG_DIR:-/data}"
AUTH_FILE="${CONFIG_DIR}/auth.json"

# Try inside the container first (where /data lives)
if docker exec bilal-web rm -f "${AUTH_FILE}" 2>/dev/null; then
    echo "Password reset. Open the dashboard to set a new one."
    exit 0
fi

# Fallback: direct file access via docker volume
VOLUME_PATH=$(docker volume inspect bilal-data --format '{{ .Mountpoint }}' 2>/dev/null || true)
if [ -n "$VOLUME_PATH" ] && [ -f "${VOLUME_PATH}/auth.json" ]; then
    sudo rm -f "${VOLUME_PATH}/auth.json"
    echo "Password reset. Open the dashboard to set a new one."
    exit 0
fi

echo "Could not find auth.json. Is bilal running?"
exit 1
