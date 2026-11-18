#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_menu_keyboard_accessibility.py: 原生风格菜单支持完整键盘焦点规则测试。

来源：§13.12 CX13Q-MENU-KEYBOARD（P2）。
验收标准：
1. 菜单支持 W3C APG 标准 roving tabindex 与键盘导航；
2. Trigger 支持 ArrowDown / Enter / Space 打开并聚焦首项；
3. 菜单项之间支持 ArrowDown / ArrowUp 循环导航与 Home / End 极值定位；
4. Escape 关闭菜单并将焦点正确归还触发者；
5. ArrowRight / ArrowLeft 支持顶级菜单项间切换。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

# 导入待测集成模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
import sys
sys.path.insert(0, str(PROJECT_DIR))


class TestMenuKeyboardAccessibility(unittest.TestCase):
    def setUp(self):
        self.app_js_path = PROJECT_DIR / "agentbus" / "web_assets" / "app.js"
        self.assertTrue(self.app_js_path.exists(), "app.js 文件必须存在")
        self.content = self.app_js_path.read_text(encoding="utf-8")

    def test_roving_tabindex_assigned_to_menu_actions(self):
        """测试菜单项已赋予 tabindex='-1' 以支持 roving tabindex。"""
        self.assertIn("act.setAttribute('tabindex','-1')", self.content)

    def test_trigger_opens_and_focuses_first_item(self):
        """测试 trigger 响应 ArrowDown/Enter/Space 并聚焦 firstAction。"""
        self.assertIn("e.key==='ArrowDown'||e.key==='Enter'||e.key===' '", self.content)
        self.assertIn("firstAction.focus()", self.content)

    def test_navigation_keys_handled(self):
        """测试 ArrowDown, ArrowUp, Home, End, Escape 导航按键均有对应分支。"""
        self.assertIn("e.key==='ArrowDown'", self.content)
        self.assertIn("e.key==='ArrowUp'", self.content)
        self.assertIn("e.key==='Home'", self.content)
        self.assertIn("e.key==='End'", self.content)
        self.assertIn("e.key==='Escape'", self.content)
        self.assertIn("trigger.focus()", self.content)

    def test_left_right_switching_handled(self):
        """测试左右箭头支持切到上一个/下一个顶级菜单。"""
        self.assertIn("e.key==='ArrowRight'", self.content)
        self.assertIn("e.key==='ArrowLeft'", self.content)


if __name__ == "__main__":
    unittest.main()
