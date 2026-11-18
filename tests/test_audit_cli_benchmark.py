# -*- coding: utf-8 -*-
"""测试 AUDIT-CLI-BENCHMARK-OPTIMIZATION (§10.1) 启动基准与自解包加速"""
from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path

from agentbus.benchmark import (
    measure_command,
    run_cli_benchmark,
    render_markdown_report,
    generate_fast_wrappers,
)
from agentbus.cli import cmd_benchmark


class TestAuditCliBenchmark(unittest.TestCase):
    def test_measure_command(self):
        res = measure_command([sys.executable, "-c", "print('bench')"], runs=2)
        self.assertEqual(res["runs"], 2)
        self.assertGreater(res["mean_s"], 0)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["sample_output"], "bench")

    def test_run_cli_benchmark(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / ".agentbus").mkdir(parents=True)
            (ws / ".agentbus" / "config.json").write_text("{}", encoding="utf-8")

            res = run_cli_benchmark(ws, runs=1)
            self.assertIn("benchmarks", res)
            self.assertIn("python_version", res["benchmarks"])
            self.assertIn("analysis", res)

            md = render_markdown_report(res)
            self.assertIn("AgentBus CLI 启动性能基准测量与优化报告", md)
            self.assertIn("核心测试数据对比", md)

    def test_generate_fast_wrappers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            target = ws / "成果"
            wrappers = generate_fast_wrappers(ws, target)
            self.assertEqual(len(wrappers), 2)
            cmd_file = target / "agentbus-fast.cmd"
            ps1_file = target / "agentbus-fast.ps1"
            self.assertTrue(cmd_file.exists())
            self.assertTrue(ps1_file.exists())
            self.assertIn("PYTHONPATH", cmd_file.read_text(encoding="utf-8"))
            self.assertIn("PYTHONPATH", ps1_file.read_text(encoding="utf-8"))

    def test_cmd_benchmark_integration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / ".agentbus").mkdir(parents=True)
            (ws / ".agentbus" / "config.json").write_text("{}", encoding="utf-8")

            args = argparse.Namespace(
                workspace=str(ws),
                exe=None,
                runs=1,
                wrappers=True,
                json=False,
            )
            exit_code = cmd_benchmark(args)
            self.assertEqual(exit_code, 0)
            self.assertTrue((ws / "成果" / "agentbus-fast.cmd").exists())


if __name__ == "__main__":
    unittest.main()
