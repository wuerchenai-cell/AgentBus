"""Extensible member profiles. Source: Codex extension_design, 2026-10-02."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.config import load
from agentbus.project_workflow import WorkflowError, resume_prompt


class DynamicWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'new project'
        self.cfg = load(self.root, create=True)
        launcher = Path(self.temp.name) / 'AgentBus.exe'
        launcher.write_bytes(b'fixture only, never execute')
        self.frozen = patch.object(sys, 'frozen', True, create=True)
        self.executable = patch.object(sys, 'executable', str(launcher))
        self.frozen.start()
        self.executable.start()
        self.addCleanup(self.frozen.stop)
        self.addCleanup(self.executable.stop)

    def test_new_member_can_use_project_entry_without_changing_product_code(self):
        self.cfg.data['workers']['new-agent'] = {
            'enabled': True, 'executor_enabled': True, 'can_spawn': False,
            'human_in_loop': True, 'kind': 'gui',
        }
        from agentbus.project_workflow import prepare_project_workflow
        prepare_project_workflow(self.cfg)
        try:
            prompt = resume_prompt(self.cfg, 'new-agent')
        except WorkflowError as exc:
            self.fail(f'configured member must be supported: {exc}')
        self.assertIn('new-agent', prompt)
        self.assertIn(str(self.root), prompt)

    def test_dynamic_prepare_and_status_cover_unknown_member(self):
        from agentbus.project_workflow import prepare_project_workflow, workflow_status
        self.cfg.data['workers']['new-agent'] = {'enabled': True, 'can_spawn': False}
        result = prepare_project_workflow(self.cfg)
        relative = '.agentbus/workflows/new-agent/SKILL.md'
        self.assertIn('new-agent', result['agents'])
        self.assertIn(relative, result['files'])
        self.assertTrue((self.root / relative).is_file())
        self.assertTrue(workflow_status(self.cfg)['ready'])
        self.cfg.data['workers']['new-agent']['enabled'] = False
        self.assertTrue(workflow_status(self.cfg)['ready'])

    def test_path_collision_is_rejected_before_workflow_writes(self):
        from agentbus.project_workflow import prepare_project_workflow
        self.cfg.data['workers']['new-agent'] = {
            'enabled': True, 'project_skill': '.trae/skills/agentbus-team/SKILL.md'}
        with self.assertRaises(WorkflowError):
            prepare_project_workflow(self.cfg)
        self.assertFalse((self.root / 'AGENTBUS.md').exists())

    def test_project_path_rejects_private_wb_and_escape(self):
        from agentbus.project_workflow import prepare_project_workflow
        for path in ('../SKILL.md', '.workbuddy/skills/SKILL.md',
                     '.WORKBUDDY/SKILL.md', 'C:/private/SKILL.md'):
            with self.subTest(path=path):
                self.cfg.data['workers']['new-agent'] = {
                    'enabled': True, 'project_skill': path}
                with self.assertRaises(WorkflowError):
                    prepare_project_workflow(self.cfg)
                self.assertFalse((self.root / 'AGENTBUS.md').exists())

    def test_configured_relative_skill_location_can_be_used(self):
        from agentbus.project_workflow import prepare_project_workflow
        self.cfg.data['workers']['new-agent'] = {
            'enabled': True, 'project_skill': '.custom/skills/agentbus-team/SKILL.md'}
        result = prepare_project_workflow(self.cfg)
        self.assertIn('.custom/skills/agentbus-team/SKILL.md', result['files'])

    def test_peer_skill_has_host_neutral_entry_and_no_fixed_codex_transporter(self):
        from agentbus.project_workflow import peer_skill
        text = peer_skill('new-agent')
        self.assertIn('team start', text)
        self.assertIn('--caller', text)
        self.assertIn('team status', text)
        self.assertIn('team dispatch', text)
        self.assertIn('不粘贴', text)
        self.assertNotIn('Codex 运输方', text)

    def test_entry_preserves_request_phrase_checkpoint_and_verified_finish(self):
        from agentbus.project_workflow import peer_skill
        text = peer_skill('new-agent')
        self.assertIn('使用这个小组工作', text)
        self.assertIn('返回的 id', text)
        self.assertIn('team checkpoint', text)
        self.assertIn('team finish', text)
        self.assertIn('--verified', text)
        self.assertIn('产物验收', text)

    def test_resume_snapshot_lists_all_ids_and_counts_when_paths_are_truncated(self):
        from agentbus.project_workflow import prepare_project_workflow
        prepare_project_workflow(self.cfg)
        ready = self.root / '.agentbus/agents/zcode/ready'
        ready.mkdir(parents=True)
        for number in range(24):
            (ready / f'm-{number:03}.json').write_text(
                json.dumps({'id': f'task-{number:03}'}), encoding='utf-8')
        text = resume_prompt(self.cfg, 'zcode')
        self.assertIn('ready=24', text)
        self.assertIn('processing=0', text)
        for number in range(20, 24):
            self.assertIn(f'task-{number:03}', text)

    def test_processing_heartbeat_warning_is_read_only(self):
        from agentbus.project_workflow import prepare_project_workflow
        prepare_project_workflow(self.cfg)
        processing = self.root / '.agentbus/agents/zcode/processing'
        processing.mkdir(parents=True)
        stale = processing / 'old.json'
        stale.write_text('{"id":"old-task"}', encoding='utf-8')
        old = time.time() - 120
        os.utime(stale, (old, old))
        self.cfg.data['gates']['heartbeat_timeout_s'] = 60
        before = stale.read_bytes()
        text = resume_prompt(self.cfg, 'zcode')
        self.assertIn('old-task', text)
        self.assertIn('未发现有效票据绑定心跳', text)
        self.assertIn('文件修改时间不是心跳', text)
        self.assertIn('不能据此声称成员离线或抢占任务', text)
        self.assertEqual(before, stale.read_bytes())
        self.assertTrue(stale.is_file())

    def test_project_entry_describes_stop_limit(self):
        from agentbus.project_workflow import prepare_project_workflow
        prepare_project_workflow(self.cfg)
        text = (self.root / 'AGENTBUS.md').read_text(encoding='utf-8')
        self.assertIn('STOP', text)
        self.assertIn('不能强制中断', text)


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.cfg = load(self.root, create=True)

    def test_member_ids_reject_path_and_device_names(self):
        from agentbus.profiles import ProfileError, validate_agent_id
        for name in ('../other', 'a/b', 'a\\b', 'C:agent', 'Upper',
                     'con', 'nul', 'lpt1', 'human', 'director', '', 'a' * 65):
            with self.subTest(name=name), self.assertRaises(ProfileError):
                validate_agent_id(name)
        for name in ('codex', 'anthropic-api', 'new-agent', 'a12'):
            self.assertEqual(validate_agent_id(name), name)
        self.assertEqual(validate_agent_id('human', allow_system=True), 'human')

    def test_new_gui_profile_keeps_coordinator_and_permissions(self):
        from agentbus.profiles import add_profile
        original = json.loads(self.cfg.path.read_text(encoding='utf-8'))
        saved = add_profile(self.cfg, 'new-agent', '新成员')
        worker = saved.worker('new-agent')
        self.assertEqual(saved.director, original['director'])
        self.assertEqual(worker['display_name'], '新成员')
        self.assertTrue(worker['enabled'])
        self.assertFalse(worker['can_spawn'])
        self.assertEqual(worker['kind'], 'gui')
        self.assertEqual(worker['transport'], 'file')
        self.assertEqual(saved.worker('codex'), self.cfg.worker('codex'))
        self.assertEqual(saved.gates, self.cfg.gates)

    def test_acp_registration_is_candidate_and_does_not_execute(self):
        from agentbus.profiles import add_profile
        command = ['fictional-agent.exe', 'acp']
        saved = add_profile(self.cfg, 'future-agent', 'Future', kind='acp', command=command)
        worker = saved.worker('future-agent')
        self.assertEqual(worker['candidate_command'], command)
        self.assertFalse(worker['can_spawn'])
        self.assertNotIn('command', worker)

    def test_cli_route_cannot_be_enabled_before_real_acceptance(self):
        from agentbus.profiles import ProfileError, add_profile
        with self.assertRaises(ProfileError):
            add_profile(self.cfg, 'future-cli', 'Future CLI', transport='cli')
        self.assertNotIn('future-cli', load(self.root).data['workers'])

    def test_duplicate_profile_cannot_replace_user_settings(self):
        from agentbus.profiles import ProfileError, add_profile
        before = self.cfg.path.read_bytes()
        with self.assertRaises(ProfileError):
            add_profile(self.cfg, 'codex', 'overwrite')
        self.assertEqual(self.cfg.path.read_bytes(), before)

    def test_profile_errors_do_not_change_disk(self):
        from agentbus.profiles import ProfileError, add_profile
        before = self.cfg.path.read_bytes()
        for name, title, kind, command in (
            ('../escape', 'bad', 'gui', None), ('new-agent', '', 'gui', None),
            ('new-agent', 'bad', 'http-llm', None), ('new-agent', 'ACP', 'acp', None),
            ('new-agent', 'ACP', 'acp', 'agent acp'),
            ('new-agent', 'ACP', 'acp', ['', 'acp']),
            ('new-agent', 'bad\nlabel', 'gui', None)):
            with self.subTest(name=name, title=title, kind=kind, command=command):
                with self.assertRaises(ProfileError):
                    add_profile(self.cfg, name, title, kind=kind, command=command)
                self.assertEqual(before, self.cfg.path.read_bytes())

    def test_profile_save_rejects_active_configuration_lease(self):
        from agentbus.lease import LeaseManager
        from agentbus.profiles import ProfileError, add_profile
        manager = LeaseManager(self.root / '.agentbus')
        held = manager.acquire('.agentbus/config.json', 'other-agent', ttl_s=60)
        self.addCleanup(manager.release, held)
        before = self.cfg.path.read_bytes()
        with self.assertRaises(ProfileError):
            add_profile(self.cfg, 'new-agent', 'New')
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_registration_uses_latest_saved_members_instead_of_stale_snapshot(self):
        from agentbus.profiles import add_profile
        add_profile(self.cfg, 'first-agent', 'First')
        second = add_profile(self.cfg, 'second-agent', 'Second')
        self.assertIn('first-agent', second.data['workers'])
        self.assertIn('second-agent', second.data['workers'])

    def test_project_path_rejects_existing_symlink_escape(self):
        from agentbus.profiles import ProfileError, project_skill_paths
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        link = self.root / 'custom'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        self.cfg.data['workers']['new-agent'] = {
            'project_skill': 'custom/agentbus-team/SKILL.md'}
        with self.assertRaises(ProfileError):
            project_skill_paths(self.cfg)


if __name__ == '__main__':
    unittest.main()
