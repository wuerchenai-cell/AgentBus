# -*- coding: utf-8 -*-
import unittest
from pathlib import Path

class TestThemeComputedStyles(unittest.TestCase):
    def test_dark_css_syntax_validity(self):
        css_path = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets" / "app.css"
        content = css_path.read_text(encoding="utf-8")
        self.assertNotIn("body[data-theme=\"dark\"],@media", content)
        self.assertIn("body[data-theme=\"dark\"]", content)

if __name__ == "__main__":
    unittest.main()
