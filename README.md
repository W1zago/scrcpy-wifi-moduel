# scrcpy-wifi-moduel — Virtual USB Cable (Kernel-Level)

Повноцінна архітектура рівня ядра для передачі USB-пакетів `scrcpy` через Wi-Fi/Internet з емуляцією фізичного USB на Windows Host.

> **Детальна архітектура:** див. [ARCHITECTURE.md](ARCHITECTURE.md) — 8 розділів, Mermaid діаграми, порівняння VHCI vs UsbDk, QUIC vs TCP, GHOST mode.

## Автозапуск однією командою (нічого вводити вручну не треба)

Програма **сама вводить всі команди**: `adb start-server`, `adb tcpip`, `adb connect`, визначення IP телефону, збірку, запуск sender+receiver і `scrcpy`.

```powershell
# Варіант A — подвійний клік (найпростіше)
START.bat                # авто: реальний телефон якщо є, інакше демо
START.bat demo           # демо без телефону (fake adbd, все симулюється)
START.bat real           # вимагає телефон (помилка якщо не знайдено)

# Варіант B — з консолі (ті самі можливості + опції)
python tools/auto_run.py                       # = START.bat
python tools/auto_run.py --mode demo           # без телефону
python tools/auto_run.py --mode real --phone-ip 192.168.1.100  # телефон за IP
python tools/auto_run.py --check               # тільки перевірка залежностей
python tools/auto_run.py --dry-run             # тільки показати команди, нічого не запускати
python tools/auto_run.py --with-vhci           # спробувати справжній vhci.sys
python tools/auto_run.py --no-scrcpy           # не запускати scrcpy автоматично
```

Що автопілот робить САМ, по кроках:

| Крок | Команда (вводиться автоматично) |
|---|---|
| 0 | Перевірка `adb`, `scrcpy`, `cmake` (підказує `winget install ...` якщо нема) |
| 1 | `adb start-server` |
| 2 | `adb devices -l` → якщо USB: `adb tcpip 5555` → `adb shell ip route` (IP) → `adb connect IP:5555` |
| 3 | `cmake -B build` + `cmake --build build --config Release` (якщо бінарників нема) |
| 4 | Запуск `agent_sender` (з `--simulate-adb` в демо, з `--auto` для реального телефону) |
| 5 | Запуск `agent_receiver --auto` (сам робить `adb devices`, перевіряє VHCI) |
| 6 | Пауза + контрольний `adb devices` |
| 7 | Запуск `scrcpy --select-usb` |
| 8 | `Ctrl+C` → коректна зупинка всіх процесів |

Окремо бінарники теж вміють `--auto` (кожен сам вводить свої команди):

```powershell
.\build\Release\agent_sender.exe --auto --listen 0.0.0.0:22777
# сам: adb start-server, adb tcpip 5555, визначення IP, adb connect

.\build\Release\agent_receiver.exe --auto --simulate
# сам: adb start-server, перевірка vhci/testsigning, adb devices, запуск scrcpy
.\build\Release\agent_receiver.exe --auto --vhci --server 192.168.1.100:22777
```

### Одноразове налаштування Windows (все само)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1
# сам: winget install cmake/adb/scrcpy, перевірка Test Signing,
#      скачування+встановлення usbip-win2, adb start-server + adb devices
```

> **Примітка:** `scripts/setup_windows.ps1` має лишатись у кодуванні **UTF-8 with BOM** (інакше PowerShell 5.1 ламається на кирилиці).

## Ручний режим (якщо треба покроково)

### 1. Встановити драйвер (один раз)

```powershell
# Увімкнути Test Signing (потрібен для usbip-win2 без EV сертифіката)
bcdedit /set testsigning on
# Перезавантажити

# Встановити usbip-win2: https://github.com/vadimgrn/usbip-win2/releases
# Скачати usbip-win2.msi і встановити
```

### 2. Зібрати

```powershell
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release

