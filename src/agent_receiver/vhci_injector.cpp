#include "vhci_injector.h"
#include <cstdio>
#include <cassert>

#ifdef _WIN32
#include <winioctl.h>
#include <setupapi.h>
#include <initguid.h>
#pragma comment(lib, "setupapi.lib")
#endif

// ============================================================================
// VhciInjector — реалізація
// ============================================================================

VhciInjector::VhciInjector() {}

VhciInjector::~VhciInjector() {
    close();
}

bool VhciInjector::open() {
#ifdef _WIN32
    // Спроба відкрити vhci пристрій. Ім'я залежить від версії usbip-win2:
    // - Старі версії: \\.\vhci
    // - Нові (ude): \\.\USBIP_VHCI
    const wchar_t* candidates[] = { L"\\\\.\\vhci", L"\\\\.\\USBIP_VHCI", L"\\\\.\\usbip_vhci" };
    for (auto name : candidates) {
        h_vhci_ = CreateFileW(name,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            nullptr, OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED, // обов'язково для IOCP
            nullptr);
        if (h_vhci_ != INVALID_HANDLE_VALUE) {
            wprintf(L"[VHCI] Opened %s handle=%p\n", name, h_vhci_);
            break;
        }
    }
    if (h_vhci_ == INVALID_HANDLE_VALUE) {
        DWORD err = GetLastError();
        fprintf(stderr, "[VHCI] Failed to open vhci device: %lu\n", err);
        fprintf(stderr, "[VHCI] Install usbip-win2 driver: https://github.com/vadimgrn/usbip-win2/releases\n");
        fprintf(stderr, "[VHCI] And enable test signing: bcdedit /set testsigning on\n");
        return false;
    }

    // Створюємо IOCP для асинхронних completion
    h_iocp_ = CreateIoCompletionPort(h_vhci_, nullptr, 0, 0);
    if (!h_iocp_) {
        fprintf(stderr, "[VHCI] CreateIoCompletionPort failed: %lu\n", GetLastError());
        CloseHandle(h_vhci_);
        h_vhci_ = INVALID_HANDLE_VALUE;
        return false;
    }

    // Прив'язуємо vhci handle до IOCP
    // Всі OVERLAPPED операції будуть завершуватися через GetQueuedCompletionStatus
    return true;
#else
    // Для Linux — vhci-hcd через /dev/vhci (не реалізовано в цьому PoC, але структура та сама)
    fprintf(stderr, "[VHCI] Non-Windows platform — VHCI not implemented in this PoC\n");
    return false;
#endif
}

void VhciInjector::close() {
    stop_completion_thread();
    if (device_added_) {
        remove_device();
    }
#ifdef _WIN32
    if (h_iocp_ != INVALID_HANDLE_VALUE && h_iocp_ != nullptr) {
        CloseHandle(h_iocp_);
        h_iocp_ = nullptr;
    }
    if (h_vhci_ != INVALID_HANDLE_VALUE) {
        CloseHandle(h_vhci_);
        h_vhci_ = INVALID_HANDLE_VALUE;
    }
#endif
}

bool VhciInjector::add_device(const VhciDeviceInfo& info) {
    if (!is_open()) {
        fprintf(stderr, "[VHCI] add_device: not open\n");
        return false;
    }
    dev_info_ = info;

#ifdef _WIN32
    // Формуємо буфер для IOCTL_VHCI_ADD_DEVICE
    // Формат залежить від драйвера — для usbip-win2 це структура з bus_id, vendor, product
    // Спрощено для PoC: відправляємо як є, драйвер розбере.
    struct AddDeviceReq {
        uint32_t busnum;
        uint32_t devnum;
        uint16_t idVendor;
        uint16_t idProduct;
        uint8_t  bus_id[32];
        uint8_t  dev_path[256]; // для usbip-win2
    } req = {};
    req.busnum = info.busnum;
    req.devnum = info.devnum;
    req.idVendor = info.vendor;
    req.idProduct = info.product;
    memcpy(req.bus_id, info.bus_id, sizeof(req.bus_id));

    DWORD bytes = 0;
    BOOL ok = DeviceIoControl(h_vhci_, IOCTL_VHCI_ADD_DEVICE,
                              &req, sizeof(req),
                              nullptr, 0, &bytes, nullptr);
    if (!ok) {
        DWORD err = GetLastError();
        // Якщо драйвер не підтримує цей IOCTL — це нормально для PoC без драйвера
        // Логуємо але не падаємо (симуляція)
        fprintf(stderr, "[VHCI] IOCTL_VHCI_ADD_DEVICE failed: %lu (simulating success for PoC)\n", err);
        // Для PoC вважаємо що пристрій додано навіть якщо драйвер відсутній
        // Це дозволяє тестувати логіку без встановленого драйвера
    } else {
        printf("[VHCI] Device added: bus %u dev %u VID=%04X PID=%04X bus_id=%s\n",
               info.busnum, info.devnum, info.vendor, info.product, info.bus_id);
    }
    device_added_ = true;
    return true;
#else
    return false;
#endif
}

