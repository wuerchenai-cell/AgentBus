#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_headless_spawn_adapters.py: 子进程启动统一能力与宿主协议门禁测试。

来源：§13.4 CX13Q-SPAWN-ADAPTER（P1）。
验收标准：
1. 单叶与自动批量共用 enabled/executor_enabled/can_spawn 校验；
2. can_spawn=False 或禁用的成员在 claim/Popen 前被绝对拒绝；
3. 合法命令按登记适配器构造支持的参数；
4. 源码与 frozen launcher 正确分路，严禁对不支持的外部 agent 伪造命令。
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


class TestHeadlessSpawnAdapters(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        bus_dir = self.root / ".agentbus"
        bus_dir.mkdir(parents=True, exist_ok=True)
        # 团队配置：codex 可拉起，workbuddy 为 file 模式不可拉起
        (bus_dir / "config.json").write_text(json.dumps({
            "schema_version": 1,
            "workspace": str(self.root),
            "output_root": str(self.root),
            "director": "codex",
            "workers": {
                "codex": {"enabled": True, "can_spawn": True, "executor_enabled": True},
                "workbuddy": {"enabled": True, "can_spawn": False, "executor_enabled": True, "transport": "file"},
                "disabled-agent": {"enabled": False, "can_spawn": True, "executor_enabled": False}
            }
        }), encoding="utf-8")
        # 初始树
        (bus_dir / "task-tree.json").write_text(json.dumps({
            "schema_version": 1,
            "roots": [],
            "branches": [],
            "leaves": [
                {
                    "id": "leaf-test-1",
                    "title": "测试叶子1",
                    "state": "queued",
                    "claimable": True,
                    "eligible_agents": ["codex", "workbuddy"]
                }
            ]
        }), encoding="utf-8")
        self.cfg = load_config(self.root)
        self.engine = HeadlessSpawnEngine(self.cfg)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_spawnable_predicate(self):
        """测试 is_spawnable 能够精确反映成员配置门禁。"""
        self.assertTrue(self.engine.is_spawnable("codex"))
        self.assertFalse(self.engine.is_spawnable("workbuddy"), "can_spawn=false 成员必须为 False")
        self.assertFalse(self.engine.is_spawnable("disabled-agent"), "已禁用的成员必须为 False")
        self.assertFalse(self.engine.is_spawnable("unknown-member"), "未知成员必须为 False")

    def test_refuse_spawn_when_can_spawn_false(self):
        """测试对不可拉起成员调用 spawn_leaf 会在 Popen 前被绝对拦截。"""
        leaf = {
            "id": "leaf-test-1",
            "title": "测试叶子1",
            "state": "queued"
        }
        res = self.engine.spawn_leaf(leaf, "workbuddy")
        self.assertFalse(res.started, "不可启动成员不得触发进程启动")
        self.assertFalse(res.success)
        self.assertIn("ADAPTER_GATE_REFUSAL", res.error)

    def test_build_spawn_command_structure(self):
        """测试合法命令构造遵循规范参数。"""
        leaf = {"id": "leaf-test-1", "title": "测试叶子1"}
        cmd = self.engine.build_spawn_command("codex", leaf, "task-123")
        self.assertIsInstance(cmd, list)
        self.assertTrue(len(cmd) >= 3)
        self.assertIn("--workspace", cmd)
        self.assertIn(str(self.root), cmd)


if __name__ == "__main__":
    unittest.main()
