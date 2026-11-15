#!/usr/bin/env bash
# Ship the repo to the target as a new release and install it: scripts/deploy.sh [install.sh args]
# The service keeps running the previous release unless install.sh succeeds.
set -euo pipefail
source "$(dirname "$0")/_remote.sh"
APP="${SCRAPE_APP_DIR:-apps/scrape-api}"
check_path "$APP"
RELEASE="$APP/releases/$(date -u +%Y%m%d-%H%M%S)"

ship "$RELEASE"
remote "cd $(q "$RELEASE") && ./deploy/install.sh $(quote_args "$@")"
# Keep the release in service, whatever its name, and the newest other one.
remote 'live="$(basename "$(systemctl show -p WorkingDirectory --value scrape-api)")"
cd '"$(q "$APP/releases")"' && ls -1 | sort -r | grep -vxF "$live" | tail -n +2 | xargs -r rm -rf --'
