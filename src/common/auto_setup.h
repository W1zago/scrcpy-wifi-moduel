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
#include <mutex>
#include <atomic>
#include <sstream>
#include <algorithm>
#include <cctype>

#ifdef _WIN32
  #ifndef NOMINMAX
  #define NOMINMAX
  #endif
  #ifndef WIN32_LEAN_AND_MEAN
  #define WIN32_LEAN_AND_MEAN
  #endif
  #include <winsock2.h>
  #include <ws2tcpip.h>
  #pragma comment(lib, "ws2_32.lib")
  #include <windows.h>
  #include <io.h>
#else
  #include <unistd.h>
  #include <fcntl.h>
  #include <sys/socket.h>
  #include <netinet/in.h>
  #include <arpa/inet.h>
  #include <netdb.h>
  #include <sys/select.h>
  #include <errno.h>
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

// ============================================================================
// Сканування LAN: всі доступні IP + назва пристрою + інтерактивний вибір
// ============================================================================

struct LanDevice {
    std::string ip;
    std::string serial;   // "ip:port" (реальний, як показав adb)
    std::string name;     // "Samsung SM-A525F (Android 13)"
    std::string state;    // device / unauthorized / offline / ""
};

inline std::string TrimStr(std::string s) {
    while (!s.empty() && (s.back() == '\r' || s.back() == '\n' || s.back() == ' ' || s.back() == '\t')) s.pop_back();
    size_t p = 0;
    while (p < s.size() && (s[p] == ' ' || s[p] == '\t')) ++p;
    if (p) s.erase(0, p);
    return s;
}

inline uint32_t IpStrToU32(const std::string& ip) {
    unsigned a = 0, b = 0, c = 0, d = 0;
    if (sscanf(ip.c_str(), "%u.%u.%u.%u", &a, &b, &c, &d) != 4) return 0;
    return (uint32_t)((a << 24) | (b << 16) | (c << 8) | d);
}

inline std::string U32ToIpStr(uint32_t v) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%u.%u.%u.%u",
             (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF);
    return std::string(buf);
}

inline int PrefixFromMaskStr(const std::string& mask) {
    uint32_t m = IpStrToU32(mask);
    int bits = 0;
    while (m & 0x80000000u) { ++bits; m <<= 1; }
    return bits == 0 ? 24 : bits;
}

// Витягнути "X.X.X.X" після ':' в рядку ipconfig. Повертає "" якщо нема.
inline std::string ExtractIpv4AfterColon(const std::string& line) {
    size_t colon = line.find(':');
    if (colon == std::string::npos) return "";
    std::string tail = line.substr(colon + 1);
    int a, b, c, d;
    // шукаємо першу четвірку чисел з крапками
    for (size_t i = 0; i < tail.size(); ++i) {
        if (sscanf(tail.c_str() + i, "%d.%d.%d.%d", &a, &b, &c, &d) == 4) {
            if (a >= 0 && a <= 255 && b >= 0 && b <= 255 && c >= 0 && c <= 255 && d >= 0 && d <= 255) {
                char buf[32];
                snprintf(buf, sizeof(buf), "%d.%d.%d.%d", a, b, c, d);
                return std::string(buf);
            }
        }
    }
    return "";
}

inline bool IsUsableScanIp(const std::string& ip) {
    if (ip.empty()) return false;
    if (ip.compare(0, 4, "127.") == 0) return false;
    if (ip.compare(0, 8, "169.254.") == 0) return false;
    if (ip == "0.0.0.0") return false;
    return true;
}

