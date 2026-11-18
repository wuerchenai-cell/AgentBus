"""Codex, 2026-10-03: isolated claim failures and explicit recovery consent.

No model, production mailbox, PID death detection or durable crash guarantee.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.bus import Bus, BusError
from agentbus.config import load
from agentbus.director import Director


class ClaimObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agentbus-claim-observe-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bus = Bus(self.root / '.agentbus')
        self.bus.init()
        self.message = self.bus.make('codex', 'workbuddy', 'task',
                                     body='PRIVATE_BODY_SENTINEL', result_to='codex',
                                     budget_s=41, deadline_ts=time.time() + 90)
        self.bus.post(self.message)
        self.ready = self.bus.dirs('workbuddy')['ready'] / (self.message.id + '.json')
        self.processing = self.bus.dirs('workbuddy')['processing'] / self.ready.name
        self.before = self.ready.read_bytes()

    def assert_no_completion_or_requeue(self):
        self.assertFalse(self.ready.exists())
        self.assertTrue(self.processing.is_file())
        self.assertFalse((self.bus.dirs('workbuddy')['done'] / self.ready.name).exists())
        self.assertFalse(any(event.get('event') == 'claim'
                             for event in self.bus.events()))

    def assert_failure(self, stage, error_type, errno=None, winerror=None):
        failures = [event for event in self.bus.events()
                    if event.get('event') == 'claim_failed']
        self.assertEqual(len(failures), 1)
        event = failures[0]
        self.assertEqual(set(event), {'event', 'stage', 'agent', 'id', 'filename',
                                     'error_type', 'errno', 'winerror', 'ts'})
        self.assertEqual(event['stage'], stage)
        self.assertEqual(event['agent'], 'workbuddy')
        self.assertEqual(event['id'], self.ready.stem)
        self.assertEqual(event['filename'], self.ready.name)
        self.assertEqual(event['error_type'], error_type)
        self.assertEqual(event['errno'], errno)
        self.assertEqual(event['winerror'], winerror)
        encoded = json.dumps(event)
        self.assertNotIn('PRIVATE_BODY_SENTINEL', encoded)
        self.assertNotIn('PRIVATE_EXCEPTION_SENTINEL', encoded)
        if stage == 'audit':
            self.assert_no_completion_or_requeue()
        else:
            self.assertTrue(self.ready.is_file())
            self.assertFalse(self.processing.exists())
            self.assertFalse((self.bus.dirs('workbuddy')['done'] / self.ready.name).exists())

    def test_moved_unreadable_message_records_read_stage_and_retains_original_bytes(self):
        invalid = b'{PRIVATE_BODY_SENTINEL'
        self.ready.write_bytes(invalid)
        with self.assertRaises(json.JSONDecodeError):
            self.bus.claim('workbuddy', self.ready)
        self.assertEqual(self.ready.read_bytes(), invalid)
        self.assert_failure('read', 'JSONDecodeError')

    def test_update_failure_records_safe_error_codes_and_keeps_original_exception(self):
        failure = PermissionError(13, 'PRIVATE_EXCEPTION_SENTINEL')
        failure.winerror = 5
        with patch.object(self.bus, '_write', side_effect=failure):
            with self.assertRaises(PermissionError) as caught:
                self.bus.claim('workbuddy', self.ready)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.ready.read_bytes(), self.before)
        self.assert_failure('write', 'PermissionError', 13, 5)

    def test_claim_audit_failure_records_audit_stage_without_requeue_or_fake_done(self):
        failure = OSError(5, 'PRIVATE_EXCEPTION_SENTINEL')
        audit = self.bus.audit
        def audited(**event):
            if event.get('event') == 'claim':
                raise failure
            return audit(**event)
        with patch.object(self.bus, 'audit', side_effect=audited):
            with self.assertRaises(OSError) as caught:
                self.bus.claim('workbuddy', self.ready)
        self.assertIs(caught.exception, failure)
        observed = self.bus.read(self.processing)
        self.assertEqual(observed.status, 'claimed')
        self.assertEqual(observed.claimed_by, 'workbuddy')
        self.assertEqual(observed.deadline_ts, self.message.deadline_ts)
        self.assert_failure('audit', 'OSError', 5)

    def test_failure_audit_is_best_effort_and_cannot_mask_original_error(self):
        failure = PermissionError(13, 'PRIVATE_EXCEPTION_SENTINEL')
        secondary = OSError(28, 'secondary audit write denied')
        with patch.object(self.bus, '_write', side_effect=failure), \
                patch.object(self.bus, 'audit', side_effect=secondary):
            with self.assertRaises(PermissionError) as caught:
                self.bus.claim('workbuddy', self.ready)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.ready.read_bytes(), self.before)
        self.assertFalse(self.processing.exists())

    def test_audit_stage_and_secondary_audit_failure_keep_first_exception(self):
        failure = OSError(5, 'PRIVATE_EXCEPTION_SENTINEL')
        secondary = OSError(28, 'secondary audit unavailable')
        with patch.object(self.bus, 'audit', side_effect=[failure, secondary]) as audited:
            with self.assertRaises(OSError) as caught:
                self.bus.claim('workbuddy', self.ready)
        self.assertIs(caught.exception, failure)
        self.assertEqual(audited.call_count, 2)
        self.assertEqual(self.bus.read(self.processing).status, 'claimed')
        self.assert_no_completion_or_requeue()

    def test_successful_claim_keeps_existing_success_audit(self):
        claimed = self.bus.claim('workbuddy', self.ready)
        self.assertEqual(claimed.status, 'claimed')
        self.assertTrue(any(event.get('event') == 'claim' for event in self.bus.events()))
        self.assertFalse(any(event.get('event') == 'claim_failed' for event in self.bus.events()))

    def stale_claim(self):
        claimed = self.bus.claim('workbuddy', self.ready)
        old = time.time() - 10000
        os.utime(self.processing, (old, old))
        return claimed

    def test_reclaim_without_confirmation_refuses_and_preserves_all_message_evidence(self):
        self.stale_claim()
        before = self.processing.read_bytes()
        events = self.bus.events_path.read_bytes()
        with self.assertRaisesRegex(BusError, 'owner_confirmed_inactive'):
            self.bus.reclaim_stale('workbuddy', 60)
        self.assertEqual(self.processing.read_bytes(), before)
        self.assertEqual(self.bus.events_path.read_bytes(), events)
        self.assertFalse(self.ready.exists())

    def test_reclaim_requires_literal_true_confirmation(self):
        self.stale_claim()
        self.assertIn('owner_confirmed_inactive', inspect.signature(self.bus.reclaim_stale).parameters)
        before = self.processing.read_bytes()
        for invalid in (False, None, 1, 'true'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(BusError):
                    self.bus.reclaim_stale('workbuddy', 60, owner_confirmed_inactive=invalid)
                self.assertEqual(self.processing.read_bytes(), before)
                self.assertFalse(self.ready.exists())

    def test_confirmed_inactive_reclaim_clears_claim_but_keeps_budget_and_deadline(self):
        claimed = self.stale_claim()
        self.assertIn('owner_confirmed_inactive', inspect.signature(self.bus.reclaim_stale).parameters)
        recovered = self.bus.reclaim_stale('workbuddy', 60, owner_confirmed_inactive=True)
        self.assertEqual(recovered, [self.message.id])
        observed = self.bus.read(self.ready)
        self.assertEqual(observed.status, 'pending')
        self.assertEqual(observed.claimed_by, '')
        self.assertEqual(observed.claimed_at, '')
        self.assertEqual(observed.attempt, claimed.attempt + 1)
        self.assertEqual(observed.budget_s, claimed.budget_s)
        self.assertEqual(observed.deadline_ts, claimed.deadline_ts)
        self.assertFalse(self.processing.exists())
        event = next(event for event in self.bus.events() if event.get('event') == 'reclaim')
        self.assertIs(event.get('owner_confirmed_inactive'), True)
        self.assertEqual(event.get('confirmation_source'), 'caller_assertion')

    def test_director_does_not_reclaim_or_execute_slow_inflight_task_based_on_mtime(self):
        cfg = load(self.root, create=True)
        for name, worker in cfg.data['workers'].items():
            worker['enabled'] = name == 'codex'
        factory_calls = []
        def forbidden_factory(*args):
            factory_calls.append(args)
            raise AssertionError('No adapter or model may run in this fixture')
        director = Director(cfg, adapter_factory=forbidden_factory)
        message = director.bus.make('human', 'codex', 'task', body='slow active work',
                                    result_to='human', budget_s=41)
        director.bus.post(message)
        ready = director.bus.dirs('codex')['ready'] / (message.id + '.json')
        director.bus.claim('codex', ready)
        processing = director.bus.dirs('codex')['processing'] / ready.name
        old = time.time() - 10000
        os.utime(processing, (old, old))
        before = processing.read_bytes()
        with patch.object(director.bus, 'watcher', return_value=None), \
                patch.object(director.bus, 'wait_for_work', return_value=None), \
                patch.object(director.bus, 'reclaim_stale', wraps=director.bus.reclaim_stale) as reclaim, \
                patch.object(director, '_handle') as handle:
            director.run(seconds=0.02, max_rounds=1)
        reclaim.assert_not_called()
        handle.assert_not_called()
        self.assertEqual(factory_calls, [])
        self.assertEqual(processing.read_bytes(), before)
        self.assertFalse(ready.exists())
        self.assertFalse(any(event.get('event') in ('reclaim', 'stale_recovered')
                             for event in director.bus.events()))


if __name__ == '__main__':
    unittest.main()