bool VhciInjector::remove_device() {
    if (!device_added_) return true;
#ifdef _WIN32
    struct RemoveReq {
        uint32_t busnum;
        uint32_t devnum;
    } req = { dev_info_.busnum, dev_info_.devnum };
    DWORD bytes = 0;
    BOOL ok = DeviceIoControl(h_vhci_, IOCTL_VHCI_REMOVE_DEVICE,
                              &req, sizeof(req),
                              nullptr, 0, &bytes, nullptr);
    if (!ok) {
        fprintf(stderr, "[VHCI] IOCTL_VHCI_REMOVE_DEVICE failed: %lu\n", GetLastError());
    } else {
        printf("[VHCI] Device removed: bus %u dev %u\n", req.busnum, req.devnum);
    }
#endif
    device_added_ = false;
    return true;
}

bool VhciInjector::submit_urb(uint32_t seqnum, uint32_t ep, uint32_t direction,
                              uint8_t* buffer, uint32_t length,
                              const uint8_t setup[8], uint32_t timeout_ms) {
    if (ghost_mode_) {
        // GHOST mode: не сабмітимо в драйвер, а ставимо в чергу
        PendingUrb p = {};
        p.seqnum = seqnum;
        p.ep = ep;
        p.direction = direction;
        p.length = length;
        p.buffer = buffer;
        // timestamp для jitter buffer (QPC)
        LARGE_INTEGER qpc, freq;
        QueryPerformanceCounter(&qpc);
        QueryPerformanceFrequency(&freq);
        p.timestamp_us = (qpc.QuadPart * 1000000ull) / freq.QuadPart;

        if (!pending_queue_.push(p)) {
            fprintf(stderr, "[VHCI] GHOST queue full, dropping URB seq=%u\n", seqnum);
            failed_++;
            return false;
        }
        printf("[VHCI] GHOST: queued URB seq=%u ep=0x%02X dir=%u len=%u (queue=%zu)\n",
               seqnum, ep, direction, length, pending_queue_.size());
        return true;
    }

    if (!is_open()) {
        // Без драйвера — симуляція (для тестів без vhci.sys)
        printf("[VHCI] SIMULATE submit seq=%u ep=0x%02X dir=%u len=%u (no driver)\n",
               seqnum, ep, direction, length);
        submitted_++;
        return true;
    }

#ifdef _WIN32
    // Реальний сабміт через OVERLAPPED
    // Виділяємо OVERLAPPED на купі, бо він має жити до completion
    OVERLAPPED* ov = new OVERLAPPED();
    memset(ov, 0, sizeof(OVERLAPPED));
    ov->hEvent = CreateEvent(nullptr, TRUE, FALSE, nullptr);

    VhciSubmitIrp irp = {};
    irp.seqnum = seqnum;
    irp.devid = (dev_info_.busnum << 16) | dev_info_.devnum;
    irp.direction = direction;
    irp.ep = ep;
    irp.transfer_buffer_length = length;
    irp.transfer_buffer = buffer; // DMA-coherent, з UrbPool (вирівняно на 4096)
    irp.timeout_ms = timeout_ms;
    if (setup) memcpy(irp.setup, setup, 8);
    irp.overlapped = *ov;

    DWORD bytes = 0;
    BOOL ok = DeviceIoControl(h_vhci_, IOCTL_VHCI_SUBMIT_URB,
                              &irp, sizeof(irp),
                              nullptr, 0, &bytes, ov);
    if (!ok) {
        DWORD err = GetLastError();
        if (err != ERROR_IO_PENDING) {
            fprintf(stderr, "[VHCI] submit_urb seq=%u failed: %lu\n", seqnum, err);
            CloseHandle(ov->hEvent);
            delete ov;
            failed_++;
            return false;
        }
        // ERROR_IO_PENDING — нормально для OVERLAPPED, чекаємо в completion thread
    }
    submitted_++;
    // Не чекаємо тут — completion прийде в IOCP
    // Зберігаємо ov для подальшого GetQueuedCompletionStatus
    return true;
#else
    return false;
#endif
}

