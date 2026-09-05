#pragma once
/**
 * protocol.h — Єдине джерело правди для мережевого протоколу Virtual USB Cable
 * 
 * Базується на USB/IP (linux/usbip.h) + WAN розширення для QUIC/TCP.
 * Всі поля в NETWORK BYTE ORDER (big-endian) при передачі, крім payload.
 * 
 * Memory layout: всі структури packed, вирівнювання 1, щоб уникнути padding-дірок
 * які зламають парсинг на різних компіляторах (MSVC vs GCC/Clang NDK).
 */

#include <cstdint>
#include <cstddef>
#include <cstring>

#if defined(_MSC_VER)
    #define PACKED_STRUCT(name) __pragma(pack(push, 1)) struct name __pragma(pack(pop))
    #define PACKED
#else
    #define PACKED_STRUCT(name) struct __attribute__((packed)) name
    #define PACKED __attribute__((packed))
#endif

// ============================================================================
// 1. USB/IP Base Protocol (сумісність з linux/usbip та usbip-win2)
// ============================================================================

// Версія протоколу — має збігатися з ядром Linux (0x0111)
static constexpr uint32_t USBIP_VERSION = 0x0111;

// Команди USB/IP
enum UsbIpCommand : uint32_t {
    USBIP_CMD_SUBMIT      = 0x00000001, // Host -> Device: URB submit
    USBIP_CMD_UNLINK      = 0x00000002, // Host -> Device: скасувати URB
    USBIP_RET_SUBMIT      = 0x00000003, // Device -> Host: відповідь на SUBMIT
    USBIP_RET_UNLINK      = 0x00000004, // Device -> Host: відповідь на UNLINK
    // Наші WAN-розширення (0x8000+ щоб не конфліктувати)
    USBIP_CMD_HELLO       = 0x80000001, // Handshake + TLS
    USBIP_CMD_PING        = 0x80000002, // Keepalive (кожні 500мс)
    USBIP_CMD_PONG        = 0x80000003,
    USBIP_CMD_RESTORE     = 0x80000004, // Replay після реконекту
    USBIP_CMD_FEC_GROUP   = 0x80000005, // FEC група для відео
};

// Базовий заголовок USB/IP (24 bytes) — як в ядрі
PACKED_STRUCT(UsbIpHeaderBase) {
    uint32_t version;   // USBIP_VERSION (BE)
    uint32_t command;   // UsbIpCommand (BE)
    uint32_t status;    // 0=OK, інакше Linux errno (BE)
};

// Заголовок SUBMIT (додатково 24 bytes після base = 48 total для SUBMIT)
// Це те, що очікує vhci-hcd та usbip-win2
PACKED_STRUCT(UsbIpSubmit) {
    uint32_t seqnum;            // монотонний лічильник (BE)
    uint32_t devid;             // (busnum << 16) | devnum (BE)
    uint32_t direction;         // 0=OUT (Host->Device), 1=IN (BE)
    uint32_t ep;                // endpoint (0x00=ep0 OUT, 0x81=ep1 IN) (BE)
    uint32_t transfer_flags;    // URB flags (BE)
    uint32_t transfer_buffer_length; // довжина payload (BE)
    uint32_t start_frame;       // для isochronous (0 для bulk) (BE)
    uint32_t number_of_packets; // для isochronous (0 для bulk) (BE)
    uint32_t interval;          // для interrupt (0 для bulk) (BE)
    uint8_t  setup[8];          // Setup пакет для Control (8 bytes)
};

PACKED_STRUCT(UsbIpRetSubmit) {
    uint32_t seqnum;            // має збігатися з SUBMIT (BE)
    uint32_t devid;             // (BE)
    uint32_t direction;         // (BE)
    uint32_t ep;                // (BE)
    uint32_t status;            // 0=OK, -EPIPE, -ETIMEDOUT etc. (BE, sign-extended)
    uint32_t actual_length;     // скільки байт реально передано (BE)
    uint32_t start_frame;       // (BE)
    uint32_t number_of_packets; // (BE)
    uint32_t error_count;       // (BE)
    uint64_t setup;             // padding / setup echo (8 bytes)
};

