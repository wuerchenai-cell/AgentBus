# -*- coding: utf-8 -*-
"""§11.1 CX13-DELIVERY-GATE: 统一提交入口的心跳与产物硬门禁回归测试。

覆盖规范：
1. 无显式心跳提交硬拦截（无心跳不得产生 result/done）；
2. 心跳 599s / 600s 合格交付（边界测试）；
3. 心跳 601s 超时过期拒绝交付；
4. 必交产物清单缺失拒绝；
5. 必交产物清单缺项拒绝；
6. 产物物理文件缺失拒绝；
7. 产物物理文件实际大小与清单声明不符拒绝；
8. 产物伪造摘要（SHA256 不符）拒绝；
9. 完整合法提交成功，产生真实 result 消息并将任务迁移至 done。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from agentbus import Bus, BusError, Message
from agentbus.heartbeat import heartbeat_path


class DeliveryEntrypointGatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gate_test_"))
        self.bus_root = self.tmp / ".agentbus"
        self.bus = Bus(self.bus_root)
        self.bus.init()
        self.agent = "antigravity"
        self.bus.register(self.agent)
        self.bus.register("codex")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create_and_claim_task(self, expected_artifacts: list[str] | None = None) -> tuple[Message, dict]:
        task = Message(
            id=f"m-test-{int(time.time()*1000)}",
            ts="2026-10-13T12:00:00+08:00",
            sender="codex",
            to=self.agent,
            type="task",
            title="测试硬门禁任务",
            body="目标内容",
            result_to="codex",
            expected_artifacts=expected_artifacts or []
        )
        self.bus.post(task)
        task_path = self.bus.dirs(self.agent)["ready"] / f"{task.id}.json"
        claimed = self.bus.claim(self.agent, task_path)
        ticket = {
            "task_id": claimed.id,
            "agent": self.agent,
            "claim_epoch": claimed.claim_epoch,
            "claim_id": claimed.claim_id,
            "conversation_id": getattr(claimed, "claim_conversation_id", "")
        }
        return claimed, ticket

    def test_reject_when_no_heartbeat(self):
        """1. 无显式心跳提交硬拦截，不得产生 result 或进入 done。"""
        task, ticket = self._create_and_claim_task()
        with self.assertRaises(BusError) as ctx:
            self.bus.commit_result(
                self.agent, task, ticket,
                body="提交测试结果", artifacts=[]
            )
        self.assertIn("工作流管道门禁硬拦截", str(ctx.exception))
        self.assertIn("缺乏合法执行会话心跳溯源记录", str(ctx.exception))
        # 验证原任务仍在 processing，未进入 done
        proc_file = self.bus.dirs(self.agent)["processing"] / f"{task.id}.json"
        done_file = self.bus.dirs(self.agent)["done"] / f"{task.id}.json"
        self.assertTrue(proc_file.exists(), "任务必须保留在 processing")
        self.assertFalse(done_file.exists(), "任务决不能进入 done")

    def test_heartbeat_boundary_599_and_600_pass(self):
        """2. 心跳在 599s 和 600s 边界内应当判定合格。"""
        # 测试 599s
        task, ticket = self._create_and_claim_task()
        self.bus.heartbeat(self.agent, task, ticket, note="599s心跳")
        hb_path = heartbeat_path(self.bus_root, task.id, self.agent)
        with hb_path.open("r", encoding="utf-8") as f:
            hb_data = json.load(f)
        hb_data["at_epoch_s"] = time.time() - 598.0
        with hb_path.open("w", encoding="utf-8") as f:
            json.dump(hb_data, f)

        res = self.bus.commit_result(
            self.agent, task, ticket,
            body="599s交付成功", artifacts=[]
        )
        self.assertIsNotNone(res)
        done_file = self.bus.dirs(self.agent)["done"] / f"{task.id}.json"
        self.assertTrue(done_file.exists(), "599s 心跳应当通过门禁进入 done")

        # 测试 600s 边界（心跳刚处于 599.5s，耗时后依然 <= 600s）
        task2, ticket2 = self._create_and_claim_task()
        self.bus.heartbeat(self.agent, task2, ticket2, note="600s心跳")
        hb_path2 = heartbeat_path(self.bus_root, task2.id, self.agent)
        with hb_path2.open("r", encoding="utf-8") as f:
            hb_data2 = json.load(f)
        hb_data2["at_epoch_s"] = time.time() - 599.5
        with hb_path2.open("w", encoding="utf-8") as f:
            json.dump(hb_data2, f)

        res2 = self.bus.commit_result(
            self.agent, task2, ticket2,
            body="600s交付成功", artifacts=[]
        )
        self.assertIsNotNone(res2)
        done_file2 = self.bus.dirs(self.agent)["done"] / f"{task2.id}.json"
        self.assertTrue(done_file2.exists(), "600s 心跳边界应当通过门禁进入 done")

    def test_reject_when_heartbeat_stale_601(self):
        """3. 心跳 601s 超时过期硬拦截。"""
        task, ticket = self._create_and_claim_task()
        now = time.time()
        self.bus.heartbeat(self.agent, task, ticket, note="601s心跳")
        hb_path = heartbeat_path(self.bus_root, task.id, self.agent)
        with hb_path.open("r", encoding="utf-8") as f:
            hb_data = json.load(f)
        hb_data["at_epoch_s"] = now - 601.0
        with hb_path.open("w", encoding="utf-8") as f:
            json.dump(hb_data, f)

        with self.assertRaises(BusError) as ctx:
            self.bus.commit_result(
                self.agent, task, ticket,
                body="尝试过期提交", artifacts=[]
            )
        self.assertIn("会话心跳已超时", str(ctx.exception))
        done_file = self.bus.dirs(self.agent)["done"] / f"{task.id}.json"
        self.assertFalse(done_file.exists(), "超时心跳决不能进入 done")

    def test_reject_when_missing_artifact_manifest(self):
        """4. 声明了 expected_artifacts 但未提供产物清单被硬拦截。"""
        exp_rel = "output/file.txt"
        task, ticket = self._create_and_claim_task(expected_artifacts=[exp_rel])
        self.bus.heartbeat(self.agent, task, ticket)

        with self.assertRaises(BusError) as ctx:
            self.bus.commit_result(
                self.agent, task, ticket,
                body="未提供产物清单", artifacts=None
            )
        self.assertIn("未提供产物清单", str(ctx.exception))

        with self.assertRaises(BusError) as ctx2:
            self.bus.commit_result(
                self.agent, task, ticket,
                body="产物清单为空列表", artifacts=[]
            )
        self.assertIn("未提供产物清单", str(ctx2.exception))

        # 清单有其他项但缺少目标项
        other_manifest = [{
            "path": "output/other.txt",
            "bytes": 5,
            "sha256": "a" * 64
        }]
        with self.assertRaises(BusError) as ctx3:
            self.bus.commit_result(
                self.agent, task, ticket,
                body="清单缺少目标项", artifacts=other_manifest
            )
        self.assertIn("未在回执产物清单中提供", str(ctx3.exception))

    def test_reject_when_artifact_file_missing_on_disk(self):
        """5. 清单声明了条目但物理文件不存在被硬拦截。"""
        exp_rel = "output/missing_file.txt"
        task, ticket = self._create_and_claim_task(expected_artifacts=[exp_rel])
        self.bus.heartbeat(self.agent, task, ticket)

        fake_manifest = [{
            "path": exp_rel,
            "bytes": 100,
            "sha256": "0" * 64
        }]
        with self.assertRaises(BusError) as ctx:
            self.bus.commit_result(
                self.agent, task, ticket,
                body="文件不在磁盘", artifacts=fake_manifest
            )
        self.assertIn("物理文件不存在于磁盘", str(ctx.exception))

    def test_reject_when_artifact_bytes_mismatch(self):
        """6. 磁盘物理文件大小与清单声明不一致被硬拦截。"""
        exp_rel = "output/size_mismatch.txt"
        file_path = self.tmp / exp_rel
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("Hello World", encoding="utf-8")  # 11 bytes

        task, ticket = self._create_and_claim_task(expected_artifacts=[exp_rel])
        self.bus.heartbeat(self.agent, task, ticket)

        fake_manifest = [{
            "path": exp_rel,
            "bytes": 999,  # 错误大小
            "sha256": hashlib.sha256(b"Hello World").hexdigest()
        }]
        with self.assertRaises(BusError) as ctx:
            self.bus.commit_result(
                self.agent, task, ticket,
                body="大小不匹配", artifacts=fake_manifest
            )
        self.assertIn("实际大小", str(ctx.exception))
        self.assertIn("与清单声明", str(ctx.exception))

    def test_reject_when_artifact_sha256_fake(self):
        """7. 磁盘物理文件 SHA256 与清单伪造摘要不一致被硬拦截。"""
        exp_rel = "output/sha_fake.txt"
        file_path = self.tmp / exp_rel
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("Hello World", encoding="utf-8")

        task, ticket = self._create_and_claim_task(expected_artifacts=[exp_rel])
        self.bus.heartbeat(self.agent, task, ticket)

        fake_manifest = [{
            "path": exp_rel,
            "bytes": 11,
            "sha256": "e" * 64  # 伪造摘要
        }]
        with self.assertRaises(BusError) as ctx:
            self.bus.commit_result(
                self.agent, task, ticket,
                body="伪造SHA256", artifacts=fake_manifest
            )
        self.assertIn("伪造摘要", str(ctx.exception))

    def test_successful_delivery_with_valid_heartbeat_and_artifacts(self):
        """8. 心跳有效且真实产物完全一致，顺利交付并进入 done。"""
        exp_rel = "output/real_valid.txt"
        file_path = self.tmp / exp_rel
        file_path.parent.mkdir(parents=True, exist_ok=True)
        content = "Authentic Production Content 2026".encode("utf-8")
        file_path.write_bytes(content)

        task, ticket = self._create_and_claim_task(expected_artifacts=[exp_rel])
        self.bus.heartbeat(self.agent, task, ticket)

        valid_manifest = [{
            "path": exp_rel,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()
        }]

        result = self.bus.commit_result(
            self.agent, task, ticket,
            body="合规交付", artifacts=valid_manifest
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.type, "result")
        self.assertEqual(result.reply_to, task.id)

        # 检查进入 done
        done_file = self.bus.dirs(self.agent)["done"] / f"{task.id}.json"
        self.assertTrue(done_file.exists(), "成功交付必须进入 done 目录")


if __name__ == "__main__":
    unittest.main()
