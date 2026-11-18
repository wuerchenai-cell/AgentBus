"""Production frontend behavior; replaces empty audit scaffold, 2026-10-14."""
import unittest
from test_frontend_closeout import FrontendCloseoutScenarios


class TestSvgFullForestExport(FrontendCloseoutScenarios, unittest.TestCase):
    def test_svg_export_contains_full_titles_and_nodes_even_when_collapsed(self):
        self.check_svg_export_contains_full_titles_and_nodes_even_when_collapsed()

    def test_svg_export_keeps_exact_curves_without_decorative_flow_duplicates(self):
        self.check_svg_export_keeps_exact_curves_without_decorative_flow_duplicates()

    def test_svg_dependency_links_remain_distinguishable_from_tree_edges(self):
        self.check_svg_dependency_links_remain_distinguishable_from_tree_edges()


if __name__ == '__main__':
    unittest.main()
