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
  [0] Перевірка залежностей (adb, scrcpy, cmake) — якщо чогось бракує,
      САМ пропонує довстановити через winget і питає підтвердження [Y/n]
      (Enter=так, --install-deps/--yes=без питань, --no-install-deps=тільки підказки)
  [1] adb start-server
  [2] Пошук телефону: adb devices
      - якщо є USB device  -> adb tcpip 5555 -> визначення IP -> adb connect IP:5555
      - якщо є TCP device  -> використовуємо як є
      - якщо кабелю НЕМА   -> вискакує ВІКНО зі знайденими телефонами (як Bluetooth):
        скан у фоні, список поповнюється наживо, клік → підключення, код
        парування — в тому ж вікні. БЕЗ КАБЕЛЮ через Бездротове налагодження
        (Android 11+): знаходить телефон сам по mDNS, просить тільки код з екрану
        (adb pair), потім adb connect на динамічний порт. Кабель взагалі не потрібен!
      - якщо нічого не знайдено -> шукає ЖИВІ IP в мережі (ping/arp, БЕЗ скану
        портів) і показує список — вибираєте номер, а порти програма підбирає
        сама точково (прощуп + перебір), з екрану треба тільки код
        (масовий скан портів — тільки з прапорцем --port-scan)
      - якщо в мережі порожньо -> демо-режим (fake adbd)
      Прапорці: --scan (примусовий скан), --no-scan (без пошуку в мережі),
                --port-scan (масовий скан портів), --scan-ports <список>,
                --no-wireless (без безкабельного етапу), --pair-code <код>,
                --pick <N|IP> (автовибір без запиту, без вікна), --scan-timeout <сек>,
                --mdns-timeout <сек>, --gui (примусово вікно), --no-gui (тільки консоль)
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
import concurrent.futures
import ipaddress
import os
import re
import shutil
import signal
import socket
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


def _electron_bin_path(ui_dir):
    """Шлях до бінарника Electron: electron.exe на Windows, electron на Linux/macOS."""
    name = "electron.exe" if os.name == "nt" else "electron"
    return ui_dir / "node_modules" / "electron" / "dist" / name


def _count_electron_processes():
    """Скільки процесів Electron вже запущено (детект дубль-вікна). 0 при невдачі."""
    try:
        if os.name == "nt":
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq electron.exe", "/FO", "CSV"],
                               capture_output=True, text=True, timeout=10,
                               encoding="utf-8", errors="replace")
            return max(0, len([l for l in (r.stdout or "").splitlines()
                               if "electron.exe" in l.lower()]))
        if shutil.which("pgrep"):
            r = subprocess.run(["pgrep", "-c", "-f", "[e]lectron"],
                               capture_output=True, text=True, timeout=10,
                               encoding="utf-8", errors="replace")
            return max(0, int((r.stdout or "0").strip() or 0))
    except Exception:
        pass
    return 0

# За замовчуванням: НЕ ганяти TCP-скан портів по всій підмережі, а шукати тільки
# живі IP (ping/arp). Порти підбираються точково на вибраному IP
# (прощуп + перебір). Старий масовий скан портів вмикається прапорцем --port-scan.
SKIP_PORT_SCAN = True

# ---------------- мультипорт-скан + автозапуск scrcpy ----------------
# Проблема 1: LAN-скан дивився ТІЛЬКИ порт 5555, а Бездротове налагодження
# (Android 11+) видає ДИНАМІЧНИЙ порт (напр. 37123). Якщо mDNS заблоковано
# роутером/брандмауером — телефон взагалі не знаходився.
# Проблема 2: після ручного adb connect картинка не з'являлась, бо scrcpy
# запускався з --select-usb (тільки USB), а Wi-Fi пристрій — це TCP-серійник
# IP:port і потребує scrcpy -s IP:port.

def parse_scan_ports(s, default_port=5555):
    """'5555,37123' -> [5555, 37123]. Сміття ігнорується."""
    ports = []
    for part in str(s or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            p = int(part)
            if 1 <= p <= 65535 and p not in ports:
                ports.append(p)
    try:
        default_port = int(default_port)
    except Exception:
        default_port = DEFAULT_ADB_PORT
    if default_port not in ports:
        ports.insert(0, default_port)
    return ports


def get_scan_ports(adb_port=5555, extra_ports=None, include_last=True, mdns_ports=None):
    """Зібрати впорядкований список портів для LAN-скану.

    Порядок (від найімовірнішого):
      [adb_port (--adb-port/5555), порт останнього відомого пристрою,
       --scan-ports, динамічні порти з mDNS].
    """
    try:
        adb_port = int(adb_port)
    except Exception:
        adb_port = DEFAULT_ADB_PORT
    ports = [adb_port]
    if include_last:
        try:
            _lip, _lport = _split_serial(load_last_phone(), adb_port)
            _lport = int(_lport)
            if 1 <= _lport <= 65535 and _lport not in ports:
                ports.append(_lport)
        except Exception:
            pass
    for p in (extra_ports or []):
        try:
            p = int(p)
            if 1 <= p <= 65535 and p not in ports:
                ports.append(p)
        except Exception:
            pass
    for p in (mdns_ports or []):
        try:
            p = int(p)
            if 1 <= p <= 65535 and p not in ports:
                ports.append(p)
        except Exception:
            pass
    return ports


def launch_scrcpy_for_device(ip, port, extra_args=None):
    """Запустити scrcpy -s IP:port у фоні (картинка одразу після adb connect).

    extra_args: додаткові прапорці якості (напр. ['-m','1280','-b','6M']).
    Повертає True якщо процес стартував, False якщо scrcpy нема/не запустився.
    ВАЖЛИВО: саме '-s серійник', а НЕ '--select-usb' (той бачить тільки USB).
    """
    if not shutil.which("scrcpy"):
        log("scrcpy not found in PATH - not opening the video window "
            "(adb connect already done).")
        return False
    serial = f"{ip}:{port}"
    cmd = ["scrcpy", "-s", serial] + list(extra_args or [])
    try:
        log_cmd(cmd)
        subprocess.Popen(cmd)
        log(f"scrcpy started for {serial} - the video window should open.")
        return True
    except FileNotFoundError:
        log("Failed to start scrcpy (not found).")
        return False
    except Exception as e:
        log(f"Failed to start scrcpy: {e}")
        return False


def build_scrcpy_cmd(args, quality_extra, adb_host, adb_port_eff, use_fake_adb):
    """Побудувати команду scrcpy з урахуванням TCP-пристрою.

    - Демо/127.0.0.1 без телефону: як було (args.scrcpy_args).
    - Реальний Wi-Fi пристрій: ['scrcpy','-s','IP:port', ...] без --select-usb
      (--select-usb ігнорує TCP-пристрої — через це і не було картинки).
    - Свої --scrcpy-args з -s/--select-*: поважаємо як є.
    """
    base = (getattr(args, "scrcpy_args", "") or "").split()
    extra = list(quality_extra or [])
    if use_fake_adb:
        return ["scrcpy"] + base + extra
    is_default = (getattr(args, "scrcpy_args", "") or "").strip() == "--select-usb"
    if not is_default:
        # Свої --scrcpy-args — поважаємо як є (там вже є -s / --select-*).
        return ["scrcpy"] + base + extra
    host = (adb_host or "").strip()
    # TCP-пристрій: завжди -s (працює і для 5555, і для динамічного порту)
    filtered = [t for t in base if t != "--select-usb"]
    return ["scrcpy", "-s", f"{host}:{adb_port_eff}"] + filtered + extra


def resolve_connect_port(ip, adb_port=5555, mdns_timeout=1.5, extra_ports=None,
                         try_tcp=True):
    """Знайти робочий ADB-порт для ВІДОМОГО IP (ручне введення без порту).

    Порядок: mDNS connect-порт цього IP -> порт останнього пристрою ->
    adb_port/5555 -> --scan-ports -> mDNS-порти інших пристроїв.
    Повертає порт (int) або 0 якщо нічого не відкрито.
    """
    ip = (ip or "").strip()
    if not ip:
        return 0
    try:
        adb_port = int(adb_port)
    except Exception:
        adb_port = DEFAULT_ADB_PORT
    mdns_ports_here = []
    mdns_ports_all = []
    try:
        devs = mdns_discover_adb(timeout=max(0.8, min(float(mdns_timeout), 3.0)))
        for d in devs:
            if d.get("kind") == "connect" and d.get("ips") and d.get("port"):
                try:
                    mdns_ports_all.append(int(d["port"]))
                except Exception:
                    pass
                if d["ips"][0] == ip:
                    mdns_ports_here.append(int(d["port"]))
    except Exception:
        pass
    cands = []
    for p in mdns_ports_here:
        if p not in cands:
            cands.append(p)
    try:
        _lip, _lport = _split_serial(load_last_phone(), adb_port)
        _lport = int(_lport)
        if _lip == ip and _lport not in cands:
            cands.append(_lport)
    except Exception:
        pass
    if adb_port not in cands:
        cands.append(adb_port)
    if DEFAULT_ADB_PORT not in cands:
        cands.append(DEFAULT_ADB_PORT)
    for p in (extra_ports or []):
        try:
            p = int(p)
            if p not in cands:
                cands.append(p)
        except Exception:
            pass
    for p in mdns_ports_all:
        if p not in cands:
            cands.append(p)
    if not try_tcp:
        return cands[0] if cands else 0
    for p in cands:
        try:
            if _tcp_port_open(ip, p, 0.5):
                if p != adb_port:
                    log(f"Found open port {p} for {ip} (instead of {adb_port}).")
                return p
        except Exception:
            pass
    return 0


def sweep_connect_port(ip, exclude=(), timeout=0.2, workers=512,
                       port_min=30000, port_max=49999, seen=None):
    """Перебір динамічних портів ОДНОГО IP — останній шанс після парування.

    Коли mDNS connect-сервіс не видно (ріже роутер), а IP телефону ТОЧНО
    відомий (парування щойно пройшло), порт підключення шукаємо перебором
    типового діапазону Бездротового налагодження. Повертає порт з ВІДКРИТИМ
    TCP або 0. Займає ~10-15с, тому викликається тільки тут, а не при кожному скані.
    seen: якщо передано список — сюди допишуться ВСІ знайдені відкриті порти
    (знадобиться guided-флоу для парування).
    """
    ip = (ip or "").strip()
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        return 0
    try:
        excl = {int(p) for p in (exclude or [])}
    except Exception:
        excl = set()
    ports = [p for p in range(port_min, port_max + 1) if p not in excl]
    log(f"Probing ports {ip}:{port_min}-{port_max} (up to ~15s, looking for the dynamic port)...")
    found = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            fut2port = {ex.submit(_tcp_port_open, ip, p, timeout): p for p in ports}
            for fut in concurrent.futures.as_completed(fut2port):
                try:
                    if fut.result():
                        p = fut2port[fut]
                        found.append(p)
                        if seen is not None and p not in seen:
                            seen.append(p)
                        log(f"  ... {ip}:{p} is open, checking if it is ADB...")
                        # Перевіряємо одразу, не чекаючи кінця перебору
                        if adb_connect_fast(ip, p, timeout=4.0):
                            log(f"Found it: {ip}:{p} responds as ADB.")
                            try:
                                ex.shutdown(wait=False, cancel_futures=True)
                            except TypeError:
                                pass
                            found.sort()
                            return p
                except Exception:
                    pass
    except KeyboardInterrupt:
        raise
    except Exception:
        pass
    if found:
        log(f"Open ports on {ip}: {found}, but adb connect failed.")
    else:
        log(f"No open ports in range {port_min}-{port_max} on {ip}.")
    return 0


def _find_connect_port_after_pair(ip, pair_port=0, extra_ports=None):
    """Знайти connect-порт після успішного парування.

    Порядок: mDNS -> TCP-прощуп відомих кандидатів -> перебір 30000-49999.
    Повертає порт або 0. Pair-порт свідомо виключаємо (це інший сервіс).
    """
    try:
        pair_port = int(pair_port or 0)
    except Exception:
        pair_port = 0
    try:
        found = resolve_connect_port(ip, mdns_timeout=2.0, extra_ports=extra_ports)
    except Exception:
        found = 0
    if found and found != pair_port:
        return found
    try:
        return sweep_connect_port(ip, exclude=(pair_port,) if pair_port else ())
    except Exception:
        return 0


def discover_alive_ips(ping_timeout_ms=400, workers=128, fast=False):
    """Знайти ВСІ живі хости в LAN (ping-sweep + arp-таблиця) — без знання портів.

    Етап 1 «сама знайшла IP»: повертає відсортований список IP. Далі користувач
    тикає свій телефон зі списку (або дивиться IP в Налаштуваннях), а порти
    програма підбере сама (див. guided_connect_by_ip).
    fast=True: тільки arp-таблиця, миттєво без трафіку (для показу «хто в мережі»
    одразу після порт-скану — всі відповідаючі хости вже є в arp-кеші).
    """
    nets = get_local_subnets()
    targets = []
    for net in nets:
        hosts = list(net.hosts())
        if len(hosts) > 1024:
            hosts = hosts[:1024]
        targets.extend(str(h) for h in hosts)
    targets = list(dict.fromkeys(targets))
    alive = set()
    # arp -a: миттєво, без трафіку (кого ПК вже бачив у мережі)
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, timeout=15)
        out = _decode_any(r.stdout or b"")
        for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)", out):
            alive.add(m.group(1))
    except Exception:
        pass
    # ping-sweep по підмережі (в fast-режимі пропускаємо — вистачить arp)
    def _ping(ip):
        if os.name == "nt":
            cmd = ["ping", "-n", "1", "-w", str(int(ping_timeout_ms)), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", "1", ip]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=6)
            out = _decode_any(r.stdout or b"")
            if re.search(r"TTL=\d+|ttl=\d+|time[=<]\s*\d+", out, re.IGNORECASE):
                return ip
        except Exception:
            pass
        return ""

    if targets and not fast:
        log(f"Looking for live hosts ({len(targets)} addresses, ping)... a few seconds.")
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                for ip in ex.map(_ping, targets):
                    if ip:
                        alive.add(ip)
        except Exception:
            pass
    res = []
    for ip in alive:
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_loopback or addr.is_link_local or addr.is_multicast:
                continue
            own_nets = [n for n in nets if addr in n]
            if not own_nets:
                continue
            # Адреса мережі (.0) і broadcast (.255) — не хости, відсікаємо
            # (відповідають на ping, але телефона там нема)
            if any(addr == n.network_address or addr == n.broadcast_address
                   for n in own_nets):
                continue
            res.append(ip)
        except Exception:
            pass
    try:
        res.sort(key=lambda s: tuple(int(p) for p in s.split(".")))
    except Exception:
        res.sort()
    return res


def _quick_conn(ip, port, poll=3.0):
    """Одна швидка спроба adb connect (для перебору портів, без довгих ретраїв)."""
    serial = f"{ip}:{port}"
    try:
        subprocess.run(["adb", "connect", serial], capture_output=True, text=True,
                       timeout=8, encoding="utf-8", errors="replace")
    except Exception:
        return False
    t0 = time.time()
    while time.time() - t0 < poll:
        if _tcp_device_ready(serial, port):
            return True
        time.sleep(0.4)
    return False


def guided_connect_by_ip(ip, pair_code=None, extra_ports=None, adb_port=5555,
                         mdns_timeout=2.0):
    """IP відомий (знайшли сканом або ввів користувач) — РЕШТУ знаходимо САМІ.

    1) Прощупуємо відомі порти (5555, останній, --scan-ports, mDNS-порти цього IP).
    2) Перебір 30000-49999 цього IP (ловить динамічні порти налагодження).
    3) По кожному відкритому: adb connect. Успіх -> (port, True).
    4) Якщо connect ніде не пішов (треба парування): питаємо ТІЛЬКИ код
       з екрану і пробуємо adb pair по відкритих портах, потім connect.
    Повертає (port, ok). Порти з екрану вводити НЕ треба.
    """
    ip = (ip or "").strip()
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        return 0, False
    try:
        adb_port = int(adb_port)
    except Exception:
        adb_port = DEFAULT_ADB_PORT
    # Відомі кандидати + mDNS-порти САМЕ цього IP
    cands = get_scan_ports(adb_port, extra_ports=extra_ports, include_last=True)
    try:
        for d in mdns_discover_adb(timeout=min(float(mdns_timeout), 2.0)):
            if d.get("ips") and d["ips"][0] == ip and d.get("port"):
                p = int(d["port"])
                if p not in cands:
                    cands.append(p)
    except Exception:
        pass
    log(f"Picking a port for {ip} (candidates: {', '.join(str(p) for p in cands)})...")
    opens = []
    for p in cands:
        try:
            if _tcp_port_open(ip, p, 0.5) and p not in opens:
                opens.append(p)
        except Exception:
            pass
    # Перебір динаміки (збираємо ВСІ відкриті, знадобляться і для pair)
    seen = []
    try:
        sweep_hit = sweep_connect_port(ip, exclude=(), seen=seen)
    except Exception:
        sweep_hit = 0
        seen = []
    if sweep_hit:
        save_last_phone(f"{ip}:{sweep_hit}")
        return sweep_hit, True
    for p in (seen or []):
        if p not in opens:
            opens.append(p)
    # Connect по кожному відкритому
    for p in opens:
        log(f"Trying adb connect {ip}:{p}...")
        if _quick_conn(ip, p):
            name = get_device_display_name(f"{ip}:{p}")
            log(f"Connected: {ip}:{p} - {name}.")
            save_last_phone(f"{ip}:{p}")
            return p, True
    # Треба парування: питаємо ТІЛЬКИ код, pair-порт визначаємо перебором
    code = (pair_code or "").strip()
    if not code:
        if not _stdin_interactive():
            log("Pairing is required but no code was given (non-interactive mode, pass --pair-code).")
            return 0, False
        log("Looks like the phone is not paired yet. On the screen: 'Pair device with pairing code'.")
        code = _prompt("Enter the 6-digit code from the phone screen: ")
    if not code:
        return 0, False
    for p in opens:
        log(f"Trying to pair on {ip}:{p}...")
        if adb_pair_verify(ip, p, code, timeout=15):
            log(f"Paired on {ip}:{p}, looking for the connection port...")
            for q in opens:
                if q == p:
                    continue
                if _quick_conn(ip, q):
                    name = get_device_display_name(f"{ip}:{q}")
                    log(f"Connected over Wi-Fi WITHOUT cable: {ip}:{q} - {name}.")
                    save_last_phone(f"{ip}:{q}")
                    return q, True
            f = _find_connect_port_after_pair(ip, p, extra_ports=extra_ports)
            if f and _quick_conn(ip, f):
                name = get_device_display_name(f"{ip}:{f}")
                log(f"Connected over Wi-Fi WITHOUT cable: {ip}:{f} - {name}.")
                save_last_phone(f"{ip}:{f}")
                return f, True
            diagnose_connect_fail(ip, p)
            return 0, False
    log("No open port accepted the pairing code. The code is one-time - refresh the screen and try again.")
    return 0, False


def identify_host(ip, ports):
    """Чи є на IP живий ADB? Прощупуємо кандидатні порти, читаємо назву з телефона.

    Повертає info dict (як get_device_info) або None. Приймає і device, і
    unauthorized/offline (такі теж підписуємо — це точно телефони).
    """
    ip = (ip or "").strip()
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        return None
    for p in (ports or []):
        try:
            p = int(p)
        except Exception:
            continue
        try:
            if not _tcp_port_open(ip, p, 0.35):
                continue
        except Exception:
            continue
        try:
            info = get_device_info(ip, p)
        except Exception:
            continue
        if info.get("state") in ("device", "unauthorized", "offline"):
            return info
    return None


def identify_hosts(ips, ports, workers=8):
    """Паралельно розпізнати список IP. Повертає {ip: info} тільки для ADB-хостів."""
    found = {}
    lock = threading.Lock()

    def _one(ip):
        info = None
        try:
            info = identify_host(ip, ports)
        except Exception:
            info = None
        if info is not None:
            with lock:
                found[ip] = info
        return ip

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_one, list(ips or [])))
    except Exception:
        pass
    return found


