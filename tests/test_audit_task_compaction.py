# -*- coding: utf-8 -*-
"""测试 AUDIT-TASK-COMPACTION-ARCHIVE (§10.2) 信箱冷热分层与安全归档"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
import unittest
from pathlib import Path

from agentbus.compaction import (
    plan_compaction,
    apply_compaction,
    query_archive,
)
from agentbus.cli import cmd_compact


class TestAuditTaskCompaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / ".agentbus").mkdir(parents=True)
        (self.ws / ".agentbus" / "config.json").write_text("{}", encoding="utf-8")

        # 模拟 agent 信箱目录
        self.agent_dir = self.ws / ".agentbus" / "agents" / "worker1"
        self.done_dir = self.agent_dir / "done"
        self.done_dir.mkdir(parents=True)

        # 创建 10 个已完成任务
        for i in range(10):
            t_id = f"task_{i:02d}"
            t_file = self.done_dir / f"{t_id}.json"
            t_data = {
                "id": t_id,
                "title": f"任务 {i}",
                "status": "done",
                "result_to": "codex",
            }
            t_file.write_text(json.dumps(t_data, ensure_ascii=False), encoding="utf-8")
            # 制造时间差
            time_offset = time.time() - (10 - i) * 10
            import os
            os.utime(str(t_file), (time_offset, time_offset))

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_compaction(self):
        # 保留最近 3 个，应有 7 个候选
        plan = plan_compaction(self.ws, keep_recent=3)
        self.assertEqual(plan["candidates_count"], 7)
        self.assertGreater(plan["total_bytes"], 0)
        c_ids = [c["task_id"] for c in plan["candidates"]]
        self.assertEqual(c_ids, [f"task_{i:02d}" for i in range(7)])

    def test_apply_and_query_compaction(self):
        plan = plan_compaction(self.ws, keep_recent=3)
        res = apply_compaction(self.ws, plan, bundle_tag="test-bundle")
        self.assertTrue(res["ok"])
        self.assertEqual(res["archived_items"], 7)
        self.assertEqual(res["removed_files"], 7)

        # 验证热信箱中仅剩最新 3 个
        remaining = list(self.done_dir.glob("*.json"))
        self.assertEqual(len(remaining), 3)
        rem_ids = sorted([f.stem for f in remaining])
        self.assertEqual(rem_ids, ["task_07", "task_08", "task_09"])

        # 验证冷存储归档包与索引
        archive_dir = self.ws / ".agentbus" / "archive"
        self.assertTrue((archive_dir / "bundle-test-bundle.json.gz").exists())
        self.assertTrue((archive_dir / "index.json").exists())

        # 验证通过 query_archive 查询历史任务
        q_res = query_archive(self.ws, "task_02")
        self.assertIsNotNone(q_res)
        self.assertEqual(q_res["task_id"], "task_02")
        self.assertEqual(q_res["bundle"], "bundle-test-bundle.json.gz")
        self.assertEqual(q_res["records"][0]["content"]["title"], "任务 2")

        # 查询不存在的任务返回 None
        self.assertIsNone(query_archive(self.ws, "non_existent"))

    def test_corrupt_archive_index_aborts_before_bundle_or_source_deletion(self):
        plan = plan_compaction(self.ws, keep_recent=3)
        archive_dir = self.ws / ".agentbus" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        index = archive_dir / "index.json"
        original_index = b'{"bundles":'
        index.write_bytes(original_index)

        with self.assertRaisesRegex(ValueError, "archive index is invalid"):
            apply_compaction(self.ws, plan, bundle_tag="must-not-commit")

        self.assertEqual(index.read_bytes(), original_index)
        self.assertEqual(len(list(self.done_dir.glob("*.json"))), 10)
        self.assertFalse((archive_dir / "bundle-must-not-commit.json.gz").exists())

    def test_cmd_compact_cli(self):
        args_plan = argparse.Namespace(
            workspace=str(self.ws),
            keep_recent=4,
            apply=False,
            query=None,
            json=True,
        )
        exit_code = cmd_compact(args_plan)
        self.assertEqual(exit_code, 0)

        # 正式 apply
        args_apply = argparse.Namespace(
            workspace=str(self.ws),
            keep_recent=4,
            apply=True,
            query=None,
            json=False,
        )
        exit_code = cmd_compact(args_apply)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(list(self.done_dir.glob("*.json"))), 4)

        # CLI 查询
        args_query = argparse.Namespace(
            workspace=str(self.ws),
            keep_recent=4,
            apply=False,
            query="task_01",
            json=True,
        )
        exit_code = cmd_compact(args_query)
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
