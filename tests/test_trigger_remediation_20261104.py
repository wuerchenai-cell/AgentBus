"""Independent audit counterexamples; local fixtures only, no model calls."""
import json
import os
from pathlib import Path
import tempfile
import multiprocessing
import subprocess
import sys
import shutil
import hashlib
import unittest
from unittest.mock import patch, Mock

from agentbus.trigger.contracts import Contact, SubmitResult, TriggerContractError
from agentbus.trigger.contacts import _atomic_write_json, checkin_contact
from agentbus.trigger.service import TriggerDeliveryService
from agentbus.trigger.wake import make_member_wakeup
from agentbus.trigger.journal import DeliveryJournal
from agentbus.trigger.connectors.universal import UniversalWakeupConnector
from agentbus.trigger.connectors.codex import CodexBridgeConnector
from agentbus.trigger.connectors.acp import ACPBridgeConnector
from agentbus.trigger.onboarding import start_flow, advance_flow
from agentbus.trigger.diagnostics import export_diagnostic_bundle, sanitize_text


def _trigger_process(workspace):
    TriggerDeliveryService(workspace).trigger_for_member('codex', 'race-task', count_hop=False)


def _checkin_process(workspace, identity):
    checkin_contact(workspace, 'codex', session_id=identity, notify_coordinator=False)


def _sync_work_process(workspace):
    TriggerDeliveryService(workspace).on_work_state_changed('codex')


