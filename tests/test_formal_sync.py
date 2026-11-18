#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_formal_sync.py: 测试正式源码树同步门禁与 Release Gate 校验。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_formal_sync import audit_formal_sync
from project_paths import host_source_root
HOST_SOURCE = host_source_root()


class TestFormalSync(unittest.TestCase):
    @unittest.skipUnless(HOST_SOURCE is not None, "Explicit host manifest audit required before release")
    def test_formal_sync_on_live_manifest(self):
        manifest = HOST_SOURCE / "packaging" / "BUILD-MANIFEST.json"
        res = audit_formal_sync(manifest)

        self.assertTrue(res["manifest_exists"])
        self.assertTrue(res["synchronized"])
        self.assertEqual(res["missing_in_formal_count"], 0)
        self.assertEqual(res["drift_in_formal_count"], 0)
        self.assertTrue(res["passed"])

    def test_formal_sync_fails_on_missing_or_drift(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            mock_manifest = Path(tmp_dir) / "BUILD-MANIFEST.json"
            data = {
                "formal_source_tree_audit": {
                    "synchronized": False,
                    "summary": {
                        "missing_in_formal_count": 2,
                        "drift_in_formal_count": 1
                    }
                }
            }
            mock_manifest.write_text(json.dumps(data), encoding="utf-8")

            res = audit_formal_sync(mock_manifest)
            self.assertFalse(res["passed"])
            self.assertFalse(res["synchronized"])
            self.assertEqual(res["missing_in_formal_count"], 2)
            self.assertEqual(res["drift_in_formal_count"], 1)

    def test_formal_sync_fails_when_manifest_not_found(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = audit_formal_sync(Path(tmp_dir) / "non_existent.json")
            self.assertFalse(res["manifest_exists"])
            self.assertFalse(res["passed"])


if __name__ == "__main__":
    unittest.main()
