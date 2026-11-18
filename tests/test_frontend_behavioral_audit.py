"""Production frontend behavior; replaces empty audit scaffold, 2026-10-14."""
import unittest
from test_frontend_closeout import FrontendCloseoutScenarios


class TestFrontendBehavioralAudit(FrontendCloseoutScenarios, unittest.TestCase):
    def test_menu_arrows_skip_disabled_items_and_home_end(self):
        self.check_menu_arrows_skip_disabled_items_and_home_end()

    def test_escape_from_open_menu_restores_trigger_focus(self):
        self.check_escape_from_open_menu_restores_trigger_focus()

    def test_export_menu_exposes_both_existing_formats(self):
        self.check_export_menu_exposes_both_existing_formats()

    def test_takeover_requires_explicit_confirmation_and_enabled_executor(self):
        self.check_takeover_requires_explicit_confirmation_and_enabled_executor()

    def test_takeover_without_other_executor_shows_empty_state(self):
        self.check_takeover_without_other_executor_shows_empty_state()

    def test_takeover_copy_failure_retains_actual_capsule_for_manual_copy(self):
        self.check_takeover_copy_failure_retains_actual_capsule_for_manual_copy()


if __name__ == '__main__':
    unittest.main()
