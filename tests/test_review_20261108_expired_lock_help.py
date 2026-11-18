"""JR-20261108-H02: expired task locks need recovery, not an unrelated lease wait."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main
from agentbus.config import load
from agentbus.exclusive_lock import lock_record_path
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class ExpiredLockGuidanceRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.forest = TaskForest(load(self.workspace, create=True))
        self.forest.seed_leaf({'id': 'leaf', 'title': 'Task lock recovery'}, branch_id='work')
        self.bridge = WebBridge(self.workspace)
        claimed = self.bridge._claim_leaf({'leaf_id': 'leaf', 'agent': 'workbuddy'})
        self.task = self.forest.bus._active_record('workbuddy', claimed['task_id'])
        self.ticket = captured_ticket(self.task)
        self.path = lock_record_path(self.forest.root, self.task.id)

    def heartbeat(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = main(['heartbeat', '--workspace', str(self.workspace), '--agent', 'workbuddy',
                       '--id', self.task.id, '--claim-epoch', str(self.ticket.claim_epoch),
                       '--claim-id', self.ticket.claim_id])
        return rc, out.getvalue()

    def test_expired_lock_gives_confirmed_recovery_steps_without_mutation(self):
        lock = json.loads(self.path.read_text(encoding='utf-8'))
        lock['expires_ts'] = time.time() - 1
        self.path.write_text(json.dumps(lock), encoding='utf-8')
        original = self.path.read_bytes()
        source = self.forest.bus.processing('workbuddy')[0].read_bytes()
        rc, text = self.heartbeat()
        self.assertNotEqual(rc, 0, text)
        self.assertIn('LOCK_EXPIRED', text)
        self.assertIn('recover', text)
        self.assertIn('--owner-confirmed-inactive', text)
        self.assertIn('新票据', text)
        self.assertNotIn('其他成员独占锁定', text)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.forest.bus.processing('workbuddy')[0].read_bytes(), source)

    def test_valid_original_ticket_renews_lock(self):
        before = json.loads(self.path.read_text(encoding='utf-8'))
        rc, text = self.heartbeat()
        self.assertEqual(rc, 0, text)
        after = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertGreaterEqual(after['expires_ts'], before['expires_ts'])
        self.assertEqual(after['claim_id'], before['claim_id'])

    def test_desktop_error_explains_recovery_without_reviving_lock(self):
        lock = json.loads(self.path.read_text(encoding='utf-8'))
        lock['expires_ts'] = time.time() - 1
        self.path.write_text(json.dumps(lock), encoding='utf-8')
        before = self.path.read_bytes()
        result = self.bridge.call('submit_leaf_result', {
            'leaf_id': 'leaf', 'agent': 'workbuddy', 'summary': 'Actual completed work',
            'operator_confirmed': True})
        self.assertFalse(result['ok'])
        self.assertIn('LOCK_EXPIRED', result['error'])
        self.assertIn('原执行者停止', result['error'])
        self.assertIn('新票据', result['error'])
        self.assertEqual(self.path.read_bytes(), before)
