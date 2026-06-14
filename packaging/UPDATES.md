# Shipping updates to TUBE-RIPPER

Installed apps check an **appcast** (`appcast.json` in this repo, served over its
public raw URL) on launch. For the common case — changes to `server.py` /
`index.html` — they download a small payload, SHA-256 verify it, swap it in, and
restart. No reinstall, and **no `gh` / tokens / Release uploads** — just git.

The apps are already pointed at:
`https://raw.githubusercontent.com/goldenpathwalkthroughs/tube-ripper/main/appcast.json`
(baked into `DEFAULT_UPDATE_URL` in `server.py`).

## Cutting a normal update (features / bug fixes)

```bash
# edit server.py / index.html, then:
packaging/release.sh 1.1.0 "Added subtitle download; fixed playlist ordering."
git add -A && git commit -m "release v1.1.0" && git push
```

`release.sh` stamps `VERSION` into `server.py`, builds
`releases/tuberipper-1.1.0.zip`, and writes `appcast.json` pointing at that
payload's raw URL. The commit + push publishes it. Next time each app launches
(or someone clicks **check for updates**), it offers v1.1.0 and installs it with
one click, then restarts itself.

That's the whole loop: **edit → `release.sh` → commit → push.**

## YouTube broke but you didn't change anything

Don't release. Tell people to click **refresh engine** in the footer — it runs
`pip install -U yt-dlp` into the app and fixes most site breakage in seconds.

## Major update (new bundled Python / ffmpeg)

A code swap can't replace binaries, and the 128 MB app zip is too big for git:

1. Bump versions in `build_app.sh` if needed → `packaging/build_app.sh`.
2. Create a GitHub Release and attach `dist/TubeRipper-macOS.zip` to it
   (web drag-and-drop, or `gh release create` if you log in).
3. In `appcast.json` set `"requires_full": true` and point `full_url` at it.

Apps then show a **“download new version”** prompt instead of auto-installing,
and people re-drag the app to Applications (the `Open Me First.command` ships in
the zip). This is rare.

## appcast.json reference

```json
{
  "version": "1.1.0",
  "published": "2026-06-20",
  "notes": "What changed…",
  "code_url": "https://raw.githubusercontent.com/.../releases/tuberipper-1.1.0.zip",
  "code_sha256": "<sha256 of the zip>",
  "requires_full": false,
  "full_url": "https://github.com/goldenpathwalkthroughs/tube-ripper/releases/latest"
}
```

## How the restart works

The `.app` launcher runs the server in a loop; the updater swaps the files and
exits with code **42**, which tells the launcher to re-exec the new `server.py`.
The page polls `/api/health` and reloads once the server is back. The access key
persists in Application Support, so the URL stays valid across the restart.
