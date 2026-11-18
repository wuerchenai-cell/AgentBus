# -*- coding: utf-8 -*-
"""测试 AUDIT-DESIGN-SYSTEM-DARK-MODE-POLISH (§10.4) 现代化设计系统与深浅双模态"""
from __future__ import annotations

import unittest
from pathlib import Path

from agentbus.design_system import (
    TOKENS,
    DARK_TOKENS,
    ELEVATIONS,
    DARK_ELEVATIONS,
    get_tokens,
    contrast_ratio,
    contrast_ok,
    choose_layout,
    scaled,
)


class TestAuditDesignSystem(unittest.TestCase):
    def test_tokens_structure_and_completeness(self):
        """验证双模态令牌结构完整性"""
        light = get_tokens("light")
        dark = get_tokens("dark")
        
        required_keys = {
            "background",
            "surface",
            "text",
            "secondary",
            "border",
            "accent",
            "accent_hover",
            "ok",
            "warn",
            "err",
            "muted",
            "input_bg",
            "surface_warn",
            "disabled_bg",
            "disabled_text",
            "on_accent",
        }
        self.assertTrue(required_keys.issubset(set(light.keys())))
        self.assertTrue(required_keys.issubset(set(dark.keys())))
        self.assertEqual(len(light), len(dark))
        
        # 验证返回的是独立副本，不可原地污染共享状态
        light["custom"] = "#000000"
        self.assertNotIn("custom", TOKENS)

    def test_wcag_contrast_ratios(self):
        """验证双模态关键文本与交互元素满足 WCAG AA 4.5:1 可达性对比度要求"""
        light = get_tokens("light")
        dark = get_tokens("dark")
        
        # 亮色模式
        self.assertTrue(contrast_ok(light["text"], light["background"], minimum=4.5))
        self.assertTrue(contrast_ok(light["text"], light["surface"], minimum=4.5))
        self.assertTrue(contrast_ok(light["accent"], light["surface"], minimum=4.5))
        self.assertTrue(contrast_ok(light["on_accent"], light["accent"], minimum=4.5))
        
        # 暗色模式
        self.assertTrue(contrast_ok(dark["text"], dark["background"], minimum=4.5))
        self.assertTrue(contrast_ok(dark["text"], dark["surface"], minimum=4.5))
        self.assertTrue(contrast_ok(dark["accent"], dark["background"], minimum=4.5))
        self.assertTrue(contrast_ok(dark["on_accent"], dark["accent"], minimum=4.5))
        self.assertTrue(contrast_ok(dark["ok"], dark["background"], minimum=4.5))
        self.assertTrue(contrast_ok(dark["err"], dark["background"], minimum=4.5))

    def test_elevation_tokens(self):
        """验证景深阴影阶梯系统 (Elevation Tokens)"""
        levels = ("e1", "e2", "e3", "e4")
        for lvl in levels:
            self.assertIn(lvl, ELEVATIONS)
            self.assertIn(lvl, DARK_ELEVATIONS)
            self.assertIn("rgba(", ELEVATIONS[lvl])
            self.assertIn("rgba(", DARK_ELEVATIONS[lvl])

    def test_css_design_system_integration(self):
        """验证 app.css 包含完整的深色模式适配和景深阴影定义"""
        css_path = Path(__file__).resolve().parent.parent / "agentbus" / "web_assets" / "app.css"
        self.assertTrue(css_path.exists())
        content = css_path.read_text(encoding="utf-8")
        
        # 验证 elevation 变量
        self.assertIn("--elevation-1", content)
        self.assertIn("--elevation-2", content)
        self.assertIn("--elevation-3", content)
        self.assertIn("--elevation-4", content)
        
        # 验证深色模式支持
        self.assertIn('body[data-theme="dark"]', content)
        self.assertIn("prefers-color-scheme:dark", content)
        self.assertIn("color-scheme:dark", content)
        self.assertIn("drop-shadow", content)

    def test_responsive_layout_helpers(self):
        """验证布局自适应纯函数辅助"""
        self.assertEqual(choose_layout(800), "compact")
        self.assertEqual(choose_layout(1000), "normal")
        self.assertEqual(choose_layout(1500), "wide")
        
        # 缩放换算
        self.assertEqual(scaled(16, 1.3333333333333333), 16)
        self.assertGreater(scaled(16, 2.0), 16)


if __name__ == "__main__":
    unittest.main()
