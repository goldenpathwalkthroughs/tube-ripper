#!/bin/bash
# TUBE-RIPPER DELUXE 2000 — .app launcher.
# Runs as Contents/MacOS/TubeRipper. Picks the right arch's bundled Python and
# ffmpeg, points the server at the bundled yt-dlp, then hands off to server.py
# (which opens the browser and serves the UI).
set -e

RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
ARCH="$(uname -m)"

case "$ARCH" in
  arm64)  PYBIN="$RES/python/arm64/bin/python3";  FFDIR="$RES/bin/arm64"  ;;
  x86_64) PYBIN="$RES/python/x86_64/bin/python3"; FFDIR="$RES/bin/x86_64" ;;
  *)      PYBIN="$RES/python/arm64/bin/python3";  FFDIR="$RES/bin/arm64"  ;;
esac

export TR_APP=1
export TR_FFMPEG_DIR="$FFDIR"
export PATH="$FFDIR:$RES/bin:$PATH"

# Run the server. Exit code 42 means "an update was installed — relaunch me"
# so the freshly-swapped server.py takes effect without the user reopening.
# (`|| code=$?` keeps `set -e` from aborting on the intentional non-zero exit.)
while true; do
  code=0
  "$PYBIN" "$RES/app/server.py" --app || code=$?
  [ "$code" -eq 42 ] || break
done
