"""Real OpenCode finding: native result plus runner result must not duplicate.
Source: Codex, 2026-10-02.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from pathlib import Path

from agentbus.adapters import Result
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load
from agentbus.director import Director, Summary


class NativeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = load(Path(self.tmp.name), create=True)
        self.director = Director(cfg)
        self.addCleanup(self.director.close)
        self.bus = self.director.bus
        self.msg = self.bus.make('codex', 'workbuddy', 'task', body='bounded task',
                                 result_to='codex')
        self.bus.post(self.msg)
        self.claimed = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])

    def run_with_native(self, recipients, native_code=0, native_artifacts=None):
        bus, msg = self.bus, self.claimed
        class NativeAdapter:
            def run(self, prompt, cwd, timeout_s):
                for recipient in recipients:
                    reply = bus.make('workbuddy', recipient, 'result',
                                     body='actual native peer receipt', reply_to=msg.id,
                                     source_claim_epoch=msg.claim_epoch, source_claim_id=msg.claim_id)
                    reply.exit_code = native_code
                    reply.artifacts = list(native_artifacts or [])
                    bus.post(reply)
                return Result(0, 'final chat summary', 0.1)
        self.director._adapters['workbuddy'] = NativeAdapter()
        summary = Summary()
        self.director._handle('workbuddy', self.claimed, summary, review=False)
        return summary

    def test_reuses_native_result_and_commits_source_without_second_receipt(self):
        summary = self.run_with_native(['codex'])
        replies = self.bus.find_results(self.msg.id, sender='workbuddy')
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].body, 'actual native peer receipt')
        source = self.bus.find_message(self.msg.id)
        self.assertEqual(source.status, 'done')
        self.assertEqual(source.result, 'actual native peer receipt')
        self.assertEqual(summary.completed, 1)

    def test_wrong_native_route_is_visible_and_not_silently_duplicated(self):
        summary = self.run_with_native(['trae'])
        self.assertEqual(len(self.bus.find_results(self.msg.id, sender='workbuddy')), 1)
        self.assertEqual(self.bus.find_message(self.msg.id).status, 'blocked')
        self.assertTrue(self.bus.pending('human'))
        self.assertEqual(summary.completed, 0)

    def test_multiple_native_receipts_require_clarification_without_a_third(self):
        self.run_with_native(['codex', 'codex'])
        self.assertEqual(len(self.bus.find_results(self.msg.id, sender='workbuddy')), 2)
        self.assertEqual(self.bus.find_message(self.msg.id).status, 'blocked')
        self.assertTrue(self.bus.pending('human'))

    def test_native_failure_is_not_promoted_to_success_by_end_turn(self):
        summary = self.run_with_native(['codex'], native_code=1)
        self.assertEqual(len(self.bus.find_results(self.msg.id, sender='workbuddy')), 1)
        self.assertEqual(self.bus.find_message(self.msg.id).status, 'failed')
        self.assertEqual(summary.completed, 0)
        self.assertEqual(summary.failed, 1)

    def test_invalid_optional_native_manifest_blocks_source_before_acceptance(self):
        artifact_path = self.director.cfg.output_root / 'native-optional-artifact.txt'
        payload = b'present but native receipt hash is wrong'
        artifact_path.write_bytes(payload)
        bad_manifest = [{'path': artifact_path.name, 'bytes': len(payload),
                         'sha256': '0' * 64}]

        summary = self.run_with_native(['codex'], native_artifacts=bad_manifest)

        source = self.bus.find_message(self.msg.id)
        self.assertEqual(source.status, 'failed')
        self.assertIn('artifact manifest mismatch', source.result)
        self.assertEqual(summary.failed, 1)
        self.assertTrue(self.bus.pending('human'))

    def test_received_optional_manifest_mismatch_is_blocked_before_ack(self):
        artifact_path = self.director.cfg.output_root / 'received-optional-artifact.txt'
        payload = b'present but received receipt hash is wrong'
        artifact_path.write_bytes(payload)
        receipt = self.bus.make(
            'workbuddy', 'codex', 'result', body='reported success', reply_to=self.msg.id,
            source_claim_epoch=self.claimed.claim_epoch, source_claim_id=self.claimed.claim_id,
            artifacts=[{'path': artifact_path.name, 'bytes': len(payload), 'sha256': '0' * 64}],
            exit_code=0)
        self.bus.post(receipt)
        claimed_receipt = self.bus.claim('codex', self.bus.pending('codex')[0])

        self.director._handle('codex', claimed_receipt, Summary(), review=False)

        self.assertEqual(self.bus.find_message(receipt.id).status, 'blocked')
        self.assertTrue(self.bus.pending('human'))

    def test_result_index_groups_only_receipts_for_one_recipient(self):
        task_two = self.bus.make('codex', 'workbuddy', 'task', result_to='codex')
        self.bus.post(task_two)
        self.bus.heartbeat('workbuddy', self.claimed, captured_ticket(self.claimed),
                           note='test receipt one started')
        first = self.bus.commit_result('workbuddy', self.claimed, captured_ticket(self.claimed), body='one')
        claimed_two = self.bus.claim('workbuddy', self.bus.pending('workbuddy')[0])
        self.bus.heartbeat('workbuddy', claimed_two, captured_ticket(claimed_two),
                           note='test receipt two started')
        second = self.bus.commit_result('workbuddy', claimed_two, captured_ticket(claimed_two), body='two')

        index = self.bus.index_results('codex')

        self.assertEqual({key: [message.id for message in value] for key, value in index.items()},
                         {self.msg.id: [first.id], task_two.id: [second.id]})

    def test_execution_respects_configured_task_timeout(self):
        seen = []
        class TimedAdapter:
            def run(self, prompt, cwd, timeout_s):
                seen.append(timeout_s)
                return Result(0, 'bounded result', 0.1)
        self.director.cfg.data['gates']['task_timeout_s'] = 15
        self.claimed.budget_s = 1000
        self.director._adapters['workbuddy'] = TimedAdapter()
        self.director.execute('workbuddy', self.claimed)
        self.assertLessEqual(seen[0], 15)


if __name__ == '__main__':
    unittest.main()
