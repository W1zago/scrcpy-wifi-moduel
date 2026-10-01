/**
 * sender_main.cpp — Agent-Sender (Android / Windows Simulator)
 * 
 * Режими:
 *  1. Android NDK (реальний): підключається до 127.0.0.1:5555 (локальний adbd)
 *     і слухає WAN порт для підключень від Receiver (Windows).
 *     Запуск: ./agent_sender --adb 127.0.0.1:5555 --listen 0.0.0.0:22777
 * 
 *  2. Windows Simulator (для тесту без телефону):
 *     Емулює adbd (фейковий ADB сервер) + Sender в одному процесі.
 *     Запуск: ./agent_sender_sim --listen 0.0.0.0:22777 --simulate-adb
 *     Це дозволяє тестувати Receiver без реального Android.
 */

#include "adb_bridge.h"
#include "../common/protocol.h"
#include "../common/auto_setup.h"
#include <cstdio>
#include <cstring>
#include <thread>
#include <atomic>
#include <vector>
#include <chrono>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#endif

static std::atomic<bool> g_running{true};

// ============================================================================
// Fake ADB Server (для Windows-симулятора, щоб тестувати без телефону)
// Відповідає на A_CNXN, A_OPEN, A_WRTE як справжній adbd
// ============================================================================

#ifdef _WIN32
void fake_adb_client_handler(SOCKET client) {
    printf("[FAKE_ADB] Client connected\n");
    // Чекаємо A_CNXN від Bridge
    AdbMessage hdr;
    int n = recv(client, (char*)&hdr, sizeof(hdr), MSG_WAITALL);
    if (n != sizeof(hdr)) {
        printf("[FAKE_ADB] No CNXN\n");
        closesocket(client);
        return;
    }
    printf("[FAKE_ADB] Got %s\n", adb_cmd_to_str(letoh32(hdr.command)));
    uint32_t data_len = letoh32(hdr.data_length);
    std::vector<uint8_t> data(data_len);
    if (data_len > 0) recv(client, (char*)data.data(), data_len, MSG_WAITALL);

    // Відповідаємо A_CNXN
    AdbMessage resp = {};
    resp.command = htole32(A_CNXN);
    resp.arg0 = htole32(ADB_VERSION);
    resp.arg1 = htole32(ADB_MAXDATA);
    const char* banner = "device::ro.product.model=VirtualPixel;ro.product.device=virtual";
    resp.data_length = htole32((uint32_t)strlen(banner));
    resp.data_checksum = htole32(adb_checksum((uint8_t*)banner, strlen(banner)));
    resp.magic = htole32(A_CNXN ^ 0xFFFFFFFFu);
    send(client, (char*)&resp, sizeof(resp), 0);
    send(client, banner, (int)strlen(banner), 0);
    printf("[FAKE_ADB] Sent A_CNXN banner\n");

    // Цикл: обробляємо A_OPEN, A_WRTE
    while (g_running) {
        n = recv(client, (char*)&hdr, sizeof(hdr), MSG_WAITALL);
        if (n <= 0) break;
        if (!adb_validate(&hdr)) {
            fprintf(stderr, "[FAKE_ADB] bad magic\n");
            break;
        }
        uint32_t cmd = letoh32(hdr.command);
        data_len = letoh32(hdr.data_length);
        data.resize(data_len);
        if (data_len > 0) {
            n = recv(client, (char*)data.data(), data_len, MSG_WAITALL);
            if (n != (int)data_len) break;
        }
        printf("[FAKE_ADB] Got %s len=%u\n", adb_cmd_to_str(cmd), data_len);

        if (cmd == A_OPEN) {
            // Відповідаємо A_OKAY
            AdbMessage okay = {};
            okay.command = htole32(A_OKAY);
            okay.arg0 = htole32(1); // local_id
            okay.arg1 = htole32(letoh32(hdr.arg0)); // remote_id = arg0 з OPEN
            okay.data_length = htole32(0);
            okay.data_checksum = htole32(0);
            okay.magic = htole32(A_OKAY ^ 0xFFFFFFFFu);
            send(client, (char*)&okay, sizeof(okay), 0);
            printf("[FAKE_ADB] Sent A_OKAY\n");

            // Симулюємо відео потік: кожні 33мс шлемо A_WRTE з фейковим H.264
            // Для тесту FEC — шлемо 10 чанків
            std::thread([client]() {
                for (int i = 0; i < 5 && g_running; ++i) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(33));
                    // Генеруємо фейковий H.264 chunk (50KB)
                    std::vector<uint8_t> fake_h264(1200, 0x42); // 1 chunk
                    for (size_t b = 0; b < fake_h264.size(); ++b) fake_h264[b] = (uint8_t)(i + b);

                    AdbMessage wrte = {};
                    wrte.command = htole32(A_WRTE);
                    wrte.arg0 = htole32(1);
                    wrte.arg1 = htole32(1);
                    wrte.data_length = htole32((uint32_t)fake_h264.size());
                    wrte.data_checksum = htole32(adb_checksum(fake_h264.data(), fake_h264.size()));
                    wrte.magic = htole32(A_WRTE ^ 0xFFFFFFFFu);
                    send(client, (char*)&wrte, sizeof(wrte), 0);
                    send(client, (char*)fake_h264.data(), (int)fake_h264.size(), 0);
                    printf("[FAKE_ADB] Sent A_WRTE video chunk %d (1200B)\n", i);

                    // Чекаємо A_OKAY від Bridge (не обов'язково для PoC)
                }
            }).detach();

        } else if (cmd == A_WRTE) {
            // Підтверджуємо A_OKAY
            AdbMessage okay = {};
            okay.command = htole32(A_OKAY);
            okay.arg0 = htole32(1);
            okay.arg1 = htole32(letoh32(hdr.arg0));
            okay.magic = htole32(A_OKAY ^ 0xFFFFFFFFu);
            send(client, (char*)&okay, sizeof(okay), 0);
        } else if (cmd == A_CLSE) {
            break;
        }
    }
    printf("[FAKE_ADB] Client disconnected\n");
    closesocket(client);
}

