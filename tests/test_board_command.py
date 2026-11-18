"""Tests for agentbus board CLI command (§7.11 一屏待办看板)."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = next((p for p in Path(__file__).resolve().parents if (p / ".agentbus").is_dir()), Path(__file__).resolve().parents[3])  # D:\示例空间\协作 样例
INTEG = Path(__file__).resolve().parents[1]


class TestBoardCommand(unittest.TestCase):

    def setUp(self):
        if not (ROOT / ".agentbus").is_dir():
            self.skipTest("test_board_command 仅在宿主真实工程目录执行，快照环境跳过")

    def run_board(self, args: list[str]) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            "-m", "agentbus.cli",
            "board",
            "--workspace", str(ROOT)
        ] + args
        return subprocess.run(
            cmd,
            # Resolve the integration package from its source root while the
            # command still inspects the real host workspace via --workspace.
            cwd=str(INTEG),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

    def test_board_all_members_table(self):
        proc = self.run_board([])
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        self.assertIn("AgentBus 全组一屏待办看板 (agentbus board)", proc.stdout)
        self.assertIn("全网汇总:", proc.stdout)
        self.assertIn("codex", proc.stdout)
        self.assertIn("antigravity", proc.stdout)

    def test_board_json_output(self):
        proc = self.run_board(["--json"])
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertEqual(data.get("schema_version"), "1.0.0")
        self.assertIn("totals", data)
        self.assertIn("members", data)
        self.assertGreater(data["totals"]["ready"], 0)

        member_names = [m["member"] for m in data["members"]]
        self.assertIn("codex", member_names)
        self.assertIn("antigravity", member_names)

    def test_board_filter_me(self):
        proc = self.run_board(["--me", "antigravity"])
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        self.assertIn("AgentBus 成员待办看板：[antigravity]", proc.stdout)
        self.assertNotIn("[codex]", proc.stdout)

    def test_board_warn_hours_and_stale_flag(self):
        # With very low warn-hours, more msgs become stale
        proc = self.run_board(["--json", "--warn-hours", "1.0"])
        self.assertEqual(proc.returncode, 0, f"Error: {proc.stderr}")
        data = json.loads(proc.stdout)
        self.assertGreater(data["totals"]["stale"], 0)

    def test_board_invalid_member(self):
        proc = self.run_board(["--me", "non_existent_robot_999"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("未找到成员 [non_existent_robot_999]", proc.stdout)


if __name__ == "__main__":
    unittest.main()
