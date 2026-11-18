"""Unit tests for §12.1 Autonomous Worker Loop engine.
Isolated sandbox tests ensuring full portability.
"""
import copy
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agentbus import config as ab_config
from agentbus.task_tree import TaskForest
from agentbus.worker_loop import WorkerLoop, WorkerLoopError, HeartbeatKeeper


class TestWorkerLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbus_test_worker_loop_"))
        self.agentbus_dir = self.tmp / ".agentbus"
        self.agentbus_dir.mkdir(parents=True)

        config_data = {
            "director": "codex",
            "coordinator": "antigravity",
            "workers": {
                "codex": {"enabled": True, "communication_mode": "file", "capabilities": ["python"]},
                "antigravity": {"enabled": True, "communication_mode": "file", "capabilities": ["python", "testing", "cli-design"]},
            },
            "gates": {"heartbeat_timeout_s": 600},
        }
        (self.agentbus_dir / "config.json").write_text(
            json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.cfg = ab_config.load(self.tmp)

        # Seed minimal task tree
        tree_data = {
            "schema_version": 1,
            "section_registry": ["0", "12", "12.1"],
            "roots": [{"id": "project", "title": "Test Project"}],
            "branches": [{"id": "branch-automation", "parent_id": "project", "section_no": "12", "title": "Automation"}],
            "leaves": [
                {
                    "id": "TASK-WORKER-01",
                    "parent_id": "branch-automation",
                    "title": "Test Worker Task",
                    "goal": "Verify worker loop processing",
                    "section_no": "12.1",
                    "dependencies": [],
                    "required_capabilities": ["python", "testing"],
                    "expected_artifacts": ["test.txt"],
                    "handoff": "Ready for execution",
                    "history_task_ids": [],
                }
            ],
        }
        (self.agentbus_dir / "task-tree.json").write_text(
            json.dumps(tree_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.forest = TaskForest(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_worker_loop_invalid_agent(self):
        with self.assertRaises(WorkerLoopError):
            WorkerLoop(self.cfg, agent="non-existent-agent-xyz")

    def test_worker_loop_initialization(self):
        loop = WorkerLoop(self.cfg, agent="antigravity", poll_interval=1.0, max_tasks=5)
        self.assertEqual(loop.agent, "antigravity")
        self.assertEqual(loop.poll_interval, 1.0)
        self.assertEqual(loop.max_tasks, 5)

    def test_scan_ready_tasks(self):
        loop = WorkerLoop(self.cfg, agent="antigravity")
        ready_tasks = loop.scan_ready_tasks()
        self.assertEqual(len(ready_tasks), 1)
        self.assertEqual(ready_tasks[0]["id"], "TASK-WORKER-01")

    def test_heartbeat_keeper_lifecycle(self):
        keeper = HeartbeatKeeper(self.agentbus_dir, "test-task-123", "antigravity", interval_s=0.1)
        keeper.start()
        time.sleep(0.3)
        keeper.stop()
        self.assertFalse(keeper._thread.is_alive())

    def test_run_with_custom_handler_and_auto_claim(self):
        executed = []

        def dummy_handler(leaf):
            executed.append(leaf["id"])
            return {"status": "ok", "leaf": leaf["id"]}

        loop = WorkerLoop(
            self.cfg,
            agent="antigravity",
            poll_interval=0.1,
            max_tasks=1,
            handler=dummy_handler,
        )
        report = loop.run(continuous=False, max_idle_loops=1)
        self.assertEqual(report["agent"], "antigravity")
        self.assertEqual(report["total_processed"], 1)
        self.assertEqual(report["succeeded"], 0)
        self.assertEqual(report["handled"], 1)
        self.assertEqual(report["awaiting_delivery"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(executed, ["TASK-WORKER-01"])

        # The original claim has an explicit heartbeat, so the projection is running.
        s = self.forest.snapshot()
        leaf = next(l for l in s["leaves"] if l["id"] == "TASK-WORKER-01")
        self.assertEqual(leaf["state"], "running")
        self.assertEqual(leaf["owner"], "antigravity")

    def test_takeover_capsule_includes_autonomous_loop_rule(self):
        leaf = next(l for l in self.forest.snapshot()["leaves"] if l["id"] == "TASK-WORKER-01")
        capsule = self.forest.generate_takeover_capsule(leaf, "antigravity")
        prompt = capsule["prompt"]
        self.assertIn("自驱动工蜂连续工作循环", prompt)
        self.assertIn("agentbus claim-next", prompt)
        self.assertIn("--auto-manifest", prompt)
        self.assertNotIn("worker-loop", prompt)
        self.assertIn("严禁直接停止工作或退出会话", prompt)


if __name__ == "__main__":
    unittest.main()