SOCKET start_fake_adb_server(uint16_t port) {
    WSADATA wsa;
    WSAStartup(MAKEWORD(2,2), &wsa);
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    BOOL reuse = TRUE;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, (char*)&reuse, sizeof(reuse));
    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(port);
    if (bind(s, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR) {
        fprintf(stderr, "[FAKE_ADB] bind failed: %d\n", WSAGetLastError());
        return INVALID_SOCKET;
    }
    if (listen(s, 5) == SOCKET_ERROR) {
        fprintf(stderr, "[FAKE_ADB] listen failed\n");
        return INVALID_SOCKET;
    }
    printf("[FAKE_ADB] Listening on 0.0.0.0:%u (fake adbd)\n", port);
    std::thread([s]() {
        while (g_running) {
            SOCKET c = accept(s, nullptr, nullptr);
            if (c == INVALID_SOCKET) {
                if (g_running) fprintf(stderr, "[FAKE_ADB] accept failed\n");
                break;
            }
            std::thread(fake_adb_client_handler, c).detach();
        }
    }).detach();
    return s;
}
#endif

// ============================================================================
// Main — Sender
// ============================================================================

void print_usage(const char* prog) {
    printf("Usage: %s [options]\n", prog);
    printf("  --adb <host:port>      adbd address (default 127.0.0.1:5555)\n");
    printf("  --listen <host:port>   WAN listen address (default 0.0.0.0:22777)\n");
    printf("  --simulate-adb         Run fake adbd on --adb port (for Windows test)\n");
    printf("  --auto                САМ вводить всі команди: adb start-server, adb tcpip,\n");
    printf("                        визначення IP телефону, СКАНУВАННЯ всієї LAN зі списком\n");
    printf("                        'IP — назва пристрою' і вибором. Нічого вводити не треба.\n");
    printf("  --phone-ip <ip>       IP телефону (якщо автовизначення не спрацювало)\n");
    printf("  --adb-port <port>     Порт tcpip (default 5555)\n");
    printf("  --scan                Примусово просканувати LAN і запропонувати вибір,\n");
    printf("                        навіть якщо телефон вже підключено\n");
    printf("  --no-scan             НЕ шукати в мережі (тільки USB/--phone-ip)\n");
    printf("  --pick <N|IP>         Автовибір пристрою без запиту (номер зі списку або IP)\n");
    printf("  --pair-code <код>     6-значний код парування (Бездротове налагодження без кабелю)\n");
    printf("  --help                 Show help\n");
    printf("\nExamples:\n");
    printf("  Android: %s --adb 127.0.0.1:5555 --listen 0.0.0.0:22777\n", prog);
    printf("  Windows sim: %s --simulate-adb --adb 127.0.0.1:5555 --listen 0.0.0.0:22777\n", prog);
    printf("  AUTO (реальний телефон по USB): %s --auto --listen 0.0.0.0:22777\n", prog);
    printf("  AUTO (скан мережі + вибір): %s --auto --scan\n", prog);
    printf("  AUTO (БЕЗ кабелю, Android 11+): %s --auto  (підкаже що ввести з екрану)\n", prog);
    printf("  AUTO (демо без телефону): %s --auto --simulate-adb\n", prog);
}

