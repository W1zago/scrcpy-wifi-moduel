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
echo [AUTO] Install it with:  winget install Python.Python.3.12
echo [AUTO] Then reopen this file by double-clicking it.
echo [AUTO] Press any key to close this window...
pause >nul
exit /b 1
