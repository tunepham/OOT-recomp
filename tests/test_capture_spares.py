"""Compile and run the capture spares' budget (gpu/vk/capture_spares.h; no game/GPU required), and check that vk_memory.cpp
uses it. A scene load captures hundreds of MB; the retired 40-60 MB buffers used to stay as spares for good on a card over
6.5 GB, up to 12 of them (768 MB of host memory). A few are enough: 32 MB on a 6.5 GB-or-less card, 256 MB otherwise;
RX_VK_CAP_SPARE_MB still overrides.

python -I -B tests/test_capture_spares.py [--cxx g++]
"""
import argparse
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]

ROOT = Path(__file__).resolve().parents[1]


def block(text, marker):
    start = text.index(marker)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def check_source():
    mem = (ROOT / "gpu/vk/vk_memory.cpp").read_text(encoding="utf-8")
    spare = block(mem, "uint64_t cap_spare_max(")
    for needle in ('getenv("RX_VK_CAP_SPARE_MB")', "capture_spares::max_bytes(vram_usage(&used, &allowed), allowed)"):
        if needle not in spare:
            raise SystemExit("vk_memory.cpp: cap_spare_max() no longer has: " + needle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cxx", default="g++")
    args = parser.parse_args()
    check_source()
    with tempfile.TemporaryDirectory(prefix="capture-spares-") as tmp:
        exe = Path(tmp) / "capture-spares"
        subprocess.run([args.cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", str(ROOT / "tests/test_capture_spares.cpp"),
                        "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