int main(int argc, char* argv[]) {
    const char* adb_host = "127.0.0.1";
    uint16_t adb_port = 5555;
    const char* listen_host = "0.0.0.0";
    uint16_t listen_port = DEFAULT_QUIC_PORT;
    bool simulate_adb = false;
    bool auto_mode = false;
    std::string phone_ip_override;
    bool force_scan = false;
    bool allow_scan = true;
    std::string pick;
    std::string pair_code;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--adb") == 0 && i+1 < argc) {
            std::string s = argv[++i];
            auto colon = s.find(':');
            if (colon != std::string::npos) {
                static char hbuf[256];
                strncpy(hbuf, s.substr(0, colon).c_str(), sizeof(hbuf)-1);
                hbuf[sizeof(hbuf)-1] = '\0';
                adb_host = hbuf;
                adb_port = (uint16_t)atoi(s.substr(colon+1).c_str());
            }
        } else if (strcmp(argv[i], "--listen") == 0 && i+1 < argc) {
            std::string s = argv[++i];
            auto colon = s.find(':');
            if (colon != std::string::npos) {
                static char hbuf2[256];
                strncpy(hbuf2, s.substr(0, colon).c_str(), sizeof(hbuf2)-1);
                hbuf2[sizeof(hbuf2)-1] = '\0';
                listen_host = hbuf2;
                listen_port = (uint16_t)atoi(s.substr(colon+1).c_str());
            }
        } else if (strcmp(argv[i], "--simulate-adb") == 0) {
            simulate_adb = true;
        } else if (strcmp(argv[i], "--auto") == 0) {
            auto_mode = true;
        } else if (strcmp(argv[i], "--phone-ip") == 0 && i+1 < argc) {
            phone_ip_override = argv[++i];
        } else if (strcmp(argv[i], "--adb-port") == 0 && i+1 < argc) {
            adb_port = (uint16_t)atoi(argv[++i]);
        } else if (strcmp(argv[i], "--scan") == 0) {
            force_scan = true;
        } else if (strcmp(argv[i], "--no-scan") == 0) {
            allow_scan = false;
        } else if (strcmp(argv[i], "--pick") == 0 && i+1 < argc) {
            pick = argv[++i];
        } else if (strcmp(argv[i], "--pair-code") == 0 && i+1 < argc) {
            pair_code = argv[++i];
        } else if (strcmp(argv[i], "--help") == 0) {
            print_usage(argv[0]);
            return 0;
        }
    }

    printf("=== Virtual USB Cable — Agent-Sender ===\n");
    printf("ADB: %s:%u  Listen: %s:%u  Simulate: %s  Auto: %s\n",
           adb_host, adb_port, listen_host, listen_port, simulate_adb ? "yes" : "no",
           auto_mode ? "yes" : "no");

    // --- AUTO: сам вводимо всі adb-команди ---
    if (auto_mode && !simulate_adb) {
        printf("\n[AUTO] ===== Авторежим Sender: сам виконую adb-команди =====\n");
        autosetup::EnsureAdbServer();
        autosetup::PrintAdbDevices();
        if (!phone_ip_override.empty()) {
            printf("[AUTO] Використовую заданий --phone-ip=%s\n", phone_ip_override.c_str());
            autosetup::AdbConnect(phone_ip_override, adb_port);
            // Після connect телефон доступний за phone_ip:port — перемикаємо adb_host
            static char auto_hbuf[256];
            strncpy(auto_hbuf, phone_ip_override.c_str(), sizeof(auto_hbuf)-1);
            auto_hbuf[sizeof(auto_hbuf)-1] = '\0';
            adb_host = auto_hbuf;
        } else {
            // Повний ланцюжок: USB -> tcpip -> connect, БЕЗ кабелю (pair), скан LAN.
            // conn = "ip:port" (порт може бути динамічним від Бездротового налагодження!).
            std::string conn = autosetup::AutoSetupPhoneWifi(adb_port, allow_scan, force_scan, pick,
                                                             600, pair_code);
            if (!conn.empty() && conn.find(':') != std::string::npos) {
                auto colon = conn.find(':');
                static char auto_hbuf2[256];
                strncpy(auto_hbuf2, conn.substr(0, colon).c_str(), sizeof(auto_hbuf2)-1);
                auto_hbuf2[sizeof(auto_hbuf2)-1] = '\0';
                adb_host = auto_hbuf2;
                adb_port = (uint16_t)atoi(conn.substr(colon + 1).c_str());
                printf("[AUTO] Телефон готовий: %s (adbd %s:%u)\n", conn.c_str(), adb_host, adb_port);
            } else {
                printf("[AUTO] Телефон по Wi-Fi не налаштовано. Якщо телефону нема — перезапустіть з --simulate-adb.\n");
                printf("[AUTO] Продовжую слухати WAN — Receiver може підключитись в демо-режимі.\n");
            }
        }
        autosetup::PrintAdbDevices();
        printf("[AUTO] ===== Автонастройка Sender завершена =====\n\n");
    }

