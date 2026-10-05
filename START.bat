@echo off
REM START.bat - double-click and everything works. No manual input needed.
REM NOTE: this file must stay pure ASCII + CRLF, otherwise cmd.exe
REM breaks lines on multi-byte chars and the window flashes and closes.
REM All user messages live in tools/auto_run.py (Python handles UTF-8 fine).

cd /d "%~dp0"
chcp 65001 >nul
echo === Virtual USB Cable - AUTOSTART (double-click, no manual input) ===
echo.

REM ---- find python (python, fallback: py -3) ----
set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto :NOPYTHON

REM ---- dispatch by first argument, default = auto ----
if "%~1"=="demo" goto :DEMO
if "%~1"=="real" goto :REAL
if "%~1"=="check" goto :CHECK
if "%~1"=="dry-run" goto :DRYRUN
if "%~1"=="install" goto :INSTALL
if "%~1"=="" goto :AUTO
goto :CUSTOM

:AUTO
%PY% tools\auto_run.py --mode auto
goto :DONE

:DEMO
%PY% tools\auto_run.py --mode demo
goto :DONE

:REAL
%PY% tools\auto_run.py --mode real
goto :DONE

:CHECK
%PY% tools\auto_run.py --check
goto :DONE

:DRYRUN
%PY% tools\auto_run.py --dry-run
goto :DONE

:INSTALL
%PY% tools\auto_run.py --install-deps --mode auto
goto :DONE

:CUSTOM
%PY% tools\auto_run.py %*
goto :DONE

:DONE
set "RC=%errorlevel%"
echo.
echo [AUTO] Exit code: %RC%
if not "%RC%"=="0" (
  echo [AUTO] Something went wrong - read the messages above.
  echo [AUTO] Tip: run "START.bat check" or "START.bat dry-run" to diagnose.
)
echo [AUTO] Press any key to close this window...
pause >nul
exit /b %RC%

:NOPYTHON
echo [AUTO] ERROR: python not found in PATH.
echo [AUTO] Python is required to run this program.
echo.
where winget >nul 2>nul
if errorlevel 1 goto :NOPYTHON_NOWINGET
echo [AUTO] I can install Python automatically via winget (Python.Python.3.12, 1-3 min).
set "INSTALL_PY="
set /p INSTALL_PY="[AUTO] Install Python now? [Y/n]: "
if /i "%INSTALL_PY%"=="n" goto :NOPYTHON_DECLINED
if /i "%INSTALL_PY%"=="no" goto :NOPYTHON_DECLINED
if /i "%INSTALL_PY%"=="N" goto :NOPYTHON_DECLINED
echo [AUTO] Installing Python, please wait... (press Yes if Windows asks for permission)
winget install --accept-source-agreements --accept-package-agreements -e --id Python.Python.3.12
echo.
echo [AUTO] Refreshing PATH for this session...
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "FRESHPATH=%%p"
if defined FRESHPATH set "PATH=%FRESHPATH%;%PATH%"
set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if defined PY (
  echo [AUTO] Python installed successfully, continuing...
  echo.
  goto :AUTO
)
echo [AUTO] ERROR: Python still not found after install.
echo [AUTO] Close this window, open a NEW cmd window and run: python --version
echo [AUTO] If it works, double-click START.bat again.
echo [AUTO] Press any key to close this window...
pause >nul
exit /b 1

:NOPYTHON_DECLINED
echo [AUTO] OK, skipping auto-install.
echo [AUTO] Install manually with:  winget install Python.Python.3.12
echo [AUTO] Or from: https://www.python.org/downloads/
echo [AUTO] Then double-click START.bat again.
echo [AUTO] Press any key to close this window...
pause >nul
exit /b 1

:NOPYTHON_NOWINGET
echo [AUTO] Cannot auto-install: 'winget' not found on this system.
echo [AUTO] Install manually from: https://www.python.org/downloads/
echo [AUTO] (tick "Add python.exe to PATH" during setup)
echo [AUTO] Then double-click START.bat again.
echo [AUTO] Press any key to close this window...
pause >nul
exit /b 1
