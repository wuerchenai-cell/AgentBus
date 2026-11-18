#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对 §3.10 PROJECT-WORKFLOW-PIPELINE 刚性不可绕行工作流管道与法定状态机的定向单测。"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import hashlib
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agentbus import bus as _bus
from agentbus import claim_fencing as _fencing
from agentbus import exclusive_lock as _exclusive_lock
from agentbus import heartbeat as _heartbeat
from agentbus import workflow_pipeline as _pipeline


class TestWorkflowPipeline(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentbus_test_pipe_")
        self.workspace = Path(self.test_dir)
        self.bus = _bus.Bus(self.workspace)
        self.bus.dirs("antigravity")
        self.bus.dirs("codex")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_unmet_dependency_blocks_claim(self):
        """测试任务若有未完成的前序依赖，认领动作被工作流管道硬拦截。"""
        task = self.bus.make("codex", "antigravity", "task", "依赖测试", "正文",
                             result_to="codex", expected_artifacts=[])
        # 模拟带未满足依赖
        task_dict = task.to_dict()
        task_dict["unmet_dependencies"] = ["PREV-TASK-001"]

        with self.assertRaises(_pipeline.WorkflowPipelineError) as ctx:
            _pipeline.verify_pipeline_prerequisites_for_claim(self.workspace, task_dict, "antigravity")
        self.assertEqual(ctx.exception.code, "PIPELINE_DEPENDENCY_UNMET")
        self.assertIn("未完成的前序依赖", str(ctx.exception))

    def test_missing_lock_blocks_delivery(self):
        """测试任务若未建立排他独占锁（跳步伪造），提交结果被管道直接熔断拦截。"""
        task = self.bus.make("codex", "antigravity", "task", "缺锁测试", "正文",
                             result_to="codex", expected_artifacts=[])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        claimed_msg = self.bus.claim("antigravity", ready_file)
        ticket = _fencing.captured_ticket(claimed_msg.to_dict())

        # 恶意抹除排他锁记录
        lock_path = _exclusive_lock.lock_record_path(self.workspace, claimed_msg.id)
        if lock_path.exists():
            lock_path.unlink()

        with self.assertRaises(_bus.BusError) as ctx:
            self.bus.commit_result("antigravity", claimed_msg, ticket, body="测试报告")
        self.assertIn("工作流管道熔断", str(ctx.exception))
        self.assertIn("缺乏物理排他独占锁记录", str(ctx.exception))

    def test_missing_expected_artifacts_blocks_delivery(self):
        """测试若任务声明了预期产物清单，但提交成果未覆盖时被工作流管道熔断拦截。"""
        task = self.bus.make("codex", "antigravity", "task", "产物核验测试", "正文",
                             result_to="codex", expected_artifacts=["成果/report.md"])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        claimed_msg = self.bus.claim("antigravity", ready_file)
        ticket = _fencing.captured_ticket(claimed_msg.to_dict())

        # 录入合法执行心跳，使流程推进到产物完整性校验阶段
        self.bus.heartbeat("antigravity", claimed_msg, ticket, note="测试活跃心跳")

        # 提交未包含 report.md 的回执，必须被管道拦截
        with self.assertRaises(_pipeline.WorkflowPipelineError) as ctx:
            _pipeline.verify_pipeline_prerequisites_for_delivery(
                self.workspace, claimed_msg.to_dict(), "antigravity", ticket,
                artifacts=[{"path": "成果/other.txt", "bytes": 10, "sha256": "abc"}])
        self.assertEqual(ctx.exception.code, "PIPELINE_ARTIFACTS_INCOMPLETE")
        self.assertIn("成果/report.md", str(ctx.exception))

    def test_fully_compliant_workflow_pipeline_succeeds(self):
        """测试完全遵循法定工作流管道（依赖就绪->认领加锁->心跳存活->产物匹配）全链路畅通交付。"""
        task = self.bus.make("codex", "antigravity", "task", "合规交付测试", "正文",
                             result_to="codex", expected_artifacts=["成果/final.md"])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"

        # 1. 认领加锁 + 原子心跳注入
        claimed_msg = self.bus.claim("antigravity", ready_file)
        self.assertIsNotNone(claimed_msg)
        ticket = _fencing.captured_ticket(claimed_msg.to_dict())

        # 2. 刷新活跃心跳
        _heartbeat.record(self.workspace, claimed_msg.id, "antigravity", ticket,
                          task_record=claimed_msg.to_dict(), note="正在执行测试")

        # 3. 创建真实磁盘产物并计算其精确大小与哈希
        final_file = self.workspace / "成果" / "final.md"
        final_file.parent.mkdir(parents=True, exist_ok=True)
        final_bytes = b"# Fully Compliant Test Artifact Content\n"
        final_file.write_bytes(final_bytes)
        final_sha = hashlib.sha256(final_bytes).hexdigest()

        # 4. 产物清单完整回投
        result_msg = self.bus.commit_result(
            "antigravity", claimed_msg, ticket, body="完全合规交付报告",
            artifacts=[{"path": "成果/final.md", "bytes": len(final_bytes), "sha256": final_sha}]
        )
        self.assertIsNotNone(result_msg)
        self.assertEqual(result_msg.type, "result")

        # 5. 验证排他锁已被安全归档释放
        active_lock = _exclusive_lock.get_task_lock_info(self.workspace, claimed_msg.id)
        self.assertIsNone(active_lock)


if __name__ == "__main__":
    unittest.main()
