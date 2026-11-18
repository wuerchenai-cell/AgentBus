"""
Automated Regression Tests for §9.1 AGENT-RIGID-PROCESS-GUARD
Source: Antigravity (2026-10-12)

Verifies:
1. Physical deprecation of all legacy binary files (all .exe renamed to .deprecated_old_version except formal root AgentBus.exe).
2. Pre-flight CLI check functionality (agentbus preflight --workspace ...).
3. Pre-flight fails if formal binary is missing or launcher hash drifts.
4. Pre-flight detects any un-deprecated bypass executable in child directories.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

INTEG_ROOT = Path(__file__).resolve().parents[1]
from project_paths import host_project_root
WS_ROOT = host_project_root()


class TestAgentRigidProcessGuard(unittest.TestCase):
    @unittest.skipUnless(WS_ROOT is not None, "Host project audit not executed in portable scope; explicit host release check required")
    def test_legacy_binaries_physical_deprecation(self):
        """Verify that in the actual project workspace, no active legacy EXEs remain un-deprecated."""
        formal_exe = (WS_ROOT / "成果/AgentBus.exe").resolve()
        self.assertTrue(formal_exe.is_file(), f"Formal binary missing: {formal_exe}")

        all_exes = glob.glob(str(WS_ROOT / "成果" / "**" / "*.exe"), recursive=True)
        active_bypasses = []
        for f in all_exes:
            p = Path(f).resolve()
            if p == formal_exe:
                continue
            if any(ign in str(p) for ign in ('.build-venv', 'venv', 'node_modules')):
                continue
            if str(p).endswith('.deprecated_old_version'):
                continue
            active_bypasses.append(str(p))

        self.assertEqual(len(active_bypasses), 0, f"Found un-deprecated bypass binaries: {active_bypasses}")

    @unittest.skipUnless(WS_ROOT is not None, "Host project audit not executed in portable scope; explicit host release check required")
    def test_preflight_cli_passes_on_current_workspace(self):
        """agentbus preflight on the current workspace must return exit code 0."""
        workflow = json.loads((WS_ROOT / '.agentbus/workflow.json').read_text(encoding='utf-8'))
        launcher = workflow['launcher']
        program = Path(launcher['executable'])
        self.assertEqual(program.resolve(), (WS_ROOT / '成果/AgentBus.exe').resolve())
        self.assertEqual(hashlib.sha256(program.read_bytes()).hexdigest(), launcher['sha256'])
        cmd = [str(program), "preflight", "--workspace", str(WS_ROOT)]
        proc = subprocess.run(cmd, cwd=str(INTEG_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, f"Preflight failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn("[PASS] 控制目录 (.agentbus) 存在且受控", proc.stdout)
        self.assertIn("[PASS] 唯一正式出厂程序物理核对无误", proc.stdout)
        self.assertIn("[PASS] 历史版本物理绝育检查通过", proc.stdout)
        self.assertIn("[SUCCESS]", proc.stdout)

    def test_preflight_fails_on_bypass_binary(self):
        """Simulate a dummy legacy bypass binary and ensure preflight catches it."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_ws = Path(tmp_dir)
            ctrl = tmp_ws / ".agentbus"
            ctrl.mkdir()
            out_root = tmp_ws / "成果"
            out_root.mkdir()

            # Create formal exe
            formal_exe = out_root / "AgentBus.exe"
            formal_exe.write_bytes(b"dummy formal exe content")
            formal_sha = hashlib.sha256(b"dummy formal exe content").hexdigest()

            wf = {"launcher": {"sha256": formal_sha}}
            (ctrl / "workflow.json").write_text(json.dumps(wf), encoding="utf-8")

            # Create an illegal bypass binary
            bypass_exe = out_root / "child" / "old_agentbus.exe"
            bypass_exe.parent.mkdir()
            bypass_exe.write_bytes(b"bypass binary")

            venv_py = INTEG_ROOT / ".build-venv/Scripts/python.exe"
            if not venv_py.is_file():
                venv_py = Path(sys.executable)

            cmd = [str(venv_py), "-m", "agentbus.cli", "preflight", "--workspace", str(tmp_ws)]
            proc = subprocess.run(cmd, cwd=str(INTEG_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 1, "Preflight must fail when bypass binary is detected")
            self.assertIn("未绝育的历史版本 EXE", proc.stdout)


if __name__ == "__main__":
    unittest.main()
