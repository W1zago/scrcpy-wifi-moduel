#pragma once
/**
 * adb_bridge.h — Міст між локальним adbd (TCP 127.0.0.1:5555) та WAN транспортом
 * 
 * Працює на Android (NDK) або на Windows-симуляторі.
 * Логіка:
 *  - Підключається до 127.0.0.1:5555 (adbd в режимі tcpip)
 *  - Читає ADB messages (24-byte header + data) з adbd
 *  - Загортає кожне ADB message в USBIP RET_SUBMIT (як bulk IN)
 *  - Відправляє через TCP/QUIC на Receiver (Windows)
 *  - Зворотний напрямок: отримує USBIP SUBMIT від Receiver, розгортає, пише в adbd
 * 
 * Потоки: AdbRead -> WanSend, WanRecv -> AdbWrite
 * Буфери: всі через UrbPool, zero-copy де можливо.
 */

#include "../common/protocol.h"
#include <cstdint>
#include <cstddef>
#include <atomic>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <winsock2.h>
#else
#include <sys/socket.h>
#endif

class AdbBridge {
public:
    AdbBridge();
    ~AdbBridge();

    // Підключитися до локального adbd
    bool connect_adb(const char* host, uint16_t port);
    void disconnect_adb();

    bool is_adb_connected() const { return adb_sock_ != -1; }

    // Встановити WAN сокет (вже підключений TCP/QUIC)
    void set_wan_socket(int wan_sock) { wan_sock_ = wan_sock; }

    // Запустити bridge потоки
    bool start();
    void stop();

    // Статистика
    uint64_t bytes_to_wan() const { return bytes_to_wan_; }
    uint64_t bytes_from_wan() const { return bytes_from_wan_; }
    uint64_t adb_messages() const { return adb_messages_; }

private:
    int adb_sock_ = -1;  // сокет до 127.0.0.1:5555
    int wan_sock_ = -1;  // сокет до Receiver (TCP)
    std::atomic<bool> running_{false};
    std::thread adb_to_wan_thread_;
    std::thread wan_to_adb_thread_;

    std::atomic<uint64_t> bytes_to_wan_{0};
    std::atomic<uint64_t> bytes_from_wan_{0};
    std::atomic<uint64_t> adb_messages_{0};

    // Replay буфер для GHOST (зберігаємо останні 256 RET_SUBMIT для повторної відправки)
    struct ReplayEntry {
        uint32_t seqnum;
        std::vector<uint8_t> packet; // повний WAN пакет
    };
    static constexpr size_t REPLAY_SIZE = 256;
    ReplayEntry replay_buf_[REPLAY_SIZE];
    size_t replay_head_ = 0;
    size_t replay_count_ = 0;

    void adb_to_wan_loop(); // читає з adbd, шле в WAN
    void wan_to_adb_loop(); // читає з WAN, пише в adbd

    bool send_wan_packet(const uint8_t* data, size_t len);
    bool recv_wan_packet(std::vector<uint8_t>& out);

    void save_replay(uint32_t seqnum, const uint8_t* pkt, size_t len);
    void handle_restore(uint32_t from_seqnum); // відправити всі з replay буфера від seqnum
};
