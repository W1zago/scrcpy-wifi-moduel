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
        # В авто-режимі не падаємо одразу, а доустановлюємо самі (швидкий пакет, 1-3 хв).
        # Може вискочити UAC-запит — це нормально, натисніть "Так" один раз.
        log("cmake нема — доустановлюю САМ через winget (1-3 хв, може попросити UAC)...")
        install_deps()
    if not shutil.which("cmake"):
        log("ERROR: 'cmake' так і не з'явився (можливо скасовано UAC або нема інтернету).")
        log("Варіант 1 (автоматом): запустіть з прапорцем --install-deps")
        log("Варіант 2 (вручну): winget install Kitware.CMake")
        log("Варіант 3 (все разом): powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1")
        return None, None
    if not check_compiler():
        log("ERROR: cmake є, але компілювати C++ нічим (MSVC не знайдено) — збірка неможлива.")
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
        # Серійник без ":" і без поля transport: — це фізичний USB (старі adb не пишуть transport:).
        transport = tm.group(1) if tm else ("tcp" if ":" in serial else "usb")
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

def _adb_shell(out_lines, cmd):
    """Виконати adb shell ..., повернути stdout (або '' якщо USB-лінк мертвий)."""
    try:
        r = subprocess.run(["adb", "shell"] + cmd, capture_output=True, text=True,
                           timeout=15, encoding="utf-8", errors="replace")
        out = r.stdout or ""
        if "no devices" in out or "device not found" in out or "device offline" in out:
            return ""
        return out
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""

def _is_usable_lan(ip):
    """Відсікаємо loopback/link-local; решта — кандидати (навіть .1, шлюз теж буває телефоном)."""
    if ip.startswith("127.") or ip.startswith("169.254."):
        return False
    return True

def detect_phone_ips():
    """Зібрати ВСІ кандидати на IP телефону (порядок: спочатку wlan0, потім src з route).

    ВАЖЛИВО: викликати ДО 'adb tcpip', поки USB-лінк живий — після перезапуску
    adbd в TCP-режим 'adb shell' вже не працює і можна схопити сміття на кшталт
    шлюзу мобільної підмережі (10.x.x.1), до якого connect висить до таймауту.
    """
    cands = []
    out = _adb_shell(None, ["ip", "addr", "show", "wlan0"])
    for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", out):
        ip = m.group(1)
        if _is_usable_lan(ip) and ip not in cands:
            cands.append(ip)
    out = _adb_shell(None, ["ip", "route"])
    for m in re.finditer(r"src\s+(\d+\.\d+\.\d+\.\d+)", out):
        ip = m.group(1)
        if _is_usable_lan(ip) and ip not in cands:
            cands.append(ip)
    if cands:
        log(f"Кандидати на IP телефону: {', '.join(cands)}")
    return cands

def detect_phone_ip():
    """Сумісність: перший кандидат або ''."""
    cands = detect_phone_ips()
    return cands[0] if cands else ""

def _tcp_device_ready(serial, adb_port):
    """Чи видно serial як 'device' в adb devices?"""
    for s, st, t in adb_devices():
        if s == serial and st == "device":
            return True
        # adb інколи показує той самий IP з іншим портом — приймаємо будь-який device з цим IP
        if s.split(":")[0] == serial.split(":")[0] and st == "device" and ":" in s:
            return True
    return False

def adb_connect_verify(ip, adb_port, attempts=3):
    """adb connect + ПЕРЕВІРКА що пристрій реально в стані device (з ретраями).

    Ретраї потрібні бо adbd на телефоні після 'tcpip' рестартиться 3-5с і перший
    connect часто падає/висить, хоча IP правильний.
    """
    serial = f"{ip}:{adb_port}"
    for i in range(1, attempts + 1):
        log(f"Спроба {i}/{attempts}: adb connect {serial} ...")
        run(["adb", "connect", serial], timeout=30)
        # даємо adbd секунду-дві на handshake (A_CNXN)
        for _ in range(4):
            time.sleep(1)
            if _tcp_device_ready(serial, adb_port):
                log(f"Підключено і підтверджено: {serial} (device).")
                return True
        log(f"Поки не device, пробую ще...")
    log(f"Не вдалося отримати device на {serial} за {attempts} спроби.")
    return False

def diagnose_connect_fail(ip, adb_port):
    log("")
    log("ДІАГНОСТИКА: adb connect не вдався. Перевірте по пунктах:")
    log(f"  1. Телефон і ПК в ОДНІЙ Wi-Fi мережі? (IP {ip} має бути з вашої LAN, напр. 192.168.x.x)")
    log(f"     Якщо IP схоже на мобільну підмережу (10.x) — увімкніть Wi-Fi на телефоні.")
    log(f"  2. На телефоні: Параметри -> Для розробників -> Бездротове налагодження (Wireless debugging) УВІМКНЕНО?")
    log(f"  3. AP isolation в роутері вимкнено? (деякі роутери блокують Wi-Fi клієнтів один від одного)")
    log(f"  4. Брандмауер Windows не ріже порт {adb_port}? Тест: ping {ip}")
    log(f"  5. Або задайте IP вручну: --phone-ip <IP з Налаштування -> Про телефон -> Статус>")
    log("")

