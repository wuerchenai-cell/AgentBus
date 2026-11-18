# -*- coding: utf-8 -*-
"""§13.8 CX13Q-RESTORE-APPROVAL: 备份还原校验摘要与真实预览确认回归测试。

覆盖要求：
1. 未知 schema（如 schema 999）或格式非法在还原前被拦截拒绝；
2. confirm 严格要求为 bool True（如 confirm='false' 或省略必须被判定为未确认，仅返回预览）；
3. 必须提供合法 preview_id，未提供或 preview_id 不匹配时拒绝执行还原；
4. 合法预览票据且 confirm=True 时成功执行还原。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, Mock
from agentbus import gui_state
from pathlib import Path

from agentbus.backup import (
    BACKUP_FORMAT,
    BACKUP_SCHEMA_VERSION,
    apply_backup_restore,
    preview_backup_restore,
)
from agentbus.config import load as load_config
from agentbus.web_bridge import WebBridge


class BackupRestoreConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup_approval_"))
        state_patch = patch.object(gui_state, 'state_path', return_value=self.tmp / 'ui.json')
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.bus_root = self.tmp / ".agentbus"
        self.bus_root.mkdir(parents=True, exist_ok=True)
        # 初始化基础配置与树
        (self.bus_root / "config.json").write_text(json.dumps({
            "schema_version": 1,
            "workspace": str(self.tmp),
            "output_root": str(self.tmp),
            "director": "codex",
            "workers": {"codex": {"enabled": True}}
        }), encoding="utf-8")
        (self.bus_root / "task-tree.json").write_text(json.dumps({
            "schema_version": 1,
            "roots": [],
            "branches": [],
            "leaves": []
        }), encoding="utf-8")
        self.cfg = load_config(self.tmp)
        self.bridge = WebBridge(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sample_valid_bundle(self):
        return {
            "format": BACKUP_FORMAT,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "task_tree": {
                "schema_version": 1,
                "format": "agentbus-task-tree",
                "title": "Sample Tree",
                "description": "Sample description",
                "roots": [{"id": "r1", "title": "Root 1", "goal": "Goal 1"}],
                "branches": [{"id": "b1", "parent_id": "r1", "title": "Branch 1"}],
                "leaves": [{"id": "l1", "parent_id": "b1", "title": "Leaf 1", "goal": "Goal 1"}]
            },
            "team_settings": {"director": "codex", "workers": {"codex": {}}},
            "ui_preferences": {}
        }

    def test_unknown_schema_or_format_rejected(self):
        """1. 未知 schema 或非法格式被拒绝。"""
        bundle = self._sample_valid_bundle()
        bundle["schema_version"] = 999
        with self.assertRaises(ValueError) as ctx:
            preview_backup_restore(self.tmp, bundle)
        self.assertIn("schema", str(ctx.exception).lower())

        bad_format_bundle = self._sample_valid_bundle()
        bad_format_bundle["format"] = "unknown-format"
        with self.assertRaises(ValueError) as ctx2:
            preview_backup_restore(self.tmp, bad_format_bundle)
        self.assertIn("格式不匹配", str(ctx2.exception))

    def test_web_bridge_confirm_false_string_returns_preview_only(self):
        """2. confirm='false' 或非严格 True 只能返回预览，不执行还原。"""
        bundle = self._sample_valid_bundle()
        res = self.bridge.call("restore_backup", {
            "bundle": bundle,
            "confirm": "false",  # 字符串假
        })
        self.assertTrue(res.get("ok"))
        data = res.get("data", {})
        self.assertIn("preview", data)
        self.assertFalse(data.get("restored", False), "字符串 'false' 绝不能触发还原")

    def test_missing_or_mismatched_preview_id_rejected(self):
        """3. apply_backup_restore 在缺少 preview_id 或 preview_id 不匹配时拒绝执行。"""
        bundle = self._sample_valid_bundle()
        # 缺少 preview_id
        with self.assertRaises(ValueError) as ctx:
            apply_backup_restore(self.tmp, bundle, preview_id=None)
        self.assertIn("preview_id", str(ctx.exception).lower())

        # 错误的 preview_id
        with self.assertRaises(ValueError) as ctx2:
            apply_backup_restore(self.tmp, bundle, preview_id="bogus_preview_id")
        self.assertIn("preview_id", str(ctx2.exception).lower())

    def test_valid_preview_id_and_confirm_succeeds(self):
        """4. 经过合法预览生成的 preview_id 且 confirm=True 成功还原。"""
        bundle = self._sample_valid_bundle()
        preview = preview_backup_restore(self.tmp, bundle)
        valid_pid = preview["preview_id"]
        self.assertTrue(valid_pid)

        # 调桥接执行还原
        res = self.bridge.call("restore_backup", {
            "bundle": bundle,
            "confirm": True,
            "preview_id": valid_pid
        })
        self.assertTrue(res.get("ok"))
        data = res.get("data", {})
        self.assertTrue(data.get("restored"))
        self.assertEqual(data["result"]["status"], "restored")


if __name__ == "__main__":
    unittest.main()
