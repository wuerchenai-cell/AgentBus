# -*- coding: utf-8 -*-
"""WB-PLAN 持续计划引擎定向回归（无模型、纯函数、零 I/O）。

来源：WorkBuddy，2026-10-08；任务 m-1791457584492-83bbee。
运行：C:/Python314/python.exe -B -X utf8 -m unittest tests.test_plan_convergence -v
（在 authors/workbuddy 目录下运行）

覆盖要求：重复要求幂等、相似但不同需求、与在途任务冲突、历史已完成复用、用户撤回、评审缺席、
CAS 变化、过期事实、两项目隔离、中文、长文本截断、不可变输入、未采用不得派工。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import copy
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE))

from agentbus import change_control as cc          # noqa: E402
from agentbus import plan_convergence as pc        # noqa: E402

SHA_A = "a" * 64
SHA_B = "b" * 64

REQ_UI = "界面需要显示每个成员的真实状态灯，不能把配置存在当成在线"
REQ_DIST = "分发需要完成干净环境的安装、升级、回滚与卸载验收"
REQ_UI_NEAR = "界面需要显示每个成员的真实状态灯，不能把配置存在当成在线运行"


def manifest(path="成果/x/a.md", digest=SHA_A, bytes_=10):
    return [{"path": path, "bytes": bytes_, "sha256": digest}]


def snapshot(**overrides) -> dict:
    snap = {
        "workspace": "D:\\示例空间\\协作 样例",
        "observed_at": 1000.0,
        "adopted_revision": {"revision_id": "r-abc", "content_sha256": SHA_A},
        "current_revision_sha256": SHA_A,
        "taskbook": {
            "acceptance": [
                {"id": "A9", "title": "产品界面与进度：真实状态灯与下一步"},
                {"id": "A10", "title": "安装与分发：安装升级回滚卸载"},
            ],
            "work_items": [
                {"id": "T1", "title": "文件路线与首用入口", "status": "completed",
                 "owner": "codex", "artifacts": ["成果/a.md"], "acceptance": ["A1"]},
                {"id": "T6", "title": "产品交付与商业发布门 安装与分发",
                 "status": "planned", "owner": "codex",
                 "artifacts": ["成果/release.md"], "acceptance": ["A10"]},
            ],
        },
        "changes": {},
    }
    for key, value in overrides.items():
        if key in ("taskbook", "changes", "adopted_revision"):
            snap[key] = value
        else:
            snap[key] = value
    return snap


class ChangeIdTests(unittest.TestCase):
    def test_same_requirement_is_idempotent(self):
        first = cc.stable_change_id(REQ_UI)
        second = cc.stable_change_id("  " + REQ_UI + "  ")
        full_width = cc.stable_change_id(REQ_UI.replace("界面", "界面"))
        self.assertEqual(first, second)
        self.assertEqual(first, full_width)
        self.assertTrue(first.startswith("CH-"))

    def test_different_requirements_get_different_ids(self):
        self.assertNotEqual(cc.stable_change_id(REQ_UI), cc.stable_change_id(REQ_DIST))

    def test_projects_are_isolated_but_idempotent_within_project(self):
        a1 = cc.stable_change_id(REQ_UI, project_id="proj-a")
        a2 = cc.stable_change_id(REQ_UI, project_id="proj-a")
        b1 = cc.stable_change_id(REQ_UI, project_id="proj-b")
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b1)

    def test_long_requirement_is_truncated_for_display_but_id_uses_full_text(self):
        long_a = "要求" + "长" * 400
        long_b = "要求" + "长" * 401
        record = cc.classify_change(long_a, snapshot=snapshot(), now_ts=1000.0)
        self.assertTrue(record["requirement_truncated"])
        self.assertLessEqual(len(record["requirement"]), cc.MAX_DISPLAY_CHARS)
        self.assertNotEqual(cc.stable_change_id(long_a), cc.stable_change_id(long_b))
        with self.assertRaises(cc.ChangeControlError):
            cc.stable_change_id("x" * (cc.MAX_REQUIREMENT_CHARS + 1))

    def test_empty_requirement_is_rejected(self):
        for bad in ("", "   ", "\n\t", None, 7):
            with self.subTest(value=bad):
                with self.assertRaises(cc.ChangeControlError):
                    cc.normalize_requirement(bad)


class ClassificationTests(unittest.TestCase):
    def test_new_requirement_is_proposed_and_cannot_dispatch(self):
        record = cc.classify_change(REQ_UI, snapshot=snapshot(), now_ts=1000.0)
        self.assertEqual(record["status"], "proposed")
        self.assertFalse(record["adopted"])
        self.assertFalse(record["may_dispatch"])
        self.assertIn("A9", record["affected_acceptance"])
        self.assertTrue(record["research_checklist"])
        self.assertIn("界面类要求需补普通窗口实际走查证据，不能用单测代替",
                      record["research_checklist"])

    def test_similar_but_different_requirement_needs_clarification(self):
        existing = cc.classify_change(REQ_UI, snapshot=snapshot(), now_ts=1000.0)
        snap = snapshot(changes={existing["change_id"]: existing})
        record = cc.classify_change(REQ_UI_NEAR, snapshot=snap, now_ts=1000.0)
        self.assertNotEqual(record["change_id"], existing["change_id"])
        self.assertEqual(record["suggested_disposition"], "needs_clarification")
        self.assertTrue(record["flags"]["similar"])
        self.assertGreater(record["flags"]["similar"][0]["similarity"], 0.5)

    def test_identical_requirement_reuses_existing_change(self):
        existing = cc.classify_change(REQ_UI, snapshot=snapshot(), now_ts=1000.0)
        snap = snapshot(changes={existing["change_id"]: existing})
        record = cc.classify_change(REQ_UI, snapshot=snap, now_ts=1000.0)
        self.assertEqual(record["change_id"], existing["change_id"])
        self.assertEqual(record["suggested_disposition"], "research")
        self.assertEqual(record["flags"]["duplicate"], existing["change_id"])

    def test_adopted_duplicate_reuses_instead_of_redoing(self):
        existing = cc.adopt_change(
            cc.classify_change(REQ_UI, snapshot=snapshot(), now_ts=1000.0),
            adopted_by="codex", at=1001.0, revision_id="r-abc", content_sha256=SHA_A)
        snap = snapshot(changes={existing["change_id"]: existing})
        record = cc.classify_change(REQ_UI, snapshot=snap, now_ts=1002.0)
        self.assertEqual(record["suggested_disposition"], "duplicate_reuse")
        self.assertEqual(record["duplicate_of"], existing["change_id"])

    def test_conflict_with_in_flight_work_item_defers(self):
        snap = snapshot()
        snap["taskbook"]["work_items"][1]["status"] = "in_progress"
        record = cc.classify_change(REQ_DIST, snapshot=snap, now_ts=1000.0)
        self.assertTrue(record["flags"]["conflict"])
        self.assertEqual(record["suggested_disposition"], "defer")
        self.assertEqual(record["flags"]["conflict"][0]["work_item"], "T6")

    def test_completed_similar_change_is_reused(self):
        done = cc.adopt_change(
            cc.classify_change(REQ_UI, snapshot=snapshot(), now_ts=1000.0),
            adopted_by="codex", at=1001.0, revision_id="r-abc", content_sha256=SHA_A)
        done["status"] = "completed"
        snap = snapshot(changes={done["change_id"]: done})
        record = cc.classify_change(REQ_UI_NEAR, snapshot=snap, now_ts=1002.0)
        self.assertEqual(record["suggested_disposition"], "duplicate_reuse")
        self.assertEqual(record["duplicate_of"], done["change_id"])

    def test_missing_review_is_flagged_when_evaluators_assigned(self):
        no_review = cc.classify_change(REQ_UI, snapshot=snapshot(), now_ts=1000.0,
                                       evaluators=["trae"])
        with_review = cc.classify_change(REQ_UI, snapshot=snapshot(), now_ts=1000.0,
                                         evaluators=["trae"], reviews=["m-1"])
        self.assertTrue(no_review["flags"]["review_missing"])
        self.assertFalse(with_review["flags"]["review_missing"])

    def test_cas_baseline_change_and_unpinned_baseline_are_stale(self):
        changed = snapshot(current_revision_sha256=SHA_B)
        record = cc.classify_change(REQ_UI, snapshot=changed, now_ts=1000.0)
        self.assertIn("CAS_BASELINE_CHANGED", record["flags"]["stale"])
        self.assertEqual(record["suggested_disposition"], "research")
        unpinned = snapshot(adopted_revision={})
        record = cc.classify_change(REQ_UI, snapshot=unpinned, now_ts=1000.0)
        self.assertIn("BASELINE_UNPINNED", record["flags"]["stale"])

    def test_expired_facts_are_flagged(self):
        old = snapshot(observed_at=0.0)
        record = cc.classify_change(REQ_UI, snapshot=old, now_ts=10 ** 7)
        self.assertIn("SNAPSHOT_STALE", record["flags"]["stale"])

    def test_inputs_are_not_mutated(self):
        snap = snapshot()
        before = copy.deepcopy(snap)
        requirement = REQ_UI
        cc.classify_change(requirement, snapshot=snap, now_ts=1000.0,
                           evaluators=["trae"], reviews=[])
        self.assertEqual(snap, before)
        self.assertEqual(requirement, REQ_UI)

    def test_malformed_snapshot_is_rejected(self):
        for bad in (None, [], "snapshot"):
            with self.subTest(value=bad):
                with self.assertRaises(cc.ChangeControlError):
                    cc.classify_change(REQ_UI, snapshot=bad, now_ts=1.0)
        with self.assertRaises(cc.ChangeControlError):
            cc.classify_change(REQ_UI, snapshot={"changes": []}, now_ts=1.0)


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.record = cc.classify_change(REQ_UI, snapshot=snapshot(), now_ts=1000.0)

    def test_not_adopted_cannot_generate_work_items(self):
        with self.assertRaises(cc.ChangeControlError) as caught:
            cc.draft_work_items(self.record, owner_map={"default": "codex"},
                                artifacts_root="成果/x")
        self.assertIn("NOT_ADOPTED_CANNOT_DISPATCH", str(caught.exception))

    def test_adoption_requires_revision_and_sha(self):
        with self.assertRaises(cc.ChangeControlError):
            cc.adopt_change(self.record, adopted_by="codex", at=1.0)
        with self.assertRaises(cc.ChangeControlError):
            cc.adopt_change(self.record, adopted_by="", at=1.0,
                            revision_id="r-abc", content_sha256=SHA_A)

    def test_adopted_change_produces_traceable_work_items(self):
        adopted = cc.adopt_change(self.record, adopted_by="codex", at=1001.0,
                                  revision_id="r-abc", content_sha256=SHA_A)
        self.assertTrue(adopted["may_dispatch"])
        items = cc.draft_work_items(adopted, owner_map={"default": "trae"},
                                    artifacts_root="成果/agentbus/pilots/x")
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item["owner"], "trae")
            self.assertEqual(item["adopted_revision"], "r-abc")
            self.assertEqual(item["adopted_revision_sha256"], SHA_A)
            self.assertTrue(item["acceptance"])
            self.assertTrue(item["artifacts_root"])
        self.assertFalse(self.record["adopted"])

    def test_withdrawn_change_must_be_re_registered(self):
        withdrawn = cc.withdraw_change(self.record, by="用户", at=1002.0,
                                       reason="需求撤回，先不做")
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertFalse(withdrawn["may_dispatch"])
        with self.assertRaises(cc.ChangeControlError) as caught:
            cc.adopt_change(withdrawn, adopted_by="codex", at=1003.0,
                            revision_id="r-abc", content_sha256=SHA_A)
        self.assertIn("WITHDRAWN_CHANGE", str(caught.exception))
        with self.assertRaises(cc.ChangeControlError):
            cc.withdraw_change(self.record, by="x", at=1.0, reason="  ")

    def test_withdrawn_change_does_not_reuse_as_adopted(self):
        withdrawn = cc.withdraw_change(self.record, by="用户", at=1002.0, reason="撤回")
        snap = snapshot(changes={withdrawn["change_id"]: withdrawn})
        again = cc.classify_change(REQ_UI, snapshot=snap, now_ts=1003.0)
        self.assertEqual(again["suggested_disposition"], "research")
        self.assertFalse(again["adopted"])

    def test_owner_map_missing_owner_is_rejected(self):
        adopted = cc.adopt_change(self.record, adopted_by="codex", at=1.0,
                                  revision_id="r-abc", content_sha256=SHA_A)
        with self.assertRaises(cc.ChangeControlError):
            cc.draft_work_items(adopted, owner_map={}, artifacts_root="成果/x")


class ConvergenceTests(unittest.TestCase):
    def good_snapshot(self):
        return snapshot(
            taskbook={
                "acceptance": [{"id": "A9", "title": "产品界面与进度"}],
                "work_items": [
                    {"id": "WB-1", "title": "界面状态灯", "status": "in_progress",
                     "change_id": "CH-1", "owner": "workbuddy",
                     "artifacts": ["成果/a.py"], "acceptance": ["A9"]},
                    {"id": "WB-2", "title": "分发验收", "status": "completed",
                     "owner": "workbuddy", "artifacts": ["成果/b.py"], "acceptance": ["A10"]},
                ],
            },
            changes={"CH-1": {"adopted": True, "change_id": "CH-1"}})

    def test_coverage_reports_all_clear_when_evidence_is_valid(self):
        receipts = {"WB-2": {"result_id": "m-1", "reply_to": "WB-2", "exit_code": 0,
                             "manifest": manifest(), "manifest_verified": True}}
        report = pc.convergence_report(self.good_snapshot(), receipts, now_ts=10.0)
        self.assertTrue(report["ok"], report["violations"])
        self.assertEqual(len(report["coverage"]), 2)
        self.assertEqual(report["coverage"][1]["evidence"]["verified"], True)

    def test_completed_without_receipt_is_unverified(self):
        report = pc.convergence_report(self.good_snapshot(), {}, now_ts=10.0)
        codes = [item["code"] for item in report["violations"]]
        self.assertIn("UNVERIFIED_COMPLETION", codes)
        self.assertFalse(report["ok"])

    def test_bad_receipt_shapes_are_enumerated(self):
        cases = {
            "RECEIPT_ROUTE_MISMATCH": {"result_id": "m-1", "reply_to": "other",
                                       "exit_code": 0, "manifest": manifest(),
                                       "manifest_verified": True},
            "RECEIPT_FAILED": {"result_id": "m-1", "reply_to": "WB-2", "exit_code": 1,
                               "manifest": manifest(), "manifest_verified": True},
            "MANIFEST_PATH_UNSAFE": {"result_id": "m-1", "reply_to": "WB-2", "exit_code": 0,
                                     "manifest": manifest(path="../escape.md"),
                                     "manifest_verified": True},
            "MANIFEST_DIGEST_INVALID": {"result_id": "m-1", "reply_to": "WB-2", "exit_code": 0,
                                        "manifest": manifest(digest="zz"), "manifest_verified": True},
            "MANIFEST_NOT_VERIFIED": {"result_id": "m-1", "reply_to": "WB-2", "exit_code": 0,
                                      "manifest": manifest(), "manifest_verified": False},
            "MANIFEST_ENTRY_SHAPE": {"result_id": "m-1", "reply_to": "WB-2", "exit_code": 0,
                                     "manifest": [{"path": "成果/a.md", "bytes": 1}],
                                     "manifest_verified": True},
        }
        for expected, receipt in cases.items():
            with self.subTest(expected=expected):
                report = pc.convergence_report(self.good_snapshot(), {"WB-2": receipt}, now_ts=10.0)
                problems = report["coverage"][1]["evidence"]["problems"]
                self.assertIn(expected, problems)

    def test_missing_owner_artifacts_and_acceptance_are_violations(self):
        snap = self.good_snapshot()
        item = snap["taskbook"]["work_items"][0]
        item.update(owner=None, artifacts=[], acceptance=[])
        report = pc.convergence_report(snap, {}, now_ts=10.0)
        codes = {violation["code"] for violation in report["violations"]}
        self.assertLessEqual({"MISSING_OWNER", "MISSING_ARTIFACTS", "MISSING_ACCEPTANCE"}, codes)

    def test_adopted_requirement_without_work_item_is_flagged(self):
        snap = self.good_snapshot()
        snap["changes"]["CH-2"] = {"adopted": True, "change_id": "CH-2"}
        report = pc.convergence_report(snap, {}, now_ts=10.0)
        codes = [violation["code"] for violation in report["violations"]]
        self.assertIn("UNMAPPED_ADOPTED_REQUIREMENT", codes)

    def test_unfinished_work_item_may_not_disappear(self):
        report = pc.convergence_report(self.good_snapshot(), {}, now_ts=10.0,
                                       previous_plan_item_ids=["WB-1", "WB-9", "WB-2"])
        disappeared = [violation for violation in report["violations"]
                       if violation["code"] == "DISAPPEARED_WORK_ITEM"]
        self.assertEqual([item["item_id"] for item in disappeared], ["WB-9"])

    def test_duplicate_and_malformed_items_are_reported(self):
        snap = self.good_snapshot()
        snap["taskbook"]["work_items"].append(
            {"id": "WB-1", "title": "重复", "status": "planned", "owner": "x",
             "artifacts": ["成果/c.py"], "acceptance": ["A9"]})
        snap["taskbook"]["work_items"].append({"title": "缺少 id", "status": "planned"})
        snap["taskbook"]["work_items"][0]["status"] = "weird"
        report = pc.convergence_report(snap, {}, now_ts=10.0)
        codes = {violation["code"] for violation in report["violations"]}
        self.assertLessEqual({"DUPLICATE_WORK_ITEM", "MALFORMED_WORK_ITEM", "UNKNOWN_STATUS"}, codes)

    def test_convergence_does_not_mutate_snapshot(self):
        snap = self.good_snapshot()
        before = copy.deepcopy(snap)
        pc.convergence_report(snap, {"WB-2": {"result_id": "m", "reply_to": "WB-2",
                                             "exit_code": 0, "manifest": manifest(),
                                             "manifest_verified": True}}, now_ts=1.0)
        self.assertEqual(snap, before)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot(
            taskbook={
                "acceptance": [{"id": "A9", "title": "界面"}],
                "work_items": [
                    {"id": "WB-1", "title": "状态灯", "status": "blocked", "owner": "workbuddy",
                     "artifacts": ["成果/a.py"], "acceptance": ["A9"], "blocker": "缺真实窗口证据"},
                    {"id": "WB-2", "title": "分发", "status": "completed", "owner": "trae",
                     "artifacts": ["成果/b.py"], "acceptance": ["A10"]},
                ],
            },
            changes={"CH-1": {"adopted": True, "change_id": "CH-1"}})

    def test_adr_is_deterministic_and_references_evidence(self):
        change = cc.classify_change(REQ_UI, snapshot=self.snap, now_ts=1000.0)
        first = pc.build_adr(change, decision="adopt", decided_at=1.0, decided_by="codex",
                             references=["TASKBOOK.md", "STATE.md"])
        second = pc.build_adr(change, decision="adopt", decided_at=1.0, decided_by="codex",
                              references=["TASKBOOK.md", "STATE.md"])
        self.assertEqual(first["adr_id"], second["adr_id"])
        self.assertTrue(first["adr_id"].startswith("ADR-"))
        self.assertEqual(first["references"], ["TASKBOOK.md", "STATE.md"])
        self.assertEqual(first["change_id"], change["change_id"])
        with self.assertRaises(pc.ConvergenceError):
            pc.build_adr(change, decision="maybe", decided_at=1.0, decided_by="x")

    def test_adr_supersede_chain_is_recorded(self):
        change = cc.classify_change(REQ_UI, snapshot=self.snap, now_ts=1000.0)
        adr = pc.build_adr(change, decision="supersede", decided_at=2.0, decided_by="codex",
                           supersedes="ADR-old", status="accepted")
        self.assertEqual(adr["supersedes"], "ADR-old")

    def test_state_projection_is_short_and_reference_only(self):
        text = pc.project_state(self.snap)
        self.assertLessEqual(len(text), pc.MAX_STATE_CHARS)
        self.assertIn("WB-1", text)
        self.assertIn("阻塞", text)
        self.assertNotIn("WB-2 [completed]", text.replace("WB-2 [", "WB-2 ["))  # 完成项不进未完成区
        self.assertIn("不复制长历史", text)

    def test_handoff_package_mentions_revision_and_next_action(self):
        text = pc.handoff_package(self.snap, next_action="先补齐真实窗口证据再继续分发验收")
        self.assertLessEqual(len(text), pc.MAX_HANDOFF_CHARS)
        self.assertIn("r-abc", text)
        self.assertIn("先补齐真实窗口证据再继续分发验收", text)
        self.assertIn("证据入口", text)
        with self.assertRaises(pc.ConvergenceError):
            pc.handoff_package(self.snap, next_action="  ")

    def test_projection_truncates_very_long_state(self):
        items = [{"id": f"W-{i}", "title": "很长的标题" * 20, "status": "planned",
                  "owner": "x", "artifacts": ["成果/f.py"], "acceptance": ["A9"]}
                 for i in range(60)]
        snap = snapshot(taskbook={"acceptance": [], "work_items": items}, changes={})
        text = pc.project_state(snap, limit=60)
        self.assertLessEqual(len(text), pc.MAX_STATE_CHARS + 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
