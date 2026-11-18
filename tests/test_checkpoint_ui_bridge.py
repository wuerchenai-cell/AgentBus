"""Regression test for §7.26 CHECKPOINT-UI-BRIDGE.
Verify drawer checkpoint contract alignment (note + progress) and persistence.
"""
import json
import unittest
from pathlib import Path
import tempfile
import shutil

from agentbus import config as ab_config
from agentbus import task_tree
from agentbus.claim_fencing import captured_ticket
from agentbus.bus import Bus
from agentbus.web_bridge import WebBridge


class TestCheckpointUiBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbus_test_cp_bridge_"))
        self.agentbus_dir = self.tmp / ".agentbus"
        self.agentbus_dir.mkdir(parents=True)
        
        config_data = {
            "director": "antigravity",
            "workers": {"antigravity": {"enabled": True, "communication_mode": "file"}},
            "gates": {"task_timeout_s": 1800, "total_budget_s": 7200}
        }
        (self.agentbus_dir / "config.json").write_text(json.dumps(config_data), encoding="utf-8")
        
        self.bus = Bus(self.agentbus_dir)
        self.bus.init()
        self.cfg = ab_config.load(self.tmp)
        self.forest = task_tree.TaskForest(self.cfg)
        
        init_tree = {
            "schema_version": 1,
            "roots": [{"id": "root", "parent_id": None, "title": "Root Task"}],
            "branches": [{"id": "branch", "parent_id": "root", "title": "Branch"}],
            "leaves": [
                {
                    "id": "LEAF-CP-001",
                    "parent_id": "branch",
                    "title": "Checkpoint Target Leaf",
                    "section_no": "7.26",
                    "dependencies": [],
                    "expected_artifacts": []
                }
            ]
        }
        (self.agentbus_dir / "task-tree.json").write_text(json.dumps(init_tree), encoding="utf-8")
        
        # Allocate and claim
        tree = self.forest._structure()
        leaf = self.forest._leaf(tree, "LEAF-CP-001")
        self.forest._allocate(tree, leaf, "antigravity")
        claim = self.forest.claim_leaf("LEAF-CP-001", "antigravity")
        self.ticket = captured_ticket(claim).to_dict()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_checkpoint_payload_with_progress(self):
        """WebBridge accepts note and progress from drawer payload and persists it."""
        bridge = WebBridge(self.tmp)
        payload = {
            "leaf_id": "LEAF-CP-001",
            "agent": "antigravity",
            "note": "阶段性断点记录：核心协议已调试完毕",
            "progress": 65,
            "ticket": self.ticket,
        }
        res = bridge.call("checkpoint_leaf", payload)
        self.assertTrue(res["ok"])
        
        # Verify from snapshot
        snap = self.forest.snapshot()
        leaf = self.forest._leaf(snap, "LEAF-CP-001")
        self.assertEqual(leaf["handoff"], "阶段性断点记录：核心协议已调试完毕")
        self.assertEqual(leaf["progress_percent"], 65)

    def test_checkpoint_invalid_progress(self):
        """Invalid progress numbers are safely rejected."""
        bridge = WebBridge(self.tmp)
        payload = {
            "leaf_id": "LEAF-CP-001",
            "agent": "antigravity",
            "note": "测试非法进度",
            "progress": 150,
            "ticket": self.ticket,
        }
        res = bridge.call("checkpoint_leaf", payload)
        self.assertFalse(res["ok"])
        self.assertTrue("0 to 100" in res["error"] or "0 到 100" in res["error"])


if __name__ == "__main__":
    unittest.main()
