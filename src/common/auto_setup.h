#pragma once
/**
 * auto_setup.h — Автоввід всіх системних команд (щоб користувач нічого не вводив вручну)
 *
 * Використовується обома бінарниками:
 *  - agent_receiver --auto  : сам робить adb start-server, перевіряє vhci, запускає scrcpy
 *  - agent_sender --auto    : сам робить adb tcpip 5555, визначає IP телефону, робить adb connect
 *
 * Header-only, без зовнішніх залежностей, тільки STL + Win32 API.
 * Всі функції логують команду ПЕРЕД виконанням: [AUTO] $ <cmd>
 */

#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <array>
#include <chrono>
#include <thread>

#ifdef _WIN32
  #ifndef NOMINMAX
  #define NOMINMAX
  #endif
  #include <windows.h>
#else
  #include <unistd.h>
#endif

namespace autosetup {

// ============================================================================
// Базовий запуск команди з захопленням виводу
// ============================================================================

struct ExecResult {
    int exit_code = -1;
    std::string output;   // stdout+stderr
    bool ok() const { return exit_code == 0; }
};

inline void LogCmd(const std::string& cmd) {
    printf("[AUTO] $ %s\n", cmd.c_str());
    fflush(stdout);
}

// Виконати команду, повернути код + вивід. timeout_ms поки ігнорується (для PoC).
inline ExecResult Exec(const std::string& cmd, bool silent = false) {
    if (!silent) LogCmd(cmd);
#ifdef _WIN32
    FILE* pipe = _popen((cmd + " 2>&1").c_str(), "r");
#else
    FILE* pipe = popen((cmd + " 2>&1").c_str(), "r");
#endif
    ExecResult r;
    if (!pipe) {
        r.exit_code = -1;
        r.output = "<popen failed>";
        return r;
    }
    std::array<char, 4096> buf{};
    std::string out;
    while (fgets(buf.data(), (int)buf.size(), pipe)) {
        out += buf.data();
    }
#ifdef _WIN32
    r.exit_code = _pclose(pipe);
#else
    r.exit_code = pclose(pipe);
#endif
    r.output = out;
    if (!silent && !out.empty()) {
        // Показуємо перші ~2000 символів, щоб лог не роздувало
        printf("[AUTO]   | %s%s", out.substr(0, 2000).c_str(), out.size() > 2000 ? "...\n" : "");
        fflush(stdout);
    }
    return r;
}

// Запуск без очікування (detached), для scrcpy / adb daemon
inline bool LaunchDetached(const std::string& cmd) {
    LogCmd(cmd + "  (& detached)");
#ifdef _WIN32
    // "start /min" щоб не блокувати консоль
    std::string full = "start \"\" /min " + cmd;
    int rc = system(full.c_str());
    return rc == 0;
#else
    std::string full = cmd + " &";
    int rc = system(full.c_str());
    return rc == 0;
#endif
}

inline bool CommandExists(const std::string& tool) {
#ifdef _WIN32
    ExecResult r = Exec("where " + tool, true);
    return r.ok() && !r.output.empty();
#else
    ExecResult r = Exec("which " + tool, true);
    return r.ok() && !r.output.empty();
#endif
}

inline bool IsAdmin() {
#ifdef _WIN32
    BOOL is_admin = FALSE;
    PSID admin_group = nullptr;
    SID_IDENTIFIER_AUTHORITY nt_auth = SECURITY_NT_AUTHORITY;
    if (AllocateAndInitializeSid(&nt_auth, 2, SECURITY_BUILTIN_DOMAIN_RID,
                                 DOMAIN_ALIAS_RID_ADMINS, 0, 0, 0, 0, 0, 0, &admin_group)) {
        CheckTokenMembership(nullptr, admin_group, &is_admin);
        FreeSid(admin_group);
    }
    return is_admin == TRUE;
#else
    return geteuid() == 0;
#endif
}

// ============================================================================
// ADB автоматизація
// ============================================================================

struct AdbDevice {
    std::string serial;     // напр. "192.168.1.100:5555" або "R5CRxxx"
    std::string state;      // device / offline / unauthorized
    std::string transport;  // usb / tcp / unknown (парсимо з -l)
    bool is_tcp() const { return serial.find(':') != std::string::npos; }
};

inline std::vector<AdbDevice> AdbDevices() {
    std::vector<AdbDevice> out;
    ExecResult r = Exec("adb devices -l", true);
    if (!r.ok()) return out;
    // Формат:
    // List of devices attached
    // R5CR11XXXX	device product:... model:... transport:usb
    // 192.168.1.100:5555	device product:... transport:tcp
    char* ctx = nullptr;
#ifdef _WIN32
    // strtok_s на Windows
    std::string copy = r.output;
    // Розбиваємо по рядках вручну щоб не залежати від strtok
    size_t pos = 0;
    std::vector<std::string> lines;
    while (pos < copy.size()) {
        size_t nl = copy.find('\n', pos);
        std::string line = copy.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        lines.push_back(line);
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
#else
    std::vector<std::string> lines;
    size_t pos = 0;
    while (pos < r.output.size()) {
        size_t nl = r.output.find('\n', pos);
        lines.push_back(r.output.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos));
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }
#endif
    (void)ctx;
    for (auto& line : lines) {
        // Пропускаємо заголовок і порожні
        if (line.find("List of devices") != std::string::npos) continue;
        if (line.empty()) continue;
        // Шукаємо таб або пробіли: "<serial>\t<state>"
        size_t tab = line.find('\t');
        if (tab == std::string::npos) tab = line.find(' ');
        if (tab == std::string::npos) continue;
        std::string serial = line.substr(0, tab);
        // trim
        while (!serial.empty() && (serial.back() == ' ' || serial.back() == '\r' || serial.back() == '\t')) serial.pop_back();
        while (!serial.empty() && (serial.front() == ' ' || serial.front() == '\t')) serial.erase(serial.begin());
        if (serial.empty()) continue;
        std::string rest = line.substr(tab + 1);
        std::string state = "unknown";
        if (rest.find("device") != std::string::npos && rest.find("offline") == std::string::npos) state = "device";
        else if (rest.find("offline") != std::string::npos) state = "offline";
        else if (rest.find("unauthorized") != std::string::npos) state = "unauthorized";
        // Серійник без ":" і без поля transport: — це фізичний USB
        // (старі adb не пишуть transport: взагалі).
        std::string transport = "unknown";
        auto tp = rest.find("transport:");
        if (tp != std::string::npos) {
            size_t s = tp + 10;
            size_t e = rest.find(' ', s);
            transport = rest.substr(s, e == std::string::npos ? std::string::npos : e - s);
            while (!transport.empty() && (transport.back() == '\r' || transport.back() == ' ')) transport.pop_back();
        } else if (serial.find(':') != std::string::npos) {
            transport = "tcp";
        } else {
            transport = "usb";
        }
        out.push_back({serial, state, transport});
    }
    return out;
}

inline void PrintAdbDevices() {
    auto devs = AdbDevices();
    printf("[AUTO] adb devices: %zu found\n", devs.size());
    for (auto& d : devs) {
        printf("[AUTO]   - %s  state=%s transport=%s\n", d.serial.c_str(), d.state.c_str(), d.transport.c_str());
    }
    if (devs.empty()) {
        printf("[AUTO]   (порожньо — телефон не підключено по USB і не законекчено по Wi-Fi)\n");
    }
}

// adb start-server (завжди безпечно викликати)
inline bool EnsureAdbServer() {
    if (!CommandExists("adb")) {
        printf("[AUTO] ERROR: 'adb' не знайдено в PATH.\n");
        printf("[AUTO] Встановіть: winget install Google.PlatformTools\n");
        return false;
    }
    ExecResult r = Exec("adb start-server");
    return r.ok();
}

// adb tcpip <port> — перемикає USB-підключений телефон в TCP режим.
// УВАГА: після цієї команди USB-лінк вмирає (adbd рестартиться), тому IP
// телефону треба визначити ЗАРАЗ — до виклику (див. AutoSetupPhoneWifi).
inline bool AdbTcpip(int port = 5555) {
    ExecResult r = Exec("adb tcpip " + std::to_string(port));
    if (!r.ok()) {
        printf("[AUTO] adb tcpip не вдався. Можливо телефон не підключено по USB або не підтверджено RSA.\n");
        return false;
    }
    // adbd рестартиться 3-5с — чекаємо довше (2с замало, connect висів до таймауту)
    printf("[AUTO] Телефон переведено в tcpip :%d. Чекаю 4с поки adbd перезапуститься...\n", port);
    std::this_thread::sleep_for(std::chrono::seconds(4));
    return true;
}

inline bool IsUsableLanIp(const std::string& ip) {
    if (ip.compare(0, 4, "127.") == 0) return false;
    if (ip.compare(0, 8, "169.254.") == 0) return false;
    return ip.find('.') != std::string::npos;
}

// Зібрати ВСІ кандидати на IP (спочатку wlan0, потім src з route).
// Викликати ДО 'adb tcpip', поки USB живий!
inline std::vector<std::string> DetectPhoneIps() {
    std::vector<std::string> out;
    auto push = [&](const std::string& ip) {
        if (!IsUsableLanIp(ip)) return;
        for (auto& e : out) if (e == ip) return;
        out.push_back(ip);
    };
    // Спосіб 1: всі inet з wlan0 (найнадійніше — реальний Wi-Fi адрес)
    {
        ExecResult r = Exec("adb shell ip addr show wlan0", true);
        size_t pos = 0;
        while ((pos = r.output.find("inet ", pos)) != std::string::npos) {
            size_t s = pos + 5;
            size_t e = r.output.find('/', s);
            std::string ip = r.output.substr(s, e == std::string::npos ? std::string::npos : e - s);
            while (!ip.empty() && (ip.back() == '\r' || ip.back() == '\n' || ip.back() == ' ')) ip.pop_back();
            push(ip);
            pos = s;
        }
    }
    // Спосіб 2: всі src з ip route
    {
        ExecResult r = Exec("adb shell ip route", true);
        size_t pos = 0;
        while ((pos = r.output.find("src ", pos)) != std::string::npos) {
            size_t s = pos + 4;
            size_t e = r.output.find_first_of(" \r\n", s);
            std::string ip = r.output.substr(s, e == std::string::npos ? std::string::npos : e - s);
            push(ip);
            pos = s;
        }
    }
    if (!out.empty()) {
        printf("[AUTO] Кандидати на IP телефону:");
        for (auto& ip : out) printf(" %s", ip.c_str());
        printf("\n");
    }
    return out;
}

// Сумісність: перший кандидат або "".
inline std::string DetectPhoneIp() {
    auto v = DetectPhoneIps();
    return v.empty() ? "" : v[0];
}

inline bool TcpDeviceReady(const std::string& serial) {
    auto devs = AdbDevices();
    std::string want_ip = serial.substr(0, serial.find(':'));
    for (auto& d : devs) {
        if (d.serial == serial && d.state == "device") return true;
        if (d.serial.compare(0, want_ip.size(), want_ip) == 0 &&
            d.state == "device" && d.is_tcp()) return true;
    }
    return false;
}

// adb connect + ПЕРЕВІРКА що пристрій реально в стані device (з ретраями,
// бо adbd після tcpip рестартиться кілька секунд і перший connect часто падає).
inline bool AdbConnect(const std::string& ip, int port = 5555, int attempts = 3) {
    std::string serial = ip + ":" + std::to_string(port);
    for (int i = 1; i <= attempts; ++i) {
        printf("[AUTO] Спроба %d/%d: adb connect %s ...\n", i, attempts, serial.c_str());
        Exec("adb connect " + serial);
        for (int w = 0; w < 4; ++w) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
            if (TcpDeviceReady(serial)) {
                printf("[AUTO] Підключено і підтверджено: %s (device).\n", serial.c_str());
                return true;
            }
        }
    }
    printf("[AUTO] Не вдалося отримати device на %s за %d спроби.\n", serial.c_str(), attempts);
    return false;
}

