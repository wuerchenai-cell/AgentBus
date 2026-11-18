"""Desktop wiring contracts; temporary public workspaces, no native CU claims.

Source: Codex desktop integration, 2026-10-08.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from agentbus import design_system, gui, recovery_center
from agentbus.bus import Bus
from agentbus.config import load
from agentbus.claim_fencing import issue, apply_ticket
from agentbus.heartbeat import record as heartbeat_record


class EvidenceWiring(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        self.cfg.data['workers'] = {'codex': {'enabled': True}}
        self.root = self.cfg.workspace / '.agentbus'
        self.task = {'id': 'm-desktop-test', 'to': 'codex', 'type': 'task',
                     'status': 'claimed', 'title': '真实任务',
                     'workspace': str(self.cfg.workspace), 'deadline_ts': 0.0}
        self.ticket = issue(self.task['id'], 'codex', 1)
        apply_ticket(self.task, self.ticket)
        self.path = self.root / 'agents/codex/processing/m-desktop-test.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.task), encoding='utf-8')

    def heartbeat(self, **fields):
        heartbeat_record(self.root, self.task['id'], 'codex', self.ticket,
                         task_record=self.task)
        path = self.root / 'heartbeats/m-desktop-test/codex.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data.update(fields)
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def evidence(self):
        return gui.read_public_snapshot(self.cfg)['member_evidence']['codex']

    def test_current_bound_heartbeat_is_execution_evidence(self):
        self.heartbeat()
        self.assertTrue(self.evidence()['heartbeat_verified'])

    def test_touch_and_claim_do_not_create_heartbeat(self):
        os.utime(self.path, None)
        evidence = self.evidence()
        self.assertEqual(evidence['claimed_count'], 1)
        self.assertFalse(evidence['heartbeat_verified'])

    def test_stale_or_future_timestamp_is_not_execution_evidence(self):
        for timestamp in (time.time() - 86400, time.time() + 86400, float('nan')):
            with self.subTest(timestamp=timestamp):
                self.heartbeat(at_epoch_s=timestamp)
                self.assertFalse(self.evidence()['heartbeat_verified'])

    def test_wrong_agent_task_epoch_claim_or_project_is_rejected(self):
        for fields in ({'agent': 'trae'}, {'task_id': 'm-another'},
                       {'claim_epoch': 2}, {'claim_id': 'a' * 32},
                       {'project_id': str(self.cfg.workspace / 'other')}):
            with self.subTest(fields=fields):
                self.heartbeat(**fields)
                self.assertFalse(self.evidence()['heartbeat_verified'])

    def test_revoked_ticket_cannot_light_running(self):
        self.heartbeat()
        self.task['claim_id'] = ''
        self.path.write_text(json.dumps(self.task), encoding='utf-8')
        self.assertFalse(self.evidence()['heartbeat_verified'])

    def test_terminal_leftover_in_processing_is_not_active_heartbeat(self):
        self.heartbeat()
        self.task['status'] = 'done'
        self.path.write_text(json.dumps(self.task), encoding='utf-8')
        self.assertFalse(self.evidence()['heartbeat_verified'])

    def test_explicit_stop_is_passed_without_headline_guessing(self):
        (self.root / 'STOP').write_text('halt', encoding='utf-8')
        snapshot = gui.read_public_snapshot(self.cfg)
        self.assertTrue(snapshot['stopped'])
        self.assertTrue(snapshot['recovery_tasks'][0]['stop'])

    def test_recovery_state_keeps_no_deadline_as_unexpired_and_no_cu_permission(self):
        row = gui.read_public_snapshot(self.cfg)['recovery_tasks'][0]
        self.assertFalse(row['deadline_expired'])
        self.assertFalse(row['computer_use_authorized'])
        self.assertEqual(recovery_center.decide_recovery(row)['action'], 'continue')

    def test_public_projection_never_exposes_claim_id_or_body(self):
        self.task['body'] = 'PRIVATE_FULL_TASK_TEXT'
        self.path.write_text(json.dumps(self.task), encoding='utf-8')
        result = json.dumps(gui.read_public_snapshot(self.cfg), ensure_ascii=False)
        self.assertNotIn(self.ticket.claim_id, result)
        self.assertNotIn('PRIVATE_FULL_TASK_TEXT', result)

    def test_expired_claim_budget_blocks_file_continuation(self):
        self.task.update(budget_s=60,
                         claimed_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
        self.path.write_text(json.dumps(self.task), encoding='utf-8')
        row = gui.read_public_snapshot(self.cfg)['recovery_tasks'][0]
        self.assertTrue(row['budget_expired'])
        self.assertEqual(recovery_center.decide_recovery(row)['action'], 'blocked')

    def test_only_explicit_task_bound_session_stop_makes_continuation_pack(self):
        path = self.root / 'session-stops' / (self.task['id'] + '.json')
        path.parent.mkdir(parents=True)
        for task_id, expected in (('m-other', 'continue'), (self.task['id'], 'new_chat_pack')):
            path.write_text(json.dumps({'task_id': task_id, 'agent': 'codex', 'stopped': True}),
                            encoding='utf-8')
            row = gui.read_public_snapshot(self.cfg)['recovery_tasks'][0]
            self.assertEqual(recovery_center.decide_recovery(row)['action'], expected)


class ActionContracts(unittest.TestCase):
    def test_zero_layout_pixel_stays_zero_at_each_scaling(self):
        for scale in (96 / 72, 2.0, 4.0):
            self.assertEqual(design_system.scaled(0, scale), 0)

    def test_recovery_dispatch_rechecks_stop_at_click(self):
        callback = Mock()
        with self.assertRaises(ValueError):
            recovery_center.execute_recovery_action(
                {'task_claimed': True, 'stop': True}, 'continue', {'continue': callback})
        callback.assert_not_called()

    def test_stale_recommendation_cannot_execute_different_action(self):
        callback = Mock()
        with self.assertRaises(ValueError):
            recovery_center.execute_recovery_action(
                {'task_claimed': True}, 'new_chat_pack', {'new_chat_pack': callback})
        callback.assert_not_called()

    def test_continue_and_new_chat_callbacks_are_real_and_do_not_claim(self):
        for state, action in (({'task_claimed': True}, 'continue'),
                              ({'process_exited': True}, 'new_chat_pack')):
            callback = Mock(return_value='actual callback result')
            result = recovery_center.execute_recovery_action(state, action, {action: callback})
            self.assertEqual(result, 'actual callback result')
            callback.assert_called_once()

    def test_unavailable_computer_control_is_a_blocked_callback(self):
        with self.assertRaises(ValueError):
            recovery_center.execute_recovery_action(
                {'process_exited': True, 'computer_use_authorized': True},
                'computer_use', {})

    def test_invalid_project_match_does_not_allow_recovery(self):
        self.assertEqual(recovery_center.decide_recovery(
            {'project_matches': 'true', 'task_claimed': True})['action'], 'blocked')

    def test_registered_wizard_uses_created_task_and_original_instructions(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = load(folder, create=True)
            value = {'state': 'committed', 'invite_id': 'i-actual', 'task_id': 'm-actual',
                     'input_path': '成果/成员接入/i-actual/input.txt',
                     'output_path': '成果/成员接入/i-actual/output.txt',
                     'instructions': {'leader': '已创建 m-actual，不能再创建',
                                      'newcomer': '仅处理 m-actual'}}
            model = gui.registered_wizard(cfg, 'codex', value, writable=True)
            self.assertEqual(model['instructions'], value['instructions'])
            self.assertEqual(model['sample']['task_id'], 'm-actual')
            self.assertFalse(model['execution_verified'])
            self.assertIn('当前会话', model['continuity_boundary'])


class TkWiring(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = gui.tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.cleanup)
        with patch('agentbus.gui.configure_initial_window'), \
             patch('agentbus.gui.initial_workspace', return_value=self.temp.name), \
             patch.object(gui.Launcher, '_auto_detect'):
            self.ui = gui.Launcher(self.root)
        self.root.update_idletasks()

    def cleanup(self):
        if hasattr(self, 'ui'):
            self.ui._overview_task.close()
            self.ui._stop_watchtower()
            task = getattr(self.ui, '_recovery_task', None)
            if task:
                task.close()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def view(self, n=1):
        return {'goal': '动态真实目标', 'coordinator': 'codex', 'route_label': '文件协作',
                'headline': '任意语言 halt', 'stage_label': '待验收', 'action_id': 'home',
                'next_action': '输入目标', 'cards': [],
                'members': [{'id': 'member-%d' % i, 'name': '成员%d' % i} for i in range(n)],
                'responsibilities': [{'member_id': 'member-%d' % i, 'member': '成员%d' % i,
                                      'task': '责任%d' % i, 'checkpoint': '续接点%d' % i,
                                      'state': '状态未知', 'next_step': '回原会话报告'}
                                     for i in range(n)]}

    def test_shell_primary_action_uses_stop_bool_and_callback_navigation(self):
        view = self.view()
        view['stopped'] = True
        self.ui._render_project_overview(view)
        self.assertEqual(self.ui.overview_next_btn.cget('text'), '查看暂停/阻塞原因')
        with patch.object(self.ui, '_show_page') as show:
            self.ui.overview_next_btn.invoke()
        show.assert_called_once_with('tasks')
        view['stopped'] = False
        view['headline'] = '暂停一词只是目标中的引用'
        self.ui._render_project_overview(view)
        self.assertEqual(self.ui._overview_view['action_id'], 'home')

    def test_member_light_is_rendered_from_heartbeat_evidence(self):
        view = self.view()
        view['member_evidence'] = {'member-0': {'heartbeat_verified': True}}
        self.ui._render_project_overview(view)
        self.assertIn('正在执行（有心跳证据）', self.ui.overview_tree.item('member-0', 'values'))
        self.assertTrue(self.ui._overview_shell['members'][0]['status_light']['execution_verified'])

    def test_read_error_does_not_keep_old_heartbeat_as_current_execution(self):
        view = self.view()
        view['member_evidence'] = {'member-0': {'heartbeat_verified': True}}
        self.ui._render_project_overview(view, read_error='读取超时')
        self.assertFalse(self.ui._overview_shell['members'][0]['status_light']['execution_verified'])
        self.assertIn('状态未确认', self.ui.overview_tree.item('member-0', 'values'))

    def test_twenty_members_fold_and_detail_keeps_all_members(self):
        self.ui._render_project_overview(self.view(20))
        self.assertGreater(self.ui._overview_shell['overflow_count'], 0)
        self.assertLess(len(self.ui.overview_tree.get_children()), 20)
        with patch.object(self.ui, '_show_readonly_text') as show:
            self.ui._show_member_details()
        self.assertIn('成员19', show.call_args.args[1])
        self.assertEqual(list(self.ui.pages), ['home', 'team', 'tasks', 'access', 'settings', 'logs'])

    def test_recovery_continue_opens_real_resume_instruction_without_releasing_claim(self):
        cfg = load(self.temp.name, create=True)
        state = {'member_id': 'codex', 'task_id': 'm-actual', 'task_claimed': True}
        with patch('agentbus.gui.read_public_snapshot', return_value={'recovery_tasks': [state]}), \
             patch('agentbus.gui.resume_prompt', return_value={'prompt': '当前会话的实际续接指令'}), \
             patch.object(self.ui, '_show_readonly_text') as show, \
             patch.object(Bus, 'recover_claim') as release:
            self.ui._execute_recovery(cfg, 'm-actual', 'continue')
        self.assertIn('实际续接指令', show.call_args.args[1])
        release.assert_not_called()

    def test_recovery_dialog_has_one_actual_action_callback(self):
        cfg = load(self.temp.name, create=True)
        state = {'member_id': 'codex', 'task_id': 'm-actual', 'task_claimed': True}
        with patch.object(self.ui, '_execute_recovery') as execute:
            self.ui._show_recovery_dialog(cfg, state)
            self.assertEqual(self.ui.recovery_action_btn.cget('text'), '继续原对话')
            self.ui.recovery_action_btn.invoke()
        execute.assert_called_once_with(cfg, 'm-actual', 'continue')

    def test_stop_arriving_before_click_blocks_gui_callback(self):
        cfg = load(self.temp.name, create=True)
        state = {'member_id': 'codex', 'task_id': 'm-actual', 'task_claimed': True, 'stop': True}
        with patch('agentbus.gui.read_public_snapshot', return_value={'recovery_tasks': [state]}), \
             patch.object(self.ui, '_show_readonly_text') as show, \
             patch('agentbus.gui.messagebox.showwarning') as warning:
            self.ui._execute_recovery(cfg, 'm-actual', 'continue')
        show.assert_not_called()
        warning.assert_called_once()

    def test_blocked_recovery_action_is_disabled(self):
        cfg = load(self.temp.name, create=True)
        self.ui._show_recovery_dialog(cfg, {'member_id': 'codex', 'task_id': 'm-actual', 'stop': True})
        self.assertTrue(self.ui.recovery_action_btn.instate(['disabled']))

    def test_navigation_shortcuts_are_bound_to_existing_six_ids(self):
        self.assertTrue(self.root.bind('<Alt-n>'))
        for index in range(1, 7):
            self.assertTrue(self.root.bind('<Alt-Key-%d>' % index))

    def live_claim(self):
        cfg = load(self.temp.name, create=True)
        bus = Bus(cfg.workspace / '.agentbus')
        bus.init()
        task = bus.make('codex', 'codex', 'task', '桌面安全回调',
                        '执行者保留在途所有权', result_to='codex')
        bus.post(task)
        task = bus.claim('codex', bus.pending('codex')[0])
        self.ui.cfg = cfg
        self.ui.ws_var.set(str(cfg.workspace))
        return cfg, bus, task, bus.processing('codex')[0]

    def test_actual_maintenance_button_observes_live_task_then_continues_without_recovery(self):
        cfg, bus, task, path = self.live_claim()
        original = path.read_bytes()
        with patch.object(self.ui, '_workflow_cfg', return_value=cfg), \
             patch('agentbus.gui.simpledialog.askstring', return_value=task.id), \
             patch('agentbus.gui.resume_prompt', return_value='当前项目的实际续接指令'), \
             patch.object(self.ui, '_show_readonly_text') as show, \
             patch.object(Bus, 'recover_claim', side_effect=AssertionError('ownership transfer')):
            self.ui.recovery_open_btn.invoke()
            self.ui._recovery_task.worker.join(2)
            self.ui._poll_recovery_center()
            self.assertEqual(self.ui.recovery_action_btn.cget('text'), '继续原对话')
            self.ui.recovery_action_btn.invoke()
        self.assertIn(task.id, show.call_args.args[1])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(bus.pending('codex')), 0)

    def test_real_stop_added_after_recovery_dialog_blocks_click_and_keeps_ticket(self):
        cfg, bus, task, path = self.live_claim()
        original = path.read_bytes()
        state = gui.read_public_snapshot(cfg)['recovery_tasks'][0]
        self.ui._show_recovery_dialog(cfg, state)
        (bus.root / 'STOP').write_text('真实 STOP 竞态', encoding='utf-8')
        with patch.object(self.ui, '_show_readonly_text') as show, \
             patch('agentbus.gui.messagebox.showwarning') as warning, \
             patch.object(Bus, 'recover_claim', side_effect=AssertionError('ownership transfer')):
            self.ui.recovery_action_btn.invoke()
        show.assert_not_called()
        warning.assert_called_once()
        self.assertIn('STOP', warning.call_args.args[1])
        self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