#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2,2), &wsa);

    SOCKET fake_adb_sock = INVALID_SOCKET;
    if (simulate_adb) {
        fake_adb_sock = start_fake_adb_server(adb_port);
        if (fake_adb_sock == INVALID_SOCKET) return 1;
        // Даємо час fake adbd запуститися
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    // Створюємо WAN listen socket
    SOCKET listen_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listen_sock == INVALID_SOCKET) {
        fprintf(stderr, "[SENDER] socket failed\n");
        return 1;
    }
    BOOL reuse = TRUE;
    setsockopt(listen_sock, SOL_SOCKET, SO_REUSEADDR, (char*)&reuse, sizeof(reuse));
    BOOL nodelay = TRUE;
    setsockopt(listen_sock, IPPROTO_TCP, TCP_NODELAY, (char*)&nodelay, sizeof(nodelay));

    sockaddr_in laddr = {};
    laddr.sin_family = AF_INET;
    laddr.sin_port = htons(listen_port);
    if (strcmp(listen_host, "0.0.0.0") == 0) laddr.sin_addr.s_addr = INADDR_ANY;
    else inet_pton(AF_INET, listen_host, &laddr.sin_addr);

    if (bind(listen_sock, (sockaddr*)&laddr, sizeof(laddr)) == SOCKET_ERROR) {
        fprintf(stderr, "[SENDER] bind %s:%u failed: %d\n", listen_host, listen_port, WSAGetLastError());
        return 1;
    }
    if (listen(listen_sock, 5) == SOCKET_ERROR) {
        fprintf(stderr, "[SENDER] listen failed\n");
        return 1;
    }
    printf("[SENDER] Listening on %s:%u (WAN)\n", listen_host, listen_port);
    printf("[SENDER] Waiting for Receiver (Windows) to connect...\n");

    // Обробка Ctrl+C
    SetConsoleCtrlHandler([](DWORD sig) -> BOOL {
        if (sig == CTRL_C_EVENT) {
            printf("\n[SENDER] Ctrl+C, shutting down...\n");
            g_running = false;
            return TRUE;
        }
        return FALSE;
    }, TRUE);

    // Accept loop — для PoC один клієнт одночасно
    while (g_running) {
        SOCKET wan_client = accept(listen_sock, nullptr, nullptr);
        if (wan_client == INVALID_SOCKET) {
            if (g_running) fprintf(stderr, "[SENDER] accept failed: %d\n", WSAGetLastError());
            break;
        }
        printf("[SENDER] Receiver connected!\n");
        setsockopt(wan_client, IPPROTO_TCP, TCP_NODELAY, (char*)&nodelay, sizeof(nodelay));

        // Підключаємося до adbd (або fake adbd)
        AdbBridge bridge;
        int retries = 5;
        bool adb_ok = false;
        while (retries-- > 0 && g_running) {
            if (bridge.connect_adb(adb_host, adb_port)) {
                adb_ok = true;
                break;
            }
            fprintf(stderr, "[SENDER] adb connect failed, retry (%d)...\n", retries);
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
        if (!adb_ok) {
            fprintf(stderr, "[SENDER] Failed to connect to adbd, closing WAN client\n");
            closesocket(wan_client);
            continue;
        }

        bridge.set_wan_socket((int)wan_client);
        if (!bridge.start()) {
            fprintf(stderr, "[SENDER] bridge start failed\n");
            closesocket(wan_client);
            continue;
        }

        printf("[SENDER] Bridge active. Forwarding ADB <-> WAN\n");
        printf("[SENDER] Run on Windows: agent_receiver.exe --server %s:%u --simulate\n",
               "127.0.0.1", listen_port); // підказка

        // Чекаємо поки клієнт відключиться або Ctrl+C
        while (g_running) {
            // Перевіряємо чи сокет живий (peek)
            char tmp;
            int r = recv(wan_client, &tmp, 1, MSG_PEEK);
            if (r == 0) {
                printf("[SENDER] Receiver disconnected (orderly)\n");
                break;
            } else if (r < 0) {
                int err = WSAGetLastError();
                if (err == WSAEWOULDBLOCK) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(200));
                    continue;
                }
                printf("[SENDER] Receiver disconnected (err=%d)\n", err);
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(200));
        }

        bridge.stop();
        bridge.disconnect_adb();
        closesocket(wan_client);
        printf("[SENDER] Bridge stopped, waiting for next Receiver...\n");
    }

    closesocket(listen_sock);
    if (fake_adb_sock != INVALID_SOCKET) closesocket(fake_adb_sock);
    printf("[SENDER] Shutdown complete\n");

#else
    // Linux/Android — аналогічно, але з POSIX sockets
    printf("[SENDER] Linux/Android build — POSIX sockets (see Windows code for logic)\n");
    // Для NDK — той самий код з close() замість closesocket()
#endif

    return 0;
}
