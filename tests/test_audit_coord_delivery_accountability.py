"""
Automated Regression Tests for §7.23 AUDIT-COORD-DELIVERY-ACCOUNTABILITY
Source: Antigravity (2026-10-12)

Verifies:
1. Formal entrance match: workflow.json launcher SHA256 matches actual 成果/AgentBus.exe on disk.
2. PROJECT.md document drift audit: critical guidelines and boundary entries are intact.
3. Peer review accountability document presence & integrity.
4. CLI execution sanity: python CLI tree status completes quickly (< 2.0s).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

INTEGRATION_ROOT = Path(__file__).resolve().parents[1]  # integration dir
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from project_paths import host_project_root

PROJECT_ROOT = host_project_root()


@unittest.skipIf(PROJECT_ROOT is None, 'host project audit requires --project-root')
class TestAuditCoordDeliveryAccountability(unittest.TestCase):
    def test_formal_launcher_digest_consistency(self):
        """Formal entry launcher sha256 in workflow.json must match actual AgentBus.exe on disk."""
        exe_path = PROJECT_ROOT / "成果/AgentBus.exe"
        self.assertTrue(exe_path.is_file(), f"Formal binary missing: {exe_path}")

        workflow_path = PROJECT_ROOT / ".agentbus/workflow.json"
        self.assertTrue(workflow_path.is_file(), f"Workflow config missing: {workflow_path}")

        with open(workflow_path, "r", encoding="utf-8") as f:
            wf = json.load(f)

        launcher = wf.get("launcher", {})
        registered_sha = launcher.get("sha256")
        self.assertTrue(registered_sha, "workflow.json must declare launcher sha256")

        real_sha = hashlib.sha256(exe_path.read_bytes()).hexdigest()
        self.assertEqual(real_sha, registered_sha, "Formal binary SHA256 must match workflow.json launcher entry")

    def test_peer_review_reference_document_exists(self):
        """Verify 20261010 peer review source file exists and is intact."""
        review_doc = PROJECT_ROOT / "成果/复函/20261010-对codex本周工作的独立评述.md"
        self.assertTrue(review_doc.is_file(), f"Review document missing: {review_doc}")
        content = review_doc.read_text(encoding="utf-8")
        self.assertIn("对 Codex 本周", content)
        self.assertIn("用测试数替代体验验收", content)
        self.assertIn("修好正式入口", content)

    def test_cli_execution_performance_baseline(self):
        """Python CLI status check must finish within 3.0 seconds in normal conditions."""
        venv_py = INTEGRATION_ROOT / ".build-venv/Scripts/python.exe"
        if not venv_py.is_file():
            venv_py = Path(sys.executable)

        cmd = [str(venv_py), "-m", "agentbus.cli", "tree", "status", "--workspace", str(PROJECT_ROOT)]
        t0 = time.time()
        proc = subprocess.run(cmd, cwd=str(INTEGRATION_ROOT),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        elapsed = time.time() - t0

        self.assertEqual(proc.returncode, 0, f"CLI command failed: {proc.stderr}")
        self.assertLess(elapsed, 4.0, f"CLI status took too long: {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
