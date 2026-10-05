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
        log("scrcpy не знайдено в PATH — вікно з картинкою не відкриваю "
            "(adb connect вже виконано).")
        return False
    serial = f"{ip}:{port}"
    cmd = ["scrcpy", "-s", serial] + list(extra_args or [])
    try:
        log_cmd(cmd)
        subprocess.Popen(cmd)
        log(f"scrcpy запущено для {serial} — вікно з картинкою має відкритись.")
        return True
    except FileNotFoundError:
        log("Не вдалося запустити scrcpy (not found).")
        return False
    except Exception as e:
        log(f"Не вдалося запустити scrcpy: {e}")
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
                    log(f"Для {ip} знайдено відкритий порт {p} (замість {adb_port}).")
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
    log(f"Перебираю порти {ip}:{port_min}-{port_max} (до ~15с, шукаю динамічний порт)...")
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
                        log(f"  ... {ip}:{p} відкрито, перевіряю чи це ADB...")
                        # Перевіряємо одразу, не чекаючи кінця перебору
                        if adb_connect_fast(ip, p, timeout=4.0):
                            log(f"Це він: {ip}:{p} відповідає як ADB.")
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
        log(f"Відкриті порти {ip}: {found}, але adb connect не пішов.")
    else:
        log(f"У діапазоні {port_min}-{port_max} на {ip} нічого відкритого.")
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
        log(f"Шукаю живі хости ({len(targets)} адрес, ping)... кілька секунд.")
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
    log(f"Підбираю порт для {ip} (кандидати: {', '.join(str(p) for p in cands)})...")
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
        log(f"Пробую adb connect {ip}:{p}...")
        if _quick_conn(ip, p):
            name = get_device_display_name(f"{ip}:{p}")
            log(f"Підключено: {ip}:{p} — {name}.")
            save_last_phone(f"{ip}:{p}")
            return p, True
    # Треба парування: питаємо ТІЛЬКИ код, pair-порт визначаємо перебором
    code = (pair_code or "").strip()
    if not code:
        if not _stdin_interactive():
            log("Потрібне парування, але коду нема (неінтерактивний режим, задайте --pair-code).")
            return 0, False
        log("Схоже, телефон ще не спаровано. На екрані: 'Pair device with pairing code'.")
        code = _prompt("Введіть 6-значний код з екрану телефону: ")
    if not code:
        return 0, False
    for p in opens:
        log(f"Пробую парування на {ip}:{p}...")
        if adb_pair_verify(ip, p, code, timeout=15):
            log(f"Пара вдалась на {ip}:{p}, шукаю порт підключення...")
            for q in opens:
                if q == p:
                    continue
                if _quick_conn(ip, q):
                    name = get_device_display_name(f"{ip}:{q}")
                    log(f"Підключено по Wi-Fi БЕЗ кабелю: {ip}:{q} — {name}.")
                    save_last_phone(f"{ip}:{q}")
                    return q, True
            f = _find_connect_port_after_pair(ip, p, extra_ports=extra_ports)
            if f and _quick_conn(ip, f):
                name = get_device_display_name(f"{ip}:{f}")
                log(f"Підключено по Wi-Fi БЕЗ кабелю: {ip}:{f} — {name}.")
                save_last_phone(f"{ip}:{f}")
                return f, True
            diagnose_connect_fail(ip, p)
            return 0, False
    log("Жоден відкритий порт не прийняв код парування. Код одноразовий — оновіть екран і спробуйте ще.")
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
    log(f"Живих хостів у мережі: {len(ips)}. Де ваш телефон? "
        "(IP видно: Налаштування → Про телефон → Статус, або Wi-Fi → ваша мережа)")
    # Позначаємо у кого відкрито 5555 (ймовірно adb/tcpip)
    marks = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            res = list(ex.map(lambda a: (a, _tcp_port_open(a, 5555, 0.35)), ips))
        marks = dict(res)
    except Exception:
        pass
    for i, ip in enumerate(ips, 1):
        tag = " [схоже ADB:5555]" if marks.get(ip) else ""
        if names and names.get(ip):
            tag = f" — {names[ip]}"
        log(f"  {i}. {ip}{tag}")
    log("  0. Ввести IP вручну / пропустити")
    log("")
    try:
        ans = input(f"Введіть номер (1-{len(ips)}) або IP, 0/Enter щоб пропустити: ").strip()
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
        # cmake вже пропонували встановити на кроці 0 (з підтвердженням).
        # Тут повторно не ставимо мовчки — тільки підказка.
        log("ERROR: 'cmake' нема (ви відмовились від автовстановлення або воно не вдалося).")
        log("Варіант 1 (автоматом): запустіть з прапорцем --install-deps")
        log("Варіант 2 (вручну): winget install Kitware.CMake")
        log("Варіант 3 (все разом): powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1")
        return None, None
    if not check_compiler():
        if no_install:
            log("ERROR: cmake є, але компілювати C++ нічим (MSVC не знайдено) — збірка неможлива.")
            return None, None
        # Важкий пакет — тільки з явної згоди (за замовчуванням default=Ні).
        offer_install_compiler(auto_yes=auto_yes)
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
        # даємо adbd час на handshake (A_CNXN); опитуємо кожні 0.5с щоб не чекати зайвого
        for _ in range(8):
            time.sleep(0.5)
            if _tcp_device_ready(serial, adb_port):
                log(f"Підключено і підтверджено: {serial} (device).")
                return True
        log(f"Поки не device, пробую ще...")
    log(f"Не вдалося отримати device на {serial} за {attempts} спроби.")
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
            log(f"Підключено: {serial} (device).")
            return True
        time.sleep(0.5)
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
        log("LAN-сканування: не вдалося визначити локальну підмережу.")
        return []
    targets = []
    for net in nets:
        hosts = list(net.hosts())
        # Захист від /22 і більше (хоч get_local_subnets вже ріже до /24, перестраховуємось)
        if len(hosts) > 1024:
            log(f"LAN-сканування: {net} завелика ({len(hosts)} адрес) — сканую перші 1024.")
            hosts = hosts[:1024]
        targets.extend(str(h) for h in hosts)
    # Прибрати дублікати, зберегти порядок (пріоритетна підмережа — першою)
    targets = list(dict.fromkeys(targets))
    if not targets:
        return []
    log(f"Сканую мережу: {', '.join(str(n) for n in nets)} "
        f"({len(targets)} адрес, порти {', '.join(str(p) for p in ports)})... це займе кілька секунд.")
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
        log(f"Відкриті ADB-порти: {', '.join(f'{ip}:{p}' for ip, p in open_pairs)} (скан {dt:.1f}с).")
    else:
        log(f"Нічого з відкритими портами {ports} не знайдено (скан {dt:.1f}с).")
        log("Підказка: увімкніть на телефоні Бездротове налагодження або виконайте 'adb tcpip 5555' по USB.")
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
            log("LAN-сканування: не вдалося визначити локальну підмережу.")
            return []
        targets = []
        for net in nets:
            hosts = list(net.hosts())
            # Захист від /22 і більше (хоч get_local_subnets вже ріже до /24, перестраховуємось)
            if len(hosts) > 1024:
                log(f"LAN-сканування: {net} завелика ({len(hosts)} адрес) — сканую перші 1024.")
                hosts = hosts[:1024]
            targets.extend(str(h) for h in hosts)
        # Прибрати дублікати, зберегти порядок (пріоритетна підмережа — першою)
        targets = list(dict.fromkeys(targets))
        if not targets:
            return []
        log(f"Сканую мережу: {', '.join(str(n) for n in nets)} "
            f"({len(targets)} адрес, порт {adb_port})... це займе кілька секунд.")
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
            log(f"Відкритий порт {adb_port} знайдено на: {', '.join(open_ips)} (скан {dt:.1f}с).")
        else:
            log(f"Нічого з відкритим портом {adb_port} не знайдено (скан {dt:.1f}с).")
            log("Підказка: увімкніть на телефоні Бездротове налагодження або виконайте 'adb tcpip 5555' по USB.")
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
        name = "невідома модель"
    if android:
        name = f"{name} (Android {android})"
    if state == "unauthorized":
        name += " [потрібно підтвердити RSA на екрані]"
    elif state == "offline":
        name += " [offline]"
    elif not state:
        name += " [не відповідає на ADB — можливо не телефон]"
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
    log(f"Знайдено пристроїв у мережі: {len(dev_infos)}. До якого приєднатись?")
    for i, d in enumerate(dev_infos, 1):
        log(f"  {i}. {d['serial']} — {d['name']}")
    log("  0. Пропустити (демо без телефону)")
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
            log(f"Автовибір (--pick {pick}): {chosen}.")
            cleanup(chosen)
            return chosen
        log(f"--pick={pick} ні з чим не збігся, питаю вручну...")

    # Один пристрій + неінтерактивний термінал (скрипт/CI) — беремо його мовчки
    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    if not interactive:
        if len(dev_infos) == 1:
            log(f"Неінтерактивний режим — беру єдиний пристрій {dev_infos[0]['ip']} автоматично.")
            return dev_infos[0]["ip"]
        log("Неінтерактивний режим і пристроїв кілька — беру перший зі станом device (якщо є).")
        for d in dev_infos:
            if d["state"] == "device":
                cleanup(d["ip"])
                return d["ip"]
        return ""

    # Інтерактивний вибір
    try:
        ans = input("Введіть номер (1-%d), IP, або 0/Enter щоб пропустити: " % len(dev_infos)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        log("Вибір скасовано.")
        cleanup(None)
        return ""
    if not ans or ans == "0":
        log("Пропущено за вибором користувача.")
        cleanup(None)
        return ""
    if ans.isdigit():
        idx = int(ans)
        if 1 <= idx <= len(dev_infos):
            chosen = dev_infos[idx - 1]["ip"]
            log(f"Вибрано: {dev_infos[idx-1]['serial']} — {dev_infos[idx-1]['name']}.")
            cleanup(chosen)
            return chosen
        log("Невірний номер — пропускаю.")
        cleanup(None)
        return ""
    # Ввели IP або серійник вручну
    for d in dev_infos:
        if d["ip"] == ans or d["serial"] == ans:
            log(f"Вибрано: {d['serial']} — {d['name']}.")
            cleanup(d["ip"])
            return d["ip"]
    # Дозволяємо будь-який IP вручну (навіть якщо сканер його не бачив — порт міг відкритись пізніше)
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", ans):
        log(f"Введено IP вручну: {ans} (його не було в скані, спробую підключитись).")
        cleanup(None)
        return ans
    log("Незрозумілий ввід — пропускаю.")
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
        log(f"mDNS: нема UDP-сокета: {e}")
        return []
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", MDNS_PORT))
        except OSError:
            log("mDNS: порт 5353 зайнято іншою програмою — авто-пошук по Wi-Fi неможливий, запропоную ручне введення.")
            return []
        try:
            mreq = socket.inet_aton(MDNS_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            log(f"mDNS: не вдалося підписатись на multicast: {e}")
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
        log(f"mDNS: нема UDP-сокета: {e}")
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", MDNS_PORT))
        except OSError:
            log("Порт 5353 зайнято іншою програмою (часто це Bonjour/Chrome) — "
                "слухати ефір не можу. Закрийте браузер і спробуйте ще раз.")
            return
        try:
            mreq = socket.inet_aton(MDNS_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            log(f"Не вдалося підписатись на multicast: {e} (VPN? антивірус з фаєрволом?)")
            return
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        except OSError:
            pass
        sock.settimeout(0.5)
        log(f"Слухаю mDNS-ефір {timeout:.0f}с... (на телефоні має бути увімкнено Бездротове налагодження)")
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
        log(f"Почуто mDNS-пакетів: {pkt_count}.")
        if adb_seen:
            for svc in sorted(adb_seen):
                log(f"  [OK] {svc}: {len(adb_seen[svc])} анонс(ів)")
                for ins in sorted(adb_seen[svc])[:5]:
                    log(f"       - {ins}")
            log("Телефон анонсується — автопошук має бачити. Якщо не бачить, "
                "проблема в портах/adb, а не в мережі.")
        elif pkt_count:
            log("  Телефонних _adb-tls-* анонсів НЕМА, але інший mDNS-трафік є:")
            for svc in sorted(other_services)[:8]:
                log(f"       - {svc}")
            log("Висновок: multicast до ПК доходить. Перевірте на телефоні:")
            log("  Параметри → Для розробників → Бездротове налагодження УВІМКНЕНО,")
            log("  і що ПК і телефон в ОДНІЙ Wi-Fi мережі (не гостьова, без VPN).")
        else:
            log("  ПОВНА ТИША — multicast до ПК не доходить.")
            log("Висновок: роутер ріже multicast між Wi-Fi клієнтами (AP/Client isolation),")
            log("  або брандмауер Windows блочить UDP 5353, або активний VPN.")
            log("  Лікування: вимкнути ізоляцію в налаштуваннях роутера / дозволити "
                "python в брандмауері / вимкнути VPN. А поки — тільки ручне введення.")
        log("")
    finally:
        try:
            sock.close()
        except Exception:
            pass


def adb_pair_verify(ip, pair_port, code, timeout=30):
    """adb pair <ip>:<pair_port> з кодом з екрану телефону. True якщо 'Successfully paired'."""
    serial = f"{ip}:{pair_port}"
    log(f"adb pair {serial} (код з екрану телефону)...")
    try:
        r = subprocess.run(["adb", "pair", serial], input=(code.strip() + "\n"),
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.strip().splitlines():
            if line.strip():
                log(f"   | {line.strip()}")
        if "Successfully paired" in out:
            log("Парування вдалося.")
            return True
    except FileNotFoundError:
        log("adb не знайдено.")
        return False
    except subprocess.TimeoutExpired:
        log("adb pair завис (таймаут).")
        return False
    log("Парування НЕ вдалося. Код одноразовий — відкрийте екран парування заново і спробуйте ще.")
    return False


def get_device_display_name(serial):
    """Прочитати 'Виробник Модель (Android X)' з уже підключеного пристрою."""
    man = _adb_shell_on(serial, ["shell", "getprop", "ro.product.manufacturer"])
    mod = _adb_shell_on(serial, ["shell", "getprop", "ro.product.model"])
    andr = _adb_shell_on(serial, ["shell", "getprop", "ro.build.version.release"])
    name = f"{man} {mod}".strip() or "невідома модель"
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
    log("Знайдено по Wi-Fi БЕЗ кабелю:")
    for i, (act, d) in enumerate(items, 1):
        host = (d["host"] or d["instance"]).replace(".local", "")
        if act == "connect":
            log(f"  {i}. [підключити] {host} — {d['ips'][0]}:{d['port']}")
        else:
            log(f"  {i}. [спарувати] {host} — {d['ips'][0]}:{d['port']} (потрібен 6-значний код з екрану)")
    log(f"  {len(items) + 1}. Ввести IP/порти/код вручну")
    log("  0. Пропустити")
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
            log(f"Автовибір (--pick {pick}).")
            return m
        log(f"--pick={pick} ні з чим не збігся, питаю вручну...")
    if not _stdin_interactive():
        if len(items) == 1 and items[0][0] == "connect":
            log(f"Неінтерактивний режим — підключаюсь до {items[0][1]['ips'][0]}:{items[0][1]['port']}.")
            return items[0]
        if pair_code:
            for act, d in items:
                if act == "pair":
                    log("Неінтерактивний режим — паруюсь по --pair-code.")
                    return (act, d)
        log("Неінтерактивний режим — вибрати не можу, пропускаю.")
        return "", None
    ans = _prompt(f"Введіть номер (1-{len(items) + 1}), IP, або 0/Enter щоб пропустити: ")
    if not ans or ans == "0":
        return "", None
    if ans == str(len(items) + 1):
        return "manual", None
    m = match(ans)
    if m:
        return m
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", ans):
        return "manual_ip", {"ips": [ans]}
    log("Незрозумілий ввід — пропускаю.")
    return "", None


def _wireless_do_pair(ip, pair_port, pair_code):
    """Запитати код (або взяти --pair-code) і виконати adb pair. Повертає True/False."""
    code = (pair_code or "").strip()
    if not code:
        if not _stdin_interactive():
            log("Неінтерактивний режим і коду парування нема (--pair-code) — пропускаю.")
            return False
        log("На телефоні відкрийте 'Pair device with pairing code' — там 6-значний код.")
        code = _prompt("Введіть код з екрану телефону: ")
    if not code:
        return False
    return adb_pair_verify(ip, pair_port, code)


def _wireless_manual(pair_code, adb_port=DEFAULT_ADB_PORT, extra_ports=None,
                     mdns_timeout=2.0):
    """Ручне введення: питаємо ТІЛЬКИ IP (порти підбираємо самі, код — якщо треба).

    Повертає (ip, port, ok). Старі поля портів лишили як запасний шлях.
    """
    log("")
    log("Ручне введення: достатньо IP телефону (Налаштування → Про телефон → Статус).")
    log("Порти підберу сам перебором; код спитаю, тільки якщо знадобиться парування.")
    ip = _prompt("IP телефону (напр. 192.168.1.50, Enter щоб пропустити): ")
    if not ip:
        return "", 0, False
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
        log("Схоже не на IP — пропускаю.")
        return "", 0, False
    port, ok = guided_connect_by_ip(ip, pair_code=pair_code,
                                    extra_ports=extra_ports,
                                    adb_port=adb_port,
                                    mdns_timeout=mdns_timeout)
    if ok:
        return ip, port, True
    # Запасний шлях: порти з екрану (раптом автопідбір не взяв)
    log("Автопідбір не вдався — спробую з портами з екрану телефону.")
    pair_port = _prompt("Порт парування ('Pair device with pairing code', Enter якщо телефон вже спаровано): ")
    if pair_port:
        if not pair_port.isdigit():
            log("Порт має бути числом — пропускаю.")
            return "", 0, False
        if not _wireless_do_pair(ip, int(pair_port), pair_code):
            return "", 0, False
    cport = _prompt("Порт підключення ('IP address & Port', напр. 37123): ")
    if not cport or not cport.isdigit():
        log("Без порту підключення ніяк — пропускаю.")
        return "", 0, False
    if adb_connect_verify(ip, int(cport)):
        name = get_device_display_name(f"{ip}:{cport}")
        log(f"Підключено по Wi-Fi БЕЗ кабелю: {ip}:{cport} — {name}.")
        save_last_phone(f"{ip}:{cport}")
        return ip, int(cport), True
    diagnose_connect_fail(ip, int(cport))
    return "", 0, False


def _try_wireless_debug(mdns_timeout=2.5, pair_code=None, pick=None,
                        adb_port=DEFAULT_ADB_PORT, extra_ports=None):
    """Підключення БЕЗ кабелю через Бездротове налагодження. Повертає (ip, port, ok)."""
    log("")
    log("Шукаю телефон по Wi-Fi БЕЗ кабелю (mDNS, Бездротове налагодження)...")
    log("На телефоні має бути увімкнено: Параметри → Для розробників → Бездротове налагодження.")
    try:
        devs = mdns_discover_adb(timeout=mdns_timeout)
    except Exception as e:
        log(f"mDNS-пошук не вдався: {e}")
        devs = []
    conns = [d for d in devs if d["kind"] == "connect"]
    pairs = [d for d in devs if d["kind"] == "pair"]
    for d in conns + pairs:
        host = (d["host"] or d["instance"]).replace(".local", "")
        log(f"  - [{d['kind']}] {host} — {d['ips'][0]}:{d['port']}")
    if not conns and not pairs:
        log("По Wi-Fi (mDNS) нічого не знайдено (можливо multicast ріже роутер/брандмауер).")
        log("Але IP знайду сам сканом мережі, а порти підберу перебором — "
            "з екрану знадобиться тільки код.")
        if pick and re.match(r"^\d+\.\d+\.\d+\.\d+$", str(pick)):
            log(f"--pick схоже на IP, підбираю порти для {pick} сам...")
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
                log(f"Скан живих хостів не вдався: {e}")
                alive = []
            if alive:
                log("Знаходжу живі хости сам — виберіть зі списку свій телефон.")
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
            ans = _prompt("Ввести IP вручну з екрану телефону? (так/ні): ").lower()
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
            log(f"Підключено по Wi-Fi БЕЗ кабелю: {ip}:{port} — {name}.")
            save_last_phone(f"{ip}:{port}")
            return ip, port, True
        log("Пряме підключення не вдалося — можливо телефон ще не спаровано. Шукаю парування...")
        same = [d for d in pairs if d["ips"] and d["ips"][0] == ip]
        if same:
            return _wireless_pair_then_connect(same[0], pair_code, mdns_timeout)
        diagnose_connect_fail(ip, port)
        return "", 0, False
    if action == "pair":
        return _wireless_pair_then_connect(dev, pair_code, mdns_timeout)
    if action in ("manual", "manual_ip"):
        if action == "manual_ip":
            cport = _prompt(f"Порт підключення для {dev['ips'][0]} ('IP address & Port'): ")
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
    log("Шукаю порт підключення цього ж телефону...")
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
        log("mDNS connect-сервіс не знайшов — шукаю порт цього IP (прощуп, потім перебір)...")
        found = _find_connect_port_after_pair(ip, pair_port)
        if found:
            cport = found
        else:
            cport_s = _prompt("Не знайшов порт автоматично. Введіть порт підключення ('IP address & Port'): ")
            if not cport_s or not cport_s.isdigit():
                return "", 0, False
            cport = int(cport_s)
    if adb_connect_fast(ip, cport, timeout=6.0):
        name = get_device_display_name(f"{ip}:{cport}")
        log(f"Підключено по Wi-Fi БЕЗ кабелю: {ip}:{cport} — {name}.")
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
        return True
    except ImportError:
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

    ui_dir = ROOT / "ui"
    if not (ui_dir / "package.json").exists():
        log("Electron UI пропущено: нема ui/package.json "
            f"(шукав {ui_dir}). Запускайте з кореня репозиторію.")
        return None

    electron_exe = ui_dir / "node_modules" / "electron" / "dist" / "electron.exe"
    if not electron_exe.exists() and not (ui_dir / "node_modules").exists():
        log("Electron UI: нема ui/node_modules — CyberDeck без нього не стартує.")
        _npm_try = shutil.which("npm.cmd") or shutil.which("npm")
        if _npm_try and _stdin_interactive() and ask_yes_no(
                "Встановити залежності CyberDeck UI зараз (cd ui && npm install, 1-3 хв, одноразово)?",
                default_yes=True):
            log("Встановлюю залежності UI — не закривайте вікно...")
            _inst = [_npm_try, "install", "--no-audit", "--no-fund"]
            if os.name == "nt" and str(_npm_try).lower().endswith((".cmd", ".bat")):
                _inst = ["cmd", "/c"] + _inst
            try:
                log_cmd(["npm", "install", "--no-audit", "--no-fund"])
                _ir = subprocess.run(_inst, cwd=str(ui_dir), timeout=600)
                if _ir.returncode == 0 and electron_exe.exists():
                    log("Залежності UI встановлено.")
                else:
                    log(f"npm install завершився з кодом {_ir.returncode}. "
                        "Якщо Electron не стартує — спробуйте вручну: cd ui && npm install, "
                        "і покажіть текст помилки.")
            except Exception as e:
                log(f"npm install не вдався ({e}) — продовжую без CyberDeck.")
        else:
            log("Одноразово виконайте вручну: cd ui && npm install "
                "(потрібен Node.js LTS з https://nodejs.org). "
                "Поки що пробую запустити через npm...")
    if electron_exe.exists():
        cmd = [str(electron_exe), str(ui_dir), "--parent-python"]
    else:
        npm_bin = shutil.which("npm.cmd") or shutil.which("npm") or shutil.which("npx.cmd") or shutil.which("npx")
        if not npm_bin:
            log("Electron UI пропущено: не знайдено npm/npx в PATH "
                "(встановіть Node.js LTS з https://nodejs.org). "
                "Переходжу на стандартне вікно...")
            return None
        cmd = [npm_bin, "--prefix", str(ui_dir), "start", "--", "--parent-python"]
        if os.name == "nt" and str(npm_bin).lower().endswith((".cmd", ".bat")):
            # .cmd/.bat не запускаються напряму через CreateProcess —
            # виконуємо через cmd /c, інакше вікно мовчки не стартує.
            cmd = ["cmd", "/c"] + cmd

    log("Відкриваю CyberDeck Electron UI...")
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
        log(f"Сокет для вікна не створився ({e}) — події підуть через stdin.")
        py_link["sock_failed"] = True
    # Дублікат вікна від минулого запуску = класична причина «порожнього списку»:
    # старе вікно (бекенд мертвий) перекриває нове. Попереджаємо одразу.
    try:
        _r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq electron.exe", "/FO", "CSV"],
                            capture_output=True, text=True, timeout=10,
                            encoding="utf-8", errors="replace")
        _n = max(0, len([l for l in (_r.stdout or "").splitlines() if "electron.exe" in l.lower()]))
        if _n:
            log(f"УВАГА: вже запущено electron.exe ({_n}). Якщо бачите порожнє вікно — "
                "закрийте ВСІ вікна програми і запустіть заново (старе вікно не оновлюється).")
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
        log(f"UI-процес запущено (pid={proc.pid}).")
    except Exception as e:
        log(f"Не вдалося відкрити Electron UI: {e}")
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
            log(f">> у вікно: {event_type} (x{n})")
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
            log("Вікно підключилось по сокету — події підуть напряму.")
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
            log("Вікно по сокету не підключилось (25с) — події підуть через stdin.")
            py_link["sock_failed"] = True

    threading.Thread(target=_accept_loop, daemon=True).start()

    def report_device(device):
        key = (device.get('kind', 'lan'), device.get('ip'), device.get('port'))
        with dev_lock:
            if key in seen_devices:
                old = seen_devices[key]
                if old.get('name') == "опитування..." and device.get('name') != "опитування...":
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
        send_event('status-update', 'Шукаю пристрої по mDNS та в мережі...')
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
                    'name': name if name and 'невідома' not in name else f"Останній ({lip})",
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
                        if name and "невідома" not in name:
                            item["name"] = name
                    except Exception:
                        pass
                report_device(item)
        except Exception as e:
            send_event('status-update', f'mDNS: {e}')

        # 2) --ip-only: БЕЗ скану портів — повне виявлення живих IP (ping+arp),
        # порти підберуться точково на вибраному рядку. Інакше — скан портів.
        if SKIP_PORT_SCAN:
            send_event('status-update', 'Шукаю живі хости в мережі (без скану портів)...')
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
                            "name": "Хост у мережі (порт невідомий)",
                            "state": "unknown"
                        })
            except Exception as e:
                send_event('status-update', f'Пошук хостів: {e}')
            # У кого відповідає ADB — міняємо непідписаний рядок на підписаний
            if alive:
                send_event('status-update', 'Перевіряю у кого відкрито ADB...')
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
                    "name": "опитування...",
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
                send_event('status-update', f'LAN скан: {e}')

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
                        "name": "Хост у мережі (порт невідомий)",
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
        send_event('status-update', f'Пошук завершено. Знайдено {len(seen_devices)} пристроїв.')

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
                    send_event('status-update', f'Підключення до {ip}:{port}...')
                    def do_connect(tip=ip, tport=port):
                        # Порт 0 = «невідомий» (рядок живого хоста): одразу підбираємо самі.
                        ok = adb_connect_fast(tip, tport) if tport else False
                        eff_port = tport
                        if not ok:
                            # Порт міг бути динамічним: підбираємо самі (прощуп + перебір)
                            send_event('status-update', f'Порт {tport} мовчить, підбираю порт для {tip} сам...')
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
                            send_event('status-update', f'Підключено до {tip}:{eff_port}')
                            time.sleep(0.6)
                            send_event('close-window', {})
                            result.update(ip=tip, port=eff_port, ok=True, done=True)
                        else:
                            send_event('connect-result', {'success': False, 'ip': tip, 'port': tport, 'error': 'Не вдалося підключитися (порт з екрану «IP address & Port»?)'})
                            send_event('status-update', f'Помилка підключення до {tip}:{tport}')
                    threading.Thread(target=do_connect, daemon=True).start()
                elif cmd_type == 'pair':
                    ip = cmd['ip']
                    port = int(cmd['port'])
                    code = str(cmd['code']).strip()
                    send_event('status-update', f'Парування з {ip}:{port}...')
                    def do_pair(tip=ip, tport=port, tcode=code):
                        def _finish_connect(cp):
                            conn_ok = adb_connect_fast(tip, cp, timeout=6.0)
                            name = get_device_display_name(f"{tip}:{cp}") if conn_ok else ""
                            if conn_ok:
                                save_last_phone(f"{tip}:{cp}")
                                send_event('connect-result', {'success': True, 'ip': tip, 'port': cp, 'name': name})
                                send_event('status-update', f'Підключено: {tip}:{cp} — {name}')
                                time.sleep(0.6)
                                send_event('close-window', {})
                                result.update(ip=tip, port=cp, ok=True, done=True)
                            return conn_ok
                        ok = adb_pair_verify(tip, tport, tcode)
                        if ok:
                            send_event('pair-result', {'success': True, 'ip': tip, 'port': tport})
                            send_event('status-update', 'Успішно спаровано! Шукаю порт підключення...')
                            try:
                                devs = mdns_discover_adb(timeout=2.0)
                                same = [x for x in devs if x["kind"] == "connect" and x["ips"] and x["ips"][0] == tip]
                                if same and _finish_connect(same[0]["port"]):
                                    return
                            except Exception:
                                pass
                            # mDNS не дав порту — прощуп + перебір портів цього IP
                            send_event('status-update', 'mDNS порту не дав — шукаю порт (прощуп, потім перебір)...')
                            try:
                                found = _find_connect_port_after_pair(
                                    tip, tport, extra_ports=_extra_scan_ports)
                            except Exception:
                                found = 0
                            if found and _finish_connect(found):
                                return
                            send_event('status-update',
                                       'Порт підключення не знайшов. Введіть його вручну: '
                                       'на телефоні «IP address & Port» → форма «Вручну» → Підключити.')
                        else:
                            send_event('pair-result', {'success': False, 'ip': tip, 'port': tport, 'error': 'Парування не вдалося'})
                            send_event('status-update', 'Парування не вдалося. Перевірте код.')
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
        log(f"Electron не зміг стартувати (код {rc}) — переходжу на стандартне вікно...")
        if err_tail:
            log(f"Причина з stderr: {err_tail[:1500]}")
        else:
            log("Підказка: найчастіше це нема ui/node_modules — виконайте: cd ui && npm install. "
                "Або подивіться повний лог вище ([UI]/[UI-ERR]).")
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

    log("CyberDeck Electron UI відкрито.")
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
    log("CyberDeck недоступний — відкриваю стандартне вікно (tkinter)...")
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

    _extra_scan_ports = [int(p) for p in (scan_ports or []) if str(p).isdigit()]

    result = {"ip": "", "port": 0, "ok": False}
    state = {"closed": False, "cancelled": False}
    devices = []
    dev_lock = threading.Lock()
    q = queue.Queue()

    root = tk.Tk()
    root.title("Virtual USB Cable — підключення телефону")
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

    status_var = tk.StringVar(value="Шукаю телефон по Wi-Fi...")

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
    btn_connect = ttk.Button(btn_frame, text="Підключити")
    btn_rescan = ttk.Button(btn_frame, text="Оновити")
    btn_manual_toggle = ttk.Button(btn_frame, text="Вручну...")
    btn_cancel = ttk.Button(btn_frame, text="Пропустити (демо)")
    for b in (btn_connect, btn_rescan, btn_manual_toggle, btn_cancel):
        b.pack(side="left", padx=(0, 6))

    pair_frame = ttk.LabelFrame(root, text="Парування (код з екрану телефону)")
    code_var = tk.StringVar(value=pair_code or "")
    ttk.Label(pair_frame, text="Код:").pack(side="left", padx=(6, 2))
    code_entry = ttk.Entry(pair_frame, textvariable=code_var, width=12)
    code_entry.pack(side="left")
    btn_pair = ttk.Button(pair_frame, text="Спарувати і підключити")
    btn_pair.pack(side="left", padx=6)

    manual_frame = ttk.LabelFrame(root, text="Вручну (з екрану: Бездротове налагодження)")
    man_vars = {}
    for key, label, width in (("ip", "IP:", 16), ("pair", "Порт парування:", 8),
                              ("code", "Код:", 8), ("conn", "Порт підключ.:", 8)):
        row = ttk.Frame(manual_frame)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text=label, width=16).pack(side="left")
        var = tk.StringVar()
        ttk.Entry(row, textvariable=var, width=width).pack(side="left")
        man_vars[key] = var
    btn_manual_go = ttk.Button(manual_frame, text="Спарувати і підключити")
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
                text = f"[спарувати] {d['name']} — {d['ip']}:{d['port']}"
            elif d["kind"] == "connect":
                text = f"[Wi-Fi] {d['name']} — {d['ip']}:{d['port']}"
            elif d["kind"] == "net":
                text = f"[IP] {d['ip']} — порт підбереться сам"
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
            set_status(f"Перепідключаюсь до відомого: {last}...")
            try:
                if _tcp_port_open(lip, lport, 0.5) and adb_connect_fast(lip, lport, timeout=4.0):
                    name = get_device_display_name(f"{lip}:{lport}")
                    save_last_phone(f"{lip}:{lport}")
                    result.update(ip=lip, port=lport, ok=True)
                    q.put(("connect_result", True, f"Підключено: {lip}:{lport} — {name}"))
                    q.put(("scan_done",))
                    return
            except Exception:
                pass
        if state["closed"]:
            return
        # 1) mDNS (Бездротове налагодження) — зазвичай відповідає за <1с
        set_status("Шукаю по mDNS (Бездротове налагодження)...")
        try:
            mdevs = mdns_discover_adb(timeout=mdns_timeout)
        except Exception as e:
            mdevs = []
            set_status(f"mDNS не вдався: {e}")
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
                    if name and "невідома" not in name:
                        item["name"] = name
                except Exception:
                    pass
            add_device(item)
        if state["closed"]:
            return
        # 2) --ip-only: БЕЗ скану портів — тільки живі IP, інакше скан портів.
        if SKIP_PORT_SCAN:
            set_status("Шукаю живі хости в мережі (без скану портів)...")
            alive = []
            try:
                for aip in discover_alive_ips():
                    if state["closed"]:
                        return
                    alive.append(aip)
                    add_device({"kind": "net", "ip": aip, "port": 0,
                                "name": "Хост у мережі (порт невідомий)",
                                "raw": {"serial": f"{aip}:0"}})
            except Exception:
                pass
            # У кого відповідає ADB — міняємо непідписаний рядок на підписаний
            if alive and not state["closed"]:
                set_status("Перевіряю у кого відкрито ADB...")
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
        set_status(f"Сканую LAN (порти {', '.join(str(p) for p in lan_ports)})...")
        last_ip = _split_serial(load_last_phone(), adb_port)[0] or None

        def lan_open(ip, port=None):
            if state["closed"]:
                return
            _p = port if port else lan_ports[0]
            add_device({"kind": "lan", "ip": ip, "port": _p,
                        "name": "опитування...", "raw": {"serial": f"{ip}:{_p}"}})

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
        set_status(f"Підключаюсь до {d['ip']}:{port}...")
        ok = adb_connect_fast(d["ip"], port, timeout=6.0) if port else False
        if not ok:
            set_status(f"Порт {port} мовчить, підбираю порт для {d['ip']} сам...")
            try:
                found = _find_connect_port_after_pair(d["ip"], port,
                                                      extra_ports=_extra_scan_ports)
            except Exception:
                found = 0
            if found and adb_connect_fast(d["ip"], found, timeout=6.0):
                ok, port = True, found
        if ok:
            name = d["name"]
            if d["kind"] in ("connect", "net") or name == "опитування...":
                name = get_device_display_name(f"{d['ip']}:{port}")
            save_last_phone(f"{d['ip']}:{port}")
            _gui_disconnect_others(snapshot(), d["ip"])
            result.update(ip=d["ip"], port=port, ok=True)
            q.put(("connect_result", True, f"Підключено: {d['ip']}:{port} — {name}"))
        else:
            q.put(("connect_result", False, f"Не вдалося підключитись до {d['ip']}:{d['port']}."))
        set_busy(False)

    def worker_pair(d, code):
        ip, pp = d["ip"], d["port"]
        set_status(f"Парування з {ip}:{pp}...")
        if not adb_pair_verify(ip, pp, code):
            q.put(("connect_result", False, "Парування не вдалося. Код одноразовий — оновіть екран."))
            set_busy(False)
            return
        set_status("Шукаю порт підключення цього телефону...")
        try:
            devs = mdns_discover_adb(timeout=2.0)
        except Exception:
            devs = []
        same = [x for x in devs if x["kind"] == "connect" and x["ips"] and x["ips"][0] == ip]
        if not same:
            # mDNS не дав порту — прощуп + перебір портів цього IP
            set_status("mDNS порту не дав — шукаю порт (прощуп, потім перебір)...")
            try:
                found = _find_connect_port_after_pair(ip, pp,
                                                      extra_ports=_extra_scan_ports)
            except Exception:
                found = 0
            if not found:
                q.put(("status", "Порт не знайшовся — введіть його вручну (IP address & Port)."))
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
            q.put(("connect_result", True, f"Підключено БЕЗ кабелю: {ip}:{cp} — {name}"))
        else:
            q.put(("connect_result", False, "Підключення не вдалося."))
        set_busy(False)

    def worker_manual(ip, pair_s, code, conn_s):
        if pair_s:
            set_status(f"Парування з {ip}:{pair_s}...")
            if not adb_pair_verify(ip, int(pair_s), code):
                q.put(("connect_result", False, "Парування не вдалося. Код одноразовий — оновіть екран."))
                set_busy(False)
                return
        set_status(f"Підключаюсь до {ip}:{conn_s}...")
        if adb_connect_fast(ip, int(conn_s), timeout=6.0):
            name = get_device_display_name(f"{ip}:{conn_s}")
            save_last_phone(f"{ip}:{conn_s}")
            _gui_disconnect_others(snapshot(), ip)
            result.update(ip=ip, port=int(conn_s), ok=True)
            q.put(("connect_result", True, f"Підключено БЕЗ кабелю: {ip}:{conn_s} — {name}"))
        else:
            q.put(("connect_result", False, "Підключення не вдалося."))
        set_busy(False)

    def on_connect():
        d = current()
        if not d:
            status_var.set("Виберіть пристрій зі списку.")
            return
        if d["kind"] == "pair":
            if not pair_frame.winfo_ismapped():
                pair_frame.pack(fill="x", padx=10, pady=5)
            code_entry.focus_set()
            status_var.set(f"Для {d['ip']}:{d['port']} введіть код з екрану і натисніть «Спарувати і підключити».")
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
            status_var.set("Виберіть пристрій [спарувати] зі списку.")
            return
        if not code:
            status_var.set("Введіть код з екрану телефону.")
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
            status_var.set("Введіть коректний IP телефону.")
            return
        if pair_s and (not pair_s.isdigit() or not code):
            status_var.set("Для парування потрібні порт і код з екрану.")
            return
        if not conn_s.isdigit():
            status_var.set("Введіть порт підключення (IP address & Port).")
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
        status_var.set("Шукаю телефон по Wi-Fi...")
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
                        status_var.set(f"Знайдено: {n}. Виберіть пристрій і натисніть «Підключити».")
                        try:
                            root.bell()
                        except Exception:
                            pass
                    else:
                        status_var.set("Нічого не знайдено. Увімкніть Бездротове налагодження, «Оновити» або «Вручну».")
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
            log(f"Пошук хостів не вдався: {e}")
            return "", adb_port, False
        if not alive:
            return "", adb_port, False
        # Підписуємо у кого відповідає ADB — вибір з назвами, а не голими IP
        log("Перевіряю у кого відкрито ADB...")
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
                log(f"Автовибір (--pick {pick}): {chosen_ip}.")
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
        log(f"LAN-сканування не вдалося: {e}")
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
    """Повний ланцюжок: USB->tcpip->connect, БЕЗ кабелю (Бездротове налагодження), скан LAN.

    Повертає (adb_host, adb_port_eff, demo_needed). Порт окремо бо в Бездротового
    налагодження він динамічний (не 5555).
    scan_ports: додаткові порти для LAN-скану (--scan-ports).
    """
    if dry_run:
        log("(dry-run) пропустив би tcpip/pair/connect/сканування LAN")
        return "127.0.0.1", adb_port, True
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
        log(f"Вже є Wi-Fi пристрій {already_tcp} — tcpip пропускаю.")
        log("Вікно вибору не відкриваю: пристрій вже підключено, воно не потрібне. "
            "Щоб примусово побачити вікно: START.bat --scan (або adb disconnect).")
        save_last_phone(already_tcp)
        host, port = _split_serial(already_tcp, adb_port)
        return host, port, False
    if already_tcp and force_scan and (allow_scan or allow_wireless):
        # --scan: навіть якщо щось вже підключено, показуємо ВСІ доступні IP і даємо вибрати
        log(f"Примусове сканування (--scan): вже підключено {already_tcp}, але дивлюсь всі IP в мережі...")
        if use_gui and _gui_usable() and not pick:
            log("Відкриваю вікно вибору пристрою (--scan)...")
            try:
                _g = gui_pick_and_connect(adb_port, mdns_timeout, scan_timeout, pair_code,
                                          scan_ports=scan_ports)
            except Exception as e:
                log(f"Вікно не відкрилось ({e}) — продовжую в консолі.")
                _g = None
            if _g is not None:
                _ip, _port, _ok = _g
                if _ok:
                    return _ip, _port, False
                log("У вікні нічого не вибрано — пробую консольний шлях.")
        elif pick:
            log(f"Вікно пропущено: задано --pick {pick} (автовибір без вікна).")
        elif not use_gui:
            log("Вікно пропущено: режим --no-gui (тільки консоль). "
                "Приберіть --no-gui або додайте --gui щоб побачити вікно.")
        elif not _gui_usable():
            log("Вікно пропущено: в цьому Python нема tkinter. "
                "Перевстановіть Python з https://www.python.org/downloads/ "
                "(лишіть опцію 'tcl/tk and IDLE'). Продовжую в консолі.")
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
        log(f"Залишаю вже підключений {already_tcp}.")
        host, port = _split_serial(already_tcp, adb_port)
        return host, port, False
    if phone_ip_override:
        log(f"Задано --phone-ip={phone_ip_override}, роблю adb connect САМ...")
        ok = adb_connect_verify(phone_ip_override, adb_port)
        if ok:
            save_last_phone(f"{phone_ip_override}:{adb_port}")
            return phone_ip_override, adb_port, False
        # Порт міг бути динамічним (Бездротове налагодження): шукаємо реальний
        found = resolve_connect_port(phone_ip_override, adb_port=adb_port,
                                     mdns_timeout=mdns_timeout, extra_ports=scan_ports)
        if found and found != adb_port:
            log(f"Порт {adb_port} мовчить, пробую знайдений {found}...")
            if adb_connect_verify(phone_ip_override, found):
                save_last_phone(f"{phone_ip_override}:{found}")
                return phone_ip_override, found, False
        return phone_ip_override, adb_port, True
    has_usb = any(":" not in s and st == "device" for s, st, t in devs)
    if not has_usb:
        # Телефон міг залишитись в tcpip-режимі з минулого запуску (USB тоді мертвий).
        # Пробуємо реанімацію: reconnect + останній відомий IP — все САМ.
        log("USB-пристроїв нема. Пробую реанімацію (телефон міг залишитись в tcpip-режимі)...")
        run(["adb", "reconnect"], timeout=30)
        time.sleep(1.5)
        devs = print_adb_devices()
        for s, st, t in devs:
            if ":" in s and st == "device":
                log(f"Реанімація вдалася: {s}.")
                save_last_phone(s)
                host, port = _split_serial(s, adb_port)
                return host, port, False
        last = load_last_phone()
        if last:
            log(f"Пробую останній відомий пристрій САМ: {last} ...")
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
        # Кабелю нема — вискакує ВІКНО зі знайденими телефонами (як Bluetooth):
        # скан у фоні, клік → підключення, код парування — в тому ж вікні.
        if use_gui and _gui_usable() and not pick:
            log("Відкриваю вікно вибору пристрою...")
            try:
                g = gui_pick_and_connect(adb_port, mdns_timeout, scan_timeout, pair_code,
                                         scan_ports=scan_ports)
            except Exception as e:
                log(f"Вікно не відкрилось ({e}) — продовжую в консолі.")
                g = None
            if g is not None:
                ip, port, ok = g
                if ok:
                    return ip, port, False
                log("Вікно закрито без вибору — НЕ йду в демо, пробую консольний шлях.")
                use_gui = False  # щоб нижче не відкривати вікно повторно
        elif not has_usb:
            # Вікно мало відкритись, але пропущено — пояснюємо чому, інакше
            # виглядає як «UI не відкривається» без жодної підказки.
            if pick:
                log(f"Вікно пропущено: задано --pick {pick} (автовибір без вікна). "
                    "Приберіть --pick щоб побачити вікно.")
            elif not use_gui:
                log("Вікно пропущено: режим --no-gui (тільки консоль). "
                    "Приберіть --no-gui або додайте --gui щоб побачити вікно.")
            elif not _gui_usable():
                log("Вікно пропущено: в цьому Python нема tkinter. "
                    "Перевстановіть Python з https://www.python.org/downloads/ "
                    "(під час встановлення лишіть опцію 'tcl/tk and IDLE'). "
                    "Поки що продовжую в консолі.")
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
            log("Дивлюсь усі доступні IP-адреси в мережі (5555 + динамічні порти Wi-Fi)...")
            ip, port, ok = _try_lan_scan_pick(adb_port, scan_timeout, pick,
                                              scan_ports=scan_ports, mdns_timeout=mdns_timeout)
            if ok:
                return ip, port, False
            log("У мережі нічого придатного не вибрано.")
        log("Реанімація не вдалася. Варіанти:")
        log("  - БЕЗ КАБЕЛЮ (Android 11+): увімкніть 'Бездротове налагодження' і запустіть знову — запропоную парування по коду")
        log("  - Перевставте USB-кабель і (якщо телефон завис в tcpip) перезавантажте телефон, потім START.bat")
        log("  - Або задайте IP вручну: --phone-ip <IP з Налаштування -> Про телефон -> Статус>")
        log("  - Або демо без телефону: START.bat demo")
        return "127.0.0.1", adb_port, True
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
        log("Не визначив IP через USB. Пробую БЕЗ кабелю, потім скан мережі...")
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
        log("Не визначив IP. Передайте вручну: --phone-ip <IP телефону>")
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
        log("USB-кандидати не підійшли. Пробую БЕЗ кабелю (Бездротове налагодження)...")
        ip, port, ok = _try_wireless_debug(mdns_timeout, pair_code, pick,
                                           adb_port=adb_port,
                                           extra_ports=scan_ports)
        if ok:
            return ip, port, False
    if allow_scan:
        log("")
        log("Дивлюсь усі доступні IP-адреси в мережі...")
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
            reasons.append(f"IP телефону {phone_ip} у стандартній підмережі хотспоту Windows ({HOTSPOT_SUBNET})")
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
                reasons.append(f"інтерфейс '{a['adapter']}' — віртуальний адаптер роздачі (hosted network)")
            if "wi-fi direct" in name or "hotspot" in name or "hosted" in name:
                reasons.append(f"інтерфейс '{a['adapter']}' ({a['desc']}) — роздача Wi-Fi з ПК")
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
    "high": ([], 30, "повна якість (швидкий роутер)"),
    "medium": (["-m", "1280", "-b", "6M", "--max-fps", "30"], 60,
               "середня якість (повільний/нестабільний канал)"),
    "low": (["-m", "1024", "-b", "4M", "--max-fps", "24"], 100,
            "економна якість (дуже повільний канал, хотспот 2.4 ГГц)"),
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
    "Увімкніть діапазон 5 ГГц для роздачі: Параметри → Мережа й Інтернет → Мобільний хот-спот → Діапазон мережі → 5 ГГц (2.4 ГГц перевантажений і дає затримки).",
    "Вимкніть економію живлення Wi-Fi: Диспетчер пристроїв → Мережеві адаптери → ваш Wi-Fi → Керування живленням → ЗНЯТИ 'Дозволити вимикати пристрій'.",
    "Тримайте телефон ближче до ПК і подалі від мікрохвильовки/Bluetooth-колонок (глушать 2.4 ГГц).",
    "Найстабільніше — той самий Wi-Fi роутер для ПК і телефону (а не роздача з ПК).",
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
        log(f"Якість: {prof} (примусово --quality) — {label}.")
        if args.scrcpy_args.strip() != "--select-usb":
            log("Свої --scrcpy-args важливіші — бітрейт не чіпаю, застосовую тільки jitter-буфер.")
            return [], jitter
        return list(extra), jitter
    if getattr(args, "quality", "auto") == "high":
        return [], 30
    if args.scrcpy_args.strip() != "--select-usb":
        log("Свої --scrcpy-args задано вручну — авто-якість не чіпаю.")
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
        log(f"Виявлено роздачу інтернету з ПК (хотспот): {reason}.")
        log("Хотспот зазвичай повільніший за роутер (2.4 ГГц + NAT) — міряю канал...")
    else:
        log("Міряю канал до телефону (--measure)...")
    m = measure_link(adb_host)
    if not m["ok"]:
        log("Заміряти канал не вдалося (ping без відповіді) — лишаю повну якість.")
        return [], 30
    log(f"Канал: RTT сер.={m['rtt_avg']}мс (мін {m['rtt_min']}, макс {m['rtt_max']}), "
        f"джиттер ~{m['jitter']}мс, втрати {m['loss']}%.")
    prof = pick_quality_profile(m["rtt_avg"], m["loss"])
    extra, jitter, label = QUALITY_PROFILES[prof]
    if prof == "high":
        log("Канал хороший — повна якість.")
        return [], 30
    log(f"Канал слабкий — вмикаю профіль '{prof}': {label}.")
    log(f"scrcpy: {' '.join(extra)}; jitter-буфер приймача: {jitter}мс.")
    if is_hs:
        log("Поради щоб прибрати затримку на хотспоті:")
        for t in HOTSPOT_TIPS:
            log(f"  • {t}")
    return list(extra), jitter


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
        return False
    installed_any = False
    for tool, pkg in WINGET_PKGS:
        if shutil.which(tool):
            continue
        log(f"Довстановлюю {tool} САМ: winget install {pkg} ... (це займе 1-3 хв)")
        rc, _ = run(["winget", "install", "--accept-source-agreements", "--accept-package-agreements",
             "-e", "--id", pkg], timeout=600)
        if rc == 0:
            installed_any = True
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
    """Запропонувати довстановити відсутнє через winget, спитавши підтвердження.

    missing: список (tool, winget_id). auto_yes=True — встановити без питань
    (режим --install-deps / --yes). Повертає True якщо встановлення виконано
    (або спробовано), False якщо користувач відмовився / нема winget / неінтерактивно.
    """
    if not missing:
        return False
    names = ", ".join(t for t, _ in missing)
    log(f"Бракує: {names}.")
    if os.name != "nt" or not shutil.which("winget"):
        log("Автовстановлення можливе тільки на Windows з winget.")
        for tool, pkg in missing:
            log(f"  Вручну: winget install {pkg}")
        return False
    if auto_yes:
        log("Автовстановлення увімкнено прапорцем — ставлю без питань...")
        install_deps()
        return True
    if not _stdin_interactive():
        log("Неінтерактивний режим — пропускаю автовстановлення.")
        for tool, pkg in missing:
            log(f"  Вручну: winget install {pkg}  (або запустіть з --install-deps)")
        return False
    log(f"Можу довстановити САМ через winget ({names}, 1-3 хв, може попросити UAC).")
    if ask_yes_no("Встановити відсутнє автоматично?", default_yes=True):
        log("Ок, встановлюю...")
        install_deps()
        return True
    log("Ок, пропускаю автовстановлення. Продовжую з тим що є.")
    for tool, pkg in missing:
        log(f"  Коли буде час: winget install {pkg}  (або START.bat install)")
    return False

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


