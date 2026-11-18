"""Codex internal regression tests, 2026-10-04; isolated files, model_calls=0."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.adapters import Result
from agentbus.config import Config
from agentbus.director import Director, Summary


class Clock:
    value = 0.0


class DirectorKeepaliveLimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.calls = []
        self.on_execute = None
        self.followup_tasks = [{"title": "next", "body": "bounded next task",
                                "worker": "codex", "result_to": "codex"}]
        self.cfg = Config(path=self.root / ".agentbus" / "config.json",
                          workspace=self.root, data={
            "director": "codex", "workers": {
                "codex": {"enabled": True, "executor_enabled": True,
                          "can_spawn": True, "kind": "mock"}},
            "gates": {"max_hops": 4, "max_retries": 2, "max_tasks": 20,
                      "task_timeout_s": 60, "total_budget_s": 10,
                      "idle_timeout_s": 0.01},
            "oversight": {"review_every_n": 99, "stuck_attempts": 2,
                          "stuck_hops": 2}})

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, _name, _config):
        case = self

        class Adapter:
            def run(self, prompt, **kwargs):
                case.calls.append(prompt)
                if case.on_execute is not None:
                    case.on_execute()
                if "刚收到一条文件回执" in prompt:
                    return Result(0, json.dumps({
                        "complete": False, "summary": "continue",
                        "tasks": case.followup_tasks}), 0.0)
                return Result(0, "controlled task complete", 0.0)

            def close(self):
                pass

        return Adapter()

    def director(self):
        return Director(self.cfg, adapter_factory=self.factory)

    def wait(self, d, agents, timeout_s, watchers, **kwargs):
        for name in agents:
            if d.bus.pending(name, message_types=kwargs.get("message_types")):
                return name
        self.clock.value += min(0.25, timeout_s)
        return None

    def run_clocked(self, d, **kwargs):
        with patch.object(d.bus, "wait_for_work",
                          side_effect=lambda *a, **kw: self.wait(d, *a, **kw)):
            return d.run(should_stop=lambda: self.clock.value >= 3, **kwargs)

    def queued_reviews(self, d, count):
        messages = []
        for number in range(count):
            msg = d.bus.make("human", "codex", "review", f"review-{number}",
                             "controlled check", result_to="human")
            d.bus.post(msg)
            messages.append(msg)
        return messages

    def followup_pair(self, d):
        source = d.bus.make("codex", "codex", "task", "source", "finished work",
                            result_to="codex")
        result = d.bus.make("codex", "codex", "result", "receipt", "finished",
                            reply_to=source.id)
        d._continue_after_results = True
        d._persistent_mode = True
        return source, result

    def test_keepalive_preserves_explicit_seconds(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            summary = self.run_clocked(d, seconds=1, keepalive=True)
        self.assertIn("运行时限", summary.stopped_reason)
        self.assertEqual(summary.elapsed_s, 1)
        self.assertEqual(self.calls, [])

    def test_keepalive_preserves_explicit_round_limit_and_pending_ownership(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            messages = self.queued_reviews(d, 3)
            summary = self.run_clocked(d, seconds=1, max_rounds=2, keepalive=True)
        self.assertEqual(summary.completed, 2)
        self.assertEqual(len(d.bus.pending("codex", message_types=("review",))), 1)
        done_ids = {d.bus.read(path).id for path in d.bus.dirs("codex")["done"].glob("*.json")}
        pending_ids = {d.bus.read(path).id for path in d.bus.pending("codex")}
        self.assertEqual(len(done_ids), 2)
        self.assertFalse(done_ids & pending_ids)
        self.assertEqual(done_ids | pending_ids, {msg.id for msg in messages})
        self.assertIn("轮次", summary.stopped_reason)

    def test_keepalive_stops_when_total_budget_is_already_exhausted(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            d.gates._started = -11
            self.queued_reviews(d, 1)
            summary = self.run_clocked(d, seconds=100, keepalive=True)
        self.assertIn("总预算", summary.stopped_reason)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(d.bus.pending("codex", message_types=("review",))), 1)

    def test_keepalive_default_duration_uses_remaining_configured_budget(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            self.clock.value = 9
            with patch.object(d.bus, "wait_for_work",
                              side_effect=lambda *a, **kw: self.wait(d, *a, **kw)):
                summary = d.run(keepalive=True, should_stop=lambda: self.clock.value >= 12)
        start = next(event for event in d.bus.events() if event.get("event") == "run_start")
        self.assertEqual(start["seconds"], 1)
        self.assertEqual(summary.elapsed_s, 1)
        self.assertEqual(self.calls, [])

    def test_run_limit_is_rechecked_before_next_message_in_same_batch(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            self.queued_reviews(d, 3)
            self.on_execute = lambda: setattr(self.clock, "value", self.clock.value + 1)
            summary = self.run_clocked(d, seconds=0.5, keepalive=True)
        self.assertEqual(summary.completed, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(d.bus.pending("codex", message_types=("review",))), 2)
        self.assertIn("运行时限", summary.stopped_reason)

    def test_stop_is_rechecked_before_next_message_in_same_batch(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            self.queued_reviews(d, 3)
            self.on_execute = lambda: (d.bus.root / "STOP").touch()
            summary = self.run_clocked(d, seconds=1, keepalive=True)
        self.assertEqual(summary.completed, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(d.bus.pending("codex", message_types=("review",))), 2)
        self.assertIn("STOP", summary.stopped_reason)

    def test_persistent_handler_never_bypasses_total_budget(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            msg = self.queued_reviews(d, 1)[0]
            claimed = d.bus.claim("codex", d.bus.pending("codex")[0])
            d._persistent_mode = True
            d.gates._started = -11
            summary = Summary()
            d._handle("codex", claimed, summary, review=False)
        self.assertEqual(self.calls, [])
        self.assertEqual(d.bus.find_message(msg.id).status, "blocked")
        self.assertTrue(summary.escalated)

    def test_expired_budget_suppresses_coordinator_model_turn(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            source, result = self.followup_pair(d)
            d.gates._started = -11
            summary = Summary()
            d._continue_after_result(source, result, summary)
        self.assertEqual(self.calls, [])
        self.assertEqual(d.bus.pending("codex"), [])
        self.assertTrue(summary.escalated)

    def test_budget_is_rechecked_after_coordinator_response_before_dispatch(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            source, result = self.followup_pair(d)
            self.on_execute = lambda: setattr(self.clock, "value", 11)
            summary = Summary()
            d._continue_after_result(source, result, summary)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(d.bus.pending("codex"), [])
        self.assertTrue(summary.escalated)

    def test_budget_is_rechecked_before_each_followup_post(self):
        with patch("agentbus.director.time.monotonic", side_effect=lambda: self.clock.value):
            d = self.director()
            source, result = self.followup_pair(d)
            self.followup_tasks.append({"title": "second", "body": "another bounded task",
                                        "worker": "codex", "result_to": "codex"})
            original_post = d.bus.post

            def post(msg):
                posted = original_post(msg)
                if msg.type == "task":
                    self.clock.value = 11
                return posted

            summary = Summary()
            with patch.object(d.bus, "post", side_effect=post):
                d._continue_after_result(source, result, summary)
        self.assertEqual(len(d.bus.pending("codex", message_types=("task",))), 1)
        self.assertTrue(summary.escalated)

    def test_keepalive_idle_uses_real_event_wait_until_finite_limit_without_model_calls(self):
        d = self.director()
        guard = time.monotonic() + 0.4
        summary = d.run(seconds=0.06, idle_timeout_s=0.001, keepalive=True,
                        should_stop=lambda: time.monotonic() >= guard)
        self.assertIn("运行时限", summary.stopped_reason)
        self.assertEqual(self.calls, [])

    def test_persistent_handler_still_blocks_expired_task_deadline_and_retry_limit(self):
        for failure in ("deadline", "retry"):
            with self.subTest(failure=failure):
                d = self.director()
                msg = self.queued_reviews(d, 1)[0]
                claimed = d.bus.claim("codex", d.bus.pending("codex")[0])
                d._persistent_mode = True
                if failure == "deadline":
                    claimed.deadline_ts = time.time() - 1
                else:
                    claimed.attempt = 3
                d._handle("codex", claimed, Summary(), review=False)
                self.assertEqual(d.bus.find_message(msg.id).status, "blocked")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
