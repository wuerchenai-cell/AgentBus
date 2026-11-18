"""Persistent, secret-free index for handing AgentBus work across chats."""
from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus import claim_fencing
from agentbus.bus import Bus, LateResultRejected
from agentbus.config import load
from agentbus.recovery_center import project_handoff_index
from agentbus.task_tree import TaskForest, TaskTreeError
from agentbus.web_bridge import WebBridge


class MultiSessionRecoveryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.cfg = load(self.workspace, create=True)
        self.forest = TaskForest(self.cfg)
        self.bus = self.forest.bus
        self.bus.init()
        self.forest.replace_structure({
            "schema_version": 1,
            "roots": [{"id": "project", "title": "Project"}],
            "branches": [{"id": "work", "parent_id": "project", "title": "Work"}],
            "leaves": [
                {"id": "first", "parent_id": "work", "title": "First task", "goal": "First goal"},
                {"id": "second", "parent_id": "work", "title": "Second task", "goal": "Second goal"},
            ],
        })

    def _claimed(self, leaf_id, agent="workbuddy"):
        claimed = self.forest.claim_leaf(leaf_id, agent)
        self.bus.heartbeat(agent, claimed, claim_fencing.captured_ticket(claimed),
                           note="test conversation started")
        return claimed

    def test_recovery_index_lists_parallel_chats_for_one_member_without_ticket_data(self):
        first = self._claimed("first")
        first_ticket = claim_fencing.captured_ticket(first)
        self.bus.commit_result("workbuddy", first, first_ticket,
                               body="First task returned by its original conversation", exit_code=0)
        second = self._claimed("second")
        second_ticket = claim_fencing.captured_ticket(second)

        response = WebBridge(self.workspace).call("recovery")

        self.assertTrue(response["ok"], response)
        entries = response["data"]["handoff_index"]["entries"]
        active = [item for item in entries if item["relation"] == "current"]
        self.assertEqual({item["task_id"] for item in active}, {first.id, second.id})
        self.assertEqual({item["conversation_id"] for item in active},
                         {first_ticket.conversation_id, second_ticket.conversation_id})
        self.assertEqual({item["agent"] for item in active}, {"workbuddy"})
        self.assertEqual(next(item for item in active if item["task_id"] == first.id)["state"], "delivered")
        serialized = json.dumps(response["data"]["handoff_index"], ensure_ascii=False)
        self.assertNotIn(first_ticket.claim_id, serialized)
        self.assertNotIn(second_ticket.claim_id, serialized)
        self.assertNotIn("claim_epoch", serialized)
        self.assertNotIn("claim_id", serialized)

    @unittest.skipUnless(sys.platform == "win32", "formal claim release is supported on Windows")
    def test_confirmed_handoff_keeps_old_chat_index_and_rejects_its_late_result(self):
        original = self._claimed("first")
        old_ticket = claim_fencing.captured_ticket(original)

        handed_off = self.forest.handoff_leaf(
            "first", "codex", reason="额度耗尽；从已保存的阶段说明继续。",
            successor="codex", owner_confirmed_inactive=True, expected_task_id=original.id, expected_owner="workbuddy", expected_claim_epoch=original.claim_epoch)
        history = handed_off["handoff_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["task_id"], original.id)
        self.assertEqual(history[0]["agent"], "workbuddy")
        self.assertEqual(history[0]["conversation_id"], old_ticket.conversation_id)
        self.assertTrue(history[0]["owner_confirmed_inactive"])
        self.assertNotIn("claim_id", history[0])
        self.assertNotIn("claim_epoch", history[0])

        replacement = self._claimed("first", "codex")
        new_ticket = claim_fencing.captured_ticket(replacement)
        self.assertNotEqual(old_ticket.conversation_id, new_ticket.conversation_id)
        with self.assertRaises(LateResultRejected):
            self.bus.commit_result("workbuddy", original, old_ticket,
                                   body="late output from the retired chat")

        index = project_handoff_index(self.forest.snapshot())
        old = next(item for item in index["entries"] if item["task_id"] == original.id)
        current = next(item for item in index["entries"] if item["task_id"] == replacement.id)
        self.assertEqual(old["relation"], "previous")
        self.assertEqual(current["relation"], "current")
        self.assertEqual(current["conversation_id"], new_ticket.conversation_id)
        self.assertNotIn(old_ticket.claim_id, json.dumps(index, ensure_ascii=False))

    def test_missing_receipt_does_not_complete_or_allow_unconfirmed_takeover(self):
        claimed = self._claimed("first")
        ticket = claim_fencing.captured_ticket(claimed)

        with self.assertRaises(TaskTreeError):
            self.forest.handoff_leaf("first", "codex", reason="尝试接手", successor="codex")

        leaf = next(item for item in self.forest.snapshot()["leaves"] if item["id"] == "first")
        self.assertIn(leaf["state"], {"claimed", "running"})
        self.assertIsNone(leaf["result_id"])
        self.assertEqual(leaf["handoff_history"], [])
        index = project_handoff_index(self.forest.snapshot())
        entry = next(item for item in index["entries"] if item["task_id"] == claimed.id)
        self.assertIn(entry["state"], {"claimed", "running"})
        self.assertEqual(entry["conversation_id"], ticket.conversation_id)
        self.assertIn("原会话", entry["next_action"])

    def test_normalizer_rejects_claim_secrets_in_persisted_handoff_history(self):
        tree = {
            "schema_version": 1,
            "roots": [{"id": "project", "title": "Project"}],
            "branches": [{"id": "work", "parent_id": "project", "title": "Work"}],
            "leaves": [{
                "id": "first", "parent_id": "work", "title": "First task",
                "handoff_history": [{"task_id": "m-abc", "agent": "workbuddy",
                                     "conversation_id": "cv-" + "a" * 24,
                                     "claim_id": "must-never-persist"}],
            }],
        }
        with self.assertRaises(TaskTreeError):
            TaskForest._normalize(tree)

    def test_handoff_index_never_exposes_free_text_notes(self):
        api_key = "sk-proj-" + "A" * 48
        jwt = "eyJ" + "a" * 18 + "." + "b" * 18 + "." + "c" * 18
        forest = {"leaves": [{
            "id": "first", "section_no": "1.1", "title": "First task",
            "task_id": "m-current", "history_task_ids": ["m-old"],
            "owner": "codex", "state": "claimed", "handoff": "next " + jwt,
            "handoff_history": [{
                "task_id": "m-old", "agent": "workbuddy",
                "conversation_id": "cv-" + "a" * 24, "timestamp": "2026-10-11T00:00:00Z",
                "reason": "resume with " + api_key, "owner_confirmed_inactive": True,
            }],
        }]}
        events = [{
            "event": "recovered", "id": "m-current", "agent": "codex",
            "conversation_id": "cv-" + "d" * 24, "ts": "2026-10-11T00:01:00Z",
            "reason": "prior " + jwt, "owner_confirmed_inactive": True,
        }]

        serialized = json.dumps(project_handoff_index(forest, events), ensure_ascii=False)

        self.assertNotIn(api_key, serialized)
        self.assertNotIn(jwt, serialized)
        self.assertIn("自由文本", serialized)

    @unittest.skipUnless(sys.platform == "win32", "confirmed claim recovery is supported on Windows")
    def test_confirmed_recovery_releases_old_exclusive_lock_and_indexes_both_chats(self):
        original = self._claimed("first")
        old_ticket = claim_fencing.captured_ticket(original)

        self.bus.recover_claim("workbuddy", original.id, owner_confirmed_inactive=True)
        replacement = self._claimed("first")
        new_ticket = claim_fencing.captured_ticket(replacement)

        self.assertNotEqual(old_ticket.conversation_id, new_ticket.conversation_id)
        with self.assertRaises(LateResultRejected):
            self.bus.commit_result("workbuddy", original, old_ticket,
                                   body="late output from the recovered chat")
        index = project_handoff_index(self.forest.snapshot(), self.bus.events())
        old = next(item for item in index["entries"]
                   if item["task_id"] == original.id and item["relation"] == "previous")
        current = next(item for item in index["entries"]
                       if item["task_id"] == replacement.id and item["relation"] == "current")
        self.assertEqual(old["conversation_id"], old_ticket.conversation_id)
        self.assertTrue(old["owner_confirmed_inactive"])
        self.assertEqual(current["conversation_id"], new_ticket.conversation_id)
        self.assertNotIn(old_ticket.claim_id, json.dumps(index, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
