#!/usr/bin/env bash
# START_UI.sh - Launch CyberDeck Electron UI (standalone mode).
set -u
cd "$(dirname "$0")/ui"

# --- 0) Дисплей: без X11/Wayland Electron мовчки впаде ---
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "[UI] ERROR: no DISPLAY / WAYLAND_DISPLAY - nowhere to open a window."
  if [ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}" ]; then
    echo "[UI] You are over SSH without X-forwarding. For a window: ssh -X user@host"
  else
    echo "[UI] Run from a graphical terminal, not from a bare TTY/docker without -e DISPLAY."
  fi
  echo "[UI] Without a window: ./START_CONSOLE.sh"
  read -r -p "[UI] Press Enter to close this window..." _ || true
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[UI] ERROR: npm not found in PATH."
  echo "[UI] Install Node.js LTS 18+ from https://nodejs.org/ (apt gives an old node!)"
  echo "[UI] Or: bash ../scripts/setup_linux.sh"
  read -r -p "[UI] Press Enter to close this window..." _ || true
  exit 1
fi

# --- 1) Node <18 не потягне Electron 28 / npm install ---
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
  if [ "${NODE_MAJOR:-0}" -lt 18 ] 2>/dev/null; then
    echo "[UI] ERROR: node $(node --version 2>/dev/null) is too old, Node.js 18+ required."
    echo "[UI] apt installs an old node. Install LTS from https://nodejs.org"
    echo "[UI] or via NodeSource, then: rm -rf node_modules package-lock.json && npm install"
    read -r -p "[UI] Press Enter to close this window..." _ || true
    exit 1
  fi
fi

repair_electron_manual() {
  # Ручне дотягування бінарника Electron без npm-хуків.
  # Навіщо: Node 24/26 ламає вбудований extract-zip в Electron 28
  # (після install в dist/ лишається тільки locales/, бінарника нема),
  # а npm 12 ще й ріже postinstall. Качаємо офіційний zip з GitHub
  # через python3-stdlib (curl/unzip не потрібні) і розпаковуємо самі.
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  echo "[UI] Fetching the Electron binary manually (workaround for broken extract-zip on Node 24/26)..."
  EVER=$(python3 -c "import json;print(json.load(open('node_modules/electron/package.json')).get('version',''))" 2>/dev/null)
  if [ -z "${EVER:-}" ]; then
    EVER=$(python3 -c "import json;d=json.load(open('package-lock.json'));print(d.get('packages',{}).get('node_modules/electron',{}).get('version',''))" 2>/dev/null)
  fi
  if [ -z "${EVER:-}" ]; then EVER="28.3.3"; fi
  MACHINE=$(uname -m 2>/dev/null || echo x86_64)
  case "$MACHINE" in
    x86_64|amd64) EARCH="x64" ;;
    aarch64|arm64) EARCH="arm64" ;;
    *) echo "[UI] Unknown architecture: $MACHINE"; return 1 ;;
  esac
  EURL="https://github.com/electron/electron/releases/download/v${EVER}/electron-v${EVER}-linux-${EARCH}.zip"
  echo "[UI] Downloading: $EURL"
  if ! python3 - "$EURL" <<'PYEOF'
import sys, urllib.request, zipfile, os
url = sys.argv[1]
tmp = "/tmp/electron-fix.zip"
try:
    urllib.request.urlretrieve(url, tmp)
except Exception as e:
    print(f"[UI] download failed: {e}")
    sys.exit(1)
try:
    os.makedirs("node_modules/electron/dist", exist_ok=True)
    with zipfile.ZipFile(tmp, "r") as z:
        z.extractall("node_modules/electron/dist")
except Exception as e:
    print(f"[UI] extract failed: {e}")
    sys.exit(1)
# path.txt чекає index.js (npm start йде через нього)
try:
    pt = "node_modules/electron/path.txt"
    if not os.path.exists(pt):
        with open(pt, "w") as f:
            f.write("dist/electron")
except Exception:
    pass
print("[UI] manual extract OK")
PYEOF
  then
    return 1
  fi
  chmod +x node_modules/electron/dist/electron 2>/dev/null || true
  # Перевірка як в самому electron/index.js
  if node -e "require('./node_modules/electron')" 2>/dev/null; then
    echo "[UI] Manual repair succeeded."
    return 0
  fi
  return 1
}

