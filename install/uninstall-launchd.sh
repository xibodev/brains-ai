#!/usr/bin/env bash
# uninstall-launchd.sh — unload + remove the brains launchd agent.
# Code, data, and logs are NOT touched.

set -euo pipefail

LABEL="com.brains.service"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true

if [[ -f "$PLIST_PATH" ]]; then
    rm -f "$PLIST_PATH"
    echo "Removed $PLIST_PATH"
else
    echo "No plist at $PLIST_PATH; nothing to remove."
fi
