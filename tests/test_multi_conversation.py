"""Conversation-scoped identity regressions for one AgentBus member."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.bus import Bus, LateResultRejected
from agentbus import claim_fencing, heartbeat
from agentbus.config import load
from agentbus.director import Director
from agentbus.task_tree import TaskForest
from agentbus.task_tree_view import continuation_text
from agentbus.web_bridge import WebBridge


class MultiConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.cfg = load(self.workspace, create=True)
        self.bus = Bus(self.workspace / ".agentbus")
        self.bus.init()

    def _task(self, title):
        task = self.bus.make("codex", "workbuddy", "task", title=title,
                             body=title, result_to="codex")
        self.bus.post(task)
        return task

    def _claim(self, task):
        path = next(path for path in self.bus.pending("workbuddy")
                    if path.stem == task.id)
        claimed = self.bus.claim("workbuddy", path)
        self.bus.heartbeat("workbuddy", claimed, claim_fencing.captured_ticket(claimed),
                           note="test conversation started")
        return claimed, claim_fencing.captured_ticket(claimed)

    def test_one_member_keeps_distinct_conversations_across_sequential_tasks(self):
        first, second = self._task("first leaf"), self._task("second leaf")
        first_claim, first_ticket = self._claim(first)
        self.bus.commit_result('workbuddy', first_claim, first_ticket, body='First conversation completed')
        second_claim, second_ticket = self._claim(second)

        self.assertNotEqual(first_ticket.conversation_id, second_ticket.conversation_id)
        self.assertEqual(first_claim.claim_conversation_id, first_ticket.conversation_id)
        self.assertEqual(second_claim.claim_conversation_id, second_ticket.conversation_id)
        self.assertEqual(claim_fencing.captured_ticket(first_claim).conversation_id,
                         first_ticket.conversation_id)

    def test_heartbeat_and_result_keep_the_claim_conversation_identity(self):
        task = self._task("deliver with a session identity")
        claimed, ticket = self._claim(task)

        beat = heartbeat.record(self.bus.root, task.id, "workbuddy", ticket,
                                task_record=claimed.to_dict(), now=100.0)
        self.assertEqual(beat["conversation_id"], ticket.conversation_id)
        observed = heartbeat.state(self.bus.root, task.id, "workbuddy",
                                   task_record=claimed.to_dict(), now=101.0)
        self.assertEqual(observed["conversation_id"], ticket.conversation_id)
        self.assertFalse(observed["stale"])

        # The synthetic clock above proves conversation binding only. Refresh a real
        # wall-clock heartbeat before exercising the production delivery gate.
        self.bus.heartbeat("workbuddy", claimed, ticket, note="test delivery started")

        receipt = self.bus.commit_result("workbuddy", claimed, ticket,
                                         body="done", exit_code=0)
        self.assertEqual(receipt.source_conversation_id, ticket.conversation_id)

    def test_result_with_another_conversation_id_is_rejected_even_with_claim_id(self):
        task = self._task("reject cross-conversation receipt")
        claimed, ticket = self._claim(task)
        receipt = self.bus.make("workbuddy", "codex", "result", reply_to=task.id,
                                result_to="codex", body="wrong conversation")
        receipt.source_claim_epoch = ticket.claim_epoch
        receipt.source_claim_id = ticket.claim_id
        receipt.source_conversation_id = "cv-000000000000000000000000"

        with self.assertRaises(LateResultRejected):
            self.bus.post(receipt)
        self.assertEqual(self.bus.find_results(task.id), [])

    def test_reclaim_rotates_conversation_and_rejects_the_previous_dialogue(self):
        task = self._task("resume after a different dialogue takes over")
        old_claim, old_ticket = self._claim(task)
        self.bus.recover_claim("workbuddy", task.id, owner_confirmed_inactive=True)
        new_claim, new_ticket = self._claim(task)
        self.assertNotEqual(old_ticket.conversation_id, new_ticket.conversation_id)

        late = self.bus.make("workbuddy", "codex", "result", reply_to=task.id,
                             result_to="codex", body="late from previous dialogue")
        late.source_claim_epoch = old_ticket.claim_epoch
        late.source_claim_id = old_ticket.claim_id
        late.source_conversation_id = old_ticket.conversation_id
        with self.assertRaises(LateResultRejected):
            self.bus.post(late)

        current = self.bus.commit_result("workbuddy", new_claim, new_ticket,
                                         body="current dialogue", exit_code=0)
        self.assertEqual(current.source_conversation_id, new_ticket.conversation_id)

    def test_task_tree_and_desktop_snapshot_list_same_member_conversations(self):
        first, second = self._task("parallel leaf one"), self._task("parallel leaf two")
        self._claim(first)
        # Inject pre-limit historical parallel state solely to test its display.
        # Current production claims remain constrained by the single work slot.
        with patch.object(self.bus, 'check_work_slot', return_value=None):
            self._claim(second)
        forest = TaskForest(self.cfg)
        forest.replace_structure({
            "schema_version": 1,
            "roots": [{"id": "root", "title": "Project"}],
            "branches": [{"id": "branch", "title": "Work", "parent_id": "root"}],
            "leaves": [
                {"id": "leaf-one", "title": "First", "parent_id": "branch", "task_id": first.id},
                {"id": "leaf-two", "title": "Second", "parent_id": "branch", "task_id": second.id},
            ],
        })

        projected = forest.snapshot()
        conversations = {leaf["conversation_id"] for leaf in projected["leaves"]}
        self.assertEqual(len(conversations), 2)

        response = WebBridge(self.workspace).call("snapshot")
        self.assertTrue(response["ok"], response)
        data = response["data"]
        workbuddy = next(member for member in data["members"] if member["id"] == "workbuddy")
        self.assertEqual(workbuddy["active_conversation_count"], 2)
        self.assertEqual({item["conversation_id"] for item in workbuddy["active_conversations"]},
                         conversations)

    def test_handoff_instruction_names_the_specific_agentbus_conversation(self):
        task = self._task("continue in the intended conversation")
        claimed, ticket = self._claim(task)
        leaf = {
            "id": "leaf-continue", "task_id": task.id, "owner": "workbuddy",
            "conversation_id": ticket.conversation_id, "goal": task.title,
        }

        prompt = continuation_text(str(self.workspace), leaf, "workbuddy")

        self.assertIn("AgentBus 对话编号：" + ticket.conversation_id, prompt)
        self.assertIn("此编号用于区分本系统中的认领会话", prompt)
        self.assertNotIn(ticket.claim_id, prompt)

    def test_same_member_gets_a_separate_native_session_record_per_conversation(self):
        made = []

        def factory(agent, config):
            adapter = SimpleNamespace(agent=agent, config=config)
            made.append(adapter)
            return adapter

        director = Director(self.cfg, adapter_factory=factory)
        first_id = "cv-" + "a" * 24
        second_id = "cv-" + "b" * 24
        first = director.adapter("codex", conversation_id=first_id)
        first_again = director.adapter("codex", conversation_id=first_id)
        second = director.adapter("codex", conversation_id=second_id)

        self.assertIs(first, first_again)
        self.assertIsNot(first, second)
        self.assertNotEqual(first.config["_session_record_path"],
                            second.config["_session_record_path"])
        self.assertEqual(len(made), 2)


if __name__ == "__main__":
    unittest.main()
