from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus.gui_state import (
    build_group_config, initial_workspace, load_last_workspace,
    load_branch_folds, load_member_icon_paths, load_viewport, save_group_config,
    save_branch_folds, save_last_workspace, save_member_icon_paths, save_viewport,
)
from agentbus.config import ConfigError, load


class GuiStateTests(unittest.TestCase):
    def test_group_config_requires_a_member_and_selected_coordinator(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            with self.assertRaisesRegex(ConfigError, "至少勾选"):
                build_group_config(cfg.data, [], "codex")
            with self.assertRaisesRegex(ConfigError, "组长需属于"):
                build_group_config(cfg.data, ["workbuddy"], "codex")

    def test_group_config_changes_membership_but_preserves_adapter_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = load(Path(temp), create=True)
            data = json.loads(json.dumps(cfg.data))
            data["workers"]["workbuddy"].update({
                "can_spawn": False, "human_in_loop": True,
                "sandbox": "read-only", "command": ["candidate", "--acp"],
            })

            updated = build_group_config(data, ["codex", "workbuddy"], "workbuddy")

            self.assertEqual(updated["director"], "workbuddy")
            self.assertTrue(updated["workers"]["codex"]["enabled"])
            self.assertTrue(updated["workers"]["codex"]["executor_enabled"])
            self.assertTrue(updated["workers"]["workbuddy"]["enabled"])
            self.assertTrue(updated["workers"]["workbuddy"]["executor_enabled"])
            self.assertFalse(updated["workers"]["workbuddy"]["can_spawn"])
            self.assertTrue(updated["workers"]["workbuddy"]["human_in_loop"])
            self.assertEqual(updated["workers"]["workbuddy"]["sandbox"], "read-only")
            self.assertEqual(updated["workers"]["workbuddy"]["command"],
                             ["candidate", "--acp"])
            self.assertTrue(cfg.data["workers"]["workbuddy"]["enabled"])

    def test_save_group_is_project_local_and_does_not_create_a_goal(self):
        from agentbus.bus import Bus

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = load(root / "project A", create=True)
            second = load(root / "project B", create=True)
            second_before = json.loads(json.dumps(second.data))

            saved = save_group_config(first, ["codex", "workbuddy"], "codex")

            self.assertEqual(saved.workspace, first.workspace)
            self.assertEqual(saved.data["director"], "codex")
            self.assertEqual(load(second.workspace).data, second_before)
            self.assertEqual(Bus(first.workspace / ".agentbus").pending("codex"), [])
            self.assertEqual(Bus(first.workspace / ".agentbus").pending("workbuddy"), [])
            self.assertEqual(Bus(first.workspace / ".agentbus").events(), [])

    def test_selected_workspace_is_restored_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            state_file = Path(temp) / "launcher.json"
            workspace = Path(temp) / "AI images"
            workspace.mkdir()

            save_last_workspace(workspace, state_file)

            self.assertEqual(load_last_workspace(state_file), str(workspace.resolve()))

    def test_invalid_state_does_not_prevent_launcher_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            state_file = Path(temp) / "launcher.json"
            state_file.write_text("not json", encoding="utf-8")

            self.assertEqual(load_last_workspace(state_file), "")
            state_file.write_bytes(b"\xff")
            self.assertEqual(load_last_workspace(state_file), "")

    def test_launcher_prefers_saved_workspace_over_current_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_file = root / "launcher.json"
            saved = root / "saved workspace"
            fallback = root / "where the exe was launched"
            saved.mkdir()
            fallback.mkdir()
            save_last_workspace(saved, state_file)

            self.assertEqual(initial_workspace(fallback, state_file),
                             str(saved.resolve()))

    def test_viewport_is_persisted_per_workspace_and_survives_workspace_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_file = root / 'launcher.json'
            first = root / 'project one'
            second = root / 'project two'
            first.mkdir()
            second.mkdir()
            saved = {'version': 1, 'scale': 2.4, 'panX': -310, 'panY': 88,
                     'width': 1440, 'height': 900}

            self.assertTrue(save_viewport(first, saved, state_file))
            self.assertTrue(save_last_workspace(second, state_file))

            self.assertEqual(load_viewport(first, state_file), saved)
            self.assertIsNone(load_viewport(second, state_file))
            self.assertEqual(load_last_workspace(state_file), str(second.resolve()))

    def test_viewport_rejects_invalid_values_and_missing_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_file = root / 'launcher.json'
            workspace = root / 'project'
            workspace.mkdir()
            invalid = {'version': 1, 'scale': 20, 'panX': 0, 'panY': 0,
                       'width': 800, 'height': 600}

            self.assertFalse(save_viewport(workspace, invalid, state_file))
            self.assertIsNone(load_viewport(root / 'missing', state_file))
            self.assertFalse(save_viewport(root / 'missing', invalid, state_file))

    def test_branch_folds_are_persisted_per_workspace_and_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_file = root / 'launcher.json'
            first = root / 'project one'
            second = root / 'project two'
            first.mkdir()
            second.mkdir()

            self.assertTrue(save_branch_folds(first, ['desktop', 'skills'], state_file))
            self.assertTrue(save_branch_folds(second, ['reliability'], state_file))
            self.assertEqual(load_branch_folds(first, state_file), ['desktop', 'skills'])
            self.assertEqual(load_branch_folds(second, state_file), ['reliability'])
            self.assertFalse(save_branch_folds(first, ['../private'], state_file))
            self.assertFalse(save_branch_folds(first, ['x' * 129], state_file))
            self.assertFalse(save_branch_folds(first, 'desktop', state_file))
            self.assertEqual(load_branch_folds(first, state_file), ['desktop', 'skills'])

    def test_member_icon_paths_are_saved_per_user_and_per_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_file = root / 'launcher.json'
            first = root / 'project one'
            second = root / 'project two'
            first.mkdir()
            second.mkdir()
            first_exe = root / 'first app.exe'
            second_exe = root / 'second app.exe'
            first_exe.touch()
            second_exe.touch()

            self.assertTrue(save_member_icon_paths(
                first, {'alpha': str(first_exe)}, state_file))
            self.assertTrue(save_member_icon_paths(
                second, {'beta': str(second_exe)}, state_file))

            self.assertEqual(load_member_icon_paths(first, state_file),
                             {'alpha': str(first_exe)})
            self.assertEqual(load_member_icon_paths(second, state_file),
                             {'beta': str(second_exe)})
            shared_config = json.loads((state_file).read_text(encoding='utf-8'))
            self.assertNotIn('desktop_icon_path', json.dumps(shared_config))

    def test_member_icon_paths_reject_invalid_entries_without_corrupting_other_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_file = root / 'launcher.json'
            workspace = root / 'project'
            workspace.mkdir()
            exe = root / 'agent.exe'
            exe.touch()

            self.assertTrue(save_last_workspace(workspace, state_file))
            self.assertFalse(save_member_icon_paths(
                workspace, {'../outside': str(exe)}, state_file))
            self.assertFalse(save_member_icon_paths(
                workspace, {'alpha': str(root / 'not-an-exe.txt')}, state_file))

            self.assertEqual(load_last_workspace(state_file), str(workspace.resolve()))
            self.assertEqual(load_member_icon_paths(workspace, state_file), {})


if __name__ == "__main__":
    unittest.main()
