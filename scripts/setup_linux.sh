#!/usr/bin/env bash
# setup_linux.sh — One-time setup of a Linux host (Debian/Ubuntu via apt,
# Arch via pacman).
# Run:  bash scripts/setup_linux.sh
# What it does:
#   1. Installs: python3, tkinter bindings, cmake, C++ compiler,
#      adb, scrcpy, Node.js LTS 18+, usbip tools + Electron sys-libs
#   2. Loads vhci-hcd kernel module (virtual USB, no Test Signing needed)
#   3. adb start-server + adb devices (check)
set -u
cd "$(dirname "$0")/.."

step() { echo; echo "[AUTO] $1"; }
ok()   { echo "[AUTO] OK: $1"; }
warn() { echo "[AUTO] WARN: $1"; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if ! command -v sudo >/dev/null 2>&1; then
    warn "Need root or sudo to install packages."
    exit 1
  fi
  SUDO="sudo"
fi

if command -v apt-get >/dev/null 2>&1; then
  PM="apt"
elif command -v pacman >/dev/null 2>&1; then
  PM="pacman"
else
  warn "Neither apt-get nor pacman found — this script targets Debian/Ubuntu and Arch."
  echo "[AUTO] Install manually: python3 (+tk), cmake, C++ compiler,"
  echo "[AUTO]   adb (android-tools), scrcpy, nodejs LTS 18+, npm, usbip tools"
  exit 1
fi

# Electron 28 системні бібліотеки (без них вікно падає з
# "error while loading shared libraries: libnss3.so" і т.д.)
ELECTRON_DEPS_APT="libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0"
ELECTRON_DEPS_PACMAN="nss atk at-spi2-atk cups libdrm libxkbcommon libxcomposite libxdamage libxfixes libxrandr mesa alsa-lib pango cairo"

ensure_node18_apt() {
  # apt з коробки дає Node 12 (Ubuntu 22.04) — для Electron 28 треба 18+.
  # Якщо node вже 18+ — нічого не робимо.
  if command -v node >/dev/null 2>&1; then
    MAJ=$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
    if [ "${MAJ:-0}" -ge 18 ] 2>/dev/null; then
      ok "node $(node --version) вже 18+, NodeSource не потрібен."
      return 0
    fi
    warn "node $(node --version 2>/dev/null) застарий — ставлю Node.js LTS 20 через NodeSource..."
  else
    echo "[AUTO] Node.js не знайдено — ставлю LTS 20 через NodeSource..."
  fi
  if ! command -v curl >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    $SUDO apt-get install -y curl ca-certificates gnupg || true
  fi
  # shellcheck disable=SC2086
  curl -fsSL https://deb.nodesource.com/setup_20.x | $SUDO -E bash - || {
    warn "NodeSource не вдався — ставлю nodejs з apt як є (може бути старий!)."
    return 1
  }
  return 0
}

step "Step 1/3: packages via $PM..."
if [ "$PM" = "apt" ]; then
  # shellcheck disable=SC2086
  $SUDO apt-get update
  ensure_node18_apt || true
  # shellcheck disable=SC2086
  $SUDO apt-get install -y python3 python3-tk cmake build-essential \
    android-tools-adb scrcpy nodejs linux-tools-generic $ELECTRON_DEPS_APT
  # npm інколи йде окремим пакетом в мінімальних образах
  if ! command -v npm >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    $SUDO apt-get install -y npm || true
  fi
else
  # shellcheck disable=SC2086
  $SUDO pacman -S --needed --noconfirm python tk cmake base-devel \
    android-tools scrcpy nodejs npm usbip $ELECTRON_DEPS_PACMAN
fi

for t in python3 cmake adb scrcpy node npm; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t installed ($($t --version 2>&1 | head -n1))."; else warn "$t still missing."; fi
done
# tkinter окремо — без нього вікна не буде, тільки консоль
if python3 -c "import tkinter" 2>/dev/null; then
  ok "python3-tk (tkinter) OK."
else
  warn "tkinter НЕМА — вікна не буде. Доставте: sudo apt-get install -y python3-tk  (Arch: sudo pacman -S --needed tk)"
fi
if command -v node >/dev/null 2>&1; then
  MAJ=$(node -p "process.versions.node.split('.')[0]" 2>/dev/null || echo 0)
  if [ "${MAJ:-0}" -lt 18 ] 2>/dev/null; then
    warn "node $(node --version) < 18 — Electron 28 / npm install можуть впасти. Поставте LTS з https://nodejs.org"
  fi
fi

step "Step 2/3: vhci-hcd kernel module (virtual USB)..."
if lsmod 2>/dev/null | grep -q vhci_hcd; then
  ok "vhci_hcd already loaded."
else
  # shellcheck disable=SC2086
  if $SUDO modprobe vhci-hcd 2>/dev/null; then
    ok "vhci_hcd loaded."
  else
    warn "Could not load vhci_hcd (Secure Boot may block unsigned modules)."
    echo "[AUTO] Screen mirroring via scrcpy over TCP works WITHOUT it."
  fi
fi

step "Step 3/3: adb start-server + adb devices (check)..."
adb start-server || true
adb devices -l || true

echo
echo "[AUTO] Перевірка UI: python3 tools/auto_run.py --check  (розділ 'вікна (GUI)')"
echo "[AUTO] ===== Done. Next just: ./START.sh  (or python3 tools/auto_run.py) ====="
