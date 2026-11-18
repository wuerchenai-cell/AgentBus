"""Source: Codex, 2026-10-04. Honest member admission and file evidence."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from receipt_helpers import receipt_ticket, receipt_source
import importlib
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentbus.bus import Bus
from agentbus.config import load, save


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cfg = load(self.root, create=True)
        for name, worker in self.cfg.data['workers'].items():
            worker['enabled'] = name in ('codex', 'workbuddy')
            worker['executor_enabled'] = worker['enabled']
        save(self.cfg)
        self.bus = Bus(self.root / '.agentbus')
        self.bus.init()

    def summary(self):
        return importlib.import_module('agentbus.onboarding').connection_summary(self.cfg)

    def member(self, name='workbuddy'):
        return next(row for row in self.summary()['members'] if row['name'] == name)

    def state_bytes(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def completed_task(self):
        task = self.bus.make('codex', 'workbuddy', 'task', body='PRIVATE_TASK_TEXT', result_to='codex')
        self.bus.post(task)
        task = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        return task

    def post_completed_receipt(self, task, result):
        for key, value in receipt_source(task).items():
            setattr(result, key, value)
        self.bus.post(result)
        self.bus.finish('workbuddy', task, status='done', ticket=receipt_ticket(task))

    def test_configuration_is_not_current_session_connection(self):
        before = self.state_bytes()
        result = self.summary()
        self.assertFalse(result['current_sessions_verified'])
        self.assertEqual([x['name'] for x in result['members']], ['codex', 'workbuddy'])
        row = self.member()
        self.assertEqual(row['mode_label'], '对话接入')
        self.assertIn('未确认', row['detail'])
        self.assertEqual(row['receipt_count'], 0)
        self.assertEqual(before, self.state_bytes(), 'observing admission must not create mailboxes')

    def test_generated_entry_uses_current_visible_button_names(self):
        from agentbus.project_workflow import prepare_project_workflow
        prepare_project_workflow(self.cfg)
        text = (self.root / 'AGENTBUS.md').read_text(encoding='utf-8')
        self.assertIn('工作台设置', text)
        self.assertIn('保存设置', text)
        self.assertIn('接入指令', text)
        self.assertNotIn('团队接入', text)
        self.assertNotIn('保存团队', text)
        self.assertNotIn('保存小组', text)

    def test_interface_configuration_is_not_a_passed_adapter(self):
        self.cfg.data['workers']['codex']['command'] = ['PRIVATE_COMMAND_TOKEN']
        row = self.member('codex')
        self.assertIn('独立接口', row['mode_label'])
        self.assertIn('未确认', row['detail'])
        self.assertNotIn('PRIVATE_COMMAND_TOKEN', json.dumps(self.summary()))

    def test_orphan_result_does_not_count_as_interconnection(self):
        folder = self.bus.dirs('codex')['ready']
        (folder / 'orphan.json').write_text(json.dumps({
            'id': 'orphan', 'sender': 'workbuddy', 'to': 'codex', 'type': 'result',
            'reply_to': 'absent', 'status': 'done', 'body': 'PRIVATE_RESULT_TEXT'}), encoding='utf-8')
        row = self.member()
        self.assertEqual(row['receipt_count'], 0)
        self.assertNotIn('PRIVATE_RESULT_TEXT', json.dumps(self.summary()))

    def test_routed_result_is_evidence_not_live_session_claim(self):
        task = self.completed_task()
        result = self.bus.make('workbuddy', 'codex', 'result', body='PRIVATE_RESULT_TEXT', reply_to=task.id)
        self.post_completed_receipt(task, result)
        row = self.member()
        self.assertEqual(row['receipt_count'], 1)
        self.assertEqual(row['consumed_receipt_count'], 0)
        self.assertFalse(row['current_session_verified'])
        self.assertIn('待收取', row['evidence_label'])
        self.assertNotIn('PRIVATE_', json.dumps(self.summary()))
        result = self.bus.claim('codex', self.bus.pending('codex')[0])
        self.bus.finish('codex', result, ticket=receipt_ticket(result))
        self.assertEqual(self.member()['consumed_receipt_count'], 1)

    def test_wrong_route_and_wrong_sender_are_not_valid_receipts(self):
        task = self.completed_task()
        result = self.bus.make('workbuddy', 'workbuddy', 'result', reply_to=task.id)
        self.post_completed_receipt(task, result)
        self.assertEqual(self.member()['receipt_count'], 0)

    def test_claimed_work_does_not_pretend_process_is_running(self):
        task = self.bus.make('codex', 'workbuddy', 'task', result_to='codex')
        self.bus.post(task)
        self.assertEqual(self.member()['indicator']['key'], 'pending')
        task = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        self.assertEqual(self.member()['indicator']['key'], 'claimed')
        self.assertIn('未确认', self.member()['indicator']['label'])
        self.bus.block('workbuddy', task, 'controlled blocker', ticket=receipt_ticket(task))
        self.assertEqual(self.member()['indicator']['key'], 'blocked')
        (self.root / '.agentbus/STOP').touch()
        self.assertEqual(self.member()['indicator']['key'], 'paused')

    def test_corrupt_message_is_attention_not_an_empty_healthy_inbox(self):
        folder = self.bus.dirs('workbuddy')['ready']
        (folder / 'bad.json').write_bytes(b'{PRIVATE_BROKEN_TEXT')
        row = self.member()
        self.assertEqual(row['indicator']['key'], 'attention')
        self.assertEqual(row['unreadable_count'], 1)
        self.assertNotIn('PRIVATE_BROKEN_TEXT', json.dumps(self.summary()))

    def test_new_product_identity_is_supported_without_known_five_assumption(self):
        self.cfg.data['workers']['sixth-agent'] = {
            'enabled': True, 'executor_enabled': True, 'can_spawn': False,
            'display_name': 'New Agent', 'kind': 'gui'}
        row = self.member('sixth-agent')
        self.assertEqual(row['display_name'], 'New Agent')
        self.assertIn('New Agent', row['next_step'])

    def test_duplicate_message_ids_do_not_create_false_receipt_evidence(self):
        task = self.completed_task()
        result = self.bus.make('workbuddy', 'codex', 'result', reply_to=task.id)
        self.post_completed_receipt(task, result)
        path = self.bus.dirs('codex')['ready'] / (result.id + '.json')
        duplicate = self.bus.dirs('workbuddy')['done'] / path.name
        duplicate.write_bytes(path.read_bytes())
        self.assertEqual(self.member()['receipt_count'], 0)

    def test_malformed_headers_are_attention_without_echoing_raw_data(self):
        folder = self.bus.dirs('workbuddy')['ready']
        (folder / 'bad-header.json').write_text(json.dumps({
            'id': 'bad-header', 'sender': 'workbuddy', 'to': 'workbuddy', 'type': 'result',
            'reply_to': {'PRIVATE_KEY': 'PRIVATE_VALUE'}, 'status': 'done',
            'ts': 'PRIVATE_TIMESTAMP'}), encoding='utf-8')
        summary = self.summary()
        self.assertEqual(self.member()['indicator']['key'], 'attention')
        self.assertNotIn('PRIVATE_', json.dumps(summary))

    def test_connections_command_is_readonly_and_never_claims_live_sessions(self):
        from agentbus.cli import main
        before = self.state_bytes()
        output = StringIO()
        with redirect_stdout(output):
            code = main(['team', 'connections', '--workspace', str(self.root)])
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertFalse(value['current_sessions_verified'])
        self.assertEqual(value['model_calls'], 0)
        self.assertEqual(before, self.state_bytes())

    def test_formal_join_confirmation_is_distinct_from_task_result_and_online(self):
        api = importlib.import_module('agentbus.onboarding')
        with patch.object(api, 'workflow_status', return_value={'ready': True}):
            result = api.confirm_connection(self.cfg, 'workbuddy')
        message = self.bus.find_message(result['id'])
        self.assertEqual(message.type, 'info')
        self.assertEqual(message.sender, 'workbuddy')
        self.assertEqual(message.to, 'codex')
        row = self.member()
        self.assertTrue(row['admission_confirmation_at'])
        self.assertEqual(row['receipt_count'], 0)
        self.assertFalse(row['current_session_verified'])
        self.assertIn('不代表', row['admission_label'])
        self.cfg.data['gates']['max_tasks'] = 7
        save(self.cfg)
        self.assertEqual(self.member()['admission_confirmation_at'], '', 'old config confirmation is stale')

    def test_join_requires_selected_identity_and_ready_workflow(self):
        api = importlib.import_module('agentbus.onboarding')
        before = self.state_bytes()
        with patch.object(api, 'workflow_status', return_value={'ready': False}):
            with self.assertRaises(ValueError): api.confirm_connection(self.cfg, 'workbuddy')
        with self.assertRaises(ValueError): api.confirm_connection(self.cfg, 'zcode')
        self.assertEqual(before, self.state_bytes())

    def test_connection_prompt_guides_formal_confirmation_without_publishing(self):
        api = importlib.import_module('agentbus.onboarding')
        before = self.state_bytes()
        with patch.object(api, 'resume_prompt', return_value='existing resume instruction'):
            text = api.connection_prompt(self.cfg, 'workbuddy')
        self.assertIn(str(self.root), text)
        self.assertIn('team join', text)
        self.assertIn('workbuddy', text)
        self.assertIn('没有程序工具', text)
        self.assertIn('不代表', text)
        self.assertEqual(before, self.state_bytes())

    def test_runner_failed_task_is_not_a_successful_cli_exit(self):
        from agentbus.cli import cmd_run
        from agentbus.director import Summary
        from types import SimpleNamespace
        args = SimpleNamespace(workspace=str(self.root), agent=None, seconds=1,
                               no_review=True, follow_ups=False, await_manual=False)
        with patch('agentbus.cli.Director') as director, redirect_stdout(StringIO()):
            director.return_value.run.return_value = Summary(failed=1)
            code = cmd_run(args)
        self.assertEqual(code, 1, 'task failure must not be hidden behind exit 0')
        director.return_value.close.assert_called_once()

    def terminal_receipt(self, status, age_hours):
        task = self.bus.make('codex', 'workbuddy', 'task', body='fixture', result_to='codex')
        self.bus.post(task)
        task = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        result = self.bus.make('workbuddy', 'codex', 'result', body='fixture', reply_to=task.id,
                               **receipt_source(task))
        result.ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
        self.bus.post(result)
        self.bus.finish('workbuddy', task, status=status, ticket=receipt_ticket(task))

    def test_recent_failure_has_warning_and_is_not_counted_as_success(self):
        self.terminal_receipt('failed', 1)
        row = self.member()
        self.assertEqual(row['indicator']['key'], 'recent_failure')
        self.assertEqual(row['failed_receipt_count'], 1)
        self.assertEqual(row['successful_receipt_count'], 0)
        self.assertEqual(row['latest_task_outcome'], 'failed')
        self.assertIn('不自动重派', row['next_step'])
        self.assertFalse(row['current_session_verified'])

    def test_latest_receipt_has_exact_readonly_detail_locator(self):
        self.terminal_receipt('failed', 1)
        row = self.member()
        result = self.bus.read(self.bus.pending('codex')[0])
        self.assertEqual(row['latest_task_id'], result.reply_to)
        self.assertEqual(row['latest_receipt_id'], result.id)
        self.assertEqual(row['latest_receipt_to'], 'codex')
        self.assertIn('查看最近任务回执', row['next_step'])
        self.assertIn(result.reply_to, row['next_step'])
        self.assertNotIn('PRIVATE_', json.dumps(self.summary()))

    def test_stop_is_visually_distinct_from_idle_without_claiming_tasks_stopped(self):
        idle = self.member()['indicator']
        (self.root / '.agentbus/STOP').touch()
        row = self.member()
        self.assertNotEqual(row['indicator']['color'], idle['color'])
        self.assertNotEqual(row['indicator']['symbol'], idle['symbol'])
        self.assertIn('在途任务可能仍在执行', row['next_step'])

    def test_old_or_future_failure_does_not_remain_an_active_warning(self):
        self.terminal_receipt('failed', 25)
        self.assertEqual(self.member()['indicator']['key'], 'idle')
        self.terminal_receipt('failed', -2)
        self.assertEqual(self.member()['indicator']['key'], 'idle')
        self.assertEqual(self.member()['failed_receipt_count'], 2)

    def test_new_success_and_active_tasks_take_precedence_over_old_failure(self):
        self.terminal_receipt('failed', 2)
        self.terminal_receipt('done', 1)
        row = self.member()
        self.assertEqual(row['indicator']['key'], 'idle')
        self.assertEqual(row['latest_task_outcome'], 'done')
        self.assertEqual(row['successful_receipt_count'], 1)
        task = self.bus.make('codex', 'workbuddy', 'task', body='new work', result_to='codex')
        self.bus.post(task)
        self.assertEqual(self.member()['indicator']['key'], 'pending')

    def test_direct_dispatch_failed_task_is_not_a_successful_cli_exit(self):
        from agentbus.cli import cmd_boss
        from agentbus.director import Summary
        from types import SimpleNamespace
        args = SimpleNamespace(workspace=str(self.root), goal='bounded task', worker='workbuddy',
                               seconds=1, no_review=True, await_manual=False)
        self.cfg.data['workers']['codex']['can_spawn'] = True
        with patch('agentbus.cli._load', return_value=self.cfg), \
                patch('agentbus.cli.Director') as director, redirect_stdout(StringIO()):
            director.return_value.submit.return_value = []
            director.return_value.run.return_value = Summary(failed=1)
            code = cmd_boss(args)
        self.assertEqual(code, 1)
        director.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
