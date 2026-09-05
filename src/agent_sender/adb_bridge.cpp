#include "adb_bridge.h"
#include "../common/protocol.h"
#include <cstdio>
#include <cstring>
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
#include <netinet/tcp.h>
#endif

// ============================================================================
// AdbBridge — реалізація
// ============================================================================

AdbBridge::AdbBridge() {}

AdbBridge::~AdbBridge() {
    stop();
    disconnect_adb();
}

bool AdbBridge::connect_adb(const char* host, uint16_t port) {
#ifdef _WIN32
    WSADATA wsa;
    WSAStartup(MAKEWORD(2,2), &wsa);
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) {
        fprintf(stderr, "[BRIDGE] socket failed: %d\n", WSAGetLastError());
        return false;
    }
    BOOL nodelay = TRUE;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (char*)&nodelay, sizeof(nodelay));

    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, host, &addr.sin_addr);

    printf("[BRIDGE] Connecting to adbd %s:%u...\n", host, port);
    if (connect(s, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR) {
        fprintf(stderr, "[BRIDGE] connect adbd failed: %d (is adb tcpip enabled? Run: adb tcpip 5555)\n", WSAGetLastError());
        closesocket(s);
        return false;
    }
    printf("[BRIDGE] Connected to adbd %s:%u\n", host, port);
    adb_sock_ = (int)s;
    return true;
#else
    // Linux/Android
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return false;
    int flag = 1;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &flag, sizeof(flag));
    sockaddr_in addr = {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, host, &addr.sin_addr);
    if (connect(s, (sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("[BRIDGE] connect adbd");
        close(s);
        return false;
    }
    adb_sock_ = s;
    return true;
#endif
}

void AdbBridge::disconnect_adb() {
    if (adb_sock_ != -1) {
#ifdef _WIN32
        closesocket((SOCKET)adb_sock_);
#else
        close(adb_sock_);
#endif
        adb_sock_ = -1;
    }
}

bool AdbBridge::start() {
    if (running_) return false;
    if (adb_sock_ == -1 || wan_sock_ == -1) {
        fprintf(stderr, "[BRIDGE] start: sockets not set (adb=%d wan=%d)\n", adb_sock_, wan_sock_);
        return false;
    }
    running_ = true;
    adb_to_wan_thread_ = std::thread(&AdbBridge::adb_to_wan_loop, this);
    wan_to_adb_thread_ = std::thread(&AdbBridge::wan_to_adb_loop, this);
    printf("[BRIDGE] Started (2 threads)\n");
    return true;
}

void AdbBridge::stop() {
    if (!running_) return;
    running_ = false;
    // Закриваємо сокети щоб розблокувати recv
    // (не закриваємо тут adb_sock/wan_sock, бо вони керуються ззовні)
#ifdef _WIN32
    if (adb_sock_ != -1) shutdown((SOCKET)adb_sock_, SD_BOTH);
    if (wan_sock_ != -1) shutdown((SOCKET)wan_sock_, SD_BOTH);
#endif
    if (adb_to_wan_thread_.joinable()) adb_to_wan_thread_.join();
    if (wan_to_adb_thread_.joinable()) wan_to_adb_thread_.join();
    printf("[BRIDGE] Stopped\n");
}

// ============================================================================
// Потік 1: adbd -> WAN (Device->Host, RET_SUBMIT)
// Читає ADB messages з локального adbd і відправляє як USBIP RET_SUBMIT
// ============================================================================

void AdbBridge::adb_to_wan_loop() {
    printf("[BRIDGE] adb->wan thread started\n");
    std::vector<uint8_t> buf(64 * 1024);

    while (running_) {
        // Читаємо ADB message header (24 bytes)
        AdbMessage hdr = {};
#ifdef _WIN32
        int n = recv((SOCKET)adb_sock_, (char*)&hdr, sizeof(hdr), MSG_WAITALL);
#else
        ssize_t n = recv(adb_sock_, &hdr, sizeof(hdr), MSG_WAITALL);
#endif
        if (n <= 0) {
            if (running_) {
                fprintf(stderr, "[BRIDGE] adb recv header failed: %d\n",
#ifdef _WIN32
                        WSAGetLastError()
#else
                        errno
#endif
                );
                // Спроба реконекту до adbd
                std::this_thread::sleep_for(std::chrono::seconds(1));
            }
            continue;
        }
        if (n != sizeof(hdr)) {
            fprintf(stderr, "[BRIDGE] short header: %d\n", n);
            continue;
        }

        // Валідація
        if (!adb_validate(&hdr)) {
            fprintf(stderr, "[BRIDGE] invalid ADB magic: cmd=0x%08X\n", hdr.command);
            continue;
        }

        uint32_t data_len = letoh32(hdr.data_length);
        if (data_len > 256 * 1024) {
            fprintf(stderr, "[BRIDGE] data too large: %u\n", data_len);
            continue;
        }

        // Читаємо data якщо є
        std::vector<uint8_t> data(data_len);
        if (data_len > 0) {
#ifdef _WIN32
            n = recv((SOCKET)adb_sock_, (char*)data.data(), data_len, MSG_WAITALL);
#else
            n = recv(adb_sock_, data.data(), data_len, MSG_WAITALL);
#endif
            if (n != (int)data_len) {
                fprintf(stderr, "[BRIDGE] short data: %d vs %u\n", n, data_len);
                continue;
            }
            // Перевіряємо checksum
            uint32_t calc = adb_checksum(data.data(), data_len);
            if (calc != letoh32(hdr.data_checksum)) {
                fprintf(stderr, "[BRIDGE] checksum mismatch: %u vs %u\n", calc, letoh32(hdr.data_checksum));
                // Не дропаємо — можливо біт-фліп, але QUIC вже захищає
            }
        }

        printf("[BRIDGE] adbd -> WAN %s len=%u\n", adb_cmd_to_str(letoh32(hdr.command)), data_len);
        adb_messages_++;

        // Формуємо повний ADB packet: [header][data]
        size_t adb_total = sizeof(hdr) + data_len;
        std::vector<uint8_t> adb_pkt(adb_total);
        memcpy(adb_pkt.data(), &hdr, sizeof(hdr));
        if (data_len > 0) memcpy(adb_pkt.data() + sizeof(hdr), data.data(), data_len);

        // Загортаємо в USBIP RET_SUBMIT
        // Для PoC генеруємо seqnum з лічильника (в реальності — з SUBMIT від Host)
        static std::atomic<uint32_t> seq{1000};
        uint32_t cur_seq = seq.fetch_add(1);

        UsbIpHeaderBase base = {};
        base.version = hton32(USBIP_VERSION);
        base.command = hton32(USBIP_RET_SUBMIT);
        base.status = hton32(0);

        UsbIpRetSubmit ret = {};
        ret.seqnum = hton32(cur_seq);
        ret.devid = hton32((1 << 16) | 2);
        ret.direction = hton32(1); // IN (Device->Host)
        ret.ep = hton32(0x81);
        ret.status = hton32(0);
        ret.actual_length = hton32((uint32_t)adb_total);

        WanHeader wan = {};
        wan.magic = WAN_MAGIC;
        // timestamp
#ifdef _WIN32
        LARGE_INTEGER qpc, freq;
        QueryPerformanceCounter(&qpc);
        QueryPerformanceFrequency(&freq);
        wan.timestamp_us = (qpc.QuadPart * 1000000ull) / freq.QuadPart;
#else
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        wan.timestamp_us = ts.tv_sec * 1000000ull + ts.tv_nsec / 1000;
#endif
        wan.fec_group = 0; // не відео
        wan.flags = WAN_FLAG_NONE;
        wan_header_hton(&wan);

        // Збираємо WAN пакет: [WanHeader][Base][RetSubmit][adb_pkt]
        size_t total = sizeof(WanHeader) + sizeof(base) + sizeof(ret) + adb_total;
        std::vector<uint8_t> wan_pkt(total);
        size_t off = 0;
        memcpy(wan_pkt.data() + off, &wan, sizeof(wan)); off += sizeof(wan);
        memcpy(wan_pkt.data() + off, &base, sizeof(base)); off += sizeof(base);
        memcpy(wan_pkt.data() + off, &ret, sizeof(ret)); off += sizeof(ret);
        memcpy(wan_pkt.data() + off, adb_pkt.data(), adb_total);

        // Зберігаємо для replay
        save_replay(cur_seq, wan_pkt.data(), wan_pkt.size());

        // Відправляємо через WAN
        if (!send_wan_packet(wan_pkt.data(), wan_pkt.size())) {
            fprintf(stderr, "[BRIDGE] send_wan failed\n");
            // WAN впав — GHOST на Receiver врятує, тут просто чекаємо
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        } else {
            bytes_to_wan_ += wan_pkt.size();
        }
    }
    printf("[BRIDGE] adb->wan thread stopped\n");
}

// ============================================================================
// Потік 2: WAN -> adbd (Host->Device, SUBMIT)
// Отримує USBIP SUBMIT від Receiver і пише в локальний adbd
// ============================================================================

void AdbBridge::wan_to_adb_loop() {
    printf("[BRIDGE] wan->adb thread started\n");
    std::vector<uint8_t> pkt;

    while (running_) {
        if (!recv_wan_packet(pkt)) {
            if (running_) {
                fprintf(stderr, "[BRIDGE] recv_wan failed\n");
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            continue;
        }
        bytes_from_wan_ += pkt.size();

        if (pkt.size() < sizeof(WanHeader) + sizeof(UsbIpHeaderBase)) {
            fprintf(stderr, "[BRIDGE] wan packet too small: %zu\n", pkt.size());
            continue;
        }

        // Парсимо WanHeader
        WanHeader wan = {};
        memcpy(&wan, pkt.data(), sizeof(wan));
        wan_header_ntoh(&wan);
        size_t off = 0;
        bool has_wan = false;
        if (wan.magic == WAN_MAGIC) {
            has_wan = true;
            off = sizeof(WanHeader);
        } else {
            // Нема WAN header — чистий USBIP (для LAN)
            off = 0;
        }

        UsbIpHeaderBase* base = reinterpret_cast<UsbIpHeaderBase*>(pkt.data() + off);
        uint32_t cmd = ntoh32(base->command);
        off += sizeof(UsbIpHeaderBase);

        if (cmd == USBIP_CMD_SUBMIT) {
            if (pkt.size() < off + sizeof(UsbIpSubmit)) {
                fprintf(stderr, "[BRIDGE] SUBMIT too small\n");
                continue;
            }
            UsbIpSubmit* sub = reinterpret_cast<UsbIpSubmit*>(pkt.data() + off);
            uint32_t data_len = ntoh32(sub->transfer_buffer_length);
            off += sizeof(UsbIpSubmit);
            if (pkt.size() < off + data_len) {
                fprintf(stderr, "[BRIDGE] SUBMIT payload short: %zu vs %u\n", pkt.size() - off, data_len);
                continue;
            }
            uint8_t* payload = pkt.data() + off;

            // Для bulk OUT це має бути ADB message
            if (data_len >= sizeof(AdbMessage)) {
                AdbMessage* msg = reinterpret_cast<AdbMessage*>(payload);
                if (adb_validate(msg)) {
                    printf("[BRIDGE] WAN -> adbd %s len=%u seq=%u\n",
                           adb_cmd_to_str(letoh32(msg->command)), letoh32(msg->data_length), ntoh32(sub->seqnum));
                }
            }

            // Пишемо в adbd (сирий TCP, без framing — adbd очікує ADB messages напряму)
#ifdef _WIN32
            int sent = send((SOCKET)adb_sock_, (char*)payload, data_len, 0);
#else
            ssize_t sent = send(adb_sock_, payload, data_len, 0);
#endif
            if (sent != (int)data_len) {
                fprintf(stderr, "[BRIDGE] send to adbd failed: %d\n", sent);
            }

        } else if (cmd == USBIP_CMD_PING) {
            // Відповідаємо PONG
            printf("[BRIDGE] PING -> PONG\n");
            UsbIpHeaderBase pong = {};
            pong.version = hton32(USBIP_VERSION);
            pong.command = hton32(USBIP_CMD_PONG);
            pong.status = hton32(0);
            send_wan_packet((uint8_t*)&pong, sizeof(pong));

        } else if (cmd == USBIP_CMD_RESTORE) {
            // Receiver просить replay після GHOST
            uint32_t from_seq = 0;
            if (pkt.size() >= off + 4) {
                from_seq = ntoh32(*(uint32_t*)(pkt.data() + off));
            }
            printf("[BRIDGE] RESTORE from seq=%u\n", from_seq);
            handle_restore(from_seq);

        } else if (cmd == USBIP_CMD_HELLO) {
            printf("[BRIDGE] HELLO received\n");
        } else {
            fprintf(stderr, "[BRIDGE] unknown WAN cmd: 0x%08X\n", cmd);
        }
    }
    printf("[BRIDGE] wan->adb thread stopped\n");
}

bool AdbBridge::send_wan_packet(const uint8_t* data, size_t len) {
    if (wan_sock_ == -1) return false;
    // Framing: [len:4 BE][data]
    uint32_t be_len = hton32((uint32_t)len);
#ifdef _WIN32
    if (send((SOCKET)wan_sock_, (char*)&be_len, 4, 0) != 4) return false;
    if (send((SOCKET)wan_sock_, (char*)data, (int)len, 0) != (int)len) return false;
#else
    if (send(wan_sock_, &be_len, 4, 0) != 4) return false;
    if (send(wan_sock_, data, len, 0) != (ssize_t)len) return false;
#endif
    return true;
}

bool AdbBridge::recv_wan_packet(std::vector<uint8_t>& out) {
    if (wan_sock_ == -1) return false;
    uint32_t be_len = 0;
#ifdef _WIN32
    if (recv((SOCKET)wan_sock_, (char*)&be_len, 4, MSG_WAITALL) != 4) return false;
#else
    if (recv(wan_sock_, &be_len, 4, MSG_WAITALL) != 4) return false;
#endif
    uint32_t len = ntoh32(be_len);
    if (len > MAX_WAN_PACKET + 1024) {
        fprintf(stderr, "[BRIDGE] wan packet too large: %u\n", len);
        return false;
    }
    out.resize(len);
#ifdef _WIN32
    if (recv((SOCKET)wan_sock_, (char*)out.data(), len, MSG_WAITALL) != (int)len) return false;
#else
    if (recv(wan_sock_, out.data(), len, MSG_WAITALL) != (ssize_t)len) return false;
#endif
    return true;
}

void AdbBridge::save_replay(uint32_t seqnum, const uint8_t* pkt, size_t len) {
    ReplayEntry& e = replay_buf_[replay_head_];
    e.seqnum = seqnum;
    e.packet.assign(pkt, pkt + len);
    replay_head_ = (replay_head_ + 1) % REPLAY_SIZE;
    if (replay_count_ < REPLAY_SIZE) replay_count_++;
}

void AdbBridge::handle_restore(uint32_t from_seqnum) {
    printf("[BRIDGE] Replaying from seq=%u (%zu in buffer)\n", from_seqnum, replay_count_);
    size_t start = (replay_head_ + REPLAY_SIZE - replay_count_) % REPLAY_SIZE;
    for (size_t i = 0; i < replay_count_; ++i) {
        size_t idx = (start + i) % REPLAY_SIZE;
        ReplayEntry& e = replay_buf_[idx];
        if (e.seqnum >= from_seqnum) {
            printf("[BRIDGE] Replay seq=%u len=%zu\n", e.seqnum, e.packet.size());
            send_wan_packet(e.packet.data(), e.packet.size());
        }
    }
}
