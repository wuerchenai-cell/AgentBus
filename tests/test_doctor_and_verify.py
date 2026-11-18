#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for Section 7.13 doctor workspace/env options and verify --all project inspection."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import subprocess
import sys
import unittest
from pathlib import Path

from project_paths import host_project_root

ROOT = host_project_root()
PYTHON_EXE = ROOT / "成果/agentbus/pilots/product-convergence-20261008/integration/.build-venv/Scripts/python.exe" if ROOT else None
INTEGRATION_DIR = ROOT / "成果/agentbus/pilots/product-convergence-20261008/integration" if ROOT else None

if INTEGRATION_DIR:
    sys.path.insert(0, str(INTEGRATION_DIR))
from agentbus.cli import verify_all_metrics, cmd_doctor, cmd_verify


@unittest.skipIf(ROOT is None or not (ROOT / ".agentbus").is_dir(), "Host project root not configured or snapshot environment")
class TestDoctorAndVerify(unittest.TestCase):
    def test_verify_all_metrics_structure(self):
        """Test verify_all_metrics extracts the expected schema and keys."""
        metrics = verify_all_metrics(ROOT)
        self.assertEqual(metrics["schema_version"], "1.0.0")
        self.assertIn("package_integrity", metrics)
        self.assertIn("cli_performance", metrics)
        self.assertIn("stop_state", metrics)
        self.assertIn("inflight_backlog", metrics)
        self.assertIn("unaccepted_summary", metrics)
        self.assertIn("recent_delivery_evidence", metrics)

        pkg = metrics["package_integrity"]
        self.assertTrue(pkg["executable_exists"])
        self.assertTrue(pkg["is_aligned"])
        self.assertTrue(pkg["manifest_exists"])

        unacc = metrics["unaccepted_summary"]
        self.assertGreaterEqual(unacc["total_leaves"], 100)
        self.assertGreaterEqual(unacc["delivered_count"], 60)

    def test_doctor_command_with_workspace_and_env(self):
        """Test doctor command runs cleanly with --workspace and --env."""
        import argparse
        import io
        from contextlib import redirect_stdout

        ns = argparse.Namespace(workspace=str(ROOT), env=True)
        f = io.StringIO()
        with redirect_stdout(f):
            ret = cmd_doctor(ns)
        self.assertEqual(ret, 0)
        out = f.getvalue()
        self.assertIn("工作空间信息", out)

    def test_verify_all_command_json(self):
        """Test verify --all --json returns valid json with zero exit code."""
        import argparse
        import io
        from contextlib import redirect_stdout

        ns = argparse.Namespace(workspace=str(ROOT), all=True, json=True, format="json")
        f = io.StringIO()
        with redirect_stdout(f):
            ret = cmd_verify(ns)
        self.assertEqual(ret, 0)
        output = f.getvalue()
        data = json.loads(output)
        self.assertEqual(data["schema_version"], "1.0.0")
        self.assertTrue(data["package_integrity"]["is_aligned"])

    def test_verify_requires_all_or_task_id(self):
        """Test verify without --all or --task-id returns exit code 1."""
        import argparse
        ns = argparse.Namespace(workspace=str(ROOT), all=False, task_id=None, job_id=None)
        ret = cmd_verify(ns)
        self.assertEqual(ret, 1)

    def test_cli_subprocess_integration(self):
        """Test running agentbus.cli via subprocess for doctor and verify."""
        # 1. doctor --workspace --env
        res_doc = subprocess.run(
            [str(PYTHON_EXE), "-m", "agentbus.cli", "doctor", "--workspace", str(ROOT), "--env"],
            cwd=str(INTEGRATION_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        self.assertEqual(res_doc.returncode, 0)
        self.assertIn("工作空间信息", res_doc.stdout)
        self.assertIn("环境变量诊断", res_doc.stdout)

        # 2. verify --all --json
        res_ver = subprocess.run(
            [str(PYTHON_EXE), "-m", "agentbus.cli", "verify", "--workspace", str(ROOT), "--all", "--json"],
            cwd=str(INTEGRATION_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        self.assertEqual(res_ver.returncode, 0)
        data = json.loads(res_ver.stdout)
        self.assertEqual(data["schema_version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