// Список IP для сканування (до ~254 на підмережу; великі мережі ріжемо до /24).
inline std::vector<std::string> GetLanScanTargets() {
    std::vector<std::pair<std::string, int> > ifs; // (ip, prefix)
    // ipconfig мовно-незалежно: IP-рядок містить "IPv4" (є і в EN, і в RU/UA "IPv4-адрес"),
    // а рядок маски — адресу 255.x. Мітки локалізовані і йдуть в OEM-кодуванні,
    // тому на текст міток не покладаємось. DNS/шлюзи без мітки IPv4 ігноруємо.
    ExecResult rc = Exec("ipconfig", true);
    if (!rc.output.empty()) {
        std::istringstream ss(rc.output);
        std::string line, cur_ip;
        while (std::getline(ss, line)) {
            std::string low = line;
            std::transform(low.begin(), low.end(), low.begin(), ::tolower);
            bool has_mask = (line.find("255.") != std::string::npos);
            bool is_ip_line = (low.find("ipv4") != std::string::npos);
            if (has_mask && !cur_ip.empty()) {
                std::string mask = ExtractIpv4AfterColon(line);
                // З огляду на has_mask маска точно є; якщо не розпарсилась — /24
                size_t p = line.find("255.");
                std::string m = (p != std::string::npos) ? line.substr(p, 15) : mask;
                // Обрізаємо до 4 октетів
                int a, b, c, d;
                int prefix = 24;
                if (sscanf(m.c_str(), "%d.%d.%d.%d", &a, &b, &c, &d) == 4) {
                    char mb[32];
                    snprintf(mb, sizeof(mb), "%d.%d.%d.%d", a, b, c, d);
                    prefix = PrefixFromMaskStr(mb);
                }
                ifs.push_back(std::make_pair(cur_ip, prefix));
                cur_ip.clear();
            } else if (is_ip_line) {
                std::string ip = ExtractIpv4AfterColon(line);
                if (IsUsableScanIp(ip)) cur_ip = ip;
                else cur_ip.clear();
            }
        }
        // Маски не знайшли (рідко): лишається cur_ip без пари — додаємо як /24
        if (!cur_ip.empty()) ifs.push_back(std::make_pair(cur_ip, 24));
    }
#ifndef _WIN32
    if (ifs.empty()) {
        ExecResult r2 = Exec("ip -4 addr show", true);
        if (r2.output.empty()) r2 = Exec("ifconfig", true);
        // шукаємо "inet 192.168.x.x/24" або "inet addr:.. Mask:.."
        const std::string& out = r2.output;
        size_t pos = 0;
        while ((pos = out.find("inet", pos)) != std::string::npos) {
            size_t s = pos + 4;
            int a, b, c, d, p = 24;
            if (sscanf(out.c_str() + s, " addr:%d.%d.%d.%d", &a, &b, &c, &d) == 4 ||
                sscanf(out.c_str() + s, " %d.%d.%d.%d/%d", &a, &b, &c, &d, &p) >= 4) {
                char buf[32];
                snprintf(buf, sizeof(buf), "%d.%d.%d.%d", a, b, c, d);
                std::string ip = buf;
                if (IsUsableScanIp(ip)) {
                    int prefix = p;
                    size_t mpos = out.find("Mask", pos);
                    size_t npos = out.find("inet", pos + 4);
                    if (mpos != std::string::npos && (npos == std::string::npos || mpos < npos)) {
                        int ma, mb, mc, md;
                        if (sscanf(out.c_str() + mpos, "Mask:%d.%d.%d.%d", &ma, &mb, &mc, &md) == 4 ||
                            sscanf(out.c_str() + mpos, "ask %d.%d.%d.%d", &ma, &mb, &mc, &md) == 4) {
                            char mb2[32];
                            snprintf(mb2, sizeof(mb2), "%d.%d.%d.%d", ma, mb, mc, md);
                            prefix = PrefixFromMaskStr(mb2);
                        }
                    }
                    ifs.push_back(std::make_pair(ip, prefix));
                }
            }
            pos += 4;
        }
    }
#endif
    std::vector<std::string> targets;
    std::vector<std::string> seen_nets;
    for (size_t i = 0; i < ifs.size(); ++i) {
        uint32_t ip = IpStrToU32(ifs[i].first);
        if (!ip) continue;
        int prefix = ifs[i].second;
        if (prefix < 24) prefix = 24; // великі мережі — тільки /24 навколо себе
        if (prefix > 30) prefix = 24;
        if (prefix < 0 || prefix > 32) prefix = 24;
        uint32_t mask = prefix == 0 ? 0 : (0xFFFFFFFFu << (32 - prefix));
        uint32_t net = ip & mask;
        uint32_t count = (prefix >= 31) ? 2 : ((1u << (32 - prefix)) - 2);
        if (count > 1024) count = 1024;
        std::string netkey = U32ToIpStr(net) + "/" + std::to_string(prefix);
        if (std::find(seen_nets.begin(), seen_nets.end(), netkey) != seen_nets.end()) continue;
        seen_nets.push_back(netkey);
        for (uint32_t h = 1; h <= count; ++h) {
            uint32_t cand = net + h;
            if (cand == ip) continue; // себе пропускаємо
            targets.push_back(U32ToIpStr(cand));
        }
    }
    std::sort(targets.begin(), targets.end());
    targets.erase(std::unique(targets.begin(), targets.end()), targets.end());
    return targets;
}

