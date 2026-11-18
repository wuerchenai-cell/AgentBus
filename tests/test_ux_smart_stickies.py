"""Unit tests for Section 6.5 UX-SMART-STICKIES.
Verifies sticky-to-task conversion, sticky card snapping/anchoring,
and interactive markdown checklist handling.
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


class TestUXSmartStickies(unittest.TestCase):

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
                    "id": "leaf-anchor-target",
                    "parent_id": "desktop",
                    "section_no": "6.5.1",
                    "title": "锚定卡片",
                    "goal": "测试便签吸附跟随平移",
                    "dependencies": []
                }
            ]
        }
        (agentbus_dir / "task-tree.json").write_text(json.dumps(initial_tree, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_frontend_smart_stickies_elements(self):
        assets_dir = Path(__file__).resolve().parent.parent / "agentbus" / "web_assets"
        js = (assets_dir / "app.js").read_text(encoding="utf-8")
        css = (assets_dir / "app.css").read_text(encoding="utf-8")

        # 1. 验证便签一键转正式任务卡片
        self.assertIn("convertStickyToTask", js)
        self.assertIn("sticky-convert-btn", js)

        # 2. 验证便签吸附与跟随平移机制
        self.assertIn("attachedLeafId", js)
        self.assertIn("sticky-attach-btn", js)
        self.assertIn("is-attached", js)
        self.assertIn(".sticky-note.is-attached", css)
        self.assertIn("sticky-attached-badge", js)
        self.assertIn(".sticky-attached-badge", css)

        # 3. 验证交互式 Markdown Checklist 原生打勾持久化
        self.assertIn("sticky-checklist", js)
        self.assertIn(".sticky-checklist", css)
        self.assertIn("sticky-checklist-item", js)
        self.assertIn(".sticky-checklist-item", css)
        self.assertIn("updateChecklist", js)

    def test_bridge_append_leaf_from_sticky_conversion(self):
        bridge = WebBridge(self.workspace)

        # 模拟由便签转换生成的任务
        sticky_text = "实现便签锚定吸附测试\n第一阶段：添加UI交互按钮\n第二阶段：支持拖拽协同移动"
        lines = sticky_text.split("\n")
        title = lines[0]
        goal = "\n".join(lines[1:])

        res = bridge.call("append_leaf", {
            "parent_id": "desktop",
            "title": title,
            "goal": goal,
            "dependencies": []
        })
        self.assertTrue(res["ok"], res)

        cfg = load(self.workspace)
        forest = TaskForest(cfg)
        snap = forest.snapshot()
        converted = next((l for l in snap["leaves"] if l["title"] == title), None)
        self.assertIsNotNone(converted)
        self.assertEqual(converted["goal"], goal)
        self.assertEqual(converted["parent_id"], "desktop")


if __name__ == "__main__":
    unittest.main()
