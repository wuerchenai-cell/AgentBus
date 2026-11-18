#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for Section 4.6 TEAM-DEFAULT-MINIMAL:
Verifying minimal initial group configuration, user-selected member activation,
preventing unconfigured/unverified vendors from appearing connected, and preserving
existing user choices across upgrades.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Self-bootstrap path discovery for test suite
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentbus.config import load, save, DEFAULTS, Config
from agentbus.onboarding import connection_summary


class TestTeamDefaultMinimal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.integ_dir = Path(__file__).resolve().parents[1]
        cls.python_exe = cls.integ_dir / ".build-venv/Scripts/python.exe"
        if not cls.python_exe.exists():
            cls.python_exe = Path(sys.executable)

    def test_init_minimal_enables_only_coordinator(self):
        """1. Verify `agentbus init --minimal` enables only coordinator."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cmd = [str(self.python_exe), "-m", "agentbus.cli", "init", "--workspace", str(ws), "--minimal"]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(res.returncode, 0, res.stderr)
            cfg = load(ws)
            enabled = list(cfg.enabled_workers())
            self.assertEqual(enabled, ["codex"])
            
            # Verify connection_summary reports only the coordinator
            summary = connection_summary(cfg)
            members = [m["name"] for m in summary["members"]]
            self.assertEqual(members, ["codex"])

    def test_init_with_explicit_members_selection(self):
        """2. Verify `agentbus init --members codex,antigravity` enables only chosen members."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cmd = [str(self.python_exe), "-m", "agentbus.cli", "init", "--workspace", str(ws),
                   "--members", "codex,antigravity", "--coordinator", "codex"]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(res.returncode, 0, res.stderr)
            cfg = load(ws)
            enabled = sorted(list(cfg.enabled_workers()))
            self.assertEqual(enabled, ["antigravity", "codex"])
            
            summary = connection_summary(cfg)
            members = sorted([m["name"] for m in summary["members"]])
            self.assertEqual(members, ["antigravity", "codex"])

    def test_upgrade_existing_project_does_not_reset_user_choice(self):
        """3. Verify re-running init on existing project preserves previously saved user member choices."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            # First init with codex only
            cmd1 = [str(self.python_exe), "-m", "agentbus.cli", "init", "--workspace", str(ws), "--minimal"]
            subprocess.run(cmd1, check=True)
            cfg1 = load(ws)
            self.assertEqual(list(cfg1.enabled_workers()), ["codex"])

            # Re-run plain init (e.g. workspace template refresh)
            cmd2 = [str(self.python_exe), "-m", "agentbus.cli", "init", "--workspace", str(ws)]
            res2 = subprocess.run(cmd2, capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(res2.returncode, 0)
            
            # Existing configuration was loaded without overwriting workers selection
            cfg2 = load(ws)
            self.assertEqual(list(cfg2.enabled_workers()), ["codex"])

    def test_unconfigured_vendors_not_reported_as_connected(self):
        """4. Verify unconfigured vendors do not appear in connection summary or ready inbox."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            cmd = [str(self.python_exe), "-m", "agentbus.cli", "init", "--workspace", str(ws),
                   "--members", "codex", "--coordinator", "codex"]
            subprocess.run(cmd, check=True)
            cfg = load(ws)
            summary = connection_summary(cfg)
            member_names = [m["name"] for m in summary["members"]]
            for unconfigured in ["trae", "workbuddy", "zcode", "opencode"]:
                self.assertNotIn(unconfigured, member_names)


if __name__ == "__main__":
    unittest.main()
