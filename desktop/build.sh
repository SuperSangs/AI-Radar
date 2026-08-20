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
  -framework WebKit \
  -framework Foundation \
  -O

cp "$ROOT/Info.plist" "$APP/Contents/Info.plist"

echo "Built $APP"