// ============================================================================
// 2. WAN Extension Header (16 bytes, додається ПЕРЕД USBIP header при WAN)
// ============================================================================
// Навіщо: додає timestamp, FEC, flags без порушення сумісності з чистим USBIP
// На LAN можна відправляти чистий USBIP (без WAN header), на WAN — з ним.
// Визначаємо за першими 4 байтами: якщо version==0x57414E01 ("WAN1") — це WAN.

static constexpr uint32_t WAN_MAGIC = 0x57414E01; // "WAN1" LE

PACKED_STRUCT(WanHeader) {
    uint32_t magic;         // WAN_MAGIC (BE) — маркер WAN-розширення
    uint64_t timestamp_us;  // QPC / clock_gettime (BE) — для jitter buffer
    uint32_t fec_group;     // ID групи FEC (0=не відео, >0=відео) (BE)
    uint16_t fec_index;     // індекс пакету в групі (BE)
    uint16_t flags;         // WanFlags (BE)
};

enum WanFlags : uint16_t {
    WAN_FLAG_NONE        = 0x0000,
    WAN_FLAG_RETRANSMIT  = 0x0001, // повторна відправка (replay)
    WAN_FLAG_FEC_PARITY  = 0x0002, // цей пакет — паритет FEC
    WAN_FLAG_KEYFRAME    = 0x0004, // H.264 I-frame (пріоритет)
    WAN_FLAG_COMPRESSED  = 0x0008, // payload стиснуто LZ4
};

// Повний WAN пакет: [WanHeader(16)][UsbIpHeaderBase(12)][UsbIpSubmit/RetSubmit(36)][payload...]
// Максимальний розмір: 16+48+16384 = 16448 + QUIC overhead

// ============================================================================
// 3. ADB Protocol (system/core/adb/protocol.txt)
// ============================================================================

static constexpr uint32_t ADB_VERSION = 0x01000001; // 1.0.0
static constexpr uint32_t ADB_MAXDATA = 4096;       // legacy
static constexpr uint32_t ADB_MAXDATA_V2 = 256 * 1024; // modern adb з large packets

// Команди ADB (little-endian в дроті! На відміну від USBIP)
enum AdbCommand : uint32_t {
    A_SYNC = 0x434e5953, // 'SYNC'
    A_CNXN = 0x4e584e43, // 'CNXN'
    A_OPEN = 0x4e45504f, // 'OPEN'
    A_OKAY = 0x59414b4f, // 'OKAY'
    A_CLSE = 0x45534c43, // 'CLSE'
    A_WRTE = 0x45545257, // 'WRTE'
    A_AUTH = 0x48545541, // 'AUTH' (RSA)
};

PACKED_STRUCT(AdbMessage) {
    uint32_t command;       // AdbCommand (LE)
    uint32_t arg0;          // (LE)
    uint32_t arg1;          // (LE)
    uint32_t data_length;   // (LE)
    uint32_t data_checksum; // сума байтів data (LE)
    uint32_t magic;         // command ^ 0xFFFFFFFF (LE)
};
// Після AdbMessage йде data[data_length] (якщо >0)

inline uint32_t adb_checksum(const uint8_t* data, size_t len) {
    uint32_t sum = 0;
    for (size_t i = 0; i < len; ++i) sum += data[i];
    return sum;
}

inline bool adb_validate(const AdbMessage* msg) {
    return msg->magic == (msg->command ^ 0xFFFFFFFFu);
}

inline const char* adb_cmd_to_str(uint32_t cmd) {
    switch (cmd) {
        case A_SYNC: return "A_SYNC";
        case A_CNXN: return "A_CNXN";
        case A_OPEN: return "A_OPEN";
        case A_OKAY: return "A_OKAY";
        case A_CLSE: return "A_CLSE";
        case A_WRTE: return "A_WRTE";
        case A_AUTH: return "A_AUTH";
        default: return "UNKNOWN";
    }
}

