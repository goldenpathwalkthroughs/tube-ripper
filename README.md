# ▓▒░ TUBE-RIPPER DELUXE 2000 ░▒▓

A **local** YouTube downloader with a UI that looks like YouTube branding got
dragged through a 2003 warez keygen — chrome logo, candy-stripe progress bar,
scanlines, scrolling greetz, fake serial generator, victory chiptune. Under the
cheese it's a clean wrapper around [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
and `ffmpeg`.

Paste a URL → **ANALYZE TARGET** → pick a quality → **RIP IT**. Files land in
`./downloads/`.

- **Single videos:** two one-click presets — **HIGHEST** (top resolution, auto-merges
  best audio) and **STANDARD** (≤1080p, smaller) — plus **AUDIO ONLY · MP3**. Need a
  specific stream? The subtle *▸ advanced* link reveals the full format ladder.
- **Playlists & channels:** paste a `playlist?list=…` or `@channel` URL and it
  switches to **batch mode** — preview the video list, optionally cap how many, pick
  one quality, and **RIP ALL**. Files are foldered per playlist with track numbers.
- **Browser auto-detect:** the SESSION COOKIES menu pre-selects whatever browser you
  opened the page in, so HD/4K works without fiddling. Override it if YouTube is
  logged in to a different browser on the host machine.
- **Save location:** defaults to your macOS **Downloads** folder. Change it in the
  **SAVE TO** box (type a path or hit **📁 BROWSE…** for a native Finder picker).
  Your choice is remembered. Destinations are confined to your home folder and
  external volumes (`/Volumes`) for safety. Override the default with the
  `DOWNLOAD_DIR` env var.

## Install as a Mac app (for the whole house)

Build a self-contained, **universal** `TubeRipper.app` that runs on both Apple
Silicon and Intel Macs with **nothing pre-installed** — Python, yt-dlp and ffmpeg
are all bundled inside:

```bash
packaging/build_app.sh
```

This produces `dist/TubeRipper-macOS.zip` (~128MB). Share that zip with any Mac in
your home. On each one:

1. Unzip, drag **TubeRipper.app** to Applications.
2. Double-click **“Open Me First.command”** once (clears the macOS quarantine and
   launches it). Or right-click the app → Open → Open.

The app opens in the browser, runs entirely locally, and has a **⏻ QUIT APP**
button. It's ad-hoc signed — no Apple Developer account needed; the one-time
unblock is only because it's distributed outside the App Store.

See [packaging/INSTALL.txt](packaging/INSTALL.txt) for the end-user instructions
and [packaging/build_app.sh](packaging/build_app.sh) for how the bundle is built
(pinned versions of CPython / yt-dlp / ffmpeg are at the top of that script).

### Shipping updates

Installed apps check the `appcast.json` in this repo on launch and can install
code updates (changes to `server.py` / `index.html`) in place — one click,
auto-restart, no reinstall. Publishing is git-only (no `gh`/tokens):

```bash
packaging/release.sh 1.1.0 "What changed…"
git add -A && git commit -m "release v1.1.0" && git push
```

`release.sh` stamps the version, builds `releases/tuberipper-1.1.0.zip`, and
rewrites `appcast.json` to point at it. The push publishes it. Full details —
including the rare "major update" path for new bundled binaries — are in
[packaging/UPDATES.md](packaging/UPDATES.md).

## Or run from source

```bash
./start.sh
```

That ensures `yt-dlp` is installed (into a local `.venv` if needed), starts the
server, and opens <http://127.0.0.1:7654/> in your browser.

Already have `yt-dlp` on your PATH? You can skip the script:

```bash
python3 server.py        # then open http://127.0.0.1:7654/
```

## Requirements

- **Python 3** (system `python3` is fine)
- **ffmpeg** — `brew install ffmpeg` (already needed to merge HD video+audio and to make MP3s)
- **yt-dlp** — `start.sh` installs it for you, or `pip install yt-dlp` / `brew install yt-dlp`

## What the quality list means

- **PROGRESSIVE** — single file, video+audio already combined (usually ≤720p).
- **+AUDIO MERGE** — high-res video-only stream; the tool auto-grabs the best
  audio and merges them into an `.mp4` with ffmpeg. This is how you get 1080p/4K.
- **EXTRACT→MP3** — pulls audio only and re-encodes to MP3 (V0 quality).
- Raw audio streams (m4a/webm) are listed too if you want the original container.

## How it works

| File | Role |
|------|------|
| `server.py` | stdlib HTTP server: `/api/info`, `/api/download`, `/api/progress`, `/api/open` |
| `index.html` | the keygen-hell single-page UI (no build step, no frameworks) |
| `start.sh` | installs yt-dlp if missing, launches, opens the browser |
| `downloads/` | where ripped files land |

Everything runs on `127.0.0.1` — nothing is uploaded anywhere; the only outbound
traffic is `yt-dlp` fetching from YouTube on your machine.

## Be cool

Only download videos you own, have permission to download, or that are licensed
for it (e.g. Creative Commons). Respect YouTube's Terms of Service and your local
copyright law. This tool is for personal/offline use of content you're allowed to
keep.
