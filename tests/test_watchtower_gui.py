from __future__ import annotations

import sys
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.bus import Bus
from agentbus.config import load
from agentbus.watchtower import Watchtower
from agentbus.watchtower_gui import TowerApp


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Text:
    def __init__(self, value):
        self.value = value

    def get(self, *_args):
        return self.value


class WatchtowerGuiPostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.cfg = load(self.workspace, create=True)
        self.bus = Bus(self.workspace / ".agentbus")
        self.bus.init()
        app = TowerApp.__new__(TowerApp)
        app.wt = Watchtower(self.cfg, self.bus)
        app.intent = Text("请执行这个任务")
        app.sender_var = Value("codex")
        app.to_var = Value("trae")
        app.result_to_var = Value("")
        app.busy_var = Value("")
        app.root = SimpleNamespace(after=lambda *_args: None)
        app.q = queue.Queue()
        app._log = lambda _text: None
        self.app = app

    def tearDown(self):
        self.temp.cleanup()

    def test_gui_post_requires_explicit_result_recipient(self):
        with patch("agentbus.watchtower_gui.messagebox.showwarning") as warning:
            self.app._post()
        warning.assert_called_once()
        self.assertEqual(self.bus.pending("trae"), [])

    def test_gui_post_persists_selected_result_recipient(self):
        self.app.result_to_var.set("zcode")
        self.app._post()
        ready = self.bus.pending("trae")
        self.assertEqual(len(ready), 1)
        task = self.bus.read(ready[0])
        self.assertEqual(task.result_to, "zcode")

    def test_gui_post_rejects_disabled_executor(self):
        self.cfg.data["workers"]["trae"]["executor_enabled"] = False
        self.app.result_to_var.set("zcode")
        with patch("agentbus.watchtower_gui.messagebox.showwarning") as warning:
            self.app._post()
        warning.assert_called_once()
        self.assertEqual(self.bus.pending("trae"), [])

    def test_gui_loop_reports_stale_processing_messages(self):
        self.app.stop_flag = threading.Event()
        self.app.stop_flag.clear()
        self.app.wt._sweep_stale = lambda _agents: ["stale warning"]
        with patch.object(self.app.wt.bus, "watcher", return_value=object()), \
                patch.object(self.app.wt.bus, "close_watchers"), \
                patch.object(self.app.wt, "scan", return_value=({}, [])), \
                patch.object(self.app.wt.bus, "wait_for_change",
                             side_effect=lambda *_args: (self.app.stop_flag.set(), None)[1]):
            self.app._loop()
        queued = []
        while not self.app.q.empty():
            queued.append(self.app.q.get_nowait())
        self.assertIn(("stale", ["stale warning"]), queued)


if __name__ == "__main__":
    unittest.main()
