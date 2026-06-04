#!/usr/bin/env bash
# -------------------------------------------------------------------
# Bilal – Watchtower-independent self-update backstop.
#
# Watchtower auto-pulls new *images*, but it can die — and it can never update
# the compose file itself (a compose change, like swapping the Watchtower image,
# can't be delivered by Watchtower). When that happens a unit freezes silently.
# This script, run on a schedule by bilal-update.timer, recovers it: fast-forward
# the repo, pull images, recreate the stack. Idempotent; safe to run anytime.
# -------------------------------------------------------------------
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$INSTALL_DIR"

log() { echo "[bilal-self-update] $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"; }

DOCKER=docker
docker ps &>/dev/null || DOCKER="sudo docker"

log "Updating $INSTALL_DIR"
# A diverged/dirty checkout shouldn't abort the image refresh — log and carry on
# with whatever is currently checked out.
git pull --ff-only || log "git pull --ff-only failed; continuing with current checkout"
$DOCKER compose pull
$DOCKER compose up -d
log "Self-update complete"
