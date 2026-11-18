from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.config import DEFAULTS, Config, save
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class DynamicTaskAppendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        data = dict(DEFAULTS)
        data["workspace"] = str(self.workspace)
        data["output_root"] = str(self.workspace)
        data["director"] = "codex"
        data["workers"] = {
            "codex": {"communication_mode": "file", "enabled": True, "executor_enabled": True},
            "antigravity": {"communication_mode": "file", "enabled": True, "executor_enabled": True},
        }
        (self.workspace / ".agentbus").mkdir(parents=True, exist_ok=True)
        self.cfg = Config(self.workspace / ".agentbus" / "config.json", self.workspace, data)
        save(self.cfg)
        Bus(self.workspace / ".agentbus").init()
        tree_data = {
            "schema_version": 1,
            "section_registry": ["0", "1"],
            "roots": [{"id": "project", "parent_id": None, "title": "测试项目", "goal": "测试", "section_no": "0"}],
            "branches": [{"id": "desktop", "parent_id": "project", "title": "桌面 UI 看板树", "section_no": "1"}],
            "leaves": []
        }
        TaskForest(self.cfg).replace_structure(tree_data)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_forest_seed_leaf_automatic_id_and_section_allocation(self):
        forest = TaskForest(self.cfg)
        # 初始化基础树
        snapshot = forest.snapshot()
        self.assertEqual(len(snapshot["roots"]), 1)
        self.assertGreaterEqual(len(snapshot["branches"]), 1)

        # 动态追加一个新任务到 desktop 分支
        added = forest.seed_leaf({
            "title": "性能监控与卡顿报警",
            "goal": "监控前端渲染帧率并在低于 30fps 时告警",
            "parent_id": "desktop",
        })
        leaves = added["leaves"]
        new_leaf = next(l for l in leaves if l["title"] == "性能监控与卡顿报警")
        self.assertIsNotNone(new_leaf)
        self.assertTrue(new_leaf["id"].startswith("TASK-") or "性能" in new_leaf["id"])
        # desktop 是第 1 分支，新叶子应以 '1.' 开头
        self.assertTrue(new_leaf["section_no"].startswith("1."))
        self.assertEqual(new_leaf["state"], "queued")
        self.assertTrue(new_leaf["claimable"])

    def test_forest_seed_leaf_resolves_section_no_branch_and_dependencies(self):
        forest = TaskForest(self.cfg)
        # 1. 在第 1 分支下追加基础节点
        res1 = forest.seed_leaf({
            "id": "STEP-ONE",
            "title": "第一步任务",
            "goal": "第一步",
            "parent_id": "1",  # 传章节号 "1"，应解析到 desktop
        })
        leaf1 = next(l for l in res1["leaves"] if l["id"] == "STEP-ONE")
        self.assertEqual(leaf1["parent_id"], "desktop")
        sec1 = leaf1["section_no"]

        # 2. 在第 2 步任务中，通过章节号依赖第一步
        res2 = forest.seed_leaf({
            "id": "STEP-TWO",
            "title": "第二步任务",
            "goal": "依赖第一步",
            "parent_id": "desktop",
            "dependencies": [sec1],  # 传章节号依赖
        })
        leaf2 = next(l for l in res2["leaves"] if l["id"] == "STEP-TWO")
        # 依赖项应已被解析为第一步的真实 ID
        self.assertIn("STEP-ONE", leaf2["dependencies"])
        self.assertIn(leaf2["state"], ("blocked", "queued"))

    def test_web_bridge_append_leaf_action(self):
        bridge = WebBridge(self.workspace)
        result = bridge.call("append_leaf", {
            "title": "暗黑模式专属配色方案",
            "goal": "适配 OLED 纯黑与对比度优化",
            "parent_id": "desktop",
            "dependencies": [],
        })
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertIn("leaf", data)
        self.assertIn("forest", data)
        self.assertEqual(data["leaf"]["title"], "暗黑模式专属配色方案")
        self.assertTrue(data["leaf"]["section_no"].startswith("1."))

    def test_cli_tree_append_with_arguments(self):
        from agentbus import cli
        import argparse

        args = argparse.Namespace(
            workspace=str(self.workspace),
            action="append",
            title="多端剪贴板同步",
            goal="在不同窗口间同步任务指派指令",
            branch="desktop",
            dependencies=None,
            file=None,
            id=None,
            agent=None,
            leaf=None,
            note="",
            claim_epoch=None,
            claim_id=None,
            template=None,
            apply=False,
            expected_sha256=None,
            successor=None,
            owner_confirmed_inactive=False,
        )
        code = cli.cmd_tree(args)
        self.assertEqual(code, 0)

        forest = TaskForest(self.cfg)
        snap = forest.snapshot()
        leaf = next(l for l in snap["leaves"] if l["title"] == "多端剪贴板同步")
        self.assertIsNotNone(leaf)


if __name__ == "__main__":
    unittest.main()
