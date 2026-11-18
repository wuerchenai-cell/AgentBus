# -*- coding: utf-8 -*-
"""Task 3 反例测试：跨宿主证据失效纯函数 evaluate_evidence。

来源：ZCode（平级成员），2026-10-05，任务 m-1791169024291-b8e3a2（job j-b2ea92e011777b6bcfa403cc91d2b68d）。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。
先红后绿：本文件先于实现编写并运行失败（ModuleNotFoundError），实现后全部通过。
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest

from agentbus.skill_evidence import SCOPE_NOTE, evaluate_evidence

NOW = 1_800_000_000.0
CTX = {
    "kind": "invocation",
    "host": "zcode",
    "host_version": "1.2.3",
    "machine_scope": "this-machine",
    "project_scope": "proj-a",
    "route": "desktop",
    "content_digest": "a" * 64,
}


def evidence(**over):
    base = dict(CTX)
    base.update({"source": "skills/demo", "outcome": "passed", "observed_at_ts": NOW - 3600})
    base.update(over)
    return base


class EvaluateEvidenceTests(unittest.TestCase):
    def test_fresh_passing_match_is_verified(self):
        out = evaluate_evidence(evidence(), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "verified")
        self.assertEqual(out["reason_codes"], [])
        self.assertIsNotNone(out["original_evidence"])

    def test_missing_evidence_unverified(self):
        out = evaluate_evidence(None, CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("EVIDENCE_MISSING", out["reason_codes"])
        self.assertIsNone(out["original_evidence"])

    def test_malformed_evidence_unverified(self):
        out = evaluate_evidence("oops", CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("EVIDENCE_MALFORMED", out["reason_codes"])

    def test_60_day_old_passed_downgrades_and_history_kept(self):
        ev = evidence(observed_at_ts=NOW - 60 * 86400)
        out = evaluate_evidence(ev, CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("EVIDENCE_TOO_OLD", out["reason_codes"])
        self.assertEqual(ev["outcome"], "passed")
        self.assertIs(out["original_evidence"], ev)

    def test_30_day_boundary_exact_age_stays_verified(self):
        ev = evidence(observed_at_ts=NOW - 2592000)
        out = evaluate_evidence(ev, CTX, now_ts=NOW, max_age_s=2592000)
        self.assertEqual(out["state"], "verified")

    def test_one_second_past_max_age_downgrades(self):
        ev = evidence(observed_at_ts=NOW - 2592000 - 1)
        out = evaluate_evidence(ev, CTX, now_ts=NOW, max_age_s=2592000)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("EVIDENCE_TOO_OLD", out["reason_codes"])

    def test_future_observed_at_unverified(self):
        out = evaluate_evidence(evidence(observed_at_ts=NOW + 1), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("OBSERVED_AT_FUTURE", out["reason_codes"])

    def test_bad_observed_at_unverified(self):
        for bad in ("yesterday", None, float("nan"), float("inf"), True):
            out = evaluate_evidence(evidence(observed_at_ts=bad), CTX, now_ts=NOW)
            self.assertEqual(out["state"], "unverified", msg=repr(bad))
            self.assertIn("OBSERVED_AT_INVALID", out["reason_codes"], msg=repr(bad))

    def test_expiry_handling(self):
        expired = evaluate_evidence(evidence(expires_at_ts=NOW - 1), CTX, now_ts=NOW)
        self.assertEqual(expired["state"], "unverified")
        self.assertIn("EVIDENCE_EXPIRED", expired["reason_codes"])

        bad = evaluate_evidence(evidence(expires_at_ts="tomorrow"), CTX, now_ts=NOW)
        self.assertEqual(bad["state"], "unverified")
        self.assertIn("EXPIRES_AT_INVALID", bad["reason_codes"])

        valid = evaluate_evidence(evidence(expires_at_ts=NOW + 86400), CTX, now_ts=NOW)
        self.assertEqual(valid["state"], "verified")

    def test_identity_field_mismatch_downgrades(self):
        changes = {
            "kind": "discovery",
            "host": "trae",
            "host_version": "9.9.9",
            "machine_scope": "other-machine",
            "project_scope": "proj-b",
            "route": "cli",
            "content_digest": "b" * 64,
        }
        for field, value in changes.items():
            ev = evidence(**{field: value})
            out = evaluate_evidence(ev, CTX, now_ts=NOW)
            self.assertEqual(out["state"], "unverified", msg=field)
            self.assertIn(field.upper() + "_MISMATCH", out["reason_codes"], msg=field)

    def test_exact_string_match_required(self):
        out = evaluate_evidence(evidence(host="zcode "), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("HOST_MISMATCH", out["reason_codes"])

    def test_missing_evidence_field_is_mismatch_not_crash(self):
        ev = evidence()
        del ev["content_digest"]
        out = evaluate_evidence(ev, CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("CONTENT_DIGEST_MISMATCH", out["reason_codes"])

    def test_install_evidence_not_valid_for_invocation(self):
        out = evaluate_evidence(evidence(kind="install"), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("KIND_MISMATCH", out["reason_codes"])

    def test_failed_outcome_unverified_and_kept(self):
        ev = evidence(outcome="failed")
        out = evaluate_evidence(ev, CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("OUTCOME_NOT_PASSED", out["reason_codes"])
        self.assertEqual(ev["outcome"], "failed")

    def test_missing_context_field_downgrades(self):
        ctx = dict(CTX)
        del ctx["route"]
        out = evaluate_evidence(evidence(), ctx, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("ROUTE_MISMATCH", out["reason_codes"])

    def test_single_evidence_scope_note_documented(self):
        self.assertIn("不等同于所有聊天", SCOPE_NOTE)
        out = evaluate_evidence(evidence(), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "verified")

    def test_invalid_now_and_max_age_raise(self):
        with self.assertRaises(ValueError):
            evaluate_evidence(evidence(), CTX, now_ts=float("nan"))
        with self.assertRaises(ValueError):
            evaluate_evidence(evidence(), CTX, now_ts=NOW, max_age_s=0)
        with self.assertRaises(ValueError):
            evaluate_evidence(evidence(), CTX, now_ts=NOW, max_age_s=-1)


    def test_max_age_over_30_days_is_rejected(self):
        # 60 天前证据 + 90 天参数：不允许放宽 30 天上限，必须拒绝
        ev = evidence(observed_at_ts=NOW - 60 * 86400)
        with self.assertRaises(ValueError):
            evaluate_evidence(ev, CTX, now_ts=NOW, max_age_s=90 * 86400)
        self.assertEqual(ev["outcome"], "passed")

    def test_huge_int_max_age_rejected_without_overflow(self):
        with self.assertRaises(ValueError):
            evaluate_evidence(evidence(), CTX, now_ts=NOW, max_age_s=10 ** 400)

    def test_empty_identity_fields_downgrade(self):
        for field in ("kind", "host", "host_version", "machine_scope",
                      "project_scope", "route", "content_digest"):
            ev = evidence(**{field: ""})
            out = evaluate_evidence(ev, CTX, now_ts=NOW)
            self.assertEqual(out["state"], "unverified", msg=field)
            self.assertIn(field.upper() + "_MISMATCH", out["reason_codes"], msg=field)

    def test_whitespace_only_identity_downgrades(self):
        out = evaluate_evidence(evidence(host="   "), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("HOST_MISMATCH", out["reason_codes"])

    def test_missing_or_blank_source_downgrades(self):
        ev = evidence()
        del ev["source"]
        out = evaluate_evidence(ev, CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("SOURCE_MISSING", out["reason_codes"])
        out = evaluate_evidence(evidence(source="   "), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("SOURCE_MISSING", out["reason_codes"])
        self.assertEqual(out["original_evidence"]["source"], "   ")

    def test_digest_format_enforced_even_when_strings_equal(self):
        # 两侧字符串相同但不是 64 位小写 hex：仍必须降级
        ctx = dict(CTX)
        ctx["content_digest"] = "A" * 64
        out = evaluate_evidence(evidence(content_digest="A" * 64), ctx, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("CONTENT_DIGEST_MISMATCH", out["reason_codes"])

    def test_digest_short_form_downgrades(self):
        out = evaluate_evidence(evidence(content_digest="a" * 63), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("CONTENT_DIGEST_MISMATCH", out["reason_codes"])

    def test_context_empty_identity_downgrades(self):
        ctx = dict(CTX)
        ctx["route"] = ""
        out = evaluate_evidence(evidence(), ctx, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("ROUTE_MISMATCH", out["reason_codes"])

    def test_huge_int_timestamps_degrade_without_overflow(self):
        out = evaluate_evidence(evidence(observed_at_ts=10 ** 400), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("OBSERVED_AT_FUTURE", out["reason_codes"])
        out = evaluate_evidence(evidence(observed_at_ts=-(10 ** 400)), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        self.assertIn("EVIDENCE_TOO_OLD", out["reason_codes"])
        out = evaluate_evidence(evidence(expires_at_ts=10 ** 400), CTX, now_ts=NOW)
        self.assertEqual(out["state"], "verified")

    def test_both_sides_blank_identity_downgrades(self):
        # 缺陷原始案例：两侧身份字段全为空字符串时旧实现判 verified
        blanks = {f: "" for f in CTX}
        ev = evidence(**blanks)
        ctx = dict(blanks)
        out = evaluate_evidence(ev, ctx, now_ts=NOW)
        self.assertEqual(out["state"], "unverified")
        codes = set(out["reason_codes"])
        self.assertIn("HOST_MISMATCH", codes)
        self.assertIn("CONTENT_DIGEST_MISMATCH", codes)

    def test_independent_route_carrier_note_documented(self):
        self.assertIn("尚无固定载体", SCOPE_NOTE)


if __name__ == "__main__":
    unittest.main()
