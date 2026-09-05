/**
 * receiver_main.cpp — Agent-Receiver (Windows Host)
 * 
 * Архітектура потоків:
 *  [QUIC Recv Thread] -> WanHeader+UsbIp -> [VHCI Injector (OVERLAPPED)] -> vhci.sys -> WinUSB -> adb
 *  [VHCI Fetch Thread] <- URB від Host (коли adb хоче читати) <- vhci.sys
 *  [Keepalive Thread] -> PING кожні 500мс
 *  [Ghost Timer] -> перевірка 30s timeout
 * 
 * Для PoC QUIC замінено на TCP (щоб не тягнути msquic залежність).
 * Заміна на QUIC — один #ifdef: замінити TcpTransport на QuicTransport.
 * Логіка URB інкапсуляції ідентична.
 */

#include "vhci_injector.h"
#include "adb_spoof.h"
#include "ghost_device.h"
#include "../common/protocol.h"
#include "../common/ring_buffer.h"
#include "../common/fec.h"
#include "../common/auto_setup.h"

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
#endif

#include <cstdio>
#include <cstdint>
#include <thread>
#include <atomic>
#include <vector>
#include <chrono>

// ============================================================================
// TCP Transport (PoC) — заміна для QUIC в демо
// Для production замінити на MsQuic API (QuicConnection, QuicStream)
// ============================================================================

class TcpTransport {
public:
    TcpTransport() : sock_(INVALID_SOCKET) {}
    ~TcpTransport() { disconnect(); }

