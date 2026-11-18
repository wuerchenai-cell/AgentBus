"""Frozen launcher integrity; Codex, 2026-10-03, isolated files only."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.config import load
from agentbus.project_workflow import (WorkflowError, prepare_project_workflow,
                                      peer_skill, resume_prompt, workflow_status)


class WorkflowIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load(self.root / 'project', create=True)
        self.exe = self.root / 'AgentBus.exe'
        self.content = b'isolated EXE fixture; never execute'
        self.exe.write_bytes(self.content)
        self.prepare()

    def prepare(self, executable=None):
        with patch.object(sys, 'frozen', True, create=True), patch.object(
                sys, 'executable', str(executable or self.exe)):
            return prepare_project_workflow(self.cfg)

    def record(self):
        return json.loads((self.cfg.workspace / '.agentbus/workflow.json').read_text(encoding='utf-8'))

    def update_launcher(self, launcher):
        path = self.cfg.workspace / '.agentbus/workflow.json'
        record = self.record()
        record['launcher'] = launcher
        path.write_text(json.dumps(record), encoding='utf-8')

    def assert_corrupt(self):
        status = workflow_status(self.cfg)
        self.assertFalse(status['ready'])
        self.assertFalse(status['launcher_ready'])
        self.assertEqual(status['launcher_integrity'], 'failed')
        self.assertIn('SHA256', status['reason'])
        self.assertIn('正确', status['reason'])
        with self.assertRaises(WorkflowError):
            resume_prompt(self.cfg, 'codex')

    def test_current_frozen_records_digest_and_verifies_using_streamed_reads(self):
        self.exe.write_bytes(b'fixture' * 310000)
        original = Path.read_bytes
        def guarded(path):
            self.assertNotEqual(path, self.exe, 'EXE must not be loaded in one read')
            return original(path)
        with patch.object(Path, 'read_bytes', guarded), patch(
                'agentbus.skill_install.default_state_dir', side_effect=AssertionError('no registry read')):
            self.prepare()
            status = workflow_status(self.cfg)
        self.assertEqual(self.record()['launcher']['sha256'],
                         hashlib.sha256(b'fixture' * 310000).hexdigest())
        self.assertTrue(status['ready'])
        self.assertEqual(status['launcher_integrity'], 'verified')
        self.assertIn('不代表认证或运行通过', status['native_loading'])

    def test_changed_byte_is_not_ready_and_cannot_generate_resume_prompt(self):
        self.exe.write_bytes(b'X' + self.content[1:])
        self.assert_corrupt()

    def test_truncated_executable_is_not_ready(self):
        self.exe.write_bytes(self.content[:5])
        self.assert_corrupt()

    def test_invalid_recorded_digest_is_rejected_without_reading_executable(self):
        for digest in ('', 'A' * 64, 'g' * 64, 0, None, 'a' * 63):
            with self.subTest(digest=digest):
                launcher = self.record()['launcher']
                launcher['sha256'] = digest
                self.update_launcher(launcher)
                original = Path.open
                def guarded(path, *args, **kwargs):
                    self.assertNotEqual(path, self.exe)
                    return original(path, *args, **kwargs)
                with patch.object(Path, 'open', guarded):
                    status = workflow_status(self.cfg)
                self.assertFalse(status['ready'])
                self.assertEqual(status['launcher_integrity'], 'failed')
                self.assertIn('SHA256 登记格式无效', status['reason'])

    def test_legacy_frozen_without_digest_is_compatible_and_not_modified(self):
        launcher = self.record()['launcher']
        launcher.pop('sha256', None)
        self.update_launcher(launcher)
        path = self.cfg.workspace / '.agentbus/workflow.json'
        before = path.read_bytes()
        status = workflow_status(self.cfg)
        self.assertTrue(status['ready'])
        self.assertEqual(status['launcher_integrity'], 'unrecorded')
        self.assertEqual(path.read_bytes(), before)

    def test_source_is_unrecorded_and_never_hashes_interpreter_or_entrypoint(self):
        entry = self.root / 'entry.py'
        entry.write_bytes(b'# fixture only')
        self.update_launcher({'mode': 'source', 'python': str(self.exe),
                              'entrypoint': str(entry), 'sha256': 'invalid; source ignores it'})
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotIn(path, (self.exe, entry))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            status = workflow_status(self.cfg)
        self.assertTrue(status['ready'])
        self.assertEqual(status['launcher_integrity'], 'unrecorded')

    def test_resaving_current_frozen_migrates_legacy_and_repairs_launcher_only(self):
        launcher = self.record()['launcher']
        launcher.pop('sha256', None)
        self.update_launcher(launcher)
        before_config = self.cfg.path.read_bytes()
        self.prepare()
        self.assertEqual(workflow_status(self.cfg)['launcher_integrity'], 'verified')
        self.exe.write_bytes(b'corrupt')
        correct = self.root / 'new/AgentBus.exe'
        correct.parent.mkdir()
        correct.write_bytes(b'new current fixture')
        self.prepare(correct)
        status = workflow_status(self.cfg)
        self.assertTrue(status['ready'])
        self.assertEqual(status['launcher_integrity'], 'verified')
        self.assertEqual(self.record()['launcher']['executable'], str(correct.resolve()))
        self.assertEqual(self.cfg.path.read_bytes(), before_config)

    def test_private_launcher_is_not_opened_for_check_or_registration(self):
        private = self.root / '.workbuddy/never-read.exe'
        self.update_launcher({'mode': 'frozen', 'executable': str(private), 'sha256': 'a' * 64})
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotIn('.workbuddy', path.parts)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            self.assertFalse(workflow_status(self.cfg)['ready'])
            with self.assertRaises(WorkflowError):
                self.prepare(private)
        self.assertFalse(private.parent.exists())

    def test_source_process_registers_current_checkout_without_adopting_legacy_frozen(self):
        state = self.root / 'isolated-registry'
        state.mkdir()
        launcher = {'mode': 'frozen', 'executable': str(self.exe)}
        registry = state / 'install.json'
        registry.write_text(json.dumps({'launcher': launcher}), encoding='utf-8')
        original = Path.open
        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path, self.exe)
            return original(path, *args, **kwargs)
        with patch.object(sys, 'frozen', False, create=True), patch(
                'agentbus.skill_install.default_state_dir', return_value=state), patch.object(Path, 'open', guarded):
            prepare_project_workflow(self.cfg)
            status = workflow_status(self.cfg)
        self.assertEqual(self.record()['launcher'], {'mode': 'source',
                         'python': str(Path(sys.executable).absolute()),
                         'entrypoint': str(Path(__file__).resolve().parents[1] / 'entry.py')})
        self.assertEqual(status['launcher_integrity'], 'unrecorded')
        self.assertEqual(json.loads(registry.read_text(encoding='utf-8'))['launcher'], launcher)

    def test_peer_workflow_requires_external_digest_check_before_launch(self):
        text = peer_skill('workbuddy')
        self.assertIn('launcher.sha256', text)
        self.assertIn('启动前用宿主文件或程序工具核对 EXE 的 SHA256', text)
        self.assertIn('不一致就停止，不能启动该 EXE 来验证自身', text)
        self.assertIn('摘要匹配不代表认证或运行通过', text)


if __name__ == '__main__':
    unittest.main()
