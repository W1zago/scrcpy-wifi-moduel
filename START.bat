@echo off
REM START.bat — ПОДВІЙНИЙ КЛІК і все працює. Нічого вводити вручну не треба.
REM Сам: перевіряє залежності, робить adb start-server/tcpip/connect,
REM      збирає (якщо треба), запускає sender + receiver + scrcpy.
REM Використання:
REM   START.bat                (= --mode auto)
REM   START.bat demo           (= --mode demo, без телефону)
REM   START.bat real           (= --mode real, вимагає телефон)
REM   START.bat check          (= тільки перевірка)

chcp 65001 >nul
cd /d %~dp0
echo === Virtual USB Cable — АВТОСТАРТ (подвійний клік, без ручного вводу) ===

where python >nul 2>&1
if errorlevel 1 (
  echo [AUTO] ERROR: python не знайдено. Встановіть: winget install Python.Python.3.12
  pause
  exit /b 1
)

if "%1"=="demo" (
  python tools\auto_run.py --mode demo
) else if "%1"=="real" (
  python tools\auto_run.py --mode real
) else if "%1"=="check" (
  python tools\auto_run.py --check
) else if "%1"=="dry-run" (
  python tools\auto_run.py --dry-run
) else if "%1"=="" (
  python tools\auto_run.py --mode auto
) else (
  python tools\auto_run.py %*
)

pause
