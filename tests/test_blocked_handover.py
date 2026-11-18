"""§6.10 一屏接手说明（`recovery_center.blocked_handover`）的契约测试。

验收口径（任务书原文）：
> 一屏列出阻塞原因、最后有效心跳、现有成果、原认领成员/会话、可验证的停止状态与
>   下一句指令；**未确认原会话停止时不释放其票据、不冒称已经接手**。

本文件重点守两条最容易做错的纪律：
  1. **心跳过期 ≠ 原会话已停止**——它只说明没人报平安；没有正面停止证据时，
     `takeover_allowed` 必须为 False，且不得出现"已经接手"的措辞。
  2. 只读：不释放票据、不代任何成员执行、不泄漏机器绝对路径。
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from agentbus import recovery_center as rc

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "agentbus" / "recovery_center.py"

_REQUIRED_KEYS = (
    "leaf", "blocked_reason", "last_valid_heartbeat", "existing_artifacts",
    "original_claimer", "stop_state", "ticket", "takeover_allowed", "next_instruction",
)


def _leaf(**over):
    base = {
        "id": "leaf-1",
        "section_no": "6.10",
        "title": "用一屏说明卡住的任务该如何接手",
        "goal": "列出阻塞原因与下一句指令",
        "state": "blocked",
        "owner": "",
        "conversation_id": "",
        "task_id": "",
        "results": "任务受阻；保留原有产物与历史。",
        "artifacts": [],
        "heartbeat": None,
        "unmet_dependencies": [],
        "handoff_history": [],
    }
    base.update(over)
    return base


def _hb(source="record", age_s=12.0, stale=False, reason="存在通过票据校验的显式心跳记录"):
    return {"source": source, "age_s": age_s, "stale": stale,
            "conversation_id": "cv-" + "a" * 24, "claim_fingerprint": "fp-abc",
            "reason": reason}


class ShapeTests(unittest.TestCase):
    def test_reports_all_six_required_items(self):
        result = rc.blocked_handover({"leaves": [_leaf()]}, section_no="6.10")
        for key in _REQUIRED_KEYS:
            self.assertIn(key, result, "缺少任务书要求的栏目：%s" % key)

    def test_missing_leaf_is_reported_not_crashed(self):
        result = rc.blocked_handover({"leaves": []}, leaf_id="nope")
        self.assertFalse(result["found"])
        self.assertTrue(result["read_only"])
        self.assertIn("next_instruction", result)

    def test_lookup_by_leaf_id_as_well_as_section(self):
        forest = {"leaves": [_leaf(id="leaf-xyz", section_no="9.9")]}
        self.assertTrue(rc.blocked_handover(forest, leaf_id="leaf-xyz")["found"])
        self.assertTrue(rc.blocked_handover(forest, section_no="9.9")["found"])

    def test_non_blocking_leaf_needs_no_takeover(self):
        result = rc.blocked_handover({"leaves": [_leaf(state="queued")]}, section_no="6.10")
        self.assertFalse(result["leaf"]["is_blocked"])
        self.assertFalse(result["takeover_allowed"])
        self.assertIn("无需接手", result["next_instruction"])


class StopEvidenceTests(unittest.TestCase):
    def test_stale_heartbeat_alone_is_not_stop_evidence(self):
        """核心纪律：心跳过期**不**等于原会话已停止。"""
        leaf = _leaf(state="claimed", owner="trae", conversation_id="cv-" + "b" * 24,
                     heartbeat=_hb(source="mtime_only", age_s=None, stale=True,
                                   reason="没有可解析的显式心跳记录"))
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10")
        self.assertTrue(result["leaf"]["is_blocked"], "心跳未确认的认领属于卡住")
        self.assertEqual("unconfirmed", result["stop_state"]["value"])
        self.assertEqual([], result["stop_state"]["evidence"])
        self.assertFalse(result["takeover_allowed"], "未确认停止时不得允许接手")
        self.assertFalse(result["ticket"]["released"], "未确认停止时不得释放票据")
        self.assertIn("未确认", result["next_instruction"])
        self.assertIn("未确认前不要重新认领", result["next_instruction"])

    def test_live_heartbeat_forbids_takeover(self):
        # 已判为卡住（blocked）但原会话仍在报心跳：绝不能重新认领。
        leaf = _leaf(state="blocked", owner="codex", conversation_id="cv-" + "c" * 24,
                     heartbeat=_hb(age_s=3.0, stale=False))
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10")
        self.assertTrue(result["leaf"]["is_blocked"])
        self.assertFalse(result["takeover_allowed"])
        self.assertIn("不要重新认领", result["next_instruction"])
        self.assertTrue(result["last_valid_heartbeat"]["valid"])

    def test_confirmed_inactive_event_unlocks_takeover(self):
        leaf = _leaf(state="blocked", owner="trae", conversation_id="cv-" + "d" * 24,
                     task_id="m-1", heartbeat=_hb(source="mtime_only", age_s=None, stale=True))
        events = [{"event": "relinquished_claim", "id": "m-1",
                   "previous_conversation_id": "cv-" + "d" * 24,
                   "owner_confirmed_inactive": True}]
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10", events=events)
        self.assertEqual("confirmed_stopped", result["stop_state"]["value"])
        self.assertTrue(result["takeover_allowed"])
        self.assertIn("claim --leaf 6.10", result["next_instruction"])

    def test_event_without_confirmation_does_not_unlock(self):
        """有事件但没写『已确认停止』——依旧不得接手。"""
        leaf = _leaf(state="blocked", owner="trae", conversation_id="cv-" + "e" * 24,
                     task_id="m-2", heartbeat=_hb(source="mtime_only", stale=True))
        events = [{"event": "recovered", "id": "m-2",
                   "previous_conversation_id": "cv-" + "e" * 24,
                   "owner_confirmed_inactive": False}]
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10", events=events)
        self.assertEqual("unconfirmed", result["stop_state"]["value"])
        self.assertFalse(result["takeover_allowed"])

    def test_event_for_another_task_is_ignored(self):
        leaf = _leaf(state="blocked", owner="trae", conversation_id="cv-" + "f" * 24,
                     task_id="m-3", heartbeat=_hb(source="mtime_only", stale=True))
        events = [{"event": "relinquished_claim", "id": "m-OTHER",
                   "owner_confirmed_inactive": True}]
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10", events=events)
        self.assertEqual("unconfirmed", result["stop_state"]["value"])

    def test_never_claimed_leaf_needs_no_stop_confirmation(self):
        result = rc.blocked_handover({"leaves": [_leaf()]}, section_no="6.10")
        self.assertEqual("confirmed_stopped", result["stop_state"]["value"])
        self.assertIn("never_claimed", [e["kind"] for e in result["stop_state"]["evidence"]])


class BlockerTests(unittest.TestCase):
    def test_unmet_dependency_names_the_blockers(self):
        leaf = _leaf(state="blocked", unmet_dependencies=["UX-SEARCH-FILTER", "UX-CANVAS"])
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10")
        self.assertEqual("unmet_dependency", result["blocked_reason"]["kind"])
        self.assertEqual(["UX-SEARCH-FILTER", "UX-CANVAS"],
                         result["blocked_reason"]["unmet_dependencies"])
        self.assertIn("UX-SEARCH-FILTER", result["next_instruction"])
        self.assertIn("先处理依赖", result["next_instruction"])

    def test_workspace_stop_flag_wins(self):
        leaf = _leaf(state="queued")
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10", stop_flag=True)
        self.assertEqual("workspace_stop_flag", result["blocked_reason"]["kind"])
        self.assertTrue(result["leaf"]["is_blocked"])
        self.assertFalse(result["takeover_allowed"],
                         "全局停止下不得接手任何叶子")
        self.assertIn("STOP", result["next_instruction"])

    def test_all_blocking_states_recognised(self):
        for state in rc.BLOCKING_STATES:
            result = rc.blocked_handover({"leaves": [_leaf(state=state)]}, section_no="6.10")
            self.assertTrue(result["leaf"]["is_blocked"], "%s 应被识别为卡住" % state)
            self.assertIn(state, result["blocked_reason"]["kind"])

    def test_results_text_is_used_as_reason(self):
        leaf = _leaf(state="rejected", results="独立审核未批准：缺少回归证据。")
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10")
        self.assertIn("缺少回归证据", result["blocked_reason"]["detail"])


class ArtifactTests(unittest.TestCase):
    def test_absolute_paths_are_not_leaked(self):
        leaf = _leaf(artifacts=[
            {"path": "D:/示例空间/协作 样例/成果/复函/x.md", "bytes": 10,
             "sha256": "a" * 64},
            {"path": "C:\\Users\\example\\secret\\y.md", "bytes": 20, "sha256": "b" * 64},
        ])
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10")
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("VibeCoding项目", blob, "不得泄漏机器绝对路径")
        self.assertNotIn("C:\\", blob)
        self.assertNotIn("tjs", blob)
        for row in result["existing_artifacts"]["rows"]:
            self.assertTrue(row["path"].startswith(".../") or row["path"] == "")
        self.assertEqual(2, result["existing_artifacts"]["count"])
        self.assertEqual(2, result["existing_artifacts"]["complete_hashes"])

    def test_relative_paths_kept_as_is(self):
        leaf = _leaf(artifacts=[{"path": "成果/复函/a.md", "bytes": 1, "sha256": "c" * 64}])
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10")
        self.assertEqual("成果/复函/a.md", result["existing_artifacts"]["rows"][0]["path"])

    def test_artifact_rows_are_capped(self):
        leaf = _leaf(artifacts=[{"path": "a/%d.md" % i, "bytes": i, "sha256": ""}
                                for i in range(50)])
        result = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10")
        self.assertEqual(50, result["existing_artifacts"]["count"])
        self.assertEqual(20, len(result["existing_artifacts"]["rows"]))


class PurityTests(unittest.TestCase):
    def test_is_deterministic(self):
        forest = {"leaves": [_leaf(unmet_dependencies=["a"])]}
        first = rc.blocked_handover(forest, section_no="6.10")
        second = rc.blocked_handover(forest, section_no="6.10")
        self.assertEqual(json.dumps(first, sort_keys=True, ensure_ascii=False),
                         json.dumps(second, sort_keys=True, ensure_ascii=False))

    def test_never_releases_a_ticket_in_any_scenario(self):
        cases = [
            ({"leaves": [_leaf()]}, {}),
            ({"leaves": [_leaf(state="claimed",
                               conversation_id="cv-" + "1" * 24,
                               heartbeat=_hb(source="mtime_only", stale=True))]}, {}),
            ({"leaves": [_leaf(state="running",
                               conversation_id="cv-" + "2" * 24,
                               heartbeat=_hb(stale=False))]}, {}),
            ({"leaves": [_leaf(state="blocked")]}, {"stop_flag": True}),
        ]
        for forest, kwargs in cases:
            result = rc.blocked_handover(forest, section_no="6.10", **kwargs)
            self.assertFalse(result["ticket"]["released"],
                             "只读视图在任何场景下都不得释放票据")
            self.assertTrue(result["read_only"])

    def test_module_does_not_write_files(self):
        """`recovery_center` 是纯模型：任何写文件调用都不允许出现在本模块。"""
        import ast
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        banned = {"write_text", "write_bytes", "mkdir", "unlink", "rmtree",
                  "remove", "chmod", "rename"}
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in banned:
                    offenders.append(name)
        self.assertEqual([], offenders, "纯模型不得写文件")

    def test_function_is_exported(self):
        self.assertIn("blocked_handover", rc.__all__)


class ScreenToolTests(unittest.TestCase):
    """`tools/handover_screen.py`：只读渲染层。"""

    def setUp(self):
        from tools import handover_screen as hs
        self.hs = hs
        self.payload = rc.blocked_handover(
            {"leaves": [_leaf(unmet_dependencies=["UX-SEARCH-FILTER"],
                              artifacts=[{"path": "成果/复函/a.md", "bytes": 2048,
                                          "sha256": "f" * 64}])]},
            section_no="6.10")

    def test_render_contains_all_six_sections(self):
        text = self.hs.render(self.payload)
        for marker in ("[1] 阻塞原因", "[2] 最后有效心跳", "[3] 现有成果",
                       "[4] 原认领成员 / 会话", "[5] 可验证的停止状态", "[6] 下一句指令"):
            self.assertIn(marker, text)
        self.assertIn("§6.10", text)

    def test_render_marks_unconfirmed_stop_explicitly(self):
        leaf = _leaf(state="claimed", owner="trae", conversation_id="cv-" + "9" * 24,
                     heartbeat=_hb(source="mtime_only", age_s=None, stale=True))
        payload = rc.blocked_handover({"leaves": [leaf]}, section_no="6.10")
        text = self.hs.render(payload)
        self.assertIn("unconfirmed", text)
        self.assertIn("允许接手：否", text)
        self.assertIn("票据释放：否", text)

    def test_stop_flag_detection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertFalse(self.hs.stop_flag_present(root))
            (root / "STOP").write_text("", encoding="utf-8")
            self.assertTrue(self.hs.stop_flag_present(root))

    def test_tool_never_writes(self):
        import ast
        source = (ROOT / "tools" / "handover_screen.py").read_text(encoding="utf-8")
        banned = {"write_text", "write_bytes", "unlink", "rmtree", "mkdir"}
        offenders = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in banned:
                    offenders.append(name)
        self.assertEqual([], offenders, "接手说明入口必须是只读的")


if __name__ == "__main__":
    unittest.main()
