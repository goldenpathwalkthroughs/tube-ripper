# Shipping updates to TUBE-RIPPER

Installed apps check a small JSON **appcast** on launch, and can install
**code updates** (changes to `server.py` / `index.html`) in place — download,
SHA-256 verify, swap, auto-restart. No reinstall needed for the common case.

## One-time setup

Pick where the appcast lives and point the apps at it. Two easy options:

- **GitHub (recommended):** keep `appcast.json` in a repo and use its raw URL,
  e.g. `https://raw.githubusercontent.com/<you>/<repo>/main/appcast.json`.
  Upload payload zips as Release assets.
- **Any web host / file server** on your LAN that serves the JSON + zip.

Tell the apps the URL in any one of these (highest priority first):
1. `TR_UPDATE_URL` environment variable
2. `~/Library/Application Support/TubeRipper/update_url.txt` (one line) — set this
   per-machine *without* rebuilding
3. bake it into `DEFAULT_UPDATE_URL` in `server.py` before building the app

Use an `https://` URL in production. The payload is integrity-checked against the
`code_sha256` in the manifest, and the updater only ever overwrites `server.py`
and `index.html`.

## Cutting a normal update (features / bug fixes)

```bash
# edit server.py / index.html, then:
export TR_RELEASE_BASEURL="https://github.com/<you>/<repo>/releases/download/v1.1.0"
packaging/release.sh 1.1.0 "Added subtitle download; fixed playlist ordering."
```

This stamps `VERSION` into `server.py`, builds `dist/release/tuberipper-1.1.0.zip`,
and writes `dist/release/appcast.json`. Then:

1. Upload `tuberipper-1.1.0.zip` to your download host.
2. Publish `appcast.json` at your appcast URL.

Next time each app launches (or when someone clicks **check for updates**), it
offers v1.1.0 and installs it with one click, then restarts itself.

## Cutting a major update (new Python / yt-dlp / ffmpeg, or big changes)

When the bundled binaries change, a code swap isn't enough:

1. Bump versions in `build_app.sh` if needed, then `packaging/build_app.sh`.
2. Upload the new `dist/TubeRipper-macOS.zip` to your host.
3. In `appcast.json` set `"requires_full": true` and point `full_url` at that zip.

Apps then show a **“download new version”** prompt instead of auto-installing, and
the user replaces the app in Applications (the friendly `Open Me First.command`
ships in the zip).

## appcast.json reference

```json
{
  "version": "1.1.0",
  "published": "2026-06-20",
  "notes": "What changed…",
  "code_url": "https://…/tuberipper-1.1.0.zip",
  "code_sha256": "<sha256 of the zip>",
  "requires_full": false,
  "full_url": "https://…/TubeRipper-macOS.zip"
}
```

## How the restart works

The `.app` launcher runs the server in a loop; the updater swaps the files and
exits with code **42**, which tells the launcher to re-exec the new `server.py`.
The page polls `/api/health` and reloads once the server is back. The access key
persists in Application Support, so the URL stays valid across the restart.
