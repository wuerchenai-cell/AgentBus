from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.config import load
from agentbus.profiles import project_skill_paths
from agentbus.project_workflow import (WorkflowError, prepare_project_workflow,
                                      workflow_status, resume_prompt)


class ProjectWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '项目 A'
        self.cfg = load(self.root, create=True)
        self.launcher = Path(self.temp.name) / 'AgentBus.exe'
        self.launcher.write_bytes(b'fixture only, never execute')
        self.frozen = patch.object(sys, 'frozen', True, create=True)
        self.executable = patch.object(sys, 'executable', str(self.launcher))
        self.frozen.start()
        self.executable.start()
        self.addCleanup(self.frozen.stop)
        self.addCleanup(self.executable.stop)

    def test_prepares_group_roles_without_changing_config_or_agents(self):
        rules = self.root / 'AGENTS.md'
        rules.write_text('existing private project rules\n', encoding='utf-8')
        before = self.cfg.path.read_bytes()
        result = prepare_project_workflow(self.cfg)
        # The roster is extensible; the generated workflow must cover the
        # currently enabled project members, including newly registered hosts.
        self.assertEqual(set(result['agents']), set(project_skill_paths(self.cfg)))
        self.assertEqual(before, self.cfg.path.read_bytes())
        self.assertEqual(rules.read_text(), 'existing private project rules\n')
        for path in result['files']:
            self.assertTrue((self.root / path).is_file())
            self.assertNotIn('.workbuddy', path)

    def test_idempotent_does_not_rewrite_unchanged_files(self):
        first = prepare_project_workflow(self.cfg)
        times = {p: (self.root / p).stat().st_mtime_ns for p in first['files']}
        second = prepare_project_workflow(self.cfg)
        self.assertEqual(second['changed'], [])
        self.assertEqual(times, {p: (self.root / p).stat().st_mtime_ns for p in times})

    def test_removed_profile_ownership_survives_upgrade_and_readd(self):
        from unittest.mock import patch
        from agentbus.project_workflow import peer_skill
        prepare_project_workflow(self.cfg)
        original = self.cfg.data['workers'].pop('zcode')
        prepare_project_workflow(self.cfg)
        manifest_path = self.root / '.agentbus/workflow.json'
        record = json.loads(manifest_path.read_text(encoding='utf-8'))
        retired = '.zcode/skills/agentbus-team/SKILL.md'
        self.assertIn(retired, record.get('retired_files', {}))
        self.cfg.data['workers']['zcode'] = original
        with patch('agentbus.project_workflow.peer_skill', side_effect=lambda agent=None: peer_skill(agent) + '\nUpdated workflow\n'):
            prepare_project_workflow(self.cfg)
        record = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertNotIn(retired, record.get('retired_files', {}))
        self.assertIn('Updated workflow', (self.root / retired).read_text(encoding='utf-8'))

    def test_project_write_failure_restores_old_manifest_and_outputs(self):
        from unittest.mock import patch
        from agentbus import skill_install
        from agentbus.project_workflow import peer_skill
        prepare_project_workflow(self.cfg)
        record = json.loads((self.root / '.agentbus/workflow.json').read_text(encoding='utf-8'))
        paths = [self.root / p for p in record['files']] + [self.root / '.agentbus/workflow.json']
        before = {path: path.read_bytes() for path in paths}
        writer = skill_install._atomic_write
        calls = 0
        def fail(path, data):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('injected transaction write failure')
            return writer(path, data)
        with patch('agentbus.project_workflow.peer_skill', side_effect=lambda agent=None: peer_skill(agent) + '\nnew version\n'), patch('agentbus.skill_install._atomic_write', side_effect=fail):
            with self.assertRaises(OSError):
                prepare_project_workflow(self.cfg)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_frozen_launcher_is_in_project_manifest_for_other_hosts(self):
        import sys
        from unittest.mock import patch
        fake_exe=Path(self.temp.name)/'共享工具 AgentBus.exe'
        fake_exe.write_bytes(b'fixture')
        with patch.object(sys,'frozen',True,create=True), patch.object(sys,'executable',str(fake_exe)):
            prepare_project_workflow(self.cfg)
        record=json.loads((self.root/'.agentbus/workflow.json').read_text(encoding='utf8'))
        self.assertEqual(record['launcher']['executable'],str(fake_exe.resolve()))
        self.assertIn(str(fake_exe.resolve()),(self.root/'AGENTBUS.md').read_text(encoding='utf8'))

    def test_explicit_frozen_launcher_registers_digest_and_size_from_source(self):
        import hashlib
        from unittest.mock import patch
        fake_exe = Path(self.temp.name) / 'formal AgentBus.exe'
        payload = b'owner-selected launcher fixture; never execute'
        fake_exe.write_bytes(payload)
        with patch.object(sys, 'frozen', False, create=True):
            prepare_project_workflow(self.cfg, frozen_executable=fake_exe)
        record = json.loads((self.root / '.agentbus/workflow.json').read_text(encoding='utf-8'))
        self.assertEqual(record['launcher'], {
            'mode': 'frozen',
            'executable': str(fake_exe.resolve()),
            'sha256': hashlib.sha256(payload).hexdigest(),
            'bytes': len(payload),
        })
        self.assertTrue(workflow_status(self.cfg)['ready'])

    def test_explicit_frozen_launcher_rejects_missing_path_before_writing(self):
        missing = Path(self.temp.name) / 'missing.exe'
        with self.assertRaises(WorkflowError):
            prepare_project_workflow(self.cfg, frozen_executable=missing)
        self.assertFalse((self.root / 'AGENTBUS.md').exists())
        self.assertFalse((self.root / '.agentbus/workflow.json').exists())

    def test_workflow_cli_accepts_explicit_frozen_launcher(self):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from agentbus.cli import main
        fake_exe = Path(self.temp.name) / 'cli-selected.exe'
        fake_exe.write_bytes(b'cli fixture; never execute')
        output = io.StringIO()
        with patch.object(sys, 'frozen', False, create=True), redirect_stdout(output):
            code = main(['workflow', 'prepare', '--workspace', str(self.root),
                         '--frozen-executable', str(fake_exe)])
        self.assertEqual(code, 0, output.getvalue())
        record = json.loads((self.root / '.agentbus/workflow.json').read_text(encoding='utf-8'))
        self.assertEqual(record['launcher']['executable'], str(fake_exe.resolve()))
        self.assertEqual(record['launcher']['bytes'], fake_exe.stat().st_size)

    def test_status_rejects_frozen_launcher_size_mismatch(self):
        prepare_project_workflow(self.cfg)
        manifest = self.root / '.agentbus/workflow.json'
        record = json.loads(manifest.read_text(encoding='utf-8'))
        record['launcher']['bytes'] = self.launcher.stat().st_size + 1
        manifest.write_text(json.dumps(record), encoding='utf-8')
        status = workflow_status(self.cfg)
        self.assertFalse(status['ready'])
        self.assertEqual(status['launcher_integrity'], 'failed')

    def test_unknown_existing_file_rejects_before_any_output(self):
        entry = self.root / 'AGENTBUS.md'
        entry.write_text('user owned', encoding='utf-8')
        with self.assertRaises(WorkflowError):
            prepare_project_workflow(self.cfg)
        self.assertEqual(entry.read_text(), 'user owned')
        self.assertFalse((self.root / '.trae/skills/agentbus-team/SKILL.md').exists())

    def test_user_modified_owned_file_is_preserved(self):
        prepare_project_workflow(self.cfg)
        skill = self.root / '.trae/skills/agentbus-team/SKILL.md'
        skill.write_text('my edits', encoding='utf-8')
        with self.assertRaises(WorkflowError):
            prepare_project_workflow(self.cfg)
        self.assertEqual(skill.read_text(), 'my edits')

    def test_status_detects_tampered_output(self):
        prepare_project_workflow(self.cfg)
        (self.root / 'AGENTBUS.md').write_text('tampered', encoding='utf-8')
        self.assertFalse(workflow_status(self.cfg)['ready'])

    def test_nested_workspace_does_not_use_parent(self):
        prepare_project_workflow(self.cfg)
        nested = load(self.root / '项目 B', create=True)
        prepare_project_workflow(nested)
        text = resume_prompt(nested, 'workbuddy')
        self.assertIn(str(nested.workspace), text)
        self.assertNotIn(str(self.root / 'AGENTBUS.md'), text)

    def test_prompt_has_explicit_identity_and_no_shell_command(self):
        prepare_project_workflow(self.cfg)
        text = resume_prompt(self.cfg, 'trae')
        self.assertIn('trae', text)
        self.assertIn('AGENTBUS.md', text)
        self.assertIn('继续', text)
        for unwanted in ('powershell', 'cmd /c', '-m agentbus', 'codebuddy -p'):
            self.assertNotIn(unwanted, text.lower())

    def test_resume_prompt_preserves_native_report_and_processing_ownership(self):
        prepare_project_workflow(self.cfg)
        text = resume_prompt(self.cfg, 'workbuddy')
        self.assertIn('agentbus_report', text)
        self.assertIn('processing', text)
        self.assertNotIn('仅回复本对话', text)

    def test_resume_prompt_includes_only_own_existing_message_file_paths(self):
        prepare_project_workflow(self.cfg)
        own = self.root / '.agentbus/agents/zcode/ready/m-task.json'
        own.parent.mkdir(parents=True)
        own.write_text('{}', encoding='utf-8')
        foreign = self.root / '.agentbus/agents/trae/ready/foreign.json'
        foreign.parent.mkdir(parents=True)
        foreign.write_text('{}', encoding='utf-8')
        text = resume_prompt(self.cfg, 'zcode')
        self.assertIn(str(own), text)
        self.assertNotIn(str(foreign), text)
        self.assertIn('目录', text)

    def test_peer_skill_distinguishes_directory_from_file_and_no_listing_from_empty(self):
        from agentbus.project_workflow import peer_skill
        text = peer_skill('zcode')
        self.assertIn('不是 ready.json', text)
        self.assertIn('不能判定', text)

    def test_disabled_member_and_unknown_identity_rejected(self):
        self.cfg.data['workers']['trae']['enabled'] = False
        with self.assertRaises(WorkflowError):
            resume_prompt(self.cfg, 'trae')
        with self.assertRaises(WorkflowError):
            resume_prompt(self.cfg, 'imaginary')

    def test_missing_config_is_not_initialized(self):
        self.cfg.path.unlink()
        with self.assertRaises(WorkflowError):
            prepare_project_workflow(self.cfg)
        self.assertFalse(self.cfg.path.exists())

    def test_stop_marker_is_explicit_in_resume_prompt(self):
        prepare_project_workflow(self.cfg)
        (self.root / '.agentbus/STOP').write_text('stopped', encoding='utf-8')
        self.assertIn('STOP', resume_prompt(self.cfg, 'codex'))

    def test_corrupt_manifest_is_not_overwritten(self):
        prepare_project_workflow(self.cfg)
        manifest = self.root / '.agentbus/workflow.json'
        manifest.write_text('{broken', encoding='utf-8')
        with self.assertRaises(WorkflowError):
            prepare_project_workflow(self.cfg)
        self.assertEqual(manifest.read_text(), '{broken')

    def test_symlink_output_cannot_escape_project(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        try:
            (self.root / '.trae').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaises(WorkflowError):
            prepare_project_workflow(self.cfg)
        self.assertFalse((outside / 'skills').exists())

    def test_status_rejects_copied_manifest_for_another_workspace(self):
        import shutil
        prepare_project_workflow(self.cfg)
        copied = Path(self.temp.name) / 'copied'
        shutil.copytree(self.root, copied)
        cfg = load(copied)
        self.assertFalse(workflow_status(cfg)['ready'])


class PeerSkillInstallTests(unittest.TestCase):
    def test_user_peer_install_uses_separate_skill_roots_and_no_private_wb(self):
        from agentbus.skill_install import install_peer_skills_for_current_user
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            state = home / 'state'
            state.mkdir()
            (state / 'install.json').write_text(json.dumps({
                'launcher': {'mode': 'frozen', 'executable': 'AgentBus.exe'}}))
            result = install_peer_skills_for_current_user('1.1.0', home=home, state_dir=state)
            self.assertEqual(set(result), {'workbuddy', 'trae', 'zcode', 'opencode'})
            for info in result.values():
                self.assertTrue(info['written'])
                self.assertFalse(info['native_verified'])
                self.assertTrue(Path(info['skill']).is_file())
            self.assertFalse((home / '.workbuddy').exists())
            second = install_peer_skills_for_current_user('1.1.0', home=home, state_dir=state)
            self.assertEqual(result, second)

    def test_user_custom_peer_skill_is_not_overwritten(self):
        from agentbus.skill_install import install_peer_skills_for_current_user
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            state = home / 'state'
            state.mkdir()
            (state / 'install.json').write_text(json.dumps({
                'launcher': {'mode': 'frozen', 'executable': 'AgentBus.exe'}}))
            custom = home / '.zcode/skills/agentbus-team/SKILL.md'
            custom.parent.mkdir(parents=True)
            custom.write_text('user rules', encoding='utf-8')
            result = install_peer_skills_for_current_user('1.1.0', home=home, state_dir=state)
            self.assertFalse(result['zcode']['written'])
            self.assertEqual(custom.read_text(), 'user rules')


if __name__ == '__main__':
    unittest.main()
