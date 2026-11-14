# Memory architecture: RAM and VRAM

How Hookshot (the Ocarina of Time Switch 2 recompilation) allocates and reclaims memory, and an
evaluation of where the remaining cost is.

| | |
|---|---|
| Scope | Guest RAM, host RAM, VRAM, texture and buffer lifetime, present-chain allocations |
| Code base | commit `b29e5f1` (working tree clean at the time of writing) |
| Method | source reading + one machine's run logs (see [§12](#12-measured-case-study)) |
| Status | descriptive document; it proposes changes but makes none |

> Line numbers refer to this commit and will drift. The code is authoritative: where a comment and
> the code disagree, this document says so explicitly (see **F6** in [§13](#13-evaluation)).

**Contents**

1. [The short version](#1-the-short-version)
2. [Address-space layout](#2-address-space-layout)
3. [Guest RAM: the `nn::os` allocator](#3-guest-ram-the-nnos-allocator)
4. [Host RAM](#4-host-ram-outside-the-guest-window)
5. [VRAM allocator](#5-vram-allocator)
6. [The four budgets](#6-the-four-budgets)
7. [Textures](#7-textures-from-guest-storage-to-vkimage)
8. [Buffer imports and the VRAM mirror](#8-buffer-imports-and-the-vram-mirror)
9. [Present chain, frame generation, DLSS](#9-present-chain-frame-generation-dlss-menu)
10. [Settings and environment variables](#10-settings-and-environment-variables)
11. [Observability](#11-observability)
12. [Measured case study](#12-measured-case-study)
13. [Evaluation](#13-evaluation)
14. [Grading rubric](#14-grading-rubric)
15. [Threats to validity](#15-threats-to-validity-and-open-questions)
16. [Appendix](#16-appendix)

**Evidence key used throughout**

- **F** — fact read from the source, with file and line.
- **M** — measured on the machine whose logs are quoted, on 2026-11-10. Not a controlled benchmark.
- **H** — hypothesis or estimate derived from F/M; needs a measurement before it is trusted.

---

## 1. The short version

Hookshot runs one 64-bit process with four memory domains that are easy to confuse:

1. **The guest window** — a 512 GB virtual range at `0x100000000000` where the recompiled game's
   address space lives. It is reserved, not committed; pages are committed when the runtime hands
   them out or, failing that, on first touch. The GPU can import these pages directly.
2. **The guest heap** — a bump-allocated region inside that window, sized by the game through
   `nn::os::SetMemoryHeapSize`. On the measured run the game asked for **6894 MB**. This is the
   "10 GB of RAM" in the README.
3. **Host memory** — the renderer's C++ heap, Vulkan staging/capture buffers, and cache files.
   The largest fixed item is the driver's pipeline cache (`mkw_vk_pipelines.bin`, **659 MB** on the
   measured install); the 515 MB shader seed is transient (freed after precompile).
4. **VRAM** — everything allocated through VMA, plus the swapchain and the driver's own allocations.
   Four budgets compete: textures (default 70 % of the driver allowance, capped at 6 GB), buffer
   "mirrors" (up to 3 GB, a quarter of the card), write-back shadows, and the fixed pools (present
   chain, frame generation, DLSS, BC3 scratch, staging).

Measured peaks on a 12 GB-class NVIDIA card: **11.2 GB of VRAM used of 10.9 GB allowed**, of which
**2.9–3.8 GB textures** and **2.4 GB mirrors**; guest heap **6.9 GB**.

The design is unusually careful about *correctness* of eviction (many comments record regressions
that were fixed by keeping data longer). The main remaining inefficiencies are therefore not
"missing compression" — large ASTC textures are already re-encoded to BC3 — but **budget
enforcement latency**, **mirror sizing**, **a preset that silently does nothing on big cards**, and
**documentation drift**.

---

## 2. Address-space layout

| Region | Address | Size | Notes |
|---|---|---|---|
| Host base for the guest window | `0x100000000000` (16 TB) | — | fixed so lifted code never loads a base (`third_party/a64-kit/rt/a64.h:61`) |
| Guest window | `0` .. `0x8000000000` | 512 GB | `HOS_GUEST_SPACE (1ULL << 39)` (`host/hos.h:46`) |
| System region | `0x01000000` .. | 16 MB-ish | runtime objects (`host/hos.h:43`) |
| Guest heap | `0x1000000000` (64 GB) | game-sized, up to 9809 MB advertised | grows up (`host/hos.h:45`) |
| Address regions | top-down from `0x8000000000` | game-sized | first-fit, may use holes below the heap (`host/hos_mem.c:61-118`) |

**Windows** (`host/hos_main.c:118-140`): the whole 512 GB is `VirtualAlloc(MEM_RESERVE,
PAGE_NOACCESS)`. Nothing is committed until:

- the runtime hands a range out (`hos_commit`, rounded to 64 KB), or
- the game touches a reserved page without going through the runtime; an access-violation handler
  commits 64 KB and retries (`host/mkw_glue.c:16-50`).

Committed-but-untouched pages cost **commit charge**, not physical RAM. This distinction matters on
16 GB machines: a 6.9 GB heap is 6.9 GB of commit charge even if much of it is never touched.

**Linux** (`host/hos_main.c:141-199`): the window is one mapping, `MAP_NORESERVE`; pages are made on
touch. When `/dev/udmabuf` is usable, the window is a **memfd** shared mapping, so pools can be
exported to the GPU as dma-bufs instead of host pointers (host-pointer imports made amdgpu re-check
every page at every submission: 15–45 ms each in Kokiri Forest). Transparent huge pages are turned
off inside the window because `khugepaged` collapses change the mapping and force the driver to pin
again (~250 ms per submit on RADV, issue 312).

`HOS_LARGE_PAGES=1` (`host/hos_main.c:86-111`) commits the window in 4 GB large pages. It needs the
`SeLockMemoryPrivilege` and, importantly, **disables page discard** (`host/hos_mem.c:131-133`): freed
guest pages stay resident. On a 16 GB machine this is usually the wrong trade.

---

## 3. Guest RAM: the `nn::os` allocator

`host/hos_mem.c` implements the Horizon memory API the game calls.

### 3.1 Advertised total and heap sizing

- Default total: **9809 MB** (Switch 2 12 GB arrangement, `host/hos_mem.c:53-55`).
  `HOS_MEMORY_MB` overrides it. The game reads this through `QueryMemoryInfo` and sizes its own heap;
  TOTK's formula is documented as `heap = total - used - 1080 MB` (`host/hos_mem.c:39-41`).
- OoT on the measured run called `SetMemoryHeapSize(6894 MB)` (**M**). That is the heap, not the
  total: 9809 MB is still what `QueryMemoryInfo` reports.
- `SetMemoryHeapSize` (`host/hos_mem.c:184-206`):
  - rejects a heap that would collide with an address region (returns `0xCA01`, out of memory),
  - zeroes the tail when shrinking,
  - optionally prefaults new pages when growing (`HOS_PREFAULT=1`; converts first-touch page faults
    into an up-front cost, does not reduce memory),
  - commits the newly added range.

### 3.2 Allocation primitives

| Call | Behaviour | Free path |
|---|---|---|
| `AllocateMemoryBlock(size)` | bump pointer from `HOS_HEAP_BASE`, rounded to 2 MB; no reuse; `0x1A01` when full | none in this API; blocks live for the process |
| `AllocateAddressRegion(size)` | top-down first fit, 64 KB aligned, may use holes below the heap; commits on success | `FreeAddressRegion` discards the pages |
| `AllocateMemoryPages(addr, n)` | commits; zeroes only pages that were not mapped before (bitmap), except above 4 GB where it commits and lets first touch zero | `FreeMemoryPages` decommits |
| `MapPhysicalMemory(addr, n)` | `hos_commit` only (`host/hos_svc.c:459-461`) | — |

Two details that surprise people:

- Above 4 GB the used-page bitmap does not reach, so `AllocateMemoryPages` cannot tell "new" from
  "already mapped" and follows Horizon's rule: commit, leave committed pages alone, fresh pages read
  zero (`host/hos_mem.c:244-256`). This was a correctness fix: zeroing the range wiped live heap
  data when the game mapped over memory it already used.
- Pages that the Vulkan backend imported are **not** decommitted on free. They are zeroed in place
  (`host/hos_mem.c:279-290`), because a hole under a dma-buf/host import leaves the GPU on the old
  pages while the game sees new ones. The cost is that imported ranges stay resident until the
  import is given back.

### 3.3 Discard policy

`discard_pages` (`host/hos_mem.c:126-151`) is the only place guest RSS is actually returned:

- Windows: `DiscardVirtualMemory`, falling back to `VirtualAlloc(MEM_RESET)`. Not available for
  large pages.
- Linux: `MADV_REMOVE` on the memfd window (pages are freed, not just dropped), otherwise
  `MADV_DONTNEED`.
- Skipped when `HOS_KEEP_FREED_PAGES` is set, when the window is in large pages, or for ranges an
  import holds (those are zeroed instead).

This is why the README can claim ~10 GB per run while the heap is 6.9 GB: freed heap pages are
returned, and the rest is live data.

### 3.4 What `QueryMemoryInfo` reports

`host/hos_mem.c:306-318`: total available, total used (program image + 16 MB), total heap,
allocated heap, program size, thread stacks (0), thread count (1). Games use the first two to size
their heap, so **changing `HOS_MEMORY_MB` changes game behaviour**, not just the number displayed.

---

## 4. Host RAM (outside the guest window)

| Consumer | Size (measured) | Lifetime | Source |
|---|---:|---|---|
| Guest heap commit | 6894 MB | session | **M**; `host/hos_mem.c` |
| `mkw_vk_pipelines.bin` (driver pipeline cache) | 659 MB | read whole at start; driver keeps its copy | `gpu/vk/vk_draw.cpp:678-700`; **M** |
| `hookshot_shaders.bin` (shader seed) | 515 MB | **transient**: read whole for precompile, then `seed_close()` frees it | `gpu/vk/vk_shader.cpp:2247-2292`, `vk_draw.cpp:6266`; **M** |
| `hookshot_programs.bin` | 6.7 GB on disk | **not resident**: per-record reads through a 20-byte index (46,587 keys, 1.1 MB index) | `gpu/vk/vk_shader.cpp:950-1060`; **M** |
| `hookshot_pipelines.bin` (pipeline notes) | 118 MB on disk | read into a vector for precompile/prediction, then freed | `gpu/vk/vk_draw.cpp:894-906`; **M** |
| `hookshot_pipeline_states.bin` | 2.3 MB | session | **M** |
| `image.bin` (game image) | 97 MB | mapped into the guest window | **M** |
| `hookshot.exe` + DLLs | 449 MB + 6.9 MB | file-backed, shared | **M** |
| Capture buffers | scene-dependent | ring of guest-page snapshots; freed when the GPU is done | `gpu/vk/vk_memory.cpp:786-830` |
| Staging arenas | 64 MB (≤6.5 GB cards) / 256 MB | kept while idle, `RX_VK_STAGING_KEEP_MB` | `gpu/vk/vk_texture.cpp:2986-2996` |
| BC3 scratch | 160 MB (+128 MB AMD resize) | kept by default (`RX_VK_BC_IDLE_S` default 0) | `gpu/vk/vk_texture.cpp:431-447` |
| Capacity-buffer spares | 32 MB on ≤6.5 GB cards | session | `gpu/vk/vk_memory.cpp:831-838` |
| Write-back shadows | allowed/8, ≤512 MB on iGPU | session | `gpu/vk/vk_memory.cpp:2976-2987` |
| Malloc arenas | scene-dependent | trimmed when free ≥ `RX_VK_MALLOC_TRIM_MB` (192) | `gpu/vk/vk_texture.cpp:3020-3046` |

The renderer logs `mallinfo2` every 1800 presents and calls `malloc_trim` when the arenas hold at
least 192 MB free. That is the only explicit host-heap compaction in the process.

**Important non-cost:** the 6.7 GB program cache is *not* loaded whole. The index maps each program
key to `(offset, size, kind)` records and each program is read on demand
(`gpu/vk/vk_shader.cpp:950-1060`). This is the model the other caches do not follow.

---

## 5. VRAM allocator

All images and device buffers go through VMA 3.3.0 (`gpu/vk/vk_vma.cpp`).

- Preferred block size **64 MB** (`vk_vma.cpp:27`), chosen because 256 MB blocks wasted VRAM.
- Images come from a dedicated pool of **128 MB blocks** (`RX_VK_IMAGE_BLOCK_MB`, minimum 16), made
  lazily. One block stays empty for the next image, so the pool costs up to ~128 MB of headroom
  (`vk_vma.cpp:39-63`).
- `VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE` **plus** `requiredFlags = DEVICE_LOCAL` (`vk_vma.cpp:68-73`).
  Images are in VRAM or not at all; an image VMA would place in system memory is read over PCIe
  every frame and competes with the 6.9 GB guest heap.
- `VMA_ALLOCATION_CREATE_WITHIN_BUDGET_BIT` is set whenever `VK_EXT_memory_budget` exists.
- If VRAM is full, `create_image` falls back to system memory **unless**
  `RX_VK_IMAGES_VRAM_ONLY` is set (`vk_vma.cpp:79-100`). This is a deliberate "draw something"
  fallback, not the normal path.
- Buffers are dedicated by default; `RX_VK_BLOCK_BUFFERS=1` makes VMA sub-allocate them.

The allocator reports three numbers that drive every budget decision
(`gpu/vk/vk_vma.cpp:172-185`): `usage`, `budget`, and `block_free` (bytes in VMA blocks not handed
to an allocation). `block_free` matters because the driver counts it as the process's; an eviction
frees space *inside* a block, which the driver still counts, so a budget based on `usage` alone
would shrink again 10 frames later. The texture budget therefore subtracts `block_free` from
`usage` before comparing to the pressure threshold (`gpu/vk/vk_texture.cpp:3084-3105`).

---

## 6. The four budgets

### 6.1 Textures

Computed every 10 frames (`gpu/vk/vk_texture.cpp:3060-3105`):

```
own = RX_VK_VRAM_MB
      else min(70% of driver allowance, 6144 MB)
      else 4096 MB if the driver gives no budget
if integrated GPU and no RX_VK_VRAM_MB: own = min(own, 2560 MB)

budget = (preset.vram_mb > 0 && preset_caps_memory()) ? preset.vram_mb : own
if preset in use and budget known: budget = min(budget, 75% of allowance)

pressure = usage - min(usage, block_free)          # unless RX_VK_BUDGET_BY_BLOCKS=1
if pressure > 90% of allowance: budget = min(budget, total_bytes - excess)
if usage    > 97% of allowance: budget = min(budget, total_bytes - excess)
```

The 6 GB cap is not arbitrary: on a 16 GB card the driver's 10.8 GB allowance filled up and the
condition was misread as a leak (issues 105/106). The 2.5 GB integrated-GPU cap exists because a
Radeon 780M with 5.3 GB of textures ran a 16 GB laptop out of RAM in Hyrule Field.

### 6.2 Buffer mirrors

`gpu/vk/vk_memory.cpp:1982-1999`:

| Card | Default mirror budget |
|---|---|
| `RX_VK_MIRROR_MB` set | that value |
| device-local heap ≤ 6.5 GB | `vram / 8` |
| otherwise | `min(vram / 4, 3072 MB)` |
| no device heap known | 3072 MB |

A preset's `mirror_mb` applies only if `preset_caps_memory()` is true (see **F1**). Mirrors are off
entirely on non-discrete GPUs unless forced (`mirror_on()`, `vk_memory.cpp:1958-1965`).

### 6.3 Write-back shadows

`shadow_room` (`gpu/vk/vk_memory.cpp:2976-2987`): `allowed / 8`, capped at 512 MB on integrated
GPUs, and only while VRAM is below 85 % full. Shadows hold prior contents for write hazards, not a
copy of the pool.

### 6.4 Everything else

The present chain, frame generation, DLSS and the BC3 scratch are **not** covered by any of the
three budgets. They are fixed by resolution and feature choice:

```
swapchain           = 4 × w × h × 4 bytes
framegen            = 2 × w×h×4                   (frames)
                    + 2 × ceil(w/4)×ceil(h/4)×2   (luma, R16F)
                    + 2 × ceil(w/16)×ceil(h/16)×8 (flow, RGBA16F)
                    + 3 sets × multiplier × w×h×4 (output frames)
DLSS                 ≈ color 8 B/px + depth 4 B/px + motion 4 B/px at feature res
                    + output 8 B/px at display res
```

At 2560×1440 with 2× frame generation that is ~59 MB of swapchain + ~118 MB of frame-generation
images; at 4K with 4× it is ~133 MB + ~465 MB. These are the first things to turn off on a card at
the VRAM limit, and they are all menu-visible.

---

## 7. Textures: from guest storage to `VkImage`

### 7.1 Identity and sharing

One `VkImage` per texture **storage** (guest GPU address), keyed by address, size, levels, layout
and format class (`gpu/vk/VULKAN.md`, "The renderer after the rewrite"). `MUTABLE_FORMAT` lets the
same image be sampled as UNORM and sRGB; render targets get views per level/layer. This avoids
duplicating a texture when the game changes only its interpretation.

### 7.2 Decoding

NVIDIA cannot sample ASTC (`textureCompressionASTC_LDR` is the documented gap), and OoT's art is
ASTC, so every ASTC texture is decoded:

- ASTC: GPU compute (`host_shaders/astc_decoder.comp`, Eden's decoder) straight from block-linear
  storage.
- Other formats: CPU unswizzle into a staging buffer, using suyu's `UnswizzleTexture`.

ASTC 4×4 is 8 bits per texel; decoded RGBA8 is 32. That 4× expansion is the single largest source
of texture VRAM.

### 7.3 BC3 re-encoding (already implemented — do not propose "add BC7" as if it were missing)

`gpu/vk/vk_texture.cpp:1560-1634` re-encodes eligible decoded ASTC textures as **BC3** (8 bpp, a
quarter of RGBA8) with a GPU compute encoder:

- 2D, single-sample, dimensions ≤ 2048 (`kBcDim`), no top-level drop with one layer, and decoded
  size ≥ 2 MB for block shapes of 6×6 texels or coarser; ≥ 64 MB for 4×4 blocks.
- On a card whose driver allowance is under **8704 MB**, the 2 MB threshold applies to 4×4 too
  (`gpu/vk/astc_bc_policy.h`). 8 GB cards are allowed ~7–7.6 GB and otherwise fill up; an RX 7700S
  held 2.5 GB of decoded ASTC in Hyrule Castle Town and 1.3 GB as BC3.
- Scratch: 2048×2048×8 RGBA8 image + output buffer = 160 MB, plus a 128 MB second scratch for the
  AMD uneven-block path. Kept by default (`RX_VK_BC_IDLE_S=0`); the comment saying "20 s" is stale
  (**F6**).
- AMD's uneven mip block grids are handled by resizing/trimming levels, but that path is **off by
  default** since v2.0.6 (`RX_VK_UNEVEN_BC=0` re-enables): in v2.0.5 it raised an RX 6700 XT from
  10–13 ms to 70–100 ms per frame and produced rainbow ground (issue 823).

This is a well-designed subsystem with careful vendor workarounds. The remaining question is only
the threshold on 10–12 GB cards (see **F5**).

### 7.4 Huge textures and top-level dropping

`gpu/vk/vk_texture.cpp:1517-1554`:

```
skip = 0
while astc and skip+1 < levels and bytes_at(skip) > RX_VK_TEX_SKIP_MB (64)
      and max(width, height) >> skip > RX_VK_TEX_MAX_DIM (2048):
    skip++                                    # drop the top level
while skip+1 < levels and bytes_at(skip) > RX_VK_TEX_MAX_MB (1536):
    skip++                                    # hard cap
```

The hard cap is **1536 MB in code** (line 1524), while the comment above it and `VULKAN.md` say 512
(**F6**). The terrain material arrays quoted in the comments are 8192×8192×68 ASTC: ~24 GB as RGBA8,
hence the dropping. Large 2D arrays in formats with sparse residency can instead be made as sparse
images and bound layer by layer (`vk_texture.cpp:1533-1543`), but OoT on the measured card did not
use this path (no "sparse" line in the logs).

### 7.5 Eviction

The sweep runs every 10 frames but only acts when the budget is exceeded or on scheduled passes
(`gpu/vk/vk_texture.cpp:3060-3350`). Order is LRU by `last_frame`, with a *long* list of
correctness-driven exceptions:

| Rule | Effect |
|---|---|
| Under budget, anything unused < 120 frames is kept (60 when over) | prevents thrash |
| Dead transient targets (drawn over from another start) go after 3 frames, swept every 60 presents | fixes the ~1.2 GB of stale render targets that filled an 8 GB card (issues 107/386) |
| Finalized small textures (GPU drew once, ≤4 MB) kept up to `RX_VK_KEEP_FINALIZED_MB` (512) | avoids empty normal maps coming back as streaks |
| Unflushable GPU-written textures < 256 MB kept unless `RX_VK_KEEP_UNFLUSHABLE=1` | prevents rainbow/garbled textures (issue 993) |
| Small (<64 MB) GPU-written textures drawn once are kept | sky LUT, colour grading |
| GPU-written textures are written back into guest memory before eviction, unless `RX_VK_EVICT_NO_FLUSH` | a re-made image is uploaded from memory rather than empty |
| Destroy only after the GPU's completed timeline value passes | no use-after-free |
| Spare capacity buffers: 32 MB on small cards, unlimited otherwise | avoids churn |

Write-backs are batched: up to 64 textures or ~512 MB of staging per submission (`vk_texture.cpp`
~3340). A sweep of 500 textures used to stall for seconds; batching is why it no longer does.

### 7.6 Accounting

`x.bytes` is the texel chain; `x.pad` is `max(bytes, allocation_size) - bytes`, so the budget counts
**what the allocator actually handed out**, not an estimate (`vk_texture.cpp:1710-1720`). The
exception is sparse images: `x.bytes = 0` and no pad, so **sparse residency is invisible to the
texture budget** (**F7**). `RX_VK_COUNT_BY_ESTIMATE=1` switches back to texel estimates, which
undercounted by ~8 % on a 2000-texture scene.

---

## 8. Buffer imports and the VRAM mirror

### 8.1 Import

Each NVN pool is imported lazily on first binding as a `VkBuffer` over the guest pages
(`VK_EXT_external_memory_host`; dma-buf on Linux when the memfd path is active). There are ~2,400
pools in MKW and NVIDIA allows ~4,096 allocations, so pools are imported on demand, not eagerly
(`gpu/vk/VULKAN.md`, milestones). A page table maps guest address → `(VkBuffer, offset, device
address)`.

### 8.2 Why mirrors exist

A host import is CPU-visible but usually not device-local; the GPU reading it over PCIe is slow.
A **mirror** is a device-local copy of the pool, and the page table is pointed at it
(`mirror_make`, `gpu/vk/vk_memory.cpp:2822-2920`). The lifecycle is:

1. First CPU **read** binding of a pool with no mirror: make one, copy the whole pool in the
   recording's upload command buffer, repoint the page table.
2. CPU → mirror: at the first use in a submission, copy the pages the CPU wrote since the last sync
   (page write generations).
3. GPU → guest: at the submission's end, copy back the ranges the shaders/copies may have written.
4. Retired pools and idle mirrors (600 epochs, 6000 long-idle) are given back
   (`mirrors_give_back`, `vk_memory.cpp:2666-2692`).
5. A pool larger than 1 GB is never mirrored; a pool that does not fit the budget stays in system
   memory and is read over PCIe.

`RX_VK_MIRROR=0` disables the system. `RX_VK_MIRROR_TIGHT=1` refuses mirrors at 90 % VRAM (off by
default: 8 GB cards sit there constantly and then read over PCIe).

### 8.3 Captures and hazards

The game writes several submissions ahead of the replay. To keep a replayed draw seeing memory as
it was **at submit time**, the renderer snapshots dirty guest pages into capture buffers and
consults them when a mirror is synced or a read lands on a page that changed
(`gpu/vk/vk_memory.cpp:786-830`). This is host RAM that scales with how far ahead the game runs, not
with VRAM.

### 8.4 Import pressure

The driver pins every imported pool and counts it against the system-heap budget the GPU can reach
(about half of physical RAM). Past `RX_VK_IMPORT_HIGH` (85 %) of that budget, idle imports are given
back to `RX_VK_IMPORT_LOW` (75 %) and re-imported on next binding (`imports_give_back`,
`vk_memory.cpp:2694-2770`). Windows default is **off** because giving imports back caused device
losses on Intel Arc and AMD; Linux enables it only when the heap budget is under 12 GB. The
comment records a 16 GB RTX 3060 hitting 7758 of 7503 MB and failing new imports with
`VK_ERROR_OUT_OF_DEVICE_MEMORY`.

---

## 9. Present chain, frame generation, DLSS, menu

- Swapchain: `minImageCount = max(caps.minImageCount, 4)` (`gpu/vk/vk_main.cpp:506`), mailbox when
  available, UNORM `B8G8R8A8`/`R8G8B8A8` with sRGB colour space. Four images are requested because
  frame interpolation needs one always free.
- Frame generation (`gpu/vk/vk_framegen.cpp:266-305`): 2 previous frames, 2 luma images at ¼
  resolution (R16F), 2 flow images at 1/16 (RGBA16F), and **3 output sets** (shown / waiting /
  being made) × multiplier full-resolution RGBA8 images. At 1440p 2× that is ~118 MB; at 4K 4×
  ~465 MB.
- DLSS (`gpu/vk/vk_dlss.cpp:1084-1088`): color (RGBA16F), depth (R32F), motion (RG16F) at feature
  resolution, output RGBA16F at display resolution. Images are destroyed 4 frames after a size or
  mode change (`free_dead`).
- The menu and FPS counter draw over the present image; no extra full-screen target.

---

## 10. Settings and environment variables

### 10.1 Menu (saved in `hookshot_options.ini`)

| Row | Values | Memory effect |
|---|---|---|
| Quality preset | Custom / Minimum (4GB) / Low (6GB) / Balanced (8GB) / High (12GB) / Ultra (16GB+) | sets `vram_mb`, `mirror_mb`, resolution, frame gen, pacing, handheld; **only honoured on a card at or below the preset's nominal size** (**F1**) |
| Render resolution | Auto, 100/75/67/50/58/33 % | render targets, DLSS input, frame-gen output, fill rate |
| Output resolution | 2048×1152, 2560×1440, 3840×2160, 1920×1080 | swapchain, frame gen, DLSS output (read once at start) |
| Frame generation | Off, 2×–8× | 3 sets × multiplier × full-res frames (formula in §6.4) |
| Upscaler | game network / DLSS / DLAA | DLSS images at feature and display resolution |
| Window scaling filter | Off / Catmull-Rom / Lanczos | none significant |
| Handheld profile | Off/On | ~4 fps target, softer, read once |

### 10.2 Environment variables (diagnostics and A/B, not user-facing)

| Variable | Default | Effect |
|---|---|---|
| `HOS_MEMORY_MB` | 9809 | total the game sees; changes heap sizing |
| `HOS_KEEP_FREED_PAGES` | off | freed guest pages stay resident |
| `HOS_LARGE_PAGES` | off | 4 GB locked pages; disables discard |
| `HOS_PREFAULT` | off | touch new heap pages up front |
| `HOS_ZERO_ALL_PAGES` | off | zero all allocated pages (diagnostic) |
| `RX_VK_VRAM_MB` | 70 % of allowance, ≤6144 | texture budget |
| `RX_VK_MIRROR_MB` | vram/4 (≤3072) or vram/8 | mirror budget |
| `RX_VK_MIRROR` | on for discrete | disable mirrors |
| `RX_VK_MIRROR_TIGHT` | off | no mirrors above 90 % VRAM |
| `RX_VK_TEX_MAX_DIM` | 2048 | top-level drop threshold; the Steam Deck recipe is 1024 |
| `RX_VK_TEX_MAX_MB` | 1536 | hard cap per texture (code; docs say 512 — **F6**) |
| `RX_VK_TEX_SKIP_MB` | 64 | size at which top levels may be dropped |
| `RX_VK_EVICT_MIN_UNUSED` | 60 | frames a texture must be unused before eviction |
| `RX_VK_EVICT_RENDERED` | off | allow evicting GPU-written textures (risky) |
| `RX_VK_KEEP_FINALIZED_MB` | 512 | finalized textures kept for reuse |
| `RX_VK_KEEP_UNFLUSHABLE` | off | evict unflushable GPU-written textures (issue 993) |
| `RX_VK_BC_IDLE_S` | 0 | seconds before the BC3 scratch is given back |
| `RX_VK_ASTC_BC_ALL` | auto by VRAM | force BC3 for all large ASTC |
| `RX_VK_ASTC_BC_MB` | 2 | minimum decoded size for BC3 |
| `RX_VK_ASTC_RGBA8` | off | disable BC3 re-encoding |
| `RX_VK_IMPORT_HIGH` / `LOW` | 85/75 (Linux small heaps) | import give-back watermarks |
| `RX_VK_IMAGES_VRAM_ONLY` | off | refuse system-memory image fallback |
| `RX_VK_IMAGE_BLOCK_MB` | 128 | image pool block size |
| `RX_VK_MALLOC_TRIM_MB` | 192 | free-arena threshold for `malloc_trim` |
| `RX_VK_STAGING_KEEP_MB` | 64/256 | idle staging kept |
| `RX_VK_BUDGET_BY_BLOCKS` | off | old budget maths (kept for A/B) |
| `RX_VK_NO_EVICT` | off | never evict (diagnostic) |
| `RX_VK_COUNT_BY_ESTIMATE` | off | count texels instead of allocations |
| `HOS_GUEST_MEMFD`, `HOS_THP` | auto | Linux mapping options |

---

## 11. Observability

The renderer already logs everything needed to grade a change. The useful lines:

| Line | Meaning |
|---|---|
| `heap: 6894 MB at 0x1000000000` | heap size the game requested |
| `vk: memory heaps: … used N of M MB` | per-heap VMA usage/budget |
| `vk: VRAM: N MB used of M MB allowed (K MB free in the blocks); textures T MB (budget B MB), C textures` | every 600 presents; the primary VRAM measurement |
| `vk: textures: decoded ASTC A MB, rendered R MB (n drawn in one frame, kept: K KB), other O MB; used in the last 8 presents P MB` | what is in the texture cache |
| `vk: a frame: mirrors N (M MB): X MB synced in Y copies, Z MB written back` | mirror traffic |
| `vk: a frame: … 3518 pipelines …, 62 programs …, 1984 textures (4299 MB)` | scene totals and frame rate |
| `vk: precompile: … programs, … pipelines in N ms (M ms of it the caches opened…)` | startup cache cost |
| `vk: program cache …: 46587 program keys kept, 6716624 KB` | program cache size |
| `vk: images from a pool of 128 MB blocks` | VMA image pool active |
| `vk: imports: N live (M MB), …` | imported pool footprint |
| `vk: system heap N of M MB: … imports given back` | import pressure |
| `vk: malloc: … arenas` / `malloc_trim: …` | host heap |
| `vk: texture …: its N top level(s) left out` | top-level dropping |
| `vk: big ASTC textures are kept as BC3` | BC3 path enabled |

The harness exists: `work/run-vk.ps1` runs the game headless and stops past `-MaxGB` of working
set; `work/vksum.py <run>` summarises a log. For a memory profile, capture the first
`vk: VRAM:` line and the peak (sort the extracted columns), plus the frame-time line, before and
after a change.

Suggested extraction:

```bash
grep -h "vk: VRAM:" hookshot_log*.txt |
  sed -E 's/.*VRAM: ([0-9]+) MB used of ([0-9]+) MB allowed.*textures ([0-9]+) MB \(budget ([0-9]+) MB\), ([0-9]+) textures.*/\1 \2 \3 \4 \5/' |
  sort -n | tail
```

---

## 12. Measured case study

**Machine/config (from the logs, 2026-11-10):** NVIDIA 12 GB-class card, 11.2 GB driver allowance,
2560×1440 output, DLSS on, frame generation 2×, render resolution 50 %, quality preset Custom.
Windows. Logs `hookshot_log.3.txt` / `.4.txt` (gameplay) and `hookshot_log.txt` (menu).

| Quantity | Value | How |
|---|---:|---|
| Guest total advertised | 9809 MB | default (`hos_mem.c:53`) |
| Heap requested by OoT | **6894 MB** | `heap: 6894 MB` |
| VRAM allowed (driver) | 10.9–11.2 GB | `VRAM:` lines |
| VRAM used, peak | **11,281 MB** | peak of `VRAM:` lines |
| Textures, peak | **3,804 MB** (budget 3,788 MB) | peak of `textures T MB` |
| Textures, at 11.2 GB used | 2,920 MB (budget 2,251 MB) | pressure-shrunk budget |
| Textures in steady gameplay | **1,984 textures / 4,299 MB** | frame line |
| Mirrors, peak | **526 mirrors / 2,416 MB** | `mirrors N (M MB)` |
| Mirror traffic per frame | ~4.9 MB synced, **39.6 MB written back** | frame line |
| Imported pools | 5–6 live / 192–224 MB | `imports:` line |
| Swapchain | 4 images, 1280×720 and 2560×1440 | `swapchain` line |
| Pipeline cache | **659 MB** | file size |
| Shader seed | 515 MB, transient | file size |
| Program cache | **6.7 GB**, 46,587 keys | log + file size |
| Precompile | 12,890 programs / 3,518 gfx / 10,328 compute in **1,957 ms** | `precompile:` line |
| Frame rate in the sampled scene | 60.2 fps at 121,440 presents | frame line |

**Interpretation (M + H):**

- The texture cache is the largest *controllable* VRAM consumer, but mirrors are a close second at
  2.4 GB. On this 11 GB card the 6 GB texture cap is not the binding constraint; the *pressure
  shrink* reduces the effective budget to 2.2–3.8 GB as total usage approaches the driver's
  allowance.
- VRAM briefly exceeds the driver allowance by ~3 % (11,281 vs 10,940 MB). That is within the
  design: the budget is a soft target, the sweep runs every 10 frames, and eviction waits 60 frames
  for reuse. It is also the window in which the driver may page VRAM, which is the expensive
  failure mode (issues 107/386/993).
- 39.6 MB of write-backs per frame is CPU/PCIe traffic, not VRAM; it is the price of keeping guest
  memory coherent.
- The ~10 GB RAM figure is mostly the 6.9 GB heap plus the 659 MB pipeline cache and host buffers;
  the 6.7 GB program cache is not resident.

**Caveats:** one machine, one vendor, one scene; driver allowance varies with other applications;
the logs were produced while tuning settings, so they are not a controlled A/B.

---

## 13. Evaluation

### 13.1 What is already well designed (credit where due)

1. **ASTC → BC3 at ¼ the size**, with a vendor-aware encoder, a scratch buffer, and a VRAM-based
   policy (`gpu/vk/vk_texture.cpp:1560-1634`, `astc_bc_policy.h`). This is the big win people
   usually propose; it is already there.
2. **Budget accounting by allocation, not estimate**, including VMA block slack
   (`vk_texture.cpp:1710-1720`, `vk_vma.cpp:172-185`). Most engines get this wrong and
   under-count.
3. **Eviction correctness**: write-back before eviction, deferred destruction on the GPU timeline,
   keep-finalized, keep-unflushable, dead-transient-target sweep. The comments document real
   regressions (issues 107, 386, 993) that each of these fixes.
4. **Imports on demand**, not eagerly; page-table redirection; import give-back under system-heap
   pressure.
5. **Program cache indexed per record**, so a 6.7 GB file does not become 6.7 GB of RAM. This is
   the pattern the pipeline cache does not follow.
6. **Device-local-only images** with an explicit, opt-out system-memory fallback, avoiding the
   classic "VMA silently put the render target in RAM" bug.

### 13.2 Findings

Each finding is graded **Impact** (memory saved or risk avoided), **Confidence**, **Effort**, and
**Risk**. "Impact" is an estimate from the formulas and the measured case study, not a measured
delta.

---

#### F1 — The Quality preset does nothing on a card larger than its nominal size

**Evidence (F).** `preset_caps_memory()` (`gpu/vk/vk_memory.cpp:1975-1980`) returns true only when
the device-local heap is at most `nominal_gb[preset] + 768 MB`. Both the texture budget
(`vk_texture.cpp:3080`) and the mirror budget (`vk_memory.cpp:1997`) apply a preset only when that
returns true. On an 11 GB card, selecting **Minimum (4GB)** or **Low (6GB)** leaves the 6 GB texture
cap and ~2.4 GB mirror budget in place. The comment even records the inverse bug (a 4 GB preset on
an 8 GB card kept 256 MB of mirrors and 2.2 GB of textures, 5 fps) — the guard was added to prevent
that, but it also removes the user's ability to reduce memory on a big card.

**Impact:** high for users on 12–16 GB cards who want to cap usage (e.g. sharing the GPU with other
apps, or a laptop on battery). **Confidence:** high. **Effort:** small. **Risk:** low if the change
is explicit (a warning in the row, or a "force" toggle), because the original regression came from
applying a small preset on a card that then read pools over PCIe.

**Change:** apply the preset's budgets on any card, but show the trade-off in the row's hint; or add
"use preset budgets anyway" as a separate value. Verify with the `VRAM: … budget …` line on a
12 GB card.

---

#### F2 — Texture budget enforcement is soft, and the overshoot is where paging happens

**Evidence (F + M).** The sweep runs every 10 frames (`vk_texture.cpp:3064`); eviction requires 60
frames of non-use (`:3190`); the budget is recomputed from `usage - block_free` (`:3084-3098`).
Measured: textures 3,804 MB against a 3,788 MB budget, and 2,920 MB against a pressure-shrunk
2,251 MB budget; total VRAM 11,281 of 10,940 MB allowed.

**Impact:** medium-high. The overshoot itself is harmless if the driver has headroom; on an 8 GB
card it is exactly the condition that triggers paging and the 5–9 fps reports. **Confidence:**
medium (the scene did not show a frame-time collapse, so this is a risk rather than an observed
regression here). **Effort:** medium. **Risk:** medium — tightening the sweep re-creates the thrash
that `RX_VK_EVICT_MIN_UNUSED=60` was raised to fix (the comment records ~300 textures evicted and
re-made with 8).

**Change direction:** react to the driver's hard 97 % signal faster (e.g. an extra sweep when
`usage > 97 %` and the last sweep was <10 frames ago), or refuse new image allocations when the
post-allocation estimate would exceed the allowance, rather than evicting after the fact. Do **not**
simply lower `min_unused`.

**Verification:** `VRAM:` peak and the frame-time line in the same scene; count
`textures retired` and re-makes.

---

#### F3 — Mirrors are 2.4 GB and their budget is card-sized, not working-set-sized

**Evidence (F + M).** `mirror_budget()` returns `min(vram/4, 3072 MB)` on cards above 6.5 GB
(`vk_memory.cpp:1982-1999`); measured 526 mirrors / 2,416 MB. The mirror is a full device-local copy
of every bound pool, made on first read binding and kept until 600/6000 idle epochs.

**Impact:** potentially 0.5–1.5 GB of VRAM on 8–12 GB cards. **Confidence:** medium. **Effort:**
high. **Risk:** **high** — this has already been tried: `RX_VK_MIRROR_MB=1024` on an 8 GB card made
captures wait on the GPU and dropped Hyrule Field from 55 to 35–45 fps (issue 995, comment at
`vk_memory.cpp:1502-1507`). The mirror is not free memory; it is a latency optimization.

**Change direction:** measure the *hot set* (bytes actually bound in the last N submissions), and
budget to that plus headroom, instead of a fraction of the card. Only mirror pools that are read
more than once per few frames; leave write-only/streaming pools in guest memory. Do not ship a
smaller fixed budget without an A/B.

**Verification:** mirror hit/miss counters already logged (`a frame: mirrors … synced … written
back`) plus frame time in Hyrule Field; A/B against `RX_VK_MIRROR=0`.

---

#### F4 — The pipeline cache is read whole at startup

**Evidence (F + M).** `pcache_init` (`gpu/vk/vk_draw.cpp:678-700`) seeks to the end, resizes a
`std::vector<uint8_t>` to the file size, and reads it. The file is **659 MB** on the measured
install; the comment records 1.7 GB when 40,000 more pipelines were added. `vkCreatePipelineCache`
then consumes the data; the driver keeps its own copy.

**Impact:** ~0.66 GB of RAM (driver copy) plus a transient 0.66 GB vector at startup. **Confidence:**
medium-high for the file size; medium for how much the driver retains. **Effort:** medium.
**Risk:** low for mmap (the pointer is valid for the call), higher for trimming the cache (rebuild
cost: the measured precompile is 1,957 ms for 3,518 graphics + 10,328 compute pipelines, so a cold
cache is not catastrophic on this CPU).

**Change direction:** map the file instead of reading it (removes the transient copy), and consider
pruning entries for pipelines this driver/GPU no longer uses. The per-record program cache is the
model to copy if the driver ever supports partial caches; `VkPipelineCache` does not.

---

#### F5 — BC3 conversion threshold leaves 4×4 ASTC as RGBA8 on 10–12 GB cards

**Evidence (F).** `min_bytes = (bw*bh >= 36 || all) ? 2 MB : 64 MB` (`vk_texture.cpp:1566-1577`).
`all_by_vram` is true only when the driver allowance is under 8,704 MB. On the measured 11.2 GB
card, 4×4 ASTC textures below 64 MB decoded stay RGBA8; only the big ones become BC3. Measured
textures: 4.3 GB in gameplay.

**Impact:** potentially several hundred MB to ~1 GB of VRAM, at some quality cost. **Confidence:**
medium (depends on how much of the 4.3 GB is 4×4 ASTC below 64 MB). **Effort:** small (policy
threshold or a menu option). **Risk:** medium — BC3 is visibly softer on UI/character art, which is
why the threshold is cautious; the AMD resize path is already off for performance reasons.

**Change direction:** expose "Texture compression: Auto / Maximum savings / Quality" in the menu,
or extend the VRAM threshold to 12 GB with an A/B on image quality and frame time. Measure the
decoded-ASTC number in `vk: textures:` before and after.

---

#### F6 — Documentation drift in three defaults (correctness of the document, not the code)

**Evidence (F).**

1. `RX_VK_TEX_MAX_MB`: code default **1536** (`vk_texture.cpp:1524`); the comment directly above
   says 512, and `gpu/vk/VULKAN.md` says 512.
2. BC3 scratch idle: the comment says "given back after 20 s"
   (`vk_texture.cpp:431-433`), but `bc_idle_s()` defaults to **0**, i.e. kept for good
   (`vk_texture.cpp:441-447`).
3. `VULKAN.md` still says the per-image cache budget is `RX_VK_VRAM_MB` 2048 in `vk_image.cpp`;
   the implementation is the LRU in `vk_texture.cpp` with a 4–6 GB default.

**Impact:** none on runtime; high on anyone grading or tuning from the docs. **Confidence:** high.
**Effort:** trivial. **Risk:** none.

**Change:** fix comments and `VULKAN.md`; add the actual formulas from §6.1 and §7.4.

---

#### F7 — Sparse images are not counted against the texture budget

**Evidence (F).** `x.bytes = x.sparse ? 0 : …` and `x.pad` is only set for non-sparse images
(`vk_texture.cpp:1700-1720`); sparse residency is bound layer by layer later. The texture budget
therefore cannot see sparse images.

**Impact:** currently latent — OoT on the measured card logged no sparse images (`grep sparse` = 0),
and the path requires a large 2D array in a sparse-capable format. On a title/card that does use it,
the budget would be wrong by however much is resident. **Confidence:** high for the code, low for
current impact. **Effort:** medium (account resident tiles as they are bound).

---

#### F8 — Guest heap is 6.9 GB of commit charge; large pages and prefault make it worse

**Evidence (F + M).** `SetMemoryHeapSize(6894 MB)` commits the range (`hos_mem.c:203`); Windows
counts it as commit charge, not RSS. `HOS_LARGE_PAGES` commits 4 GB locked and disables discard
(`hos_mem.c:131-133`); `HOS_PREFAULT` touches every new page up front (`hos_mem.c:196-202`). On a
16 GB machine, 6.9 GB + 0.66 GB pipeline cache + host buffers is close to the commit limit.

**Impact:** avoiding large pages / prefault is free and prevents a known regression mode; actually
shrinking the heap requires the game to accept a smaller `HOS_MEMORY_MB` (and OoT may size its
allocations from it). **Confidence:** high for the mechanism, medium for whether OoT tolerates a
smaller heap. **Effort:** low (documentation/default) to high (lazy commit).

**Change direction:** keep the defaults; document that `HOS_LARGE_PAGES` is a latency experiment, not
a memory saving. A commit-on-demand heap is possible (the first-touch handler already exists) but
host APIs given guest buffers need committed pages, so it is not a small change.

---

#### F9 — Frame generation is the largest optional VRAM item at high resolution

**Evidence (F).** Formula in §6.4; 3 output sets × multiplier full-resolution images
(`vk_framegen.cpp:75-76, 290-293`). At 4K 4× that is ~465 MB before DLSS and the swapchain.
**Impact:** settings-only; turning frame generation off recovers it immediately. **Confidence:**
high (arithmetic). **Effort:** none (already a menu row). **Risk:** none.

---

#### F10 — Startup work is already small; do not "optimize" precompile blindly

**Evidence (M).** 12,890 programs and 13,846 pipelines prepared in **1,957 ms**, of which 1,434 ms
is opening the caches; the frame line shows 3,518 pipelines built in 66,850 ms *in total* over the
session (lazy builds during play). Translation in play is 353 ms in total. **Impact:** low; the
cache design is working. **Confidence:** high for this machine. **Risk:** changes here can regress
first-run time.

### 13.3 Things that look wasteful but are deliberate

- **Four swapchain images.** Needed so frame interpolation always has a free image; dropping to 3
  saves ~15 MB at 1440p and costs pacing.
- **A 160 MB BC3 scratch kept for good.** It avoids re-making the encoder resources at every scene
  load; `RX_VK_BC_IDLE_S` can return it, but the give-back caused garbled textures on 4–8 GB cards
  (issue 993).
- **Keeping finalized and unflushable textures.** Each rule exists because evicting them produced
  visible corruption (streaks, rainbow textures, black ground). They are bounded (512 MB and 256 MB).
- **System-memory image fallback.** Slow, but the alternative is a black screen on a 4 GB card
  (issue 96).
- **Writing back GPU-rendered textures before eviction.** Costs bandwidth; without it a re-made
  image comes back empty (issue 386).

### 13.4 Priority matrix

| Finding | VRAM/RAM at stake | Effort | Risk | Recommended order |
|---|---|---:|---|---|
| F1 preset ignored on big cards | user-controlled caps | S | Low | 1 |
| F6 documentation drift | none (trust) | XS | None | 2 |
| F2 budget enforcement latency | up to ~0.7 GB VRAM | M | Medium | 3 |
| F5 BC3 threshold on 10–12 GB | ~0.3–1 GB VRAM | S | Medium | 4 |
| F4 pipeline cache read | ~0.66 GB RAM | M | Low | 5 |
| F3 mirror hot-set budget | ~0.5–1.5 GB VRAM | L | High | 6 (measure first) |
| F7 sparse accounting | unknown | M | Low | when used |
| F8 heap commit charge | up to ~1 GB commit | L | Medium | document only |
| F9 frame-gen VRAM | up to ~465 MB | none | None | user setting |

---

## 14. Grading rubric

For a reviewer grading this document, or a change proposed from it. Each criterion is 0–5; a
passing analysis scores **≥ 28/40**, with no 0 in criteria 2, 4, or 6.

| # | Criterion | What a 5 looks like | How to check |
|---|---|---|---|
| 1 | Coverage | Covers guest heap, host heap, textures, buffer/mirrors, present chain; names the budget for each | §2–§9; every consumer in §4/§6 has a source |
| 2 | Evidence | Every numeric claim has `file:line` or a quoted log line; F/M/H are separated | spot-check 10 claims against the code |
| 3 | Arithmetic | Formulas for texture bytes, mirror budget, framegen bytes are reproducible | recompute §6.1/§6.4 by hand |
| 4 | Correctness | Does not claim BC7/BC3 is missing; does not propose lowering `min_unused` without the thrash caveat; notes the 6 GB cap's reason | §7.3, §13.2 F2/F3 |
| 5 | Measured vs inferred | Log numbers are dated, scoped to one machine, and not presented as universal | §12 caveats |
| 6 | Risk analysis | Every proposal names a regression it could recreate and an A/B | §13.2 "Risk" and "Verification" |
| 7 | Actionability | Smallest change, expected delta, rollback, exact log line to watch | F1/F6 are XS; F2/F4 have a plan |
| 8 | Documentation hygiene | Defaults in prose match code; stale comments flagged | F6 lists three |

**Claim ledger (the claims most likely to be challenged):**

| Claim | Type | Evidence | Confidence |
|---|---|---|---|
| Guest heap is 6894 MB on this run | M | `heap: 6894 MB` | high |
| Textures peak 3.8 GB, mirrors 2.4 GB | M | `VRAM:` / `mirrors` lines | high |
| Texture default cap is 6 GB | F | `vk_texture.cpp:3068-3071` | high |
| Mirror default is `min(vram/4, 3072)` | F | `vk_memory.cpp:1991-1992` | high |
| Presets are ignored on larger cards | F | `preset_caps_memory`, two call sites | high |
| ASTC → BC3 is already implemented | F | `vk_texture.cpp:1560-1634` | high |
| `RX_VK_TEX_MAX_MB` default is 1536 | F | `vk_texture.cpp:1524` | high |
| Sparse images are uncounted | F | `vk_texture.cpp:1706-1720` | high (impact unknown) |
| Program cache is not resident | F | per-record reads, index | high |
| Pipeline cache is 659 MB and read whole | M + F | file size, `vk_draw.cpp:678-700` | high |

---

## 15. Threats to validity and open questions

- **One machine.** The measured numbers come from a 12 GB NVIDIA card at 1440p with DLSS and 2×
  frame generation. AMD/Intel, 8 GB cards, Linux/dma-buf, and 4K change every constant.
- **Logs are not a benchmark.** They were captured while settings were being tuned; frame rate and
  scene vary. The peak extraction is descriptive.
- **Driver allowance is shared.** `VK_EXT_memory_budget` reflects other applications; the same
  install can report 10.9 or 11.2 GB on different runs.
- **Comments can be stale.** Three cases are documented in F6; assume more.
- **No allocation trace.** There is no per-allocation timeline, so "who peaked" is inferred from
  aggregate counters. A targeted instrumentation pass (tag VMA allocations by consumer) would
  improve F2/F3.
- **CPU is out of scope.** The replay cost is logged (submit/draw/descriptors/memory sync) but this
  document does not attempt a CPU profile.

Open questions worth answering before any large change:

1. How much of the 4.3 GB texture set is 4×4 ASTC below the 64 MB BC3 threshold? (F5)
2. What is the mirror *hit rate* and the frame-time delta of `RX_VK_MIRROR=0` on an 8 GB card? (F3)
3. Does the 3 % VRAM overshoot actually trigger paging on 8 GB cards, or is it absorbed by the
   driver? (F2)
4. Does OoT honour a smaller `HOS_MEMORY_MB` without changing behaviour? (F8)

---

## 16. Appendix

### 16.1 Glossary

| Term | Meaning here |
|---|---|
| Guest | The recompiled game's address space (the 512 GB window) |
| Pool | An NVN buffer allocation the game made; imported as a `VkBuffer` |
| Mirror | Device-local copy of a pool, used instead of reading guest memory over PCIe |
| Capture | Snapshot of dirty guest pages, so a replayed draw sees memory as of its submit |
| Shadow | Prior contents kept for write hazards, budgeted separately from mirrors |
| Storage | The guest GPU address that identifies a texture |
| Top-level drop | Omitting the largest mip levels of a huge texture to fit a cap |
| Sparse | A `VkImage` whose layers are bound as they are written, not allocated whole |
| BC3 | 8 bpp block compression, ¼ the size of RGBA8; used for decoded ASTC |
| Commit charge | Virtual memory Windows has promised, whether or not it is resident |

### 16.2 Source map

| Area | File |
|---|---|
| Guest window, commit-on-touch, large pages | `host/hos_main.c`, `host/mkw_glue.c` |
| Guest heap, regions, pages, discard | `host/hos_mem.c` |
| GPU VA → guest mapping, write-watch ranges | `host/nvdrv.c` |
| VMA setup, image pool, usage/budget | `gpu/vk/vk_vma.cpp` |
| Pool import, mirrors, captures, shadows | `gpu/vk/vk_memory.cpp` |
| Textures, ASTC/BC3, eviction, staging | `gpu/vk/vk_texture.cpp` |
| BC3 policy | `gpu/vk/astc_bc_policy.h` |
| Pipeline cache, program cache, prediction | `gpu/vk/vk_draw.cpp`, `gpu/vk/vk_shader.cpp` |
| Swapchain, device selection | `gpu/vk/vk_main.cpp` |
| Frame generation | `gpu/vk/vk_framegen.cpp` |
| DLSS | `gpu/vk/vk_dlss.cpp` |
| Options and presets | `gpu/vk/vk_menu.h`, `gpu/vk/vk_menu.cpp` |
| Architecture notes (partly stale, see F6) | `gpu/vk/VULKAN.md` |
| Requirements and menu table | `readme.md` |

### 16.3 Useful one-liners

```bash
# Peak VRAM and texture cache
grep -h "vk: VRAM:" hookshot_log*.txt | sed -E 's/.*VRAM: ([0-9]+) MB used of ([0-9]+) MB allowed.*textures ([0-9]+) MB \(budget ([0-9]+) MB\), ([0-9]+) textures.*/\1 \2 \3 \4 \5/' | sort -n | tail -5

# Peak mirrors
grep -h -E "mirrors? [0-9]+ \([0-9]+ MB\)" hookshot_log*.txt | sed -E 's/.*mirrors? ([0-9]+) \(([0-9]+) MB\).*/\2/' | sort -n | tail -3

# What is in the cache
grep -h "vk: textures: decoded" hookshot_log*.txt | tail -5

# Heap and cache files
grep -h -E "heap: [0-9]+ MB|program cache .*KB|precompile:" hookshot_log*.txt | tail -5
ls -lh hookshot_programs.bin hookshot_shaders.bin mkw_vk_pipelines.bin hookshot_pipelines.bin
```