install_ui_deps() {
  echo "[UI] First run: installing UI dependencies (npm install, 1-3 min)..."
  if ! npm install --no-audit --no-fund; then
    echo "[UI] ERROR: npm install failed - read the messages above."
    echo "[UI] Common causes on Linux: old node (<18), missing build-essential/python3."
    return 1
  fi
  # npm 12 блокить postinstall за замовчуванням (allowScripts).
  # Пробуємо всі відомі способи дозволу: новий синтаксис v12, старий alias,
  # прапорець --allow-scripts. `ls` питаємо ПІСЛЯ install.
  if [ ! -x "node_modules/electron/dist/electron" ]; then
    echo "[UI] Electron binary missing - trying to allow postinstall..."
    npm approve-scripts electron 2>/dev/null || npm install-scripts approve electron 2>/dev/null || true
    npm install --no-audit --no-fund || true
    if [ ! -x "node_modules/electron/dist/electron" ]; then
      npm install --no-audit --no-fund --allow-scripts=electron 2>/dev/null || true
    fi
    # Останній шанс без npm-хуків: запустити install.js вручну
    # (обходить і блок, і битий extract на нових Node).
    if [ ! -x "node_modules/electron/dist/electron" ] && [ -f "node_modules/electron/install.js" ]; then
      echo "[UI] Trying manual node install.js..."
      node node_modules/electron/install.js || true
    fi
    # Node 24/26 + Electron 28: install.js відпрацьовує мовчки, а в dist/
    # лишається тільки locales/. Дотягуємо офіційний zip самі.
    if [ ! -x "node_modules/electron/dist/electron" ]; then
      repair_electron_manual || true
    fi
  fi
  # Останній шанс: бита папка electron без бінарника — знести і поставити начисто.
  if [ ! -x "node_modules/electron/dist/electron" ] && [ -d "node_modules/electron" ]; then
    echo "[UI] Electron has no binary - reinstalling cleanly..."
    rm -rf node_modules/electron
    npm install --no-audit --no-fund || return 1
  fi
  if [ ! -x "node_modules/electron/dist/electron" ]; then
    echo "[UI] ERROR: Electron still failed to install."
    echo "[UI] Manually: cd ui && rm -rf node_modules package-lock.json && npm install"
    echo "[UI] npm 12: npm approve-scripts electron && npm install"
    echo "[UI] (old syntax: npm install-scripts approve electron && npm install)"
    echo "[UI] Node 26 breaks Electron 28 extraction (extract-zip): install Node 20 LTS"
    echo "[UI] via nvm (nvm install 20 && nvm use 20) and repeat install."
    echo "[UI] Or system Electron from Arch: sudo pacman -S electron"
    return 1
  fi
  # chrome-sandbox без setuid-root часто блокує старт Electron на Debian/Ubuntu
  if [ -f "node_modules/electron/dist/chrome-sandbox" ]; then
    chmod +x node_modules/electron/dist/electron 2>/dev/null || true
  fi
  return 0
}

if [ ! -x "node_modules/electron/dist/electron" ]; then
  if ! install_ui_deps; then
    read -r -p "[UI] Press Enter to close this window..." _ || true
    exit 1
  fi
fi

# --- 2) Root вимагає --no-sandbox, інакше "Running as root without --no-sandbox" ---
EXTRA_ARGS=()
if [ "$(id -u)" -eq 0 ]; then
  echo "[UI] Running as root - adding --no-sandbox (otherwise Chromium refuses to start)."
  EXTRA_ARGS+=(--no-sandbox)
fi
# VM / старі GPU: ELECTRON_DISABLE_GPU=1 ./START_UI.sh
if [ "${ELECTRON_DISABLE_GPU:-0}" = "1" ]; then
  EXTRA_ARGS+=(--disable-gpu)
fi

# Прокидаємо додаткові аргументи: ./START_UI.sh --disable-gpu ...
EXTRA_ARGS+=("$@")

echo "[UI] Launching: npm start -- ${EXTRA_ARGS[*]:-}"
npm start -- "${EXTRA_ARGS[@]:-}"
RC=$?
echo
echo "[UI] Exit code: $RC"
if [ "$RC" -ne 0 ]; then
  echo "[UI] If no window appeared - see lines above with [UI-ERR]/libnss/libatk."
  echo "[UI] Diagnostics: ./START.sh check"
  echo "[UI] Fallback without Electron: python3 tools/auto_run.py --mode auto (tkinter window)"
  if command -v pacman >/dev/null 2>&1; then
    echo "[UI] Need tk: sudo pacman -S --needed tk"
  else
    echo "[UI] Need python3-tk: sudo apt-get install -y python3-tk"
  fi
fi
read -r -p "[UI] Press Enter to close this window..." _ || true
exit "$RC"
