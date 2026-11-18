"""Independent child oversight and real retry dedup. Source: Codex, 2026-10-02."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


from receipt_helpers import receipt_ticket
import tempfile
import unittest
from pathlib import Path

from agentbus.config import load
from agentbus.director import Director, Summary


class OversightChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load(Path(self.temp.name), create=True)
        self.director = Director(self.cfg)
        self.addCleanup(self.director.close)
        self.bus = self.director.bus
        self.goal = self.bus.make('human', 'codex', 'goal', body='Independent project branches',
                                   result_to='human')
        self.bus.post(self.goal)

    def child(self, worker='trae', title='branch'):
        task = self.bus.make('codex', worker, 'task', title=title, body='Bounded independent work',
                             reply_to=self.goal.id, result_to='codex')
        self.bus.post(task)
        return task

    def failed_review(self, task):
        task.result = 'Specific failure evidence'
        task.exit_code = 1
        self.bus.finish(task.to, task, 'failed', ticket=receipt_ticket(task))
        return self.director.schedule_peer_review(task, task.to, 'failed')

    def reviews(self):
        return [self.bus.read(path) for agent in self.cfg.enabled_workers()
                for path in self.bus.pending(agent, message_types=['review'])]

    def test_different_workers_under_same_goal_each_receive_failure_review(self):
        first = self.child('trae', 'branch A')
        second = self.child('workbuddy', 'branch B')
        self.assertIsNotNone(self.failed_review(first))
        self.assertIsNotNone(self.failed_review(second))
        self.assertEqual(len(self.reviews()), 2)
        queued = [event for event in self.bus.events() if event.get('event') == 'peer_review_queued']
        self.assertEqual({event['source_task_id'] for event in queued}, {first.id, second.id})
        self.assertEqual({event['subject'] for event in queued}, {'trae', 'workbuddy'})

    def test_same_worker_independent_branches_do_not_suppress_each_other(self):
        first = self.child('trae', 'independent A')
        second = self.child('trae', 'independent B')
        self.assertIsNotNone(self.failed_review(first))
        self.assertIsNotNone(self.failed_review(second))
        self.assertEqual(len(self.reviews()), 2)

    def test_goal_supervision_is_separate_from_its_child_task_supervision(self):
        child = self.child()
        self.assertIsNotNone(self.failed_review(child))
        self.goal.result = 'Coordinator failure evidence'
        self.bus.finish('codex', self.goal, 'failed', ticket=receipt_ticket(self.goal))
        review = self.director.schedule_peer_review(self.goal, 'codex', 'failed')
        self.assertIsNotNone(review)
        self.assertEqual(len(self.reviews()), 2)
        self.assertEqual(review.reply_to, self.goal.id)
        events = [event for event in self.bus.events() if event.get('event') == 'peer_review_queued']
        self.assertEqual({event['source_task_id'] for event in events}, {child.id, self.goal.id})

    def test_job_retry_links_to_failed_task_and_keeps_one_chain_review(self):
        first = self.child()
        self.assertIsNotNone(self.failed_review(first))
        summary = Summary()
        self.assertTrue(self.director._retry_or_escalate(first, 'retry with evidence', summary))
        retry = self.bus.read(self.bus.pending('trae', message_types=['task'])[0])
        self.assertEqual(retry.reply_to, first.id)
        self.assertEqual((retry.attempt, retry.hop), (1, 1))
        self.assertIsNone(self.failed_review(retry))
        self.assertTrue(self.director._retry_or_escalate(retry, 'second retry', summary))
        last = self.bus.read(self.bus.pending('trae', message_types=['task'])[0])
        self.assertEqual(last.reply_to, retry.id)
        self.assertEqual((last.attempt, last.hop), (2, 2))
        self.assertIsNone(self.failed_review(last))
        self.assertEqual(len(self.reviews()), 1)

    def test_legacy_standalone_retry_task_parent_keeps_deduplication(self):
        first = self.bus.make('human', 'trae', 'task', body='Standalone original', result_to='human')
        self.bus.post(first)
        self.assertIsNotNone(self.failed_review(first))
        retry = self.bus.make('codex', 'trae', 'task', body='Standalone retry', result_to='human',
                              reply_to=first.id, hop=1, attempt=1)
        self.bus.post(retry)
        self.assertIsNone(self.failed_review(retry))
        self.assertEqual(len(self.reviews()), 1)

    def test_independent_followup_task_has_its_own_review_despite_task_parent(self):
        first = self.child()
        self.assertIsNotNone(self.failed_review(first))
        followup = self.bus.make('codex', 'trae', 'task', body='New followup delivery',
                                 result_to='codex', reply_to=first.id, hop=1, attempt=0)
        self.bus.post(followup)
        self.assertIsNotNone(self.failed_review(followup))
        self.assertEqual(len(self.reviews()), 2)

    def test_claim_recovery_on_same_task_id_does_not_create_second_review(self):
        first = self.child()
        self.assertIsNotNone(self.failed_review(first))
        first.attempt += 1
        self.assertIsNone(self.director.schedule_peer_review(first, first.to, 'failed'))
        self.assertEqual(len(self.reviews()), 1)

    def test_review_itself_never_creates_another_review(self):
        child = self.child()
        review = self.failed_review(child)
        self.assertIsNotNone(review)
        self.assertIsNone(self.director.schedule_peer_review(review, review.to, 'failed'))
        self.assertEqual(len(self.reviews()), 1)

    def old_review_record(self, source):
        review = self.bus.make('codex', 'zcode', 'review', body='Existing audited review',
                               result_to='codex', reply_to=source.id)
        self.bus.post(review)
        self.bus.audit(event='peer_review_queued', source_task_id=self.goal.id,
                       task_id=source.id, reviewer='zcode', subject=source.to, trigger='failed')
        return review

    def test_old_goal_key_review_deduplicates_its_child_but_not_goal(self):
        child = self.child()
        child.exit_code = 1
        self.bus.finish(child.to, child, 'failed', ticket=receipt_ticket(child))
        self.old_review_record(child)
        self.assertIsNone(self.director.schedule_peer_review(child, child.to, 'failed'))
        self.goal.exit_code = 1
        self.bus.finish('codex', self.goal, 'failed', ticket=receipt_ticket(self.goal))
        self.assertIsNotNone(self.director.schedule_peer_review(self.goal, 'codex', 'failed'))
        self.assertEqual(len(self.reviews()), 2)

    def test_old_goal_key_retry_review_still_deduplicates_true_task_chain(self):
        child = self.child()
        self.bus.finish(child.to, child, 'failed', ticket=receipt_ticket(child))
        self.assertTrue(self.director._retry_or_escalate(child, 'retry original branch', Summary()))
        retry = self.bus.read(self.bus.pending('trae', message_types=['task'])[0])
        self.bus.finish(retry.to, retry, 'failed', ticket=receipt_ticket(retry))
        self.old_review_record(retry)
        self.assertIsNone(self.director.schedule_peer_review(child, child.to, 'failed'))
        self.assertIsNone(self.director.schedule_peer_review(retry, retry.to, 'failed'))
        self.assertEqual(len(self.reviews()), 1)


if __name__ == '__main__':
    unittest.main()
