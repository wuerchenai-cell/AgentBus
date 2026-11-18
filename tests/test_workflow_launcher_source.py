"""Source-mode launcher validation; Codex, 2026-10-03, isolated registry only."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.config import load
from agentbus.project_workflow import WorkflowError, prepare_project_workflow, resume_prompt, workflow_status, _launcher_status


class SourceWorkflowLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load(self.root / 'project', create=True)
        self.state = self.root / 'isolated-public-registry'
        self.state.mkdir()
        self.registry = self.state / 'install.json'
        self.frozen = patch.object(sys, 'frozen', False, create=True)
        self.registry_location = patch('agentbus.skill_install.default_state_dir', return_value=self.state)
        self.frozen.start()
        self.registry_location.start()
        self.addCleanup(self.frozen.stop)
        self.addCleanup(self.registry_location.stop)

    def record(self, launcher):
        self.registry.write_text(json.dumps({'launcher': launcher}), encoding='utf-8')

    def assert_current_source(self):
        prepare_project_workflow(self.cfg)
        self.assertTrue(self.cfg.path.is_file())
        status = workflow_status(self.cfg)
        self.assertTrue(status['files_ready'])
        self.assertTrue(status['launcher_ready'])
        self.assertTrue(status['ready'])
        record = json.loads((self.cfg.workspace / '.agentbus/workflow.json').read_text(encoding='utf-8'))
        self.assertEqual(record['launcher'], {'mode': 'source', 'python': str(Path(sys.executable).absolute()),
                         'entrypoint': str(Path(__file__).resolve().parents[1] / 'entry.py')})
        self.assertIsInstance(resume_prompt(self.cfg, 'codex'), str)

    def test_nonobject_launcher_is_not_adopted_or_unhandled(self):
        for launcher in ([], None):
            with self.subTest(launcher=launcher):
                self.record(launcher)
                self.assertFalse(_launcher_status(launcher)[0])
                self.assert_current_source()

    def test_source_entrypoint_without_interpreter_is_not_adopted(self):
        entry = self.root / 'entry.py'
        entry.write_text('# fixture; never execute', encoding='utf-8')
        invalid = {'mode': 'source', 'python': str(self.root / 'absent-python.exe'), 'entrypoint': str(entry)}
        self.record(invalid)
        self.assertFalse(_launcher_status(invalid)[0])
        self.assert_current_source()
        record = json.loads((self.cfg.workspace / '.agentbus/workflow.json').read_text(encoding='utf-8'))
        self.assertNotEqual(record['launcher'], invalid, 'do not adopt a partly valid registry command')

    def test_no_registry_does_not_guess_previous_executable(self):
        self.assert_current_source()
        self.assertFalse(self.registry.exists())

    def test_existing_source_interpreter_and_entrypoint_are_adopted_read_only(self):
        entry = Path(__file__).resolve().parents[1] / 'entry.py'
        launcher = {'mode': 'source', 'python': str(Path(sys.executable).resolve()),
                    'entrypoint': str(entry)}
        self.record(launcher)
        before = self.registry.read_bytes()
        prepare_project_workflow(self.cfg)
        self.assertTrue(workflow_status(self.cfg)['ready'])
        record = json.loads((self.cfg.workspace / '.agentbus/workflow.json').read_text(encoding='utf-8'))
        self.assertEqual(record['launcher'], launcher)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_relative_launcher_is_not_adopted_despite_existing_cwd_file(self):
        invalid = {'mode': 'source', 'python': sys.executable, 'entrypoint': 'entry.py'}
        self.record(invalid)
        self.assertFalse(_launcher_status(invalid)[0])
        self.assert_current_source()

    def test_unready_source_or_unknown_reason_has_source_recovery_guidance(self):
        prepare_project_workflow(self.cfg)
        path = self.cfg.workspace / '.agentbus/workflow.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        guidance = '源码运行需先登记有效的 Python 与入口文件，再准备工作流'
        for launcher in ({}, {'mode': 'source', 'python': sys.executable,
                              'entrypoint': str(self.root / 'missing-entry.py')}):
            with self.subTest(launcher=launcher):
                record['launcher'] = launcher
                path.write_text(json.dumps(record), encoding='utf-8')
                status = workflow_status(self.cfg)
                self.assertFalse(status['ready'])
                self.assertIn(guidance, status['reason'])
        record['launcher'] = {'mode': 'frozen', 'executable': str(self.root / 'missing.exe')}
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertNotIn(guidance, workflow_status(self.cfg)['reason'])
        self.assertIn('打开新位置的 EXE', workflow_status(self.cfg)['reason'])


if __name__ == '__main__':
    unittest.main()
