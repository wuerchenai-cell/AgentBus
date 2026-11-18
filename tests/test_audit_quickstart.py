# -*- coding: utf-8 -*-
"""测试 AUDIT-ZERO-CONFIG-QUICKSTART-GUIDE (§10.3) 零配置快速向导"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.quickstart import run_quickstart
from agentbus.config import Config


class TestAuditQuickstart(unittest.TestCase):
    def test_run_quickstart_scaffolding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            res = run_quickstart(ws, print_output=False)
            self.assertTrue(res["ok"])
            self.assertIn("director", res)
            self.assertTrue((ws / ".agentbus" / "config.json").exists())
            self.assertTrue((ws / "AGENTS.md").exists())
            self.assertTrue((ws / "TASKBOOK.md").exists())
            self.assertTrue((ws / "MEMORY.md").exists())
            self.assertTrue((ws / "STATE.md").exists())
            self.assertTrue((ws / "PROJECT.md").exists())

            # 验证初始任务树
            tree_file = ws / ".agentbus" / "task-tree.json"
            self.assertTrue(tree_file.exists())
            tree_data = json.loads(tree_file.read_text(encoding="utf-8"))
            self.assertEqual(len(tree_data["branches"]), 1)
            self.assertEqual(tree_data["branches"][0]["id"], "welcome")
            self.assertEqual(len(tree_data["leaves"]), 1)
            self.assertEqual(tree_data["leaves"][0]["section_no"], "0.1")
            self.assertEqual(tree_data["leaves"][0]["id"], "WELCOME-KICKOFF")

            # 验证向导指引文本输出内容
            self.assertIn("AgentBus 3 分钟零配置极速上手向导", res["guide_text"])
            self.assertIn("极速起步 3 步操作路线", res["guide_text"])

    def test_run_quickstart_coordinator_override(self):
        from agentbus.config import load
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            res = run_quickstart(ws, coordinator_override="antigravity", print_output=False)
            self.assertTrue(res["ok"])
            self.assertEqual(res["director"], "antigravity")
            cfg = load(ws)
            self.assertEqual(cfg.director, "antigravity")

    def test_quickstart_cli_integration(self):
        import argparse
        from agentbus.cli import cmd_init
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            args = argparse.Namespace(
                workspace=str(ws),
                output_root=None,
                quickstart=True,
                json=False,
                coordinator="codex",
                dry_run=False,
            )
            exit_code = cmd_init(args)
            self.assertEqual(exit_code, 0)
            self.assertTrue((ws / ".agentbus" / "config.json").exists())
            self.assertTrue((ws / ".agentbus" / "task-tree.json").exists())


if __name__ == "__main__":
    unittest.main()
