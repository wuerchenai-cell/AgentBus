"""Production frontend behavior; replaces empty audit scaffold, 2026-10-14."""
import unittest
from test_frontend_closeout import FrontendCloseoutScenarios


class TestOnboardClipboardFeedback(FrontendCloseoutScenarios, unittest.TestCase):
    def test_copy_exception_cleans_temporary_field_and_reports_failure(self):
        self.check_copy_exception_cleans_temporary_field_and_reports_failure()

    def test_onboarding_copy_failure_keeps_prompt_without_claiming_clipboard_success(self):
        self.check_onboarding_copy_failure_keeps_prompt_without_claiming_clipboard_success()


if __name__ == '__main__':
    unittest.main()
