"""Web desktop routing and release boundaries; no accounts or model calls.

Source: Codex, 2026-10-09. Real WebView2 is exercised by gui --smoke.
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class WebDesktopTests(unittest.TestCase):
    def api(self):
        return importlib.import_module('agentbus.web_desktop')

    def write_assets(self, base):
        assets = base / 'agentbus/web_assets'
        assets.mkdir(parents=True)
        for name in ('index.html', 'app.css', 'app.js', 'trigger_onboarding.js'):
            (assets / name).write_text('public asset', encoding='utf-8')
        return assets

    def test_app_icon_resolves_from_source_and_frozen_assets(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            icon = base / 'agentbus/assets/agentbus.ico'
            icon.parent.mkdir(parents=True)
            icon.write_bytes(b'icon')
            with patch.object(api.sys, '_MEIPASS', str(base), create=True):
                self.assertEqual(api.app_icon_path(), icon)
                icon.unlink()
                with self.assertRaisesRegex(api.DesktopStartupError, 'application icon'):
                    api.app_icon_path()

    def test_native_window_start_uses_the_packaged_application_icon(self):
        api = self.api()

        class Signal:
            def __init__(self):
                self.callbacks = []

            def __iadd__(self, callback):
                self.callbacks.append(callback)
                return self

        class Window:
            def __init__(self):
                self.events = SimpleNamespace(initialized=Signal(), before_show=Signal(),
                                              loaded=Signal(), closed=Signal())
                self.real_url = 'http://127.0.0.1:43210/index.html'

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            icon = base / 'agentbus/assets/agentbus.ico'
            icon.parent.mkdir(parents=True)
            icon.write_bytes(b'icon')
            window = Window()
            fake_view = SimpleNamespace(settings={}, create_window=lambda *a, **k: window)

            def start(**kwargs):
                self.assertEqual(kwargs['icon'], str(icon))
                for callback in window.events.initialized.callbacks:
                    callback('edgechromium')
                for callback in window.events.before_show.callbacks:
                    callback()
                for callback in window.events.closed.callbacks:
                    callback()

            fake_view.start = start
            with patch.object(api, 'runtime_diagnostics', return_value={
                    'platform': 'win32', 'webview2_installed': True, 'guidance': ''}), \
                 patch.object(api, 'asset_directory', return_value=base), \
                 patch.object(api, 'app_icon_path', return_value=icon), \
                 patch('agentbus.web_bridge.WebBridge', return_value=SimpleNamespace(
                     bind_window=lambda _window: None)), \
                 patch.object(api, '_guard_navigation', side_effect=lambda _window, state:
                              state.update(navigation_guard_installed=True)), \
                 patch.dict(sys.modules, {'webview': fake_view}):
                report = {}
                self.assertEqual(api._launch(None, smoke=False, timeout_s=1, report=report), 0)
        self.assertTrue(report['success'])

    def test_workspace_drop_passes_one_folder_through_the_validated_bridge(self):
        api = self.api()

        class Signal:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        class Handler:
            def __init__(self, callback, *args, **kwargs):
                self.callback = callback

        class Bridge:
            def __init__(self):
                self.calls = []

            def call(self, action, payload):
                self.calls.append((action, payload))
                return {'ok': True, 'data': {'project': {'workspace': payload['workspace']}}}

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'project'
            folder.mkdir()
            events = SimpleNamespace(dragenter=Signal(), dragover=Signal(), drop=Signal())
            scripts = []
            window = SimpleNamespace(
                dom=SimpleNamespace(document=SimpleNamespace(events=events)),
                evaluate_js=lambda script: scripts.append(script))
            bridge = Bridge()
            report = {}
            fake_dom = SimpleNamespace(DOMEventHandler=Handler)
            with patch.dict(sys.modules, {'webview.dom': fake_dom}):
                self.assertTrue(api._bind_workspace_drop(window, bridge, report))
                handler = events.drop.handlers[0]
                handler.callback({'domTransfer': {'files': [
                    {'pywebviewFullPath': str(folder)}]}})
                handler.callback({'domTransfer': {'files': [
                    {'pywebviewFullPath': str(folder)},
                    {'pywebviewFullPath': str(folder)}]}})
        self.assertEqual(bridge.calls, [('set_workspace', {'workspace': str(folder)})])
        self.assertEqual(scripts, [
            'window.AgentBusUI?.workspaceDropped?.()',
            'window.AgentBusUI?.workspaceDropRejected?.()'])
        self.assertTrue(report['workspace_drop_available'])
        self.assertEqual(report['workspace_drop_count'], 1)

    def test_frozen_assets_resolve_inside_extraction_root(self):
        api = self.api()
        with tempfile.TemporaryDirectory(prefix='web assets 空格 ') as directory:
            base = Path(directory)
            expected = self.write_assets(base)
            with patch.object(api.sys, '_MEIPASS', str(base), create=True):
                self.assertEqual(api.asset_directory(), expected)
            (expected / 'app.js').unlink()
            with patch.object(api.sys, '_MEIPASS', str(base), create=True):
                with self.assertRaisesRegex(api.DesktopStartupError, 'app.js'):
                    api.asset_directory()

    def test_explicit_workspace_wins_and_private_paths_are_rejected(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            explicit = base / 'explicit'
            explicit.mkdir()
            with patch('agentbus.gui_state.load_last_workspace', return_value=str(base / 'other')):
                self.assertEqual(api.select_workspace(str(explicit), cwd=base), explicit)
            with self.assertRaisesRegex(api.DesktopStartupError, 'private'):
                api.select_workspace(str(base / '.workbuddy'), cwd=base)

    def test_default_workspace_uses_cwd_or_registered_valid_project_only(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cwd = base / 'current'
            registered = base / 'registered'
            cwd.mkdir()
            (registered / '.agentbus').mkdir(parents=True)
            (registered / '.agentbus/config.json').write_text('{}')
            with patch('agentbus.gui_state.load_last_workspace', return_value=str(registered)):
                self.assertEqual(api.select_workspace(cwd=cwd), registered)
                (cwd / '.agentbus').mkdir()
                (cwd / '.agentbus/config.json').write_text('{}')
                self.assertEqual(api.select_workspace(cwd=cwd), cwd)
            with patch('agentbus.gui_state.load_last_workspace', return_value=str(base / 'missing')):
                self.assertIsNone(api.select_workspace(cwd=base))

    def test_missing_runtime_diagnostic_is_actionable_without_importing_webview(self):
        api = self.api()
        with patch.object(api, '_runtime_registry_versions', return_value=[]), \
             patch.object(api, '_runtime_directory_versions', return_value=[]), \
             patch.dict(sys.modules, {'webview': None}):
            report = api.runtime_diagnostics(platform_name='win32')
        self.assertFalse(report['webview2_installed'])
        self.assertIn('developer.microsoft.com', report['install_url'])
        self.assertIn('Evergreen', report['guidance'])

    def test_runtime_diagnostic_accepts_nonzero_registry_version(self):
        api = self.api()
        with patch.object(api, '_runtime_registry_versions', return_value=[
                {'version': '0.0.0.0', 'source': 'registry'},
                {'version': '140.0.3485.81', 'source': 'registry'}]), \
             patch.object(api, '_runtime_directory_versions', return_value=[]):
            report = api.runtime_diagnostics(platform_name='win32')
        self.assertTrue(report['webview2_installed'])
        self.assertEqual(report['webview2_version'], '140.0.3485.81')

    def test_cli_help_remains_available_without_webview_or_bridge(self):
        root = Path(__file__).resolve().parents[1]
        code = ('import sys; sys.modules["webview"] = None; '
                'sys.modules["agentbus.web_bridge"] = None; '
                'sys.argv = ["agentbus", "--help"]; '
                'from agentbus.__main__ import run; raise SystemExit(run())')
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', code],
                                cwd=root, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
        self.assertIn('AgentBus', result.stdout.decode('utf-8'))

    def test_bridge_report_counts_formal_forest_nodes_without_copying_ticket_data(self):
        api = self.api()
        summary = api._bridge_summary({'ok': True, 'data': {'forest': {
            'roots': [{'id': 'root'}], 'branches': [{'id': 'branch'}],
            'leaves': [{'id': 'leaf-a'}, {'id': 'leaf-b'}]}}})
        self.assertEqual(summary['node_count'], 4)
        self.assertEqual(summary['leaf_count'], 2)
        self.assertTrue(summary['ok'])

    def test_page_api_exposes_only_call(self):
        api = self.api()
        facade = api._PageAPI(SimpleNamespace())
        self.assertEqual([name for name in dir(facade) if not name.startswith('_')], ['call'])

    def test_page_api_is_disabled_until_local_navigation_guard_is_ready(self):
        api = self.api()

        class Bridge:
            def __init__(self):
                self.calls = 0

            def call(self, action, payload):
                self.calls += 1
                return {'ok': True, 'data': {'calls': self.calls}}

        bridge = Bridge()
        facade = api._PageAPI(bridge)
        self.assertFalse(facade.call('snapshot', {})['ok'])
        self.assertEqual(bridge.calls, 0)
        facade._enabled = True
        self.assertEqual(facade.call('snapshot', {})['data']['calls'], 1)
        facade._enabled = False
        self.assertFalse(facade.call('snapshot', {})['ok'])
        self.assertEqual(bridge.calls, 1)

    def test_navigation_guard_allows_local_fragment_and_cancels_remote_document(self):
        api = self.api()

        class NativeEvent:
            def __iadd__(self, callback):
                self.callback = callback
                return self

        event = NativeEvent()
        window = SimpleNamespace(real_url='http://127.0.0.1:43210/index.html',
                                 native=SimpleNamespace(browser=SimpleNamespace(
                                     webview=SimpleNamespace(NavigationStarting=event))))
        state = {'blocked_navigation_count': 0}
        api._guard_navigation(window, state)
        local = SimpleNamespace(Uri='http://127.0.0.1:43210/index.html#forest', Cancel=False)
        remote = SimpleNamespace(Uri='https://example.invalid/', Cancel=False)
        event.callback(None, local)
        event.callback(None, remote)
        self.assertFalse(local.Cancel)
        self.assertTrue(remote.Cancel)
        self.assertEqual(state['blocked_navigation_count'], 1)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows suspended launch cleanup')
    def test_web_smoke_job_binding_failure_reaps_its_suspended_child(self):
        api = self.api()
        from tools.smoke_frozen import _WindowsJob
        original = subprocess.Popen
        launched = []

        def child(*args, **kwargs):
            process = original([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], **kwargs)
            launched.append(process)
            return process

        def fail_binding(job, process):
            raise OSError('controlled binding failure')

        args = SimpleNamespace(timeout=1, workspace=None)
        with patch.object(api.subprocess, 'Popen', side_effect=child), \
             patch.object(_WindowsJob, 'attach_and_resume', fail_binding):
            try:
                with self.assertRaisesRegex(OSError, 'controlled binding failure'):
                    api._bounded_smoke(args)
            finally:
                # A failing test must not leak the intentionally suspended child.
                for process in launched:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].poll())

    def test_source_gui_arguments_route_to_web_desktop(self):
        from agentbus.__main__ import main
        fake_sys = SimpleNamespace(argv=['agentbus', 'gui', '--workspace', 'chosen', '--smoke'])
        with patch('agentbus.__main__.sys', fake_sys), \
             patch('agentbus.web_desktop.main', return_value=0) as desktop:
            self.assertEqual(main(), 0)
        desktop.assert_called_once_with(['--workspace', 'chosen', '--smoke'])

    def test_double_click_routes_to_web_desktop(self):
        from agentbus.__main__ import main
        fake_sys = SimpleNamespace(argv=['AgentBus.exe'], frozen=True)
        with patch('agentbus.__main__.sys', fake_sys), \
             patch('agentbus.web_desktop.main', return_value=0) as desktop:
            self.assertEqual(main(), 0)
        desktop.assert_called_once_with([])

    def test_builder_collects_webview_and_embeds_three_assets(self):
        from tools.build_release import build_command
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('entry.py', 'schemas/plan.schema.json',
                         'skills/agentbus-team/SKILL.md', 'share_assets/开始使用.md',
                         'runtime/bin/codex.exe', 'agentbus/web_desktop.py',
                         'LICENSE', 'SOURCE_LICENSE_STATUS.md', 'docs/public/README.md',
                         'docs/public/RELEASE.md', 'docs/public/ECOSYSTEM.md', 'docs/public/SECURITY.md'):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('fixture')
            assets = self.write_assets(root)
            icon = root / 'agentbus/assets/agentbus.ico'
            icon.parent.mkdir(parents=True, exist_ok=True)
            entry = bytes((16, 16, 0, 0, 1, 0, 32, 0)) + (12).to_bytes(4, 'little') + (22).to_bytes(4, 'little')
            icon.write_bytes(b'\x00\x00\x01\x00\x01\x00' + entry + b'fixture-icon')
            command = build_command(root, root / 'build', root / 'dist', root / 'runtime')
            for name in ('index.html', 'app.css', 'app.js', 'trigger_onboarding.js'):
                self.assertIn(str(assets / name) + ';agentbus/web_assets', command)
            self.assertIn('--icon', command)
            self.assertIn(str(icon) + ';agentbus/assets', command)
            for module in ('webview', 'pythonnet', 'clr_loader'):
                self.assertIn(module, [command[index + 1] for index, value in enumerate(command)
                                      if value == '--collect-all'])
            (assets / 'index.html').unlink()
            with self.assertRaisesRegex(ValueError, 'Web'):
                build_command(root, root / 'build', root / 'dist', root / 'runtime')

    def test_builder_rejects_unsupported_python_before_freezing_web_desktop(self):
        from tools import build_release
        with patch.object(build_release.sys, 'version_info', (3, 14, 7)):
            with self.assertRaisesRegex(ValueError, 'Python 3.12'):
                build_release.validate_web_build_environment()


if __name__ == '__main__':
    unittest.main()
