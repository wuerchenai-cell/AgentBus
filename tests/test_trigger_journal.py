#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_journal.py: 投递日志、状态跃迁、去重幂等与崩溃不确定调和测试。"""

import os
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contracts import (
    WakeRequest,
    TriggerContractError,
    TriggerErrorCode,
    derive_request_id,
)
from agentbus.trigger.journal import (
    DeliveryJournal,
    DeliveryStatus,
    JournalEntry,
)


class TriggerJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)
        self.journal = DeliveryJournal(self.ws)

    def _make_req(self, purpose="task_ready", sources=None, scope="scope_1"):
        sources = sources or ["msg_001"]
        req_id = derive_request_id(
            project_key=str(self.ws),
            member_id="codearts",
            binding_id="bnd_01",
            binding_revision=1,
            purpose=purpose,
            source_message_ids=sources,
            scope_id=scope,
        )
        return WakeRequest(
            schema_version=1,
            request_id=req_id,
            project_key=str(self.ws),
            member_id="codearts",
            binding_id="bnd_01",
            binding_revision=1,
            purpose=purpose,
            source_message_ids=sources,
            scope_id=scope,
            created_at="2026-10-30T12:00:00Z",
            prompt_summary="Work on task msg_001",
            prompt_utf8_sha256="hash123",
            execution_surface="desktop_existing",
            installation_key="inst_1",
            connector_version="1.0.0",
        )

    def test_duplicate_events_make_one_request(self):
        req = self._make_req()

        # Enqueue 100 times
        for _ in range(100):
            res = self.journal.enqueue(req)
            self.assertEqual(res.status, DeliveryStatus.QUEUED.value)
            self.assertEqual(res.request_id, req.request_id)

        # Active queued requests count must be exactly 1
        queued = self.journal.list_by_status(DeliveryStatus.QUEUED.value)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].request_id, req.request_id)

    def test_submitting_transition_and_uncertain_on_crash(self):
        req = self._make_req()
        self.journal.enqueue(req)

        # Transition to prepared -> submitting
        self.journal.transition(req.request_id, DeliveryStatus.PREPARED.value)
        self.journal.transition(req.request_id, DeliveryStatus.SUBMITTING.value, details={"attempt_id": 1})

        # Simulate service crash and restart: create a new DeliveryJournal instance
        recovered_journal = DeliveryJournal(self.ws)
        recovered_journal.reconcile_on_startup()

        # Must be in uncertain state, NEVER queued or submitted
        status_info = recovered_journal.get_request_status(req.request_id)
        self.assertEqual(status_info["status"], DeliveryStatus.UNCERTAIN.value)
        self.assertEqual(status_info.get("uncertain_reason"), "CRASH_DURING_SUBMISSION")

        # Uncertain request cannot be blindly re-queued or auto-retried
        can_retry = recovered_journal.can_auto_retry(req.request_id)
        self.assertFalse(can_retry)

    def test_reverse_source_index_prevents_duplicate_submission(self):
        req1 = self._make_req(sources=["msg_task_A", "msg_task_B"], scope="scope_1")
        self.journal.enqueue(req1)
        self.journal.transition(req1.request_id, DeliveryStatus.SUBMITTING.value)

        # Attempt to enqueue req2 that shares msg_task_A under a different scope
        req2 = self._make_req(sources=["msg_task_A", "msg_task_C"], scope="scope_2")
        with self.assertRaises(TriggerContractError) as ctx:
            self.journal.enqueue(req2)
        self.assertIn("Source message msg_task_A is already in flight", str(ctx.exception))

    def test_submitted_and_rejected_transitions(self):
        req = self._make_req()
        self.journal.enqueue(req)
        self.journal.transition(req.request_id, DeliveryStatus.PREPARED.value)
        self.journal.transition(req.request_id, DeliveryStatus.SUBMITTING.value, details={"attempt_id": 1})
        self.journal.transition(
            req.request_id,
            DeliveryStatus.SUBMITTED.value,
            details={"native_message_id": "nat_999", "accepted_by_host": True}
        )

        status_info = self.journal.get_request_status(req.request_id)
        self.assertEqual(status_info["status"], DeliveryStatus.SUBMITTED.value)
        self.assertEqual(status_info["details"]["native_message_id"], "nat_999")

    def test_journal_audit_trail_immutable(self):
        req = self._make_req()
        self.journal.enqueue(req)
        self.journal.transition(req.request_id, DeliveryStatus.PREPARED.value)
        self.journal.transition(req.request_id, DeliveryStatus.SUBMITTING.value)

        history = self.journal.get_history(req.request_id)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0].to_status, DeliveryStatus.QUEUED.value)
        self.assertEqual(history[1].to_status, DeliveryStatus.PREPARED.value)
        self.assertEqual(history[2].to_status, DeliveryStatus.SUBMITTING.value)


if __name__ == "__main__":
    unittest.main()
