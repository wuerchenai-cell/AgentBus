"""JR-20261108-D03: transport results must commit, not orphan a live claim."""
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

from agentbus.bus import Bus, BusError
from agentbus import claim_fencing as fencing
from agentbus.cli import main
from agentbus.config import load, save
from agentbus.mcp_server import AgentBusMcp
from agentbus.workspace_files import WorkspaceFiles


class ResultCommitRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        load(self.workspace, create=True)
        self.bus = Bus(self.workspace / '.agentbus')
        self.bus.init()
        self.task = self.bus.make('codex', 'workbuddy', 'task', body='deliver result', result_to='codex')
        self.bus.post(self.task)
        self.message = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        self.ticket = fencing.captured_ticket(self.message)

    def heartbeat(self):
        self.bus.heartbeat('workbuddy', self.message, self.ticket, note='real executor progress')

    def cli_post(self, body='completed actual task', *options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(['post','--workspace',str(self.workspace),'--from','workbuddy',
                '--to','codex','--type','result','--reply-to',self.task.id,'--intent',body,
                '--claim-epoch',str(self.ticket.claim_epoch),'--claim-id',self.ticket.claim_id, *options])
        return code, output.getvalue()

    def test_cli_post_with_original_ticket_finishes_source_task(self):
        self.heartbeat()
        code, output = self.cli_post()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.bus.find_results(self.task.id)), 1)
        self.assertEqual(self.bus.processing('workbuddy'), [])
        terminal = self.bus.read(self.bus.dirs('workbuddy')['done'] / (self.task.id+'.json'))
        self.assertEqual(terminal.status, 'done')
        self.assertEqual(terminal.claim_id, self.ticket.claim_id)

    def test_mcp_send_with_original_ticket_finishes_source_task(self):
        self.heartbeat()
        app = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        app.session_send(to='codex', body='real completed work', type='result',
                         reply_to=self.task.id, ticket=self.ticket.to_dict())
        self.assertEqual(len(self.bus.find_results(self.task.id)), 1)
        self.assertEqual(self.bus.processing('workbuddy'), [])
        self.assertTrue((self.bus.dirs('workbuddy')['done'] / (self.task.id+'.json')).exists())

    def test_cli_missing_pipeline_evidence_cannot_publish_orphan_result(self):
        code, output = self.cli_post()
        self.assertNotEqual(code, 0, output)
        self.assertIn('心跳', output)
        self.assertEqual(self.bus.find_results(self.task.id), [])
        self.assertTrue(self.bus.processing('workbuddy'))
        self.heartbeat()
        code, output = self.cli_post()
        self.assertEqual(code, 0, output)

    def test_mcp_missing_pipeline_evidence_cannot_publish_orphan_result(self):
        app = AgentBusMcp(self.workspace)
        with self.assertRaises((ValueError, BusError)):
            app.send('workbuddy','codex','missing progress',type='result',
                     reply_to=self.task.id,ticket=self.ticket.to_dict())
        self.assertEqual(self.bus.find_results(self.task.id), [])
        self.assertTrue(self.bus.processing('workbuddy'))

    def test_completed_direct_result_does_not_poison_later_answer(self):
        self.heartbeat()
        code, output = self.cli_post()
        self.assertEqual(code,0,output)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(['answer','--workspace',str(self.workspace),'--agent','workbuddy',
                  '--id',self.task.id,'--body','completed actual task',
                  '--claim-epoch',str(self.ticket.claim_epoch),'--claim-id',self.ticket.claim_id])
        self.assertEqual(self.bus.processing('workbuddy'), [])
        self.assertEqual(list(self.bus.dirs('workbuddy')['blocked'].glob('*.json')), [])
        self.assertEqual(len(self.bus.find_results(self.task.id)),1)

    def test_info_transport_does_not_close_an_unrelated_claim(self):
        app=AgentBusMcp(self.workspace)
        app.send('workbuddy','codex','progress only',type='info')
        self.assertTrue(self.bus.processing('workbuddy'))
        self.assertEqual(self.bus.find_results(self.task.id), [])

    def test_pending_audit_is_not_announced_as_a_delivered_result(self):
        cfg = load(self.workspace)
        check = self.workspace / 'test_acceptance.py'
        check.write_text('import unittest\nclass Acceptance(unittest.TestCase):\n'
                         '    def test_success(self):\n        self.assertTrue(True)\n', encoding='utf-8')
        cfg.data['supervision'] = {
            'strict_supervision': True,
            'designated_auditor': 'trae',
            'checks': [{'name': 'acceptance', 'argv': [sys.executable, '-m', 'unittest',
                        'test_acceptance'], 'timeout_seconds': 10}],
        }
        save(cfg)
        (self.workspace / 'output.txt').write_text('actual verified output', encoding='utf-8')
        artifacts = [WorkspaceFiles(self.workspace).describe_artifact('output.txt')]
        (self.workspace / 'manifest.json').write_text(json.dumps(artifacts), encoding='utf-8')
        self.heartbeat()
        code, output = self.cli_post('completed actual task', '--artifacts-file', 'manifest.json')
        self.assertEqual(code, 0, output)
        self.assertIn('待审核', output)
        self.assertNotIn('已投递', output)
        app = AgentBusMcp(self.workspace)
        status = app.send('workbuddy', 'codex', 'completed actual task', type='result',
                          reply_to=self.task.id, ticket=self.ticket.to_dict(), artifacts=artifacts)
        self.assertIn('待独立审核', status)
        self.assertEqual(self.bus.find_results(self.task.id), [])
        self.assertEqual(self.bus.find_message(self.task.id).status, 'awaiting_audit')
        self.assertEqual(len(self.bus.pending('trae')), 1)


if __name__=='__main__':unittest.main()
