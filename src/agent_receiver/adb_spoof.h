#pragma once
/**
 * adb_spoof.h — Обробка ADB handshake та USB дескрипторів
 * 
 * Завдання: зробити так, щоб Windows adb daemon повірив, що спілкується з
 * фізичним USB пристроєм, хоча насправді дані йдуть через QUIC.
 */

#include <cstdint>
#include <cstddef>

class AdbSpoof {
public:
    enum class State {
        DISCONNECTED,
        CNXN_SENT,      // Host надіслав A_CNXN
        AUTH_PENDING,   // Чекаємо підтвердження RSA на телефоні
        CONNECTED,      // A_CNXN обмінялися
    };

    AdbSpoof() : state_(State::DISCONNECTED), maxdata_(4096) {}

    // Обробка OUT (Host->Device) — викликається перед відправкою в мережу
    bool handle_outgoing(const uint8_t* data, size_t len, uint32_t ep);

    // Обробка IN (Device->Host) — викликається при отриманні з мережі
    bool handle_incoming(const uint8_t* data, size_t len, uint32_t ep);

    // Обробка Control (ep0) — GET_DESCRIPTOR, SET_CONFIGURATION
    bool handle_control(const uint8_t* data, size_t len);

    // Генерація відповіді на GET_DESCRIPTOR
    size_t get_descriptor_response(uint8_t desc_type, uint8_t desc_idx,
                                   uint8_t* out, size_t out_max);

    State state() const { return state_; }
    uint32_t maxdata() const { return maxdata_; }

private:
    State state_;
    uint32_t maxdata_;
};
