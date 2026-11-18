"""Production frontend behavior; replaces empty audit scaffold, 2026-10-14."""
import unittest
from test_frontend_closeout import FrontendCloseoutScenarios


class TestPngLongForestExport(FrontendCloseoutScenarios, unittest.TestCase):
    def test_png_export_preserves_titles_and_uses_readable_tall_output(self):
        self.check_png_export_preserves_titles_and_uses_readable_tall_output()


if __name__ == '__main__':
    unittest.main()
