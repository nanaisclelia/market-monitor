#!/bin/zsh
# 安装 launchd 定时任务：每 10 分钟调用一次 `run --auto`，由程序按交易所日历判断是否到点。
# 用法：./scripts/install_launchd.sh        卸载：./scripts/install_launchd.sh uninstall
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.findash.auto"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$1" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"; echo "已卸载 $LABEL"; exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$DIR/.venv/bin/python</string><string>-m</string><string>src.run</string><string>--auto</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安装：$PLIST"