STATE_FILE = ROOT / "build" / "last_phone.txt"

def save_last_phone(serial):
    """Запам'ятати останній робочий пристрій (щоб пережити tcpip-режим між запусками)."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(serial.strip(), encoding="utf-8")
    except Exception:
        pass

def load_last_phone():
    try:
        s = STATE_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if s:
            return s
    except Exception:
        pass
    return ""

def setup_phone_wifi(adb_port, phone_ip_override, dry_run, devs=None):
    """Повний ланцюжок USB->tcpip->connect. Повертає (adb_host, demo_needed)."""
    if dry_run:
        log("(dry-run) пропустив би tcpip/connect")
        return "127.0.0.1", True
    # adb server вже запущено викликачем (main); список або передано, або читаємо свіжий
    if devs is None:
        devs = print_adb_devices()
    # вже є TCP device?
    for s, st, t in devs:
        if ":" in s and st == "device":
            log(f"Вже є Wi-Fi пристрій {s} — tcpip пропускаю.")
            save_last_phone(s)
            return s.split(":")[0], False
    if phone_ip_override:
        log(f"Задано --phone-ip={phone_ip_override}, роблю adb connect САМ...")
        ok = adb_connect_verify(phone_ip_override, adb_port)
        if ok:
            save_last_phone(f"{phone_ip_override}:{adb_port}")
        return phone_ip_override, not ok
    has_usb = any(":" not in s and st == "device" for s, st, t in devs)
    if not has_usb:
        # Телефон міг залишитись в tcpip-режимі з минулого запуску (USB тоді мертвий).
        # Пробуємо реанімацію: reconnect + останній відомий IP — все САМ.
        log("USB-пристроїв нема. Пробую реанімацію (телефон міг залишитись в tcpip-режимі)...")
        run(["adb", "reconnect"], timeout=30)
        time.sleep(2)
        devs = print_adb_devices()
        for s, st, t in devs:
            if ":" in s and st == "device":
                log(f"Реанімація вдалася: {s}.")
                save_last_phone(s)
                return s.split(":")[0], False
        last = load_last_phone()
        if last:
            log(f"Пробую останній відомий пристрій САМ: {last} ...")
            lip = last.split(":")[0]
            lport = int(last.split(":")[1]) if ":" in last and last.split(":")[1].isdigit() else adb_port
            if adb_connect_verify(lip, lport, attempts=2):
                save_last_phone(f"{lip}:{lport}")
                return lip, False
        log("Реанімація не вдалася. Варіанти:")
        log("  - Перевставте USB-кабель і (якщо телефон завис в tcpip) перезавантажте телефон, потім START.bat")
        log("  - Або задайте IP вручну: --phone-ip <IP з Налаштування -> Про телефон -> Статус>")
        log("  - Або демо без телефону: START.bat demo")
        return "127.0.0.1", True
    # КРОК 1: IP визначаємо ДО tcpip, поки USB живий
    log("Визначаю IP телефону ДО перезапуску adbd (поки USB живий)...")
    candidates = detect_phone_ips()
    log(f"Знайдено USB — перемикаю в Wi-Fi САМ: adb tcpip {adb_port} ...")
    run(["adb", "tcpip", str(adb_port)], timeout=30)
    # adbd рестартиться 3-5с — чекаємо довше ніж раніше (було 2с, цього мало)
    log("Чекаю 4с поки adbd перезапуститься в TCP-режимі...")
    time.sleep(4)
    if not candidates:
        log("До tcpip IP не визначився — пробую ще раз (може не вийти, USB вже мертвий)...")
        candidates = detect_phone_ips()
    if not candidates:
        log("Не визначив IP. Передайте вручну: --phone-ip <IP телефону>")
        return "127.0.0.1", True
    # КРОК 2: пробуємо КОЖНОГО кандидата з перевіркою
    for ip in candidates:
        if adb_connect_verify(ip, adb_port):
            save_last_phone(f"{ip}:{adb_port}")
            return ip, False
    diagnose_connect_fail(candidates[0], adb_port)
    return "127.0.0.1", True

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

# ---------------- залежності ----------------

WINGET_PKGS = (
    ("cmake", "Kitware.CMake"),
    ("adb", "Google.PlatformTools"),
    ("scrcpy", "Genymobile.scrcpy"),
)

def install_deps():
    """Самому доустановити відсутнє через winget (швидкі пакети, без компілятора)."""
    if os.name != "nt" or not shutil.which("winget"):
        log("winget не знайдено — встановіть залежності вручну (див. README).")
        return
    for tool, pkg in WINGET_PKGS:
        if shutil.which(tool):
            continue
        log(f"Доустанавливаю {tool} САМ: winget install {pkg} ... (це займе 1-3 хв)")
        run(["winget", "install", "--accept-source-agreements", "--accept-package-agreements",
             "-e", "--id", pkg], timeout=600)
    # оновити PATH поточної сесії з машинного/користувацького оточення
    log("Оновлюю PATH сесії...")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + "
                            "[Environment]::GetEnvironmentVariable('Path','User')"],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        if r.stdout.strip():
            os.environ["Path"] = r.stdout.strip() + ";" + os.environ.get("Path", "")
    except Exception:
        pass

def check_compiler():
    """Чи є чим компілювати C++ (MSVC). Повертає True якщо компілятор знайдено."""
    if shutil.which("cl") or shutil.which("msbuild"):
        log("  compiler (MSVC): OK")
        return True
    # vswhere — точніший спосіб знайти Visual Studio
    vswhere = (os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
               + r"\Microsoft Visual Studio\Installer\vswhere.exe")
    try:
        r = subprocess.run([vswhere, "-requires", "Microsoft.VisualStudio.Component.VC.Tools",
                            "-property", "installationPath"],
                           capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
        if r.stdout.strip():
            log(f"  compiler (MSVC): OK ({r.stdout.strip().splitlines()[0]})")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    log("  compiler (MSVC): НЕМА — для збірки C++ потрібен Visual Studio Build Tools з C++.")
    log("  Встановіть САМІ однією командою (3-8 ГБ, 10-30 хв, одноразово):")
    log("    winget install Microsoft.VisualStudio.2022.BuildTools --override "
        "\"--quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended\"")
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
    ap.add_argument("--install-deps", action="store_true",
                    help="Самому доустановити відсутнє (cmake/adb/scrcpy) через winget")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    print("=== Virtual USB Cable — АВТОПІЛОТ (сам вводить всі команди) ===", flush=True)

    # [0] залежності (+ опційне довстановлення)
    if args.install_deps and not args.dry_run:
        install_deps()
    log("Крок 0: перевірка залежностей...")
    has_adb = shutil.which("adb") is not None
    has_scrcpy = shutil.which("scrcpy") is not None
    has_cmake = shutil.which("cmake") is not None
    has_py = True
    log(f"  adb: {'OK' if has_adb else 'НЕМА (winget install Google.PlatformTools або --install-deps)'}")
    log(f"  scrcpy: {'OK' if has_scrcpy else 'НЕМА (winget install Genymobile.scrcpy або --install-deps)'}")
    log(f"  cmake: {'OK' if has_cmake else 'НЕМА (winget install Kitware.CMake або --install-deps)'}")
    log(f"  python: OK ({sys.version.split()[0]})")
    if args.check:
        check_compiler()
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
            adb_host, need_demo = setup_phone_wifi(args.adb_port, args.phone_ip, args.dry_run,
                                                       devs if not args.dry_run else None)
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
        # FALLBACK: бінарників нема (нема toolchain), АЛЕ телефон вже по Wi-Fi в device —
        # даємо робоче дзеркало ВЖЕ ЗАРАЗ через звичайний scrcpy по TCP.
        # Це НЕ віртуальний USB (для нього добудуйте toolchain), але екран/керування працюють.
        tcp_serial = ""
        if not use_fake_adb and has_adb and not args.dry_run:
            for s, st, t in adb_devices():
                if ":" in s and st == "device":
                    tcp_serial = s
                    break
        if tcp_serial and not args.no_scrcpy and has_scrcpy:
            log("")
            log("===== FALLBACK (тимчасово, поки нема toolchain) =====")
            log(f"Телефон {tcp_serial} вже по Wi-Fi — запускаю ЗВИЧАЙНИЙ scrcpy по TCP:")
            log(f"  scrcpy -s {tcp_serial}")
            log("Це звичайний TCP-режим scrcpy (НЕ віртуальний USB).")
            log("Для повного модуля (віртуальний USB) добудуйте toolchain:")
            log("  winget install Kitware.CMake  (або START.bat install)")
            log("  + MSVC Build Tools з C++ (команда була в --check)")
            log("Потім перезапустіть START.bat — підхопить телефон сам (IP запам'ятав).")
            log("======================================================")
            log("")
            fb = None
            try:
                fb = subprocess.Popen(["scrcpy", "-s", tcp_serial])
                log(f"scrcpy запущено (pid={fb.pid}). Ctrl+C щоб зупинити.")
                while True:
                    time.sleep(1)
                    if fb.poll() is not None:
                        log("scrcpy завершився.")
                        break
            except KeyboardInterrupt:
                log("Ctrl+C — зупиняю scrcpy...")
            finally:
                try:
                    if fb is not None and fb.poll() is None:
                        fb.terminate()
                except Exception:
                    pass
            return 0
        log("ERROR: бінарників нема і зібрати не вдалося.")
        log("Варіанти: START.bat install (cmake сам) + MSVC Build Tools, або --skip-build якщо бінарники десь інде.")
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
