#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_verification.py: 有界真实验证控制、随机算题生成与回报核验测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.config import load, save
from agentbus.trigger.contracts import Contact
from agentbus.trigger.verification import (
    VerificationPlan,
    VerificationReport,
    VerificationRun,
    get_verification,
    plan_verification,
    start_verification,
)


class TriggerVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)
        # A real verification task requires an existing configured project and member.
        cfg = load(self.ws, create=True)
        cfg.data['workers']['codearts'] = {'enabled': True, 'communication_mode': 'file'}
        save(cfg)

        self.contact_codearts = Contact(
            schema_version=1,
            project_key=str(self.ws),
            member_id="codearts",
            binding_id="bnd_codearts_01",
            revision=1,
            status="bound",
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_huawei_01",
            execution_surface="desktop_existing",
            native_conversation_id="sess_codearts_100",
            identity_source="host_query",
            bound_at="2026-10-30T12:00:00Z",
            updated_at="2026-10-30T12:00:00Z",
        )
        contact_file = self.ws / '.agentbus/agents/codearts/contact.json'
        contact_file.parent.mkdir(parents=True, exist_ok=True)
        contact_file.write_text(json.dumps(self.contact_codearts.to_dict()), encoding='utf-8')
        from agentbus.trigger.service import TriggerDeliveryService
        TriggerDeliveryService(self.ws).set_stopped(True)

    def test_plan_verification_member_only(self):
        plan = plan_verification(self.ws, self.contact_codearts, mode="member_only")
        self.assertEqual(plan.mode, "member_only")
        self.assertEqual(plan.max_submit_budget, 1)
        self.assertTrue(plan.sample_input_path.endswith(".json"))
        self.assertIn("nonce", plan.expected_payload)
        self.assertIn("a", plan.expected_payload)
        self.assertIn("b", plan.expected_payload)

    def test_plan_verification_roundtrip(self):
        plan = plan_verification(self.ws, self.contact_codearts, mode="roundtrip", coordinator_id="codex")
        self.assertEqual(plan.mode, "roundtrip")
        self.assertEqual(plan.max_submit_budget, 2)
        self.assertEqual(plan.coordinator_id, "codex")

    def test_start_and_get_verification(self):
        plan = plan_verification(self.ws, self.contact_codearts, mode="member_only")
        run = start_verification(self.ws, plan, authorization_ref="auth_test_01")
        self.assertEqual(run.status, "in_progress")
        self.assertTrue((self.ws / plan.sample_input_path).is_file())

        report = get_verification(self.ws, run.run_id)
        self.assertIsNotNone(report)
        self.assertEqual(report.run_id, run.run_id)
        self.assertEqual(report.member_id, "codearts")


if __name__ == "__main__":
    unittest.main()
