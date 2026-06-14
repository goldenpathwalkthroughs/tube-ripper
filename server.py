#!/usr/bin/env python3
"""
TUBE-RIPPER DELUXE 2000 — local backend.

A tiny dependency-free (stdlib only) HTTP server that wraps `yt-dlp` and the
system `ffmpeg` to:
  * fetch every available quality/format for a YouTube URL  (/api/info)
  * download a chosen format, auto-merging video+audio or extracting MP3,
    while streaming live progress                            (/api/download + /api/progress)

The front-end (index.html) is pure static HTML/CSS/JS — this server just
serves it and exposes the JSON API.

Nothing here talks to any service except YouTube (via yt-dlp) on the user's
own machine. Files land in ./downloads.
"""

import hashlib
import ipaddress
import json
import os
import py_compile
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "1.0.0"

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.realpath(os.path.expanduser("~"))

# App mode: set by the .app launcher. Changes where we persist state (the app
# bundle itself is read-only once installed in /Applications) and makes the
# server open a browser on launch.
APP_MODE = os.environ.get("TR_APP") == "1"
if APP_MODE:
    SUPPORT_DIR = os.path.join(HOME, "Library", "Application Support", "TubeRipper")
    try:
        os.makedirs(SUPPORT_DIR, exist_ok=True)
    except OSError:
        SUPPORT_DIR = HERE
else:
    SUPPORT_DIR = HERE

HTTPD = None  # set in main(); used by /api/quit

# Wrapped: launched inside the native Swift window (WKWebView), not a browser.
WRAPPED = os.environ.get("TR_WRAPPED") == "1"

# Default destination: the macOS Downloads folder (override with DOWNLOAD_DIR).
DEFAULT_DEST = os.environ.get("DOWNLOAD_DIR") or os.path.join(HOME, "Downloads")

# Destinations are confined to these roots so a LAN client can't write to
# arbitrary system paths. Home covers ~/Downloads, ~/Movies, etc.; /Volumes
# covers external drives.
ALLOWED_DEST_ROOTS = [HOME, "/Volumes"]


def resolve_dest(dest):
    """Validate & normalise a destination folder. Raises ValueError if it
    falls outside the allowed roots or isn't writable."""
    d = (dest or "").strip()
    d = os.path.expanduser(d) if d else DEFAULT_DEST
    d = os.path.realpath(os.path.abspath(d))
    if not any(d == r or d.startswith(r + os.sep) for r in ALLOWED_DEST_ROOTS):
        raise ValueError("Folder must be inside your home folder or an external volume (/Volumes).")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Could not create that folder: {e}")
    if not os.access(d, os.W_OK):
        raise ValueError("That folder isn't writable.")
    return d


def pick_folder():
    """Pop a native macOS folder picker; returns chosen POSIX path or ''."""
    script = ('set f to choose folder with prompt '
            '"Choose where TUBE-RIPPER saves downloads"\n'
            'return POSIX path of f')
    try:
        p = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True, timeout=120)
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        pass
    return ""  # user cancelled or not on macOS


# Ensure the default exists at boot (best-effort).
try:
    os.makedirs(DEFAULT_DEST, exist_ok=True)
except OSError:
    DEFAULT_DEST = os.path.join(HERE, "downloads")
    os.makedirs(DEFAULT_DEST, exist_ok=True)

# --------------------------------------------------------------------------- #
#  Self-update channel
#
#  The app checks a small JSON "appcast" for a newer version, then downloads a
#  code payload (server.py + index.html), verifies its SHA-256, and swaps it in
#  place — then restarts (the .app launcher re-execs on exit code 42).
#
#  Set the appcast URL once via the TR_UPDATE_URL env var or a one-line file at
#  ~/Library/Application Support/TubeRipper/update_url.txt, or bake it into
#  DEFAULT_UPDATE_URL below before building. Use an https:// URL in production.
# --------------------------------------------------------------------------- #
DEFAULT_UPDATE_URL = "https://raw.githubusercontent.com/goldenpathwalkthroughs/tube-ripper/main/appcast.json"
RESTART_EXIT_CODE = 42
UPDATE_FILES = ("server.py", "index.html")   # only these are ever overwritten


