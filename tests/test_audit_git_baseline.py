"""
Tests for AUDIT-GIT-BASELINE (P3, §7.22)
Source: Antigravity (2026-10-12)
"""
import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from project_paths import host_project_root

ROOT = host_project_root()


@unittest.skipIf(ROOT is None, 'host project audit requires --project-root')
class TestAuditGitBaseline(unittest.TestCase):
    def test_report_exists_and_contains_evidence(self):
        report_path = ROOT / "成果" / "审查" / "20261012" / "AUDIT_GIT_BASELINE_REPORT.md"
        self.assertTrue(report_path.is_file(), "审计报告必须存在")
        content = report_path.read_text(encoding="utf-8")
        self.assertIn("Test-Path .git", content)
        self.assertIn("False", content)
        self.assertIn("workflow.json", content)

    def test_launcher_sha256_registered_in_workflow_json(self):
        wf_path = ROOT / ".agentbus" / "workflow.json"
        self.assertTrue(wf_path.is_file())
        wf = json.loads(wf_path.read_text(encoding="utf-8"))
        sha = wf.get("launcher", {}).get("sha256")
        self.assertTrue(bool(sha) and len(sha) == 64, "必须存在 64 位 SHA256 登记")


if __name__ == "__main__":
    unittest.main()
