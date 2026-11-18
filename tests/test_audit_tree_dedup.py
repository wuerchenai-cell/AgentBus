# -*- coding: utf-8 -*-
"""测试 AUDIT-360-TREE-DEDUP-MECHANISM (§10.14) 任务树只读查重工具"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.tree_dedup_check import tokenize, score_leaf, check_dedup


class TestAuditTreeDedup(unittest.TestCase):
    def test_tokenize(self):
        tokens = tokenize("快速上手与CLI测试")
        self.assertIn("快速", tokens)
        self.assertIn("上手", tokens)
        self.assertIn("cli", tokens)
        self.assertIn("测试", tokens)

    def test_score_leaf(self):
        leaf = {
            "title": "设计系统深浅双模态",
            "goal": "重构现代化设计系统令牌，支持暗黑模式",
        }
        query_tokens = tokenize("设计系统")
        score = score_leaf("设计系统", query_tokens, leaf)
        self.assertGreaterEqual(score, 0.8)

    def test_check_dedup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            tree_file = ws / "task-tree.json"
            mock_data = {
                "leaves": [
                    {
                        "section_no": "1.1",
                        "id": "T-01",
                        "title": "零配置快速上手向导",
                        "goal": "提供命令行新手极速启动向导",
                    },
                    {
                        "section_no": "1.2",
                        "id": "T-02",
                        "title": "数据库持久层设计",
                        "goal": "PostgreSQL 表设计",
                    },
                ]
            }
            tree_file.write_text(json.dumps(mock_data), encoding="utf-8")

            results = check_dedup(tree_file, "快速上手")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], "T-01")
            self.assertGreater(results[0]["score"], 0.8)


if __name__ == "__main__":
    unittest.main()
