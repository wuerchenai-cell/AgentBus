"""Codex, 2026-10-03: local admission todos without granting capabilities."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.config import load
from agentbus.profiles import add_profile
from agentbus.project_workflow import WorkflowError, prepare_project_workflow, workflow_status
from agentbus.sharing import export_template, import_template, new_template_config


class AdmissionChecklistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agentbus-admission-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cfg = load(self.base / 'project', create=True)
        self.launcher = self.base / 'AgentBus.exe'
        self.launcher.write_bytes(b'isolated launcher fixture, never execute')
        self.frozen = patch.object(sys, 'frozen', True, create=True)
        self.executable = patch.object(sys, 'executable', str(self.launcher))
        self.frozen.start()
        self.executable.start()
        self.addCleanup(self.frozen.stop)
        self.addCleanup(self.executable.stop)

    def checklist(self, cfg=None):
        cfg = cfg or self.cfg
        text = (cfg.workspace / 'AGENTBUS.md').read_text(encoding='utf-8')
        self.assertIn('## 本机接入待办', text)
        return text.split('## 本机接入待办', 1)[1].split('## 恢复顺序', 1)[0]

    def persist(self, cfg=None):
        cfg = cfg or self.cfg
        cfg.path.parent.mkdir(parents=True, exist_ok=True)
        cfg.path.write_text(json.dumps(cfg.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_selected_five_have_four_local_todos_and_no_pass_certificate(self):
        self.cfg.data['workers']['opencode'].update(enabled=True, executor_enabled=True)
        self.persist()
        before = self.cfg.path.read_bytes()
        result = prepare_project_workflow(self.cfg)
        text = self.checklist()
        for member in ('codex', 'workbuddy', 'trae', 'zcode', 'opencode'):
            self.assertIn('### `' + member + '`', text)
        self.assertEqual(text.count('- [ ]'), 20)
        for gate in ('技能发现', '握手', '真实任务', '正确回执', '不是通过证书'):
            self.assertIn(gate, text)
        self.assertIn('独立入口（已配置，未在此验收）', text)
        self.assertIn('桌面对话接手', text)
        self.assertEqual(set(result), {'agents', 'files', 'changed'})
        self.assertEqual(before, self.cfg.path.read_bytes())

    def test_sixth_candidate_gets_persistent_todos_without_commands_paths_or_capability_change(self):
        self.cfg.data['workers']['opencode'].update(enabled=True, executor_enabled=True)
        self.persist()
        self.cfg = add_profile(self.cfg, 'sixth', 'PRIVATE_LABEL_SENTINEL', 'acp',
                               ['C:/PRIVATE_PATH_SENTINEL/agent.exe', '--token=PRIVATE_TOKEN_SENTINEL'])
        before = self.cfg.path.read_bytes()
        prepare_project_workflow(self.cfg)
        text = self.checklist()
        self.assertIn('### `sixth`', text)
        self.assertEqual(text.count('- [ ]'), 24)
        self.assertIn('独立入口候选，未验收', text)
        for forbidden in ('PRIVATE_LABEL_SENTINEL', 'PRIVATE_PATH_SENTINEL',
                          'PRIVATE_TOKEN_SENTINEL', str(self.cfg.workspace), str(self.launcher),
                          'candidate_command', 'command', 'C:/'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(before, self.cfg.path.read_bytes())
        self.assertFalse(self.cfg.data['workers']['sixth']['can_spawn'])
        self.assertTrue((self.cfg.workspace / 'AGENTBUS.md').is_file())

    def test_first_friend_import_six_members_stays_manual_and_uses_same_generated_file(self):
        self.cfg.data['workers']['opencode'].update(enabled=True, executor_enabled=True)
        self.persist()
        self.cfg = add_profile(self.cfg, 'sixth', 'Sixth Agent')
        template = export_template(self.cfg)
        friend = import_template(new_template_config(self.base / 'friend'), template)
        self.persist(friend)
        before = friend.path.read_bytes()
        gates = dict(friend.gates)
        prepare_project_workflow(friend)
        text = self.checklist(friend)
        self.assertEqual(text.count('### `'), 6)
        self.assertEqual(text.count('桌面对话接手'), 6)
        for member in friend.enabled_workers().values():
            self.assertFalse(member['can_spawn'])
        self.assertEqual(before, friend.path.read_bytes())
        self.assertEqual(gates, friend.gates)
        self.assertNotIn(str(self.cfg.workspace), text)

    def test_disabled_members_are_not_listed_as_admission_todos(self):
        self.cfg.data['workers']['workbuddy']['enabled'] = False
        self.persist()
        prepare_project_workflow(self.cfg)
        text = self.checklist()
        self.assertNotIn('### `workbuddy`', text)
        self.assertNotIn('### `opencode`', text)
        self.assertEqual(text.count('### `'), len(self.cfg.enabled_workers()))

    def test_managed_legacy_entry_updates_only_entry_digest_and_keeps_skills_and_config(self):
        result = prepare_project_workflow(self.cfg)
        before_config = self.cfg.path.read_bytes()
        skills = {relative: (self.cfg.workspace / relative).read_bytes()
                  for relative in result['files'] if relative != 'AGENTBUS.md'}
        # Model a prior owned generated entry, not an unowned/user-edited file.
        entry = self.cfg.workspace / 'AGENTBUS.md'
        legacy = b'# previous AgentBus generated entry\n'
        entry.write_bytes(legacy)
        manifest_path = self.cfg.workspace / '.agentbus/workflow.json'
        record = json.loads(manifest_path.read_text(encoding='utf-8'))
        record['files']['AGENTBUS.md'] = hashlib.sha256(legacy).hexdigest()
        manifest_path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        refreshed = prepare_project_workflow(self.cfg)
        self.assertEqual(refreshed['changed'], ['AGENTBUS.md'])
        self.checklist()
        self.assertEqual(before_config, self.cfg.path.read_bytes())
        self.assertEqual(skills, {relative: (self.cfg.workspace / relative).read_bytes()
                                  for relative in skills})
        self.assertTrue(workflow_status(self.cfg)['ready'])
        self.assertEqual(prepare_project_workflow(self.cfg)['changed'], [])

    def test_user_edited_managed_entry_is_preserved_without_overwriting_checkboxes(self):
        prepare_project_workflow(self.cfg)
        text = self.checklist()
        self.assertIn('- [ ]', text)
        entry = self.cfg.workspace / 'AGENTBUS.md'
        entry.write_text(entry.read_text(encoding='utf-8').replace('- [ ]', '- [x]', 1), encoding='utf-8')
        edited = entry.read_bytes()
        before_manifest = (self.cfg.workspace / '.agentbus/workflow.json').read_bytes()
        with self.assertRaises(WorkflowError):
            prepare_project_workflow(self.cfg)
        self.assertEqual(edited, entry.read_bytes())
        self.assertEqual(before_manifest, (self.cfg.workspace / '.agentbus/workflow.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
