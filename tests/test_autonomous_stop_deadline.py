#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_autonomous_stop_deadline.py: 自动执行每项检查停止与时间预算测试。

来源：§13.3 CX13Q-STOP-DEADLINE（P1）。
验收标准：
1. STOP/总期限须在每个叶子进入 handler 或 Popen 前检查；
2. 存在 STOP 文件时，execute_task 返回 success=False 并标明 STOP_REQUESTED；
3. 期限不足或超期时不启动执行，返回 DEADLINE_EXCEEDED；
4. 不把暂停派工误写成任务完成。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

# 导入待测集成模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
import sys
sys.path.insert(0, str(PROJECT_DIR))

from agentbus.config import load as load_config  # noqa: E402
from agentbus.worker_loop import WorkerLoop  # noqa: E402
from agentbus.headless_spawn import HeadlessSpawnEngine  # noqa: E402


class TestAutonomousStopDeadline(unittest.TestCase):
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
            "roots": [],
            "branches": [],
            "leaves": [
                {
                    "id": "leaf-stop-1",
                    "title": "测试叶子-停止",
                    "state": "claimed",
                    "task_id": "m-task-stop-01",
                    "claimable": False,
                    "owner": "codex"
                }
            ]
        }), encoding="utf-8")
        self.cfg = load_config(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_worker_loop_stops_on_stop_file(self):
        """测试在存在 STOP 文件时，worker_loop 拒绝执行任务。"""
        stop_file = self.bus_dir / "STOP"
        stop_file.write_text("STOP", encoding="utf-8")

        handler_called = [False]
        def fake_handler(leaf):
            handler_called[0] = True
            return {"status": "done"}

        loop = WorkerLoop(self.cfg, "codex", handler=fake_handler)
        leaf = {
            "id": "leaf-stop-1",
            "state": "claimed",
            "owner": "codex",
            "task_id": "m-task-stop-01"
        }
        res = loop.execute_task(leaf)
        self.assertFalse(res["success"])
        self.assertIn("STOP_REQUESTED", res["error"])
        self.assertFalse(handler_called[0], "存在 STOP 文件时绝不调用 handler")

    def test_worker_loop_stops_on_exceeded_deadline(self):
        """测试在超过截止时间时，worker_loop 拒绝执行任务。"""
        handler_called = [False]
        def fake_handler(leaf):
            handler_called[0] = True
            return {"status": "done"}

        loop = WorkerLoop(self.cfg, "codex", handler=fake_handler)
        leaf = {
            "id": "leaf-stop-1",
            "state": "claimed",
            "owner": "codex",
            "task_id": "m-task-stop-01",
            "deadline_ts": time.time() - 100.0  # 过去的时间戳
        }
        res = loop.execute_task(leaf)
        self.assertFalse(res["success"])
        self.assertIn("DEADLINE_EXCEEDED", res["error"])
        self.assertFalse(handler_called[0], "超时任务绝不调用 handler")

    def test_spawn_stops_on_stop_file(self):
        """测试存在 STOP 文件时 headless_spawn 拒绝启动子进程。"""
        stop_file = self.bus_dir / "STOP"
        stop_file.write_text("STOP", encoding="utf-8")

        engine = HeadlessSpawnEngine(self.cfg)
        leaf = {
            "id": "leaf-stop-1",
            "task_id": "m-task-stop-01",
            "state": "claimed",
            "owner": "codex"
        }
        res = engine.spawn_leaf(leaf, "codex")
        self.assertFalse(res.started)
        self.assertFalse(res.success)
        self.assertIn("STOP_REQUESTED", res.error)


if __name__ == "__main__":
    unittest.main()
