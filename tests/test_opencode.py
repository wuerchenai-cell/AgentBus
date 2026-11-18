"""OpenCode integration regression tests. Source: Codex, 2026-10-02."""
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

from agentbus.config import load, save
from agentbus.project_workflow import prepare_project_workflow, resume_prompt, workflow_status
from agentbus.skill_install import install_peer_skills_for_current_user


class OpenCodeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / '中文 project'
        self.cfg = load(self.root, create=True)
        launcher = Path(self.tmp.name) / 'AgentBus.exe'
        launcher.write_bytes(b'fixture only, never execute')
        self.frozen = patch.object(sys, 'frozen', True, create=True)
        self.executable = patch.object(sys, 'executable', str(launcher))
        self.frozen.start()
        self.executable.start()
        self.addCleanup(self.frozen.stop)
        self.addCleanup(self.executable.stop)

    def test_new_product_is_selectable_but_does_not_silently_join_old_groups(self):
        workers = self.cfg.data['workers']
        self.assertIn('opencode', workers)
        self.assertFalse(workers['opencode']['enabled'])
        old = json.loads(self.cfg.path.read_text(encoding='utf-8'))
        old['workers'].pop('opencode')
        self.cfg.path.write_text(json.dumps(old), encoding='utf-8')
        self.assertFalse(load(self.root).data['workers']['opencode']['enabled'])

    def test_opencode_can_be_selected_as_peer_and_leader(self):
        from agentbus.gui_state import build_group_config
        data = build_group_config(self.cfg.data, ['codex', 'opencode'], 'opencode')
        saved = save(self.cfg, data)
        self.assertEqual(set(saved.executor_workers()), {'codex', 'opencode'})
        self.assertEqual(saved.director, 'opencode')
        prepare_project_workflow(saved)
        self.assertIn(str(self.root), resume_prompt(saved, 'opencode'))

    def test_project_skill_migration_preserves_config_rules_and_receipts(self):
        prepare_project_workflow(self.cfg)
        rules = self.root / 'AGENTS.md'
        rules.write_text('user project rules', encoding='utf-8')
        receipt = self.root / '.agentbus/agents/codex/done/old.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text('{"id":"old"}', encoding='utf-8')
        before = self.cfg.path.read_bytes()
        result = prepare_project_workflow(self.cfg)
        self.assertTrue((self.root / '.opencode/skills/agentbus-team/SKILL.md').is_file())
        self.assertEqual(before, self.cfg.path.read_bytes())
        self.assertEqual(rules.read_text(), 'user project rules')
        self.assertEqual(receipt.read_text(), '{"id":"old"}')
        self.assertIn('opencode', result['agents'])
        self.assertTrue(workflow_status(self.cfg)['ready'])

    def test_global_opencode_skill_uses_official_root(self):
        home = Path(self.tmp.name) / 'home'
        state = home / 'state'
        state.mkdir(parents=True)
        (state / 'install.json').write_text(json.dumps({
            'launcher': {'mode': 'frozen', 'executable': 'AgentBus.exe'}}))
        result = install_peer_skills_for_current_user('1.1.0', home=home, state_dir=state)
        self.assertTrue(result['opencode']['written'])
        self.assertEqual(Path(result['opencode']['skill']),
                         home / '.config/opencode/skills/agentbus-team/SKILL.md')
        self.assertFalse(result['opencode']['native_verified'])
        self.assertFalse((home / '.workbuddy').exists())

    def test_product_probe_discovers_bundled_cli_without_credentials(self):
        from agentbus.detect import probe_opencode, to_config_patch
        with patch('agentbus.detect._which', return_value=None), \
             patch('agentbus.detect._first_existing',
                   return_value=Path('D:/Apps/OpenCode/OpenCode.exe')), \
             patch('agentbus.detect._version', return_value='opencode v2.0.21'), \
             patch.object(Path, 'is_file', return_value=True):
            found = probe_opencode({}, {'opencode.exe'})
        self.assertTrue(found.installed)
        self.assertEqual(Path(found.detail['command'][0]),
                         Path('D:/Apps/OpenCode/resources/opencode-cli.exe'))
        self.assertEqual(found.detail['command'][1:], ['acp'])
        data = to_config_patch({'opencode': found})['opencode']
        self.assertEqual(data['kind'], 'acp')
        self.assertTrue(data['can_spawn'])
        self.assertNotIn('--auto', data['command'])

    def test_scoped_runner_leaves_active_codex_tasks_for_current_chat(self):
        from types import SimpleNamespace
        from agentbus.bus import Bus
        from agentbus.cli import cmd_run
        from agentbus.director import Director
        from agentbus.adapters import MockAdapter
        self.cfg.data['workers']['opencode'].update(enabled=True, executor_enabled=True,
                                                   can_spawn=True, kind='acp')
        bus = Bus(self.root / '.agentbus')
        oc = bus.make('codex', 'opencode', 'task', body='bounded task', result_to='codex')
        own = bus.make('human', 'codex', 'task', body='active chat task', result_to='human')
        bus.post(oc); bus.post(own)
        cfg_bytes = self.cfg.path.read_bytes()
        def factory(cfg):
            return Director(cfg, adapter_factory=lambda name, conf: MockAdapter())
        with patch('agentbus.cli._load', return_value=self.cfg), \
             patch('agentbus.cli.Director', side_effect=factory):
            code = cmd_run(SimpleNamespace(workspace=str(self.root),agent='opencode',
                seconds=1,no_review=False,follow_ups=False,await_manual=False))
        self.assertEqual(code, 0)
        self.assertEqual(bus.find_message(oc.id).status, 'done')
        self.assertEqual(bus.find_message(own.id).status, 'pending')
        self.assertEqual(self.cfg.path.read_bytes(), cfg_bytes)

    # Source: actual 2.0.22 ACP review and native receipt, Codex, 2026-10-03.
    def test_current_tested_opencode_version_is_not_a_pending_candidate(self):
        from agentbus.detect import probe_opencode
        with patch('agentbus.detect._which', return_value=Path('opencode.exe')), \
             patch('agentbus.detect._version', return_value='opencode v2.0.22'):
            found = probe_opencode({}, set())
        self.assertTrue(found.can_spawn)
        self.assertFalse(found.candidate_adapter)
        self.assertIn('2.0.22', found.note)

    def test_unknown_opencode_version_remains_unaccepted(self):
        from agentbus.detect import probe_opencode
        for version in ('opencode v2.0.23', 'opencode v9.0.0', '', 'opencode v2.0.22-beta'):
            with self.subTest(version=version), \
                 patch('agentbus.detect._which', return_value=Path('opencode.exe')), \
                 patch('agentbus.detect._version', return_value=version):
                found = probe_opencode({}, set())
            self.assertFalse(found.can_spawn)
            self.assertTrue(found.candidate_adapter)

    def test_scoped_runner_rejects_unaccepted_manual_members(self):
        from types import SimpleNamespace
        from agentbus.cli import cmd_run
        from agentbus.config import ConfigError
        with patch('agentbus.cli._load', return_value=self.cfg):
            with self.assertRaises(ConfigError):
                cmd_run(SimpleNamespace(agent='workbuddy',follow_ups=False,
                    seconds=0,no_review=False,await_manual=False))

    def test_gui_distinguishes_tested_opencode_from_unaccepted_runtime_entries(self):
        from agentbus.gui import worker_label
        self.assertIn('已配置', worker_label('opencode', {'can_spawn': True}))
        self.assertNotIn('已实测', worker_label('opencode', {'can_spawn': True}))
        self.assertNotIn('任务需验收', worker_label('opencode', {'can_spawn': True}))
        self.assertIn('任务需验收', worker_label('codex', {'can_spawn': True}))
        self.assertIn('需人工接手', worker_label('opencode', {'can_spawn': False}))


if __name__ == '__main__':
    unittest.main()
