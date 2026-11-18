"""JR-20261108-R04: the reserved auditor gets an actionable role explanation."""
import contextlib
import io
from pathlib import Path
import tempfile
import sys
import unittest

from agentbus.cli import main
from agentbus.config import load, save
from agentbus.task_tree import TaskForest, TaskTreeError


class AuditorRoleRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        cfg = load(self.workspace, create=True)
        cfg.data['supervision'] = {
            'strict_supervision': True, 'designated_auditor': 'trae',
            'checks': [{'name': 'acceptance tests', 'argv': [sys.executable, '-m', 'unittest'],
                        'timeout_seconds': 10}]}
        self.cfg = save(cfg)
        self.forest = TaskForest(self.cfg)
        self.forest.seed_leaf({'id': 'ordinary-task', 'title': 'Ordinary work'}, branch_id='work')

    def test_auditor_cannot_pull_ordinary_work_and_gets_a_role_reason(self):
        with self.assertRaisesRegex(TaskTreeError, '监督员.*审核'):
            self.forest.claim_next('trae')
        self.assertFalse(self.forest.bus.processing('trae'))
        self.assertIsNone(self.forest.snapshot()['leaves'][0]['task_id'])

    def test_direct_claim_and_cli_report_the_reserved_role(self):
        with self.assertRaisesRegex(TaskTreeError, '监督员.*审核'):
            self.forest.claim_leaf('ordinary-task', 'trae')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = main(['claim-next', '--agent', 'trae', '--wait', '30',
                       '--workspace', str(self.workspace)])
        self.assertNotEqual(rc, 0, output.getvalue())
        self.assertIn('监督员', output.getvalue())
        self.assertIn('inbox', output.getvalue())

    def test_workers_keep_claiming_and_auditor_still_receives_reviews(self):
        message = self.forest.claim_next('workbuddy')['message']
        self.assertEqual(message.to, 'workbuddy')
        bus = self.forest.bus
        review = bus.make('codex', 'trae', 'review', body='Independently inspect a result', result_to='codex')
        bus.post(review)
        claimed = bus.claim('trae', bus.pending('trae')[0])
        self.assertEqual((claimed.id, claimed.type), (review.id, 'review'))
