@echo off
REM START_CONSOLE.bat - same as START.bat but console-only, never opens a window.
REM NOTE: keep this file pure ASCII + CRLF, otherwise cmd.exe breaks.
REM Extra arguments are forwarded, e.g.: START_CONSOLE.bat --scan
call "%~dp0START.bat" --no-gui %*
