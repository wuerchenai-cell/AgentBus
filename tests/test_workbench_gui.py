"""Workbench layout contracts, Codex internal integration, 2026-10-05.

These tests use a withdrawn Tk window. They do not certify frozen EXE
visibility, vendor sessions, or unattended execution.
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus import gui


class WorkbenchContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = gui.tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.cleanup_root)
        with patch('agentbus.gui.configure_initial_window'), \
             patch('agentbus.gui.initial_workspace', return_value=self.temp.name), \
             patch.object(gui.Launcher, '_auto_detect'):
            self.ui = gui.Launcher(self.root)
        self.root.withdraw()
        self.root.update_idletasks()

    def cleanup_root(self):
        self.ui._stop_watchtower() if hasattr(self, 'ui') else None
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def test_six_stable_pages_start_at_home_with_one_selected_navigation(self):
        self.assertEqual(list(self.ui.pages),
                         ['home', 'team', 'tasks', 'access', 'settings', 'logs'])
        self.assertEqual(set(self.ui.nav_buttons), set(self.ui.pages))
        self.assertEqual(self.ui.current_page, 'home')
        selected = [key for key, button in self.ui.nav_buttons.items()
                    if button.cget('style') == 'NavSelected.TButton']
        self.assertEqual(selected, ['home'])
        self.assertEqual(self.ui.tabs.select(), str(self.ui.pages['home']))

    def test_navigation_preserves_uncommitted_team_and_goal_widgets(self):
        originals = dict(self.ui.pages)
        original_text = self.ui.goal_text
        self.ui.ws_var.set('draft project')
        self.ui.director_var.set('draft-leader')
        self.ui.goal_text.insert('1.0', 'goal that must survive navigation')
        for name in self.ui.pages:
            self.ui._show_page(name)
            self.assertEqual(self.ui.current_page, name)
        self.assertEqual(self.ui.pages, originals)
        self.assertIs(self.ui.goal_text, original_text)
        self.assertEqual(self.ui.ws_var.get(), 'draft project')
        self.assertEqual(self.ui.director_var.get(), 'draft-leader')
        self.assertIn('goal that must survive navigation',
                      self.ui.goal_text.get('1.0', 'end'))

    def test_configuration_is_owned_by_team_not_a_persistent_root_card(self):
        node = self.ui.workers_canvas
        while node is not self.ui.pages['team'] and node is not self.root:
            node = node.master
        self.assertIs(node, self.ui.pages['team'])
        self.assertIs(self.ui.connection_page, self.ui.pages['access'])
        self.assertIs(self.ui.management_page, self.ui.pages['settings'])

    def test_page_selection_alone_never_creates_project_configuration(self):
        before = sorted(Path(self.temp.name).rglob('*'))
        for name in self.ui.pages:
            self.ui._show_page(name)
        self.assertEqual(sorted(Path(self.temp.name).rglob('*')), before)
        self.assertFalse((Path(self.temp.name) / '.agentbus').exists())

    def test_missing_evidence_is_not_green_online(self):
        self.ui._refresh_workbench_overview()
        rows = [self.ui.overview_tree.item(item, 'values')
                for item in self.ui.overview_tree.get_children()]
        self.assertFalse(any('在线' == str(value) for row in rows for value in row))
        self.assertNotIn('已连接', self.ui.overview_summary_var.get())


class RetainedCallbackContracts(unittest.TestCase):
    def test_all_existing_user_functions_keep_real_button_bindings(self):
        tree = ast.parse(Path(gui.__file__).read_text(encoding='utf-8'))
        bindings = {ast.unparse(keyword.value)
                    for node in ast.walk(tree) if isinstance(node, ast.Call)
                    for keyword in node.keywords if keyword.arg == 'command'}
        required = {'_save_group', '_add_member', '_pick_ws', '_open_ws', '_start',
                    '_show_usage_guide', '_copy_connection', '_refresh_connections',
                    '_show_recent_receipt', '_prepare_workflow', '_detect',
                    '_import_team', '_export_team', '_open_opencode_records',
                    '_refresh_project_plan', '_refresh_handoff', '_copy_handoff',
                    '_refresh_esc', '_gen_mcp', '_copy_mcp', '_check_mcp',
                    '_copy_resume', '_inspect_workflows', '_inspect_project',
                    '_save_diagnostic', '_show_skill_check', '_install_codex_skill',
                    '_preview_uninstall_workflows', '_uninstall_workflows',
                    '_open_recovery_center'}
        self.assertTrue({f'self.{name}' for name in required}.issubset(bindings),
                        sorted({f'self.{name}' for name in required} - bindings))


class WorkbenchVisibleLayouts(unittest.TestCase):
    """Four actual Tk size/scale fixtures; frozen UI is checked separately."""
    def exercise(self, geometry, scaling):
        with tempfile.TemporaryDirectory() as temp:
            root = gui.tk.Tk()
            errors = []
            root.report_callback_exception = lambda *args: errors.append(args)
            try:
                root.tk.call('tk', 'scaling', scaling)
                with patch('agentbus.gui.initial_workspace', return_value=temp), \
                     patch.object(gui.Launcher, '_auto_detect'):
                    ui = gui.Launcher(root)
                root.state('normal')
                root.geometry(geometry)
                root.update()
                def visible(widget):
                    self.assertTrue(widget.winfo_ismapped())
                    # Flexible page frames intentionally give space to their
                    # scrollable children; only controls need full request height.
                    if isinstance(widget, gui.tk.Canvas):
                        self.assertGreaterEqual(widget.winfo_height(), 60)
                    elif not isinstance(widget, gui.ttk.Frame):
                        self.assertGreaterEqual(widget.winfo_height(), widget.winfo_reqheight())
                    else:
                        self.assertGreater(widget.winfo_height(), 80)
                    self.assertGreaterEqual(widget.winfo_rootx(), root.winfo_rootx())
                    self.assertGreaterEqual(widget.winfo_rooty(), root.winfo_rooty())
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                         root.winfo_rootx() + root.winfo_width())
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                         root.winfo_rooty() + root.winfo_height())
                for page in ui.pages:
                    ui._show_page(page)
                    root.update()
                    visible(ui.nav_buttons[page])
                    visible(ui.pages[page])
                ui._show_page('home')
                root.update()
                visible(ui.start_btn)
                visible(ui.overview_scroll)
                ui._show_page('team')
                root.update()
                visible(ui.director_box)
                visible(ui.workers_canvas)
                def descendants(widget):
                    for child in widget.winfo_children():
                        yield child
                        yield from descendants(child)
                required = {'保存团队', '检测本机入口', '添加 Agent', '导入成员模板'}
                actions = {widget.cget('text'): widget
                           for widget in descendants(ui.pages['team'])
                           if isinstance(widget, gui.ttk.Button)}
                self.assertTrue(required.issubset(actions))
                for label in required:
                    visible(actions[label])
                ui._show_page('access')
                notebook = next(n for n, item in ui._subnavigation.items()
                                if 'skills' in item['pages'])
                ui._show_subpage(notebook, 'skills')
                root.update()
                skill_page = ui._subnavigation[notebook]['pages']['skills']
                skill_button = next(widget for widget in descendants(skill_page)
                                    if isinstance(widget, gui.ttk.Button)
                                    and widget.cget('text') == '技能检查')
                visible(skill_button)
                self.assertEqual(gui.FONT[1], gui.TYPE_SCALE['body'])
                self.assertGreater(gui.FONT[1], 0, 'Fonts use points, not doubled pixel scaling')
                self.assertEqual(errors, [])
                self.assertFalse((Path(temp) / '.agentbus').exists())
            finally:
                for callback in root.tk.call('after', 'info'):
                    root.after_cancel(callback)
                root.destroy()
    def test_1150_760_at_100_percent(self):
        self.exercise('1150x760', 96 / 72)
    def test_1366_768_at_100_percent(self):
        self.exercise('1366x768', 96 / 72)
    def test_1150_760_at_150_percent(self):
        self.exercise('1150x760', 2.0)
    def test_1366_768_at_150_percent(self):
        self.exercise('1366x768', 2.0)


if __name__ == '__main__':
    unittest.main()
