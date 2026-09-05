#include "adb_spoof.h"
#include "../common/protocol.h"
#include <cstdio>
#include <cstring>

// ============================================================================
// AdbSpoof — обробка ADB handshake
// ============================================================================

bool AdbSpoof::handle_outgoing(const uint8_t* data, size_t len, uint32_t ep) {
    // Host -> Device (OUT на ep 0x01)
    // Може бути: Control (GET_DESCRIPTOR) або Bulk (ADB message)
    if (ep == 0x00) {
        // Control endpoint — GET_DESCRIPTOR, SET_CONFIGURATION
        return handle_control(data, len);
    }
    if (ep == 0x01) {
        // Bulk OUT — ADB message від Windows adb daemon
        if (len < sizeof(AdbMessage)) {
            fprintf(stderr, "[ADB] Bulk OUT too short: %zu\n", len);
            return false;
        }
        const AdbMessage* msg = reinterpret_cast<const AdbMessage*>(data);
        // Перевіряємо magic (network byte order для ADB — LE, тому не свопимо)
        // Але для дебагу виводимо
        if (!adb_validate(msg)) {
            fprintf(stderr, "[ADB] Invalid magic: cmd=0x%08X magic=0x%08X\n", msg->command, msg->magic);
            // Не дропаємо — можливо це не ADB message, а вже фрагментований H.264?
            // Але bulk ep 0x01 має бути ТІЛЬКИ ADB, тому дропаємо
            return false;
        }
        const char* cmd_str = adb_cmd_to_str(msg->command);
        printf("[ADB] Host->Device %s arg0=%u arg1=%u len=%u\n",
               cmd_str, msg->arg0, msg->arg1, msg->data_length);

        // Обробка A_CNXN
        if (msg->command == A_CNXN) {
            // Host надсилає: version=0x01000001, maxdata=4096 або 16384, payload="host::\0"
            // Зберігаємо maxdata для фрагментації відповідей
            maxdata_ = msg->arg1;
            if (maxdata_ == 0) maxdata_ = ADB_MAXDATA;
            printf("[ADB] A_CNXN maxdata=%u\n", maxdata_);
            // Ставимо стан
            state_ = State::CNXN_SENT;
            // Форвардимо як є через QUIC на телефон (не модифікуємо)
        } else if (msg->command == A_OPEN) {
            // A_OPEN: arg0=local_id, arg1=0, data="shell:scrcpy" або "abb:scrcpy"
            // Логуємо, щоб знати який сервіс відкриває scrcpy
            if (len >= sizeof(AdbMessage) + msg->data_length) {
                const char* service = reinterpret_cast<const char*>(data + sizeof(AdbMessage));
                printf("[ADB] A_OPEN service='%.*s'\n", (int)msg->data_length, service);
            }
        } else if (msg->command == A_AUTH) {
            // A_AUTH: RSA ключ. При першому підключенні телефон надсилає A_AUTH,
            // Host має відповісти A_AUTH з підписаним токеном (adbkey).
            // Ми просто проксіюємо — Windows adb має свій adbkey.pub
            printf("[ADB] A_AUTH (RSA handshake)\n");
        }
        // Всі інші (A_WRTE, A_OKAY, A_CLSE) — просто форвардимо
        return true;
    }
    return false;
}

