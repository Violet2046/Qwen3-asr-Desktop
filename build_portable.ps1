<#
  Build a portable Windows x86 app (no installation; copy the whole dist folder).

  Usage:
    powershell -ExecutionPolicy Bypass -File .\build_portable.ps1
    powershell -ExecutionPolicy Bypass -File .\build_portable.ps1 -OneFile

  Notes:
  - Default is --onedir (fast startup). -OneFile builds a single exe (slower first start).
  - Upstream Web UI assets are bundled; imageio-ffmpeg provides a bundled ffmpeg.
#>
param(
    [switch]$OneFile,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $Python) {
    if (Test-Path ".venv\Scripts\python.exe") { $Python = ".venv\Scripts\python.exe" }
    else { $Python = "python" }
}

Write-Host "==> Python: $Python"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }

$pyArgs = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean", "--windowed",
    "--name", "Qwen3-ASR-Desktop",
    $mode,
    "--add-data", "asr_desktop/web;asr_desktop/web",
    "--collect-all", "imageio_ffmpeg",
    "--collect-submodules", "uvicorn",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops.auto",
    "--hidden-import", "uvicorn.protocols.http.auto",
    "--hidden-import", "uvicorn.protocols.websockets.auto",
    "--hidden-import", "uvicorn.lifespan.on",
    "app.py"
)

Write-Host "==> Running PyInstaller..."
& $Python @pyArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$out = if ($OneFile) { "dist" } else { "dist\Qwen3-ASR-Desktop" }
Copy-Item -Force "README.md" $out
Copy-Item -Force "LICENSE" $out
Copy-Item -Force "THIRD_PARTY_NOTICES.md" $out
New-Item -ItemType Directory -Force (Join-Path $out "data") | Out-Null

Write-Host ""
Write-Host "==> Done: $out"
if (-not $OneFile) {
    Write-Host "    Run $out\Qwen3-ASR-Desktop.exe (the whole folder is portable)."
} else {
    Write-Host "    Run $out\Qwen3-ASR-Desktop.exe"
}
exit 0
