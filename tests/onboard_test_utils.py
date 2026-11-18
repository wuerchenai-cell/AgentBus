"""Temporary project fixtures shared only by closeout onboarding tests."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from agentbus.config import load, save
from agentbus.project_workflow import prepare_project_workflow

class OnboardFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='agentbus-onboard-closeout-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project with spaces'
        self.root.mkdir()
        cfg = load(self.root, create=True)
        for name, worker in cfg.data['workers'].items():
            worker['enabled'] = name == 'codex'
            worker['executor_enabled'] = name == 'codex'
        save(cfg)
        prepare_project_workflow(cfg)

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob('*') if path.is_file()
            and '/leases/' not in path.as_posix() and '/operation-locks/' not in path.as_posix()}

    def add_peer(self, name='existing-peer', **values):
        cfg = load(self.root)
        cfg.data['workers'][name] = {'display_name': 'Existing Peer', 'enabled': True,
            'executor_enabled': True, 'can_spawn': False, 'kind': 'gui', 'transport': 'file',
            'communication_mode': 'file', 'file_capabilities': ['read'],
            'project_skill': '.public/skills/existing-peer/SKILL.md', **values}
        save(cfg)
        prepare_project_workflow(cfg)
        return cfg.data['workers'][name]

    def link(self, alias, target):
        alias.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(target)], capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.addCleanup(lambda: os.rmdir(alias) if alias.exists() else None)
        else:
            alias.symlink_to(target, target_is_directory=True)
            self.addCleanup(lambda: alias.unlink(missing_ok=True))
