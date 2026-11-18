"""Maturity diagnostics; Codex, 2026-10-03. No model or credentials."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.bus import Bus
from agentbus.config import load
from agentbus.health import inspect_project
from agentbus.project_workflow import prepare_project_workflow


class ProcessingDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '项目'
        self.cfg = load(self.root, create=True)
        self.bus = Bus(self.root / '.agentbus')
        self.bus.init()
        launcher = Path(self.temp.name) / 'AgentBus.exe'
        launcher.write_bytes(b'fixture only')
        with patch('sys.frozen', True, create=True), patch('sys.executable', str(launcher)):
            prepare_project_workflow(self.cfg)

    def claimed(self):
        msg = self.bus.make('codex', 'workbuddy', 'task', body='SECRET_TASK_TEXT', result_to='codex')
        self.bus.post(msg)
        msg = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        return self.bus.dirs('workbuddy')['processing'] / (msg.id + '.json')

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob('*') if p.is_file()}

    def test_orphan_claim_is_visible_and_inspection_is_read_only(self):
        self.claimed()
        before = self.snapshot()
        report = inspect_project(self.cfg)
        self.assertEqual(report['status'], 'attention')
        record = report['processing'][0]
        self.assertFalse(record['registered_job'])
        self.assertTrue(record['claim_complete'])
        self.assertEqual(record['claimed_by'], 'workbuddy')
        self.assertNotIn('SECRET_TASK_TEXT', json.dumps(report))
        self.assertEqual(before, self.snapshot())

    def test_physical_processing_without_claim_is_not_normal_work(self):
        path = self.claimed()
        data = json.loads(path.read_text(encoding='utf-8'))
        data.update(status='pending', claimed_by='', claimed_at='')
        path.write_text(json.dumps(data), encoding='utf-8')
        before = self.snapshot()
        record = inspect_project(self.cfg)['processing'][0]
        self.assertFalse(record['claim_complete'])
        self.assertIn('认领', record['message'])
        self.assertEqual(before, self.snapshot())

    def test_stale_is_file_age_not_death_certificate(self):
        path = self.claimed()
        old = time.time() - 10000
        os.utime(path, (old, old))
        record = inspect_project(self.cfg)['processing'][0]
        self.assertTrue(record['heartbeat_stale'])
        self.assertIn('不证明', record['message'])
        self.assertTrue(path.exists())
        self.assertEqual(len(self.bus.pending('workbuddy')), 0)

    def test_corrupt_processing_is_reported_without_body_or_repair(self):
        path = self.claimed()
        path.write_text('{"body":"SECRET_CORRUPT', encoding='utf-8')
        before = self.snapshot()
        report = inspect_project(self.cfg)
        self.assertEqual(report['processing'][0]['status'], 'unreadable')
        self.assertNotIn('SECRET_CORRUPT', json.dumps(report))
        self.assertEqual(before, self.snapshot())

    def test_diagnostic_is_allowlisted_and_does_not_overwrite(self):
        from agentbus.health import save_diagnostic
        self.claimed()
        before = self.snapshot()
        dest = Path(self.temp.name) / '诊断.json'
        report = save_diagnostic(self.cfg, dest)
        text = dest.read_text(encoding='utf-8')
        self.assertNotIn('SECRET_TASK_TEXT', text)
        self.assertNotIn('body', text)
        self.assertNotIn('command', text)
        self.assertEqual(report['model_calls'], 0)
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(FileExistsError):
            save_diagnostic(self.cfg, dest)
        self.assertEqual(text, dest.read_text(encoding='utf-8'))

    def test_diagnostic_rejects_private_workbuddy_destination(self):
        from agentbus.health import save_diagnostic
        with self.assertRaises(ValueError):
            save_diagnostic(self.cfg, Path(self.temp.name) / '.workbuddy' / 'diag.json')

    def test_state_path_occupied_by_file_is_attention_not_empty(self):
        state = self.bus.dirs('workbuddy')['processing']
        state.rmdir()
        state.write_text('preserve evidence', encoding='utf-8')
        before = self.snapshot()
        report = inspect_project(self.cfg)
        self.assertEqual(report['status'], 'attention')
        self.assertTrue(any(c['code'] == 'processing' and c['status'] == 'attention'
                            for c in report['checks']))
        self.assertEqual(before, self.snapshot())

    def test_diagnostic_masks_unregistered_free_text_identifiers(self):
        from agentbus.health import save_diagnostic
        directory = self.bus.agents_dir / 'SECRET_NAME command --flag' / 'processing'
        directory.mkdir(parents=True)
        (directory / 'SECRET_FILENAME task body.json').write_text('{}', encoding='utf-8')
        dest = Path(self.temp.name) / 'safe.json'
        save_diagnostic(self.cfg, dest)
        text = dest.read_text(encoding='utf-8')
        self.assertNotIn('SECRET_NAME', text)
        self.assertNotIn('SECRET_FILENAME', text)
        self.assertNotIn('--flag', text)
        self.assertIn('invalid-', text)

    def test_damaged_audit_is_visible_without_exporting_raw_text(self):
        self.bus.events_path.write_text('{"body":"SECRET_AUDIT_PARTIAL', encoding='utf-8')
        before = self.snapshot()
        report = inspect_project(self.cfg)
        self.assertEqual(report['status'], 'attention')
        self.assertTrue(any(c['code'] == 'audit' and c['status'] == 'attention'
                            for c in report['checks']))
        self.assertNotIn('SECRET_AUDIT_PARTIAL', json.dumps(report))
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
