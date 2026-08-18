#!/usr/bin/env bash
# install-systemd.sh — install the brains supervisor as a systemd user unit.
#
# Run from the brains-ai code dir or from anywhere; the script resolves
# the code dir from its own location.
#
#   ./install/install-systemd.sh
#
# Honors $BRAINS_STATE_DIR and $PYTHON_BIN if set in the calling environment.
#
# Prereq: install the package first (recommended: pipx)
#   pipx install .
# This puts `brains` on PATH inside an isolated venv.
# If `brains` isn't on PATH, the unit falls back to
# `${PYTHON_BIN} -m brains.control.supervisor`.

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="$(cd "$INSTALL_DIR/.." && pwd)"
PYTHON="${PYTHON_BIN:-python3}"

if BRAINS_BIN="$(command -v brains 2>/dev/null)"; then
    EXEC_LINE="${BRAINS_BIN} serve-all"
    echo "Using installed entry point: ${BRAINS_BIN} serve-all"
else
    EXEC_LINE="${PYTHON} -m brains.control.supervisor"
    echo "brains not on PATH; falling back to '${EXEC_LINE}'."
    echo "Consider 'pipx install ${CODE_DIR}' for a cleaner install."
fi

# Build the Environment= line if BRAINS_STATE_DIR was set
ENV_LINE=""
if [[ -n "${BRAINS_STATE_DIR:-}" ]]; then
    ENV_LINE="Environment=BRAINS_STATE_DIR=${BRAINS_STATE_DIR}"
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT_PATH="$UNIT_DIR/brains.service"

cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=brains supervisor (gateway + dashboard)
After=network.target

[Service]
Type=simple
ExecStart=${EXEC_LINE}
WorkingDirectory=${CODE_DIR}
${ENV_LINE}
Restart=on-failure
RestartSec=5
# StandardOutput/StandardError default to journal under user-systemd;
# the supervisor also writes to <BRAINS_STATE_DIR>/sessions/service.log.

[Install]
WantedBy=default.target
UNIT

echo "Wrote $UNIT_PATH"
systemctl --user daemon-reload
systemctl --user enable --now brains.service
echo
echo "Status:    systemctl --user status brains.service"
echo "Live log:  journalctl --user -u brains.service -f"
echo "Stop:      systemctl --user stop brains.service"
echo "Disable:   systemctl --user disable brains.service"
echo
echo "Tip: to keep the service running while you are logged out,"
echo "     enable lingering:  loginctl enable-linger \"\$USER\""
