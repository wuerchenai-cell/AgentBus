"""Persistent team job contracts. Source: Codex jobs worker, 2026-10-02."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agentbus.bus import Bus
from agentbus.config import load


class JobFeaturePresenceTests(unittest.TestCase):
    def test_persistent_job_api_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('agentbus.jobs'),
                             'persistent jobs API is not implemented')


@unittest.skipUnless(importlib.util.find_spec('agentbus.jobs'), 'API not implemented yet')
class PersistentJobTests(unittest.TestCase):
    def setUp(self):
        from agentbus import jobs
        self.jobs = jobs
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load(Path(self.tmp.name), create=True)
        self.cfg.data['workers']['opencode']['enabled'] = True
        self.cfg.data['workers']['opencode']['executor_enabled'] = True
        self.bus = Bus(self.cfg.workspace / '.agentbus')
        self.bus.init()

    def start(self, caller='workbuddy', request_id='request-1'):
        return self.jobs.start_job(self.cfg, caller, 'Complete a bounded real file task',
                                   request_id=request_id)

    def plan(self, worker='opencode'):
        return [{'worker': worker, 'title': 'file', 'body': 'Write and verify own output',
                 'result_to': 'codex'}]

    def test_optional_artifact_manifest_is_still_verified_when_present(self):
        artifact_path = self.cfg.output_root / 'optional-result.txt'
        payload = b'optional artifact still needs integrity verification'
        artifact_path.write_bytes(payload)
        artifact = {
            'path': 'optional-result.txt',
            'bytes': len(payload),
            'sha256': '0' * 64,
        }
        receipt = self.bus.make(
            'opencode', 'codex', 'result', body='completed',
            reply_to='m-1791945999999-abcdef')
        receipt.artifacts = [artifact]

        error = self.jobs._artifact_error(
            self.cfg,
            {'id': receipt.reply_to, 'expected_artifacts': []},
            receipt,
        )

        self.assertIn('artifact manifest mismatch', error)

    def test_empty_optional_artifact_manifest_remains_valid(self):
        receipt = self.bus.make(
            'opencode', 'codex', 'result', body='completed',
            reply_to='m-1791946000000-abcdef')
        empty_error = self.jobs._artifact_error(
            self.cfg,
            {'id': receipt.reply_to, 'expected_artifacts': []},
            receipt,
        )
        self.assertEqual(empty_error, '')

        artifact_path = self.cfg.output_root / 'optional-valid-result.txt'
        payload = b'optional artifacts are allowed when verified'
        artifact_path.write_bytes(payload)
        receipt.artifacts = [{
            'path': artifact_path.name,
            'bytes': len(payload),
            'sha256': hashlib.sha256(payload).hexdigest(),
        }]
        valid_error = self.jobs._artifact_error(
            self.cfg,
            {'id': receipt.reply_to, 'expected_artifacts': []},
            receipt,
        )

        self.assertEqual(valid_error, '')

    def dispatched(self, worker='opencode'):
        job = self.start()
        return self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan(worker))

    def finish_task(self, job, recipient='codex', exit_code=0, receipt=True, artifacts=None):
        task = self.bus.find_message(job['tasks'][0]['id'])
        task.exit_code = exit_code
        task.result = 'verified file'
        self.bus.finish(task.to, task, 'done' if exit_code == 0 else 'failed')
        if receipt:
            result = self.bus.make(task.to, recipient, 'result', body=task.result,
                                   reply_to=task.id)
            result.exit_code = exit_code
            result.artifacts = list(artifacts or [])
            self.bus.post(result)
        return task

    def test_every_selected_member_can_initiate_formal_goal(self):
        for caller in ('codex', 'workbuddy', 'trae', 'zcode', 'opencode'):
            job = self.start(caller, 'once-' + caller)
            goal = self.bus.find_message(job['goal_id'])
            self.assertEqual((goal.sender, goal.to, goal.result_to), (caller, 'codex', caller))
            self.assertEqual(goal.type, 'goal')
            self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['state'], 'queued')

    def test_start_requires_selected_member_and_safe_request_id(self):
        for caller, request in [('stranger', 'okay'), ('workbuddy', '../escape'),
                                ('workbuddy', '汉字'), ('workbuddy', '')]:
            with self.subTest(caller=caller, request=request):
                with self.assertRaises(self.jobs.JobError):
                    self.start(caller, request)
        self.assertEqual(self.jobs.list_jobs(self.cfg), [])

    def test_idempotent_request_survives_reload_and_rejects_changed_goal(self):
        first = self.start()
        again = self.start()
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(len(self.bus.pending('codex')), 1)
        with self.assertRaises(self.jobs.JobError):
            self.jobs.start_job(self.cfg, 'workbuddy', 'different goal', request_id='request-1')
        self.assertEqual(len(self.jobs.list_jobs(self.cfg)), 1)

    def test_concurrent_identical_requests_publish_one_goal(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: self.start(), range(4)))
        self.assertEqual(len({job['id'] for job in jobs}), 1)
        self.assertEqual(len(self.bus.pending('codex')), 1)

    def test_start_recovers_precommitted_goal_after_post_failure(self):
        real_post = Bus.post
        with patch.object(Bus, 'post', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                self.start()
        job = self.start()
        self.assertIsNotNone(self.bus.find_message(job['goal_id']))
        self.assertEqual(len(self.bus.pending('codex')), 1)

    def test_stop_rejects_start_and_dispatch_and_has_visible_state(self):
        job = self.start()
        (self.cfg.workspace / '.agentbus' / 'STOP').write_text('operator stop', encoding='utf-8')
        with self.assertRaises(self.jobs.JobError):
            self.jobs.start_job(self.cfg, 'trae', 'another goal')
        with self.assertRaises(self.jobs.JobError):
            self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan())
        self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['state'], 'stopped')
        self.assertFalse(self.bus.pending('opencode'))

    def test_nonleader_cannot_dispatch_and_bad_plan_has_no_partial_posts(self):
        job = self.start()
        with self.assertRaises(self.jobs.JobError):
            self.jobs.dispatch_job(self.cfg, 'workbuddy', job['id'], self.plan())
        invalid = self.plan() + [{'worker': 'trae', 'body': 'valid body'}]
        with self.assertRaises(self.jobs.JobError):
            self.jobs.dispatch_job(self.cfg, 'codex', job['id'], invalid)
        self.assertFalse(self.bus.pending('opencode'))
        self.assertFalse(self.bus.pending('trae'))
        self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['tasks'], [])

    def test_task_count_gate_rejects_whole_plan_before_posting(self):
        job = self.start()
        self.cfg.data['gates']['max_tasks'] = 1
        with self.assertRaises(self.jobs.JobError):
            self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan() * 2)
        self.assertFalse(self.bus.pending('opencode'))

    def test_dispatch_is_idempotent_and_results_keep_original_task_reference(self):
        job = self.dispatched()
        again = self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan())
        self.assertEqual(job['tasks'], again['tasks'])
        self.assertEqual(len(self.bus.pending('opencode')), 1)
        task = self.bus.find_message(job['tasks'][0]['id'])
        self.assertEqual(task.reply_to, job['goal_id'])
        with self.assertRaises(self.jobs.JobError):
            self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan('trae'))

    def test_partial_dispatch_recovers_missing_task_without_duplication(self):
        job = self.start()
        plan = self.plan() + self.plan('trae')
        real_post = Bus.post
        calls = []
        def failing_post(bus, msg):
            calls.append(msg.id)
            if len(calls) == 2:
                raise OSError('simulated second-file failure')
            return real_post(bus, msg)
        with patch.object(Bus, 'post', failing_post):
            with self.assertRaises(OSError):
                self.jobs.dispatch_job(self.cfg, 'codex', job['id'], plan)
        partial = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(partial['state'], 'blocked')
        recovered = self.jobs.dispatch_job(self.cfg, 'codex', job['id'], plan)
        self.assertEqual(len(recovered['tasks']), 2)
        self.assertEqual(len(self.bus.pending('opencode')), 1)
        self.assertEqual(len(self.bus.pending('trae')), 1)

    def test_pending_manual_task_needs_handoff_and_claimed_is_working(self):
        job = self.dispatched('trae')
        self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['state'], 'waiting_handoff')
        task = self.bus.claim('trae', self.bus.pending('trae')[0])
        from agentbus.claim_fencing import captured_ticket
        self.bus.heartbeat('trae', task, captured_ticket(task), note='explicit test executor heartbeat')
        self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['state'], 'working')

    def test_empty_queue_does_not_mean_success_and_task_without_receipt_is_blocked(self):
        job = self.dispatched()
        self.finish_task(job, receipt=False)
        state = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(state['state'], 'blocked')
        self.assertIn('result', state['reason'])
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'looks done', verified=True)

    def test_failure_and_wrong_route_remain_visible_and_cannot_be_finalized(self):
        for failure, wrong_route in [(1, False), (0, True)]:
            job = self.jobs.start_job(self.cfg, 'workbuddy', f'goal-{failure}-{wrong_route}')
            job = self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan())
            self.finish_task(job, recipient='trae' if wrong_route else 'codex', exit_code=failure)
            state = self.jobs.status_job(self.cfg, job['id'])
            self.assertEqual(state['state'], 'blocked')
            with self.assertRaises(self.jobs.JobError):
                self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'do not guess', verified=True)

    def test_pending_peer_review_prevents_finalization(self):
        job = self.dispatched()
        task = self.finish_task(job)
        review = self.bus.make('codex', 'zcode', 'review', body='independent check',
                               reply_to=task.id, result_to='codex')
        self.bus.post(review)
        state = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(state['state'], 'awaiting_summary')
        self.assertEqual(state['pending_reviews'], [review.id])
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'summary', verified=True)

    def test_completed_requires_explicit_verification_and_formal_goal_receipt(self):
        job = self.dispatched()
        self.finish_task(job)
        self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['state'], 'awaiting_summary')
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'summary')
        result = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'checked exact artifact',
                                        verified=True)
        self.assertEqual(result['state'], 'completed')
        receipts = self.bus.find_results(job['goal_id'])
        self.assertEqual(len(receipts), 1)
        self.assertEqual((receipts[0].sender, receipts[0].to), ('codex', 'workbuddy'))
        self.assertEqual(self.bus.find_message(job['goal_id']).status, 'done')
        again = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'checked exact artifact',
                                       verified=True)
        self.assertEqual(again['state'], 'completed')
        self.assertEqual(len(self.bus.find_results(job['goal_id'])), 1)

    def test_optional_manifest_mismatch_blocks_status_and_finalize(self):
        artifact_path = self.cfg.output_root / 'optional-job-artifact.txt'
        payload = b'present but receipt hash is wrong'
        artifact_path.write_bytes(payload)
        bad_manifest = [{'path': artifact_path.name, 'bytes': len(payload),
                         'sha256': '0' * 64}]
        job = self.dispatched()
        self.finish_task(job, artifacts=bad_manifest)

        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('artifact manifest mismatch', status['reason'])

        # Exercise the finalization guard independently of status_job's earlier
        # rejection: finalization must not trust an optional manifest either.
        with patch.object(self.jobs, 'status_job', return_value={
                'state': 'awaiting_summary', 'pending_reviews': []}):
            with self.assertRaisesRegex(self.jobs.JobError, 'artifact manifest mismatch'):
                self.jobs.finalize_job(self.cfg, 'codex', job['id'],
                                       'must not finalize unverified artifacts', verified=True)

    def test_processing_goal_cannot_be_stolen_for_finalization(self):
        job = self.dispatched()
        self.finish_task(job)
        self.bus.claim('codex', self.bus.pending('codex')[0])
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'summary', verified=True)
        self.assertEqual(self.bus.find_message(job['goal_id']).status, 'claimed')
        self.assertFalse(self.bus.find_results(job['goal_id']))

    def test_checkpoint_survives_new_config_object_without_model_or_budget_reset(self):
        job = self.start()
        note = 'Next: verify UTF-8 artifact; waiting for a GUI peer'
        self.jobs.checkpoint_job(self.cfg, 'zcode', job['id'], note)
        resumed_cfg = load(self.cfg.workspace)
        status = self.jobs.status_job(resumed_cfg, job['id'])
        self.assertEqual(status['checkpoints'][-1]['note'], note)
        self.assertEqual(status['deadline_ts'], job['deadline_ts'])
        self.assertNotEqual(status['state'], 'completed')

    def test_wall_clock_budget_expires_across_restart_and_rejects_more_dispatch(self):
        job = self.start()
        path = self.cfg.workspace / '.agentbus' / 'jobs' / f"{job['id']}.json"
        record = json.loads(path.read_text(encoding='utf-8'))
        record['deadline_ts'] = time.time() - 1
        path.write_text(json.dumps(record), encoding='utf-8')
        state = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(state['state'], 'blocked')
        self.assertIn('budget', state['reason'])
        with self.assertRaises(self.jobs.JobError):
            self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan())
        self.assertFalse(self.bus.pending('opencode'))

    def test_expired_pending_task_is_visible_without_attempting_model_execution(self):
        job = self.dispatched()
        task = self.bus.find_message(job['tasks'][0]['id'])
        task.deadline_ts = time.time() - 1
        self.bus._write(self.bus.dirs(task.to)['ready'] / f'{task.id}.json', task)
        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('deadline', status['reason'])

    def test_member_removed_mid_job_does_not_appear_runnable(self):
        job = self.dispatched()
        self.cfg.data['workers']['opencode']['enabled'] = False
        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('selected', status['reason'])

    def test_existing_formal_goal_result_is_reused_without_second_summary(self):
        job = self.dispatched()
        self.finish_task(job)
        goal = self.bus.find_message(job['goal_id'])
        goal.result = 'checked exact artifact'
        goal.exit_code = 0
        self.bus.finish(goal.to, goal)
        receipt = self.bus.make('codex', 'workbuddy', 'result', body=goal.result,
                                 reply_to=goal.id)
        receipt.exit_code = 0
        self.bus.post(receipt)
        status = self.jobs.finalize_job(self.cfg, 'codex', job['id'], goal.result, verified=True)
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(len(self.bus.find_results(goal.id)), 1)

    def test_existing_goal_result_with_other_summary_is_rejected_without_duplicate(self):
        job = self.dispatched()
        self.finish_task(job)
        goal = self.bus.find_message(job['goal_id'])
        self.bus.finish(goal.to, goal)
        self.bus.post(self.bus.make('codex', 'workbuddy', 'result', body='original statement',
                                    reply_to=goal.id))
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'different statement', verified=True)
        self.assertEqual(len(self.bus.find_results(goal.id)), 1)

    def test_stop_during_dispatch_preserves_allocated_ids_for_resume(self):
        job = self.start()
        plan = self.plan() + self.plan('trae')
        real_post = Bus.post
        def stop_after_post(bus, msg):
            result = real_post(bus, msg)
            (self.cfg.workspace / '.agentbus' / 'STOP').touch()
            return result
        with patch.object(Bus, 'post', stop_after_post):
            with self.assertRaises(self.jobs.JobError):
                self.jobs.dispatch_job(self.cfg, 'codex', job['id'], plan)
        old_ids = [task['id'] for task in self.jobs.status_job(self.cfg, job['id'])['tasks']]
        (self.cfg.workspace / '.agentbus' / 'STOP').unlink()
        resumed = self.jobs.dispatch_job(self.cfg, 'codex', job['id'], plan)
        self.assertEqual([task['id'] for task in resumed['tasks']], old_ids)
        self.assertEqual(len(self.bus.pending('opencode')), 1)
        self.assertEqual(len(self.bus.pending('trae')), 1)

    def test_finalize_recovers_after_summary_post_fails_after_goal_archive(self):
        job = self.dispatched()
        self.finish_task(job)
        real_post = Bus.post
        def result_disk_failure(bus, msg):
            if msg.type == 'result':
                raise OSError('summary publication interrupted')
            return real_post(bus, msg)
        with patch.object(Bus, 'post', result_disk_failure):
            with self.assertRaises(OSError):
                self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)
        self.assertNotEqual(self.jobs.status_job(self.cfg, job['id'])['state'], 'completed')
        result = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(len(self.bus.find_results(job['goal_id'])), 1)

    def test_stale_processing_claim_is_visible_without_stealing_it(self):
        job = self.dispatched('trae')
        task = self.bus.claim('trae', self.bus.pending('trae')[0])
        path = self.bus.dirs('trae')['processing'] / f'{task.id}.json'
        self.cfg.data['gates']['heartbeat_timeout_s'] = 10
        os.utime(path, (time.time() - 60, time.time() - 60))
        status = self.jobs.status_job(self.cfg, job['id'])
        detail = status['tasks'][0]
        self.assertTrue(detail['heartbeat_stale'])
        self.assertEqual(detail['claimed_by'], 'trae')
        self.assertTrue(detail['claimed_at'])
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('manual', status['reason'])
        self.assertTrue(path.exists())
        self.assertFalse(self.bus.pending('trae'))

    def test_peer_checkpoint_does_not_grant_finalization_authority(self):
        job = self.dispatched()
        self.finish_task(job)
        self.jobs.checkpoint_job(self.cfg, 'trae', job['id'], 'Peer verified independent evidence')
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'trae', job['id'], 'take over summary', verified=True)
        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'awaiting_summary')
        self.assertIsNone(status['finalization'])

    def test_cross_process_identical_requests_publish_one_goal(self):
        script = (
            'import json,sys; from agentbus.config import load; '
            'from agentbus.jobs import start_job; '
            "j=start_job(load(sys.argv[1]),'workbuddy','one durable request',request_id='cross-process'); "
            "print(j['id'])"
        )
        sub_env = {**os.environ, 'PYTHONPATH': str(_ROOT_DIR)}
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.cfg.workspace)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=sub_env) for _ in range(4)]
        outputs = [process.communicate(timeout=15) for process in processes]
        self.assertEqual([p.returncode for p in processes], [0, 0, 0, 0], outputs)
        self.assertEqual(len({out.decode('utf-8').strip() for out, _ in outputs}), 1)
        self.assertEqual(len(self.bus.pending('codex')), 1)

    def test_wrong_formal_task_intent_is_not_counted_as_completed(self):
        job = self.dispatched()
        task = self.bus.find_message(job['tasks'][0]['id'])
        task.body = 'different intent with reused task id'
        task.exit_code = 0
        self.bus.finish(task.to, task)
        self.bus.post(self.bus.make(task.to, 'codex', 'result', body='I did something else',
                                    reply_to=task.id))
        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('intent', status['reason'])

    def test_finalization_can_resume_after_stop_between_goal_archive_and_result(self):
        job = self.dispatched()
        self.finish_task(job)
        real_finish = Bus.finish_if_ready
        def stop_after_archive(bus, agent, msg, status='done'):
            result = real_finish(bus, agent, msg, status)
            (self.cfg.workspace / '.agentbus' / 'STOP').touch()
            return result
        with patch.object(Bus, 'finish_if_ready', stop_after_archive):
            with self.assertRaises(self.jobs.JobError):
                self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)
        self.assertFalse(self.bus.find_results(job['goal_id']))
        self.assertNotEqual(self.jobs.status_job(self.cfg, job['id'])['state'], 'completed')
        (self.cfg.workspace / '.agentbus' / 'STOP').unlink()
        resumed = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)
        self.assertEqual(resumed['state'], 'completed')

    def test_crash_after_bus_publish_but_before_job_commit_does_not_duplicate(self):
        job = self.start()
        real_post = Bus.post
        def fail_after_post(bus, msg):
            result = real_post(bus, msg)
            raise OSError('process died after mailbox replace')
        with patch.object(Bus, 'post', fail_after_post):
            with self.assertRaises(OSError):
                self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan())
        recovered = self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan())
        self.assertEqual(len(recovered['tasks']), 1)
        self.assertEqual(len(self.bus.pending('opencode')), 1)

    def test_unsafe_job_path_cannot_escape_jobs_directory(self):
        with self.assertRaises(self.jobs.JobError):
            self.jobs.status_job(self.cfg, '../config')
        with self.assertRaises(self.jobs.JobError):
            self.jobs.checkpoint_job(self.cfg, 'codex', '../../config', 'escape')

    def test_removed_initiator_cannot_receive_silent_final_summary(self):
        job = self.dispatched()
        self.finish_task(job)
        self.cfg.data['workers']['workbuddy']['enabled'] = False
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)
        self.assertFalse(self.bus.find_results(job['goal_id']))
        self.assertEqual(self.bus.find_message(job['goal_id']).status, 'pending')

    def test_unreadable_task_json_is_visible_as_blocked(self):
        job = self.dispatched()
        path = self.bus.pending('opencode')[0]
        path.write_text('{broken json', encoding='utf-8')
        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('unreadable', status['reason'])

    def test_unreadable_associated_review_is_not_silently_accepted(self):
        job = self.dispatched()
        task = self.finish_task(job)
        review = self.bus.make('codex', 'zcode', 'review', body='independent check',
                               reply_to=task.id, result_to='codex')
        self.bus.post(review)
        path = self.bus.dirs('zcode')['ready'] / f'{review.id}.json'
        path.write_text('{broken json', encoding='utf-8')
        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['pending_reviews'], [review.id])
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)

    def test_incomplete_or_nonobject_audit_tail_does_not_crash_status(self):
        job = self.start()
        with self.bus.events_path.open('a', encoding='utf-8') as stream:
            stream.write('[]\n{"incomplete":')
        before = (self.bus.events_path.read_bytes(), self.bus.events_path.stat().st_mtime_ns)
        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['state'], 'blocked')
        self.assertIn('audit', status['reason'])
        self.assertIn('review completeness', status['reason'])
        self.assertEqual((self.bus.events_path.read_bytes(), self.bus.events_path.stat().st_mtime_ns), before)

    def review_for_finished_task(self, job):
        task = self.finish_task(job)
        review = self.bus.make('codex', 'zcode', 'review', body='independent check',
                               reply_to=task.id, result_to='codex')
        self.bus.post(review)
        self.bus.finish('zcode', review)
        return review

    def test_done_review_without_formal_receipt_still_blocks_completion(self):
        job = self.dispatched()
        review = self.review_for_finished_task(job)
        status = self.jobs.status_job(self.cfg, job['id'])
        self.assertEqual(status['pending_reviews'], [review.id])
        with self.assertRaises(self.jobs.JobError):
            self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)

    def test_done_review_wrong_route_or_failed_result_still_blocks_completion(self):
        for recipient, code in [('trae', 0), ('codex', 1)]:
            with self.subTest(recipient=recipient, code=code):
                job = self.jobs.start_job(self.cfg, 'workbuddy', 'bounded review case',
                                          request_id=f'review-{recipient}-{code}')
                job = self.jobs.dispatch_job(self.cfg, 'codex', job['id'], self.plan())
                review = self.review_for_finished_task(job)
                result = self.bus.make('zcode', recipient, 'result', body='review actual conclusion',
                                       reply_to=review.id)
                result.exit_code = code
                self.bus.post(result)
                status = self.jobs.status_job(self.cfg, job['id'])
                self.assertEqual(status['pending_reviews'], [review.id])
                with self.assertRaises(self.jobs.JobError):
                    self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)

    def test_done_review_multiple_results_requires_clarification(self):
        job = self.dispatched()
        review = self.review_for_finished_task(job)
        for _ in range(2):
            self.bus.post(self.bus.make('zcode', 'codex', 'result', body='review conclusion',
                                        reply_to=review.id))
        self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['pending_reviews'], [review.id])

    def test_done_review_with_unique_correct_result_allows_explicit_finalization(self):
        job = self.dispatched()
        review = self.review_for_finished_task(job)
        self.bus.post(self.bus.make('zcode', 'codex', 'result', body='reviewed artifact evidence',
                                    reply_to=review.id))
        self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['pending_reviews'], [])
        completed = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)
        self.assertEqual(completed['state'], 'completed')

    def test_coordinator_periodic_review_is_queued_before_announcing_completion(self):
        from agentbus.director import Director
        from agentbus.mcp_server import AgentBusMcp
        for index in range(2):
            prior = self.bus.make('workbuddy', 'codex', 'task', body=f'prior completed task {index}',
                                  result_to='workbuddy')
            self.bus.post(prior)
            prior.result = 'prior artifact evidence'
            prior.exit_code = 0
            self.bus.finish('codex', prior)
        job = self.dispatched()
        self.finish_task(job)
        status = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)
        self.assertEqual(status['state'], 'awaiting_summary')
        self.assertEqual(len(status['pending_reviews']), 1)
        self.assertFalse(status['finalization']['committed'])
        self.assertFalse(self.bus.find_results(job['goal_id']))
        review = self.bus.find_message(status['pending_reviews'][0])
        self.assertNotEqual(review.to, 'codex')
        self.assertEqual(review.reply_to, job['goal_id'])
        self.bus.finish(review.to, review)
        self.bus.post(self.bus.make(review.to, 'codex', 'result', body='coordinator evidence reviewed',
                                    reply_to=review.id))
        completed = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)
        self.assertEqual(completed['state'], 'completed')
        director = Director(self.cfg, bus=self.bus)
        self.addCleanup(director.close)
        self.assertIsNone(director.schedule_peer_review(self.bus.find_message(job['goal_id']),
                                                        'codex', 'done'))
        events = [event for event in self.bus.events()
                  if event.get('event') == 'peer_review_queued' and
                  event.get('source_task_id') == job['goal_id']]
        self.assertEqual(len(events), 1)
        client = AgentBusMcp(self.cfg.workspace, identity='workbuddy')
        self.addCleanup(client.director.close)
        received = client.next_task('workbuddy', message_types=['result'])
        self.assertIn(job['goal_id'], received)
        self.assertEqual(self.jobs.status_job(self.cfg, job['id'])['state'], 'completed')
        events = [event for event in self.bus.events()
                  if event.get('event') == 'peer_review_queued' and
                  event.get('source_task_id') == job['goal_id']]
        self.assertEqual(len(events), 1)

    def test_busy_review_scheduler_does_not_allow_early_completion(self):
        from agentbus.lease import LeaseManager
        for index in range(2):
            prior = self.bus.make('workbuddy', 'codex', 'task', body=f'prior {index}',
                                  result_to='workbuddy')
            self.bus.post(prior)
            self.bus.finish('codex', prior)
        job = self.dispatched()
        self.finish_task(job)
        manager = LeaseManager(self.bus.root)
        lease = manager.acquire(f"peer-review/{job['goal_id']}", 'concurrent-reviewer', ttl_s=60)
        self.assertIsNotNone(lease)
        try:
            status = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)
            self.assertEqual(status['state'], 'awaiting_summary')
            self.assertFalse(status['finalization']['committed'])
            self.assertFalse(self.bus.find_results(job['goal_id']))
            self.assertIn('scheduling', status['reason'])
        finally:
            manager.release(lease)
        resumed = self.jobs.finalize_job(self.cfg, 'codex', job['id'], 'verified', verified=True)
        self.assertEqual(len(resumed['pending_reviews']), 1)


class JobContinuationTests(unittest.TestCase):
    """New budgets require an explicit linked job; never rewrite an old job."""
    setUp = PersistentJobTests.setUp
    start = PersistentJobTests.start
    plan = PersistentJobTests.plan
    finish_task = PersistentJobTests.finish_task
    def parent(self, request='parent', completed=False):
        self.cfg.data.setdefault('oversight', {})['review_every_n'] = 0
        parent = self.jobs.start_job(self.cfg, 'workbuddy', 'parent goal', request_id=request)
        parent = self.jobs.dispatch_job(self.cfg, 'codex', parent['id'], self.plan())
        self.jobs.checkpoint_job(self.cfg, 'opencode', parent['id'], 'verified file; next independent step')
        self.finish_task(parent)
        if completed:
            return self.jobs.finalize_job(self.cfg, 'codex', parent['id'], 'parent exact artifact checked', verified=True)
        path = self.jobs._path(self.cfg, parent['id'])
        value = json.loads(path.read_text(encoding='utf-8'))
        value['deadline_ts'] = time.time() - 1
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return self.jobs.status_job(self.cfg, parent['id'])

    def child(self, parent_id, request='child', cfg=None):
        try:
            return self.jobs.start_job(cfg or self.cfg, 'trae', 'next authorized bounded step',
                                       request_id=request, parent_job_id=parent_id)
        except TypeError as exc:
            self.fail('Explicit parent_job_id continuation API is missing: ' + str(exc))

    def test_dispatch_refuses_mechanical_copy_of_completed_parent_task(self):
        parent = self.parent(completed=True)
        child = self.child(parent['id'])
        before = self.jobs._path(self.cfg, child['id']).read_bytes()
        with self.assertRaisesRegex(self.jobs.JobError, 'completed parent task'):
            self.jobs.dispatch_job(self.cfg, 'codex', child['id'], self.plan('trae'))
        self.assertEqual(self.jobs._path(self.cfg, child['id']).read_bytes(), before)
        self.assertFalse(self.bus.pending('trae'))

    def test_dispatch_can_recheck_with_explicit_new_scope_and_acceptance_body(self):
        parent = self.parent(completed=True)
        child = self.child(parent['id'])
        plan = [{'worker': 'trae', 'body': 'Recheck the preserved artifact after relocation; do not rewrite it',
                 'title': 'New authorized verification', 'result_to': 'codex'}]
        result = self.jobs.dispatch_job(self.cfg, 'codex', child['id'], plan)
        self.assertEqual(len(result['tasks']), 1)

    def test_failed_parent_task_can_be_explicitly_retried(self):
        parent = self.jobs.start_job(self.cfg, 'workbuddy', 'parent failed goal', request_id='failed-parent')
        parent = self.jobs.dispatch_job(self.cfg, 'codex', parent['id'], self.plan())
        self.finish_task(parent, exit_code=1)
        child = self.child(parent['id'])
        result = self.jobs.dispatch_job(self.cfg, 'codex', child['id'], self.plan('trae'))
        self.assertEqual(len(result['tasks']), 1)

    def test_dispatch_preserves_malformed_parent_task_snapshot(self):
        parent = self.parent(completed=True)
        child = self.child(parent['id'])
        path = self.jobs._path(self.cfg, child['id'])
        for broken in ('bad states', [None], [{}], [{
                'id': 'm-finished', 'status': 'done', 'location': 'done',
                'body_sha256': 'not-a-sha256'}]):
            data = json.loads(path.read_text(encoding='utf-8'))
            data['parent_snapshot']['task_states'] = broken
            path.write_text(json.dumps(data), encoding='utf-8')
            before = path.read_bytes()
            with self.assertRaisesRegex(self.jobs.JobError, 'malformed'):
                self.jobs.dispatch_job(self.cfg, 'codex', child['id'], self.plan('trae'))
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(self.bus.pending('trae'))

    def test_completed_parent_creates_self_contained_new_goal_without_old_mutation(self):
        parent = self.parent(completed=True)
        paths = [self.jobs._path(self.cfg, parent['id'])]
        paths.extend(path for agent in self.bus.agents_dir.iterdir() if agent.is_dir()
                     for state in ('ready', 'processing', 'blocked', 'done')
                     for path in (agent / state).glob('*.json'))
        before = {path: path.read_bytes() for path in paths}
        child = self.child(parent['id'])
        self.assertEqual(child['parent_job_id'], parent['id'])
        self.assertEqual(child['tasks'], [])
        self.assertIsNone(child['plan'])
        self.assertIsNone(child['finalization'])
        self.assertEqual(child['checkpoints'], [])
        self.assertGreater(child['deadline_ts'], parent['deadline_ts'])
        snapshot = child['parent_snapshot']
        self.assertEqual(set(snapshot), {'id', 'goal', 'state', 'reason', 'done_task_ids',
                                         'task_states', 'latest_checkpoint', 'final_summary'})
        self.assertEqual(snapshot['done_task_ids'], [parent['tasks'][0]['id']])
        self.assertEqual(snapshot['state'], 'completed')
        self.assertEqual(snapshot['final_summary'], 'parent exact artifact checked')
        self.assertEqual(snapshot['latest_checkpoint']['caller'], 'opencode')
        self.assertEqual(snapshot['task_states'], [{'id': parent['tasks'][0]['id'],
                                                  'worker': 'opencode', 'status': 'done',
                                                  'location': 'done', 'body_sha256':
                                                  'f098b59c8fa4c4da0579f395b4371dd63336395aca05425f3a84b4c490d2a774'}])
        goal = self.bus.find_message(child['goal_id'])
        self.assertEqual(goal.result_to, 'trae')
        self.assertIn(parent['id'], goal.body)
        self.assertIn('不要重做', goal.body)
        self.assertIn('processing', goal.body)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_expired_parent_gets_new_budget_while_old_deadline_stays_expired(self):
        parent = self.parent()
        original = self.jobs._path(self.cfg, parent['id']).read_bytes()
        child = self.child(parent['id'])
        self.assertEqual(child['parent_snapshot']['state'], 'blocked')
        self.assertGreater(child['remaining_s'], 0)
        self.assertEqual(self.jobs._path(self.cfg, parent['id']).read_bytes(), original)
        self.assertEqual(self.jobs.status_job(self.cfg, parent['id'])['state'], 'blocked')

    def test_missing_or_foreign_parent_does_not_create_local_child(self):
        parent = self.parent(completed=True)
        with tempfile.TemporaryDirectory() as foreign:
            cfg = load(Path(foreign), create=True)
            with self.assertRaises(self.jobs.JobError):
                self.child(parent['id'], cfg=cfg)
            self.assertEqual(self.jobs.list_jobs(cfg), [])
        before = len(self.jobs.list_jobs(self.cfg))
        for identity in ('j-does-not-exist', '../escape', ''):
            with self.subTest(parent=identity), self.assertRaises(self.jobs.JobError):
                self.child(identity)
        self.assertEqual(len(self.jobs.list_jobs(self.cfg)), before)

    def test_active_parent_is_rejected(self):
        parent = self.start()
        with self.assertRaises(self.jobs.JobError):
            self.child(parent['id'])
        self.assertEqual(len(self.jobs.list_jobs(self.cfg)), 1)

    def test_pending_review_blocks_even_expired_parent(self):
        parent = self.parent()
        review = self.bus.make('codex', 'zcode', 'review', body='unclosed independent review',
                               reply_to=parent['tasks'][0]['id'], result_to='codex')
        self.bus.post(review)
        with self.assertRaises(self.jobs.JobError):
            self.child(parent['id'])
        self.assertEqual(self.bus.find_message(review.id).status, 'pending')

    def test_processing_goal_or_task_blocks_expired_parent_without_stealing(self):
        for target in ('goal', 'task'):
            with self.subTest(target=target):
                parent = self.jobs.start_job(self.cfg, 'workbuddy', 'claimed parent', request_id='p-' + target)
                parent = self.jobs.dispatch_job(self.cfg, 'codex', parent['id'], self.plan())
                message_id = parent['goal_id'] if target == 'goal' else parent['tasks'][0]['id']
                message = self.bus.find_message(message_id)
                path = self.bus.dirs(message.to)['ready'] / (message.id + '.json')
                self.bus.claim(message.to, path)
                record_path = self.jobs._path(self.cfg, parent['id'])
                record = json.loads(record_path.read_text(encoding='utf-8'))
                record['deadline_ts'] = time.time() - 1
                record_path.write_text(json.dumps(record), encoding='utf-8')
                processing = self.bus.dirs(message.to)['processing'] / (message.id + '.json')
                before = processing.read_bytes()
                with self.assertRaises(self.jobs.JobError):
                    self.child(parent['id'], request='c-' + target)
                self.assertEqual(processing.read_bytes(), before)

    def test_idempotent_request_reuses_snapshot_and_rejects_different_parent(self):
        parent = self.parent(completed=True)
        child = self.child(parent['id'])
        again = self.child(parent['id'])
        self.assertEqual((again['id'], again['goal_id'], again['parent_snapshot']),
                         (child['id'], child['goal_id'], child['parent_snapshot']))
        self.assertEqual(len([self.bus.read(p) for p in self.bus.pending('codex')
                              if self.bus.read(p).id == child['goal_id']]), 1)
        second = self.parent(request='second', completed=True)
        with self.assertRaises(self.jobs.JobError):
            self.child(second['id'])
        with self.assertRaises(self.jobs.JobError):
            self.jobs.start_job(self.cfg, 'trae', child['goal'], request_id='child')

    def test_stop_marker_prevents_continuation_and_failed_call_releases_leases(self):
        from agentbus.lease import LeaseManager
        parent = self.parent()
        stop = self.bus.root / 'STOP'
        stop.touch()
        with self.assertRaises(self.jobs.JobError):
            self.child(parent['id'])
        stop.unlink()
        active = self.start(request_id='active')
        with self.assertRaises(self.jobs.JobError):
            self.child(active['id'])
        manager = LeaseManager(self.bus.root)
        for identity in (parent['id'], active['id']):
            self.assertIsNone(manager.holder('.agentbus/jobs/' + identity + '.json'))
        lease_files = [p for p in manager.root.glob('*.json') if p.is_file()]
        self.assertEqual(lease_files, [])

    def test_copied_foreign_record_is_not_accepted_as_local_parent(self):
        parent = self.parent(completed=True)
        path = self.jobs._path(self.cfg, parent['id'])
        value = json.loads(path.read_text(encoding='utf-8'))
        value['workspace'] = str(self.cfg.workspace / 'other project')
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaises(self.jobs.JobError):
            self.child(parent['id'])

    def test_nonobject_parent_record_is_actionable_error_not_unhandled_exception(self):
        parent = self.parent()
        self.jobs._path(self.cfg, parent['id']).write_text('[]', encoding='utf-8')
        with self.assertRaises(self.jobs.JobError):
            self.child(parent['id'])

    def test_incomplete_parent_record_is_actionable_error_not_key_error(self):
        parent = self.parent()
        path = self.jobs._path(self.cfg, parent['id'])
        path.write_text(json.dumps({'schema_version': 1, 'id': parent['id']}), encoding='utf-8')
        with self.assertRaises(self.jobs.JobError):
            self.child(parent['id'])

    def test_terminal_processing_duplicate_still_prevents_continuation(self):
        parent = self.parent(completed=True)
        task = self.bus.find_message(parent['tasks'][0]['id'])
        done = self.bus.dirs(task.to)['done'] / f'{task.id}.json'
        duplicate = self.bus.dirs(task.to)['processing'] / done.name
        duplicate.write_bytes(done.read_bytes())
        with self.assertRaises(self.jobs.JobError):
            self.child(parent['id'])
        self.assertTrue(duplicate.is_file())

    def test_retry_after_goal_publish_failure_reuses_preallocated_child_snapshot(self):
        parent = self.parent(completed=True)
        with patch.object(Bus, 'post', side_effect=OSError('bounded publication failure')):
            with self.assertRaises(OSError):
                self.child(parent['id'])
        children = [j for j in self.jobs.list_jobs(self.cfg) if j['id'] != parent['id']]
        self.assertEqual(len(children), 1)
        saved = children[0]
        review = self.bus.make('codex', 'zcode', 'review', body='late review after allocated child',
                               reply_to=parent['goal_id'], result_to='codex')
        self.bus.post(review)
        recovered = self.child(parent['id'])
        self.assertEqual((recovered['id'], recovered['goal_id'], recovered['parent_snapshot']),
                         (saved['id'], saved['goal_id'], saved['parent_snapshot']))
        self.assertEqual(len(self.bus.find_results(parent['goal_id'])), 1)

    def test_parent_and_child_leases_acquire_in_fixed_sorted_order(self):
        from contextlib import contextmanager
        parent = self.parent(completed=True)
        original = self.jobs._locked
        acquired = []
        @contextmanager
        def observe(cfg, identity, caller):
            acquired.append(identity)
            with original(cfg, identity, caller):
                yield
        with patch.object(self.jobs, '_locked', observe):
            child = self.child(parent['id'])
        self.assertEqual(acquired, sorted([parent['id'], child['id']]))

    def test_parallel_child_calls_publish_one_goal_and_keep_parent_bytes(self):
        parent = self.parent(completed=True)
        before = self.jobs._path(self.cfg, parent['id']).read_bytes()
        with ThreadPoolExecutor(max_workers=4) as pool:
            children = list(pool.map(lambda _: self.child(parent['id']), range(4)))
        self.assertEqual(len({j['goal_id'] for j in children}), 1)
        self.assertEqual(self.jobs._path(self.cfg, parent['id']).read_bytes(), before)

    def test_fresh_process_child_requests_publish_one_associated_goal(self):
        parent = self.parent(completed=True)
        before = self.jobs._path(self.cfg, parent['id']).read_bytes()
        script = ('import sys; from agentbus.config import load; from agentbus.jobs import start_job; '
                  'j=start_job(load(sys.argv[1]),"trae","next independent step",'
                  'request_id="process-child",parent_job_id=sys.argv[2]); print(j["goal_id"])')
        sub_env = {**os.environ, 'PYTHONPATH': str(_ROOT_DIR)}
        processes = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script,
                                       str(self.cfg.workspace), parent['id']],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=sub_env)
                     for _ in range(4)]
        outputs = [process.communicate(timeout=25) for process in processes]
        self.assertEqual([process.returncode for process in processes], [0] * 4, outputs)
        self.assertEqual(len({stdout.strip() for stdout, _ in outputs}), 1)
        self.assertEqual(self.jobs._path(self.cfg, parent['id']).read_bytes(), before)
        self.assertEqual(len(self.jobs.list_jobs(self.cfg)), 2)

    def test_a_job_cannot_be_its_own_parent(self):
        parent = self.parent(completed=True)
        with self.assertRaises(self.jobs.JobError):
            self.jobs.start_job(self.cfg, 'workbuddy', 'parent goal',
                                 request_id='parent', parent_job_id=parent['id'])
        self.assertEqual(len(self.jobs.list_jobs(self.cfg)), 1)

    def test_parent_snapshot_is_observed_while_parent_lease_is_held(self):
        from agentbus.lease import LeaseManager
        parent = self.parent()
        original = self.jobs._parent_snapshot
        def observe(cfg, identity):
            manager = LeaseManager(cfg.workspace / '.agentbus')
            self.assertIsNotNone(manager.holder('.agentbus/jobs/' + identity + '.json'))
            return original(cfg, identity)
        with patch.object(self.jobs, '_parent_snapshot', side_effect=observe):
            child = self.child(parent['id'])
        self.assertEqual(child['parent_job_id'], parent['id'])

    def test_allocated_child_with_incomplete_record_fails_without_traceback_or_new_goal(self):
        parent = self.parent(completed=True)
        child = self.child(parent['id'])
        child_path = self.jobs._path(self.cfg, child['id'])
        child_path.write_text(json.dumps({'schema_version': 1, 'id': child['id']}), encoding='utf-8')
        before = child_path.read_bytes()
        with self.assertRaisesRegex(self.jobs.JobError, 'record.*inspect'):
            self.child(parent['id'])
        self.assertEqual(child_path.read_bytes(), before)
        code = ('import sys; from agentbus.cli import main; raise SystemExit(main(sys.argv[1:]))')
        args = ['team', 'start', '--workspace', str(self.cfg.workspace), '--caller', 'trae',
                '--goal', child['goal'], '--request-id', 'child', '--from-job', parent['id']]
        sub_env = {**os.environ, 'PYTHONPATH': str(_ROOT_DIR)}
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, *args],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding='utf-8', timeout=10, env=sub_env)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('inspect', result.stderr)
        self.assertEqual(child_path.read_bytes(), before)
        self.assertEqual(len([p for p in self.bus.pending('codex')
                              if self.bus.read(p).id == child['goal_id']]), 1)

    def test_allocated_child_retry_rejects_bad_required_field_types_and_keeps_record(self):
        parent = self.parent(completed=True)
        child = self.child(parent['id'])
        path = self.jobs._path(self.cfg, child['id'])
        original = json.loads(path.read_text(encoding='utf-8'))
        for field, value in [('goal_message', []), ('tasks', {}), ('deadline_ts', None),
                             ('deadline_ts', float('nan')), ('checkpoints', {}),
                             ('parent_snapshot', []), ('finalization', []),
                             ('tasks', [{'id': 'bad'}])]:
            with self.subTest(field=field, value=value):
                record = dict(original)
                record[field] = value
                path.write_text(json.dumps(record), encoding='utf-8')
                before = path.read_bytes()
                with self.assertRaisesRegex(self.jobs.JobError, 'record.*inspect'):
                    self.child(parent['id'])
                self.assertEqual(path.read_bytes(), before)

    def test_copied_allocated_child_retry_is_rejected_in_foreign_workspace(self):
        import shutil
        parent = self.parent(completed=True)
        child = self.child(parent['id'])
        copied = Path(self.tmp.name) / 'copied workspace'
        shutil.copytree(self.cfg.workspace / '.agentbus', copied / '.agentbus')
        cfg = load(copied)
        path = self.jobs._path(cfg, child['id'])
        before = path.read_bytes()
        with self.assertRaisesRegex(self.jobs.JobError, 'another project'):
            self.child(parent['id'], cfg=cfg)
        self.assertEqual(path.read_bytes(), before)

    def test_artifact_error_strict_type_and_omission_validation(self):
        receipt = self.bus.make('opencode', 'codex', 'result', body='completed',
                                reply_to='m-1791946000001-abcdef')
        receipt.artifacts = 'not-a-list'
        err = self.jobs._artifact_error(self.cfg, {'id': receipt.reply_to, 'expected_artifacts': []}, receipt)
        self.assertIn('must be a list', err)

        receipt.artifacts = []
        err = self.jobs._artifact_error(self.cfg, {'id': receipt.reply_to, 'expected_artifacts': 'not-a-list'}, receipt)
        self.assertIn('must be a list', err)

        receipt.artifacts = []
        err = self.jobs._artifact_error(self.cfg, {'id': receipt.reply_to, 'expected_artifacts': ['required.txt']}, receipt)
        self.assertIn('omits declared paths', err)

        err = self.jobs._artifact_error(self.cfg, {'id': receipt.reply_to, 'expected_artifacts': []}, receipt)
        self.assertEqual(err, '')

    def test_parent_snapshot_preserves_failed_blocked_and_missing_task_states(self):
        self.cfg.data['oversight']['review_every_n'] = 0
        for outcome in ('failed', 'blocked', 'missing'):
            with self.subTest(outcome=outcome):
                parent = self.jobs.start_job(self.cfg, 'workbuddy', 'failed stage',
                                            request_id='parent-' + outcome)
                parent = self.jobs.dispatch_job(self.cfg, 'codex', parent['id'], self.plan())
                task = self.bus.find_message(parent['tasks'][0]['id'])
                ready = self.bus.dirs(task.to)['ready'] / (task.id + '.json')
                if outcome == 'missing':
                    ready.unlink()
                else:
                    task.result = 'actual ' + outcome + ' evidence'
                    task.exit_code = 1
                    self.bus.finish(task.to, task, outcome)
                child = self.child(parent['id'], request='child-' + outcome)
                expected_location = 'missing' if outcome == 'missing' else 'done'
                self.assertEqual(child['parent_snapshot'].get('task_states'),
                                 [{'id': task.id, 'worker': 'opencode',
                                   'status': outcome, 'location': expected_location,
                                   'body_sha256': 'f098b59c8fa4c4da0579f395b4371dd63336395aca05425f3a84b4c490d2a774'}])
                self.assertEqual(child['parent_snapshot']['done_task_ids'], [])


if __name__ == '__main__':
    unittest.main()
