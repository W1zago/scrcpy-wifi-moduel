#pragma once
/**
 * ring_buffer.h — Lock-free SPSC ring buffer для URB pool
 * 
 * Використовується між QUIC receive thread та VHCI completion thread.
 * SPSC = Single Producer Single Consumer — достатньо для 1 потоку прийому + 1 потоку відправки.
 * Вирівнювання на 64 bytes (cache line) щоб уникнути false sharing між ядрами.
 */

#include <atomic>
#include <cstdint>
#include <cstddef>
#include <cassert>

template<typename T, size_t Capacity>
class RingBuffer {
    static_assert((Capacity & (Capacity - 1)) == 0, "Capacity must be power of 2");
public:
    RingBuffer() : head_(0), tail_(0) {}

    // Викликає ТІЛЬКИ producer (QUIC thread)
    bool push(const T& item) {
        size_t head = head_.load(std::memory_order_relaxed);
        size_t next_head = (head + 1) & (Capacity - 1);
        if (next_head == tail_.load(std::memory_order_acquire)) {
            return false; // full
        }
        buffer_[head] = item;
        head_.store(next_head, std::memory_order_release);
        return true;
    }

    // Викликає ТІЛЬКИ consumer (VHCI thread)
    bool pop(T& out) {
        size_t tail = tail_.load(std::memory_order_relaxed);
        if (tail == head_.load(std::memory_order_acquire)) {
            return false; // empty
        }
        out = buffer_[tail];
        tail_.store((tail + 1) & (Capacity - 1), std::memory_order_release);
        return true;
    }

    bool empty() const {
        return head_.load(std::memory_order_acquire) == tail_.load(std::memory_order_acquire);
    }

    size_t size() const {
        size_t h = head_.load(std::memory_order_acquire);
        size_t t = tail_.load(std::memory_order_acquire);
        return (h - t) & (Capacity - 1);
    }

    static constexpr size_t capacity() { return Capacity - 1; } // один слот завжди порожній

private:
    alignas(64) std::atomic<size_t> head_;
    alignas(64) std::atomic<size_t> tail_;
    alignas(64) T buffer_[Capacity];
};

// ============================================================================
// URB Pool — попередньо-алоковані буфери 16KB, вирівняні на 4096 (сторінка)
// Вирівнювання важливе для DMA в vhci.sys (MemLock, MDL)
// ============================================================================

#include "protocol.h"

struct UrbSlot {
    alignas(4096) uint8_t data[MAX_URB_PAYLOAD];
    size_t  length = 0;
    uint32_t seqnum = 0;
    uint32_t ep = 0;
    bool     in_use = false;
};

class UrbPool {
public:
    UrbPool() {
        for (size_t i = 0; i < URB_POOL_SIZE; ++i) {
            slots_[i].in_use = false;
        }
    }

    // Знайти вільний слот (lock-free через CAS)
    UrbSlot* acquire() {
        for (size_t i = 0; i < URB_POOL_SIZE; ++i) {
            bool expected = false;
            // Використовуємо атомарний прапорець в кожному слоті
            // Спрощено: крутимося по кільцю з atomic index
            size_t idx = next_idx_.fetch_add(1, std::memory_order_relaxed) & (URB_POOL_SIZE - 1);
            // Спроба захопити слот idx
            // Оскільки in_use не атомарний в структурі, використовуємо окремий масив флагів
            if (!in_use_[idx].exchange(true, std::memory_order_acquire)) {
                return &slots_[idx];
            }
        }
        return nullptr; // pool exhausted — потрібно дропнути або зачекати
    }

    void release(UrbSlot* slot) {
        size_t idx = slot - slots_;
        assert(idx < URB_POOL_SIZE);
        slot->length = 0;
        slot->seqnum = 0;
        in_use_[idx].store(false, std::memory_order_release);
    }

    // Для дебагу: скільки зайнято
    size_t used() const {
        size_t c = 0;
        for (size_t i = 0; i < URB_POOL_SIZE; ++i) if (in_use_[i].load()) ++c;
        return c;
    }

private:
    UrbSlot slots_[URB_POOL_SIZE];
    std::atomic<bool> in_use_[URB_POOL_SIZE] = {};
    std::atomic<size_t> next_idx_{0};
};
