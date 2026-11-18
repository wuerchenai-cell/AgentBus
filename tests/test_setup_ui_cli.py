"""Source: isolated install/use/remove acceptance, Codex, 2026-10-03."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agentbus import cli, gui


class SetupInterfaceTests(unittest.TestCase):
    def test_removed_host_is_not_still_shown_installed(self):
        text = gui.render_maintenance({'status': 'uninstalled', 'applied': True,
            'removed': ['owned skill'], 'hosts': {'codex': {'skill': 'owned skill', 'status': 'installed'}}})
        self.assertIn('codex：已移除', text)
        self.assertNotIn('codex：文件已安装', text)

    def test_actual_waiting_handoff_state_is_translated(self):
        text = gui.render_maintenance({'status': 'healthy',
            'jobs': [{'id': 'j-test', 'state': 'waiting_handoff'}]})
        self.assertIn('作业 j-test：等待对话接手', text)

    def test_gui_project_check_reloads_disk_configuration(self):
        app = gui.Launcher.__new__(gui.Launcher)
        app._show_maintenance = MagicMock()
        cfg = SimpleNamespace(workspace=Path.cwd(), path=Path.cwd()/'.agentbus/config.json')
        app._workflow_cfg = MagicMock(return_value=cfg)
        with patch('agentbus.gui.load', return_value='fresh cfg') as read, \
             patch('agentbus.gui.inspect_project', return_value={'status': 'healthy'}) as check:
            app._inspect_project()
        read.assert_called_once_with(cfg.workspace)
        check.assert_called_once_with('fresh cfg')

    def test_gui_project_check_shows_config_parse_failure(self):
        app = gui.Launcher.__new__(gui.Launcher)
        app._show_maintenance = MagicMock()
        cfg = SimpleNamespace(workspace=Path.cwd(), path=Path.cwd()/'.agentbus/config.json')
        app._workflow_cfg = MagicMock(return_value=cfg)
        with patch('agentbus.gui.load', side_effect=gui.ConfigError('bad config')), \
             patch('agentbus.gui.messagebox.showerror') as show:
            app._inspect_project()
        show.assert_called_once()
        app._show_maintenance.assert_not_called()

    def test_maintenance_summary_labels_modified_file_and_preview(self):
        text = gui.render_maintenance({'status': 'partial', 'applied': False,
            'removed': [], 'preserved': ['my file'],
            'hosts': {'codex': {'status': 'modified', 'skill': 'my file'}}})
        self.assertIn('用户修改，予以保留', text)
        self.assertIn('尚未删除任何文件', text)
        self.assertIn('不代表各软件已加载', text)

    def test_maintenance_health_identifies_peer_and_translates_job_state(self):
        text = gui.render_maintenance({'status': 'healthy',
            'checks': [{'code': 'member:trae', 'message': '需桌面对话接手'}],
            'jobs': [{'id': 'j-test', 'state': 'awaiting_summary'}]})
        self.assertIn('trae：需桌面对话接手', text)
        self.assertIn('回执已到，等待验收汇总', text)

    def command(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_cli_uninstall_is_preview_unless_apply_explicit(self):
        with patch('agentbus.cli.uninstall_workflows', return_value={'status': 'preview'}) as call:
            code, output = self.command(['setup', 'uninstall', '--home', 'isolated user'])
        self.assertEqual(code, 0)
        self.assertIn('preview', output)
        self.assertFalse(call.call_args.kwargs['apply'])
        self.assertEqual(str(call.call_args.kwargs['home']), 'isolated user')

    def test_cli_apply_and_partial_report_preserved_files(self):
        with patch('agentbus.cli.uninstall_workflows', return_value={'status': 'partial'}) as call:
            code, _ = self.command(['setup', 'uninstall', '--apply'])
        self.assertEqual(code, 1)
        self.assertTrue(call.call_args.kwargs['apply'])

    def test_cli_status_does_not_install(self):
        with patch('agentbus.cli.inspect_workflows', return_value={'status': 'not_installed'}) as read, \
             patch('agentbus.cli.install_workflows') as write:
            code, _ = self.command(['setup', 'status'])
        self.assertEqual(code, 0)
        read.assert_called_once()
        write.assert_not_called()

    def test_cli_check_is_read_only_and_reports_attention(self):
        with patch('agentbus.cli.inspect_project', return_value={'status': 'attention', 'model_calls': 0}) as read, \
             patch('agentbus.cli._load', return_value='cfg'), \
             patch('agentbus.cli.install_workflows') as write:
            code, output = self.command(['setup', 'check', '--workspace', 'current project'])
        self.assertEqual(code, 1)
        self.assertIn('model_calls', output)
        read.assert_called_once_with('cfg')
        write.assert_not_called()

    def test_cli_install_reports_no_native_discovery_certificate(self):
        with patch('agentbus.cli.install_workflows', return_value={
                'status': 'installed', 'native_verified': False}) as call:
            code, output = self.command(['setup', 'install'])
        self.assertEqual(code, 0)
        self.assertIn('false', output)
        call.assert_called_once()

    def test_gui_preview_does_not_uninstall(self):
        app = gui.Launcher.__new__(gui.Launcher)
        app.maintenance_box = MagicMock()
        with patch('agentbus.gui.uninstall_workflows', return_value={'status': 'preview'}) as call:
            app._preview_uninstall_workflows()
        self.assertFalse(call.call_args.kwargs.get('apply', False))
        app.maintenance_box.insert.assert_called_once()

    def test_gui_cancel_preserves_files(self):
        app = gui.Launcher.__new__(gui.Launcher)
        app.maintenance_box = MagicMock()
        with patch('agentbus.gui.uninstall_workflows', return_value={
                'status': 'preview', 'removable': ['a']}) as call, \
             patch('agentbus.gui.messagebox.askyesno', return_value=False):
            app._uninstall_workflows()
        self.assertEqual(call.call_count, 1)
        self.assertFalse(call.call_args.kwargs.get('apply', False))

    def test_gui_invalid_manifest_does_not_offer_apply(self):
        app = gui.Launcher.__new__(gui.Launcher)
        app.maintenance_box = MagicMock()
        with patch('agentbus.gui.uninstall_workflows', return_value={
                'status': 'invalid', 'errors': ['invalid ownership']}) as call, \
             patch('agentbus.gui.messagebox.askyesno') as ask:
            app._uninstall_workflows()
        self.assertEqual(call.call_count, 1)
        ask.assert_not_called()


if __name__ == '__main__':
    unittest.main()
