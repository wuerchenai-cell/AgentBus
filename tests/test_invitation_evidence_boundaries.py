"""Codex, 2026-10-06: invitation evidence boundaries from peer review."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import sys
import tempfile
import unittest
from pathlib import Path

from agentbus.config import load, save
from agentbus.invitation_copy import describe_invitation
from agentbus.member_invitation import (
    invitation_status,
    prepare_invitation,
    record_invitation_event,
)
from agentbus.project_workflow import prepare_project_workflow


def completed_readonly_evidence():
    return {
        "task_id": "task-1",
        "receipt_id": "result-1",
        "linked_task_id": "task-1",
        "outcome": "done",
        "capability_sample": {
            "outcome": "passed",
            "transport": "chat-transcription",
            "project_id": "project-1",
            "task_id": "task-1",
            "result_id": "result-1",
            "verified": True,
            "acceptance_verified": True,
            "capability_family": "readonly-review",
        },
    }


class InvitationEvidencePureTests(unittest.TestCase):
    def test_arbitrary_sent_confirmation_is_not_delivery_evidence(self):
        state = describe_invitation({"sent_confirmed_by": "大概吧"})
        self.assertFalse(state["sent"])
        self.assertIn("未验证", state["labels"]["sent"])

    def test_authorized_observation_requires_observer_and_timestamp(self):
        observer_only = describe_invitation({"observed_sent_by": "codex"})
        time_only = describe_invitation({"observed_at": "2026-10-06T10:00:00+08:00"})
        complete = describe_invitation({
            "observed_sent_by": "codex",
            "observed_at": "2026-10-06T10:00:00+08:00",
        })
        self.assertFalse(observer_only["sent"])
        self.assertFalse(time_only["sent"])
        self.assertTrue(complete["sent"])
        self.assertIn("codex", complete["labels"]["sent"])

    def test_write_capability_requires_verified_artifact_digest(self):
        evidence = completed_readonly_evidence()
        evidence["capability_sample"]["capability_family"] = "file-write"
        self.assertFalse(describe_invitation(evidence)["capability_passed"])

        evidence["capability_sample"]["artifact_verified"] = True
        self.assertFalse(describe_invitation(evidence)["capability_passed"])

        evidence["capability_sample"]["artifact_sha256"] = "a" * 64
        self.assertTrue(describe_invitation(evidence)["capability_passed"])

    def test_receipt_without_claim_does_not_report_claimed(self):
        evidence = completed_readonly_evidence()
        evidence["capability_sample"]["outcome"] = "unverified"
        state = describe_invitation(evidence)
        self.assertTrue(state["receipt_channel_verified"])
        self.assertNotIn("已认领", state["next_action"])

    def test_direct_formal_receipt_needs_no_copy_or_sent_event(self):
        state = describe_invitation(completed_readonly_evidence())
        self.assertFalse(state["copied"])
        self.assertFalse(state["sent"])
        self.assertTrue(state["receipt_channel_verified"])
        self.assertTrue(state["capability_passed"])


class InvitationEvidenceRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load(self.root, create=True)
        self.cfg.data["workers"] = {
            "codex": {
                "enabled": True,
                "executor_enabled": True,
                "kind": "gui",
                "can_spawn": False,
            },
            "new-agent": {
                "display_name": "New Agent",
                "enabled": True,
                "executor_enabled": True,
                "kind": "gui",
                "can_spawn": False,
            },
        }
        save(self.cfg)
        prepare_project_workflow(self.cfg)
        manifest = self.root / ".agentbus/workflow.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["launcher"] = {
            "mode": "source",
            "python": sys.executable,
            "entrypoint": str(Path(__file__).resolve()),
        }
        manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.invite = prepare_invitation(
            self.cfg, "codex", "new-agent", "evidence-boundary", writable=False
        )

    def test_sent_event_rejects_uncontrolled_source(self):
        with self.assertRaisesRegex(ValueError, "observation source"):
            record_invitation_event(
                self.cfg, "codex", self.invite["invite_id"], "sent", "大概吧"
            )

    def test_authorized_computer_use_event_records_delivery(self):
        record_invitation_event(
            self.cfg,
            "codex",
            self.invite["invite_id"],
            "sent",
            "authorized-computer-use:codex",
        )
        state = invitation_status(self.cfg, self.invite["invite_id"])
        self.assertTrue(state["sent"])
        self.assertIn("codex", state["labels"]["sent"])


if __name__ == "__main__":
    unittest.main()
