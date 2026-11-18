#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_unknown_software.py: 未知软件防御、模板连接器安全性与评估包验证测试。"""

import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.connectors.template import NewAgentConnectorTemplate
from agentbus.trigger.contracts import Contact, TriggerErrorCode, WakeRequest
from agentbus.trigger.discovery import resolve_product_identity
from agentbus.trigger.onboarding import generate_evaluation_pack


class TriggerUnknownSoftwareTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

    def test_unknown_product_astduio_defense(self):
        res = resolve_product_identity("astduio")
        self.assertFalse(res.resolved)
        self.assertEqual(res.error_code, TriggerErrorCode.PRODUCT_UNRESOLVED.value)

        # Generates evaluation pack with 0 model calls and 0 messages sent
        pack = generate_evaluation_pack("astduio", "C:/tools/astduio.exe", "0.1.0")
        self.assertEqual(pack["automated_message_sent"], 0)
        self.assertIn("required_capabilities", pack)

    def test_template_connector_safely_rejects_submits(self):
        connector = NewAgentConnectorTemplate("agentbus.custom-test")
        desc = connector.describe()
        self.assertEqual(desc.connector_id, "agentbus.custom-test")

        contact = Contact(
            schema_version=1,
            project_key=str(self.ws),
            member_id="custom_worker",
            binding_id="bnd_001",
            revision=1,
            status="bound",
            connector_id="agentbus.custom-test",
            connector_version="0.1.0",
            installation_key="inst_custom",
            execution_surface="desktop_existing",
            native_conversation_id="chat_001",
            identity_source="prompt_response",
            bound_at="2026-10-30T12:00:00Z",
            updated_at="2026-10-30T12:00:00Z",
        )

        req = WakeRequest(
            schema_version=1,
            request_id="req_test_001",
            project_key=str(self.ws),
            member_id="custom_worker",
            binding_id="bnd_001",
            binding_revision=1,
            purpose="task_ready",
            source_message_ids=["task_001"],
            scope_id="scope_1",
            created_at="2026-10-30T12:00:00Z",
            prompt_summary="Do work",
            prompt_utf8_sha256="abc",
            execution_surface="desktop_existing",
            installation_key="inst_custom",
            connector_version="0.1.0",
        )

        sub_res = connector.submit(self.ws, contact, req)
        self.assertEqual(sub_res.status, "rejected")
        self.assertFalse(sub_res.accepted_by_host)
        self.assertEqual(sub_res.error_code, TriggerErrorCode.CONNECTOR_UNSUPPORTED.value)


if __name__ == "__main__":
    unittest.main()
