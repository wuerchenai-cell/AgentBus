"""Source: Codex internal candidate implementation, 2026-10-04. Model-free contracts."""
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
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus import jobs, project_plan
from agentbus.adapters import Result
from agentbus.bus import Bus, BusError
from agentbus.config import load
from agentbus.director import Director, Summary
from agentbus.workspace_files import WorkspaceFiles


class JobPlanBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load(Path(self.tmp.name), create=True)
        self.cfg.data['oversight']['review_every_n'] = 99
        self.bus = Bus(self.cfg.workspace / '.agentbus')
        self.bus.init()

    def adopt(self, text, request='plan-one'):
        summary = project_plan.plan_summary(self.cfg)
        base = summary['current_sha256']
        proposal = project_plan.propose_plan(self.cfg, 'codex', text, 'User goal',
            ['workbuddy'], base, request)
        task = self.bus.make('codex', 'workbuddy', 'review',
            body='SHA256: ' + proposal['content_sha256'], result_to='codex')
        self.bus.post(task)
        task = self.bus.claim('workbuddy', self.bus.dirs('workbuddy')['ready'] / (task.id + '.json'))
        task_ticket = receipt_ticket(task)
        self.bus.heartbeat('workbuddy', task, task_ticket, note='test plan review started')
        result = self.bus.commit_result('workbuddy', task, task_ticket,
            body='Verdict: accept\nEvidence preserved', result_to='codex', exit_code=0)
        result = self.bus.claim('codex', self.bus.dirs('codex')['ready'] / (result.id + '.json'))
        result_ticket = receipt_ticket(result)
        self.bus.heartbeat('codex', result, result_ticket, note='test coordinator received review')
        self.bus.finish('codex', result, ticket=result_ticket)
        project_plan.record_review(self.cfg, 'codex', proposal['id'], task.id, 'accept')
        return project_plan.adopt_plan(self.cfg, 'codex', proposal['id'], base,
            {'workbuddy': 'Accepted with original evidence'})

    def start(self, goal='ORIGINAL WHOLE PROJECT GOAL'):
        return jobs.start_job(self.cfg, 'workbuddy', goal, request_id='job-one')

    def dispatch(self, job, expected=None):
        item = {'worker': 'trae', 'title': 'bounded', 'body': 'ONLY SUBTASK', 'result_to': 'codex'}
        if expected is not None:
            item['expected_artifacts'] = expected
        return jobs.dispatch_job(self.cfg, 'codex', job['id'], [item])

    def finish(self, job, artifacts=None, consume=True):
        task = self.bus.find_message(job['tasks'][0]['id'])
        task = self.bus.claim(task.to, self.bus.dirs(task.to)['ready'] / (task.id + '.json'))
        task_ticket = receipt_ticket(task)
        self.bus.heartbeat(task.to, task, task_ticket, note='test worker started')
        result = self.bus.commit_result(task.to, task, task_ticket,
                                       body='done', artifacts=artifacts or [], exit_code=0)
        task = self.bus.find_message(task.id)
        if consume:
            result = self.bus.claim('codex', self.bus.dirs('codex')['ready'] / (result.id + '.json'))
            result_ticket = receipt_ticket(result)
            self.bus.heartbeat('codex', result, result_ticket, note='test coordinator received result')
            self.bus.finish('codex', result, ticket=result_ticket)
        return task, result

    def director(self, payload=None):
        class Capture:
            prompt = None
            calls = 0
            def run(adapter, prompt, **kwargs):
                adapter.prompt, adapter.calls = prompt, adapter.calls + 1
                return Result(0, json.dumps(payload or {'complete': True, 'tasks': [], 'summary': 'done'}), 0)
        adapter = Capture()
        d = Director(self.cfg, bus=self.bus, adapter_factory=lambda *_: adapter)
        self.addCleanup(d.close)
        d._continue_after_results = True
        return d, adapter

    def test_new_job_binds_immutable_plan_and_goal_metadata(self):
        plan = self.adopt('# FIRST WHOLE PLAN')
        job = self.start()
        self.assertEqual(job['plan_snapshot'], plan)
        goal = self.bus.find_message(job['goal_id'])
        self.assertEqual((goal.job_id, goal.plan_id, goal.plan_sha256),
                         (job['id'], plan['revision_id'], plan['content_sha256']))

    def test_current_plan_update_does_not_replace_old_binding_deadline_or_goal(self):
        old = self.adopt('# FIRST WHOLE PLAN')
        job = self.start()
        path = self.cfg.workspace / '.agentbus/jobs' / (job['id'] + '.json')
        original = path.read_bytes()
        current = self.adopt('# SECOND WHOLE PLAN', 'plan-two')
        state = jobs.status_job(self.cfg, job['id'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(state['plan_binding']['bound_id'], old['revision_id'])
        self.assertEqual(state['plan_binding']['current_id'], current['revision_id'])
        self.assertTrue(state['plan_binding']['differs_from_current'])
        self.assertEqual(state['deadline_ts'], job['deadline_ts'])

    def test_dispatch_keeps_raw_work_item_and_adds_original_goal_bound_plan(self):
        plan = self.adopt('# FIRST WHOLE PLAN')
        job = self.dispatch(self.start())
        task = self.bus.find_message(job['tasks'][0]['id'])
        self.assertEqual(job['tasks'][0]['body'], 'ONLY SUBTASK')
        self.assertIn('ORIGINAL WHOLE PROJECT GOAL', task.body)
        self.assertIn('# FIRST WHOLE PLAN', task.body)
        self.assertEqual(task.job_id, job['id'])
        self.assertEqual(task.plan_id, plan['revision_id'])
        self.assertLessEqual(task.deadline_ts, job['deadline_ts'])

    def test_new_unbound_job_does_not_adopt_later_plan(self):
        job = self.start()
        self.adopt('# LATER PLAN')
        dispatched = self.dispatch(job)
        task = self.bus.find_message(dispatched['tasks'][0]['id'])
        self.assertIsNone(task.plan_id)
        self.assertNotIn('# LATER PLAN', task.body)
        self.assertIsNone(jobs.status_job(self.cfg, job['id'])['plan_snapshot'])

    def test_legacy_job_is_not_backfilled_on_read_or_dispatch(self):
        job = self.start()
        path = self.cfg.workspace / '.agentbus/jobs' / (job['id'] + '.json')
        data = json.loads(path.read_text(encoding='utf-8'))
        data.pop('plan_snapshot', None)
        for key in ('job_id', 'plan_id', 'plan_sha256'):
            data['goal_message'].pop(key, None)
        goal = self.bus.find_message(job['goal_id'])
        goal.job_id = goal.plan_id = goal.plan_sha256 = None
        ready = self.bus.dirs('codex')['ready'] / (goal.id + '.json')
        ready.write_text(json.dumps(goal.to_dict()), encoding='utf-8')
        path.write_text(json.dumps(data), encoding='utf-8')
        self.adopt('# CURRENT PLAN')
        before = path.read_bytes()
        jobs.status_job(self.cfg, job['id'])
        self.assertEqual(path.read_bytes(), before)
        dispatched = self.dispatch(job)
        task = self.bus.find_message(dispatched['tasks'][0]['id'])
        self.assertIsNone(task.job_id)
        self.assertNotIn('# CURRENT PLAN', task.body)

    def test_restarted_followup_reads_job_goal_and_bound_plan(self):
        self.adopt('# BOUND LONG TERM PLAN')
        job = self.dispatch(self.start())
        source, result = self.finish(job)
        d, adapter = self.director()
        d._continue_after_result(source, result, Summary())
        self.assertIn('ORIGINAL WHOLE PROJECT GOAL', adapter.prompt)
        self.assertIn('# BOUND LONG TERM PLAN', adapter.prompt)

    def test_followup_propagates_binding_and_never_extends_source_deadline(self):
        plan = self.adopt('# BOUND LONG TERM PLAN')
        job = self.dispatch(self.start())
        source, result = self.finish(job)
        source.deadline_ts = time.time() + 40
        d, adapter = self.director({'complete': False, 'summary': 'next', 'tasks': [{
            'worker': 'trae', 'body': 'NEXT WORK', 'result_to': 'codex'}]})
        d._continue_after_result(source, result, Summary())
        pending = [self.bus.read(p) for p in self.bus.pending('trae', ['task'])]
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0].job_id, pending[0].plan_id, pending[0].plan_sha256),
            (job['id'], plan['revision_id'], plan['content_sha256']))
        self.assertLessEqual(pending[0].deadline_ts, source.deadline_ts)
        self.assertIn('ORIGINAL WHOLE PROJECT GOAL', pending[0].body)
        self.assertEqual(len(jobs.status_job(self.cfg, job['id'])['tasks']), 2)

    def test_review_result_does_not_generate_execution_followup(self):
        source = self.bus.make('codex', 'trae', 'review', body='independent opinion', result_to='codex')
        self.bus.post(source)
        source = self.bus.claim('trae', self.bus.dirs('trae')['ready'] / (source.id + '.json'))
        source_ticket = receipt_ticket(source)
        self.bus.heartbeat('trae', source, source_ticket, note='test reviewer started')
        result = self.bus.commit_result('trae', source, source_ticket,
            body='accept with changes', result_to='codex', exit_code=0)
        d, adapter = self.director()
        d._handle('codex', result, Summary(), review=True)
        self.assertEqual(adapter.calls, 0)

    def test_peer_review_includes_original_goal_and_binding(self):
        plan = self.adopt('# BOUND LONG TERM PLAN')
        job = self.dispatch(self.start())
        source, _ = self.finish(job)
        d, _ = self.director()
        review = d.schedule_peer_review(source, 'trae', 'failed')
        self.assertIsNotNone(review)
        self.assertIn('ORIGINAL WHOLE PROJECT GOAL', review.body)
        self.assertIn('# BOUND LONG TERM PLAN', review.body)
        self.assertEqual((review.job_id, review.plan_id, review.plan_sha256),
                         (job['id'], plan['revision_id'], plan['content_sha256']))
        self.assertLessEqual(review.deadline_ts, source.deadline_ts)

    def test_retry_propagates_binding_and_expected_artifacts(self):
        plan = self.adopt('# BOUND LONG TERM PLAN')
        job = self.dispatch(self.start(), ['out.txt'])
        source = self.bus.find_message(job['tasks'][0]['id'])
        source = self.bus.claim('trae', self.bus.dirs('trae')['ready'] / (source.id + '.json'))
        source_ticket = receipt_ticket(source)
        self.bus.heartbeat('trae', source, source_ticket, note='test worker started')
        self.bus.finish('trae', source, 'failed', ticket=source_ticket)
        d, _ = self.director()
        self.assertTrue(d._retry_or_escalate(source, 'failed', Summary()))
        retry = self.bus.read(self.bus.pending('trae', ['task'])[0])
        self.assertEqual((retry.job_id, retry.plan_id, retry.plan_sha256),
                         (job['id'], plan['revision_id'], plan['content_sha256']))
        self.assertEqual(retry.expected_artifacts, ['out.txt'])

    def test_artifact_contract_is_optional_without_changing_legacy_normalized_item(self):
        plain = [{'worker': 'trae', 'title': '', 'body': 'READ ONLY', 'result_to': 'codex'}]
        self.assertEqual(jobs._plan(self.cfg, plain), plain)
        required = [dict(plain[0], expected_artifacts=['out.txt'])]
        self.assertEqual(jobs._plan(self.cfg, required), required)

    def test_expected_artifacts_rejects_nonlist_unsafe_and_duplicates_before_dispatch(self):
        job = self.start()
        for invalid in ('out.txt', ['../outside'], ['.workbuddy/private'], ['out.txt', 'out.txt']):
            with self.subTest(value=invalid), self.assertRaises(jobs.JobError):
                self.dispatch(job, invalid)
        self.assertFalse(self.bus.pending('trae'))

    def test_declared_artifact_without_manifest_cannot_be_verified(self):
        job = self.dispatch(self.start(), ['out.txt'])
        with self.assertRaises(BusError):
            self.finish(job)
        source = self.bus.find_message(job['tasks'][0]['id'])
        self.assertTrue((self.bus.dirs('trae')['processing'] / (source.id + '.json')).is_file())
        self.assertFalse(self.bus.find_results(source.id))
        with self.assertRaises(jobs.JobError):
            jobs.finalize_job(self.cfg, 'codex', job['id'], 'looks done', verified=True)
        self.assertEqual(jobs.status_job(self.cfg, job['id'])['artifact_contract'], 'required_verified')
        self.assertFalse(self.bus.find_results(job['goal_id']))

    def test_declared_artifact_manifest_is_mechanically_verified_before_completion(self):
        (self.cfg.workspace / 'out.txt').write_bytes(b'actual artifact')
        entry = WorkspaceFiles(self.cfg.workspace).describe_artifact('out.txt')
        job = self.dispatch(self.start(), ['out.txt'])
        self.finish(job, [entry])
        complete = jobs.finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)
        self.assertEqual(complete['state'], 'completed')
        self.assertTrue(complete['finalization']['automatic_artifact_verification'])

    def test_manifest_mismatch_is_not_bypassed_by_verbal_verified_true(self):
        (self.cfg.workspace / 'out.txt').write_bytes(b'actual artifact')
        entry = WorkspaceFiles(self.cfg.workspace).describe_artifact('out.txt')
        job = self.dispatch(self.start(), ['out.txt'])
        with self.assertRaises(BusError):
            self.finish(job, [dict(entry, sha256='0' * 64)])
        with self.assertRaises(jobs.JobError):
            jobs.finalize_job(self.cfg, 'codex', job['id'], 'claimed verified', verified=True)
        self.assertFalse(self.bus.find_results(job['goal_id']))

    def test_undeclared_read_only_task_keeps_legacy_external_artifact_contract(self):
        job = self.dispatch(self.start())
        self.finish(job)
        state = jobs.finalize_job(self.cfg, 'codex', job['id'], 'read only checked', verified=True)
        self.assertEqual(state['state'], 'completed')
        self.assertEqual(state['artifact_contract'], 'legacy_external')

    def test_explicit_wrong_receipt_binding_blocks_even_legacy_external_task(self):
        job = self.dispatch(self.start())
        task, result = self.finish(job)
        result.job_id = 'j-other'
        path = self.bus.dirs('codex')['done'] / (result.id + '.json')
        path.write_text(json.dumps(result.to_dict()), encoding='utf-8')
        self.assertEqual(jobs.status_job(self.cfg, job['id'])['state'], 'blocked')
        with self.assertRaises(jobs.JobError):
            jobs.finalize_job(self.cfg, 'codex', job['id'], 'wrong binding', verified=True)

    def test_bound_receipt_conflict_does_not_start_a_coordinator_model_turn(self):
        self.adopt('# FIXED PLAN')
        job = self.dispatch(self.start())
        source, result = self.finish(job, consume=False)
        result.plan_id = 'r-wrong'
        d, adapter = self.director()
        d._handle('codex', result, Summary(), review=True)
        self.assertEqual(adapter.calls, 0)
        self.assertTrue(self.bus.pending('human'))
        self.assertTrue((self.bus.dirs('codex')['blocked'] / (result.id + '.json')).is_file())
        self.assertFalse((self.bus.dirs('codex')['done'] / (result.id + '.json')).exists())

    def test_expired_job_never_starts_followup_model_and_original_deadline_remains(self):
        job = self.dispatch(self.start())
        source, result = self.finish(job)
        data = jobs._load(self.cfg, job['id'])
        data['deadline_ts'] = time.time() - 1
        jobs._save(self.cfg, data)
        d, adapter = self.director()
        d._continue_after_result(source, result, Summary())
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(jobs._load(self.cfg, job['id'])['deadline_ts'], data['deadline_ts'])

    def test_followup_model_timeout_is_bounded_by_source_and_job_deadline(self):
        job = self.dispatch(self.start())
        source, result = self.finish(job)
        source.deadline_ts = time.time() + 35
        d, adapter = self.director()
        with patch.object(adapter, 'run', wraps=adapter.run) as run:
            d._continue_after_result(source, result, Summary())
        self.assertLessEqual(run.call_args.kwargs['timeout_s'], 35)

    def test_artifact_change_after_completion_is_visible_without_rewriting_history(self):
        target = self.cfg.workspace / 'out.txt'
        target.write_bytes(b'original')
        job = self.dispatch(self.start(), ['out.txt'])
        self.finish(job, [WorkspaceFiles(self.cfg.workspace).describe_artifact('out.txt')])
        jobs.finalize_job(self.cfg, 'codex', job['id'], 'checked', verified=True)
        record = self.cfg.workspace / '.agentbus/jobs' / (job['id'] + '.json')
        before = record.read_bytes()
        target.write_bytes(b'changed')
        self.assertEqual(jobs.status_job(self.cfg, job['id'])['state'], 'blocked')
        self.assertEqual(record.read_bytes(), before)

    def test_receipt_wrong_sender_is_blocked_without_editing_source_processing(self):
        self.adopt('# FIXED PLAN')
        job = self.dispatch(self.start())
        source = self.bus.claim('trae', self.bus.pending('trae', ['task'])[0])
        source_path = self.bus.dirs('trae')['processing'] / (source.id + '.json')
        before = source_path.read_bytes()
        result = self.bus.make('workbuddy', 'codex', 'result', body='spoofed', reply_to=source.id,
            job_id=source.job_id, plan_id=source.plan_id, plan_sha256=source.plan_sha256)
        from agentbus.bus import LateResultRejected
        with self.assertRaisesRegex(LateResultRejected, 'AGENT_MISMATCH'):
            self.bus.post(result)
        self.assertEqual(source_path.read_bytes(), before)
        self.assertFalse(self.bus.find_results(source.id))
        self.assertTrue(list((self.bus.root / 'late-results' / source.id).glob('*.json')))

    def test_required_artifact_fallback_without_manifest_reports_failure(self):
        (self.cfg.workspace / 'out.txt').write_bytes(b'exists but no manifest')
        job = self.dispatch(self.start(), ['out.txt'])
        source = self.bus.claim('trae', self.bus.pending('trae', ['task'])[0])
        d, adapter = self.director()
        d._handle('trae', source, Summary(), review=True)
        done = self.bus.find_message(source.id)
        self.assertEqual(done.status, 'failed')
        result = self.bus.find_results(source.id)[0]
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.job_id, source.job_id)
        self.assertIn('artifact', result.body.lower())

    def test_invalid_native_receipt_manifest_cannot_commit_success(self):
        (self.cfg.workspace / 'out.txt').write_bytes(b'real bytes')
        job = self.dispatch(self.start(), ['out.txt'])
        source = self.bus.claim('trae', self.bus.pending('trae', ['task'])[0])
        result = self.bus.make('trae', 'codex', 'result', body='claims done', reply_to=source.id,
            job_id=source.job_id, plan_id=source.plan_id, plan_sha256=source.plan_sha256,
            source_claim_epoch=source.claim_epoch, source_claim_id=source.claim_id,
            artifacts=[{'path': 'out.txt', 'bytes': 10, 'sha256': '0' * 64}])
        self.bus.post(result)
        d, adapter = self.director()
        d._handle('trae', source, Summary(), review=True)
        self.assertNotEqual(self.bus.find_message(source.id).status, 'done')
        self.assertTrue((self.bus.dirs('codex')['blocked'] / (result.id + '.json')).exists())


if __name__ == '__main__':
    unittest.main()
