#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_run.py — ОДНА команда замість всіх ручних команд.

Замість того щоб вручну вводити:
    bcdedit /set testsigning on
    cmake -B build ... / cmake --build ...
    adb start-server / adb tcpip 5555 / adb shell ip ... / adb connect ... / adb devices
    agent_sender.exe --simulate-adb ...
    agent_receiver.exe --server ... --vhci/--simulate
    scrcpy --select-usb

Просто запустіть:
    python tools/auto_run.py              (авто: спробує реальний телефон, інакше демо)
    python tools/auto_run.py --mode demo  (без телефону, все симулюється)
    python tools/auto_run.py --mode real  (вимагає телефон)
    START.bat                             (подвійний клік, те саме що --mode auto)

Що робить скрипт САМ (по кроках):
  [0] Перевірка залежностей (adb, scrcpy, cmake) — за потреби підказує winget-команду
  [1] adb start-server
  [2] Пошук телефону: adb devices
      - якщо є USB device  -> adb tcpip 5555 -> визначення IP -> adb connect IP:5555
      - якщо є TCP device  -> використовуємо як є
      - якщо нема нічого   -> демо-режим (fake adbd)
  [3] Збірка (cmake) якщо бінарників нема (можна пропустити --skip-build)
  [4] Запуск agent_sender (підпроцес, лог з префіксом [SENDER])
  [5] Запуск agent_receiver --auto (підпроцес, [RECEIVER]) — він САМ зробить
      adb devices + автозапуск scrcpy НЕ робимо тут, щоб не дублювати
  [6] Очікування + періодичний adb devices
  [7] Автозапуск scrcpy (якщо --no-scrcpy не передано)
  [8] Ctrl+C -> коректне завершення всіх процесів

Коди виходу: 0=OK (завершено користувачем), 1=критична помилка, 2=нема телефону в --mode real.
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Windows-консоль часто в cp866/cp1251 -> вмикаємо UTF-8 щоб українська не була "кракозябрами"
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if os.name == "nt":
        os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
SENDER_NAMES = ["agent_sender.exe", "agent_sender"]
RECEIVER_NAMES = ["agent_receiver.exe", "agent_receiver"]
DEFAULT_SENDER_PORT = 22777
DEFAULT_ADB_PORT = 5555

# ---------------- логування ----------------

def log(msg=""):
    print(f"[AUTO] {msg}", flush=True)

def log_cmd(cmd):
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(c) for c in cmd)
    print(f"[AUTO] $ {cmd}", flush=True)

