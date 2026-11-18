#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_commit_result_caller_guard.py: §3.10 管道门禁对 commit_result 的前置条件守护测试。

来源：§10.18 AUDIT-360-PIPELINE-GATE-CALLER-MIGRATION（P0）。
验收标准：
1. 缺少合法心跳/排他锁或票据不合规时，直接调用 bus.commit_result() 必须被硬门禁拦截；
2. 拦截时抛出包含"工作流管道门禁硬拦截"或对应错误代码的异常，绝不静默放行；
3. 遵循合规流程（具备有效心跳、票据和产物完整性）提交时通过校验。
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

from agentbus.bus import Bus, BusError, Message  # noqa: E402
from agentbus.config import load as load_config  # noqa: E402


class TestCommitResultCallerGuard(unittest.TestCase):
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
                "codex": {"enabled": True, "can_spawn": True, "executor_enabled": True}
            }
        }), encoding="utf-8")
        self.cfg = load_config(self.root)
        self.bus = Bus(bus_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_heartbeat_blocked_at_commit_result(self):
        """测试未写入有效心跳直接调用 commit_result 时被硬门禁拦截。"""
        task = self.bus.make("codex", "codex", "task", body="test task body", result_to="codex")
        self.bus.post(task)

        task_path = self.bus.dirs("codex")["ready"] / f"{task.id}.json"
        claimed = self.bus.claim("codex", task_path)
        self.assertIsNotNone(claimed)

        ticket = {
            "task_id": claimed.id,
            "agent": "codex",
            "claim_epoch": claimed.claim_epoch,
            "claim_id": claimed.claim_id
        }

        # 在没有执行合法心跳/前置门禁流程的情况下直接调用 commit_result 应该被门禁拦截
        with self.assertRaises(BusError) as ctx:
            self.bus.commit_result(
                "codex", claimed, ticket,
                body="Premature result without heartbeat",
                artifacts=[]
            )
        self.assertIn("工作流管道门禁硬拦截", str(ctx.exception))

    def test_invalid_ticket_rejected(self):
        """测试伪造或非法票据调用 commit_result 时被拒绝。"""
        task = self.bus.make("codex", "codex", "task", body="test task body 2", result_to="codex")
        self.bus.post(task)

        task_path = self.bus.dirs("codex")["ready"] / f"{task.id}.json"
        claimed = self.bus.claim("codex", task_path)

        fake_ticket = {
            "task_id": task.id,
            "agent": "codex",
            "claim_epoch": 999999,
            "claim_id": "fake_claim_id"
        }

        with self.assertRaises(BusError):
            self.bus.commit_result(
                "codex", claimed, fake_ticket,
                body="Result with fake ticket",
                artifacts=[]
            )


if __name__ == "__main__":
    unittest.main()
