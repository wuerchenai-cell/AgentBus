"""Codex, 2026-10-03: caught IO failures during isolated message updates.

These fixtures are not a hard-power-loss test or an exactly-once guarantee.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.bus import Bus


class HalfWriteFailure:
    """Write real partial bytes, then simulate a caught IO failure."""
    def __init__(self, stream):
        self.stream = stream
    def __enter__(self):
        self.stream.__enter__()
        return self
    def __exit__(self, *args):
        return self.stream.__exit__(*args)
    def write(self, content):
        self.stream.write(content[:max(1, len(content) // 2)])
        self.stream.flush()
        raise OSError('isolated partial write failure')


class BusAtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agentbus-atomic-message-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bus = Bus(self.root / '.agentbus')
        self.bus.init()
        message = self.bus.make('codex', 'workbuddy', 'task', body='原始完整任务',
                                result_to='codex', budget_s=41, hop=1)
        self.bus.post(message)
        self.message = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        self.path = self.bus.dirs('workbuddy')['processing'] / (message.id + '.json')
        self.original_bytes = self.path.read_bytes()
        self.updated = replace(self.message, result='中文更新结果；不得截断原消息', status='running')
        self.foreign_temp = self.path.parent / 'unrelated-message.write.tmp'
        self.foreign_temp.write_bytes(b'another writer owns this file')

    def fail_partial_write(self):
        original_open = Path.open
        def opened(path, mode='r', *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return HalfWriteFailure(stream) if mode in ('w', 'x') else stream
        return patch.object(Path, 'open', opened)

    def assert_clean(self):
        self.assertEqual(list(self.path.parent.glob('.*.write.tmp')), [])
        self.assertEqual(self.foreign_temp.read_bytes(), b'another writer owns this file')

    def test_partial_update_write_failure_preserves_complete_previous_message(self):
        with self.fail_partial_write():
            with self.assertRaisesRegex(OSError, 'partial write failure'):
                self.bus._write(self.path, self.updated)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(self.bus.read(self.path).body, '原始完整任务')
        self.assert_clean()

    def test_replace_failure_preserves_previous_message_and_cleans_only_own_temp(self):
        with patch('agentbus.bus.os.replace', side_effect=PermissionError('isolated replace failure')), \
                patch('agentbus.bus.time.sleep'):
            with self.assertRaisesRegex(PermissionError, 'replace failure'):
                self.bus._write(self.path, self.updated)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assert_clean()

    def test_transient_permission_error_winerror5_retries_then_replaces_complete_message(self):
        # Codex, 2026-10-03: actual Windows read sharing can report 5, not 32/33.
        import os
        real_replace = os.replace
        failure = PermissionError(13, 'isolated transient read sharing')
        failure.winerror = 5
        calls = []
        def replacing(staging, target):
            calls.append((staging, target))
            self.assertEqual(json.loads(Path(staging).read_text(encoding='utf-8')),
                             self.updated.to_dict())
            if len(calls) <= 2:
                self.assertEqual(self.path.read_bytes(), self.original_bytes)
                raise failure
            return real_replace(staging, target)
        with patch('agentbus.bus.os.replace', side_effect=replacing), \
                patch('agentbus.bus.time.sleep') as sleep:
            self.bus._write(self.path, self.updated)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({staging for staging, _ in calls}), 1)
        self.assertEqual(sleep.call_args_list, [call(0.01)] * 2)
        self.assertEqual(self.bus.read(self.path).to_dict(), self.updated.to_dict())
        self.assertEqual(self.bus.read(self.path).attempt, self.message.attempt)
        self.assert_clean()

    def test_persistent_permission_error_has_bounded_attempts_and_preserves_original(self):
        failure = PermissionError(13, 'isolated persistent read sharing')
        failure.winerror = 5
        with patch('agentbus.bus.os.replace', side_effect=failure) as replacing, \
                patch('agentbus.bus.time.sleep') as sleep:
            with self.assertRaises(PermissionError) as caught:
                self.bus._write(self.path, self.updated)
        self.assertIs(caught.exception, failure)
        self.assertEqual(replacing.call_count, 50)
        self.assertEqual(sleep.call_args_list, [call(0.01)] * 49)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 0.5)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assert_clean()

    def test_non_permission_replace_error_is_not_retried(self):
        failure = OSError(5, 'isolated non-permission IO failure')
        with patch('agentbus.bus.os.replace', side_effect=failure) as replacing, \
                patch('agentbus.bus.time.sleep') as sleep:
            with self.assertRaises(OSError) as caught:
                self.bus._write(self.path, self.updated)
        self.assertIs(caught.exception, failure)
        self.assertEqual(replacing.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assert_clean()

    def test_successful_update_is_complete_utf8_and_preserves_message_fields(self):
        self.bus._write(self.path, self.updated)
        content = self.path.read_bytes()
        decoded = json.loads(content.decode('utf-8', errors='strict'))
        self.assertEqual(decoded, self.updated.to_dict())
        self.assertIn('中文更新结果'.encode('utf-8'), content)
        self.assertEqual(self.bus.read(self.path).budget_s, 41)
        self.assertEqual(self.bus.read(self.path).hop, 1)
        self.assert_clean()

    def test_new_staging_file_remains_usable_by_existing_publication_protocol(self):
        staging = self.bus.dirs('workbuddy')['tmp'] / 'fresh-message.json'
        self.assertFalse(staging.exists())
        self.bus._write(staging, self.updated)
        self.assertEqual(self.bus.read(staging).to_dict(), self.updated.to_dict())
        self.assertEqual(list(staging.parent.glob('.*.write.tmp')), [])
        self.assert_clean()

    def test_claim_update_failure_keeps_moved_message_readable_without_pid_ownership(self):
        from agentbus.claim_fencing import captured_ticket
        from agentbus.config import load
        load(self.root, create=True)
        self.bus.relinquish_claim('workbuddy', self.message.id,
                                  ticket=captured_ticket(self.message), reason='Independent failure fixture')
        message = self.bus.make('codex', 'workbuddy', 'task', body='只在隔离信箱认领',
                                result_to='codex', budget_s=43)
        self.bus.post(message)
        ready = self.bus.dirs('workbuddy')['ready'] / (message.id + '.json')
        before = ready.read_bytes()
        with self.fail_partial_write():
            with self.assertRaisesRegex(OSError, 'partial write failure'):
                self.bus.claim('workbuddy', ready)
        moved = self.path.parent / ready.name
        self.assertTrue(ready.exists())
        self.assertFalse(moved.exists())
        self.assertEqual(ready.read_bytes(), before)
        observed = self.bus.read(ready)
        self.assertEqual(observed.status, 'pending')
        self.assertEqual(observed.claimed_by, '')
        self.assertEqual(observed.budget_s, 43)
        self.assertFalse((self.bus.dirs('workbuddy')['done'] / ready.name).exists())
        self.assertEqual(self.bus.pending('workbuddy'), [ready])
        self.assertFalse(any(event.get('event') == 'claim' and event.get('id') == message.id
                             for event in self.bus.events()))
        self.assert_clean()

    def test_serialization_failure_preserves_previous_message_without_temp_files(self):
        invalid = replace(self.updated, refs=[object()])
        with self.assertRaises(TypeError):
            self.bus._write(self.path, invalid)
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()
