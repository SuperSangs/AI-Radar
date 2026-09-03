#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
APP_NAME="AIRadarDesktop"
BUILD_DIR="$ROOT/build"
APP="$BUILD_DIR/$APP_NAME.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

clang "$ROOT/AIRadarDesktop.m" \
  -o "$APP/Contents/MacOS/$APP_NAME" \
  -fobjc-arc \
  -framework AppKit \
  -framework ServiceManagement \
  -framework WebKit \
  -framework Foundation \
  -O

cp "$ROOT/Info.plist" "$APP/Contents/Info.plist"

# Sign the finished bundle, not only the Mach-O produced by clang. LaunchServices
# rejects a linker-signed executable once Info.plist has been added to the bundle.
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict "$APP"

echo "Built $APP"
