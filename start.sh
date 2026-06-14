#!/usr/bin/env bash
# TUBE-RIPPER DELUXE 2000 — one-shot launcher.
# Ensures yt-dlp is available, starts the local server, opens the UI.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-1337}"

# 1. ffmpeg (needed to merge video+audio and to make MP3s)
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "!! ffmpeg not found. Install it:  brew install ffmpeg"
fi

# 2. yt-dlp — prefer a real binary; otherwise install into a local venv.
if ! command -v yt-dlp >/dev/null 2>&1 && ! python3 -c "import yt_dlp" >/dev/null 2>&1; then
  echo ">> yt-dlp not found — installing into ./.venv ..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip yt-dlp
  PY="./.venv/bin/python3"
elif [ -x "./.venv/bin/python3" ]; then
  PY="./.venv/bin/python3"
else
  PY="python3"
fi

echo ">> launching backend on http://localhost:${PORT}/  (LAN access enabled, key-gated)"
( sleep 1; open "http://localhost:${PORT}/" >/dev/null 2>&1 || true ) &
exec "$PY" server.py
