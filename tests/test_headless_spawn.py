"""Unit tests for §12.2 Headless Spawn Engine.
Isolated sandbox tests verifying subprocess execution and watchdog guards.
"""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentbus import config as ab_config
from agentbus.headless_spawn import HeadlessSpawnEngine, SpawnResult
from agentbus.task_tree import TaskForest


class TestHeadlessSpawn(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbus_test_spawn_"))
        self.agentbus_dir = self.tmp / ".agentbus"
        self.agentbus_dir.mkdir(parents=True)

        config_data = {
            "director": "codex",
            "coordinator": "antigravity",
            "workers": {
                "codex": {
                    "enabled": True,
                    "can_spawn": True,
                    "communication_mode": "cli",
                    "command": [sys.executable, "-c", "import sys; print('spawn_codex_ok'); sys.exit(0)"],
                    "capabilities": ["python", "testing"],
                },
                "gui-agent": {
                    "enabled": True,
                    "can_spawn": False,
                    "communication_mode": "file",
                    "capabilities": ["python"],
                },
                "slow-worker": {
                    "enabled": True,
                    "can_spawn": True,
                    "communication_mode": "cli",
                    "command": [sys.executable, "-c", "import time; time.sleep(10)"],
                    "capabilities": ["python", "testing"],
                },
            },
            "gates": {"heartbeat_timeout_s": 600},
        }
        (self.agentbus_dir / "config.json").write_text(
            json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.cfg = ab_config.load(self.tmp)

        tree_data = {
            "schema_version": 1,
            "section_registry": ["0", "12", "12.2"],
            "roots": [{"id": "project", "title": "Test Project"}],
            "branches": [{"id": "branch-automation", "parent_id": "project", "section_no": "12", "title": "Automation"}],
            "leaves": [
                {
                    "id": "TASK-SPAWN-01",
                    "parent_id": "branch-automation",
                    "title": "Headless Test Task",
                    "goal": "Verify spawn execution",
                    "section_no": "12.2",
                    "dependencies": [],
                    "required_capabilities": ["python", "testing"],
                    "expected_artifacts": [],
                    "handoff": "Ready to spawn",
                    "history_task_ids": [],
                }
            ],
        }
        (self.agentbus_dir / "task-tree.json").write_text(
            json.dumps(tree_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.forest = TaskForest(self.cfg)
        self.engine = HeadlessSpawnEngine(self.cfg, default_timeout_s=5.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_spawnable(self):
        self.assertTrue(self.engine.is_spawnable("codex"))
        self.assertFalse(self.engine.is_spawnable("gui-agent"))
        self.assertFalse(self.engine.is_spawnable("unknown-agent"))

    def test_build_spawn_command(self):
        leaf = self.forest._leaf(self.forest.snapshot(), "TASK-SPAWN-01")
        cmd = self.engine.build_spawn_command("codex", leaf, "t-123")
        self.assertEqual(cmd[0], sys.executable)
        self.assertIn("--task-id", cmd)
        self.assertIn("t-123", cmd)

    def test_spawn_leaf_execution_success(self):
        leaf = self.forest._leaf(self.forest.snapshot(), "TASK-SPAWN-01")
        res = self.engine.spawn_leaf(leaf, "codex", timeout_s=5.0, wait=True)
        self.assertTrue(res.process_success)
        self.assertFalse(res.success, "A process exit alone does not verify delivery")
        self.assertEqual(res.exit_code, 0)
        self.assertTrue(Path(res.log_path).exists())
        log_content = Path(res.log_path).read_text(encoding="utf-8")
        self.assertIn("spawn_codex_ok", log_content)

    def test_spawn_leaf_watchdog_timeout(self):
        leaf = self.forest._leaf(self.forest.snapshot(), "TASK-SPAWN-01")
        res = self.engine.spawn_leaf(leaf, "slow-worker", timeout_s=0.5, wait=True)
        self.assertFalse(res.success)
        self.assertEqual(res.exit_code, -1)

    def test_auto_spawn_ready(self):
        # TASK-SPAWN-01 is queued and eligible for codex
        results = self.engine.auto_spawn_ready(max_spawn=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].agent, "codex")
        self.assertTrue(results[0].process_success)
        self.assertFalse(results[0].success)


if __name__ == "__main__":
    unittest.main()
