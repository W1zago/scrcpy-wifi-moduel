#!/usr/bin/env bash
# START_UI.sh - Launch CyberDeck Electron UI (standalone mode).
set -u
cd "$(dirname "$0")/ui"

# --- 0) Дисплей: без X11/Wayland Electron мовчки впаде ---
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "[UI] ERROR: нема DISPLAY / WAYLAND_DISPLAY — вікно відкрити ніде."
  if [ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}" ]; then
    echo "[UI] Ви по SSH без X-forwarding. Для вікна: ssh -X user@host"
  else
    echo "[UI] Запускайте з графічного термінала, не з чистого TTY/docker без -e DISPLAY."
  fi
  echo "[UI] Без вікна: ./START_CONSOLE.sh"
  read -r -p "[UI] Press Enter to close this window..." _ || true
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[UI] ERROR: npm not found in PATH."
  echo "[UI] Install Node.js LTS 18+ from https://nodejs.org/ (apt дає старий node!)"
  echo "[UI] Або: bash ../scripts/setup_linux.sh"
  read -r -p "[UI] Press Enter to close this window..." _ || true
  exit 1
fi

# --- 1) Node <18 не потягне Electron 28 / npm install ---
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
  if [ "${NODE_MAJOR:-0}" -lt 18 ] 2>/dev/null; then
    echo "[UI] ERROR: node $(node --version 2>/dev/null) застарий, треба Node.js 18+."
    echo "[UI] apt ставить старий node. Поставте LTS з https://nodejs.org"
    echo "[UI] або через NodeSource, потім: rm -rf node_modules package-lock.json && npm install"
    read -r -p "[UI] Press Enter to close this window..." _ || true
    exit 1
  fi
fi

if [ ! -x "node_modules/electron/dist/electron" ]; then
  echo "[UI] First run: installing UI dependencies (npm install, 1-3 min)..."
  if ! npm install --no-audit --no-fund; then
    echo "[UI] ERROR: npm install failed - read the messages above."
    echo "[UI] Часті причини на Linux: старий node (<18), нема build-essential/python3."
    read -r -p "[UI] Press Enter to close this window..." _ || true
    exit 1
  fi
  # chrome-sandbox без setuid-root часто блокує старт Electron на Debian/Ubuntu
  if [ -f "node_modules/electron/dist/chrome-sandbox" ]; then
    chmod +x node_modules/electron/dist/electron 2>/dev/null || true
  fi
fi

# --- 2) Root вимагає --no-sandbox, інакше "Running as root without --no-sandbox" ---
EXTRA_ARGS=()
if [ "$(id -u)" -eq 0 ]; then
  echo "[UI] Запуск від root — додаю --no-sandbox (інакше Chromium відмовляється стартувати)."
  EXTRA_ARGS+=(--no-sandbox)
fi
# VM / старі GPU: ELECTRON_DISABLE_GPU=1 ./START_UI.sh
if [ "${ELECTRON_DISABLE_GPU:-0}" = "1" ]; then
  EXTRA_ARGS+=(--disable-gpu)
fi

# Прокидаємо додаткові аргументи: ./START_UI.sh --disable-gpu ...
EXTRA_ARGS+=("$@")

echo "[UI] Запуск: npm start -- ${EXTRA_ARGS[*]:-}"
npm start -- "${EXTRA_ARGS[@]:-}"
RC=$?
echo
echo "[UI] Exit code: $RC"
if [ "$RC" -ne 0 ]; then
  echo "[UI] Якщо вікно не з'явилось — дивіться рядки вище з [UI-ERR]/libnss/libatk."
  echo "[UI] Діагностика: ./START.sh check"
  echo "[UI] Fallback без Electron: python3 tools/auto_run.py --mode auto (tkinter-вікно)"
  echo "[UI] Потрібен python3-tk: sudo apt-get install -y python3-tk"
fi
read -r -p "[UI] Press Enter to close this window..." _ || true
exit "$RC"
