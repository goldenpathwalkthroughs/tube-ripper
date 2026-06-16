#!/bin/bash
# Double-click this ONCE on a new Mac. It clears the "downloaded from the
# internet" quarantine flag from TubeRipper (so macOS lets it run — including
# the Python bundled inside it) and launches it. Works whether the app is still
# next to this file or already in your Applications folder.
cd "$(dirname "$0")"

echo "Setting up TUBE-RIPPER DELUXE 2000…"

# Find the app: prefer a copy next to this file; otherwise /Applications.
APP=""
if [ -d "TubeRipper.app" ]; then
  APP="$(pwd)/TubeRipper.app"
elif [ -d "/Applications/TubeRipper.app" ]; then
  APP="/Applications/TubeRipper.app"
fi

if [ -z "$APP" ]; then
  echo "!! Couldn't find TubeRipper.app."
  echo "   Keep it next to this file, or drag it to your Applications folder, then run this again."
  echo "Press Return to close."; read _; exit 1
fi

# If it's sitting here next to us, move it to Applications for them.
if [ "$APP" = "$(pwd)/TubeRipper.app" ] && [ ! -d "/Applications/TubeRipper.app" ]; then
  echo "Moving TubeRipper to Applications…"
  if cp -R "TubeRipper.app" /Applications/ 2>/dev/null; then
    APP="/Applications/TubeRipper.app"
  fi
fi

echo "Unblocking $APP …"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo "Launching…"
open "$APP"
echo "Done — you can close this window. (Open TUBE-RIPPER from Applications next time.)"
