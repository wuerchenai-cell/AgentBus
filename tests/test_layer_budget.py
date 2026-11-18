#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_layer_budget.py: 测试分层墙钟预算与可移植性测试。

来源：§10.12 AUDIT-360-TEST-PORTABILITY（P2）。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import importlib.util

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
TOOLS_DIR = PROJECT_DIR / "tools"

_spec = importlib.util.spec_from_file_location("layer_budget_tool", str(TOOLS_DIR / "test_layer_budget.py"))
test_layer_budget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_layer_budget)


class TestLayerBudget(unittest.TestCase):
    def test_check_symlink_capability(self):
        """验证主机环境符号链接能力探测接口。"""
        res = test_layer_budget.check_symlink_capability()
        self.assertIn("can_symlink", res)
        self.assertIn("platform", res)
        self.assertIn("reason", res)
        self.assertIsInstance(res["can_symlink"], bool)

    def test_layer_budgets_defined(self):
        """验证 4 大分层墙钟预算定义完整且指标严格。"""
        budgets = test_layer_budget.LAYER_BUDGETS
        self.assertIn("smoke", budgets)
        self.assertIn("contract", budgets)
        self.assertIn("release", budgets)
        self.assertIn("full", budgets)

        self.assertLessEqual(budgets["smoke"], 30.0)
        self.assertLessEqual(budgets["contract"], 120.0)
        self.assertLessEqual(budgets["release"], 300.0)

    def test_smoke_layer_budget_execution(self):
        """验证 smoke 分层实际执行耗时达标且全部通过。"""
        res = test_layer_budget.run_tier("smoke")
        self.assertTrue(res["success"], f"Smoke layer failed: {res}")
        self.assertTrue(res["within_budget"], f"Smoke elapsed {res['elapsed_s']}s exceeded budget {res['budget_s']}s")
        self.assertGreater(res["tests"], 0)

    def test_cli_smoke_check_invocation(self):
        """验证 CLI 入口在 --check 模式下可正常运行并通过。"""
        script = TOOLS_DIR / "test_layer_budget.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--tier", "smoke", "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, f"CLI exited with {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("[PASS]", proc.stdout)


if __name__ == "__main__":
    unittest.main()
