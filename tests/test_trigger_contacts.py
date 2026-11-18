#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_contacts.py: 联系卡生命周期、邀请管理、CAS版本与隔离性测试。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import (
    Contact,
    TargetObservation,
    TriggerContractError,
    TriggerErrorCode,
)
from agentbus.trigger.contacts import (
    create_invite,
    get_invite,
    confirm_contact,
    load_contact,
    retire_contact,
    prepare_rebind,
)


class TriggerContactsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

    def test_same_project_member_has_one_active_contact(self):
        invite = create_invite(
            self.ws,
            member_id="codearts",
            connector_id="agentbus.codearts-hook",
            installation_key="inst_huawei_codearts",
        )
        self.assertIsNotNone(invite.invite_id)

        obs = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_001",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_huawei_codearts",
            instance_id="inst_run_1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="chat_sess_01",
            identity_source="host_query",
            exists=True,
        )

        c1 = confirm_contact(self.ws, invite.invite_id, obs)
        self.assertEqual(c1.member_id, "codearts")
        self.assertEqual(c1.revision, 1)
        self.assertEqual(c1.status, "bound")

        # Confirming the same invite again must be idempotent and not create rev 2
        c2 = confirm_contact(self.ws, invite.invite_id, obs)
        self.assertEqual(c1.binding_id, c2.binding_id)
        self.assertEqual(c2.revision, 1)

        # Loaded contact must match
        c_loaded = load_contact(self.ws, "codearts")
        self.assertIsNotNone(c_loaded)
        self.assertEqual(c_loaded.binding_id, c1.binding_id)
        self.assertEqual(c_loaded.revision, 1)

    def test_wrong_invite_or_workspace_cannot_activate(self):
        obs = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_001",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_huawei_codearts",
            instance_id="inst_run_1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="chat_sess_01",
            identity_source="host_query",
            exists=True,
        )

        # Non-existent invite
        with self.assertRaises(TriggerContractError):
            confirm_contact(self.ws, "inv_nonexistent_123", obs)

        # Mismatched workspace: invite has project_key=self.ws, but tried in other_ws
        invite = create_invite(
            self.ws,
            member_id="codearts",
            connector_id="agentbus.codearts-hook",
            installation_key="inst_huawei_codearts",
        )
        other_ws = Path(self.temp_dir.name) / "other_project"
        other_invites_dir = other_ws / ".agentbus" / "triggers" / "invitations"
        other_invites_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(self.ws / ".agentbus" / "triggers" / "invitations" / f"{invite.invite_id}.json", other_invites_dir)
        with self.assertRaises(TriggerContractError) as ctx:
            confirm_contact(other_ws, invite.invite_id, obs)
        self.assertIn(TriggerErrorCode.PROJECT_MISMATCH.value, str(ctx.exception))

    def test_old_revision_cannot_replace_new_binding(self):
        invite1 = create_invite(
            self.ws,
            member_id="codearts",
            connector_id="agentbus.codearts-hook",
            installation_key="inst_huawei_codearts",
        )
        obs1 = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_001",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_huawei_codearts",
            instance_id="inst_run_1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="chat_sess_01",
            identity_source="host_query",
            exists=True,
        )
        c1 = confirm_contact(self.ws, invite1.invite_id, obs1)
        self.assertEqual(c1.revision, 1)

        # Rebind to rev 2
        invite2 = prepare_rebind(
            self.ws,
            member_id="codearts",
            expected_revision=1,
            connector_id="agentbus.codearts-hook",
            installation_key="inst_huawei_codearts",
        )
        obs2 = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_002",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_huawei_codearts",
            instance_id="inst_run_2",
            observed_at="2026-10-30T12:05:00Z",
            status="ready",
            native_conversation_id="chat_sess_02",
            identity_source="host_query",
            exists=True,
        )
        c2 = confirm_contact(self.ws, invite2.invite_id, obs2)
        self.assertEqual(c2.revision, 2)
        self.assertEqual(c2.native_conversation_id, "chat_sess_02")

        # Now an old invite with expected_revision=1 cannot replace rev 2
        invite_old = create_invite(
            self.ws,
            member_id="codearts",
            connector_id="agentbus.codearts-hook",
            installation_key="inst_huawei_codearts",
            expected_revision=1,
        )
        with self.assertRaises(TriggerContractError) as ctx:
            confirm_contact(self.ws, invite_old.invite_id, obs1)
        self.assertIn("CAS revision conflict", str(ctx.exception))

    def test_unknown_native_id_stays_pending(self):
        invite = create_invite(
            self.ws,
            member_id="codearts",
            connector_id="agentbus.codearts-hook",
            installation_key="inst_huawei_codearts",
        )
        obs_unknown = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_003",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_huawei_codearts",
            instance_id="inst_run_1",
            observed_at="2026-10-30T12:00:00Z",
            status="not_ready",
            native_conversation_id="chat_unknown_id",
            identity_source="prompt_response",
            exists=False,  # Target not verified
        )
        with self.assertRaises(TriggerContractError) as ctx:
            confirm_contact(self.ws, invite.invite_id, obs_unknown)
        self.assertIn(TriggerErrorCode.TARGET_UNKNOWN.value, str(ctx.exception))

        # Member contact must remain uncreated or not bound
        loaded = load_contact(self.ws, "codearts")
        self.assertIsNone(loaded)

    def test_contact_not_scanned_as_mail(self):
        invite = create_invite(
            self.ws,
            member_id="codearts",
            connector_id="agentbus.codearts-hook",
            installation_key="inst_huawei_codearts",
        )
        obs = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_001",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_huawei_codearts",
            instance_id="inst_run_1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="chat_sess_01",
            identity_source="host_query",
            exists=True,
        )
        confirm_contact(self.ws, invite.invite_id, obs)

        contact_file = self.ws / ".agentbus" / "agents" / "codearts" / "contact.json"
        self.assertTrue(contact_file.exists())

        # Mail scanning path is mailbox/{ready,processing,done,result}/
        ready_dir = self.ws / ".agentbus" / "agents" / "codearts" / "mailbox" / "ready"
        if ready_dir.exists():
            files_in_ready = list(ready_dir.glob("*.json"))
            self.assertNotIn(contact_file, files_in_ready)

    def test_second_project_same_member_independent_contact(self):
        ws2 = Path(self.temp_dir.name) / "test_project_2"
        ws2.mkdir(parents=True, exist_ok=True)

        inv1 = create_invite(self.ws, "codearts", "agentbus.codearts-hook", "inst_1")
        inv2 = create_invite(ws2, "codearts", "agentbus.codearts-hook", "inst_1")

        obs1 = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_1",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_1",
            instance_id="i1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="chat_ws1",
            identity_source="host_query",
            exists=True,
        )
        obs2 = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_2",
            project_key=str(ws2),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_1",
            instance_id="i1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="chat_ws2",
            identity_source="host_query",
            exists=True,
        )

        c1 = confirm_contact(self.ws, inv1.invite_id, obs1)
        c2 = confirm_contact(ws2, inv2.invite_id, obs2)

        self.assertEqual(c1.native_conversation_id, "chat_ws1")
        self.assertEqual(c2.native_conversation_id, "chat_ws2")
        self.assertNotEqual(c1.binding_id, c2.binding_id)

    def test_retire_and_prepare_rebind(self):
        invite = create_invite(self.ws, "codearts", "agentbus.codearts-hook", "inst_1")
        obs = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_1",
            project_key=str(self.ws),
            connector_id="agentbus.codearts-hook",
            connector_version="1.0.0",
            installation_key="inst_1",
            instance_id="i1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="chat_sess_01",
            identity_source="host_query",
            exists=True,
        )
        c1 = confirm_contact(self.ws, invite.invite_id, obs)
        self.assertEqual(c1.status, "bound")

        # Retire
        c_retired = retire_contact(self.ws, "codearts", expected_revision=1, reason="user_requested")
        self.assertEqual(c_retired.status, "retired")
        self.assertEqual(c_retired.reason_code, "user_requested")
        self.assertEqual(c_retired.revision, 2)

        # Check history snapshot
        history_dir = self.ws / ".agentbus" / "agents" / "codearts" / "contact-history"
        snapshots = list(history_dir.glob("*.json"))
        self.assertEqual(len(snapshots), 1)

        # Prepare rebind with rev 2
        inv_rebind = prepare_rebind(self.ws, "codearts", expected_revision=2, connector_id="agentbus.codearts-hook", installation_key="inst_1")
        self.assertEqual(inv_rebind.expected_revision, 2)


if __name__ == "__main__":
    unittest.main()
