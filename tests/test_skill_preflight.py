"""Actual Windows skill preflight regressions; Codex, 2026-10-03.

Only a script fragment and isolated bytes are used. No EXE is ever executed.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from agentbus.skill_install import bundled_skill_source


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'Windows PowerShell unavailable')
class SkillPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.program = self.base / 'program fixture.exe'
        self.program.write_bytes(b'never execute these bytes')
        self.entry = self.base / 'entry.py'
        self.entry.write_bytes(b'# never execute')
        self.digest = hashlib.sha256(self.program.read_bytes()).hexdigest()
        self.record = self.base / 'record.json'
        text = bundled_skill_source().read_text(encoding='utf-8')
        fragment = ('function Assert-AgentBusLauncherPath {'
                    + text.split('function Assert-AgentBusLauncherPath {', 1)[1]
                    .split('\nfunction Invoke-AgentBus', 1)[0])
        quoted = str(self.record).replace("'", "''")
        self.script = self.base / 'preflight.ps1'
        self.script.write_text(
            "$ErrorActionPreference='Stop'\n"
            "$install=Get-Content -LiteralPath '" + quoted + "' -Raw | ConvertFrom-Json\n"
            "$script:agentBusProgram=[string]$install.launcher.executable\n"
            "$script:agentBusPrefix=@()\n"
            "if ($install.launcher.mode -eq 'source') {"
            "$script:agentBusProgram=[string]$install.launcher.python;"
            "$script:agentBusPrefix=@([string]$install.launcher.entrypoint)}\n"
            + fragment + "\nWrite-Output 'validated without executing program'\n",
            encoding='utf-8-sig')

    def check(self, launcher, expected):
        self.record.write_text(json.dumps({'launcher': launcher}), encoding='utf-8-sig')
        result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(self.script)], cwd=self.base,
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(self.program.read_bytes(), b'never execute these bytes')
        self.assertFalse((self.base / '.workbuddy').exists())

    def test_digest_matching_legacy_and_invalid_records(self):
        for values, expected in (({'sha256': self.digest}, 0), ({}, 0),
                                 ({'sha256': None}, 1), ({'sha256': 'a' * 64}, 1),
                                 ({'sha256': self.digest.upper()}, 1)):
            with self.subTest(values=values):
                self.check({'mode': 'frozen', 'executable': str(self.program), **values}, expected)

    def test_relative_private_and_source_paths_are_checked_before_launch(self):
        for value in (self.program.name, 'C:program fixture.exe', '\\program fixture.exe',
                      str(self.base / '.workbuddy/never-access.exe'), str(self.base),
                      str(self.program) + '\x01'):
            with self.subTest(path=value):
                self.check({'mode': 'frozen', 'executable': value}, 1)
        self.check({'mode': 'source', 'python': str(self.program), 'entrypoint': str(self.entry)}, 0)
        self.check({'mode': 'source', 'python': str(self.program), 'entrypoint': 'entry.py'}, 1)


if __name__ == '__main__':
    unittest.main()
