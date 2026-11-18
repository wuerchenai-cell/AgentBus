"""Enterprise navigation contracts. Source: user/group review, 2026-10-04.

Widget calls are recorded without opening a desktop window or a model session.
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from receipt_helpers import receipt_ticket

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agentbus import gui
from agentbus.config import load


class Value:
    def __init__(self, value="", **_kwargs):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def trace_add(self, *_args):
        return 'trace'


class Widget:
    def __init__(self, parent=None, **kwargs):
        self.master = parent
        self.kwargs = kwargs
        self.children = []
        self.pages = []
        self.selected = None
        self.text = ""
        self.bindings = {}
        self.tags = {}
        self.tagged_ranges = []
        if parent is not None:
            parent.children.append(self)

    def pack(self, **_kwargs):
        pass

    def grid(self, **_kwargs):
        pass

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)

    def cget(self, key):
        return self.kwargs.get(key, "#f3f5f8")

    def add(self, frame, **kwargs):
        self.pages.append((frame, kwargs))
        if self.selected is None:
            self.selected = frame

    def select(self, frame=None):
        if frame is not None:
            self.selected = frame
        return self.selected

    def bind(self, event, callback, **_kwargs):
        self.bindings[event] = callback

    def columnconfigure(self, *_args, **_kwargs):
        pass

    # Source: Codex, 2026-10-05. Faithful row layout capability for the
    # maintenance Canvas; this stub records navigation, not physical geometry.
    # Ordinary-window reachability is checked by separate real Tk tests.
    def rowconfigure(self, *_args, **_kwargs):
        pass

    def winfo_children(self):
        return self.children

    def heading(self, *_args, **_kwargs):
        pass

    def column(self, *_args, **_kwargs):
        pass

    def get_children(self):
        return []

    def selection(self):
        return []

    def create_window(self, *_args, **_kwargs):
        return 1

    def yview(self, *_args):
        pass

    def xview(self, *_args):
        pass

    def set(self, *_args):
        pass

    def tag_config(self, name, **kwargs):
        self.tags[name] = kwargs

    tag_configure = tag_config

    def tag_add(self, name, start, end):
        self.tagged_ranges.append((name, start, end))

    def tag_delete(self, name):
        self.tags.pop(name, None)

    def delete(self, *_args):
        self.text = ""

    def insert(self, _where, value):
        self.text += value

    def title(self, *_args):
        pass

    def geometry(self, *_args):
        pass

    def minsize(self, *_args):
        pass

    def transient(self, *_args):
        pass

    def destroy(self):
        pass


class EnterpriseUiTests(unittest.TestCase):
    def test_install_hint_only_mentions_trae_when_it_is_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = gui.Launcher.__new__(gui.Launcher)
            ui.cfg = cfg
            ui._show_maintenance = MagicMock()
            result = {'hosts': {'codex': {'skill': 'isolated text skill'}}}
            for selected in (False, True):
                cfg.data['workers']['trae']['enabled'] = selected
                with patch('agentbus.gui.install_workflows', return_value=result), \
                     patch('agentbus.gui.messagebox.showinfo') as dialog:
                    ui._install_codex_skill()
                self.assertEqual('Trae：' in dialog.call_args.args[1], selected)

    def test_resume_tool_explains_difference_from_admission_confirmation(self):
        _ui, records = self.build('_build_tabs')
        text = '\n'.join(str(item.kwargs.get('text', '')) for item in records)
        self.assertIn('仅恢复任务，不登记文件接入确认', text)
        self.assertIn('复制接入指令', text)

    def test_latest_receipt_rejects_nontext_body_and_oversized_content(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            cfg, task, result = self._receipt_fixture(Path(temp))
            item = {'latest_task_id': task.id, 'latest_receipt_id': result.id, 'latest_receipt_to': 'codex'}
            path = cfg.workspace / '.agentbus/agents/codex/ready' / (result.id + '.json')
            original = path.read_bytes()
            data = json.loads(original)
            data['body'] = {'not': 'text'}
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '正文|文本'):
                gui.read_latest_receipt(cfg, 'workbuddy', item)
            path.write_bytes(b'x' * (gui.MAX_BYTES + 1))
            with self.assertRaisesRegex(ValueError, '上限'):
                gui.read_latest_receipt(cfg, 'workbuddy', item)

    def test_member_overview_is_compact_and_shares_one_session_boundary(self):
        members = [{'name': f'member-{index}', 'display_name': f'Agent {index}',
                    'mode_label': '对话接入', 'next_step': '复制接入指令',
                    'detail': '重复边界说明不应渲染',
                    'indicator': {'symbol': '◐', 'label': '已认领，运行未确认', 'color': '#7C3AED'}}
                   for index in range(5)]
        text = gui.render_connection_summary({'workspace': 'project', 'coordinator': 'member-0',
                                               'members': members})
        self.assertNotIn('重复边界说明不应渲染', text)
        self.assertEqual(text.count('当前会话未确认'), 1)
        self.assertEqual(text.count('◐ Agent'), 5)
        self.assertLessEqual(len(text.splitlines()), 41)

    def test_all_member_rows_get_individual_colored_symbols(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = gui.Launcher.__new__(gui.Launcher)
            ui.cfg = cfg
            ui.ws_var = Value(str(cfg.workspace))
            ui.director_var = Value(cfg.director)
            ui.worker_vars = {name: Value(True) for name in cfg.enabled_workers()}
            ui.connection_agent = Value('codex')
            ui.connection_box = Widget()
            value = {'workspace': str(cfg.workspace), 'coordinator': cfg.director,
                     'members': [{'name': name, 'display_name': name,
                                  'indicator': {'symbol': '◐' if name == 'workbuddy' else 'Ⅱ',
                                                'label': '状态记录',
                                                'color': '#7C3AED' if name == 'workbuddy' else '#D97706'}}
                                 for name in cfg.enabled_workers()]}
            ui._refresh_connections(summary=value)
            self.assertEqual(len(ui.connection_box.tagged_ranges), len(value['members']))
            colors = {entry['foreground'] for entry in ui.connection_box.tags.values()}
            self.assertEqual(colors, {'#7C3AED', '#D97706'})

    def test_member_onboarding_has_explicit_read_only_receipt_entry(self):
        ui, records = self.build('_build_tabs')
        buttons = [item for item in records if item.kind == 'Button'
                   and item.kwargs.get('text') == '查看最近任务回执']
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].kwargs['command'], ui._show_recent_receipt)

    def _receipt_fixture(self, root):
        from agentbus.bus import Bus
        cfg = load(root / 'project', create=True)
        bus = Bus(cfg.workspace / '.agentbus')
        task = bus.make('codex', 'workbuddy', 'task', 'Scoped failed task',
                        'original task instruction', result_to='codex')
        bus.post(task)
        task = bus.claim('workbuddy', bus.pending('workbuddy')[0])
        # Codex integration migration, 2026-10-08: submit with the original
        # captured claim before terminal completion, exactly as production does.
        ticket = receipt_ticket(task)
        bus.heartbeat('workbuddy', task, ticket, note='test executor is alive')
        result = bus.commit_result('workbuddy', task, ticket,
                                   body='Actual failure body: missing local dependency',
                                   exit_code=1)
        return cfg, task, result

    def test_latest_receipt_reads_only_exact_associated_terminal_record_without_writes(self):
        self.assertTrue(hasattr(gui, 'read_latest_receipt'))
        with tempfile.TemporaryDirectory() as temp:
            cfg, task, result = self._receipt_fixture(Path(temp))
            before = {path.relative_to(cfg.workspace).as_posix(): path.read_bytes()
                      for path in cfg.workspace.rglob('*') if path.is_file()}
            item = {'latest_task_id': task.id, 'latest_receipt_id': result.id, 'latest_receipt_to': 'codex'}
            with patch('agentbus.gui.Bus', side_effect=AssertionError('viewer must not initialize a bus')), \
                 patch('subprocess.Popen', side_effect=AssertionError('viewer must not execute')):
                value = gui.read_latest_receipt(cfg, 'workbuddy', item)
            self.assertEqual(value['task_id'], task.id)
            self.assertEqual(value['status'], 'failed')
            self.assertEqual(value['body'], result.body)
            self.assertEqual(before, {path.relative_to(cfg.workspace).as_posix(): path.read_bytes()
                                    for path in cfg.workspace.rglob('*') if path.is_file()})

    def test_latest_receipt_rejects_unknown_member_route_mismatch_and_path_injection(self):
        self.assertTrue(hasattr(gui, 'read_latest_receipt'))
        with tempfile.TemporaryDirectory() as temp:
            cfg, task, result = self._receipt_fixture(Path(temp))
            item = {'latest_task_id': task.id, 'latest_receipt_id': result.id, 'latest_receipt_to': 'codex'}
            for member, patch_item in [('opencode', item), ('workbuddy', dict(item, latest_receipt_to='outside')),
                                       ('workbuddy', dict(item, latest_task_id='../private')),
                                       ('workbuddy', dict(item, latest_receipt_id='../private'))]:
                with self.subTest(member=member, item=patch_item), \
                     patch.object(Path, 'open', side_effect=AssertionError('invalid identity opened a file')):
                    with self.assertRaises(ValueError):
                        gui.read_latest_receipt(cfg, member, patch_item)
            path = cfg.workspace / '.agentbus/agents/codex/ready' / (result.id + '.json')
            import json
            data = json.loads(path.read_text(encoding='utf-8'))
            data['sender'] = 'trae'
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '关联|路由|成员'):
                gui.read_latest_receipt(cfg, 'workbuddy', item)

    def test_latest_receipt_rejects_nonterminal_duplicate_or_linked_message(self):
        self.assertTrue(hasattr(gui, 'read_latest_receipt'))
        import json
        with tempfile.TemporaryDirectory() as temp:
            cfg, task, result = self._receipt_fixture(Path(temp))
            item = {'latest_task_id': task.id, 'latest_receipt_id': result.id, 'latest_receipt_to': 'codex'}
            done = cfg.workspace / '.agentbus/agents/workbuddy/done' / (task.id + '.json')
            data = json.loads(done.read_text(encoding='utf-8'))
            data['status'] = 'pending'
            done.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '终态|完成'):
                gui.read_latest_receipt(cfg, 'workbuddy', item)
            data['status'] = 'failed'
            done.write_text(json.dumps(data), encoding='utf-8')
            origin = cfg.workspace / '.agentbus/agents/codex/ready' / (result.id + '.json')
            duplicate = origin.parent.parent / 'done' / origin.name
            duplicate.write_bytes(origin.read_bytes())
            with self.assertRaisesRegex(ValueError, '重复|唯一'):
                gui.read_latest_receipt(cfg, 'workbuddy', item)

    def test_read_only_receipt_window_requires_click_and_shows_original_failure(self):
        self.assertTrue(hasattr(gui.Launcher, '_show_recent_receipt'))
        with tempfile.TemporaryDirectory() as temp:
            cfg, task, result = self._receipt_fixture(Path(temp))
            ui = gui.Launcher.__new__(gui.Launcher)
            ui.root = MagicMock()
            ui._workflow_cfg = lambda: cfg
            ui.connection_agent = Value('workbuddy')
            ui._show_readonly_text = MagicMock()
            with patch('agentbus.gui.connection_summary', return_value={'members': [{
                'name': 'workbuddy', 'latest_task_id': task.id, 'latest_receipt_id': result.id,
                'latest_receipt_to': 'codex'}]}), \
                 patch('subprocess.Popen', side_effect=AssertionError('viewer must not run a process')):
                ui._show_recent_receipt()
            displayed = ui._show_readonly_text.call_args.args[1]
            self.assertIn(task.id, displayed)
            self.assertIn(result.id, displayed)
            self.assertIn('失败', displayed)
            self.assertIn(result.body, displayed)

    def test_header_exposes_usage_guide_without_project_setup(self):
        ui, records = self.build('_build_header')
        guides = [item for item in records if item.kind == 'Button'
                  and item.kwargs.get('text') == '使用指南']
        self.assertEqual(len(guides), 1)
        self.assertEqual(guides[0].kwargs['command'], ui._show_usage_guide)

    def test_usage_guide_reads_fixed_bundle_resource_and_never_falls_back_to_project(self):
        self.assertTrue(hasattr(gui, 'read_usage_guide'))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            guide = root / 'share_assets/开始使用.md'
            guide.parent.mkdir()
            guide.write_text('固定资源指南\n第一步：保存团队', encoding='utf-8')
            with patch.object(gui.sys, '_MEIPASS', str(root), create=True):
                self.assertEqual(gui.read_usage_guide(), '固定资源指南\n第一步：保存团队')
                guide.unlink()
                with self.assertRaises(FileNotFoundError):
                    gui.read_usage_guide()

    def test_usage_guide_refuses_oversized_or_non_utf8_resource(self):
        self.assertTrue(hasattr(gui, 'read_usage_guide'))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            guide = root / 'share_assets/开始使用.md'
            guide.parent.mkdir()
            with patch.object(gui.sys, '_MEIPASS', str(root), create=True):
                guide.write_bytes(b'x' * (256 * 1024 + 1))
                with self.assertRaisesRegex(ValueError, '过大|大小|限制'):
                    gui.read_usage_guide()
                guide.write_bytes(b'\xff\xfeinvalid utf8')
                with self.assertRaises(UnicodeDecodeError):
                    gui.read_usage_guide()

    def test_usage_guide_rejects_parent_junction_without_reading_destination(self):
        self.assertTrue(hasattr(gui, 'read_usage_guide'))
        if os.name != 'nt':
            self.skipTest('Windows junction boundary')
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / 'bundle'
            external = root / 'external'
            bundle.mkdir()
            external.mkdir()
            (external / '开始使用.md').write_text('must not read linked content', encoding='utf-8')
            link = bundle / 'share_assets'
            created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(external)],
                                     capture_output=True, check=False)
            self.assertEqual(created.returncode, 0, created.stderr)
            try:
                with patch.object(gui.sys, '_MEIPASS', str(bundle), create=True), \
                     patch.object(Path, 'open', side_effect=AssertionError('junction content was read')):
                    with self.assertRaisesRegex(ValueError, '链接|重解析|reparse'):
                        gui.read_usage_guide()
            finally:
                os.rmdir(link)

    def test_usage_guide_dialog_is_read_only_internal_and_has_visible_close_action(self):
        self.assertTrue(hasattr(gui.Launcher, '_show_usage_guide'))
        ui = gui.Launcher.__new__(gui.Launcher)
        ui.root = Widget()
        ui.cfg = None
        widgets = []

        def make(parent=None, **kwargs):
            result = Widget(parent, **kwargs)
            widgets.append(result)
            return result

        with patch('agentbus.gui.read_usage_guide', return_value='Complete packaged guide') as read, \
             patch('agentbus.gui.tk.Toplevel', side_effect=make), \
             patch('agentbus.gui.ttk.Frame', side_effect=make), \
             patch('agentbus.gui.ttk.Button', side_effect=make), \
             patch('agentbus.gui.ttk.Label', side_effect=make), \
             patch('agentbus.gui.scrolledtext.ScrolledText', side_effect=make), \
             patch('agentbus.gui.os.startfile', side_effect=AssertionError('external editor used'), create=True), \
             patch('subprocess.Popen', side_effect=AssertionError('external program used')):
            ui._show_usage_guide()
        read.assert_called_once_with()
        text = next(item for item in widgets if item.text == 'Complete packaged guide')
        self.assertEqual(text.kwargs['state'], 'disabled')
        self.assertTrue(any(item.kwargs.get('text') == '关闭' for item in widgets))

    def test_missing_usage_resource_shows_error_and_does_not_open_an_empty_window(self):
        self.assertTrue(hasattr(gui.Launcher, '_show_usage_guide'))
        ui = gui.Launcher.__new__(gui.Launcher)
        ui.root = Widget()
        with patch('agentbus.gui.read_usage_guide', side_effect=FileNotFoundError('packaged guide missing')), \
             patch('agentbus.gui.messagebox.showwarning') as warning, \
             patch('agentbus.gui.tk.Toplevel') as window:
            ui._show_usage_guide()
        window.assert_not_called()
        self.assertIn('使用指南', warning.call_args.args[0])
        self.assertIn('重新获取', warning.call_args.args[1])

    def test_receipt_summary_distinguishes_task_failure_from_connection(self):
        text = gui.render_connection_summary({'workspace': 'project', 'coordinator': 'codex',
            'members': [{'name': 'workbuddy', 'receipt_count': 3,
                         'successful_receipt_count': 2, 'failed_receipt_count': 1,
                         'latest_task_outcome': 'failed',
                         'latest_outcome_at': '2026-10-04T10:00:00+08:00'}]})
        self.assertIn('成功 2 / 失败 1', text)
        self.assertIn('最近任务结果：失败', text)
        self.assertIn('2026-10-04T10:00:00+08:00', text)
        self.assertIn('会话未确认', text)

    def build(self, method):
        ui = gui.Launcher.__new__(gui.Launcher)
        ui.root = Widget()
        ui.cfg = None
        ui._refresh_workflow = lambda *_args, **_kwargs: None
        ui._refresh_connections = lambda *_args, **_kwargs: None
        ui._refresh_workbench_overview = lambda *_args, **_kwargs: None
        records = []

        def make(kind):
            class RecordedWidget(Widget):
                def __init__(self, parent=None, **kwargs):
                    super().__init__(parent, **kwargs)
                    self.kind = kind
                    records.append(self)
            return RecordedWidget

        with contextlib.ExitStack() as stack:
            for kind in ('Frame', 'LabelFrame', 'Label', 'Button', 'Entry',
                         'Combobox', 'Scrollbar', 'Notebook', 'Checkbutton', 'Treeview'):
                stack.enter_context(patch(f'agentbus.gui.ttk.{kind}', new=make(kind)))
            stack.enter_context(patch('agentbus.gui.tk.Canvas', new=make('Canvas')))
            stack.enter_context(patch('agentbus.gui.tk.Text', new=make('Text')))
            stack.enter_context(patch('agentbus.gui.scrolledtext.ScrolledText', new=make('Text')))
            stack.enter_context(patch('agentbus.gui.tk.StringVar', side_effect=lambda **kw: Value(**kw)))
            stack.enter_context(patch('agentbus.gui.tk.BooleanVar', side_effect=lambda **kw: Value(**kw)))
            stack.enter_context(patch('agentbus.gui.initial_workspace', return_value='project'))
            getattr(ui, method)()
        return ui, records

    def test_primary_navigation_has_six_stable_pages_and_starts_at_home(self):
        ui, records = self.build('_build_tabs')
        top = [item for item in records if item.kind == 'Notebook' and item.master is ui.content_frame]
        self.assertEqual(len(top), 1)
        self.assertEqual([options['text'].strip() for _, options in top[0].pages],
                         ['工作台', '团队', '任务', '接入与技能', '设置', '运行记录'])
        self.assertEqual(list(ui.pages), ['home', 'team', 'tasks', 'access', 'settings', 'logs'])
        self.assertIs(top[0].selected, ui.pages['home'])
        self.assertIs(ui.connection_page, ui.pages['access'])

    def test_unread_task_views_explain_refresh_instead_of_looking_empty(self):
        # Source: actual frozen GUI observation, Codex, 2026-10-04.
        ui, _records = self.build('_build_tabs')
        for box in (ui.jobs_box, ui.handoff_box, ui.esc_box):
            self.assertIn('尚未读取', box.text)
            self.assertIn('刷新', box.text)
            self.assertIn('不代表', box.text)
            self.assertEqual(box.kwargs['state'], 'disabled')

    def test_copy_handoff_rejects_unsaved_team_before_using_cached_prompt(self):
        ui = gui.Launcher.__new__(gui.Launcher)
        ui._workflow_cfg = MagicMock(return_value=None)
        ui._refresh_handoff = MagicMock()
        ui._manual_prompts = ['old project cached task']
        ui.root = MagicMock()
        ui.log = MagicMock()
        ui._copy_handoff()
        ui._workflow_cfg.assert_called_once_with()
        ui._refresh_handoff.assert_not_called()
        ui.root.clipboard_clear.assert_not_called()
        ui.root.clipboard_append.assert_not_called()

    def test_copy_handoff_refreshes_saved_queue_before_copying(self):
        ui = gui.Launcher.__new__(gui.Launcher)
        ui._workflow_cfg = MagicMock(return_value=object())
        ui._manual_prompts = ['cached task already completed']
        ui._refresh_handoff = MagicMock(side_effect=lambda: setattr(ui, '_manual_prompts', []))
        ui.root = MagicMock()
        ui.log = MagicMock()
        with patch('agentbus.gui.messagebox.showinfo') as info:
            ui._copy_handoff()
        ui._refresh_handoff.assert_called_once_with()
        ui.root.clipboard_append.assert_not_called()
        info.assert_called_once()

    def test_configuration_toolbar_keeps_only_save_and_add_member(self):
        _ui, records = self.build('_build_config')
        buttons = [item.kwargs.get('text') for item in records if item.kind == 'Button']
        self.assertIn('保存团队', buttons)
        self.assertIn('添加 Agent', buttons)
        self.assertNotIn('探测本机 agent', buttons)
        self.assertNotIn('导入成员模板', buttons)
        self.assertNotIn('分享给朋友', buttons)
        self.assertNotIn('初始化', buttons)

    def test_management_groups_secondary_actions_without_losing_existing_controls(self):
        ui, records = self.build('_build_tabs')
        management = [item for item in records if item.kind == 'Notebook'
                      and item.master is ui.management_page]
        self.assertEqual(len(management), 1)
        self.assertEqual([options['text'].strip() for _, options in management[0].pages],
                         ['接口配置', '维护', '应用分发'])
        commands = {item.kwargs.get('command') for item in records if item.kind == 'Button'}
        for method in ('_detect', '_add_member', '_import_team', '_export_team',
                       '_copy_mcp', '_check_mcp', '_install_codex_skill',
                       '_open_recovery_center', '_save_diagnostic', '_uninstall_workflows'):
            self.assertIn(getattr(ui, method), commands, method)

    def test_goal_action_labels_copy_as_transport_not_as_dispatch(self):
        _ui, records = self.build('_build_goal')
        labels = [item.kwargs.get('text', '') for item in records]
        self.assertIn('复制任务指令', labels)
        self.assertTrue(any('复制不派发' in text for text in labels))

    def test_save_selects_member_onboarding_and_does_not_claim_connected(self):
        ui = gui.Launcher.__new__(gui.Launcher)
        ui.cfg = SimpleNamespace(workspace=Path('current-project'))
        ui._collect_ui_config = lambda: {}
        ui._refresh_saved_group = MagicMock(return_value=True)
        ui._refresh_connections = MagicMock()
        ui.tabs = MagicMock()
        ui.connection_page = object()
        ui.status_var = Value()
        with patch('agentbus.gui.messagebox.showinfo') as message:
            ui._save_group()
        ui.tabs.select.assert_called_once_with(ui.connection_page)
        ui._refresh_connections.assert_called_once()
        self.assertIn('接入', ui.status_var.get())
        self.assertNotIn('已连接', ui.status_var.get())
        message.assert_not_called()

    def test_connection_instruction_preserves_project_member_and_requires_explicit_send(self):
        self.assertTrue(hasattr(gui.Launcher, '_copy_connection'),
                        'onboarding needs a distinct, project-scoped copy action')
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            before = cfg.path.read_bytes()
            ui = gui.Launcher.__new__(gui.Launcher)
            ui.root = MagicMock()
            ui._workflow_cfg = lambda: cfg
            ui.connection_agent = Value('workbuddy')
            ui.log = MagicMock()
            ui.status_var = Value()
            with patch('agentbus.gui.workflow_status', return_value={'ready': True}), \
                 patch('agentbus.gui.connection_prompt', return_value='project-scoped instruction', create=True) as prompt, \
                 patch('agentbus.gui.resume_prompt', side_effect=AssertionError('new onboarding needs explicit confirmation')) as resume, \
                 patch('subprocess.Popen', side_effect=AssertionError('copy must not launch')), \
                 patch('agentbus.gui.prepare_project_workflow', side_effect=AssertionError('copy must not write')):
                ui._copy_connection()
            prompt.assert_called_once_with(cfg, 'workbuddy')
            resume.assert_not_called()
            ui.root.clipboard_append.assert_called_once_with('project-scoped instruction')
            self.assertIn('发送', ui.status_var.get())
            self.assertEqual(cfg.path.read_bytes(), before)

    def test_file_admission_and_task_receipts_have_separate_visible_times_and_scopes(self):
        text = gui.render_connection_summary({
            'workspace': 'project', 'coordinator': 'codex',
            'members': [{'name': 'workbuddy', 'display_name': 'WorkBuddy',
                         'admission_confirmation_at': '2026-10-04T11:03:00+08:00',
                         'admission_label': '已登记文件接入自报', 'receipt_count': 2,
                         'last_receipt_at': '2026-10-03T20:00:00+08:00'}]}, 'workbuddy')
        self.assertIn('文件接入确认', text)
        self.assertIn('2026-10-04T11:03:00+08:00', text)
        self.assertIn('不是在线验证', text)
        self.assertIn('任务回执：2', text)
        self.assertIn('2026-10-03T20:00:00+08:00', text)
        self.assertIn('当前会话未确认', text)

    def test_codex_task_instruction_includes_selected_project_and_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            ui = gui.Launcher.__new__(gui.Launcher)
            ui.root = MagicMock()
            ui.cfg = cfg
            ui.worker = None
            ui.goal_text = MagicMock()
            ui.goal_text.get.return_value = 'bounded delivery'
            ui.ws_var = Value(str(cfg.workspace))
            ui.worker_vars = {name: Value(True) for name in cfg.enabled_workers()}
            ui.director_var = Value(cfg.director)
            ui.goal_agent = Value('codex')
            ui.log = MagicMock()
            with patch('agentbus.gui.messagebox.showinfo'):
                ui._start()
            copied = ui.root.clipboard_append.call_args.args[0]
            self.assertIn(str(cfg.workspace), copied)
            self.assertIn('codex', copied)
            self.assertIn('bounded delivery', copied)

    def test_read_only_connection_display_cannot_certify_current_chat_from_history(self):
        self.assertTrue(hasattr(gui, 'render_connection_summary'))
        text = gui.render_connection_summary({
            'workspace': 'project', 'coordinator': 'codex',
            'members': [{'name': 'workbuddy', 'display_name': 'WorkBuddy',
                         'mode_label': '人工接手', 'evidence_label': '已有有效总线回执',
                         'detail': '历史回执 2026-10-03',
                         'next_step': '在当前项目新建对话发送接入指令'}]}, 'workbuddy')
        self.assertIn('当前会话', text)
        self.assertIn('不代表', text)
        self.assertIn('人工接手', text)
        self.assertNotIn('当前会话已连接', text)

    def test_selected_member_indicator_explains_the_member_and_file_state_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            before = {path.relative_to(cfg.workspace).as_posix(): path.read_bytes()
                      for path in cfg.workspace.rglob('*') if path.is_file()}
            ui = gui.Launcher.__new__(gui.Launcher)
            ui.cfg = cfg
            ui.ws_var = Value(str(cfg.workspace))
            ui.director_var = Value(cfg.director)
            ui.worker_vars = {name: Value(True) for name in cfg.enabled_workers()}
            ui.connection_agent = Value('workbuddy')
            ui.connection_agent_box = Widget()
            ui.connection_box = Widget()
            ui.connection_indicator = Widget()
            value = {'workspace': str(cfg.workspace), 'coordinator': cfg.director,
                     'members': [{'name': 'workbuddy', 'display_name': 'WorkBuddy',
                                  'indicator': {'label': '已认领（运行未确认）', 'color': '#7C3AED', 'symbol': '◐'}}]}
            with patch('agentbus.gui.connection_summary', return_value=value), \
                 patch('subprocess.Popen', side_effect=AssertionError('display must not start session')):
                ui._refresh_connections()
            self.assertIn('WorkBuddy', ui.connection_indicator.kwargs['text'])
            self.assertIn('运行未确认', ui.connection_indicator.kwargs['text'])
            self.assertEqual(ui.connection_indicator.kwargs['foreground'], '#7C3AED')
            self.assertTrue(ui.connection_indicator.kwargs['text'].startswith('◐'))
            self.assertEqual(ui.connection_box.kwargs['state'], 'disabled')
            self.assertIn('当前会话未确认', ui.connection_box.text)
            self.assertEqual(before, {path.relative_to(cfg.workspace).as_posix(): path.read_bytes()
                                    for path in cfg.workspace.rglob('*') if path.is_file()})

    def test_missing_project_mcp_preflight_points_to_the_visible_save_action(self):
        with tempfile.TemporaryDirectory() as temp:
            ui = gui.Launcher.__new__(gui.Launcher)
            ui.ws_var = Value(temp)
            with patch('agentbus.gui.messagebox.showwarning') as warning, \
                 patch('agentbus.gui.subprocess.run') as run:
                ui._check_mcp()
            run.assert_not_called()
            self.assertIn('保存团队', warning.call_args.args[1])
            self.assertNotIn('初始化', warning.call_args.args[1])

    def test_manual_transport_uses_configured_coordinator_and_does_not_claim_automatic_relay(self):
        from agentbus.bus import Bus
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            cfg.data['director'] = 'workbuddy'
            bus = Bus(cfg.workspace / '.agentbus')
            task = bus.make('workbuddy', 'trae', 'task', 'scoped task',
                            'read only', result_to='workbuddy')
            bus.post(task)
            ui = gui.Launcher.__new__(gui.Launcher)
            ui.cfg, ui.bus = cfg, bus
            ui.handoff_box = MagicMock()
            ui._refresh_handoff()
            prompt = ui._manual_prompts[0]
            self.assertIn('项目组长：workbuddy', prompt)
            self.assertIn('人工传递不代表各软件已自动互联', prompt)
            self.assertNotIn('Codex 在观察', prompt)
            self.assertEqual(len(bus.pending('trae')), 1)

    def test_unsaved_project_member_or_leader_clears_previous_evidence_and_disables_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp) / 'saved-project', create=True)
            before = cfg.path.read_bytes()
            for change in ('project', 'member', 'leader'):
                with self.subTest(change=change):
                    ui = gui.Launcher.__new__(gui.Launcher)
                    ui.cfg = cfg
                    ui.ws_var = Value(str(cfg.workspace))
                    ui.director_var = Value(cfg.director)
                    ui.worker_vars = {name: Value(True) for name in cfg.enabled_workers()}
                    ui.connection_agent = Value('workbuddy')
                    ui.connection_agent_box = Widget()
                    ui.connection_box = Widget()
                    ui.connection_indicator = Widget()
                    ui.connection_copy_btn = Widget()
                    if change == 'project':
                        ui.ws_var.set(str(Path(temp) / 'unsaved-project'))
                    elif change == 'member':
                        ui.worker_vars['workbuddy'].set(False)
                    else:
                        ui.director_var.set('workbuddy')
                    with patch('agentbus.gui.connection_summary') as read:
                        ui._refresh_connections()
                    read.assert_not_called()
                    self.assertEqual(ui.connection_copy_btn.kwargs.get('state'), 'disabled')
                    self.assertIn('待保存', ui.connection_indicator.kwargs['text'])
                    self.assertNotIn('已有有效总线回执', ui.connection_box.text)
                    self.assertNotIn('历史回执 2026', ui.connection_box.text)
                    self.assertEqual(cfg.path.read_bytes(), before)

    def test_draft_notifications_are_debounced_read_only_and_refresh_latest_selection(self):
        self.assertTrue(hasattr(gui.Launcher, '_on_team_draft_changed'))
        ui = gui.Launcher.__new__(gui.Launcher)
        ui.root = MagicMock()
        ui.connection_box = Widget()
        ui._refresh_connections = MagicMock()
        ui._refresh_workflow = MagicMock()
        with patch('agentbus.gui.prepare_project_workflow') as prepare, \
             patch('agentbus.gui.detect_group_agents') as detect:
            ui._on_team_draft_changed()
            ui._on_team_draft_changed()
            ui.root.after_idle.assert_called_once()
            ui.root.after_idle.call_args.args[0]()
        ui._refresh_connections.assert_called_once()
        ui._refresh_workflow.assert_called_once()
        prepare.assert_not_called()
        detect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
