"""Source: Codex internal GUI integration, 2026-10-05.

Real temporary public-package checks and queue lifecycle tests. No native GUI,
model, network, private host directory or deployment is opened by these tests.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentbus import gui
from agentbus.skill_packages import SkillPackageError


class SkillGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package('skills/demo')

    def package(self, relative, body=b'# Demo\r\n'):
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_bytes(body)
        return path

    def check(self, source='skills/demo', target='targets/demo', **context):
        function = getattr(gui, 'check_skill_packages', None)
        self.assertTrue(callable(function), 'read-only GUI check is missing')
        return function(self.root, source, target,
                        host=context.get('host', 'codex'),
                        scope=context.get('scope', 'project'),
                        route=context.get('route', 'desktop'))

    def test_missing_target_is_previewed_without_creating_directory_or_control_files(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*'))
        result = self.check()
        self.assertEqual(result['preview']['action'], 'create')
        self.assertIsNone(result['target'])
        self.assertFalse(result['preview']['write_permitted'])
        self.assertFalse(result['presentation']['sync_clickable'])
        self.assertEqual(before, sorted(p.relative_to(self.root).as_posix()
                                        for p in self.root.rglob('*')))

    def test_existing_equal_target_keeps_unknown_owner_and_does_not_write(self):
        target = self.package('targets/demo')
        before = (target / 'SKILL.md').read_bytes()
        result = self.check()
        self.assertEqual(result['preview']['action'], 'conflict')
        self.assertIn('OWNER_UNKNOWN', result['preview']['reason_codes'])
        self.assertEqual(result['preview']['owner'], 'unknown')
        self.assertEqual(before, (target / 'SKILL.md').read_bytes())
        self.assertIn('保留现有内容', result['presentation']['summary'])

    def test_all_files_and_raw_script_change_reach_gui_result(self):
        scripts = self.root / 'skills/demo/scripts'
        scripts.mkdir()
        (scripts / 'a.py').write_bytes(b'print(1)\r\n')
        before = self.check()['source']
        (scripts / 'a.py').write_bytes(b'print(2)\r\n')
        after = self.check()['source']
        self.assertEqual([item['path'] for item in after['files']],
                         ['SKILL.md', 'scripts/a.py'])
        self.assertNotEqual(before['content_digest'], after['content_digest'])

    def test_skill_script_is_read_as_bytes_and_never_executed(self):
        sentinel = self.root / 'script-ran'
        (self.root / 'skills/demo/run.py').write_text(
            f'from pathlib import Path\nPath({str(sentinel)!r}).write_text("executed")\n',
            encoding='utf-8')
        self.assertIn('run.py', [entry['path'] for entry in self.check()['source']['files']])
        self.assertFalse(sentinel.exists())

    def test_target_appearing_during_source_scan_is_rejected_as_changed(self):
        real_inspect = gui.inspect_package
        def inspect(workspace, relative, **limits):
            result = real_inspect(workspace, relative, **limits)
            if relative == 'skills/demo':
                self.package('targets/demo', b'# User-owned target\n')
            return result
        with patch('agentbus.gui.inspect_package', side_effect=inspect):
            with self.assertRaises(ValueError) as failure:
                self.check()
        self.assertIn('FILE_CHANGED', str(failure.exception))
        self.assertEqual((self.root / 'targets/demo/SKILL.md').read_bytes(),
                         b'# User-owned target\n')

    def test_source_and_missing_target_reject_private_or_escaping_paths(self):
        for relative in ('../outside', '/outside', 'C:/outside',
                         '.workbuddy/demo', '.codex/demo', '不要访问/demo'):
            for field in ('source', 'target'):
                with self.subTest(relative=relative, field=field):
                    values = {'source': 'skills/demo', 'target': 'targets/demo'}
                    values[field] = relative
                    with self.assertRaises((SkillPackageError, ValueError)):
                        self.check(**values)
        self.assertFalse((self.root / 'targets').exists())

    def test_non_package_target_is_an_error_rather_than_absent(self):
        (self.root / 'targets').mkdir()
        (self.root / 'targets/demo').write_bytes(b'user file')
        with self.assertRaises((SkillPackageError, ValueError)):
            self.check()
        self.assertEqual((self.root / 'targets/demo').read_bytes(), b'user file')

    def test_directory_picker_converts_only_existing_public_project_directory(self):
        converter = getattr(gui, 'skill_directory_relative', None)
        self.assertTrue(callable(converter), 'public directory picker validation is missing')
        self.assertEqual(converter(self.root, self.root / 'skills/demo'), 'skills/demo')
        for selected in (self.root.parent, self.root / '.workbuddy/demo',
                         self.root / 'missing'):
            with self.subTest(selected=selected):
                with self.assertRaises(ValueError):
                    converter(self.root, selected)

    def test_source_scan_has_file_count_boundary(self):
        source = self.root / 'skills/demo'
        for index in range(256):
            (source / f'file-{index:03d}').write_bytes(b'x')
        with self.assertRaises(SkillPackageError) as failure:
            self.check()
        self.assertEqual(failure.exception.code, 'TOO_LARGE')

    def test_source_scan_has_directory_count_boundary(self):
        source = self.root / 'skills/demo'
        for index in range(1024):
            (source / f'dir-{index:04d}').mkdir()
        with self.assertRaises(SkillPackageError) as failure:
            self.check()
        self.assertEqual(failure.exception.code, 'TOO_LARGE')

    def test_render_shows_manifest_digest_conflict_and_three_unverified_states(self):
        self.package('targets/demo')
        result = self.check(scope='global', route='desktop')
        renderer = getattr(gui, 'render_skill_check', None)
        self.assertTrue(callable(renderer), 'GUI result rendering is missing')
        text = renderer(result)
        self.assertIn('SKILL.md', text)
        self.assertIn(result['source']['content_digest'], text)
        self.assertIn('OWNER_UNKNOWN', text)
        self.assertIn('维护者未知', text)
        self.assertIn('安装：未验证', text)
        self.assertIn('发现：未验证', text)
        self.assertIn('调用：未验证', text)
        self.assertIn('实际加载未验证', text)
        self.assertIn('1024', text)
        self.assertIn('目录节点', text)
        self.assertIn('单目录条目', text)
        self.assertNotIn('执行版本已固定', text)

    def test_saved_project_gate_prevents_opening_dialog_for_unsaved_workspace(self):
        ui = gui.Launcher.__new__(gui.Launcher)
        ui._workflow_cfg = lambda: None
        method = getattr(ui, '_show_skill_check', None)
        self.assertTrue(callable(method), 'skill dialog entrypoint is missing')
        with patch('agentbus.gui.tk.Toplevel', side_effect=AssertionError('dialog opened')):
            method()

    def test_closed_dialog_with_pending_io_cannot_start_another_worker(self):
        ui = gui.Launcher.__new__(gui.Launcher)
        ui.root = None
        ui._workflow_cfg = lambda: SimpleNamespace(workspace=self.root)
        ui._skill_dialog = SimpleNamespace(closed=True, task=SimpleNamespace(
            worker=SimpleNamespace(is_alive=lambda: True)))
        with patch('agentbus.gui.SkillCheckDialog', side_effect=AssertionError('second worker')), \
                patch('agentbus.gui.messagebox.showwarning') as warning:
            ui._show_skill_check()
        self.assertIn('前一次检查', warning.call_args.args[1])

    def test_poll_after_dialog_close_never_reads_or_updates_destroyed_widgets(self):
        dialog = gui.SkillCheckDialog.__new__(gui.SkillCheckDialog)
        dialog.closed = True
        dialog.after_id = 'old-tk-callback'
        # No task/window/output exists: any access after close is a bug.
        dialog._poll()
        self.assertIsNone(dialog.after_id)


class SkillCheckTaskTests(unittest.TestCase):
    def task(self, checker, **options):
        cls = getattr(gui, 'SkillCheckTask', None)
        self.assertTrue(callable(cls), 'background queue task is missing')
        return cls(checker=checker, **options)

    def wait_result(self, task):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = task.poll()
            if result:
                return result
            threading.Event().wait(0.005)
        self.fail('worker did not return a queued result')

    def test_check_runs_off_caller_thread_and_returns_real_result_through_queue(self):
        caller = threading.get_ident()
        def check(**request):
            return {'thread': threading.get_ident(), 'source': request['source']}
        task = self.task(check)
        self.addCleanup(task.close)
        self.assertTrue(task.start(source='skills/demo'))
        event = self.wait_result(task)
        self.assertEqual(event['kind'], 'result')
        self.assertNotEqual(event['value']['thread'], caller)
        self.assertEqual(event['value']['source'], 'skills/demo')

    def test_error_reaches_queue_with_explicit_code(self):
        def check(**request):
            raise SkillPackageError('NO_SKILL_ENTRY', 'package needs SKILL.md')
        task = self.task(check)
        self.addCleanup(task.close)
        task.start(source='skills/demo')
        event = self.wait_result(task)
        self.assertEqual(event['kind'], 'error')
        self.assertIn('NO_SKILL_ENTRY', event['message'])
        self.assertIn('SKILL.md', event['message'])

    def test_pending_check_does_not_allow_duplicate_workers(self):
        release = threading.Event()
        entered = threading.Event()
        def check(**request):
            entered.set()
            release.wait(2)
            return {'ok': True}
        task = self.task(check)
        self.addCleanup(release.set)
        self.addCleanup(task.close)
        self.assertTrue(task.start(source='skills/demo'))
        self.assertTrue(entered.wait(1))
        self.assertFalse(task.start(source='skills/another'))
        release.set()
        self.assertEqual(self.wait_result(task)['kind'], 'result')

    def test_timeout_is_reported_once_and_late_result_is_discarded(self):
        release = threading.Event()
        entered = threading.Event()
        def check(**request):
            entered.set()
            release.wait(2)
            return {'should_not_display': True}
        task = self.task(check, timeout_s=0.01)
        self.addCleanup(release.set)
        self.addCleanup(task.close)
        task.start(source='skills/demo')
        self.assertTrue(entered.wait(1))
        threading.Event().wait(0.02)
        self.assertEqual(task.poll()['kind'], 'timeout')
        self.assertIsNone(task.poll())
        self.assertFalse(task.start(source='skills/another'))
        release.set()
        task.worker.join(1)
        self.assertIsNone(task.poll())

    def test_close_discards_late_completion_and_disallows_restart(self):
        release = threading.Event()
        entered = threading.Event()
        def check(**request):
            entered.set()
            release.wait(2)
            return {'should_not_display': True}
        task = self.task(check)
        self.addCleanup(release.set)
        task.start(source='skills/demo')
        self.assertTrue(entered.wait(1))
        task.close()
        release.set()
        task.worker.join(1)
        self.assertIsNone(task.poll())
        self.assertFalse(task.start(source='skills/another'))


class SkillGuiLayoutTests(unittest.TestCase):
    """Real Tk layouts, including restored ordinary windows; not EXE acceptance."""
    ACTIONS = ('检查安装', '检查当前团队', '保存诊断', '技能检查',
               '安装 / 修复工作流', '预览移除', '移除已安装工作流', '恢复中断任务')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = gui.tk.Tk()
        self.addCleanup(self.destroy_root)
        self.callback_errors = []
        self.root.report_callback_exception = lambda *args: self.callback_errors.append(args)
        self.root.tk.call('tk', 'scaling', 2.0)
        with patch('agentbus.gui.initial_workspace', return_value=self.temp.name), \
             patch('agentbus.gui.detect_group_agents', return_value={}), \
             patch.object(gui.Launcher, '_auto_detect'):
            self.ui = gui.Launcher(self.root)
        self.root.state('normal')
        self.root.geometry('1180x760')
        self.ui.tabs.select(self.ui.management_page)
        nested = next(child for child in self.ui.management_page.winfo_children()
                      if isinstance(child, gui.ttk.Notebook))
        page_id = next(tab for tab in nested.tabs() if nested.tab(tab, 'text') == '维护')
        nested.select(page_id)
        self.page = self.root.nametowidget(page_id)
        self.root.update()

    def destroy_root(self):
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.assertEqual(self.callback_errors, [], 'Tk callback exceptions must fail layout validation')

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def buttons(self):
        return {child.cget('text'): child for child in self.descendants(self.page)
                if isinstance(child, gui.ttk.Button)}

    def fully_visible(self, widget, viewport):
        return (widget.winfo_ismapped()
                and widget.winfo_height() >= widget.winfo_reqheight()
                and widget.winfo_rooty() >= viewport.winfo_rooty()
                and widget.winfo_rooty() + widget.winfo_height()
                    <= viewport.winfo_rooty() + viewport.winfo_height())

    def test_restored_high_dpi_maintenance_actions_are_reachable_by_local_scrolling(self):
        self.root.geometry('1150x620')
        self.root.update()
        self.assertEqual(self.root.state(), 'normal')
        canvas = getattr(self.ui, 'maintenance_canvas', None)
        self.assertIsNotNone(canvas, 'ordinary window needs a local maintenance scroll fallback')
        self.assertLess(canvas.yview()[1], 1, 'fixture must actually constrain the content')
        buttons = self.buttons()
        self.assertTrue(set(self.ACTIONS).issubset(buttons))
        for label in self.ACTIONS:
            reachable = False
            for step in range(41):
                canvas.yview_moveto(step / 40)
                self.root.update()
                if self.fully_visible(buttons[label], canvas):
                    reachable = True
                    break
            self.assertTrue(reachable, f'{label} cannot be reached in normal window')
        status = next(child for child in self.descendants(self.root)
                      if child.winfo_class() == 'TLabel'
                      and str(child.cget('textvariable')) == str(self.ui.status_var))
        self.assertTrue(self.fully_visible(status, self.root))

    def test_wheel_over_maintenance_action_reveals_clipped_controls_without_project_write(self):
        self.root.geometry('1150x620')
        self.root.update()
        canvas = getattr(self.ui, 'maintenance_canvas', None)
        self.assertIsNotNone(canvas, 'maintenance scrolling is missing')
        # The redesigned ordinary window now fits the action rows. Constrain
        # only this fixture's local viewport to exercise the wheel fallback;
        # do not claim clipping in the real ordinary window.
        canvas.grid_configure(sticky='new')
        canvas.configure(height=100)
        buttons = self.buttons()
        canvas.yview_moveto(0)
        self.root.update()
        recovery = buttons['恢复中断任务']
        self.assertFalse(self.fully_visible(recovery, canvas),
                         'fixture must initially clip the last control row')
        before = sorted(p.relative_to(self.temp.name).as_posix()
                        for p in Path(self.temp.name).rglob('*'))
        for _ in range(40):
            buttons['技能检查'].event_generate('<MouseWheel>', delta=-120)
            self.root.update()
            if self.fully_visible(recovery, canvas):
                break
        self.assertTrue(self.fully_visible(recovery, canvas))
        self.assertEqual(before, sorted(p.relative_to(self.temp.name).as_posix()
                                         for p in Path(self.temp.name).rglob('*')))

    def test_large_window_keeps_existing_maintenance_actions_visible_at_top(self):
        canvas = getattr(self.ui, 'maintenance_canvas', None)
        self.assertIsNotNone(canvas, 'maintenance scrolling is missing')
        if gui.sys.platform == 'win32':
            self.root.state('zoomed')
        else:
            self.root.geometry('1400x1000')
        self.root.update()
        canvas.yview_moveto(0)
        self.root.update()
        buttons = self.buttons()
        self.assertTrue(set(self.ACTIONS).issubset(buttons))
        for label in self.ACTIONS:
            self.assertTrue(self.fully_visible(buttons[label], canvas), label)


if __name__ == '__main__':
    unittest.main()
