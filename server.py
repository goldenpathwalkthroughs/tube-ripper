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
import io
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
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

VERSION = "1.0.5"

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

HTTPD = None       # set in main(); used by /api/quit
BOUND_PORT = None  # set in main(); used to build phone URLs

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
UPDATE_REQUIRED = ("server.py", "index.html")          # must be in every payload
UPDATE_OPTIONAL = ("favicon.svg", "apple-touch-icon.png")  # shipped if present
UPDATE_FILES = UPDATE_REQUIRED + UPDATE_OPTIONAL        # all files we may overwrite


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
            for fname in UPDATE_REQUIRED:
                if fname not in names:
                    return False, f"Update payload is missing {fname}."
            for fname in UPDATE_FILES:
                if fname not in names:
                    continue   # optional extras (icons) may be absent
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
        for fname in staged:
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
#  Access control — two layers so this is safe on a LAN:
#    1. the source IP must be loopback / private (RFC1918) / CGNAT-Tailscale.
#       The public internet is rejected outright, even if a router ever
#       forwarded the port.
#    2. the local machine (loopback) is trusted automatically; any other device
#       logs in once with a short PIN, then carries a session cookie (the long
#       ACCESS_TOKEN). Clean URL, remembered per device — no key in the URL.
# --------------------------------------------------------------------------- #
TOKEN_FILE = os.path.join(SUPPORT_DIR, ".access_token")
PIN_FILE = os.path.join(SUPPORT_DIR, ".pin")
SESSION_COOKIE = "tr_auth"


def _load_or_make(path, factory):
    try:
        with open(path) as fh:
            v = fh.read().strip()
            if v:
                return v
    except FileNotFoundError:
        pass
    v = factory()
    try:
        with open(path, "w") as fh:
            fh.write(v)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return v


def load_token():
    env = os.environ.get("ACCESS_TOKEN")
    if env:
        return env.strip()
    return _load_or_make(TOKEN_FILE, lambda: secrets.token_urlsafe(24))


def load_pin():
    env = os.environ.get("TR_PIN")
    if env:
        return env.strip()
    # 6 digits — easy to type on a phone's numeric keypad
    return _load_or_make(PIN_FILE, lambda: f"{secrets.randbelow(900000) + 100000}")


ACCESS_TOKEN = load_token()
PIN = load_pin()

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


def tailscale_ip():
    """A Tailscale (100.64.0.0/10 CGNAT) address on this machine, if any —
    lets the phone reach the tool privately from anywhere, no public exposure."""
    net = ipaddress.ip_network("100.64.0.0/10")
    ifconfig = "/sbin/ifconfig" if os.path.exists("/sbin/ifconfig") else "ifconfig"
    try:
        out = subprocess.run([ifconfig], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                ip = line.split()[1]
                try:
                    if ipaddress.ip_address(ip) in net:
                        return ip
                except ValueError:
                    pass
    except Exception:
        pass
    return ""


def local_hostname():
    """The Mac's stable Bonjour name, e.g. 'macbook-pro.local' — resolvable by
    other Apple devices on the same network without knowing the IP."""
    scutil = "/usr/sbin/scutil" if os.path.exists("/usr/sbin/scutil") else "scutil"
    try:
        n = subprocess.run([scutil, "--get", "LocalHostName"],
                        capture_output=True, text=True, timeout=4).stdout.strip()
        if n:
            return n + ".local"
    except Exception:
        pass
    try:
        h = socket.gethostname()
        return h if h.endswith(".local") else (h + ".local")
    except Exception:
        return ""

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
        # Prefer H.264 + AAC so it plays in QuickTime / Photos / iOS out of the
        # box; fall back through generic ≤1080p, then anything, so it never fails.
        return ["-f",
                "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/"
                "b[height<=1080][ext=mp4]/"
                "bv*[height<=1080]+ba/b[height<=1080]/b",
                "--merge-output-format", "mp4"]
    # highest — best available (4K/HDR may be VP9/AV1, which needs VLC to play)
    return ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]


def _build_cmd(url, browser, sel, dest, batch):
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
        fid = sel.get("format_id")
        if fid:
            cmd += ["-f", f"{fid}/bestaudio/best"]
    elif sel.get("kind") == "audio":
        fid = sel.get("format_id") or "bestaudio"
        cmd += ["-f", f"{fid}/bestaudio/best"]
    else:
        fmt_id = sel.get("format_id", "")
        cmd += ["-f", f"{fmt_id}+bestaudio/{fmt_id}/best", "--merge-output-format", "mp4"]
    cmd.append(url)
    return cmd


