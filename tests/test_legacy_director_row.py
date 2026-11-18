#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for Section 4.7 LEGACY-DIRECTOR-ROW:
Verifying empty legacy director mailbox folding behavior, visibility with --all,
and clear explanation of current coordinator relationship.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = next((p for p in Path(__file__).resolve().parents if (p / ".agentbus").is_dir()), Path(__file__).resolve().parents[3])
INTEGRATION_DIR = Path(__file__).resolve().parents[1]
PYTHON_EXE = Path(sys.executable)

sys.path.insert(0, str(INTEGRATION_DIR))
from agentbus.cli import cmd_status


@unittest.skipIf(not (ROOT / ".agentbus").is_dir(), "Snapshot environment missing root .agentbus workspace")
class TestLegacyDirectorRow(unittest.TestCase):
    def test_default_status_hides_empty_director(self):
        """Test agentbus status without --all hides empty legacy director mailbox."""
        import argparse
        ns = argparse.Namespace(workspace=str(ROOT), all=False)
        f = io.StringIO()
        with redirect_stdout(f):
            ret = cmd_status(ns)
        self.assertEqual(ret, 0)
        out = f.getvalue()
        # director row should not appear in the table
        lines = out.splitlines()
        table_rows = [line for line in lines if line.startswith("director ")]
        self.assertEqual(len(table_rows), 0)
        # Should have folding hint
        self.assertIn("旧版 director 信箱无消息已折叠", out)
        self.assertIn("当前平级协调者为", out)

    def test_all_flag_shows_legacy_director(self):
        """Test agentbus status --all shows legacy director mailbox with relationship explanation."""
        import argparse
        ns = argparse.Namespace(workspace=str(ROOT), all=True)
        f = io.StringIO()
        with redirect_stdout(f):
            ret = cmd_status(ns)
        self.assertEqual(ret, 0)
        out = f.getvalue()
        lines = out.splitlines()
        table_rows = [line for line in lines if line.startswith("director ")]
        self.assertEqual(len(table_rows), 1)
        self.assertIn("旧版保留收件箱", table_rows[0])
        self.assertIn("协调者见", table_rows[0])

    def test_subprocess_cli_output(self):
        """Test status via subprocess CLI execution."""
        res_default = subprocess.run(
            [str(PYTHON_EXE), "-m", "agentbus.cli", "status", "--workspace", str(ROOT)],
            cwd=str(INTEGRATION_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        self.assertEqual(res_default.returncode, 0)
        self.assertNotIn("director    ", res_default.stdout)

        res_all = subprocess.run(
            [str(PYTHON_EXE), "-m", "agentbus.cli", "status", "--workspace", str(ROOT), "--all"],
            cwd=str(INTEGRATION_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        self.assertEqual(res_all.returncode, 0)
        self.assertIn("director    ", res_all.stdout)


if __name__ == "__main__":
    unittest.main()