def interactive_pick_ip(ips, names=None):
    """Список живих IP -> вибір користувача. Повертає IP або ''.
    names: {ip: 'назва'} — підписані показуються з назвою."""
    if not ips:
        return ""
    log("")
    log(f"Live hosts on the network: {len(ips)}. Which one is your phone? "
        "(IP is shown at: Settings -> About phone -> Status, or Wi-Fi -> your network)")
    # Позначаємо у кого відкрито 5555 (ймовірно adb/tcpip)
    marks = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            res = list(ex.map(lambda a: (a, _tcp_port_open(a, 5555, 0.35)), ips))
        marks = dict(res)
    except Exception:
        pass
    for i, ip in enumerate(ips, 1):
        tag = " [likely ADB:5555]" if marks.get(ip) else ""
        if names and names.get(ip):
            tag = f" — {names[ip]}"
        log(f"  {i}. {ip}{tag}")
    log("  0. Enter IP manually / skip")
    log("")
    try:
        ans = input(f"Enter a number (1-{len(ips)}) or IP, 0/Enter to skip: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    if not ans or ans == "0":
        return ""
    if ans.isdigit():
        idx = int(ans)
        if 1 <= idx <= len(ips):
            return ips[idx - 1]
        return ""
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", ans):
        return ans
    return ""

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

def ensure_built(skip_build, dry_run, auto_yes=False, no_install=False):
    sender = find_binary(SENDER_NAMES)
    receiver = find_binary(RECEIVER_NAMES)
    # Receiver існує тільки на Windows (VHCI-драйвер); на Linux його відсутність —
    # норма, там працюємо через scrcpy по TCP, перезбирати щоразу не треба.
    need_receiver = (os.name == "nt")
    if sender and (receiver or not need_receiver):
        log(f"Binaries found: sender={sender} receiver={receiver}")
        return sender, receiver
    if skip_build:
        log("No binaries and --skip-build: continuing without them (dry-run / adb only).")
        return sender, receiver
    if dry_run:
        log("No binaries (dry-run: skipping the build).")
        return None, None
    log("No binaries - building MYSELF (cmake)...")
    if not shutil.which("cmake"):
        # cmake вже пропонували встановити на кроці 0 (з підтвердженням).
        # Тут повторно не ставимо мовчки — тільки підказка.
        log("ERROR: no 'cmake' (you declined auto-install or it failed).")
        log("Option 1 (automatic): re-run with --install-deps")
        if os.name == "nt":
            log("Option 2 (manual): winget install Kitware.CMake")
            log("Option 3 (all at once): powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1")
        else:
            log(f"Option 2 (manual): {_manual_install_cmd('cmake', 'build-essential')}")
            log("Option 3 (all at once): bash scripts/setup_linux.sh")
        return None, None
    if not check_compiler():
        if no_install:
            log("ERROR: cmake is present but there is no C++ compiler - build is impossible.")
            return None, None
        # Важкий пакет — тільки з явної згоди (за замовчуванням default=Ні).
        offer_install_compiler(auto_yes=auto_yes)
        if not check_compiler():
            log("ERROR: cmake is present but there is no C++ compiler - build is impossible.")
            return None, None
    rc, _ = run(["cmake", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"], timeout=180)
    if rc != 0:
        log("ERROR: cmake configure failed.")
        return None, None
    rc, _ = run(["cmake", "--build", "build", "--config", "Release"], timeout=600)
    if rc != 0:
        log("ERROR: build failed.")
        return None, None
    sender = find_binary(SENDER_NAMES)
    receiver = find_binary(RECEIVER_NAMES)
    log(f"Build ready: sender={sender} receiver={receiver}")
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
    log(f"adb devices: found {len(devs)}")
    for s, st, t in devs:
        log(f"  - {s}  state={st} transport={t}")
    if not devs:
        log("  (empty - phone not connected)")
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
        log(f"Phone IP candidates: {', '.join(cands)}")
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
        log(f"Attempt {i}/{attempts}: adb connect {serial} ...")
        run(["adb", "connect", serial], timeout=30)
        # даємо adbd час на handshake (A_CNXN); опитуємо кожні 0.5с щоб не чекати зайвого
        for _ in range(8):
            time.sleep(0.5)
            if _tcp_device_ready(serial, adb_port):
                log(f"Connected and confirmed: {serial} (device).")
                return True
        log(f"Not device yet, retrying...")
    log(f"Failed to get device state on {serial} after {attempts} attempts.")
    return False

def adb_connect_fast(ip, adb_port, timeout=5.0):
    """Швидкий connect для ВІДОМИХ/знайдених пристроїв: один adb connect + опитування.

    На відміну від adb_connect_verify (довгі ретраї для adbd що рестартиться після
    tcpip) — повертається за ~1-2с коли все добре, і не висить коли погано.
    """
    serial = f"{ip}:{adb_port}"
    run(["adb", "connect", serial], timeout=15)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _tcp_device_ready(serial, adb_port):
            log(f"Connected: {serial} (device).")
            return True
        time.sleep(0.5)
    return False

def diagnose_connect_fail(ip, adb_port):
    log("")
    log("DIAGNOSTICS: adb connect failed. Check the following:")
    log(f"  1. Are the phone and PC on the SAME Wi-Fi network? (IP {ip} should be from your LAN, e.g. 192.168.x.x)")
    log(f"     If the IP looks like a mobile subnet (10.x) - enable Wi-Fi on the phone.")
    log(f"  2. On the phone: Settings -> Developer options -> Wireless debugging ENABLED?")
    log(f"  3. Is AP isolation disabled on the router? (some routers block Wi-Fi clients from each other)")
    log(f"  4. Is Windows Firewall blocking port {adb_port}? Test: ping {ip}")
    log(f"  5. Or pass the IP manually: --phone-ip <IP from Settings -> About phone -> Status>")
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


# ---------------- сканування LAN: всі доступні IP + назви пристроїв ----------------

def _prefix_from_netmask(mask_str):
    """'255.255.255.0' -> 24. Повертає 24 якщо розпарсити не вдалося."""
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask_str.strip()}").prefixlen
    except Exception:
        return 24


def get_local_subnets(first_ip=None):
    """Визначити локальні підмережі ПК. Повертає список IPv4Network.

    Пробує: ipconfig (Windows) -> ip addr (Linux) -> fallback socket (/24).
    Великі мережі (префікс < 24) обрізаємо до /24 навколо власного IP,
    щоб сканування займало секунди, а не години.
    first_ip: якщо задано — підмережа з цим IP йде ПЕРШОЮ (там найімовірніше телефон).
    """
    found = []  # список (ip_str, prefix)

    def add(ip_str, prefix):
        try:
            ip = ipaddress.ip_address(ip_str.strip())
            if not isinstance(ip, ipaddress.IPv4Address):
                return
            if ip.is_loopback or ip.is_link_local:
                return
            if ip_str.strip().startswith("169.254."):
                return
            prefix = max(0, min(32, int(prefix)))
            found.append((str(ip), prefix))
        except Exception:
            pass

    # Windows: ipconfig (мовно-незалежно: IP-рядок -> найближчий рядок з маскою 255.x).
    # Мітки ipconfig локалізовані (Subnet Mask / Маска подсети / Маска підмережі) і йдуть
    # в OEM-кодуванні, тому покладаємось тільки на ASCII-ознаки: "IPv4" і "255.".
    if os.name == "nt":
        try:
            r = subprocess.run(["ipconfig"], capture_output=True, text=True,
                               timeout=15, encoding="utf-8", errors="replace")
            out = r.stdout or ""
            cur_ip = ""
            for line in out.splitlines():
                ips_in_line = re.findall(r"(\d+\.\d+\.\d+\.\d+)", line)
                if not ips_in_line:
                    continue
                masks = [ip for ip in ips_in_line if ip.startswith("255.")]
                if masks and cur_ip:
                    # Рядок з маскою — паруємо з останнім побаченим IP
                    add(cur_ip, _prefix_from_netmask(masks[0]))
                    cur_ip = ""
                    continue
                # Рядок з адресою інтерфейсу: містить "IPv4" (є і в EN, і в RU/UA "IPv4-адрес").
                # DNS-шлюзи не чіпаємо — вони без мітки IPv4.
                if re.search(r"IPv4", line, re.IGNORECASE):
                    for ip in ips_in_line:
                        if not ip.startswith("255.") and _is_usable_lan(ip):
                            cur_ip = ip
                            break
                    continue
            if cur_ip:
                # Маски не знайшли (рідко) — беремо /24
                add(cur_ip, 24)
        except Exception:
            pass
    else:
        # Linux: ip -4 addr show
        for cmd in (["ip", "-4", "addr", "show"], ["ifconfig"]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=15, encoding="utf-8", errors="replace")
                out = r.stdout or ""
                for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", out):
                    add(m.group(1), int(m.group(2)))
                for m in re.finditer(r"inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+).*?(?:Mask|netmask)[:\s]+(\d+\.\d+\.\d+\.\d+)",
                                     out, re.IGNORECASE):
                    add(m.group(1), _prefix_from_netmask(m.group(2)))
                if found:
                    break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            except Exception:
                continue

    # Fallback: локальний IP через "з'єднання" на 8.8.8.8 (трафіку нема, тільки getsockname)
    if not found:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(3)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            add(local_ip, 24)
        except Exception:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                add(info[4][0], 24)
        except Exception:
            pass

    nets = []
    seen = set()
    for ip_str, prefix in found:
        try:
            # Великі мережі сканувати цілком довго — беремо /24 навколо власного IP
            eff_prefix = prefix if prefix >= 24 else 24
            net = ipaddress.ip_network(f"{ip_str}/{eff_prefix}", strict=False)
            if str(net) not in seen:
                seen.add(str(net))
                nets.append(net)
        except Exception:
            continue
    if first_ip:
        # Підмережа з останнім відомим IP телефону — першою (там він найімовірніше)
        try:
            fip = ipaddress.ip_address(first_ip.strip())
            nets.sort(key=lambda n: (0 if fip in n else 1, str(n)))
        except Exception:
            pass
    return nets


def _tcp_port_open(ip, port, timeout):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def scan_lan_for_adb_ports(ports, timeout=0.45, workers=96, first_ip=None, on_found=None):
    """Просканувати LAN по КІЛЬКОХ портах (5555 + динамічні порти Wi-Fi налагодження).

    ports: список портів. on_found(ip, port): колбек одразу як знайдено
    (для сумісності колбек on_found(ip) з одним аргументом теж підтримується).
    Повертає відсортований список (ip, port).
    """
    try:
        ports = [int(p) for p in (ports or []) if 1 <= int(p) <= 65535]
    except Exception:
        ports = []
    ports = list(dict.fromkeys(ports))
    if not ports:
        return []
    nets = get_local_subnets(first_ip=first_ip)
    if not nets:
        log("LAN scan: failed to determine the local subnet.")
        return []
    targets = []
    for net in nets:
        hosts = list(net.hosts())
        # Захист від /22 і більше (хоч get_local_subnets вже ріже до /24, перестраховуємось)
        if len(hosts) > 1024:
            log(f"LAN scan: {net} is too large ({len(hosts)} addresses) - scanning the first 1024.")
            hosts = hosts[:1024]
        targets.extend(str(h) for h in hosts)
    # Прибрати дублікати, зберегти порядок (пріоритетна підмережа — першою)
    targets = list(dict.fromkeys(targets))
    if not targets:
        return []
    log(f"Scanning network: {', '.join(str(n) for n in nets)} "
        f"({len(targets)} addresses, ports {', '.join(str(p) for p in ports)})... this will take a few seconds.")
    open_pairs = []
    t0 = time.time()
    combos = [(ip, port) for ip in targets for port in ports]
    # Більше комбінацій — більше воркерів, але не безмежно
    eff_workers = min(max(workers, 96), 256) if len(ports) > 1 else workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=eff_workers) as ex:
        fut2pair = {ex.submit(_tcp_port_open, ip, port, timeout): (ip, port)
                    for ip, port in combos}
        for fut in concurrent.futures.as_completed(fut2pair):
            try:
                if fut.result():
                    ip, port = fut2pair[fut]
                    open_pairs.append((ip, port))
                    if on_found:
                        try:
                            try:
                                on_found(ip, port)
                            except TypeError:
                                on_found(ip)
                        except Exception:
                            pass
            except Exception:
                pass
    # Сортуємо як IP, потім порт
    try:
        open_pairs.sort(key=lambda t: (tuple(int(p) for p in t[0].split(".")), t[1]))
    except Exception:
        open_pairs.sort()
    dt = time.time() - t0
    if open_pairs:
        log(f"Open ADB ports: {', '.join(f'{ip}:{p}' for ip, p in open_pairs)} (scan {dt:.1f}s).")
    else:
        log(f"Nothing with open ports {ports} found (scan {dt:.1f}s).")
        log("Hint: enable Wireless debugging on the phone or run 'adb tcpip 5555' over USB.")
    return open_pairs


def scan_lan_for_adb(adb_port=5555, timeout=0.45, workers=96, first_ip=None, on_found=None,
                     extra_ports=None, mdns_ports=None):
    """Просканувати всі локальні підмережі, повернути сортированный список IP з відкритим adb-портом.

    first_ip: підмережа з цим IP сканується першою. on_found(ip): колбек одразу як
    знайдено відкритий порт (не чекаючи кінця скану — для живого оновлення списку).
    extra_ports/mdns_ports: додаткові порти (динамічні порти Бездротового
    налагодження, --scan-ports). Якщо їх нема — поведінка як раніше (тільки adb_port).
    УВАГА: при кількох портах повертає IP у яких відкритий ХОЧА Б ОДИН з портів.
    Для точних пар (ip, port) використовуйте scan_lan_for_adb_ports().
    """
    ports = get_scan_ports(adb_port, extra_ports=extra_ports,
                           include_last=True, mdns_ports=mdns_ports)
    if len(ports) == 1:
        nets = get_local_subnets(first_ip=first_ip)
        if not nets:
            log("LAN scan: failed to determine the local subnet.")
            return []
        targets = []
        for net in nets:
            hosts = list(net.hosts())
            # Захист від /22 і більше (хоч get_local_subnets вже ріже до /24, перестраховуємось)
            if len(hosts) > 1024:
                log(f"LAN scan: {net} is too large ({len(hosts)} addresses) - scanning the first 1024.")
                hosts = hosts[:1024]
            targets.extend(str(h) for h in hosts)
        # Прибрати дублікати, зберегти порядок (пріоритетна підмережа — першою)
        targets = list(dict.fromkeys(targets))
        if not targets:
            return []
        log(f"Scanning network: {', '.join(str(n) for n in nets)} "
            f"({len(targets)} addresses, port {adb_port})... this will take a few seconds.")
        open_ips = []
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            fut2ip = {ex.submit(_tcp_port_open, ip, adb_port, timeout): ip for ip in targets}
            for fut in concurrent.futures.as_completed(fut2ip):
                try:
                    if fut.result():
                        ip = fut2ip[fut]
                        open_ips.append(ip)
                        if on_found:
                            try:
                                on_found(ip)
                            except Exception:
                                pass
                except Exception:
                    pass
        # Сортуємо як IP, не як рядки (щоб .100 не було перед .20)
        try:
            open_ips.sort(key=lambda s: tuple(int(p) for p in s.split(".")))
        except Exception:
            open_ips.sort()
        dt = time.time() - t0
        if open_ips:
            log(f"Open port {adb_port} found on: {', '.join(open_ips)} (scan {dt:.1f}s).")
        else:
            log(f"Nothing with open port {adb_port} found (scan {dt:.1f}s).")
            log("Hint: enable Wireless debugging on the phone or run 'adb tcpip 5555' over USB.")
        return open_ips
    # Кілька портів: один прохід, повертаємо унікальні IP (перший знайдений порт — пріоритет adb_port)
    pairs = scan_lan_for_adb_ports(ports, timeout=timeout, workers=workers,
                                   first_ip=first_ip, on_found=on_found)
    best = {}
    rank = {p: i for i, p in enumerate(ports)}
    for ip, port in pairs:
        if ip not in best or rank.get(port, 99) < rank.get(best[ip], 99):
            best[ip] = port
    try:
        return sorted(best.keys(), key=lambda s: tuple(int(p) for p in s.split(".")))
    except Exception:
        return sorted(best.keys())


def _adb_shell_on(serial, args, timeout=6):
    """adb -s <serial> <args...> -> stdout або ''."""
    try:
        r = subprocess.run(["adb", "-s", serial] + args, capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
        out = (r.stdout or "").strip()
        if "no devices" in out or "device not found" in out or "device offline" in out:
            return ""
        if r.returncode != 0 and not out:
            return ""
        return out
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def get_device_info(ip, adb_port=5555):
    """Підключитись до IP, прочитати назву. Повертає dict(ip, serial, name, model, ...)."""
    serial = f"{ip}:{adb_port}"
    # Один connect (без довгих ретраїв — ретраї будуть після вибору користувача)
    try:
        subprocess.run(["adb", "connect", serial], capture_output=True, text=True,
                       timeout=20, encoding="utf-8", errors="replace")
    except Exception:
        pass
    time.sleep(0.5)
    state = ""
    for s, st, _t in adb_devices():
        if s == serial or (s.split(":")[0] == ip and ":" in s):
            state = st
            serial = s  # adb міг показати інший порт — беремо реальний серійник
            break
    manufacturer = _adb_shell_on(serial, ["shell", "getprop", "ro.product.manufacturer"]) if state == "device" else ""
    model = _adb_shell_on(serial, ["shell", "getprop", "ro.product.model"]) if state == "device" else ""
    device = _adb_shell_on(serial, ["shell", "getprop", "ro.product.device"]) if state == "device" else ""
    android = _adb_shell_on(serial, ["shell", "getprop", "ro.build.version.release"]) if state == "device" else ""
    dev_name = _adb_shell_on(serial, ["shell", "settings", "get", "global", "device_name"]) if state == "device" else ""
    if not manufacturer and not model:
        # Запасний варіант: банер з adb devices -l (model:...)
        try:
            r = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True,
                               timeout=10, encoding="utf-8", errors="replace")
            for line in (r.stdout or "").splitlines():
                if serial.split(":")[0] in line:
                    m = re.search(r"model:(\S+)", line)
                    if m:
                        model = model or m.group(1)
                    m2 = re.search(r"device:(\S+)", line)
                    if m2:
                        device = device or m2.group(1)
        except Exception:
            pass
    if manufacturer or model:
        name = f"{manufacturer} {model}".strip()
    elif dev_name and dev_name not in ("null", ""):
        name = dev_name
    elif device:
        name = device
    elif model:
        name = model
    else:
        name = "unknown model"
    if android:
        name = f"{name} (Android {android})"
    if state == "unauthorized":
        name += " [need to confirm RSA on screen]"
    elif state == "offline":
        name += " [offline]"
    elif not state:
        name += " [not responding to ADB - maybe not a phone]"
    return {"ip": ip, "serial": serial, "name": name, "state": state,
            "manufacturer": manufacturer, "model": model, "android": android}


def scan_lan_with_names(adb_port=5555, timeout=0.45, on_found=None, first_ip=None,
                        on_port_open=None, adb_ports=None, extra_ports=None, mdns_ports=None):
    """Скан LAN + назва кожного пристрою. Повертає список dict (див. get_device_info).

    Назви читаються ПАРАЛЕЛЬНО (по ~2с на всі, а не по черзі). on_port_open(ip):
    порт відкрився (назви ще нема). on_found(info): готова назва (одразу, не чекаючи інших).
    adb_ports: явний список портів для скану. Якщо не задано — збирається сам:
    [adb_port, порт останнього пристрою, extra_ports (--scan-ports), mdns_ports].
    Це знаходить і 5555 (adb tcpip), і ДИНАМІЧНІ порти Бездротового налагодження.
    """
    ports = list(adb_ports) if adb_ports else get_scan_ports(
        adb_port, extra_ports=extra_ports, include_last=True, mdns_ports=mdns_ports)
    try:
        ports = [int(p) for p in ports if 1 <= int(p) <= 65535]
    except Exception:
        ports = [adb_port]
    ports = list(dict.fromkeys(ports))
    if not ports:
        ports = [adb_port]

    def on_open(ip, port=None):
        if on_port_open:
            try:
                try:
                    on_port_open(ip, port if port is not None else ports[0])
                except TypeError:
                    on_port_open(ip)
            except Exception:
                pass

    if len(ports) == 1:
        ips = scan_lan_for_adb(adb_port=ports[0], timeout=timeout,
                               first_ip=first_ip, on_found=on_open)
        pairs = [(ip, ports[0]) for ip in ips]
    else:
        pairs = scan_lan_for_adb_ports(ports, timeout=timeout,
                                       first_ip=first_ip, on_found=on_open)
    if not pairs:
        return []
    infos = []
    lock = threading.Lock()

    def resolve(pair):
        ip, port = pair
        info = get_device_info(ip, port)
        with lock:
            infos.append(info)
        log(f"  - {info['serial']} — {info['name']}")
        if on_found:
            try:
                on_found(info)
            except Exception:
                pass
        return info

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(resolve, pairs))
    try:
        infos.sort(key=lambda d: (tuple(int(p) for p in d["ip"].split(".")), d.get("serial", "")))
    except Exception:
        pass
    return infos