inline void EnsureWsa() {
#ifdef _WIN32
    static bool inited = false;
    static std::mutex m;
    std::lock_guard<std::mutex> lk(m);
    if (!inited) {
        WSADATA wsa;
        WSAStartup(MAKEWORD(2, 2), &wsa);
        inited = true;
    }
#endif
}

inline bool TcpPortOpen(const std::string& ip, int port, int timeout_ms = 600) {
#ifdef _WIN32
    EnsureWsa();
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return false;
    u_long nb = 1;
    ioctlsocket(s, FIONBIO, &nb);
    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)port);
    inet_pton(AF_INET, ip.c_str(), &addr.sin_addr);
    int rc = ::connect(s, (sockaddr*)&addr, sizeof(addr));
    bool open = false;
    if (rc == 0) {
        open = true;
    } else if (WSAGetLastError() == WSAEWOULDBLOCK) {
        fd_set ws;
        FD_ZERO(&ws);
        FD_SET(s, &ws);
        timeval tv;
        tv.tv_sec = timeout_ms / 1000;
        tv.tv_usec = (timeout_ms % 1000) * 1000;
        int r = select(0, nullptr, &ws, nullptr, &tv);
        if (r > 0) {
            int err = 0;
            int len = sizeof(err);
            getsockopt(s, SOL_SOCKET, SO_ERROR, (char*)&err, &len);
            open = (err == 0);
        }
    }
    closesocket(s);
    return open;
#else
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return false;
    int flags = fcntl(s, F_GETFL, 0);
    fcntl(s, F_SETFL, flags | O_NONBLOCK);
    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    inet_pton(AF_INET, ip.c_str(), &addr.sin_addr);
    int rc = ::connect(s, (sockaddr*)&addr, sizeof(addr));
    bool open = false;
    if (rc == 0) open = true;
    else if (errno == EINPROGRESS) {
        fd_set ws;
        FD_ZERO(&ws);
        FD_SET(s, &ws);
        timeval tv;
        tv.tv_sec = timeout_ms / 1000;
        tv.tv_usec = (timeout_ms % 1000) * 1000;
        int r = select(s + 1, nullptr, &ws, nullptr, &tv);
        if (r > 0) {
            int err = 0;
            socklen_t len = sizeof(err);
            getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &len);
            open = (err == 0);
        }
    }
    ::close(s);
    return open;
#endif
}

inline std::vector<std::string> ScanLanForAdb(int port = 5555, int timeout_ms = 600) {
    std::vector<std::string> targets = GetLanScanTargets();
    std::vector<std::string> open;
    if (targets.empty()) {
        printf("[AUTO] LAN-сканування: не вдалося визначити локальну підмережу.\n");
        return open;
    }
    printf("[AUTO] Сканую мережу (%zu адрес, порт %d)... це займе кілька секунд.\n",
           targets.size(), port);
    std::mutex m;
    std::atomic<size_t> idx(0);
    int nthreads = (int)std::min<size_t>(64, targets.size());
    std::vector<std::thread> th;
    for (int t = 0; t < nthreads; ++t) {
        th.push_back(std::thread([&]() {
            while (true) {
                size_t i = idx.fetch_add(1);
                if (i >= targets.size()) break;
                if (TcpPortOpen(targets[i], port, timeout_ms)) {
                    std::lock_guard<std::mutex> lk(m);
                    open.push_back(targets[i]);
                }
            }
        }));
    }
    for (size_t i = 0; i < th.size(); ++i) th[i].join();
    std::sort(open.begin(), open.end(), [](const std::string& a, const std::string& b) {
        return IpStrToU32(a) < IpStrToU32(b);
    });
    if (!open.empty()) {
        printf("[AUTO] Відкритий порт %d знайдено на:", port);
        for (size_t i = 0; i < open.size(); ++i) printf(" %s", open[i].c_str());
        printf("\n");
    } else {
        printf("[AUTO] Нічого з відкритим портом %d не знайдено.\n", port);
        printf("[AUTO] Підказка: увімкніть Бездротове налагодження або виконайте 'adb tcpip 5555' по USB.\n");
    }
    return open;
}

