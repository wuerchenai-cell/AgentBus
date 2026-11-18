"""Owner-approved task tree handoff; original claims remain fenced history."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.bus import Bus, BusError, LateResultRejected
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus import jobs


class RelinquishClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        self.bus = Bus(self.cfg.workspace / '.agentbus')
        self.bus.init()

    def task(self, claimed=True, **fields):
        msg = self.bus.make('codex', 'workbuddy', 'task', body='original goal',
                            result_to='codex', deadline_ts=fields.pop('deadline_ts', time.time() + 120),
                            **fields)
        self.bus.post(msg)
        return self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0]) if claimed else msg

    def assert_preserved(self, msg):
        observed = self.bus.find_message(msg.id)
        self.assertEqual(observed.status, 'claimed')
        self.assertEqual(observed.claim_id, msg.claim_id)
        self.assertFalse((self.bus.dirs(msg.to)['done'] / (msg.id + '.json')).exists())

    def test_live_claim_without_original_ticket_or_inactive_confirmation_is_rejected(self):
        msg = self.task()
        for invalid in (False, None, 1, 'true'):
            with self.subTest(confirmation=invalid):
                with self.assertRaises(BusError):
                    self.bus.relinquish_claim(msg.to, msg.id, owner_confirmed_inactive=invalid)
                self.assert_preserved(msg)

    def test_ticket_release_preserves_history_and_rejects_late_result(self):
        msg = self.task()
        ticket = captured_ticket(msg)
        closed = self.bus.relinquish_claim(msg.to, msg.id, ticket=ticket, reason='quota exhausted')
        self.assertEqual((closed.status, closed.claim_epoch, closed.claim_id), ('blocked', 1, ''))
        self.assertEqual((closed.body, closed.deadline_ts, closed.attempt),
                         (msg.body, msg.deadline_ts, msg.attempt))
        self.assertFalse(self.bus.pending(msg.to))
        self.assertFalse(self.bus.processing(msg.to))
        reply = self.bus.make(msg.to, 'codex', 'result', reply_to=msg.id,
                              source_claim_epoch=ticket.claim_epoch, source_claim_id=ticket.claim_id,
                              body='old worker finished late')
        with self.assertRaises(LateResultRejected):
            self.bus.post(reply)
        with self.assertRaises(LateResultRejected):
            self.bus.commit_result(msg.to, msg, ticket, body=reply.body)
        self.assertFalse(self.bus.find_results(msg.id))
        self.assertEqual(self.bus.find_message(msg.id).status, 'blocked')
        self.assertTrue(list((self.bus.root / 'late-results' / msg.id).glob('*.json')))

    def test_explicit_inactive_release_does_not_issue_success_or_new_execution(self):
        msg = self.task()
        result = self.bus.relinquish_claim(msg.to, msg.id, reason='owner stopped',
                                           owner_confirmed_inactive=True)
        self.assertEqual(result.status, 'blocked')
        self.assertFalse(result.claim_id)
        self.assertEqual(result.attempt, msg.attempt)
        self.assertFalse(self.bus.pending('workbuddy'))
        self.assertFalse(self.bus.find_results(msg.id))
        self.assertTrue(any(e['event'] == 'relinquished_claim' for e in self.bus.events()))

    def test_unclaimed_ready_task_can_be_released_without_claiming_it(self):
        msg = self.task(claimed=False)
        closed = self.bus.relinquish_claim(msg.to, msg.id, reason='rerouted before execution')
        self.assertEqual((closed.status, closed.claim_epoch, closed.claim_id), ('blocked', 0, ''))
        self.assertEqual(closed.deadline_ts, msg.deadline_ts)
        self.assertFalse(any(e['event'] == 'claim' for e in self.bus.events()))

    def test_disabled_stopped_origin_can_be_retired_without_reenabling_execution(self):
        msg = self.task()
        self.cfg.data['workers']['workbuddy']['enabled'] = False
        self.cfg.data['workers']['workbuddy']['executor_enabled'] = False
        save(self.cfg)
        with self.assertRaises(BusError):
            self.bus.recover_claim(msg.to, msg.id, owner_confirmed_inactive=True)
        closed = self.bus.relinquish_claim(msg.to, msg.id, reason='disabled stopped owner',
                                          owner_confirmed_inactive=True)
        self.assertEqual(closed.status, 'blocked')
        self.assertFalse(self.bus.pending(msg.to))
        self.assertFalse(self.cfg.data['workers']['workbuddy']['enabled'])

    def test_stop_expiry_and_retry_limit_preserve_original_claim(self):
        for case in ('stop', 'expiry', 'retry'):
            with self.subTest(case=case):
                self.setUp()  # Each refusal intentionally keeps its original claim live.
                fields = {'attempt': 2} if case == 'retry' else {}
                if case == 'expiry':
                    fields['deadline_ts'] = time.time() - 1
                msg = self.task(**fields)
                if case == 'stop':
                    (self.bus.root / 'STOP').write_text('stop', encoding='utf-8')
                with self.assertRaises(BusError):
                    self.bus.relinquish_claim(msg.to, msg.id, ticket=captured_ticket(msg))
                self.assert_preserved(msg)
                (self.bus.root / 'STOP').unlink(missing_ok=True)

    def test_write_failure_leaves_original_ticket_valid(self):
        msg = self.task()
        with patch.object(self.bus, '_write', side_effect=OSError('publication failed')):
            with self.assertRaises(OSError):
                self.bus.relinquish_claim(msg.to, msg.id, ticket=captured_ticket(msg))
        self.assert_preserved(msg)

    def test_same_handoff_retry_reuses_official_retired_record(self):
        msg = self.task()
        original = self.bus.relinquish_claim(msg.to, msg.id, ticket=captured_ticket(msg), reason='relay')
        repeat = self.bus.relinquish_claim(msg.to, msg.id, reason='relay')
        self.assertEqual(original.to_dict(), repeat.to_dict())
        self.assertEqual(sum(e['event'] == 'relinquished_claim' for e in self.bus.events()), 1)
        with self.assertRaises(BusError):
            self.bus.relinquish_claim(msg.to, msg.id, reason='other relay')

    def test_final_audit_failure_is_repaired_without_restarting_original_execution(self):
        msg = self.task()
        audit = self.bus.audit
        def fail_final(**event):
            if event.get('event') == 'relinquished_claim':
                raise OSError('final audit unavailable')
            return audit(**event)
        with patch.object(self.bus, 'audit', side_effect=fail_final):
            with self.assertRaises(OSError):
                self.bus.relinquish_claim(msg.to, msg.id, ticket=captured_ticket(msg), reason='relay')
        self.assertEqual(self.bus.find_message(msg.id).status, 'blocked')
        repaired = self.bus.relinquish_claim(msg.to, msg.id, reason='relay')
        self.assertEqual(repaired.status, 'blocked')
        self.assertFalse(self.bus.pending(msg.to))
        self.assertEqual(sum(e['event'] == 'relinquished_claim' for e in self.bus.events()), 1)

    def test_stop_after_authorization_preserves_original_claim(self):
        msg = self.task()
        audit = self.bus.audit
        def stopped(**event):
            audit(**event)
            if event.get('event') == 'relinquish_authorized':
                (self.bus.root / 'STOP').write_text('stop before transition', encoding='utf-8')
        with patch.object(self.bus, 'audit', side_effect=stopped):
            with self.assertRaises(BusError):
                self.bus.relinquish_claim(msg.to, msg.id, ticket=captured_ticket(msg), reason='relay')
        self.assert_preserved(msg)

    def test_ordinary_blocked_task_cannot_authorize_a_handoff_retry(self):
        msg = self.task()
        self.bus.block(msg.to, msg, 'ordinary blocker', ticket=captured_ticket(msg))
        with self.assertRaises(BusError):
            self.bus.relinquish_claim(msg.to, msg.id, reason='ordinary blocker')
        self.assertEqual(self.bus.find_message(msg.id).claim_id, msg.claim_id)

    def test_completed_source_still_requires_original_source_ticket(self):
        msg = self.task()
        ticket = captured_ticket(msg)
        self.bus.heartbeat(msg.to, msg, ticket, note='valid source session')
        self.bus.commit_result(msg.to, msg, ticket, body='actual result')
        forged = self.bus.make(msg.to, 'codex', 'result', reply_to=msg.id, body='unbound result')
        with self.assertRaises(LateResultRejected):
            self.bus.post(forged)
        forged.sender = 'trae'
        forged.source_claim_epoch = msg.claim_epoch
        forged.source_claim_id = msg.claim_id
        with self.assertRaises(LateResultRejected):
            self.bus.post(forged)

    def bound_handoff(self, *, bind_audit=True, change=None):
        job = jobs.start_job(self.cfg, 'workbuddy', 'original project goal', request_id='tree-relay')
        job = jobs.dispatch_job(self.cfg, 'codex', job['id'], [
            {'worker': 'workbuddy', 'title': 'one obligation', 'body': 'keep this exact goal', 'result_to': 'codex'}])
        old = self.bus.claim('workbuddy', self.bus.dirs('workbuddy')['ready'] / (job['tasks'][0]['id'] + '.json'))
        self.bus.relinquish_claim(old.to, old.id, ticket=captured_ticket(old), reason='relay')
        new = self.bus.make('codex', 'trae', 'task', title=old.title, body=old.body,
                            result_to=old.result_to, reply_to=old.id, refs=[old.id],
                            job_id=old.job_id, plan_id=old.plan_id, plan_sha256=old.plan_sha256,
                            expected_artifacts=old.expected_artifacts, hop=old.hop,
                            attempt=old.attempt + 1, deadline_ts=old.deadline_ts, budget_s=old.budget_s)
        if change:
            for key, value in change.items():
                setattr(new, key, value)
        jobs.register_job_tasks(self.cfg, 'codex', job['id'], [new])
        self.bus.post(new)
        if bind_audit:
            self.bus.audit(event='task_tree_successor_bound', leaf_id='leaf-relay', job_id=job['id'],
                           old_task_id=old.id, task_id=new.id, successor='trae', source='task_forest', reason='relay')
        return job, old, new

    def test_only_formal_bound_successor_discharges_old_blocker_without_fake_completion(self):
        job, old, new = self.bound_handoff()
        observed = jobs.status_job(self.cfg, job['id'])
        prior = next(task for task in observed['tasks'] if task['id'] == old.id)
        self.assertEqual(prior['status'], 'transferred')
        self.assertEqual(prior['successor_task_id'], new.id)
        self.assertNotEqual(observed['state'], 'blocked')
        self.assertNotEqual(observed['state'], 'completed')
        self.assertEqual(self.bus.find_message(old.id).status, 'blocked')
        self.assertEqual(len(jobs._load(self.cfg, job['id'])['tasks']), 2)

    def test_successor_without_binding_audit_cannot_clear_old_blocker(self):
        job, old, new = self.bound_handoff(bind_audit=False)
        observed = jobs.status_job(self.cfg, job['id'])
        self.assertEqual(observed['state'], 'blocked')
        self.assertEqual(observed['tasks'][0]['status'], 'blocked')

    def test_successor_changing_goal_does_not_clear_old_blocker(self):
        job, old, new = self.bound_handoff(change={'body': 'different goal'})
        observed = jobs.status_job(self.cfg, job['id'])
        self.assertEqual(observed['state'], 'blocked')
        self.assertEqual(observed['tasks'][0]['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
