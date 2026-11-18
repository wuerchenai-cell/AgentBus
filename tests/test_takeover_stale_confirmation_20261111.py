import unittest
from tests.test_task_takeover import TestTaskTakeover
from agentbus import task_tree


class TakeoverStaleConfirmationTests(TestTaskTakeover):
    def _first_claim(self):
        tree = self.forest._structure()
        leaf = self.forest._leaf(tree, "LEAF-TEST-001")
        self.forest._allocate(tree, leaf, "codex")
        return self.forest.claim_leaf("LEAF-TEST-001", "codex")

    def test_force_requires_original_binding(self):
        first = self._first_claim()
        with self.assertRaises(task_tree.TaskTreeError):
            self.forest.takeover_leaf("LEAF-TEST-001", "workbuddy", force=True)
        self.assertEqual(self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")["task_id"], first.id)

    def test_old_confirmation_cannot_take_over_new_claim(self):
        first = self._first_claim()
        self.forest.takeover_leaf("LEAF-TEST-001", "antigravity", force=True,
            expected_task_id=first.id, expected_owner="codex", expected_claim_epoch=first.claim_epoch)
        second = self.forest.claim_leaf("LEAF-TEST-001", "antigravity")
        with self.assertRaises(task_tree.TaskTreeError):
            self.forest.takeover_leaf("LEAF-TEST-001", "workbuddy", force=True,
                expected_task_id=first.id, expected_owner="codex", expected_claim_epoch=first.claim_epoch)
        live = self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")
        self.assertEqual((live["task_id"], live["owner"], live["location"]), (second.id, "antigravity", "processing"))

    def test_admin_handoff_rejects_old_binding(self):
        first = self._first_claim()
        with self.assertRaises(task_tree.TaskTreeError):
            self.forest.handoff_leaf("LEAF-TEST-001", "workbuddy", reason="test",
                owner_confirmed_inactive=True, expected_task_id=first.id,
                expected_owner="codex", expected_claim_epoch=first.claim_epoch + 1)
        self.assertEqual(self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")["task_id"], first.id)

    def test_old_generation_for_same_task_cannot_be_taken(self):
        first = self._first_claim()
        self.forest.bus.recover_claim("codex", first.id, owner_confirmed_inactive=True,
                                      expected_claim_epoch=first.claim_epoch)
        second = self.forest.claim_leaf("LEAF-TEST-001", "codex")
        self.assertGreater(second.claim_epoch, first.claim_epoch)
        with self.assertRaises(task_tree.TaskTreeError):
            self.forest.takeover_leaf("LEAF-TEST-001", "workbuddy", force=True,
                expected_task_id=first.id, expected_owner="codex", expected_claim_epoch=first.claim_epoch)
        self.assertEqual(self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")["claim_epoch"], second.claim_epoch)

    def test_stale_confirmation_does_not_transfer_ready_successor(self):
        first = self._first_claim()
        successor = self.forest.takeover_leaf("LEAF-TEST-001", "antigravity", force=True,
            expected_task_id=first.id, expected_owner="codex", expected_claim_epoch=first.claim_epoch)
        with self.assertRaises(task_tree.TaskTreeError):
            self.forest.takeover_leaf("LEAF-TEST-001", "workbuddy", force=True,
                expected_task_id=first.id, expected_owner="codex", expected_claim_epoch=first.claim_epoch)
        self.assertEqual(self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")["task_id"], successor["new_task_id"])

    def test_bridge_rejects_stale_and_accepts_fresh_binding(self):
        from agentbus.web_bridge import WebBridge
        first = self._first_claim()
        bridge = WebBridge(self.tmp)
        bad = bridge.call("takeover_leaf", {"leaf_id":"LEAF-TEST-001", "agent":"workbuddy",
            "force":True, "expected_task_id":first.id, "expected_owner":"codex",
            "expected_claim_epoch":first.claim_epoch + 1})
        self.assertFalse(bad["ok"])
        self.assertEqual(self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")["task_id"], first.id)
        good = bridge.call("takeover_leaf", {"leaf_id":"LEAF-TEST-001", "agent":"workbuddy",
            "force":True, "expected_task_id":first.id, "expected_owner":"codex",
            "expected_claim_epoch":first.claim_epoch})
        self.assertTrue(good["ok"], good)
        self.assertEqual(good["data"]["agent"], "workbuddy")

    def test_cli_confirmed_handoff_accepts_binding_without_old_secret_ticket(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from agentbus.cli import main
        first = self._first_claim()
        args = ["tree", "handoff", "--workspace", str(self.tmp), "--leaf", "LEAF-TEST-001",
                "--agent", "workbuddy", "--note", "stopped", "--owner-confirmed-inactive",
                "--id", first.id, "--expected-owner", "codex", "--claim-epoch", str(first.claim_epoch)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertNotEqual(self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")["location"], "processing")

    def test_bridge_handoff_rejects_stale_generation(self):
        from agentbus.web_bridge import WebBridge
        first = self._first_claim()
        result = WebBridge(self.tmp).call("handoff_leaf", {"leaf_id":"LEAF-TEST-001",
            "agent":"workbuddy", "reason":"isolated", "owner_confirmed_inactive":True,
            "expected_task_id":first.id, "expected_owner":"codex",
            "expected_claim_epoch":first.claim_epoch + 1})
        self.assertFalse(result["ok"])
        live = self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")
        self.assertEqual((live["task_id"], live["owner"], live["location"]), (first.id, "codex", "processing"))
