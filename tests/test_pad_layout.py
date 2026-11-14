"""The Layout preset's face buttons (gpu/vk/vk_menu.cpp): Nintendo, Xbox / PC and Swap Face.

Xbox / PC must put the game's A on the bottom, B on the right, X on the left and Y on the top. The preset is compiled and
run against a fake host's bindings, from the default and from a custom mapping; detect_pad_layout must recognize each preset
and call a hand-made mapping Custom. Applying a preset must clear both gamepad inputs on the four face buttons, so a stale
Input 2 cannot keep the Nintendo arrangement alive. menu_init is checked for applying a saved preset when the controls are
still the default (a hand-made mapping is not overwritten). The Talk ring's bind-to-place mapping is compiled too.

    python -I -B tests/test_pad_layout.py [--cxx g++]
"""
import argparse
import re
import subprocess
import tempfile
from pathlib import Path

RUN_ALL_ARGS = ["--cxx", "{cxx}"]
ROOT = Path(__file__).resolve().parents[1]


def check(ok, message):
    if not ok:
        raise SystemExit("FAIL: " + message)


def block(text, signature):
    start = text.index(signature)
    brace = text.index("{", start)
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("unterminated " + signature)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cxx", default="g++")
    a = p.parse_args()
    src = (ROOT / "gpu" / "vk" / "vk_menu.cpp").read_text(encoding="utf-8")
    table = re.search(r"const int kPadFaceButtons\[4\].*?\n\};\n", src, re.S)
    check(table is not None, "the face-button table is missing")
    apply = block(src, "void apply_pad_layout(int layout)")
    detect = block(src, "int detect_pad_layout()")
    init = block(src, "void menu_init()")
    host_src = (ROOT / "host" / "prompts.c").read_text(encoding="utf-8")
    ring_places = block(host_src, "static void ring_places(const int *bind, int place[4])")

    check("HOS_SW_A, HOS_SW_B, HOS_SW_X, HOS_SW_Y" in table.group(0), "the face buttons are not A B X Y")
    check("{HOS_PAD_SOUTH, HOS_PAD_EAST, HOS_PAD_WEST, HOS_PAD_NORTH}" in table.group(0),
          "Xbox / PC is not A bottom, B right, X left, Y top")
    check("detect_pad_layout() == 0" in init and "apply_pad_layout(opts.pad_layout)" in init,
          "menu_init does not apply a saved layout when the controls are still the default")

    fixture = "\n".join([
        '#include "gpu/vk/vk_api.h"',
        "#include <cstdio>",
        "#include <cstdlib>",
        "#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, \"FAIL:%d %s\\n\", __LINE__, #x); std::exit(1); } } while (0)",
        "struct Host {",
        "    int (*binding)(int, int, int);",
        "    void (*bind)(int, int, int, int);",
        "    int (*pad_raw)(void);",
        "};",
        "struct Options { int pad_layout = 0; } opts;",
        "static int binds[16][2];",
        "static int raw() { return 0; }",
        "static int binding(int, int button, int slot) { return binds[button][slot]; }",
        "static void bind(int, int button, int slot, int code) { binds[button][slot] = code; }",
        "static Host fake{binding, bind, raw}, *host = &fake;",
        "static bool can_bind() { return host && host->binding && host->bind && host->pad_raw; }",
        table.group(0),
        apply,
        detect,
        ring_places,
        "static void set_faces(int a, int b, int x, int y) {",
        "    binds[HOS_SW_A][0] = a, binds[HOS_SW_B][0] = b, binds[HOS_SW_X][0] = x, binds[HOS_SW_Y][0] = y;",
        "    binds[HOS_SW_A][1] = binds[HOS_SW_B][1] = binds[HOS_SW_X][1] = binds[HOS_SW_Y][1] = 9;",
        "}",
        "static void expect(int layout, int a, int b, int x, int y) {",
        "    CHECK(binds[HOS_SW_A][0] == a && binds[HOS_SW_B][0] == b && binds[HOS_SW_X][0] == x && binds[HOS_SW_Y][0] == y);",
        "    CHECK(binds[HOS_SW_A][1] == -1 && binds[HOS_SW_B][1] == -1 && binds[HOS_SW_X][1] == -1 && binds[HOS_SW_Y][1] == -1);",
        "    CHECK(detect_pad_layout() == layout);",
        "}",
        "int main() {",
        "    set_faces(HOS_PAD_NORTH, HOS_PAD_WEST, HOS_PAD_SOUTH, HOS_PAD_EAST);   // (a hand-made mapping)",
        "    CHECK(detect_pad_layout() == 3);",
        "    apply_pad_layout(1);",
        "    expect(1, HOS_PAD_SOUTH, HOS_PAD_EAST, HOS_PAD_WEST, HOS_PAD_NORTH);",
        "    apply_pad_layout(0);",
        "    expect(0, HOS_PAD_EAST, HOS_PAD_SOUTH, HOS_PAD_NORTH, HOS_PAD_WEST);",
        "    apply_pad_layout(2);",
        "    expect(2, HOS_PAD_SOUTH, HOS_PAD_EAST, HOS_PAD_NORTH, HOS_PAD_WEST);",
        "    set_faces(HOS_PAD_NORTH, HOS_PAD_WEST, HOS_PAD_SOUTH, HOS_PAD_EAST);",
        "    apply_pad_layout(1);",
        "    expect(1, HOS_PAD_SOUTH, HOS_PAD_EAST, HOS_PAD_WEST, HOS_PAD_NORTH);",
        "    apply_pad_layout(1);",
        "    expect(1, HOS_PAD_SOUTH, HOS_PAD_EAST, HOS_PAD_WEST, HOS_PAD_NORTH);",
        "    int xbox[HOS_SW_N] = {0}, place[4];",
        "    xbox[HOS_SW_A] = HOS_PAD_SOUTH, xbox[HOS_SW_B] = HOS_PAD_EAST;",
        "    xbox[HOS_SW_X] = HOS_PAD_WEST, xbox[HOS_SW_Y] = HOS_PAD_NORTH;",
        "    ring_places(xbox, place);",
        "    CHECK(place[0] == 1 && place[1] == 0 && place[2] == 3 && place[3] == 2);",
        "    std::puts(\"PASS: the face presets, their detection, Input 2 clearing and Talk-ring places\");",
        "    return 0;",
        "}",
        "",
    ])
    with tempfile.TemporaryDirectory() as tmp:
        cpp = Path(tmp) / "fixture.cpp"
        exe = Path(tmp) / "fixture.exe"
        cpp.write_text(fixture, encoding="utf-8")
        subprocess.run([a.cxx, "-std=c++20", "-I", str(ROOT), str(cpp), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    main()