def interactive_pick_device(dev_infos, adb_port=5555, pick=None):
    """Показати список і попросити вибрати. Повертає IP (або ''). Зайві підключення відключає."""
    if not dev_infos:
        return ""
    log("")
    log(f"Found {len(dev_infos)} devices on the network. Which one to join?")
    for i, d in enumerate(dev_infos, 1):
        log(f"  {i}. {d['serial']} — {d['name']}")
    log("  0. Skip (demo without phone)")
    log("")

    def cleanup(keep_ip):
        for d in dev_infos:
            if d["ip"] != keep_ip:
                try:
                    subprocess.run(["adb", "disconnect", d["serial"]],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass

    # Неінтерактивний вибір через --pick (номер або IP)
    if pick:
        p = str(pick).strip()
        chosen = ""
        if p.isdigit():
            idx = int(p)
            if 1 <= idx <= len(dev_infos):
                chosen = dev_infos[idx - 1]["ip"]
        else:
            for d in dev_infos:
                if d["ip"] == p or d["serial"] == p or p in d["serial"]:
                    chosen = d["ip"]
                    break
        if chosen:
            log(f"Auto-select (--pick {pick}): {chosen}.")
            cleanup(chosen)
            return chosen
        log(f"--pick={pick} matched nothing, asking manually...")

    # Один пристрій + неінтерактивний термінал (скрипт/CI) — беремо його мовчки
    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    if not interactive:
        if len(dev_infos) == 1:
            log(f"Non-interactive mode - taking the only device {dev_infos[0]['ip']} automatically.")
            return dev_infos[0]["ip"]
        log("Non-interactive mode with several devices - taking the first one in device state (if any).")
        for d in dev_infos:
            if d["state"] == "device":
                cleanup(d["ip"])
                return d["ip"]
        return ""

    # Інтерактивний вибір
    try:
        ans = input("Enter a number (1-%d), IP, or 0/Enter to skip: " % len(dev_infos)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        log("Selection cancelled.")
        cleanup(None)
        return ""
    if not ans or ans == "0":
        log("Skipped by user choice.")
        cleanup(None)
        return ""
    if ans.isdigit():
        idx = int(ans)
        if 1 <= idx <= len(dev_infos):
            chosen = dev_infos[idx - 1]["ip"]
            log(f"Selected: {dev_infos[idx-1]['serial']} - {dev_infos[idx-1]['name']}.")
            cleanup(chosen)
            return chosen
        log("Invalid number - skipping.")
        cleanup(None)
        return ""
    # Ввели IP або серійник вручну
    for d in dev_infos:
        if d["ip"] == ans or d["serial"] == ans:
            log(f"Selected: {d['serial']} - {d['name']}.")
            cleanup(d["ip"])
            return d["ip"]
    # Дозволяємо будь-який IP вручну (навіть якщо сканер його не бачив — порт міг відкритись пізніше)
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", ans):
        log(f"IP entered manually: {ans} (it was not in the scan, trying to connect).")
        cleanup(None)
        return ans
    log("Unrecognized input - skipping.")
    cleanup(None)
    return ""

# ---------------- бездротове налагодження БЕЗ кабелю (Android 11+, mDNS + adb pair) ----------------
#
# Кабель потрібен лише для старої схеми 'adb tcpip 5555'. Якщо на телефоні увімкнено
# Параметри -> Для розробників -> Бездротове налагодження, телефон сам анонсує себе
# в LAN через mDNS (_adb-tls-connect для підключення, _adb-tls-pairing для парування),
# і програма підключається БЕЗ кабелю: знаходить IP:порт сама, просить тільки код з екрану.

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
ADB_PAIR_SVC = "_adb-tls-pairing._tcp.local"
ADB_CONN_SVC = "_adb-tls-connect._tcp.local"


def _mdns_encode_name(name):
    out = b""
    for part in name.split("."):
        b = part.encode("utf-8")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _mdns_decode_name(pkt, off):
    """Декодувати DNS-ім'я з підтримкою compression-пойнтерів. Повертає (ім'я, наступний offset)."""
    labels = []
    consumed = None
    jumps = 0
    while True:
        if off >= len(pkt):
            return "", (consumed if consumed is not None else off)
        ln = pkt[off]
        if ln & 0xC0 == 0xC0:
            if off + 1 >= len(pkt):
                return "", (consumed if consumed is not None else off)
            if consumed is None:
                consumed = off + 2
            off = ((ln & 0x3F) << 8) | pkt[off + 1]
            jumps += 1
            if jumps > 16:
                return "", consumed
            continue
        if ln == 0:
            off += 1
            if consumed is None:
                consumed = off
            break
        off += 1
        if off + ln > len(pkt):
            return "", (consumed if consumed is not None else off)
        labels.append(pkt[off:off + ln])
        off += ln
    try:
        name = b".".join(labels).decode("utf-8", "replace")
    except Exception:
        name = ""
    return name, consumed


def _mdns_build_query(services):
    import struct
    hdr = struct.pack(">HHHHHH", 0, 0, len(services), 0, 0, 0)
    body = b""
    for s in services:
        body += _mdns_encode_name(s) + struct.pack(">HH", 12, 1)  # PTR, IN
    return hdr + body


def _mdns_parse_packet(pkt):
    """Розібрати mDNS-пакет, повернути список (owner_name, rtype, rdata)."""
    import struct
    if len(pkt) < 12:
        return []
    try:
        _id, _fl, qd, an, ns, ar = struct.unpack(">HHHHHH", pkt[:12])
    except Exception:
        return []
    off = 12
    try:
        for _ in range(qd):
            _, off = _mdns_decode_name(pkt, off)
            off += 4
        recs = []
        for _ in range(an + ns + ar):
            if off + 10 > len(pkt):
                break
            name, off = _mdns_decode_name(pkt, off)
            rtype, _rc, _ttl, rdlen = struct.unpack(">HHIH", pkt[off:off + 10])
            off += 10
            rdata = pkt[off:off + rdlen]
            off += rdlen
            recs.append((name, rtype, rdata))
        return recs
    except Exception:
        return []


def mdns_discover_adb(timeout=2.5):
    """Знайти телефони з Бездротовим налагодженням через mDNS (без кабелю, без скану портів).

    Повертає список dict: {kind: 'pair'|'connect', instance, host, port, ips, txt}.
    """
    import struct
    query = _mdns_build_query([ADB_PAIR_SVC, ADB_CONN_SVC])
    inst = {}      # (kind, instance) -> dict
    host_ips = {}  # lower(host) -> [ip]
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except Exception as e:
        log(f"mDNS: no UDP socket: {e}")
        return []
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", MDNS_PORT))
        except OSError:
            log("mDNS: port 5353 is busy with another app - Wi-Fi auto-discovery is impossible, will offer manual entry.")
            return []
        try:
            mreq = socket.inet_aton(MDNS_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            log(f"mDNS: failed to subscribe to multicast: {e}")
            return []
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        except OSError:
            pass
        sock.settimeout(0.5)
        deadline = time.time() + max(1.0, timeout)
        nxt_q = 0.0
        while time.time() < deadline:
            now = time.time()
            if now >= nxt_q:
                try:
                    sock.sendto(query, (MDNS_ADDR, MDNS_PORT))
                except OSError:
                    pass
                nxt_q = now + 0.8
            try:
                data, _addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            for name, rtype, rdata in _mdns_parse_packet(data):
                lname = name.lower()
                kind = ""
                if lname == ADB_PAIR_SVC:
                    kind = "pair"
                elif lname == ADB_CONN_SVC:
                    kind = "connect"
                if rtype == 12 and kind:  # PTR: власник=сервіс, rdata=інстанс
                    ins, _ = _mdns_decode_name(rdata, 0)
                    if ins:
                        key = (kind, ins)
                        inst.setdefault(key, {"kind": kind, "instance": ins, "host": "",
                                              "port": 0, "ips": [], "txt": []})
                elif rtype == 33:  # SRV: власник=інстанс, всередині порт і хост
                    if len(rdata) >= 6:
                        port = struct.unpack(">H", rdata[4:6])[0]
                        host, _ = _mdns_decode_name(rdata, 6)
                        for key in list(inst.keys()):
                            if key[1].lower() == lname:
                                inst[key]["port"] = port
                                inst[key]["host"] = host.rstrip(".")
                elif rtype == 1:  # A: власник=хост
                    if len(rdata) == 4:
                        ip = ".".join(str(b) for b in rdata)
                        host_ips.setdefault(lname, [])
                        if ip not in host_ips[lname]:
                            host_ips[lname].append(ip)
                elif rtype == 16:  # TXT: власник=інстанс
                    strs = []
                    i = 0
                    while i < len(rdata):
                        ln = rdata[i]
                        i += 1
                        strs.append(rdata[i:i + ln].decode("utf-8", "replace"))
                        i += ln
                    for key in list(inst.keys()):
                        if key[1].lower() == lname and strs:
                            inst[key]["txt"] = strs
        out = []
        for _key, d in inst.items():
            d["ips"] = list(host_ips.get(d["host"].lower(), []))
            if d["port"] and d["ips"]:
                out.append(d)
        # Спочатку connect (до них можна одразу), потім pair (потрібен код)
        out.sort(key=lambda d: (0 if d["kind"] == "connect" else 1, d["ips"][0]))
        return out
    finally:
        try:
            sock.close()
        except Exception:
            pass


def mdns_listen_check(timeout=8.0):
    """Діагностика: що реально чути в mDNS-ефірі (пасивне слухання + запити).

    Показує ЧОМУ автопошук не бачить телефон:
    - видно _adb-tls-* -> телефон анонсується, копайте в бік портів/adb;
    - видно чужий mDNS, але не adb -> на телефоні вимкнено Бездротове налагодження
      або телефон/ПК у різних мережах (гостьова Wi-Fi, VPN, ізоляція клієнтів);
    - повна тиша -> multicast ріже роутер або брандмауер Windows
      (тільки ручне введення IP/порту).
    """
    query = _mdns_build_query([ADB_PAIR_SVC, ADB_CONN_SVC])
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except Exception as e:
        log(f"mDNS: no UDP socket: {e}")
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", MDNS_PORT))
        except OSError:
            log("Port 5353 is busy with another app (often Bonjour/Chrome) - "
                "cannot listen to the air. Close the browser and try again.")
            return
        try:
            mreq = socket.inet_aton(MDNS_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            log(f"Failed to subscribe to multicast: {e} (VPN? antivirus with firewall?)")
            return
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        except OSError:
            pass
        sock.settimeout(0.5)
        log(f"Listening to mDNS air {timeout:.0f}s... (Wireless debugging must be enabled on the phone)")
        adb_seen = {}
        other_services = set()
        pkt_count = 0
        deadline = time.time() + max(3.0, timeout)
        nxt_q = 0.0
        while time.time() < deadline:
            now = time.time()
            if now >= nxt_q:
                try:
                    sock.sendto(query, (MDNS_ADDR, MDNS_PORT))
                except OSError:
                    pass
                nxt_q = now + 1.0
            try:
                data, _addr = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            pkt_count += 1
            for name, rtype, rdata in _mdns_parse_packet(data):
                lname = name.lower()
                if rtype != 12:  # цікавлять тільки PTR-анонси сервісів
                    continue
                if lname in (ADB_PAIR_SVC, ADB_CONN_SVC):
                    ins, _ = _mdns_decode_name(rdata, 0)
                    if ins:
                        adb_seen.setdefault(lname, set()).add(ins)
                elif lname.endswith("._tcp.local") or lname.endswith("._udp.local"):
                    other_services.add(lname)
        log("")
        log(f"Heard mDNS packets: {pkt_count}.")
        if adb_seen:
            for svc in sorted(adb_seen):
                log(f"  [OK] {svc}: {len(adb_seen[svc])} announcement(s)")
                for ins in sorted(adb_seen[svc])[:5]:
                    log(f"       - {ins}")
            log("The phone is announcing - auto-discovery should see it. If not, "
                "the problem is in ports/adb, not in the network.")
        elif pkt_count:
            log("  No phone _adb-tls-* announcements, but other mDNS traffic exists:")
            for svc in sorted(other_services)[:8]:
                log(f"       - {svc}")
            log("Conclusion: multicast reaches the PC. Check on the phone:")
            log("  Settings -> Developer options -> Wireless debugging ENABLED,")
            log("  and that the PC and phone are on the SAME Wi-Fi network (not guest, no VPN).")
        else:
            log("  COMPLETE SILENCE - multicast does not reach the PC.")
            log("Conclusion: the router cuts multicast between Wi-Fi clients (AP/Client isolation),")
            log("  or Windows Firewall blocks UDP 5353, or a VPN is active.")
            log("  Fix: disable isolation in router settings / allow "
                "python in the firewall / disable VPN. Meanwhile - only manual entry.")
        log("")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def adb_pair_verify(ip, pair_port, code, timeout=30):
    """adb pair <ip>:<pair_port> з кодом з екрану телефону. True якщо 'Successfully paired'."""
    serial = f"{ip}:{pair_port}"
    log(f"adb pair {serial} (code from the phone screen)...")
    try:
        r = subprocess.run(["adb", "pair", serial], input=(code.strip() + "\n"),
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.strip().splitlines():
            if line.strip():
                log(f"   | {line.strip()}")
        if "Successfully paired" in out:
            log("Pairing succeeded.")
            return True
    except FileNotFoundError:
        log("adb not found.")
        return False
    except subprocess.TimeoutExpired:
        log("adb pair hung (timeout).")
        return False
    log("Pairing FAILED. The code is one-time - reopen the pairing screen and try again.")
    return False


def get_device_display_name(serial):
    """Прочитати 'Виробник Модель (Android X)' з уже підключеного пристрою."""
    man = _adb_shell_on(serial, ["shell", "getprop", "ro.product.manufacturer"])
    mod = _adb_shell_on(serial, ["shell", "getprop", "ro.product.model"])
    andr = _adb_shell_on(serial, ["shell", "getprop", "ro.build.version.release"])
    name = f"{man} {mod}".strip() or "unknown model"
    if andr:
        name += f" (Android {andr})"
    return name


def _prompt(text):
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _stdin_interactive():
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def interactive_pick_wireless(conns, pairs, pair_code=None, pick=None):
    """Меню Wi-Fi пристроїв без кабелю. Повертає (action, dev).

    action: 'connect' (adb connect), 'pair' (adb pair по коду), 'manual' (ручне введення), '' (пропуск).
    """
    items = [("connect", d) for d in conns] + [("pair", d) for d in pairs]
    if not items:
        return "", None
    log("")
    log("Found over Wi-Fi WITHOUT cable:")
    for i, (act, d) in enumerate(items, 1):
        host = (d["host"] or d["instance"]).replace(".local", "")
        if act == "connect":
            log(f"  {i}. [connect] {host} - {d['ips'][0]}:{d['port']}")
        else:
            log(f"  {i}. [pair] {host} - {d['ips'][0]}:{d['port']} (need the 6-digit code from the screen)")
    log(f"  {len(items) + 1}. Enter IP/ports/code manually")
    log("  0. Skip")
    log("")

    def match(p):
        p = str(p).strip()
        if p.isdigit():
            idx = int(p)
            if 1 <= idx <= len(items):
                return items[idx - 1]
            return None
        for act, d in items:
            if d["ips"] and (d["ips"][0] == p or p in d["instance"] or p in (d["host"] or "")):
                return (act, d)
        return None

    if pick:
        m = match(pick)
        if m:
            log(f"Auto-select (--pick {pick}).")
            return m
        log(f"--pick={pick} matched nothing, asking manually...")
    if not _stdin_interactive():
        if len(items) == 1 and items[0][0] == "connect":
            log(f"Non-interactive mode - connecting to {items[0][1]['ips'][0]}:{items[0][1]['port']}.")
            return items[0]
        if pair_code:
            for act, d in items:
                if act == "pair":
                    log("Non-interactive mode - pairing with --pair-code.")
                    return (act, d)
        log("Non-interactive mode - cannot choose, skipping.")
        return "", None
    ans = _prompt(f"Enter a number (1-{len(items) + 1}), IP, or 0/Enter to skip: ")
    if not ans or ans == "0":
        return "", None
    if ans == str(len(items) + 1):
        return "manual", None
    m = match(ans)
    if m:
        return m
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", ans):
        return "manual_ip", {"ips": [ans]}
    log("Unrecognized input - skipping.")
    return "", None


def _wireless_do_pair(ip, pair_port, pair_code):
    """Запитати код (або взяти --pair-code) і виконати adb pair. Повертає True/False."""
    code = (pair_code or "").strip()
    if not code:
        if not _stdin_interactive():
            log("Non-interactive mode and no pairing code (--pair-code) - skipping.")
            return False
        log("On the phone open 'Pair device with pairing code' - there is a 6-digit code.")
        code = _prompt("Enter the code from the phone screen: ")
    if not code:
        return False
    return adb_pair_verify(ip, pair_port, code)


def _wireless_manual(pair_code, adb_port=DEFAULT_ADB_PORT, extra_ports=None,
                     mdns_timeout=2.0):
    """Ручне введення: питаємо ТІЛЬКИ IP (порти підбираємо самі, код — якщо треба).

    Повертає (ip, port, ok). Старі поля портів лишили як запасний шлях.
    """
    log("")
    log("Manual entry: the phone IP is enough (Settings -> About phone -> Status).")
    log("I will pick ports myself by probing; I will ask for the code only if pairing is needed.")
    ip = _prompt("Phone IP (e.g. 192.168.1.50, Enter to skip): ")
    if not ip:
        return "", 0, False
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        log("Does not look like an IP - skipping.")
        return "", 0, False
    port, ok = guided_connect_by_ip(ip, pair_code=pair_code,
                                    extra_ports=extra_ports,
                                    adb_port=adb_port,
                                    mdns_timeout=mdns_timeout)
    if ok:
        return ip, port, True
    # Запасний шлях: порти з екрану (раптом автопідбір не взяв)
    log("Auto-pick failed - trying with ports from the phone screen.")
    pair_port = _prompt("Pairing port ('Pair device with pairing code', Enter if the phone is already paired): ")
    if pair_port:
        if not pair_port.isdigit():
            log("Port must be a number - skipping.")
            return "", 0, False
        if not _wireless_do_pair(ip, int(pair_port), pair_code):
            return "", 0, False
    cport = _prompt("Connection port ('IP address & Port', e.g. 37123): ")
    if not cport or not cport.isdigit():
        log("No connection port - cannot proceed, skipping.")
        return "", 0, False
    if adb_connect_verify(ip, int(cport)):
        name = get_device_display_name(f"{ip}:{cport}")
        log(f"Connected over Wi-Fi WITHOUT cable: {ip}:{cport} - {name}.")
        save_last_phone(f"{ip}:{cport}")
        return ip, int(cport), True
    diagnose_connect_fail(ip, int(cport))
    return "", 0, False


def _try_wireless_debug(mdns_timeout=2.5, pair_code=None, pick=None,
                        adb_port=DEFAULT_ADB_PORT, extra_ports=None):
    """Підключення БЕЗ кабелю через Бездротове налагодження. Повертає (ip, port, ok)."""
    log("")
    log("Looking for the phone over Wi-Fi WITHOUT cable (mDNS, Wireless debugging)...")
    log("Must be enabled on the phone: Settings -> Developer options -> Wireless debugging.")
    try:
        devs = mdns_discover_adb(timeout=mdns_timeout)
    except Exception as e:
        log(f"mDNS discovery failed: {e}")
        devs = []
    conns = [d for d in devs if d["kind"] == "connect"]
    pairs = [d for d in devs if d["kind"] == "pair"]
    for d in conns + pairs:
        host = (d["host"] or d["instance"]).replace(".local", "")
        log(f"  - [{d['kind']}] {host} — {d['ips'][0]}:{d['port']}")
    if not conns and not pairs:
        log("Nothing found over Wi-Fi (mDNS) (maybe the router/firewall cuts multicast).")
        log("But I will find the IP myself by scanning the network, and pick ports by probing - "
            "only the code from the screen will be needed.")
        if pick and re.match(r"^\d+\.\d+\.\d+\.\d+$", str(pick)):
            log(f"--pick looks like an IP, picking ports for {pick} myself...")
            port, ok = guided_connect_by_ip(str(pick), pair_code=pair_code,
                                            extra_ports=extra_ports,
                                            adb_port=adb_port,
                                            mdns_timeout=mdns_timeout)
            if ok:
                return str(pick), port, True
            return "", 0, False
        if _stdin_interactive() and not pick:
            try:
                alive = discover_alive_ips()
            except Exception as e:
                log(f"Live host scan failed: {e}")
                alive = []
            if alive:
                log("Finding live hosts myself - pick your phone from the list.")
                try:
                    _iports = get_scan_ports(adb_port, extra_ports=extra_ports,
                                             include_last=True)
                except Exception:
                    try:
                        _iports = [int(adb_port)]
                    except Exception:
                        _iports = [DEFAULT_ADB_PORT]
                try:
                    identified = identify_hosts(alive, _iports)
                except Exception:
                    identified = {}
                names = {ip: info.get("name", "") for ip, info in identified.items()}
                chosen = interactive_pick_ip(alive, names=names)
                if chosen:
                    if chosen in identified:
                        try:
                            _h, _p = _split_serial(identified[chosen].get("serial", ""),
                                                   adb_port)
                            cport = int(_p)
                        except Exception:
                            try:
                                cport = int(adb_port)
                            except Exception:
                                cport = DEFAULT_ADB_PORT
                        if adb_connect_fast(chosen, cport, timeout=5.0):
                            save_last_phone(f"{chosen}:{cport}")
                            return chosen, cport, True
                    port, ok = guided_connect_by_ip(chosen, pair_code=pair_code,
                                                    extra_ports=extra_ports,
                                                    adb_port=adb_port,
                                                    mdns_timeout=mdns_timeout)
                    if ok:
                        return chosen, port, True
                    return "", 0, False
            ans = _prompt("Enter the IP manually from the phone screen? (y/n): ").lower()
            if ans in ("так", "да", "y", "yes", "т", "д", ""):
                return _wireless_manual(pair_code, adb_port=adb_port,
                                        extra_ports=extra_ports,
                                        mdns_timeout=mdns_timeout)
        return "", 0, False
    action, dev = interactive_pick_wireless(conns, pairs, pair_code, pick)
    if action == "connect":
        ip, port = dev["ips"][0], dev["port"]
        if adb_connect_fast(ip, port, timeout=6.0):
            name = get_device_display_name(f"{ip}:{port}")
            log(f"Connected over Wi-Fi WITHOUT cable: {ip}:{port} - {name}.")
            save_last_phone(f"{ip}:{port}")
            return ip, port, True
        log("Direct connection failed - the phone may not be paired yet. Looking for pairing...")
        same = [d for d in pairs if d["ips"] and d["ips"][0] == ip]
        if same:
            return _wireless_pair_then_connect(same[0], pair_code, mdns_timeout)
        diagnose_connect_fail(ip, port)
        return "", 0, False
    if action == "pair":
        return _wireless_pair_then_connect(dev, pair_code, mdns_timeout)
    if action in ("manual", "manual_ip"):
        if action == "manual_ip":
            cport = _prompt(f"Connection port for {dev['ips'][0]} ('IP address & Port'): ")
            if cport and cport.isdigit() and adb_connect_verify(dev["ips"][0], int(cport)):
                save_last_phone(f"{dev['ips'][0]}:{cport}")
                return dev["ips"][0], int(cport), True
            return "", 0, False
        return _wireless_manual(pair_code, adb_port=adb_port,
                                extra_ports=extra_ports,
                                mdns_timeout=mdns_timeout)
    return "", 0, False


def _wireless_pair_then_connect(pair_dev, pair_code, mdns_timeout=2.5):
    """adb pair по коду, потім пошук connect-порту і adb connect. Повертає (ip, port, ok)."""
    ip, pair_port = pair_dev["ips"][0], pair_dev["port"]
    if not _wireless_do_pair(ip, pair_port, pair_code):
        return "", 0, False
    # Після парування шукаємо connect-сервіс того ж IP (порт динамічний!)
    log("Looking for the connection port of the same phone...")
    try:
        devs = mdns_discover_adb(timeout=max(2.0, min(mdns_timeout, 4.0)))
    except Exception:
        devs = []
    same = [d for d in devs if d["kind"] == "connect" and d["ips"] and d["ips"][0] == ip]
    if same:
        cport = same[0]["port"]
    else:
        # mDNS connect-сервіс не побачив (хоча парування пройшло) —
        # не чекаємо вводу одразу, а спочатку прощупуємо порти цього IP напряму
        # (останній порт, 5555, порти з mDNS), в крайньому разі — перебір
        # 30000-49999. Часто знаходить без жодних питань.
        log("mDNS connect service not found - looking for this IP's port (probe, then brute force)...")
        found = _find_connect_port_after_pair(ip, pair_port)
        if found:
            cport = found
        else:
            cport_s = _prompt("Could not find the port automatically. Enter the connection port ('IP address & Port'): ")
            if not cport_s or not cport_s.isdigit():
                return "", 0, False
            cport = int(cport_s)
    if adb_connect_fast(ip, cport, timeout=6.0):
        name = get_device_display_name(f"{ip}:{cport}")
        log(f"Connected over Wi-Fi WITHOUT cable: {ip}:{cport} - {name}.")
        save_last_phone(f"{ip}:{cport}")
        return ip, cport, True
    diagnose_connect_fail(ip, cport)
    return "", 0, False


# ---------------- GUI-вікно вибору пристрою (як Bluetooth-діалог) ----------------
#
# Замість вводу в консоль при старті (коли нема USB/відомого пристрою) вискакує
# вікно: програма сама сканує Wi-Fi у фоні, список поповнюється наживо,
# клік по телефону → підключення; для парування — поле коду в тому ж вікні.
# Працює на вбудованому tkinter (нічого ставити не треба). Без дисплея —
# автоматичний fallback у консольний режим.

def _gui_usable():
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    # Linux без X11/Wayland: Tk() впаде з "no display name" — вважаємо GUI недоступним
    # одразу, щоб показати зрозумілу підказку, а не мовчки йти в консоль.
    if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    return True


def _display_missing_reason():
    """Людяне пояснення чому вікна не буде (тільки для логів на Linux)."""
    if os.name == "nt":
        return ""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"):
            return ("no DISPLAY/WAYLAND_DISPLAY (SSH without X-forwarding). "
                    "For the window: ssh -X user@host, or run locally. "
                    "Without window: ./START_CONSOLE.sh or --no-gui.")
        return ("no DISPLAY/WAYLAND_DISPLAY (no graphical session). "
                "Run from a graphical terminal, not from plain TTY/docker without -e DISPLAY. "
                "Without window: ./START_CONSOLE.sh or --no-gui.")
    return ""


def _needs_no_sandbox():
    """Чи треба запускати Electron з --no-sandbox (root на Linux)."""
    if os.name == "nt":
        return False
    try:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception:
        return False


def _gui_disconnect_others(devices_snapshot, keep_ip):
    for d in devices_snapshot:
        serials = {f"{d['ip']}:{d['port']}", (d.get("raw") or {}).get("serial", "")}
        for serial in serials:
            if serial and not serial.startswith(keep_ip + ":"):
                try:
                    subprocess.run(["adb", "disconnect", serial],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass


def _repair_electron_dist(ui_dir):
    """Дотягнути бінарник Electron вручну без npm-хуків.

    Навіщо: Node 24/26 ламає вбудований extract-zip в Electron 28 —
    після install в dist/ лишається тільки locales/, бінарника нема.
    Качаємо офіційний zip з GitHub через stdlib (urllib/zipfile) і
    розпаковуємо самі. Повертає True якщо dist/electron з'явився.
    """
    import json
    import platform
    import urllib.request
    import zipfile
    try:
        edir = Path(ui_dir) / "node_modules" / "electron"
        dist = edir / "dist"
        exe = _electron_bin_path(Path(ui_dir))
        if exe.exists():
            return True
        ver = ""
        try:
            ver = json.loads((edir / "package.json").read_text(encoding="utf-8")).get("version", "")
        except Exception:
            ver = ""
        if not ver:
            try:
                lock = json.loads((Path(ui_dir) / "package-lock.json").read_text(encoding="utf-8"))
                ver = lock.get("packages", {}).get("node_modules/electron", {}).get("version", "")
            except Exception:
                ver = ""
        if not ver:
            ver = "28.3.3"
        mach = platform.machine().lower()
        if mach in ("x86_64", "amd64"):
            arch = "x64"
        elif mach in ("aarch64", "arm64"):
            arch = "arm64"
        else:
            log(f"Unknown architecture for manual Electron repair: {mach}")
            return False
        url = f"https://github.com/electron/electron/releases/download/v{ver}/electron-v{ver}-linux-{arch}.zip"
        log(f"Fetching the Electron binary manually: {url}")
        tmp = Path("/tmp/electron-fix.zip")
        try:
            urllib.request.urlretrieve(url, str(tmp))
        except Exception as e:
            log(f"Failed to download Electron: {e}")
            return False
        try:
            dist.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(str(tmp), "r") as z:
                z.extractall(str(dist))
        except Exception as e:
            log(f"Failed to unpack Electron: {e}")
            return False
        try:
            pt = edir / "path.txt"
            if not pt.exists():
                pt.write_text("dist/electron", encoding="utf-8")
        except Exception:
            pass
        try:
            os.chmod(str(exe), 0o755)
        except Exception:
            pass
        if exe.exists():
            log("Manual Electron repair succeeded.")
            return True
        return False
    except Exception as e:
        log(f"Manual Electron repair failed: {e}")
        return False


def electron_gui_pick_and_connect(adb_port=5555, mdns_timeout=2.5, scan_timeout=0.45, pair_code=None,
                                scan_ports=None):
    """Спроба відкрити CyberDeck Electron UI. Повертає (ip, port, ok) або None якщо не вдалося."""
    import json
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path

    _extra_scan_ports = [int(p) for p in (scan_ports or []) if str(p).isdigit()]

    # Linux без графічної сесії: Electron впаде мовчки — не пробуємо, одразу tkinter-fallback.
    if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        log("Electron UI skipped: " + _display_missing_reason())
        return None

    ui_dir = ROOT / "ui"
    if not (ui_dir / "package.json").exists():
        log("Electron UI skipped: no ui/package.json "
            f"(looked in {ui_dir}). Run from the repo root.")
        return None

    electron_exe = _electron_bin_path(ui_dir)
    if not electron_exe.exists() and not (ui_dir / "node_modules").exists():
        log("Electron UI: no ui/node_modules - CyberDeck cannot start without it.")
        _npm_try = shutil.which("npm.cmd") or shutil.which("npm")
        if _npm_try and _stdin_interactive() and ask_yes_no(
                "Install CyberDeck UI dependencies now (cd ui && npm install, 1-3 min, one-time)?",
                default_yes=True):
            log("Installing UI dependencies - do not close the window...")
            _inst = [_npm_try, "install", "--no-audit", "--no-fund"]
            if os.name == "nt" and str(_npm_try).lower().endswith((".cmd", ".bat")):
                _inst = ["cmd", "/c"] + _inst
            try:
                log_cmd(["npm", "install", "--no-audit", "--no-fund"])
                _ir = subprocess.run(_inst, cwd=str(ui_dir), timeout=600)
                # npm 12 блокить postinstall за замовчуванням (allowScripts).
                # approve пробуємо завжди коли бінарника нема (ls до install
                # каже "No packages..." бо node_modules ще нема; на npm 12
                # команда — `npm approve-scripts`, старий alias теж пробуємо).
                if not electron_exe.exists():
                    try:
                        log("npm: no Electron binary - trying to allow postinstall...")
                        for _cmd in (["approve-scripts", "electron"],
                                     ["install-scripts", "approve", "electron"]):
                            try:
                                subprocess.run([_npm_try] + _cmd,
                                               cwd=str(ui_dir), timeout=60,
                                               capture_output=True)
                            except Exception:
                                pass
                        log_cmd(["npm", "install", "--no-audit", "--no-fund"])
                        _ir = subprocess.run(_inst, cwd=str(ui_dir), timeout=600)
                        if not electron_exe.exists():
                            # Прапорець напряму + ручний install.js
                            # (ручний обходить і блок, і битий extract-zip на Node 26).
                            try:
                                subprocess.run(_inst + ["--allow-scripts=electron"],
                                               cwd=str(ui_dir), timeout=600,
                                               capture_output=True)
                            except Exception:
                                pass
                            _ij = ui_dir / "node_modules" / "electron" / "install.js"
                            if not electron_exe.exists() and _ij.is_file():
                                log("Trying manual node install.js...")
                                try:
                                    subprocess.run([shutil.which("node") or "node", str(_ij)],
                                                   cwd=str(ui_dir), timeout=300)
                                except Exception:
                                    pass
                            # Node 24/26 + Electron 28: install.js мовчки лишає
                            # тільки locales/ — дотягуємо офіційний zip самі.
                            if not electron_exe.exists():
                                try:
                                    _repair_electron_dist(ui_dir)
                                    electron_exe = _electron_bin_path(ui_dir)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                if _ir.returncode == 0 and electron_exe.exists():
                    log("UI dependencies installed.")
                else:
                    log(f"npm install exited with code {_ir.returncode}. "
                        "If Electron does not start - try manually: cd ui && npm install, "
                        "npm 12: npm approve-scripts electron && npm install. "
                        "Node 26 breaks Electron 28 unpacking: Node 20 LTS is required "
                        "(nvm install 20 && nvm use 20).")
            except Exception as e:
                log(f"npm install failed ({e}) - continuing without CyberDeck.")
        else:
            log("Once, run manually: cd ui && npm install "
                "(needs Node.js LTS from https://nodejs.org). "
                "Trying to launch via npm for now...")
    if electron_exe.exists():
        cmd = [str(electron_exe), str(ui_dir), "--parent-python"]
        # Root на Linux: Chromium відмовляється без --no-sandbox, main.js теж додає
        # прапорець, але передаємо явно для надійності (старий білд main.js).
        if _needs_no_sandbox() and "--no-sandbox" not in cmd:
            cmd = [cmd[0], "--no-sandbox"] + cmd[1:]
    else:
        npm_bin = shutil.which("npm.cmd") or shutil.which("npm") or shutil.which("npx.cmd") or shutil.which("npx")
        if not npm_bin:
            log("Electron UI skipped: npm/npx not found in PATH "
                "(install Node.js LTS from https://nodejs.org). "
                "Falling back to the standard window...")
            return None
        cmd = [npm_bin, "--prefix", str(ui_dir), "start", "--", "--parent-python"]
        if _needs_no_sandbox():
            # npm start -- <args> прокидаються в electron, chromium їх розбере
            cmd = cmd + ["--no-sandbox"]
        if os.name == "nt" and str(npm_bin).lower().endswith((".cmd", ".bat")):
            # .cmd/.bat не запускаються напряму через CreateProcess —
            # виконуємо через cmd /c, інакше вікно мовчки не стартує.
            cmd = ["cmd", "/c"] + cmd

    log("Opening CyberDeck Electron UI...")
    # TCP-сокет для подій бекенд->вікно (stdin в Electron на Windows глухий:
    # пише без помилок, але вікно нічого не отримує). Команди вікно->бекенд
    # і далі йдуть через stdout-пайп (той працює — рескан доходить).
    # Порт передаємо вікні аргументом --py-port.
    import socket as _sockmod
    py_link = {"sock": None, "lock": threading.Lock(), "pending": [],
               "sock_failed": False, "srv": None}
    try:
        _srv = _sockmod.socket(_sockmod.AF_INET, _sockmod.SOCK_STREAM)
        _srv.setsockopt(_sockmod.SOL_SOCKET, _sockmod.SO_REUSEADDR, 1)
        _srv.bind(("127.0.0.1", 0))
        _srv.listen(1)
        _py_port = _srv.getsockname()[1]
        py_link["srv"] = _srv
        cmd = cmd + ["--py-port", str(_py_port)]
    except Exception as e:
        log(f"Socket for the window was not created ({e}) - events will go via stdin.")
        py_link["sock_failed"] = True
    # Дублікат вікна від минулого запуску = класична причина «порожнього списку»:
    # старе вікно (бекенд мертвий) перекриває нове. Попереджаємо одразу.
    try:
        _n = _count_electron_processes()
        if _n:
            _exe_name = "electron.exe" if os.name == "nt" else "electron"
            log(f"WARNING: {_exe_name} is already running ({_n}). If you see an empty window - "
                "close ALL app windows and start again (the old window does not refresh).")
    except Exception:
        pass
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1
        )
        log(f"UI process started (pid={proc.pid}).")
    except Exception as e:
        log(f"Failed to open Electron UI: {e}")
        return None

    result = {"ip": "", "port": 0, "ok": False, "done": False}
    seen_devices = {}
    dev_lock = threading.Lock()

    def send_event(event_type, data):
        if proc.poll() is not None:
            return
        try:
            line = json.dumps({"event": event_type, "data": data}, ensure_ascii=False)
        except Exception:
            return
        if event_type in ("device-found", "device-list", "connect-result", "pair-result"):
            n = len(data) if isinstance(data, list) else 1
            log(f">> to window: {event_type} (x{n})")
        payload = (line + "\n").encode("utf-8")
        if py_link["sock"] is not None:
            try:
                with py_link["lock"]:
                    py_link["sock"].sendall(payload)
                return
            except Exception:
                try:
                    py_link["sock"].close()
                except Exception:
                    pass
                py_link["sock"] = None
        if py_link.get("sock_failed"):
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except Exception:
                pass
        else:
            # Сокет ще не підключено — складаємо в чергу, відправимо при accept
            with py_link["lock"]:
                py_link["pending"].append(payload)
                py_link["pending"] = py_link["pending"][-300:]

    def _accept_loop():
        """Чекаємо підключення вікна по сокету, зливаємо чергу подій."""
        srv = py_link.get("srv")
        if srv is None:
            return
        srv.settimeout(1.0)
        t0 = time.time()
        while proc.poll() is None and time.time() - t0 < 25:
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log("Window connected via socket - events will go directly.")
            with py_link["lock"]:
                py_link["sock"] = conn
                pend = py_link["pending"]
                py_link["pending"] = []
            for pl in pend[-300:]:
                try:
                    conn.sendall(pl)
                except Exception:
                    break
            return
        if py_link["sock"] is None and proc.poll() is None:
            log("Window did not connect via socket (25s) - events will go via stdin.")
            py_link["sock_failed"] = True

    threading.Thread(target=_accept_loop, daemon=True).start()

    def report_device(device):
        key = (device.get('kind', 'lan'), device.get('ip'), device.get('port'))
        with dev_lock:
            if key in seen_devices:
                old = seen_devices[key]
                if old.get('name') == "polling..." and device.get('name') != "polling...":
                    seen_devices[key] = device
                else:
                    return
            else:
                if device.get('kind') == 'lan' and any(d.get('ip') == device.get('ip') and d.get('kind') == 'connect' for d in seen_devices.values()):
                    return
                seen_devices[key] = device

        send_event('device-found', {
            'ip': device['ip'],
            'port': device['port'],
            'name': device.get('name', 'Android Device'),
            'kind': device.get('kind', 'lan'),
            'state': device.get('state', 'device')
        })

    def run_full_scan():
        send_event('status-update', 'Looking for devices via mDNS and on the network...')
        # 0) Останній відомий пристрій
        last = load_last_phone()
        if last:
            lip, lport = _split_serial(last, adb_port)
            if lip and _tcp_port_open(lip, lport, 0.4):
                name = get_device_display_name(f"{lip}:{lport}")
                report_device({
                    'kind': 'connect',
                    'ip': lip,
                    'port': lport,
                    'name': name if name and 'unknown' not in name else f"Last known ({lip})",
                    'state': 'device'
                })

        # 1) mDNS (Бездротове налагодження)
        try:
            mdevs = mdns_discover_adb(timeout=mdns_timeout)
            for d in mdevs:
                host = (d.get("host") or d.get("instance") or "").replace(".local", "")
                ip = d["ips"][0] if d.get("ips") else ""
                if not ip:
                    continue
                item = {
                    "kind": d["kind"],
                    "ip": ip,
                    "port": d["port"],
                    "name": host or "Android Device",
                    "state": "device"
                }
                if d["kind"] == "connect":
                    try:
                        name = get_device_display_name(f"{ip}:{d['port']}")
                        if name and "unknown" not in name:
                            item["name"] = name
                    except Exception:
                        pass
                report_device(item)
        except Exception as e:
            send_event('status-update', f'mDNS: {e}')

        # 2) --ip-only: БЕЗ скану портів — повне виявлення живих IP (ping+arp),
        # порти підберуться точково на вибраному рядку. Інакше — скан портів.
        if SKIP_PORT_SCAN:
            send_event('status-update', 'Looking for live hosts on the network (no port scan)...')
            try:
                _mports = []
                for _d in (mdevs or []):
                    if _d.get("kind") == "connect" and _d.get("port"):
                        _p = int(_d["port"])
                        if _p not in _mports:
                            _mports.append(_p)
            except Exception:
                _mports = []
            try:
                _iports = get_scan_ports(adb_port, extra_ports=_extra_scan_ports,
                                         include_last=True, mdns_ports=_mports)
            except Exception:
                _iports = [adb_port]
            alive = []
            try:
                with dev_lock:
                    known_ips = {d.get('ip') for d in seen_devices.values() if d.get('ip')}
                for aip in discover_alive_ips():
                    if aip not in known_ips:
                        known_ips.add(aip)
                        alive.append(aip)
                        report_device({
                            "kind": "net",
                            "ip": aip,
                            "port": 0,
                            "name": "Host on network (port unknown)",
                            "state": "unknown"
                        })
            except Exception as e:
                send_event('status-update', f'Host search: {e}')
            # У кого відповідає ADB — міняємо непідписаний рядок на підписаний
            if alive:
                send_event('status-update', 'Checking who has ADB open...')
                try:
                    for _aip, _info in identify_hosts(alive, _iports).items():
                        with dev_lock:
                            seen_devices.pop(('net', _aip, 0), None)
                        try:
                            _port = _split_serial(_info.get("serial", ""), adb_port)[1]
                        except Exception:
                            _port = adb_port
                        report_device({
                            "kind": "lan",
                            "ip": _info["ip"],
                            "port": _port,
                            "name": _info.get("name", "Android Device"),
                            "state": _info.get("state", "device")
                        })
                except Exception:
                    pass
        else:
            # LAN-сканування: 5555 + порт останнього пристрою + --scan-ports +
            # динамічні порти з mDNS (Бездротове налагодження дає випадковий порт,
            # і скан тільки 5555 такі телефони не бачив).
            mdns_ports = []
            try:
                for _d in (mdevs or []):
                    if _d.get("kind") == "connect" and _d.get("port"):
                        _p = int(_d["port"])
                        if _p not in mdns_ports:
                            mdns_ports.append(_p)
            except Exception:
                pass
            lan_ports = get_scan_ports(adb_port, extra_ports=_extra_scan_ports,
                                       include_last=True, mdns_ports=mdns_ports)

            def lan_open(ip, port=None):
                report_device({
                    "kind": "lan",
                    "ip": ip,
                    "port": port if port else lan_ports[0],
                    "name": "polling...",
                    "state": "device"
                })

            def lan_named(info):
                report_device({
                    "kind": "lan",
                    "ip": info["ip"],
                    "port": info.get("port", lan_ports[0]),
                    "name": info.get("name", "Android Device"),
                    "state": info.get("state", "device")
                })

            try:
                scan_lan_with_names(
                    adb_port=adb_port,
                    timeout=scan_timeout,
                    on_port_open=lan_open,
                    on_found=lan_named,
                    adb_ports=lan_ports
                )
            except Exception as e:
                send_event('status-update', f'LAN scan: {e}')

        # 3) Живі хости БЕЗ відомих ADB-портів: шукаємо самі IP (швидко, по arp,
        # без скану портів) і показуємо рядком — клік підбере порт перебором.
        try:
            with dev_lock:
                known_ips = {d.get('ip') for d in seen_devices.values() if d.get('ip')}
            for aip in discover_alive_ips(fast=True):
                if aip not in known_ips:
                    report_device({
                        "kind": "net",
                        "ip": aip,
                        "port": 0,
                        "name": "Host on network (port unknown)",
                        "state": "unknown"
                    })
        except Exception:
            pass

        send_event('scan-progress', {'done': True, 'count': len(seen_devices)})
        # Контрольний знімок ВСЬОГО списку: окремі device-found могли загубитись
        # (рескан під час скану, перестворення вікна) — знімок гарантує що UI
        # покаже те саме що знайшов бекенд.
        try:
            with dev_lock:
                snap = [
                    {'ip': d.get('ip'), 'port': d.get('port'),
                     'name': d.get('name', 'Android Device'),
                     'kind': d.get('kind', 'lan'),
                     'state': d.get('state', 'device')}
                    for d in seen_devices.values()
                ]
            send_event('device-list', snap)
        except Exception:
            pass
        send_event('status-update', f'Search finished. Found {len(seen_devices)} devices.')

    threading.Thread(target=run_full_scan, daemon=True).start()

    def read_electron_output():
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
                cmd_type = cmd.get('type')
                if cmd_type == 'scan':
                    with dev_lock:
                        seen_devices.clear()
                    threading.Thread(target=run_full_scan, daemon=True).start()
                elif cmd_type == 'connect':
                    ip = cmd['ip']
                    port = int(cmd.get('port', adb_port))
                    send_event('status-update', f'Connecting to {ip}:{port}...')
                    def do_connect(tip=ip, tport=port):
                        # Порт 0 = «невідомий» (рядок живого хоста): одразу підбираємо самі.
                        ok = adb_connect_fast(tip, tport) if tport else False
                        eff_port = tport
                        if not ok:
                            # Порт міг бути динамічним: підбираємо самі (прощуп + перебір)
                            send_event('status-update', f'Port {tport} is silent, picking a port for {tip} myself...')
                            try:
                                found = _find_connect_port_after_pair(
                                    tip, tport, extra_ports=_extra_scan_ports)
                            except Exception:
                                found = 0
                            if found and adb_connect_fast(tip, found, timeout=6.0):
                                ok = True
                                eff_port = found
                        name = get_device_display_name(f"{tip}:{eff_port}") if ok else ""
                        if ok:
                            save_last_phone(f"{tip}:{eff_port}")
                            send_event('connect-result', {'success': True, 'ip': tip, 'port': eff_port, 'name': name})
                            send_event('status-update', f'Connected to {tip}:{eff_port}')
                            time.sleep(0.6)
                            send_event('close-window', {})
                            result.update(ip=tip, port=eff_port, ok=True, done=True)
                        else:
                            send_event('connect-result', {'success': False, 'ip': tip, 'port': tport, 'error': 'Failed to connect (port from the "IP address & Port" screen?)'})
                            send_event('status-update', f'Connection error to {tip}:{tport}')
                    threading.Thread(target=do_connect, daemon=True).start()
                elif cmd_type == 'pair':
                    ip = cmd['ip']
                    port = int(cmd['port'])
                    code = str(cmd['code']).strip()
                    send_event('status-update', f'Pairing with {ip}:{port}...')
                    def do_pair(tip=ip, tport=port, tcode=code):
                        def _finish_connect(cp):
                            conn_ok = adb_connect_fast(tip, cp, timeout=6.0)
                            name = get_device_display_name(f"{tip}:{cp}") if conn_ok else ""
                            if conn_ok:
                                save_last_phone(f"{tip}:{cp}")
                                send_event('connect-result', {'success': True, 'ip': tip, 'port': cp, 'name': name})
                                send_event('status-update', f'Connected: {tip}:{cp} - {name}')
                                time.sleep(0.6)
                                send_event('close-window', {})
                                result.update(ip=tip, port=cp, ok=True, done=True)
                            return conn_ok
                        ok = adb_pair_verify(tip, tport, tcode)
                        if ok:
                            send_event('pair-result', {'success': True, 'ip': tip, 'port': tport})
                            send_event('status-update', 'Paired successfully! Looking for the connection port...')
                            try:
                                devs = mdns_discover_adb(timeout=2.0)
                                same = [x for x in devs if x["kind"] == "connect" and x["ips"] and x["ips"][0] == tip]
                                if same and _finish_connect(same[0]["port"]):
                                    return
                            except Exception:
                                pass
                            # mDNS не дав порту — прощуп + перебір портів цього IP
                            send_event('status-update', 'mDNS gave no port - looking for the port (probe, then brute force)...')
                            try:
                                found = _find_connect_port_after_pair(
                                    tip, tport, extra_ports=_extra_scan_ports)
                            except Exception:
                                found = 0
                            if found and _finish_connect(found):
                                return
                            send_event('status-update',
                                       'Connection port not found. Enter it manually: '
                                       'on the phone "IP address & Port" -> the "Manual" form -> Connect.')
                        else:
                            send_event('pair-result', {'success': False, 'ip': tip, 'port': tport, 'error': 'Pairing failed'})
                            send_event('status-update', 'Pairing failed. Check the code.')
                    threading.Thread(target=do_pair, daemon=True).start()
                elif cmd_type == 'close':
                    result['done'] = True
            except Exception:
                # Не JSON — службові рядки самого вікна (логи пересилки подій тощо)
                if line:
                    print(f"[UI] {line}", flush=True)

    t_read = threading.Thread(target=read_electron_output, daemon=True)
    t_read.start()

    time.sleep(0.5)
    if proc.poll() is not None:
        rc = proc.poll()
        err_tail = ""
        try:
            # Процес вже завершився — збираємо залишок stderr для діагностики.
            _rest = proc.stderr.read() or ""
            err_tail = _rest.strip().splitlines()
            err_tail = "\n".join(err_tail[-15:])
        except Exception:
            pass
        log(f"Electron failed to start (code {rc}) - falling back to the standard window...")
        if err_tail:
            log(f"Reason from stderr: {err_tail[:1500]}")
            low = err_tail.lower()
            if "no-sandbox" in low or ("root" in low and "sandbox" in low):
                log("This is a root launch: restart NOT as root, or Electron will add "
                    "--no-sandbox (update the code: ui/electron/main.js). Meanwhile: "
                    "run electron with --no-sandbox manually.")
            elif "libnss" in low or "libatk" in low or "cannot open shared object" in low \
                    or "error while loading shared libraries" in low:
                log("Electron system libraries are missing. Install: "
                    f"{_manual_install_cmd('libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2')} "
                    "or run: bash scripts/setup_linux.sh")
            elif "no display" in low or "missing x server" in low or "cannot open display" in low:
                log("No display: " + _display_missing_reason())
            elif "gpu" in low:
                log("Looks like a GPU stack crash (VM/old drivers): try "
                    "ELECTRON_DISABLE_GPU=1 ./START.sh")
        else:
            log("Hint: most often this is missing ui/node_modules - run: cd ui && npm install. "
                "Or see the full log above ([UI]/[UI-ERR]). "
                "Diagnostics: ./START.sh check")
        return None

    def _drain_stderr():
        # stderr інакше ніколи не читається: при verbose-логах Electron труба
        # переповнюється і вікно зависає. Зливаємо в [UI-ERR].
        try:
            for _line in proc.stderr:
                _line = (_line or "").strip()
                if _line:
                    print(f"[UI-ERR] {_line}", flush=True)
        except Exception:
            pass

    threading.Thread(target=_drain_stderr, daemon=True).start()

    log("CyberDeck Electron UI opened.")
    while proc.poll() is None and not result['done']:
        time.sleep(0.1)

    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        if py_link.get("sock") is not None:
            py_link["sock"].close()
    except Exception:
        pass
    try:
        if py_link.get("srv") is not None:
            py_link["srv"].close()
    except Exception:
        pass

    if result['ok']:
        return result['ip'], result['port'], True

    return "", 0, False


def gui_pick_and_connect(adb_port=5555, mdns_timeout=2.5, scan_timeout=0.45, pair_code=None,
                       scan_ports=None):
    """Вікно зі списком телефонів (mDNS + LAN), парування і підключення."""
    res = electron_gui_pick_and_connect(adb_port, mdns_timeout, scan_timeout, pair_code,
                                        scan_ports=scan_ports)
    if res is not None:
        return res
    log("CyberDeck unavailable - opening the standard window (tkinter)...")
    return _tkinter_gui_pick_and_connect(adb_port, mdns_timeout, scan_timeout, pair_code,
                                         scan_ports=scan_ports)


def _tkinter_gui_pick_and_connect(adb_port=5555, mdns_timeout=2.5, scan_timeout=0.45, pair_code=None,
                                 scan_ports=None):
    """Вікно зі списком телефонів (mDNS + LAN), парування і підключення.

    Повертає (ip, port, ok) або None якщо вікно відкрити неможливо (нема дисплея).
    Викликати з головного потоку. Сканування і adb-операції — у фонових потоках.
    """
    import queue
    import threading
    import tkinter as tk
    from tkinter import ttk

    if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError(_display_missing_reason() or "no display (DISPLAY/WAYLAND_DISPLAY)")

    _extra_scan_ports = [int(p) for p in (scan_ports or []) if str(p).isdigit()]

    result = {"ip": "", "port": 0, "ok": False}
    state = {"closed": False, "cancelled": False}
    devices = []
    dev_lock = threading.Lock()
    q = queue.Queue()

    try:
        root = tk.Tk()
    except Exception as e:
        # Типово Linux: _tkinter.TclError: couldn't connect to display / no display name
        msg = str(e)
        if "display" in msg.lower() or "couldn't connect" in msg.lower():
            raise RuntimeError(f"{_display_missing_reason()} (tkinter: {msg})")
        raise
    root.title("Virtual USB Cable - phone connection")
    root.geometry("600x560")
    root.minsize(520, 480)
    try:
        root.eval("tk::PlaceWindow . center")
    except Exception:
        pass
    try:
        root.attributes("-topmost", True)  # як Bluetooth-діалог — поверх усіх
    except Exception:
        pass

    status_var = tk.StringVar(value="Looking for the phone over Wi-Fi...")

    list_frame = ttk.Frame(root)
    list_frame.pack(fill="both", expand=True, padx=10, pady=(10, 5))
    lb = tk.Listbox(list_frame, height=11)
    sb = ttk.Scrollbar(list_frame, orient="vertical", command=lb.yview)
    lb.configure(yscrollcommand=sb.set)
    lb.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    prog = ttk.Progressbar(root, mode="indeterminate")
    prog.pack(fill="x", padx=10, pady=(0, 4))
    prog.start(15)

    status = ttk.Label(root, textvariable=status_var, wraplength=570)
    status.pack(fill="x", padx=10, pady=2)

    btn_frame = ttk.Frame(root)
    btn_frame.pack(fill="x", padx=10, pady=5)
    btn_connect = ttk.Button(btn_frame, text="Connect")
    btn_rescan = ttk.Button(btn_frame, text="Refresh")
    btn_manual_toggle = ttk.Button(btn_frame, text="Manual...")
    btn_cancel = ttk.Button(btn_frame, text="Skip (demo)")
    for b in (btn_connect, btn_rescan, btn_manual_toggle, btn_cancel):
        b.pack(side="left", padx=(0, 6))

    pair_frame = ttk.LabelFrame(root, text="Pairing (code from the phone screen)")
    code_var = tk.StringVar(value=pair_code or "")
    ttk.Label(pair_frame, text="Code:").pack(side="left", padx=(6, 2))
    code_entry = ttk.Entry(pair_frame, textvariable=code_var, width=12)
    code_entry.pack(side="left")
    btn_pair = ttk.Button(pair_frame, text="Pair and connect")
    btn_pair.pack(side="left", padx=6)

    manual_frame = ttk.LabelFrame(root, text="Manual (from screen: Wireless debugging)")
    man_vars = {}
    for key, label, width in (("ip", "IP:", 16), ("pair", "Pairing port:", 8),
                              ("code", "Code:", 8), ("conn", "Connection port:", 8)):
        row = ttk.Frame(manual_frame)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text=label, width=16).pack(side="left")
        var = tk.StringVar()
        ttk.Entry(row, textvariable=var, width=width).pack(side="left")
        man_vars[key] = var
    btn_manual_go = ttk.Button(manual_frame, text="Pair and connect")
    btn_manual_go.pack(padx=6, pady=4)

    def set_status(text):
        log(text)
        q.put(("status", text))

    def set_busy(busy):
        q.put(("busy", busy))

    def refresh_list(items):
        # Зберігаємо вибір користувача при живому оновленні списку
        sel = lb.curselection()
        sel_key = None
        if sel:
            try:
                cur = lb.get(sel[0])
                sel_key = cur
            except Exception:
                pass
        lb.delete(0, "end")
        restore = None
        for i, d in enumerate(items):
            if d["kind"] == "pair":
                text = f"[pair] {d['name']} - {d['ip']}:{d['port']}"
            elif d["kind"] == "connect":
                text = f"[Wi-Fi] {d['name']} — {d['ip']}:{d['port']}"
            elif d["kind"] == "net":
                text = f"[IP] {d['ip']} - port will be auto-detected"
            else:
                text = f"[5555] {d['name']} — {d['ip']}:{d['port']}"
            lb.insert("end", text)
            if sel_key is not None and text == sel_key:
                restore = i
        if restore is not None:
            lb.selection_set(restore)
            lb.see(restore)
        elif items and not sel:
            lb.selection_set(0)

    def current():
        sel = lb.curselection()
        if not sel:
            return None
        with dev_lock:
            return devices[sel[0]] if sel[0] < len(devices) else None

    def snapshot():
        with dev_lock:
            return list(devices)

    def add_device(item):
        with dev_lock:
            for i, x in enumerate(devices):
                if (x["kind"], x["ip"], x["port"]) == (item["kind"], item["ip"], item["port"]):
                    devices[i] = item
                    break
            else:
                # Той самий IP з mDNS вже є — LAN-дублікат не додаємо
                if item["kind"] == "lan" and any(x["ip"] == item["ip"] for x in devices):
                    return
                devices.append(item)
            snapshot = list(devices)
        q.put(("devices", snapshot))

    def do_scan():
        # 0) Швидкий шлях: останній відомий пристрій (як BT-перепідключення, ~1-2с)
        last = load_last_phone()
        if last and not state["closed"]:
            lip, lport = _split_serial(last, adb_port)
            set_status(f"Reconnecting to the known one: {last}...")
            try:
                if _tcp_port_open(lip, lport, 0.5) and adb_connect_fast(lip, lport, timeout=4.0):
                    name = get_device_display_name(f"{lip}:{lport}")
                    save_last_phone(f"{lip}:{lport}")
                    result.update(ip=lip, port=lport, ok=True)
                    q.put(("connect_result", True, f"Connected: {lip}:{lport} - {name}"))
                    q.put(("scan_done",))
                    return
            except Exception:
                pass
        if state["closed"]:
            return
        # 1) mDNS (Бездротове налагодження) — зазвичай відповідає за <1с
        set_status("Looking via mDNS (Wireless debugging)...")
        try:
            mdevs = mdns_discover_adb(timeout=mdns_timeout)
        except Exception as e:
            mdevs = []
            set_status(f"mDNS failed: {e}")
        for d in mdevs:
            if state["closed"]:
                return
            host = (d["host"] or d["instance"]).replace(".local", "")
            item = {"kind": d["kind"], "ip": d["ips"][0], "port": d["port"],
                    "name": host, "raw": d}
            if d["kind"] == "connect":
                # Реальну модель підтягуємо одразу (швидкий connect + getprop)
                try:
                    subprocess.run(["adb", "connect", f"{item['ip']}:{item['port']}"],
                                   capture_output=True, timeout=20)
                    name = get_device_display_name(f"{item['ip']}:{item['port']}")
                    if name and "unknown" not in name:
                        item["name"] = name
                except Exception:
                    pass
            add_device(item)
        if state["closed"]:
            return
        # 2) --ip-only: БЕЗ скану портів — тільки живі IP, інакше скан портів.
        if SKIP_PORT_SCAN:
            set_status("Looking for live hosts on the network (no port scan)...")
            alive = []
            try:
                for aip in discover_alive_ips():
                    if state["closed"]:
                        return
                    alive.append(aip)
                    add_device({"kind": "net", "ip": aip, "port": 0,
                                "name": "Host on network (port unknown)",
                                "raw": {"serial": f"{aip}:0"}})
            except Exception:
                pass
            # У кого відповідає ADB — міняємо непідписаний рядок на підписаний
            if alive and not state["closed"]:
                set_status("Checking who has ADB open...")
                try:
                    _mports = []
                    for _d in (mdevs or []):
                        if _d.get("kind") == "connect" and _d.get("port"):
                            _p = int(_d["port"])
                            if _p not in _mports:
                                _mports.append(_p)
                    _iports = get_scan_ports(adb_port, extra_ports=_extra_scan_ports,
                                             include_last=True, mdns_ports=_mports)
                except Exception:
                    _iports = [adb_port]
                try:
                    for _aip, _info in identify_hosts(alive, _iports).items():
                        if state["closed"]:
                            return
                        try:
                            _port = _split_serial(_info.get("serial", ""), adb_port)[1]
                        except Exception:
                            _port = adb_port
                        with dev_lock:
                            devices[:] = [x for x in devices
                                          if not (x.get("kind") == "net" and x.get("ip") == _aip)]
                            snap = list(devices)
                        q.put(("devices", snap))
                        add_device({"kind": "lan", "ip": _info["ip"], "port": _port,
                                    "name": _info.get("name", "Android Device"),
                                    "raw": _info})
                except Exception:
                    pass
            q.put(("scan_done",))
            return
        # LAN-скан: пріоритет — підмережа останнього IP; рядки з'являються наживо.
        # Скануємо 5555 + порт останнього пристрою + --scan-ports + mDNS-порти
        # (динамічні порти Бездротового налагодження інакше не знайти).
        mdns_ports = []
        try:
            for _d in (mdevs or []):
                if _d.get("kind") == "connect" and _d.get("port"):
                    _p = int(_d["port"])
                    if _p not in mdns_ports:
                        mdns_ports.append(_p)
        except Exception:
            pass
        lan_ports = get_scan_ports(adb_port, extra_ports=_extra_scan_ports,
                                   include_last=True, mdns_ports=mdns_ports)
        set_status(f"Scanning LAN (ports {', '.join(str(p) for p in lan_ports)})...")
        last_ip = _split_serial(load_last_phone(), adb_port)[0] or None

        def lan_open(ip, port=None):
            if state["closed"]:
                return
            _p = port if port else lan_ports[0]
            add_device({"kind": "lan", "ip": ip, "port": _p,
                        "name": "polling...", "raw": {"serial": f"{ip}:{_p}"}})

        def lan_named(info):
            if state["closed"]:
                return
            add_device({"kind": "lan", "ip": info["ip"], "port": info.get("port", lan_ports[0]),
                        "name": info["name"], "raw": info})

        try:
            # Імена/рядки приходять через колбеки наживо; повернений список тут не потрібен
            scan_lan_with_names(adb_port=adb_port, timeout=scan_timeout,
                                first_ip=last_ip, on_port_open=lan_open,
                                on_found=lan_named, adb_ports=lan_ports)
        except Exception:
            pass
        q.put(("scan_done",))

    def worker_connect(d):
        # Порт 0 = «невідомий» (рядок живого хоста): підбираємо самі.
        port = d["port"]
        set_status(f"Connecting to {d['ip']}:{port}...")
        ok = adb_connect_fast(d["ip"], port, timeout=6.0) if port else False
        if not ok:
            set_status(f"Port {port} is silent, picking a port for {d['ip']} myself...")
            try:
                found = _find_connect_port_after_pair(d["ip"], port,
                                                      extra_ports=_extra_scan_ports)
            except Exception:
                found = 0
            if found and adb_connect_fast(d["ip"], found, timeout=6.0):
                ok, port = True, found
        if ok:
            name = d["name"]
            if d["kind"] in ("connect", "net") or name == "polling...":
                name = get_device_display_name(f"{d['ip']}:{port}")
            save_last_phone(f"{d['ip']}:{port}")
            _gui_disconnect_others(snapshot(), d["ip"])
            result.update(ip=d["ip"], port=port, ok=True)
            q.put(("connect_result", True, f"Connected: {d['ip']}:{port} - {name}"))
        else:
            q.put(("connect_result", False, f"Failed to connect to {d['ip']}:{d['port']}."))
        set_busy(False)

    def worker_pair(d, code):
        ip, pp = d["ip"], d["port"]
        set_status(f"Pairing with {ip}:{pp}...")
        if not adb_pair_verify(ip, pp, code):
            q.put(("connect_result", False, "Pairing failed. The code is one-time - refresh the screen."))
            set_busy(False)
            return
        set_status("Looking for this phone's connection port...")
        try:
            devs = mdns_discover_adb(timeout=2.0)
        except Exception:
            devs = []
        same = [x for x in devs if x["kind"] == "connect" and x["ips"] and x["ips"][0] == ip]
        if not same:
            # mDNS не дав порту — прощуп + перебір портів цього IP
            set_status("mDNS gave no port - looking for the port (probe, then brute force)...")
            try:
                found = _find_connect_port_after_pair(ip, pp,
                                                      extra_ports=_extra_scan_ports)
            except Exception:
                found = 0
            if not found:
                q.put(("status", "Port not found - enter it manually (IP address & Port)."))
                man_vars["ip"].set(ip)
                q.put(("show_manual",))
                set_busy(False)
                return
            cp = found
        else:
            cp = same[0]["port"]
        if adb_connect_fast(ip, cp, timeout=6.0):
            name = get_device_display_name(f"{ip}:{cp}")
            save_last_phone(f"{ip}:{cp}")
            _gui_disconnect_others(snapshot(), ip)
            result.update(ip=ip, port=cp, ok=True)
            q.put(("connect_result", True, f"Connected WITHOUT cable: {ip}:{cp} - {name}"))
        else:
            q.put(("connect_result", False, "Connection failed."))
        set_busy(False)

    def worker_manual(ip, pair_s, code, conn_s):
        if pair_s:
            set_status(f"Pairing with {ip}:{pair_s}...")
            if not adb_pair_verify(ip, int(pair_s), code):
                q.put(("connect_result", False, "Pairing failed. The code is one-time - refresh the screen."))
                set_busy(False)
                return
        set_status(f"Connecting to {ip}:{conn_s}...")
        if adb_connect_fast(ip, int(conn_s), timeout=6.0):
            name = get_device_display_name(f"{ip}:{conn_s}")
            save_last_phone(f"{ip}:{conn_s}")
            _gui_disconnect_others(snapshot(), ip)
            result.update(ip=ip, port=int(conn_s), ok=True)
            q.put(("connect_result", True, f"Connected WITHOUT cable: {ip}:{conn_s} - {name}"))
        else:
            q.put(("connect_result", False, "Connection failed."))
        set_busy(False)

    def on_connect():
        d = current()
        if not d:
            status_var.set("Select a device from the list.")
            return
        if d["kind"] == "pair":
            if not pair_frame.winfo_ismapped():
                pair_frame.pack(fill="x", padx=10, pady=5)
            code_entry.focus_set()
            status_var.set(f"For {d['ip']}:{d['port']} enter the code from the screen and press 'Pair and connect'.")
            return
        set_busy(True)
        threading.Thread(target=worker_connect, args=(d,), daemon=True).start()

    def on_pair_go():
        d = current()
        if not d or d["kind"] != "pair":
            with dev_lock:
                pairs = [x for x in devices if x["kind"] == "pair"]
            d = pairs[0] if pairs else None
        code = code_var.get().strip()
        if not d:
            status_var.set("Select a [pair] device from the list.")
            return
        if not code:
            status_var.set("Enter the code from the phone screen.")
            code_entry.focus_set()
            return
        set_busy(True)
        threading.Thread(target=worker_pair, args=(d, code), daemon=True).start()

    def on_manual_go():
        ip = man_vars["ip"].get().strip()
        pair_s = man_vars["pair"].get().strip()
        code = man_vars["code"].get().strip() or (pair_code or "")
        conn_s = man_vars["conn"].get().strip()
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            status_var.set("Enter a valid phone IP.")
            return
        if pair_s and (not pair_s.isdigit() or not code):
            status_var.set("Pairing needs the port and the code from the screen.")
            return
        if not conn_s.isdigit():
            status_var.set("Enter the connection port (IP address & Port).")
            return
        set_busy(True)
        threading.Thread(target=worker_manual, args=(ip, pair_s, code, conn_s), daemon=True).start()

    def on_rescan():
        lb.delete(0, "end")
        with dev_lock:
            devices.clear()
        if not prog.winfo_ismapped():
            prog.pack(fill="x", padx=10, pady=(0, 4))
        prog.start(15)
        status_var.set("Looking for the phone over Wi-Fi...")
        threading.Thread(target=do_scan, daemon=True).start()

    def on_toggle_manual():
        if manual_frame.winfo_ismapped():
            manual_frame.pack_forget()
        else:
            manual_frame.pack(fill="x", padx=10, pady=5)

    def close_ok():
        state["closed"] = True
        try:
            root.destroy()
        except Exception:
            pass

    def on_cancel():
        state["cancelled"] = True
        close_ok()

    def on_queue():
        try:
            while True:
                msg = q.get_nowait()
                tag = msg[0]
                if tag == "status":
                    status_var.set(msg[1])
                elif tag == "devices":
                    refresh_list(msg[1])
                elif tag == "scan_done":
                    prog.stop()
                    prog.pack_forget()
                    with dev_lock:
                        n = len(devices)
                    if n:
                        status_var.set(f"Found: {n}. Select a device and press 'Connect'.")
                        try:
                            root.bell()
                        except Exception:
                            pass
                    else:
                        status_var.set("Nothing found. Enable Wireless debugging, 'Refresh' or 'Manual'.")
                elif tag == "busy":
                    busy = msg[1]
                    widgets = (btn_connect, btn_rescan, btn_manual_toggle,
                               btn_cancel, btn_pair, btn_manual_go)
                    for w in widgets:
                        try:
                            w.configure(state="disabled" if busy else "normal")
                        except Exception:
                            pass
                elif tag == "show_manual":
                    if not manual_frame.winfo_ismapped():
                        manual_frame.pack(fill="x", padx=10, pady=5)
                elif tag == "connect_result":
                    _, ok, text = msg
                    status_var.set(text)
                    if ok:
                        root.after(900, close_ok)
        except queue.Empty:
            pass
        if not state["closed"]:
            root.after(120, on_queue)

    btn_connect.configure(command=on_connect)
    btn_rescan.configure(command=on_rescan)
    btn_manual_toggle.configure(command=on_toggle_manual)
    btn_cancel.configure(command=on_cancel)
    btn_pair.configure(command=on_pair_go)
    btn_manual_go.configure(command=on_manual_go)
    lb.bind("<Double-Button-1>", lambda _e: on_connect())
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    threading.Thread(target=do_scan, daemon=True).start()
    root.after(120, on_queue)
    try:
        root.focus_force()
    except Exception:
        pass
    root.mainloop()
    if state["cancelled"] or not result["ok"]:
        if not result["ok"]:
            return "", 0, False
    return result["ip"], result["port"], result["ok"]


def _last_known_ip(default_port):
    """Останній відомий IP телефону (для пріоритету скану). Повертає (ip, port) або ('', port)."""
    last = load_last_phone()
    if last:
        return _split_serial(last, default_port)
    return "", default_port


def _try_lan_scan_pick(adb_port, scan_timeout, pick, scan_ports=None, mdns_timeout=2.5):
    """Допоміжне: просканувати LAN, показати назви, дати вибрати, підключитись.

    Повертає (ip, port, ok). Порт — реальний (може бути динамічний, не 5555).
    В --ip-only: без скану портів — список живих IP + автопідбір порту на вибраному.
    """
    if SKIP_PORT_SCAN:
        try:
            alive = discover_alive_ips()
        except Exception as e:
            log(f"Host search failed: {e}")
            return "", adb_port, False
        if not alive:
            return "", adb_port, False
        # Підписуємо у кого відповідає ADB — вибір з назвами, а не голими IP
        log("Checking who has ADB open...")
        try:
            _iports = get_scan_ports(adb_port, extra_ports=scan_ports, include_last=True)
        except Exception:
            _iports = [adb_port]
        try:
            identified = identify_hosts(alive, _iports)
        except Exception:
            identified = {}
        names = {ip: info.get("name", "") for ip, info in identified.items()}
        chosen_ip = ""
        if pick:
            p = str(pick).strip()
            if p.isdigit():
                idx = int(p)
                chosen_ip = alive[idx - 1] if 1 <= idx <= len(alive) else ""
            elif re.match(r"^\d+\.\d+\.\d+\.\d+$", p):
                chosen_ip = p
            if chosen_ip:
                log(f"Auto-select (--pick {pick}): {chosen_ip}.")
        if not chosen_ip:
            chosen_ip = interactive_pick_ip(alive, names=names)
        if not chosen_ip:
            return "", adb_port, False
        # Вже розпізнаний — конектимось одразу по відомому порту
        if chosen_ip in identified:
            try:
                _h, _p = _split_serial(identified[chosen_ip].get("serial", ""), adb_port)
                chosen_port = int(_p)
            except Exception:
                chosen_port = adb_port
            if adb_connect_fast(chosen_ip, chosen_port, timeout=5.0):
                save_last_phone(f"{chosen_ip}:{chosen_port}")
                return chosen_ip, chosen_port, True
        port, ok = guided_connect_by_ip(chosen_ip, adb_port=adb_port,
                                        extra_ports=scan_ports,
                                        mdns_timeout=mdns_timeout)
        if ok:
            return chosen_ip, port, True
        return "", adb_port, False
    try:
        first_ip = _last_known_ip(adb_port)[0] or None
        # mDNS-порти сюди: навіть якщо сам mDNS-хост не дістав IP, його порт
        # підкаже які динамічні порти сканувати по всій LAN.
        mdns_ports = []
        try:
            for _d in mdns_discover_adb(timeout=min(float(mdns_timeout), 2.5)):
                if _d.get("kind") == "connect" and _d.get("port"):
                    _p = int(_d["port"])
                    if _p not in mdns_ports:
                        mdns_ports.append(_p)
        except Exception:
            pass
        infos = scan_lan_with_names(adb_port=adb_port, timeout=scan_timeout, first_ip=first_ip,
                                    extra_ports=scan_ports, mdns_ports=mdns_ports)
    except Exception as e:
        log(f"LAN scan failed: {e}")
        return "", adb_port, False
    if not infos:
        return "", adb_port, False
    chosen_ip = interactive_pick_device(infos, adb_port, pick=pick)
    if not chosen_ip:
        return "", adb_port, False
    # Порт беремо з знайденого серійника (може бути динамічний, не 5555!)
    chosen_port = adb_port
    for _d in infos:
        if _d.get("ip") == chosen_ip:
            try:
                _h, _p = _split_serial(_d.get("serial", ""), adb_port)
                chosen_port = int(_p)
            except Exception:
                pass
            break
    # Порт уже відкритий і назву прочитано — вистачить швидкого конекту
    ok = adb_connect_fast(chosen_ip, chosen_port, timeout=5.0)
    if ok:
        save_last_phone(f"{chosen_ip}:{chosen_port}")
        return chosen_ip, chosen_port, True
    diagnose_connect_fail(chosen_ip, chosen_port)
    return "", adb_port, False


def _split_serial(serial, default_port):
    """'192.168.1.50:37123' -> ('192.168.1.50', 37123)."""
    try:
        host, _, port = serial.partition(":")
        return host, int(port) if port.isdigit() else default_port
    except Exception:
        return serial, default_port


def setup_phone_wifi(adb_port, phone_ip_override, dry_run, devs=None,
                     allow_scan=True, force_scan=False, scan_timeout=0.45, pick=None,
                     allow_wireless=True, mdns_timeout=2.5, pair_code=None,
                     use_gui=True, scan_ports=None):
    """Повний ланцюжок: СПОЧАТКУ вікно вибору, потім USB->tcpip->connect,
    БЕЗ кабелю (Бездротове налагодження), скан LAN.

    Повертає (adb_host, adb_port_eff, demo_needed). Порт окремо бо в Бездротового
    налагодження він динамічний (не 5555).
    scan_ports: додаткові порти для LAN-скану (--scan-ports).
    Без вікна: --no-gui / --pick / явно заданий --phone-ip (див. START_CONSOLE.bat).
    """
    if dry_run:
        log("(dry-run) would skip tcpip/pair/connect/LAN scan")
        return "127.0.0.1", adb_port, True
    # ВІКНО — ЗАВЖДИ першим (крім --no-gui / --pick / явно заданого --phone-ip):
    # список телефонів, клік → підключення, код парування — в тому ж вікні.
    # Закрили без вибору або вікно неможливе — тихо йдемо звичайним шляхом нижче.
    _window_tried = False
    if use_gui and not pick and not phone_ip_override:
        log("Opening the device picker window...")
        try:
            _w = gui_pick_and_connect(adb_port, mdns_timeout, scan_timeout, pair_code,
                                      scan_ports=scan_ports)
        except Exception as e:
            log(f"Window did not open ({e}) - continuing automatically in the console.")
            _w = None
        _window_tried = True
        if _w is not None:
            _wip, _wport, _wok = _w
            if _wok:
                return _wip, _wport, False
            log("Nothing selected in the window - continuing automatically.")
    # adb server вже запущено викликачем (main); список або передано, або читаємо свіжий
    if devs is None:
        devs = print_adb_devices()
    # вже є TCP device?
    already_tcp = ""
    for s, st, t in devs:
        if ":" in s and st == "device":
            already_tcp = s
            break
    if already_tcp and not force_scan:
        log(f"Wi-Fi device {already_tcp} already present - skipping tcpip.")
        save_last_phone(already_tcp)
        host, port = _split_serial(already_tcp, adb_port)
        return host, port, False
    if already_tcp and force_scan and (allow_scan or allow_wireless):
        # --scan: навіть якщо щось вже підключено, показуємо ВСІ доступні IP і даємо вибрати
        log(f"Forced scan (--scan): {already_tcp} already connected, but looking at all IPs on the network...")
        # Вікно вже показували на вході (_window_tried) — повторно не відкриваємо.
        if use_gui and not pick and not _window_tried:
            log("Opening the device picker window (--scan)...")
            try:
                _g = gui_pick_and_connect(adb_port, mdns_timeout, scan_timeout, pair_code,
                                          scan_ports=scan_ports)
            except Exception as e:
                log(f"Window did not open ({e}) - continuing in the console.")
                _g = None
            if _g is not None:
                _ip, _port, _ok = _g
                if _ok:
                    return _ip, _port, False
                log("Nothing selected in the window - trying the console path.")
        elif not _window_tried:
            if pick:
                log(f"Window skipped: --pick {pick} given (auto-select without window).")
            elif not use_gui:
                log("Window skipped: --no-gui mode (console only, see START_CONSOLE.bat).")
        if allow_wireless:
            ip, port, ok = _try_wireless_debug(mdns_timeout, pair_code, pick,
                                               adb_port=adb_port,
                                               extra_ports=scan_ports)
            if ok:
                return ip, port, False
        if allow_scan:
            ip, port, ok = _try_lan_scan_pick(adb_port, scan_timeout, pick,
                                              scan_ports=scan_ports, mdns_timeout=mdns_timeout)
            if ok:
                return ip, port, False
        log(f"Keeping the already connected {already_tcp}.")
        host, port = _split_serial(already_tcp, adb_port)
        return host, port, False
    if phone_ip_override:
        log(f"Given --phone-ip={phone_ip_override}, doing adb connect MYSELF...")
        ok = adb_connect_verify(phone_ip_override, adb_port)
        if ok:
            save_last_phone(f"{phone_ip_override}:{adb_port}")
            return phone_ip_override, adb_port, False
        # Порт міг бути динамічним (Бездротове налагодження): шукаємо реальний
        found = resolve_connect_port(phone_ip_override, adb_port=adb_port,
                                     mdns_timeout=mdns_timeout, extra_ports=scan_ports)
        if found and found != adb_port:
            log(f"Port {adb_port} is silent, trying the found {found}...")
            if adb_connect_verify(phone_ip_override, found):
                save_last_phone(f"{phone_ip_override}:{found}")
                return phone_ip_override, found, False
        return phone_ip_override, adb_port, True
    has_usb = any(":" not in s and st == "device" for s, st, t in devs)
    if not has_usb:
        # Телефон міг залишитись в tcpip-режимі з минулого запуску (USB тоді мертвий).
        # Пробуємо реанімацію: reconnect + останній відомий IP — все САМ.
        log("No USB devices. Trying resuscitation (the phone may have stayed in tcpip mode)...")
        run(["adb", "reconnect"], timeout=30)
        time.sleep(1.5)
        devs = print_adb_devices()
        for s, st, t in devs:
            if ":" in s and st == "device":
                log(f"Resuscitation worked: {s}.")
                save_last_phone(s)
                host, port = _split_serial(s, adb_port)
                return host, port, False
        last = load_last_phone()
        if last:
            log(f"Trying the last known device MYSELF: {last} ...")
            lip, lport = _split_serial(last, adb_port)
            # Спочатку швидкий TCP-пречек (0.5с), щоб не висіти на мертвому IP,
            # потім швидкий конект (без довгих ретраїв — пристрій же відомий).
            fast_ok = False
            try:
                if _tcp_port_open(lip, lport, 0.5):
                    fast_ok = adb_connect_fast(lip, lport, timeout=4.0)
            except Exception:
                fast_ok = False
            if fast_ok:
                save_last_phone(f"{lip}:{lport}")
                return lip, lport, False
        # Вікно вже показували на вході (_window_tried) — повторно не відкриваємо,
        # далі тільки консольний флоу.
        if use_gui and not pick and not _window_tried:
            log("Opening the device picker window...")
            try:
                g = gui_pick_and_connect(adb_port, mdns_timeout, scan_timeout, pair_code,
                                         scan_ports=scan_ports)
            except Exception as e:
                log(f"Window did not open ({e}) - continuing in the console.")
                g = None
            if g is not None:
                ip, port, ok = g
                if ok:
                    return ip, port, False
                log("Window closed with no selection - NOT going demo, trying the console path.")
                use_gui = False  # щоб нижче не відкривати вікно повторно
        elif not _window_tried:
            # Вікно мало відкритись, але пропущено — пояснюємо чому, інакше
            # виглядає як «UI не відкривається» без жодної підказки.
            if pick:
                log(f"Window skipped: --pick {pick} given (auto-select without window). "
                    "Remove --pick to see the window.")
            elif not use_gui:
                log("Window skipped: --no-gui mode (console only, see START_CONSOLE.bat).")
        # Без вікна (нема дисплея / --no-gui / --pick / вікно не допомогло):
        # консольний флоу.
        # СПОЧАТКУ пробуємо взагалі БЕЗ кабелю (Бездротове налагодження),
        # і тільки потім класичний скан порту 5555 (для тих хто вже робив tcpip по USB).
        if allow_wireless:
            ip, port, ok = _try_wireless_debug(mdns_timeout, pair_code, pick,
                                               adb_port=adb_port,
                                               extra_ports=scan_ports)
            if ok:
                return ip, port, False
        if allow_scan:
            log("")
            log("Looking at all available IP addresses on the network (5555 + dynamic Wi-Fi ports)...")
            ip, port, ok = _try_lan_scan_pick(adb_port, scan_timeout, pick,
                                              scan_ports=scan_ports, mdns_timeout=mdns_timeout)
            if ok:
                return ip, port, False
            log("Nothing suitable selected on the network.")
        log("Resuscitation failed. Options:")
        log("  - WITHOUT CABLE (Android 11+): enable 'Wireless debugging' and run again - I will offer code pairing")
        log("  - Replug the USB cable and (if the phone is stuck in tcpip) reboot the phone, then START.bat")
        log("  - Or pass the IP manually: --phone-ip <IP from Settings -> About phone -> Status>")
        log("  - Or demo without phone: START.bat demo")
        return "127.0.0.1", adb_port, True
    # КРОК 1: IP визначаємо ДО tcpip, поки USB живий
    log("Detecting the phone IP BEFORE adbd restart (while USB is alive)...")
    candidates = detect_phone_ips()
    log(f"USB found - switching to Wi-Fi MYSELF: adb tcpip {adb_port} ...")
    run(["adb", "tcpip", str(adb_port)], timeout=30)
    # adbd рестартиться 3-5с — чекаємо довше ніж раніше (було 2с, цього мало)
    log("Waiting 4s for adbd to restart in TCP mode...")
    time.sleep(4)
    if not candidates:
        log("IP was not detected before tcpip - retrying (may fail, USB is already dead)...")
        candidates = detect_phone_ips()
    if not candidates:
        log("Could not detect IP over USB. Trying WITHOUT cable, then network scan...")
        if allow_wireless:
            ip, port, ok = _try_wireless_debug(mdns_timeout, pair_code, pick,
                                               adb_port=adb_port,
                                               extra_ports=scan_ports)
            if ok:
                return ip, port, False
        if allow_scan:
            ip, port, ok = _try_lan_scan_pick(adb_port, scan_timeout, pick,
                                              scan_ports=scan_ports, mdns_timeout=mdns_timeout)
            if ok:
                return ip, port, False
        log("Could not detect IP. Pass it manually: --phone-ip <phone IP>")
        return "127.0.0.1", adb_port, True
    # КРОК 2: пробуємо КОЖНОГО кандидата з перевіркою
    for ip in candidates:
        if adb_connect_verify(ip, adb_port):
            save_last_phone(f"{ip}:{adb_port}")
            return ip, adb_port, False
    diagnose_connect_fail(candidates[0], adb_port)
    # USB-кандидати не підійшли — пробуємо без кабелю, потім інші пристрої в LAN
    if allow_wireless:
        log("")
        log("USB candidates did not fit. Trying WITHOUT cable (Wireless debugging)...")
        ip, port, ok = _try_wireless_debug(mdns_timeout, pair_code, pick,
                                           adb_port=adb_port,
                                           extra_ports=scan_ports)
        if ok:
            return ip, port, False
    if allow_scan:
        log("")
        log("Looking at all available IP addresses on the network...")
        ip, port, ok = _try_lan_scan_pick(adb_port, scan_timeout, pick,
                                          scan_ports=scan_ports, mdns_timeout=mdns_timeout)
        if ok:
            return ip, port, False
    return "127.0.0.1", adb_port, True

# ---------------- якість каналу: хотспот, замір RTT/втрат, авто-профіль ----------------
#
# Типова причина лагів: роздача інтернету з ПК (хотспот Windows) зазвичай працює на
# перевантаженому 2.4 ГГц + NAT, з великим джиттером і втратами. Транспорт і бітрейт
# за замовчуванням налаштовані під швидкий роутер, тому на хотспоті відео "пливе".
# Лікується: визначенням хотспоту -> швидким заміром каналу -> зниженням бітрейту/
# роздільності/fps у scrcpy + збільшенням jitter-буфера приймача.

HOTSPOT_SUBNET = "192.168.137.0/24"  # стандартна підмережа хотспоту/ICS Windows


def _decode_any(b):
    """Декодувати вивід консольної утиліти (OEM-кодування непередбачуване)."""
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")


def get_adapter_info():
    """Список мережевих інтерфейсів: [{adapter, desc, ip, prefix}]. Тільки ASCII-ознаки."""
    infos = []
    cur = {"adapter": "", "desc": "", "ip": "", "prefix": 24}
    have_ip = False

    def flush():
        if cur["ip"] and _is_usable_lan(cur["ip"]):
            infos.append(dict(cur))

    try:
        r = subprocess.run(["ipconfig"] if os.name == "nt" else ["ip", "-4", "addr", "show"],
                           capture_output=True, timeout=15)
        out = _decode_any(r.stdout or b"")
    except Exception:
        return infos
    if os.name == "nt":
        # Мовно-незалежно, тільки ASCII-ознаки:
        # - порожній рядок = межа секцій (скидаємо ім'я адаптера, щоб не приписати чуже);
        # - '*' в заголовку без IP = віртуальний адаптер роздачі ("Local Area Connection* 12",
        #   зірочка є в усіх мовах); EN-заголовки містять 'adapter';
        # - 'Description' (імена драйверів зазвичай англійські навіть на локалізованій Windows);
        # - 'IPv4' є і в EN, і в RU/UA ("IPv4-адрес"); рядок 'Connection-specific DNS Suffix .:'
        #   ігноруємо (нема '*'/'adapter'/'IPv4').
        for line in out.splitlines():
            s = line.strip()
            if not s:
                # Межа секцій: зберігаємо зібране, але ім'я НЕ скидаємо тут —
                # після заголовка теж йде порожній рядок, а новий заголовок
                # і так почне секцію заново (див. гілку нижче).
                if have_ip:
                    flush()
                    cur = {"adapter": "", "desc": "", "ip": "", "prefix": 24}
                    have_ip = False
                continue
            no_ip = not re.search(r"\d+\.\d+\.\d+\.\d+", s)
            if s.endswith(":") and no_ip and ("*" in s or "adapter" in s.lower()):
                if have_ip:
                    flush()
                cur = {"adapter": s[:-1].strip(), "desc": "", "ip": "", "prefix": 24}
                have_ip = False
                continue
            low = s.lower()
            if "description" in low and ":" in s:
                cur["desc"] = s.split(":", 1)[1].strip()
                continue
            ips_in_line = re.findall(r"(\d+\.\d+\.\d+\.\d+)", s)
            if not ips_in_line:
                continue
            masks = [ip for ip in ips_in_line if ip.startswith("255.")]
            if masks and cur["ip"]:
                cur["prefix"] = _prefix_from_netmask(masks[0])
                continue
            if re.search(r"IPv4", s, re.IGNORECASE):
                for ip in ips_in_line:
                    if not ip.startswith("255.") and _is_usable_lan(ip):
                        if have_ip:
                            flush()
                            cur = {"adapter": cur["adapter"], "desc": cur["desc"],
                                   "ip": "", "prefix": 24}
                        cur["ip"] = ip
                        have_ip = True
                        break
        if have_ip:
            flush()
    else:
        for m in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", out):
            if _is_usable_lan(m.group(1)):
                infos.append({"adapter": "", "desc": "", "ip": m.group(1),
                              "prefix": int(m.group(2))})
    return infos


def detect_hotspot(phone_ip):
    """Чи підключено телефон через роздачу з ПК (хотспот)? Повертає (так/ні, причина)."""
    reasons = []
    try:
        if phone_ip and ipaddress.ip_address(phone_ip.strip()) in ipaddress.ip_network(HOTSPOT_SUBNET):
            reasons.append(f"Phone IP {phone_ip} is in the standard Windows hotspot subnet ({HOTSPOT_SUBNET})")
    except Exception:
        pass
    try:
        for a in get_adapter_info():
            try:
                same_net = (phone_ip and ipaddress.ip_address(phone_ip.strip())
                            in ipaddress.ip_network(f"{a['ip']}/{a['prefix']}", strict=False))
            except Exception:
                same_net = False
            if not same_net:
                continue
            name = (a["adapter"] + " " + a["desc"]).lower()
            if "*" in a["adapter"]:
                reasons.append(f"interface '{a['adapter']}' - virtual sharing adapter (hosted network)")
            if "wi-fi direct" in name or "hotspot" in name or "hosted" in name:
                reasons.append(f"interface '{a['adapter']}' ({a['desc']}) - Wi-Fi sharing from the PC")
    except Exception:
        pass
    if reasons:
        return True, "; ".join(dict.fromkeys(reasons))
    return False, ""


def measure_link(ip, count=3):
    """Швидкий замір каналу до телефону: ping. Повертає dict(ok, rtt_avg, rtt_max, jitter, loss)."""
    res = {"ok": False, "rtt_avg": None, "rtt_min": None, "rtt_max": None, "jitter": None, "loss": 100}
    if os.name == "nt":
        cmd = ["ping", "-n", str(count), "-w", "800", ip]
    else:
        cmd = ["ping", "-c", str(count), "-W", "1", ip]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15 + count * 2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return res
    out = _decode_any(r.stdout or b"")
    # Часи відповідей: 'time=5ms' / 'время=5мс' (знак може бути = або <;
    # кирилична 'м' — окремий символ, тому клас [mм])
    times = [int(x) for x in re.findall(r"(?:time|время)\s*[=<]\s*(\d+)\s*[mм]", out, re.IGNORECASE)]
    # Середнє з підсумку: 'Average = 12' / 'Среднее = 12' / 'Середнє = 12'
    m = re.search(r"(?:Average|Средн\w*|Середн\w*)\s*=\s*(\d+)", out, re.IGNORECASE)
    # Втрати: '(25% loss)' / '(25% потерь)' / '(25% втрат)' — відсоток у дужках універсальний
    ml = re.search(r"\((\d+)%", out)
    if ml:
        try:
            res["loss"] = int(ml.group(1))
        except Exception:
            pass
    if m:
        res["rtt_avg"] = int(m.group(1))
    if times:
        res["rtt_min"] = min(times)
        res["rtt_max"] = max(times)
        if res["rtt_avg"] is None:
            res["rtt_avg"] = sum(times) // len(times)
        if len(times) >= 2:
            res["jitter"] = max(abs(a - b) for a, b in zip(times, times[1:]))
        else:
            res["jitter"] = 0
    res["ok"] = res["rtt_avg"] is not None
    return res


# Профілі: (scrcpy_аргументи, jitter_ms приймача, опис)
QUALITY_PROFILES = {
    "high": ([], 30, "full quality (fast router)"),
    "medium": (["-m", "1280", "-b", "6M", "--max-fps", "30"], 60,
               "medium quality (slow/unstable link)"),
    "low": (["-m", "1024", "-b", "4M", "--max-fps", "24"], 100,
            "saver quality (very slow link, 2.4 GHz hotspot)"),
}


def pick_quality_profile(rtt_avg, loss):
    """Вибрати профіль за заміром: повертає ім'я профілю."""
    try:
        loss = int(loss)
    except Exception:
        loss = 0
    if rtt_avg is None:
        return "high" if loss == 0 else "medium"
    if rtt_avg > 80 or loss >= 25:
        return "low"
    if rtt_avg > 30 or loss > 0:
        return "medium"
    return "high"


HOTSPOT_TIPS = [
    "Enable the 5 GHz band for sharing: Settings -> Network & Internet -> Mobile hotspot -> Network band -> 5 GHz (2.4 GHz is congested and adds latency).",
    "Disable Wi-Fi power saving: Device Manager -> Network adapters -> your Wi-Fi -> Power Management -> UNCHECK 'Allow turning off this device'.",
    "Keep the phone closer to the PC and away from microwaves/Bluetooth speakers (they jam 2.4 GHz).",
    "Most stable is the same Wi-Fi router for the PC and the phone (not sharing from the PC).",
]


def decide_quality(args, adb_host, use_fake_adb):
    """Визначити профіль якості. Повертає (scrcpy_extra_args, jitter_ms).

    --quality high/medium/low = примусово; auto = заміряти якщо хотспот.
    Свій --scrcpy-args завжди поважаємо і нічого не додаємо.
    """
    if use_fake_adb:
        return [], 30
    if getattr(args, "quality", "auto") in ("medium", "low"):
        prof = args.quality
        extra, jitter, label = QUALITY_PROFILES[prof]
        log(f"Quality: {prof} (forced --quality) - {label}.")
        if args.scrcpy_args.strip() != "--select-usb":
            log("Custom --scrcpy-args take priority - leaving bitrate alone, applying only the jitter buffer.")
            return [], jitter
        return list(extra), jitter
    if getattr(args, "quality", "auto") == "high":
        return [], 30
    if args.scrcpy_args.strip() != "--select-usb":
        log("Custom --scrcpy-args given manually - leaving auto-quality alone.")
        return [], 30
    # auto
    try:
        is_hs, reason = detect_hotspot(adb_host)
    except Exception:
        is_hs, reason = False, ""
    need_measure = is_hs or getattr(args, "measure", False)
    if not need_measure:
        return [], 30
    if is_hs:
        log(f"Detected internet sharing from the PC (hotspot): {reason}.")
        log("Hotspot is usually slower than a router (2.4 GHz + NAT) - measuring the link...")
    else:
        log("Measuring the link to the phone (--measure)...")
    m = measure_link(adb_host)
    if not m["ok"]:
        log("Could not measure the link (ping with no reply) - keeping full quality.")
        return [], 30
    log(f"Link: RTT avg={m['rtt_avg']}ms (min {m['rtt_min']}, max {m['rtt_max']}), "
        f"jitter ~{m['jitter']}ms, loss {m['loss']}%.")
    prof = pick_quality_profile(m["rtt_avg"], m["loss"])
    extra, jitter, label = QUALITY_PROFILES[prof]
    if prof == "high":
        log("Link is good - full quality.")
        return [], 30
    log(f"Link is weak - enabling profile '{prof}': {label}.")
    log(f"scrcpy: {' '.join(extra)}; receiver jitter buffer: {jitter}ms.")
    if is_hs:
        log("Tips to remove hotspot latency:")
        for t in HOTSPOT_TIPS:
            log(f"  • {t}")
    return list(extra), jitter


def check_vhci():
    """Чи є драйвер vhci (для --vhci)."""
    if os.name != "nt":
        # Linux: VHCI — штатний модуль ядра vhci-hcd, Test Signing не потрібен.
        try:
            import glob as _glob
            if _glob.glob("/dev/vhci*") or _glob.glob("/sys/bus/platform/drivers/vhci_hcd/*"):
                log("VHCI (vhci-hcd) found - virtual USB is possible.")
                return True
            r = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=10,
                               encoding="utf-8", errors="replace")
            if "vhci_hcd" in (r.stdout or ""):
                log("VHCI (vhci_hcd module loaded) - virtual USB is possible.")
                return True
        except Exception:
            pass
        if shutil.which("usbip"):
            log("VHCI: vhci-hcd module not loaded, but the usbip tool exists "
                "(sudo modprobe vhci-hcd for virtual USB).")
        else:
            log("VHCI: no vhci-hcd/usbip - will work via scrcpy over TCP "
                f"(usbip comes with: {_manual_install_cmd('linux-tools-generic')}).")
        return False
    for dev in (r"\\.\vhci", r"\\.\USBIP_VHCI"):
        try:
            import ctypes
            GENERIC = 0x80000000 | 0x40000000
            h = ctypes.windll.kernel32.CreateFileW(
                dev, GENERIC, 0, None, 3, 0, None)
            if h and h != -1 and int(h) != -1:
                ctypes.windll.kernel32.CloseHandle(h)
                log(f"VHCI driver found ({dev}).")
                return True
        except Exception:
            pass
    log("VHCI driver NOT found - will be SIMULATION (no Device Manager).")
    log("For real USB: scripts/setup_windows.ps1  (installs usbip-win2)")
    return False

# ---------------- залежності ----------------

WINGET_PKGS = (
    ("cmake", "Kitware.CMake"),
    ("adb", "Google.PlatformTools"),
    ("scrcpy", "Genymobile.scrcpy"),
)

# Linux (Debian/Ubuntu): tool -> apt-пакет. adb дає android-tools-adb,
# scrcpy є в universe починаючи з 22.04.
APT_PKGS = (
    ("cmake", "cmake"),
    ("adb", "android-tools-adb"),
    ("scrcpy", "scrcpy"),
)

# Linux (Arch): tool -> pacman-пакет.
PACMAN_PKGS = (
    ("cmake", "cmake"),
    ("adb", "android-tools"),
    ("scrcpy", "scrcpy"),
)


def _linux_pkg_mgr():
    """Менеджер пакетів Linux: 'apt', 'pacman' або None."""
    if shutil.which("apt-get"):
        return "apt"
    if shutil.which("pacman"):
        return "pacman"
    return None


def _manual_install_cmd(*pkgs):
    """Команда ручного встановлення пакетів під поточну ОС (для підказок)."""
    if os.name == "nt":
        return "winget install " + " ".join(pkgs)
    if _linux_pkg_mgr() == "pacman":
        conv = {"android-tools-adb": "android-tools",
                "build-essential": "base-devel",
                "python3-tk": "tk",
                "linux-tools-generic": "usbip",
                "cmake build-essential": "cmake base-devel"}
        pkgs = [conv.get(p, p) for p in pkgs]
        return "sudo pacman -S --needed " + " ".join(pkgs)
    return "sudo apt-get install -y " + " ".join(pkgs)


def _os_pkgs():
    """Таблиця (tool, pkg-id) під поточну ОС і менеджер пакетів."""
    if os.name == "nt":
        return WINGET_PKGS
    return PACMAN_PKGS if _linux_pkg_mgr() == "pacman" else APT_PKGS


def install_linux_deps():
    """Самому доустановити відсутнє (apt на Debian/Ubuntu, pacman на Arch)."""
    mgr = _linux_pkg_mgr()
    table = APT_PKGS if mgr == "apt" else (PACMAN_PKGS if mgr == "pacman" else ())
    if not table:
        log("No apt-get/pacman - install dependencies manually (see README).")
        return False
    missing_pkgs = [pkg for tool, pkg in table if not shutil.which(tool)]
    if not missing_pkgs:
        return False
    if os.geteuid() != 0 and not shutil.which("sudo"):
        log("Need root or sudo - install manually: "
            f"{_manual_install_cmd(*missing_pkgs)}")
        return False
    prefix = [] if os.geteuid() == 0 else ["sudo"]
    if mgr == "apt":
        log(f"Installing MYSELF: apt-get install -y {' '.join(missing_pkgs)} ...")
        rc, _ = run(prefix + ["apt-get", "update"], timeout=300)
        if rc != 0:
            log("apt-get update failed - trying to install as-is below.")
        rc, _ = run(prefix + ["apt-get", "install", "-y"] + missing_pkgs, timeout=600)
    else:
        log(f"Installing MYSELF: pacman -S --needed {' '.join(missing_pkgs)} ...")
        rc, _ = run(prefix + ["pacman", "-S", "--needed", "--noconfirm"] + missing_pkgs,
                     timeout=600)
    return rc == 0


def install_deps():
    """Самому доустановити відсутнє: winget на Windows, apt на Linux."""
    if os.name != "nt":
        return install_linux_deps()
    if not shutil.which("winget"):
        log("winget not found - install dependencies manually (see README).")
        return False
    installed_any = False
    for tool, pkg in WINGET_PKGS:
        if shutil.which(tool):
            continue
        log(f"Installing {tool} MYSELF: winget install {pkg} ... (takes 1-3 min)")
        rc, _ = run(["winget", "install", "--accept-source-agreements", "--accept-package-agreements",
             "-e", "--id", pkg], timeout=600)
        if rc == 0:
            installed_any = True
    # оновити PATH поточної сесії з машинного/користувацького оточення
    log("Refreshing the session PATH...")
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
    return installed_any


def ask_yes_no(question, default_yes=True):
    """Питання Y/n з підтвердженням. Повертає True тільки при явній згоді.

    Порожній ввід (просто Enter) = default_yes. Будь-яке незрозуміле = Ні
    (безпечно: нічого не встановлюємо без явної згоди).
    Працює тільки в інтерактивному терміналі, інакше повертає False.
    """
    if not _stdin_interactive():
        return False
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    try:
        # Навмисно НЕ використовуємо _prompt(): він перетворює EOF на "",
        # що невідрізнити від Enter. А при закритому stdin (CI/пайп) ставити
        # нічого не можна — тільки явна згода живої людини.
        ans = input(question + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    except Exception:
        return False
    if not ans:
        return bool(default_yes)
    if ans in ("y", "yes", "yep", "yeah", "т", "так", "та", "да", "д", "ага", "1", "+"):
        return True
    return False


def offer_install_missing_deps(missing, auto_yes=False):
    """Запропонувати довстановити відсутнє (winget/apt), спитавши підтвердження.

    missing: список (tool, pkg-id під поточну ОС). auto_yes=True — встановити
    без питань (режим --install-deps / --yes). Повертає True якщо встановлення
    виконано (або спробовано), False якщо відмова/неможливо/неінтерактивно.
    """
    if not missing:
        return False
    names = ", ".join(t for t, _ in missing)
    log(f"Missing: {names}.")
    if os.name != "nt":
        mgr = _linux_pkg_mgr()
        if mgr is None:
            log("Auto-install: apt-get (Debian/Ubuntu) and pacman (Arch) are supported.")
            for tool, pkg in missing:
                log(f"  Manual: install the package with {tool} ({pkg})")
            return False
        manual_word = "apt-get" if mgr == "apt" else "pacman"
        if auto_yes:
            log("Auto-install enabled by flag - installing with no questions...")
            install_linux_deps()
            return True
        if not _stdin_interactive():
            log("Non-interactive mode - skipping auto-install.")
            for tool, pkg in missing:
                log(f"  Manual: {_manual_install_cmd(pkg)}  (or run with --install-deps)")
            return False
        log(f"I can install MYSELF via {manual_word} ({names}, sudo will be needed).")
        if ask_yes_no("Install the missing components automatically?", default_yes=True):
            log("OK, installing...")
            install_linux_deps()
            return True
        log("OK, skipping auto-install. Continuing with what is present.")
        for tool, pkg in missing:
            log(f"  When you have time: {_manual_install_cmd(pkg)}  (or ./START.sh install)")
        return False
    if not shutil.which("winget"):
        log("Auto-install is possible only on Windows with winget.")
        for tool, pkg in missing:
            log(f"  Manual: winget install {pkg}")
        return False
    if auto_yes:
        log("Auto-install enabled by flag - installing with no questions...")
        install_deps()
        return True
    if not _stdin_interactive():
        log("Non-interactive mode - skipping auto-install.")
        for tool, pkg in missing:
            log(f"  Manual: winget install {pkg}  (or run with --install-deps)")
        return False
    log(f"I can install MYSELF via winget ({names}, 1-3 min, may ask for UAC).")
    if ask_yes_no("Install the missing components automatically?", default_yes=True):
        log("OK, installing...")
        install_deps()
        return True
    log("OK, skipping auto-install. Continuing with what is present.")
    for tool, pkg in missing:
        log(f"  When you have time: winget install {pkg}  (or START.bat install)")
    return False

def check_compiler():
    """Чи є чим компілювати C++ (MSVC на Windows, gcc/clang на Linux)."""
    if os.name != "nt":
        for cc in ("gcc", "clang", "cc"):
            if shutil.which(cc):
                log(f"  compiler ({cc}): OK")
                return True
        log("  compiler: MISSING - a C++ compiler is needed for the build.")
        log(f"    {_manual_install_cmd('cmake', 'build-essential')}")
        return False
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
    log("  compiler (MSVC): MISSING - Visual Studio Build Tools with C++ is needed for the C++ build.")
    log("  Install YOURSELF with one command (3-8 GB, 10-30 min, one-time):")
    log("    winget install Microsoft.VisualStudio.2022.BuildTools --override "
        "\"--quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended\"")
    return False


def check_gui():
    """Діагностика вікон: tkinter (стандартне) + Node/Electron (CyberDeck)."""
    log("  --- windows (GUI) ---")
    try:
        import tkinter  # noqa: F401
        if os.name != "nt" and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            log("  tkinter: installed, but NO DISPLAY/WAYLAND_DISPLAY - no window.")
            log(f"  reason: {_display_missing_reason()}")
        else:
            log("  tkinter: OK (standard window will work)")
    except ImportError:
        if os.name == "nt":
            log("  tkinter: MISSING - no window, console only. "
                "Reinstall Python from https://www.python.org/downloads/ "
                "(keep the 'tcl/tk and IDLE' option).")
        else:
            log("  tkinter: MISSING - no window, console only. "
                f"Install: {_manual_install_cmd('python3-tk')}")
    if os.name != "nt":
        _disp = os.environ.get("DISPLAY", "")
        _way = os.environ.get("WAYLAND_DISPLAY", "")
        _sess = os.environ.get("XDG_SESSION_TYPE", "")
        log(f"  display: DISPLAY={_disp or '-'} WAYLAND_DISPLAY={_way or '-'} XDG_SESSION_TYPE={_sess or '-'}")
        try:
            _is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        except Exception:
            _is_root = False
        if _is_root:
            log("  sandbox: running as ROOT - Electron needs --no-sandbox "
                "(this code already adds it; the old version crashed silently).")
    ui_dir = ROOT / "ui"
    log(f"  ui/package.json: {'OK' if (ui_dir / 'package.json').exists() else 'MISSING (not run from repo root?)'}")
    _exe = _electron_bin_path(ui_dir)
    if _exe.exists():
        log("  electron: OK (CyberDeck window will work)")
    else:
        log("  electron: MISSING - for the CyberDeck window, one-time: cd ui && npm install "
            "(needs Node.js LTS from https://nodejs.org). "
            "Without it the standard window (tkinter) will work.")
        if (ui_dir / "node_modules" / "electron").exists():
            log("  hint: node_modules/electron folder exists but has no binary - "
                "this is a blocked postinstall (npm 11+: 'install scripts blocked'). "
                "Fix: cd ui && npm install-scripts approve electron && npm install")
    _node = shutil.which("node")
    _npm = shutil.which("npm.cmd") or shutil.which("npm")
    log(f"  node: {_node if _node else 'MISSING (https://nodejs.org)'}")
    log(f"  npm: {_npm if _npm else 'MISSING (https://nodejs.org)'}")
    if _node and os.name != "nt":
        try:
            _r = subprocess.run([_node, "--version"], capture_output=True, text=True, timeout=10,
                                encoding="utf-8", errors="replace")
            _ver = (_r.stdout or "").strip()
            log(f"  node version: {_ver or '?'}")
            if _ver.startswith("v"):
                try:
                    _major = int(_ver[1:].split(".")[0])
                    if _major < 18:
                        log("  node: TOO OLD (<18). Electron 28 needs Node 18+. "
                            "apt gives an old node - install LTS from https://nodejs.org "
                            "or via NodeSource (see scripts/setup_linux.sh). "
                            "Otherwise npm install will fail.")
                except Exception:
                    pass
        except Exception:
            pass


def offer_install_compiler(auto_yes=False):
    """Запропонувати встановити компілятор C++ з підтвердженням.

    Windows: MSVC Build Tools (важкий пакет!). Linux: build-essential через apt.
    Повертає True якщо встановлення спробовано, False якщо відмова/неможливо.
    """
    if os.name != "nt":
        if check_compiler():
            return False
        if not shutil.which("apt-get"):
            log("Install the compiler manually (see README).")
            return False
        do_it = bool(auto_yes)
        if not do_it:
            if not _stdin_interactive():
                log("Non-interactive mode - not installing the compiler (run with --install-deps to install with no questions).")
                return False
            do_it = ask_yes_no("Install build-essential now (sudo apt-get, ~200 MB)?", default_yes=True)
        if not do_it:
            log("OK, without a compiler the C++ build is impossible - continuing without it (fallback via scrcpy over TCP).")
            return False
        prefix = [] if os.geteuid() == 0 else ["sudo"]
        if not shutil.which("sudo") and os.geteuid() != 0:
            log("No sudo and not root - install manually: sudo apt-get install -y build-essential")
            return False
        run(prefix + ["apt-get", "install", "-y", "build-essential"], timeout=600)
        if check_compiler():
            log("Compiler appeared - continuing the build.")
            return True
        log("Compiler still not found.")
        return True
    if not shutil.which("winget"):
        log("Compiler auto-install is possible only on Windows with winget.")
        return False
    if check_compiler():
        return False
    log("WARNING: Build Tools is 3-8 GB and 10-30 min, installed ONCE. There will be a UAC prompt.")
    do_it = bool(auto_yes)
    if not do_it:
        if not _stdin_interactive():
            log("Non-interactive mode - not installing the compiler (run with --install-deps to install with no questions).")
            return False
        do_it = ask_yes_no("Install Visual Studio Build Tools with C++ now?", default_yes=False)
    if not do_it:
        log("OK, without a compiler the C++ build is impossible - continuing without it (fallback via scrcpy over TCP if the phone is already on Wi-Fi).")
        return False
    log("Installing Build Tools MYSELF (slow, do not close the window)...")
    run(["winget", "install", "--accept-source-agreements", "--accept-package-agreements",
         "-e", "--id", "Microsoft.VisualStudio.2022.BuildTools", "--override",
         "--quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"],
        timeout=3600)
    if check_compiler():
        log("Compiler appeared - continuing the build.")
        return True
    log("Compiler still not found (a console restart may be needed).")
    return True

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
            log(f"({self.tag} dry-run: not starting)")
            return True
        try:
            self.proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except FileNotFoundError:
            log(f"[{self.tag}] NOT FOUND: {self.cmd[0]}")
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
        description="Virtual USB Cable - autopilot (types all commands itself).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python tools/auto_run.py                 # auto: phone if present, else demo\n"
               "  python tools/auto_run.py --mode demo     # no phone\n"
               "  python tools/auto_run.py --mode real --phone-ip 192.168.1.100\n"
               "  python tools/auto_run.py --check         # dependency check only\n"
               "  python tools/auto_run.py --dry-run       # show commands only\n")
    ap.add_argument("--mode", choices=["auto", "demo", "real"], default="auto")
    ap.add_argument("--phone-ip", default="", help="Phone IP (if auto-detect failed)")
    ap.add_argument("--adb-port", type=int, default=DEFAULT_ADB_PORT)
    ap.add_argument("--sender-port", type=int, default=DEFAULT_SENDER_PORT)
    ap.add_argument("--with-vhci", action="store_true", help="Try the real vhci.sys (needs usbip-win2)")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--no-scrcpy", action="store_true", help="Do not start scrcpy automatically")
    ap.add_argument("--scrcpy-args", default="--select-usb")
    ap.add_argument("--quality", choices=["auto", "high", "medium", "low"], default="auto",
                    help="Video quality: auto (measures the link on hotspot itself), high/medium/low (forced)")
    ap.add_argument("--measure", action="store_true",
                    help="Force link measurement (ping) and quality match, even without hotspot")
    ap.add_argument("--check", action="store_true", help="Check dependencies only and exit")
    ap.add_argument("--dry-run", action="store_true", help="Show commands only, start nothing")
    ap.add_argument("--install-deps", action="store_true",
                    help="Auto-install the missing (cmake/adb/scrcpy) via winget with NO questions")
    ap.add_argument("--no-install-deps", action="store_true",
                    help="Never offer auto-install (check + hints only)")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Answer YES to all install questions (for scripts)")
    ap.add_argument("--scan", action="store_true",
                    help="Force-scan the whole LAN and offer device choice (with name), "
                         "even if the phone is already connected")
    ap.add_argument("--no-scan", action="store_true",
                    help="Do NOT scan the network (USB/last-phone/--phone-ip only)")
    ap.add_argument("--scan-timeout", type=float, default=0.45,
                    help="Timeout of one IP during LAN scan, sec (default 0.45)")
    ap.add_argument("--pick", default="",
                    help="Auto-select device with no prompt: list number or IP (for scripts)")
    ap.add_argument("--pair-code", default="",
                    help="6-digit pairing code from the screen (Wireless debugging) - no prompt")
    ap.add_argument("--mdns-timeout", type=float, default=2.5,
                    help="How many seconds to listen to phone mDNS announcements (default 2.5)")
    ap.add_argument("--mdns-check", action="store_true",
                    help="Air diagnostics: listen to mDNS for 8s and show WHY auto-discovery "
                         "does not see the phone (silence / no adb announcements / all heard)")
    ap.add_argument("--no-wireless", action="store_true",
                    help="Do NOT try cable-free connection (USB/tcpip/LAN scan only)")
    ap.add_argument("--scan-ports", default="",
                    help="Extra ADB ports for LAN scan, comma-separated (e.g. 37123,41234). "
                         "By default the scan already covers 5555 + the last "
                         "device port + dynamic mDNS ports; this is for manual extras.")
    ap.add_argument("--no-auto-scrcpy", action="store_true",
                    help="In electron mode do NOT run scrcpy -s after adb connect (adb only)")
    ap.add_argument("--ip-only", action="store_true",
                    help="Same as the default: look for live IPs only "
                         "(no TCP port scan of the network). Kept for compatibility.")
    ap.add_argument("--port-scan", action="store_true",
                    help="Enable mass TCP port scan of the network (5555 + dynamic). "
                         "Disabled by default: only live IPs are looked for, "
                         "and ports are picked precisely on the selected one.")
    ap.add_argument("--gui", action="store_true",
                    help="Force showing the device picker window (by default the window pops up by itself)")
    ap.add_argument("--no-gui", action="store_true",
                    help="No window - console prompts only")
    ap.add_argument("--electron-mode", action="store_true",
                    help="JSON event mode for Electron GUI")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    global SKIP_PORT_SCAN
    SKIP_PORT_SCAN = not bool(args.port_scan)
    if SKIP_PORT_SCAN:
        log("Default mode: looking for live IPs only (no mass port scan).")
    else:
        log("--port-scan mode: scanning TCP ports on the network.")

    if args.mdns_check:
        print("=== mDNS air diagnostics ===", flush=True)
        mdns_listen_check()
        return 0

    if args.electron_mode:
        _cli_scan_ports = [p for p in parse_scan_ports(args.scan_ports, args.adb_port)
                           if p != args.adb_port] if args.scan_ports else []
        electron_gui_mode(adb_port=args.adb_port, mdns_timeout=args.mdns_timeout,
                          scan_timeout=args.scan_timeout, scan_ports=_cli_scan_ports,
                          auto_scrcpy=(not args.no_auto_scrcpy))
        return 0

    print("=== Virtual USB Cable - AUTOPILOT (types all commands itself) ===", flush=True)

    # [0] залежності (+ довстановлення з підтвердженням)
    # --install-deps / --yes = ставити мовчки; --no-install-deps = тільки підказки;
    # за замовчуванням в інтерактивному терміналі ПИТАЄМО, в неінтерактиві — тільки підказки.
    auto_yes = bool(getattr(args, "install_deps", False) or getattr(args, "yes", False))
    no_install = bool(getattr(args, "no_install_deps", False))
    if auto_yes and not args.dry_run and not no_install:
        install_deps()
    log("Step 0: checking dependencies...")
    has_adb = shutil.which("adb") is not None
    has_scrcpy = shutil.which("scrcpy") is not None
    has_cmake = shutil.which("cmake") is not None
    has_py = True
    if os.name == "nt":
        log(f"  adb: {'OK' if has_adb else 'MISSING (winget install Google.PlatformTools or --install-deps)'}")
        log(f"  scrcpy: {'OK' if has_scrcpy else 'MISSING (winget install Genymobile.scrcpy or --install-deps)'}")
        log(f"  cmake: {'OK' if has_cmake else 'MISSING (winget install Kitware.CMake or --install-deps)'}")
    else:
        log(f"  adb: {'OK' if has_adb else 'MISSING (' + _manual_install_cmd('android-tools-adb') + ' or --install-deps)'}")
        log(f"  scrcpy: {'OK' if has_scrcpy else 'MISSING (' + _manual_install_cmd('scrcpy') + ' or --install-deps)'}")
        log(f"  cmake: {'OK' if has_cmake else 'MISSING (' + _manual_install_cmd('cmake', 'build-essential') + ' or --install-deps)'}")
    log(f"  python: OK ({sys.version.split()[0]})")
    if (not has_adb or not has_scrcpy or not has_cmake) and not args.dry_run and not no_install:
        missing = [(t, p) for t, p in _os_pkgs() if not shutil.which(t)]
        if missing:
            offer_install_missing_deps(missing, auto_yes=auto_yes)
            # перечитати після можливого встановлення
            has_adb = shutil.which("adb") is not None
            has_scrcpy = shutil.which("scrcpy") is not None
            has_cmake = shutil.which("cmake") is not None
            log(f"  re-check: adb={'OK' if has_adb else 'MISSING'} "
                f"scrcpy={'OK' if has_scrcpy else 'MISSING'} cmake={'OK' if has_cmake else 'MISSING'}")
    if args.check:
        check_compiler()
        check_gui()
        return 0 if (has_adb and has_scrcpy) else 1
    if not has_adb and args.mode == "real":
        log("ERROR: --mode real needs adb.")
        return 1

    # [1-2] телефон (крім demo)
    adb_host = "127.0.0.1"
    adb_port_eff = args.adb_port
    use_fake_adb = False
    if args.mode == "demo":
        log("Step 1-2: --mode demo - no phone needed, will simulate (fake adbd).")
        use_fake_adb = True
    else:
        if not has_adb:
            log("No adb - switching to demo mode.")
            use_fake_adb = True
        else:
            log("Step 1: adb start-server (MYSELF)...")
            if not args.dry_run:
                run(["adb", "start-server"], timeout=30)
            else:
                log_cmd(["adb", "start-server"])
            log("Step 2: looking for the phone (MYSELF)...")
            if not args.dry_run:
                devs = print_adb_devices()
            else:
                devs = []
                log_cmd(["adb", "devices", "-l"])
            if (args.mode == "real" and not devs and not args.phone_ip
                    and args.no_scan and args.no_wireless):
                log("ERROR: --mode real, but 'adb devices' is empty and --phone-ip is not given.")
                log("Options: USB cable, --phone-ip, or WITHOUT cable via Wireless debugging (without --no-wireless).")
                return 2
            # За замовчуванням: USB -> БЕЗ кабелю (mDNS + pair) -> скан LAN.
            # --no-wireless вимикає безкабельний етап, --no-scan — скан LAN,
            # --scan примушує показати всі пристрої навіть при наявному підключенні.
            _cli_scan_ports = [p for p in parse_scan_ports(args.scan_ports, args.adb_port)
                               if p != args.adb_port] if args.scan_ports else []
            adb_host, adb_port_eff, need_demo = setup_phone_wifi(
                args.adb_port, args.phone_ip, args.dry_run,
                devs if not args.dry_run else None,
                allow_scan=(not args.no_scan), force_scan=args.scan,
                scan_timeout=args.scan_timeout, pick=args.pick or None,
                allow_wireless=(not args.no_wireless),
                mdns_timeout=args.mdns_timeout, pair_code=args.pair_code or None,
                use_gui=args.gui or (not args.no_gui and not args.pick),
                scan_ports=_cli_scan_ports)
            if args.mode == "real" and need_demo and not args.dry_run:
                # в real пробуємо ще раз показати
                devs2 = print_adb_devices()
                if not any(st == "device" for _, st, _ in devs2):
                    log("ERROR: the phone never appeared. Check USB/Wi-Fi and the RSA confirmation.")
                    return 2
            use_fake_adb = need_demo and (args.mode != "real")

    # [3] збірка
    log("Step 3: build (if needed, MYSELF)...")
    sender_bin, receiver_bin = ensure_built(args.skip_build, args.dry_run,
                                            auto_yes=auto_yes, no_install=no_install)
    if args.dry_run:
        log("(dry-run) only showing the launch commands below:")
        log_cmd(f"agent_sender --adb {adb_host}:{adb_port_eff} --listen 0.0.0.0:{args.sender_port}"
                + (" --simulate-adb" if use_fake_adb else ""))
        log_cmd(f"agent_receiver --server 127.0.0.1:{args.sender_port} --auto --simulate"
                + (" --vhci" if args.with_vhci else ""))
        if not args.no_scrcpy:
            _dry_quality, _ = decide_quality(args, adb_host, use_fake_adb)
            log_cmd(" ".join(build_scrcpy_cmd(args, _dry_quality, adb_host, adb_port_eff, use_fake_adb)))
        log("DRY-RUN done.")
        return 0
    # [3b] якість каналу (хотспот? замір? профіль scrcpy + jitter).
    # В демо decide_quality нічого не робить (нема телефону).
    log("Step 3b: checking link quality (hotspot/measurement)...")
    quality_extra, jitter_ms = decide_quality(args, adb_host, use_fake_adb)
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
            log("===== FALLBACK (temporary, while there is no toolchain) =====")
            log(f"Phone {tcp_serial} is already on Wi-Fi - starting PLAIN scrcpy over TCP:")
            log(f"  scrcpy -s {tcp_serial}")
            log("This is plain scrcpy TCP mode (NOT virtual USB).")
            log("For the full module (virtual USB) add the toolchain:")
            if os.name == "nt":
                log("  winget install Kitware.CMake  (or START.bat install)")
                log("  + MSVC Build Tools with C++ (the command was in --check)")
            else:
                log("  sudo apt-get install -y cmake build-essential  (or ./START.sh install)")
            log("Then restart START.bat - it will pick up the phone itself (IP remembered).")
            log("======================================================")
            log("")
            fb = None
            try:
                fb = subprocess.Popen(["scrcpy", "-s", tcp_serial] + quality_extra)
                log(f"scrcpy started (pid={fb.pid}). Ctrl+C to stop.")
                while True:
                    time.sleep(1)
                    if fb.poll() is not None:
                        log("scrcpy exited.")
                        break
            except KeyboardInterrupt:
                log("Ctrl+C - stopping scrcpy...")
            finally:
                try:
                    if fb is not None and fb.poll() is None:
                        fb.terminate()
                except Exception:
                    pass
            return 0
        log("ERROR: no binaries and the build failed.")
        if os.name == "nt":
            log("Options: START.bat install (cmake itself) + MSVC Build Tools, or --skip-build if the binaries are elsewhere.")
        else:
            log("Options: ./START.sh install (cmake itself) + build-essential, or --skip-build if the binaries are elsewhere.")
        return 1

    # VHCI?
    vhci_ok = check_vhci()
    use_vhci = bool(args.with_vhci and vhci_ok)
    if args.with_vhci and not vhci_ok:
        log("--with-vhci requested, but no driver - continuing in SIMULATION.")

    # [4] sender
    log("Step 4: starting agent_sender MYSELF...")
    sender_cmd = [sender_bin, "--adb", f"{adb_host}:{adb_port_eff}",
                  "--listen", f"0.0.0.0:{args.sender_port}"]
    if use_fake_adb:
        sender_cmd.append("--simulate-adb")
    # sender --auto тільки для реального телефону (з fake він не потрібен).
    # Передаємо вже знайдений IP:порт як --phone-ip/--adb-port, щоб sender одразу
    # підключився і НЕ перепитував/перескановував (особливо важливо для динамічного
    # порту Бездротового налагодження).
    if not use_fake_adb:
        sender_cmd.append("--auto")
        sender_cmd += ["--phone-ip", adb_host, "--adb-port", str(adb_port_eff)]
        if args.pair_code:
            sender_cmd += ["--pair-code", args.pair_code]
    sender = ProcMon("SENDER", sender_cmd)
    if not sender.start():
        return 1
    time.sleep(1.0)
    if not sender.alive():
        log("WARNING: sender exited immediately. Maybe the port is busy or adb is unavailable.")
        log("Trying to continue - the receiver will show the connection error.")

    # [5] receiver (він САМ зробить adb devices; scrcpy запускаємо тут щоб не дублювати)
    log("Step 5: starting agent_receiver MYSELF (--auto, but I will start scrcpy myself centrally)...")
    receiver_cmd = [receiver_bin, "--server", f"127.0.0.1:{args.sender_port}", "--auto", "--no-scrcpy"]
    # handshake-симуляція ТІЛЬКИ для демо; з реальним телефоном handshake прийде з мережі
    if use_fake_adb:
        receiver_cmd.append("--simulate")
    if use_vhci:
        receiver_cmd.append("--vhci")
    if jitter_ms != 30:
        # Старі бінарники невідомий прапорець мовчки ігнорують — безпечно
        receiver_cmd += ["--jitter-ms", str(jitter_ms)]
    receiver = ProcMon("RECEIVER", receiver_cmd)
    if not receiver.start():
        sender.stop()
        return 1

    # [6] очікування + перевірка
    log("Step 6: waiting 3s and checking adb devices MYSELF...")
    for i in range(3):
        time.sleep(1)
        if not sender.alive():
            log("WARNING: sender stopped.")
        if not receiver.alive():
            log("WARNING: receiver stopped.")
    if has_adb and not args.dry_run:
        print_adb_devices()

    # [7] scrcpy — для Wi-Fi пристрою ОБОВ'ЯЗКОВО '-s IP:port', а не --select-usb
    # (--select-usb бачить тільки USB і давав чорний екран / "no device").
    scrcpy_cmd = build_scrcpy_cmd(args, quality_extra, adb_host, adb_port_eff, use_fake_adb)
    scrcpy_proc = None
    if not args.no_scrcpy:
        if not has_scrcpy:
            if os.name == "nt":
                log("Step 7: no scrcpy - skipping autostart. Install: winget install Genymobile.scrcpy")
            else:
                log("Step 7: no scrcpy - skipping autostart. "
                    f"Install: {_manual_install_cmd('scrcpy')}")
        else:
            log(f"Step 7: starting scrcpy MYSELF: {' '.join(scrcpy_cmd)} ...")
            try:
                scrcpy_proc = subprocess.Popen(scrcpy_cmd)
                log(f"scrcpy started (pid={scrcpy_proc.pid}).")
            except FileNotFoundError:
                log("Failed to start scrcpy.")
    else:
        log("Step 7: --no-scrcpy - not starting scrcpy.")

    log("")
    log("===== EVERYTHING STARTED AUTOMATICALLY. No input needed. Ctrl+C to stop. =====")
    log(f"Sender: agent_sender (port {args.sender_port}) | Receiver: agent_receiver --auto | scrcpy: {'on' if scrcpy_proc else 'off'}")

    # [8] моніторинг до Ctrl+C
    try:
        while True:
            time.sleep(1)
            if not sender.alive() and not receiver.alive():
                log("Both processes stopped - exiting.")
                break
    except KeyboardInterrupt:
        log("")
        log("Ctrl+C - stopping everything MYSELF...")
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
        log("Stopped. Goodbye!")
    return 0


def electron_gui_mode(adb_port=DEFAULT_ADB_PORT, mdns_timeout=2.5, scan_timeout=0.45,
                     scan_ports=None, auto_scrcpy=True):
    """GUI режим для Electron: JSON events через stdout / команди через stdin."""
    import json
    import sys
    import threading

    _extra_scan_ports = [int(p) for p in (scan_ports or []) if str(p).isdigit()]

    def emit(event_type, data):
        try:
            payload = json.dumps({'event': event_type, 'data': data}, ensure_ascii=False)
            print(payload, flush=True)
            if event_type in ("device-found", "device-list", "connect-result", "pair-result"):
                # Не JSON-рядок: міст покаже як службовий лог, парсинг подій не ламає
                n = len(data) if isinstance(data, list) else 1
                print(f"[EV] >> to window: {event_type} (x{n})", flush=True)
        except Exception as ex:
            print(f"[ERROR] emit {event_type}: {ex}", file=sys.stderr, flush=True)

    seen_devices = {}
    dev_lock = threading.Lock()

    def report_device(device):
        key = (device.get('kind', 'lan'), device.get('ip'), device.get('port'))
        with dev_lock:
            if key in seen_devices:
                old = seen_devices[key]
                if old.get('name') == "polling..." and device.get('name') != "polling...":
                    seen_devices[key] = device
                else:
                    return
            else:
                if device.get('kind') == 'lan' and any(d.get('ip') == device.get('ip') and d.get('kind') == 'connect' for d in seen_devices.values()):
                    return
                seen_devices[key] = device

        emit('device-found', {
            'ip': device['ip'],
            'port': device['port'],
            'name': device.get('name', 'Android Device'),
            'kind': device.get('kind', 'lan'),
            'state': device.get('state', 'device')
        })

    def run_full_scan():
        emit('status-update', 'Looking for devices via mDNS and on the network...')
        # 0) Останній відомий пристрій
        last = load_last_phone()
        if last:
            lip, lport = _split_serial(last, adb_port)
            if lip and _tcp_port_open(lip, lport, 0.4):
                name = get_device_display_name(f"{lip}:{lport}")
                report_device({
                    'kind': 'connect',
                    'ip': lip,
                    'port': lport,
                    'name': name if name and 'unknown' not in name else f"Last known ({lip})",
                    'state': 'device'
                })

        # 1) mDNS (Бездротове налагодження)
        try:
            mdevs = mdns_discover_adb(timeout=mdns_timeout)
            for d in mdevs:
                host = (d.get("host") or d.get("instance") or "").replace(".local", "")
                ip = d["ips"][0] if d.get("ips") else ""
                if not ip:
                    continue
                item = {
                    "kind": d["kind"],
                    "ip": ip,
                    "port": d["port"],
                    "name": host or "Android Device",
                    "state": "device"
                }
                if d["kind"] == "connect":
                    try:
                        name = get_device_display_name(f"{ip}:{d['port']}")
                        if name and "unknown" not in name:
                            item["name"] = name
                    except Exception:
                        pass
                report_device(item)
        except Exception as e:
            emit('status-update', f'mDNS: {e}')

        # 2) --ip-only: БЕЗ скану портів — повне виявлення живих IP (ping+arp),
        # порти підберуться точково на вибраному рядку. Інакше — скан портів.
        if SKIP_PORT_SCAN:
            emit('status-update', 'Looking for live hosts on the network (no port scan)...')
            try:
                _mports = []
                for _d in (mdevs or []):
                    if _d.get("kind") == "connect" and _d.get("port"):
                        _p = int(_d["port"])
                        if _p not in _mports:
                            _mports.append(_p)
            except Exception:
                _mports = []
            try:
                _iports = get_scan_ports(adb_port, extra_ports=_extra_scan_ports,
                                         include_last=True, mdns_ports=_mports)
            except Exception:
                _iports = [adb_port]
            alive = []
            try:
                with dev_lock:
                    known_ips = {d.get('ip') for d in seen_devices.values() if d.get('ip')}
                for aip in discover_alive_ips():
                    if aip not in known_ips:
                        known_ips.add(aip)
                        alive.append(aip)
                        report_device({
                            "kind": "net",
                            "ip": aip,
                            "port": 0,
                            "name": "Host on network (port unknown)",
                            "state": "unknown"
                        })
            except Exception as e:
                emit('status-update', f'Host search: {e}')
            # У кого відповідає ADB — міняємо непідписаний рядок на підписаний
            if alive:
                emit('status-update', 'Checking who has ADB open...')
                try:
                    for _aip, _info in identify_hosts(alive, _iports).items():
                        with dev_lock:
                            seen_devices.pop(('net', _aip, 0), None)
                        try:
                            _port = _split_serial(_info.get("serial", ""), adb_port)[1]
                        except Exception:
                            _port = adb_port
                        report_device({
                            "kind": "lan",
                            "ip": _info["ip"],
                            "port": _port,
                            "name": _info.get("name", "Android Device"),
                            "state": _info.get("state", "device")
                        })
                except Exception:
                    pass
        else:
            # LAN-сканування: 5555 + порт останнього пристрою + --scan-ports +
            # динамічні порти з mDNS (Бездротове налагодження дає випадковий порт,
            # і скан тільки 5555 такі телефони не бачив).
            mdns_ports = []
            try:
                for _d in (mdevs or []):
                    if _d.get("kind") == "connect" and _d.get("port"):
                        _p = int(_d["port"])
                        if _p not in mdns_ports:
                            mdns_ports.append(_p)
            except Exception:
                pass
            lan_ports = get_scan_ports(adb_port, extra_ports=_extra_scan_ports,
                                       include_last=True, mdns_ports=mdns_ports)

            def lan_open(ip, port=None):
                report_device({
                    "kind": "lan",
                    "ip": ip,
                    "port": port if port else lan_ports[0],
                    "name": "polling...",
                    "state": "device"
                })

            def lan_named(info):
                report_device({
                    "kind": "lan",
                    "ip": info["ip"],
                    "port": info.get("port", lan_ports[0]),
                    "name": info.get("name", "Android Device"),
                    "state": info.get("state", "device")
                })

            try:
                scan_lan_with_names(
                    adb_port=adb_port,
                    timeout=scan_timeout,
                    on_port_open=lan_open,
                    on_found=lan_named,
                    adb_ports=lan_ports
                )
            except Exception as e:
                emit('status-update', f'LAN scan: {e}')

        # 3) Живі хости БЕЗ відомих ADB-портів: шукаємо самі IP (швидко, по arp,
        # без скану портів) і показуємо рядком — клік підбере порт перебором.
        try:
            with dev_lock:
                known_ips = {d.get('ip') for d in seen_devices.values() if d.get('ip')}
            for aip in discover_alive_ips(fast=True):
                if aip not in known_ips:
                    report_device({
                        "kind": "net",
                        "ip": aip,
                        "port": 0,
                        "name": "Host on network (port unknown)",
                        "state": "unknown"
                    })
        except Exception:
            pass

        emit('scan-progress', {'done': True, 'count': len(seen_devices)})
        # Контрольний знімок ВСЬОГО списку (див. коментар вище).
        try:
            with dev_lock:
                snap = [
                    {'ip': d.get('ip'), 'port': d.get('port'),
                     'name': d.get('name', 'Android Device'),
                     'kind': d.get('kind', 'lan'),
                     'state': d.get('state', 'device')}
                    for d in seen_devices.values()
                ]
            emit('device-list', snap)
        except Exception:
            pass
        emit('status-update', f'Search finished. Found {len(seen_devices)} devices.')

    # Початковий запуск сканування
    threading.Thread(target=run_full_scan, daemon=True).start()

    # Читання команд від Electron через stdin
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            cmd_type = cmd.get('type')
            if cmd_type == 'scan':
                with dev_lock:
                    seen_devices.clear()
                threading.Thread(target=run_full_scan, daemon=True).start()
            elif cmd_type == 'connect':
                ip = cmd['ip']
                port = int(cmd.get('port', adb_port))
                emit('status-update', f'Connecting to {ip}:{port}...')
                def do_connect(tip=ip, tport=port):
                    # Порт 0 = «невідомий» (рядок живого хоста): одразу підбираємо самі.
                    result = adb_connect_fast(tip, tport) if tport else False
                    eff_port = tport
                    if not result:
                        # Порт міг змінитись (динамічний порт Бездротового налагодження):
                        # підбираємо самі: прощуп + перебір 30000-49999.
                        emit('status-update', f'Port {tport} is silent, picking a port for {tip} myself...')
                        try:
                            found = _find_connect_port_after_pair(
                                tip, tport, extra_ports=_extra_scan_ports)
                        except Exception:
                            found = 0
                        if found:
                            emit('status-update', f'Trying {tip}:{found}...')
                            if adb_connect_fast(tip, found, timeout=5.0):
                                result = True
                                eff_port = found
                    name = get_device_display_name(f"{tip}:{eff_port}") if result else ""
                    scrcpy_ok = False
                    if result:
                        save_last_phone(f"{tip}:{eff_port}")
                        if auto_scrcpy:
                            emit('status-update', f'Connected to {tip}:{eff_port} - starting scrcpy...')
                            scrcpy_ok = launch_scrcpy_for_device(tip, eff_port)
                    emit('connect-result', {
                        'success': bool(result),
                        'ip': tip,
                        'port': eff_port,
                        'name': name,
                        'scrcpy': scrcpy_ok,
                        'error': '' if result else 'Failed to connect (check the IP/port on the "IP address & Port" screen)'
                    })
                    if result:
                        emit('status-update', f'Connected to {tip}:{eff_port}' + ('' if scrcpy_ok else ' (scrcpy did not start - run manually: scrcpy -s %s:%s)' % (tip, eff_port)))
                    else:
                        emit('status-update', f'Connection error to {tip}:{tport}')
                threading.Thread(target=do_connect, daemon=True).start()
            elif cmd_type == 'pair':
                ip = cmd['ip']
                port = int(cmd['port'])
                code = str(cmd['code']).strip()
                emit('status-update', f'Pairing with {ip}:{port}...')
                def do_pair(tip=ip, tport=port, tcode=code):
                    def _finish_connect(cp):
                        conn_ok = adb_connect_fast(tip, cp, timeout=6.0)
                        name = get_device_display_name(f"{tip}:{cp}") if conn_ok else ""
                        scrcpy_ok = False
                        if conn_ok:
                            save_last_phone(f"{tip}:{cp}")
                            if auto_scrcpy:
                                scrcpy_ok = launch_scrcpy_for_device(tip, cp)
                        emit('connect-result', {'success': bool(conn_ok), 'ip': tip, 'port': cp, 'name': name, 'scrcpy': scrcpy_ok})
                        emit('status-update', f'Connected WITHOUT cable: {tip}:{cp} - {name}' if conn_ok else 'Failed to connect to the connection port.')
                        return conn_ok
                    result = adb_pair_verify(tip, tport, tcode)
                    if result:
                        emit('pair-result', {'success': True, 'ip': tip, 'port': tport})
                        emit('status-update', 'Paired successfully! Looking for the connection port...')
                        try:
                            devs = mdns_discover_adb(timeout=2.0)
                            same = [x for x in devs if x["kind"] == "connect" and x["ips"] and x["ips"][0] == tip]
                            if same and _finish_connect(same[0]["port"]):
                                return
                        except Exception:
                            pass
                        # mDNS не дав порту — прощуп + перебір портів цього IP
                        emit('status-update', 'mDNS gave no port - looking for the port (probe, then brute force)...')
                        try:
                            found = _find_connect_port_after_pair(
                                tip, tport, extra_ports=_extra_scan_ports)
                        except Exception:
                            found = 0
                        if found and _finish_connect(found):
                            return
                        emit('status-update',
                             'Connection port not found. Enter it manually: '
                             'on the phone "IP address & Port" -> the "Manual" form -> Connect.')
                    else:
                        emit('pair-result', {'success': False, 'ip': tip, 'port': tport, 'error': 'Pairing failed'})
                        emit('status-update', 'Pairing failed. Check the code.')
                threading.Thread(target=do_pair, daemon=True).start()
        except Exception as e:
            emit('error', {'message': str(e)})


if __name__ == "__main__":
    sys.exit(main())