def check_gui():
    """Діагностика вікон: tkinter (стандартне) + Node/Electron (CyberDeck)."""
    log("  --- вікна (GUI) ---")
    try:
        import tkinter  # noqa: F401
        log("  tkinter: OK (стандартне вікно працюватиме)")
    except ImportError:
        log("  tkinter: НЕМА — вікна не буде, тільки консоль. "
            "Перевстановіть Python з https://www.python.org/downloads/ "
            "(лишіть опцію 'tcl/tk and IDLE').")
    ui_dir = ROOT / "ui"
    log(f"  ui/package.json: {'OK' if (ui_dir / 'package.json').exists() else 'НЕМА (запуск не з кореня репозиторію?)'}")
    _exe = ui_dir / "node_modules" / "electron" / "dist" / "electron.exe"
    if _exe.exists():
        log("  electron: OK (CyberDeck вікно працюватиме)")
    else:
        log("  electron: НЕМА — для CyberDeck вікна одноразово: cd ui && npm install "
            "(потрібен Node.js LTS з https://nodejs.org). "
            "Без нього працюватиме стандартне вікно (tkinter).")
    _node = shutil.which("node")
    _npm = shutil.which("npm.cmd") or shutil.which("npm")
    log(f"  node: {_node if _node else 'НЕМА (https://nodejs.org)'}")
    log(f"  npm: {_npm if _npm else 'НЕМА (https://nodejs.org)'}")


