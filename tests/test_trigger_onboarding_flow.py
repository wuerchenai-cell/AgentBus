#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_onboarding_flow.py: 新成员接入向导状态机、断点恢复与未知软件评估包测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import TriggerContractError, TriggerErrorCode
from agentbus.trigger.onboarding import (
    FlowState,
    advance_flow,
    generate_evaluation_pack,
    resume_flow,
    start_flow,
)


class TriggerOnboardingFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

    def test_flow_lifecycle_s0_to_s6(self):
        flow = start_flow(self.ws, member_id="codearts")
        self.assertEqual(flow.step, "S0")
        self.assertEqual(flow.revision, 1)

        # S0 -> S1
        f1 = advance_flow(self.ws, flow.flow_id, 1, "confirm_identity", {"member_name": "CodeArts Agent"})
        self.assertEqual(f1.step, "S1")
        self.assertEqual(f1.revision, 2)

        # S1 -> S2 (Select CodeArts)
        f2 = advance_flow(self.ws, flow.flow_id, 2, "select_software", {
            "product_id": "codearts",
            "installation_key": "inst_codearts_01",
        })
        self.assertEqual(f2.step, "S2")
        self.assertEqual(f2.revision, 3)

        # S2 -> S3 (Choose connector)
        f3 = advance_flow(self.ws, flow.flow_id, 3, "confirm_scope", {
            "connector_id": "agentbus.codearts-hook",
        })
        self.assertEqual(f3.step, "S3")
        self.assertEqual(f3.revision, 4)

        # S3 -> S4 (Install component)
        f4 = advance_flow(self.ws, flow.flow_id, 4, "apply_install", {})
        self.assertEqual(f4.step, "S4")
        self.assertEqual(f4.revision, 5)
        self.assertIsNotNone(f4.invite_id)

        # S4 -> S5 (Bind chat)
        f5 = advance_flow(self.ws, flow.flow_id, 5, "bind_chat", {
            "native_conversation_id": "chat_session_01",
            "identity_source": "host_query",
        })
        self.assertEqual(f5.step, "S5")
        self.assertEqual(f5.revision, 6)

        # A UI boolean cannot substitute for actual file-flow delivery.
        with self.assertRaises(TriggerContractError):
            advance_flow(self.ws, flow.flow_id, 6, 'complete_verification', {'verified':True})
        from agentbus.config import load, save
        from agentbus.bus import Bus
        from agentbus.claim_fencing import captured_ticket
        from agentbus.trigger.contacts import load_contact
        from agentbus.trigger.service import TriggerDeliveryService
        from agentbus.trigger.verification import plan_verification, start_verification
        from agentbus.workspace_files import WorkspaceFiles
        cfg = load(self.ws, create=True)
        cfg.data['workers']['codearts'] = {'enabled':True, 'executor_enabled':True,
            'communication_mode':'file', 'transport':'file', 'can_spawn':False}
        cfg = save(cfg)
        bus = Bus(self.ws / '.agentbus'); bus.init()
        TriggerDeliveryService(self.ws).set_stopped(True)
        plan = plan_verification(self.ws, load_contact(self.ws, 'codearts'))
        run = start_verification(self.ws, plan)
        claimed = bus.claim('codearts', bus.dirs('codearts')['ready'] / (run.task_id + '.json'))
        ticket = captured_ticket(claimed)
        bus.heartbeat('codearts', claimed, ticket, note='isolated onboarding check')
        output = cfg.output_root / plan.expected_output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({'nonce':plan.expected_payload['nonce'],
            'result':plan.expected_payload['expected_sum']}), encoding='utf-8')
        manifest = [WorkspaceFiles(cfg.output_root).describe_artifact(plan.expected_output_path)]
        bus.commit_result('codearts', claimed, ticket, body='Computed and checked the local challenge',
            artifacts=manifest, exit_code=0, artifact_root=cfg.output_root)
        f6 = advance_flow(self.ws, flow.flow_id, 6, 'complete_verification', {'run_id':run.run_id})
        self.assertEqual(f6.step, "S6")
        self.assertEqual(f6.revision, 7)

        # S6 -> Completed
        f7 = advance_flow(self.ws, flow.flow_id, 7, "enable_collaboration", {
            "auto_wake": True,
        })
        self.assertEqual(f7.step, "COMPLETED")
        self.assertTrue(f7.completed)

    def test_flow_resume_after_interruption(self):
        flow = start_flow(self.ws, member_id="codearts")
        f1 = advance_flow(self.ws, flow.flow_id, 1, "confirm_identity", {})
        f2 = advance_flow(self.ws, flow.flow_id, 2, "select_software", {
            "product_id": "codearts",
            "installation_key": "inst_codearts_01",
        })

        # Simulate closing wizard and resuming
        resumed = resume_flow(self.ws, flow.flow_id)
        self.assertEqual(resumed.step, "S2")
        self.assertEqual(resumed.member_id, "codearts")
        self.assertEqual(resumed.selected_product, "codearts")

    def test_unknown_software_generate_evaluation_pack(self):
        pack = generate_evaluation_pack(
            product_name="astduio",
            executable_path="C:/tools/astduio.exe",
            detected_version="0.1.0",
        )
        self.assertEqual(pack["product_name"], "astduio")
        self.assertIn("required_capabilities", pack)
        self.assertIn("experimental_design", pack)
        self.assertEqual(pack["automated_message_sent"], 0)


if __name__ == "__main__":
    unittest.main()