def run(cmd, shell=False, timeout=30, check=False, capture=True):
    """Виконати команду, показати її, повернути (rc, output)."""
    log_cmd(cmd)
    try:
        r = subprocess.run(
            cmd, shell=shell, timeout=timeout,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        out = r.stdout or ""
        if out.strip():
            for line in out.strip().splitlines()[:30]:
                print(f"[AUTO]   | {line}", flush=True)
            if len(out.strip().splitlines()) > 30:
                print("[AUTO]   | ...", flush=True)
        return r.returncode, out
    except subprocess.TimeoutExpired:
        log("   | TIMEOUT")
        return 124, ""
    except FileNotFoundError as e:
        log(f"   | NOT FOUND: {e}")
        return 127, ""

# ---------------- пошук бінарників ----------------

def find_binary(names):
    """Шукає зібраний бінарник: build/Release, build/Debug, build/."""
    search_dirs = [BUILD_DIR / "Release", BUILD_DIR / "Debug", BUILD_DIR,
                   ROOT / "build" / "Release"]
    for d in search_dirs:
        for n in names:
            p = d / n
            if p.is_file():
                return str(p)
    # fallback: в PATH
    for n in names:
        w = shutil.which(n.replace(".exe", ""))
        if w:
            return w
    return None

def ensure_built(skip_build, dry_run):
    sender = find_binary(SENDER_NAMES)
    receiver = find_binary(RECEIVER_NAMES)
    if sender and receiver:
        log(f"Бінарники знайдено: sender={sender} receiver={receiver}")
        return sender, receiver
    if skip_build:
        log("Бінарників нема і --skip-build: продовжую без них (dry-run / тільки adb).")
        return sender, receiver
    if dry_run:
        log("Бінарників нема (dry-run: збірку пропускаю).")
        return None, None
    log("Бінарників нема — запускаю збірку САМ (cmake)...")
    if not shutil.which("cmake"):
        log("ERROR: 'cmake' не знайдено.")
        log("Встановіть САМІ однією командою:")
        log("  winget install Kitware.CMake")
        log("Або запустіть: powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1")
        return None, None
    rc, _ = run(["cmake", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], timeout=180)
    if rc != 0:
        log("ERROR: cmake configure не вдався.")
        return None, None
    rc, _ = run(["cmake", "--build", "build", "--config", "Release"], timeout=600)
    if rc != 0:
        log("ERROR: збірка не вдалася.")
        return None, None
    sender = find_binary(SENDER_NAMES)
    receiver = find_binary(RECEIVER_NAMES)
    log(f"Збірка готова: sender={sender} receiver={receiver}")
    return sender, receiver

# ---------------- adb ----------------

def adb_devices():
    """Повертає список (serial, state, transport)."""
    try:
        r = subprocess.run(["adb", "devices", "-l"], capture_output=True,
                           text=True, timeout=15, encoding="utf-8", errors="replace")
        out = r.stdout or ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    devs = []
    for line in out.splitlines():
        if "List of devices" in line or not line.strip():
            continue
        m = re.split(r"\s+", line.strip(), maxsplit=1)
        if len(m) < 2:
            continue
        serial, rest = m[0], m[1]
        if rest.startswith("device"):
            state = "device"
        elif "offline" in rest:
            state = "offline"
        elif "unauthorized" in rest:
            state = "unauthorized"
        else:
            state = rest.split()[0]
        tm = re.search(r"transport:(\w+)", rest)
        transport = tm.group(1) if tm else ("tcp" if ":" in serial else "unknown")
        devs.append((serial, state, transport))
    return devs

def print_adb_devices():
    devs = adb_devices()
    log(f"adb devices: {len(devs)} знайдено")
    for s, st, t in devs:
        log(f"  - {s}  state={st} transport={t}")
    if not devs:
        log("  (порожньо — телефон не підключено)")
    return devs

def detect_phone_ip():
    for cmd in (["adb", "shell", "ip", "route"], ["adb", "shell", "ip", "addr", "show", "wlan0"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                               encoding="utf-8", errors="replace")
            out = r.stdout or ""
            m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
            if m:
                log(f"IP телефону: {m.group(1)}")
                return m.group(1)
            m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", out)
            if m:
                log(f"IP телефону (wlan0): {m.group(1)}")
                return m.group(1)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return ""

def setup_phone_wifi(adb_port, phone_ip_override, dry_run):
    """Повний ланцюжок USB->tcpip->connect. Повертає (adb_host, demo_needed)."""
    if dry_run:
        log("(dry-run) пропустив би tcpip/connect")
        return "127.0.0.1", True
    run(["adb", "start-server"], timeout=30)
    devs = print_adb_devices()
    # вже є TCP device?
    for s, st, t in devs:
        if ":" in s and st == "device":
            log(f"Вже є Wi-Fi пристрій {s} — tcpip пропускаю.")
            return s.split(":")[0], False
    if phone_ip_override:
        log(f"Задано --phone-ip={phone_ip_override}, роблю adb connect САМ...")
        run(["adb", "connect", f"{phone_ip_override}:{adb_port}"], timeout=30)
        return phone_ip_override, False
    has_usb = any(":" not in s and st == "device" for s, st, t in devs)
    if not has_usb:
        log("USB-пристрою нема — потрібен демо-режим або --phone-ip.")
        return "127.0.0.1", True
    log(f"Знайдено USB — перемикаю в Wi-Fi САМ: adb tcpip {adb_port} ...")
    run(["adb", "tcpip", str(adb_port)], timeout=30)
    time.sleep(2)
    ip = detect_phone_ip()
    if not ip:
        log("Не визначив IP. Підказка: adb shell ip route  (або передайте --phone-ip)")
        return "127.0.0.1", True
    run(["adb", "connect", f"{ip}:{adb_port}"], timeout=30)
    return ip, False

def check_vhci():
    """Чи є драйвер vhci (для --vhci)."""
    if os.name != "nt":
        return False
    for dev in (r"\\.\vhci", r"\\.\USBIP_VHCI"):
        try:
            import ctypes
            GENERIC = 0x80000000 | 0x40000000
            h = ctypes.windll.kernel32.CreateFileW(
                dev, GENERIC, 0, None, 3, 0, None)
            if h and h != -1 and int(h) != -1:
                ctypes.windll.kernel32.CloseHandle(h)
                log(f"VHCI драйвер знайдено ({dev}).")
                return True
        except Exception:
            pass
    log("VHCI драйвер НЕ знайдено — буде SIMULATION (без Device Manager).")
    log("Для справжнього USB: scripts/setup_windows.ps1  (встановить usbip-win2)")
    return False

# ---------------- процеси ----------------

class ProcMon:
    """Запуск підпроцесу з префіксом логу, коректне завершення."""
    def __init__(self, tag, cmd):
        self.tag = tag
        self.cmd = cmd
        self.proc = None
        self.thread = None

    def start(self, dry_run=False):
        log_cmd(self.cmd)
        if dry_run:
            log(f"({self.tag} dry-run: не запускаю)")
            return True
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except FileNotFoundError:
            log(f"[{self.tag}] НЕ ЗНАЙДЕНО: {self.cmd[0]}")
            return False
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()
        return True

    def _pump(self):
        try:
            for line in self.proc.stdout:
                print(f"[{self.tag}] {line.rstrip()}", flush=True)
        except Exception:
            pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc and self.alive():
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            except Exception:
                pass

# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser(
        description="Virtual USB Cable — автопілот (сам вводить всі команди).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Приклади:\n"
               "  python tools/auto_run.py                 # авто: телефон якщо є, інакше демо\n"
               "  python tools/auto_run.py --mode demo     # без телефону\n"
               "  python tools/auto_run.py --mode real --phone-ip 192.168.1.100\n"
               "  python tools/auto_run.py --check         # тільки перевірка залежностей\n"
               "  python tools/auto_run.py --dry-run       # тільки показати команди\n")
    ap.add_argument("--mode", choices=["auto", "demo", "real"], default="auto")
    ap.add_argument("--phone-ip", default="", help="IP телефону (якщо автовизначення не спрацювало)")
    ap.add_argument("--adb-port", type=int, default=DEFAULT_ADB_PORT)
    ap.add_argument("--sender-port", type=int, default=DEFAULT_SENDER_PORT)
    ap.add_argument("--with-vhci", action="store_true", help="Спробувати реальний vhci.sys (потрібен usbip-win2)")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--no-scrcpy", action="store_true", help="Не запускати scrcpy автоматично")
    ap.add_argument("--scrcpy-args", default="--select-usb")
    ap.add_argument("--check", action="store_true", help="Тільки перевірити залежності і вийти")
    ap.add_argument("--dry-run", action="store_true", help="Тільки показати команди, нічого не запускати")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print("=== Virtual USB Cable — АВТОПІЛОТ (сам вводить всі команди) ===", flush=True)

    # [0] залежності
    log("Крок 0: перевірка залежностей...")
    has_adb = shutil.which("adb") is not None
    has_scrcpy = shutil.which("scrcpy") is not None
    has_cmake = shutil.which("cmake") is not None
    has_py = True
    log(f"  adb: {'OK' if has_adb else 'НЕМА (winget install Google.PlatformTools)'}")
    log(f"  scrcpy: {'OK' if has_scrcpy else 'НЕМА (winget install Genymobile.scrcpy)'}")
    log(f"  cmake: {'OK' if has_cmake else 'НЕМА (winget install Kitware.CMake) — або --skip-build'}")
    log(f"  python: OK ({sys.version.split()[0]})")
    if args.check:
        return 0 if (has_adb and has_scrcpy) else 1
    if not has_adb and args.mode == "real":
        log("ERROR: для --mode real потрібен adb.")
        return 1

    # [1-2] телефон (крім demo)
    adb_host = "127.0.0.1"
    use_fake_adb = False
    if args.mode == "demo":
        log("Крок 1-2: --mode demo — телефон не потрібен, буду симулювати (fake adbd).")
        use_fake_adb = True
    else:
        if not has_adb:
            log("adb нема — перемикаюсь в демо-режим.")
            use_fake_adb = True
        else:
            log("Крок 1: adb start-server (САМ)...")
            if not args.dry_run:
                run(["adb", "start-server"], timeout=30)
            else:
                log_cmd(["adb", "start-server"])
            log("Крок 2: пошук телефону (САМ)...")
            if not args.dry_run:
                devs = print_adb_devices()
            else:
                devs = []
                log_cmd(["adb", "devices", "-l"])
            if args.mode == "real" and not devs and not args.phone_ip:
                log("ERROR: --mode real, але 'adb devices' порожньо і --phone-ip не задано.")
                log("Підключіть телефон по USB (і підтвердіть RSA на екрані) або задайте --phone-ip.")
                return 2
            adb_host, need_demo = setup_phone_wifi(args.adb_port, args.phone_ip, args.dry_run)
            if args.mode == "real" and need_demo and not args.dry_run:
                # в real пробуємо ще раз показати
                devs2 = print_adb_devices()
                if not any(st == "device" for _, st, _ in devs2):
                    log("ERROR: телефон так і не з'явився. Перевірте USB/Wi-Fi і RSA-підтвердження.")
                    return 2
            use_fake_adb = need_demo and (args.mode != "real")

    # [3] збірка
    log("Крок 3: збірка (якщо треба, САМ)...")
    sender_bin, receiver_bin = ensure_built(args.skip_build, args.dry_run)
    if args.dry_run:
        log("(dry-run) далі тільки показую команди запуску:")
        log_cmd(f"agent_sender --adb {adb_host}:{args.adb_port} --listen 0.0.0.0:{args.sender_port}"
                + (" --simulate-adb" if use_fake_adb else ""))
        log_cmd(f"agent_receiver --server 127.0.0.1:{args.sender_port} --auto --simulate"
                + (" --vhci" if args.with_vhci else ""))
        if not args.no_scrcpy:
            log_cmd(f"scrcpy {args.scrcpy_args}")
        log("DRY-RUN готово.")
        return 0
    if not sender_bin or not receiver_bin:
        log("ERROR: бінарників нема і зібрати не вдалося.")
        log("Варіанти: встановіть cmake (winget install Kitware.CMake) або запустіть з --skip-build якщо бінарники десь інде.")
        return 1

    # VHCI?
    vhci_ok = check_vhci() if os.name == "nt" else False
    use_vhci = bool(args.with_vhci and vhci_ok)
    if args.with_vhci and not vhci_ok:
        log("--with-vhci запитано, але драйвера нема — продовжую в SIMULATION.")

    # [4] sender
    log("Крок 4: запускаю agent_sender САМ...")
    sender_cmd = [sender_bin, "--adb", f"{adb_host}:{args.adb_port}",
                  "--listen", f"0.0.0.0:{args.sender_port}"]
    if use_fake_adb:
        sender_cmd.append("--simulate-adb")
    # sender --auto тільки для реального телефону (з fake він не потрібен)
    if not use_fake_adb:
        sender_cmd.append("--auto")
        if args.phone_ip:
            sender_cmd += ["--phone-ip", args.phone_ip]
    sender = ProcMon("SENDER", sender_cmd)
    if not sender.start():
        return 1
    time.sleep(1.0)
    if not sender.alive():
        log("WARNING: sender одразу завершився. Можливо порт зайнято або adb недоступний.")
        log("Пробую продовжити — receiver покаже помилку підключення.")

    # [5] receiver (він САМ зробить adb devices; scrcpy запускаємо тут щоб не дублювати)
    log("Крок 5: запускаю agent_receiver САМ (--auto, але scrcpy запущу сам централізовано)...")
    receiver_cmd = [receiver_bin, "--server", f"127.0.0.1:{args.sender_port}", "--auto", "--no-scrcpy"]
    # handshake-симуляція ТІЛЬКИ для демо; з реальним телефоном handshake прийде з мережі
    if use_fake_adb:
        receiver_cmd.append("--simulate")
    if use_vhci:
        receiver_cmd.append("--vhci")
    receiver = ProcMon("RECEIVER", receiver_cmd)
    if not receiver.start():
        sender.stop()
        return 1

    # [6] очікування + перевірка
    log("Крок 6: чекаю 3с і перевіряю adb devices САМ...")
    for i in range(3):
        time.sleep(1)
        if not sender.alive():
            log("WARNING: sender зупинився.")
        if not receiver.alive():
            log("WARNING: receiver зупинився.")
    if has_adb and not args.dry_run:
        print_adb_devices()

    # [7] scrcpy
    scrcpy_proc = None
    if not args.no_scrcpy:
        if not has_scrcpy:
            log("Крок 7: scrcpy НЕМА — пропускаю автозапуск. Встановіть: winget install Genymobile.scrcpy")
        else:
            log(f"Крок 7: запускаю scrcpy САМ: scrcpy {args.scrcpy_args} ...")
            try:
                scrcpy_proc = subprocess.Popen(["scrcpy"] + args.scrcpy_args.split())
                log(f"scrcpy запущено (pid={scrcpy_proc.pid}).")
            except FileNotFoundError:
                log("Не вдалося запустити scrcpy.")
    else:
        log("Крок 7: --no-scrcpy — scrcpy не запускаю.")

    log("")
    log("===== ВСЕ ЗАПУЩЕНО АВТОМАТИЧНО. Нічого вводити не треба. Ctrl+C щоб зупинити. =====")
    log(f"Sender: agent_sender (порт {args.sender_port}) | Receiver: agent_receiver --auto | scrcpy: {'on' if scrcpy_proc else 'off'}")

    # [8] моніторинг до Ctrl+C
    try:
        while True:
            time.sleep(1)
            if not sender.alive() and not receiver.alive():
                log("Обидва процеси зупинились — виходжу.")
                break
    except KeyboardInterrupt:
        log("")
        log("Ctrl+C — зупиняю все САМ...")
    finally:
        sender.stop()
        receiver.stop()
        if scrcpy_proc and scrcpy_proc.poll() is None:
            try:
                scrcpy_proc.terminate()
            except Exception:
                pass
        # фінальний adb devices щоб показати стан
        if has_adb:
            try:
                print_adb_devices()
            except Exception:
                pass
        log("Зупинено. До побачення!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
