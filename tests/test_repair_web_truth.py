"""2026-11-04 audit repair: observable verification and bounded public reads."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.public_files import PublicFiles, PublicFileError
from agentbus.trigger.contracts import Contact
from agentbus.trigger.verification import plan_verification, start_verification, get_verification
from agentbus.web_bridge import WebBridge
from agentbus.workspace_files import WorkspaceFiles


class WebTruthRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / 'project'
        self.ws.mkdir()
        self.cfg = load(self.ws, create=True)
        self.cfg.data['workers']['alpha'] = {'enabled': True, 'executor_enabled': True,
            'communication_mode': 'file', 'transport': 'file', 'can_spawn': False}
        self.cfg = save(self.cfg)
        self.bus = Bus(self.ws / '.agentbus')
        self.bus.init()
        self.contact = Contact(schema_version=1, project_key=str(self.ws), member_id='alpha',
            binding_id='bnd-alpha', revision=1, status='bound',
            connector_id='agentbus.universal-wakeup', installation_key='inst-alpha')
        contact_file = self.bus.register('alpha') / 'contact.json'
        contact_file.write_text(json.dumps(self.contact.to_dict()), encoding='utf-8')
        # Isolated file-flow tests intentionally do not contact an external host.
        from agentbus.trigger.service import TriggerDeliveryService
        TriggerDeliveryService(self.ws).set_stopped(True)

    def plan(self):
        return plan_verification(self.ws, self.contact)

    def write_answer(self, plan):
        output = self.cfg.output_root / plan.expected_output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({'nonce': plan.expected_payload['nonce'],
            'result': plan.expected_payload['expected_sum']}), encoding='utf-8')
        return output

    def test_web_run_never_writes_answer_or_claims_an_unexecuted_pass(self):
        plan = self.plan()
        response = WebBridge(self.ws).call('trigger_verification_run', {'plan': plan.to_dict()})
        self.assertTrue(response['ok'], response)
        self.assertFalse(response['data']['passed'])
        self.assertEqual(response['data']['status'], 'pending')
        self.assertEqual(response['data']['actual_submits'], 0)
        self.assertFalse((self.cfg.output_root / plan.expected_output_path).exists())

    def test_start_posts_one_real_task_and_is_idempotent(self):
        plan = self.plan()
        first = start_verification(self.ws, plan)
        again = start_verification(self.ws, plan)
        self.assertEqual(first.run_id, again.run_id)
        task = self.bus.find_message(first.task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.to, 'alpha')
        self.assertEqual(task.expected_artifacts, [plan.expected_output_path])
        self.assertIn(str(self.ws), task.body)
        self.assertEqual(len(self.bus.pending('alpha')), 1)

    def test_correct_file_without_formal_delivery_stays_pending(self):
        plan = self.plan()
        run = start_verification(self.ws, plan)
        self.write_answer(plan)
        report = get_verification(self.ws, run.run_id)
        self.assertEqual(report.status, 'pending')
        self.assertFalse(report.member_delivered)
        self.assertFalse(report.artifact_verified)
        self.assertEqual(report.actual_submits, 0)

    def test_real_claim_heartbeat_receipt_and_answer_pass(self):
        plan = self.plan()
        run = start_verification(self.ws, plan)
        task_path = self.bus.dirs('alpha')['ready'] / (run.task_id + '.json')
        self.assertTrue(task_path.is_file())
        claimed = self.bus.claim('alpha', task_path)
        ticket = captured_ticket(claimed)
        self.bus.heartbeat('alpha', claimed, ticket, note='real isolated verification')
        self.write_answer(plan)
        manifest = [WorkspaceFiles(self.cfg.output_root).describe_artifact(plan.expected_output_path)]
        self.bus.commit_result('alpha', claimed, ticket, body='Computed challenge and saved JSON',
            artifacts=manifest, exit_code=0, artifact_root=self.cfg.output_root)
        report = get_verification(self.ws, run.run_id)
        self.assertEqual(report.status, 'passed')
        self.assertTrue(report.member_delivered)
        self.assertTrue(report.artifact_verified)
        # Formal file delivery is not a model submit.
        self.assertEqual(report.actual_submits, 0)
        self.write_answer(plan).write_text('{}', encoding='utf-8')
        self.assertNotEqual(get_verification(self.ws, run.run_id).status, 'passed')

    def test_client_plan_path_injection_cannot_write_outside_workspace(self):
        plan = self.plan()
        outside = self.ws.parent / 'outside.json'
        plan.sample_input_path = str(outside)
        response = WebBridge(self.ws).call('trigger_verification_run', {'plan': plan.to_dict()})
        self.assertFalse(response['ok'])
        self.assertFalse(outside.exists())

    def test_contact_rebinding_invalidates_an_old_passed_run(self):
        plan = self.plan()
        run = start_verification(self.ws, plan)
        claimed = self.bus.claim('alpha', self.bus.dirs('alpha')['ready'] / (run.task_id + '.json'))
        ticket = captured_ticket(claimed)
        self.bus.heartbeat('alpha', claimed, ticket, note='bounded verification')
        self.write_answer(plan)
        manifest = [WorkspaceFiles(self.cfg.output_root).describe_artifact(plan.expected_output_path)]
        self.bus.commit_result('alpha', claimed, ticket, body='challenge delivered',
            artifacts=manifest, exit_code=0, artifact_root=self.cfg.output_root)
        self.assertEqual(get_verification(self.ws, run.run_id).status, 'passed')
        self.contact.binding_id = 'bnd-another-window'
        self.contact.revision = 2
        (self.bus.register('alpha') / 'contact.json').write_text(
            json.dumps(self.contact.to_dict()), encoding='utf-8')
        report = get_verification(self.ws, run.run_id)
        self.assertNotEqual(report.status, 'passed')
        self.assertFalse(report.member_delivered)

    def test_run_id_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            get_verification(self.ws, '../../outside')

    def test_snapshot_survives_600_valid_history_records(self):
        folder = self.bus.dirs('alpha')['done']
        for index in range(600):
            message = self.bus.make('codex', 'alpha', 'task', title='History', body='History')
            message.status = 'done'
            (folder / (message.id + '.json')).write_text(json.dumps(message.to_dict()), encoding='utf-8')
        response = WebBridge(self.ws).call('snapshot', {})
        self.assertTrue(response['ok'], response)
        self.assertEqual(len(WebBridge._mailboxes(self.cfg)['alpha']['done']), 600)

    def test_public_mailbox_pages_are_bounded_without_dropping_history(self):
        folder = self.bus.dirs('alpha')['done']
        for index in range(600):
            (folder/f'm-{index:04}.json').write_text('{}', encoding='utf-8')
        public = PublicFiles(self.ws)
        pages = list(public.iter_file_pages('.agentbus/agents/alpha/done', page_size=17))
        names = [p.name for page in pages for p in page]
        self.assertTrue(all(0 < len(page) <= 17 for page in pages))
        self.assertEqual(len(names), 600)
        self.assertEqual(len(set(names)), 600)

    def test_public_reads_keep_private_and_reparse_boundary(self):
        public = PublicFiles(self.ws)
        with self.assertRaises(PublicFileError):
            public.checked('.workbuddy/never-read')
        with self.assertRaises(PublicFileError):
            public.checked('不要访问/never-read')
        target = self.ws / 'linked'
        if os.name == 'nt':
            process = subprocess.run(['cmd', '/c', 'mklink', '/J', str(target), str(self.ws.parent)],
                capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
        else:
            target.symlink_to(self.ws.parent, target_is_directory=True)
        with self.assertRaises(PublicFileError):
            public.checked('linked/outside.json')

    def test_actual_submit_count_comes_from_each_associated_attempt(self):
        plan = self.plan()
        run = start_verification(self.ws, plan)
        request_dir = self.ws / '.agentbus/triggers/requests'
        request_dir.mkdir(parents=True, exist_ok=True)
        (request_dir / 'req_test.json').write_text(json.dumps({'request_id': 'req_test',
            'source_message_ids': [run.task_id]}), encoding='utf-8')
        journal = self.ws / '.agentbus/triggers/journal/req_test/journal.jsonl'
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text('\n'.join(json.dumps({'request_id': 'req_test',
            'to_status': status}) for status in ('queued', 'submitting', 'submitted',
                'submitting', 'submitted')), encoding='utf-8')
        report = get_verification(self.ws, run.run_id)
        self.assertEqual(report.actual_submits, 2)
        self.assertEqual(report.status, 'failed')

    def test_verification_uses_configured_separate_output_root(self):
        output = self.ws.parent / 'deliverables'
        output.mkdir()
        self.cfg.data['output_root'] = str(output)
        self.cfg = save(self.cfg)
        plan = self.plan()
        run = start_verification(self.ws, plan)
        task = self.bus.find_message(run.task_id)
        self.assertIn(str(output), task.body)
        self.assertFalse((output / plan.expected_output_path).exists())
        # An unassociated file in either root still cannot prove execution.
        self.write_answer(plan)
        self.assertEqual(get_verification(self.ws, run.run_id).status, 'pending')

    def test_onboarding_command_carries_registered_absolute_context(self):
        from agentbus.trigger.onboarding import FlowState
        exe = self.ws / 'published' / 'AgentBus.exe'
        exe.parent.mkdir()
        exe.touch()
        workflow = self.ws / '.agentbus/workflow.json'
        workflow.write_text(json.dumps({'launcher': {'executable': str(exe)}}), encoding='utf-8')
        flow = FlowState(flow_id='flow-test', project_key=str(self.ws), member_id='alpha',
            step='S4', connector_id='agentbus.universal-wakeup', selected_installation='inst-alpha')
        data = WebBridge._trigger_flow_data(self.cfg, flow)
        self.assertEqual(data['executable'], str(exe))
        for value in (str(exe), str(self.ws), '--workspace', '--agent', 'alpha',
                '--connector', 'agentbus.universal-wakeup', '--installation', 'inst-alpha', '--session'):
            self.assertIn(value, data['checkin_command'])

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime unavailable')
    def test_frontend_pending_checks_existing_run_without_republishing(self):
        asset = Path(__file__).resolve().parents[1] / 'agentbus/web_assets/trigger_onboarding.js'
        program = r'''
const fs=require('fs'), vm=require('vm');
const calls=[], box={innerHTML:''}, btn={disabled:false,textContent:''};
global.window={agentbusCall:async(action,payload)=>{calls.push({action,payload});
  if(action==='trigger_verification_plan')return {plan_id:'plan'};
  return {run_id:'vrun_test',status:'pending',passed:false,actual_submits:0};}};
global.document={getElementById:id=>id==='verificationBox'?box:btn};
global.alert=()=>{};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
(async()=>{const wizard=new window.TriggerOnboardingWizard(null);
wizard.container={querySelector:selector=>selector==='#verificationBox'?box:btn};
wizard.memberId='alpha';wizard.flowState={member_id:'alpha'};wizard.currentStep='S5';
const rendered=wizard.renderStepContent();
await wizard.handleRunVerification();await wizard.handleRunVerification();
console.log(JSON.stringify({calls,message:box.innerHTML,rendered,step:wizard.currentStep}));})();
'''
        process = subprocess.run([shutil.which('node'), '-e', program, str(asset)],
            capture_output=True, timeout=10, text=True, encoding='utf-8')
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual([call['action'] for call in result['calls']],
            ['trigger_verification_plan', 'trigger_verification_run', 'trigger_verification_check'])
        self.assertIn('等待成员实际执行', result['message'])
        self.assertNotIn('直接确认通过', result['rendered'])
        self.assertEqual(result['step'], 'S5')


if __name__ == '__main__':
    unittest.main()
