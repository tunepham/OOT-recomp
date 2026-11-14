"""The camera keeps the heading a ZL recenter gave it, without the removed option.

The old patcher hooked f_80644c80 with an extern call to hos_disable_camera_centering. The runtime function was removed, and
that extern was the undefined symbol at the link. This feeds the patcher the exact camera shape and checks that it now
skips the movement yaw follow unconditionally, emits no centering symbol, and that the option, menu row and replay answer
stay gone.

python -I -B tests/test_camera_centering.py
"""
import sys
import unittest
from pathlib import Path

RUN_ALL_ARGS = []

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "nx2"))
import oot_patches


class CameraFollowTests(unittest.TestCase):
    def camera(self):
        body = ("L_8064720c:;\nif (!c->z) { goto L_806492c4; }\n"
                "    c->x[25] = c->x[26];\n"
                "L_80645ad8:;\n    return;\n")
        return '#include "a64.h"\n' + oot_patches.CAM_HEAD + body + '}\n'

    def test_patcher_emits_unconditional_follow(self):
        out, counts, lines = oot_patches.patch_file(self.camera(), "code_000.c", True)
        self.assertEqual(out.count(oot_patches.CAM_FOLLOW_HOOK), 1)
        self.assertEqual(out.count("L_80645ad8:;"), 1)
        self.assertNotIn("hos_disable_camera_centering", out)
        self.assertNotIn("extern int hos_disable", out)
        self.assertEqual(counts["camera follow"], 1)
        self.assertTrue([line for line in lines if "recenter" in line])

    def test_patching_twice_does_not_duplicate_the_hook(self):
        once = oot_patches.patch_file(self.camera(), "code_000.c", True)[0]
        twice = oot_patches.patch_file(once, "code_000.c", True)[0]
        self.assertEqual(twice.count(oot_patches.CAM_FOLLOW_HOOK), 1)

    def test_a_cached_old_lift_loses_the_removed_symbol(self):
        old = self.camera().replace(
            '#include "a64.h"\n',
            '#include "a64.h"\nextern int hos_disable_camera_centering(void);\n').replace(
            oot_patches.CAM_FOLLOW_ANCHOR,
            oot_patches.CAM_FOLLOW_ANCHOR + oot_patches.CAM_FOLLOW_OLD, 1)
        out = oot_patches.patch_file(old, "code_000.c", True)[0]
        self.assertNotIn("hos_disable_camera_centering", out)
        self.assertEqual(out.count(oot_patches.CAM_FOLLOW_HOOK), 1)
        self.assertEqual(out.count(oot_patches.CAM_FOLLOW_OLD), 0)

    def test_option_menu_row_and_replay_answer_are_gone(self):
        for rel in ("gpu/vk/vk_api.h", "gpu/vk/vk_menu.h", "gpu/vk/vk_menu.cpp", "gpu/vk/vk_replay.cpp"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            for needle in ("HOS_OPT_DISABLE_CAMERA_CENTERING", "disable_camera_centering", "Disable camera centering"):
                self.assertNotIn(needle, text, rel)


if __name__ == "__main__":
    unittest.main()
