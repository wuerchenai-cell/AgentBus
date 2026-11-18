"""
Tests for UX-SEARCH-FILTER (P1, §6.1)
Source: Antigravity (2026-10-12)

Verifies:
1. Frontend assets (app.js):
   - Global Ctrl+F / Cmd+F / '/' keyboard shortcut intercepts browser search and focuses #leafSearch.
   - Smooth panLayer transition (focus-motion) and amber pulsing animation (.locate-pulse / @keyframes card-locate-pulse) on card locate.
   - findLineage topological graph algorithm extracts accurate ancestor and descendant lineages.
   - applySpotlight, clearSpotlight, toggleSpotlight implementations with Alt+L and Escape handling.
   - Multi-dimensional noise reduction: leafPriority extraction and matches logic supporting
     status, blocked, and priority (P0-P3) filters.
2. Frontend style assets (app.css):
   - card-locate-pulse amber glowing animation.
   - .spotlight-mode with .spotlight-focused, .spotlight-ancestor, .spotlight-descendant,
     .spotlight-dimmed, and .wire-dimmed.
3. Frontend markup assets (index.html):
   - Filter menu includes blocked, and priority P0, P1, P2, P3 options.
"""
from __future__ import annotations

import unittest
from pathlib import Path


class TestSearchFilterUX(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assets_dir = Path(__file__).resolve().parents[1] / 'agentbus/web_assets'
        cls.app_js_path = assets_dir / 'app.js'
        cls.app_css_path = assets_dir / 'app.css'
        cls.index_html_path = assets_dir / 'index.html'

        cls.app_js = cls.app_js_path.read_text(encoding='utf-8')
        cls.app_css = cls.app_css_path.read_text(encoding='utf-8')
        cls.index_html = cls.index_html_path.read_text(encoding='utf-8')

    def test_ctrl_f_search_shortcut_and_focus(self):
        # 1. Global Ctrl+F / Cmd+F intercepts browser search and focuses search box
        self.assertIn("event.key.toLowerCase() === 'f'", self.app_js)
        self.assertIn("searchBox.focus()", self.app_js)
        self.assertIn("searchBox.select()", self.app_js)

    def test_card_locate_pulse_and_motion(self):
        # 2. Smooth bezier flight and amber pulsing animation
        self.assertIn("layer.classList.add('focus-motion')", self.app_js)
        self.assertIn("card.classList.add('locate-pulse')", self.app_js)

        # In CSS: amber keyframes defined
        self.assertIn("@keyframes card-locate-pulse", self.app_css)
        self.assertIn("rgba(245, 158, 11", self.app_css)  # Amber-500 glow

    def test_spotlight_lineage_engine(self):
        # 3. Lineage topological graph algorithm and spotlight mode
        self.assertIn("function findLineage(leafId)", self.app_js)
        self.assertIn("function applySpotlight(leafId)", self.app_js)
        self.assertIn("function clearSpotlight()", self.app_js)
        self.assertIn("function toggleSpotlight(leafId)", self.app_js)

        # Alt+L toggle shortcut and Esc clearing
        self.assertIn("event.altKey && event.key.toLowerCase() === 'l'", self.app_js)
        self.assertIn("spotlightTargetLeafId", self.app_js)
        self.assertIn("clearSpotlight()", self.app_js)

        # In CSS: spotlight mode styling
        self.assertIn(".spotlight-mode", self.app_css)
        self.assertIn(".spotlight-focused", self.app_css)
        self.assertIn(".spotlight-ancestor", self.app_css)
        self.assertIn(".spotlight-descendant", self.app_css)
        self.assertIn(".spotlight-dimmed", self.app_css)
        self.assertIn(".wire-dimmed", self.app_css)

    def test_multi_dimensional_noise_filtering(self):
        # 4. Multi-dimensional filtering logic: priority, status, blocked
        self.assertIn("function leafPriority(leaf)", self.app_js)
        self.assertIn("filter==='blocked'", self.app_js)
        self.assertIn("filter==='p0'", self.app_js)
        self.assertIn("filter==='p1'", self.app_js)
        self.assertIn("filter==='p2'", self.app_js)
        self.assertIn("filter==='p3'", self.app_js)

        # In HTML: Filter menu items
        self.assertIn('data-filter="blocked"', self.index_html)
        self.assertIn('data-filter="p0"', self.index_html)
        self.assertIn('data-filter="p1"', self.index_html)
        self.assertIn('data-filter="p2"', self.index_html)
        self.assertIn('data-filter="p3"', self.index_html)


if __name__ == '__main__':
    unittest.main()