def offer_install_compiler(auto_yes=False):
    """Запропонувати встановити MSVC Build Tools (важкий пакет!) з підтвердженням.

    Повертає True якщо встановлення спробовано, False якщо відмова/неможливо.
    """
    if os.name != "nt" or not shutil.which("winget"):
        log("Автовстановлення компілятора можливе тільки на Windows з winget.")
        return False
    if check_compiler():
        return False
    log("УВАГА: Build Tools це 3-8 ГБ і 10-30 хв, ставиться ОДИН раз. Буде UAC-запит.")
    do_it = bool(auto_yes)
    if not do_it:
        if not _stdin_interactive():
            log("Неінтерактивний режим — компілятор не ставлю (запустіть з --install-deps щоб ставити без питань).")
            return False
        do_it = ask_yes_no("Встановити Visual Studio Build Tools з C++ зараз?", default_yes=False)
    if not do_it:
        log("Ок, без компілятора збірка C++ неможлива — продовжую без неї (fallback через scrcpy по TCP, якщо телефон вже по Wi-Fi).")
        return False
    log("Ставлю Build Tools САМ (довго, не закривайте вікно)...")
    run(["winget", "install", "--accept-source-agreements", "--accept-package-agreements",
         "-e", "--id", "Microsoft.VisualStudio.2022.BuildTools", "--override",
         "--quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"],
        timeout=3600)
    if check_compiler():
        log("Компілятор з'явився — продовжую збірку.")
        return True
    log("Компілятор так і не знайдено (можливо потрібен перезапуск консолі).")
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
    ap.add_argument("--quality", choices=["auto", "high", "medium", "low"], default="auto",
                    help="Якість відео: auto (сам заміряє канал на хотспоті), high/medium/low (примусово)")
    ap.add_argument("--measure", action="store_true",
                    help="Примусово заміряти канал (ping) і підібрати якість, навіть без хотспоту")
    ap.add_argument("--check", action="store_true", help="Тільки перевірити залежності і вийти")
    ap.add_argument("--dry-run", action="store_true", help="Тільки показати команди, нічого не запускати")
    ap.add_argument("--install-deps", action="store_true",
                    help="Самому доустановити відсутнє (cmake/adb/scrcpy) через winget БЕЗ питань")
    ap.add_argument("--no-install-deps", action="store_true",
                    help="Ніколи не пропонувати автовстановлення (тільки перевірка + підказки)")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="На всі питання про довстановлення відповідати ТАК (для скриптів)")
    ap.add_argument("--scan", action="store_true",
                    help="Примусово просканувати всю LAN і запропонувати вибір пристрою (з назвою), "
                         "навіть якщо телефон вже підключено")
    ap.add_argument("--no-scan", action="store_true",
                    help="НЕ сканувати мережу (тільки USB/last-phone/--phone-ip)")
    ap.add_argument("--scan-timeout", type=float, default=0.45,
                    help="Таймаут одного IP при скануванні LAN, сек (default 0.45)")
    ap.add_argument("--pick", default="",
                    help="Автовибір пристрою без запиту: номер зі списку або IP (для скриптів)")
    ap.add_argument("--pair-code", default="",
                    help="6-значний код парування з екрану (Бездротове налагодження) — без запиту")
    ap.add_argument("--mdns-timeout", type=float, default=2.5,
                    help="Скільки секунд слухати mDNS-анонси телефону (default 2.5)")
    ap.add_argument("--mdns-check", action="store_true",
                    help="Діагностика ефіру: 8с слухати mDNS і показати ЧОМУ автопошук "
                         "не бачить телефон (тиша / нема adb-анонсів / все чути)")
    ap.add_argument("--no-wireless", action="store_true",
                    help="НЕ пробувати підключення без кабелю (тільки USB/tcpip/скан LAN)")
    ap.add_argument("--scan-ports", default="",
                    help="Додаткові ADB-порти для LAN-скану через кому (напр. 37123,41234). "
                         "За замовчуванням скан і так дивиться 5555 + порт останнього "
                         "пристрою + динамічні порти з mDNS; це — для ручного доповнення.")
    ap.add_argument("--no-auto-scrcpy", action="store_true",
                    help="В electron-режимі НЕ запускати scrcpy -s після adb connect (тільки adb)")
    ap.add_argument("--ip-only", action="store_true",
                    help="Те саме що за замовчуванням: шукати тільки живі IP "
                         "(без TCP-скану портів по мережі). Залишено для сумісності.")
    ap.add_argument("--port-scan", action="store_true",
                    help="Увімкнути масовий TCP-скан портів по мережі (5555 + динамічні). "
                         "За замовчуванням вимкнено: шукаються тільки живі IP, "
                         "а порти підбираються точково на вибраному.")
    ap.add_argument("--gui", action="store_true",
                    help="Примусово показати вікно вибору пристрою (за замовчуванням вікно і так вискакує само)")
    ap.add_argument("--no-gui", action="store_true",
                    help="Без вікна — тільки консольні запити")
    ap.add_argument("--electron-mode", action="store_true",
                    help="JSON event mode for Electron GUI")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    global SKIP_PORT_SCAN
    SKIP_PORT_SCAN = not bool(args.port_scan)
    if SKIP_PORT_SCAN:
        log("Режим за замовчуванням: шукаю тільки живі IP (без масового скану портів).")
    else:
        log("Режим --port-scan: сканую TCP-порти по мережі.")

    if args.mdns_check:
        print("=== mDNS-діагностика ефіру ===", flush=True)
        mdns_listen_check()
        return 0

    if args.electron_mode:
        _cli_scan_ports = [p for p in parse_scan_ports(args.scan_ports, args.adb_port)
                           if p != args.adb_port] if args.scan_ports else []
        electron_gui_mode(adb_port=args.adb_port, mdns_timeout=args.mdns_timeout,
                          scan_timeout=args.scan_timeout, scan_ports=_cli_scan_ports,
                          auto_scrcpy=(not args.no_auto_scrcpy))
        return 0

    print("=== Virtual USB Cable — АВТОПІЛОТ (сам вводить всі команди) ===", flush=True)

    # [0] залежності (+ довстановлення з підтвердженням)
    # --install-deps / --yes = ставити мовчки; --no-install-deps = тільки підказки;
    # за замовчуванням в інтерактивному терміналі ПИТАЄМО, в неінтерактиві — тільки підказки.
    auto_yes = bool(getattr(args, "install_deps", False) or getattr(args, "yes", False))
    no_install = bool(getattr(args, "no_install_deps", False))
    if auto_yes and not args.dry_run and not no_install:
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
    if (not has_adb or not has_scrcpy or not has_cmake) and not args.dry_run and not no_install:
        missing = [(t, p) for t, p in WINGET_PKGS if not shutil.which(t)]
        if missing:
            offer_install_missing_deps(missing, auto_yes=auto_yes)
            # перечитати після можливого встановлення
            has_adb = shutil.which("adb") is not None
            has_scrcpy = shutil.which("scrcpy") is not None
            has_cmake = shutil.which("cmake") is not None
            log(f"  повторна перевірка: adb={'OK' if has_adb else 'НЕМА'} "
                f"scrcpy={'OK' if has_scrcpy else 'НЕМА'} cmake={'OK' if has_cmake else 'НЕМА'}")
    if args.check:
        check_compiler()
        check_gui()
        return 0 if (has_adb and has_scrcpy) else 1
    if not has_adb and args.mode == "real":
        log("ERROR: для --mode real потрібен adb.")
        return 1

    # [1-2] телефон (крім demo)
    adb_host = "127.0.0.1"
    adb_port_eff = args.adb_port
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
            if (args.mode == "real" and not devs and not args.phone_ip
                    and args.no_scan and args.no_wireless):
                log("ERROR: --mode real, але 'adb devices' порожньо і --phone-ip не задано.")
                log("Варіанти: USB-кабель, --phone-ip, або БЕЗ кабелю через Бездротове налагодження (без --no-wireless).")
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
                    log("ERROR: телефон так і не з'явився. Перевірте USB/Wi-Fi і RSA-підтвердження.")
                    return 2
            use_fake_adb = need_demo and (args.mode != "real")

    # [3] збірка
    log("Крок 3: збірка (якщо треба, САМ)...")
    sender_bin, receiver_bin = ensure_built(args.skip_build, args.dry_run,
                                            auto_yes=auto_yes, no_install=no_install)
    if args.dry_run:
        log("(dry-run) далі тільки показую команди запуску:")
        log_cmd(f"agent_sender --adb {adb_host}:{adb_port_eff} --listen 0.0.0.0:{args.sender_port}"
                + (" --simulate-adb" if use_fake_adb else ""))
        log_cmd(f"agent_receiver --server 127.0.0.1:{args.sender_port} --auto --simulate"
                + (" --vhci" if args.with_vhci else ""))
        if not args.no_scrcpy:
            _dry_quality, _ = decide_quality(args, adb_host, use_fake_adb)
            log_cmd(" ".join(build_scrcpy_cmd(args, _dry_quality, adb_host, adb_port_eff, use_fake_adb)))
        log("DRY-RUN готово.")
        return 0
    # [3b] якість каналу (хотспот? замір? профіль scrcpy + jitter).
    # В демо decide_quality нічого не робить (нема телефону).
    log("Крок 3b: перевірка якості каналу (хотспот/замір)...")
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
                fb = subprocess.Popen(["scrcpy", "-s", tcp_serial] + quality_extra)
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
    if jitter_ms != 30:
        # Старі бінарники невідомий прапорець мовчки ігнорують — безпечно
        receiver_cmd += ["--jitter-ms", str(jitter_ms)]
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

    # [7] scrcpy — для Wi-Fi пристрою ОБОВ'ЯЗКОВО '-s IP:port', а не --select-usb
    # (--select-usb бачить тільки USB і давав чорний екран / "no device").
    scrcpy_cmd = build_scrcpy_cmd(args, quality_extra, adb_host, adb_port_eff, use_fake_adb)
    scrcpy_proc = None
    if not args.no_scrcpy:
        if not has_scrcpy:
            log("Крок 7: scrcpy НЕМА — пропускаю автозапуск. Встановіть: winget install Genymobile.scrcpy")
        else:
            log(f"Крок 7: запускаю scrcpy САМ: {' '.join(scrcpy_cmd)} ...")
            try:
                scrcpy_proc = subprocess.Popen(scrcpy_cmd)
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
                print(f"[EV] >> у вікно: {event_type} (x{n})", flush=True)
        except Exception as ex:
            print(f"[ERROR] emit {event_type}: {ex}", file=sys.stderr, flush=True)

    seen_devices = {}
    dev_lock = threading.Lock()

    def report_device(device):
        key = (device.get('kind', 'lan'), device.get('ip'), device.get('port'))
        with dev_lock:
            if key in seen_devices:
                old = seen_devices[key]
                if old.get('name') == "опитування..." and device.get('name') != "опитування...":
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
        emit('status-update', 'Шукаю пристрої по mDNS та в мережі...')
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
                    'name': name if name and 'невідома' not in name else f"Останній ({lip})",
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
                        if name and "невідома" not in name:
                            item["name"] = name
                    except Exception:
                        pass
                report_device(item)
        except Exception as e:
            emit('status-update', f'mDNS: {e}')

        # 2) --ip-only: БЕЗ скану портів — повне виявлення живих IP (ping+arp),
        # порти підберуться точково на вибраному рядку. Інакше — скан портів.
        if SKIP_PORT_SCAN:
            emit('status-update', 'Шукаю живі хости в мережі (без скану портів)...')
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
                            "name": "Хост у мережі (порт невідомий)",
                            "state": "unknown"
                        })
            except Exception as e:
                emit('status-update', f'Пошук хостів: {e}')
            # У кого відповідає ADB — міняємо непідписаний рядок на підписаний
            if alive:
                emit('status-update', 'Перевіряю у кого відкрито ADB...')
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
                    "name": "опитування...",
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
                emit('status-update', f'LAN скан: {e}')

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
                        "name": "Хост у мережі (порт невідомий)",
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
        emit('status-update', f'Пошук завершено. Знайдено {len(seen_devices)} пристроїв.')

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
                emit('status-update', f'Підключення до {ip}:{port}...')
                def do_connect(tip=ip, tport=port):
                    # Порт 0 = «невідомий» (рядок живого хоста): одразу підбираємо самі.
                    result = adb_connect_fast(tip, tport) if tport else False
                    eff_port = tport
                    if not result:
                        # Порт міг змінитись (динамічний порт Бездротового налагодження):
                        # підбираємо самі: прощуп + перебір 30000-49999.
                        emit('status-update', f'Порт {tport} мовчить, підбираю порт для {tip} сам...')
                        try:
                            found = _find_connect_port_after_pair(
                                tip, tport, extra_ports=_extra_scan_ports)
                        except Exception:
                            found = 0
                        if found:
                            emit('status-update', f'Пробую {tip}:{found}...')
                            if adb_connect_fast(tip, found, timeout=5.0):
                                result = True
                                eff_port = found
                    name = get_device_display_name(f"{tip}:{eff_port}") if result else ""
                    scrcpy_ok = False
                    if result:
                        save_last_phone(f"{tip}:{eff_port}")
                        if auto_scrcpy:
                            emit('status-update', f'Підключено до {tip}:{eff_port} — запускаю scrcpy...')
                            scrcpy_ok = launch_scrcpy_for_device(tip, eff_port)
                    emit('connect-result', {
                        'success': bool(result),
                        'ip': tip,
                        'port': eff_port,
                        'name': name,
                        'scrcpy': scrcpy_ok,
                        'error': '' if result else 'Не вдалося підключитися (перевірте IP/порт з екрану «IP address & Port»)'
                    })
                    if result:
                        emit('status-update', f'Підключено до {tip}:{eff_port}' + ('' if scrcpy_ok else ' (scrcpy не стартував — запустіть вручну: scrcpy -s %s:%s)' % (tip, eff_port)))
                    else:
                        emit('status-update', f'Помилка підключення до {tip}:{tport}')
                threading.Thread(target=do_connect, daemon=True).start()
            elif cmd_type == 'pair':
                ip = cmd['ip']
                port = int(cmd['port'])
                code = str(cmd['code']).strip()
                emit('status-update', f'Парування з {ip}:{port}...')
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
                        emit('status-update', f'Підключено БЕЗ кабелю: {tip}:{cp} — {name}' if conn_ok else 'Не вдалося підключитися до порту підключення.')
                        return conn_ok
                    result = adb_pair_verify(tip, tport, tcode)
                    if result:
                        emit('pair-result', {'success': True, 'ip': tip, 'port': tport})
                        emit('status-update', 'Успішно спаровано! Шукаю порт підключення...')
                        try:
                            devs = mdns_discover_adb(timeout=2.0)
                            same = [x for x in devs if x["kind"] == "connect" and x["ips"] and x["ips"][0] == tip]
                            if same and _finish_connect(same[0]["port"]):
                                return
                        except Exception:
                            pass
                        # mDNS не дав порту — прощуп + перебір портів цього IP
                        emit('status-update', 'mDNS порту не дав — шукаю порт (прощуп, потім перебір)...')
                        try:
                            found = _find_connect_port_after_pair(
                                tip, tport, extra_ports=_extra_scan_ports)
                        except Exception:
                            found = 0
                        if found and _finish_connect(found):
                            return
                        emit('status-update',
                             'Порт підключення не знайшов. Введіть його вручну: '
                             'на телефоні «IP address & Port» → форма «Вручну» → Підключити.')
                    else:
                        emit('pair-result', {'success': False, 'ip': tip, 'port': tport, 'error': 'Парування не вдалося'})
                        emit('status-update', 'Парування не вдалося. Перевірте код.')
                threading.Thread(target=do_pair, daemon=True).start()
        except Exception as e:
            emit('error', {'message': str(e)})


if __name__ == "__main__":
    sys.exit(main())