# Тести
ctest --test-dir build -V
```

### 3. Запустити (тест без телефону — симуляція)

Відкрий 2 термінали:

```powershell
# Термінал 1 — Sender (симулює телефон + adbd)
.\build\Release\agent_sender.exe --simulate-adb --adb 127.0.0.1:5555 --listen 0.0.0.0:22777

# Термінал 2 — Receiver (створює віртуальний USB)
.\build\Release\agent_receiver.exe --server 127.0.0.1:22777 --simulate
# З --vhci: створить реальний пристрій в Device Manager
.\build\Release\agent_receiver.exe --server 127.0.0.1:22777 --vhci

# Перевірити
adb devices
# Має показати: 0123456789ABCDEF    device  (transport usb, а не tcp!)

# Запустити scrcpy
scrcpy --select-usb
```

### 4. Запустити з реальним телефоном

```powershell
# На телефоні: увімкнути Wireless Debugging (Android 11+)
# Налаштування -> Для розробників -> Wireless debugging -> Pair

# Або через USB один раз:
adb tcpip 5555
adb shell ip addr show wlan0  # дізнатися IP телефону, напр. 192.168.1.100

# На Windows (Sender на телефоні — APK, або тимчасово через adb forward)
# Для PoC: запустити sender на Windows який форвардить на телефон:
.\build\Release\agent_sender.exe --adb 192.168.1.100:5555 --listen 0.0.0.0:22777

# Receiver як і раніше
.\build\Release\agent_receiver.exe --server 127.0.0.1:22777 --vhci
```

## Архітектура

```
[Android adbd:5555] <--TCP--> [Agent-Sender (NDK)] <--QUIC/WAN--> [Agent-Receiver] --> [vhci.sys] --> [WinUSB] --> [adb:5037] --> [scrcpy]
                                  No-Root!                TLS 1.3              Ghost Mode!
```

**Ключові рішення:**

- **VHCI:** `usbip-win2` (Virtual Host Controller) — єдиний спосіб дати `adb` справжній `USB` транспорт на Windows. `UsbDk` — deprecated.
- **Транспорт:** `QUIC` (msquic) з 3 streams (Control, Bulk, Video) + fallback на `TCP+TLS`. `UDP+FEC` тільки для відео.
- **GHOST mode:** При обриві мережі пристрій НЕ видаляється 30с, URB ставляться в чергу, replay при реконекті — `scrcpy` не крашиться.
- **Jitter buffer:** 30мс для згладжування джиттеру WAN.
- **FEC:** Reed-Solomon(14,10) для відео — відновлення до 4 втрат без ретрансміту.

## Структура

```
src/common/protocol.h      — USBIP+WAN+ADB заголовки
src/common/fec.h/.cpp      — FEC для відео
src/common/ring_buffer.h   — Lock-free ring + UrbPool
src/common/auto_setup.h    — Автоввід команд (adb/vhci/scrcpy), header-only
src/agent_receiver/        — Windows VHCI інжектор, ADB spoof, Ghost (+ --auto)
src/agent_sender/          — ADB bridge, Gadget stub (Tier-A) (+ --auto)
tools/auto_run.py          — Автопілот: одна команда на все
scripts/setup_windows.ps1  — Автоналаштування Windows (UTF-8 with BOM!)
START.bat                  — Подвійний клік = автопілот
tests/                     — test_fec, test_protocol, test_ringbuf
```

## Обмеження (чесно)

- Без root/Pi — тільки ADB (достатньо для scrcpy), без MTP/PTP. З root/Pi — повний USB passthrough (див. `gadget_stub.cpp`).
- Драйвер потребує `Test Signing` або EV сертифікат.
- WAN латентність >100мс дає лаг (фізика). Для геймінгу — тільки LAN.
- Див. повний список в [ARCHITECTURE.md §8](ARCHITECTURE.md#8-обмеження-та-чесні-застереження)

## Ліцензія

MIT
"# scrcpy-wifi-moduel" 
