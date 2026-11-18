# -*- coding: utf-8 -*-
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
import unittest

# Ensure agentbus package importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.config import load
from agentbus.bus import Bus
from agentbus.task_tree import TaskForest
from agentbus.cli import cmd_claim_next, cmd_answer, cmd_wait, build_parser


class TestTierBudgetAndWait(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="agentbus-tier-test-")
        self.ws = Path(self.temp_dir)
        self.agentbus_dir = self.ws / ".agentbus"
        self.agentbus_dir.mkdir(parents=True, exist_ok=True)
        (self.agentbus_dir / "inboxes").mkdir(exist_ok=True)
        (self.agentbus_dir / "receipts").mkdir(exist_ok=True)
        (self.agentbus_dir / "leases").mkdir(exist_ok=True)
        (self.agentbus_dir / "operation-locks").mkdir(exist_ok=True)
        (self.agentbus_dir / "exclusive-locks").mkdir(exist_ok=True)

        self.cfg_data = {
            "version": 1,
            "project_name": "test-tier",
            "workers": {"codex": {"enabled": True}, "workbuddy": {"enabled": True}},
            "director": "codex",
            "communication_mode": "file",
            "assignment_mode": "autonomous"
        }
        (self.agentbus_dir / "config.json").write_text(json.dumps(self.cfg_data), encoding="utf-8")
        self.cfg = load(self.ws)
        self.forest = TaskForest(self.cfg)
        self.parser = build_parser()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_claim_next_with_cost_tier_preference(self):
        """测试不同模型/工位按 cost_tier (如 critical vs low) 偏好智能匹配认领"""
        tree_data = {
            "schema_version": 1,
            "section_registry": ["1.1", "1.2"],
            "roots": [{"id": "root-1", "title": "根规划", "goal": "目标"}],
            "branches": [{"id": "branch-1", "title": "分支1", "parent_id": "root-1", "goal": "目标"}],
            "leaves": [
                {
                    "id": "task-critical",
                    "title": "高阶架构审核任务",
                    "goal": "核心把关",
                    "parent_id": "branch-1",
                    "section_no": "1.1",
                    "cost_tier": "critical",
                    "token_budget": 5000,
                    "expected_artifacts": []
                },
                {
                    "id": "task-low",
                    "title": "简单测试用例补齐",
                    "goal": "机械执行",
                    "parent_id": "branch-1",
                    "section_no": "1.2",
                    "cost_tier": "low",
                    "token_budget": 1000,
                    "expected_artifacts": []
                }
            ]
        }
        self.forest.replace_structure(tree_data)

        # 1. workbuddy (模拟轻量执行端) 声明 --tier low 抢单，应优先抢到 task-low
        res_wb = self.forest.claim_next("workbuddy", tier="low")
        self.assertEqual(res_wb["status"], "claimed")
        self.assertEqual(res_wb["leaf"]["id"], "task-low")
        self.assertEqual(res_wb["leaf"]["cost_tier"], "low")

        # 2. codex (模拟主脑) 声明 --tier critical 抢单，应抢到 task-critical
        res_cx = self.forest.claim_next("codex", tier="critical")
        self.assertEqual(res_cx["status"], "claimed")
        self.assertEqual(res_cx["leaf"]["id"], "task-critical")
        self.assertEqual(res_cx["leaf"]["cost_tier"], "critical")

    def test_answer_tokens_and_cost_recording(self):
        """测试交付成果时记录 tokens 消耗与成本，并在全树聚合"""
        tree_data = {
            "schema_version": 1,
            "section_registry": ["2.1"],
            "roots": [{"id": "r2", "title": "根", "goal": "目"}],
            "branches": [{"id": "b2", "title": "分", "parent_id": "r2", "goal": "目"}],
            "leaves": [
                {
                    "id": "task-eval",
                    "title": "评估任务",
                    "goal": "测算",
                    "parent_id": "b2",
                    "section_no": "2.1",
                    "cost_tier": "standard",
                    "expected_artifacts": ["output.txt"]
                }
            ]
        }
        self.forest.replace_structure(tree_data)

        # 认领
        claim_res = self.forest.claim_next("codex")
        self.assertEqual(claim_res["status"], "claimed")
        task_id = claim_res["message"].id
        from agentbus.claim_fencing import captured_ticket
        original_ticket = captured_ticket(claim_res['message'])

        # 写入物理产物
        (self.ws / "output.txt").write_text("hello tokens", encoding="utf-8")

        # 提交回执，省略 --id（自动推断当前在途任务），带 --tokens 3200 和 --cost 0.08
        body_file = self.ws / "receipt.md"
        body_file.write_text("完成核验", encoding="utf-8")
        import hashlib
        body_sha = hashlib.sha256(body_file.read_bytes()).hexdigest()

        args = self.parser.parse_args([
            "answer",
            "--workspace", str(self.ws),
            "--agent", "codex",
            "--claim-epoch", str(original_ticket.claim_epoch),
            "--claim-id", original_ticket.claim_id,
            "--body-file", "receipt.md",
            "--expected-body-sha256", body_sha,
            "--auto-manifest",
            "--tokens", "3200",
            "--cost", "0.08"
        ])
        code = cmd_answer(args)
        self.assertEqual(code, 0)

        # 验证全树快照聚合
        snap = self.forest.snapshot()
        self.assertIn("token_summary", snap)
        summary = snap["token_summary"]
        self.assertEqual(summary["total_tokens"], 3200)
        self.assertAlmostEqual(summary["total_cost"], 0.08)
        self.assertIn("codex", summary["by_agent"])
        self.assertEqual(summary["by_agent"]["codex"]["tokens"], 3200)
        self.assertEqual(summary["by_agent"]["codex"]["count"], 1)

        # 验证 cmd_wait 检测已交付任务立刻返回 0
        wait_args = self.parser.parse_args([
            "wait",
            "--workspace", str(self.ws),
            "--id", task_id,
            "--timeout", "1.0",
            "--poll-interval", "0.2"
        ])
        wait_code = cmd_wait(wait_args)
        self.assertEqual(wait_code, 0)

    def test_cmd_wait_returns_immediately_on_failed_or_blocked(self):
        """测试 cmd_wait 检测到 done/ 中的 failed 或 blocked/ 中的受阻消息时立即返回 1（不傻等超时）"""
        bus = Bus(self.ws / ".agentbus")
        bus.init()
        bus.register("codex")
        bus.register("workbuddy")
        msg = bus.post(bus.make(sender="codex", to="workbuddy", type="task", title="测试失败任务", body="执行", result_to="codex"))
        claimed = bus.claim("workbuddy", bus.dirs("workbuddy")["ready"] / f"{msg.id}.json")
        bus.block("workbuddy", claimed, "依赖外部环境缺失")

        t0 = time.monotonic()
        wait_args = self.parser.parse_args([
            "wait",
            "--workspace", str(self.ws),
            "--id", msg.id,
            "--timeout", "5.0",
            "--poll-interval", "0.5"
        ])
        code = cmd_wait(wait_args)
        elapsed = time.monotonic() - t0
        self.assertEqual(code, 1)
        self.assertLess(elapsed, 1.5, "检测到 blocked 状态应立即返回 1，不应等待至 5.0s 超时")

    def test_cmd_wait_timeout(self):
        """测试 cmd_wait 在无事件超时时能够安全返回 1，且不阻塞"""
        args = self.parser.parse_args([
            "wait",
            "--workspace", str(self.ws),
            "--id", "m-nonexistent-9999",
            "--timeout", "0.5",
            "--poll-interval", "0.2"
        ])
        code = cmd_wait(args)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
