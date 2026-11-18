#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_registry.py: 连接器清单加载、格式校验与兼容匹配测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import TriggerContractError
from agentbus.trigger.registry import (
    ConnectorDescriptor,
    ConnectorRegistry,
    InstallationObservation,
    load_manifest,
    match_connectors,
)


class TriggerRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.manifests_dir = Path(self.temp_dir.name) / "manifests"
        self.manifests_dir.mkdir(parents=True, exist_ok=True)

    def _create_sample_manifest(self, connector_id="agentbus.codearts-hook", version="1.0.0"):
        data = {
            "schema_version": 1,
            "connector_id": connector_id,
            "connector_version": version,
            "protocol_version": "agentbus-trigger/1",
            "display_name": "CodeArts Hook Connector",
            "products": [
                {
                    "product_id": "codearts",
                    "vendor": "Huawei",
                    "channels": ["stable"],
                    "os": ["windows", "linux"],
                    "min_version": "26.10.0",
                }
            ],
            "entry_kind": "hook",
            "execution_surface": "desktop_existing",
            "transport": "file_channel",
            "asset_refs": [
                {
                    "relpath": "host_assets/codearts/agentbus_hook.js",
                    "bytes": 2048,
                    "sha256": "fakehash123",
                    "license": "MIT",
                }
            ],
            "install_actions": [
                {
                    "action_id": "install_project_hook",
                    "target_relpath": ".codearts/hooks/agentbus.js",
                }
            ],
            "activation": {
                "reload_required": False,
                "entrypoint": ".codearts/hooks/agentbus.js",
            },
            "permission_requirements": ["read_project", "write_project_hooks"],
            "declared_capabilities": {
                "can_identify_target": True,
                "can_check_project": True,
                "can_submit_existing": True,
                "can_route_exact_id": True,
                "can_detect_busy": True,
                "host_queue_verified": False,
                "atomic_target_submit": True,
                "can_report_native_start": True,
                "can_report_native_finish": True,
                "can_reconcile": True,
                "can_cancel_owned_turn": False,
                "cold_start_verified": False,
                "requires_visible_target": False,
                "requires_manual_bootstrap": False,
            },
            "docs_refs": ["https://support.huaweicloud.com"],
            "maintenance": {
                "maintainer": "AgentBus Team",
                "uninstall_steps": "Remove hook file",
            },
        }
        mf_path = self.manifests_dir / f"{connector_id}.json"
        mf_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return mf_path

    def test_load_valid_manifest(self):
        mf_path = self._create_sample_manifest()
        desc = load_manifest(mf_path)
        self.assertEqual(desc.connector_id, "agentbus.codearts-hook")
        self.assertEqual(desc.schema_version, 1)
        self.assertTrue(desc.declared_capabilities["can_submit_existing"])

    def test_load_invalid_schema_version_rejected(self):
        mf_path = self.manifests_dir / "bad_schema.json"
        mf_path.write_text(json.dumps({"schema_version": 99, "connector_id": "bad"}), encoding="utf-8")
        with self.assertRaises(TriggerContractError):
            load_manifest(mf_path)

    def test_match_connectors_by_product_and_channel(self):
        self._create_sample_manifest("agentbus.codearts-hook")
        registry = ConnectorRegistry(self.manifests_dir)

        # Matching installation
        inst_match = InstallationObservation(
            installation_key="inst_huawei_codearts_01",
            product_id="codearts",
            product_name="CodeArts Agent",
            vendor="Huawei",
            channel="stable",
            version="26.10.1",
            executable_path="C:/Huawei/CodeArts/CodeArts.exe",
            os="windows",
        )
        matches = registry.find_matching_connectors(inst_match)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].connector_id, "agentbus.codearts-hook")

        # Non-matching installation (different product)
        inst_other = InstallationObservation(
            installation_key="inst_vscode",
            product_id="vscode",
            product_name="Visual Studio Code",
            vendor="Microsoft",
            channel="stable",
            version="1.94.0",
            executable_path="C:/VSCode/Code.exe",
            os="windows",
        )
        matches_other = registry.find_matching_connectors(inst_other)
        self.assertEqual(len(matches_other), 0)

    def test_different_channels_do_not_automatically_match(self):
        self._create_sample_manifest("agentbus.codearts-hook")
        registry = ConnectorRegistry(self.manifests_dir)

        inst_beta = InstallationObservation(
            installation_key="inst_codearts_beta",
            product_id="codearts",
            product_name="CodeArts Agent Beta",
            vendor="Huawei",
            channel="insiders",  # not in ['stable']
            version="26.10.1",
            executable_path="C:/Huawei/CodeArtsBeta/CodeArts.exe",
            os="windows",
        )
        matches = registry.find_matching_connectors(inst_beta)
        self.assertEqual(len(matches), 0)


if __name__ == "__main__":
    unittest.main()
