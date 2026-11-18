"""Preserved invalid audit observability; Codex, 2026-10-03, no models."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from receipt_helpers import receipt_ticket
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.bus import Bus
from agentbus.config import load
from agentbus.director import Director
from agentbus.health import inspect_project
from agentbus.jobs import JobError, dispatch_job, finalize_job, start_job, status_job


class AuditObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        self.bus = Bus(self.cfg.workspace / '.agentbus')
        self.bus.init()

    def append(self, data):
        with self.bus.events_path.open('ab') as stream:
            stream.write(data)

    def safe_events(self):
        try:
            return self.bus.events()
        except (ValueError, TypeError, UnicodeError) as exc:
            self.fail('invalid audit must produce metadata, not crash: ' + type(exc).__name__)

    def snapshot(self):
        return {str(p.relative_to(self.bus.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.bus.root.rglob('*') if p.is_file() and 'leases' not in p.parts}

    def completed_tasks(self):
        job = start_job(self.cfg, 'workbuddy', 'Verify bounded work', request_id='audit-work')
        job = dispatch_job(self.cfg, 'codex', job['id'], [
            {'worker': 'trae', 'body': 'Read one bounded file', 'result_to': 'codex'}])
        task = self.bus.find_message(job['tasks'][0]['id'])
        task.result, task.exit_code = 'verified', 0
        self.bus.finish(task.to, task, 'done', ticket=receipt_ticket(task))
        result = self.bus.make('trae', 'codex', 'result', body='verified', reply_to=task.id)
        result.exit_code = 0
        self.bus.post(result)
        return job, task

    def test_invalid_lines_have_generated_metadata_without_exposing_raw_text(self):
        self.append(b'{"event":"valid","id":"one"}\n'
                    b'{"bad":"SECRET_CORRUPT"\n'
                    b'[]\nnull\n'
                    b'\xffSECRET_BYTES\n'
                    b'{"event":"valid","id":"two"}\n')
        before = self.snapshot()
        events = self.safe_events()
        self.assertEqual([e['id'] for e in events if e.get('event') == 'valid'], ['one', 'two'])
        markers = [e for e in events if e.get('event') == 'audit_invalid_line']
        self.assertEqual([e['line'] for e in markers], [2, 3, 4, 5])
        self.assertEqual([e['error_type'] for e in markers],
                         ['JSONDecodeError', 'NonObject', 'NonObject', 'UnicodeDecodeError'])
        self.assertTrue(all(e['generated_by'] == 'agentbus.events' for e in markers))
        self.assertNotIn('SECRET_', json.dumps(events))
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn(b'audit_invalid_line', self.bus.events_path.read_bytes())

    def test_cli_status_warns_but_still_displays_valid_events_without_raw_line(self):
        self.append(b'{"event":"post","id":"valid"}\n{"SECRET_CORRUPT":\n\xffSECRET_BYTES\n')
        before = self.snapshot()
        run = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'agentbus', 'status',
                              '--workspace', str(self.cfg.workspace)], capture_output=True,
                             env={**os.environ, 'PYTHONPATH': str(_ROOT_DIR)})
        self.assertEqual(run.returncode, 0, run.stderr.decode('utf-8'))
        text = run.stdout.decode('utf-8') + run.stderr.decode('utf-8')
        self.assertIn('审计异常', text)
        self.assertIn('post×1', text)
        self.assertNotIn('SECRET_', text)
        self.assertEqual(self.snapshot(), before)

    def test_partial_line_preserved_by_recovery_can_be_displayed_without_crashing(self):
        task = self.bus.make('codex', 'workbuddy', 'task', body='private work', result_to='codex')
        self.bus.post(task)
        self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        self.append(b'{"SECRET_RECOVERY_HALF":')
        self.bus.recover_claim('workbuddy', task.id, owner_confirmed_inactive=True)
        before = self.snapshot()
        run = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'agentbus', 'status',
                              '--workspace', str(self.cfg.workspace)], capture_output=True,
                             env={**os.environ, 'PYTHONPATH': str(_ROOT_DIR)})
        self.assertEqual(run.returncode, 0)
        self.assertIn('审计异常', run.stdout.decode('utf-8'))
        self.assertNotIn('SECRET_RECOVERY_HALF', run.stdout.decode('utf-8'))
        self.assertEqual(self.snapshot(), before)

    def test_active_job_is_blocked_when_review_completeness_cannot_be_established(self):
        job = start_job(self.cfg, 'workbuddy', 'Bounded goal', request_id='active')
        self.append(b'{"SECRET_LOST_REVIEW":\n')
        before = self.snapshot()
        status = status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('audit', status['reason'])
        self.assertIn('review completeness', status['reason'])
        self.assertNotIn('SECRET_', status['reason'])
        self.assertEqual(self.snapshot(), before)

    def test_health_warns_and_uses_same_blocked_job_reason_without_raw_text(self):
        job = start_job(self.cfg, 'workbuddy', 'Bounded goal', request_id='health-audit')
        self.append(b'{"SECRET_HEALTH_AUDIT":\n')
        before = self.snapshot()
        status = status_job(self.cfg, job['id'])
        report = inspect_project(self.cfg)
        self.assertEqual(report['status'], 'attention')
        audit = next(row for row in report['checks'] if row['code'] == 'audit')
        self.assertEqual(audit['status'], 'attention')
        self.assertIn('保留证据', audit['message'])
        observed = next(row for row in report['jobs'] if row['id'] == job['id'])
        self.assertEqual(observed['state'], 'blocked')
        self.assertEqual(observed['reason'], status['reason'])
        self.assertNotIn('SECRET_', json.dumps(report))
        self.assertEqual(self.snapshot(), before)

    def test_previously_completed_job_is_not_declared_accepted_after_invalid_audit(self):
        job, task = self.completed_tasks()
        self.assertEqual(finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)['state'], 'completed')
        self.append(b'[]\n')
        before = self.snapshot()
        self.assertEqual(status_job(self.cfg, job['id'])['state'], 'blocked')
        with self.assertRaises(JobError):
            finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)
        self.assertEqual(self.snapshot(), before)

    def test_bad_utf8_blocks_finalization_without_modifying_any_record(self):
        job, task = self.completed_tasks()
        self.append(b'\xffSECRET_UTF8\n')
        before = self.snapshot()
        try:
            status = status_job(self.cfg, job['id'])
        except UnicodeError:
            self.fail('bad audit encoding should return blocked instead of raising')
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('audit', status['reason'])
        with self.assertRaises(JobError):
            finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)
        self.assertEqual(self.snapshot(), before)

    def test_valid_review_post_remains_pending_amid_invalid_audit_lines(self):
        job, task = self.completed_tasks()
        review = self.bus.make('codex', 'zcode', 'review', body='Independent bounded review',
                               reply_to=task.id, result_to='codex')
        self.bus.post(review)
        self.append(b'not-json\n')
        status = status_job(self.cfg, job['id'])
        self.assertEqual(status['pending_reviews'], [review.id])
        self.assertEqual(status['state'], 'blocked')

    def test_scheduling_peer_review_does_not_crash_or_count_generated_marker_as_done(self):
        job, task = self.completed_tasks()
        self.cfg.data['oversight']['review_every_n'] = 1
        self.append(b'not-json\n')
        director = Director(self.cfg, bus=self.bus)
        try:
            try:
                review = director.schedule_peer_review(task, 'trae', 'done')
            except ValueError:
                self.fail('a bad audit line should not crash review scheduling')
            self.assertIsNotNone(review)
            self.assertEqual(review.type, 'review')
            self.assertEqual(review.reply_to, task.id)
        finally:
            director.close()


if __name__ == '__main__':
    unittest.main()
