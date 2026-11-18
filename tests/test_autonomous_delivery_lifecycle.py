#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_autonomous_delivery_lifecycle.py: 执行循环与启动结果绑定真实回执测试。

来源：§13.1 CX13Q-WORKER-LIFECYCLE（P1）。
验收标准：
1. 无 handler 不得返回 success=True，区分已领取与业务交付；
2. 注入合法 handler 执行后正常返回 success=True；
3. 单任务 max_tasks 约束正常生效；
4. 失败有正式阶段记录，不留无人负责的 claimed 悬挂。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

# 导入待测集成模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
import sys
sys.path.insert(0, str(PROJECT_DIR))

from agentbus.config import load as load_config  # noqa: E402
from agentbus.worker_loop import WorkerLoop  # noqa: E402
from agentbus.task_tree import TaskForest


class TestAutonomousDeliveryLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bus_dir = self.root / ".agentbus"
        self.bus_dir.mkdir(parents=True, exist_ok=True)
        (self.bus_dir / "config.json").write_text(json.dumps({
            "schema_version": 1,
            "workspace": str(self.root),
            "output_root": str(self.root),
            "director": "codex",
            "workers": {
                "codex": {"enabled": True, "can_spawn": True, "executor_enabled": True}
            }
        }), encoding="utf-8")
        (self.bus_dir / "task-tree.json").write_text(json.dumps({
            "schema_version": 1,
            "section_registry": ["0", "1", "1.1"],
            "roots": [{"id": "root", "title": "Fixture"}],
            "branches": [{"id": "branch", "parent_id": "root", "section_no": "1", "title": "Fixture"}],
            "leaves": [
                {
                    "id": "leaf-lifecycle-1",
                    "title": "测试生命周期叶子",
                    "parent_id": "branch", "section_no": "1.1", "goal": "Verify lifecycle",
                    "dependencies": [], "required_capabilities": [], "expected_artifacts": [], "handoff": "Fixture"
                }
            ]
        }), encoding="utf-8")
        self.cfg = load_config(self.root)
        self.forest = TaskForest(self.cfg)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_no_handler_returns_failure_without_fake_success(self):
        """测试在未注入 handler 时，execute_task 返回 success=False，绝不虚报成功。"""
        loop = WorkerLoop(self.cfg, "codex", handler=None)
        leaf = {
            "id": "leaf-lifecycle-1",
            "state": "claimed",
            "owner": "codex",
            "task_id": "m-task-life-01"
        }
        res = loop.execute_task(leaf)
        self.assertFalse(res["success"], "无 handler 绝不得声称任务成功")
        self.assertIn("NO_HANDLER", res["error"])
        self.assertEqual(res["result"]["status"], "awaiting_handler")

    def test_with_handler_returns_success(self):
        """测试在注入真实 handler 时，execute_task 正常执行并返回 success=True。"""
        def success_handler(leaf):
            msg = self.forest.bus._active_record("codex", leaf["task_id"])
            self.forest.bus.commit_result("codex", msg, leaf["claim_ticket"], body="Verified fixture receipt", exit_code=0)
            return {"status": "executed", "leaf_id": leaf["id"]}

        loop = WorkerLoop(self.cfg, "codex", handler=success_handler)
        leaf = self.forest._leaf(self.forest.snapshot(), "leaf-lifecycle-1")
        res = loop.execute_task(leaf)
        self.assertTrue(res["success"])
        self.assertEqual(res["error"], "")
        self.assertEqual(res["result"]["status"], "executed")


if __name__ == "__main__":
    unittest.main()
