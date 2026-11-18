"""JR-20261108-D01: administrator decisions are distinct from delivery evidence."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main
from agentbus.config import load, save
from agentbus.task_tree import TaskForest, TaskTreeError
from agentbus.workspace_files import WorkspaceFiles


class AdministratorPassRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.cfg = load(self.workspace, create=True)
        self.forest = TaskForest(self.cfg)
        self.forest.replace_structure({
            'roots': [{'id': 'ROOT', 'title': 'Isolated project'}],
            'branches': [{'id': 'UPSTREAM', 'parent_id': 'ROOT', 'title': 'Upstream'},
                         {'id': 'DOWNSTREAM', 'parent_id': 'ROOT', 'title': 'Downstream'}],
            'leaves': [{'id': 'A', 'parent_id': 'UPSTREAM', 'title': 'First task'},
                       {'id': 'B', 'parent_id': 'DOWNSTREAM', 'title': 'Dependent task',
                        'dependencies': ['UPSTREAM']}],
        })

    def test_pass_unblocks_branch_dependency_without_certifying_artifacts(self):
        self.forest.pass_leaf('A', 'Owner resolved a historical dependency')
        snapshot = {leaf['id']: leaf for leaf in self.forest.snapshot()['leaves']}
        self.assertEqual(snapshot['A']['state'], 'admin_passed')
        self.assertEqual(snapshot['A']['receipt_state'], 'missing')
        self.assertEqual(snapshot['A']['artifact_state'], 'not_checked')
        self.assertIsNone(snapshot['A']['task_id'])
        self.assertTrue(snapshot['B']['claimable'])

        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = main(['tree', 'artifacts', '--workspace', str(self.workspace), '--export', 'output.zip'])
        self.assertNotEqual(rc, 0)
        self.assertIn('管理员放行', output.getvalue())
        self.assertNotIn('全部通过', output.getvalue())
        self.assertFalse((self.workspace / 'output.zip').exists())

    def test_pass_preserves_existing_evidence_error(self):
        data = self.forest._structure()
        data['leaves'][0]['task_id'] = 'missing-original-task'
        self.forest.replace_structure(data)
        original = self.forest.snapshot()['leaves'][0]['evidence_error']
        self.assertTrue(original)
        self.forest.pass_leaf('A', 'Owner acknowledges missing evidence')
        self.assertEqual(self.forest.snapshot()['leaves'][0]['evidence_error'], original)

    def _start_supervised_work(self):
        self.cfg.data['supervision'] = {
            'strict_supervision': True, 'designated_auditor': 'trae',
            'checks': [{'name': 'isolated tests', 'argv': [sys.executable, '-m', 'unittest',
                        'test_acceptance'], 'timeout_seconds': 10}],
        }
        save(self.cfg)
        (self.workspace / 'test_acceptance.py').write_text(
            'import unittest\nclass Acceptance(unittest.TestCase):\n'
            '    def test_success(self):\n        self.assertTrue(True)\n', encoding='utf-8')
        (self.workspace / 'artifact.txt').write_text('actual output', encoding='utf-8')
        self.forest = TaskForest(load(self.workspace))
        bus = self.forest.bus
        task = self.forest.claim_leaf('A', 'workbuddy')
        ticket = captured_ticket(task)
        bus.heartbeat('workbuddy', task, ticket, note='isolated task is in progress')
        return bus, task, ticket

    def _submit_work(self, bus, task, ticket):
        bus.commit_result('workbuddy', task, ticket, body='completed', exit_code=0,
                          artifacts=[WorkspaceFiles(self.workspace).describe_artifact('artifact.txt')])

    def test_pending_review_cannot_be_passed_or_projected_as_delivered(self):
        bus, task, ticket = self._start_supervised_work()
        with self.assertRaisesRegex(TaskTreeError, '审核'):
            self.forest.pass_leaf('A', 'attempted bypass before submitting')
        historical = self.forest._structure()
        historical['leaves'][0]['admin_override'] = {'admin': 'admin', 'reason': 'historical pass'}
        self.forest.replace_structure(historical)
        self.assertFalse(self.forest.snapshot()['leaves'][1]['claimable'])
        with self.assertRaises(TaskTreeError):
            self.forest.claim_leaf('B', 'codex')

        self._submit_work(bus, task, ticket)
        self.assertEqual(self.forest.snapshot()['leaves'][0]['state'], 'awaiting_audit')
        with self.assertRaisesRegex(TaskTreeError, '审核'):
            self.forest.pass_leaf('A', 'attempted bypass')
        self.assertEqual(bus.find_results(task.id), [])
        self.assertEqual(len(bus.pending('trae')), 1)

        # Keep older stored decisions readable without letting them override review state.
        leaves = self.forest.snapshot()['leaves']
        self.assertEqual(leaves[0]['state'], 'awaiting_audit')
        self.assertFalse(leaves[1]['claimable'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = main(['tree', 'artifacts', '--workspace', str(self.workspace), '--export', 'output.zip'])
        self.assertNotEqual(rc, 0)
        self.assertIn('等待独立审核', output.getvalue())
        self.assertFalse((self.workspace / 'output.zip').exists())

        # A pending independent review must remain intact even when an operator
        # asserts that the original execution session has stopped.
        with self.assertRaisesRegex(TaskTreeError, '审核'):
            self.forest.reset_leaf('A', 'Return work to the queue', owner_confirmed_inactive=True)
        self.assertEqual(self.forest.snapshot()['leaves'][0]['state'], 'awaiting_audit')
        self.assertFalse(self.forest.snapshot()['leaves'][1]['claimable'])

    def test_independent_approval_unlocks_an_actual_dependent_claim(self):
        bus, task, ticket = self._start_supervised_work()
        self._submit_work(bus, task, ticket)
        review = bus.claim('trae', bus.pending('trae')[0])
        review_ticket = captured_ticket(review)
        bus.heartbeat('trae', review, review_ticket, note='independent audit started')
        link = json.loads(review.body)
        bus.commit_result('trae', review, review_ticket, body=json.dumps({
            'schema_version': 1,
            'task_id': link['task_id'],
            'result_id': link['result_id'],
            'decision': 'approve',
            'artifacts_reviewed': True,
            'checks_reviewed': True,
            'text_scan_reviewed': True,
            'reason': 'independent audit completed',
        }))
        leaf = self.forest.snapshot()['leaves'][0]
        self.assertEqual((leaf['state'], leaf['receipt_state'], leaf['artifact_state']),
                         ('delivered', 'valid', 'verified'))
        with self.assertRaisesRegex(TaskTreeError, '无需管理员放行'):
            self.forest.pass_leaf('A', 'No current reason to waive a dependency')
        self.assertIsNotNone(self.forest.claim_leaf('B', 'codex'))
