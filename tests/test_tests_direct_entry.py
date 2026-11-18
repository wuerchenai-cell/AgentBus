#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for Section 5.7 TESTS-DIRECT-ENTRY:
Verifying unified official test entrypoint `agentbus test`, test self-bootstrap path discovery,
direct file execution from root and integration directories, and clear error guidance for invalid commands.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

# Self-bootstrap path discovery for test suite
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentbus.cli import main


class TestTestsDirectEntry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        cls.integ_dir = Path(__file__).resolve().parents[1]
        cls.repo_root = next((p for p in Path(__file__).resolve().parents if (p / ".agentbus").is_dir()), Path(__file__).resolve().parents[3])
        cls.python_exe = cls.integ_dir / ".build-venv/Scripts/python.exe"
        if not cls.python_exe.exists():
            cls.python_exe = Path(sys.executable)
        cls.env = dict(os.environ)
        pypath = str(cls.integ_dir)
        if "PYTHONPATH" in cls.env and cls.env["PYTHONPATH"]:
            cls.env["PYTHONPATH"] = pypath + os.pathsep + cls.env["PYTHONPATH"]
        else:
            cls.env["PYTHONPATH"] = pypath

    def test_agentbus_test_cli_subcommand_runs_suite(self):
        """1. Verify `agentbus test` exists and can run tests by pattern."""
        cmd = [str(self.python_exe), "-m", "agentbus.cli", "test", "--pattern", "test_agent_id_safety.py"]
        res = subprocess.run(cmd, cwd=str(self.integ_dir), capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Ran 5 tests", res.stdout + res.stderr)
        self.assertIn('"success": true', res.stdout + res.stderr)

    def test_agentbus_test_from_workspace_root(self):
        """2. Verify `agentbus test` runs seamlessly when cwd is workspace root."""
        if not (self.repo_root / ".agentbus").is_dir():
            self.skipTest("快照沙箱测试环境下未挂载宿主工作区根目录")
        cmd = [str(self.python_exe), "-m", "agentbus.cli", "test", "--pattern", "test_board_command.py"]
        res = subprocess.run(cmd, cwd=str(self.repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Ran 5 tests", res.stdout + res.stderr)

    def test_direct_file_execution_from_integration_dir(self):
        """3. Verify direct file execution `python tests/test_*.py` from integration directory."""
        cmd = [str(self.python_exe), "tests/test_agent_id_safety.py"]
        res = subprocess.run(cmd, cwd=str(self.integ_dir), capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Ran 5 tests", res.stdout + res.stderr)
        self.assertIn("OK", res.stdout + res.stderr)

    def test_direct_file_execution_from_repo_root(self):
        """4. Verify direct file execution from repo root directory."""
        if not (self.repo_root / ".agentbus").is_dir():
            self.skipTest("快照沙箱测试环境下未挂载宿主工作区根目录")
        test_file = self.integ_dir / "tests/test_agent_id_safety.py"
        cmd = [str(self.python_exe), str(test_file)]
        res = subprocess.run(cmd, cwd=str(self.repo_root), capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Ran 5 tests", res.stdout + res.stderr)
        self.assertIn("OK", res.stdout + res.stderr)

    def test_invalid_command_shows_recommended_test_entry(self):
        """5. Verify CLI choices include 'test' and recommendations highlight official entry."""
        run_cwd = str(self.repo_root if (self.repo_root / ".agentbus").is_dir() else self.integ_dir)
        cmd = [str(self.python_exe), "-m", "agentbus.cli", "invalid_action_xyz"]
        res = subprocess.run(cmd, cwd=run_cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
        self.assertNotEqual(res.returncode, 0)
        output = res.stdout + res.stderr
        self.assertTrue("choose from 'test'" in output or "choose from test" in output)
        
        # Test --help contains test in recommended commands
        cmd_help = [str(self.python_exe), "-m", "agentbus.cli", "--help"]
        res_help = subprocess.run(cmd_help, cwd=run_cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
        self.assertEqual(res_help.returncode, 0)
        self.assertIn("test", res_help.stdout)
        self.assertIn("统一官方测试入口", res_help.stdout)


if __name__ == "__main__":
    unittest.main()
