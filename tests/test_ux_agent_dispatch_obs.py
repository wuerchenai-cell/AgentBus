"""Unit tests for Section 6.4 UX-AGENT-DISPATCH-OBS.
Verifies one-click agent wake-up prompt generation, stale task recovery,
collapsible console mirror output, and execution duration statistics.
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


class TestUXAgentDispatchObs(unittest.TestCase):

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
                    "id": "leaf-dispatch-test",
                    "parent_id": "desktop",
                    "section_no": "6.4.1",
                    "title": "测试唤醒与控制台镜像",
                    "goal": "测试专属提示词与事件输出",
                    "dependencies": []
                }
            ]
        }
        (agentbus_dir / "task-tree.json").write_text(json.dumps(initial_tree, ensure_ascii=False), encoding="utf-8")

        # 写入一条模拟事件
        events_file = agentbus_dir / "events.jsonl"
        event_entry = {
            "ts": "2026-10-12T18:30:00+08:00",
            "event": "claim",
            "agent": "antigravity",
            "task_id": "m-tree-mock-123",
            "leaf_id": "leaf-dispatch-test"
        }
        events_file.write_text(json.dumps(event_entry, ensure_ascii=False) + "\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_web_bridge_console_mirror(self):
        bridge = WebBridge(self.workspace)
        res = bridge.call("console_mirror", {"limit": 10})
        self.assertTrue(res["ok"], res)
        data = res["data"]
        self.assertIn("lines", data)
        self.assertGreaterEqual(len(data["lines"]), 1)
        first_line = data["lines"][0]
        self.assertIn("text", first_line)
        self.assertIn("CLAIM", first_line["text"])

    def test_web_bridge_recover_task_validation(self):
        bridge = WebBridge(self.workspace)

        # 缺少参数直接报错
        res_empty = bridge.call("recover_task", {})
        self.assertFalse(res_empty["ok"])

        # 恢复不存在的任务时干净失败
        res_nonexistent = bridge.call("recover_task", {
            "task_id": "m-tree-nonexistent",
            "agent": "antigravity",
            "owner_confirmed_inactive": True
        })
        self.assertFalse(res_nonexistent["ok"])

    def test_frontend_dispatch_obs_elements_present(self):
        assets_dir = Path(__file__).resolve().parent.parent / "agentbus" / "web_assets"
        js = (assets_dir / "app.js").read_text(encoding="utf-8")
        css = (assets_dir / "app.css").read_text(encoding="utf-8")

        # 1. 验证复制专属唤醒指令
        self.assertIn("复制专属唤醒指令", js)
        self.assertIn("taskHandoffPrompt", js)

        # 2. 验证卡死任务重置打回待办
        self.assertIn("重置打回待办", js)
        self.assertIn("recover_task", js)

        # 3. 验证终端控制台镜像
        self.assertIn("console-mirror-box", js)
        self.assertIn("console_mirror", js)
        self.assertIn(".console-mirror-box", css)
        self.assertIn(".console-log-line", css)
        self.assertIn(".console-duration-badge", css)


if __name__ == "__main__":
    unittest.main()
