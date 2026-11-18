#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_contrast_audit.py: 前端 WCAG AA 对比度与字号阶梯门禁测试。

来源：§10.9 AUDIT-360-A11Y-CONTRAST-FONT（P1）。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

# 导入待测模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
TOOLS_DIR = PROJECT_DIR / "tools"
sys.path.insert(0, str(PROJECT_DIR))

from tools import contrast_audit


class TestContrastAudit(unittest.TestCase):
    def test_contrast_ratio_formula(self):
        """验证 WCAG 2.1 相对亮度与对比度公式的基准值。"""
        # 纯白 vs 纯黑应为 21.0:1
        ratio_wb = contrast_audit.contrast_ratio("#ffffff", "#000000")
        self.assertEqual(round(ratio_wb, 1), 21.0)

        # 纯白 vs 纯白应为 1.0:1
        ratio_ww = contrast_audit.contrast_ratio("#ffffff", "#ffffff")
        self.assertEqual(round(ratio_ww, 1), 1.0)

        # 验证简写 #fff 与 #ffffff 一致
        self.assertEqual(contrast_audit.contrast_ratio("#fff", "#000"), ratio_wb)

    def test_app_css_compliance(self):
        """验证实际源码树中的 app.css 达到 WCAG AA 与字号阶梯标准。"""
        css_path = PROJECT_DIR / "agentbus" / "web_assets" / "app.css"
        self.assertTrue(css_path.exists(), f"app.css not found at {css_path}")

        res = contrast_audit.audit_css(css_path)

        # 1. 核心字号门禁：8px 及以下清零
        self.assertEqual(res["le_8px_count"], 0, f"Found {res['le_8px_count']} rules with font-size <= 8px")
        # 9px 也完全上提
        self.assertEqual(res["le_9px_count"], 0, f"Found {res['le_9px_count']} rules with font-size <= 9px")

        # 2. 对比度全项达标 (>= 4.5:1)
        self.assertTrue(res["contrast_pass"], "Some color pairs failed WCAG AA threshold")
        for r in res["contrast_results"]:
            self.assertTrue(r["passed"], f"Failed pair: {r['pair']} with ratio {r['ratio']}:1 (min {r['required']}:1)")
            self.assertGreaterEqual(r["ratio"], 4.5)

        # 3. !important 必须为个位数
        self.assertLess(res["important_count"], 10, f"!important count too high: {res['important_count']}")

        # 4. 总体评估通过
        self.assertTrue(res["overall_pass"])

    def test_contrast_audit_cli_strict(self):
        """验证 contrast_audit.py 在 CLI --strict 模式下运行成功（退出码 0）。"""
        script_path = TOOLS_DIR / "contrast_audit.py"
        css_path = PROJECT_DIR / "agentbus" / "web_assets" / "app.css"

        proc = subprocess.run(
            [sys.executable, str(script_path), "--css", str(css_path), "--strict"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, f"CLI exited with {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("[OK]", proc.stdout)


if __name__ == "__main__":
    unittest.main()
