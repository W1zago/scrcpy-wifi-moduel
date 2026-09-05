#include "fec.h"
#include <cstring>
#include <algorithm>

// ============================================================================
// FecEncoder — XOR parity (PoC). Для production замінити на GF(256) RS.
// XOR дозволяє відновити 1 втрату на групу. Для демонстрації достатньо.
// Повний RS: кожен паритет = лінійна комбінація data в GF(256) з Vandermonde матрицею.
// ============================================================================

bool FecEncoder::add_data(const uint8_t* chunk, size_t len, uint32_t& out_group, uint16_t& out_index) {
    if (current_data_count_ >= FEC_DATA_SHARDS) {
        // Група переповнена — треба було викликати get_parity і скинути
        return false;
    }
    current_.data[current_data_count_].assign(chunk, chunk + len);
    out_group = next_group_id_;
    out_index = (uint16_t)current_data_count_;
    current_.group_id = next_group_id_;
    current_data_count_++;

    if (current_data_count_ == FEC_DATA_SHARDS) {
        compute_parity();
        return true; // група готова
    }
    return false;
}

void FecEncoder::compute_parity() {
    // Знаходимо макс довжину серед data (для padding)
    size_t max_len = 0;
    for (size_t i = 0; i < FEC_DATA_SHARDS; ++i) {
        max_len = std::max(max_len, current_.data[i].size());
    }
    // Ініціалізуємо parity нулями
    for (size_t p = 0; p < FEC_PARITY_SHARDS; ++p) {
        current_.parity[p].assign(max_len, 0);
    }
    // Parity 0 = XOR всіх data (може відновити 1 втрату)
    // Parity 1..3 = XOR з різними ротаціями (спрощено, для PoC)
    for (size_t i = 0; i < FEC_DATA_SHARDS; ++i) {
        for (size_t b = 0; b < current_.data[i].size(); ++b) {
            current_.parity[0][b] ^= current_.data[i][b];
            // Parity 1: XOR з ротацією на 1 біт
            current_.parity[1][b] ^= (uint8_t)((current_.data[i][b] << 1) | (current_.data[i][b] >> 7));
            // Parity 2,3 — аналогічно, для PoC просто копії з різними ключами
            current_.parity[2][b] ^= (uint8_t)(current_.data[i][b] ^ (i * 0x55));
            current_.parity[3][b] ^= (uint8_t)(current_.data[i][b] ^ (i * 0xAA));
        }
        // Паддінг нулями для коротших пакетів — вже враховано (parity має max_len)
    }
}

void FecEncoder::get_parity_packets(uint32_t group_id, std::array<std::vector<uint8_t>, FEC_PARITY_SHARDS>& out) {
    // group_id має збігатися з current
    (void)group_id;
    out = current_.parity;
}

void FecEncoder::reset_group(uint32_t group_id) {
    (void)group_id;
    current_ = FecGroup();
    current_data_count_ = 0;
    next_group_id_++;
    if (next_group_id_ == 0) next_group_id_ = 1; // 0 зарезервовано
}

// ============================================================================
// FecDecoder
// ============================================================================

FecGroup* FecDecoder::find_or_create(uint32_t group_id) {
    // Шукаємо існуючу
    for (size_t i = 0; i < MAX_GROUPS; ++i) {
        if (group_ids_[i] == group_id) return &groups_[i];
    }
    // Створюємо нову (evict найстарішу)
    // Проста стратегія: кільцева заміна по group_id % MAX_GROUPS
    size_t idx = group_id % MAX_GROUPS;
    // Якщо слот зайнятий іншою групою — затираємо (вона або завершена, або втрачена)
    groups_[idx] = FecGroup();
    groups_[idx].group_id = group_id;
    group_ids_[idx] = group_id;
    return &groups_[idx];
}

bool FecDecoder::receive_packet(uint32_t group_id, uint16_t index, const uint8_t* data, size_t len, bool is_parity) {
    FecGroup* g = find_or_create(group_id);
    if (!g) return false;

    if (is_parity) {
        if (index >= FEC_PARITY_SHARDS) return false;
        g->parity[index].assign(data, data + len);
        g->received_mask |= (1u << (FEC_DATA_SHARDS + index));
    } else {
        if (index >= FEC_DATA_SHARDS) return false;
        g->data[index].assign(data, data + len);
        g->received_mask |= (1u << index);
    }

    // Перевіряємо чи можна завершити групу
    // Варіант 1: всі 10 data отримані — готово
    uint16_t data_mask = (1u << FEC_DATA_SHARDS) - 1; // 0x3FF (10 біт)
    if ((g->received_mask & data_mask) == data_mask) {
        g->is_complete = true;
        return true;
    }

    // Варіант 2: спроба відновлення через FEC (для PoC — тільки 1 втрата)
    if (try_recover(*g)) {
        g->is_complete = true;
        return true;
    }

    // Ще не готово — чекаємо більше пакетів
    return false;
}

bool FecDecoder::try_recover(FecGroup& g) {
    // Рахуємо скільки data втрачено
    uint16_t data_mask = (1u << FEC_DATA_SHARDS) - 1;
    uint16_t received_data = g.received_mask & data_mask;
    int missing = 0;
    int missing_idx = -1;
    for (int i = 0; i < (int)FEC_DATA_SHARDS; ++i) {
        if (!(received_data & (1u << i))) {
            missing++;
            missing_idx = i;
        }
    }
    if (missing != 1) return false; // PoC XOR — тільки 1 втрата. Для RS — до 4.

    // Чи є parity 0?
    if (!(g.received_mask & (1u << FEC_DATA_SHARDS))) return false;

    // Відновлюємо XOR: missing = parity0 ^ (всі інші data)
    size_t max_len = g.parity[0].size();
    g.data[missing_idx].assign(max_len, 0);
    // Починаємо з parity
    for (size_t b = 0; b < max_len; ++b) {
        g.data[missing_idx][b] = g.parity[0][b];
    }
    // XOR всі інші отримані data
    for (int i = 0; i < (int)FEC_DATA_SHARDS; ++i) {
        if (i == missing_idx) continue;
        if (!(received_data & (1u << i))) continue; // не отримано (але missing==1, тому не станеться)
        for (size_t b = 0; b < g.data[i].size() && b < max_len; ++b) {
            g.data[missing_idx][b] ^= g.data[i][b];
        }
    }
    // Обрізаємо паддінг (якщо оригінальний пакет був коротший за max_len)
    // Для PoC залишаємо max_len — зайві нулі не завадять (H.264 parser їх проігнорує)
    g.received_mask |= (1u << missing_idx);
    return true;
}

bool FecDecoder::get_recovered_data(uint32_t group_id, std::array<std::vector<uint8_t>, FEC_DATA_SHARDS>& out) {
    for (size_t i = 0; i < MAX_GROUPS; ++i) {
        if (group_ids_[i] == group_id && groups_[i].is_complete) {
            out = groups_[i].data;
            return true;
        }
    }
    return false;
}

void FecDecoder::cleanup_old_groups(uint32_t up_to_group_id) {
    for (size_t i = 0; i < MAX_GROUPS; ++i) {
        if (group_ids_[i] != 0 && group_ids_[i] < up_to_group_id) {
            groups_[i] = FecGroup();
            group_ids_[i] = 0;
        }
    }
}
