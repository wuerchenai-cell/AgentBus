#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for Section 6.9 COMMAND-PALETTE:
Verifying global Command Palette DOM structure, ARIA accessibility semantics,
hotkey bindings, action catalogs, task locating, and shortcuts guide integration.
"""
import unittest
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parents[1]
_WEB_ASSETS = _ROOT_DIR / "agentbus/web_assets"


class TestCommandPalette(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (_WEB_ASSETS / "index.html").read_text(encoding="utf-8")
        cls.js = (_WEB_ASSETS / "app.js").read_text(encoding="utf-8")
        cls.css = (_WEB_ASSETS / "app.css").read_text(encoding="utf-8")

    def test_dom_structure_and_aria_attributes(self):
        """1. Verify Command Palette DOM hierarchy and ARIA combobox accessibility semantics."""
        self.assertIn('id="commandPaletteBackdrop"', self.html)
        self.assertIn('id="commandPaletteModal"', self.html)
        self.assertIn('role="dialog"', self.html)
        self.assertIn('aria-modal="true"', self.html)
        self.assertIn('id="commandPaletteInput"', self.html)
        self.assertIn('role="combobox"', self.html)
        self.assertIn('aria-expanded="true"', self.html)
        self.assertIn('aria-controls="commandPaletteResults"', self.html)
        self.assertIn('id="commandPaletteResults"', self.html)
        self.assertIn('role="listbox"', self.html)

    def test_trigger_buttons_in_toolbar_and_menu(self):
        """2. Verify quick action button and Tools menu item exist with hotkey hints."""
        self.assertIn('id="quickCommandPaletteBtn"', self.html)
        self.assertIn('Ctrl+K', self.html)
        self.assertIn('id="menuCommandPaletteAction"', self.html)

    def test_hotkey_registration_in_app_js(self):
        """3. Verify Ctrl+K / Cmd+K hotkey binding in app.js."""
        self.assertIn("event.key.toLowerCase() === 'k'", self.js)
        self.assertIn("openCommandPalette()", self.js)
        self.assertIn("closeCommandPalette()", self.js)

    def test_action_catalog_contains_essential_operations(self):
        """4. Verify Command Palette action catalog includes layout, sticky notes, minimap, wires, fit, etc."""
        self.assertIn("function getCommandPaletteActions()", self.js)
        for act in ["智能紧凑布局", "新建随手记便签", "全景小地图", "依赖连线", "全景显示任务森林", "恢复 1:1", "追加新任务", "深色 / 浅色", "导出当前任务树", "导出项目交接"]:
            self.assertIn(act, self.js, f"Missing essential action: {act}")

    def test_keyboard_navigation_and_locate_card(self):
        """5. Verify arrow key selection, Enter execution, and locateCard spatial transform logic."""
        self.assertIn("function locateCard(leafId)", self.js)
        self.assertIn("locate-pulse", self.js)
        self.assertIn("ArrowDown", self.js)
        self.assertIn("ArrowUp", self.js)
        self.assertIn("aria-activedescendant", self.js)

    def test_shortcuts_guide_integration(self):
        """6. Verify Command Palette hotkey is documented in shortcuts guide."""
        self.assertIn("Ctrl+K / Cmd+K", self.js)
        self.assertIn("呼出全局命令面板", self.js)

    def test_css_styling_and_pulse_animation(self):
        """7. Verify Command Palette backdrop, glassmorphism modal, and locate-pulse animation in app.css."""
        self.assertIn(".command-palette-backdrop", self.css)
        self.assertIn(".command-palette-modal", self.css)
        self.assertIn(".command-palette-input", self.css)
        self.assertIn(".locate-pulse", self.css)
        self.assertIn("@keyframes card-locate-pulse", self.css)

    def test_api_export_in_agentbus_ui(self):
        """8. Verify commandPalette API is exposed in window.AgentBusUI for automation."""
        self.assertIn("commandPalette:{open:openCommandPalette", self.js)


if __name__ == "__main__":
    unittest.main()
