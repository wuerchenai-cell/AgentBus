# -*- coding: utf-8 -*-
"""测试 AUDIT-360-CLI-COGNITIVE-LOAD (§10.10) CLI 分组呈现与路径净化"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from agentbus.cli import _HELP_EPILOG, main
from tools.cli_surface_check import run_surface_check


class TestAuditCliCognitiveLoad(unittest.TestCase):
    def test_epilog_contains_quickstart_and_domains(self):
        """验证帮助尾注包含高频 5 场景与功能域分组"""
        epilog = _HELP_EPILOG()
        self.assertIn("高频快速入口", epilog)
        self.assertIn("agentbus gui", epilog)
        self.assertIn("agentbus init --quickstart", epilog)
        self.assertIn("agentbus inbox", epilog)
        self.assertIn("agentbus claim", epilog)
        self.assertIn("agentbus answer", epilog)
        self.assertIn("功能域分组", epilog)
        self.assertIn("[任务与流转]", epilog)
        self.assertIn("[协作与接入]", epilog)
        self.assertIn("[诊断与体检]", epilog)
        self.assertIn("[性能与维护]", epilog)
        self.assertIn("[计划与配置]", epilog)

    def test_path_sanitization(self):
        """验证错误信息路径净化，不泄露工具链内部路径"""
        proc = subprocess.run(
            [sys.executable, "-m", "agentbus.cli", "tree", "status", "--workspace", "/nonexistent_sample"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=str(_ROOT_DIR),
            env={**os.environ, "PYTHONPATH": str(_ROOT_DIR)},
        )
        err = proc.stderr + proc.stdout
        self.assertNotIn("portablegit", err.lower())
        self.assertIn("下一步建议", err)
        self.assertEqual(proc.returncode, 2)  # 退出码保持 2（配置错误）

    def test_cli_surface_check_tool_passes(self):
        """验证 38+ 个子命令全量表面检查通过"""
        res = run_surface_check()
        self.assertEqual(res["failed_commands"], [])
        self.assertTrue(res["has_quick_start"])
        self.assertTrue(res["has_domain_grouping"])
        self.assertTrue(res["path_sanitized"])
        self.assertTrue(res["all_passed"])


if __name__ == "__main__":
    unittest.main()
