from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agentbus-supervision-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = importlib.import_module("agentbus.supervision")

    def settings(self, **overrides):
        value = {
            "strict_supervision": True,
            "designated_auditor": "reviewer",
            "checks": [{"argv": [sys.executable, "-c", "print('ok')"],
                        "timeout_seconds": 10}],
        }
        value.update(overrides)
        return value

    def workers(self, **overrides):
        value = {
            "reviewer": {"enabled": True, "executor_enabled": True},
            "worker": {"enabled": True, "executor_enabled": True},
        }
        value.update(overrides)
        return value

    def artifact(self, path: str, data: bytes):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": path, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def test_settings_accept_enabled_independent_auditor_and_owner_argv(self):
        result = self.module.validate_settings(self.settings(), self.workers())
        self.assertTrue(result["strict_supervision"])
        self.assertEqual(result["designated_auditor"], "reviewer")
        self.assertEqual(result["checks"][0]["argv"][0], sys.executable)

    def test_settings_require_a_boolean_strict_switch(self):
        with self.assertRaises(self.module.SupervisionError):
            self.module.validate_settings(self.settings(strict_supervision="yes"), self.workers())

    def test_settings_require_enabled_auditor_with_executor_permission(self):
        cases = [
            (self.workers(reviewer={"enabled": False, "executor_enabled": True}), "reviewer"),
            (self.workers(reviewer={"enabled": True, "executor_enabled": False}), "reviewer"),
            (self.workers(), "missing"),
        ]
        for workers, auditor in cases:
            with self.subTest(auditor=auditor, workers=workers):
                with self.assertRaises(self.module.SupervisionError):
                    self.module.validate_settings(
                        self.settings(designated_auditor=auditor), workers)

    def test_strict_settings_require_owner_authored_checks(self):
        for checks in (None, [], "python -m unittest"):
            with self.subTest(checks=checks):
                with self.assertRaises(self.module.SupervisionError):
                    self.module.validate_settings(self.settings(checks=checks), self.workers())

    def test_settings_reject_shell_strings_and_unbounded_argv_or_timeout(self):
        invalid_checks = [
            [{"argv": "python -m unittest", "timeout_seconds": 10}],
            [{"argv": [], "timeout_seconds": 10}],
            [{"argv": [sys.executable, "x" * 4097], "timeout_seconds": 10}],
            [{"argv": [sys.executable] * 65, "timeout_seconds": 10}],
            [{"argv": [sys.executable], "timeout_seconds": 0}],
            [{"argv": [sys.executable], "timeout_seconds": 1801}],
            [{"argv": [sys.executable], "timeout_seconds": True}],
        ]
        for checks in invalid_checks:
            with self.subTest(checks=checks):
                with self.assertRaises(self.module.SupervisionError):
                    self.module.validate_settings(self.settings(checks=checks), self.workers())

    def test_artifact_verification_checks_bytes_hash_and_text_classification(self):
        entry = self.artifact("src/report.md", "交付内容\n".encode("utf-8"))
        result = self.module.verify_artifacts(self.root, [entry])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["artifacts"][0]["content_kind"], "text")
        self.assertEqual(result["artifacts"][0]["sha256"], entry["sha256"])

    def test_artifact_verification_fails_on_missing_or_mismatched_file(self):
        entry = self.artifact("out.txt", b"real")
        bad = dict(entry, bytes=99)
        result = self.module.verify_artifacts(self.root, [bad])
        self.assertEqual(result["verdict"], "fail")
        self.assertTrue(result["issues"])
        missing = {"path": "missing.txt", "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
        self.assertEqual(self.module.verify_artifacts(self.root, [missing])["verdict"], "fail")

    def test_artifact_verification_rejects_path_traversal_and_symlinks(self):
        outside = self.root.parent / (self.root.name + "-outside.txt")
        outside.write_bytes(b"outside")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        traversal = {"path": "../" + outside.name, "bytes": 7,
                     "sha256": hashlib.sha256(b"outside").hexdigest()}
        result = self.module.verify_artifacts(self.root, [traversal])
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("UNSAFE_PATH", {item["code"] for item in result["issues"]})

        target = self.root / "target.txt"
        target.write_text("safe", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("this Windows account cannot create a symlink")
        entry = {"path": "link.txt", "bytes": target.stat().st_size,
                 "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        linked = self.module.verify_artifacts(self.root, [entry])
        self.assertEqual(linked["verdict"], "fail")
        self.assertIn("UNSAFE_PATH", {item["code"] for item in linked["issues"]})

    def test_invalid_utf8_is_classified_as_binary_and_not_as_verified_text(self):
        entry = self.artifact("report.md", b"\xff\xfe\x00")
        result = self.module.verify_artifacts(self.root, [entry])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["artifacts"][0]["content_kind"], "binary")
        self.assertEqual(self.module.scan_text_artifacts(self.root, [entry]), [])

    def test_text_scan_reports_question_marks_and_unimplemented_placeholders(self):
        content = "ok\n??? broken\nTODO: finish\nNotImplementedError\n".encode("utf-8")
        entry = self.artifact("src/module.py", content)
        findings = self.module.scan_text_artifacts(self.root, [entry])
        self.assertEqual([(x["line"], x["code"]) for x in findings], [
            (2, "QUESTION_MARKS"), (3, "TODO"), (4, "NOT_IMPLEMENTED")])
        self.assertTrue(all(item["path"] == "src/module.py" for item in findings))
        self.assertTrue(all(item.get("is_quality_proof") is False for item in findings))

    def test_check_runner_uses_argv_cwd_and_captures_real_exit_code(self):
        check = {"argv": [sys.executable, "-c", "import os; print(os.getcwd()); raise SystemExit(7)"],
                 "timeout_seconds": 10}
        result = self.module.run_allowlisted_checks(self.root, [check])[0]
        self.assertEqual(result["exit_code"], 7)
        self.assertFalse(result["timed_out"])
        self.assertIn(str(self.root), result["stdout"])
        self.assertFalse(result["ok"])

    def test_check_runner_does_not_pass_unapproved_environment_secrets(self):
        marker = "agentbus-supervision-test-secret"
        check = {"argv": [sys.executable, "-c",
                          "import os; print(os.getenv('AGENTBUS_TEST_SECRET', ''))"],
                 "timeout_seconds": 10}
        with patch.dict(os.environ, {"AGENTBUS_TEST_SECRET": marker}):
            result = self.module.run_allowlisted_checks(self.root, [check])[0]
        self.assertEqual(result["stdout"].strip(), "")
        self.assertNotIn(marker, repr(result))

    def test_check_runner_bounds_stored_output(self):
        check = {"argv": [sys.executable, "-c", "print('x' * 100000)"],
                 "timeout_seconds": 10}
        result = self.module.run_allowlisted_checks(self.root, [check])[0]
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 65536)
        self.assertTrue(result["stdout_truncated"])

    def test_check_runner_times_out_and_reports_nonpass(self):
        check = {"argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                 "timeout_seconds": 1}
        result = self.module.run_allowlisted_checks(self.root, [check])[0]
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["ok"])
        self.assertIsNone(result["exit_code"])

    def test_audit_qualification_rejects_inline_zero_exit_noop(self):
        assessment = self.module.assess_check_for_audit(
            {"argv": [sys.executable, "-c", "print('ok')"]},
            {"exit_code": 0, "ok": True, "stdout": "ok\n", "stderr": "",
             "timed_out": False, "stdout_truncated": False,
             "stderr_truncated": False, "error": ""})
        self.assertFalse(assessment["audit_qualifying"])
        self.assertIn("inline", assessment["qualification_reason"])

    def test_audit_qualification_rejects_unittest_run_with_zero_tests(self):
        assessment = self.module.assess_check_for_audit(
            {"argv": [sys.executable, "-m", "unittest", "discover"]},
            {"exit_code": 0, "ok": True, "stdout": "Ran 0 tests in 0.000s\nOK\n",
             "stderr": "", "timed_out": False, "stdout_truncated": False,
             "stderr_truncated": False, "error": ""})
        self.assertFalse(assessment["audit_qualifying"])
        self.assertEqual(assessment["tests_run"], 0)

    def test_audit_qualification_accepts_nonempty_unittest_run(self):
        assessment = self.module.assess_check_for_audit(
            {"argv": [sys.executable, "-m", "unittest", "test_acceptance"]},
            {"exit_code": 0, "ok": True, "stdout": "", "stderr": "Ran 3 tests in 0.001s\nOK\n",
             "timed_out": False, "stdout_truncated": False,
             "stderr_truncated": False, "error": ""})
        self.assertTrue(assessment["audit_qualifying"])
        self.assertEqual(assessment["tests_run"], 3)

    def test_audit_qualification_reports_zero_passed_pytest_tests_when_all_skipped(self):
        assessment = self.module.assess_check_for_audit(
            {"argv": [sys.executable, "-m", "pytest", "-q"]},
            {"exit_code": 0, "ok": True, "stdout": "1 skipped in 0.01s\n",
             "stderr": "", "timed_out": False, "stdout_truncated": False,
             "stderr_truncated": False, "error": ""})
        self.assertFalse(assessment["audit_qualifying"])
        self.assertEqual(assessment["tests_run"], 0)

    def test_settings_reject_forbidden_or_unauthorized_check_programs(self):
        forbidden = ["cmd", "cmd.exe", "powershell", "powershell.exe", "bash", "sh", "curl", "del", "rm", "shutdown", "unknown.exe"]
        for prog in forbidden:
            with self.subTest(prog=prog):
                with self.assertRaises(self.module.SupervisionError):
                    self.module.validate_settings(
                        self.settings(checks=[{"argv": [prog, "arg"], "timeout_seconds": 10}]),
                        self.workers()
                    )


if __name__ == "__main__":
    unittest.main()
