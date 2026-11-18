"""A live independent audit is pending work, not a pipeline deadlock."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbus.config import load
from agentbus.task_tree import TaskForest
from test_review_20261108_desktop_audit import DesktopAuditRegression


class AuditDeadlockTests(unittest.TestCase):
    def test_waiting_for_review_is_not_a_deadlock_but_real_block_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            forest = TaskForest(load(Path(directory), create=True))
            pending = {'id': 'audit', 'title': '待审核', 'state': 'awaiting_audit',
                       'claimable': False, 'dependency_ready': False, 'dependencies': []}
            waiting = {'id': 'next', 'title': '后续', 'state': 'blocked',
                       'claimable': False, 'dependency_ready': False, 'dependencies': ['audit']}
            snap = {'roots': [], 'branches': [], 'leaves': [pending, waiting]}
            self.assertFalse(forest.diagnose_deadlock(snap)['is_deadlocked'])
            pending['evidence_error'] = '审核任务已过期'
            self.assertTrue(forest.diagnose_deadlock(snap)['is_deadlocked'])
            pending['evidence_error'] = ''
            pending['state'] = 'rejected'
            self.assertTrue(forest.diagnose_deadlock(snap)['is_deadlocked'])


class RejectedReasonTests(DesktopAuditRegression):
    def test_rejection_explains_review_not_missing_executor(self):
        bridge, forest = self.pending()
        self.assertTrue(bridge.call('claim_audit', {'leaf_id':'work','agent':'reviewer'})['ok'])
        result = self.decision(bridge, decision='reject', reason='实际成果缺少说明')
        self.assertTrue(result['ok'], result)
        leaf = forest.snapshot()['leaves'][0]
        self.assertEqual(leaf['state'], 'rejected')
        self.assertIn('审核', leaf['blocked_reason'])
        self.assertNotIn('未配置可执行成员', leaf['blocked_reason'])


if __name__ == '__main__':
    unittest.main()
