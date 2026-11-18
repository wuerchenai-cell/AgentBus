"""Unit tests for Section 6.2 UX-CANVAS-HANDLING.
Verifies Ctrl+Z/Ctrl+Y undo-redo stack, marquee multi-selection & batch drag,
in-place card renaming on double-click, Ctrl+C/V card cloning,
and wire disconnect confirmation.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import json
import shutil
import tempfile
import unittest

from agentbus.config import load
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TestUXCanvasHandling(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.workspace = Path(self.test_dir)
        agentbus_dir = self.workspace / ".agentbus"
        agentbus_dir.mkdir(parents=True)

        config_data = {
            "version": "1.0.0",
            "director": "antigravity",
            "workers": {
                "antigravity": {
                    "role": "fullstack",
                    "capabilities": ["code", "test"],
                    "enabled": True,
                    "communication_mode": "file"
                },
                "codex": {
                    "role": "reviewer",
                    "capabilities": ["review"],
                    "enabled": True,
                    "communication_mode": "file"
                }
            },
            "output_root": str(self.workspace)
        }
        (agentbus_dir / "config.json").write_text(json.dumps(config_data, ensure_ascii=False), encoding="utf-8")

        initial_tree = {
            "version": "1.0.0",
            "roots": [{"id": "root-1", "title": "项目根"}],
            "branches": [{"id": "desktop", "parent_id": "root-1", "title": "桌面交互"}],
            "leaves": [
                {
                    "id": "leaf-1",
                    "parent_id": "desktop",
                    "section_no": "6.2.1",
                    "title": "原始任务卡片",
                    "goal": "测试就地重命名与克隆",
                    "dependencies": []
                },
                {
                    "id": "leaf-2",
                    "parent_id": "desktop",
                    "section_no": "6.2.2",
                    "title": "后序依赖卡片",
                    "goal": "测试依赖连线断开确认",
                    "dependencies": ["leaf-1"]
                }
            ]
        }
        (agentbus_dir / "task-tree.json").write_text(json.dumps(initial_tree, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_frontend_canvas_handling_elements_and_hotkeys(self):
        assets_dir = Path(__file__).resolve().parent.parent / "agentbus" / "web_assets"
        js = (assets_dir / "app.js").read_text(encoding="utf-8")
        css = (assets_dir / "app.css").read_text(encoding="utf-8")

        # 1. 验证框选与批量平移机制
        self.assertIn("startMarqueeSelection", js)
        self.assertIn("batchSelectedCardIds", js)
        self.assertIn("clearBatchSelection", js)
        self.assertIn("is-batch-selected", js)
        self.assertIn(".is-batch-selected", css)

        # 2. 验证双击卡片就地重命名
        self.assertIn("startInPlaceCardRename", js)
        self.assertIn("inplace-rename-input", js)
        self.assertIn(".inplace-rename-input", css)
        self.assertIn("dblclick", js)

        # 3. 验证 Ctrl+C / Ctrl+V 卡片克隆
        self.assertIn("executeCloneCard", js)
        self.assertIn("clipboardCard", js)
        self.assertIn("event.key.toLowerCase() === 'c'", js)
        self.assertIn("event.key.toLowerCase() === 'v'", js)

        # 4. 验证依赖连线断开确认防误触
        self.assertIn("showDisconnectPopover", js)
        self.assertIn("wire-disconnect-popover", js)
        self.assertIn("断开依赖", js)

        # 5. 验证撤销重做支持
        self.assertIn("BATCH_SPATIAL_MOVE", js)
        self.assertIn("CARD_RENAME", js)
        from test_undo_real_semantics import TestUndoRealSemantics
        TestUndoRealSemantics('test_clone_is_history_barrier_and_never_promises_fake_undo').test_clone_is_history_barrier_and_never_promises_fake_undo()

    def test_bridge_update_leaf_topology_for_rename_and_disconnect(self):
        bridge = WebBridge(self.workspace)

        # 1. 重命名叶子节点
        res_rename = bridge.call("update_leaf_topology", {
            "leaf_id": "leaf-1",
            "title": "重命名后的任务卡片"
        })
        self.assertTrue(res_rename["ok"], res_rename)

        cfg = load(self.workspace)
        forest = TaskForest(cfg)
        snap = forest.snapshot()
        leaf1 = next(l for l in snap["leaves"] if l["id"] == "leaf-1")
        self.assertEqual(leaf1["title"], "重命名后的任务卡片")

        # 2. 断开依赖连线
        res_disconnect = bridge.call("update_leaf_topology", {
            "leaf_id": "leaf-2",
            "dependencies": []
        })
        self.assertTrue(res_disconnect["ok"], res_disconnect)

        forest = TaskForest(cfg)
        snap = forest.snapshot()
        leaf2 = next(l for l in snap["leaves"] if l["id"] == "leaf-2")
        self.assertEqual(leaf2["dependencies"], [])

    def test_cycle_dependency_gives_actionable_chinese_and_preserves_tree(self):
        tree_path = self.workspace / ".agentbus" / "task-tree.json"
        before = tree_path.read_bytes()
        result = WebBridge(self.workspace).call("update_leaf_topology", {
            "leaf_id": "leaf-1", "dependencies": ["leaf-2"]
        })
        self.assertFalse(result["ok"])
        self.assertIn("循环依赖", result["error"])
        self.assertIn("无法建立", result["error"])
        self.assertEqual(tree_path.read_bytes(), before)

    def test_bridge_append_leaf_for_card_cloning(self):
        bridge = WebBridge(self.workspace)

        # 测试克隆卡片调用 append_leaf
        res_clone = bridge.call("append_leaf", {
            "parent_id": "desktop",
            "title": "克隆 - 原始任务卡片",
            "goal": "测试克隆产物",
            "dependencies": ["leaf-1"]
        })
        self.assertTrue(res_clone["ok"], res_clone)

        cfg = load(self.workspace)
        forest = TaskForest(cfg)
        snap = forest.snapshot()
        cloned = [l for l in snap["leaves"] if l["title"] == "克隆 - 原始任务卡片"]
        self.assertEqual(len(cloned), 1)
        self.assertEqual(cloned[0]["dependencies"], ["leaf-1"])


if __name__ == "__main__":
    unittest.main()
