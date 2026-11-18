#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_roundtrip.py: 任务发布、成员认领、交付回报与组长续派双任务闭环测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contacts import confirm_contact, create_invite
from agentbus.trigger.contracts import TargetObservation
from agentbus.trigger.connectors.codearts import CodeArtsHookConnector
from agentbus.trigger.connectors.codex import CodexBridgeConnector
from agentbus.trigger.journal import DeliveryJournal, DeliveryStatus
from agentbus.trigger.service import CoordinatorRouteMode, TriggerDeliveryService
from agentbus.trigger.wake import make_member_wakeup, make_result_wakeup


class TriggerRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

        self.service = TriggerDeliveryService(self.ws)
        self.journal = DeliveryJournal(self.ws)

        # 1. Register Codex coordinator
        inv_codex = create_invite(self.ws, "codex", "agentbus.codex-bridge", "inst_codex")
        obs_codex = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_c1",
            project_key=str(self.ws),
            connector_id="agentbus.codex-bridge",
            connector_version="1.0.0",
            installation_key="inst_codex",
            instance_id="i_codex",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="thread_codex_coord",
            identity_source="host_query",
            exists=True,
        )
        self.contact_codex = confirm_contact(self.ws, inv_codex.invite_id, obs_codex)

        # 2. Register CodeArts member
        inv_codearts = create_invite(self.ws, "codearts", "agentbus.codearts-hook", "inst_codearts")
        obs_codearts = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_ca1",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_codearts",
            instance_id="i_codearts",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="sess_codearts_worker",
            identity_source="host_query",
            exists=True,
        )
        self.contact_codearts = confirm_contact(self.ws, inv_codearts.invite_id, obs_codearts)

    def test_complete_two_task_roundtrip_flow(self):
        # Phase 1: Coordinator dispatches Task 1 to CodeArts
        task_1_id = "task_phase_1_sum"
        req_task_1 = make_member_wakeup(self.ws, "codearts", task_1_id, self.contact_codearts)
        self.journal.enqueue(req_task_1)

        # Preflight and submit Task 1
        pf_1 = self.service.preflight(self.contact_codearts, req_task_1)
        self.assertEqual(pf_1.status, "ready")

        self.journal.transition(req_task_1.request_id, DeliveryStatus.SUBMITTING.value)
        # Mock successful delivery
        self.journal.transition(
            req_task_1.request_id,
            DeliveryStatus.SUBMITTED.value,
            details={"native_message_id": "msg_huawei_001", "accepted_by_host": True}
        )

        # Member simulates execution and generates result_1
        result_1_id = "res_phase_1_done"
        req_res_1 = make_result_wakeup(
            workspace=self.ws,
            coordinator_id="codex",
            member_id="codearts",
            result_id=result_1_id,
            reply_to_task_id=task_1_id,
            contact=self.contact_codex,
        )
        self.journal.enqueue(req_res_1)

        # Only desktop coordinator is woken, Director mutual exclusion respected
        self.assertTrue(self.service.should_wake_desktop_coordinator())

        pf_coord_1 = self.service.preflight(self.contact_codex, req_res_1)
        self.assertEqual(pf_coord_1.status, "ready")
        self.journal.transition(req_res_1.request_id, DeliveryStatus.SUBMITTED.value, details={"accepted_by_host": True})

        # Phase 2: Coordinator dispatches Task 2 dependent on Task 1 output
        task_2_id = "task_phase_2_multiply"
        req_task_2 = make_member_wakeup(self.ws, "codearts", task_2_id, self.contact_codearts)
        self.journal.enqueue(req_task_2)

        self.journal.transition(req_task_2.request_id, DeliveryStatus.SUBMITTED.value, details={"accepted_by_host": True})

        # Member executes Task 2 and delivers result_2
        result_2_id = "res_phase_2_done"
        req_res_2 = make_result_wakeup(
            workspace=self.ws,
            coordinator_id="codex",
            member_id="codearts",
            result_id=result_2_id,
            reply_to_task_id=task_2_id,
            contact=self.contact_codex,
        )
        self.journal.enqueue(req_res_2)
        self.journal.transition(req_res_2.request_id, DeliveryStatus.SUBMITTED.value, details={"accepted_by_host": True})

        # Final Verification of Journal
        submitted_requests = self.journal.list_by_status(DeliveryStatus.SUBMITTED.value)
        self.assertEqual(len(submitted_requests), 4)  # 2 tasks + 2 results
        req_ids = [r.request_id for r in submitted_requests]
        self.assertIn(req_task_1.request_id, req_ids)
        self.assertIn(req_res_1.request_id, req_ids)
        self.assertIn(req_task_2.request_id, req_ids)
        self.assertIn(req_res_2.request_id, req_ids)

        # Zero uncertain or rejected requests
        self.assertEqual(len(self.journal.list_by_status(DeliveryStatus.UNCERTAIN.value)), 0)
        self.assertEqual(len(self.journal.list_by_status(DeliveryStatus.REJECTED.value)), 0)


if __name__ == "__main__":
    unittest.main()