inline std::string AdbShellOn(const std::string& serial, const std::string& shell_args) {
    ExecResult r = Exec("adb -s " + serial + " " + shell_args, true);
    std::string out = TrimStr(r.output);
    if (out.find("no devices") != std::string::npos ||
        out.find("device not found") != std::string::npos ||
        out.find("device offline") != std::string::npos) return "";
    if (r.exit_code != 0 && out.empty()) return "";
    // getprop повертає один рядок; прибираємо зайве
    size_t nl = out.find('\n');
    if (nl != std::string::npos) out = TrimStr(out.substr(0, nl));
    return out;
}

inline LanDevice GetLanDeviceInfo(const std::string& ip, int port = 5555) {
    LanDevice d;
    d.ip = ip;
    d.serial = ip + ":" + std::to_string(port);
    Exec("adb connect " + d.serial, true);
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    // Уточнюємо стан і реальний серійник
    auto devs = AdbDevices();
    std::string want = ip;
    for (size_t i = 0; i < devs.size(); ++i) {
        std::string cur_ip = devs[i].serial.substr(0, devs[i].serial.find(':'));
        if (devs[i].serial == d.serial || (cur_ip == want && devs[i].is_tcp())) {
            d.state = devs[i].state;
            d.serial = devs[i].serial;
            break;
        }
    }
    std::string manuf, model, dev, android, devname;
    if (d.state == "device") {
        manuf = AdbShellOn(d.serial, "shell getprop ro.product.manufacturer");
        model = AdbShellOn(d.serial, "shell getprop ro.product.model");
        dev = AdbShellOn(d.serial, "shell getprop ro.product.device");
        android = AdbShellOn(d.serial, "shell getprop ro.build.version.release");
        devname = AdbShellOn(d.serial, "shell settings get global device_name");
    }
    if (!manuf.empty() || !model.empty()) d.name = TrimStr(manuf + " " + model);
    else if (!devname.empty() && devname != "null") d.name = devname;
    else if (!dev.empty()) d.name = dev;
    else d.name = u8"невідома модель";
    if (!android.empty()) d.name += " (Android " + android + ")";
    if (d.state == "unauthorized") d.name += u8" [потрібно підтвердити RSA на екрані]";
    else if (d.state == "offline") d.name += " [offline]";
    else if (d.state.empty()) d.name += u8" [не відповідає на ADB — можливо не телефон]";
    return d;
}

inline std::vector<LanDevice> ScanLanWithNames(int port = 5555, int timeout_ms = 600) {
    std::vector<LanDevice> out;
    std::vector<std::string> ips = ScanLanForAdb(port, timeout_ms);
    for (size_t i = 0; i < ips.size(); ++i) {
        LanDevice d = GetLanDeviceInfo(ips[i], port);
        printf("[AUTO]   - %s — %s\n", d.serial.c_str(), d.name.c_str());
        out.push_back(d);
    }
    return out;
}

inline void AdbDisconnectOthers(const std::vector<LanDevice>& devs, const std::string& keep_ip) {
    for (size_t i = 0; i < devs.size(); ++i) {
        if (devs[i].ip != keep_ip) Exec("adb disconnect " + devs[i].serial, true);
    }
}

inline bool StdinInteractive() {
#ifdef _WIN32
    return _isatty(_fileno(stdin)) != 0;
#else
    return isatty(STDIN_FILENO) != 0;
#endif
}

