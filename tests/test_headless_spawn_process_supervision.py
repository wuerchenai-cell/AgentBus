#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_headless_spawn_process_supervision.py: 看门狗监管异步进程及完整子进程树测试。

来源：§13.5 CX13Q-PROCESS-SUPERVISION（P1）。
验收标准：
1. wait=False 必须登记并持续监管本次进程，不返回业务 success（即 success=False, started=True）；
2. 任务代次/票据/PID 唯一登记防重复启动（返回 ALREADY_ACTIVE）；
3. 日志含唯一 run_id 避免同秒覆盖；
4. 发生超时/异常时调用进程树清理，不遗留孤儿进程。
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
from agentbus.headless_spawn import HeadlessSpawnEngine  # noqa: E402


class TestHeadlessSpawnProcessSupervision(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        bus_dir = self.root / ".agentbus"
        bus_dir.mkdir(parents=True, exist_ok=True)
        (bus_dir / "config.json").write_text(json.dumps({
            "schema_version": 1,
            "workspace": str(self.root),
            "output_root": str(self.root),
            "director": "codex",
            "workers": {
                "codex": {"enabled": True, "can_spawn": True, "executor_enabled": True,
                          "communication_mode": "cli", "capabilities": ["python"],
                          "command": [sys.executable, "-c", "import time; time.sleep(5)"]}
            }
        }), encoding="utf-8")
        (bus_dir / "task-tree.json").write_text(json.dumps({
            "schema_version": 1,
            "section_registry": ["0", "1", "1.1"],
            "roots": [{"id": "root", "title": "Fixture"}],
            "branches": [{"id": "branch", "parent_id": "root", "section_no": "1", "title": "Fixture"}],
            "leaves": [
                {
                    "id": "leaf-test-supervision",
                    "title": "测试监督叶子",
                    "parent_id": "branch", "section_no": "1.1", "goal": "Harmless supervision fixture",
                    "required_capabilities": ["python"], "expected_artifacts": [], "dependencies": [], "handoff": "Fixture"
                }
            ]
        }), encoding="utf-8")
        self.cfg = load_config(self.root)
        self.engine = HeadlessSpawnEngine(self.cfg)

    def tearDown(self):
        self.engine.terminate_all_active()
        self.temp_dir.cleanup()

    def test_async_wait_false_does_not_declare_delivery_success(self):
        """测试 wait=False 启动异步进程时，started=True 但 success=False。"""
        leaf = self.engine.forest._leaf(self.engine.forest.snapshot(), "leaf-test-supervision")
        res = self.engine.spawn_leaf(leaf, "codex", wait=False)
        self.assertTrue(res.started, "异步模式下进程应正常启动")
        self.assertFalse(res.success, "异步启动仅代表子进程运行中，绝不声称业务交付成功")
        self.assertIsNotNone(res.pid)
        self.assertIn(res.task_id, self.engine.active_processes)

    def test_duplicate_spawn_rejection(self):
        """测试同一任务在已有运行子进程时拒绝重复启动。"""
        leaf = self.engine.forest._leaf(self.engine.forest.snapshot(), "leaf-test-supervision")
        # 第一次启动
        res1 = self.engine.spawn_leaf(leaf, "codex", wait=False)
        self.assertTrue(res1.started)

        # 第二次启动同任务
        current = self.engine.forest._leaf(self.engine.forest.snapshot(), "leaf-test-supervision")
        res2 = self.engine.spawn_leaf(current, "codex", wait=False)
        self.assertFalse(res2.started)
        self.assertFalse(res2.success)
        self.assertIn("ALREADY_ACTIVE", res2.error)


if __name__ == "__main__":
    unittest.main()