// ============================================================================
// 4. USB Descriptors (синтетичні, щоб Windows повірив що це фізичний пристрій)
// ============================================================================

// Device Descriptor (18 bytes) — Google Pixel 7, ADB enabled
static constexpr uint8_t USB_DEVICE_DESCRIPTOR[18] = {
    0x12,       // bLength
    0x01,       // bDescriptorType = DEVICE
    0x00, 0x02, // bcdUSB = 2.0
    0x00,       // bDeviceClass = 0 (defined at interface)
    0x00,       // bDeviceSubClass
    0x00,       // bDeviceProtocol
    0x40,       // bMaxPacketSize0 = 64
    0xD1, 0x18, // idVendor = 0x18D1 (Google)
    0xE7, 0x4E, // idProduct = 0x4EE7 (ADB)
    0x04, 0x04, // bcdDevice = 4.04
    0x01,       // iManufacturer = 1
    0x02,       // iProduct = 2
    0x03,       // iSerialNumber = 3
    0x01        // bNumConfigurations = 1
};

// Config Descriptor (32 bytes) — 1 config, 1 interface (ADB), 2 endpoints
static constexpr uint8_t USB_CONFIG_DESCRIPTOR[32] = {
    // Config
    0x09,       // bLength
    0x02,       // bDescriptorType = CONFIG
    0x20, 0x00, // wTotalLength = 32
    0x01,       // bNumInterfaces = 1
    0x01,       // bConfigurationValue = 1
    0x00,       // iConfiguration = 0
    0x80,       // bmAttributes = bus powered
    0xFA,       // bMaxPower = 500mA
    // Interface (ADB)
    0x09,       // bLength
    0x04,       // bDescriptorType = INTERFACE
    0x00,       // bInterfaceNumber = 0
    0x00,       // bAlternateSetting = 0
    0x02,       // bNumEndpoints = 2
    0xFF,       // bInterfaceClass = Vendor Specific (0xFF)
    0x42,       // bInterfaceSubClass = 0x42 (ADB)
    0x01,       // bInterfaceProtocol = 1 (ADB)
    0x00,       // iInterface = 0
    // Endpoint 1 OUT (BULK)
    0x07,       // bLength
    0x05,       // bDescriptorType = ENDPOINT
    0x01,       // bEndpointAddress = 0x01 (OUT, ep1)
    0x02,       // bmAttributes = BULK
    0x00, 0x02, // wMaxPacketSize = 512
    0x00,       // bInterval = 0
    // Endpoint 1 IN (BULK)
    0x07,       // bLength
    0x05,       // bDescriptorType = ENDPOINT
    0x81,       // bEndpointAddress = 0x81 (IN, ep1)
    0x02,       // bmAttributes = BULK
    0x00, 0x02, // wMaxPacketSize = 512
    0x00        // bInterval = 0
};

// String Descriptors (UTF16-LE)
static constexpr uint8_t USB_STRING_MANUFACTURER[] = {
    0x0E, 0x03, 'G',0,'o',0,'o',0,'g',0,'l',0,'e',0 // "Google"
};
static constexpr uint8_t USB_STRING_PRODUCT[] = {
    0x12, 0x03, 'P',0,'i',0,'x',0,'e',0,'l',0,' ',0,'7',0 // "Pixel 7"
};
static constexpr uint8_t USB_STRING_SERIAL[] = {
    0x1A, 0x03, 'V',0,'I',0,'R',0,'T',0,'U',0,'A',0,'L',0,'0',0,'0',0,'1',0 // "VIRTUAL001"
};

// ============================================================================
// 5. Константи транспорту
// ============================================================================

