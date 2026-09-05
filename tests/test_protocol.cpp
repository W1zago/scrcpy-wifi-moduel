#include "../src/common/protocol.h"
#include <cstdio>
#include <cassert>
#include <cstring>

int main() {
    printf("=== test_protocol ===\n");

    // Test 1: ADB checksum
    {
        const char* data = "host::";
        uint32_t sum = adb_checksum((uint8_t*)data, strlen(data));
        // 'h'(104)+'o'(111)+'s'(115)+'t'(116)+':'(58)+':'(58) = 562
        uint32_t expected = 104+111+115+116+58+58; // =562
        printf("Test 1: ADB checksum 'host::' = %u expected %u\n", sum, expected);
        assert(sum == 562);
        assert(sum == expected);
    }

    // Test 2: ADB validate
    {
        AdbMessage msg = {};
        msg.command = htole32(A_CNXN);
        msg.magic = htole32(A_CNXN ^ 0xFFFFFFFFu);
        assert(adb_validate(&msg));
        msg.magic = 0;
        assert(!adb_validate(&msg));
        printf("Test 2: ADB validate PASSED\n");
    }

    // Test 3: WAN header hton/ntoh
    {
        WanHeader h = {};
        h.magic = WAN_MAGIC;
        h.timestamp_us = 1234567890123ull;
        h.fec_group = 42;
        h.fec_index = 5;
        h.flags = WAN_FLAG_FEC_PARITY;
        WanHeader orig = h;
        wan_header_hton(&h);
        wan_header_ntoh(&h);
        assert(h.magic == orig.magic);
        assert(h.timestamp_us == orig.timestamp_us);
        assert(h.fec_group == orig.fec_group);
        printf("Test 3: WAN header hton/ntoh PASSED\n");
    }

    // Test 4: USB descriptors size
    {
        assert(sizeof(USB_DEVICE_DESCRIPTOR) == 18);
        assert(sizeof(USB_CONFIG_DESCRIPTOR) == 32);
        printf("Test 4: USB descriptors PASSED\n");
    }

    // Test 5: USBIP header sizes
    {
        assert(sizeof(UsbIpHeaderBase) == 12);
        assert(sizeof(UsbIpSubmit) == 36); // 6*4 + 8 + 4
        // 12 + 36 = 48 для SUBMIT
        printf("Test 5: USBIP headers size PASSED (base=%zu submit=%zu)\n",
               sizeof(UsbIpHeaderBase), sizeof(UsbIpSubmit));
    }

    printf("All protocol tests passed!\n");
    return 0;
}
