#pragma once
/**
 * ghost_device.h — Логіка "Ghost Device" для переживання обривів без disconnect
 * 
 * Ідея:
 *  - При втраті QUIC з'єднання НЕ видаляємо пристрій з vhci.sys.
 *  - Переводимо в GHOST стан: всі URB від Host ставимо в pending чергу,
 *    відповідаємо STATUS_PENDING (а не DEVICE_NOT_CONNECTED).
 *  - adb daemon чекає (timeout 5с), але не вбиває сесію.
 *  - При реконекті (QUIC 0-RTT, <100мс) — replay всіх pending URB.
 *  - Якщо реконекту немає 30с — лише тоді робимо REMOVE_DEVICE.
 */

#include <cstdint>
#include <windows.h>
#include "../common/protocol.h"
#include "../common/ring_buffer.h"

class VhciInjector; // forward

class GhostDevice {
public:
    enum class State {
        CONNECTED,  // нормальний режим
        GHOST,      // мережа впала, тримаємо пристрій
        RECONNECTING,
    };

    GhostDevice(VhciInjector* vhci);

    void on_disconnected();
    void on_reconnected();
    void on_tick(); // викликати кожні 100мс для перевірки таймауту

    bool is_ghost() const { return state_ == State::GHOST; }
    State state() const { return state_; }

    // Скільки часу в GHOST (мс)
    uint64_t ghost_duration_ms() const;

    // Replay буфер — зберігає всі надіслані URB для повторної відправки
    struct ReplayEntry {
        uint32_t seqnum;
        uint32_t ep;
        uint32_t direction;
        uint32_t length;
        uint64_t timestamp_us;
        uint8_t  data[MAX_URB_PAYLOAD];
    };

    void save_for_replay(const ReplayEntry& e);
    void replay_all(); // відправити всі з replay буфера через vhci

private:
    VhciInjector* vhci_;
    State state_ = State::CONNECTED;
    uint64_t ghost_enter_ms_ = 0;
    uint64_t last_ping_ms_ = 0;

    // Replay буфер — кільцевий на 256 URB ( ~4MB)
    static constexpr size_t REPLAY_SIZE = 256;
    ReplayEntry replay_buf_[REPLAY_SIZE];
    size_t replay_head_ = 0;
    size_t replay_count_ = 0;

    void start_ghost_timer();
    void cancel_ghost_timer();
    static void CALLBACK ghost_timeout_proc(HWND, UINT, UINT_PTR, DWORD);
};
