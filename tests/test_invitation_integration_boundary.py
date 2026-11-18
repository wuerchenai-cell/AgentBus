"""Codex 2026-10-05: acceptance counterexamples independent of author tests."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest
from agentbus.invitation_copy import describe_invitation


class InvitationBoundaryTests(unittest.TestCase):
    def test_native_label_without_formal_receipt_cannot_certify_capability(self):
        result=describe_invitation({"task_id":"task-1","capability_sample":{"outcome":"passed","transport":"native"}})
        self.assertFalse(result["capability_passed"])

    def test_verified_readonly_transcription_can_verify_limited_capability(self):
        result=describe_invitation({"task_id":"task-1","receipt_id":"result-1","linked_task_id":"task-1",
            "outcome":"done", "capability_sample":{"outcome":"passed","transport":"chat-transcription",
            "project_id":"project-1","task_id":"task-1","result_id":"result-1","verified":True,
            "acceptance_verified":True,"capability_family":"readonly-review"}})
        self.assertTrue(result["capability_passed"])


if __name__ == '__main__':
    unittest.main()
