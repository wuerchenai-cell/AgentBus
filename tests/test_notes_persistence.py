"""§7.25 画布/便签偏好持久化回归。

问题：`app.js` 的画布便签只存 `localStorage`，而桌面壳用 `private_mode` 的 WebView
会清理临时用户目录——**重启后便签丢失**。视口与分支折叠早已有 Python 侧持久化
（`gui_state.py` 写本机 `~/.agentbus_ui.json`），便签却没有。

修法：把便签接到**同一套项目级状态接口**（`gui_state.load_notes/save_notes`），
经 `web_bridge` 的 `load_notes`/`save_notes` 动作与快照 `ui_state.notes` 暴露；
前端改为**双通道**：宿主状态为主、`localStorage` 兜底。

验收对应：
  * 保存到现有项目级状态接口 ✅（复用 gui_state 模式，不新造存储）
  * 按工作区隔离并校验大小/字段 ✅
  * 重启和切项目后恢复、不覆盖另一项目 ✅（含真实"重启"用例）
  * 区分设备本地偏好与共享任务事实 ✅（只写本机 UI 状态，绝不写项目共享目录）
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from agentbus import gui_state as gs
from agentbus.web_bridge import WebBridge

_APP_JS = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets" / "app.js"


def _note(note_id="note-abc-1", **overrides):
    note = {"id": note_id, "x": 40, "y": 40, "w": 240, "h": 190,
            "color": "green", "text": "内容", "collapsed": False}
    note.update(overrides)
    return note


class GuiStateNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_file = self.root / "ui.json"
        self.ws = self.root / "proj"
        self.ws.mkdir()
        (self.ws / ".agentbus").mkdir()

    def test_round_trip(self):
        self.assertTrue(gs.save_notes(self.ws, [_note()], path=self.state_file))
        loaded = gs.load_notes(self.ws, path=self.state_file)
        self.assertEqual(1, len(loaded))
        self.assertEqual("note-abc-1", loaded[0]["id"])
        self.assertEqual("内容", loaded[0]["text"])

    def test_workspace_isolation(self):
        other = self.root / "proj2"
        other.mkdir()
        gs.save_notes(self.ws, [_note()], path=self.state_file)
        self.assertEqual([], gs.load_notes(other, path=self.state_file))

    def test_saving_one_project_keeps_the_other(self):
        other = self.root / "proj2"
        other.mkdir()
        gs.save_notes(self.ws, [_note("note-a")], path=self.state_file)
        gs.save_notes(other, [_note("note-b")], path=self.state_file)
        self.assertEqual(["note-a"], [n["id"] for n in gs.load_notes(self.ws, path=self.state_file)])
        self.assertEqual(["note-b"], [n["id"] for n in gs.load_notes(other, path=self.state_file)])

    def test_invalid_payload_is_rejected_whole(self):
        for bad in ([_note(w=10)], [_note(id="bad id")],
                    [_note(x=float("nan"))], [_note(text="x" * (gs.MAX_NOTE_TEXT + 1))],
                    [{"id": "note-ok"}]):
            with self.subTest(bad=bad):
                self.assertFalse(gs.save_notes(self.ws, bad, path=self.state_file))

    def test_too_many_notes_rejected(self):
        many = [_note("note-%d" % n) for n in range(gs.MAX_NOTES + 1)]
        self.assertFalse(gs.save_notes(self.ws, many, path=self.state_file))

    def test_duplicate_ids_rejected_on_save(self):
        self.assertFalse(gs.save_notes(self.ws, [_note("note-x"), _note("note-x")],
                                       path=self.state_file))

    def test_unknown_color_normalises_to_default(self):
        self.assertTrue(gs.save_notes(self.ws, [_note(color="rainbow")], path=self.state_file))
        self.assertEqual(gs.STICKY_DEFAULT_COLOR,
                         gs.load_notes(self.ws, path=self.state_file)[0]["color"])

    def test_load_drops_invalid_entries_without_failing(self):
        gs.save_notes(self.ws, [_note("note-good")], path=self.state_file)
        raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        key = os.path.normcase(str(self.ws.resolve()))
        raw["sticky_notes"][key].append({"id": "BAD ID!!", "x": 0, "y": 0, "w": 240, "h": 190})
        self.state_file.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(["note-good"],
                         [n["id"] for n in gs.load_notes(self.ws, path=self.state_file)])

    def test_notes_do_not_clobber_other_ui_preferences(self):
        gs.save_viewport(self.ws, {"version": 1, "scale": 1.0, "panX": 0, "panY": 0,
                                   "width": 800, "height": 600}, path=self.state_file)
        gs.save_notes(self.ws, [_note()], path=self.state_file)
        self.assertIsNotNone(gs.load_viewport(self.ws, path=self.state_file))
        self.assertEqual(1, len(gs.load_notes(self.ws, path=self.state_file)))


class BridgeNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ws = self.root / "proj"
        self.ws.mkdir()
        (self.ws / ".agentbus").mkdir()
        self.state_file = self.root / "ui.json"
        patcher = patch.object(gs, "state_path", lambda: self.state_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = WebBridge(self.ws)

    def test_save_and_load_via_bridge(self):
        result = self.bridge.call("save_notes", {"workspace": str(self.ws), "notes": [_note()]})
        self.assertTrue(result["ok"], result)
        loaded = self.bridge.call("load_notes", {"workspace": str(self.ws)})
        self.assertEqual(1, len(loaded["data"]["notes"]))

    def test_snapshot_exposes_notes(self):
        self.bridge.call("save_notes", {"workspace": str(self.ws), "notes": [_note()]})
        snapshot = self.bridge.call("snapshot", {})
        self.assertIn("notes", snapshot["data"]["ui_state"])

    def test_invalid_notes_and_extra_fields_rejected(self):
        bad = self.bridge.call("save_notes",
                               {"workspace": str(self.ws), "notes": [_note(w=10)]})
        self.assertFalse(bad["ok"])
        extra = self.bridge.call("save_notes",
                                 {"workspace": str(self.ws), "notes": [], "extra": 1})
        self.assertFalse(extra["ok"])

    def test_notes_survive_a_restart(self):
        """真实"重启"：同一个本机状态文件 + 新建桥接对象，便签必须还在。"""
        self.bridge.call("save_notes", {"workspace": str(self.ws), "notes": [_note(text="跨重启")]})
        restarted = WebBridge(self.ws)
        loaded = restarted.call("load_notes", {"workspace": str(self.ws)})
        self.assertEqual(["跨重启"], [n["text"] for n in loaded["data"]["notes"]])

    def test_notes_are_device_local_not_project_files(self):
        """设备本地偏好：不得在项目目录里留下便签文件。"""
        self.bridge.call("save_notes", {"workspace": str(self.ws), "notes": [_note()]})
        leaked = [p for p in self.ws.rglob("*") if p.is_file() and "note" in p.name.casefold()]
        self.assertEqual([], leaked)


class FrontendWiringTests(unittest.TestCase):
    _TEXT = _APP_JS.read_text(encoding="utf-8")

    def test_reads_hosted_notes_first(self):
        self.assertRegex(self._TEXT, r"snapshot\.ui_state\s*&&\s*snapshot\.ui_state\.notes",
                         "便签应优先读宿主状态 ui_state.notes")

    def test_keeps_localstorage_fallback(self):
        self.assertIn("STICKY_KEY_PREFIX", self._TEXT)
        self.assertRegex(self._TEXT, r"localStorage\.getItem\(STICKY_KEY_PREFIX")

    def test_persists_through_bridge(self):
        self.assertRegex(self._TEXT, r"call\('save_notes',\s*\{\s*workspace:\s*stickyScope\(\)",
                         "便签应经 save_notes 动作写到项目级状态")

    def test_mirrors_branch_fold_dual_channel(self):
        """与 collapsed_branches 同策略——两者都应存在宿主通道。"""
        self.assertIn("ui_state?.collapsed_branches", self._TEXT)
        self.assertIn("ui_state.notes", self._TEXT)


if __name__ == "__main__":
    unittest.main()
