"""Production frontend behavior; replaces empty audit scaffold, 2026-10-14."""
import unittest
from test_frontend_closeout import FrontendCloseoutScenarios


class TestUndoRealSemantics(FrontendCloseoutScenarios, unittest.TestCase):
    def test_rename_undo_failure_keeps_history_and_reports_error(self):
        self.check_rename_undo_failure_keeps_history_and_reports_error()

    def test_rename_undo_waits_for_bridge_ack_before_moving_history(self):
        self.check_rename_undo_waits_for_bridge_ack_before_moving_history()

    def test_clone_is_history_barrier_and_never_promises_fake_undo(self):
        self.check_clone_is_history_barrier_and_never_promises_fake_undo()

    def test_switching_workspace_clears_private_undo_history(self):
        self.check_switching_workspace_clears_private_undo_history()


if __name__ == '__main__':
    unittest.main()
