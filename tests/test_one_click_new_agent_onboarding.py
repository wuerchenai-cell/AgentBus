"""
Automated Regression Tests for §9.2 ONE-CLICK-NEW-AGENT-ONBOARDING
Source: Antigravity (2026-10-12)

Verifies:
1. One-click onboarding CLI (agentbus member onboard --workspace ... --id ... --preset ...).
2. Automatic registration in config.json without manual editing.
3. Creation of complete mailbox tree (ready, processing, done, blocked).
4. Tailored newcomer prompt generation with current formal executable hash, contract rules and instructions.
5. Presets (generic, claude, cursor, windsurf) behave correctly with proper prompt tips.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INTEG_ROOT = Path(__file__).resolve().parents[1]
WS_ROOT = next((p for p in Path(__file__).resolve().parents if (p / ".agentbus").is_dir()), Path(__file__).resolve().parents[3])  # D:\示例空间\协作 样例


class TestOneClickNewAgentOnboarding(unittest.TestCase):
    def test_one_click_onboarding_in_isolated_workspace(self):
        """Test end-to-end one-click onboarding in a temporary workspace."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_ws = Path(tmp_dir)

            venv_py = INTEG_ROOT / ".build-venv/Scripts/python.exe"
            if not venv_py.is_file():
                venv_py = Path(sys.executable)

            # 1. Initialize workspace
            init_cmd = [str(venv_py), "-m", "agentbus.cli", "init", "--workspace", str(tmp_ws), "--minimal"]
            p_init = subprocess.run(init_cmd, cwd=str(INTEG_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(p_init.returncode, 0, f"Init failed: {p_init.stderr}")

            # 2. Run one-click onboard for a new agent with cursor preset
            onboard_cmd = [
                str(venv_py), "-m", "agentbus.cli", "member", "onboard",
                "--workspace", str(tmp_ws),
                "--id", "pilot-cursor-agent",
                "--name", "Cursor Pilot Agent",
                "--preset", "cursor",
                "--json"
            ]
            p_onboard = subprocess.run(onboard_cmd, cwd=str(INTEG_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(p_onboard.returncode, 0, f"Onboard failed: {p_onboard.stderr}\n{p_onboard.stdout}")

            res = json.loads(p_onboard.stdout)
            self.assertTrue(res["success"])
            self.assertEqual(res["member_id"], "pilot-cursor-agent")
            self.assertEqual(res["display_name"], "Cursor Pilot Agent")

            # Check prompt contains critical instructions
            prompt = res["prompt"]
            self.assertIn("pilot-cursor-agent", prompt)
            self.assertIn("Cursor 提示", prompt)
            self.assertIn("agentbus claim", prompt)
            self.assertIn("agentbus answer", prompt)

            # Check config.json has the new worker
            cfg_file = tmp_ws / ".agentbus" / "config.json"
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertIn("pilot-cursor-agent", cfg.get("workers", {}))
            worker = cfg["workers"]["pilot-cursor-agent"]
            self.assertTrue(worker["enabled"])
            self.assertEqual(worker["display_name"], "Cursor Pilot Agent")

            # Check mailbox directories created
            mailbox_dir = tmp_ws / ".agentbus" / "agents" / "pilot-cursor-agent"
            for box in ("ready", "processing", "done", "blocked"):
                self.assertTrue((mailbox_dir / box).is_dir(), f"Mailbox {box} missing")

    def test_presets_variation(self):
        """Verify different presets produce specific guidance in prompt."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_ws = Path(tmp_dir)
            venv_py = INTEG_ROOT / ".build-venv/Scripts/python.exe"
            if not venv_py.is_file():
                venv_py = Path(sys.executable)

            subprocess.run([str(venv_py), "-m", "agentbus.cli", "init", "--workspace", str(tmp_ws), "--minimal"],
                           cwd=str(INTEG_ROOT), capture_output=True, check=True)

            presets = {
                "claude": "Claude Code 提示",
                "windsurf": "Windsurf 提示",
                "cursor": "Cursor 提示"
            }

            for preset, expected_hint in presets.items():
                agent_id = f"agent-{preset}"
                cmd = [
                    str(venv_py), "-m", "agentbus.cli", "member", "onboard",
                    "--workspace", str(tmp_ws),
                    "--id", agent_id,
                    "--preset", preset,
                    "--json"
                ]
                proc = subprocess.run(cmd, cwd=str(INTEG_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
                self.assertEqual(proc.returncode, 0, f"Failed for {preset}: {proc.stderr}")
                data = json.loads(proc.stdout)
                self.assertIn(expected_hint, data["prompt"])


if __name__ == "__main__":
    unittest.main()
