#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对 §3.9 TASK-EXCLUSIVE-LOCKING 任务排他独占锁与多 Agent 防竞态机制的定向单测。"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import shutil
import tempfile
import unittest
from pathlib import Path

from agentbus import bus as _bus
from agentbus import exclusive_lock as _exclusive_lock
from agentbus import claim_fencing as _fencing


class TestTaskExclusiveLocking(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentbus_test_lock_")
        self.workspace = Path(self.test_dir)
        self.bus = _bus.Bus(self.workspace)
        self.bus.dirs("antigravity")
        self.bus.dirs("codex")
        self.bus.dirs("workbuddy")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_concurrent_claim_blocked_by_exclusive_lock(self):
        """测试任务被某 Agent 成功认领建立排他锁后，其他 Agent 并发认领必须立刻被拦截。"""
        # 1. 投递任务到 antigravity 和 codex 的 ready/（模拟多 Agent 争抢同一任务 ID）
        task = self.bus.make("codex", "antigravity", "task", "独占锁测试", "目标描述",
                             result_to="codex", expected_artifacts=[])
        msg = self.bus.post(task)
        ready_file_ag = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        self.assertTrue(ready_file_ag.exists())

        # 2. antigravity 成功认领并原子抢占排他锁
        claimed_ag = self.bus.claim("antigravity", ready_file_ag)
        self.assertIsNotNone(claimed_ag)
        lock_info = _exclusive_lock.get_task_lock_info(self.workspace, msg.id)
        self.assertIsNotNone(lock_info)
        self.assertEqual(lock_info["owner_agent"], "antigravity")
        self.assertEqual(lock_info["claim_id"], claimed_ag.claim_id)

        # 3. 模拟 workbuddy 也尝试对同一个 task_id 建立排他锁或认领
        fake_ticket = _fencing.issue(msg.id, "workbuddy", 1)
        with self.assertRaises(_exclusive_lock.ExclusiveLockError) as ctx:
            _exclusive_lock.acquire_task_lock(self.workspace, msg.id, "workbuddy", fake_ticket)
        self.assertEqual(ctx.exception.code, "TASK_ALREADY_LOCKED")
        self.assertIn("已被成员 antigravity", str(ctx.exception))

    def test_non_owner_commit_blocked(self):
        """测试非锁持有者尝试提交成果回执时被排他锁门禁硬拦截。"""
        task = self.bus.make("codex", "antigravity", "task", "持有者校验测试", "目标描述",
                             result_to="codex", expected_artifacts=[])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        claimed_ag = self.bus.claim("antigravity", ready_file)
        ticket_ag = _fencing.captured_ticket(claimed_ag.to_dict())

        # 伪造一个 workbuddy 冒名顶替的 ticket
        fake_ticket = _fencing.ClaimTicket(
            task_id=claimed_ag.id,
            agent="workbuddy",
            claim_epoch=claimed_ag.claim_epoch,
            claim_id=claimed_ag.claim_id,
            issued_at=_fencing.iso_now()
        )

        with self.assertRaises(_exclusive_lock.ExclusiveLockError) as ctx:
            _exclusive_lock.verify_task_lock(self.workspace, claimed_ag.id, "workbuddy", fake_ticket)
        self.assertEqual(ctx.exception.code, "LOCK_OWNER_MISMATCH")

    def test_reentry_allowed_and_released_on_commit(self):
        """测试同成员同票据重入允许刷新，且在正常 commit 终结后排他锁原子归档释放。"""
        task = self.bus.make("codex", "antigravity", "task", "释放闭环测试", "目标描述",
                             result_to="codex", expected_artifacts=[])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        claimed_ag = self.bus.claim("antigravity", ready_file)
        ticket = _fencing.captured_ticket(claimed_ag.to_dict())

        # 验证重入刷新
        reenter = _exclusive_lock.acquire_task_lock(self.workspace, claimed_ag.id, "antigravity", ticket)
        self.assertIsNotNone(reenter)

        # 录入会话存活心跳（符合法定工作流管道）
        self.bus.heartbeat("antigravity", claimed_ag, ticket, note="执行中活跃心跳")

        # 提交结果
        res = self.bus.commit_result("antigravity", claimed_ag, ticket, body="测试交付结果")
        self.assertIsNotNone(res)

        # 验证排他锁已被释放并转移到 released 目录
        active_lock = _exclusive_lock.get_task_lock_info(self.workspace, claimed_ag.id)
        self.assertIsNone(active_lock, "任务终结后活动排他锁必须被移除")
        released_path = (self.workspace / _exclusive_lock.EXCLUSIVE_LOCKS_DIRNAME /
                         _exclusive_lock.RELEASED_LOCKS_DIRNAME / f"{claimed_ag.id}.released.json")
        self.assertTrue(released_path.exists(), "任务排他锁必须被归档到 released 目录")

    def test_expired_lock_can_be_taken_over(self):
        """测试锁超时后允许其他 Agent 依法接管僵尸锁。"""
        task = self.bus.make("codex", "antigravity", "task", "超时接管测试", "目标", result_to="codex", expected_artifacts=[])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        claimed_ag = self.bus.claim("antigravity", ready_file)

        # 检查初始锁信息带 expires_ts
        lock_info = _exclusive_lock.get_task_lock_info(self.workspace, msg.id)
        self.assertIn("expires_ts", lock_info)
        self.assertIn("expires_at", lock_info)

        # 模拟该锁已经超时（修改 expires_ts 为过去时间）
        lock_path = _exclusive_lock.lock_record_path(self.workspace, msg.id)
        data = _exclusive_lock.json.loads(lock_path.read_text(encoding="utf-8"))
        data["expires_ts"] = _exclusive_lock.time.time() - 100.0
        lock_path.write_text(_exclusive_lock.json.dumps(data, indent=2), encoding="utf-8")

        # workbuddy 发起抢锁，由于原锁已超时，应该成功接管
        ticket_wb = _fencing.issue(msg.id, "workbuddy", 2)
        new_lock = _exclusive_lock.acquire_task_lock(self.workspace, msg.id, "workbuddy", ticket_wb)
        self.assertEqual(new_lock["owner_agent"], "workbuddy")
        self.assertEqual(new_lock["claim_epoch"], 2)


if __name__ == "__main__":
    unittest.main()
