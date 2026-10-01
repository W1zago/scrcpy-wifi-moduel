@echo off
REM START_UI.bat - Launch CyberDeck Electron UI for scrcpy-wifi-module
cd /d "%~dp0ui"
call npm.cmd start
