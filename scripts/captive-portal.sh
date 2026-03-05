#!/usr/bin/env bash
# -------------------------------------------------------------------
# Bilal Captive Portal – WiFi Setup via Access Point
#
# On first boot (or when no known WiFi is available), this script
# creates a hotspot named "Bilal-Setup" so the user can connect
# from their phone and enter WiFi credentials via the web dashboard.
#
# Dependencies: NetworkManager (nmcli)
# -------------------------------------------------------------------

set -euo pipefail

AP_NAME="Bilal-Setup"
AP_PASSWORD="bilal1234"          # Minimum WPA2 password for the setup AP
AP_INTERFACE="${AP_INTERFACE:-wlan0}"
TIMEOUT_SECONDS=30

log() { echo "[bilal-captive] $(date '+%H:%M:%S') $*"; }

has_wifi_connection() {
    nmcli -t -f TYPE,STATE connection show --active 2>/dev/null \
        | grep -q "^802-11-wireless:activated"
}

wait_for_wifi() {
    log "Waiting ${TIMEOUT_SECONDS}s for an existing WiFi connection..."
    for i in $(seq 1 "$TIMEOUT_SECONDS"); do
        if has_wifi_connection; then
            log "WiFi connected."
            return 0
        fi
        sleep 1
    done
    return 1
}

start_hotspot() {
    log "No WiFi found. Starting Access Point '${AP_NAME}'..."

    # Remove any previous AP connection of the same name
    nmcli connection delete "$AP_NAME" 2>/dev/null || true

    nmcli device wifi hotspot \
        ifname "$AP_INTERFACE" \
        ssid "$AP_NAME" \
        password "$AP_PASSWORD"

    log "Hotspot active. SSID='${AP_NAME}' Password='${AP_PASSWORD}'"
    log "Users can connect and open http://192.168.4.1:5000 to configure WiFi."
}

stop_hotspot() {
    log "Stopping hotspot..."
    nmcli connection down "$AP_NAME" 2>/dev/null || true
    nmcli connection delete "$AP_NAME" 2>/dev/null || true
}

configure_wifi() {
    # Called from the web UI after user enters SSID/password
    local ssid="$1"
    local password="$2"

    log "Configuring WiFi for SSID='${ssid}'..."
    stop_hotspot

    nmcli device wifi connect "$ssid" password "$password" ifname "$AP_INTERFACE"

    if has_wifi_connection; then
        log "Successfully connected to '${ssid}'."
        return 0
    else
        log "Failed to connect to '${ssid}'. Restarting hotspot..."
        start_hotspot
        return 1
    fi
}

# --- Main ---
case "${1:-auto}" in
    auto)
        if ! wait_for_wifi; then
            start_hotspot
        fi
        ;;
    hotspot)
        start_hotspot
        ;;
    stop)
        stop_hotspot
        ;;
    connect)
        configure_wifi "${2:-}" "${3:-}"
        ;;
    *)
        echo "Usage: $0 {auto|hotspot|stop|connect <ssid> <password>}"
        exit 1
        ;;
esac
