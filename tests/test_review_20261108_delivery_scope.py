"""JR-20261108-D04: exports and submission responses reflect actual delivery."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agentbus.claim_fencing import captured_ticket
from agentbus.adapters import Result
from agentbus.cli import main
from agentbus.config import load, save
from agentbus.director import Director, Summary
from agentbus.mcp_server import AgentBusMcp
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge
from agentbus.workspace_files import WorkspaceFiles


class DeliveryScopeRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.cfg = load(self.workspace, create=True)
        self.forest = TaskForest(self.cfg)
        self.forest.seed_leaf({'id': 'first', 'title': 'First deliverable'}, branch_id='work')
        self.bus = self.forest.bus

    def cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = main([*args, '--workspace', str(self.workspace)])
        return rc, output.getvalue()

    def start(self, *, bridge=None):
        if bridge is None:
            task = self.forest.claim_leaf('first', 'workbuddy')
        else:
            claimed = bridge._claim_leaf({'leaf_id': 'first', 'agent': 'workbuddy'})
            task = self.bus._active_record('workbuddy', claimed['task_id'])
        ticket = captured_ticket(task)
        self.bus.heartbeat('workbuddy', task, ticket, note='working on isolated task')
        return task, ticket

    def artifact(self):
        (self.workspace / 'out.txt').write_text('Real deliverable', encoding='utf-8')
        return WorkspaceFiles(self.workspace).describe_artifact('out.txt')

    def complete(self, *, artifacts=()):
        task, ticket = self.start()
        self.bus.commit_result('workbuddy', task, ticket, body='Real work completed',
                               artifacts=list(artifacts), exit_code=0)

    def strict_supervision(self):
        self.cfg.data['supervision'] = {
            'strict_supervision': True, 'designated_auditor': 'trae',
            'checks': [{'name': 'acceptance tests', 'argv': [sys.executable, '-m', 'unittest',
                        'test_acceptance'], 'timeout_seconds': 10}],
        }
        save(self.cfg)
        (self.workspace / 'test_acceptance.py').write_text(
            'import unittest\nclass Acceptance(unittest.TestCase):\n'
            '    def test_success(self):\n        self.assertTrue(True)\n', encoding='utf-8')
        self.forest = TaskForest(load(self.workspace))
        self.bus = self.forest.bus

    def test_zero_of_one_does_not_generate_a_release(self):
        rc, text = self.cli('tree', 'artifacts', '--export', 'out.zip', '--summary-file', 'summary.md')
        self.assertNotEqual(rc, 0, text)
        self.assertIn('0/1', text)
        self.assertNotIn('全部通过', text)
        self.assertFalse((self.workspace / 'out.zip').exists())
        self.assertFalse((self.workspace / 'summary.md').exists())

    def test_no_file_delivery_is_valid_but_file_export_is_not_applicable(self):
        self.complete()
        rc, text = self.cli('tree', 'verify', '--summary-file', 'summary.md')
        self.assertEqual(rc, 0, text)
        self.assertIn('不适用', text)
        self.assertIn('不适用', (self.workspace / 'summary.md').read_text(encoding='utf-8'))
        rc, text = self.cli('tree', 'artifacts', '--export', 'empty.zip')
        self.assertNotEqual(rc, 0, text)
        self.assertFalse((self.workspace / 'empty.zip').exists())

    def test_explicit_partial_export_has_only_verified_files_and_clear_label(self):
        self.complete(artifacts=[self.artifact()])
        self.forest.seed_leaf({'id': 'second', 'title': 'Pending task'}, branch_id='work')
        args = ('tree', 'artifacts', '--export', 'out.zip', '--summary-file', 'summary.md')
        rc, text = self.cli(*args)
        self.assertNotEqual(rc, 0, text)
        self.assertFalse((self.workspace / 'out.zip').exists())
        rc, text = self.cli(*args, '--allow-partial')
        self.assertEqual(rc, 0, text)
        self.assertIn('部分', text)
        self.assertNotIn('全部通过', text)
        report = (self.workspace / 'summary.md').read_text(encoding='utf-8')
        self.assertIn('部分', report)
        self.assertIn('1/2', report)
        with zipfile.ZipFile(self.workspace / 'out.zip') as archive:
            self.assertEqual(archive.namelist(), ['out.txt'])

        previous = self.workspace / 'old-output'
        previous.mkdir()
        (previous / 'stale.txt').write_text('old file', encoding='utf-8')
        rc, text = self.cli('tree', 'artifacts', '--export', 'old-output', '--allow-partial')
        self.assertNotEqual(rc, 0, text)
        self.assertIn('空目录或新目录', text)
        self.assertEqual([path.name for path in previous.iterdir()], ['stale.txt'])

    def test_changed_artifact_does_not_replace_existing_export(self):
        self.complete(artifacts=[self.artifact()])
        archive = self.workspace / 'out.zip'
        archive.write_bytes(b'previous archive')
        open_zip = zipfile.ZipFile

        def change_artifact_after_initial_verification(path, mode, **options):
            (self.workspace / 'out.txt').write_text('different file at export time', encoding='utf-8')
            return open_zip(path, mode, **options)

        with patch('zipfile.ZipFile', side_effect=change_artifact_after_initial_verification):
            rc, text = self.cli('tree', 'artifacts', '--export', 'out.zip')
        self.assertNotEqual(rc, 0, text)
        self.assertIn('核验后发生变化', text)
        self.assertEqual(archive.read_bytes(), b'previous archive')
        self.assertFalse(list(self.workspace.glob('.agentbus-export-*')))

    def test_complete_directory_export_cannot_mix_unverified_old_files(self):
        self.complete(artifacts=[self.artifact()])
        target = self.workspace / 'previous-output'
        target.mkdir()
        (target / 'stale.txt').write_text('not in current manifest', encoding='utf-8')
        rc, text = self.cli('tree', 'artifacts', '--export', 'previous-output')
        self.assertNotEqual(rc, 0, text)
        self.assertIn('空目录或新目录', text)
        self.assertFalse((target / 'out.txt').exists())

    def test_cli_pending_audit_preserves_escalation_and_skips_peer_review(self):
        self.strict_supervision()
        self.artifact()
        task, ticket = self.start()
        escalation = self.bus.make('codex', 'human', 'escalation', body='Wait for verified work',
                                   reply_to=task.id)
        self.bus.post(escalation)
        with patch('agentbus.director.Director.schedule_peer_review') as peer_review:
            rc, text = self.cli('answer', '--agent', 'workbuddy', '--body', '成果已写入',
                                '--claim-epoch', str(ticket.claim_epoch), '--claim-id', ticket.claim_id,
                                '--artifacts', 'out.txt')
            self.assertEqual(rc, 0, text)
            self.assertIn('待审核', text)
            self.assertNotIn('已回投结果', text)
            peer_review.assert_not_called()
        self.assertFalse(self.bus.find_results(task.id))
        self.assertEqual(self.bus.pending('human'), [self.bus.dirs('human')['ready'] / f'{escalation.id}.json'])
        self.assertEqual(len(self.bus.pending('trae')), 1)

        review = self.bus.claim('trae', self.bus.pending('trae')[0])
        review_ticket = captured_ticket(review)
        self.bus.heartbeat('trae', review, review_ticket, note='reviewing independently')
        link = json.loads(review.body)
        self.bus.commit_result('trae', review, review_ticket, body=json.dumps({
            'schema_version': 1, 'task_id': task.id, 'result_id': link['result_id'],
            'decision': 'approve', 'artifacts_reviewed': True, 'checks_reviewed': True,
            'text_scan_reviewed': True, 'reason': 'Evidence verified independently',
        }))
        self.assertEqual(len(self.bus.find_results(task.id)), 1)
        self.assertFalse(self.bus.pending('human'))

    def test_mcp_pending_audit_reports_pending_status(self):
        self.strict_supervision()
        task, ticket = self.start()
        app = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        with patch('agentbus.director.Director.schedule_peer_review') as peer_review:
            text = app.report('workbuddy', task.id, '成果已写入', artifacts=[self.artifact()],
                              ticket=ticket.to_dict())
            self.assertIn('待独立审核', text)
            self.assertNotIn('已回投给', text)
            peer_review.assert_not_called()
        self.assertFalse(self.bus.find_results(task.id))

    def test_native_report_stays_pending_when_the_runner_returns(self):
        self.strict_supervision()
        task, ticket = self.start()
        artifacts = [self.artifact()]
        app = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        director = Director(load(self.workspace), bus=self.bus)
        self.addCleanup(director.close)
        summary = Summary()

        def report_from_session(agent, message):
            app.report(agent, message.id, 'Native session delivered',
                       artifacts=artifacts, ticket=ticket.to_dict())
            return Result(0, 'Session end summary', 0.2)

        with patch.object(director, 'execute', side_effect=report_from_session):
            with patch.object(director, 'schedule_peer_review') as peer_review:
                director._handle('workbuddy', task, summary, review=False)
                peer_review.assert_not_called()
        current = self.bus._active_record('workbuddy', task.id)
        self.assertEqual(current.status, 'awaiting_audit')
        self.assertEqual((summary.completed, summary.failed), (0, 0))
        self.assertEqual(len(self.bus.pending('trae')), 1)
        self.assertFalse(self.bus.find_results(task.id))

    def test_desktop_bridge_reports_pending_audit(self):
        self.strict_supervision()
        self.artifact()
        bridge = WebBridge(self.workspace)
        task, _ = self.start(bridge=bridge)
        with patch('agentbus.director.Director.schedule_peer_review') as peer_review:
            result = bridge._submit_leaf_result({'leaf_id': 'first', 'agent': 'workbuddy',
                'summary': '已写入文件', 'artifact_paths': ['out.txt'], 'operator_confirmed': True})
            self.assertEqual(result.get('submission_state'), 'awaiting_audit')
            peer_review.assert_not_called()
        self.assertFalse(self.bus.find_results(task.id))
