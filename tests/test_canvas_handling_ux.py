"""
Tests for UX-CANVAS-HANDLING (P1, §6.2)
Source: Antigravity (2026-10-12)

Verifies:
1. Undo / Redo Stack (Ctrl+Z / Ctrl+Y):
   - applyAction supports reversible moves/rename; clone establishes a history barrier.
   - Global keyboard shortcuts intercept Ctrl+Z and Ctrl+Y/Ctrl+Shift+Z.
2. Marquee Selection and Batch Translation:
   - batchSelectedCardIds, clearBatchSelection, toggleCardBatchSelected, startMarqueeSelection.
   - Shift + pointerdown on viewport triggers startMarqueeSelection.
   - Batch drag translates all selected cards simultaneously with undoable BATCH_SPATIAL_MOVE.
   - CSS defines .selection-marquee and .leaf-card.is-batch-selected.
3. In-place Card Renaming:
   - Card dblclick event triggers startInPlaceCardRename.
   - Title input inline substitution and commit with CARD_RENAME undo action.
   - CSS defines .inplace-rename-input.
   - Backend TaskForest.update_leaf_topology and WebBridge._update_leaf_topology support title.
4. Card Cloning:
   - Global Ctrl+C copies focused/selected leaf into clipboardCard.
   - Global Ctrl+V calls executeCloneCard with append_leaf and offsets spatial layout.
5. Wire Disconnect Confirm Popover:
   - .wire-disconnect-popover confirms before severing dependencies to prevent misoperation.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

integration_dir = Path(__file__).resolve().parents[1]
if str(integration_dir) not in sys.path:
    sys.path.insert(0, str(integration_dir))

from agentbus.config import load, save
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TestCanvasHandlingUX(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assets_dir = Path(__file__).resolve().parents[1] / 'agentbus/web_assets'
        cls.app_js_path = assets_dir / 'app.js'
        cls.app_css_path = assets_dir / 'app.css'

        cls.app_js = cls.app_js_path.read_text(encoding='utf-8')
        cls.app_css = cls.app_css_path.read_text(encoding='utf-8')

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.cfg = load(self.workspace, create=True)
        self.cfg.data['workers']['codex']['enabled'] = True
        save(self.cfg)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_undo_redo_stack_and_shortcuts(self):
        # 1. applyAction branches
        self.assertIn("case 'BATCH_SPATIAL_MOVE':", self.app_js)
        self.assertIn("case 'CARD_RENAME':", self.app_js)
        # Clone has no safe backend delete operation. Exercise its explicit
        # history barrier rather than accepting a toast-only fake undo.
        from test_undo_real_semantics import TestUndoRealSemantics
        TestUndoRealSemantics('test_clone_is_history_barrier_and_never_promises_fake_undo').test_clone_is_history_barrier_and_never_promises_fake_undo()

        # 2. Ctrl+Z / Ctrl+Y shortcuts
        self.assertIn("event.key.toLowerCase() === 'z'", self.app_js)
        self.assertIn("performUndo()", self.app_js)
        self.assertIn("event.key.toLowerCase() === 'y'", self.app_js)
        self.assertIn("performRedo()", self.app_js)

    def test_marquee_selection_and_batch_translation(self):
        # 1. Selection engine functions
        self.assertIn("const batchSelectedCardIds = new Set();", self.app_js)
        self.assertIn("function clearBatchSelection()", self.app_js)
        self.assertIn("function toggleCardBatchSelected(cardId)", self.app_js)
        self.assertIn("function startMarqueeSelection(event)", self.app_js)

        # 2. Shift key triggers
        self.assertIn("if(event.shiftKey){", self.app_js)
        self.assertIn("startMarqueeSelection(event);", self.app_js)

        # 3. Batch drag in bindCardSpatialDragging
        self.assertIn("isBatch = batchSelectedCardIds.has(leaf.id)", self.app_js)
        self.assertIn("pushUndoAction({ type: 'BATCH_SPATIAL_MOVE', moves });", self.app_js)

        # 4. CSS styles
        self.assertIn(".selection-marquee", self.app_css)
        self.assertIn(".leaf-card.is-batch-selected", self.app_css)

    def test_in_place_renaming(self):
        # 1. Dblclick listener and rename function
        self.assertIn("startInPlaceCardRename(card, leaf);", self.app_js)
        self.assertIn("function startInPlaceCardRename(card, leaf)", self.app_js)
        self.assertIn(".inplace-rename-input", self.app_css)

        # 2. Backend update_leaf_topology supports title
        tree_path = self.workspace / '.agentbus/task-tree.json'
        tree_path.write_text('{"schema_version":1,"section_registry":[],"roots":[{"id":"r1","title":"R","parent_id":null}],"branches":[],"leaves":[{"id":"l1","section_no":"1.1","parent_id":"r1","title":"原标题","dependencies":[]}]}', encoding='utf-8')

        forest = TaskForest(self.cfg)
        forest.update_leaf_topology('l1', title='新标题测试')
        leaf = forest.resolve_leaf('l1')
        self.assertEqual(leaf['title'], '新标题测试')

        # 3. WebBridge supports title payload
        bridge = WebBridge(self.workspace)
        res = bridge.call('update_leaf_topology', {'leaf_id': 'l1', 'title': '通过WebBridge修改标题'})
        self.assertTrue(res.get('ok'))
        leaf2 = forest.resolve_leaf('l1')
        self.assertEqual(leaf2['title'], '通过WebBridge修改标题')

    def test_card_clone_copy_paste(self):
        # 1. Global Ctrl+C and Ctrl+V listeners
        self.assertIn("event.key.toLowerCase() === 'c'", self.app_js)
        self.assertIn("clipboardCard = { ...target };", self.app_js)
        self.assertIn("event.key.toLowerCase() === 'v'", self.app_js)
        self.assertIn("executeCloneCard(clipboardCard);", self.app_js)

        # 2. executeCloneCard implementation
        self.assertIn("async function executeCloneCard(sourceLeaf)", self.app_js)
        from test_undo_real_semantics import TestUndoRealSemantics
        TestUndoRealSemantics('test_clone_is_history_barrier_and_never_promises_fake_undo').test_clone_is_history_barrier_and_never_promises_fake_undo()

    def test_wire_disconnect_popover_confirmation(self):
        # Popover exists to prevent accidental wire removal
        self.assertIn(".wire-disconnect-popover", self.app_js)
        self.assertIn(".wire-disconnect-popover", self.app_css)


if __name__ == '__main__':
    unittest.main()
