"""Source: Codex lifecycle audit, 2026-10-03. Isolated homes only."""
from __future__ import annotations
import importlib
import hashlib
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / '用户 空格'
        self.state = self.home / 'launcher'

    def api(self):
        try:
            return importlib.import_module('agentbus.lifecycle')
        except ImportError:
            self.fail('Lifecycle module missing: installation cannot be safely inspected/uninstalled')

    def install(self):
        return self.api().install_workflows('test', home=self.home, state_dir=self.state)

    def test_install_preview_apply_idempotent_uninstall_reinstall(self):
        api = self.api()
        installed = self.install()
        self.assertEqual(installed['status'], 'installed')
        self.assertEqual(set(installed['hosts']), {'codex', 'workbuddy', 'trae', 'zcode', 'opencode'})
        self.assertEqual(api.inspect_workflows(home=self.home, state_dir=self.state)['status'], 'installed')
        preview = api.uninstall_workflows(home=self.home, state_dir=self.state)
        self.assertFalse(preview['applied'])
        self.assertEqual(len(preview['removable']), 10)
        for path in preview['removable']:
            self.assertTrue(Path(path).is_file())
        applied = api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        self.assertEqual(applied['status'], 'uninstalled')
        self.assertEqual(len(applied['removed']), 10)
        self.assertEqual(api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)['removed'], [])
        self.assertEqual(self.install()['status'], 'installed')

    def test_modified_skill_and_its_manifest_are_preserved(self):
        api = self.api()
        result = self.install()
        skill = Path(result['hosts']['zcode']['skill'])
        manifest = Path(result['hosts']['zcode']['manifest'])
        skill.write_text('user edits', encoding='utf-8')
        result = api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(skill.read_text(), 'user edits')
        self.assertTrue(manifest.is_file())
        self.assertIn(str(skill), result['preserved'])

    def test_applied_uninstall_reports_post_removal_hosts_and_removable(self):
        api = self.api()
        self.install()
        result = api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        current = api.inspect_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(result['status'], 'uninstalled')
        self.assertEqual(len(result['removed']), 10)
        self.assertEqual(result['hosts'], current['hosts'])
        self.assertEqual(result['removable'], current['removable'])
        self.assertEqual(result['preserved'], current['preserved'])
        self.assertEqual(result['errors'], current['errors'])
        for host in result['hosts'].values():
            self.assertEqual(host['status'], 'missing')
            self.assertFalse(host['owned'])
            self.assertFalse(host['launcher_ready'])

    def test_partial_uninstall_reports_removed_and_modified_hosts_as_they_are(self):
        api = self.api()
        installed = self.install()
        protected = Path(installed['hosts']['zcode']['skill'])
        protected.write_text('user edit must remain', encoding='utf-8')
        result = api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        current = api.inspect_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['removed']), 8)
        self.assertEqual(result['hosts'], current['hosts'])
        self.assertEqual(result['removable'], current['removable'])
        self.assertEqual(result['preserved'], current['preserved'])
        self.assertEqual(result['hosts']['zcode']['status'], 'modified')
        self.assertTrue(result['hosts']['zcode']['owned'])
        for name, host in result['hosts'].items():
            if name != 'zcode':
                self.assertEqual(host['status'], 'missing')
                self.assertFalse(host['owned'])
                self.assertFalse(host['launcher_ready'])
        self.assertEqual(protected.read_text(encoding='utf-8'), 'user edit must remain')

    def test_uninstall_removal_error_preserves_history_and_refreshes_remaining_host(self):
        api = self.api()
        installed = self.install()
        manifest = Path(installed['hosts']['codex']['manifest'])
        skill = Path(installed['hosts']['codex']['skill'])
        original = Path.unlink
        def fail_manifest(path, *args, **kwargs):
            if path == manifest:
                raise PermissionError('synthetic registered manifest busy')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_manifest):
            result = api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        current = api.inspect_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(result['status'], 'partial')
        self.assertIn(str(skill), result['removed'])
        self.assertNotIn(str(manifest), result['removed'])
        self.assertTrue(any('synthetic registered manifest busy' in error for error in result['errors']))
        self.assertEqual(result['hosts'], current['hosts'])
        self.assertEqual(result['removable'], current['removable'])
        self.assertEqual(result['hosts']['codex']['status'], 'missing_file')
        self.assertTrue(result['hosts']['codex']['owned'])
        self.assertTrue(manifest.is_file())
        self.assertFalse(skill.exists())

    def test_identical_unregistered_skill_is_not_claimed_for_deletion(self):
        from agentbus.skill_install import bundled_skill_source
        skill = self.home / '.agents/skills/agentbus-team/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_bytes(bundled_skill_source().read_bytes())
        installed = self.install()
        manifest = json.loads(Path(installed['hosts']['codex']['manifest']).read_text(encoding='utf-8'))
        self.assertFalse(manifest['owned'])
        result = self.api().uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        self.assertTrue(skill.is_file())
        self.assertIn(str(skill), result['preserved'])

    def test_registered_unowned_unchanged_skill_can_upgrade_without_claiming_deletion(self):
        from agentbus import skill_install as si
        source = si.bundled_skill_source()
        skill = self.home / '.agents/skills/agentbus-team/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_bytes(source.read_bytes())
        installed = self.install()
        manifest = Path(installed['hosts']['codex']['manifest'])
        self.assertFalse(json.loads(manifest.read_text(encoding='utf-8'))['owned'])
        updated = Path(self.temp.name) / 'updated-source.md'
        updated.write_bytes(source.read_bytes() + b'\n<!-- isolated upgrade fixture -->\n')
        with patch.object(si, 'bundled_skill_source', return_value=updated):
            self.assertEqual(self.install()['status'], 'installed')
        self.assertEqual(skill.read_bytes(), updated.read_bytes())
        self.assertFalse(json.loads(manifest.read_text(encoding='utf-8'))['owned'])
        result = self.api().uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        self.assertTrue(skill.is_file())
        self.assertIn(str(skill), result['preserved'])

    def test_registered_unowned_user_edit_still_blocks_upgrade(self):
        from agentbus import skill_install as si
        skill = self.home / '.agents/skills/agentbus-team/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_bytes(si.bundled_skill_source().read_bytes())
        self.install()
        skill.write_bytes(b'user modification')
        before = skill.read_bytes()
        with self.assertRaises(self.api().LifecycleError):
            self.install()
        self.assertEqual(skill.read_bytes(), before)

    def test_invalid_manifest_or_injected_root_prevents_any_deletion(self):
        api = self.api()
        installed = self.install()
        manifest = Path(installed['hosts']['codex']['manifest'])
        original = json.loads(manifest.read_text(encoding='utf-8'))
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        sentinel = outside / 'SKILL.md'
        sentinel.write_text('do not remove')
        for update in ({'owner': 'other'}, {'schema_version': 99}, {'skill_root': str(outside)}):
            data = original | update
            manifest.write_text(json.dumps(data), encoding='utf-8')
            result = api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
            self.assertEqual(result['removed'], [])
            self.assertTrue(result['errors'])
            self.assertEqual(sentinel.read_text(), 'do not remove')
        manifest.write_text('{broken', encoding='utf-8')
        self.assertEqual(api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)['removed'], [])

    def test_uninstall_preserves_other_projects_and_unrelated_skill_files(self):
        api = self.api()
        installed = self.install()
        project = self.home / '项目/.agentbus/jobs/j-test.json'
        project.parent.mkdir(parents=True)
        project.write_bytes(b'project data')
        skill_dir = Path(installed['hosts']['codex']['skill']).parent
        extra = skill_dir / 'user-notes.md'
        extra.write_bytes(b'user notes')
        api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        self.assertEqual(project.read_bytes(), b'project data')
        self.assertEqual(extra.read_bytes(), b'user notes')
        self.assertTrue(skill_dir.parent.is_dir())

    def test_write_failure_rolls_back_previous_hosts(self):
        api = self.api()
        from agentbus import skill_install
        original = skill_install._atomic_write
        calls = 0
        def fail(path, data):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError('injected write failure')
            return original(path, data)
        with patch('agentbus.skill_install._atomic_write', side_effect=fail):
            with self.assertRaises(api.LifecycleError):
                self.install()
        self.assertFalse((self.state / 'install.json').exists())
        self.assertFalse((self.home / '.agents/skills/agentbus-team/SKILL.md').exists())
        self.assertEqual(self.install()['status'], 'installed')

    def test_failure_rollback_preserves_intervening_user_edit(self):
        api = self.api()
        from agentbus import skill_install
        original = skill_install._atomic_write
        skill = self.home / '.agents/skills/agentbus-team/SKILL.md'
        calls = 0
        def fail(path, data):
            nonlocal calls
            calls += 1
            if calls == 4:
                skill.write_text('user changed this during installation', encoding='utf-8')
                raise OSError('injected failure after user modification')
            return original(path, data)
        with patch('agentbus.skill_install._atomic_write', side_effect=fail):
            with self.assertRaisesRegex(api.LifecycleError, '已变化'):
                self.install()
        self.assertEqual(skill.read_text(), 'user changed this during installation')

    def test_write_error_after_publish_also_restores_previous_bytes(self):
        api = self.api()
        from agentbus import skill_install
        writer = skill_install._atomic_write
        calls = 0
        def fail(path, data):
            nonlocal calls
            calls += 1
            writer(path, data)
            if calls == 2:
                raise OSError('failure after replace')
        with patch('agentbus.skill_install._atomic_write', side_effect=fail):
            with self.assertRaises(api.LifecycleError):
                self.install()
        self.assertFalse((self.state / 'install.json').exists())
        self.assertFalse((self.home / '.agents/skills/agentbus-team/SKILL.md').exists())

    def test_malformed_launcher_is_reported_and_deleted_exe_is_observable(self):
        api = self.api()
        exe = Path(self.temp.name) / 'portable.exe'
        exe.write_bytes(b'fixture')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
            result = self.install()
        manifest = Path(result['hosts']['codex']['manifest'])
        data = json.loads(manifest.read_text(encoding='utf-8'))
        data['launcher'] = []
        manifest.write_text(json.dumps(data), encoding='utf-8')
        inspected = api.inspect_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(inspected['status'], 'invalid')
        self.assertTrue(inspected['errors'])
        data['launcher'] = {'mode': 'frozen', 'executable': str(Path(self.temp.name) / '.workbuddy/never-stat.exe'),
                            'sha256': 'a' * 64}
        manifest.write_text(json.dumps(data), encoding='utf-8')
        original_open, original_stat = Path.open, Path.stat
        def guarded_open(path, *args, **kwargs):
            self.assertNotIn('.workbuddy', (part.casefold() for part in path.parts))
            return original_open(path, *args, **kwargs)
        def guarded_stat(path, *args, **kwargs):
            self.assertNotIn('.workbuddy', (part.casefold() for part in path.parts))
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded_open), patch.object(Path, 'stat', guarded_stat):
            inspected = api.inspect_workflows(home=self.home, state_dir=self.state)
        self.assertFalse(inspected['hosts']['codex']['launcher_ready'])
        self.assertEqual(inspected['hosts']['codex']['launcher_integrity'], 'failed')
        self.assertIn('私有', inspected['hosts']['codex']['launcher_reason'])
        data['launcher'] = {'mode': 'frozen', 'executable': str(exe)}
        manifest.write_text(json.dumps(data), encoding='utf-8')
        exe.unlink()
        inspected = api.inspect_workflows(home=self.home, state_dir=self.state)
        self.assertFalse(inspected['hosts']['codex']['launcher_exists'])
        self.assertEqual(inspected['hosts']['codex']['status'], 'missing_launcher')
        self.assertEqual(api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True)['status'], 'uninstalled')

    def test_parallel_fresh_processes_share_one_lifecycle_lock(self):
        import subprocess
        source = str(Path(__file__).resolve().parents[1])
        script = 'import sys; sys.path.insert(0,sys.argv[1]); from agentbus.lifecycle import install_workflows; install_workflows("process-test",home=sys.argv[2],state_dir=sys.argv[3])'
        argv = [sys.executable, '-X', 'utf8', '-c', script, source, str(self.home), str(self.state)]
        processes = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr.decode('utf-8'))
        self.assertEqual(self.api().inspect_workflows(home=self.home, state_dir=self.state)['status'], 'installed')

    def test_parallel_installations_and_removal_do_not_damage_owned_files(self):
        api = self.api()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.install(), range(8)))
        self.assertEqual(api.inspect_workflows(home=self.home, state_dir=self.state)['status'], 'installed')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: api.uninstall_workflows(home=self.home, state_dir=self.state, apply=True), range(4)))
        self.assertEqual(sum(len(r['removed']) for r in results), 10)

    def test_skill_child_link_outside_root_is_rejected(self):
        api = self.api()
        root = self.home / '.agents/skills'
        root.mkdir(parents=True)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        try:
            (root / 'agentbus-team').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        sentinel = outside / 'SKILL.md'
        sentinel.write_text('outside')
        with self.assertRaises(api.LifecycleError):
            self.install()
        self.assertEqual(sentinel.read_text(), 'outside')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows junction test')
    def test_directory_junction_is_rejected_without_reading_outside_file(self):
        import subprocess
        root = self.home / '.agents/skills'
        root.mkdir(parents=True)
        outside = Path(self.temp.name) / 'outside-junction'
        outside.mkdir()
        sentinel = outside / 'SKILL.md'
        sentinel.write_text('outside user file')
        link = root / 'agentbus-team'
        created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        if created.returncode:
            self.skipTest('directory junction unavailable')
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        with self.assertRaises(self.api().LifecycleError):
            self.install()
        self.assertEqual(sentinel.read_text(), 'outside user file')

    def test_reinstall_updates_launchers_without_reading_real_environment(self):
        api = self.api()
        first = Path(self.temp.name) / 'first.exe'
        second = Path(self.temp.name) / 'moved.exe'
        first.write_bytes(b'first')
        second.write_bytes(b'second')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(first)):
            self.install()
        with patch.dict('os.environ', {'CODEX_HOME': 'C:/do-not-read'}), patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(second)):
            result = self.install()
        for host in result['hosts'].values():
            record = json.loads(Path(host['manifest']).read_text(encoding='utf-8'))
            self.assertEqual(record['launcher']['executable'], str(second.resolve()))
        self.assertFalse((self.home / '.workbuddy').exists())

    # Codex, 2026-10-03: setup install shares one verified frozen launcher
    # across all hosts, without reading private host state or granting ownership.
    def test_new_frozen_setup_install_records_streamed_digest_for_all_hosts(self):
        exe = Path(self.temp.name) / 'AgentBus.exe'
        content = b'isolated fixture never executed' * 90000
        exe.write_bytes(content)
        original = Path.read_bytes
        def guarded(path):
            self.assertNotEqual(path, exe, 'EXE must be hashed with bounded reads')
            return original(path)
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)), \
             patch.object(Path, 'read_bytes', guarded):
            result = self.install()
        for host in result['hosts'].values():
            launcher = json.loads(Path(host['manifest']).read_text(encoding='utf-8'))['launcher']
            self.assertEqual(launcher.get('sha256'), hashlib.sha256(content).hexdigest())
            self.assertEqual(launcher['executable'], str(exe.resolve()))

    def test_new_frozen_setup_install_rejects_private_or_invalid_launcher_before_outputs(self):
        api = self.api()
        base = Path(self.temp.name)
        cases = [str(base / '.workbuddy/never-read.exe'), 'AgentBus.exe',
                 str(base), str(base / 'missing.exe'), 'invalid\x00path', 42, '']
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotIn('.workbuddy', (part.casefold() for part in path.parts))
            return original(path, *args, **kwargs)
        for index, executable in enumerate(cases):
            with self.subTest(executable=executable):
                self.home = base / ('isolated-' + str(index))
                self.state = self.home / 'launcher'
                failure = None
                with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', executable), \
                     patch.object(Path, 'open', guarded):
                    try:
                        self.install()
                    except Exception as exc:
                        failure = exc
                self.assertIsInstance(failure, api.LifecycleError)
                self.assertEqual(list(self.home.rglob('SKILL.md')), [])
                self.assertEqual(list(self.home.rglob('install.json')), [])
        self.assertFalse((base / '.workbuddy').exists())

    def test_legacy_frozen_setup_status_keeps_missing_digest_without_reading_program(self):
        result = self.install()
        exe = Path(self.temp.name) / 'legacy.exe'
        exe.write_bytes(b'legacy fixture never executed')
        manifests = []
        for host in result['hosts'].values():
            path = Path(host['manifest'])
            record = json.loads(path.read_text(encoding='utf-8'))
            record['launcher'] = {'mode': 'frozen', 'executable': str(exe)}
            path.write_text(json.dumps(record), encoding='utf-8')
            manifests.append((path, path.read_bytes()))
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path, exe)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            report = self.api().inspect_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(report['status'], 'installed')
        self.assertTrue(all('sha256' not in host['launcher'] for host in report['hosts'].values()))
        self.assertTrue(all(host.get('launcher_ready') for host in report['hosts'].values()))
        self.assertTrue(all(host.get('launcher_integrity') == 'unrecorded' for host in report['hosts'].values()))
        for path, before in manifests:
            self.assertEqual(path.read_bytes(), before)

    # Codex, 2026-10-03: setup status must check newly recorded executable
    # digests without executing them or making removal depend on the program.
    def test_matching_frozen_setup_status_reports_integrity_without_runtime_claim(self):
        exe = Path(self.temp.name) / 'AgentBus.exe'
        exe.write_bytes(b'isolated fixture, never execute')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
            self.install()
        report = self.api().inspect_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(report['status'], 'installed')
        for host in report['hosts'].values():
            self.assertTrue(host.get('launcher_ready'))
            self.assertEqual(host.get('launcher_integrity'), 'verified')
            self.assertFalse(host['native_verified'])
            self.assertIn('认证', host.get('launcher_reason', ''))
            self.assertIn('未运行', host.get('launcher_reason', ''))

    def test_corrupted_frozen_setup_status_is_partial_and_strictly_read_only(self):
        exe = Path(self.temp.name) / 'AgentBus.exe'
        exe.write_bytes(b'original isolated fixture')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
            self.install()
        exe.write_bytes(b'Xriginal isolated fixture')
        before = {path.relative_to(self.home).as_posix(): path.read_bytes()
                  for path in self.home.rglob('*') if path.is_file()}
        report = self.api().inspect_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['errors'], [])
        for host in report['hosts'].values():
            self.assertEqual(host['status'], 'invalid_launcher')
            self.assertTrue(host['launcher_exists'])
            self.assertFalse(host.get('launcher_ready'))
            self.assertEqual(host.get('launcher_integrity'), 'failed')
            self.assertIn('SHA256', host.get('launcher_reason', ''))
        after = {path.relative_to(self.home).as_posix(): path.read_bytes()
                 for path in self.home.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(exe.read_bytes(), b'Xriginal isolated fixture')

    def test_source_setup_status_remains_unrecorded_without_reading_program(self):
        with patch.object(sys, 'frozen', False, create=True):
            self.install()
        from agentbus import lifecycle
        interpreter = Path(sys.executable)
        entry = Path(lifecycle.__file__).resolve().parent.parent / 'entry.py'
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotIn(path, (interpreter, entry))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            report = self.api().inspect_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(report['status'], 'installed')
        for host in report['hosts'].values():
            self.assertTrue(host.get('launcher_ready'))
            self.assertEqual(host.get('launcher_integrity'), 'unrecorded')
            self.assertFalse(host['native_verified'])

    def test_corrupted_launcher_does_not_prevent_owned_removal_or_claim_unowned_files(self):
        from agentbus.skill_install import bundled_skill_source
        unowned = self.home / '.agents/skills/agentbus-team/SKILL.md'
        unowned.parent.mkdir(parents=True)
        unowned.write_bytes(bundled_skill_source().read_bytes())
        exe = Path(self.temp.name) / 'AgentBus.exe'
        exe.write_bytes(b'original isolated fixture')
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(exe)):
            installed = self.install()
        manifest = Path(installed['hosts']['codex']['manifest'])
        unowned_before = (unowned.read_bytes(), manifest.read_bytes())
        exe.write_bytes(b'Xriginal isolated fixture')
        preview = self.api().uninstall_workflows(home=self.home, state_dir=self.state)
        self.assertEqual(preview['hosts']['codex']['status'], 'invalid_launcher')
        self.assertEqual(len(preview['removable']), 8)
        result = self.api().uninstall_workflows(home=self.home, state_dir=self.state, apply=True)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['errors'], [])
        self.assertEqual(len(result['removed']), 8)
        self.assertEqual((unowned.read_bytes(), manifest.read_bytes()), unowned_before)
        self.assertEqual(exe.read_bytes(), b'Xriginal isolated fixture')


if __name__ == '__main__':
    unittest.main()
