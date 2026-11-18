"""Explicit single-claim recovery; Codex, 2026-10-03, no model calls."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

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

from agentbus.bus import Bus, BusError
from agentbus.config import load, save


class ClaimRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        # Three retries are allowed in this fixture; 2 -> 3 tests the boundary.
        self.cfg.data['gates']['max_retries'] = 3
        save(self.cfg)
        self.bus = Bus(self.cfg.workspace / '.agentbus')
        self.bus.init()
        self.msg = self.bus.make('codex', 'workbuddy', 'task', body='PRIVATE_TASK_BODY',
                                 refs=['receipt.md'], result_to='codex', budget_s=77,
                                 deadline_ts=time.time() + 3600, attempt=2)
        self.bus.post(self.msg)
        self.msg = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        self.dirs = self.bus.dirs('workbuddy')
        self.source = self.dirs['processing'] / f'{self.msg.id}.json'
        self.ready = self.dirs['ready'] / self.source.name

    def recover(self, **kwargs):
        method = getattr(self.bus, 'recover_claim', None)
        self.assertTrue(callable(method), 'single-message controlled recovery API is missing')
        return method('workbuddy', self.msg.id, **kwargs)

    def snapshot(self):
        return {str(p.relative_to(self.bus.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.bus.root.rglob('*') if p.is_file() and 'leases' not in p.parts}

    def recovery_events(self):
        return [e for e in self.bus.events() if e.get('event') in ('recovery_authorized', 'recovered')]

    def test_without_confirmation_refuses_without_file_changes(self):
        before = self.snapshot()
        with self.assertRaisesRegex(BusError, '确认'):
            self.recover()
        self.assertEqual(before, self.snapshot())

    def test_one_confirmed_claim_clears_owner_increments_once_preserves_intent_budget_and_others(self):
        other = self.bus.make('codex', 'trae', 'task', body='Other task', result_to='codex')
        self.bus.post(other)
        other = self.bus.claim('trae', self.bus.pending('trae')[0])
        other_path = self.bus.dirs('trae')['processing'] / f'{other.id}.json'
        other_bytes = other_path.read_bytes()
        before = self.msg.to_dict()
        recovered = self.recover(owner_confirmed_inactive=True)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.ready.exists())
        expected = {**before, 'status': 'pending', 'claimed_by': '', 'claimed_at': '', 'attempt': 3,
                    'claim_id': '', 'claim_agent': '', 'claim_conversation_id': ''}
        self.assertEqual(recovered.to_dict(), expected)
        self.assertEqual(self.bus.read(self.ready).to_dict(), expected)
        self.assertEqual(other_path.read_bytes(), other_bytes)
        self.assertEqual([e['event'] for e in self.recovery_events()], ['recovery_authorized', 'recovered'])
        self.assertNotIn('PRIVATE_TASK_BODY', json.dumps(self.recovery_events()))
        self.assertTrue(all(e['confirmation_source'] == 'caller_assertion' for e in self.recovery_events()))

    def test_ready_conflict_and_terminal_copy_refuse_without_overwriting(self):
        for location in ('ready', 'done', 'blocked'):
            with self.subTest(location=location):
                conflict = self.dirs[location] / self.source.name
                conflict.write_bytes(self.source.read_bytes())
                before = self.snapshot()
                with self.assertRaises(BusError):
                    self.recover(owner_confirmed_inactive=True)
                self.assertEqual(before, self.snapshot())
                conflict.unlink()  # Isolated test fixture only.

    def test_missing_or_corrupt_claim_refuses_and_preserves_evidence(self):
        original = self.source.read_bytes()
        for payload in (None, b'{broken', b'[]', b'{"id":"other"}'):
            with self.subTest(payload=payload):
                if payload is None:
                    self.source.unlink()
                else:
                    self.source.write_bytes(payload)
                before = self.snapshot()
                with self.assertRaises(BusError):
                    self.recover(owner_confirmed_inactive=True)
                self.assertEqual(before, self.snapshot())
                self.source.write_bytes(original)

    def test_pending_physical_claim_can_be_recovered_after_owner_confirmation(self):
        data = self.msg.to_dict()
        data.update(status='pending', claimed_by='', claimed_at='')
        self.source.write_text(json.dumps(data), encoding='utf-8')
        msg = self.recover(owner_confirmed_inactive=True)
        self.assertEqual(msg.attempt, 3)
        self.assertEqual(msg.status, 'pending')

    def test_identity_mismatch_bad_attempt_or_terminal_status_are_not_guessed(self):
        original = self.source.read_bytes()
        for updates in ({'id': 'other'}, {'to': 'trae'}, {'attempt': '2'},
                        {'attempt': -1}, {'status': 'done'}):
            with self.subTest(updates=updates):
                self.source.write_text(json.dumps({**self.msg.to_dict(), **updates}), encoding='utf-8')
                before = self.snapshot()
                with self.assertRaises(BusError):
                    self.recover(owner_confirmed_inactive=True)
                self.assertEqual(before, self.snapshot())
        self.source.write_bytes(original)

    def test_initial_audit_failure_leaves_original_claim_and_retry_recovers_once(self):
        before = self.snapshot()
        with patch.object(self.bus, 'audit', side_effect=OSError('isolated audit failure')):
            with self.assertRaises(OSError):
                self.recover(owner_confirmed_inactive=True)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)

    def test_message_write_failure_is_retryable_without_double_increment(self):
        before = self.source.read_bytes()
        with patch.object(self.bus, '_write', side_effect=OSError('isolated write failure')):
            with self.assertRaises(OSError):
                self.recover(owner_confirmed_inactive=True)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)
        self.assertEqual(len(self.recovery_events()), 2)

    def test_ready_publish_failure_keeps_processing_and_retry_does_not_increment_twice(self):
        original = os.rename
        def fail_publish(src, dst):
            if Path(dst) == self.ready:
                raise PermissionError('isolated ready publish failure')
            return original(src, dst)
        with patch('agentbus.bus.os.rename', fail_publish):
            with self.assertRaises(OSError):
                self.recover(owner_confirmed_inactive=True)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.bus.read(self.source).attempt, 3)
        self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)

    def test_final_audit_failure_is_idempotently_completed_without_losing_task(self):
        original = self.bus.audit
        def fail_final(**kwargs):
            if kwargs.get('event') == 'recovered':
                raise OSError('isolated final audit failure')
            return original(**kwargs)
        with patch.object(self.bus, 'audit', fail_final):
            with self.assertRaises(OSError):
                self.recover(owner_confirmed_inactive=True)
        self.assertTrue(self.ready.exists())
        self.assertFalse(self.source.exists())
        self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)
        before = self.snapshot()
        self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.recovery_events()), 2)

    def test_partial_failed_audit_line_cannot_swallow_retry_authorization(self):
        original = self.bus.audit
        def fail_partial(**kwargs):
            with self.bus.events_path.open('a', encoding='utf-8') as stream:
                stream.write('{"interrupted":')
            raise OSError('isolated partial audit failure')
        with patch.object(self.bus, 'audit', fail_partial):
            with self.assertRaises(OSError):
                self.recover(owner_confirmed_inactive=True)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.bus.read(self.source).attempt, 2)
        try:
            self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)
            self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)
        except BusError as exc:
            self.fail('a failed partial audit must permit idempotent retry: ' + str(exc))

    def test_source_change_during_authorization_is_not_overwritten(self):
        original = self.bus.audit
        def concurrent_update(**kwargs):
            result = original(**kwargs)
            if kwargs.get('event') == 'recovery_authorized':
                changed = self.msg.to_dict()
                changed['body'] = 'Changed by another writer'
                self.source.write_text(json.dumps(changed), encoding='utf-8')
            return result
        with patch.object(self.bus, 'audit', concurrent_update):
            with self.assertRaises(BusError):
                self.recover(owner_confirmed_inactive=True)
        self.assertEqual(self.bus.read(self.source).body, 'Changed by another writer')
        self.assertFalse(self.ready.exists())

    def test_audit_that_wrote_then_raised_does_not_duplicate_success_on_retry(self):
        original = self.bus.audit
        def wrote_then_failed(**kwargs):
            original(**kwargs)
            if kwargs.get('event') == 'recovered':
                raise OSError('isolated exception after audit append')
        with patch.object(self.bus, 'audit', wrote_then_failed):
            with self.assertRaises(OSError):
                self.recover(owner_confirmed_inactive=True)
        active_lock = self.bus.root / 'exclusive-locks' / f'{self.msg.id}.lock.json'
        released_lock = self.bus.root / 'exclusive-locks' / 'released' / f'{self.msg.id}.released.json'
        self.assertTrue(active_lock.is_file(), 'lock cleanup must wait until retry confirms recovery')
        self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)
        self.assertFalse(active_lock.exists())
        self.assertTrue(released_lock.is_file())
        stable = self.snapshot()
        self.assertEqual(self.recover(owner_confirmed_inactive=True).attempt, 3)
        self.assertEqual(self.snapshot(), stable)

    def test_concurrent_ready_creation_is_never_overwritten(self):
        original = os.rename
        def creates_conflict(src, dst):
            if Path(dst) == self.ready:
                self.ready.write_bytes(b'concurrent conflict; preserve')
            return original(src, dst)
        with patch('agentbus.bus.os.rename', creates_conflict):
            with self.assertRaises(OSError):
                self.recover(owner_confirmed_inactive=True)
        self.assertEqual(self.ready.read_bytes(), b'concurrent conflict; preserve')
        self.assertTrue(self.source.exists())

    def test_independent_cli_processes_recover_once(self):
        argv = [sys.executable, '-X', 'utf8', '-m', 'agentbus', 'recover',
                '--workspace', str(self.cfg.workspace), '--agent', 'workbuddy', '--id', self.msg.id,
                '--owner-confirmed-inactive']
        sub_env = {**os.environ, 'PYTHONPATH': str(_ROOT_DIR)}
        processes = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=sub_env) for _ in range(4)]
        try:
            outputs = [process.communicate(timeout=15) for process in processes]
            self.assertEqual([process.returncode for process in processes], [0] * 4,
                             [err.decode('utf-8') for out, err in outputs])
            self.assertEqual([json.loads(out)['attempt'] for out, err in outputs], [3] * 4)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(len(self.recovery_events()), 2)

    def test_concurrent_confirmed_controllers_publish_one_ready_copy_and_one_increment(self):
        self.assertTrue(callable(getattr(self.bus, 'recover_claim', None)))
        def controller(_):
            bus = Bus(self.bus.root)
            return bus.recover_claim('workbuddy', self.msg.id, owner_confirmed_inactive=True).attempt
        with ThreadPoolExecutor(max_workers=4) as pool:
            attempts = list(pool.map(controller, range(4)))
        self.assertEqual(attempts, [3] * 4)
        self.assertEqual(len(list(self.dirs['ready'].glob('*.json'))), 1)
        self.assertFalse(self.source.exists())
        self.assertEqual(len(self.recovery_events()), 2)

    def test_unsafe_identity_or_private_root_are_refused_before_reading(self):
        self.assertTrue(callable(getattr(self.bus, 'recover_claim', None)))
        for agent, identity in (('../escape', self.msg.id), ('workbuddy', '../escape')):
            with self.assertRaises(BusError):
                self.bus.recover_claim(agent, identity, owner_confirmed_inactive=True)
        private = Path(self.temp.name) / '.workbuddy' / 'no-read'
        with self.assertRaises(BusError):
            Bus(private).recover_claim('workbuddy', self.msg.id, owner_confirmed_inactive=True)
        self.assertFalse(private.parent.exists())

    def test_nonwindows_recovery_is_explicitly_unsupported_and_leaves_claim(self):
        before = self.snapshot()
        with patch('agentbus.bus.os.name', 'posix'):
            with self.assertRaisesRegex(BusError, '平台'):
                self.recover(owner_confirmed_inactive=True)
        self.assertEqual(before, self.snapshot())

    def test_cli_recovery_requires_confirmation_known_member_and_returns_formal_json(self):
        argv = [sys.executable, '-X', 'utf8', '-m', 'agentbus', 'recover',
                '--workspace', str(self.cfg.workspace), '--agent', 'workbuddy', '--id', self.msg.id]
        before = self.snapshot()
        sub_env = {**os.environ, 'PYTHONPATH': str(_ROOT_DIR)}
        rejected = subprocess.run(argv, capture_output=True, env=sub_env)
        self.assertEqual(rejected.returncode, 4)
        self.assertEqual(before, self.snapshot())
        unknown = [*argv, '--owner-confirmed-inactive']
        unknown[unknown.index('workbuddy')] = 'unknown'
        self.assertEqual(subprocess.run(unknown, capture_output=True, env=sub_env).returncode, 4)
        accepted = subprocess.run([*argv, '--owner-confirmed-inactive'], capture_output=True, env=sub_env)
        self.assertEqual(accepted.returncode, 0, accepted.stderr.decode('utf-8'))
        payload = json.loads(accepted.stdout.decode('utf-8'))
        self.assertEqual(payload['id'], self.msg.id)
        self.assertEqual(payload['attempt'], 3)
        self.assertEqual(payload['confirmation_source'], 'caller_assertion')
        repeated = subprocess.run([*argv, '--owner-confirmed-inactive'], capture_output=True, env=sub_env)
        self.assertEqual(repeated.returncode, 0)
        self.assertEqual(json.loads(repeated.stdout)['attempt'], 3)


if __name__ == '__main__':
    unittest.main()
