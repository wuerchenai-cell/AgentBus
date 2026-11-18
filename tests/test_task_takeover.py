"""Tests for Task Takeover and Seamless Handoff mechanism.
Source: Google Antigravity, 2026-10-13.
"""
import copy
import json
import time
import unittest
from pathlib import Path
import tempfile
import shutil

from agentbus import config as ab_config
from agentbus import heartbeat
from agentbus import task_tree
from agentbus.bus import Bus, Message, now_iso
from agentbus.claim_fencing import captured_ticket
from agentbus.web_bridge import WebBridge


class TestTaskTakeover(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbus_test_takeover_"))
        self.agentbus_dir = self.tmp / ".agentbus"
        self.agentbus_dir.mkdir(parents=True)
        
        # Seed basic config
        config_data = {
            "director": "codex",
            "coordinator": "antigravity",
            "workers": {
                "codex": {"enabled": True, "communication_mode": "file"},
                "antigravity": {"enabled": True, "communication_mode": "file"},
                "workbuddy": {"enabled": True, "communication_mode": "file"}
            },
            "gates": {
                "max_retries": 3,
                "task_timeout_s": 1800,
                "total_budget_s": 7200
            }
        }
        (self.agentbus_dir / "config.json").write_text(json.dumps(config_data), encoding="utf-8")
        
        self.bus = Bus(self.agentbus_dir)
        self.bus.init()
        self.cfg = ab_config.load(self.tmp)
        self.forest = task_tree.TaskForest(self.cfg)
        
        # Seed an initial tree structure with one leaf
        init_tree = {
            "schema_version": 1,
            "roots": [{"id": "root", "parent_id": None, "title": "Root Task", "goal": "Goal"}],
            "branches": [{"id": "branch", "parent_id": "root", "title": "Branch"}],
            "leaves": [
                {
                    "id": "LEAF-TEST-001",
                    "parent_id": "branch",
                    "title": "Initial Task",
                    "section_no": "1.1",
                    "goal": "Do work",
                    "dependencies": [],
                    "expected_artifacts": ["out.txt"]
                }
            ]
        }
        (self.agentbus_dir / "task-tree.json").write_text(json.dumps(init_tree), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_direct_takeover_unassigned(self):
        """Unassigned leaf can be directly taken over by an eligible agent."""
        res = self.forest.takeover_leaf("LEAF-TEST-001", "antigravity", reason="直接接手待办任务")
        self.assertTrue(res["ok"])
        self.assertEqual(res["agent"], "antigravity")
        self.assertTrue(res["new_task_id"].startswith("m-tree-"))
        self.assertIn("antigravity", res["capsule"]["prompt"])
        
        # Verify in snapshot
        snap = self.forest.snapshot()
        leaf = self.forest._leaf(snap, "LEAF-TEST-001")
        self.assertEqual(leaf["owner"], "antigravity")
        self.assertEqual(leaf["location"], "ready")

    def test_takeover_from_stale_or_blocked_agent(self):
        """Takeover from an agent whose task is timed out or blocked."""
        # 1. Allocate to codex first
        tree = self.forest._structure()
        leaf = self.forest._leaf(tree, "LEAF-TEST-001")
        msg = self.forest._allocate(tree, leaf, "codex")
        self.assertEqual(msg.to, "codex")
        
        # 2. Simulate claim by codex
        claim_res = self.forest.claim_leaf("LEAF-TEST-001", "codex")
        ticket = captured_ticket(claim_res)
        self.bus.heartbeat("codex", claim_res, ticket, note="test stale worker started")
        snap = self.forest.snapshot()
        leaf = self.forest._leaf(snap, "LEAF-TEST-001")
        self.assertEqual(leaf["owner"], "codex")
        self.assertEqual(leaf["location"], "processing")
        
        # 3. Simulate a lost worker via an expired heartbeat while preserving the
        # original task deadline. Takeover must not extend the task's time budget.
        heartbeat_file = heartbeat.heartbeat_path(self.bus.root, msg.id, "codex")
        beat = json.loads(heartbeat_file.read_text(encoding="utf-8"))
        beat["at_epoch_s"] = time.time() - 1000
        heartbeat_file.write_text(json.dumps(beat), encoding="utf-8")
        
        # Verify snapshot sees it as blocked
        snap = self.forest.snapshot()
        leaf = self.forest._leaf(snap, "LEAF-TEST-001")
        self.assertEqual(leaf["state"], "claimed")
        self.assertTrue(leaf["heartbeat"]["stale"])
        
        # 4. Now antigravity safely takes over
        res = self.forest.takeover_leaf("LEAF-TEST-001", "antigravity", reason="Codex 超时，由 Antigravity 接管", force=True, expected_task_id=claim_res.id, expected_owner="codex", expected_claim_epoch=claim_res.claim_epoch)
        self.assertTrue(res["ok"])
        self.assertEqual(res["agent"], "antigravity")
        
        # 5. Verify snapshot after takeover
        snap_after = self.forest.snapshot()
        leaf_after = self.forest._leaf(snap_after, "LEAF-TEST-001")
        self.assertEqual(leaf_after["owner"], "antigravity")
        self.assertEqual(leaf_after["state"], "queued")
        self.assertEqual(leaf_after["location"], "ready")
        self.assertTrue(leaf_after["claimable"])
        
        # 6. Verify handoff_history recorded correctly
        self.assertEqual(len(leaf_after["handoff_history"]), 1)
        record = leaf_after["handoff_history"][0]
        self.assertEqual(record["agent"], "codex")
        self.assertEqual(record["task_id"], msg.id)
        self.assertIn("接替", record["reason"])
        self.assertTrue(record["owner_confirmed_inactive"])

    def test_failed_old_claim_release_aborts_takeover_before_new_dispatch(self):
        tree = self.forest._structure()
        leaf = self.forest._leaf(tree, "LEAF-TEST-001")
        msg = self.forest._allocate(tree, leaf, "codex")
        claimed = self.forest.claim_leaf("LEAF-TEST-001", "codex")
        self.assertEqual(claimed.id, msg.id)

        original_release = self.forest.bus.relinquish_claim

        def fail_release(*_args, **_kwargs):
            raise OSError("simulated mailbox release failure")

        self.forest.bus.relinquish_claim = fail_release
        try:
            with self.assertRaisesRegex(task_tree.TaskTreeError, "旧认领释放失败"):
                self.forest.takeover_leaf("LEAF-TEST-001", "antigravity", force=True, expected_task_id=claimed.id, expected_owner="codex", expected_claim_epoch=claimed.claim_epoch)
        finally:
            self.forest.bus.relinquish_claim = original_release

        current = self.forest._leaf(self.forest.snapshot(), "LEAF-TEST-001")
        self.assertEqual(current["task_id"], msg.id)
        self.assertEqual(current["owner"], "codex")
        self.assertEqual(current["location"], "processing")
        self.assertEqual(self.bus.pending("antigravity"), [])

    def test_web_bridge_takeover_action(self):
        """Test WebBridge API actions for takeover_leaf and takeover_capsule."""
        bridge = WebBridge(self.tmp)
        
        # Call takeover_leaf
        payload = {
            "leaf_id": "LEAF-TEST-001",
            "agent": "workbuddy",
            "reason": "由 WorkBuddy 负责此项工作",
            "force": True
        }
        res = bridge.call("takeover_leaf", payload)
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["agent"], "workbuddy")
        
        # Call takeover_capsule
        cap_res = bridge.call("takeover_capsule", {"leaf_id": "LEAF-TEST-001", "agent": "workbuddy"})
        self.assertTrue(cap_res["ok"])
        self.assertIn("workbuddy", cap_res["data"]["capsule"]["prompt"])


if __name__ == "__main__":
    unittest.main()
