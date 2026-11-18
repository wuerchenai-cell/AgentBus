# -*- coding: utf-8 -*-
"""test_trigger_web_bridge.py: 测试 WebBridge 中新增的 trigger 扩展接口。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contacts import confirm_contact, create_invite
from agentbus.trigger.contracts import TargetObservation
from agentbus.web_bridge import WebBridge


class TestTriggerWebBridge(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentbus_test_wb_")
        self.workspace = Path(self.test_dir)
        (self.workspace / ".agentbus").mkdir(parents=True, exist_ok=True)
        # 写入标准的 config.json
        cfg_data = {
            "schema_version": 1,
            "workspace": str(self.workspace),
            "output_root": str(self.workspace),
            "director": "codex",
            "workers": {
                "codex": {"enabled": True, "communication_mode": "file"},
                "trae": {"enabled": True, "communication_mode": "file"},
            },
            "assignment_mode": "automatic",
            "communication_mode": "file",
        }
        (self.workspace / ".agentbus" / "config.json").write_text(
            json.dumps(cfg_data), encoding="utf-8"
        )
        # This unit fixture has no external host; keep publication separate
        # from connector invocation, which is covered by trigger-service tests.
        from agentbus.trigger.service import TriggerDeliveryService
        TriggerDeliveryService(self.workspace).set_stopped(True)
        self.bridge = WebBridge(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_bound_member(self, member_id: str = "codex"):
        invite = create_invite(
            self.workspace,
            member_id=member_id,
            connector_id="agentbus.codex-bridge",
            installation_key="inst_wb_01",
        )
        obs = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_wb_01",
            project_key=str(self.workspace),
            connector_id="agentbus.codex-bridge",
            connector_version="1.0.0",
            installation_key="inst_wb_01",
            instance_id="inst_run_1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="conv_wb_123",
            identity_source="host_query",
            exists=True,
        )
        return confirm_contact(self.workspace, invite.invite_id, obs)

    def test_trigger_verification_flow(self):
        self._setup_bound_member("codex")

        # 1. Plan verification
        plan_res = self.bridge.call("trigger_verification_plan", {"member_id": "codex"})
        self.assertTrue(plan_res.get("ok"), msg=str(plan_res))
        plan = plan_res["data"]
        self.assertEqual(plan["member_id"], "codex")
        self.assertIn("plan_id", plan)

        # 2. Run verification
        run_res = self.bridge.call("trigger_verification_run", {"plan": plan})
        self.assertTrue(run_res.get("ok"), msg=str(run_res))
        receipt = run_res["data"]
        # Publishing a challenge is not member execution or delivery.
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["status"], "pending")
        self.assertFalse(receipt["member_delivered"])
        self.assertEqual(receipt["actual_submits"], 0)
        self.assertEqual(receipt["member_id"], "codex")

    def test_trigger_reconcile(self):
        rec_res = self.bridge.call("trigger_reconcile", {})
        self.assertTrue(rec_res.get("ok"), msg=str(rec_res))
        self.assertIn("reconciled_count", rec_res["data"])

    def test_trigger_scan_now(self):
        scan_res = self.bridge.call("trigger_scan_now", {})
        self.assertTrue(scan_res.get("ok"), msg=str(scan_res))
        self.assertIn("scanned", scan_res["data"])
        self.assertIn("delivered", scan_res["data"])


if __name__ == "__main__":
    unittest.main()