def _stream_ytdlp(job_id, cmd, batch):
    """Run yt-dlp, stream progress into the job, return (ok, final_file, done_count)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except Exception as e:
        _set(job_id, status="error", error=str(e))
        return (False, None, 0)

    final_file, cur_item, total, done_count = None, 0, 0, 0
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
            update["percent"] = (round(((cur_item - 1) + file_pct / 100.0) / total * 100, 1)
                                if batch and total else file_pct)
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
    ok = proc.returncode == 0 or (batch and done_count > 0)
    return (ok, final_file, done_count)


def run_download(job_id, url, browser, sel, auto_browser=""):
    batch = bool(sel.get("batch"))
    dest = sel.get("dest") or DEFAULT_DEST
    plat = detect_platform(url)
    plat_name = plat["name"]

    _set(job_id, status="running", line="Spawning yt-dlp...", percent=0,
        batch=batch, item=0, total=0, done_count=0)

    ok, final_file, done_count = _stream_ytdlp(job_id, _build_cmd(url, browser, sel, dest, batch), batch)

    # Bidirectional cookie fallback:
    #  • YouTube with cookies often gets only broken SABR formats → retry clean.
    #  • Instagram/Facebook/etc. need a logged-in session → if no cookies were
    #    used, retry with the browser the page was opened in.
    if not ok and browser:
        _set(job_id, line="Session cookies didn't work — retrying without them…",
            percent=0, item=0, total=0, done_count=0)
        ok, final_file, done_count = _stream_ytdlp(job_id, _build_cmd(url, "", sel, dest, batch), batch)
    elif not ok and not browser and plat["key"] != "youtube" and auto_browser:
        _set(job_id, line=f"{plat_name} needs a sign-in — retrying with your {auto_browser} session…",
            percent=0, item=0, total=0, done_count=0)
        ok, final_file, done_count = _stream_ytdlp(job_id, _build_cmd(url, auto_browser, sel, dest, batch), batch)

    if ok:
        if batch:
            line = f"BATCH COMPLETE. Ripped {done_count} video(s)."
            name = None
        else:
            line = "RIP COMPLETE. Welcome to the scene."
            name = os.path.basename(final_file) if final_file else None
        _set(job_id, status="done", percent=100, line=line,
            file=name, file_path=(final_file if not batch else None),
            done_count=done_count)
    else:
        cur = JOBS.get(job_id, {})
        msg = cur.get("line") or "yt-dlp couldn't download this."
        ml = msg.lower()
        plat_key = detect_platform(url)["key"]
        login_ish = ("login" in ml or "private" in ml or "rate-limit" in ml
                    or "not available" in ml or "cookies" in ml or "sign in" in ml
                    or "empty" in ml or "no video" in ml)
        if plat_key == "youtube" and browser and ("format" in ml or "403" in msg
                or "forbidden" in ml or "sabr" in ml or "po token" in ml):
            msg += ("  ►► TIP: try SESSION COOKIES = none — a logged-in YouTube session can "
                    "make YouTube serve formats this tool can't fetch.")
        elif plat_key != "youtube" and not browser and login_ish:
            msg += (f"  ►► TIP: {plat_name} usually needs you to be logged in. Pick the browser "
                    f"you're signed in to {plat_name} with from the SESSION COOKIES menu, then retry.")
        _set(job_id, status="error", error=msg)


def _set(job_id, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(kw)


LOGIN_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>:: TUBE-RIPPER :: SIGN IN ::</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-title" content="TUBE-RIPPER">
<style>
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    font-family:"Lucida Console",monospace;color:#eee;
    background:radial-gradient(ellipse at 50% -10%,#3a0d63,#11021f 55%,#05010a)}
  .card{width:90%;max-width:340px;text-align:center;border:2px solid #000;border-radius:8px;
    padding:26px 22px;background:linear-gradient(180deg,#1f0c33,#160826);
    box-shadow:0 0 0 2px #4d178a,0 0 26px rgba(255,0,51,.5)}
  .logo{font-family:Impact,"Arial Black",sans-serif;font-size:26px;letter-spacing:1px;color:#fff;
    text-shadow:0 0 14px #ff0033;margin-bottom:2px}
  .sub{color:#00ffd5;font-size:11px;letter-spacing:4px;margin-bottom:20px;text-shadow:0 0 8px #00ffd5}
  label{display:block;font-size:11px;color:#ffcc00;letter-spacing:2px;margin-bottom:8px}
  input{width:100%;font-family:inherit;font-size:26px;text-align:center;letter-spacing:10px;
    color:#39ff14;background:#000;border:2px solid #000;border-radius:4px;padding:12px;
    box-shadow:inset 0 0 12px rgba(0,0,0,.9),0 0 0 1px #00ffd5;outline:none;text-shadow:0 0 6px #39ff14}
  button{margin-top:16px;width:100%;cursor:pointer;font-family:Impact,sans-serif;letter-spacing:2px;
    font-size:18px;color:#fff;border:2px solid #000;border-radius:5px;padding:12px;
    background:linear-gradient(180deg,#ff4d6a,#ff0033 45%,#990014);
    box-shadow:inset 0 2px 1px rgba(255,255,255,.6),0 0 14px #ff0033;text-shadow:1px 1px 0 #000}
  .err{color:#ff0033;font-size:12px;margin-top:12px;min-height:16px;text-shadow:0 0 6px #ff0033}
  .hint{color:#9a7ac0;font-size:11px;margin-top:14px;line-height:1.5}
</style></head>
<body>
  <form class="card" id="f">
    <div class="logo">TUBE-RIPPER</div>
    <div class="sub">D E L U X E &nbsp; 2 0 0 0</div>
    <label>ENTER PIN</label>
    <input id="pin" inputmode="numeric" pattern="[0-9]*" autocomplete="one-time-code"
        maxlength="6" placeholder="······" autofocus>
    <button type="submit">UNLOCK</button>
    <div class="err" id="err"></div>
    <div class="hint">The 6-digit PIN is shown on the Mac running TUBE-RIPPER
        (under “USE ON PHONE”). You only enter it once on this device.</div>
  </form>
<script>
  const f=document.getElementById("f"),pin=document.getElementById("pin"),err=document.getElementById("err");
  f.addEventListener("submit",async e=>{
    e.preventDefault(); err.textContent="";
    try{
      const r=await fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({pin:pin.value.trim()})});
      const d=await r.json();
      if(d.ok){ location.replace("/"); } else { err.textContent="✖ "+(d.error||"Wrong PIN"); pin.value=""; pin.focus(); }
    }catch(_){ err.textContent="✖ connection error"; }
  });
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
#  HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("TR_DEBUG"):
            try:
                sys.stderr.write("[tr] %s %s\n" % (self.client_address[0], fmt % args))
            except Exception:
                pass

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
    def _is_loopback(self):
        a = self.client_address[0]
        return a == "::1" or a.startswith("127.")

    def _supplied_token(self, qs):
        hdr = self.headers.get("X-Access-Token")
        if hdr:
            return hdr.strip()
        return (qs.get("key") or [""])[0]

    def _has_session(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        try:
            ck = SimpleCookie(raw).get(SESSION_COOKIE)
            return ck is not None and secrets.compare_digest(ck.value, ACCESS_TOKEN)
        except Exception:
            return False

    def _authed(self, qs):
        """The Mac itself is trusted; other devices need a session cookie (set
        after the PIN login) or a back-compat ?key= token."""
        if self._is_loopback() or self._has_session():
            return True
        k = self._supplied_token(qs)
        return bool(k) and secrets.compare_digest(k, ACCESS_TOKEN)

    def _ip_ok(self):
        if ip_allowed(self.client_address[0]):
            return True
        self._deny(403, "FORBIDDEN — this address is outside the allowed local "
                    "network. The internet cannot reach this tool.")
        return False

    def _set_session_cookie(self):
        # one year, the token is the value; HttpOnly so page JS can't read it
        self.send_header("Set-Cookie",
            f"{SESSION_COOKIE}={ACCESS_TOKEN}; Max-Age=31536000; Path=/; "
            "HttpOnly; SameSite=Lax")

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
        if not self._ip_ok():
            return
        # Icons are public (the login page and bookmarks need them, pre-auth).
        # iOS requests several sized variants (apple-touch-icon-152x152.png …) —
        # serve the one icon for any of them so none 404.
        if path in ("/favicon.svg", "/favicon.ico"):
            return self._serve_static(os.path.join(HERE, "favicon.svg"), "image/svg+xml")
        if path.startswith("/apple-touch-icon"):
            return self._serve_static(os.path.join(HERE, "apple-touch-icon.png"), "image/png")
        if not self._authed(qs):
            # Unknown device: show the PIN login for the page, refuse everything else.
            if path in ("/", "/index.html"):
                return self._serve_login()
            return self._deny(401, "Enter the PIN on this device to use TUBE-RIPPER.")

        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/api/health":
            return self._json({"ytdlp": have_ytdlp(),
                              "ytdlp_version": ytdlp_version(),
                              "ffmpeg": have_ffmpeg(),
                              "default_dest": DEFAULT_DEST,
                              "home": HOME, "app": APP_MODE, "wrapped": WRAPPED,
                              "local": self._is_loopback(), "version": VERSION,
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
        if path == "/api/share":
            # How to reach it from a phone: a stable Bonjour hostname (no IP to
            # memorise), the IP as a fallback, Tailscale for away-from-home, and
            # the PIN to log in once. Private to your network — never public.
            port = BOUND_PORT or 1337
            out = {"pin": PIN, "port": port}
            host = local_hostname()
            if host:
                out["host"] = f"{host}:{port}"
            lan = lan_ip()
            if lan and lan != "127.0.0.1":
                out["ip"] = f"{lan}:{port}"
            ts = tailscale_ip()
            if ts:
                out["tailscale"] = f"{ts}:{port}"
            return self._json(out)
        if path == "/api/file":
            # Serve a finished single-video file so a phone can save it locally.
            jid = (qs.get("id") or [""])[0]
            with JOBS_LOCK:
                fp = (JOBS.get(jid) or {}).get("file_path")
            return self._serve_download(fp)
        if path == "/api/fda":
            # open System Settings → Privacy & Security → Full Disk Access
            try:
                subprocess.Popen(["open",
                    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
            except Exception:
                pass
            return self._json({"ok": True})
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
        # Unknown route: API gets a clean JSON 404; any other navigation is
        # bounced to the app so a stray/stale URL never shows a raw 404 page.
        if path.startswith("/api/"):
            return self._json({"error": "unknown endpoint"}, 404)
        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if not self._ip_ok():
            return
        try:
            body = self._read_body()
        except Exception:
            return self._json({"error": "bad request body"}, 400)

        # The login step itself can't require a session yet.
        if path == "/login":
            pin = str(body.get("pin", "")).strip()
            if pin and secrets.compare_digest(pin, PIN):
                out = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self._set_session_cookie()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            else:
                self._json({"ok": False, "error": "Wrong PIN."}, 401)
            return

        if not self._authed(qs):
            return self._json({"error": "Not authorised — log in with the PIN."}, 401)

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
            browser = (body.get("browser") or "").strip()
            auto_browser = (body.get("auto_browser") or "").strip()
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
                                args=(job_id, url, browser, sel, auto_browser),
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

        return self._json({"error": "unknown endpoint"}, 404)

    def _serve_download(self, fp):
        """Stream a downloaded file as an attachment, but only if it really
        sits inside an allowed destination root (no arbitrary file reads)."""
        if not fp:
            return self.send_error(404, "No file for that job.")
        real = os.path.realpath(fp)
        if not any(real == r or real.startswith(r + os.sep) for r in ALLOWED_DEST_ROOTS):
            return self.send_error(403, "Forbidden path.")
        if not os.path.isfile(real):
            return self.send_error(404, "File not found.")
        import mimetypes
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        name = os.path.basename(real)
        try:
            size = os.path.getsize(real)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                            "attachment; filename*=UTF-8''" + quote(name))
            self.end_headers()
            with open(real, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_index(self):
        fp = os.path.join(HERE, "index.html")
        if not os.path.exists(fp):
            return self.send_error(404)
        with open(fp, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        # If a remote device got here (cookie or ?key), make the session stick.
        if not self._is_loopback():
            self._set_session_cookie()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, fp, ctype):
        if not os.path.isfile(fp):
            return self.send_error(404)
        with open(fp, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _serve_login(self):
        html = LOGIN_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


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

    # In app mode the port must stay stable (1337) so phone bookmarks and the
    # launcher's "Open" keep working; only hunt in source mode.
    candidates = [want_port] if app_mode else list(range(want_port, want_port + 12))
    server, port = None, want_port
    for p in candidates:
        try:
            server = ThreadingHTTPServer((host, p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("!! could not bind a port in range"); return
    global BOUND_PORT
    HTTPD = server
    BOUND_PORT = port

    lan = lan_ip()
    host_name = local_hostname()
    print("=" * 64)
    print("  TUBE-RIPPER DELUXE 2000  ::  backend online")
    print(f"  >> this machine : http://localhost:{port}/")
    if host != "127.0.0.1":
        target = host_name or lan
        if target and target != "127.0.0.1":
            print(f"  >> on your phone: http://{target}:{port}/   PIN: {PIN}")
            if host_name and lan != "127.0.0.1":
                print(f"     (or http://{lan}:{port}/ if the name won't resolve)")
            print(f"     same Wi-Fi/LAN only — the public internet is blocked")
    print(f"  >> downloads     : {DEFAULT_DEST}  (changeable in the UI)")
    print(f"  >> yt-dlp: {'OK' if have_ytdlp() else 'MISSING'}"
        f"   ffmpeg: {'OK' if have_ffmpeg() else 'MISSING'}")
    print("=" * 64)

    if app_mode and not WRAPPED and os.environ.get("TR_NO_OPEN") != "1":
        threading.Timer(0.8, _open_browser, args=(port,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("\n  >> shutting down. stay frosty.")


if __name__ == "__main__":
    main()