bool AdbSpoof::handle_incoming(const uint8_t* data, size_t len, uint32_t ep) {
    // Device -> Host (IN на ep 0x81)
    if (ep != 0x81) return false;
    if (len < sizeof(AdbMessage)) {
        fprintf(stderr, "[ADB] Bulk IN too short: %zu\n", len);
        return false;
    }
    const AdbMessage* msg = reinterpret_cast<const AdbMessage*>(data);
    if (!adb_validate(msg)) {
        fprintf(stderr, "[ADB] IN Invalid magic\n");
        return false;
    }
    printf("[ADB] Device->Host %s arg0=%u arg1=%u len=%u\n",
           adb_cmd_to_str(msg->command), msg->arg0, msg->arg1, msg->data_length);

    if (msg->command == A_CNXN) {
        // Відповідь від телефону: version, maxdata, payload="device::ro.product.model=..."
        maxdata_ = msg->arg1;
        state_ = State::CONNECTED;
        printf("[ADB] Connected! Device maxdata=%u\n", maxdata_);
        if (len >= sizeof(AdbMessage) + msg->data_length) {
            const char* banner = reinterpret_cast<const char*>(data + sizeof(AdbMessage));
            printf("[ADB] Banner: '%.*s'\n", (int)msg->data_length, banner);
        }
    } else if (msg->command == A_AUTH) {
        // Телефон просить авторизацію — Host має показати діалог
        // На Windows adb покаже "Allow USB debugging?"
        printf("[ADB] Device requests AUTH — user must confirm on phone\n");
        state_ = State::AUTH_PENDING;
    }
    return true;
}

bool AdbSpoof::handle_control(const uint8_t* data, size_t len) {
    // Control transfers — 8-byte setup packet
    if (len < 8) return false;
    // Setup packet: bmRequestType, bRequest, wValue, wIndex, wLength (всі LE)
    uint8_t  bmRequestType = data[0];
    uint8_t  bRequest      = data[1];
    uint16_t wValue        = data[2] | (data[3] << 8);
    uint16_t wIndex        = data[4] | (data[5] << 8);
    uint16_t wLength       = data[6] | (data[7] << 8);

    printf("[ADB] Control bmRT=0x%02X bReq=0x%02X wVal=0x%04X wIdx=0x%04X wLen=%u\n",
           bmRequestType, bRequest, wValue, wIndex, wLength);

    // Стандартні запити:
    // - GET_DESCRIPTOR (0x06): wValue = descriptor type <<8 | index
    //   type 1=DEVICE, 2=CONFIG, 3=STRING
    if (bRequest == 0x06) { // GET_DESCRIPTOR
        uint8_t desc_type = wValue >> 8;
        uint8_t desc_idx  = wValue & 0xFF;
        printf("[ADB] GET_DESCRIPTOR type=%u idx=%u\n", desc_type, desc_idx);
        // Відповідь генерується в get_descriptor_response() — викликається з vhci_injector
        // коли приходить USBIP_RET_SUBMIT для ep0
    } else if (bRequest == 0x09) { // SET_CONFIGURATION
        printf("[ADB] SET_CONFIGURATION %u\n", wValue);
    }
    return true;
}

// Генерація відповіді на GET_DESCRIPTOR (викликається коли Host просить дескриптор)
size_t AdbSpoof::get_descriptor_response(uint8_t desc_type, uint8_t desc_idx,
                                         uint8_t* out, size_t out_max) {
    const uint8_t* src = nullptr;
    size_t src_len = 0;

    switch (desc_type) {
        case 1: // DEVICE
            src = USB_DEVICE_DESCRIPTOR;
            src_len = sizeof(USB_DEVICE_DESCRIPTOR);
            break;
        case 2: // CONFIG
            src = USB_CONFIG_DESCRIPTOR;
            src_len = sizeof(USB_CONFIG_DESCRIPTOR);
            break;
        case 3: // STRING
            switch (desc_idx) {
                case 0: { // LANGID (English US)
                    static const uint8_t lang[] = { 0x04, 0x03, 0x09, 0x04 };
                    src = lang; src_len = sizeof(lang); break;
                }
                case 1: src = USB_STRING_MANUFACTURER; src_len = sizeof(USB_STRING_MANUFACTURER); break;
                case 2: src = USB_STRING_PRODUCT; src_len = sizeof(USB_STRING_PRODUCT); break;
                case 3: src = USB_STRING_SERIAL; src_len = sizeof(USB_STRING_SERIAL); break;
                default: return 0;
            }
            break;
        default:
            return 0;
    }

    size_t copy = (src_len < out_max) ? src_len : out_max;
    memcpy(out, src, copy);
    printf("[ADB] Descriptor response type=%u idx=%u len=%zu\n", desc_type, desc_idx, copy);
    return copy;
}
