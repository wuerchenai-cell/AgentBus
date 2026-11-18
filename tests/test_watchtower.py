from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.bus import Bus, new_id, now_iso
from agentbus.config import ConfigError, DEFAULTS, load, validate
from agentbus.watchtower import Watchtower


class ConfigRoutingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_include_oversight_thresholds(self):
        cfg = load(self.ws, create=True)
        self.assertEqual(cfg.data["oversight"], {
            "review_every_n": 3, "stuck_attempts": 2, "stuck_hops": 2,
        })

    def test_gui_enabled_worker_can_be_director(self):
        cfg = load(self.ws, create=True)
        data = json.loads(cfg.path.read_text(encoding="utf-8"))
        data["director"] = "trae"
        cfg.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(load(self.ws).director, "trae")

    def test_disabled_worker_cannot_be_director(self):
        cfg = load(self.ws, create=True)
        data = json.loads(cfg.path.read_text(encoding="utf-8"))
        data["workers"]["trae"]["enabled"] = False
        data["director"] = "trae"
        cfg.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load(self.ws)

    def test_oversight_thresholds_require_nonnegative_integers(self):
        for value in (-1, "2", 1.5, True):
            with self.subTest(value=value):
                data = json.loads(json.dumps(DEFAULTS))
                data["oversight"]["stuck_hops"] = value
                with self.assertRaises(ConfigError):
                    from agentbus.config import validate
                    validate(data)

    def test_executor_enabled_must_be_boolean(self):
        data = json.loads(json.dumps(DEFAULTS))
        data["workers"]["codex"]["executor_enabled"] = "false"
        with self.assertRaises(ConfigError):
            validate(data)


class WatchtowerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name)
        self.cfg = load(self.ws, create=True)
        self.bus = Bus(self.ws / ".agentbus")
        self.bus.init()
        self.watchtower = Watchtower(self.cfg, self.bus)

    def tearDown(self):
        self.bus.close_watchers({})
        self.temp.cleanup()

    def test_observation_signature_changes_for_jobs_and_processing_metadata(self):
        observe = getattr(self.watchtower, 'observation_signature', None)
        self.assertTrue(callable(observe), 'job and processing metadata need an event signature')
        before = observe()
        jobs = self.ws / '.agentbus/jobs'
        jobs.mkdir(exist_ok=True)
        (jobs / 'j-progress.json').write_text('{"note":"first"}', encoding='utf-8')
        first = observe()
        self.assertNotEqual(first, before)
        (jobs / 'j-progress.json').write_text('{"note":"next step"}', encoding='utf-8')
        second = observe()
        self.assertNotEqual(second, first)
        message = self.message()
        self.bus.post(message)
        self.bus.claim('trae', self.bus.pending('trae')[0])
        self.assertNotEqual(observe(), second)

    def test_existing_directory_events_cover_ready_processing_and_jobs(self):
        open_watchers = getattr(self.watchtower, 'event_watchers', None)
        self.assertTrue(callable(open_watchers), 'the monitor must subscribe to job/processing directories')
        watchers = open_watchers()
        try:
            self.assertIn('trae', watchers)
            self.assertIn('processing:trae', watchers)
            self.assertIn('jobs', watchers)
            self.assertTrue((self.ws / '.agentbus/jobs').is_dir())
            if sys.platform == 'win32':
                self.assertIsNotNone(watchers['jobs'])
                self.assertTrue(watchers['jobs'].wait(0.01) is False)
                (self.ws / '.agentbus/jobs/j-new.json').write_text('{}', encoding='utf-8')
                self.assertTrue(watchers['jobs'].wait(2))
        finally:
            self.bus.close_watchers(watchers)

    def message(self, kind="task", result_to="codex", body=""):
        return self.bus.make(sender="codex", to="trae", type=kind,
                             title="实施项", body=body, result_to=result_to)

    def test_task_goal_review_wake_lines_name_actual_result_recipient(self):
        for kind in ("task", "goal", "review"):
            with self.subTest(kind=kind):
                line = self.watchtower.wake_line("trae", [self.message(kind)])
                self.assertIn("结果收件人：codex", line)

    def test_reply_box_uses_message_route_not_coordinator(self):
        route_to_trae = self.message(result_to="trae")
        self.assertEqual(self.watchtower.reply_box(route_to_trae),
                         self.bus.dirs("trae")["ready"])
        with self.assertRaises(ValueError):
            self.watchtower.reply_box(self.message(result_to=None))

    def test_missing_route_says_pause_and_ask_instead_of_guessing(self):
        line = self.watchtower.wake_line("trae", [self.message(result_to=None)])
        self.assertIn("暂停并询问", line)
        self.assertNotIn("结果收件人：codex", line)
        self.assertNotIn("结果收件人：trae", line)

    def test_gui_processing_timeout_warns_without_reclaim(self):
        claimed = self.bus.make(sender="codex", to="trae", type="task", result_to="codex")
        self.bus.post(claimed)
        path = self.bus.dirs("trae")["ready"] / f"{claimed.id}.json"
        self.bus.claim("trae", path)
        processing = self.bus.dirs("trae")["processing"] / path.name
        old = 1000.0
        # Set old mtime explicitly; GUI work must stay in processing.
        import os
        os.utime(processing, (old, old))
        with patch("agentbus.watchtower.time.time", return_value=old + 10000):
            with patch("builtins.print") as output:
                self.watchtower._sweep_stale(["trae"])
        self.assertTrue(processing.exists())
        self.assertIn("超时", " ".join(str(c.args) for c in output.call_args_list))

    def test_cli_processing_timeout_warns_without_reclaim(self):
        claimed = self.bus.make(sender="human", to="codex", type="task",
                                result_to="human")
        self.bus.post(claimed)
        path = self.bus.dirs("codex")["ready"] / f"{claimed.id}.json"
        self.bus.claim("codex", path)
        processing = self.bus.dirs("codex")["processing"] / path.name
        old = 1000.0
        import os
        os.utime(processing, (old, old))
        with patch("agentbus.watchtower.time.time", return_value=old + 10000):
            with patch("builtins.print") as output:
                self.watchtower._sweep_stale(["codex"])
        self.assertTrue(processing.exists())
        self.assertIn("人工确认", " ".join(str(c.args) for c in output.call_args_list))
        self.assertNotIn("回收卡死", " ".join(str(c.args) for c in output.call_args_list))

    def test_stale_processing_warning_is_emitted_once_per_watchtower_run(self):
        claimed = self.bus.make(sender="human", to="trae", type="task",
                                result_to="human")
        self.bus.post(claimed)
        path = self.bus.dirs("trae")["ready"] / f"{claimed.id}.json"
        self.bus.claim("trae", path)
        processing = self.bus.dirs("trae")["processing"] / path.name
        old = 1000.0
        import os
        os.utime(processing, (old, old))
        with patch("agentbus.watchtower.time.time", return_value=old + 10000):
            with patch("builtins.print") as output:
                self.watchtower._sweep_stale(["trae"])
                self.watchtower._sweep_stale(["trae"])
        warnings = [call for call in output.call_args_list if "超时" in str(call.args)]
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()

