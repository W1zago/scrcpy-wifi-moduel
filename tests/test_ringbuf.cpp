#include "../src/common/ring_buffer.h"
#include <cstdio>
#include <cassert>

int main() {
    printf("=== test_ringbuf ===\n");

    RingBuffer<int, 8> rb;
    assert(rb.empty());
    assert(rb.size() == 0);

    // Push 5
    for (int i = 0; i < 5; ++i) {
        assert(rb.push(i * 10));
    }
    assert(rb.size() == 5);

    // Pop 3
    for (int i = 0; i < 3; ++i) {
        int v;
        assert(rb.pop(v));
        assert(v == i * 10);
    }
    assert(rb.size() == 2);

    // Push до переповнення (capacity=7 для 8)
    for (int i = 0; i < 5; ++i) {
        assert(rb.push(100 + i));
    }
    assert(rb.size() == 7);
    assert(!rb.push(999)); // full

    // Pop all
    int v;
    assert(rb.pop(v) && v == 30);
    assert(rb.pop(v) && v == 40);
    for (int i = 0; i < 5; ++i) {
        assert(rb.pop(v) && v == 100 + i);
    }
    assert(rb.empty());
    assert(!rb.pop(v));

    printf("RingBuffer test PASSED\n");

    // UrbPool
    UrbPool pool;
    assert(pool.used() == 0);
    UrbSlot* s1 = pool.acquire();
    assert(s1 != nullptr);
    assert(pool.used() == 1);
    UrbSlot* s2 = pool.acquire();
    assert(s2 != nullptr);
    assert(pool.used() == 2);
    pool.release(s1);
    assert(pool.used() == 1);
    pool.release(s2);
    assert(pool.used() == 0);
    printf("UrbPool test PASSED\n");

    printf("All ringbuf tests passed!\n");
    return 0;
}
