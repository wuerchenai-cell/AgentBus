"""Read-only formal-goal processing diagnostics; Codex, 2026-10-03."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from receipt_helpers import receipt_ticket
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.config import load
from agentbus.jobs import dispatch_job, start_job, status_job


class GoalProcessingVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = load(self.root, create=True)
        self.cfg.data['gates']['heartbeat_timeout_s'] = 10
        self.bus = Bus(self.root / '.agentbus')
        self.bus.init()

    def start(self, request='visibility', parent=None):
        return start_job(self.cfg, 'workbuddy', 'Inspect a bounded formal goal',
                         request_id=request, parent_job_id=parent)

    def claim_goal(self, job):
        path = self.bus.dirs('codex')['ready'] / f"{job['goal_id']}.json"
        goal = self.bus.claim('codex', path)
        self.assertIsNotNone(goal)
        return goal, self.bus.dirs('codex')['processing'] / path.name

    def write_fixture(self, path, values):
        data = json.loads(path.read_text(encoding='utf-8'))
        data.update(values)
        path.write_text(json.dumps(data), encoding='utf-8')

    def snapshot(self):
        return {str(path.relative_to(self.root)): (
                    hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in self.root.rglob('*') if path.is_file()}

    def beat(self, original, *, at=None):
        from agentbus import heartbeat
        return heartbeat.record(self.bus.root, original.id, original.to, receipt_ticket(original),
                                task_record=original.to_dict(), now=at)

    def test_fresh_claimed_goal_reports_owner_age_and_complete_claim(self):
        job = self.start()
        goal, path = self.claim_goal(job)
        self.beat(goal)
        before = self.snapshot()
        status = status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'working')
        self.assertEqual(status.get('goal_status'), 'claimed')
        self.assertEqual(status.get('goal_claimed_by'), 'codex')
        self.assertEqual(status.get('goal_claimed_at'), goal.claimed_at)
        self.assertTrue(status.get('goal_claim_complete'))
        self.assertFalse(status.get('goal_heartbeat_stale'))
        self.assertGreaterEqual(status.get('goal_heartbeat_age_s', -1), 0)
        self.assertLess(status['goal_heartbeat_age_s'], 10)
        self.assertEqual(self.snapshot(), before)

    def test_stale_goal_is_blocked_but_age_does_not_prove_process_dead(self):
        job = self.start()
        goal, path = self.claim_goal(job)
        age = time.time() - 60
        self.beat(goal, at=age)
        os.utime(path, (age, age))
        before = self.snapshot()
        status = status_job(self.cfg, job['id'])
        self.assertTrue(status.get('goal_heartbeat_stale'))
        self.assertGreaterEqual(status['goal_heartbeat_age_s'], 59)
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('owner', status['reason'])
        self.assertIn('does not prove the process is dead', status['reason'])
        self.assertIn('do not automatically restart', status['reason'])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.bus.dirs('codex')['ready'] / path.name).exists())

    def test_processing_pending_move_is_an_incomplete_claim_not_normal_working(self):
        job = self.start()
        ready = self.bus.dirs('codex')['ready'] / f"{job['goal_id']}.json"
        processing = self.bus.dirs('codex')['processing'] / ready.name
        os.replace(ready, processing)  # Simulate interruption before claim metadata commit.
        before = self.snapshot()
        status = status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertEqual(status.get('goal_status'), 'pending')
        self.assertEqual(status.get('goal_claimed_by'), '')
        self.assertFalse(status.get('goal_claim_complete', True))
        self.assertIn('incomplete claim', status['reason'])
        self.assertIn('owner', status['reason'])
        self.assertIn('do not automatically restart', status['reason'])
        self.assertEqual(self.snapshot(), before)

    def test_claimed_or_running_goal_without_owner_or_time_is_not_complete(self):
        for index, values in enumerate(({'status': 'claimed', 'claimed_by': ''},
                       {'status': 'running', 'claimed_at': ''},
                       {'status': 'claimed', 'claimed_by': '  '},
                       {'status': 'running', 'claimed_at': 42})):
            with self.subTest(values=values):
                self.setUp()  # A deliberately incomplete live claim must not occupy the next case's slot.
                job = self.start('partial-' + str(index))
                goal, path = self.claim_goal(job)
                self.write_fixture(path, values)
                before = self.snapshot()
                status = status_job(self.cfg, job['id'])
                self.assertEqual(status['state'], 'blocked')
                self.assertFalse(status.get('goal_claim_complete', True))
                self.assertIn('incomplete claim', status['reason'])
                self.assertEqual(self.snapshot(), before)

    def test_fresh_running_goal_with_metadata_remains_working(self):
        job = self.start()
        goal, path = self.claim_goal(job)
        self.write_fixture(path, {'status': 'running'})
        self.beat(goal)
        status = status_job(self.cfg, job['id'])
        self.assertEqual(status.get('goal_status'), 'running')
        self.assertTrue(status.get('goal_claim_complete'))
        self.assertEqual(status['state'], 'working')

    def test_expired_goal_is_blocked_even_when_job_budget_remains(self):
        for location in ('ready', 'processing'):
            with self.subTest(location=location):
                job = self.start('expired-' + location)
                if location == 'processing':
                    _, path = self.claim_goal(job)
                else:
                    path = self.bus.dirs('codex')['ready'] / f"{job['goal_id']}.json"
                self.write_fixture(path, {'deadline_ts': time.time() - 1})
                before = self.snapshot()
                status = status_job(self.cfg, job['id'])
                self.assertGreater(status['remaining_s'], 0)
                self.assertEqual(status['state'], 'blocked')
                self.assertIn('goal', status['reason'])
                self.assertIn('deadline expired', status['reason'])
                self.assertIn('do not automatically restart', status['reason'])
                self.assertEqual(self.snapshot(), before)

    def test_goal_diagnostics_are_present_without_processing(self):
        job = self.start()
        ready = status_job(self.cfg, job['id'])
        self.assertEqual(ready.get('goal_status'), 'pending')
        self.assertEqual(ready.get('goal_heartbeat_age_s'), 0)
        self.assertFalse(ready.get('goal_heartbeat_stale', True))
        goal = self.bus.find_message(job['goal_id'])
        self.bus.finish('codex', goal, 'done', ticket=receipt_ticket(goal))
        done = status_job(self.cfg, job['id'])
        self.assertEqual(done.get('goal_status'), 'done')
        self.assertEqual(done.get('goal_heartbeat_age_s'), 0)
        self.assertFalse(done.get('goal_heartbeat_stale', True))
        self.assertEqual(done['state'], 'awaiting_summary')

    def test_stale_goal_cannot_hide_behind_a_fresh_processing_task(self):
        job = self.start()
        job = dispatch_job(self.cfg, 'codex', job['id'], [
            {'worker': 'trae', 'body': 'Review one bounded file', 'result_to': 'codex'}])
        goal, goal_path = self.claim_goal(job)
        age = time.time() - 60
        self.beat(goal, at=age)
        os.utime(goal_path, (age, age))
        task_path = self.bus.dirs('trae')['ready'] / f"{job['tasks'][0]['id']}.json"
        claimed_task = self.bus.claim('trae', task_path)
        from agentbus import heartbeat as H
        H.record(self.bus.root, claimed_task.id, 'trae', receipt_ticket(claimed_task), task_record=claimed_task.to_dict())
        before = self.snapshot()
        status = status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertTrue(status.get('goal_heartbeat_stale'))
        self.assertFalse(status['tasks'][0]['heartbeat_stale'])
        self.assertEqual(self.snapshot(), before)

    def test_touching_goal_file_without_explicit_heartbeat_is_not_execution_proof(self):
        job = self.start()
        goal, path = self.claim_goal(job)
        os.utime(path, None)
        before = self.snapshot()
        observed = status_job(self.cfg, job['id'])
        self.assertEqual(observed['state'], 'blocked')
        self.assertTrue(observed['goal_heartbeat_stale'])
        self.assertEqual(observed['goal_heartbeat_source'], 'none')
        self.assertEqual(self.snapshot(), before)

    def test_repeated_queries_preserve_parent_budget_attempts_and_all_files(self):
        parent = self.start('parent')
        parent_path = self.root / '.agentbus/jobs' / f"{parent['id']}.json"
        self.write_fixture(parent_path, {'deadline_ts': time.time() - 1})
        child = self.start('child', parent['id'])
        _, path = self.claim_goal(child)
        self.write_fixture(path, {'attempt': 2, 'budget_s': 77})
        age = time.time() - 60
        os.utime(path, (age, age))
        before = self.snapshot()
        for _ in range(3):
            status = status_job(self.cfg, child['id'])
            self.assertEqual(status['state'], 'blocked')
            self.assertEqual(status.get('goal_claimed_by'), 'codex')
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
