# -*- coding: utf-8 -*-
"""工作台视觉 tokens 与导航配置的纯函数/结构测试（离线、确定性、无显示依赖）。

来源：Trae（平级成员），2026-10-05，任务 m-1791187019713-373b6e
（job j-534407960b61037245f56fe225e91459）。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。

先写测试（红）再实现（绿）。不访问磁盘、不启动进程、不引入任何 GUI 工具包。
覆盖：原 COLORS 键无损、状态/禁用/输入底 token、间距标准刻度、Tk point 字号、
6 项稳定导航与只读映射、team/access 拆分、有界缩放、真实前背景对比度、纯模块约束。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import re
import unittest
from pathlib import Path
from types import MappingProxyType

from agentbus import workbench_theme as theme

HEX_RE = re.compile(r"^#[0-9a-f]{6}$")
ORIGINAL_COLORS = {
    "background": "#f3f5f8",
    "surface": "#ffffff",
    "text": "#1f2937",
    "secondary": "#5b6575",
    "border": "#d8dee8",
    "accent": "#2459b8",
    "accent_hover": "#1e4c9d",
}
STABLE_NAV_IDS = ("home", "team", "tasks", "access", "settings", "logs")


class PaletteTests(unittest.TestCase):
    def test_palette_includes_original_colors_exactly(self):
        for key, value in ORIGINAL_COLORS.items():
            self.assertIn(key, theme.PALETTE)
            self.assertEqual(theme.PALETTE[key], value)

    def test_palette_values_are_lowercase_hex(self):
        for key, value in theme.PALETTE.items():
            self.assertIsInstance(value, str, msg=key)
            self.assertRegex(value, HEX_RE, msg=f"{key}={value!r}")

    def test_palette_unifies_state_disabled_input_tokens(self):
        for key in ("ok", "warn", "err", "muted", "input_bg",
                    "surface_warn", "disabled_bg", "disabled_text", "on_accent"):
            self.assertIn(key, theme.PALETTE, msg=key)


class SpacingTests(unittest.TestCase):
    def test_spacing_is_standard_scale(self):
        self.assertEqual(theme.SPACING,
                         {"xs": 4, "sm": 8, "md": 16, "lg": 24, "xl": 32})

    def test_space_and_pad_helpers(self):
        self.assertEqual(theme.space("md"), 16)
        self.assertEqual(theme.pad("md"), (16, 16))
        self.assertEqual(theme.pad("xs"), (4, 4))
        with self.assertRaises(KeyError):
            theme.space("nope")
        with self.assertRaises(KeyError):
            theme.pad("nope")


class TypeScaleTests(unittest.TestCase):
    def test_type_scale_are_positive_tk_points(self):
        for key, value in theme.TYPE_SCALE.items():
            self.assertIsInstance(value, int, msg=key)
            self.assertFalse(isinstance(value, bool), msg=key)
            self.assertGreater(value, 0, msg=key)

    def test_body_and_title_ranges(self):
        self.assertIn(theme.TYPE_SCALE["body"], (10, 11))
        self.assertGreaterEqual(theme.TYPE_SCALE["title"], 18)
        self.assertLessEqual(theme.TYPE_SCALE["title"], 22)

    def test_font_points_is_identity_not_scaled(self):
        for key in theme.TYPE_SCALE:
            self.assertEqual(theme.font_points(key), theme.TYPE_SCALE[key])
        with self.assertRaises(KeyError):
            theme.font_points("nope")


class NavTests(unittest.TestCase):
    def test_nav_has_six_stable_ids_in_order(self):
        self.assertEqual(tuple(i["id"] for i in theme.NAV_ITEMS), STABLE_NAV_IDS)
        self.assertEqual(theme.NAV_ORDER, STABLE_NAV_IDS)

    def test_nav_ids_unique(self):
        ids = [i["id"] for i in theme.NAV_ITEMS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_nav_items_only_three_keys(self):
        for item in theme.NAV_ITEMS:
            self.assertEqual(set(item), {"id", "title", "subtitle"})

    def test_nav_text_nonempty_and_bounded(self):
        for item in theme.NAV_ITEMS:
            self.assertTrue(item["title"].strip(), msg=item["id"])
            self.assertTrue(item["subtitle"].strip(), msg=item["id"])
            self.assertLessEqual(len(item["title"]), 12, msg=item["id"])
            self.assertLessEqual(len(item["subtitle"]), 32, msg=item["id"])

    def test_nav_by_id_maps_and_is_readonly(self):
        self.assertEqual(set(theme.NAV_BY_ID), set(STABLE_NAV_IDS))
        self.assertEqual(theme.NAV_BY_ID["home"]["title"], "工作台")
        with self.assertRaises(TypeError):
            theme.NAV_BY_ID["home"] = {}  # type: ignore[index]

    def test_team_and_access_split_reflected(self):
        team = theme.NAV_BY_ID["team"]["subtitle"]
        access = theme.NAV_BY_ID["access"]["subtitle"]
        self.assertIn("成员", team)
        self.assertIn("组长", team)
        self.assertIn("接入", access)
        self.assertIn("技能", access)


class ScalingTests(unittest.TestCase):
    def test_baseline_scaling_is_identity(self):
        self.assertAlmostEqual(theme.BASELINE_SCALING, 96 / 72)
        self.assertEqual(theme.scaled_px(180, theme.BASELINE_SCALING), 180)

    def test_scaled_px_grows_and_is_bounded(self):
        self.assertGreater(theme.scaled_px(180, 2.0), theme.scaled_px(180, 1.0))
        self.assertLessEqual(theme.scaled_px(180, 99.0), 4096)
        self.assertGreaterEqual(theme.scaled_px(0, 1.0), 1)

    def test_clamp_scaling_bounds_and_bad_input(self):
        self.assertAlmostEqual(theme.clamp_scaling(0), theme.BASELINE_SCALING)
        self.assertAlmostEqual(theme.clamp_scaling("bad"), theme.BASELINE_SCALING)
        self.assertLessEqual(theme.clamp_scaling(1000), theme.MAX_SCALING)
        self.assertGreaterEqual(theme.clamp_scaling(0.001), theme.BASELINE_SCALING)

    def test_scaled_px_rejects_bad_types(self):
        with self.assertRaises(TypeError):
            theme.scaled_px(True, 1.0)
        with self.assertRaises(ValueError):
            theme.scaled_px(-1, 1.0)


class ContrastTests(unittest.TestCase):
    def test_luminance_bounds(self):
        self.assertAlmostEqual(theme.relative_luminance("#000000"), 0.0, places=6)
        self.assertAlmostEqual(theme.relative_luminance("#ffffff"), 1.0, places=6)

    def test_contrast_extremes_and_symmetry(self):
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(theme.contrast_ratio("#123456", "#123456"), 1.0, places=6)
        self.assertAlmostEqual(theme.contrast_ratio("#2459b8", "#ffffff"),
                               theme.contrast_ratio("#ffffff", "#2459b8"), places=6)

    def test_all_required_pairs_meet_threshold(self):
        self.assertTrue(theme.CONTRAST_REQUIREMENTS)
        for fg, bg, minimum in theme.CONTRAST_REQUIREMENTS:
            with self.subTest(fg=fg, bg=bg):
                ratio = theme.contrast_ratio(theme.PALETTE[fg], theme.PALETTE[bg])
                self.assertGreaterEqual(round(ratio, 2), minimum,
                                        msg=f"{fg} on {bg} = {ratio:.3f} < {minimum}")


class PurityTests(unittest.TestCase):
    def test_module_has_no_gui_toolkit_or_new_dependency(self):
        source = Path(theme.__file__).read_text(encoding="utf-8")
        self.assertNotIn("tk" + "inter", source)
        self.assertNotIn("Tk(", source)
        self.assertNotIn("Toplevel(", source)

    def test_module_level_only_stdlib_imports(self):
        source = Path(theme.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                self.assertTrue(
                    stripped.startswith(("from __future__", "from types",
                                         "from typing")),
                    msg=f"unexpected import: {stripped}")

    def test_nav_by_id_is_mappingproxy(self):
        self.assertIsInstance(theme.NAV_BY_ID, MappingProxyType)


if __name__ == "__main__":
    unittest.main()