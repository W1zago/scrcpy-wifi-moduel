#pragma once
/**
 * vhci_injector.h — Інтерфейс до vhci.sys (usbip-win2) на Windows
 * 
 * Відповідальність:
 *  - Відкрити handle до \\.\vhci (або \\.\USBIP_VHCI)
 *  - Створити віртуальний USB пристрій (VID/PID Google ADB)
 *  - Приймати WAN пакети з QUIC/TCP і інжектити як URB через DeviceIoControl
 *  - Обробляти completion через IOCP (I/O Completion Port)
 * 
 * Пам'ять: всі URB буфери з UrbPool (вирівняні на 4096, pinned для DMA).
 * Потоки: QuicRecv -> Submit, CompletionPort -> Return.
 */

#include "../common/protocol.h"
#include "../common/ring_buffer.h"
#include <windows.h>
#include <cstdint>
#include <cstddef>
#include <atomic>
#include <functional>

#ifdef _WIN32
// IOCTL коди з usbip-win2 (https://github.com/vadimgrn/usbip-win2/blob/master/userspace/libusbip/vhci.h)
// Якщо драйвер не встановлено — ці коди не спрацюють, але код скомпілюється.
#define FILE_DEVICE_VHCI 0x8000
#define IOCTL_VHCI_ADD_DEVICE     CTL_CODE(FILE_DEVICE_VHCI, 0x800, METHOD_BUFFERED, FILE_WRITE_ACCESS)
#define IOCTL_VHCI_REMOVE_DEVICE  CTL_CODE(FILE_DEVICE_VHCI, 0x801, METHOD_BUFFERED, FILE_WRITE_ACCESS)
#define IOCTL_VHCI_SUBMIT_URB     CTL_CODE(FILE_DEVICE_VHCI, 0x802, METHOD_BUFFERED, FILE_WRITE_ACCESS)
#define IOCTL_VHCI_FETCH_URB      CTL_CODE(FILE_DEVICE_VHCI, 0x803, METHOD_BUFFERED, FILE_READ_ACCESS)
#endif

// Структура для ioctl SUBMIT (має збігатися з rg.vhci_driver.h)
struct VhciSubmitIrp {
    uint32_t seqnum;
    uint32_t devid;           // busnum<<16 | devnum
    uint32_t direction;       // 0=OUT, 1=IN
    uint32_t ep;              // endpoint address
    uint32_t transfer_flags;
    uint32_t transfer_buffer_length;
    uint8_t* transfer_buffer; // pointer to UrbPool slot (DMA)
    uint32_t timeout_ms;
    uint8_t  setup[8];        // для control
    OVERLAPPED overlapped;    // для async IO
};

struct VhciDeviceInfo {
    uint32_t busnum = 1;
    uint32_t devnum = 2;
    uint16_t vendor = 0x18D1;
    uint16_t product = 0x4EE7;
    uint8_t  bus_id[32] = "1-1"; // як в usbip
};

class VhciInjector {
public:
    VhciInjector();
    ~VhciInjector();

    // Відкрити handle до vhci.sys. Повертає false якщо драйвер не встановлено.
    bool open();

    void close();

    bool is_open() const { return h_vhci_ != INVALID_HANDLE_VALUE; }

    // Створити віртуальний пристрій. Має бути викликано ПЕРЕД submit.
    bool add_device(const VhciDeviceInfo& info);

    // Видалити пристрій (викликає disconnect в Windows USB stack)
    bool remove_device();

    // Сабміт URB (асинхронно, через OVERLAPPED). Буфер має жити до completion.
    // Повертає seqnum для відстеження.
    bool submit_urb(uint32_t seqnum, uint32_t ep, uint32_t direction,
                    uint8_t* buffer, uint32_t length,
                    const uint8_t setup[8], uint32_t timeout_ms = 5000);

    // Завершити URB (коли прийшла відповідь з мережі)
    bool complete_urb(uint32_t seqnum, uint32_t status, uint32_t actual_length);

    // Потік який чекає completion через GetQueuedCompletionStatus
    void start_completion_thread();
    void stop_completion_thread();

    // Callback коли vhci просить новий URB (для IN transfers, коли Host хоче читати)
    using FetchCallback = std::function<void(uint32_t seqnum, uint32_t ep)>;
    void set_fetch_callback(FetchCallback cb) { fetch_cb_ = cb; }

    // Статистика
    uint64_t submitted() const { return submitted_; }
    uint64_t completed() const { return completed_; }
    uint64_t failed() const { return failed_; }

    // Для GHOST mode: поставити URB в чергу замість сабміту
    void set_ghost_mode(bool ghost) { ghost_mode_ = ghost; }
    bool ghost_mode() const { return ghost_mode_; }

private:
    HANDLE h_vhci_ = INVALID_HANDLE_VALUE;
    HANDLE h_iocp_ = INVALID_HANDLE_VALUE;
    HANDLE h_completion_thread_ = nullptr;
    bool   ghost_mode_ = false;
    bool   running_ = false;

    VhciDeviceInfo dev_info_;
    bool device_added_ = false;

    std::atomic<uint64_t> submitted_{0};
    std::atomic<uint64_t> completed_{0};
    std::atomic<uint64_t> failed_{0};

    FetchCallback fetch_cb_;

    // Черга для GHOST mode (коли мережа впала, URB не сабмітимо, а тримаємо)
    struct PendingUrb {
        uint32_t seqnum;
        uint32_t ep;
        uint32_t direction;
        uint32_t length;
        uint8_t* buffer;
        uint64_t timestamp_us;
    };
    RingBuffer<PendingUrb, 1024> pending_queue_;

    static DWORD WINAPI completion_thread_proc(LPVOID param);
    void completion_loop();
};
