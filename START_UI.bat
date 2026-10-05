@echo off
REM START_UI.bat - Launch CyberDeck Electron UI for scrcpy-wifi-module
REM NOTE: keep this file pure ASCII + CRLF, otherwise cmd.exe breaks.
cd /d "%~dp0ui"

where npm >nul 2>nul
if errorlevel 1 (
  echo [UI] ERROR: npm not found in PATH.
  echo [UI] Install Node.js LTS from https://nodejs.org/ , then run again.
  echo [UI] Press any key to close this window...
  pause >nul
  exit /b 1
)

if not exist "node_modules\electron\dist\electron.exe" (
  echo [UI] First run: installing UI dependencies (npm install, 1-3 min)...
  call npm.cmd install --no-audit --no-fund
  if errorlevel 1 (
    echo [UI] ERROR: npm install failed - read the messages above.
    echo [UI] Press any key to close this window...
    pause >nul
    exit /b 1
  )
)

call npm.cmd start
set "RC=%errorlevel%"
echo.
echo [UI] Exit code: %RC%
echo [UI] Press any key to close this window...
pause >nul
exit /b %RC%