inline void DiagnoseConnectFail(const std::string& ip, int port) {
    printf("\n[AUTO] ДІАГНОСТИКА: adb connect не вдався. Перевірте по пунктах:\n");
    printf("[AUTO]   1. Телефон і ПК в ОДНІЙ Wi-Fi мережі? (IP %s має бути з вашої LAN, напр. 192.168.x.x)\n", ip.c_str());
    printf("[AUTO]      Якщо IP схоже на мобільну підмережу (10.x) — увімкніть Wi-Fi на телефоні.\n");
    printf("[AUTO]   2. На телефоні: Параметри -> Для розробників -> Бездротове налагодження УВІМКНЕНО?\n");
    printf("[AUTO]   3. AP isolation в роутері вимкнено?\n");
    printf("[AUTO]   4. Брандмауер Windows не ріже порт %d? Тест: ping %s\n", port, ip.c_str());
    printf("[AUTO]   5. Або задайте IP вручну: --phone-ip <IP з Налаштування -> Про телефон -> Статус>\n\n");
}

// Повний ланцюжок: detect IP (поки USB живий!) -> tcpip -> connect з verify.
// Повертає "ip:port" або "".
inline std::string AutoSetupPhoneWifi(int port = 5555) {
    EnsureAdbServer();
    auto devs = AdbDevices();
    // Якщо вже є TCP пристрій в стані device — нічого не робимо
    for (auto& d : devs) {
        if (d.is_tcp() && d.state == "device") {
            printf("[AUTO] Вже є Wi-Fi пристрій: %s — пропускаємо tcpip.\n", d.serial.c_str());
            return d.serial;
        }
    }
    // Чи є USB пристрій?
    bool has_usb = false;
    for (auto& d : devs) {
        if (!d.is_tcp() && d.state == "device") { has_usb = true; break; }
    }
    if (!has_usb) {
        printf("[AUTO] USB-пристрій не знайдено. Пропускаємо tcpip (можливо вже в tcpip або потрібен --phone-ip).\n");
        return "";
    }
    // КРОК 1: IP — ДО tcpip, поки USB живий (після рестарту adbd 'adb shell' вже не працює
    // і можна схопити сміття на кшталт шлюзу мобільної підмережі).
    printf("[AUTO] Визначаю IP телефону ДО перезапуску adbd (поки USB живий)...\n");
    auto candidates = DetectPhoneIps();
    printf("[AUTO] Знайдено USB-пристрій, перемикаємо в Wi-Fi (tcpip %d)...\n", port);
    if (!AdbTcpip(port)) return "";
    if (candidates.empty()) {
        printf("[AUTO] До tcpip IP не визначився — пробую ще раз (може не вийти, USB вже мертвий)...\n");
        candidates = DetectPhoneIps();
    }
    if (candidates.empty()) {
        printf("[AUTO] Не вдалося визначити IP. Введіть вручну або передайте --phone-ip.\n");
        return "";
    }
    // КРОК 2: пробуємо КОЖНОГО кандидата з перевіркою
    for (auto& ip : candidates) {
        if (AdbConnect(ip, port)) return ip + ":" + std::to_string(port);
    }
    DiagnoseConnectFail(candidates[0], port);
    return "";
}