    bool connect(const char* host, uint16_t port) {
        WSADATA wsa;
        WSAStartup(MAKEWORD(2,2), &wsa);
        sock_ = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (sock_ == INVALID_SOCKET) return false;

        // TCP_NODELAY — критично для ADB (маленькі повідомлення)
        BOOL nodelay = TRUE;
        setsockopt(sock_, IPPROTO_TCP, TCP_NODELAY, (char*)&nodelay, sizeof(nodelay));
        // Keepalive
        BOOL keepalive = TRUE;
        setsockopt(sock_, SOL_SOCKET, SO_KEEPALIVE, (char*)&keepalive, sizeof(keepalive));
        // BBR або CUBIC — на Windows за замовчуванням CUBIC, для WAN ок

        sockaddr_in addr = {};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(port);
        inet_pton(AF_INET, host, &addr.sin_addr);

        printf("[TCP] Connecting to %s:%u...\n", host, port);
        if (::connect(sock_, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR) {
            fprintf(stderr, "[TCP] connect failed: %d\n", WSAGetLastError());
            closesocket(sock_);
            sock_ = INVALID_SOCKET;
            return false;
        }
        printf("[TCP] Connected to %s:%u\n", host, port);
        return true;
    }

    void disconnect() {
        if (sock_ != INVALID_SOCKET) {
            closesocket(sock_);
            sock_ = INVALID_SOCKET;
        }
    }

    bool is_connected() const { return sock_ != INVALID_SOCKET; }

    // Відправити: [len:4 BE][data]
    bool send_packet(const uint8_t* data, size_t len) {
        if (sock_ == INVALID_SOCKET) return false;
        uint32_t be_len = hton32((uint32_t)len);
        if (send_all((uint8_t*)&be_len, 4) != 4) return false;
        if (send_all(data, len) != (int)len) return false;
        return true;
    }

    // Прийняти: [len:4 BE][data]
    bool recv_packet(std::vector<uint8_t>& out) {
        if (sock_ == INVALID_SOCKET) return false;
        uint32_t be_len = 0;
        if (recv_all((uint8_t*)&be_len, 4) != 4) return false;
        uint32_t len = ntoh32(be_len);
        if (len > MAX_WAN_PACKET + 1024) {
            fprintf(stderr, "[TCP] Packet too large: %u\n", len);
            return false;
        }
        out.resize(len);
        if (recv_all(out.data(), len) != (int)len) return false;
        return true;
    }

    // Сирий send/recv для ADB bridge (без framing)
    int send_all(const uint8_t* data, size_t len) {
        size_t sent = 0;
        while (sent < len) {
            int n = ::send(sock_, (char*)data + sent, (int)(len - sent), 0);
            if (n <= 0) return -1;
            sent += n;
        }
        return (int)sent;
    }
    int recv_all(uint8_t* data, size_t len) {
        size_t recvd = 0;
        while (recvd < len) {
            int n = ::recv(sock_, (char*)data + recvd, (int)(len - recvd), 0);
            if (n <= 0) return -1;
            recvd += n;
        }
        return (int)recvd;
    }

    SOCKET raw_socket() const { return sock_; }

private:
    SOCKET sock_;
};

// ============================================================================
// Глобальний стан
// ============================================================================

static VhciInjector g_vhci;
static AdbSpoof     g_adb;
static GhostDevice  g_ghost(&g_vhci);
static UrbPool      g_pool;
static FecDecoder   g_fec_decoder;
static std::atomic<bool> g_running{true};
static std::atomic<uint32_t> g_seqnum{100};

// Jitter buffer — затримує IN URB на 30мс перед віддачею в vhci
struct JitterEntry {
    uint32_t seqnum;
    uint32_t ep;
    uint32_t actual_length;
    uint32_t status;
    uint64_t ready_time_us; // QPC + 30ms
    std::vector<uint8_t> data;
};
static RingBuffer<JitterEntry, 256> g_jitter_buf;

// ============================================================================
// Потік 1: Мережа -> VHCI
// ============================================================================

void network_to_vhci_loop(TcpTransport* transport) {
    printf("[RECV] Network->VHCI thread started\n");
    std::vector<uint8_t> pkt;

    while (g_running) {
        if (!transport->recv_packet(pkt)) {
            if (!g_running) break;
            fprintf(stderr, "[RECV] recv failed, entering GHOST\n");
            g_ghost.on_disconnected();
            // Чекаємо реконекту
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            g_ghost.on_tick();
            continue;
        }

        // Якщо були в GHOST — реконект
        if (g_ghost.is_ghost()) {
            g_ghost.on_reconnected();
        }

        if (pkt.size() < sizeof(UsbIpHeaderBase)) {
            fprintf(stderr, "[RECV] Packet too small: %zu\n", pkt.size());
            continue;
        }

        // Парсимо: чи є WanHeader?
        size_t offset = 0;
        WanHeader wan = {};
        bool has_wan = false;
        if (pkt.size() >= sizeof(WanHeader) + sizeof(UsbIpHeaderBase)) {
            WanHeader* wh = reinterpret_cast<WanHeader*>(pkt.data());
            // Перевіряємо magic (BE)
            if (ntoh32(wh->magic) == WAN_MAGIC) {
                wan = *wh;
                wan_header_ntoh(&wan);
                has_wan = true;
                offset += sizeof(WanHeader);
                // Jitter handling: якщо timestamp + 30ms < зараз — вже запізнились, віддаємо одразу
                // Інакше — кладемо в jitter buffer
            }
        }

        UsbIpHeaderBase* base = reinterpret_cast<UsbIpHeaderBase*>(pkt.data() + offset);
        uint32_t cmd = ntoh32(base->command);
        offset += sizeof(UsbIpHeaderBase);

        if (cmd == USBIP_RET_SUBMIT) {
            // Відповідь від телефону (Device->Host) — треба завершити URB в vhci
            if (pkt.size() < offset + sizeof(UsbIpRetSubmit)) {
                fprintf(stderr, "[RECV] RET_SUBMIT too small\n");
                continue;
            }
            UsbIpRetSubmit* ret = reinterpret_cast<UsbIpRetSubmit*>(pkt.data() + offset);
            uint32_t seqnum = ntoh32(ret->seqnum);
            uint32_t status = ntoh32(ret->status);
            uint32_t actual_len = ntoh32(ret->actual_length);
            offset += sizeof(UsbIpRetSubmit);
            size_t payload_len = pkt.size() - offset;
            uint8_t* payload = pkt.data() + offset;

            // Перевіряємо чи це ADB — логуємо
            if (payload_len >= sizeof(AdbMessage)) {
                g_adb.handle_incoming(payload, payload_len, ntoh32(ret->ep));
            }

            // FEC: якщо це відео (fec_group !=0) — декодуємо
            if (has_wan && wan.fec_group != 0) {
                bool is_parity = (wan.flags & WAN_FLAG_FEC_PARITY) != 0;
                bool complete = g_fec_decoder.receive_packet(wan.fec_group, wan.fec_index, payload, payload_len, is_parity);
                if (complete) {
                    // Група завершена — відновлюємо всі 10 пакетів і віддаємо в vhci
                    std::array<std::vector<uint8_t>, FEC_DATA_SHARDS> recovered;
                    if (g_fec_decoder.get_recovered_data(wan.fec_group, recovered)) {
                        for (int i = 0; i < (int)FEC_DATA_SHARDS; ++i) {
                            // Кожен recovered — окремий URB
                            // Для PoC просто ігноруємо (відео відновилося)
                        }
                        g_fec_decoder.cleanup_old_groups(wan.fec_group);
                    }
                    continue; // не віддаємо цей паритетний пакет як URB
                }
                if (is_parity) continue; // паритет не віддаємо в vhci напряму
            }

            // Jitter buffer: якщо IN і has_wan — затримуємо 30мс
            if (has_wan && ntoh32(ret->direction) == 1) {
                LARGE_INTEGER qpc, freq;
                QueryPerformanceCounter(&qpc);
                QueryPerformanceFrequency(&freq);
                uint64_t now_us = (qpc.QuadPart * 1000000ull) / freq.QuadPart;
                uint64_t ready_us = wan.timestamp_us + JITTER_BUFFER_MS * 1000;
                if (ready_us > now_us) {
                    // Кладемо в jitter buffer
                    JitterEntry je;
                    je.seqnum = seqnum;
                    je.ep = ntoh32(ret->ep);
                    je.actual_length = actual_len;
                    je.status = status;
                    je.ready_time_us = ready_us;
                    je.data.assign(payload, payload + payload_len);
                    if (!g_jitter_buf.push(je)) {
                        fprintf(stderr, "[JITTER] Buffer full, dropping seq=%u\n", seqnum);
                    } else {
                        printf("[JITTER] Queued seq=%u ready in %llu us\n", seqnum, ready_us - now_us);
                    }
                    continue;
                }
            }

            // Без jitter — одразу complete
            // Потрібно скопіювати payload в UrbPool слот (бо pkt буде перезаписано)
            UrbSlot* slot = g_pool.acquire();
            if (!slot) {
                fprintf(stderr, "[RECV] UrbPool exhausted, dropping seq=%u\n", seqnum);
                continue;
            }
            if (payload_len > MAX_URB_PAYLOAD) payload_len = MAX_URB_PAYLOAD;
            memcpy(slot->data, payload, payload_len);
            slot->length = payload_len;
            slot->seqnum = seqnum;

            // Завершуємо URB в vhci
            g_vhci.complete_urb(seqnum, status, actual_len);
            g_pool.release(slot);

        } else if (cmd == USBIP_CMD_PONG) {
            printf("[RECV] PONG received\n");
        } else if (cmd == USBIP_CMD_HELLO) {
            printf("[RECV] HELLO from sender\n");
        } else {
            fprintf(stderr, "[RECV] Unknown command: 0x%08X\n", cmd);
        }
    }
    printf("[RECV] Network->VHCI thread stopped\n");
}

// ============================================================================
// Потік 2: Jitter buffer -> VHCI (віддає затримані IN URB)
// ============================================================================

void jitter_loop() {
    printf("[JITTER] Jitter thread started (30ms buffer)\n");
    while (g_running) {
        JitterEntry je;
        if (!g_jitter_buf.pop(je)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            continue;
        }
        LARGE_INTEGER qpc, freq;
        QueryPerformanceCounter(&qpc);
        QueryPerformanceFrequency(&freq);
        uint64_t now_us = (qpc.QuadPart * 1000000ull) / freq.QuadPart;
        if (je.ready_time_us > now_us) {
            uint64_t wait_us = je.ready_time_us - now_us;
            if (wait_us > 50000) wait_us = 50000; // кап 50мс
            std::this_thread::sleep_for(std::chrono::microseconds(wait_us));
        }
        // Віддаємо в vhci
        UrbSlot* slot = g_pool.acquire();
        if (slot) {
            size_t copy = je.data.size() > MAX_URB_PAYLOAD ? MAX_URB_PAYLOAD : je.data.size();
            memcpy(slot->data, je.data.data(), copy);
            g_vhci.complete_urb(je.seqnum, je.status, je.actual_length);
            g_pool.release(slot);
            printf("[JITTER] Released seq=%u ep=0x%02X len=%u\n", je.seqnum, je.ep, je.actual_length);
        }
    }
    printf("[JITTER] Jitter thread stopped\n");
}

// ============================================================================
// Потік 3: VHCI Fetch -> Мережа (коли Host хоче надіслати URB)
// В реальному драйвері — vhci.sys генерує FETCH через ioctl.
// В PoC — симулюємо через періодичний poll (або через adb daemon bulk OUT)
// Для демо — цей потік не потрібен, бо Host->Device йде через інший канал
// Але для повноти — показуємо як би це було.
// ============================================================================

void vhci_to_network_loop(TcpTransport* transport) {
    printf("[SEND] VHCI->Network thread started (simulated)\n");
    // В реальному коді тут:
    // while (g_running) {
    //   UsbIpSubmit submit;
    //   DWORD bytes;
    //   DeviceIoControl(h_vhci, IOCTL_VHCI_FETCH_URB, nullptr, 0, &submit, sizeof(submit), &bytes, &ov);
    //   // Отримали URB від Host (наприклад, A_CNXN від adb)
    //   // Загортаємо в WanHeader + UsbIpHeaderBase + UsbIpSubmit + payload
    //   // і відправляємо через transport->send_packet()
    // }
    // Для PoC — просто keepalive
    while (g_running) {
        std::this_thread::sleep_for(std::chrono::milliseconds(KEEPALIVE_MS));
        if (!transport->is_connected()) continue;

        // Відправляємо PING
        UsbIpHeaderBase ping = {};
        ping.version = hton32(USBIP_VERSION);
        ping.command = hton32(USBIP_CMD_PING);
        ping.status = hton32(0);
        std::vector<uint8_t> pkt(sizeof(ping));
        memcpy(pkt.data(), &ping, sizeof(ping));
        // Додаємо WanHeader для WAN
        WanHeader wan = {};
        wan.magic = hton32(WAN_MAGIC);
        LARGE_INTEGER qpc, freq;
        QueryPerformanceCounter(&qpc);
        QueryPerformanceFrequency(&freq);
        wan.timestamp_us = hton64((qpc.QuadPart * 1000000ull) / freq.QuadPart);
        wan.fec_group = hton32(0);
        wan.flags = hton32(WAN_FLAG_NONE);
        std::vector<uint8_t> wan_pkt(sizeof(WanHeader) + pkt.size());
        memcpy(wan_pkt.data(), &wan, sizeof(WanHeader));
        memcpy(wan_pkt.data() + sizeof(WanHeader), pkt.data(), pkt.size());
        // Переставляємо magic вже в BE (wan_header_hton зробила своп, але ми вручну)
        // Для спрощення — відправляємо без WAN header для PING
        transport->send_packet(pkt.data(), pkt.size());
    }
    printf("[SEND] VHCI->Network thread stopped\n");
}

// ============================================================================
// Симуляція Host->Device URB (для тесту без реального vhci.sys)
// Генерує фейковий A_CNXN і відправляє на Sender
// ============================================================================

void simulate_adb_handshake(TcpTransport* transport) {
    printf("[SIM] Simulating ADB handshake (Host->Device A_CNXN)\n");

    // Формуємо ADB A_CNXN message
    AdbMessage cnxn = {};
    cnxn.command = htole32(A_CNXN);
    cnxn.arg0 = htole32(ADB_VERSION);
    cnxn.arg1 = htole32(ADB_MAXDATA);
    const char* banner = "host::";
    cnxn.data_length = htole32((uint32_t)strlen(banner));
    cnxn.data_checksum = htole32(adb_checksum((uint8_t*)banner, strlen(banner)));
    cnxn.magic = htole32(A_CNXN ^ 0xFFFFFFFFu);

    // Загортаємо в USBIP SUBMIT
    UsbIpHeaderBase base = {};
    base.version = hton32(USBIP_VERSION);
    base.command = hton32(USBIP_CMD_SUBMIT);
    base.status = hton32(0);

    UsbIpSubmit submit = {};
    submit.seqnum = hton32(g_seqnum.fetch_add(1));
    submit.devid = hton32((1 << 16) | 2);
    submit.direction = hton32(0); // OUT
    submit.ep = hton32(0x01);
    submit.transfer_flags = hton32(0);
    submit.transfer_buffer_length = hton32(sizeof(AdbMessage) + strlen(banner));
    // setup нулі для bulk

    WanHeader wan = {};
    wan.magic = WAN_MAGIC;
    wan.timestamp_us = 0;
    wan.fec_group = 0;
    wan.flags = WAN_FLAG_NONE;
    wan_header_hton(&wan);

    // Збираємо пакет: [WanHeader][Base][Submit][AdbMessage][banner]
    size_t total = sizeof(WanHeader) + sizeof(base) + sizeof(submit) + sizeof(cnxn) + strlen(banner);
    std::vector<uint8_t> pkt(total);
    size_t off = 0;
    memcpy(pkt.data() + off, &wan, sizeof(wan)); off += sizeof(wan);
    memcpy(pkt.data() + off, &base, sizeof(base)); off += sizeof(base);
    memcpy(pkt.data() + off, &submit, sizeof(submit)); off += sizeof(submit);
    memcpy(pkt.data() + off, &cnxn, sizeof(cnxn)); off += sizeof(cnxn);
    memcpy(pkt.data() + off, banner, strlen(banner));

    transport->send_packet(pkt.data(), pkt.size());
    printf("[SIM] Sent A_CNXN (seq=%u)\n", ntoh32(submit.seqnum));
}

// ============================================================================
// Main
// ============================================================================

void print_usage(const char* prog) {
    printf("Usage: %s --server <host:port> [--vhci] [--simulate] [--auto] [--no-scrcpy]\n", prog);
    printf("  --server <host:port>  Sender address (default 127.0.0.1:22777)\n");
    printf("  --vhci                Try to use real vhci.sys (requires usbip-win2)\n");
    printf("  --simulate            Simulate ADB handshake without real device\n");
    printf("  --auto                САМ вводить всі команди: adb start-server, перевірка vhci,\n");
    printf("                        adb devices, автозапуск scrcpy. Нічого вводити вручну не треба.\n");
    printf("  --no-scrcpy           Не запускати scrcpy автоматично (навіть з --auto)\n");
    printf("  --scrcpy-args \"...\"   Аргументи для scrcpy (default \"--select-usb\")\n");
    printf("  --help                Show this help\n");
    printf("\nExamples:\n");
    printf("  %s --auto --simulate                      (демо без телефону, все само)\n", prog);
    printf("  %s --auto --vhci --server 192.168.1.100:22777  (реальний телефон + драйвер)\n", prog);
}

int main(int argc, char* argv[]) {
    const char* server_host = "127.0.0.1";
    uint16_t server_port = DEFAULT_QUIC_PORT;
    bool use_vhci = false;
    bool simulate = false;
    bool auto_mode = false;
    bool auto_scrcpy = true;
    std::string scrcpy_args = "--select-usb";

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--server") == 0 && i+1 < argc) {
            std::string s = argv[++i];
            auto colon = s.find(':');
            if (colon != std::string::npos) {
                server_host = argv[i]; // спрощено
                // Парсимо host:port
                std::string host = s.substr(0, colon);
                std::string port = s.substr(colon+1);
                // Копіюємо host в статичний буфер
                static char host_buf[256];
                strncpy(host_buf, host.c_str(), sizeof(host_buf)-1);
                host_buf[sizeof(host_buf)-1] = '\0';
                server_host = host_buf;
                server_port = (uint16_t)atoi(port.c_str());
            }
        } else if (strcmp(argv[i], "--vhci") == 0) {
            use_vhci = true;
        } else if (strcmp(argv[i], "--simulate") == 0) {
            simulate = true;
        } else if (strcmp(argv[i], "--auto") == 0) {
            auto_mode = true;
        } else if (strcmp(argv[i], "--no-scrcpy") == 0) {
            auto_scrcpy = false;
        } else if (strcmp(argv[i], "--scrcpy-args") == 0 && i+1 < argc) {
            scrcpy_args = argv[++i];
        } else if (strcmp(argv[i], "--help") == 0) {
            print_usage(argv[0]);
            return 0;
        }
    }

    printf("=== Virtual USB Cable — Agent-Receiver (Windows) ===\n");
    printf("Server: %s:%u  VHCI: %s  Simulate: %s  Auto: %s\n",
           server_host, server_port, use_vhci ? "yes" : "no", simulate ? "yes" : "no",
           auto_mode ? "yes" : "no");

    // --- AUTO: сам вводимо всі команди ---
    if (auto_mode) {
        printf("\n[AUTO] ===== Авторежим: сам виконую всі команди =====\n");
        autosetup::EnsureAdbServer();
        autosetup::CheckTestSigning();
        bool vhci_ok = autosetup::CheckVhciPresent();
        if (use_vhci && !vhci_ok) {
            printf("[AUTO] --vhci запитано, але драйвер не знайдено. Продовжую в SIMULATION.\n");
            printf("[AUTO] Встановіть драйвер: powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1\n");
        }
        if (!use_vhci && vhci_ok) {
            printf("[AUTO] Драйвер є, але --vhci не передано. Вмикаю --vhci автоматично.\n");
            use_vhci = true;
        }
        if (!simulate) {
            // В авторежимі без реального телефону краще симулювати handshake,
            // щоб scrcpy хоч щось побачив. Реальний handshake прийде з мережі сам.
            printf("[AUTO] Перевіряю adb devices перед підключенням...\n");
            autosetup::PrintAdbDevices();
        }
        printf("[AUTO] ===== Автонастройка завершена, підключаюсь =====\n\n");
    }

    // Ініціалізуємо VHCI
    if (use_vhci) {
        if (!g_vhci.open()) {
            fprintf(stderr, "[MAIN] VHCI open failed, continuing in SIMULATION mode\n");
            // Не виходимо — працюємо в симуляції
        } else {
            VhciDeviceInfo info;
            info.busnum = 1;
            info.devnum = 2;
            info.vendor = 0x18D1;
            info.product = 0x4EE7;
            memcpy(info.bus_id, "1-1", 4);
            g_vhci.add_device(info);
            g_vhci.start_completion_thread();
        }
    } else {
        printf("[MAIN] VHCI disabled — running in network-only mode (no Device Manager device)\n");
        printf("[MAIN] Use --vhci to create real USB device (requires usbip-win2 driver)\n");
    }

    // Підключаємося до Sender
    TcpTransport transport;
    // Retry loop
    while (g_running) {
        if (transport.connect(server_host, server_port)) break;
        fprintf(stderr, "[MAIN] Connect failed, retry in 2s...\n");
        std::this_thread::sleep_for(std::chrono::seconds(2));
    }
    if (!transport.is_connected()) {
        fprintf(stderr, "[MAIN] Failed to connect, exiting\n");
        return 1;
    }

    // Запускаємо потоки
    std::thread recv_thread(network_to_vhci_loop, &transport);
    std::thread jitter_thread(jitter_loop);
    std::thread send_thread(vhci_to_network_loop, &transport);

    // Симуляція handshake якщо ввімкнено
    if (simulate) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        simulate_adb_handshake(&transport);
    }

    printf("[MAIN] Running. Press Ctrl+C to stop.\n");
    if (auto_mode) {
        // AUTO: сам перевіряємо adb і сам запускаємо scrcpy — користувач нічого не вводить
        std::this_thread::sleep_for(std::chrono::milliseconds(800));
        printf("\n[AUTO] ===== Перевірка результату =====\n");
        autosetup::PrintAdbDevices();
        if (auto_scrcpy) {
            // Невелика пауза щоб vhci пристрій встиг з'явитися в Device Manager
            std::this_thread::sleep_for(std::chrono::milliseconds(700));
            if (!autosetup::LaunchScrcpy(scrcpy_args)) {
                printf("[AUTO] Не вдалося запустити scrcpy автоматично. Запустіть вручну: scrcpy %s\n", scrcpy_args.c_str());
            }
        } else {
            printf("[AUTO] --no-scrcpy: scrcpy не запускаю. Команда вручну: scrcpy %s\n", scrcpy_args.c_str());
        }
        printf("[AUTO] ===== Все готово, працюю. Ctrl+C щоб зупинити =====\n\n");
    } else {
        printf("[MAIN] Check 'adb devices' — should show device as USB (if --vhci)\n");
        printf("[MAIN] Run scrcpy: scrcpy --select-usb\n");
        printf("[MAIN] Або запустіть з --auto щоб все зробилось само.\n");
    }

    // Головний цикл — чекаємо Ctrl+C
    // На Windows — SetConsoleCtrlHandler
#ifdef _WIN32
    auto handler = [](DWORD sig) -> BOOL {
        if (sig == CTRL_C_EVENT) {
            printf("\n[MAIN] Ctrl+C received, shutting down...\n");
            g_running = false;
            return TRUE;
        }
        return FALSE;
    };
    SetConsoleCtrlHandler((PHANDLER_ROUTINE)handler, TRUE);
#endif

    // Чекаємо завершення потоків
    while (g_running) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
        g_ghost.on_tick();
    }

    // Shutdown
    transport.disconnect();
    g_vhci.stop_completion_thread();
    g_vhci.remove_device();
    g_vhci.close();

    if (recv_thread.joinable()) recv_thread.join();
    if (jitter_thread.joinable()) jitter_thread.join();
    if (send_thread.joinable()) send_thread.join();

    printf("[MAIN] Shutdown complete\n");
    return 0;
}
