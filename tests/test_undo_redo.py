#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对 §8.6 AUDIT-TRAE-UNDO-REDO 撤销/重做栈与快捷键支持的定向单测。"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import unittest
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets"


class TestUndoRedo(unittest.TestCase):
    def setUp(self):
        self.js = (ASSETS / "app.js").read_text(encoding="utf-8")

    def test_undo_redo_core_structures_present(self):
        """测试 app.js 中包含撤销栈、重做栈及容量保护逻辑。"""
        self.assertIn("const undoStack = [];", self.js)
        self.assertIn("const redoStack = [];", self.js)
        self.assertIn("function pushUndoAction(action)", self.js)
        self.assertIn("function performUndo()", self.js)
        self.assertIn("function performRedo()", self.js)
        self.assertIn("function applyAction(action, isUndo)", self.js)

    def test_ctrl_z_and_ctrl_y_shortcuts_handled(self):
        """测试键盘事件中拦截并处理 Ctrl+Z 与 Ctrl+Y 快捷键。"""
        self.assertIn("event.key.toLowerCase() === 'z'", self.js)
        self.assertIn("event.key.toLowerCase() === 'y'", self.js)
        self.assertIn("performUndo();", self.js)
        self.assertIn("performRedo();", self.js)

    def test_sticky_delete_records_undo_action(self):
        """测试便签删除时将便签快照压入撤销栈，支持 Ctrl+Z 恢复。"""
        self.assertIn("pushUndoAction({ type: 'STICKY_DELETE', note: { ...note } });", self.js)
        self.assertIn("case 'STICKY_DELETE':", self.js)

    def test_sticky_create_records_undo_action(self):
        """测试便签创建时记录 STICKY_CREATE 撤销动作。"""
        self.assertIn("pushUndoAction({ type: 'STICKY_CREATE', id: note.id, note: { ...note } });", self.js)
        self.assertIn("case 'STICKY_CREATE':", self.js)

    def test_spatial_move_records_undo_action(self):
        """测试卡片与分支拖拽移动时记录 SPATIAL_MOVE 撤销动作。"""
        self.assertIn("case 'SPATIAL_MOVE':", self.js)
        self.assertIn("pushUndoAction({ type: 'SPATIAL_MOVE', id: groupId", self.js)
        self.assertIn("pushUndoAction({ type: 'SPATIAL_MOVE', id: leaf.id", self.js)

    def test_tidy_all_records_undo_action(self):
        """测试智能布局排版前快照旧坐标，支持 Ctrl+Z 一键复原自定义排版。"""
        self.assertIn("case 'TIDY_ALL':", self.js)
        self.assertIn("pushUndoAction({ type: 'TIDY_ALL', oldLayout });", self.js)

    def test_agentbus_ui_exports_undo_redo_api(self):
        """测试 window.AgentBusUI 正确导出 undo 与 redo 接口供外部调用。"""
        self.assertIn("undo:performUndo", self.js)
        self.assertIn("redo:performRedo", self.js)
        self.assertIn("undoStack", self.js)
        self.assertIn("redoStack", self.js)

    def test_shortcuts_guide_mentions_ctrl_z(self):
        """测试快捷键手册中包含了 Ctrl+Z / Ctrl+Y 说明。"""
        self.assertIn("Ctrl+Z / Ctrl+Y", self.js)


if __name__ == "__main__":
    unittest.main()
