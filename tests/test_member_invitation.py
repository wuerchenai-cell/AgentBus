"""Codex, 2026-10-05: actual-file invitation recovery and evidence boundaries."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from receipt_helpers import receipt_ticket
import json
import hashlib
import tempfile
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.project_workflow import prepare_project_workflow
from agentbus.member_invitation import prepare_invitation, invitation_status, record_invitation_event


class MemberInvitationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.cfg=load(self.root,create=True)
        self.cfg.data['workers']={'codex':{'enabled':True,'executor_enabled':True,'kind':'gui','can_spawn':False},
            'new-agent':{'display_name':'New Agent','enabled':True,'executor_enabled':True,'kind':'gui','can_spawn':False}}
        save(self.cfg)
        prepare_project_workflow(self.cfg)
        # The temporary fixture uses a real, existing source launcher, never a model.
        manifest=self.root/'.agentbus/workflow.json'
        data=json.loads(manifest.read_text(encoding='utf-8'))
        import sys
        data['launcher']={'mode':'source','python':sys.executable,'entrypoint':str(Path(__file__).resolve())}
        manifest.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')

    def invite(self,**kw):
        return prepare_invitation(self.cfg,'codex','new-agent','request-1',**kw)

    def test_real_task_inbox_input_paths_and_two_instructions(self):
        item=self.invite()
        self.assertEqual(item['state'],'committed')
        bus=Bus(self.root/'.agentbus'); task=bus.find_message(item['task_id'])
        self.assertEqual(task.result_to,'codex'); self.assertEqual(task.to,'new-agent')
        self.assertTrue((self.root/item['input_path']).is_file())
        for name in ('leader','newcomer'):
            self.assertIn(item['task_id'],item['instructions'][name])
            self.assertIn(str(self.root),item['instructions'][name])

    def test_same_request_id_only_posts_once(self):
        first=self.invite(); second=self.invite()
        self.assertEqual(first['task_id'],second['task_id'])
        ready=list((self.root/'.agentbus/agents/new-agent/ready').glob('*.json'))
        self.assertEqual(len(ready),1)

    def test_same_request_with_changed_intent_rejected(self):
        self.invite()
        with self.assertRaisesRegex(ValueError,'INVITE_CONFLICT'):
            self.invite(writable=False)

    def test_workflow_conflict_has_no_effective_task(self):
        (self.root/'AGENTBUS.md').write_text('local user edit',encoding='utf-8')
        item=self.invite()
        self.assertEqual(item['state'],'conflict'); self.assertFalse(item.get('task_id'))
        self.assertIn('这不是可执行邀请',item['reason'])
        self.assertIsNone(Bus(self.root/'.agentbus').find_message(item.get('task_id') or 'none'))

    def test_copy_is_never_delivery_or_capability(self):
        item=self.invite()
        record_invitation_event(self.cfg,'codex',item['invite_id'],'copied','user-interface')
        state=invitation_status(self.cfg,item['invite_id'])
        self.assertTrue(state['copied']);self.assertFalse(state['sent'])
        self.assertFalse(state['receipt_channel_verified']);self.assertFalse(state['capability_passed'])

    def test_stop_prevents_sample_dispatch(self):
        (self.root/'.agentbus/STOP').write_text('stop',encoding='utf-8')
        item=self.invite()
        self.assertEqual(item['state'],'conflict');self.assertFalse(item.get('task_id'))

    def test_readonly_invitation_requires_no_output(self):
        item=self.invite(writable=False); task=Bus(self.root/'.agentbus').find_message(item['task_id'])
        self.assertEqual(task.expected_artifacts,[])
        self.assertIn('写能力未验证',item['instructions']['newcomer'])

    def test_interruption_after_post_recovers_same_task(self):
        import agentbus.member_invitation as module
        original=module._save_record
        def interrupt(path,data):
            if data.get('state')=='committed': raise OSError('simulated crash after publication')
            return original(path,data)
        with patch.object(module,'_save_record',side_effect=interrupt):
            with self.assertRaises(OSError): self.invite()
        item=self.invite()
        self.assertEqual(item['state'],'committed')
        self.assertEqual(len(list((self.root/'.agentbus/agents/new-agent/ready').glob('*.json'))),1)

    def test_failed_real_receipt_only_verifies_channel(self):
        item=self.invite();bus=Bus(self.root/'.agentbus')
        task=bus.find_message(item['task_id']);claimed=bus.claim('new-agent',bus.dirs('new-agent')['ready']/(task.id+'.json'))
        ticket=receipt_ticket(claimed);bus.heartbeat('new-agent',claimed,ticket,note='test member started')
        bus.commit_result('new-agent',claimed,ticket,body='failed',exit_code=1)
        state=invitation_status(self.cfg,item['invite_id'])
        self.assertTrue(state['receipt_channel_verified']);self.assertFalse(state['capability_passed'])

    def record_path(self,item):
        return self.root/'.agentbus/invites'/(item['invite_id']+'.json')

    def assert_non_executable(self,item):
        self.assertEqual(item['state'],'conflict')
        self.assertFalse(item.get('task_id'))
        self.assertIn('这不是可执行邀请',item['reason'])

    def test_committed_cache_respects_stop_without_reposting(self):
        original=self.invite()
        (self.root/'.agentbus/STOP').write_text('stop',encoding='utf-8')
        self.assert_non_executable(self.invite())
        self.assertIsNotNone(Bus(self.root/'.agentbus').find_message(original['task_id']))
        self.assertEqual(len(list((self.root/'.agentbus/agents/new-agent/ready').glob('*.json'))),1)

    def test_committed_cache_respects_unsaved_team(self):
        self.invite();self.cfg.data['workers']['new-agent']['display_name']='unsaved draft'
        self.assert_non_executable(self.invite())

    def test_committed_cache_respects_changed_saved_team(self):
        self.invite();self.cfg.data['workers']['new-agent']['display_name']='saved change'
        save(self.cfg)
        self.assert_non_executable(self.invite())

    def test_committed_cache_respects_changed_workflow(self):
        self.invite()
        (self.root/'AGENTBUS.md').write_text('local modification',encoding='utf-8')
        self.assert_non_executable(self.invite())

    def test_committed_cache_retains_original_deadline_on_expiry(self):
        original=self.invite()
        with patch('agentbus.member_invitation.time.time',return_value=original['deadline_ts']+1):
            self.assert_non_executable(self.invite())
        record=json.loads(self.record_path(original).read_text(encoding='utf-8'))
        self.assertEqual(record['deadline_ts'],original['deadline_ts'])
        self.assertEqual(record['message']['id'],original['task_id'])

    def test_committed_cache_does_not_recreate_missing_original_task(self):
        original=self.invite()
        (self.root/'.agentbus/agents/new-agent/ready'/(original['task_id']+'.json')).unlink()
        self.assert_non_executable(self.invite())
        self.assertEqual(list((self.root/'.agentbus/agents/new-agent/ready').glob('*.json')),[])

    def test_committed_cache_rejects_published_intent_mismatch(self):
        original=self.invite();path=self.root/'.agentbus/agents/new-agent/ready'/(original['task_id']+'.json')
        message=json.loads(path.read_text(encoding='utf-8'));message['body']='different task'
        path.write_text(json.dumps(message),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'INVITE_CONFLICT'):
            self.invite()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['body'],'different task')

    def test_tampered_record_paths_never_read_or_write_outside_sample(self):
        original=self.invite();path=self.record_path(original)
        raw=path.read_bytes();outside=self.root.parent/(self.root.name+'-sentinel.txt')
        outside.write_bytes(b'preserve');self.addCleanup(outside.unlink)
        for field,value in [('input_path',str(outside)),('output_path','../'+outside.name)]:
            with self.subTest(field=field):
                record=json.loads(raw);record[field]=value
                path.write_text(json.dumps(record),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'INVITE_CONFLICT'):
                    invitation_status(self.cfg,original['invite_id'])
                with self.assertRaisesRegex(ValueError,'INVITE_CONFLICT'):
                    self.invite()
                self.assertEqual(outside.read_bytes(),b'preserve')

    def test_record_corruption_types_and_duplicate_keys_rejected_preserved(self):
        original=self.invite();path=self.record_path(original);raw=path.read_bytes()
        corrupt=[b'not json',b'\xff',b'x'*65537]
        for key,value in [('schema_version',True),('schema_version',1.0),('writable','false'),
                          ('events',{}),('deadline_ts',True),('deadline_ts','now'),
                          ('message',[]),('member',42),('state','invented')]:
            record=json.loads(raw);record[key]=value;corrupt.append(json.dumps(record).encode('utf-8'))
        corrupt.append(raw[:-1]+b',"schema_version":1}')
        for value in corrupt:
            with self.subTest(raw_size=len(value)):
                path.write_bytes(value)
                with self.assertRaisesRegex(ValueError,'INVITE_CONFLICT'):
                    invitation_status(self.cfg,original['invite_id'])
                self.assertEqual(path.read_bytes(),value)

    def test_record_identity_must_match_project_and_invitation_id(self):
        original=self.invite();path=self.record_path(original);raw=path.read_bytes()
        for key,value in [('id','i-'+'f'*32),('workspace',str(self.root.parent)),('request_id','another')]:
            with self.subTest(field=key):
                record=json.loads(raw);record[key]=value
                path.write_text(json.dumps(record),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'INVITE_CONFLICT'):
                    invitation_status(self.cfg,original['invite_id'])

    def test_linked_control_lock_is_rejected_before_external_write(self):
        leases=self.root/'.agentbus/leases';leases.mkdir(parents=True,exist_ok=True)
        outside=self.root/'external-lock';outside.write_bytes(b'untouched')
        alias=leases/'.mutation.lock'
        alias.unlink(missing_ok=True)
        if os.name=='nt':
            made=subprocess.run(['cmd','/c','mklink','/H',str(alias),str(outside)],capture_output=True)
            if made.returncode:self.skipTest('hardlink creation unavailable')
        else:alias.symlink_to(outside)
        self.addCleanup(alias.unlink)
        with self.assertRaisesRegex(ValueError,'UNSAFE_PATH'):
            self.invite()
        self.assertEqual(outside.read_bytes(),b'untouched')

    def test_linked_lease_directory_is_rejected_without_external_write(self):
        leases=self.root/'.agentbus/leases'
        for child in leases.iterdir():child.unlink()
        leases.rmdir()
        outside=self.root/'outside';outside.mkdir()
        if os.name=='nt':
            made=subprocess.run(['cmd','/c','mklink','/J',str(leases),str(outside)],capture_output=True)
            if made.returncode:self.skipTest('junction creation unavailable')
            self.addCleanup(leases.rmdir)
        else:
            leases.symlink_to(outside,target_is_directory=True);self.addCleanup(leases.unlink)
        with self.assertRaisesRegex(ValueError,'UNSAFE_PATH'):
            self.invite()
        self.assertEqual(list(outside.iterdir()),[])

    def complete_sample(self,item,*,body=None):
        bus=Bus(self.root/'.agentbus');task=bus.find_message(item['task_id'])
        claimed=bus.claim('new-agent',bus.dirs('new-agent')['ready']/(task.id+'.json'))
        artifacts=[]
        if task.expected_artifacts:
            source=(self.root/item['input_path']).read_bytes().rstrip(b'\n')
            (self.root/item['output_path']).write_bytes(source)
            artifacts=[{'path':item['output_path'],'bytes':len(source),'sha256':hashlib.sha256(source).hexdigest()}]
        ticket=receipt_ticket(claimed);bus.heartbeat('new-agent',claimed,ticket,note='test member started')
        receipt=bus.commit_result('new-agent',claimed,ticket,
                         body=body or 'AgentBus sample '+item['invite_id'],artifacts=artifacts,exit_code=0)
        receipt=bus.claim('codex',bus.dirs('codex')['ready']/(receipt.id+'.json'))
        receipt_ticket_value=receipt_ticket(receipt)
        bus.heartbeat('codex',receipt,receipt_ticket_value,note='test coordinator received result')
        bus.finish('codex',receipt,ticket=receipt_ticket_value)
        return receipt

    def test_direct_real_completion_needs_no_copy_event(self):
        original=self.invite();self.complete_sample(original)
        state=invitation_status(self.cfg,original['invite_id'])
        self.assertFalse(state['copied']);self.assertFalse(state['sent'])
        self.assertTrue(state['receipt_channel_verified']);self.assertTrue(state['capability_passed'])

    def test_readonly_completion_requires_unchanged_input(self):
        original=self.invite(writable=False);self.complete_sample(original)
        self.assertTrue(invitation_status(self.cfg,original['invite_id'])['capability_passed'])
        (self.root/original['input_path']).write_bytes(b'changed')
        state=invitation_status(self.cfg,original['invite_id'])
        self.assertTrue(state['receipt_channel_verified']);self.assertFalse(state['capability_passed'])

    def test_failed_receipt_never_verifies_completed_task_capability(self):
        original=self.invite();receipt=self.complete_sample(original)
        path=self.root/'.agentbus/agents/codex/done'/(receipt.id+'.json')
        result=json.loads(path.read_text(encoding='utf-8'));result['status']='failed'
        path.write_text(json.dumps(result),encoding='utf-8')
        state=invitation_status(self.cfg,original['invite_id'])
        self.assertTrue(state['receipt_channel_verified']);self.assertFalse(state['capability_passed'])


if __name__ == '__main__':
    unittest.main()
