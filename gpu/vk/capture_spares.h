// The capture spares' byte budget (gpu/vk/vk_memory.cpp: cap_spare_max): retired capture buffers kept for the next captures.
// A scene load captures hundreds of MB; the retired 40-60 MB buffers used to stay as spares for good on a card over 6.5 GB,
// up to 12 of them (768 MB of host memory). A few large ones, or the small ones gameplay's captures take, are enough.
// 32 MB on a card of 6.5 GB or less (the 6 GB cards), 256 MB otherwise. RX_VK_CAP_SPARE_MB=<n>: n MB, 0: unbounded, as before.
#pragma once
#include <cstdint>

namespace capture_spares {
inline uint64_t max_bytes(bool vram_known, uint64_t allowed) {
    return (vram_known && allowed <= (6ull << 30) + (512ull << 20) ? 32ull : 256ull) << 20;
}
} // namespace capture_spares
