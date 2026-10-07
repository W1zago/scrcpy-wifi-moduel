#!/usr/bin/env bash
# START.sh - double-click (or ./START.sh) and everything works. Linux/macOS.
# Usage: ./START.sh [demo|real|check|dry-run|install|--scan|--no-gui|...]
set -u
cd "$(dirname "$0")"

echo "=== Virtual USB Cable - AUTOSTART (Linux) ==="
echo

PY=""
if command -v python3 >/dev/null 2>&1; then PY="python3"; fi
if [ -z "$PY" ] && command -v python >/dev/null 2>&1; then PY="python"; fi
if [ -z "$PY" ]; then
  echo "[AUTO] ERROR: python3 not found in PATH."
  echo "[AUTO] Install it with: sudo apt-get install -y python3 python3-tk"
  read -r -p "[AUTO] Press Enter to close this window..." _
  exit 1
fi

# chmod helpers (first checkout from zip often loses +x)
chmod +x START.sh START_CONSOLE.sh START_UI.sh 2>/dev/null || true

case "${1:-}" in
  demo)    exec "$PY" tools/auto_run.py --mode demo ;;
  real)    exec "$PY" tools/auto_run.py --mode real ;;
  check)   exec "$PY" tools/auto_run.py --check ;;
  dry-run) exec "$PY" tools/auto_run.py --dry-run ;;
  install) exec "$PY" tools/auto_run.py --install-deps --mode auto ;;
  "")      exec "$PY" tools/auto_run.py --mode auto ;;
  *)       exec "$PY" tools/auto_run.py "$@" ;;
esac
