"""JR-20261108-R03: recovery cannot infer consent from missing or truthy fields."""
from pathlib import Path
import tempfile
import unittest

from agentbus.claim_fencing import captured_ticket
from agentbus.bus import BusError
from agentbus.config import load
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class RecoveryConfirmationRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.forest = TaskForest(load(self.workspace, create=True))
        self.forest.seed_leaf({'id': 'work', 'title': 'Work in progress'}, branch_id='work-branch')
        self.bus = self.forest.bus
        self.task = self.forest.claim_leaf('work', 'workbuddy')
        self.ticket = captured_ticket(self.task)
        self.bus.heartbeat('workbuddy', self.task, self.ticket, note='Active worker')
        self.bridge = WebBridge(self.workspace)

    def test_missing_confirmation_is_rejected_without_mutation(self):
        before = self.bus.processing('workbuddy')[0].read_bytes()
        with self.assertRaisesRegex(ValueError, '确认'):
            self.bridge._recover_task({'task_id': self.task.id, 'agent': 'workbuddy'})
        self.assertEqual(self.bus.processing('workbuddy')[0].read_bytes(), before)

    def test_false_and_nonboolean_confirmations_are_rejected(self):
        before = self.bus.processing('workbuddy')[0].read_bytes()
        for flag in (None, False, 'true', 'false', 1, [], {}):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(ValueError, '确认'):
                    self.bridge._recover_task({'task_id': self.task.id, 'agent': 'workbuddy',
                                                'owner_confirmed_inactive': flag})
                self.assertEqual(self.bus.processing('workbuddy')[0].read_bytes(), before)

    def test_explicit_recovery_revokes_old_ticket_and_allows_a_new_claim(self):
        result = self.bridge._recover_task({'task_id': self.task.id, 'agent': 'workbuddy',
                                            'owner_confirmed_inactive': True,
                                            'expected_claim_epoch': self.task.claim_epoch})
        self.assertTrue(result['recovered'])
        newer = self.forest.claim_leaf('work', 'workbuddy')
        self.assertGreater(newer.claim_epoch, self.task.claim_epoch)
        self.bus.heartbeat('workbuddy', newer, captured_ticket(newer), note='Replacement worker')
        with self.assertRaises(BusError):
            self.bus.commit_result('workbuddy', self.task, self.ticket,
                                   body='Late result from previous worker', exit_code=0)
        self.assertFalse(self.bus.find_results(self.task.id))
        self.bus.commit_result('workbuddy', newer, captured_ticket(newer),
                               body='Actual replacement work completed', exit_code=0)
        self.assertEqual(self.bus.find_message(newer.id).status, 'done')
