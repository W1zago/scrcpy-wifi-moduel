#!/usr/bin/env bash
# START_UI.sh - Launch CyberDeck Electron UI (standalone mode).
set -u
cd "$(dirname "$0")/ui"

if ! command -v npm >/dev/null 2>&1; then
  echo "[UI] ERROR: npm not found in PATH."
  echo "[UI] Install Node.js LTS from https://nodejs.org/"
  read -r -p "[UI] Press Enter to close this window..." _
  exit 1
fi

if [ ! -x "node_modules/electron/dist/electron" ]; then
  echo "[UI] First run: installing UI dependencies (npm install, 1-3 min)..."
  if ! npm install --no-audit --no-fund; then
    echo "[UI] ERROR: npm install failed - read the messages above."
    read -r -p "[UI] Press Enter to close this window..." _
    exit 1
  fi
fi

npm start
RC=$?
echo
echo "[UI] Exit code: $RC"
read -r -p "[UI] Press Enter to close this window..." _
exit "$RC"
