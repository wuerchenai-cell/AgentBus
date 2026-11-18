"""Read-only project health contracts. Source: Codex healthcheck, 2026-10-03."""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.config import load
from agentbus.bus import Bus
from agentbus.jobs import dispatch_job, start_job
from agentbus.project_workflow import prepare_project_workflow


class HealthFeaturePresenceTests(unittest.TestCase):
    def test_project_health_check_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('agentbus.health'),
                             'read-only health API is not implemented')


@unittest.skipUnless(importlib.util.find_spec('agentbus.health'), 'API absent')
class ProjectHealthTests(unittest.TestCase):
    def setUp(self):
        from agentbus.health import inspect_project
        self.inspect = inspect_project
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '项目'
        self.cfg = load(self.root, create=True)
        self.launcher = Path(self.temp.name) / 'AgentBus.exe'
        self.launcher.write_bytes(b'fixture only; never executed')
        with patch('sys.frozen', True, create=True), patch('sys.executable', str(self.launcher)):
            prepare_project_workflow(self.cfg)

    def checks(self, report):
        return {check['code']: check for check in report['checks']}

    def snapshot(self):
        # This is an isolated test fixture, not a recursive production scan.
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob('*') if path.is_file()}

    def test_valid_manual_group_is_healthy_and_does_not_change_files(self):
        before = self.snapshot()
        report = self.inspect(self.cfg)
        self.assertEqual(report['status'], 'healthy')
        self.assertEqual(report['model_calls'], 0)
        self.assertEqual(report['members']['workbuddy']['mode'], 'manual')
        self.assertEqual(report['members']['codex']['mode'], 'automatic-configured')
        self.assertFalse(report['members']['codex']['automatic_accepted'])
        self.assertEqual(self.checks(report)['member:workbuddy']['status'], 'info')
        self.assertEqual(before, self.snapshot())

    def test_missing_workflow_and_launcher_are_attention_without_generating_files(self):
        for path in list(self.root.glob('AGENTBUS.md')) + [self.root / '.agentbus/workflow.json']:
            path.unlink()
        before = self.snapshot()
        report = self.inspect(self.cfg)
        self.assertEqual(report['status'], 'attention')
        self.assertEqual(self.checks(report)['workflow']['status'], 'attention')
        self.assertEqual(self.checks(report)['launcher']['status'], 'attention')
        self.assertEqual(before, self.snapshot())

    def test_modified_workflow_and_missing_launcher_are_reported_separately(self):
        (self.root / 'AGENTBUS.md').write_text('user modification', encoding='utf-8')
        self.launcher.unlink()
        report = self.inspect(self.cfg)
        checks = self.checks(report)
        self.assertEqual(report['status'], 'attention')
        self.assertEqual(checks['workflow']['status'], 'attention')
        self.assertEqual(checks['launcher']['status'], 'attention')
        self.assertEqual((self.root / 'AGENTBUS.md').read_text(encoding='utf-8'), 'user modification')

    def test_stop_is_attention_and_remains_present(self):
        stop = self.root / '.agentbus/STOP'
        stop.write_text('user pause', encoding='utf-8')
        report = self.inspect(self.cfg)
        self.assertEqual(report['status'], 'attention')
        self.assertEqual(self.checks(report)['stop']['status'], 'attention')
        self.assertEqual(stop.read_text(encoding='utf-8'), 'user pause')

    def test_corrupted_recorded_launcher_is_attention_with_specific_reason(self):
        import hashlib
        manifest = self.root / '.agentbus/workflow.json'
        value = json.loads(manifest.read_text(encoding='utf-8'))
        value['launcher']['sha256'] = hashlib.sha256(self.launcher.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(value), encoding='utf-8')
        self.launcher.write_bytes(b'corrupted fixture, never execute')
        before = self.snapshot()
        report = self.inspect(self.cfg)
        launcher = self.checks(report)['launcher']
        self.assertEqual(launcher['status'], 'attention')
        self.assertIn('SHA256', launcher['message'])
        self.assertEqual(before, self.snapshot())

    def test_matching_launcher_digest_never_claims_authentication_or_running_it(self):
        import hashlib
        manifest = self.root / '.agentbus/workflow.json'
        value = json.loads(manifest.read_text(encoding='utf-8'))
        value['launcher']['sha256'] = hashlib.sha256(self.launcher.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(value), encoding='utf-8')
        launcher = self.checks(self.inspect(self.cfg))['launcher']
        self.assertEqual(launcher['status'], 'ok')
        self.assertIn('摘要', launcher['message'])
        self.assertIn('没有运行', launcher['message'])

    def test_gui_waiting_handoff_is_info_not_a_failure(self):
        job = start_job(self.cfg, 'workbuddy', 'one read-only task', request_id='manual')
        dispatch_job(self.cfg, 'codex', job['id'], [{'worker': 'trae', 'body': 'Read only', 'result_to': 'codex'}])
        before = self.snapshot()
        report = self.inspect(self.cfg)
        self.assertEqual(report['status'], 'healthy')
        self.assertEqual(self.checks(report)['job:' + job['id']]['status'], 'info')
        self.assertEqual(report['jobs'][0]['state'], 'waiting_handoff')
        self.assertEqual(before, self.snapshot())

    def test_expired_job_is_attention_and_never_restarted(self):
        job = start_job(self.cfg, 'workbuddy', 'short task', request_id='expired')
        path = self.root / '.agentbus/jobs' / (job['id'] + '.json')
        data = json.loads(path.read_text(encoding='utf-8'))
        data['deadline_ts'] = time.time() - 10
        path.write_text(json.dumps(data), encoding='utf-8')
        before = self.snapshot()
        report = self.inspect(self.cfg)
        self.assertEqual(report['status'], 'attention')
        self.assertEqual(self.checks(report)['job:' + job['id']]['status'], 'attention')
        self.assertEqual(report['jobs'][0]['state'], 'blocked')
        self.assertEqual(before, self.snapshot())

    def test_stale_processing_is_reported_without_stealing_claim(self):
        job = start_job(self.cfg, 'workbuddy', 'one task', request_id='stale')
        dispatch_job(self.cfg, 'codex', job['id'], [{'worker': 'trae', 'body': 'Read only', 'result_to': 'codex'}])
        bus = Bus(self.root / '.agentbus')
        task = bus.claim('trae', bus.pending('trae')[0])
        path = bus.dirs('trae')['processing'] / (task.id + '.json')
        self.cfg.data['gates']['heartbeat_timeout_s'] = 10
        os.utime(path, (time.time() - 60, time.time() - 60))
        before = self.snapshot()
        report = self.inspect(self.cfg)
        self.assertEqual(self.checks(report)['job:' + job['id']]['status'], 'attention')
        self.assertEqual(report['jobs'][0]['state'], 'blocked')
        self.assertEqual(before, self.snapshot())
        self.assertTrue(path.is_file())
        self.assertFalse(bus.pending('trae'))

    def test_jobs_path_replaced_by_a_file_is_not_reported_as_empty_and_healthy(self):
        path = self.root / '.agentbus/jobs'
        path.write_text('user-owned collision', encoding='utf-8')
        report = self.inspect(self.cfg)
        self.assertEqual(self.checks(report)['jobs']['status'], 'attention')
        self.assertEqual(path.read_text(encoding='utf-8'), 'user-owned collision')

    def test_damaged_job_and_manifest_are_attention_without_overwriting_evidence(self):
        jobs = self.root / '.agentbus/jobs'
        jobs.mkdir(exist_ok=True)
        bad_job = jobs / 'j-broken.json'
        bad_job.write_text('{invalid', encoding='utf-8')
        manifest = self.root / '.agentbus/workflow.json'
        manifest.write_text('{invalid', encoding='utf-8')
        before = self.snapshot()
        report = self.inspect(self.cfg)
        self.assertEqual(report['status'], 'attention')
        self.assertEqual(self.checks(report)['jobs']['status'], 'attention')
        self.assertEqual(before, self.snapshot())

    def test_invalid_member_id_is_invalid_without_reading_a_private_path(self):
        self.cfg.data['workers']['../.workbuddy'] = {'enabled': True}
        report = self.inspect(self.cfg)
        self.assertEqual(report['status'], 'invalid')
        self.assertEqual(report['model_calls'], 0)
        self.assertEqual(report['members'], {})

    def test_source_launcher_requires_both_interpreter_and_entrypoint(self):
        path = self.root / '.agentbus/workflow.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        entry = Path(self.temp.name) / 'entry.py'
        entry.write_text('fixture', encoding='utf-8')
        record['launcher'] = {'mode': 'source', 'python': str(self.launcher), 'entrypoint': str(entry)}
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(self.checks(self.inspect(self.cfg))['launcher']['status'], 'ok')
        entry.unlink()
        self.assertEqual(self.checks(self.inspect(self.cfg))['launcher']['status'], 'attention')

    def test_unverified_custom_adapter_does_not_gain_automatic_acceptance(self):
        self.cfg.data['workers']['new-agent'] = {'enabled': True, 'can_spawn': True, 'kind': 'acp', 'accepted': True}
        report = self.inspect(self.cfg)
        self.assertEqual(report['members']['new-agent']['mode'], 'automatic-configured')
        self.assertFalse(report['members']['new-agent']['automatic_accepted'])

    def test_entry_resolving_into_private_directory_is_not_read(self):
        entry = self.root / 'AGENTBUS.md'
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            if path == entry:
                return self.root / '.workbuddy/private/SKILL.md'
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve):
            report = self.inspect(self.cfg)
        self.assertEqual(self.checks(report)['workflow']['status'], 'attention')

    def test_relative_launcher_is_not_accepted_against_current_process_directory(self):
        path = self.root / '.agentbus/workflow.json'
        record = json.loads(path.read_text(encoding='utf-8'))
        record['launcher'] = {'mode': 'frozen', 'executable': self.launcher.name}
        path.write_text(json.dumps(record), encoding='utf-8')
        original_cwd = Path.cwd()
        try:
            os.chdir(self.launcher.parent)
            report = self.inspect(self.cfg)
        finally:
            os.chdir(original_cwd)
        self.assertEqual(self.checks(report)['launcher']['status'], 'attention')

    def test_linked_jobs_outside_project_are_not_read(self):
        external = Path(self.temp.name) / 'external'
        external.mkdir()
        (external / 'j-secret.json').write_text('{private fixture}', encoding='utf-8')
        jobs = self.root / '.agentbus/jobs'
        try:
            jobs.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest('symbolic link privilege unavailable: ' + str(exc))
        with patch('agentbus.health.list_jobs', side_effect=AssertionError('must not follow outside link')):
            report = self.inspect(self.cfg)
        self.assertEqual(self.checks(report)['jobs']['status'], 'attention')


if __name__ == '__main__':
    unittest.main()
