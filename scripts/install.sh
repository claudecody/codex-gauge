#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="CodexGauge"
APP_DIR="${HOME}/Applications/CodexGauge.app"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"
BIN_DIR="${HOME}/.local/bin"
LAUNCH_AGENT="${HOME}/Library/LaunchAgents/dev.claudecody.codex-gauge.plist"
CONFIG="${HOME}/.codex-gauge.json"

command -v swiftc >/dev/null 2>&1 || {
  echo "swiftc is required. Install Xcode command line tools first: xcode-select --install" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required." >&2
  exit 1
}

mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}" "${BIN_DIR}" "${HOME}/Library/LaunchAgents"

echo "Building Codex Gauge..."
swiftc -framework AppKit -framework Foundation \
  "${ROOT}/Sources/CodexGauge/main.swift" \
  -o "${MACOS_DIR}/${APP_NAME}"

cp "${ROOT}/packaging/Info.plist" "${CONTENTS_DIR}/Info.plist"
cp "${ROOT}/scripts/codex-gauge-usage.py" "${RESOURCES_DIR}/codex-gauge-usage.py"
chmod +x "${MACOS_DIR}/${APP_NAME}" "${RESOURCES_DIR}/codex-gauge-usage.py"

if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "${APP_DIR}" >/dev/null
fi

if [ ! -f "${CONFIG}" ]; then
  cat > "${CONFIG}" <<'JSON'
{
  "weekly_soft_budget_usd": 30.0,
  "weekly_max_budget_usd": 100.0
}
JSON
  echo "Created ${CONFIG}"
fi

cat > "${BIN_DIR}/codex-gauge" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
exec open "${HOME}/Applications/CodexGauge.app"
SH
chmod +x "${BIN_DIR}/codex-gauge"

cat > "${LAUNCH_AGENT}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.claudecody.codex-gauge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>${APP_DIR}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${HOME}/Library/Logs/codex-gauge.log</string>
  <key>StandardErrorPath</key>
  <string>${HOME}/Library/Logs/codex-gauge.err.log</string>
</dict>
</plist>
PLIST

domain="gui/$(id -u)"
launchctl bootout "${domain}" "${LAUNCH_AGENT}" >/dev/null 2>&1 || true
if launchctl bootstrap "${domain}" "${LAUNCH_AGENT}" >/dev/null 2>&1; then
  echo "Registered login item."
else
  echo "Warning: could not register login item. You can still launch Codex Gauge manually with: codex-gauge" >&2
fi

open "${APP_DIR}" || {
  echo "Installed, but macOS did not open the app automatically. Run: codex-gauge" >&2
}

echo "Installed Codex Gauge."
echo "Run again with: codex-gauge"
