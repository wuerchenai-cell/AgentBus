import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus.routing import RouteDecision, resolve_result_route, resolve_result_to_arg


NOW = 1_000.0
PARTICIPANTS = {"codex", "trae", "workbuddy"}
BODY_DEADLINE = 2_000.0


class ResolveResultToArgTests(unittest.TestCase):
    def test_concrete_participant(self):
        self.assertEqual(resolve_result_to_arg("trae", sender="codex", director="workbuddy", participants=PARTICIPANTS), "trae")

    def test_sender_alias(self):
        self.assertEqual(resolve_result_to_arg("sender", sender="trae", director="codex", participants=PARTICIPANTS), "trae")

    def test_director_alias(self):
        self.assertEqual(resolve_result_to_arg("director", sender="trae", director="workbuddy", participants=PARTICIPANTS), "workbuddy")

    def test_empty_unknown_and_unavailable_targets_raise(self):
        for value, participants in (("", PARTICIPANTS), ("ghost", PARTICIPANTS), ("director", {"codex", "trae"})):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    resolve_result_to_arg(value, sender="codex", director="workbuddy", participants=participants)


class ResolveResultRouteTests(unittest.TestCase):
    def test_explicit_field(self):
        self.assertEqual(resolve_result_route("TraE", "结果收件人：workbuddy", PARTICIPANTS), RouteDecision("trae", "field"))

    def test_invalid_explicit_field_does_not_fall_back_to_body(self):
        self.assertEqual(resolve_result_route("ghost", "结果收件人：trae", PARTICIPANTS, now=NOW, body_compat_until=BODY_DEADLINE), RouteDecision(None, "invalid", "unknown result recipient: ghost"))

    def test_body_labels(self):
        labels = ("结果收件人", "回执投给", "结果回给", "结果发给")
        for label in labels:
            for colon in (":", "："):
                with self.subTest(label=label, colon=colon):
                    body = f"任务说明\n{label}{colon} TRAE \n请完成任务"
                    self.assertEqual(resolve_result_route(None, body, PARTICIPANTS, now=NOW, body_compat_until=BODY_DEADLINE), RouteDecision("trae", "body"))

    def test_expired_body_compat_is_missing(self):
        decision = resolve_result_route(None, "结果回给：trae", PARTICIPANTS, now=BODY_DEADLINE + 1, body_compat_until=BODY_DEADLINE)
        self.assertEqual(decision, RouteDecision(None, "missing"))

    def test_body_is_missing_without_deadline(self):
        self.assertEqual(resolve_result_route(None, "结果发给：trae", PARTICIPANTS, now=NOW), RouteDecision(None, "missing"))

    def test_missing_ambiguous_conflicting_and_unknown_body(self):
        cases = (
            ("没有收件人", RouteDecision(None, "missing")),
            ("结果收件人：trae\n回执投给：workbuddy", RouteDecision(None, "conflict", "conflicting result recipients: trae, workbuddy")),
            ("结果收件人：trae\n结果发给：TRAE", RouteDecision("trae", "body")),
            ("结果回给：ghost", RouteDecision(None, "conflict", "unknown result recipient: ghost")),
            ("结果发给：trae 和 workbuddy", RouteDecision(None, "conflict", "conflicting result recipients: trae, workbuddy")),
            ("结果发给：", RouteDecision(None, "conflict", "empty result recipient in body")),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(resolve_result_route(None, body, PARTICIPANTS, now=NOW, body_compat_until=BODY_DEADLINE), expected)

    def test_unknown_names_containing_participant_substrings_do_not_route(self):
        participants = PARTICIPANTS | {"zcode"}
        for value in ("notzcode", "zcode2", "prefix-codex"):
            with self.subTest(value=value):
                decision = resolve_result_route(
                    None, f"结果收件人：{value}", participants,
                    now=NOW, body_compat_until=BODY_DEADLINE)
                self.assertEqual(
                    decision,
                    RouteDecision(None, "conflict", f"unknown result recipient: {value}"))


if __name__ == "__main__":
    unittest.main()
