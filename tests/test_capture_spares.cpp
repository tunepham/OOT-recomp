// The capture spares' budget (gpu/vk/capture_spares.h): tests/test_capture_spares.py compiles and runs this.
#include "../gpu/vk/capture_spares.h"

#include <cstdio>
#include <cstdlib>

#define CHECK(x) do { if (!(x)) { std::printf("FAIL line %d: %s\n", __LINE__, #x); std::exit(1); } } while (0)

int main() {
    using namespace capture_spares;
    const uint64_t MB = 1ull << 20, GB = 1ull << 30;
    CHECK(max_bytes(true, 5 * GB) == 32 * MB);                      // (a 6 GB card's allowance)
    CHECK(max_bytes(true, 6 * GB + 512 * MB) == 32 * MB);           // (the line itself: the small budget)
    CHECK(max_bytes(true, 6 * GB + 512 * MB + 1) == 256 * MB);      // (past it: the large one)
    CHECK(max_bytes(true, 12 * GB) == 256 * MB);                    // (a 12 GB card: not the old 768 MB)
    CHECK(max_bytes(false, 0) == 256 * MB);                         // (no allowance known: the conservative budget)
    std::printf("ok\n");
    return 0;
}