// Показати "IP — назва" і попросити вибрати. Повертає IP або "".
inline std::string InteractivePickDevice(const std::vector<LanDevice>& devs, const std::string& pick = "") {
    if (devs.empty()) return "";
    printf("\n[AUTO] Знайдено пристроїв у мережі: %zu. До якого приєднатись?\n", devs.size());
    for (size_t i = 0; i < devs.size(); ++i) {
        printf("[AUTO]   %zu. %s — %s\n", i + 1, devs[i].serial.c_str(), devs[i].name.c_str());
    }
    printf("[AUTO]   0. Пропустити (демо без телефону)\n\n");
    if (!pick.empty()) {
        std::string p = TrimStr(pick);
        std::string chosen;
        bool numeric = !p.empty() && p.find_first_not_of("0123456789") == std::string::npos;
        if (numeric) {
            int idx = atoi(p.c_str());
            if (idx >= 1 && idx <= (int)devs.size()) chosen = devs[idx - 1].ip;
        } else {
            for (size_t i = 0; i < devs.size(); ++i) {
                if (devs[i].ip == p || devs[i].serial == p ||
                    devs[i].serial.find(p) != std::string::npos) { chosen = devs[i].ip; break; }
            }
        }
        if (!chosen.empty()) {
            printf("[AUTO] Автовибір (--pick %s): %s.\n", p.c_str(), chosen.c_str());
            AdbDisconnectOthers(devs, chosen);
            return chosen;
        }
        printf("[AUTO] --pick=%s ні з чим не збігся, питаю вручну...\n", p.c_str());
    }
    if (!StdinInteractive()) {
        if (devs.size() == 1) {
            printf("[AUTO] Неінтерактивний режим — беру єдиний пристрій %s автоматично.\n", devs[0].ip.c_str());
            return devs[0].ip;
        }
        for (size_t i = 0; i < devs.size(); ++i) {
            if (devs[i].state == "device") {
                AdbDisconnectOthers(devs, devs[i].ip);
                return devs[i].ip;
            }
        }
        return "";
    }
    printf("Введіть номер (1-%zu), IP, або 0/Enter щоб пропустити: ", devs.size());
    fflush(stdout);
    char buf[256] = {0};
    if (!fgets(buf, sizeof(buf), stdin)) {
        printf("\n[AUTO] Вибір скасовано.\n");
        AdbDisconnectOthers(devs, "");
        return "";
    }
    std::string ans = TrimStr(buf);
    if (ans.empty() || ans == "0") {
        printf("[AUTO] Пропущено за вибором користувача.\n");
        AdbDisconnectOthers(devs, "");
        return "";
    }
    bool numeric = ans.find_first_not_of("0123456789") == std::string::npos;
    if (numeric) {
        int idx = atoi(ans.c_str());
        if (idx >= 1 && idx <= (int)devs.size()) {
            printf("[AUTO] Вибрано: %s — %s.\n", devs[idx - 1].serial.c_str(), devs[idx - 1].name.c_str());
            AdbDisconnectOthers(devs, devs[idx - 1].ip);
            return devs[idx - 1].ip;
        }
        AdbDisconnectOthers(devs, "");
        return "";
    }
    for (size_t i = 0; i < devs.size(); ++i) {
        if (devs[i].ip == ans || devs[i].serial == ans) {
            printf("[AUTO] Вибрано: %s — %s.\n", devs[i].serial.c_str(), devs[i].name.c_str());
            AdbDisconnectOthers(devs, devs[i].ip);
            return devs[i].ip;
        }
    }
    // Дозволяємо IP вручну навіть якщо сканер його не бачив
    int a, b, c, d;
    if (sscanf(ans.c_str(), "%d.%d.%d.%d", &a, &b, &c, &d) == 4) {
        printf("[AUTO] Введено IP вручну: %s (його не було в скані, спробую підключитись).\n", ans.c_str());
        AdbDisconnectOthers(devs, "");
        return ans;
    }
    AdbDisconnectOthers(devs, "");
    return "";
}

// ============================================================================
// Підключення БЕЗ кабелю (Android 11+: Бездротове налагодження, adb pair)
// Кабель потрібен лише для старої схеми 'adb tcpip 5555'. Тут користувач вмикає
// Бездротове налагодження на телефоні і вводить IP/порти/код з екрану —
// програма сама робить 'adb pair' + 'adb connect' на динамічний порт.
// ============================================================================