// ============================================================================
// VHCI / драйвер
// ============================================================================

inline bool CheckVhciPresent() {
#ifdef _WIN32
    // Спосіб 1: спроба відкрити пристрій
    HANDLE h = CreateFileW(L"\\\\.\\vhci", GENERIC_READ | GENERIC_WRITE,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                           OPEN_EXISTING, 0, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
        CloseHandle(h);
        printf("[AUTO] VHCI драйвер знайдено (\\\\.\\vhci).\n");
        return true;
    }
    HANDLE h2 = CreateFileW(L"\\\\.\\USBIP_VHCI", GENERIC_READ | GENERIC_WRITE,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                            OPEN_EXISTING, 0, nullptr);
    if (h2 != INVALID_HANDLE_VALUE) {
        CloseHandle(h2);
        printf("[AUTO] VHCI драйвер знайдено (\\\\.\\USBIP_VHCI).\n");
        return true;
    }
    printf("[AUTO] VHCI драйвер НЕ знайдено. Працюємо в SIMULATION режимі.\n");
    printf("[AUTO] Для справжнього USB: встановіть usbip-win2 (див. scripts/setup_windows.ps1) і запустіть з --vhci\n");
    return false;
#else
    ExecResult r = Exec("ls /dev/vhci 2>&1", true);
    return r.ok();
#endif
}

