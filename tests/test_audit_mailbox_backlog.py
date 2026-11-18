"""Tests for Mailbox Backlog Audit Tool and Who-Waits-For-Whom Analyzer."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = next((p for p in Path(__file__).resolve().parents if (p / ".agentbus").is_dir()), Path(__file__).resolve().parents[3])
INTEG = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(INTEG))
try:
    from tools.audit_mailbox_backlog import audit_mailboxes, parse_message_age
except ImportError:
    audit_mailboxes = None
    parse_message_age = None


class TestMailboxBacklogAudit(unittest.TestCase):

    def setUp(self):
        if audit_mailboxes is None or not (ROOT / ".agentbus/agents").is_dir():
            self.skipTest("audit_mailbox_backlog 或宿主信箱环境未包含在当前快照中，跳过")

    def test_parse_message_age(self):
        now_dt = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
        # 1 day ago
        ts_1d = "2026-10-10T12:00:00+00:00"
        age = parse_message_age(ts_1d, now_dt)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 1.0, places=2)

        # None or corrupt
        self.assertIsNone(parse_message_age(None, now_dt))
        self.assertIsNone(parse_message_age("invalid-date", now_dt))

    def test_audit_mailboxes_structure(self):
        report = audit_mailboxes(ROOT)
        self.assertEqual(report.get("schema_version"), "1.0.0")
        self.assertIn("mailboxes", report)
        self.assertIn("who_waits_for_whom", report)
        self.assertIn("age_distribution", report)
        self.assertIn("totals", report)

        totals = report["totals"]
        self.assertGreater(totals["ready"] + totals["done"], 50, "Should detect substantial mailbox history")
        self.assertGreater(totals["agents_count"], 4, "Should find multiple agent directories")

    def test_codex_mailbox_backlog_detection(self):
        report = audit_mailboxes(ROOT)
        mailboxes = report.get("mailboxes", {})
        self.assertIn("codex", mailboxes, "codex mailbox must be inspected")

        codex_box = mailboxes["codex"]
        self.assertGreater(codex_box["ready_count"] + codex_box["done_count"], 70, "codex total messages should exceed 70 msgs")
        self.assertIsInstance(codex_box["by_type"], dict)

    def test_who_waits_for_whom_matrix(self):
        report = audit_mailboxes(ROOT)
        wwfw = report.get("who_waits_for_whom", {})
        self.assertTrue(any(r.get("codex", 0) > 0 for s, r in wwfw.items()), "Members must be recorded waiting for codex")
        self.assertIn("codex", wwfw)

    def test_cli_execution(self):
        cmd = [sys.executable, str(INTEG / "tools/audit_mailbox_backlog.py"), "--json"]
        proc = subprocess.run(cmd, cwd=str(INTEG), capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"CLI error: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("schema_version"), "1.0.0")


if __name__ == "__main__":
    unittest.main()