def update_url():
    env = os.environ.get("TR_UPDATE_URL")
    if env:
        return env.strip()
    cfg = os.path.join(SUPPORT_DIR, "update_url.txt")
    try:
        with open(cfg) as fh:
            u = fh.read().strip()
            if u:
                return u
    except OSError:
        pass
    return DEFAULT_UPDATE_URL


def _semver(v):
    parts = []
    for chunk in str(v).split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts + [0] * (3 - len(parts)))[:3]


def _http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "TubeRipper/" + VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def check_update():
    """Fetch the appcast and report whether a newer version exists."""
    url = update_url()
    if not url:
        return {"current": VERSION, "configured": False,
                "available": False, "error": "No update URL configured."}
    try:
        manifest = json.loads(_http_get(url))
    except Exception as e:
        return {"current": VERSION, "configured": True,
                "available": False, "error": f"Couldn't reach update server: {e}"}

    latest = str(manifest.get("version", "0"))
    available = _semver(latest) > _semver(VERSION)
    kind = "full" if manifest.get("requires_full") else "code"
    return {
        "current": VERSION,
        "configured": True,
        "available": available,
        "latest": latest,
        "notes": manifest.get("notes", ""),
        "published": manifest.get("published", ""),
        "kind": kind,
        "full_url": manifest.get("full_url", ""),
        "_manifest": manifest,
    }


def apply_code_update():
    """Download, verify, and swap in a code-only update. Returns (ok, message).
    On success the caller should restart (exit RESTART_EXIT_CODE)."""
    info = check_update()
    if info.get("error"):
        return False, info["error"]
    if not info.get("available"):
        return False, "Already up to date."
    if info.get("kind") == "full":
        return False, "This is a major update — download the new app instead."

    m = info["_manifest"]
    code_url = m.get("code_url")
    want_hash = (m.get("code_sha256") or "").lower()
    if not code_url or not want_hash:
        return False, "Update manifest is missing code_url / code_sha256."

    try:
        blob = _http_get(code_url, timeout=120)
    except Exception as e:
        return False, f"Download failed: {e}"

    got_hash = hashlib.sha256(blob).hexdigest()
    if got_hash != want_hash:
        return False, "Checksum mismatch — refusing to install (download may be corrupt or tampered)."

    tmp = tempfile.mkdtemp(prefix="tr_update_")
    try:
        zpath = os.path.join(tmp, "payload.zip")
        with open(zpath, "wb") as fh:
            fh.write(blob)
        staged = {}
        with zipfile.ZipFile(zpath) as zf:
            names = set(zf.namelist())
            for fname in UPDATE_FILES:
                if fname not in names:
                    return False, f"Update payload is missing {fname}."
                out = os.path.join(tmp, fname)
                with open(out, "wb") as fh:
                    fh.write(zf.read(fname))
                staged[fname] = out

        # validate the new server.py compiles before we trust it
        try:
            py_compile.compile(staged["server.py"], doraise=True)
        except py_compile.PyCompileError as e:
            return False, f"New server.py failed to compile; aborting. ({e})"

        # back up current files, then atomically swap
        backup = os.path.join(SUPPORT_DIR, "backup", VERSION)
        os.makedirs(backup, exist_ok=True)
        for fname in UPDATE_FILES:
            cur = os.path.join(HERE, fname)
            if os.path.exists(cur):
                shutil.copy2(cur, os.path.join(backup, fname))
            dst_tmp = cur + ".new"
            shutil.copy2(staged[fname], dst_tmp)
            os.replace(dst_tmp, cur)   # atomic
        return True, f"Updated to v{info['latest']}. Restarting…"
    except PermissionError:
        return False, ("Couldn't write the update — the app folder isn't writable. "
                    "Reinstall by downloading the latest app instead.")
    except Exception as e:
        return False, f"Update failed: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# --------------------------------------------------------------------------- #
#  Access control — two independent gates so this is safe on a LAN:
#    1. the source IP must be loopback / private (RFC1918) / CGNAT-Tailscale.
#       The public internet is rejected outright, even if a router ever
#       forwarded the port.
#    2. a secret key (?key= or X-Access-Token header) must match. The key is
#       generated once and stored in .access_token next to this file.
# --------------------------------------------------------------------------- #
TOKEN_FILE = os.path.join(SUPPORT_DIR, ".access_token")