bool VhciInjector::complete_urb(uint32_t seqnum, uint32_t status, uint32_t actual_length) {
    // Виклик коли прийшла відповідь з мережі (USBIP_RET_SUBMIT)
    // Потрібно завершити IRP в драйвері
    if (ghost_mode_) {
        // В GHOST mode шукаємо в pending_queue і завершуємо звідти
        // Спрощено: просто логуємо
        printf("[VHCI] GHOST complete seq=%u status=%u len=%u\n", seqnum, status, actual_length);
        completed_++;
        return true;
    }

    if (!is_open()) {
        printf("[VHCI] SIMULATE complete seq=%u status=%u len=%u\n", seqnum, status, actual_length);
        completed_++;
        return true;
    }

#ifdef _WIN32
    // Для usbip-win2 completion робиться через окремий IOCTL або через завершення OVERLAPPED
    // В реальному драйвері — викликаємо DeviceIoControl з RET_SUBMIT
    struct CompleteReq {
        uint32_t seqnum;
        uint32_t status;
        uint32_t actual_length;
    } req = { seqnum, status, actual_length };
    DWORD bytes = 0;
    // Використовуємо той самий IOCTL, але з RET кодом (драйвер розрізняє за seqnum)
    // Для PoC просто логуємо
    printf("[VHCI] Complete URB seq=%u status=%u actual_len=%u\n", seqnum, status, actual_length);
    completed_++;
    return true;
#else
    return false;
#endif
}

void VhciInjector::start_completion_thread() {
    if (running_) return;
    running_ = true;
#ifdef _WIN32
    h_completion_thread_ = CreateThread(nullptr, 0, completion_thread_proc, this, 0, nullptr);
#endif
}

void VhciInjector::stop_completion_thread() {
    if (!running_) return;
    running_ = false;
#ifdef _WIN32
    if (h_iocp_) {
        PostQueuedCompletionStatus(h_iocp_, 0, 0, nullptr); // розбудити потік
    }
    if (h_completion_thread_) {
        WaitForSingleObject(h_completion_thread_, 2000);
        CloseHandle(h_completion_thread_);
        h_completion_thread_ = nullptr;
    }
#endif
}

DWORD WINAPI VhciInjector::completion_thread_proc(LPVOID param) {
    VhciInjector* self = (VhciInjector*)param;
    self->completion_loop();
    return 0;
}

void VhciInjector::completion_loop() {
#ifdef _WIN32
    while (running_) {
        DWORD bytes = 0;
        ULONG_PTR key = 0;
        OVERLAPPED* ov = nullptr;
        BOOL ok = GetQueuedCompletionStatus(h_iocp_, &bytes, &key, &ov, 1000);
        if (!running_) break;
        if (!ok) {
            DWORD err = GetLastError();
            if (err == WAIT_TIMEOUT) continue;
            if (!ov) continue; // помилка IOCP, але не URB
            fprintf(stderr, "[VHCI] Completion error: %lu\n", err);
            failed_++;
            if (ov) {
                if (ov->hEvent) CloseHandle(ov->hEvent);
                delete ov;
            }
            continue;
        }
        if (!ov) continue; // PostQueuedCompletionStatus для wakeup
        // Успішне завершення URB
        completed_++;
        if (ov->hEvent) CloseHandle(ov->hEvent);
        delete ov;

        // Якщо є fetch callback — викликаємо (для IN URB, коли Host хоче читати)
        if (fetch_cb_) {
            // В реальному драйвері тут приходить новий URB від Host (FETCH)
            // Для PoC не генеруємо
        }
    }
#endif
}
