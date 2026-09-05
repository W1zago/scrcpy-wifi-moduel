#include "ghost_device.h"
#include "vhci_injector.h"
#include <cstdio>

GhostDevice::GhostDevice(VhciInjector* vhci) : vhci_(vhci) {}

void GhostDevice::on_disconnected() {
    if (state_ == State::GHOST) return; // вже в ghost
    printf("[GHOST] Network lost! Entering GHOST mode (30s grace)\n");
    state_ = State::GHOST;
    ghost_enter_ms_ = GetTickCount64();
    vhci_->set_ghost_mode(true);
    start_ghost_timer();
}

void GhostDevice::on_reconnected() {
    if (state_ != State::GHOST && state_ != State::RECONNECTING) return;
    uint64_t dur = ghost_duration_ms();
    printf("[GHOST] Reconnected after %llu ms! Replaying %zu URBs\n", dur, replay_count_);
    state_ = State::CONNECTED;
    cancel_ghost_timer();
    vhci_->set_ghost_mode(false);
    replay_all();
}

void GhostDevice::on_tick() {
    if (state_ != State::GHOST) return;
    uint64_t dur = ghost_duration_ms();
    if (dur >= GHOST_TIMEOUT_MS) {
        printf("[GHOST] Timeout %llu ms exceeded! Removing virtual device\n", dur);
        state_ = State::CONNECTED;
        vhci_->set_ghost_mode(false);
        vhci_->remove_device();
        replay_count_ = 0;
        replay_head_ = 0;
    }
}

uint64_t GhostDevice::ghost_duration_ms() const {
    if (state_ != State::GHOST) return 0;
    return GetTickCount64() - ghost_enter_ms_;
}

void GhostDevice::save_for_replay(const ReplayEntry& e) {
    replay_buf_[replay_head_] = e;
    replay_head_ = (replay_head_ + 1) % REPLAY_SIZE;
    if (replay_count_ < REPLAY_SIZE) replay_count_++;
    // Якщо переповнили — найстаріший затирається (кільце)
}

void GhostDevice::replay_all() {
    if (replay_count_ == 0) {
        printf("[GHOST] Nothing to replay\n");
        return;
    }
    // Відтворюємо в порядку seqnum (replay_buf вже в порядку додавання)
    size_t start = (replay_head_ + REPLAY_SIZE - replay_count_) % REPLAY_SIZE;
    for (size_t i = 0; i < replay_count_; ++i) {
        size_t idx = (start + i) % REPLAY_SIZE;
        ReplayEntry& e = replay_buf_[idx];
        // Відправляємо через vhci (вже не в ghost mode)
        // Використовуємо той самий seqnum — Sender має replay буфер і віддасть відповідь
        printf("[GHOST] Replay seq=%u ep=0x%02X len=%u\n", e.seqnum, e.ep, e.length);
        // Копіюємо дані в UrbPool слот і сабмітимо
        // Спрощено: викликаємо submit_urb напряму
        // В реальному коді — алокуємо UrbSlot з pool
        vhci_->submit_urb(e.seqnum, e.ep, e.direction, e.data, e.length, nullptr);
    }
    // Не чистимо replay_count одразу — чекаємо підтвердження RET_SUBMIT
    // Очистимо коли всі RET прийдуть
}

void GhostDevice::start_ghost_timer() {
    // В реальному коді — SetTimer з HWND. Для PoC використовуємо GetTickCount64 в on_tick
    printf("[GHOST] Timer started (30s)\n");
}

void GhostDevice::cancel_ghost_timer() {
    printf("[GHOST] Timer cancelled\n");
}

void CALLBACK GhostDevice::ghost_timeout_proc(HWND hwnd, UINT msg, UINT_PTR id, DWORD time) {
    (void)hwnd; (void)msg; (void)id; (void)time;
    // Викликається через WM_TIMER — в PoC не використовуємо
}
