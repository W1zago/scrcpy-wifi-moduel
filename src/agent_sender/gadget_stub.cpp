/**
 * gadget_stub.cpp — Tier-A референс: як би виглядав Agent-Sender з root/Pi (FunctionFS)
 * 
 * ЦЕЙ ФАЙЛ НЕ КОМПІЛЮЄТЬСЯ В PoC (No-Root), АЛЕ ПОКАЗУЄ ПОВНУ ЛОГІКУ ДЛЯ МАЙБУТНЬОГО АПГРЕЙДУ.
 * Якщо отримаєте root або Raspberry Pi Zero — замініть adb_bridge.cpp на цей файл.
 * 
 * Логіка FunctionFS:
 *  - Відкриває /dev/usb-ffs/adb/ep0, ep1, ep2
 *  - Читає сирі URB (setup пакет + bulk) напряму з ядра
 *  - Інкапсулює в USBIP і шле через QUIC
 *  - На відміну від adb_bridge, тут ми бачимо СПРАВЖНІ USB дескриптори, а не ADB messages
 */

#include <cstdio>

#if 0 // Вимкнено для No-Root PoC — вмикається при #define USE_GADGET

#include "../common/protocol.h"
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/usb/functionfs.h>
#include <cstdio>

// FunctionFS заголовки (linux/usb/functionfs.h)
struct GadgetSender {
    int ep0_fd = -1; // /dev/usb-ffs/adb/ep0 — control
    int ep1_out_fd = -1; // ep1 bulk OUT
    int ep2_in_fd = -1;  // ep2 bulk IN

    bool open_gadget() {
        // Монтуємо configfs (потрібен root)
        // mkdir /config/usb_gadget/g1
        // echo 0x18D1 > idVendor; echo 0x4EE7 > idProduct
        // mkdir functions/ffs.adb; ln -s functions/ffs.adb configs/c.1
        // echo `ls /sys/class/udc` > UDC

        ep0_fd = open("/dev/usb-ffs/adb/ep0", O_RDWR);
        if (ep0_fd < 0) { perror("open ep0"); return false; }

        // Пишемо дескриптори в ep0 (FunctionFS протокол)
        struct {
            struct usb_functionfs_descs_head_v2 header;
            __le32 fs_count;
            __le32 hs_count;
            struct {
                struct usb_interface_descriptor intf;
                struct usb_endpoint_descriptor_no_audio bulk_out;
                struct usb_endpoint_descriptor_no_audio bulk_in;
            } __attribute__((packed)) fs_descs, hs_descs;
        } __attribute__((packed)) descs = {};

        descs.header.magic = cpu_to_le32(FUNCTIONFS_DESCRIPTORS_MAGIC_V2);
        descs.header.length = cpu_to_le32(sizeof(descs));
        descs.header.flags = cpu_to_le32(FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC);
        descs.fs_count = cpu_to_le32(3);
        descs.hs_count = cpu_to_le32(3);
        // ... заповнити дескриптори з protocol.h: USB_DEVICE_DESCRIPTOR etc.

        if (write(ep0_fd, &descs, sizeof(descs)) < 0) {
            perror("write descs");
            return false;
        }

        // Відкриваємо bulk endpoints
        ep1_out_fd = open("/dev/usb-ffs/adb/ep1", O_RDWR);
        ep2_in_fd = open("/dev/usb-ffs/adb/ep2", O_RDWR);
        if (ep1_out_fd < 0 || ep2_in_fd < 0) {
            perror("open bulk");
            return false;
        }
        printf("[GADGET] FunctionFS opened (ep0=%d ep1=%d ep2=%d)\n", ep0_fd, ep1_out_fd, ep2_in_fd);
        return true;
    }

    void loop(int wan_sock) {
        // Потік 1: ep1 OUT -> WAN (читаємо URB від Host через USB, шлемо в мережу)
        // Кожен read(ep1_out_fd) = один URB bulk OUT (до 16384)
        uint8_t buf[MAX_URB_PAYLOAD];
        while (true) {
            ssize_t n = read(ep1_out_fd, buf, sizeof(buf));
            if (n < 0) { perror("read ep1"); break; }
            // Загортаємо в USBIP SUBMIT і відправляємо в WAN
            UsbIpHeaderBase base = {};
            base.version = hton32(USBIP_VERSION);
            base.command = hton32(USBIP_CMD_SUBMIT);
            UsbIpSubmit sub = {};
            sub.seqnum = hton32(1); // монотонний
            sub.ep = hton32(0x01);
            sub.direction = hton32(0);
            sub.transfer_buffer_length = hton32(n);
            // Відправити через wan_sock з WanHeader
        }
    }

    void close_gadget() {
        if (ep0_fd >= 0) close(ep0_fd);
        if (ep1_out_fd >= 0) close(ep1_out_fd);
        if (ep2_in_fd >= 0) close(ep2_in_fd);
    }
};

#endif

// Заглушка для компіляції в No-Root режимі
void gadget_stub_info() {
    printf("[GADGET] Tier-A (FunctionFS) not compiled — requires root/Pi\n");
    printf("[GADGET] See gadget_stub.cpp for reference implementation\n");
    printf("[GADGET] To enable: define USE_GADGET and run as root\n");
}
