#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_connectors.py: Codex与CodeArts连接器离线生命周期与投递验证测试。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import (
    Contact,
    WakeRequest,
    TriggerErrorCode,
    derive_request_id,
)
from agentbus.trigger.connectors.codex import CodexBridgeConnector
from agentbus.trigger.connectors.codearts import CodeArtsHookConnector


class TriggerConnectorsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

        self.contact_codex = Contact(
            schema_version=1,
            project_key=str(self.ws),
            member_id="codex",
            binding_id="bnd_codex",
            revision=1,
            status="bound",
            connector_id="agentbus.codex-bridge",
            connector_version="1.0.0",
            installation_key="inst_codex",
            execution_surface="desktop_existing",
            native_conversation_id="thread_codex_999",
            identity_source="host_query",
            bound_at="2026-10-30T12:00:00Z",
            updated_at="2026-10-30T12:00:00Z",
        )

        self.contact_codearts = Contact(
            schema_version=1,
            project_key=str(self.ws),
            member_id="codearts",
            binding_id="bnd_codearts",
            revision=1,
            status="bound",
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_codearts",
            execution_surface="desktop_existing",
            native_conversation_id="sess_codearts_888",
            identity_source="host_query",
            bound_at="2026-10-30T12:00:00Z",
            updated_at="2026-10-30T12:00:00Z",
        )

    def _make_req(self, member_id, contact, purpose="task_ready"):
        req_id = derive_request_id(
            project_key=str(self.ws),
            member_id=member_id,
            binding_id=contact.binding_id,
            binding_revision=contact.revision,
            purpose=purpose,
            source_message_ids=["msg_001"],
            scope_id="scope_1",
        )
        return WakeRequest(
            schema_version=1,
            request_id=req_id,
            project_key=str(self.ws),
            member_id=member_id,
            binding_id=contact.binding_id,
            binding_revision=contact.revision,
            purpose=purpose,
            source_message_ids=["msg_001"],
            scope_id="scope_1",
            created_at="2026-10-30T12:00:00Z",
            prompt_summary="Work on msg_001",
            prompt_utf8_sha256="abc",
            execution_surface="desktop_existing",
            installation_key=contact.installation_key,
            connector_version=contact.connector_version,
        )

    def test_codex_connector_lifecycle(self):
        connector = CodexBridgeConnector()
        desc = connector.describe()
        self.assertEqual(desc.connector_id, "agentbus.codex-bridge")

        # Probe
        obs = connector.probe(self.ws, self.contact_codex.installation_key)
        self.assertEqual(obs.status, "unknown")

        # Submit
        req = self._make_req("codex", self.contact_codex, purpose="result_ready")
        sub_res = connector.submit(self.ws, self.contact_codex, req)
        self.assertEqual(sub_res.status, "queued")
        self.assertFalse(sub_res.accepted_by_host)
        self.assertIsNone(sub_res.native_message_id)

    def test_codearts_probe_and_install_hook(self):
        connector = CodeArtsHookConnector()

        # Before installation, probe shows hook not installed
        obs = connector.probe(self.ws, self.contact_codearts.installation_key)
        self.assertEqual(obs.status, "not_ready")
        self.assertEqual(obs.error_code, TriggerErrorCode.INSTALL_RELOAD_REQUIRED.value)

        # Install hook
        install_res = connector.install_hook(self.ws)
        self.assertTrue(install_res.get("installed"))
        hook_file = self.ws / ".codearts" / "hooks" / "agentbus.js"
        self.assertTrue(hook_file.is_file())

        # After install, probe shows hook installed
        obs2 = connector.probe(self.ws, self.contact_codearts.installation_key)
        self.assertEqual(obs2.status, "not_ready")
        self.assertEqual(obs2.error_code, TriggerErrorCode.BOOTSTRAP_REQUIRED.value)

    def test_codearts_submit_uncertain_on_timeout(self):
        connector = CodeArtsHookConnector()
        connector.install_hook(self.ws)
        req = self._make_req("codearts", self.contact_codearts, purpose="task_ready")

        # Hook is not simulated as responding -> timeout returns uncertain, NOT accepted
        sub_res = connector.submit(self.ws, self.contact_codearts, req, timeout_s=0.1)
        self.assertEqual(sub_res.status, "uncertain")
        self.assertFalse(sub_res.accepted_by_host)
        self.assertEqual(sub_res.error_code, TriggerErrorCode.SEND_UNCERTAIN.value)

    def test_codearts_submit_accepted_when_hook_confirms(self):
        connector = CodeArtsHookConnector()
        connector.install_hook(self.ws)
        req = self._make_req("codearts", self.contact_codearts, purpose="task_ready")

        # Simulate hook confirming dispatch
        def simulate_hook_response():
            bridge_res_dir = self.ws / ".agentbus" / "triggers" / "bridge" / "codearts" / "outcomes"
            bridge_res_dir.mkdir(parents=True, exist_ok=True)
            outcome = {
                "request_id": req.request_id,
                "status": "sdk_returned",
                "assistant_id": "msg_huawei_001",
                "session_id": self.contact_codearts.native_conversation_id,
            }
            (bridge_res_dir / f"{req.request_id}.json").write_text(json.dumps(outcome), encoding="utf-8")

        simulate_hook_response()
        sub_res = connector.submit(self.ws, self.contact_codearts, req, timeout_s=1.0)
        self.assertEqual(sub_res.status, "accepted")
        self.assertTrue(sub_res.accepted_by_host)
        self.assertEqual(sub_res.native_message_id, "msg_huawei_001")


if __name__ == "__main__":
    unittest.main()
