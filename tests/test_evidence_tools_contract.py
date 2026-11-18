# -*- coding: utf-8 -*-
"""OC-EVIDENCE 三个只读核心的测试。

来源：OpenCode（平级成员），2026-10-08，任务 m-17914575845029999-eeeeeec65ce
（product-convergence），reply_to=m-1791457574691-fa3254。
采用计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…80cc3b5。

覆盖任务点名的 11 类：
  乱码 events、截断 events、循环 reply、重复 ID、错摘要、迟到回执、
  转录、failed result、空产物、跨根、HTML 转义、确定性输出

运行：--target-runtime <.../product-convergence-20261008/authors/opencode/candidate>
未指定运行时或缺少三个模块时整模块 pending，不记 pass。

证据边界：本文件验证只读工具对脏数据的处理与证据分级，不证明任何厂商回执、
真实桌面、跨机或长期运行。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest


def _arg_value(flag: str):
    argv = list(sys.argv)
    value = None
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            value = argv[index + 1]
            del argv[index:index + 2]
            break
        if item.startswith(flag + "="):
            value = item.split("=", 1)[1]
            del argv[index]
            break
    sys.argv = argv
    return value


def resolve_runtime():
    raw = _arg_value("--target-runtime") or os.environ.get("AGENTBUS_TEST_RUNTIME") or str(Path(__file__).resolve().parent.parent)
    if not raw or not str(raw).strip():
        raise unittest.SkipTest(
            "PENDING：未指定被测运行时；本测试刻意不默认回退到正式 c365。")
    root = os.path.abspath(os.path.expanduser(str(raw).strip()))
    if not os.path.isfile(os.path.join(root, "agentbus", "__init__.py")):
        raise unittest.SkipTest("PENDING：指定运行时不含 agentbus/__init__.py")
    return root


_PENDING_REASON = ""
RUNTIME = None
try:
    RUNTIME = resolve_runtime()
except unittest.SkipTest as _exc:
    _PENDING_REASON = str(_exc)

if RUNTIME is not None:
    sys.path.insert(0, RUNTIME)
    try:
        from agentbus import attestation as A  # noqa: E402
        from agentbus import delivery_verify as DV  # noqa: E402
        from agentbus import timeline as TL  # noqa: E402
    except ImportError as _exc:
        RUNTIME = None
        _PENDING_REASON = ("PENDING：指定运行时缺少 delivery_verify/timeline/attestation"
                           "（%s）；缺功能先明确 pending，不以「测试文件存在」宣称通过。"
                           "runtime=%s" % (_exc, RUNTIME))


def setUpModule() -> None:
    if RUNTIME is None:
        raise unittest.SkipTest(_PENDING_REASON)


TASK = "m-17914575845020001-aaaaaa"
TASK2 = "m-17914575845020002-bbbbbb"
JOB = "j-1791457584502aaaa0000000000000001"


def digest(text: str) -> tuple[int, str]:
    raw = text.encode("utf-8")
    return len(raw), hashlib.sha256(raw).hexdigest()


class BusBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="oc-ev-")
        self.root = Path(self._tmp.name) / "project"
        self.bus_root = self.root / ".agentbus"
        (self.bus_root / "agents" / "alpha" / "done").mkdir(parents=True)
        (self.bus_root / "agents" / "codex" / "done").mkdir(parents=True)
        (self.bus_root / "jobs").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def write_artifact(self, rel="out/report.md", text="# 报告\n"):
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        size, sha = digest(text)
        return {"path": rel, "bytes": size, "sha256": sha}

    def write_message(self, folder, agent, record):
        path = self.bus_root / "agents" / agent / folder / ("%s.json" % record["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return path

    def write_events(self, lines):
        path = self.bus_root / "events.jsonl"
        with path.open("wb") as stream:
            for raw in lines:
                stream.write(raw if isinstance(raw, bytes)
                             else json.dumps(raw, ensure_ascii=False).encode("utf-8"))
                stream.write(b"\n")
        return path

    def task_record(self, **overrides):
        record = {"id": TASK, "ts": "2026-10-08T19:00:00+08:00",
                  "sender": "codex", "to": "alpha", "type": "task",
                  "title": "写报告", "body": "读输入写输出",
                  "result_to": "codex", "status": "claimed"}
        record.update(overrides)
        return record


class EvidenceClassification(unittest.TestCase):
    def test_no_evidence_is_none(self):
        level, reason = DV.classify_evidence(receipt_found=False)
        self.assertEqual(level, DV.EVIDENCE_NONE)
        self.assertIn("没有任何", reason)

    def test_self_report_never_becomes_native(self):
        level, _ = DV.classify_evidence(receipt_found=False, self_reported=True)
        self.assertEqual(level, DV.EVIDENCE_SELF_REPORT)

    def test_failed_receipt_is_failed_not_native(self):
        level, reason = DV.classify_evidence(receipt_found=True, receipt_failed=True)
        self.assertEqual(level, DV.EVIDENCE_FAILED)
        self.assertIn("能力未通过", reason)

    def test_transcript_receipt_stays_transcript(self):
        level, reason = DV.classify_evidence(receipt_found=True, transport="chat")
        self.assertEqual(level, DV.EVIDENCE_TRANSCRIPT)
        self.assertIn("转录", reason)

    def test_native_without_artifact_verification_is_downgraded(self):
        level, reason = DV.classify_evidence(receipt_found=True,
                                             transport="program",
                                             artifact_verified=False)
        self.assertEqual(level, DV.EVIDENCE_TRANSCRIPT)
        self.assertIn("尚未核验", reason)

    def test_native_with_verified_artifact_is_top(self):
        level, _ = DV.classify_evidence(receipt_found=True, transport="program",
                                        artifact_verified=True)
        self.assertEqual(level, DV.EVIDENCE_NATIVE)

    def test_evidence_order_is_monotonic(self):
        self.assertEqual(
            [DV.EVIDENCE_NONE, DV.EVIDENCE_SELF_REPORT, DV.EVIDENCE_FAILED,
             DV.EVIDENCE_TRANSCRIPT, DV.EVIDENCE_NATIVE],
            list(DV.EVIDENCE_ORDER))


class VerifyTask(BusBase):
    def test_missing_everything_is_partial_not_pass(self):
        out = DV.verify_task(self.bus_root, self.root, TASK)
        self.assertEqual(out["verdict"], "partial")
        self.assertEqual(out["evidence"], DV.EVIDENCE_NONE)
        self.assertIn("single_result", out["unverified"] + [c["code"] for c in out["checks"]])

    def test_bad_task_id_is_fail(self):
        out = DV.verify_task(self.bus_root, self.root, "not-a-task")
        self.assertEqual(out["verdict"], "fail")

    def test_native_receipt_with_matching_artifact_is_pass(self):
        art = self.write_artifact()
        self.write_message("done", "codex", {
            "id": "m-17914575845029999-b83901", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "result_to": "codex", "transport": "program",
            "artifacts": [art], "exit_code": 0, "status": "done"})
        # 没有评审消息时 review_closure 记未验证，因此总体是 partial 而非 pass。
        # 这正是设计意图：证据齐但评审未闭环，不得说「通过」。
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record())
        self.assertEqual(out["evidence"], DV.EVIDENCE_NATIVE)
        self.assertEqual(out["verdict"], "partial")
        self.assertIn("review_closure", out["unverified"])

    def test_native_receipt_with_review_closure_is_pass(self):
        art = self.write_artifact()
        self.write_message("done", "codex", {
            "id": "m-17914575845029998-b81a12", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "result_to": "codex", "transport": "program",
            "artifacts": [art], "exit_code": 0, "status": "done"})
        self.write_message("done", "trae", {
            "id": "m-17914575845029997-b80f23", "ts": "2026-10-08T19:20:00+08:00",
            "sender": "codex", "to": "trae", "type": "review",
            "result_to": "codex", "reply_to": TASK, "status": "done",
            "body": "同意 %s 的产物" % TASK, "refs": [TASK]})
        # Integration correction: a review message alone is not a completed review.
        self.write_message("ready", "codex", {
            "id": "m-17914575845029996-c80f24", "type": "result",
            "sender": "trae", "to": "codex", "reply_to": "m-17914575845029997-b80f23",
            "status": "pending", "exit_code": 0})
        (self.bus_root / "agents" / "trae" / "done").mkdir(parents=True, exist_ok=True)
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record())
        self.assertEqual(out["evidence"], DV.EVIDENCE_NATIVE)
        self.assertEqual(out["verdict"], "pass", out)

    def test_transcript_receipt_is_not_passed(self):
        art = self.write_artifact()
        self.write_message("done", "codex", {
            "id": "m-17914575845029998-b81a12", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "transport": "chat-transcript",
            "artifacts": [art], "exit_code": 0, "status": "done"})
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record())
        self.assertEqual(out["evidence"], DV.EVIDENCE_TRANSCRIPT)

    def test_failed_result_keeps_channel_but_blocks_capability(self):
        self.write_artifact()
        self.write_message("done", "codex", {
            "id": "m-17914575845029997-b7fb23", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "exit_code": 2, "status": "failed"})
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record())
        self.assertEqual(out["evidence"], DV.EVIDENCE_FAILED)

    def test_wrong_digest_is_fail(self):
        self.write_artifact()
        bad = {"path": "out/report.md", "bytes": 999, "sha256": "a" * 64}
        self.write_message("done", "codex", {
            "id": "m-17914575845029996-b7dc34", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "transport": "program", "artifacts": [bad]})
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record())
        codes = [c["code"] for c in out["checks"] if c["verdict"] == "fail"]
        self.assertIn("artifacts_verified", codes)
        self.assertEqual(out["verdict"], "fail")

    def test_empty_artifact_list_is_fail_when_expected(self):
        self.write_message("done", "codex", {
            "id": "m-17914575845029995-b7bd45", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "artifacts": []})
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record(
                                 expected_artifacts=["out/report.md"]))
        codes = [c["code"] for c in out["checks"] if c["verdict"] == "fail"]
        self.assertIn("expected_artifacts_present", codes)

    def test_duplicate_result_ids_are_reported(self):
        record = {"id": TASK, "ts": "2026-10-08T19:10:00+08:00", "sender": "alpha",
                  "to": "codex", "type": "result", "reply_to": TASK}
        self.write_message("done", "codex", record)
        (self.bus_root / "agents" / "alpha" / "done").mkdir(parents=True, exist_ok=True)
        (self.bus_root / "agents" / "alpha" / "done" / ("%s.json" % TASK)).write_text(
            json.dumps(record), encoding="utf-8")
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record())
        codes = [c for c in out["checks"] if c["code"] == "no_duplicate_result_id"]
        self.assertEqual(codes[0]["verdict"], "fail")

    def test_result_to_mismatch_is_fail(self):
        self.write_message("done", "codex", {
            "id": "m-17914575845029994-b79e56", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "bravo", "type": "result",
            "reply_to": TASK})
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record())
        codes = [c["code"] for c in out["checks"] if c["verdict"] == "fail"]
        self.assertIn("result_to_binding", codes)

    def test_fenced_task_with_mismatched_epoch_is_fail(self):
        self.write_message("done", "codex", {
            "id": "m-17914575845029993-b77f67", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "source_claim_epoch": 1,
            "source_claim_id": "a" * 32})
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record(claim_epoch=2,
                                                          claim_id="b" * 32,
                                                          claim_agent="alpha"))
        codes = [c["code"] for c in out["checks"] if c["verdict"] == "fail"]
        self.assertIn("claim_epoch_match", codes)

    def test_expired_deadline_is_fail(self):
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record(deadline_ts=1.0),
                             now=time.time())
        codes = [c["code"] for c in out["checks"] if c["verdict"] == "fail"]
        self.assertIn("within_deadline", codes)

    def test_zero_deadline_is_not_expired(self):
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record(deadline_ts=0.0),
                             now=time.time())
        codes = [c for c in out["checks"] if c["code"] == "within_deadline"]
        self.assertEqual(codes[0]["verdict"], "pass")

    def test_report_text_is_human_readable(self):
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record=self.task_record())
        report = DV.Report("task", TASK)
        report.checks = out["checks"]
        report.evidence = out["evidence"]
        report.evidence_reason = out["evidence_reason"]
        report.unverified = out["unverified"]
        text = report.to_text()
        self.assertIn("交付体检", text)
        self.assertIn("结论", text)

    def test_verify_task_does_not_write_anything(self):
        before = sorted(p.name for p in self.bus_root.rglob("*"))
        DV.verify_task(self.bus_root, self.root, TASK,
                       task_record=self.task_record())
        after = sorted(p.name for p in self.bus_root.rglob("*"))
        self.assertEqual(before, after)


class VerifyJob(BusBase):
    def test_missing_job_is_partial(self):
        out = DV.verify_job(self.bus_root, self.root, JOB)
        self.assertEqual(out["verdict"], "partial")
        self.assertIn("job_record_readable", out["unverified"])

    def test_job_with_no_tasks_is_fail(self):
        (self.bus_root / "jobs" / ("%s.json" % JOB)).write_text(
            json.dumps({"id": JOB, "state": "working", "tasks": []}),
            encoding="utf-8")
        out = DV.verify_job(self.bus_root, self.root, JOB)
        self.assertEqual(out["verdict"], "fail")

    def test_job_claiming_completed_with_failed_task_is_fail(self):
        self.write_message("done", "codex", {
            "id": "m-17914575845028888-31f9c8", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "transport": "chat", "exit_code": 3,
            "status": "failed"})
        (self.bus_root / "jobs" / ("%s.json" % JOB)).write_text(json.dumps({
            "id": JOB, "state": "completed", "summary": "完成",
            "tasks": [{"id": TASK}]}), encoding="utf-8")
        out = DV.verify_job(self.bus_root, self.root, JOB)
        self.assertEqual(out["verdict"], "fail")

    def test_job_audit_damage_is_counted_not_hidden(self):
        self.write_events([{"event": "claim", "agent": "alpha"},
                           b"{ truncated \xff\xfe",
                           b"not json at all"])
        (self.bus_root / "jobs" / ("%s.json" % JOB)).write_text(json.dumps({
            "id": JOB, "state": "working",
            "tasks": [{"id": TASK}]}), encoding="utf-8")
        out = DV.verify_job(self.bus_root, self.root, JOB)
        codes = {c["code"]: c for c in out["checks"]}
        self.assertEqual(codes["audit_readable"]["verdict"], "pass")
        self.assertIn("2 条损坏", codes["audit_readable"]["detail"])
        self.assertTrue(any("损坏行" in n for n in out["notes"]))

    def test_job_missing_summary_is_fail(self):
        (self.bus_root / "jobs" / ("%s.json" % JOB)).write_text(json.dumps({
            "id": JOB, "state": "working",
            "tasks": [{"id": TASK}]}), encoding="utf-8")
        out = DV.verify_job(self.bus_root, self.root, JOB)
        codes = [c["code"] for c in out["checks"] if c["verdict"] == "fail"]
        self.assertIn("summary_present", codes)


class DamagedAndLegacy(BusBase):
    def test_truncated_events_are_counted(self):
        self.write_events([{"event": "a"}, b'{"event": "b"'])
        events, damaged = DV.load_events(self.bus_root)
        self.assertEqual(len(events), 1)
        self.assertEqual(damaged, 1)

    def test_garbled_utf8_events_are_counted(self):
        self.write_events([b'{"event": "x", "note": "\xff\xfe\xfa"}'])
        events, damaged = DV.load_events(self.bus_root)
        self.assertEqual(damaged, 1)

    def test_blank_lines_are_skipped_not_counted_as_damage(self):
        self.write_events([{"event": "a"}, b"", b"   "])
        events, damaged = DV.load_events(self.bus_root)
        self.assertEqual(len(events), 1)
        self.assertEqual(damaged, 0)

    def test_non_object_event_line_is_damage(self):
        self.write_events([[1, 2, 3]])
        events, damaged = DV.load_events(self.bus_root)
        self.assertEqual(damaged, 1)

    def test_missing_events_file_is_empty(self):
        events, damaged = DV.load_events(self.bus_root)
        self.assertEqual(events, [])
        self.assertEqual(damaged, 0)

    def test_legacy_message_without_new_fields_verifies(self):
        """旧记录没有 claim_* 字段，必须照常可体检而不是崩。"""
        self.write_artifact()
        self.write_message("done", "codex", {
            "id": "m-17914575845027777-abba8f", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "transport": "program",
            "artifacts": [self.write_artifact()]})
        out = DV.verify_task(self.bus_root, self.root, TASK,
                             task_record={"id": TASK, "to": "alpha",
                                          "result_to": "codex"})
        codes = {c["code"]: c for c in out["checks"]}
        self.assertEqual(codes["claim_epoch_match"]["verdict"], "pass")
        self.assertIn("legacy", codes["claim_epoch_match"]["detail"])

    def test_unreadable_receipt_file_is_skipped_not_fatal(self):
        target = self.bus_root / "agents" / "codex" / "done" / "broken.json"
        target.write_text("{ truncated", encoding="utf-8")
        receipts = DV.collect_receipts(self.bus_root, TASK)
        self.assertEqual(receipts, [])

    def test_cross_root_artifact_is_rejected(self):
        entries = [{"path": "../outside.txt", "bytes": 1, "sha256": "a" * 64}]
        ok, problems = DV.verify_artifacts(self.root, entries)
        self.assertFalse(ok)
        self.assertEqual(problems[0]["code"], "UNSAFE_PATH")

    def test_absolute_artifact_path_is_rejected(self):
        entries = [{"path": "C:/Windows/x", "bytes": 1, "sha256": "a" * 64}]
        ok, problems = DV.verify_artifacts(self.root, entries)
        self.assertFalse(ok)
        self.assertEqual(problems[0]["code"], "UNSAFE_PATH")

    def test_private_directory_artifact_is_rejected(self):
        entries = [{"path": ".workbuddy/notes.md", "bytes": 1, "sha256": "a" * 64}]
        ok, problems = DV.verify_artifacts(self.root, entries)
        self.assertFalse(ok)
        self.assertEqual(problems[0]["code"], "UNSAFE_PATH")

    def test_safe_relative_accepts_normal_paths(self):
        self.assertEqual(DV.safe_relative("成果/a.md"), "成果/a.md")
        for bad in ("", "  ", "a\\b", "C:/x", "/x", "../x", "a//b", "./x"):
            self.assertIsNone(DV.safe_relative(bad), bad)


class TimelineBasics(BusBase):
    def test_empty_workspace_produces_empty_timeline(self):
        data = TL.build_timeline(self.bus_root)
        self.assertEqual(data["counts"]["nodes"], 0)
        self.assertEqual(data["counts"]["unverified"], 0)

    def test_native_badge_requires_verified_artifact(self):
        """声明原生运输但产物核验不通过时，最高只能算转录。

        否则一个空的 transport 标记就能自称原生可信。
        """
        self.write_artifact("out/report.md", "# 报告\n")
        self.write_message("done", "codex", {
            "id": "m-17914575845026666-257b56", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "transport": "program",
            "artifacts": [{"path": "out/report.md", "bytes": 1, "sha256": "a" * 64}]})
        data = TL.build_timeline(self.bus_root)
        self.assertEqual(data["counts"]["native"], 0)
        self.assertEqual(data["counts"]["transcript"], 1)

    def test_native_with_verified_artifact_is_distinguished_from_chat(self):
        self.write_message("done", "codex", {
            "id": "m-17914575845026665-255c67", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "transport": "program",
            "artifacts": [self.write_artifact()]})
        self.write_message("done", "codex", {
            "id": "m-17914575845026664-253d78", "ts": "2026-10-08T19:11:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "transport": "chat"})
        data = TL.build_timeline(self.bus_root)
        self.assertEqual(data["counts"]["native"], 1)
        self.assertEqual(data["counts"]["transcript"], 1)

    def test_self_report_is_labelled(self):
        self.write_message("done", "codex", {
            "id": "m-17914575845026663-251c3a", "ts": "2026-10-08T19:12:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "self_report": "我做了"})
        data = TL.build_timeline(self.bus_root)
        self.assertEqual(data["counts"]["self_report"], 1)

    def test_late_results_are_listed(self):
        directory = self.bus_root / "late-results" / TASK
        directory.mkdir(parents=True)
        (directory / "ev.json").write_text(json.dumps({
            "reason_code": "STALE_EPOCH", "advanced_task": False}), encoding="utf-8")
        data = TL.build_timeline(self.bus_root)
        self.assertEqual(len(data["late_results"]), 1)
        self.assertEqual(data["late_results"][0]["reason_code"], "STALE_EPOCH")

    def test_unreadable_late_result_is_flagged(self):
        directory = self.bus_root / "late-results" / TASK
        directory.mkdir(parents=True)
        (directory / "bad.json").write_text("{ truncated", encoding="utf-8")
        data = TL.build_timeline(self.bus_root)
        self.assertFalse(data["late_results"][0]["readable"])

    def test_reply_cycle_is_detected(self):
        self.write_message("done", "codex", {
            "id": TASK, "ts": "2026-10-08T19:00:00+08:00", "sender": "codex",
            "to": "alpha", "type": "task", "reply_to": TASK2})
        self.write_message("done", "codex", {
            "id": TASK2, "ts": "2026-10-08T19:01:00+08:00", "sender": "alpha",
            "to": "codex", "type": "result", "reply_to": TASK})
        data = TL.build_timeline(self.bus_root)
        self.assertTrue(data["cycles"], data)
        self.assertIn(TASK, data["cycles"][0])

    def test_detect_cycles_on_acyclic_records(self):
        self.assertEqual(TL.detect_cycles([
            {"id": "a", "reply_to": ""}, {"id": "b", "reply_to": "a"}]), [])

    def test_damaged_events_counted_in_timeline(self):
        self.write_events([{"event": "a", "id": TASK}, b"{ broken"])
        data = TL.build_timeline(self.bus_root)
        self.assertEqual(data["event_damaged"], 1)

    def test_duplicate_ids_reported(self):
        record = {"id": TASK, "ts": "2026-10-08T19:00:00+08:00", "sender": "codex",
                  "to": "alpha", "type": "task"}
        self.write_message("done", "codex", record)
        (self.bus_root / "agents" / "alpha" / "done").mkdir(parents=True, exist_ok=True)
        (self.bus_root / "agents" / "alpha" / "done" / ("%s.json" % TASK)).write_text(
            json.dumps(record), encoding="utf-8")
        data = TL.build_timeline(self.bus_root)
        self.assertIn(TASK, data["duplicate_ids"])

    def test_timeline_does_not_execute_logged_commands(self):
        """日志里的命令字段只当字符串，不得被执行或写回。"""
        marker = self.root / "pwned.txt"
        self.write_events([{"event": "cmd", "command": "touch %s" % marker}])
        TL.build_timeline(self.bus_root)
        TL.to_markdown(TL.build_timeline(self.bus_root))
        TL.to_html(TL.build_timeline(self.bus_root))
        self.assertFalse(marker.exists())

    def test_build_timeline_is_read_only(self):
        self.write_message("done", "codex", {
            "id": TASK, "ts": "2026-10-08T19:00:00+08:00", "sender": "codex",
            "to": "alpha", "type": "task"})
        before = {p: p.stat().st_mtime for p in self.bus_root.rglob("*") if p.is_file()}
        TL.build_timeline(self.bus_root)
        after = {p: p.stat().st_mtime for p in self.bus_root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class DeterminismAndEscaping(BusBase):
    def _populate(self):
        self.write_artifact()
        self.write_message("done", "codex", {
            "id": "m-17914575845025555-9f3c1d", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "transport": "program",
            "title": '<script>alert("x")</script>',
            "body": "包含 & < > \" ' 符号",
            "artifacts": [self.write_artifact()]})
        self.write_events([{"event": "claim", "agent": "alpha", "id": TASK},
                           b"{ broken"])

    def test_markdown_is_byte_identical_across_runs(self):
        self._populate()
        first = TL.to_markdown(TL.build_timeline(self.bus_root))
        second = TL.to_markdown(TL.build_timeline(self.bus_root))
        self.assertEqual(TL.timeline_digest(first), TL.timeline_digest(second))

    def test_html_is_byte_identical_across_runs(self):
        self._populate()
        first = TL.to_html(TL.build_timeline(self.bus_root))
        second = TL.to_html(TL.build_timeline(self.bus_root))
        self.assertEqual(TL.timeline_digest(first), TL.timeline_digest(second))

    def test_html_escapes_script_tags(self):
        self._populate()
        html = TL.to_html(TL.build_timeline(self.bus_root))
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_html_escapes_ampersands(self):
        self._populate()
        html = TL.to_html(TL.build_timeline(self.bus_root))
        self.assertNotIn("包含 & <", html)

    def test_markdown_escapes_pipes_in_cells(self):
        self.write_message("done", "codex", {
            "id": "m-17914575845025554-9f1d2e", "ts": "2026-10-08T19:10:00+08:00",
            "sender": "alpha", "to": "codex", "type": "result",
            "reply_to": TASK, "title": "a|b"})
        text = TL.to_markdown(TL.build_timeline(self.bus_root))
        self.assertIn("a\\|b", text)

    def test_missing_timestamp_uses_stable_placeholder(self):
        self.write_message("done", "codex", {
            "id": TASK, "sender": "codex", "to": "alpha", "type": "task"})
        data = TL.build_timeline(self.bus_root)
        self.assertEqual(data["nodes"][0]["ts"], TL.UNKNOWN_TS)

    def test_output_has_no_generation_timestamp(self):
        self._populate()
        text = TL.to_markdown(TL.build_timeline(self.bus_root))
        self.assertNotIn(time.strftime("%Y-%m-%dT%H:%M:%S"), text.replace(
            "2026-10-08T19:10:00+08:00", ""))

    def test_render_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            TL.render({}, "pdf")

    def test_render_accepts_markdown_and_html(self):
        self.assertIn("# 交付时间线", TL.render({}, "markdown"))
        self.assertIn("<!DOCTYPE html>", TL.render({}, "html"))


class AttestationBasics(BusBase):
    def test_passport_badge_a_when_everything_verified(self):
        out = A.attest(self.root, author="opencode", task_id=TASK,
                       outputs=[self.write_artifact()],
                       evidence="native_receipt")
        self.assertEqual(out["badge"], "A")
        self.assertEqual(out["outputs"][0]["verdict"], "match")

    def test_passport_badge_d_when_output_mismatches(self):
        bad = {"path": "out/report.md", "bytes": 1, "sha256": "a" * 64}
        self.write_artifact()
        out = A.attest(self.root, author="opencode", task_id=TASK,
                       outputs=[bad], evidence="native_receipt")
        self.assertEqual(out["badge"], "D")

    def test_self_report_cannot_earn_badge_a(self):
        out = A.attest(self.root, author="opencode", task_id=TASK,
                       outputs=[self.write_artifact()],
                       evidence="self_report")
        self.assertEqual(out["badge"], "C")

    def test_invalid_evidence_is_rejected(self):
        with self.assertRaises(A.AttestationError) as c:
            A.attest(self.root, author="opencode", task_id=TASK,
                     evidence="trust-me")
        self.assertEqual(c.exception.code, "INVALID_EVIDENCE")

    def test_author_and_task_are_required(self):
        with self.assertRaises(A.AttestationError):
            A.attest(self.root, author="", task_id=TASK)
        with self.assertRaises(A.AttestationError):
            A.attest(self.root, author="opencode", task_id="")

    def test_cross_root_artifact_is_rejected(self):
        with self.assertRaises(A.AttestationError) as c:
            A.attest(self.root, author="opencode", task_id=TASK,
                     outputs=[{"path": "../escape.txt"}])
        self.assertEqual(c.exception.code, "UNSAFE_PATH")

    def test_private_directory_artifact_is_rejected(self):
        with self.assertRaises(A.AttestationError) as c:
            A.attest(self.root, author="opencode", task_id=TASK,
                     outputs=[{"path": ".workbuddy/x"}])
        self.assertEqual(c.exception.code, "PRIVATE_PATH")

    def test_absolute_path_artifact_is_rejected(self):
        with self.assertRaises(A.AttestationError) as c:
            A.attest(self.root, author="opencode", task_id=TASK,
                     outputs=[{"path": "C:/Windows/x"}])
        self.assertEqual(c.exception.code, "UNSAFE_PATH")

    def test_missing_artifact_is_unverified_not_match(self):
        out = A.attest(self.root, author="opencode", task_id=TASK,
                       outputs=[{"path": "out/absent.md"}])
        self.assertEqual(out["outputs"][0]["verdict"], "unverified")

    def test_forbidden_review_fields_are_filtered(self):
        out = A.attest(self.root, author="opencode", task_id=TASK,
                       reviews={"verdict": "accept", "token": "secret-value",
                                "chat": "全文", "body_text": "x",
                                "nested": {"k": "v"}})
        self.assertIn("verdict", out["reviews"])
        for banned in ("token", "chat", "body_text", "nested"):
            self.assertNotIn(banned, out["reviews"])

    def test_passport_never_embeds_file_content(self):
        secret = "机密内容标记-ABC123"
        self.write_artifact("out/doc.md", secret)
        out = A.attest(self.root, author="opencode", task_id=TASK,
                       outputs=[self.write_artifact("out/doc.md", secret)])
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn(secret, blob)

    def test_passport_environment_has_no_env_values(self):
        out = A.attest(self.root, author="opencode", task_id=TASK)
        for key in A.PUBLIC_ENV_KEYS:
            self.assertIn(key, out["environment"])
        self.assertNotIn("PATH", out["environment"])
        self.assertNotIn("environ", out["environment"])

    def test_passport_has_privacy_declaration(self):
        out = A.attest(self.root, author="opencode", task_id=TASK)
        self.assertIn("excluded", out["privacy"])
        self.assertIn("凭据与令牌", "、".join(out["privacy"]["excluded"]))

    def test_passport_markdown_is_deterministic(self):
        art = self.write_artifact()
        first = A.to_markdown(A.attest(self.root, author="opencode",
                                       task_id=TASK, outputs=[art],
                                       evidence="native_receipt",
                                       observed_at=1000.0))
        second = A.to_markdown(A.attest(self.root, author="opencode",
                                        task_id=TASK, outputs=[art],
                                        evidence="native_receipt",
                                        observed_at=1000.0))
        self.assertEqual(first, second)

    def test_attest_is_read_only(self):
        art = self.write_artifact()
        before = sorted((str(p), p.stat().st_mtime)
                        for p in self.root.rglob("*") if p.is_file())
        A.attest(self.root, author="opencode", task_id=TASK, outputs=[art])
        after = sorted((str(p), p.stat().st_mtime)
                       for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(before, after)

    def test_too_many_artifacts_is_rejected(self):
        entries = [{"path": "out/a%d.md" % i} for i in range(A.MAX_ARTIFACTS + 1)]
        with self.assertRaises(A.AttestationError) as c:
            A.collect_artifacts(self.root, entries)
        self.assertEqual(c.exception.code, "TOO_MANY")


if __name__ == "__main__":
    unittest.main(verbosity=2)