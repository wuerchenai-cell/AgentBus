#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_contracts.py: 触发协议、数据模型与契约安全校验测试。"""

import json
import unittest
from pathlib import Path

from agentbus.trigger.contracts import (
    RegistrationInvite,
    Contact,
    WakeRequest,
    PreflightResult,
    SubmitResult,
    ReadinessObservation,
    TargetObservation,
    NativeEvent,
    ReconcileResult,
    TriggerContractError,
    TriggerErrorCode,
    normalize_project_key,
    validate_member_id,
    validate_native_id,
    derive_request_id,
)


class TriggerContractsTests(unittest.TestCase):
    def test_normalize_project_key(self):
        p1 = normalize_project_key("D:\\示例空间\\协作 样例")
        p2 = normalize_project_key("D:/示例空间/协作 样例/")
        self.assertEqual(p1, p2)
        self.assertFalse(p1.endswith("/") or p1.endswith("\\"))

        # Rejects path traversal
        with self.assertRaises(TriggerContractError):
            normalize_project_key("I:/foo/../bar")

    def test_validate_member_id(self):
        self.assertTrue(validate_member_id("codearts"))
        self.assertTrue(validate_member_id("worker_01"))

        with self.assertRaises(TriggerContractError):
            validate_member_id("../bad")
        with self.assertRaises(TriggerContractError):
            validate_member_id("bad/id")
        with self.assertRaises(TriggerContractError):
            validate_member_id("bad\x00id")
        with self.assertRaises(TriggerContractError):
            validate_member_id("")

    def test_validate_native_id(self):
        self.assertTrue(validate_native_id("chat_123456"))
        self.assertTrue(validate_native_id("c9820f4b-3260-449e-ba69-37f26f63a23a"))

        with self.assertRaises(TriggerContractError):
            validate_native_id("bad/slash")
        with self.assertRaises(TriggerContractError):
            validate_native_id("bad\\backslash")
        with self.assertRaises(TriggerContractError):
            validate_native_id("bad\nid")
        with self.assertRaises(TriggerContractError):
            validate_native_id("a" * 300)

    def test_contact_excludes_credentials(self):
        data = {
            "schema_version": 1,
            "project_key": "I:/test-project",
            "member_id": "codearts",
            "binding_id": "bnd_123",
            "revision": 1,
            "status": "bound",
            "connector_id": "agentbus.codearts-hook",
            "connector_version": "1.0.0",
            "installation_key": "inst_codearts",
            "execution_surface": "desktop_existing",
            "native_conversation_id": "sess_999",
            "identity_source": "host_query",
            "bound_at": "2026-10-30T12:00:00Z",
            "updated_at": "2026-10-30T12:00:00Z",
            "api_key": "secret_12345",  # Malicious / dangerous field
        }
        with self.assertRaises(TriggerContractError) as ctx:
            Contact.from_dict(data)
        self.assertIn("credential", str(ctx.exception).lower())

    def test_contact_serialization_roundtrip(self):
        c = Contact(
            schema_version=1,
            project_key="I:/test-project",
            member_id="codearts",
            binding_id="bnd_123",
            revision=1,
            status="bound",
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_codearts",
            execution_surface="desktop_existing",
            native_conversation_id="sess_999",
            identity_source="host_query",
            bound_at="2026-10-30T12:00:00Z",
            updated_at="2026-10-30T12:00:00Z",
        )
        d = c.to_dict()
        c2 = Contact.from_dict(d)
        self.assertEqual(c, c2)
        raw_json = json.dumps(d)
        self.assertLess(len(raw_json.encode("utf-8")), 65536)

    def test_derive_request_id_deterministic(self):
        r1 = derive_request_id(
            project_key="I:/test-project",
            member_id="codearts",
            binding_id="bnd_123",
            binding_revision=1,
            purpose="task_ready",
            source_message_ids=["msg_002", "msg_001"],
            scope_id="scope_alpha",
        )
        r2 = derive_request_id(
            project_key="I:/test-project",
            member_id="codearts",
            binding_id="bnd_123",
            binding_revision=1,
            purpose="task_ready",
            source_message_ids=["msg_001", "msg_002"],  # Different input order
            scope_id="scope_alpha",
        )
        self.assertEqual(r1, r2)

        # Different purpose produces different ID
        r3 = derive_request_id(
            project_key="I:/test-project",
            member_id="codearts",
            binding_id="bnd_123",
            binding_revision=1,
            purpose="result_ready",
            source_message_ids=["msg_001", "msg_002"],
            scope_id="scope_alpha",
        )
        self.assertNotEqual(r1, r3)

    def test_wake_request_prompt_length_limit(self):
        long_prompt = "A" * 1600
        with self.assertRaises(TriggerContractError):
            WakeRequest(
                schema_version=1,
                request_id="req_123",
                project_key="I:/test-project",
                member_id="codearts",
                binding_id="bnd_123",
                binding_revision=1,
                purpose="task_ready",
                source_message_ids=["msg_001"],
                scope_id="scope_1",
                created_at="2026-10-30T12:00:00Z",
                prompt_summary=long_prompt,
                prompt_utf8_sha256="abc",
                execution_surface="desktop_existing",
                installation_key="inst_1",
                connector_version="1.0.0",
            )

    def test_trigger_error_codes_comprehensive(self):
        expected_codes = [
            "PRODUCT_UNRESOLVED",
            "CONNECTOR_UNSUPPORTED",
            "INSTALL_RELOAD_REQUIRED",
            "WORKFLOW_CONFLICT",
            "INVITE_EXPIRED",
            "TARGET_UNKNOWN",
            "TARGET_MISSING",
            "TARGET_CHANGED",
            "PROJECT_MISMATCH",
            "BOOTSTRAP_REQUIRED",
            "AUTH_REQUIRED",
            "HOST_DENIED",
            "BUSY",
            "APPROVAL_REQUIRED",
            "BUSY_UNKNOWN",
            "SEND_UNCERTAIN",
            "VERSION_CHANGED",
            "STOPPED",
            "SCOPE_EXPIRED",
            "INSTANCE_CONFLICT",
        ]
        for code in expected_codes:
            self.assertTrue(hasattr(TriggerErrorCode, code), f"Missing error code: {code}")


if __name__ == "__main__":
    unittest.main()
