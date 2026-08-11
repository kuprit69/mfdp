#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

APP_NAME="LungPrometheus"
BUNDLE_DIR="dist/${APP_NAME}.app"
CONTENTS_DIR="${BUNDLE_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"

swift build -c release

rm -rf "$BUNDLE_DIR"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR/web"
mkdir -p "$RESOURCES_DIR/backend"
cp ".build/release/LungPrometheus" "$MACOS_DIR/LungPrometheus"
cp -R public/. "$RESOURCES_DIR/web/"
cp -R backend/. "$RESOURCES_DIR/backend/"
find "$RESOURCES_DIR/backend" -name "__pycache__" -type d -prune -exec rm -rf {} +
find "$RESOURCES_DIR/backend" -name "*.pyc" -type f -delete

cat > "$CONTENTS_DIR/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>LungPrometheus</string>
  <key>CFBundleIdentifier</key>
  <string>local.lungprometheus.prototype</string>
  <key>CFBundleName</key>
  <string>LungPrometheus</string>
  <key>CFBundleDisplayName</key>
  <string>LungPrometheus</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

echo "Built ${BUNDLE_DIR}"
