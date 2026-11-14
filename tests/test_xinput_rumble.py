"""The game's rumble reaches an Xbox pad through XInput when SDL cannot deliver it.

SDL's motor path is the normal one. Without an SDL DLL, input still works through XInput, but the game's vibration values
used to go nowhere. If SDL opens a pad but refuses its motor rumble, that also used to leave the pad silent. nn_hid.c now
loads XInputSetState, sends the same heavy/light values through pad_sdl.c's Rumble strength scaling, and uses XInput only
while SDL has no pad or its last motor send failed.

python -I -B tests/test_xinput_rumble.py
"""
from pathlib import Path

RUN_ALL_ARGS = []

ROOT = Path(__file__).resolve().parents[1]


def block(text, marker):
    start = text.index(marker)
    end = text.index("{", start) + 1
    depth = 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def check(cond, what):
    if not cond:
        raise SystemExit("FAIL: " + what)


def main():
    src = (ROOT / "host" / "nn_hid.c").read_text(encoding="utf-8")
    pad = (ROOT / "host" / "pad_sdl.c").read_text(encoding="utf-8")
    sdl = (ROOT / "host" / "hos_sdl.c").read_text(encoding="utf-8")
    header = (ROOT / "host" / "hos_sdl.h").read_text(encoding="utf-8")
    check("typedef DWORD(WINAPI *XInputSetStateFn)" in src, "nn_hid.c does not declare XInputSetState")
    check('GetProcAddress(x, "XInputSetState")' in block(src, "static void pads_read("),
          "pads_read does not load XInputSetState")
    pads = block(src, "static void pads_read(")
    check("const int xinput_rumble = !sdl_pads || hos_sdl_rumble_failed();" in pads,
          "pads_read does not select XInput when SDL cannot rumble")
    check("if (xinput_input || xinput_rumble)" in pads, "pads_read cannot poll XInput for rumble alone")
    check("if (xinput_input) {" in pads, "rumble-only XInput polling still merges buttons")
    check("InterlockedExchange(&xinput_pad, xinput_rumble ? xinput_here : -1);" in pads,
          "pads_read does not hand rumble ownership to XInput")
    send = block(src, "static void vibration_send(uint64_t handle, uint64_t value) {")
    check("const float heavy" in send and "const float light" in send, "vibration_send does not split the motors")
    check("hos_sdl_rumble(heavy, light);" in send, "vibration_send no longer sends SDL the motors")
    check("hos_sdl_rumble_motors(motors)" in send, "the XInput fallback bypasses the Rumble strength row's scaling")
    check("xinput_set((DWORD)pad, &xv)" in send, "vibration_send has no XInput fallback")
    check("pad >= 0 && xinput_set" in send, "the XInput fallback can run while SDL owns the input")
    check("rumble_bad" in pad and "hos_sdl_rumble_failed" in pad, "pad_sdl.c does not report a refused rumble")
    check("rumble_bad = (uint8_t)!took;" in pad, "pad_sdl.c does not record SDL's motor result")
    check("int hos_sdl_pad_rumble(" in sdl, "hos_sdl.c does not return SDL's motor result")
    check("int hos_sdl_pad_rumble(" in header, "hos_sdl.h still declares the old motor send")
    print("ok: XInput rumble fallback is wired for no SDL or a refused SDL motor send")


if __name__ == "__main__":
    main()
