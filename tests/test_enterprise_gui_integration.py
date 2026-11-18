"""Enterprise GUI integration contracts, Codex internal helper, 2026-10-05.

No vendor/model/CU invocation. Temporary public workspaces and real withdrawn
Tk rendering do not certify the frozen EXE or unattended desktop recovery.
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus import gui
from agentbus.config import load


class PublicSnapshotContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        self.cfg.data['workers'] = {
            'codex': {'enabled': True, 'display_name': 'Codex'},
            'reader': {'enabled': True, 'display_name': '只读成员'},
        }

    def record(self, member, folder, mid, **extra):
        directory = self.cfg.workspace / '.agentbus/agents' / member / folder
        directory.mkdir(parents=True, exist_ok=True)
        record = {'id': mid, 'type': 'task', 'sender': 'codex', 'to': member,
                  'status': 'pending', 'ts': '2026-10-05T21:00:00+08:00'}
        record.update(extra)
        (directory / (mid + '.json')).write_text(json.dumps(record), encoding='utf-8')

    def job(self, index, **extra):
        directory = self.cfg.workspace / '.agentbus/jobs'
        directory.mkdir(parents=True, exist_ok=True)
        job = {'schema_version': 1, 'id': 'j-%02d' % index,
               'goal': '目标 %d' % index, 'created_ts': index,
               'created_at': '2026-10-05T21:00:00+08:00', 'tasks': [],
               'checkpoints': [], 'finalization': None}
        job.update(extra)
        (directory / (job['id'] + '.json')).write_text(json.dumps(job), encoding='utf-8')

    def test_real_task_state_and_checkpoint_are_observed_without_job_execution(self):
        self.record('reader', 'processing', 'm-one', status='claimed')
        self.job(1, tasks=[{'id': 'm-one', 'worker': 'reader', 'title': '读取样例'}],
                 checkpoints=[{'caller': 'reader', 'at': 'now', 'note': '已读，等待审核'}])
        with patch('agentbus.jobs.list_jobs', side_effect=AssertionError('unbounded scan')):
            value = gui.read_public_snapshot(self.cfg)
        self.assertEqual(value['jobs'][0]['tasks'][0]['location'], 'processing')
        self.assertEqual(value['jobs'][0]['tasks'][0]['status'], 'claimed')
        self.assertFalse(value['jobs'][0]['artifacts_verified'])
        self.assertEqual(value['jobs'][0]['checkpoints'][0]['caller'], 'reader')
        self.assertEqual(value['records_read'], 1)

    def test_jobs_are_bounded_and_truncation_is_not_an_empty_team(self):
        for i in range(23):
            self.job(i)
        result = gui.read_public_snapshot(self.cfg)
        self.assertLessEqual(len(result['jobs']), 20)
        self.assertTrue(result['health']['truncated'])
        self.assertTrue(result['records_incomplete'])
        self.assertIn('作业', result['incomplete_reason'])

    def test_message_reads_are_bounded_and_claim_is_not_execution(self):
        for i in range(2003):
            self.record('reader', 'ready', 'm-%04d' % i)
        result = gui.read_public_snapshot(self.cfg, read_window_s=60)
        self.assertEqual(result['records_read'], 2000)
        self.assertTrue(result['health']['truncated'])
        self.assertNotIn('在线', json.dumps(result['health'], ensure_ascii=False))

    def test_bad_record_is_unknown_and_no_private_directory_is_read(self):
        self.record('reader', 'ready', 'm-good')
        directory = self.cfg.workspace / '.agentbus/agents/reader/ready'
        (directory / 'm-bad.json').write_text('{', encoding='utf-8')
        result = gui.read_public_snapshot(self.cfg)
        reader = next(m for m in result['health']['members'] if m['name'] == 'reader')
        self.assertGreater(reader['unreadable_count'], 0)
        self.assertIn('无法读取', result['incomplete_reason'])

    def test_invalid_job_files_also_count_toward_read_limit(self):
        directory = self.cfg.workspace / '.agentbus/jobs'
        directory.mkdir(parents=True, exist_ok=True)
        for i in range(35):
            (directory / ('j-%03d.json' % i)).write_text('{', encoding='utf-8')
        result = gui.read_public_snapshot(self.cfg)
        self.assertEqual(result['jobs_read'], 20)
        self.assertTrue(result['records_incomplete'])

    def test_job_task_intents_share_one_total_bound(self):
        for i in range(3):
            self.job(i, tasks=[{'id': 'm-%d-%d' % (i, n), 'worker': 'reader'}
                               for n in range(900)])
        result = gui.read_public_snapshot(self.cfg)
        self.assertLessEqual(sum(len(row['tasks']) for row in result['jobs']), 2000)
        self.assertTrue(result['records_incomplete'])


class SnapshotWorkerContracts(unittest.TestCase):
    def test_checker_runs_once_off_calling_thread_and_only_returns_data(self):
        seen = []
        started = threading.Event()
        release = threading.Event()
        def checker(value):
            seen.append(threading.get_ident())
            started.set()
            release.wait(1)
            return {'value': value}
        task = gui.OverviewSnapshotTask(checker=checker, timeout_s=2)
        self.addCleanup(task.close)
        self.assertTrue(task.start(value=42))
        self.assertTrue(started.wait(1))
        self.assertFalse(task.start(value=43))
        release.set()
        task.worker.join(1)
        event = task.poll()
        self.assertEqual(event['value'], {'value': 42})
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], threading.get_ident())

    def test_timeout_retains_busy_worker_and_discards_late_result(self):
        release = threading.Event()
        task = gui.OverviewSnapshotTask(checker=lambda: release.wait(1), timeout_s=0.01)
        self.addCleanup(task.close)
        self.assertTrue(task.start())
        time.sleep(0.02)
        self.assertEqual(task.poll()['kind'], 'timeout')
        self.assertFalse(task.start())
        release.set()
        task.worker.join(1)
        self.assertIsNone(task.poll())

    def test_fast_completed_result_is_not_discarded_when_main_thread_polls_late(self):
        task = gui.OverviewSnapshotTask(checker=lambda: {'ok': True}, timeout_s=0.1)
        self.addCleanup(task.close)
        task.start()
        task.worker.join(1)
        time.sleep(0.15)
        self.assertEqual(task.poll()['kind'], 'result')


class EnterpriseRenderingContracts(unittest.TestCase):
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
            task = getattr(self.ui, '_overview_task', None)
            if task is not None:
                task.close()
            self.ui._stop_watchtower()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def sample(self):
        return {'goal': '交付正式项目工作台',
                'goal_source': {'job_id': 'j-real', 'selected_by': 'explicit', 'reason': '明确作业'},
                'stage_label': '待验收', 'project_title': '示例项目', 'coordinator': 'codex',
                'member_count': 1, 'route_label': '文件协作', 'headline': '已认领，运行未确认',
                'action_id': 'tasks', 'next_action': '回原会话报告', 'records_incomplete': False,
                'cards': [{'id': key, 'label': title, 'count': n} for key, title, n in
                          (('tasks', '任务', 1), ('deliverables', '已验证任务交付', 0),
                           ('ready', '待接手', 0), ('blocked', '阻塞', 0))],
                'members': [{'id': 'codex', 'name': 'Codex', 'state_label': '已认领，运行未确认',
                             'observed_at': '真实观察', 'evidence': ['已有认领记录'], 'tone': 'info'}],
                'responsibilities': [{'member_id': 'codex', 'member': 'Codex', 'task': '首页接线',
                                      'checkpoint': '作业级续接记录', 'state': '已认领，运行未确认',
                                      'next_step': '回原会话报告'}],
                'members_collapsed': [{'id': 'codex', 'name': 'Codex', 'role': '负责人',
                                       'state_label': '已认领，运行未确认'}]}

    def test_home_renders_goal_cards_responsibilities_and_goal_source(self):
        self.ui._render_project_overview(self.sample(), observed_at='真实观察')
        self.assertIn('交付正式项目工作台', self.ui.overview_goal_var.get())
        self.assertIn('j-real', self.ui.overview_goal_source_var.get())
        self.assertEqual(len(self.ui.overview_card_vars), 4)
        self.assertEqual(self.ui.overview_card_vars['tasks'].get(), '1')
        row = self.ui.overview_tree.item('codex', 'values')
        self.assertIn('首页接线', row)
        self.assertIn('已认领，运行未确认', row)
        self.assertIn('回原会话报告', row)
        self.assertEqual(list(self.ui.pages), ['home', 'team', 'tasks', 'access', 'settings', 'logs'])

    def test_error_preserves_previous_goal_but_never_presents_zero_as_confirmed(self):
        self.ui._render_project_overview(self.sample(), observed_at='先前观察')
        self.ui._render_project_overview(self.sample(), observed_at='先前观察',
                                          read_error='读取超时，旧快照已保留')
        self.assertIn('交付正式项目工作台', self.ui.overview_goal_var.get())
        self.assertIn('先前观察', self.ui.overview_evidence_var.get())
        self.assertIn('未确认', self.ui.overview_summary_var.get())
        self.assertTrue(all(var.get() == '—' for var in self.ui.overview_card_vars.values()))
        self.assertEqual(self.ui._overview_view['action_id'], 'tasks')

    def test_member_details_are_secondary_and_invitation_action_exists(self):
        self.assertEqual(self.ui.overview_members_btn.cget('text'), '成员详情')
        self.assertEqual(self.ui.invitation_prepare_btn.cget('text'), '生成成员接入指令')
        self.assertTrue(self.ui.invitation_writable.get())

    def test_invitation_preparation_is_off_tk_thread_and_reuses_request_identity(self):
        cfg = load(Path(self.temp.name), create=True)
        self.ui.cfg = cfg
        self.ui.ws_var.set(str(cfg.workspace))
        self.ui.connection_agent.set('codex')
        calls = []
        def prepare(*args, **kwargs):
            calls.append((threading.get_ident(), kwargs['request_id'], kwargs['writable']))
            return {'invite_id': 'i-real', 'task_id': 'm-real', 'state': 'committed',
                    'instructions': {'leader': '给负责人', 'newcomer': '给新成员'}}
        with patch.object(self.ui, '_workflow_cfg', return_value=cfg), \
             patch('agentbus.member_invitation.prepare_invitation', side_effect=prepare), \
             patch.object(self.ui, '_show_invitation_dialog') as show:
            self.ui._prepare_member_invitation()
            self.ui._invitation_task.worker.join(1)
            self.ui._poll_member_invitation()
            self.ui._prepare_member_invitation()
            self.ui._invitation_task.worker.join(1)
            self.ui._poll_member_invitation()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertTrue(all(tid != threading.get_ident() for tid, _, _ in calls))
        self.assertEqual(show.call_count, 2)

    def test_copy_records_only_copied_not_sent_or_verified(self):
        cfg = load(Path(self.temp.name), create=True)
        self.ui.cfg = cfg
        observed = []
        with patch('agentbus.member_invitation.record_invitation_event',
                   side_effect=lambda *args: observed.append(args)), \
             patch.object(self.ui, 'log'):
            self.ui._copy_member_invitation(cfg, 'codex', 'i-real', 'leader', '实际负责人指令')
            self.ui._invitation_task.worker.join(1)
            self.ui._poll_member_invitation()
        self.assertEqual(self.root.clipboard_get(), '实际负责人指令')
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][3], 'copied')
        self.assertIn('非送达', observed[0][4])

    def test_late_snapshot_does_not_hide_unsaved_team_changes(self):
        cfg = load(Path(self.temp.name), create=True)
        self.ui.cfg = cfg
        self.ui.ws_var.set(str(cfg.workspace))
        self.ui.director_var.set(cfg.director)
        self.ui.worker_vars = {key: gui.tk.BooleanVar(value=True)
                               for key in cfg.enabled_workers()}
        release = threading.Event()
        task = gui.OverviewSnapshotTask(checker=lambda **_: release.wait(1) or {})
        # Supply a real dictionary once the first request is in flight.
        task.checker = lambda **_: (release.wait(1) and
            {'health': None, 'jobs': [], 'observed_at': '先前观察', 'records_incomplete': False})
        self.ui._overview_task = task
        self.ui._refresh_workbench_overview()
        self.ui.worker_vars['codex'].set(False)
        release.set()
        task.worker.join(1)
        self.ui._poll_workbench_snapshot()
        self.assertEqual(self.ui.overview_summary_var.get(), '团队有未保存修改')
        self.assertEqual(self.ui._overview_view['action_id'], 'team')


if __name__ == '__main__':
    unittest.main()
