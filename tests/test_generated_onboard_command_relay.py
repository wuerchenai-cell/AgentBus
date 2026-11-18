"""Generated recipes use registered launcher and complete fenced file relay."""
import contextlib
import hashlib
import io
import json
import re
import shlex
from agentbus import cli
from agentbus.bus import Bus
from agentbus.onboarding_pack import auto_onboard_agent, build_one_click_join_prompt, deploy_onboarding_pack
from tests.onboard_test_utils import OnboardFixture

class TestGeneratedOnboardCommandRelay(OnboardFixture):
    def test_command_uses_formal_launcher(self):
        result = auto_onboard_agent(self.root, 'new-peer')
        prompt = result['prompt']
        launcher = json.loads((self.root / '.agentbus/workflow.json').read_text(encoding='utf-8'))['launcher']
        self.assertIn(launcher['python'], prompt)
        self.assertIn(launcher['entrypoint'], prompt)
        self.assertIn('--claim-epoch', prompt)
        self.assertIn('--claim-id', prompt)
        self.assertIn(' heartbeat ', prompt)
        self.assertNotIn('`agentbus claim', prompt)
        self.assertNotIn('已成功接入', prompt)

    def test_recorded_frozen_launcher_is_used_and_digest_mismatch_refused(self):
        executable = self.root / '成果/AgentBus.exe'
        executable.parent.mkdir()
        executable.write_bytes(b'synthetic inert binary; never executed')
        path = self.root / '.agentbus/workflow.json'
        workflow = json.loads(path.read_text(encoding='utf-8'))
        workflow['launcher'] = {'mode': 'frozen', 'executable': str(executable),
            'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(), 'bytes': executable.stat().st_size}
        path.write_text(json.dumps(workflow), encoding='utf-8')
        result = auto_onboard_agent(self.root, 'new-peer')
        self.assertIn("& '" + str(executable) + "' claim", result['prompt'])
        executable.write_bytes(b'changed synthetic inert binary')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            auto_onboard_agent(self.root, 'other-peer')
        self.assertEqual(before, self.snapshot())

    def test_legacy_frozen_launcher_never_claims_source_integrity(self):
        executable = self.root / 'legacy-inert.exe'
        executable.write_bytes(b'inert synthetic launcher; never executed')
        path = self.root / '.agentbus/workflow.json'
        workflow = json.loads(path.read_text(encoding='utf-8'))
        workflow['launcher'] = {'mode': 'frozen', 'executable': str(executable)}
        path.write_text(json.dumps(workflow), encoding='utf-8')
        prompt = build_one_click_join_prompt(self.root, 'new-peer')
        self.assertNotIn('EXE摘要不适用', prompt)
        self.assertIn('未登记摘要', prompt)

    def test_generated_claim_heartbeat_answer_relay(self):
        auto_onboard_agent(self.root, 'new-peer')
        pack = deploy_onboarding_pack(self.root, 'new-peer', task_id='m-onboard-relay-1')
        prompt = pack['newcomer_instructions']
        recipes = [line.strip('` ') for line in prompt.splitlines() if line.startswith('`& ')]
        self.assertGreaterEqual(len(recipes), 3)
        parsed = {}
        for recipe in recipes:
            args = shlex.split(recipe)[1:]
            operation = next(name for name in ('claim', 'heartbeat', 'answer') if name in args)
            parsed[operation] = args[args.index(operation):]
        claim = [value.replace('<任务ID>', pack['task_id']) for value in parsed['claim']]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, cli.main(claim))
        ticket = json.loads(output.getvalue().splitlines()[-1])
        replacements = {'<任务ID>': pack['task_id'], '<claim_epoch>': str(ticket['claim_epoch']), '<claim_id>': ticket['claim_id']}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main([replacements.get(value, value) for value in parsed['heartbeat']]))
        output_file = self.root / pack['output_path']
        output_file.write_bytes((self.root / pack['input_path']).read_bytes().splitlines()[0])
        manifest = self.root / pack['manifest_path']
        manifest.write_text(json.dumps([{'path': pack['output_path'], 'bytes': output_file.stat().st_size,
            'sha256': hashlib.sha256(output_file.read_bytes()).hexdigest()}]), encoding='utf-8')
        verification = re.search(r'`(& [^`]+)`', pack['leader_instructions']).group(1)
        verify_args = shlex.split(verification)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cli.main(verify_args[verify_args.index('files'):]))
        body = self.root / 'receipt.md'; body.write_text('完成合成样例', encoding='utf-8')
        replacements.update({'<回执路径>': 'receipt.md', '<正文SHA256>': hashlib.sha256(body.read_bytes()).hexdigest(), '<产物清单路径>': pack['manifest_path']})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main([replacements.get(value, value) for value in parsed['answer']]))
        bus = Bus(self.root / '.agentbus')
        self.assertTrue((bus.dirs('new-peer')['done'] / (pack['task_id'] + '.json')).is_file())
        receipts = [bus.read(path) for path in bus.pending('codex')]
        self.assertTrue(any(msg.type == 'result' and msg.reply_to == pack['task_id'] for msg in receipts))

    def test_malformed_workflow_reports_validation_error_without_nameerror(self):
        (self.root / '.agentbus/workflow.json').write_text('{broken', encoding='utf-8')
        with self.assertRaises(ValueError):
            build_one_click_join_prompt(self.root, 'new-peer')