class TriggerAuditCounterexamples(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name)
        self.contact = Contact(project_key=str(self.ws), member_id='codex', binding_id='b1',
            status='bound', connector_id='agentbus.universal-wakeup', installation_key='i1',
            native_conversation_id='native-thread-1')
        _atomic_write_json(self.ws/'.agentbus/agents/codex/contact.json', self.contact.to_dict())
        self.service = TriggerDeliveryService(self.ws)
        self.connector = Mock()
        self.connector.describe.return_value.declared_capabilities = {}
        self.connector.submit.return_value = SubmitResult(status='accepted', accepted_by_host=True)
        self.service._get_connector = lambda _: self.connector

    def test_repeated_direct_trigger_does_not_submit_twice(self):
        self.service.trigger_for_member('codex', 't1', count_hop=False)
        self.service.trigger_for_member('codex', 't1', count_hop=False)
        self.assertEqual(self.connector.submit.call_count, 1)

    def test_uncertain_direct_trigger_is_not_resent(self):
        self.connector.submit.return_value = SubmitResult(status='uncertain')
        self.service.trigger_for_member('codex', 't1', count_hop=False)
        self.service.trigger_for_member('codex', 't1', count_hop=False)
        self.assertEqual(self.connector.submit.call_count, 1)

    def test_queued_same_state_does_not_append_history(self):
        req = make_member_wakeup(self.ws, 'codex', 't1', self.contact)
        j = self.service.journal
        j.enqueue(req)
        j.transition(req.request_id, 'queued', {'queued_reason':'target_busy'})
        count = len(j.get_history(req.request_id))
        j.transition(req.request_id, 'queued', {'queued_reason':'target_busy'})
        self.assertEqual(len(j.get_history(req.request_id)), count)

    def test_processing_tasks_are_busy_without_marker(self):
        p = self.ws/'.agentbus/agents/codex/processing/t.json'
        _atomic_write_json(p, {'id':'t'})
        self.assertTrue(self.service.is_member_busy(self.contact))

    def test_stop_file_and_corrupt_policy_fail_closed(self):
        (self.ws/'.agentbus/STOP').write_text('stop')
        self.assertTrue(self.service.is_stopped())
        (self.ws/'.agentbus/STOP').unlink()
        self.service.policy_file.parent.mkdir(parents=True, exist_ok=True)
        self.service.policy_file.write_text('{bad')
        self.assertTrue(self.service.is_stopped())

    def test_local_signal_is_not_host_acceptance_or_completion(self):
        req = make_member_wakeup(self.ws, 'codex', 't1', self.contact)
        with patch.dict(os.environ, {'AGENTBUS_DISABLE_DESKTOP_POPUP':'1'}):
            for cls in (UniversalWakeupConnector, CodexBridgeConnector, ACPBridgeConnector):
                c = cls()
                result = c.submit(self.ws, self.contact, req)
                self.assertFalse(result.accepted_by_host, cls.__name__)
                self.assertEqual(result.status, 'queued')
                self.assertIsNone(result.native_message_id)
                (self.ws/'.agentbus/triggers/wake-signals/codex.json').unlink(missing_ok=True)
                self.assertFalse(any(e.kind == 'completed' for e in c.observe(self.ws, self.contact)))
                self.assertNotEqual(c.reconcile(self.ws, req).status, 'accepted')

    def test_onboarding_cannot_skip_to_complete(self):
        state = start_flow(self.ws, 'newmember')
        with self.assertRaises(TriggerContractError):
            advance_flow(self.ws, state.flow_id, state.revision, 'enable_collaboration', {})

    def test_explicit_file_route_never_starts_saved_acp_command(self):
        _atomic_write_json(self.ws/'.agentbus/config.json', {'workers':{'codex':{
            'communication_mode':'file', 'acp_command':['old-acp-command']}}})
        req = make_member_wakeup(self.ws, 'codex', 'file-mode-task', self.contact)
        with patch('agentbus.acp.ACPAdapter') as adapter:
            outcome = ACPBridgeConnector().submit(self.ws, self.contact, req)
        adapter.assert_not_called()
        self.assertEqual(outcome.status, 'queued')
        self.assertFalse(outcome.accepted_by_host)

    def test_diagnostics_do_not_claim_uninstalled_connectors(self):
        self.assertEqual(export_diagnostic_bundle(self.ws).installed_connectors, [])

    def test_structured_secrets_redacted(self):
        raw = '{"token": "top secret", "Authorization": "Basic YWJj", "password": "with spaces"}'
        clean = sanitize_text(raw)
        self.assertNotIn('top secret', clean)
        self.assertNotIn('YWJj', clean)
        self.assertNotIn('with spaces', clean)

    def test_checkin_needs_real_session_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TriggerContractError):
                checkin_contact(self.ws, 'codex', notify_coordinator=False)

    def test_multiprocess_trigger_has_one_submission_claim(self):
        children = [multiprocessing.get_context('spawn').Process(target=_trigger_process, args=(str(self.ws),)) for _ in range(4)]
        for child in children: child.start()
        for child in children:
            child.join(15)
            self.assertEqual(child.exitcode, 0)
        req = make_member_wakeup(self.ws, 'codex', 'race-task', self.contact)
        history = self.service.journal.get_history(req.request_id)
        self.assertEqual(sum(e.to_status == 'submitting' for e in history), 1)

    def test_multiprocess_checkin_keeps_each_revision(self):
        children = [multiprocessing.get_context('spawn').Process(target=_checkin_process,
            args=(str(self.ws), 'native-thread-' + str(i))) for i in range(2, 6)]
        for child in children: child.start()
        for child in children:
            child.join(15)
            self.assertEqual(child.exitcode, 0)
        from agentbus.trigger.contacts import load_contact
        self.assertEqual(load_contact(self.ws, 'codex').revision, 5)
        self.assertEqual(len(list((self.ws/'.agentbus/agents/codex/contact-history').glob('*.json'))), 4)

    def test_host_dispatch_does_not_hold_member_lock_over_bus_callbacks(self):
        def submit(*args):
            child = multiprocessing.get_context('spawn').Process(target=_sync_work_process, args=(str(self.ws),))
            child.start(); child.join(3)
            if child.is_alive():
                child.terminate(); child.join(3)
            self.assertEqual(child.exitcode, 0, 'Host Bus callback was blocked by dispatcher member lock')
            return SubmitResult(status='accepted', accepted_by_host=True)
        self.connector.submit.side_effect = submit
        outcome = self.service.trigger_for_member('codex', 'callback-task', count_hop=False)
        self.assertEqual(outcome.get('status'), 'ok', outcome)

    def test_bus_claim_to_commit_drains_only_next_member_item(self):
        from agentbus.config import load, save
        from agentbus.bus import Bus
        from agentbus.claim_fencing import captured_ticket
        cfg = load(self.ws, create=True)
        cfg.data['coordinator'] = 'sink'; save(cfg)
        bus = Bus(self.ws/'.agentbus'); bus.init()
        with patch.object(TriggerDeliveryService, '_get_connector', return_value=self.connector):
            first = bus.make('sink', 'codex', 'task', body='first', result_to='sink')
            second = bus.make('sink', 'codex', 'task', body='second', result_to='sink')
            third = bus.make('sink', 'codex', 'task', body='third', result_to='sink')
            for task in (first, second, third): bus.post(task)
            self.service.scan_and_deliver_pending()
            # IDs created in the same millisecond have random suffix order.
            # Complete the task actually notified first, not an assumed order.
            submitted = self.service.journal.list_by_status('submitted')
            self.assertEqual(len(submitted), 1)
            first = bus.find_message(submitted[0].source_message_ids[0])
            claimed = bus.claim('codex', bus.dirs('codex')['ready']/(first.id+'.json'))
            self.assertTrue((self.service.busy_dir/'codex.json').is_file())
            ticket = captured_ticket(claimed)
            bus.heartbeat('codex', claimed, ticket, note='isolated lifecycle verification')
            self.assertTrue(self.service.is_member_busy(self.contact))
            before = self.connector.submit.call_count
            bus.commit_result('codex', claimed, ticket, body='First task executed and checked', exit_code=0)
            self.assertFalse(list(bus.dirs('codex')['processing'].glob('*.json')))
            self.assertEqual(self.connector.submit.call_count, before + 1)
            self.assertEqual(len(self.service.list_member_queued_requests('codex')), 1)

    def test_sdk_uses_real_result_exit_code_and_no_fake_native_ids(self):
        from agentbus.adapters import Result
        _atomic_write_json(self.ws/'.agentbus/config.json', {'workers':{'codex':{'communication_mode':'sdk'}}})
        req = make_member_wakeup(self.ws, 'codex', 'sdk-task', self.contact)
        for code, expected in ((0, True), (2, False)):
            with patch('agentbus.adapters.resolve_codex', return_value=Path(sys.executable)) as resolver, patch('agentbus.codex_sdk.CodexSDKAdapter') as adapter:
                adapter.return_value.run.return_value = Result(code, 'fixture', 0)
                result = CodexBridgeConnector().submit(self.ws, self.contact, req)
                resolver.assert_called_once_with()
                adapter.return_value.run.assert_called_once()
                self.assertEqual(result.accepted_by_host, expected)
                self.assertIsNone(result.native_message_id)
                self.assertFalse((self.ws/'.agentbus/triggers/wake-signals/codex.json').exists())

    def test_codearts_install_preserves_existing_and_requires_asset(self):
        from agentbus.trigger.connectors.codearts import CodeArtsHookConnector
        c = CodeArtsHookConnector()
        target = self.ws/'.codearts/hooks/agentbus.js'
        target.parent.mkdir(parents=True)
        target.write_text('existing user hook')
        with self.assertRaises(TriggerContractError): c.install_hook(self.ws)
        self.assertEqual(target.read_text(), 'existing user hook')
        c._host_asset = self.ws/'missing.js'
        with self.assertRaises(TriggerContractError): c.install_hook(self.ws)

    def test_hook_persistent_claim_stop_and_bad_json_isolation(self):
        node = shutil.which('node')
        if node is None: self.skipTest('Node runtime not present')
        asset = Path(__file__).resolve().parents[1]/'agentbus/host_assets/codearts/agentbus_hook.js'
        script = r'''
            import fs from 'node:fs/promises';
            import path from 'node:path';
            const [root, asset] = process.argv.slice(1);
            const src = await fs.readFile(asset, 'utf8');
            const {createHooks} = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
            const requests = path.join(root, '.agentbus/triggers/bridge/codearts/requests');
            await fs.mkdir(requests,{recursive:true});
            await fs.writeFile(path.join(requests,'req_bad.json'),'{bad');
            await fs.writeFile(path.join(requests,'req_good.json'),JSON.stringify({request_id:'req_good',native_conversation_id:'native-1',prompt_summary:'fixture'}));
            let calls = 0;
            const ctx = {client:{session:{get:async ()=>({data:{id:'native-1'}}),prompt:async ()=>{calls++;return {data:{info:{id:'real-host-message'}}};}}}};
            await createHooks(ctx,{root});
            await new Promise(r=>setTimeout(r,100));
            await createHooks(ctx,{root});
            await new Promise(r=>setTimeout(r,100));
            if(calls!==1) throw new Error('Duplicate host submission: '+calls);
            const bad=JSON.parse(await fs.readFile(path.join(root,'.agentbus/triggers/bridge/codearts/outcomes/req_bad.json'),'utf8'));
            if(bad.status!=='rejected')throw new Error('Bad request blocked valid request');
            await fs.writeFile(path.join(root,'.agentbus/STOP'),'stop');
            await fs.writeFile(path.join(requests,'req_stopped.json'),JSON.stringify({request_id:'req_stopped',native_conversation_id:'native-1',prompt_summary:'fixture'}));
            await new Promise(r=>setTimeout(r,550));
            if(calls!==1)throw new Error('STOP ignored');
            process.stdout.write(JSON.stringify({calls,bad_status:bad.status}));
        '''
        result = subprocess.run([node,'--input-type=module','-e',script,str(self.ws),str(asset)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['calls'], 1)


if __name__ == '__main__':
    unittest.main()