// Виконати команду, подавши рядок на stdin (для 'adb pair', що питає код).
inline int ExecWithInput(const std::string& cmd, const std::string& input) {
    LogCmd(cmd + "  (< код парування через stdin)");
#ifdef _WIN32
    FILE* pipe = _popen(cmd.c_str(), "w");
#else
    FILE* pipe = popen(cmd.c_str(), "w");
#endif
    if (!pipe) return -1;
    fwrite(input.c_str(), 1, input.size(), pipe);
    fwrite("\n", 1, 1, pipe);
#ifdef _WIN32
    int rc = _pclose(pipe);
#else
    int rc = pclose(pipe);
#endif
    return rc;
}

inline bool WirelessPair(const std::string& ip, int pair_port, const std::string& code) {
    std::string serial = ip + ":" + std::to_string(pair_port);
    printf("[AUTO] adb pair %s (код з екрану телефону)...\n", serial.c_str());
    int rc = ExecWithInput("adb pair " + serial, code);
    if (rc == 0) {
        printf("[AUTO] Парування вдалося (код прийнято).\n");
        return true;
    }
    printf("[AUTO] Парування НЕ вдалося (rc=%d). Код одноразовий — відкрийте екран заново.\n", rc);
    return false;
}

// Інтерактивне парування без кабелю: питає "IP pair_port code connect_port".
// Повертає "ip:connect_port" або "". Код з --pair-code підставляється замість вводу.
inline std::string TryWirelessPairingInput(const std::string& pair_code = "") {
    if (!StdinInteractive() && pair_code.empty()) return "";
    printf("\n[AUTO] БЕЗ КАБЕЛЮ (Android 11+): на телефоні увімкніть\n");
    printf("[AUTO]   Параметри -> Для розробників -> Бездротове налагодження,\n");
    printf("[AUTO]   відкрийте 'Pair device with pairing code'.\n");
    if (pair_code.empty())
        printf("[AUTO] Введіть: IP pair_port code connect_port\n");
    else
        printf("[AUTO] Введіть: IP pair_port connect_port (код візьму з --pair-code)\n");
    printf("[AUTO] (напр.: 192.168.1.50 39511 482906 37123; порожньо = пропустити): ");
    fflush(stdout);
    char buf[256] = {0};
    if (!fgets(buf, sizeof(buf), stdin)) return "";
    std::string line = TrimStr(buf);
    if (line.empty()) {
        printf("[AUTO] Пропущено.\n");
        return "";
    }
    std::istringstream ss(line);
    std::string ip, s_pair, t3, t4;
    ss >> ip >> s_pair >> t3 >> t4;
    std::string code, s_conn;
    if (!t4.empty()) { code = t3; s_conn = t4; }                    // 4 токени: IP pair code conn
    else if (!pair_code.empty()) { code = pair_code; s_conn = t3; } // 3 токени + --pair-code
    else if (!t3.empty()) {
        // 3 токени без прапорця: IP pair_port connect_port — код запитаємо окремо
        s_conn = t3;
        printf("[AUTO] Введіть 6-значний код з екрану телефону: ");
        fflush(stdout);
        char cb[64] = {0};
        if (fgets(cb, sizeof(cb), stdin)) code = TrimStr(cb);
    }
    int a, b, c, d;
    if (sscanf(ip.c_str(), "%d.%d.%d.%d", &a, &b, &c, &d) != 4) {
        printf("[AUTO] Схоже не на IP — пропускаю.\n");
        return "";
    }
    int pair_port = atoi(s_pair.c_str());
    int conn_port = atoi(s_conn.c_str());
    if (pair_port > 0 && !code.empty()) {
        if (!WirelessPair(ip, pair_port, code)) return "";
    } else if (pair_port > 0) {
        printf("[AUTO] Коду нема — пропускаю парування, пробую одразу підключитись.\n");
    }
    if (conn_port <= 0) {
        printf("[AUTO] Без порту підключення ('IP address & Port') ніяк — пропускаю.\n");
        return "";
    }
    if (AdbConnect(ip, conn_port)) return ip + ":" + std::to_string(conn_port);
    DiagnoseConnectFail(ip, conn_port);
    return "";
}

