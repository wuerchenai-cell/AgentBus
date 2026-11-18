from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import unittest
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets"


class SmartLayoutTests(unittest.TestCase):
    def test_html_buttons_updated(self):
        html = (ASSETS / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="quickTidyBtn"', html)
        self.assertIn('✦ 智能对齐', html)
        self.assertIn('id="tidyView"', html)
        self.assertIn('smart-layout-btn', html)

    def test_css_contains_smart_layout_rules(self):
        css = (ASSETS / "app.css").read_text(encoding="utf-8")
        self.assertIn(".is-smart-layout-animating", css)
        self.assertIn(".smart-layout-btn", css)
        self.assertIn("cubic-bezier", css)

    def test_js_smart_multi_column_logic(self):
        js = (ASSETS / "app.js").read_text(encoding="utf-8")
        self.assertIn("pureLeaves.length > 4", js)
        self.assertIn("colX1=baseX+colW+colGap", js)
        self.assertIn("colCursors[0]<=colCursors[1]?0:1", js)
        self.assertIn("is-smart-layout-animating", js)
        self.assertIn("✦ 已应用智能化紧凑布局与拓扑顺流 (Alt+A)", js)


if __name__ == "__main__":
    unittest.main()
