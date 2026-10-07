#!/usr/bin/env bash
# setup_linux.sh — One-time setup of a Linux host (Debian/Ubuntu).
# Run:  bash scripts/setup_linux.sh
# What it does:
#   1. apt-get install: python3, python3-tk, cmake, build-essential,
#      adb (android-tools-adb), scrcpy, nodejs, npm, usbip (linux-tools-generic)
#   2. Loads vhci-hcd kernel module (virtual USB, no Test Signing needed)
#   3. adb start-server + adb devices (check)
set -u
cd "$(dirname "$0")/.."

step() { echo; echo "[AUTO] $1"; }
ok()   { echo "[AUTO] OK: $1"; }
warn() { echo "[AUTO] WARN: $1"; }

if ! command -v apt-get >/dev/null 2>&1; then
  warn "apt-get not found — this script targets Debian/Ubuntu."
  echo "[AUTO] Install manually: python3, python3-tk, cmake, build-essential,"
  echo "[AUTO]   android-tools-adb, scrcpy, nodejs, npm, linux-tools-generic"
  exit 1
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if ! command -v sudo >/dev/null 2>&1; then
    warn "Need root or sudo to install packages."
    exit 1
  fi
  SUDO="sudo"
fi

step "Step 1/3: packages via apt-get..."
# shellcheck disable=SC2086
$SUDO apt-get update
# shellcheck disable=SC2086
$SUDO apt-get install -y python3 python3-tk cmake build-essential \
  android-tools-adb scrcpy nodejs npm linux-tools-generic

for t in python3 cmake adb scrcpy node npm; do
  if command -v "$t" >/dev/null 2>&1; then ok "$t installed."; else warn "$t still missing."; fi
done

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
adb start-server
adb devices -l

echo
echo "[AUTO] ===== Done. Next just: ./START.sh  (or python3 tools/auto_run.py) ====="
