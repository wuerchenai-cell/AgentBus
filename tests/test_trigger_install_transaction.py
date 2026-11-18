#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_install_transaction.py: 宿主组件安装计划、原子执行、摘要校验与撤销回滚测试。"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import TriggerContractError
from agentbus.trigger.installer import (
    InstallPlan,
    apply_install,
    plan_install,
    rollback_install,
)
from agentbus.trigger.registry import ConnectorDescriptor, InstallationObservation


class TriggerInstallTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

        self.inst = InstallationObservation(
            installation_key="inst_huawei_codearts_01",
            product_id="codearts",
            product_name="CodeArts Agent",
            vendor="Huawei",
            channel="stable",
            version="26.10.1",
            executable_path="C:/Huawei/CodeArts.exe",
            os="windows",
        )

        # Asset source file
        self.asset_file = Path(self.temp_dir.name) / "agentbus_hook.js"
        self.asset_content = b"console.log('hook active');"
        self.asset_file.write_bytes(self.asset_content)
        self.asset_sha256 = hashlib.sha256(self.asset_content).hexdigest()

        self.connector = ConnectorDescriptor(
            schema_version=1,
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            protocol_version="agentbus-trigger/1",
            display_name="CodeArts Hook Connector",
            entry_kind="hook",
            install_actions=[
                {
                    "action_id": "copy_asset",
                    "source_path": str(self.asset_file),
                    "target_relpath": ".codearts/hooks/agentbus.js",
                    "sha256": self.asset_sha256,
                }
            ],
            activation={"reload_required": False},
        )

    def test_plan_and_apply_install(self):
        plan = plan_install(self.inst, self.ws, self.connector)
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0]["target_relpath"], ".codearts/hooks/agentbus.js")

        result = apply_install(self.ws, plan, authorization_ref="user_auth_01")
        self.assertTrue(result["success"])
        tx_id = result["transaction_id"]

        installed_file = self.ws / ".codearts" / "hooks" / "agentbus.js"
        self.assertTrue(installed_file.is_file())
        self.assertEqual(installed_file.read_bytes(), self.asset_content)

        # Rollback
        rb_res = rollback_install(self.ws, tx_id)
        self.assertTrue(rb_res["success"])
        self.assertFalse(installed_file.is_file())

    def test_apply_install_fails_on_digest_mismatch(self):
        # Tampered content
        tampered_connector = ConnectorDescriptor(
            schema_version=1,
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            protocol_version="agentbus-trigger/1",
            display_name="CodeArts Hook Connector",
            entry_kind="hook",
            install_actions=[
                {
                    "action_id": "copy_asset",
                    "source_path": str(self.asset_file),
                    "target_relpath": ".codearts/hooks/agentbus.js",
                    "sha256": "wrong_hash_12345",
                }
            ],
        )
        plan = plan_install(self.inst, self.ws, tampered_connector)
        with self.assertRaises(TriggerContractError) as ctx:
            apply_install(self.ws, plan, authorization_ref="user_auth_01")
        self.assertIn("digest mismatch", str(ctx.exception).lower())

        # Target file must not exist
        installed_file = self.ws / ".codearts" / "hooks" / "agentbus.js"
        self.assertFalse(installed_file.is_file())


if __name__ == "__main__":
    unittest.main()
