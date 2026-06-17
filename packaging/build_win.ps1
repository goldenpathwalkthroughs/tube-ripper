# =====================================================================
# Build TUBE-RIPPER DELUXE 2000 as a self-contained Windows package.
#
# Bundles (no install needed on the target PC): relocatable CPython 3.13,
# yt-dlp (pip module), ffmpeg.exe. A .vbs launcher starts it with no console
# and opens the browser at http://localhost:1337.
#
# Run on Windows (PowerShell):  packaging\build_win.ps1
# Output: dist\TubeRipper-Windows.zip   (x64; covers virtually all Windows PCs)
# =====================================================================
$ErrorActionPreference = "Stop"
$PBS_TAG = "20260610"
$PY_VER  = "3.13.14"
$FF_URL  = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

$root  = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build-win"
$dist  = Join-Path $root "dist"
$app   = Join-Path $build "TubeRipper"
$cache = Join-Path $build "cache"

function Say($m) { Write-Host ">> $m" -ForegroundColor Cyan }

Remove-Item -Recurse -Force $app -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $cache, "$app\app", "$app\python", "$app\bin", $dist | Out-Null

# 1. Python (Windows x64, relocatable) ---------------------------------
$pyTar = Join-Path $cache "python-win.tar.gz"
if (-not (Test-Path $pyTar)) {
  Say "downloading Python $PY_VER"
  $u = "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PY_VER+$PBS_TAG-x86_64-pc-windows-msvc-install_only.tar.gz"
  Invoke-WebRequest -Uri $u -OutFile $pyTar
}
Say "extracting Python"
$tmp = Join-Path $build "pytmp"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $tmp | Out-Null
tar -xzf $pyTar -C $tmp
Copy-Item "$tmp\python\*" "$app\python\" -Recurse -Force

# 2. yt-dlp into the bundled Python ------------------------------------
Say "installing yt-dlp"
& "$app\python\python.exe" -m pip install --quiet --no-warn-script-location --upgrade pip yt-dlp
if ($LASTEXITCODE -ne 0) { throw "pip install yt-dlp failed" }

# 3. ffmpeg.exe --------------------------------------------------------
$ffZip = Join-Path $cache "ffmpeg.zip"
if (-not (Test-Path $ffZip)) { Say "downloading ffmpeg"; Invoke-WebRequest -Uri $FF_URL -OutFile $ffZip }
Say "extracting ffmpeg"
$ffDir = Join-Path $build "ff"
Remove-Item -Recurse -Force $ffDir -ErrorAction SilentlyContinue
Expand-Archive -Path $ffZip -DestinationPath $ffDir -Force
$ffExe = Get-ChildItem -Path $ffDir -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
if (-not $ffExe) { throw "ffmpeg.exe not found in archive" }
Copy-Item $ffExe.FullName "$app\bin\ffmpeg.exe" -Force

# 4. app code + launcher ----------------------------------------------
Say "copying app"
foreach ($f in @("server.py","index.html","favicon.svg","apple-touch-icon.png")) {
  if (Test-Path "$root\$f") { Copy-Item "$root\$f" "$app\app\$f" -Force }
}
Copy-Item "$root\packaging\launcher.vbs" "$app\TubeRipper.vbs" -Force
if (Test-Path "$root\packaging\INSTALL-Windows.txt") {
  Copy-Item "$root\packaging\INSTALL-Windows.txt" "$app\INSTALL.txt" -Force
}

# 5. zip ---------------------------------------------------------------
Say "zipping"
$zip = Join-Path $dist "TubeRipper-Windows.zip"
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path $app -DestinationPath $zip -Force

$size = "{0:N0} MB" -f ((Get-Item $zip).Length / 1MB)
Say "DONE -> $zip ($size)"
