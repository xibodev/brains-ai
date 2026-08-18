#!/usr/bin/env bash
# install-launchd.sh — install the brains supervisor as a macOS launchd user agent.
#
#   ./install/install-launchd.sh
#
# Honors $BRAINS_STATE_DIR and $PYTHON_BIN if set in the calling environment.
#
# Prereq: install the package first (recommended: pipx)
#   pipx install .
# This puts `brains` on PATH inside an isolated venv.
# If `brains` isn't on PATH, the agent falls back to
# `${PYTHON_BIN} -m brains.control.supervisor`.

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="$(cd "$INSTALL_DIR/.." && pwd)"
PYTHON="${PYTHON_BIN:-python3}"

if BRAINS_BIN="$(command -v brains 2>/dev/null)"; then
    PROGRAM_ARGS_XML="<string>${BRAINS_BIN}</string>
        <string>serve-all</string>"
    echo "Using installed entry point: ${BRAINS_BIN} serve-all"
else
    PROGRAM_ARGS_XML="<string>${PYTHON}</string>
        <string>-m</string>
        <string>brains.control.supervisor</string>"
    echo "brains not on PATH; falling back to '${PYTHON} -m brains.control.supervisor'."
    echo "Consider 'pipx install ${CODE_DIR}' for a cleaner install."
fi

LABEL="com.brains.service"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
mkdir -p "$(dirname "$PLIST_PATH")"

# Resolve the log dir up front. If BRAINS_STATE_DIR is unset, the supervisor
# itself will fall back to ~/.brains; we mirror that here so launchd has a
# writable StandardOutPath/StandardErrorPath.
LOG_HOME="${BRAINS_STATE_DIR:-$HOME/.brains}"
mkdir -p "$LOG_HOME/sessions"

ENV_BLOCK=""
if [[ -n "${BRAINS_STATE_DIR:-}" ]]; then
    ENV_BLOCK=$(cat <<ENV
    <key>EnvironmentVariables</key>
    <dict>
        <key>BRAINS_STATE_DIR</key>
        <string>${BRAINS_STATE_DIR}</string>
    </dict>
ENV
)
fi

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        ${PROGRAM_ARGS_XML}
    </array>
    <key>WorkingDirectory</key>
    <string>${CODE_DIR}</string>
${ENV_BLOCK}
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_HOME}/sessions/service.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_HOME}/sessions/service.stderr.log</string>
</dict>
</plist>
PLIST

echo "Wrote $PLIST_PATH"

# Reload if already loaded
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo
echo "Status:  launchctl print gui/$(id -u)/${LABEL}"
echo "Stop:    launchctl bootout gui/$(id -u)/${LABEL}"
echo "Logs:    tail -f ${LOG_HOME}/sessions/service.log"
