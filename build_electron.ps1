<#
  Build the full portable Electron app:
    1) PyInstaller builds the Python backend into dist\Qwen3-ASR-Desktop
    2) electron-builder packs Electron + backend into dist-electron\win-unpacked

  Usage:
    powershell -ExecutionPolicy Bypass -File .\build_electron.ps1
    powershell -ExecutionPolicy Bypass -File .\build_electron.ps1 -SkipBackend
#>
param(
    [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- locate Node.js (winget user install does not refresh this shell's PATH) ---
$nodeRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe\node-v24.20.0-win-x64'
if (-not (Get-Command node -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $nodeRoot 'node.exe'))) {
    $env:Path = "$nodeRoot;" + $env:Path
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js not found. Install it first: winget install OpenJS.NodeJS.LTS"
}

if (Get-Command npm -ErrorAction SilentlyContinue | Out-Null) {
    $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
    $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/"
}

if (-not $SkipBackend) {
    Write-Host "==> Building Python backend..."
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'build_portable.ps1')
}

Push-Location (Join-Path $PSScriptRoot 'electron')
try {
    if (-not (Test-Path 'node_modules\electron\package.json')) {
        Write-Host "==> Installing Electron dependencies..."
        npm install --no-audit --no-fund
    }
    if (-not (Test-Path 'node_modules\electron\dist\electron.exe')) {
        Write-Host "==> Downloading Electron binary..."
        node node_modules\electron\install.js
    }

    # 清掉旧产物，避免 electron-builder 复用上一次的 resources/backend
    $electronOut = Join-Path $PSScriptRoot 'dist-app'
    if (Test-Path $electronOut) { Remove-Item $electronOut -Recurse -Force }
    $backendOut = Join-Path $PSScriptRoot 'dist\Qwen3-ASR-Desktop'
    Remove-Item (Join-Path $backendOut 'settings.json') -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $backendOut 'data') -Recurse -Force -ErrorAction SilentlyContinue

    Write-Host "==> Packaging with electron-builder..."
    npm run dist
} finally {
    Pop-Location
}

$out = Join-Path $PSScriptRoot 'dist-app\win-unpacked'
Copy-Item -Force (Join-Path $PSScriptRoot 'README.md') $out
Copy-Item -Force (Join-Path $PSScriptRoot 'LICENSE') $out
Copy-Item -Force (Join-Path $PSScriptRoot 'THIRD_PARTY_NOTICES.md') $out

# 双击启动脚本 + 便携 zip
$startBat = Join-Path $out 'Start.bat'
Set-Content -Path $startBat -Value "@echo off`r`nstart `"`" `"%~dp0Qwen3-ASR Desktop.exe`"" -Encoding ASCII

$appVersion = (Get-Content (Join-Path $PSScriptRoot 'electron\package.json') -Raw | ConvertFrom-Json).version
$zipPath = Join-Path $PSScriptRoot ("dist-app\Qwen3-ASR-Desktop-portable-win-x64-v{0}.zip" -f $appVersion)

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
Write-Host "==> Creating portable zip..."
& $python (Join-Path $PSScriptRoot 'tools\make_portable_zip.py') $out $zipPath

Write-Host ""
Write-Host "==> Done: $out"
Write-Host "    Run: $out\Qwen3-ASR Desktop.exe"
Write-Host "    Zip: $zipPath"
exit 0
