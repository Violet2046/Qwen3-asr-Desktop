@echo off
setlocal
cd /d "%~dp0"

rem 1) 优先使用 Electron 桌面外壳（已执行过 npm install 时）
if exist "electron\node_modules\electron\dist\electron.exe" (
  start "" "electron\node_modules\electron\dist\electron.exe" "electron"
  goto :eof
)

rem 2) 回退：Python 后端 + 系统浏览器窗口
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py %*
  goto :eof
)
where python >nul 2>nul
if errorlevel 1 (
  echo [!] 未找到 Python。请安装 Python 3.10+，或直接运行打包后的 dist-electron\win-unpacked\Qwen3-ASR Desktop.exe
  pause
  exit /b 1
)
python app.py %*
