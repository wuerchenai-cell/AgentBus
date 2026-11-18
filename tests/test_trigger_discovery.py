#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_discovery.py: 软件发现、未知产品防御与路径边界检查测试。"""

import os
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import TriggerContractError, TriggerErrorCode
from agentbus.trigger.discovery import (
    discover_installation_from_path,
    resolve_product_identity,
)


class TriggerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_resolve_known_product_codearts(self):
        exe_path = self.root / "CodeArts" / "CodeArts.exe"
        exe_path.parent.mkdir(parents=True, exist_ok=True)
        exe_path.write_bytes(b"dummy codearts binary")

        obs = discover_installation_from_path(exe_path)
        self.assertEqual(obs.product_id, "codearts")
        self.assertEqual(obs.vendor, "Huawei")
        self.assertEqual(obs.channel, "stable")

    def test_unresolved_product_astduio(self):
        # User input 'astduio' - must NOT be silently mapped to Android Studio or AI Studio
        res = resolve_product_identity("astduio")
        self.assertFalse(res.resolved)
        self.assertEqual(res.error_code, TriggerErrorCode.PRODUCT_UNRESOLVED.value)
        self.assertEqual(res.product_id, "astduio")

    def test_refuse_scanning_forbidden_directories(self):
        forbidden_path = self.root / ".workbuddy" / "secrets.exe"
        forbidden_path.parent.mkdir(parents=True, exist_ok=True)
        forbidden_path.write_bytes(b"forbidden")

        with self.assertRaises(TriggerContractError) as ctx:
            discover_installation_from_path(forbidden_path)
        self.assertIn("forbidden", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
