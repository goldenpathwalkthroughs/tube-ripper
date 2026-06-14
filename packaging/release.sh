#!/usr/bin/env bash
# ===================================================================
# Cut a TUBE-RIPPER update — git-only, no gh/token needed.
#
# Most updates only change server.py / index.html, so this:
#   * stamps the new VERSION into server.py
#   * builds a small code payload zip into  releases/  (committed to the repo)
#   * writes appcast.json pointing at that payload's raw.githubusercontent URL
#
# You then just commit + push. Installed apps poll appcast.json, download the
# payload over the public raw URL, verify its SHA-256, swap it in, and restart.
#
# Usage:   packaging/release.sh <version> "<release notes>"
# Then:    git add -A && git commit -m "release vX.Y.Z" && git push
#
# (Major updates that change the bundled Python/ffmpeg can't ship this way —
#  the full .app zip is too big for git. Build it, attach it to a GitHub
#  Release, point full_url at it, and set requires_full=true. That's rare.)
# ===================================================================
set -euo pipefail

REPO="goldenpathwalkthroughs/tube-ripper"
BRANCH="main"
RAW="https://raw.githubusercontent.com/$REPO/$BRANCH"

VER="${1:-}"
NOTES="${2:-}"
[ -z "$VER" ] && { echo "usage: release.sh <version> \"notes\""; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REL="$ROOT/releases"
TODAY="$(date +%Y-%m-%d)"
mkdir -p "$REL"

# 1. stamp the version into server.py (single source of truth)
/usr/bin/sed -i '' -E "s/^VERSION = \".*\"/VERSION = \"$VER\"/" "$ROOT/server.py"
echo ">> set VERSION = $VER in server.py"

# 2. build the code payload (exactly the files the updater may overwrite)
ZIP="$REL/tuberipper-$VER.zip"
rm -f "$ZIP"
( cd "$ROOT" && zip -q -j "$ZIP" server.py index.html )
SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
echo ">> built releases/tuberipper-$VER.zip"

# 3. appcast at the repo root
cat > "$ROOT/appcast.json" <<JSON
{
  "version": "$VER",
  "published": "$TODAY",
  "notes": $(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$NOTES"),
  "code_url": "$RAW/releases/tuberipper-$VER.zip",
  "code_sha256": "$SHA",
  "requires_full": false,
  "full_url": "https://github.com/$REPO/releases/latest"
}
JSON
echo ">> wrote appcast.json (v$VER)"
echo
echo "PUBLISH IT:"
echo "  git add -A && git commit -m \"release v$VER\" && git push"
echo "Installed apps will offer v$VER on their next launch."
