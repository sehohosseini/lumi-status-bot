#!/bin/bash
# Install the LUMI status bot as a macOS background job (launchd) that checks every 5 min.
#   ./install_macos.sh             install (runs Telegram setup first if needed)
#   ./install_macos.sh uninstall   stop and remove the background job (keeps config)
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_HOME="$HOME/.lumi-status-bot"
LABEL="local.lumi-status-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="${PYTHON:-/usr/bin/python3}"
INTERVAL="${INTERVAL:-300}"   # seconds between checks

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

if [ "${1:-}" = "uninstall" ]; then
  rm -f "$PLIST"
  echo "Background job removed. Config and logs are still in $BOT_HOME (delete it to remove everything)."
  exit 0
fi

# Run from a copy outside OneDrive so launchd never needs access to cloud-synced files.
mkdir -p "$BOT_HOME" && chmod 700 "$BOT_HOME"
cp "$SRC_DIR/lumi_status_bot.py" "$BOT_HOME/"

if [ ! -f "$BOT_HOME/config.json" ]; then
  "$PY" "$BOT_HOME/lumi_status_bot.py" setup
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$BOT_HOME/lumi_status_bot.py</string>
    <string>check</string>
  </array>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$BOT_HOME/launchd.err</string>
</dict>
</plist>
EOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. LUMI is checked every $((INTERVAL / 60)) min; log: $BOT_HOME/bot.log"
