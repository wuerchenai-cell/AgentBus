"""JR-20261108-H01: an explicit verification waiver is visible and bounded."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main
from agentbus.config import load
from agentbus.task_tree import TaskForest


class VerificationWaiverRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.forest = TaskForest(load(self.workspace, create=True))
        self.forest.seed_leaf({'id': 'work', 'title': 'Explicit syntax waiver',
                              'expected_artifacts': ['module.py']}, branch_id='checks')
        self.bus = self.forest.bus
        self.task = self.forest.claim_leaf('work', 'workbuddy')
        self.ticket = captured_ticket(self.task)
        self.bus.heartbeat('workbuddy', self.task, self.ticket, note='test task in progress')

    def answer(self, *options):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = main(['answer', '--workspace', str(self.workspace), '--agent', 'workbuddy',
                       '--body', 'Explicitly waived static syntax, remaining checks enforced',
                       '--claim-epoch', str(self.ticket.claim_epoch), '--claim-id', self.ticket.claim_id,
                       '--artifacts', 'module.py', *options])
        return rc, out.getvalue()

    def test_waiver_is_audited_and_displayed_as_not_executed(self):
        (self.workspace / 'module.py').write_text('def incomplete(\n', encoding='utf-8')
        rc, _ = self.answer()
        self.assertNotEqual(rc, 0)
        rc, text = self.answer('--skip-verify')
        self.assertEqual(rc, 0, text)
        self.assertIn('未执行', text)
        self.assertIn('--skip-verify', text)
        events = [event for event in self.bus.events()
                  if event.get('event') == 'pre_answer_verification_skipped']
        self.assertEqual(len(events), 1)
        self.assertIs(events[0].get('checks_executed'), False)
        self.assertEqual(events[0].get('id'), self.task.id)
        self.assertTrue(self.bus.find_results(self.task.id))

    def test_waiver_does_not_allow_missing_expected_artifact(self):
        rc, text = self.answer('--skip-verify')
        self.assertNotEqual(rc, 0, text)
        self.assertFalse(self.bus.find_results(self.task.id))
