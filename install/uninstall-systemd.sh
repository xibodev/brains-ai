#!/usr/bin/env bash
# uninstall-systemd.sh — disable + remove the brains.service user unit.
# Code, data, and logs are NOT touched.

set -euo pipefail

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/brains.service"

systemctl --user disable --now brains.service 2>/dev/null || true

if [[ -f "$UNIT_PATH" ]]; then
    rm -f "$UNIT_PATH"
    systemctl --user daemon-reload
    echo "Removed $UNIT_PATH"
else
    echo "No unit at $UNIT_PATH; nothing to remove."
fi
