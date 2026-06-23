#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${HOME}/Applications/CodexGauge.app"
LAUNCH_AGENT="${HOME}/Library/LaunchAgents/dev.claudecody.codex-gauge.plist"
LAUNCHER="${HOME}/.local/bin/codex-gauge"
domain="gui/$(id -u)"

launchctl bootout "${domain}" "${LAUNCH_AGENT}" >/dev/null 2>&1 || true
rm -rf "${APP_DIR}"
rm -f "${LAUNCH_AGENT}" "${LAUNCHER}"

echo "Uninstalled Codex Gauge."
echo "Kept ~/.codex-gauge.json so your budget settings are not lost."