inline void CheckTestSigning() {
#ifdef _WIN32
    ExecResult r = Exec("bcdedit /enum {current} | findstr testsigning", true);
    if (r.output.find("Yes") != std::string::npos || r.output.find("testsigning          Yes") != std::string::npos) {
        printf("[AUTO] Test Signing: ON.\n");
    } else {
        printf("[AUTO] Test Signing: OFF або невідомо (потрібно для usbip-win2 без EV-сертифіката).\n");
        printf("[AUTO] Увімкнути (потрібні права адміна): bcdedit /set testsigning on  + перезавантаження\n");
    }
#endif
}

// ============================================================================
// scrcpy
// ============================================================================

inline bool CheckScrcpy() {
    if (!CommandExists("scrcpy")) {
        printf("[AUTO] 'scrcpy' не знайдено в PATH. Встановіть: winget install Genymobile.scrcpy\n");
        return false;
    }
    Exec("scrcpy --version", true);
    return true;
}

// Запустити scrcpy автоматично (після того як пристрій готовий)
inline bool LaunchScrcpy(const std::string& extra_args = "--select-usb") {
    if (!CheckScrcpy()) return false;
    std::string cmd = "scrcpy " + extra_args;
    printf("[AUTO] Запускаємо scrcpy автоматично...\n");
    return LaunchDetached(cmd);
}

// ============================================================================
// Збірка
// ============================================================================

inline bool CheckCmake() {
    if (!CommandExists("cmake")) {
        printf("[AUTO] 'cmake' не знайдено. Встановіть: winget install Kitware.CMake\n");
        return false;
    }
    return true;
}

} // namespace autosetup
