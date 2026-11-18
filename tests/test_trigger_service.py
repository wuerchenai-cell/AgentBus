#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_service.py: 唤醒生成、发前重检、串行互斥与后台服务核心测试。"""

import os
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import (
    Contact,
    PreflightResult,
    SubmitResult,
    TriggerErrorCode,
    WakeRequest,
)
from agentbus.trigger.wake import (
    make_member_wakeup,
    make_result_wakeup,
)
from agentbus.trigger.service import (
    CoordinatorRouteMode,
    ProjectTriggerPolicy,
    TriggerDeliveryService,
)


class TriggerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

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

        self.contact_codex = Contact(
            schema_version=1,
            project_key=str(self.ws),
            member_id="codex",
            binding_id="bnd_codex_01",
            revision=1,
            status="bound",
            connector_id="agentbus.codex-bridge",
            connector_version="1.0.0",
            installation_key="inst_codex_01",
            execution_surface="desktop_existing",
            native_conversation_id="sess_codex_200",
            identity_source="host_query",
            bound_at="2026-10-30T12:00:00Z",
            updated_at="2026-10-30T12:00:00Z",
        )

    def test_make_member_wakeup(self):
        req = make_member_wakeup(
            workspace=self.ws,
            member_id="codearts",
            task_id="task_calc_123",
            contact=self.contact_codearts,
            scope_id="scope_alpha",
        )
        self.assertEqual(req.purpose, "task_ready")
        self.assertIn("task_calc_123", req.prompt_summary)
        self.assertIn("codearts", req.prompt_summary)
        self.assertLessEqual(len(req.prompt_summary), 1500)
        self.assertEqual(req.source_message_ids, ["task_calc_123"])
        self.assertTrue(req.request_id.startswith("req_"))

    def test_make_result_wakeup(self):
        req = make_result_wakeup(
            workspace=self.ws,
            coordinator_id="codex",
            member_id="codearts",
            result_id="res_calc_123",
            reply_to_task_id="task_calc_123",
            contact=self.contact_codex,
            scope_id="scope_alpha",
        )
        self.assertEqual(req.purpose, "result_ready")
        self.assertIn("res_calc_123", req.prompt_summary)
        self.assertIn("task_calc_123", req.prompt_summary)
        self.assertIn("codearts", req.prompt_summary)
        self.assertEqual(req.source_message_ids, ["res_calc_123"])

    def test_preflight_checks_stop_policy(self):
        service = TriggerDeliveryService(self.ws)
        req = make_member_wakeup(
            workspace=self.ws,
            member_id="codearts",
            task_id="task_calc_123",
            contact=self.contact_codearts,
        )

        # Normal preflight: ready
        pf1 = service.preflight(self.contact_codearts, req)
        self.assertEqual(pf1.status, "ready")

        # Set STOP in policy
        service.set_stopped(True)
        pf2 = service.preflight(self.contact_codearts, req)
        self.assertEqual(pf2.status, "not_ready")
        self.assertEqual(pf2.reason_code, TriggerErrorCode.STOPPED.value)

    def test_preflight_checks_binding_revision(self):
        service = TriggerDeliveryService(self.ws)
        req = make_member_wakeup(
            workspace=self.ws,
            member_id="codearts",
            task_id="task_calc_123",
            contact=self.contact_codearts,
        )

        # Contact updated to revision 2, but request was prepared with revision 1
        stale_contact = Contact(
            schema_version=1,
            project_key=str(self.ws),
            member_id="codearts",
            binding_id="bnd_codearts_01",
            revision=2,  # Drifted revision
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
        pf = service.preflight(stale_contact, req)
        self.assertEqual(pf.status, "conflict")
        self.assertEqual(pf.reason_code, TriggerErrorCode.VERSION_CHANGED.value)

    def test_coordinator_route_mutual_exclusion(self):
        service = TriggerDeliveryService(self.ws)

        # Default is desktop route
        route1 = service.get_coordinator_route()
        self.assertEqual(route1, CoordinatorRouteMode.DESKTOP.value)
        self.assertTrue(service.should_wake_desktop_coordinator())

        # Switch to existing_executor route (e.g. standalone Director)
        service.set_coordinator_route(CoordinatorRouteMode.EXISTING_EXECUTOR.value)
        self.assertFalse(service.should_wake_desktop_coordinator())


if __name__ == "__main__":
    unittest.main()
