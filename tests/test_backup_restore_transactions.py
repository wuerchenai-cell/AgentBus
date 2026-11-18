# -*- coding: utf-8 -*-
"""§13.9 CX13Q-RESTORE-TRANSACTION: 还原租约与偏好原子提交及可恢复测试。

覆盖要求：
1. 锁超时保护：当 task-forest 锁已被外部进程占用且超时时，抛出 TimeoutError 并拒绝还原；
2. save_notes 返回 False 时中断失败并回滚，严禁虚报 restored=True；
3. save_branch_folds / save_priorities 返回 False 时中断失败并回滚；
4. 发生回滚后目标任务树保持原始干净状态，不留残留节点；
5. 正常恢复时受锁保护原子提交，返回 status='restored' 与 restored=True。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, Mock
from agentbus import gui_state
from pathlib import Path
from unittest.mock import patch

from agentbus.backup import (
    BACKUP_FORMAT,
    BACKUP_SCHEMA_VERSION,
    apply_backup_restore,
    preview_backup_restore,
)
from agentbus.config import load as load_config
from agentbus.task_tree import TaskForest


class BackupRestoreTransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup_tx_"))
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

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sample_valid_bundle(self):
        return {
            "format": BACKUP_FORMAT,
            "schema_version": BACKUP_SCHEMA_VERSION,
            "task_tree": {
                "schema_version": 1,
                "format": "agentbus-task-tree",
                "title": "TX Tree",
                "description": "TX description",
                "roots": [{"id": "r1", "title": "Root 1", "goal": "Goal 1"}],
                "branches": [{"id": "b1", "parent_id": "r1", "title": "Branch 1"}],
                "leaves": [{"id": "l1", "parent_id": "b1", "title": "Leaf 1", "goal": "Goal 1"}]
            },
            "team_settings": {"director": "codex", "workers": {"codex": {}}},
            "ui_preferences": {
                "sticky_notes": [{"id": "note-1", "text": "Note 1", "color": "yellow", "x": 10, "y": 10, "w": 200, "h": 150}],
                "collapsed_branches": ["b1"],
                "card_priorities": {"l1": "P1"}
            }
        }

    def test_save_notes_failure_aborts_and_rollbacks(self):
        """1. 当 save_notes 返回 False 时中断抛出 ValueError 并回滚任务树。"""
        bundle = self._sample_valid_bundle()
        preview = preview_backup_restore(self.tmp, bundle)
        pid = preview["preview_id"]

        with patch("agentbus.backup.save_notes", return_value=False):
            with self.assertRaises(ValueError) as ctx:
                apply_backup_restore(self.tmp, bundle, preview_id=pid)
            self.assertIn("便签", str(ctx.exception))

        # 验证回滚：任务树中不应留存导入的节点
        forest = TaskForest(load_config(self.tmp))
        snap = forest.snapshot()
        self.assertEqual(len(snap.get("leaves", [])), 0, "偏好保存失败后必须完整回滚任务树")

    def test_save_folds_failure_aborts_and_rollbacks(self):
        """2. 当 save_branch_folds 返回 False 时中断并回滚。"""
        bundle = self._sample_valid_bundle()
        preview = preview_backup_restore(self.tmp, bundle)
        pid = preview["preview_id"]

        with patch("agentbus.backup.save_branch_folds", return_value=False):
            with self.assertRaises(ValueError) as ctx:
                apply_backup_restore(self.tmp, bundle, preview_id=pid)
            self.assertIn("折叠", str(ctx.exception))

        forest = TaskForest(load_config(self.tmp))
        snap = forest.snapshot()
        self.assertEqual(len(snap.get("leaves", [])), 0)

    def test_save_priorities_failure_aborts_and_rollbacks(self):
        """3. 当 save_priorities 返回 False 时中断并回滚。"""
        bundle = self._sample_valid_bundle()
        preview = preview_backup_restore(self.tmp, bundle)
        pid = preview["preview_id"]

        with patch("agentbus.backup.save_priorities", return_value=False):
            with self.assertRaises(ValueError) as ctx:
                apply_backup_restore(self.tmp, bundle, preview_id=pid)
            self.assertIn("优先级", str(ctx.exception))

        forest = TaskForest(load_config(self.tmp))
        snap = forest.snapshot()
        self.assertEqual(len(snap.get("leaves", [])), 0)

    def test_atomic_lock_contention_blocks_restore(self):
        """4. 外部竞争持有 task-forest 锁超时时拒绝恢复执行。"""
        bundle = self._sample_valid_bundle()
        preview = preview_backup_restore(self.tmp, bundle)
        pid = preview["preview_id"]

        with patch("agentbus.backup.task_lock", side_effect=TimeoutError("task operation is busy")):
            with self.assertRaises(TimeoutError):
                apply_backup_restore(self.tmp, bundle, preview_id=pid)

    def test_successful_restore_commits_atomically(self):
        """5. 正常流程原子提交成功，返回完整数据。"""
        bundle = self._sample_valid_bundle()
        preview = preview_backup_restore(self.tmp, bundle)
        pid = preview["preview_id"]

        res = apply_backup_restore(self.tmp, bundle, preview_id=pid)
        self.assertEqual(res["status"], "restored")
        self.assertTrue(res["restored"])
        self.assertEqual(res["notes_count"], 1)


if __name__ == "__main__":
    unittest.main()
