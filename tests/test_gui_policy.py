from __future__ import annotations

from receipt_helpers import receipt_ticket

import gc
import json
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus.config import Config, load, save
from agentbus import gui
from agentbus.gui import Launcher, merge_probe_patch


class Value:
    def __init__(self, value=None):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class Box:
    def __init__(self):
        self.values = None
    def configure(self, **kwargs):
        self.values = kwargs.get("values", self.values)


class TextBox:
    def __init__(self):
        self.text = ''
        self.state = 'disabled'
    def configure(self, **kwargs):
        self.state = kwargs.get('state', self.state)
    def delete(self, *_args):
        self.text = ''
    def insert(self, _where, text):
        self.text += text


class Frame:
    def __init__(self):
        self.children = []
    def winfo_children(self):
        return self.children


class Checkbutton:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
    def pack(self, **kwargs):
        pass
    def grid(self, **kwargs):
        self.grid_options = kwargs
    def bind(self, *args, **kwargs):
        pass


class GuiPolicyTests(unittest.TestCase):
    @staticmethod
    def _destroy_fixture_root(root):
        # Source: observed callbacks leaking across isolated Tk fixtures,
        # Codex, 2026-10-04. A direct test destroy skips Launcher._close.
        # Cancel this interpreter's scheduled callbacks before teardown.
        for callback in root.tk.call('after', 'info'):
            root.after_cancel(callback)
        root.destroy()

    # Codex, 2026-10-03: frozen GUI reopen showed an empty workflow page even
    # though the project entry was usable; display must remain observational.
    def _workflow_fixture(self, root, name='saved-project'):
        from agentbus.project_workflow import prepare_project_workflow
        cfg = load(root / name, create=True)
        exe = root / (name + '-AgentBus.exe')
        exe.write_bytes(b'isolated GUI fixture, never execute')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
            prepare_project_workflow(cfg)
        ui = self._member_change_ui(cfg)
        ui.cfg = None
        ui.workflow_box = TextBox()
        ui._gen_mcp = lambda: None
        return cfg, exe, ui

    def _load_workflow_read_only(self, ui):
        with patch('agentbus.gui.tk.BooleanVar', side_effect=lambda **kw: Value(kw['value'])), \
             patch('agentbus.gui.ttk.Checkbutton', side_effect=lambda *a, **kw: Checkbutton(**kw)), \
             patch('agentbus.gui.prepare_project_workflow', side_effect=AssertionError('display must not prepare')):
            ui._load_cfg(silent=True, monitor=False)

    def _workflow_snapshot(self, root):
        return {path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob('*') if path.is_file()}

    def test_reopening_configured_project_shows_ready_workflow_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg, exe, ui = self._workflow_fixture(root)
            before = self._workflow_snapshot(root)
            self._load_workflow_read_only(ui)
            text = ui.workflow_box.text
            self.assertIn(str(cfg.workspace), text)
            self.assertIn('组长：codex', text)
            self.assertIn('成员：', text)
            self.assertIn('已准备', text)
            self.assertIn('摘要', text)
            self.assertIn('匹配', text)
            self.assertIn('使用这个小组工作', text)
            self.assertEqual(ui.workflow_box.state, 'disabled')
            self.assertEqual(before, self._workflow_snapshot(root))

    def test_reopening_corrupted_entry_shows_specific_reason_without_prepare(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg, exe, ui = self._workflow_fixture(root)
            exe.write_bytes(b'Xsolated GUI fixture, never execute')
            before = self._workflow_snapshot(root)
            self._load_workflow_read_only(ui)
            text = ui.workflow_box.text
            self.assertIn(str(cfg.workspace), text)
            self.assertIn('SHA256', text)
            self.assertIn('不一致', text)
            self.assertIn('保存团队', text)
            self.assertIn('正确', text)
            self.assertEqual(before, self._workflow_snapshot(root))

    def test_switching_projects_replaces_workflow_page_instead_of_leaving_old_info(self):
        from agentbus.config import save
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, exe, ui = self._workflow_fixture(root, 'first-project')
            second, second_exe, unused = self._workflow_fixture(root, 'second-project')
            second.data['director'] = 'workbuddy'
            save(second)
            before = self._workflow_snapshot(root)
            self._load_workflow_read_only(ui)
            self.assertIn(str(first.workspace), ui.workflow_box.text)
            ui.ws_var.set(str(second.workspace))
            self._load_workflow_read_only(ui)
            self.assertIn(str(second.workspace), ui.workflow_box.text)
            self.assertIn('组长：workbuddy', ui.workflow_box.text)
            self.assertNotIn(str(first.workspace), ui.workflow_box.text)
            self.assertEqual(before, self._workflow_snapshot(root))

    def test_switching_to_unsaved_project_shows_save_hint_without_old_info_or_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, exe, ui = self._workflow_fixture(root)
            self._load_workflow_read_only(ui)
            fresh = root / 'unsaved-project'
            fresh.mkdir()
            before = self._workflow_snapshot(root)
            ui.ws_var.set(str(fresh))
            self._load_workflow_read_only(ui)
            self.assertIn(str(fresh), ui.workflow_box.text)
            self.assertIn('保存团队', ui.workflow_box.text)
            self.assertNotIn(str(first.workspace), ui.workflow_box.text)
            self.assertIsNone(ui.cfg)
            self.assertIsNone(ui.bus)
            self.assertEqual(before, self._workflow_snapshot(root))
            self.assertFalse((fresh / '.agentbus').exists())

    def test_explicit_prepare_reuses_the_same_workflow_status_rendering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg, exe, ui = self._workflow_fixture(root)
            self._load_workflow_read_only(ui)
            reopened = ui.workflow_box.text
            self.assertTrue(reopened, 'reopened project needs a visible workflow summary')
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
                ui._prepare_workflow()
            self.assertEqual(ui.workflow_box.text, reopened)

    # Source: actual frozen GUI at high DPI, Codex, 2026-10-03.
    def test_initial_windows_layout_keeps_job_panel_visible(self):
        root = MagicMock()
        with patch.object(gui.sys, 'platform', 'win32'):
            gui.configure_initial_window(root)
        root.geometry.assert_called_once_with('1180x760')
        root.state.assert_called_once_with('zoomed')

    def test_initial_non_windows_layout_keeps_normal_window(self):
        root = MagicMock()
        with patch.object(gui.sys, 'platform', 'linux'):
            gui.configure_initial_window(root)
        root.geometry.assert_called_once_with('1180x760')
        root.state.assert_not_called()

    def test_initial_zoom_failure_does_not_break_launch(self):
        root = MagicMock()
        root.state.side_effect = gui.tk.TclError('not supported')
        with patch.object(gui.sys, 'platform', 'win32'):
            gui.configure_initial_window(root)
        root.minsize.assert_called_once_with(1100, 620)

    def tearDown(self):
        # Source: observed Tk cleanup warning, 2026-09-30. Collect mock/Tk
        # reference cycles on their creating thread before threaded tests run.
        gc.collect()

    # Source: user's OpenCode desktop visibility question, 2026-10-02.
    def test_worker_labels_offer_desktop_opencode_and_optional_independent_capability(self):
        label = gui.worker_label('opencode', {'can_spawn': True})
        self.assertIn('已配置', label)
        self.assertNotIn('已实测', label)
        self.assertIn('桌面文件交接', label)
        self.assertIn('独立接口可选', label)
        self.assertNotIn('不进入桌面聊天', label)
        custom = gui.worker_label('cursor', {'can_spawn': False, 'display_name': 'Cursor编辑器'})
        self.assertIn('Cursor编辑器', custom)
        self.assertIn('cursor', custom)
        imported = gui.worker_label('future-agent', {'can_spawn': False, 'label': '未来助手'})
        self.assertIn('未来助手', imported)
        antigravity = gui.worker_label('antigravity', {
            'can_spawn': False, 'display_name': 'Google Antigravity', 'transport': 'file'})
        self.assertIn('Google Antigravity', antigravity)
        self.assertIn('文件主路线', antigravity)

    def test_probe_banner_reports_tested_release_without_changing_selection(self):
        from agentbus.detect import Probe
        ui = Launcher.__new__(Launcher)
        ui.detect_label = MagicMock()
        ui.log = MagicMock()
        ui.worker_vars = {'opencode': Value(True), 'workbuddy': Value(True)}
        with patch('agentbus.gui.detect_group_agents', return_value={
                'opencode': Probe('opencode', 'acp', installed=True, can_spawn=True,
                                  version='opencode v2.0.22'),
                'workbuddy': Probe('workbuddy', 'acp', installed=True,
                                   candidate_adapter=True)}):
            ui._refresh_product_probes()
        text = ui.detect_label.configure.call_args.kwargs['text']
        self.assertIn('独立接口短任务已实测：opencode v2.0.22', text)
        self.assertIn('不代表整组自动接力', text)
        self.assertIn('待验收候选：workbuddy', text)
        self.assertTrue(ui.worker_vars['opencode'].get())
        self.assertTrue(ui.worker_vars['workbuddy'].get())

    def test_probe_banner_unknown_release_never_claims_acceptance(self):
        from agentbus.detect import Probe
        ui = Launcher.__new__(Launcher)
        ui.detect_label = MagicMock()
        ui.log = MagicMock()
        with patch('agentbus.gui.detect_group_agents', return_value={
                'opencode': Probe('opencode', 'acp', installed=True,
                                  candidate_adapter=True, version='opencode v2.0.23')}):
            ui._refresh_product_probes()
        text = ui.detect_label.configure.call_args.kwargs['text']
        self.assertIn('待验收候选：opencode', text)
        self.assertNotIn('自动接力已实测', text)

    def test_jobs_panel_shows_human_summary_and_separate_technical_details(self):
        from agentbus.jobs import start_job, dispatch_job, checkpoint_job
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = self._member_change_ui(cfg)
            ui.jobs_box = MagicMock()
            job = start_job(cfg, 'workbuddy', '核对共享文件', request_id='gui-summary')
            state = dispatch_job(cfg, 'codex', job['id'], [
                {'worker': 'trae', 'title': '检查产物', 'body': '只读核对', 'result_to': 'codex'}])
            checkpoint_job(cfg, 'workbuddy', job['id'], '下一步：等待Trae回执')
            ui._refresh_jobs()
            rendered = ui.jobs_box.insert.call_args.args[1]
            self.assertIn('等待接手', rendered)
            self.assertIn('发起者：workbuddy', rendered)
            self.assertIn('组长：codex', rendered)
            self.assertIn('检查产物', rendered)
            self.assertIn('执行成员：trae', rendered)
            self.assertIn('位置：待办信箱', rendered)
            self.assertIn('状态：待执行', rendered)
            self.assertIn('待评审：0', rendered)
            self.assertIn('下一步：等待Trae回执', rendered)
            self.assertNotIn('"schema_version"', rendered)
            ui._refresh_jobs(technical=True)
            details = json.loads(ui.jobs_box.insert.call_args.args[1])
            self.assertEqual(details[0]['id'], state['id'])
            self.assertIn('goal_message', details[0])

    def test_external_watchtower_events_refresh_jobs_without_repeating_log(self):
        from agentbus.jobs import start_job, dispatch_job
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = self._member_change_ui(cfg)
            ui.jobs_box = MagicMock()
            ui._refresh_handoff = lambda: None
            ui._refresh_esc = lambda: None
            state = start_job(cfg, 'workbuddy', '外部CLI更新可见', request_id='external-gui')
            event = {'total': 1, 'bad': [], 'pending': {}}
            ui._handle_event('watchtower_update', event)
            self.assertIsNotNone(ui.jobs_box.insert.call_args, 'mailbox events must refresh jobs panel')
            first = ui.jobs_box.insert.call_args.args[1]
            self.assertIn(state['id'], first)
            dispatch_job(cfg, 'codex', state['id'], [
                {'worker': 'trae', 'title': '新派发步骤', 'body': '验收结果', 'result_to': 'codex'}])
            ui._handle_event('watchtower_update', event)
            second = ui.jobs_box.insert.call_args.args[1]
            self.assertIn('新派发步骤', second)
            calls = ui.jobs_box.insert.call_count
            ui._handle_event('watchtower_update', event)
            self.assertEqual(ui.jobs_box.insert.call_count, calls)
            self.assertEqual(len(ui.entries), 1)

    def test_external_cli_goal_reaches_monitor_and_human_jobs_panel(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = self._member_change_ui(cfg)
            ui.jobs_box = MagicMock()
            ui._refresh_handoff = lambda: None
            ui._refresh_esc = lambda: None
            with patch('agentbus.gui.Watchtower.notify'):
                ui._start_watchtower(cfg)
                ui.q.get(timeout=3)  # Empty initial scan before the external write.
                try:
                    arguments = ['team', 'start', '--workspace', temp, '--caller', 'workbuddy',
                                 '--goal', '外部命令进度可见', '--request-id', 'gui-real-cli']
                    command = 'from agentbus.cli import main; raise SystemExit(main(' + repr(arguments) + '))'
                    completed = subprocess.run([sys.executable, '-c', command],
                        cwd=str(Path(__file__).resolve().parent.parent), capture_output=True,
                        encoding='utf-8', timeout=10)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    job = json.loads(completed.stdout)
                    kind, event = ui.q.get(timeout=3)
                    self.assertEqual(kind, 'watchtower_update')
                    ui._handle_event(kind, event)
                    rendered = ui.jobs_box.insert.call_args.args[1]
                    self.assertIn(job['id'], rendered)
                    self.assertIn('外部命令进度可见', rendered)
                    self.assertIn('发起者：workbuddy', rendered)
                finally:
                    ui._stop_watchtower()

    def test_external_cli_checkpoint_refreshes_jobs_without_changing_inbox(self):
        import subprocess
        from agentbus.jobs import start_job
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            job = start_job(cfg, 'workbuddy', '续接记录自动可见', request_id='checkpoint-event')
            ui = self._member_change_ui(cfg)
            ui.jobs_box = MagicMock()
            ui._refresh_handoff = lambda: None
            ui._refresh_esc = lambda: None
            with patch('agentbus.gui.Watchtower.notify') as notify:
                ui._start_watchtower(cfg)
                kind, initial = ui.q.get(timeout=3)
                ui._handle_event(kind, initial)
                baseline = initial['total']
                notify.reset_mock()
                try:
                    arguments = ['team', 'checkpoint', '--workspace', temp,
                        '--caller', 'workbuddy', '--job-id', job['id'], '--note', '下轮优先复核配置']
                    command = 'from agentbus.cli import main; raise SystemExit(main(' + repr(arguments) + '))'
                    completed = subprocess.run([sys.executable, '-c', command],
                        cwd=str(Path(__file__).resolve().parent.parent), capture_output=True,
                        encoding='utf-8', timeout=10)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    try:
                        kind, event = ui.q.get(timeout=4)
                    except queue.Empty:
                        self.fail('checkpoint directory event must refresh the jobs panel')
                    self.assertEqual(kind, 'watchtower_update')
                    self.assertEqual(event['total'], baseline)
                    ui._handle_event(kind, event)
                    rendered = ui.jobs_box.insert.call_args.args[1]
                    self.assertIn('下轮优先复核配置', rendered)
                    notify.assert_not_called()  # Progress is not a new task notification.
                finally:
                    ui._stop_watchtower()

    def test_automatic_jobs_refresh_ignores_unsaved_controls_without_modal_prompt(self):
        from agentbus.jobs import start_job
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = self._member_change_ui(cfg)
            ui.jobs_box = MagicMock()
            ui.director_var.set('trae')  # Draft edit; saved coordinator stays codex.
            state = start_job(cfg, 'workbuddy', '保留已保存小组', request_id='silent-ui')
            with patch('agentbus.gui.messagebox.showwarning') as warning:
                ui._refresh_jobs(silent=True)
            warning.assert_not_called()
            rendered = ui.jobs_box.insert.call_args.args[1]
            self.assertIn(state['id'], rendered)
            self.assertIn('组长：codex', rendered)

    def test_jobs_summary_keeps_review_count_blocker_and_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            job = {'id': 'j-summary', 'goal': '评审进度', 'caller': 'workbuddy',
                   'coordinator': 'codex', 'state': 'awaiting_summary', 'tasks': [],
                   'pending_reviews': ['r-1', 'r-2'],
                   'reason': 'associated peer review is unfinished',
                   'checkpoints': [{'caller': 'trae', 'at': 'earlier', 'note': '已过时'},
                                   {'caller': 'zcode', 'at': 'now', 'note': '下一步检查回执'}]}
            text = gui.jobs_summary(cfg, [job])
            self.assertIn('待评审：2', text)
            self.assertIn('同伴评审尚未完成', text)
            self.assertIn('zcode · now', text)
            self.assertIn('下一步检查回执', text)
            self.assertNotIn('已过时', text)

    def test_progress_watcher_open_failure_is_reported_as_monitor_error(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = self._member_change_ui(cfg)
            watchtower = gui.Watchtower(cfg)
            with patch.object(watchtower, 'event_watchers', side_effect=OSError('cannot observe jobs')):
                try:
                    ui._watch_loop(watchtower, threading.Event())
                except OSError:
                    self.fail('native subscription failures must be reported to the GUI')
            kind, event = ui.q.get_nowait()
            self.assertEqual(kind, 'watchtower_error')
            self.assertIn('cannot observe jobs', event['text'])

    def test_jobs_panel_explains_opencode_records_and_stale_ownership(self):
        from agentbus.jobs import start_job, dispatch_job
        from agentbus.bus import Bus
        import os
        import time
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            cfg.data['workers']['opencode'].update(enabled=True, executor_enabled=True)
            from agentbus.config import save
            save(cfg)
            ui = self._member_change_ui(cfg)
            ui.jobs_box = MagicMock()
            state = start_job(cfg, 'workbuddy', '接口记录入口', request_id='opencode-visible')
            state = dispatch_job(cfg, 'codex', state['id'], [
                {'worker': 'opencode', 'title': '接口执行', 'body': '只读核对', 'result_to': 'codex'}])
            bus = Bus(cfg.workspace / '.agentbus')
            path = bus.pending('opencode')[0]
            message = bus.claim('opencode', path)
            active = bus.dirs('opencode')['processing'] / (message.id + '.json')
            old = time.time() - int(cfg.gates.get('heartbeat_timeout_s', 600)) - 5
            os.utime(active, (old, old))
            ui._refresh_jobs()
            rendered = ui.jobs_box.insert.call_args.args[1]
            self.assertIn('桌面文件交接', rendered)
            self.assertIn('独立接口为可选路线', rendered)
            self.assertNotIn('不进入桌面聊天', rendered)
            self.assertIn(str(cfg.workspace / '.agentbus/agents/opencode'), rendered)
            self.assertIn('心跳过期', rendered)
            self.assertIn('不能抢占', rendered)
            self.assertTrue(active.is_file())

    def _member_change_ui(self, cfg):
        """Real saved configuration and monitor, without a native dialog."""
        ui = Launcher.__new__(Launcher)
        ui.cfg = cfg
        ui.ws_var = Value(str(cfg.workspace))
        ui.worker_vars = {name: Value(worker.get('enabled', True))
                          for name, worker in cfg.data['workers'].items()}
        ui.workers_frame = Frame()
        ui.director_var = Value(cfg.director)
        ui.director_box = Box()
        ui.goal_agent = Value('codex')
        ui.goal_agent_box = Box()
        ui.workflow_agent = Value('codex')
        ui.workflow_agent_box = Box()
        ui.q = queue.Queue()
        ui.watchtower = ui.watch_thread = ui.watch_stop = None
        ui.watch_status_var = Value('')
        ui.entries = []
        ui.log = lambda text, tag='info': ui.entries.append((text, tag))
        return ui

    # Source: GUI partial-save review by Codex, 2026-10-02. The file conflict
    # must not conceal a member already committed to disk.
    def test_added_member_refreshes_controls_and_monitor_despite_workflow_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            protected = cfg.workspace / 'AGENTBUS.md'
            protected.write_text('user owned instructions', encoding='utf-8')
            ui = self._member_change_ui(cfg)
            register = getattr(ui, '_register_member', None)
            self.assertTrue(callable(register), 'member registration needs a testable save handler')
            with patch('agentbus.gui.tk.BooleanVar', side_effect=lambda **kw: Value(kw['value'])), \
                 patch('agentbus.gui.ttk.Checkbutton', side_effect=lambda *a, **kw: Checkbutton(**kw)), \
                 patch('agentbus.gui.Watchtower.notify'), \
                 patch('agentbus.gui.save_last_workspace'), \
                 patch('agentbus.gui.messagebox.showwarning') as warning:
                ui._start_watchtower(cfg)
                previous = ui.watch_thread
                try:
                    self.assertTrue(register('cursor', 'Cursor', None))
                    self.assertIn('cursor', load(cfg.workspace).enabled_workers())
                    self.assertIn('cursor', ui.worker_vars)
                    self.assertIn('cursor', ui.goal_agent_box.values)
                    self.assertIn('cursor', ui.watchtower.watched_agents())
                    self.assertFalse(previous.is_alive())
                    self.assertEqual(protected.read_text(encoding='utf-8'), 'user owned instructions')
                    title, body = warning.call_args.args[:2]
                    self.assertIn('已保存', title)
                    self.assertIn('工作流', body)
                    self.assertNotIn('未添加', title)
                finally:
                    ui._stop_watchtower()

    def test_import_refreshes_saved_group_and_monitor_despite_workflow_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            protected = cfg.workspace / 'AGENTBUS.md'
            protected.write_text('user owned instructions', encoding='utf-8')
            template = cfg.workspace / 'template.json'
            template.write_text(json.dumps({'format': 'agentbus.team-template', 'schema_version': 1,
                'coordinator': 'cursor', 'members': [{'id': 'cursor', 'label': 'Cursor'},
                    {'id': 'codex', 'label': 'Codex'}]}), encoding='utf-8')
            ui = self._member_change_ui(cfg)
            with patch('agentbus.gui.tk.BooleanVar', side_effect=lambda **kw: Value(kw['value'])), \
                 patch('agentbus.gui.ttk.Checkbutton', side_effect=lambda *a, **kw: Checkbutton(**kw)), \
                 patch('agentbus.gui.Watchtower.notify'), \
                 patch('agentbus.gui.save_last_workspace'), \
                 patch('agentbus.gui.filedialog.askopenfilename', return_value=str(template)), \
                 patch('agentbus.gui.messagebox.askyesno', return_value=True), \
                 patch('agentbus.gui.messagebox.showwarning') as warning:
                ui._start_watchtower(cfg)
                previous = ui.watch_thread
                try:
                    ui._import_team()
                    fresh = load(cfg.workspace)
                    self.assertEqual(fresh.director, 'cursor')
                    self.assertEqual(ui.director_var.get(), 'cursor')
                    self.assertEqual(set(ui.director_box.values), {'codex', 'cursor'})
                    self.assertEqual(set(ui.watchtower.watched_agents()), {'codex', 'cursor'})
                    self.assertFalse(previous.is_alive())
                    self.assertEqual(protected.read_text(encoding='utf-8'), 'user owned instructions')
                    title, body = warning.call_args.args[:2]
                    self.assertIn('已保存', title)
                    self.assertIn('工作流', body)
                    self.assertNotIn('未导入', title)
                finally:
                    ui._stop_watchtower()

    def test_added_member_prepares_workflow_and_refreshes_monitor(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = self._member_change_ui(cfg)
            register = getattr(ui, '_register_member', None)
            self.assertTrue(callable(register), 'member registration needs a testable save handler')
            with patch('agentbus.gui.tk.BooleanVar', side_effect=lambda **kw: Value(kw['value'])), \
                 patch('agentbus.gui.ttk.Checkbutton', side_effect=lambda *a, **kw: Checkbutton(**kw)), \
                 patch('agentbus.gui.Watchtower.notify'), \
                 patch('agentbus.gui.save_last_workspace'), \
                 patch('agentbus.gui.messagebox.showwarning') as warning:
                try:
                    self.assertTrue(register('cursor', 'Cursor', None))
                    self.assertIn('cursor', ui.watchtower.watched_agents())
                    self.assertEqual(ui.director_var.get(), cfg.director)
                    self.assertTrue((cfg.workspace / '.agentbus/workflows/cursor/SKILL.md').is_file())
                    warning.assert_not_called()
                finally:
                    ui._stop_watchtower()

    def test_import_prepares_workflow_and_restarts_monitor_for_new_coordinator(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            template = cfg.workspace / 'template.json'
            template.write_text(json.dumps({'format': 'agentbus.team-template', 'schema_version': 1,
                'coordinator': 'cursor', 'members': [{'id': 'cursor', 'label': 'Cursor'},
                    {'id': 'codex', 'label': 'Codex'}]}), encoding='utf-8')
            ui = self._member_change_ui(cfg)
            with patch('agentbus.gui.tk.BooleanVar', side_effect=lambda **kw: Value(kw['value'])), \
                 patch('agentbus.gui.ttk.Checkbutton', side_effect=lambda *a, **kw: Checkbutton(**kw)), \
                 patch('agentbus.gui.Watchtower.notify'), \
                 patch('agentbus.gui.save_last_workspace'), \
                 patch('agentbus.gui.filedialog.askopenfilename', return_value=str(template)), \
                 patch('agentbus.gui.messagebox.askyesno', return_value=True), \
                 patch('agentbus.gui.messagebox.showwarning') as warning:
                ui._start_watchtower(cfg)
                previous = ui.watch_thread
                try:
                    ui._import_team()
                    self.assertEqual(ui.director_var.get(), 'cursor')
                    self.assertEqual(ui.watchtower.cfg.director, 'cursor')
                    self.assertFalse(previous.is_alive())
                    self.assertTrue((cfg.workspace / '.agentbus/workflows/cursor/SKILL.md').is_file())
                    warning.assert_not_called()
                finally:
                    ui._stop_watchtower()

    def test_first_project_import_creates_exact_template_group_without_default_save(self):
        from agentbus.config import DEFAULTS
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp) / 'new-project'
            cfg = Config(ws / '.agentbus/config.json', ws.resolve(), json.loads(json.dumps(DEFAULTS)))
            ui = self._member_change_ui(cfg)
            ui.cfg = None
            template = Path(temp) / 'team-template.json'
            template.write_text(json.dumps({'format': 'agentbus.team-template', 'schema_version': 1,
                'coordinator': 'cursor', 'members': [{'id': 'cursor', 'label': 'Cursor'},
                    {'id': 'workbuddy', 'label': 'WorkBuddy'},
                    {'id': 'codex', 'label': 'Codex'},
                    {'id': 'opencode', 'label': 'OpenCode'}]}), encoding='utf-8')
            with patch('agentbus.gui.tk.BooleanVar', side_effect=lambda **kw: Value(kw['value'])), \
                 patch('agentbus.gui.ttk.Checkbutton', side_effect=lambda *a, **kw: Checkbutton(**kw)), \
                 patch('agentbus.gui.Watchtower.notify'), \
                 patch('agentbus.gui.save_last_workspace'), \
                 patch('agentbus.gui.detect_group_agents') as detector, \
                 patch('agentbus.gui.filedialog.askopenfilename', return_value=str(template)), \
                 patch('agentbus.gui.messagebox.askyesno', return_value=True) as confirm, \
                 patch('agentbus.gui.messagebox.showwarning') as warning:
                try:
                    ui._import_team()
                    self.assertTrue(cfg.path.is_file(), 'first import must not require saving a default group')
                    saved = load(ws)
                    self.assertEqual(saved.director, 'cursor')
                    self.assertEqual(set(saved.enabled_workers()), {'cursor', 'workbuddy', 'codex', 'opencode'})
                    self.assertEqual(set(ui.watchtower.watched_agents()), {'cursor', 'workbuddy', 'codex', 'opencode'})
                    for name in saved.enabled_workers():
                        self.assertFalse(saved.worker(name)['can_spawn'], name)
                        self.assertTrue(saved.worker(name)['human_in_loop'], name)
                    self.assertTrue((ws / 'AGENTBUS.md').is_file())
                    self.assertIn('cursor', confirm.call_args.args[1])
                    detector.assert_not_called()
                    warning.assert_not_called()
                finally:
                    ui._stop_watchtower()

    def test_invalid_first_import_does_not_create_configuration_or_runtime_folders(self):
        from agentbus.config import DEFAULTS
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp) / 'new-project'
            cfg = Config(ws / '.agentbus/config.json', ws.resolve(), json.loads(json.dumps(DEFAULTS)))
            ui = self._member_change_ui(cfg)
            ui.cfg = None
            template = Path(temp) / 'bad-template.json'
            template.write_text(json.dumps({'format': 'agentbus.team-template', 'schema_version': 1,
                'coordinator': 'cursor', 'members': [{'id': 'cursor', 'label': 'Cursor',
                                                    'command': ['untrusted-executable']}]}), encoding='utf-8')
            with patch('agentbus.gui.filedialog.askopenfilename', return_value=str(template)) as pick, \
                 patch('agentbus.gui.messagebox.askyesno') as confirm, \
                 patch('agentbus.gui.messagebox.showwarning') as warning:
                ui._import_team()
            pick.assert_called_once()
            self.assertEqual(warning.call_args.args[0], '未导入')
            confirm.assert_not_called()
            self.assertFalse(ws.exists())
            self.assertIsNone(ui.watchtower)

    def test_normal_save_refreshes_monitor_and_reports_workflow_conflict_honestly(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            protected = cfg.workspace / 'AGENTBUS.md'
            protected.write_text('user owned instructions', encoding='utf-8')
            ui = self._member_change_ui(cfg)
            ui.worker_vars = {name: Value(name == 'codex') for name in cfg.data['workers']}
            with patch('agentbus.gui.tk.BooleanVar', side_effect=lambda **kw: Value(kw['value'])), \
                 patch('agentbus.gui.ttk.Checkbutton', side_effect=lambda *a, **kw: Checkbutton(**kw)), \
                 patch('agentbus.gui.Watchtower.notify'), \
                 patch('agentbus.gui.save_last_workspace'), \
                 patch('agentbus.gui.messagebox.showwarning') as warning, \
                 patch('agentbus.gui.messagebox.showinfo') as success:
                ui._start_watchtower(cfg)
                previous = ui.watch_thread
                try:
                    ui._save_group()
                    self.assertEqual(set(load(cfg.workspace).enabled_workers()), {'codex'})
                    self.assertEqual(set(ui.watchtower.watched_agents()), {'codex'})
                    self.assertFalse(previous.is_alive())
                    self.assertEqual(protected.read_text(encoding='utf-8'), 'user owned instructions')
                    self.assertIn('已保存', warning.call_args.args[0])
                    success.assert_not_called()
                finally:
                    ui._stop_watchtower()

    def test_invalid_added_member_does_not_change_saved_group(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = self._member_change_ui(cfg)
            before = cfg.path.read_bytes()
            with patch('agentbus.gui.messagebox.showwarning') as warning:
                self.assertFalse(ui._register_member('../outside', 'invalid', None))
            self.assertEqual(cfg.path.read_bytes(), before)
            self.assertIsNone(ui.watchtower)
            self.assertEqual(warning.call_args.args[0], '未添加')

    def test_many_members_scroll_without_hiding_goal_or_tabs(self):
        root = gui.tk.Tk()
        try:
            root.tk.call('tk', 'scaling', 2.0)
            callback_errors = []
            root.report_callback_exception = lambda *args: callback_errors.append(args)
            with tempfile.TemporaryDirectory() as temp, \
                 patch('agentbus.gui.initial_workspace', return_value=temp), \
                 patch.object(gui.Launcher, '_auto_detect'):
                ui = Launcher(root)
                cfg = load(Path(temp), create=True)
                for index in range(60):
                    cfg.data['workers'][f'member-{index:02}'] = {
                        'kind': 'gui', 'enabled': True, 'executor_enabled': True,
                        'can_spawn': False, 'human_in_loop': True}
                # The delayed startup reload resets the viewport to the top.
                # Test the layout after loading the fixture once.
                cfg = save(cfg)
                ui._apply_cfg_to_ui(cfg)
                ui._show_page('team')
                root.update_idletasks()
                root.update()
                canvas = getattr(ui, 'workers_canvas', None)
                self.assertIsNotNone(canvas, 'member picker must use a bounded scrolling area')
                self.assertLess(canvas.winfo_height(), ui.pages['team'].winfo_height())
                self.assertLessEqual(canvas.winfo_rooty() + canvas.winfo_height(),
                                     root.winfo_rooty() + root.winfo_height())
                view = canvas.yview()
                self.assertLess(
                    view[1], 1,
                    'member viewport should scroll: '
                    f'view={view}, canvas_height={canvas.winfo_height()}, '
                    f'content_height={ui.workers_frame.winfo_reqheight()}, '
                    f'scrollregion={canvas.cget("scrollregion")}, '
                    f'scaling={root.tk.call("tk", "scaling")}, '
                    f'children={len(ui.workers_frame.winfo_children())}, '
                    f'workers={len(cfg.data.get("workers", {}))}, '
                    f'callback_errors={len(callback_errors)}',
                )
                self.assertGreater(ui.tabs.winfo_height(), 80)
                canvas.yview_moveto(1)
                root.update()
                last = ui.workers_frame.winfo_children()[-1]
                self.assertLessEqual(last.winfo_rooty() + last.winfo_height(),
                                     canvas.winfo_rooty() + canvas.winfo_height(),
                                     f'view={canvas.yview()}, region={canvas.cget("scrollregion")}, '
                                     f'window={root.winfo_geometry()}')
                self.assertGreaterEqual(last.winfo_rooty(), canvas.winfo_rooty())
                ui._show_page('home')
                root.update()
                self.assertTrue(ui.start_btn.winfo_ismapped())
                self.assertLessEqual(ui.start_btn.winfo_rooty() + ui.start_btn.winfo_height(),
                                     root.winfo_rooty() + root.winfo_height())
                self.assertEqual(callback_errors, [])
        finally:
            self._destroy_fixture_root(root)

    # Source: actual default-window clipping, Codex Computer Use, 2026-09-30.
    def test_default_window_keeps_tab_actions_and_status_visible(self):
        root = gui.tk.Tk()
        try:
            # Frozen runw's Windows DPI context differs from python.exe.
            # Exercise the larger text metrics observed in the real EXE.
            root.tk.call("tk", "scaling", 2.0)
            callback_errors = []
            root.report_callback_exception = lambda *args: callback_errors.append(args)
            with tempfile.TemporaryDirectory() as temp, \
                 patch('agentbus.gui.initial_workspace', return_value=temp), \
                 patch.object(gui.Launcher, '_auto_detect'):
                ui = Launcher(root)
                cfg = load(Path(temp), create=True)
                ui._apply_cfg_to_ui(cfg)
                root.update()

                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)

                notebook = next(w for w in descendants(root)
                                if isinstance(w, gui.ttk.Notebook))
                # Source: enterprise nesting, Codex/product review, 2026-10-04.
                # Visit every nested page explicitly. Hidden subpage buttons
                # must never pass a geometry check just because they are absent.
                required = {'复制接入指令', '刷新接入记录', '复制首条任务指令',
                            '刷新', '生成配置', '复制', '自检 MCP 是否可用',
                            '检查项目工作流', '复制续接指令', '刷新任务进度',
                            '检查安装', '检查当前团队', '保存诊断', '恢复中断任务',
                            '安装 / 修复工作流', '预览移除', '移除已安装工作流',
                            '检测本机入口', '导入成员模板', '导出分发包', '添加 Agent'}
                seen = set()

                def inspect_visible(widget):
                    if isinstance(widget, gui.ttk.Notebook):
                        for tab_id in widget.tabs():
                            widget.select(tab_id)
                            root.update()
                            inspect_visible(root.nametowidget(tab_id))
                        return
                    if widget.winfo_class() == 'TButton':
                        label = widget.cget('text')
                        seen.add(label)
                        # A local scroll viewport is an intentional part of
                        # the workbench. Prove reachability rather than demand
                        # that all maintenance controls fit simultaneously.
                        canvas = None
                        parent = widget.master
                        while parent is not root:
                            if isinstance(parent, gui.tk.Canvas):
                                canvas = parent
                                break
                            parent = parent.master
                        if canvas is not None:
                            reached = False
                            for step in range(41):
                                canvas.yview_moveto(step / 40)
                                root.update()
                                reached = (widget.winfo_rooty() >= canvas.winfo_rooty()
                                    and widget.winfo_rooty() + widget.winfo_height()
                                        <= canvas.winfo_rooty() + canvas.winfo_height())
                                if reached:
                                    break
                            self.assertTrue(reached, label)
                        self.assertTrue(widget.winfo_ismapped(),
                            f'{label}: window={root.winfo_geometry()}, '
                            f'screen={root.winfo_screenwidth()}x{root.winfo_screenheight()}, '
                            f'parent={widget.master.winfo_geometry()}, '
                            f'font={root.tk.call("font", "actual", gui.FONT)}')
                        self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight(), label)
                        self.assertLessEqual(widget.winfo_y() + widget.winfo_height(),
                                             widget.master.winfo_height(), label)
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                             root.winfo_rooty() + root.winfo_height(), label)
                        self.assertGreaterEqual(widget.winfo_rootx(), root.winfo_rootx(), label)
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                             root.winfo_rootx() + root.winfo_width(), label)
                    for child in widget.winfo_children():
                        inspect_visible(child)

                self.assertEqual([notebook.tab(tab_id, 'text').strip()
                                  for tab_id in notebook.tabs()],
                                 ['工作台', '团队', '任务', '接入与技能', '设置', '运行记录'])
                inspect_visible(notebook)
                self.assertTrue(required.issubset(seen), f'Missing nested actions: {required - seen}')
                status = next(w for w in descendants(root)
                              if w.winfo_class() == "TLabel"
                              and str(w.cget("textvariable")) == str(ui.status_var))
                self.assertTrue(status.winfo_ismapped())
                self.assertLessEqual(status.winfo_rooty() + status.winfo_height(),
                                     root.winfo_rooty() + root.winfo_height())
                self.assertEqual(callback_errors, [])
        finally:
            self._destroy_fixture_root(root)

    def test_internal_usage_guide_keeps_copy_close_and_read_only_text_visible(self):
        root = gui.tk.Tk()
        try:
            root.tk.call('tk', 'scaling', 2.0)
            with tempfile.TemporaryDirectory() as temp, \
                 patch('agentbus.gui.initial_workspace', return_value=temp), \
                 patch('agentbus.gui.detect_group_agents', return_value={}), \
                 patch('agentbus.gui.read_usage_guide', return_value='隔离指南内容\n' * 100):
                ui = Launcher(root)
                ui._show_usage_guide()
                root.update()
                dialog = next(child for child in root.winfo_children()
                              if isinstance(child, gui.tk.Toplevel))

                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)

                actions = [widget for widget in descendants(dialog)
                           if widget.winfo_class() == 'TButton']
                self.assertEqual({button.cget('text') for button in actions}, {'关闭', '复制全文'})
                for button in actions:
                    self.assertTrue(button.winfo_ismapped(), button.cget('text'))
                    self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
                    self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                         dialog.winfo_rooty() + dialog.winfo_height())
                    self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                         dialog.winfo_rootx() + dialog.winfo_width())
                text = next(widget for widget in descendants(dialog)
                            if widget.winfo_class() == 'Text')
                self.assertEqual(text.cget('state'), 'disabled')
                self.assertGreaterEqual(text.winfo_height(), 200)
                self.assertIn('隔离指南内容', text.get('1.0', 'end'))
        finally:
            self._destroy_fixture_root(root)

    # Source: Trae GUI peer review and Codex verification, 2026-09-30.
    def test_explicit_save_updates_detected_path_without_enabling_auto_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            cfg.data["workers"]["codex"].update(
                command=["old-entry"], can_spawn=False,
                sandbox="read-only", human_in_loop=True)
            # User settings must be on disk before the leased save re-reads
            # them. A detached in-memory fixture is not a saved configuration.
            from agentbus.config import save
            save(cfg)
            ui = Launcher.__new__(Launcher)
            ui.ws_var = Value(temp)
            ui.cfg = cfg
            ui.probes = {"codex": object()}
            ui.worker_vars = {name: Value(name == "codex")
                              for name in cfg.data["workers"]}
            ui.director_var = Value("codex")
            detected = {"codex": {"command": ["new-entry"],
                                   "can_spawn": True, "human_in_loop": False,
                                   "sandbox": "workspace-write"}}
            with patch("agentbus.gui.to_config_patch", return_value=detected):
                result = ui._collect_ui_config()
            worker = result["workers"]["codex"]
            self.assertEqual(worker["command"], ["new-entry"])
            self.assertFalse(worker["can_spawn"])
            self.assertTrue(worker["human_in_loop"])
            self.assertEqual(worker["sandbox"], "read-only")

    def test_copy_handoff_copies_only_first_self_contained_prompt(self):
        ui = Launcher.__new__(Launcher)
        ui.root = MagicMock()
        ui.log = lambda *a, **kw: None
        ui._workflow_cfg = lambda: object()
        ui._refresh_handoff = lambda: None
        ui._manual_prompts = ["first task", "second task"]
        ui._handoff_text = "first task\nsecond task"
        ui._copy_handoff()
        ui.root.clipboard_append.assert_called_once_with("first task")

    def test_startup_probe_does_not_write_config_or_start_monitor(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            before = cfg.path.read_bytes()
            ui = Launcher.__new__(Launcher)
            ui.ws_var = Value(temp)
            ui.cfg = None
            ui._apply_cfg_to_ui = lambda cfg: None
            ui._gen_mcp = lambda: None
            ui.log = lambda *a, **kw: None
            ui.detect_label = MagicMock()
            ui._start_watchtower = MagicMock()
            with patch("agentbus.gui.detect_group_agents", return_value={}), \
                 patch("agentbus.gui.save_last_workspace"):
                ui._auto_detect()
            self.assertEqual(cfg.path.read_bytes(), before)
            ui._start_watchtower.assert_not_called()

    def test_refresh_probe_does_not_replace_selected_config(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            before = cfg.path.read_bytes()
            ui = Launcher.__new__(Launcher)
            ui.cfg = cfg
            ui._apply_cfg_to_ui = lambda cfg: None
            ui.log = lambda *a, **kw: None
            ui.detect_label = MagicMock()
            probe = SimpleNamespace(can_spawn=False, candidate_adapter=True,
                                    installed=True)
            with patch("agentbus.gui.detect_group_agents", return_value={"workbuddy": probe}), \
                 patch("agentbus.gui.to_config_patch", return_value={
                     "workbuddy": {"kind": "acp", "command": ["new-candidate"],
                                   "can_spawn": False}}):
                ui._refresh_product_probes()
            self.assertEqual(cfg.path.read_bytes(), before)

    def test_handoff_exposes_blocked_task_without_gui_terminal_command(self):
        from agentbus.bus import Bus
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            cfg.data["workers"]["workbuddy"].update(can_spawn=False, human_in_loop=True)
            bus = Bus(cfg.workspace / ".agentbus")
            bus.init()
            task = bus.make("codex", "workbuddy", "task", "Blocked visibility",
                            "Read the assigned file", result_to="trae")
            bus.post(task)
            message = bus.claim("workbuddy", bus.pending("workbuddy")[0])
            bus.block("workbuddy", message, "route clarification", ticket=receipt_ticket(message))
            ui = Launcher.__new__(Launcher)
            ui.cfg, ui.bus = cfg, bus
            ui.handoff_box = MagicMock()
            ui._refresh_handoff()
            text = "".join(str(c.args[1]) for c in ui.handoff_box.insert.call_args_list)
            self.assertIn(task.id, text)
            self.assertIn("trae", text)
            self.assertNotIn("PowerShell", text)
            self.assertNotIn("--body", text)

    # Source: WorkBuddy peer review, 2026-09-30. Blocked work needs human
    # visibility even when the member has a configured automatic entrypoint.
    def test_handoff_exposes_blocked_spawnable_member_but_not_result_as_task(self):
        from agentbus.bus import Bus
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            cfg.data["workers"]["codex"].update(can_spawn=True)
            cfg.data["workers"]["workbuddy"].update(can_spawn=False, human_in_loop=True)
            bus = Bus(cfg.workspace / ".agentbus")
            bus.init()
            task = bus.make("workbuddy", "codex", "task", "Spawnable blocked",
                            "Clarify before running", result_to="workbuddy")
            bus.post(task)
            message = bus.claim("codex", bus.pending("codex")[0])
            bus.block("codex", message, "clarification", ticket=receipt_ticket(message))
            receipt = bus.make("codex", "workbuddy", "result", "Receipt only",
                               "This is not an assignment", reply_to=task.id,
                               source_claim_epoch=message.claim_epoch,
                               source_claim_id=message.claim_id)
            bus.post(receipt)
            ui = Launcher.__new__(Launcher)
            ui.cfg, ui.bus = cfg, bus
            ui.handoff_box = MagicMock()
            ui._refresh_handoff()
            text = "".join(str(c.args[1]) for c in ui.handoff_box.insert.call_args_list)
            self.assertIn(task.id, text)
            self.assertNotIn(receipt.id, text)

    def test_copy_handoff_excludes_terminal_task_with_leftover_blocked_copy(self):
        # Source: independent Codex release review, 2026-10-04. A failed
        # cleanup after terminal commit must not dispatch completed work.
        from agentbus.bus import Bus
        original_unlink = Path.unlink
        for status in ('done', 'failed'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                cfg = load(Path(temp), create=True)
                cfg.data['workers']['workbuddy'].update(can_spawn=False, human_in_loop=True)
                bus = Bus(cfg.workspace / '.agentbus')
                bus.init()
                task = bus.make('codex', 'workbuddy', 'task', 'Completed blocked copy',
                                'Never repeat this work', result_to='codex')
                bus.post(task)
                active = bus.claim('workbuddy', bus.pending('workbuddy')[0])
                blocked = bus.block('workbuddy', active, 'clarification', ticket=receipt_ticket(active))

                def fail_blocked_unlink(path, *args, **kwargs):
                    if path == blocked:
                        raise PermissionError('injected cleanup failure')
                    return original_unlink(path, *args, **kwargs)

                with patch.object(Path, 'unlink', fail_blocked_unlink):
                    with self.assertRaises(PermissionError):
                        bus.finish('workbuddy', active, status=status, ticket=receipt_ticket(active))
                terminal = bus.dirs('workbuddy')['done'] / blocked.name
                before = {path: path.read_bytes() for path in (blocked, terminal)}
                ui = Launcher.__new__(Launcher)
                ui.cfg, ui.bus, ui.root = cfg, bus, MagicMock()
                ui.handoff_box, ui.log = MagicMock(), MagicMock()
                ui._workflow_cfg = MagicMock(return_value=cfg)
                with patch('agentbus.gui.messagebox.showinfo'):
                    ui._copy_handoff()
                ui.root.clipboard_append.assert_not_called()
                self.assertEqual(ui._manual_prompts, [])
                for path, blob in before.items():
                    self.assertEqual(path.read_bytes(), blob)

    def test_handoff_keeps_blocked_task_visible_when_terminal_record_is_invalid(self):
        from agentbus.bus import Bus
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            cfg.data['workers']['workbuddy'].update(can_spawn=False, human_in_loop=True)
            bus = Bus(cfg.workspace / '.agentbus')
            bus.init()
            task = bus.make('codex', 'workbuddy', 'task', 'Still blocked',
                            'Needs clarification', result_to='codex')
            bus.post(task)
            active = bus.claim('workbuddy', bus.pending('workbuddy')[0])
            bus.block('workbuddy', active, 'clarification', ticket=receipt_ticket(active))
            terminal = bus.dirs('workbuddy')['done'] / f'{task.id}.json'
            terminal.write_text('{invalid', encoding='utf-8')
            ui = Launcher.__new__(Launcher)
            ui.cfg, ui.bus, ui.handoff_box = cfg, bus, MagicMock()
            ui._refresh_handoff()
            self.assertEqual(len(ui._manual_prompts), 1)
            self.assertIn(task.id, ui._manual_prompts[0])
            self.assertEqual(terminal.read_text(encoding='utf-8'), '{invalid')

    def test_gui_goal_copies_workflow_without_starting_independent_model(self):
        ui = Launcher.__new__(Launcher)
        ui.worker = None
        ui.root = MagicMock()
        ui.goal_text = MagicMock()
        ui.goal_text.get.return_value = "Check the group"
        ui._collect_ui_config = MagicMock(return_value={})
        ui.cfg = SimpleNamespace(workspace=Path("project").resolve(),
                                 director="codex", data={"workers": {
                                     "codex": {"enabled": True}}})
        ui.ws_var = Value(str(ui.cfg.workspace))
        ui.worker_vars = {"codex": Value(True)}
        ui.director_var = Value("codex")
        ui.log = lambda *a, **kw: None
        ui.stop_flag = threading.Event()
        ui.start_btn = MagicMock()
        ui.stop_btn = MagicMock()
        ui.status_var = Value("")
        ui.approve_var = Value(False)
        with patch("agentbus.gui.threading.Thread") as worker, \
             patch("agentbus.gui.messagebox.showinfo"):
            ui._start()
        worker.assert_not_called()
        ui._collect_ui_config.assert_not_called()
        ui.root.clipboard_append.assert_called_once_with(
            "$agentbus-team Check the group。项目：" + str(ui.cfg.workspace) +
            "；当前成员身份：codex。先读本项目AGENTBUS.md并按工作流登记目标，组长按配置，不改权限。")

    # Source: ZCode independent GUI review, 2026-09-30.
    def test_copy_goal_requires_explicit_save_for_new_workspace(self):
        ui = Launcher.__new__(Launcher)
        ui.worker = None
        ui.root = MagicMock()
        ui.goal_text = MagicMock()
        ui.goal_text.get.return_value = "Read only"
        ui.cfg = None
        ui._collect_ui_config = MagicMock(return_value=None)
        with patch("agentbus.gui.messagebox.showwarning") as warning:
            ui._start()
        warning.assert_called_once()
        ui._collect_ui_config.assert_not_called()
        ui.root.clipboard_append.assert_not_called()

    def test_copy_goal_requires_explicit_save_for_changed_group(self):
        ui = Launcher.__new__(Launcher)
        ui.worker = None
        ui.root = MagicMock()
        ui.goal_text = MagicMock()
        ui.goal_text.get.return_value = "Read only"
        ui.cfg = SimpleNamespace(workspace=Path("project").resolve(),
                                 director="codex", data={"workers": {
                                     "codex": {"enabled": True}}})
        ui.ws_var = Value(str(ui.cfg.workspace))
        ui.worker_vars = {"codex": Value(False)}
        ui.director_var = Value("codex")
        ui.log = lambda *a, **kw: None
        ui._collect_ui_config = MagicMock(return_value={})
        with patch("agentbus.gui.messagebox.showwarning") as warning, \
             patch("agentbus.gui.messagebox.showinfo"):
            ui._start()
        warning.assert_called_once()
        ui._collect_ui_config.assert_not_called()
        ui.root.clipboard_append.assert_not_called()

    def test_long_goal_does_not_hide_plan_approval_buttons(self):
        root = gui.tk.Tk()
        try:
            with patch.object(gui.PlanDialog, "wait_window"):
                dialog = gui.PlanDialog(root, [{
                    "title": "Layout regression", "worker": "codex",
                    "body": "Read-only layout verification", "result_to": "codex",
                }], "长目标布局验证。" * 100)
            dialog.update()

            def buttons(widget):
                for child in widget.winfo_children():
                    if child.winfo_class() == "TButton":
                        yield child
                    yield from buttons(child)

            actions = list(buttons(dialog))
            self.assertEqual(len(actions), 2)
            self.assertGreaterEqual(dialog.detail.winfo_height(), 100)
            for action in actions:
                self.assertTrue(action.winfo_ismapped(), action.cget("text"))
                self.assertGreaterEqual(action.winfo_height(), action.winfo_reqheight())
                self.assertLessEqual(action.winfo_rooty() + action.winfo_height(),
                                     dialog.winfo_rooty() + dialog.winfo_height())
        finally:
            self._destroy_fixture_root(root)

    def test_plan_confirmation_is_opt_in_by_default(self):
        ui = Launcher.__new__(Launcher)
        ui.root = MagicMock()

        with patch.object(gui.ttk, "LabelFrame", return_value=MagicMock()), \
             patch.object(gui.ttk, "Frame", return_value=MagicMock()), \
             patch.object(gui.ttk, "Label", return_value=MagicMock()), \
             patch.object(gui.ttk, "Button", return_value=MagicMock()), \
             patch.object(gui.ttk, "Checkbutton", return_value=MagicMock()), \
             patch.object(gui.ttk, "Combobox", return_value=MagicMock()), \
             patch.object(gui.tk, "Text", return_value=MagicMock()), \
             patch.object(gui.tk, "StringVar", side_effect=lambda value: Value(value)), \
             patch.object(gui.tk, "BooleanVar", side_effect=lambda value: Value(value)):
            ui._build_goal()

        self.assertFalse(ui.approve_var.get())

    def test_any_selected_host_can_receive_goal_entry(self):
        ui = Launcher.__new__(Launcher)
        ui.worker = None
        ui.root = MagicMock(); ui.goal_text = MagicMock()
        ui.goal_text.get.return_value = '短目标'
        ui.cfg = SimpleNamespace(workspace=Path('project').resolve(),director='zcode',
            data={'workers':{'workbuddy':{'enabled':True},'zcode':{'enabled':True}}})
        ui.ws_var=Value(str(ui.cfg.workspace)); ui.director_var=Value('zcode')
        ui.worker_vars={'workbuddy':Value(True),'zcode':Value(True)}
        ui.goal_agent=Value('workbuddy'); ui.log=lambda *a: None
        with patch('agentbus.gui.messagebox.showinfo'):
            ui._start()
        prompt=ui.root.clipboard_append.call_args.args[0]
        self.assertIn('使用这个小组工作',prompt)
        self.assertIn('workbuddy',prompt)
        self.assertNotIn('$agentbus-team',prompt)
        self.assertEqual(ui.cfg.director,'zcode')

    def test_group_save_respects_another_config_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=load(temp,create=True)
            ui=Launcher.__new__(Launcher); ui.cfg=cfg; ui.probes={}
            ui.ws_var=Value(str(cfg.workspace)); ui.director_var=Value('codex')
            ui.worker_vars={n:Value(n=='codex') for n in cfg.data['workers']}
            from agentbus.lease import LeaseManager
            manager=LeaseManager(cfg.workspace/'.agentbus')
            lease=manager.acquire('.agentbus/config.json','peer',ttl_s=30)
            before=cfg.path.read_bytes()
            try:
                with patch('agentbus.gui.messagebox.showwarning'):
                    self.assertIsNone(ui._collect_ui_config())
                self.assertEqual(cfg.path.read_bytes(),before)
            finally:
                manager.release(lease)

    def test_new_gui_group_defaults_to_codex_and_keeps_detected_peers_unchecked(self):
        from agentbus.config import DEFAULTS

        data = json.loads(json.dumps(DEFAULTS))
        patch = {
            "workbuddy": {"enabled": True, "can_spawn": False,
                          "candidate_adapter": True, "kind": "acp",
                          "command": ["node", "codebuddy", "--acp"]},
            "trae": {"enabled": True, "can_spawn": False, "kind": "gui"},
        }

        result = gui.initial_gui_group(data, patch)

        self.assertEqual(result["director"], "codex")
        self.assertTrue(result["workers"]["codex"]["enabled"])
        self.assertTrue(result["workers"]["codex"]["executor_enabled"])
        self.assertFalse(result["workers"]["workbuddy"]["can_spawn"])
        self.assertEqual(result["workers"]["workbuddy"]["kind"], "acp")
        self.assertFalse(result["workers"]["workbuddy"]["enabled"])
        self.assertFalse(result["workers"]["trae"]["enabled"])
        self.assertFalse(result["workers"]["zcode"]["enabled"])

    def test_init_new_workspace_refreshes_local_agent_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            ui = Launcher.__new__(Launcher)
            ui.ws_var = Value(temp)
            ui.log = lambda *args, **kwargs: None
            ui._apply_cfg_to_ui = lambda cfg: None
            ui._refresh_product_probes = lambda: calls.append("probe")
            ui._gen_mcp = lambda: None
            ui._start_watchtower = lambda cfg: None
            calls = []

            with patch("agentbus.gui.save_last_workspace"):
                self.assertEqual(ui._init_ws(), 0)

            self.assertEqual(calls, ["probe"])
            with (Path(temp) / ".agentbus" / "config.json").open(
                    "r", encoding="utf-8") as f:
                config = json.load(f)
            self.assertEqual(config["director"], "codex")
            self.assertTrue(config["workers"]["codex"]["enabled"])
            self.assertFalse(config["workers"]["workbuddy"]["enabled"])

    def test_probe_refresh_updates_adapter_but_preserves_user_group_and_permission(self):
        data = {"workers": {
            "workbuddy": {"enabled": False, "executor_enabled": False,
                          "kind": "gui", "can_spawn": False,
                          "sandbox": "read-only", "timeout_s": 90},
        }}
        merged = merge_probe_patch(data, {"workbuddy": {
            "enabled": True, "executor_enabled": True, "kind": "acp",
            "can_spawn": False, "candidate_adapter": True,
            "command": ["node", "codebuddy", "--acp"],
            "candidate_command": ["node", "codebuddy", "--acp"],
            "timeout_s": 1800, "note": "ACP entrypoint found",
        }})
        worker = merged["workers"]["workbuddy"]
        self.assertEqual(worker["kind"], "acp")
        self.assertFalse(worker["can_spawn"])
        self.assertTrue(worker["candidate_adapter"])
        self.assertEqual(worker["command"], ["node", "codebuddy", "--acp"])
        self.assertFalse(worker["enabled"])
        self.assertFalse(worker["executor_enabled"])
        self.assertEqual(worker["sandbox"], "read-only")
        self.assertEqual(worker["timeout_s"], 90)

    def test_probe_refresh_preserves_explicit_manual_handoff_choice(self):
        data = {"workers": {"workbuddy": {"human_in_loop": True}}}
        merged = merge_probe_patch(data, {"workbuddy": {"human_in_loop": False}})
        self.assertIs(merged["workers"]["workbuddy"]["human_in_loop"], True)

    def test_group_picker_shows_disabled_agents_and_coordinator_uses_selected_members(self):
        ui = Launcher.__new__(Launcher)
        ui.director_box = Box()
        ui.director_var = Value()
        ui.workers_frame = Frame()
        ui.worker_vars = {}
        cfg = SimpleNamespace(
            director="workbuddy",
            data={"workers": {
                "codex": {"enabled": False, "can_spawn": True},
                "workbuddy": {"enabled": True, "can_spawn": False},
                "trae": {"enabled": True, "can_spawn": False},
            }},
            enabled_workers=lambda: {"workbuddy": {}, "trae": {}})
        with patch("agentbus.gui.ttk.Checkbutton", side_effect=lambda *a, **kw: Checkbutton(**kw)) as checkbutton, \
             patch("agentbus.gui.tk.BooleanVar", side_effect=lambda value=False: Value(value)):
            ui._apply_cfg_to_ui(cfg)
        self.assertEqual(ui.director_box.values, ["trae", "workbuddy"])
        self.assertEqual(ui.director_var.get(), "workbuddy")
        labels = [call.kwargs["text"] for call in checkbutton.call_args_list]
        self.assertEqual(len(labels), 3)
        self.assertTrue(any(label.startswith("codex") for label in labels))
        self.assertTrue(any(label.startswith("trae") for label in labels))
        self.assertTrue(any(label.startswith("workbuddy") for label in labels))
        self.assertFalse(any("已验收" in label for label in labels))
        self.assertTrue(any("任务需验收" in label for label in labels
                            if label.startswith("codex")))
        self.assertFalse(ui.worker_vars["codex"].get())

    def test_selected_group_members_are_enabled_and_can_receive_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp).resolve()
            cfg = load(ws, create=True)
            config_path = cfg.path
            data = cfg.data
            data["director"] = "workbuddy"
            ui = Launcher.__new__(Launcher)
            ui.ws_var = Value(str(ws))
            ui.cfg = Config(config_path, ws, data)
            ui.worker_vars = {
                name: Value(name in {"codex", "trae"})
                for name in data["workers"]
            }
            ui.director_var = Value("codex")
            with patch("agentbus.gui.messagebox.showerror") as showerror:
                result = ui._collect_ui_config()
            self.assertFalse(showerror.called, str(showerror.call_args))
            self.assertIsNotNone(result)
            self.assertTrue(result["workers"]["codex"]["enabled"])
            self.assertTrue(result["workers"]["codex"]["executor_enabled"])
            self.assertFalse(result["workers"]["workbuddy"]["enabled"])
            self.assertFalse(result["workers"]["workbuddy"]["executor_enabled"])
            self.assertTrue(result["workers"]["trae"]["enabled"])
            self.assertEqual(result["director"], "codex")

    def test_coordinator_must_be_a_selected_group_member(self):
        with tempfile.TemporaryDirectory() as temp:
            ws = Path(temp).resolve()
            cfg = load(ws, create=True)
            ui = Launcher.__new__(Launcher)
            ui.ws_var = Value(str(ws))
            ui.cfg = cfg
            ui.worker_vars = {
                name: Value(name != "codex") for name in cfg.data["workers"]
            }
            ui.director_var = Value("codex")
            with patch("agentbus.gui.messagebox.showwarning") as showwarning:
                result = ui._collect_ui_config()
            self.assertIsNone(result)
            self.assertTrue(showwarning.called)

    def test_deselecting_coordinator_falls_back_to_selected_codex(self):
        ui = Launcher.__new__(Launcher)
        ui.cfg = SimpleNamespace(data={"workers": {
            "codex": {"enabled": True},
            "trae": {"enabled": True},
        }})
        ui.worker_vars = {"codex": Value(True), "trae": Value(False)}
        ui.director_box = Box()
        ui.director_var = Value("trae")
        ui._refresh_director_choices()
        self.assertEqual(ui.director_box.values, ["codex"])
        self.assertEqual(ui.director_var.get(), "codex")

    def test_new_workspace_first_save_preserves_selected_members_and_leader(self):
        with tempfile.TemporaryDirectory() as temp:
            ui = Launcher.__new__(Launcher)
            ui.ws_var = Value(temp)
            ui.cfg = None
            ui.worker_vars = {n: Value(n in {'workbuddy', 'trae'})
                              for n in ('codex', 'workbuddy', 'trae', 'zcode')}
            ui.director_var = Value('workbuddy')
            def init():
                ui.cfg = load(Path(temp), create=True)
                ui.worker_vars = {n: Value(n == 'codex') for n in ui.worker_vars}
                ui.director_var.set('codex')
                return 0
            ui._init_ws = init
            result = ui._collect_ui_config()
            self.assertEqual(result['director'], 'workbuddy')
            self.assertEqual({n for n, w in result['workers'].items() if w['enabled']},
                             {'workbuddy', 'trae'})

    def test_new_unconfigured_workspace_keeps_leader_picker_from_ui_draft(self):
        ui = Launcher.__new__(Launcher)
        ui.cfg = None
        ui.worker_vars = {'codex': Value(False), 'workbuddy': Value(True), 'trae': Value(True)}
        ui.director_var = Value('workbuddy')
        ui.director_box = Box()
        ui._refresh_director_choices()
        self.assertEqual(ui.director_box.values, ['trae', 'workbuddy'])
        self.assertEqual(ui.director_var.get(), 'workbuddy')

    def test_run_pipeline_starts_selected_sessions_and_keeps_event_loop_alive(self):
        emitted = []
        calls = []
        ui = Launcher.__new__(Launcher)
        ui.cfg = SimpleNamespace(director="codex", data={"workers": {
            "codex": {"can_spawn": True}}})
        ui.q = SimpleNamespace(put=lambda item: emitted.append(item))
        ui.stop_flag = SimpleNamespace(is_set=lambda: False)
        class FakeDirector:
            def __init__(self, cfg, on_event=None):
                pass
            def start_sessions(self):
                calls.append("start")
                return {}
            def submit(self, *args, **kwargs):
                calls.append("submit")
                return [{"id": "task-1"}]
            def run(self, *args, **kwargs):
                calls.append(("run", kwargs.get("keepalive")))
        with patch("agentbus.gui.Director", FakeDirector), \
             patch.object(ui, "_approve_plan", return_value=None):
            ui._run_pipeline("ship it", True)
        self.assertEqual(calls, ["start", "submit", ("run", True)])
        self.assertFalse(any(kind == "coordinator_queued" for kind, _ in emitted))

    def test_close_waits_for_pipeline_cleanup_before_destroying_window(self):
        ui = Launcher.__new__(Launcher)
        ui.root = MagicMock()
        ui.stop_flag = threading.Event()
        ui._close_requested = False
        ui._closed = False
        ui.ws_var = Value("workspace")
        ui.status_var = Value("")
        ui.log = lambda *args, **kwargs: None
        ui._stop_watchtower = MagicMock()
        scheduled = []
        ui.root.after.side_effect = lambda delay, callback: scheduled.append(callback)
        worker = SimpleNamespace(alive=True)
        worker.is_alive = lambda: worker.alive
        ui.worker = worker

        with patch("agentbus.gui.save_last_workspace"):
            ui._close()

        self.assertTrue(ui.stop_flag.is_set())
        ui.root.destroy.assert_not_called()
        ui._stop_watchtower.assert_not_called()

        ui._handle_event("pipeline_finished", {})
        self.assertEqual(len(scheduled), 1)
        ui.root.destroy.assert_not_called()

        worker.alive = False
        with patch("agentbus.gui.save_last_workspace"):
            scheduled.pop(0)()

        ui._stop_watchtower.assert_called_once_with()
        ui.root.destroy.assert_called_once_with()

    def test_close_without_active_pipeline_destroys_window_immediately(self):
        ui = Launcher.__new__(Launcher)
        ui.root = MagicMock()
        ui.stop_flag = threading.Event()
        ui._close_requested = False
        ui._closed = False
        ui.worker = None
        ui.ws_var = Value("workspace")
        ui.log = lambda *args, **kwargs: None
        ui._stop_watchtower = MagicMock()

        with patch("agentbus.gui.save_last_workspace"):
            ui._close()

        self.assertTrue(ui.stop_flag.is_set())
        ui._stop_watchtower.assert_called_once_with()
        ui.root.destroy.assert_called_once_with()

    def test_run_pipeline_refuses_to_start_when_any_selected_session_is_unready(self):
        emitted = []
        calls = []
        ui = Launcher.__new__(Launcher)
        ui.cfg = SimpleNamespace(director="codex", data={"workers": {
            "codex": {"can_spawn": True}}})
        ui.q = SimpleNamespace(put=lambda item: emitted.append(item))
        ui.stop_flag = SimpleNamespace(is_set=lambda: False)
        class FakeDirector:
            def __init__(self, cfg, on_event=None):
                pass
            def start_sessions(self):
                return {"workbuddy": "ACP unavailable"}
            def submit(self, *args, **kwargs):
                calls.append("submit")
            def run(self, *args, **kwargs):
                calls.append("run")
            def close(self):
                calls.append("close")
        with patch("agentbus.gui.Director", FakeDirector):
            ui._run_pipeline("ship it", True)
        self.assertNotIn("submit", calls)
        self.assertNotIn("run", calls)
        self.assertIn("close", calls)
        self.assertEqual(emitted[0][0], "session_preflight_failed")
        self.assertEqual(emitted[-1][0], "pipeline_finished")

    def test_peer_review_result_is_visible_in_activity_log(self):
        ui = Launcher.__new__(Launcher)
        entries = []
        ui.log = lambda text, tag="info": entries.append((text, tag))

        ui._handle_event("peer_review_result", {
            "reviewer": "workbuddy", "title": "部署计划审查",
            "result": "配置中的端口冲突，建议先修正。",
        })

        rendered = "\n".join(text for text, _ in entries)
        self.assertIn("workbuddy", rendered)
        self.assertIn("部署计划审查", rendered)
        self.assertIn("端口冲突", rendered)

    def test_main_launcher_runs_and_stops_embedded_inbox_monitor(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = Launcher.__new__(Launcher)
            ui.q = queue.Queue()
            ui.watchtower = None
            ui.watch_thread = None
            ui.watch_stop = None
            ui.watch_status_var = Value("")

            with patch("agentbus.gui.Watchtower.notify"):
                ui._start_watchtower(cfg)
                thread = ui.watch_thread
                try:
                    kind, data = ui.q.get(timeout=3)
                    self.assertEqual(kind, "watchtower_update")
                    self.assertEqual(data["total"], 0)
                    self.assertIn("值守", ui.watch_status_var.get())
                    self.assertTrue(thread.is_alive())

                    task = ui.watchtower.bus.make(
                        "codex", "trae", "task", "smoke", "test inbox event",
                        result_to="codex")
                    ui.watchtower.bus.post(task)
                    kind, data = ui.q.get(timeout=3)
                    self.assertEqual(kind, "watchtower_update")
                    self.assertEqual(data["total"], 1)
                finally:
                    ui._stop_watchtower()
                    thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_same_project_group_change_restarts_monitor_for_new_members(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            cfg.data = gui.initial_gui_group(cfg.data)
            ui = Launcher.__new__(Launcher)
            ui.q = queue.Queue()
            ui.watchtower = ui.watch_thread = ui.watch_stop = None
            ui.watch_status_var = Value('')
            with patch('agentbus.gui.Watchtower.notify'):
                ui._start_watchtower(cfg)
                previous = ui.watch_thread
                old = ui.watchtower
                ui.q.get(timeout=3)
                data = json.loads(json.dumps(cfg.data))
                data['director'] = 'workbuddy'
                for name, worker in data['workers'].items():
                    worker['enabled'] = name in {'workbuddy', 'trae'}
                updated = Config(cfg.path, cfg.workspace, data)
                try:
                    ui._start_watchtower(updated)
                    self.assertIsNot(ui.watchtower, old)
                    self.assertFalse(previous.is_alive())
                    self.assertEqual(set(ui.watchtower.watched_agents()), {'workbuddy', 'trae'})
                    self.assertEqual(ui.watchtower.cfg.director, 'workbuddy')
                    initial = ui.q.get(timeout=3)
                    self.assertEqual(initial[1]['total'], 0)
                    task = ui.watchtower.bus.make('workbuddy', 'trae', 'task', 'new group',
                                                   'new member inbox', result_to='workbuddy')
                    ui.watchtower.bus.post(task)
                    kind, event = ui.q.get(timeout=3)
                    self.assertEqual(kind, 'watchtower_update')
                    self.assertEqual(event['total'], 1)
                finally:
                    ui._stop_watchtower()


class MaintenanceRemovalWordingTests(unittest.TestCase):
    def test_safe_partial_removal_explains_intentional_user_file_preservation(self):
        text = gui.render_maintenance({'status': 'partial', 'applied': True,
            'removed': ['owned-skill'], 'preserved': ['user-skill'],
            'errors': [], 'hosts': {}})
        self.assertIn('已移除归属文件；用户文件按约保留', text.splitlines()[0])
        self.assertNotIn('需要处理', text.splitlines()[0])

    def test_partial_removal_error_is_still_shown_as_requiring_attention(self):
        text = gui.render_maintenance({'status': 'partial', 'applied': True,
            'removed': [], 'preserved': ['user-skill'],
            'errors': ['access denied'], 'hosts': {}})
        self.assertIn('需要处理', text.splitlines()[0])
        self.assertIn('access denied', text)

    def test_preserved_only_removal_does_not_claim_any_file_was_deleted(self):
        text = gui.render_maintenance({'status': 'partial', 'applied': True,
            'removed': [], 'preserved': ['user-skill'], 'errors': [], 'hosts': {}})
        self.assertIn('没有可移除的归属文件', text.splitlines()[0])
        self.assertNotIn('已移除', text.splitlines()[0])

    def test_launcher_integrity_failure_does_not_display_as_installed(self):
        text = gui.render_maintenance({'status': 'partial', 'hosts': {
            'codex': {'status': 'invalid_launcher', 'skill': 'registered-skill',
                      'launcher_reason': 'SHA256 不一致'}},
            'errors': []})
        self.assertIn('codex：程序入口校验未通过', text)
        self.assertIn('SHA256 不一致', text)


if __name__ == "__main__":
    unittest.main()

