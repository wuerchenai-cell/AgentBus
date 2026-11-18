"""Source: Codex, 2026-10-03. Portable release tools; no models or accounts."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class PortableReleaseTests(unittest.TestCase):
    def builder(self):
        path = ROOT / 'tools/build_release.py'
        self.assertTrue(path.is_file(), 'portable release builder is missing')
        spec = importlib.util.spec_from_file_location('release_builder_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self, root):
        for rel in ['entry.py', 'schemas/plan.schema.json',
                    'skills/agentbus-team/SKILL.md', 'share_assets/开始使用.md',
                    'runtime/bin/codex.exe', 'LICENSE', 'SOURCE_LICENSE_STATUS.md',
                    'docs/public/README.md', 'docs/public/RELEASE.md',
                    'docs/public/ECOSYSTEM.md', 'docs/public/SECURITY.md']:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('fixture', encoding='utf-8')

    def test_offline_test_environment_requires_explicit_valid_project_root(self):
        api = self.builder()
        key = 'AGENTBUS_TEST_PROJECT_ROOT'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.agentbus').mkdir()
            (root / '.agentbus/config.json').write_text('{}', encoding='utf-8')
            env = api.build_offline_test_env(root, {key: 'stale-value', 'KEEP': 'yes'})
            self.assertEqual(str(root.resolve()), env[key])
            self.assertEqual('yes', env['KEEP'])
            portable = api.build_offline_test_env(None, {key: 'stale-value', 'KEEP': 'yes'})
            self.assertNotIn(key, portable)
            self.assertEqual('yes', portable['KEEP'])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, '--project-root'):
                api.build_offline_test_env(Path(temp), {})

    def test_build_command_preserves_paths_and_includes_share_assets(self):
        api = self.builder()
        with tempfile.TemporaryDirectory(prefix='release 空格 ') as d:
            root = Path(d)
            self.fixture(root)
            argv = api.build_command(root, root / '.release-build', root / 'dist', root / 'runtime')
            self.assertEqual(argv[:3], [sys.executable, '-m', 'PyInstaller'])
            self.assertIn(str(root / 'entry.py'), argv)
            self.assertIn(str(root / 'share_assets') + ';share_assets', argv)
            self.assertIn(str(root / 'runtime') + ';codex_cli_bin', argv)
            self.assertIn(str(root / 'LICENSE') + ';.', argv)
            self.assertIn(str(root / 'SOURCE_LICENSE_STATUS.md') + ';.', argv)
            self.assertIn('tools.export_source',argv)
            for name in ('README.md','RELEASE.md','ECOSYSTEM.md','SECURITY.md'):
                self.assertIn(str(root/'docs/public'/name)+';docs/public',argv)

    def test_web_desktop_build_embeds_explorer_and_window_icons(self):
        api = self.builder()
        with tempfile.TemporaryDirectory(prefix='release icon ') as directory:
            root = Path(directory)
            self.fixture(root)
            (root / 'agentbus/web_desktop.py').parent.mkdir(parents=True, exist_ok=True)
            (root / 'agentbus/web_desktop.py').write_text('# desktop', encoding='utf-8')
            for relative in ('agentbus/web_assets/index.html', 'agentbus/web_assets/app.css',
                             'agentbus/web_assets/app.js'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('asset', encoding='utf-8')
            icon = root / 'agentbus/assets/agentbus.ico'
            icon.parent.mkdir(parents=True, exist_ok=True)
            icon.write_bytes(b'\x00\x00\x01\x00\x01\x00'
                             + bytes((16, 16, 0, 0, 1, 0, 32, 0))
                             + (12).to_bytes(4, 'little') + (22).to_bytes(4, 'little')
                             + b'fixture-icon')
            argv = api.build_command(root, root / '.release-build', root / 'dist', root / 'runtime')
            self.assertIn('--icon', argv)
            self.assertEqual(argv[argv.index('--icon') + 1], str(icon))
            self.assertIn(str(icon) + ';agentbus/assets', argv)

    def test_web_desktop_build_rejects_missing_application_icon(self):
        api = self.builder()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / 'agentbus/web_desktop.py').parent.mkdir(parents=True, exist_ok=True)
            (root / 'agentbus/web_desktop.py').write_text('# desktop', encoding='utf-8')
            for relative in ('agentbus/web_assets/index.html', 'agentbus/web_assets/app.css',
                             'agentbus/web_assets/app.js'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('asset', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'application icon'):
                api.build_command(root, root / '.release-build', root / 'dist', root / 'runtime')

    def test_build_command_rejects_output_outside_source_and_root_overwrite(self):
        api = self.builder()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'source'
            self.fixture(root)
            for dist in [Path(d) / 'other', root]:
                with self.subTest(dist=dist), self.assertRaises(ValueError):
                    api.build_command(root, root / '.release-build', dist, root / 'runtime')

    def test_missing_codex_runtime_fails_before_build(self):
        api = self.builder()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            (root / 'runtime/bin/codex.exe').unlink()
            with self.assertRaises(ValueError):
                api.build_command(root, root / '.release-build', root / 'dist', root / 'runtime')

    def asset_fixture(self, root):
        from tools.export_source import LICENSE_NAMES
        assets = root / 'share_assets'
        (assets / 'licenses').mkdir(parents=True)
        for name in ['开始使用.md', 'THIRD_PARTY_NOTICES.md']:
            (assets / name).write_text('fixture', encoding='utf-8')
        entries = []
        for name in sorted(LICENSE_NAMES):
            (assets / 'licenses' / name).write_bytes(b'license fixture')
            entries.append({'path': 'licenses/' + name, 'component': 'test', 'version': '1',
                            'source': 'controlled test fixture',
                            'sha256': hashlib.sha256(b'license fixture').hexdigest()})
        (assets / 'licenses/provenance.json').write_text(json.dumps({
            'schema_version': 1, 'collected_at': '2026-10-03', 'coverage': 'fixture',
            'files': entries}), encoding='utf-8')

    def test_builder_validates_the_actual_exporter_license_contract(self):
        api = self.builder()
        self.assertTrue(callable(getattr(api, 'validate_share_assets', None)))
        with tempfile.TemporaryDirectory() as d:
            self.asset_fixture(Path(d))
            api.validate_share_assets(Path(d))

    def test_builder_rejects_stray_assets_and_modified_license(self):
        api = self.builder()
        self.assertTrue(callable(getattr(api, 'validate_share_assets', None)))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.asset_fixture(root)
            unexpected = root / 'share_assets/private-note.txt'
            unexpected.write_text('do not distribute', encoding='utf-8')
            with self.assertRaises(ValueError): api.validate_share_assets(root)
            unexpected.unlink()
            (root / 'share_assets/licenses/codex-LICENSE.txt').write_bytes(b'changed')
            with self.assertRaises(ValueError): api.validate_share_assets(root)

    def test_offline_runner_works_from_an_unrelated_directory(self):
        runner = ROOT / 'tools/check_offline.py'
        self.assertTrue(runner.is_file(), 'credential-free offline runner is missing')
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(runner),
                                     '--pattern', 'test_lease_startup.py'], cwd=d,
                                    capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
            self.assertIn('"model_calls": 0', result.stdout.decode('utf-8'))

    def test_offline_runner_child_module_imports_from_unrelated_directory(self):
        # This suite launches python -m agentbus. Updating only sys.path in
        # the parent runner cannot make the child import its source package.
        runner = ROOT / 'tools/check_offline.py'
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(runner),
                                     '--pattern', 'test_audit_observability.py'], cwd=d,
                                    capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
            summary = json.loads(result.stdout.decode('utf-8').strip().splitlines()[-1])
            self.assertEqual(summary['failures'], 0)
            self.assertGreater(summary['tests'], 1)

    def smoke_module(self):
        spec = importlib.util.spec_from_file_location('release_smoke_test', ROOT / 'tools/smoke_frozen.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_mcp_smoke_has_deadline_and_reaps_a_nonresponding_process(self):
        # A pipe held open by a live child used to block readline forever.
        api = self.smoke_module()
        self.assertTrue(callable(getattr(api, 'smoke', None)), 'bounded MCP smoke API missing')
        launched = []
        original = subprocess.Popen

        def silent_child(*args, **kwargs):
            if args[0][0] == 'taskkill':
                return original(*args, **kwargs)
            proc = original([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], **kwargs)
            launched.append(proc)
            return proc

        with patch.object(api.subprocess, 'Popen', side_effect=silent_child):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                api.smoke(Path(sys.executable), timeout_s=0.2)
        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].poll(), 'MCP timeout left its child running')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows inherited pipe/Job Object regression')
    def test_mcp_deadline_when_parent_exits_and_descendant_holds_pipes(self):
        api = self.smoke_module()
        original = subprocess.Popen
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'owned-child.pid'
            code = ('import subprocess,sys,pathlib; '
                    'p=subprocess.Popen([sys.executable,"-B","-c","import time; time.sleep(60)"]); '
                    'pathlib.Path(sys.argv[1]).write_text(str(p.pid)); sys.exit(0)')

            def inherited_child(*args, **kwargs):
                if args[0][0] == 'taskkill':
                    return original(*args, **kwargs)
                return original([sys.executable, '-B', '-c', code, str(pidfile)], **kwargs)

            def check():
                try:
                    api.smoke(Path(sys.executable), timeout_s=0.5)
                except Exception as exc:
                    errors.append(exc)

            with patch.object(api.subprocess, 'Popen', side_effect=inherited_child):
                thread = threading.Thread(target=check, daemon=True)
                thread.start()
                thread.join(2)
                missed_deadline = thread.is_alive()
                # The red test must clean its own descendant before asserting.
                if missed_deadline and pidfile.exists():
                    original(['taskkill', '/PID', pidfile.read_text(), '/T', '/F'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).wait(timeout=5)
                thread.join(5)
            self.assertFalse(missed_deadline, 'MCP deadline failed after its parent exited')
            self.assertTrue(pidfile.exists(), 'synthetic descendant was not exercised')
            self.assertEqual(len(errors), 1)
            self.assertRegex(str(errors[0]), 'timed out')
            # Query only this owned synthetic PID; no signal or arbitrary kill.
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, int(pidfile.read_text()))
            if handle:
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                try:
                    result = ctypes.c_ulong()
                    self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(result)))
                    self.assertNotEqual(result.value, 259, 'MCP descendant survived its timeout')
                finally:
                    kernel.CloseHandle(handle)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows suspended launch cleanup')
    def test_mcp_job_binding_failures_reap_the_suspended_root(self):
        api = self.smoke_module()
        original = subprocess.Popen
        for assigned in [False, True]:
            launched = []

            def child(*args, **kwargs):
                if args[0][0] == 'taskkill':
                    return original(*args, **kwargs)
                process = original([sys.executable, '-B', '-c', 'import time; time.sleep(60)'], **kwargs)
                launched.append(process)
                return process

            def fail_binding(job, process):
                if assigned:
                    self.assertTrue(job.k.AssignProcessToJobObject(job.handle, int(process._handle)))
                raise OSError('controlled bind failure')

            with self.subTest(assigned=assigned), \
                 patch.object(api.subprocess, 'Popen', side_effect=child), \
                 patch.object(api._WindowsJob, 'attach_and_resume', fail_binding):
                with self.assertRaisesRegex(OSError, 'controlled bind failure'):
                    api.smoke(Path(sys.executable), timeout_s=1)
            self.assertEqual(len(launched), 1)
            self.assertIsNotNone(launched[0].poll(), 'job failure left a suspended root')

    def test_builder_smoke_timeout_preserves_existing_release(self):
        api = self.builder()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'dist/AgentBus.exe'
            output.parent.mkdir()
            output.write_bytes(b'existing release')

            def export_fixture(source, package):
                with zipfile.ZipFile(package, 'w') as archive:
                    archive.writestr('entry.py', '')
                return {'sha256': 'fixture'}

            def fake_run(command, **kwargs):
                if any('smoke_frozen.py' in str(part) for part in command):
                    self.assertGreater(kwargs.get('timeout', 0), 0, 'outer smoke deadline missing')
                    raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                if 'PyInstaller' in command:
                    candidate = Path(kwargs['cwd']) / 'dist/AgentBus.exe'
                    candidate.parent.mkdir()
                    candidate.write_bytes(b'unverified candidate')
                return subprocess.CompletedProcess(command, 0)

            spec = MagicMock(submodule_search_locations=[str(root / 'runtime')])
            with patch.object(api.importlib.util, 'find_spec', return_value=spec), \
                 patch.object(api, 'build_command', side_effect=lambda *args: [sys.executable, '-m', 'PyInstaller']), \
                 patch.object(api, 'validate_share_assets'), \
                 patch('tools.export_source.export_source', side_effect=export_fixture), \
                 patch.object(api.subprocess, 'run', side_effect=fake_run):
                code = api.main(['--root', str(root), '--dist-dir', 'dist'])
            self.assertEqual(code, 1)
            self.assertEqual(output.read_bytes(), b'existing release')

    def test_builder_rejects_source_root_links_before_runtime_lookup(self):
        from tools.export_source import SourceReleaseError
        api = self.builder()
        with tempfile.TemporaryDirectory() as directory, \
             patch('tools.export_source.validate_source_root', side_effect=SourceReleaseError('root link')), \
             patch.object(api.importlib.util, 'find_spec') as lookup:
            code = api.main(['--root', directory, '--dry-run'])
        self.assertEqual(code, 1)
        lookup.assert_not_called()

    def test_tower_without_saved_project_uses_current_directory(self):
        from agentbus import watchtower_gui as module

        class Value:
            def __init__(self, value): self.value = value
            def get(self): return self.value

        with tempfile.TemporaryDirectory() as d, patch.object(module, '_load_state', return_value={}), \
             patch.object(module.TowerApp, '_build'), patch.object(module.tk, 'StringVar', side_effect=Value), \
             patch.object(module.Path, 'cwd', return_value=Path(d)):
            app = module.TowerApp(MagicMock())
            self.assertEqual(app.ws_var.get(), d)

    def test_builder_release_gate_invokes_delivery_self_proof_and_blocks_on_failure(self):
        """验证 Release Gate 真实调用 delivery_self_proof，且在自证失败时熔断发版。"""
        api = self.builder()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.fixture(root)
            (root / "packaging").mkdir(parents=True, exist_ok=True)
            (root / "packaging/BUILD-MANIFEST.json").write_text('{"synchronized": true}', encoding="utf-8")

            spec = MagicMock(submodule_search_locations=[str(root / 'runtime')])
            with patch.object(api.importlib.util, 'find_spec', return_value=spec), \
                 patch.object(api, 'validate_share_assets'), \
                 patch('tools.check_formal_sync.audit_formal_sync', return_value={"passed": True}), \
                 patch('tools.delivery_self_proof.verify_delivery_self_proof', return_value={"verdict": "fail", "reasons": ["自证测试失败模拟"]}):
                code = api.main(['--root', str(root), '--dist-dir', 'dist'])
            self.assertEqual(code, 1, "自证门禁判定失败时发版必须被熔断退出码 1")


if __name__ == '__main__':
    unittest.main()
