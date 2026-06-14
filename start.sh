#!/usr/bin/env bash
# TUBE-RIPPER DELUXE 2000 — one-shot launcher.
# Ensures yt-dlp is available, starts the local server, opens the UI.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-7654}"

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

# Ensure a stable access key exists, so we can open the browser straight to it.
if [ ! -s .access_token ]; then
  "$PY" -c "import secrets;open('.access_token','w').write(secrets.token_urlsafe(15))"
  chmod 600 .access_token 2>/dev/null || true
fi
KEY="$(cat .access_token)"

echo ">> launching backend on http://127.0.0.1:${PORT}/  (LAN access enabled, key-gated)"
( sleep 1; open "http://127.0.0.1:${PORT}/?key=${KEY}" >/dev/null 2>&1 || true ) &
exec "$PY" server.py
