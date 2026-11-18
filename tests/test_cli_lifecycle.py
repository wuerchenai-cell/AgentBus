from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus.cli import cmd_boss, cmd_init, cmd_run, main as cli_main


class CliLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            director="codex",
            worker=lambda name: {"can_spawn": name == "codex"},
            spawnable_workers=lambda: {"codex": {"can_spawn": True}},
        )
        self.instances = []

        class FakeDirector:
            def __init__(inner, cfg):
                inner.closed = False
                self.instances.append(inner)

            def submit(inner, *args, **kwargs):
                return [SimpleNamespace(to="workbuddy", title="task", id="task-1")]

            def queue_goal(inner, *args, **kwargs):
                return SimpleNamespace(id="goal-1")

            def run(inner, **kwargs):
                inner.run_kwargs = kwargs
                # Source: 2026-10-04 CLI failure-exit contract. Production
                # Summary always carries failed; this double models success.
                return SimpleNamespace(render=lambda: "finished", failed=0)

            def _manual_work_pending(inner, result_consumers):
                return False

            def close(inner):
                inner.closed = True

        self.fake_director = FakeDirector

    def run_command(self, command, args):
        output = io.StringIO()
        with patch("agentbus.cli._load", return_value=self.cfg), \
             patch("agentbus.cli.Director", self.fake_director), \
             patch("agentbus.cli.cmd_handoff", return_value=0), \
             contextlib.redirect_stdout(output):
            code = command(args)
        return code, output.getvalue()

    def test_boss_closes_agent_processes_after_run(self):
        args = SimpleNamespace(goal="goal", worker=None, seconds=1, no_review=False,
                               await_manual=True, workspace="project with spaces")
        code, output = self.run_command(cmd_boss, args)

        self.assertEqual(code, 0, output)
        self.assertTrue(self.instances[0].closed)
        self.assertTrue(self.instances[0].run_kwargs["continue_after_results"])
        self.assertTrue(self.instances[0].run_kwargs["await_manual"])

    def test_gui_only_coordinator_queues_goal_and_stops_for_manual_handoff(self):
        self.cfg = SimpleNamespace(
            director="workbuddy",
            worker=lambda name: {"can_spawn": name == "codex"},
            spawnable_workers=lambda: {"codex": {"can_spawn": True}},
        )
        args = SimpleNamespace(goal="inspect project", worker=None, seconds=10,
                               no_review=False, await_manual=True,
                               workspace="project with spaces")
        code, output = self.run_command(cmd_boss, args)

        self.assertEqual(code, 0, output)
        self.assertIn("goal-1", output)
        self.assertIn("手动接手", output)
        self.assertFalse(hasattr(self.instances[0], "run_kwargs"))

    def test_run_closes_agent_processes_after_run(self):
        args = SimpleNamespace(seconds=1, no_review=False,
                               follow_ups=True, await_manual=True)
        code, output = self.run_command(cmd_run, args)

        self.assertEqual(code, 0, output)
        self.assertTrue(self.instances[0].closed)
        self.assertTrue(self.instances[0].run_kwargs["continue_after_results"])
        self.assertTrue(self.instances[0].run_kwargs["await_manual"])

    def test_source_cli_forces_utf8_when_parent_console_uses_legacy_encoding(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "gbk"
        repo = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "agentbus", "doctor"],
            cwd=repo, env=env, capture_output=True, timeout=20)

        self.assertEqual(result.returncode, 0, result.stderr.decode("gbk", "replace"))
        self.assertIn("环境预检".encode("utf-8"), result.stdout)

    def test_init_generates_actionable_agents_contract(self):
        with tempfile.TemporaryDirectory(prefix="agentbus-init-") as temp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cmd_init(SimpleNamespace(workspace=temp, output_root=None))

            self.assertEqual(code, 0, output.getvalue())
            contract = (Path(temp) / "AGENTS.md").read_text(encoding="utf-8")
            for expected in ("agentbus claim", "PROJECT.md", "MEMORY.md",
                             "控制目录", "业务产出目录", ".agentbus"):
                self.assertIn(expected, contract)

    def test_init_creates_taskbook_memory_state_and_log_templates(self):
        with tempfile.TemporaryDirectory(prefix="agentbus-init-") as temp:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cmd_init(SimpleNamespace(workspace=temp, output_root=None))

            self.assertEqual(code, 0, output.getvalue())
            required = {
                "TASKBOOK.md": ("当前目标", "持续改进", "来源", "时间", "下一步"),
                "MEMORY.md": ("来源", "时间"),
                "STATE.md": ("当前目标", "下一步"),
                "LOG.md": ("来源", "时间"),
            }
            for name, markers in required.items():
                path = Path(temp) / name
                self.assertTrue(path.is_file(), name)
                text = path.read_text(encoding="utf-8")
                for marker in markers:
                    self.assertIn(marker, text, f"{name} 缺少 {marker}")
            self.assertIn("TASKBOOK.md", output.getvalue())
            self.assertIn("MEMORY.md", output.getvalue())
            self.assertIn("STATE.md", output.getvalue())
            self.assertIn("LOG.md", output.getvalue())

    def test_init_never_overwrites_existing_continuity_file(self):
        with tempfile.TemporaryDirectory(prefix="agentbus-init-") as temp:
            taskbook = Path(temp) / "TASKBOOK.md"
            taskbook.write_text("用户已有任务书\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = cmd_init(SimpleNamespace(workspace=temp, output_root=None))

            self.assertEqual(code, 0)
            self.assertEqual(taskbook.read_text(encoding="utf-8"), "用户已有任务书\n")

    def test_run_follow_ups_flag_reaches_command_handler(self):
        captured = {}

        def capture(args):
            captured["follow_ups"] = args.follow_ups
            return 0

        with patch("agentbus.cli.cmd_run", side_effect=capture):
            code = cli_main(["run", "--workspace", "project with spaces",
                             "--follow-ups"])

        self.assertEqual(code, 0)
        self.assertTrue(captured["follow_ups"])


if __name__ == "__main__":
    unittest.main()