static constexpr size_t MAX_URB_PAYLOAD = 16384;      // макс. один URB
static constexpr size_t MAX_WAN_PACKET = 16448;       // WAN header + URB
static constexpr size_t URB_POOL_SIZE = 64;           // кількість буферів в pool
static constexpr size_t JITTER_BUFFER_MS = 30;        // затримка для згладжування
static constexpr size_t KEEPALIVE_MS = 500;           // ping інтервал
static constexpr size_t GHOST_TIMEOUT_MS = 30000;     // скільки тримати GHOST
static constexpr size_t QUIC_MAX_STREAMS = 3;         // Control, Bulk, Video
static constexpr uint32_t DEFAULT_QUIC_PORT = 22777;  // порт за замовчуванням
static constexpr uint32_t DEFAULT_ADB_PORT = 5555;    // adb tcpip порт

// ============================================================================
// 6. Утиліти byte order (щоб не тягнути <arpa/inet.h> на Windows)
// ============================================================================

inline uint16_t swap16(uint16_t x) {
    return (uint16_t)(((x & 0xFF00u) >> 8) | ((x & 0x00FFu) << 8));
}
inline uint32_t swap32(uint32_t x) {
    return ((x & 0xFF000000u) >> 24) | ((x & 0x00FF0000u) >> 8) |
           ((x & 0x0000FF00u) << 8)  | ((x & 0x000000FFu) << 24);
}
inline uint64_t swap64(uint64_t x) {
    return ((x & 0xFF00000000000000ull) >> 56) | ((x & 0x00FF000000000000ull) >> 40) |
           ((x & 0x0000FF0000000000ull) >> 24) | ((x & 0x00FF0000000000ull) >> 8)  |
           ((x & 0x000000FF00000000ull) << 8)  | ((x & 0x00000000FF000000ull) << 24) |
           ((x & 0x0000000000FF0000ull) << 40) | ((x & 0x00000000000000FFull) << 56);
}
#if defined(_WIN32) || defined(__LITTLE_ENDIAN__) || defined(__x86_64__) || defined(_M_X64) || defined(_M_IX86)
    inline uint16_t hton16(uint16_t x) { return swap16(x); }
    inline uint16_t ntoh16(uint16_t x) { return swap16(x); }
    inline uint32_t hton32(uint32_t x) { return swap32(x); }
    inline uint32_t ntoh32(uint32_t x) { return swap32(x); }
    inline uint64_t hton64(uint64_t x) { return swap64(x); }
    inline uint64_t ntoh64(uint64_t x) { return swap64(x); }
    // ADB little-endian — на little-endian host нічого не робимо
    inline uint32_t htole32(uint32_t x) { return x; }
    inline uint32_t letoh32(uint32_t x) { return x; }
#else
    inline uint16_t hton16(uint16_t x) { return x; }
    inline uint16_t ntoh16(uint16_t x) { return x; }
    inline uint32_t hton32(uint32_t x) { return x; }
    inline uint32_t ntoh32(uint32_t x) { return x; }
    inline uint64_t hton64(uint64_t x) { return x; }
    inline uint64_t ntoh64(uint64_t x) { return x; }
    inline uint32_t htole32(uint32_t x) { return swap32(x); }
    inline uint32_t letoh32(uint32_t x) { return swap32(x); }
#endif

// Хелпер для серіалізації WanHeader в BE
inline void wan_header_hton(WanHeader* h) {
    h->magic = hton32(h->magic);
    h->timestamp_us = hton64(h->timestamp_us);
    h->fec_group = hton32(h->fec_group);
    h->fec_index = hton16(h->fec_index);
    h->flags = hton16(h->flags);
}
inline void wan_header_ntoh(WanHeader* h) {
    h->magic = ntoh32(h->magic);
    h->timestamp_us = ntoh64(h->timestamp_us);
    h->fec_group = ntoh32(h->fec_group);
    h->fec_index = ntoh16(h->fec_index);
    h->flags = ntoh16(h->flags);
}