// Скан LAN + вибір + connect з перевіркою. Повертає "ip:port" або "".
inline std::string TryLanScanPick(int port = 5555, int timeout_ms = 600, const std::string& pick = "") {
    std::vector<LanDevice> infos = ScanLanWithNames(port, timeout_ms);
    if (infos.empty()) return "";
    std::string chosen = InteractivePickDevice(infos, pick);
    if (chosen.empty()) return "";
    if (AdbConnect(chosen, port)) return chosen + ":" + std::to_string(port);
    DiagnoseConnectFail(chosen, port);
    return "";
}

// Повний ланцюжок: USB->tcpip->connect, БЕЗ кабелю (adb pair), скан LAN.
// Повертає "ip:port" або "".
inline std::string AutoSetupPhoneWifi(int port = 5555, bool allow_scan = true,
                                      bool force_scan = false, const std::string& pick = "",
                                      int scan_timeout_ms = 600,
                                      const std::string& pair_code = "") {
    EnsureAdbServer();
    auto devs = AdbDevices();
    // Якщо вже є TCP пристрій в стані device — нічого не робимо (крім --scan)
    std::string already;
    for (size_t i = 0; i < devs.size(); ++i) {
        if (devs[i].is_tcp() && devs[i].state == "device") { already = devs[i].serial; break; }
    }
    if (!already.empty() && !force_scan) {
        printf("[AUTO] Вже є Wi-Fi пристрій: %s — пропускаємо tcpip.\n", already.c_str());
        return already;
    }
    if (!already.empty() && force_scan && allow_scan) {
        printf("[AUTO] Примусове сканування (--scan): вже підключено %s, але дивлюсь всі IP в мережі...\n",
               already.c_str());
        std::string r = TryLanScanPick(port, scan_timeout_ms, pick);
        if (!r.empty()) return r;
        printf("[AUTO] Залишаю вже підключений %s.\n", already.c_str());
        return already;
    }
    // Чи є USB пристрій?
    bool has_usb = false;
    for (size_t i = 0; i < devs.size(); ++i) {
        if (!devs[i].is_tcp() && devs[i].state == "device") { has_usb = true; break; }
    }
    if (!has_usb) {
        printf("[AUTO] USB-пристрій не знайдено.");
        // Кабелю нема — СПОЧАТКУ пробуємо взагалі БЕЗ кабелю (Бездротове налагодження),
        // потім класичний скан порту 5555.
        if (allow_scan) {
            printf("\n");
            std::string w = TryWirelessPairingInput(pair_code);
            if (!w.empty()) return w;
            printf("[AUTO] Дивлюсь усі доступні IP-адреси в мережі...\n");
            std::string r = TryLanScanPick(port, scan_timeout_ms, pick);
            if (!r.empty()) return r;
            printf("[AUTO] У мережі нічого придатного не вибрано.\n");
        } else {
            printf(" (мережевий пошук вимкнено --no-scan).\n");
        }
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
        printf("[AUTO] Не визначив IP через USB. Дивлюсь усі доступні IP-адреси в мережі...\n");
        if (allow_scan) {
            std::string r = TryLanScanPick(port, scan_timeout_ms, pick);
            if (!r.empty()) return r;
        }
        printf("[AUTO] Не вдалося визначити IP. Введіть вручну або передайте --phone-ip.\n");
        return "";
    }
    // КРОК 2: пробуємо КОЖНОГО кандидата з перевіркою
    for (size_t i = 0; i < candidates.size(); ++i) {
        if (AdbConnect(candidates[i], port)) return candidates[i] + ":" + std::to_string(port);
    }
    DiagnoseConnectFail(candidates[0], port);
    if (allow_scan) {
        printf("[AUTO] USB-кандидати не підійшли. Дивлюсь усі доступні IP-адреси в мережі...\n");
        std::string r = TryLanScanPick(port, scan_timeout_ms, pick);
        if (!r.empty()) return r;
    }
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