def load_token():
    env = os.environ.get("ACCESS_TOKEN")
    if env:
        return env.strip()
    try:
        with open(TOKEN_FILE) as fh:
            t = fh.read().strip()
            if t:
                return t
    except FileNotFoundError:
        pass
    t = secrets.token_urlsafe(15)
    try:
        with open(TOKEN_FILE, "w") as fh:
            fh.write(t)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return t


ACCESS_TOKEN = load_token()

# Source ranges allowed to reach the server. Loopback + the three RFC1918
# private blocks + link-local + the 100.64/10 CGNAT block (Tailscale / VPNs).
_ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10",
    "100.64.0.0/10",                       # CGNAT / Tailscale
    "fc00::/7",                            # IPv6 unique-local
)]


def ip_allowed(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _ALLOWED_NETS)


def lan_ip():
    """Best-effort primary LAN IP for printing a reachable URL (no traffic sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# job_id -> {"status","percent","speed","eta","line","file","title","error"}
JOBS = {}
JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
#  yt-dlp helpers
# --------------------------------------------------------------------------- #
def ytdlp_cmd():
    """Return the invocation prefix for yt-dlp.

    Prefers `<this python> -m yt_dlp` when the module is importable — under the
    bundled standalone Python this starts in well under a second, versus ~8s for
    the onefile yt-dlp_macos binary (which re-extracts itself on every run).
    Falls back to a bundled/PATH binary."""
    try:
        import importlib.util
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass
    bundled = os.environ.get("TR_YTDLP")
    if bundled and os.path.exists(bundled):
        return [bundled]
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "yt_dlp"]


def ffmpeg_dir():
    """Directory holding the bundled ffmpeg, if any (TR_FFMPEG_DIR)."""
    return os.environ.get("TR_FFMPEG_DIR") or ""


# Tool availability doesn't change during a run — probe once, then cache.
_TOOL_CACHE = {}


def have_ffmpeg():
    if "ffmpeg" not in _TOOL_CACHE:
        d = ffmpeg_dir()
        _TOOL_CACHE["ffmpeg"] = bool(
            shutil.which("ffmpeg") or (d and os.path.exists(os.path.join(d, "ffmpeg"))))
    return _TOOL_CACHE["ffmpeg"]


def _probe_ytdlp():
    try:
        out = subprocess.run(ytdlp_cmd() + ["--version"],
                            capture_output=True, text=True, timeout=20)
        if out.returncode == 0:
            _TOOL_CACHE["ytdlp"] = True
            _TOOL_CACHE["ytdlp_version"] = out.stdout.strip()
            return
    except Exception:
        pass
    _TOOL_CACHE["ytdlp"] = False
    _TOOL_CACHE["ytdlp_version"] = ""


def have_ytdlp():
    if "ytdlp" not in _TOOL_CACHE:
        _probe_ytdlp()
    return _TOOL_CACHE["ytdlp"]


def ytdlp_version():
    if "ytdlp_version" not in _TOOL_CACHE:
        _probe_ytdlp()
    return _TOOL_CACHE.get("ytdlp_version", "")


def refresh_ytdlp():
    """`pip install -U yt-dlp` into the running (bundled) Python, so YouTube
    breakage can be fixed without re-releasing the whole app."""
    cmd = [sys.executable, "-m", "pip", "install", "-U",
        "--no-warn-script-location", "--disable-pip-version-check", "yt-dlp"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:
        return {"ok": False, "error": f"Couldn't run pip: {e}"}
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        msg = tail[-1] if tail else "pip failed."
        if "Permission" in msg or "Read-only" in msg or "denied" in msg:
            msg = ("Couldn't write the update — the app folder isn't writable. "
                "Move TubeRipper to Applications (so it's owned by you), or reinstall the latest app.")
        return {"ok": False, "error": msg}
    _TOOL_CACHE.pop("ytdlp", None)
    _TOOL_CACHE.pop("ytdlp_version", None)
    return {"ok": True, "version": ytdlp_version()}


ALLOWED_BROWSERS = {"safari", "chrome", "brave", "edge", "firefox", "chromium", "vivaldi", "opera"}


def cookie_args(browser):
    """Return yt-dlp cookie flags for a user-selected browser, or [].

    This is opt-in: the user explicitly picks their browser in the UI so yt-dlp
    can reuse their existing logged-in YouTube session. Without it, YouTube now
    forces SABR streaming and only exposes a single low-res legacy format.
    """
    if browser and browser.lower() in ALLOWED_BROWSERS:
        return ["--cookies-from-browser", browser.lower()]
    return []


def human_size(n):
    if not n:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    n = float(n)
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.1f}{units[i]}"


# --------------------------------------------------------------------------- #
#  Platform registry
#
#  Downloading is generic (yt-dlp supports hundreds of sites), so adding a
#  platform is mostly: recognise its hostnames, set a support level, and — when
#  ready — teach classify_url any batch URL shapes (profiles, etc.). The UI and
#  FAQ read `support` so a future update can flip a platform from "experimental"
#  to "full" without code surgery.
#
#  support: "full"         = tested + first-class
#           "experimental" = works via yt-dlp but not hardened/QA'd here yet
#           "planned"      = recognised, not enabled
# --------------------------------------------------------------------------- #
PLATFORMS = [
    {"key": "youtube", "name": "YouTube", "support": "full",
    "hosts": ["youtube.com", "youtu.be", "youtube-nocookie.com", "m.youtube.com"]},
    {"key": "tiktok", "name": "TikTok", "support": "experimental",
    "hosts": ["tiktok.com", "vm.tiktok.com", "vt.tiktok.com"]},
    {"key": "instagram", "name": "Instagram", "support": "experimental",
    "hosts": ["instagram.com", "instagr.am", "ddinstagram.com"]},
    {"key": "facebook", "name": "Facebook", "support": "experimental",
    "hosts": ["facebook.com", "fb.watch", "fb.com", "m.facebook.com"]},
]


def detect_platform(url):
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    for p in PLATFORMS:
        if any(host == h or host.endswith("." + h) for h in p["hosts"]):
            return p
    return {"key": "other", "name": "this site", "support": "experimental", "hosts": []}


def classify_url(url):
    """Decide whether a URL is a single video, a playlist, or a channel.

    YouTube gets full playlist/channel (batch) detection. Other platforms are
    treated as single items for now — yt-dlp still downloads them, but batch
    URL shapes (e.g. an Instagram profile, a TikTok user) are a later update.

    A bare watch URL (even with &list=) counts as a single video — pasting a
    video should download that video.
    """
    p = urlparse(url)
    path = p.path.lower()
    q = parse_qs(p.query)

    if detect_platform(url)["key"] != "youtube":
        return "video"

    if "/playlist" in path and q.get("list"):
        return "playlist"
    if (path.startswith("/@") or "/channel/" in path or "/c/" in path
            or "/user/" in path or path.rstrip("/").endswith(("/videos", "/streams", "/shorts"))):
        return "channel"
    if q.get("v") or "/watch" in path or "/shorts/" in path or "youtu.be" in (p.netloc or ""):
        return "video"
    # list= with no video id -> treat as playlist
    if q.get("list"):
        return "playlist"
    return "video"


# How many entries to enumerate when previewing a playlist/channel.
PREVIEW_CAP = 200


def fetch_playlist(url, browser=None):
    """Flat-list a playlist/channel: fast, no per-video extraction."""
    proc = subprocess.run(
        ytdlp_cmd() + ["-J", "--flat-playlist", "--playlist-end", str(PREVIEW_CAP)]
        + cookie_args(browser) + [url],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        raise RuntimeError(err[-1] if err else "yt-dlp could not read that list.")
    data = json.loads(proc.stdout)
    entries = [e for e in (data.get("entries") or []) if e]
    rows = [{"title": e.get("title") or "(untitled)",
            "id": e.get("id"),
            "duration_h": _fmt_dur(e.get("duration"))} for e in entries[:PREVIEW_CAP]]
    total = data.get("playlist_count") or len(entries)
    return {
        "type": "playlist",
        "title": data.get("title") or "playlist",
        "uploader": data.get("uploader") or data.get("channel") or "unknown",
        "count": total,
        "capped": len(entries) >= PREVIEW_CAP,
        "entries": rows,
    }


def fetch_info(url, browser=None):
    """Dispatch to playlist or single-video analysis."""
    plat = detect_platform(url)
    kind = classify_url(url)
    if kind in ("playlist", "channel"):
        info = fetch_playlist(url, browser)
        info["kind"] = kind
    else:
        info = fetch_video(url, browser)
        # The "only low-res came back, add cookies" hint is YouTube's SABR
        # behaviour; a single progressive file is normal elsewhere (TikTok etc.).
        if plat["key"] != "youtube":
            info["limited"] = False
    info["platform"] = plat["name"]
    info["platform_key"] = plat["key"]
    info["support"] = plat["support"]
    return info


def fetch_video(url, browser=None):
    """Run yt-dlp -J for one video and shape selectable formats + presets."""
    proc = subprocess.run(
        ytdlp_cmd() + ["-J", "--no-playlist"] + cookie_args(browser) + [url],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        msg = err[-1] if err else "yt-dlp failed to read that target."
        raise RuntimeError(msg)

    data = json.loads(proc.stdout)
    raw = data.get("formats", []) or []

    video = []   # video-bearing formats (progressive or video-only)
    audio = []   # audio-only formats

    for f in raw:
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        fid = f.get("format_id")
        if not fid:
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        ext = f.get("ext") or "?"
        if vcodec != "none":
            height = f.get("height") or 0
            fps = f.get("fps")
            note = f.get("format_note") or (f"{height}p" if height else ext)
            video.append({
                "id": fid,
                "kind": "video",
                "height": height,
                "fps": fps,
                "ext": ext,
                "vcodec": (vcodec.split(".")[0]),
                "acodec": ("none" if acodec == "none" else acodec.split(".")[0]),
                "progressive": acodec != "none",
                "size": size,
                "size_h": human_size(size),
                "label": note,
                "tbr": f.get("tbr") or 0,
            })
        elif acodec != "none":
            abr = f.get("abr") or f.get("tbr") or 0
            audio.append({
                "id": fid,
                "kind": "audio",
                "ext": ext,
                "acodec": acodec.split(".")[0],
                "abr": abr,
                "size": size,
                "size_h": human_size(size),
                "label": f"{int(abr)}kbps {ext}" if abr else ext,
            })

    # Best video per (height,fps), preferring larger bitrate.
    best = {}
    for v in video:
        key = (v["height"], v["fps"] or 0)
        if key not in best or (v["tbr"] or 0) > (best[key]["tbr"] or 0):
            best[key] = v
    video_rows = sorted(best.values(),
                        key=lambda v: (v["height"], v["fps"] or 0),
                        reverse=True)

    audio_rows = sorted(audio, key=lambda a: a["abr"] or 0, reverse=True)

    # When YouTube only coughs up the single legacy progressive format, the
    # user almost certainly needs to supply session cookies to see HD/4K.
    limited = len(video_rows) <= 1 and len(audio_rows) == 0

    # Two friendly presets. HIGHEST = top of the ladder. STANDARD = the best
    # mainstream rung at or below 1080p (and below highest where possible).
    presets = {}
    if video_rows:
        presets["highest"] = _preset_row(video_rows[0])
        std = next((v for v in video_rows
                    if v["height"] and v["height"] <= 1080
                    and v["height"] < video_rows[0]["height"]), None)
        if not std:
            std = next((v for v in video_rows if v["height"] and v["height"] <= 720), None)
        if std and std is not video_rows[0]:
            presets["standard"] = _preset_row(std)

    return {
        "type": "video",
        "title": data.get("title") or "untitled",
        "uploader": data.get("uploader") or data.get("channel") or "unknown",
        "duration": data.get("duration"),
        "duration_h": _fmt_dur(data.get("duration")),
        "thumbnail": data.get("thumbnail"),
        "view_count": data.get("view_count"),
        "video": video_rows,
        "audio": audio_rows,
        "presets": presets,
        "limited": limited,
    }


def _preset_row(v):
    fps = f" {round(v['fps'])}fps" if v.get("fps") else ""
    return {"label": f"{v['label']}{fps}", "height": v["height"],
            "size_h": v["size_h"], "ext": v["ext"]}


def _fmt_dur(secs):
    if not secs:
        return "??:??"
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
#  Download worker
# --------------------------------------------------------------------------- #
PCT_RE = re.compile(r"(\d{1,3}\.\d)%")
SPEED_RE = re.compile(r"at\s+([\d.]+\w+/s)")
ETA_RE = re.compile(r"ETA\s+([\d:]+)")
ITEM_RE = re.compile(r"Downloading (?:item|video) (\d+) of (\d+)")


def quality_args(quality):
    """Map a preset token to a generic yt-dlp format selector that works for
    any video (single or across a whole playlist)."""
    if quality == "audio":
        return ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if quality == "standard":
        return ["-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
                "--merge-output-format", "mp4"]
    # highest
    return ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]


def run_download(job_id, url, browser, sel):
    batch = bool(sel.get("batch"))
    dest = sel.get("dest") or DEFAULT_DEST
    plat_name = detect_platform(url)["name"]
    cmd = ytdlp_cmd() + ["--newline", "--no-part", "--ignore-errors"] + cookie_args(browser)
    if ffmpeg_dir():
        cmd += ["--ffmpeg-location", ffmpeg_dir()]

    if batch:
        cmd += ["--yes-playlist"]
        limit = sel.get("limit")
        if limit:
            cmd += ["--playlist-end", str(int(limit))]
        out = os.path.join(dest,
                          "%(playlist_title).60s/%(playlist_index)03d - %(title).70s [%(id)s].%(ext)s")
    else:
        cmd += ["--no-playlist"]
        out = os.path.join(dest, "%(title).80s [%(id)s].%(ext)s")
    cmd += ["-o", out]

    quality = sel.get("quality")
    if quality:
        cmd += quality_args(quality)
    elif sel.get("mp3"):
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        if sel.get("format_id"):
            cmd += ["-f", sel["format_id"]]
    elif sel.get("kind") == "audio":
        cmd += ["-f", sel.get("format_id", "bestaudio")]
    else:
        fmt_id = sel.get("format_id", "")
        cmd += ["-f", f"{fmt_id}+bestaudio/{fmt_id}/best",
                "--merge-output-format", "mp4"]

    cmd.append(url)
    _set(job_id, status="running", line="Spawning yt-dlp...", percent=0,
        batch=batch, item=0, total=0, done_count=0)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception as e:
        _set(job_id, status="error", error=str(e))
        return

    final_file = None
    cur_item, total, done_count = 0, 0, 0
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        update = {"line": line}

        it = ITEM_RE.search(line)
        if it:
            cur_item, total = int(it.group(1)), int(it.group(2))
            update["item"] = cur_item
            update["total"] = total

        m = PCT_RE.search(line)
        if m:
            file_pct = float(m.group(1))
            update["file_percent"] = file_pct
            if batch and total:
                update["percent"] = round(((cur_item - 1) + file_pct / 100.0) / total * 100, 1)
            else:
                update["percent"] = file_pct
        s = SPEED_RE.search(line)
        if s:
            update["speed"] = s.group(1)
        e = ETA_RE.search(line)
        if e:
            update["eta"] = e.group(1)

        fm = re.search(r"\[(?:download|Merger|ExtractAudio)\].*?(?:Destination:|Merging formats into|Destination)\s+\"?(.+?)\"?$", line)
        if fm:
            final_file = fm.group(1)
            if batch:
                done_count += 1
                update["done_count"] = done_count
        if "has already been downloaded" in line:
            fm2 = re.search(r"\]\s+(.+?)\s+has already", line)
            if fm2:
                final_file = fm2.group(1)
        _set(job_id, **update)

    proc.wait()
    # --ignore-errors means a non-zero exit can still mean "mostly succeeded"
    # for a batch; report what we got.
    if proc.returncode == 0 or (batch and done_count):
        if batch:
            line = f"BATCH COMPLETE. Ripped {done_count} of {total or done_count} videos."
            name = None
        else:
            line = "RIP COMPLETE. Welcome to the scene."
            name = os.path.basename(final_file) if final_file else None
        _set(job_id, status="done", percent=100, line=line,
            file=name, done_count=done_count)
    elif batch and not done_count:
        msg = f"No videos downloaded — {plat_name} blocked every item (usually the session gate)."
        if not browser:
            msg += f"  ►► FIX: pick the browser you're signed in to {plat_name} with from the SESSION COOKIES menu, then try again."
        _set(job_id, status="error", error=msg)
    else:
        cur = JOBS.get(job_id, {})
        msg = cur.get("line") or "yt-dlp exited non-zero."
        if ("403" in msg or "Forbidden" in msg or "SABR" in msg or "login" in msg.lower()
                or "private" in msg.lower() or "PO Token" in msg) and not browser:
            msg += f"  ►► FIX: pick the browser you're signed in to {plat_name} with from the SESSION COOKIES menu, then try again."
        _set(job_id, status="error", error=msg)


def _set(job_id, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(kw)


# --------------------------------------------------------------------------- #
#  HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- access control ----
    def _supplied_token(self, qs):
        hdr = self.headers.get("X-Access-Token")
        if hdr:
            return hdr.strip()
        return (qs.get("key") or [""])[0]

    def _gate(self, qs):
        """Return None if the request may proceed, else send a denial and
        return the reason string."""
        addr = self.client_address[0]
        if not ip_allowed(addr):
            self._deny(403, "FORBIDDEN — this address is outside the allowed "
                          "local network. The internet cannot reach this tool.")
            return "ip"
        # Loopback is this machine's own user — no key needed, so the local URL
        # can stay clean (http://localhost:PORT). The key still gates LAN access.
        if addr == "::1" or addr.startswith("127."):
            return None
        if not secrets.compare_digest(self._supplied_token(qs), ACCESS_TOKEN):
            self._deny(401, "ACCESS DENIED — missing or wrong key. Open the "
                          "exact URL printed in the server terminal "
                          "(it includes ?key=…).")
            return "token"
        return None

    def _deny(self, code, msg):
        body = (
            "<!doctype html><meta charset=utf-8>"
            "<title>:: ACCESS DENIED ::</title>"
            "<body style='background:#05010a;color:#ff0033;font-family:monospace;"
            "text-align:center;padding:14vh 8vw;text-shadow:0 0 8px #ff0033'>"
            "<h1 style='font-size:34px;letter-spacing:3px'>▓▒░ ACCESS DENIED ░▒▓</h1>"
            f"<p style='color:#39ff14;text-shadow:0 0 6px #39ff14;font-size:14px'>{msg}</p>"
            "</body>"
        ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if self._gate(qs):
            return

        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/health":
            return self._json({"ytdlp": have_ytdlp(),
                              "ytdlp_version": ytdlp_version(),
                              "ffmpeg": have_ffmpeg(),
                              "default_dest": DEFAULT_DEST,
                              "home": HOME, "app": APP_MODE, "wrapped": WRAPPED,
                              "version": VERSION,
                              "platforms": [{"name": p["name"], "support": p["support"]}
                                            for p in PLATFORMS]})
        if path == "/api/update/check":
            info = check_update()
            info.pop("_manifest", None)
            return self._json(info)
        if path == "/api/quit":
            self._json({"ok": True})
            if HTTPD is not None:
                threading.Thread(target=HTTPD.shutdown, daemon=True).start()
            return
        if path == "/api/progress":
            jid = (qs.get("id") or [""])[0]
            with JOBS_LOCK:
                job = dict(JOBS.get(jid, {"status": "unknown"}))
            return self._json(job)
        if path == "/api/browse":
            return self._json({"path": pick_folder()})
        if path == "/api/open":
            want = (qs.get("dir") or [""])[0]
            try:
                target = resolve_dest(want) if want else DEFAULT_DEST
            except ValueError:
                target = DEFAULT_DEST
            try:
                subprocess.Popen(["open", target])
            except Exception:
                pass
            return self._json({"ok": True, "dir": target})
        self.send_error(404)

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if self._gate(qs):
            return
        try:
            body = self._read_body()
        except Exception:
            return self._json({"error": "bad request body"}, 400)

        if path == "/api/info":
            url = (body.get("url") or "").strip()
            if not _looks_like_url(url):
                return self._json({"error": "That doesn't look like a URL, agent."}, 400)
            if not have_ytdlp():
                return self._json({"error": "yt-dlp is not installed. Run ./start.sh or pip install yt-dlp."}, 500)
            try:
                return self._json(fetch_info(url, body.get("browser")))
            except subprocess.TimeoutExpired:
                return self._json({"error": "Target timed out."}, 504)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path == "/api/download":
            url = (body.get("url") or "").strip()
            browser = body.get("browser")
            if not _looks_like_url(url):
                return self._json({"error": "bad url"}, 400)
            try:
                dest = resolve_dest(body.get("dest"))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            sel = {
                "format_id": body.get("format_id") or "",
                "kind": body.get("kind") or "video",
                "mp3": bool(body.get("mp3")),
                "quality": body.get("quality") or "",
                "batch": bool(body.get("batch")),
                "limit": body.get("limit"),
                "dest": dest,
            }
            job_id = uuid.uuid4().hex[:12]
            _set(job_id, status="queued", percent=0, line="Queued.", dest=dest)
            t = threading.Thread(target=run_download,
                                args=(job_id, url, browser, sel),
                                daemon=True)
            t.start()
            return self._json({"job_id": job_id})

        if path == "/api/update/apply":
            if not (APP_MODE or "--app" in sys.argv):
                return self._json({"error": "Updates are only available in the app. "
                                            "From source, use git."}, 400)
            ok, msg = apply_code_update()
            self._json({"ok": ok, "message": msg, "restart": ok})
            if ok:
                # let the response flush, then restart so the launcher re-execs
                # the freshly-written server.py
                threading.Timer(0.6, os._exit, args=(RESTART_EXIT_CODE,)).start()
            return

        if path == "/api/ytdlp/refresh":
            return self._json(refresh_ytdlp())

        self.send_error(404)

    def _serve_index(self):
        fp = os.path.join(HERE, "index.html")
        if not os.path.exists(fp):
            return self.send_error(404)
        with open(fp, "r", encoding="utf-8") as fh:
            html = fh.read()
        # Hand the page its own access key so every API call it makes is
        # authenticated without the user re-typing it.
        inject = f'<script>window.ACCESS_TOKEN={json.dumps(ACCESS_TOKEN)};</script>'
        html = html.replace("</head>", inject + "</head>", 1)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _looks_like_url(u):
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _port_open(port, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s2 = socket.create_connection((host, port), timeout=0.4)
        s2.close()
        return True
    except OSError:
        return False
    finally:
        s.close()


def _open_browser(port):
    import webbrowser
    # Clean local URL — loopback skips the key gate.
    webbrowser.open(f"http://localhost:{port}/")


def main():
    global HTTPD
    app_mode = APP_MODE or ("--app" in sys.argv)
    want_port = int(os.environ.get("PORT", "1337"))   # leet, on-theme, > 1024
    host = os.environ.get("HOST", "0.0.0.0")

    # Single-instance: if launched again while already running, just reopen the
    # browser on the existing server and exit. (Not in wrapped mode — the native
    # window manages its own server on a private port.)
    if app_mode and not WRAPPED and _port_open(want_port):
        _open_browser(want_port)
        return

    # Bind, hunting for a free port if the preferred one is taken.
    server, port = None, want_port
    for p in range(want_port, want_port + 12):
        try:
            server = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("!! could not bind a port in range"); return
    HTTPD = server

    key = ACCESS_TOKEN
    lan = lan_ip()
    print("=" * 64)
    print("  TUBE-RIPPER DELUXE 2000  ::  backend online")
    print(f"  >> this machine : http://localhost:{port}/")
    if host != "127.0.0.1" and lan != "127.0.0.1":
        print(f"  >> other devices: http://{lan}:{port}/?key={key}")
        print(f"     (same Wi-Fi/LAN only — the public internet is blocked)")
    print(f"  >> downloads     : {DEFAULT_DEST}  (changeable in the UI)")
    print(f"  >> yt-dlp: {'OK' if have_ytdlp() else 'MISSING'}"
        f"   ffmpeg: {'OK' if have_ffmpeg() else 'MISSING'}")
    print("=" * 64)

    if app_mode and not WRAPPED:
        threading.Timer(0.8, _open_browser, args=(port,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("\n  >> shutting down. stay frosty.")


if __name__ == "__main__":
    main()
