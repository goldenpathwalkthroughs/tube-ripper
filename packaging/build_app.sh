#!/usr/bin/env bash
# ===================================================================
# Build TUBE-RIPPER DELUXE 2000 as a self-contained macOS .app.
#
# Bundles (no install needed on the target Mac): relocatable CPython 3.13,
# yt-dlp (pip module), ffmpeg (static) — for the chosen architecture(s).
# Ad-hoc signed (no Apple Developer account); receiving Macs do a one-time
# unblock via "Open Me First.command".
#
# Usage:
#   packaging/build_app.sh                 # universal (Apple Silicon + Intel)
#   packaging/build_app.sh intel           # Intel only  (x86_64)
#   packaging/build_app.sh apple           # Apple Silicon only (arm64)
#
# Output: dist/TubeRipper.app  and  dist/TubeRipper[-Intel|-AppleSilicon]-macOS.zip
# ===================================================================
set -euo pipefail

# ---- pinned versions (bump as needed) ----
PBS_TAG="20260610"            # python-build-standalone release tag
PY_VER="3.13.14"             # cpython version within that release
FF_TAG="b6.1.1"              # eugeneware/ffmpeg-static release tag

# ---- target architecture(s) ----
case "${1:-universal}" in
  universal|"") ARCHS="arm64 x86_64"; VARIANT=""            ; LABEL="universal (Apple Silicon + Intel)";;
  intel|x86_64) ARCHS="x86_64";       VARIANT="-Intel"      ; LABEL="Intel (x86_64)";;
  apple|arm64)  ARCHS="arm64";        VARIANT="-AppleSilicon"; LABEL="Apple Silicon (arm64)";;
  *) echo "usage: build_app.sh [universal|intel|apple]"; exit 1;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/packaging"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
APP="$DIST/TubeRipper.app"
CACHE="$BUILD/cache"
RES="$APP/Contents/Resources"
MACOS="$APP/Contents/MacOS"
ZIP="$DIST/TubeRipper${VARIANT}-macOS.zip"

PBS_BASE="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG"
FF_BASE="https://github.com/eugeneware/ffmpeg-static/releases/download/$FF_TAG"
HOST_ARCH="$(uname -m)"

say(){ printf "\033[36m>> %s\033[0m\n" "$*"; }
fetch(){ local url="$1" dest="$2"; if [ -f "$dest" ]; then say "cached $(basename "$dest")"; return; fi
         say "downloading $(basename "$dest")"; curl -fL --retry 3 -o "$dest" "$url"; }

# map our arch name -> python-build-standalone triple part / ffmpeg asset name
pbs_src(){ [ "$1" = arm64 ] && echo aarch64 || echo x86_64; }
ff_name(){ [ "$1" = arm64 ] && echo darwin-arm64 || echo darwin-x64; }

say "build target: $LABEL"
say "clean"
rm -rf "$APP"
mkdir -p "$CACHE" "$RES/app" "$RES/python" "$MACOS"

# ---- 1. Python + yt-dlp (per selected arch) ----
for dst in $ARCHS; do
  src="$(pbs_src "$dst")"
  tarball="$CACHE/python-$dst.tar.gz"
  fetch "$PBS_BASE/cpython-$PY_VER+$PBS_TAG-$src-apple-darwin-install_only.tar.gz" "$tarball"
  say "extract python/$dst"
  rm -rf "$RES/python/$dst"; mkdir -p "$RES/python/$dst"
  tmp="$(mktemp -d)"; tar xzf "$tarball" -C "$tmp"; mv "$tmp/python/"* "$RES/python/$dst/"; rm -rf "$tmp"
  say "install yt-dlp into python/$dst"
  PREFIX=""; [ "$dst" != "$HOST_ARCH" ] && PREFIX="arch -$dst"   # Rosetta for the other arch
  $PREFIX "$RES/python/$dst/bin/python3" -m pip install --quiet \
      --no-warn-script-location --upgrade pip yt-dlp
done

# ---- 2. ffmpeg (per selected arch) ----
for dst in $ARCHS; do
  mkdir -p "$RES/bin/$dst"
  name="$(ff_name "$dst")"
  fetch "$FF_BASE/ffmpeg-$name.gz" "$CACHE/ffmpeg-$dst.gz"
  say "install ffmpeg/$dst"
  gunzip -c "$CACHE/ffmpeg-$dst.gz" > "$RES/bin/$dst/ffmpeg"
  chmod +x "$RES/bin/$dst/ffmpeg"
done

# ---- 3. icons (generate if missing and Pillow is available) ----
if [ ! -f "$PKG/app.icns" ] && [ -x "$ROOT/.venv/bin/python3" ]; then
  say "generating app icon"
  "$ROOT/.venv/bin/python3" "$PKG/make_icon.py" || say "icon generation skipped"
fi

# ---- 4. app code + plist + icons ----
say "copy app code"
cp "$ROOT/server.py" "$ROOT/index.html" "$RES/app/"
for f in favicon.svg apple-touch-icon.png; do
  [ -f "$ROOT/$f" ] && cp "$ROOT/$f" "$RES/app/$f" || true
done
cp "$PKG/Info.plist" "$APP/Contents/Info.plist"
[ -f "$PKG/app.icns" ] && cp "$PKG/app.icns" "$RES/app.icns" || true

# ---- 5. native launcher (selected arch[es]) ----
say "compile native launcher: $ARCHS"
SLICES=()
for dst in $ARCHS; do
  xcrun swiftc -O -target "$dst-apple-macos11" "$PKG/TubeRipperApp.swift" \
      -framework Cocoa -o "$BUILD/tr-$dst"
  SLICES+=("$BUILD/tr-$dst")
done
lipo -create "${SLICES[@]}" -o "$MACOS/TubeRipper"
chmod +x "$MACOS/TubeRipper"
say "main executable arch: $(lipo -archs "$MACOS/TubeRipper")"

# ---- 6. ad-hoc codesign ----
if command -v codesign >/dev/null 2>&1; then
  say "ad-hoc codesign"
  codesign --force --deep --sign - "$APP" 2>/dev/null || say "codesign skipped (non-fatal)"
fi

# ---- 7. distribution folder + zip ----
say "assemble distribution folder"
STAGE="$DIST/TubeRipper"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp "$PKG/INSTALL.txt" "$STAGE/INSTALL.txt"
cp "$PKG/Open Me First.command" "$STAGE/Open Me First.command"
chmod +x "$STAGE/Open Me First.command"

say "zip"
rm -f "$ZIP"
( cd "$DIST" && ditto -c -k --sequesterRsrc --keepParent "TubeRipper" "$(basename "$ZIP")" )

echo
say "DONE → $APP  ($(du -sh "$APP" | cut -f1))   [$LABEL]"
say "      → $ZIP  ($(du -sh "$ZIP" | cut -f1))  ← share this"
