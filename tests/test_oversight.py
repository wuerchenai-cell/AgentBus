import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.oversight import OversightDecision, choose_reviewer, review_trigger


class ReviewTriggerTests(unittest.TestCase):
    def test_periodic_review_on_positive_multiples_only(self):
        self.assertEqual(review_trigger(message_type="task", status="done", attempt=0, hop=0, completed_count=3), OversightDecision(True, "periodic"))
        self.assertFalse(review_trigger(message_type="task", status="done", attempt=0, hop=0, completed_count=2).required)
        self.assertFalse(review_trigger(message_type="task", status="done", attempt=0, hop=0, completed_count=0).required)

    def test_custom_cadence_and_disabled_cadence(self):
        args = dict(message_type="goal", status="done", attempt=0, hop=0, completed_count=4)
        self.assertTrue(review_trigger(**args, review_every_n=2).required)
        self.assertFalse(review_trigger(**args, review_every_n=0).required)
        self.assertFalse(review_trigger(**args, review_every_n=-1).required)

    def test_immediate_failure_blocked_and_stuck_thresholds(self):
        for status in ("failed", "blocked"):
            with self.subTest(status=status):
                self.assertEqual(review_trigger(message_type="task", status=status, attempt=0, hop=0, completed_count=0), OversightDecision(True, status))
        self.assertTrue(review_trigger(message_type="task", status="running", attempt=2, hop=0, completed_count=0).required)
        self.assertTrue(review_trigger(message_type="task", status="running", attempt=0, hop=2, completed_count=0).required)

    def test_stuck_triggers_remain_when_periodic_cadence_disabled(self):
        self.assertTrue(review_trigger(message_type="task", status="blocked", attempt=0, hop=0, completed_count=0, review_every_n=0).required)

    def test_review_messages_never_trigger_recursively(self):
        self.assertFalse(review_trigger(message_type="review", status="failed", attempt=9, hop=9, completed_count=99).required)
        self.assertFalse(review_trigger(message_type="review", status="done", attempt=0, hop=0, completed_count=3).required)


class ReviewerSelectionTests(unittest.TestCase):
    def test_avoids_author_when_three_or_more_participants(self):
        reviewer = choose_reviewer(subject="worker", author="lead", participants={"worker", "lead", "peer"}, assignment_counts={})
        self.assertEqual(reviewer, "peer")

    def test_author_allowed_if_only_available_peer(self):
        self.assertEqual(choose_reviewer(subject="worker", author="lead", participants={"worker", "lead"}, assignment_counts={}), "lead")

    def test_tie_rotation_is_deterministic(self):
        people = {"c", "a", "b"}
        self.assertEqual(choose_reviewer(subject="x", author="nobody", participants=people, assignment_counts={}, rotation_index=0), "a")
        self.assertEqual(choose_reviewer(subject="x", author="nobody", participants=people, assignment_counts={}, rotation_index=1), "b")
        self.assertEqual(choose_reviewer(subject="x", author="nobody", participants=people, assignment_counts={}, rotation_index=4), "b")

    def test_least_assigned_candidate_wins(self):
        self.assertEqual(choose_reviewer(subject="x", author="nobody", participants={"a", "b", "c"}, assignment_counts={"a": 4, "b": 1, "c": 2}), "b")

    def test_rotation_applies_within_least_assigned_tie(self):
        counts = {"a": 1, "b": 1, "c": 5}
        self.assertEqual(choose_reviewer(subject="x", author="nobody", participants={"a", "b", "c"}, assignment_counts=counts, rotation_index=1), "b")

    def test_no_peer_returns_none(self):
        self.assertIsNone(choose_reviewer(subject="solo", author="solo", participants={"solo"}, assignment_counts={}))

    def test_decision_is_immutable(self):
        decision = review_trigger(message_type="task", status="done", attempt=0, hop=0, completed_count=3)
        with self.assertRaises(FrozenInstanceError):
            decision.required = False


if __name__ == "__main__":
    unittest.main()


