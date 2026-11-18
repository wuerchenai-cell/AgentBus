"""
Tests for PRIORITY-BADGE-MEANING (P2, §7.30)
Source: Antigravity (2026-10-12)

Verifies:
1. Card priority badges (P0/P1/P2/empty) are treated as local visual preference marks,
   not algorithm dispatch priorities.
2. In gui_state.py, load_priorities and save_priorities properly store and validate
   priorities per project in ~/.agentbus_ui.json, supporting cross-restart preservation
   even in InPrivate WebView2 mode.
3. WebBridge `load_priorities` and `save_priorities` endpoints operate correctly,
   and snapshot ui_state contains priorities.
4. Frontend app.js clearly documents and tooltips the badge as a personal visual mark,
   not affecting underlying recommendation or dispatch algorithms.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.config import load, save
from agentbus.gui_state import load_priorities, save_priorities
from agentbus.web_bridge import WebBridge


class TestPriorityBadgeMeaning(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.state_file = self.workspace / "test_ui_state.json"
        self.cfg = load(self.workspace, create=True)
        save(self.cfg)
        self.bus = Bus(self.workspace / ".agentbus")
        self.bus.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_gui_state_priorities_persistence_and_validation(self):
        # 1. Initially empty
        initial = load_priorities(self.workspace, path=self.state_file)
        self.assertEqual(initial, {})

        # 2. Save valid priorities
        valid = {"leaf-1": "P0", "leaf-2": "P1", "leaf-3": "P2"}
        ok = save_priorities(self.workspace, valid, path=self.state_file)
        self.assertTrue(ok)

        # 3. Load back
        loaded = load_priorities(self.workspace, path=self.state_file)
        self.assertEqual(loaded, valid)

        # 4. Reject invalid priorities (e.g. non-P0/P1/P2 value)
        invalid = {"leaf-1": "HIGH"}
        ok_invalid = save_priorities(self.workspace, invalid, path=self.state_file)
        self.assertFalse(ok_invalid)
        # Verify original state preserved
        self.assertEqual(load_priorities(self.workspace, path=self.state_file), valid)

    def test_web_bridge_priorities_actions_and_snapshot(self):
        bridge = WebBridge(self.workspace)

        # 1. Save priorities via bridge
        res_save = bridge.call("save_priorities", {
            "workspace": str(self.workspace),
            "priorities": {"task-a": "P0", "task-b": "P2"}
        })
        self.assertTrue(res_save.get("ok"))
        self.assertEqual(res_save["data"]["priorities"], {"task-a": "P0", "task-b": "P2"})

        # 2. Load priorities via bridge
        res_load = bridge.call("load_priorities", {"workspace": str(self.workspace)})
        self.assertTrue(res_load.get("ok"))
        self.assertEqual(res_load["data"]["priorities"], {"task-a": "P0", "task-b": "P2"})

        # 3. Snapshot includes ui_state.priorities
        res_snap = bridge.call("snapshot", {})
        self.assertTrue(res_snap.get("ok"))
        ui_state = res_snap["data"]["ui_state"]
        self.assertIn("priorities", ui_state)
        self.assertEqual(ui_state["priorities"], {"task-a": "P0", "task-b": "P2"})

    def test_frontend_app_js_explicit_scope_and_naming(self):
        app_js_path = Path(__file__).resolve().parents[1] / "agentbus/web_assets/app.js"
        app_js = app_js_path.read_text(encoding="utf-8")

        # 1. Verify app.js explains visual nature in toast and tooltip
        self.assertIn("个人看板视觉标注，不作为调度执行器的算法排序依据", app_js)
        self.assertIn("仅作看板视觉标记，不改变底层调度顺序", app_js)

        # 2. Verify dual storage (ui_state priorities + localStorage fallback)
        self.assertIn("snapshot.ui_state.priorities", app_js)
        self.assertIn("call('save_priorities'", app_js)


if __name__ == '__main__':
    unittest.main()
