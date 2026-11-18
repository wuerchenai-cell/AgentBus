"""Source: Codex integrations of actual peer feedback, 2026-10-04."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus, Message
from agentbus.cli import main
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load
from agentbus.mcp_server import AgentBusMcp, TOOLS


class PlanningEntrypointsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.cfg = load(self.ws, create=True)
        self.bus = Bus(self.ws / '.agentbus'); self.bus.init()

    def cli(self, *args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = main([*args, '--workspace', str(self.ws)])
        return code, stream.getvalue()

    def test_optional_plan_fields_round_trip_old_messages_remain_readable(self):
        msg = self.bus.make('codex', 'workbuddy', 'task', body='small', result_to='codex',
                            job_id='j-123', plan_id='plan-123', plan_sha256='a'*64)
        clone = Message.from_dict(msg.to_dict(), strict=True)
        self.assertEqual(clone.plan_sha256, 'a'*64)
        old = Message.from_dict(dict(id='old', ts='now', sender='codex', to='workbuddy', type='task'))
        self.assertIsNone(old.plan_id)

    def test_cli_files_read_reports_original_newlines_and_digest(self):
        (self.ws/'中文.txt').write_bytes('你好\r\n世界'.encode('utf-8'))
        rc, out = self.cli('files', 'read', '--path', '中文.txt')
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)['text'], '你好\r\n世界')
        self.assertEqual(len(json.loads(out)['sha256']), 64)

    def test_cli_files_write_requires_cas_and_keeps_original_on_conflict(self):
        (self.ws/'x.txt').write_text('old', encoding='utf-8')
        rc, out = self.cli('files', 'write', '--path', 'x.txt', '--text', 'new',
                           '--caller', 'codex', '--expected-sha256', 'MISSING')
        self.assertNotEqual(rc, 0, out)
        self.assertEqual((self.ws/'x.txt').read_text(), 'old')

    def test_cli_files_dry_run_does_not_create_target(self):
        rc,out=self.cli('files','write','--path','new.txt','--text','你好',
                        '--caller','codex','--expected-sha256','MISSING','--dry-run')
        self.assertEqual(rc,0,out); self.assertFalse((self.ws/'new.txt').exists())

    def test_cli_plan_status_empty_is_honest(self):
        rc,out=self.cli('plan','status')
        self.assertEqual(rc,0,out)
        self.assertIsNone(json.loads(out)['current'])

    def test_cli_describe_can_publish_strict_manifest_with_explicit_cas(self):
        (self.ws/'artifact.txt').write_bytes('中文\r\n'.encode('utf-8'))
        rc,out=self.cli('files','describe','--path','artifact.txt','--manifest-file','manifest.json',
                        '--caller','codex','--expected-sha256','MISSING')
        self.assertEqual(rc,0,out)
        manifest=json.loads((self.ws/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(set(manifest[0]),{'path','bytes','sha256'})
        rc,out=self.cli('files','verify','--manifest-file','manifest.json')
        self.assertEqual(rc,0,out);self.assertEqual(json.loads(out)['verdict'],'match')

    def test_cli_describe_without_manifest_is_read_only(self):
        (self.ws/'artifact.txt').write_bytes(b'OK')
        rc,out=self.cli('files','describe','--path','artifact.txt')
        self.assertEqual(rc,0,out);self.assertEqual(json.loads(out)['bytes'],2)
        self.assertFalse((self.ws/'manifest.json').exists())

    def test_cli_describe_manifest_requires_owner_and_preserves_existing_file(self):
        (self.ws/'artifact.txt').write_bytes(b'OK')
        (self.ws/'manifest.json').write_bytes(b'original')
        rc,out=self.cli('files','describe','--path','artifact.txt','--manifest-file','manifest.json',
                        '--expected-sha256','MISSING')
        self.assertNotEqual(rc,0,out)
        rc,out=self.cli('files','describe','--path','artifact.txt','--manifest-file','manifest.json',
                        '--caller','codex','--expected-sha256','MISSING')
        self.assertNotEqual(rc,0,out);self.assertEqual((self.ws/'manifest.json').read_bytes(),b'original')

    def test_cli_describe_manifest_dry_run_keeps_workspace_unchanged(self):
        (self.ws/'artifact.txt').write_bytes(b'OK')
        rc,out=self.cli('files','describe','--path','artifact.txt','--manifest-file','manifest.json',
                        '--caller','codex','--expected-sha256','MISSING','--dry-run')
        self.assertEqual(rc,0,out);self.assertFalse((self.ws/'manifest.json').exists())

    def test_cli_plan_propose_cannot_adopt_before_peer_receipt(self):
        (self.ws/'draft.md').write_text('# Overall\nGoal: deliver',encoding='utf-8')
        rc,out=self.cli('plan','propose','--caller','codex','--file','draft.md',
                        '--reason','user request','--reviewers','workbuddy','--request-id','p1')
        self.assertEqual(rc,0,out)
        proposal=json.loads(out)
        rc,out=self.cli('plan','adopt','--caller','codex','--proposal-id',proposal['id'],
                        '--expected-current-sha256','MISSING','--dispositions-json','{"workbuddy":"adopt"}')
        self.assertNotEqual(rc,0,out)

    def test_mcp_file_and_plan_tools_have_annotations(self):
        names={x['name']:x for x in TOOLS}
        for name in ('agentbus_file_read','agentbus_file_write','agentbus_artifacts_verify','agentbus_plan_status'):
            self.assertIn(name,names)
            self.assertIn('annotations',names[name])

    def test_cli_direct_result_cannot_bypass_required_artifact_manifest(self):
        msg=self.bus.make('codex','workbuddy','task',body='produce',result_to='codex',
                          expected_artifacts=['deliver.txt'],job_id='j-123',plan_id='p-123',plan_sha256='a'*64)
        self.bus.post(msg)
        rc,out=self.cli('post','--from','workbuddy','--to','codex','--type','result',
                        '--intent','done','--reply-to',msg.id)
        self.assertNotEqual(rc,0,out);self.assertEqual(self.bus.find_results(msg.id),[])

    def test_mcp_direct_result_verifies_and_preserves_binding(self):
        from agentbus.workspace_files import WorkspaceFiles
        (self.ws/'deliver.txt').write_bytes(b'OK')
        manifest=[WorkspaceFiles(self.ws).describe_artifact('deliver.txt')]
        msg=self.bus.make('codex','workbuddy','task',body='produce',result_to='codex',
                          expected_artifacts=['deliver.txt'],job_id='j-123',plan_id='p-123',plan_sha256='a'*64)
        self.bus.post(msg);server=AgentBusMcp(self.ws)
        claimed = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        ticket = captured_ticket(claimed)
        self.bus.heartbeat('workbuddy', claimed, ticket, note='manifest test progress')
        with self.assertRaises(ValueError):
            server.send('workbuddy','codex','done',type='result',reply_to=msg.id,ticket=ticket)
        server.send('workbuddy','codex','done',type='result',reply_to=msg.id,artifacts=manifest,ticket=ticket)
        receipt=self.bus.find_results(msg.id)[0]
        self.assertEqual(receipt.artifacts,manifest);self.assertEqual(receipt.job_id,'j-123')
        self.assertEqual(receipt.plan_sha256,'a'*64)

    def test_mcp_report_rejects_invalid_manifest_type(self):
        msg=self.bus.make('codex','workbuddy','task',body='read only',result_to='codex')
        self.bus.post(msg);server=AgentBusMcp(self.ws)
        with self.assertRaises(ValueError):
            server.report('workbuddy',msg.id,'done',artifacts={})
        self.assertEqual(self.bus.find_results(msg.id),[])

    def test_repeated_report_cannot_silently_change_artifact_list(self):
        msg=self.bus.make('codex','workbuddy','task',body='read only',result_to='codex')
        self.bus.post(msg)
        receipt=self.bus.make('workbuddy','codex','result',body='done',reply_to=msg.id,exit_code=0)
        self.bus.post(receipt)
        (self.ws/'unexpected.txt').write_bytes(b'new')
        from agentbus.workspace_files import WorkspaceFiles
        server=AgentBusMcp(self.ws)
        with self.assertRaises(ValueError):
            server.report('workbuddy',msg.id,'done',artifacts=[WorkspaceFiles(self.ws).describe_artifact('unexpected.txt')])
        self.assertEqual(len(self.bus.find_results(msg.id)),1)

    def test_mcp_file_write_identity_is_fixed_and_unknown_owner_rejected(self):
        app=AgentBusMcp(self.ws,identity='workbuddy')
        with self.assertRaises(ValueError):
            app.file_write('output.txt','x','codex','MISSING')

    def test_artifact_answer_rejects_missing_required_file_before_receipt(self):
        task=self.bus.make('codex','workbuddy','task',body='create file',result_to='codex',
                           expected_artifacts=['output.txt'])
        self.bus.post(task)
        rc,out=self.cli('answer','--agent','workbuddy','--id',task.id,'--body','done')
        self.assertNotEqual(rc,0,out)
        self.assertEqual(self.bus.find_results(task.id),[])

    def test_artifact_answer_revalidates_bytes_and_propagates_plan(self):
        import hashlib
        content='真实产物'.encode('utf-8');(self.ws/'output.txt').write_bytes(content)
        items=[{'path':'output.txt','bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}]
        (self.ws/'manifest.json').write_text(json.dumps(items),encoding='utf-8')
        task=self.bus.make('codex','workbuddy','task',body='create file',result_to='codex',
                           job_id='j-1',plan_id='plan-1',plan_sha256='b'*64,expected_artifacts=['output.txt'])
        self.bus.post(task)
        claim_rc,claim_out=self.cli('claim','--agent','workbuddy','--id',task.id)
        self.assertEqual(claim_rc,0,claim_out)
        ticket=json.loads(claim_out.splitlines()[-1])
        heartbeat_rc,heartbeat_out=self.cli(
            'heartbeat','--agent','workbuddy','--id',task.id,
            '--claim-epoch',str(ticket['claim_epoch']),'--claim-id',ticket['claim_id'])
        self.assertEqual(heartbeat_rc,0,heartbeat_out)
        rc,out=self.cli('answer','--agent','workbuddy','--id',task.id,'--body','done',
                        '--artifacts-file','manifest.json',
                        '--claim-epoch',str(ticket['claim_epoch']),'--claim-id',ticket['claim_id'])
        self.assertEqual(rc,0,out)
        result=self.bus.find_results(task.id)[0]
        self.assertEqual(result.artifacts,items);self.assertEqual(result.plan_id,'plan-1')


if __name__ == '__main__':
    unittest.main()
