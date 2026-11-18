#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_diagnostics.py: 脱敏诊断包导出、秘密清洗与错误分类测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import TriggerErrorCode
from agentbus.trigger.diagnostics import (
    DiagnosticBundle,
    export_diagnostic_bundle,
    get_error_resolution_guide,
)


class TriggerDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

    def test_export_diagnostic_bundle_sanitizes_secrets(self):
        # Create a mock file containing private token to test redaction
        fake_log = self.ws / ".agentbus" / "triggers" / "raw_log.txt"
        fake_log.parent.mkdir(parents=True, exist_ok=True)
        fake_log.write_text("Authorization: Bearer secret_token_xyz123\napi_key=sk-secret456", encoding="utf-8")

        bundle = export_diagnostic_bundle(self.ws)
        self.assertIsInstance(bundle, DiagnosticBundle)
        bundle_dict = bundle.to_dict()
        bundle_json = json.dumps(bundle_dict)

        # Secrets must be scrubbed / redacted
        self.assertNotIn("secret_token_xyz123", bundle_json)
        self.assertNotIn("sk-secret456", bundle_json)
        # Arbitrary text logs can contain private chats and are omitted entirely.
        self.assertEqual(bundle.sanitized_logs, [])
        self.assertEqual(bundle.installed_connectors, [])

    def test_get_error_resolution_guide(self):
        guide = get_error_resolution_guide(TriggerErrorCode.PRODUCT_UNRESOLVED.value)
        self.assertIn("user_action", guide)
        self.assertIn("program_action", guide)
        self.assertEqual(guide["code"], TriggerErrorCode.PRODUCT_UNRESOLVED.value)


if __name__ == "__main__":
    unittest.main()
