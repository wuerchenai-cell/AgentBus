"""Production frontend behavior; replaces empty audit scaffold, 2026-10-14."""
import unittest
from test_frontend_closeout import FrontendCloseoutScenarios


class TestLargeForestViewport(FrontendCloseoutScenarios, unittest.TestCase):
    def test_first_view_contains_160_and_320_leaf_scenes(self):
        self.check_first_view_contains_160_and_320_leaf_scenes()

    def test_fit_includes_negative_custom_positions_and_all_nodes(self):
        self.check_fit_includes_negative_custom_positions_and_all_nodes()

    def test_low_fit_zoom_can_be_restored(self):
        self.check_low_fit_zoom_can_be_restored()

    def test_low_zoom_controls_change_in_requested_direction(self):
        self.check_low_zoom_controls_change_in_requested_direction()

    def test_initial_fit_tracks_viewport_height_after_layout_settles(self):
        self.check_initial_fit_tracks_viewport_height_after_layout_settles()

    def test_fit_uses_actual_layout_dimensions_larger_than_dataset_estimate(self):
        self.check_fit_uses_actual_layout_dimensions_larger_than_dataset_estimate()


if __name__ == '__main__':
    unittest.main()
