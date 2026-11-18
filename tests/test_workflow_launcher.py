"""Moved or missing workflow launcher regressions; Codex, 2026-10-03."""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.config import load
from agentbus.project_workflow import (WorkflowError, prepare_project_workflow,
                                      workflow_status, resume_prompt)


class WorkflowLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '项目'
        self.cfg = load(self.root, create=True)
        self.exe = Path(self.temp.name) / '长期程序位置/AgentBus.exe'
        self.exe.parent.mkdir()
        self.exe.write_bytes(b'fixture only, never execute')
        self.prepare(self.cfg, self.exe)

    def prepare(self, cfg, executable):
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', str(executable)):
            return prepare_project_workflow(cfg)

    def manifest(self, launcher):
        path = self.root / '.agentbus/workflow.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['launcher'] = launcher
        path.write_text(json.dumps(value), encoding='utf-8')

    def not_ready(self):
        status = workflow_status(self.cfg)
        self.assertFalse(status['ready'])
        self.assertFalse(status['launcher_ready'])
        self.assertIn('打开新位置的 EXE', status['reason'])
        with self.assertRaisesRegex(WorkflowError, '选择当前项目.*保存设置'):
            resume_prompt(self.cfg, 'workbuddy')

    def test_missing_executable_is_not_ready_and_cannot_generate_resume_prompt(self):
        self.exe.unlink()
        self.not_ready()

    def test_source_launcher_requires_existing_interpreter_and_entrypoint(self):
        entry = Path(self.temp.name) / 'entry.py'
        entry.write_text('fixture only', encoding='utf-8')
        self.manifest({'mode': 'source', 'python': str(self.exe), 'entrypoint': str(entry)})
        self.assertTrue(workflow_status(self.cfg)['ready'])
        entry.unlink()
        self.not_ready()
        entry.write_text('fixture only', encoding='utf-8')
        self.exe.unlink()
        self.not_ready()

    def test_unsupported_or_missing_launcher_is_not_ready(self):
        for launcher in ({}, {'mode': 'network', 'executable': str(self.exe)},
                         {'mode': 'frozen'}, None):
            with self.subTest(launcher=launcher):
                self.manifest(launcher)
                self.not_ready()

    def test_directory_relative_and_malformed_launcher_paths_are_rejected(self):
        for value in (str(self.exe.parent), self.exe.name, 'bad\x00path', 42, ''):
            with self.subTest(value=value):
                self.manifest({'mode': 'frozen', 'executable': value})
                self.not_ready()

    def test_private_directory_launcher_is_rejected_without_executing_or_reading_it(self):
        target = Path(self.temp.name) / '.workbuddy/private-launcher.exe'
        self.manifest({'mode': 'frozen', 'executable': str(target)})
        self.not_ready()
        self.assertFalse(target.parent.exists())

    def test_moved_project_repairs_workspace_and_launcher_on_prepare(self):
        moved = Path(self.temp.name) / '搬迁项目'
        shutil.move(str(self.root), str(moved))
        cfg = load(moved)
        self.assertFalse(workflow_status(cfg)['ready'])
        current_exe = Path(self.temp.name) / '新程序/AgentBus.exe'
        current_exe.parent.mkdir()
        current_exe.write_bytes(b'relocated fixture')
        self.exe.unlink()
        before_config = cfg.path.read_bytes()
        self.prepare(cfg, current_exe)
        status = workflow_status(cfg)
        self.assertTrue(status['ready'])
        self.assertTrue(status['launcher_ready'])
        self.assertEqual(before_config, cfg.path.read_bytes())
        self.assertIn(str(moved), resume_prompt(cfg, 'workbuddy'))
        record = json.loads((moved / '.agentbus/workflow.json').read_text(encoding='utf-8'))
        self.assertEqual(record['workspace'], str(moved.resolve()))
        self.assertEqual(record['launcher']['executable'], str(current_exe.resolve()))

    def test_missing_manifest_is_not_ready_and_does_not_create_files(self):
        path = self.root / '.agentbus/workflow.json'
        path.unlink()
        self.not_ready()
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
