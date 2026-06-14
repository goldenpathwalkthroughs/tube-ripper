#!/usr/bin/env bash
# ===================================================================
# Cut a TUBE-RIPPER update. Most updates only change server.py / index.html,
# so this builds a small "code payload" zip + an appcast.json manifest that
# installed apps poll. (Only re-run build_app.sh when the bundled binaries —
# Python / yt-dlp / ffmpeg — change; then publish a full app and set
# requires_full=true in the manifest.)
#
# Usage:
#   packaging/release.sh <version> "<release notes>"
#
# Env:
#   TR_RELEASE_BASEURL   base URL where you'll upload the payload zip
#                        (e.g. https://github.com/you/repo/releases/download/v<version>)
#
# Output (in dist/release/):
#   tuberipper-<version>.zip   ← upload this as a release asset
#   appcast.json               ← host this at your TR_UPDATE_URL
# ===================================================================
set -euo pipefail

VER="${1:-}"
NOTES="${2:-}"
[ -z "$VER" ] && { echo "usage: release.sh <version> \"notes\""; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist/release"
BASEURL="${TR_RELEASE_BASEURL:-REPLACE_WITH_DOWNLOAD_URL}"
TODAY="$(date +%Y-%m-%d)"

mkdir -p "$OUT"

# 1. stamp the version into server.py (single source of truth)
/usr/bin/sed -i '' -E "s/^VERSION = \".*\"/VERSION = \"$VER\"/" "$ROOT/server.py"
echo ">> set VERSION = $VER in server.py"

# 2. build the code payload (exactly the files the updater is allowed to swap)
ZIP="$OUT/tuberipper-$VER.zip"
rm -f "$ZIP"
( cd "$ROOT" && zip -q -j "$ZIP" server.py index.html )
echo ">> built $ZIP"

# 3. checksum
SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"

# 4. manifest
cat > "$OUT/appcast.json" <<JSON
{
  "version": "$VER",
  "published": "$TODAY",
  "notes": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$NOTES"),
  "code_url": "$BASEURL/tuberipper-$VER.zip",
  "code_sha256": "$SHA",
  "requires_full": false,
  "full_url": "$BASEURL/TubeRipper-macOS.zip"
}
JSON

echo ">> wrote $OUT/appcast.json"
echo
echo "NEXT STEPS:"
echo "  1. Upload  $ZIP  to your download host."
[ "$BASEURL" = "REPLACE_WITH_DOWNLOAD_URL" ] && \
  echo "     (set TR_RELEASE_BASEURL so code_url is filled in automatically)"
echo "  2. Publish  $OUT/appcast.json  at the URL the apps check (TR_UPDATE_URL)."
echo "  3. Installed apps will offer v$VER on their next launch."
