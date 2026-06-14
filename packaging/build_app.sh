#!/usr/bin/env bash
# ===================================================================
# Build TUBE-RIPPER DELUXE 2000 as a self-contained, universal macOS .app.
#
# Bundles (no install needed on the target Mac):
#   * relocatable CPython 3.13  (arm64 + x86_64)
#   * yt-dlp                    (universal binary)
#   * ffmpeg                    (arm64 + x86_64 static, selected at runtime)
#
# Runs on both Apple Silicon and Intel. Ad-hoc signed (no Apple Developer
# account required) — receiving Macs just need a one-time right-click → Open.
#
# Usage:  packaging/build_app.sh
# Output: dist/TubeRipper.app  and  dist/TubeRipper-macOS.zip
# ===================================================================
set -euo pipefail

# ---- pinned versions (bump as needed) ----
PBS_TAG="20260610"            # python-build-standalone release tag
PY_VER="3.13.14"             # cpython version within that release
FF_TAG="b6.1.1"              # eugeneware/ffmpeg-static release tag

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/packaging"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
APP="$DIST/TubeRipper.app"
CACHE="$BUILD/cache"
RES="$APP/Contents/Resources"
MACOS="$APP/Contents/MacOS"

PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG"
FF_BASE="https://github.com/eugeneware/ffmpeg-static/releases/download/$FF_TAG"

say(){ printf "\033[36m>> %s\033[0m\n" "$*"; }

fetch(){ # url dest
  local url="$1" dest="$2"
  if [ -f "$dest" ]; then say "cached $(basename "$dest")"; return; fi
  say "downloading $(basename "$dest")"
  curl -fL --retry 3 -o "$dest" "$url"
}

say "clean"
rm -rf "$APP"
mkdir -p "$CACHE" "$RES/app" "$RES/bin/arm64" "$RES/bin/x86_64" \
         "$RES/python" "$MACOS"

HOST_ARCH="$(uname -m)"

# ---- 1. Python (both arches) + yt-dlp installed into each ----
# yt-dlp goes in as a pip module (pure Python) rather than the onefile
# yt-dlp_macos binary, which re-extracts itself on every run (~8s startup).
for pair in "aarch64:arm64" "x86_64:x86_64"; do
  src="${pair%%:*}"; dst="${pair##*:}"
  tarball="$CACHE/python-$dst.tar.gz"
  fetch "$PBS_BASE/cpython-$PY_VER+$PBS_TAG-$src-apple-darwin-install_only.tar.gz" "$tarball"
  say "extract python/$dst"
  rm -rf "$RES/python/$dst"; mkdir -p "$RES/python/$dst"
  tmp="$(mktemp -d)"
  tar xzf "$tarball" -C "$tmp"
  mv "$tmp/python/"* "$RES/python/$dst/"
  rm -rf "$tmp"

  say "install yt-dlp into python/$dst"
  PREFIX=""
  [ "$dst" != "$HOST_ARCH" ] && PREFIX="arch -$dst"   # Rosetta for the other arch
  $PREFIX "$RES/python/$dst/bin/python3" -m pip install --quiet \
      --no-warn-script-location --upgrade pip yt-dlp
done

# ---- 2. ffmpeg (per arch) ----
for pair in "arm64:darwin-arm64" "x86_64:darwin-x64"; do
  dst="${pair%%:*}"; name="${pair##*:}"
  fetch "$FF_BASE/ffmpeg-$name.gz" "$CACHE/ffmpeg-$dst.gz"
  say "install ffmpeg/$dst"
  gunzip -c "$CACHE/ffmpeg-$dst.gz" > "$RES/bin/$dst/ffmpeg"
  chmod +x "$RES/bin/$dst/ffmpeg"
done

# ---- 4. icons (generate if missing and Pillow is available) ----
if [ ! -f "$PKG/app.icns" ] && [ -x "$ROOT/.venv/bin/python3" ]; then
  say "generating app icon"
  "$ROOT/.venv/bin/python3" "$PKG/make_icon.py" || say "icon generation skipped"
fi

# ---- 5. app code + native launcher (universal2) + plist + icons ----
say "copy app code"
cp "$ROOT/server.py" "$ROOT/index.html" "$RES/app/"
for f in favicon.svg apple-touch-icon.png; do
  [ -f "$ROOT/$f" ] && cp "$ROOT/$f" "$RES/app/$f" || true
done
cp "$PKG/Info.plist" "$APP/Contents/Info.plist"
[ -f "$PKG/app.icns" ] && cp "$PKG/app.icns" "$RES/app.icns" || true

say "compile native launcher (universal2: arm64 + x86_64)"
xcrun swiftc -O -target arm64-apple-macos11   "$PKG/TubeRipperApp.swift" \
    -framework Cocoa -o "$BUILD/tr-arm64"
xcrun swiftc -O -target x86_64-apple-macos11  "$PKG/TubeRipperApp.swift" \
    -framework Cocoa -o "$BUILD/tr-x86_64"
lipo -create "$BUILD/tr-arm64" "$BUILD/tr-x86_64" -o "$MACOS/TubeRipper"
chmod +x "$MACOS/TubeRipper"
say "main executable arch: $(lipo -archs "$MACOS/TubeRipper")"

# ---- 6. ad-hoc codesign (helps Gatekeeper; no Developer ID needed) ----
if command -v codesign >/dev/null 2>&1; then
  say "ad-hoc codesign"
  codesign --force --deep --sign - "$APP" 2>/dev/null || say "codesign skipped (non-fatal)"
fi

# ---- 7. assemble a friendly distribution folder + zip it ----
say "assemble distribution folder"
STAGE="$DIST/TubeRipper"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp "$PKG/INSTALL.txt" "$STAGE/INSTALL.txt"
cp "$PKG/Open Me First.command" "$STAGE/Open Me First.command"
chmod +x "$STAGE/Open Me First.command"

say "zip (ditto preserves perms + symlinks)"
rm -f "$DIST/TubeRipper-macOS.zip"
( cd "$DIST" && ditto -c -k --sequesterRsrc --keepParent "TubeRipper" "TubeRipper-macOS.zip" )

SIZE="$(du -sh "$APP" | cut -f1)"
ZSIZE="$(du -sh "$DIST/TubeRipper-macOS.zip" | cut -f1)"
echo
say "DONE → $APP  ($SIZE)"
say "      → $DIST/TubeRipper-macOS.zip  ($ZSIZE)  ← share this"
