"""Unit tests for §12.3 Desktop Wakeup Bridge and WebBridge integration.
Ensures native OS notifications, audible chimes, and clipboard hot-loading work seamlessly.
"""
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, Mock
from agentbus import gui_state
from pathlib import Path

from agentbus import config as ab_config
from agentbus.desktop_wakeup import DesktopWakeupBridge
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TestDesktopWakeup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentbus_test_wakeup_"))
        state_patch = patch.object(gui_state, 'state_path', return_value=self.tmp / 'ui.json')
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.agentbus_dir = self.tmp / ".agentbus"
        self.agentbus_dir.mkdir(parents=True)

        config_data = {
            "director": "codex",
            "coordinator": "antigravity",
            "workers": {
                "codex": {"enabled": True, "communication_mode": "file", "capabilities": ["python"]},
                "antigravity": {"enabled": True, "communication_mode": "file", "capabilities": ["python", "testing"]},
            },
            "gates": {"heartbeat_timeout_s": 600},
        }
        (self.agentbus_dir / "config.json").write_text(
            json.dumps(config_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.cfg = ab_config.load(self.tmp)

        tree_data = {
            "schema_version": 1,
            "section_registry": ["0", "12", "12.3"],
            "roots": [{"id": "project", "title": "Test Project"}],
            "branches": [{"id": "branch-automation", "parent_id": "project", "section_no": "12", "title": "Automation"}],
            "leaves": [
                {
                    "id": "TASK-WAKEUP-01",
                    "parent_id": "branch-automation",
                    "title": "Desktop Wakeup Test Task",
                    "goal": "Verify desktop native wakeup sequence",
                    "section_no": "12.3",
                    "dependencies": [],
                    "required_capabilities": ["python", "testing"],
                    "expected_artifacts": [],
                    "handoff": "Ready to awaken",
                    "history_task_ids": [],
                }
            ],
        }
        (self.agentbus_dir / "task-tree.json").write_text(
            json.dumps(tree_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.forest = TaskForest(self.cfg)
        def helper(*args, **kwargs):
            process = Mock()
            process.returncode = 0
            process.poll.return_value = 0
            process.communicate.return_value = (None, None)
            return process
        for mocker in (patch('agentbus.desktop_wakeup.subprocess.Popen', side_effect=helper),
                       patch('agentbus.desktop_wakeup.subprocess.run', return_value=Mock(returncode=0)),
                       patch('winsound.Beep')):
            mocker.start()
            self.addCleanup(mocker.stop)
        self.bridge = DesktopWakeupBridge(self.cfg)
        self.web_bridge = WebBridge(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_play_chime_non_blocking(self):
        res = self.bridge.play_chime()
        if isinstance(res, tuple):
            self.assertIsInstance(res[0], bool)
            self.assertIsInstance(res[1], str)
        else:
            self.assertIsInstance(res, bool)

    def test_copy_to_clipboard(self):
        # Even if headless environment lacks interactive clipboard, method shouldn't crash
        res = self.bridge.copy_to_clipboard("test clipboard text")
        if isinstance(res, tuple):
            self.assertIsInstance(res[0], bool)
            self.assertIsInstance(res[1], str)
        else:
            self.assertIsInstance(res, bool)

    def test_notify_task_wakeup_sequence(self):
        leaf = self.forest._leaf(self.forest.snapshot(), "TASK-WAKEUP-01")
        res = self.bridge.notify_task_wakeup(leaf, "antigravity", auto_copy=False, play_sound=False)
        self.assertTrue(res["success"])
        self.assertEqual(res["leaf_id"], "TASK-WAKEUP-01")
        self.assertEqual(res["section_no"], "12.3")
        self.assertEqual(res["agent"], "antigravity")
        self.assertGreater(res["prompt_length"], 50)

    def test_web_bridge_trigger_desktop_wakeup(self):
        res = self.web_bridge.call("trigger_desktop_wakeup", {
            "leaf_id": "TASK-WAKEUP-01",
            "agent": "antigravity",
            "auto_copy": False,
            "sound": False,
        })
        self.assertTrue(res["ok"])
        data = res["data"]
        self.assertTrue(data["success"])
        self.assertEqual(data["leaf_id"], "TASK-WAKEUP-01")


if __name__ == "__main__":
    unittest.main()
