#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_desktop_wakeup_results.py: 桌面唤醒反馈实际结果测试。

来源：§13.6 CX13Q-WAKEUP-RESULT（P1）。
验收标准：
1. 通知/提示音/复制返回 requested/completed/error 和分项状态；
2. auto_copy=False 时不声称复制；
3. 发生通道失败时整体 success 真实反映状态，不得虚假 success=True；
4. 剪贴板 helper 超时被安全回收。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 导入待测集成模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
import sys
sys.path.insert(0, str(PROJECT_DIR))

from agentbus.config import load as load_config  # noqa: E402
from agentbus.desktop_wakeup import DesktopWakeupBridge  # noqa: E402


class TestDesktopWakeupResults(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        bus_dir = self.root / ".agentbus"
        bus_dir.mkdir(parents=True, exist_ok=True)
        (bus_dir / "config.json").write_text(json.dumps({
            "schema_version": 1,
            "workspace": str(self.root),
            "output_root": str(self.root),
            "director": "codex",
            "workers": {
                "codex": {"enabled": True, "can_spawn": True, "executor_enabled": True}
            }
        }), encoding="utf-8")
        (bus_dir / "task-tree.json").write_text(json.dumps({
            "schema_version": 1,
            "roots": [],
            "branches": [],
            "leaves": [
                {
                    "id": "leaf-wakeup-1",
                    "title": "测试唤醒叶子",
                    "section_no": "12.3",
                    "state": "claimed",
                    "task_id": "m-task-wake-01",
                    "claimable": False,
                    "owner": "codex"
                }
            ]
        }), encoding="utf-8")
        self.cfg = load_config(self.root)
        self.bridge = DesktopWakeupBridge(self.cfg)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_auto_copy_false_reports_not_copied(self):
        """测试在 auto_copy=False 时，返回结果准确声明未复制。"""
        leaf = {
            "id": "leaf-wakeup-1",
            "title": "测试唤醒叶子",
            "section_no": "12.3",
            "state": "claimed",
            "task_id": "m-task-wake-01",
        }
        with patch.object(self.bridge, "show_toast", return_value=(True, "")):
            with patch.object(self.bridge, "play_chime", return_value=(True, "")):
                res = self.bridge.notify_task_wakeup(leaf, "codex", auto_copy=False, play_sound=True)
                self.assertFalse(res["clipboard"]["requested"])
                self.assertFalse(res["copied_to_clipboard"])
                self.assertTrue(res["sound"]["completed"])
                self.assertTrue(res["toast"]["completed"])

    def test_channel_failure_reflects_in_success_flag(self):
        """测试当某个请求的通道失败时，结果准确记录错误且 overall success 为 False。"""
        leaf = {
            "id": "leaf-wakeup-1",
            "title": "测试唤醒叶子",
            "section_no": "12.3",
            "state": "claimed",
            "task_id": "m-task-wake-01",
        }
        with patch.object(self.bridge, "show_toast", return_value=(False, "TOAST_FAILED")):
            with patch.object(self.bridge, "play_chime", return_value=(True, "")):
                with patch.object(self.bridge, "copy_to_clipboard", return_value=(True, "")):
                    res = self.bridge.notify_task_wakeup(leaf, "codex", auto_copy=True, play_sound=True)
                    self.assertFalse(res["success"], "任一请求通道失败时 overall success 必须为 False")
                    self.assertFalse(res["toast"]["completed"])
                    self.assertEqual(res["toast"]["error"], "TOAST_FAILED")


if __name__ == "__main__":
    unittest.main()
