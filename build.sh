#!/bin/bash
# Builds the Mac app. Usage: ./build.sh [--install]
set -euo pipefail
cd "$(dirname "$0")"
# Your Apple developer team ID, from $DEVELOPMENT_TEAM or a one-line .team file.
TEAM="${DEVELOPMENT_TEAM:-$(cat .team 2>/dev/null || true)}"
[[ -n "$TEAM" ]] || { echo "Set DEVELOPMENT_TEAM or put your team ID in .team" >&2; exit 1; }
xcodegen generate --quiet
xcodebuild -project LocalControl.xcodeproj -scheme LocalControl -configuration Release \
  -derivedDataPath .build-mac DEVELOPMENT_TEAM="$TEAM" -allowProvisioningUpdates -quiet build
BUILT=.build-mac/Build/Products/Release/LocalControl.app
echo "Built $(pwd)/$BUILT"
if [[ "${1:-}" == "--install" ]]; then
  APP="/Applications/LocalControl.app"
  # A running copy refuses to quit while a rule is locked in; replace the bundle underneath it then.
  osascript -e 'quit app "LocalControl"' 2>/dev/null || true
  rm -rf "$APP"
  cp -R "$BUILT" "$APP"
  for _ in 1 2 3 4 5; do open "$APP" 2>/dev/null && break; sleep 1; done
  echo "Installed to $APP and launched"
fi
