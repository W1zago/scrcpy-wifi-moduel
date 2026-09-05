#pragma once
/**
 * fec.h — Forward Error Correction для відео потоку (Stream 2)
 * 
 * Використовуємо Reed-Solomon(14,10) — кожні 10 пакетів даних + 4 паритетних.
 * При втраті до 4 пакетів з 14 — відновлюємо без ретрансміту.
 * Для WAN з 2-5% втрат це покриває 99.9% випадків.
 * 
 * Реалізація: спрощений XOR-based FEC (для PoC) — швидкий, без Galois Field.
 * Для production замінити на `libfec` або `jerasure` (GF(2^8)).
 * XOR FEC відновлює 1 втрату на групу, для PoC достатньо.
 * Повний RS буде в fec.cpp з GF(256).
 */

#include <cstdint>
#include <cstddef>
#include <vector>
#include <array>

static constexpr size_t FEC_DATA_SHARDS = 10;
static constexpr size_t FEC_PARITY_SHARDS = 4;
static constexpr size_t FEC_TOTAL_SHARDS = FEC_DATA_SHARDS + FEC_PARITY_SHARDS;
static constexpr size_t FEC_MAX_PACKET = 1200; // MTU - headers

struct FecGroup {
    uint32_t group_id = 0;
    // Дані: 10 пакетів
    std::array<std::vector<uint8_t>, FEC_DATA_SHARDS> data;
    // Паритет: 4 пакети (XOR для PoC)
    std::array<std::vector<uint8_t>, FEC_PARITY_SHARDS> parity;
    // Бітмаска отриманих пакетів (14 біт)
    uint16_t received_mask = 0;
    // Чи група завершена (всі дані отримані або відновлені)
    bool is_complete = false;

    FecGroup() {
        for (auto& d : data) d.reserve(FEC_MAX_PACKET);
        for (auto& p : parity) p.reserve(FEC_MAX_PACKET);
    }
};

// Енкодер (Sender)
class FecEncoder {
public:
    FecEncoder() : next_group_id_(1) {}

    // Додати chunk даних (1200B). Повертає true якщо група заповнена і готова до відправки.
    bool add_data(const uint8_t* chunk, size_t len, uint32_t& out_group, uint16_t& out_index);

    // Отримати паритетні пакети для поточної групи (викликати коли add_data повернув true)
    void get_parity_packets(uint32_t group_id, std::array<std::vector<uint8_t>, FEC_PARITY_SHARDS>& out);

    // Скинути групу
    void reset_group(uint32_t group_id);

private:
    uint32_t next_group_id_;
    FecGroup current_;
    size_t   current_data_count_ = 0;

    void compute_parity(); // XOR для PoC
};

// Декодер (Receiver)
class FecDecoder {
public:
    // Отримати пакет (може бути data або parity)
    // Повертає true якщо групу вдалося завершити (всі дані відновлені)
    bool receive_packet(uint32_t group_id, uint16_t index, const uint8_t* data, size_t len, bool is_parity);

    // Отримати відновлені дані групи (10 пакетів)
    bool get_recovered_data(uint32_t group_id, std::array<std::vector<uint8_t>, FEC_DATA_SHARDS>& out);

    // Очистити старі групи (щоб не текла пам'ять)
    void cleanup_old_groups(uint32_t up_to_group_id);

private:
    // Карта груп (для PoC — простий масив на 64 групи, кільцевий)
    static constexpr size_t MAX_GROUPS = 64;
    std::array<FecGroup, MAX_GROUPS> groups_;
    std::array<uint32_t, MAX_GROUPS> group_ids_ = {}; // 0 = порожньо

    FecGroup* find_or_create(uint32_t group_id);
    bool try_recover(FecGroup& g);
};
