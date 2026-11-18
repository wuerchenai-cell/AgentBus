# -*- coding: utf-8 -*-
"""Task 4 测试：正式展示文案与纯展示层。

来源：WorkBuddy（平级成员），2026-10-05，任务 m-1791169024293-907d25
（job j-b2ea92e011777b6bcfa403cc91d2b68d）。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。
依据：IMPLEMENTATION_PLAN.md Task 4、DESIGN_RESOLUTIONS.md 第 1/4/5 节、Global Constraints。

反例先行的三组（对应 Task 4 的三条勾选项）：
  1. unknown 冲突 → 说明具体原因并出现“保留现有内容”；只读 create/update → “尚未同步”。
  2. 三态初值未验证；global/desktop 执行版本为“已记录版本，实际加载未验证”；
     安装成功不得涂成调用通过。
  3. 一页流程稿为只读阶段，无虚假按钮；六条图例各有条件与来源证据，无泛化“已连接”。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import unittest

# Codex internal integration correction, 2026-10-05: success fixtures supply
# trusted caller validation results. Original author delivery is archived.

from agentbus.skill_presentation import (
    LEGEND,
    LEGEND_ORDER,
    READONLY_STAGE_NOTE,
    present_preview,
)


def _minimal(action, **extra):
    base = {"action": action}
    base.update(extra)
    return base


class ConflictWordingTests(unittest.TestCase):
    """unknown 冲突必须给出具体原因，并明确保留现有内容。"""

    def test_unknown_owner_conflict_says_keep_existing(self):
        view = present_preview(_minimal(
            "conflict", owner="unknown", reason_codes=["OWNER_UNKNOWN"],
            scope="global", route="desktop"))
        self.assertIn("保留", view["summary"])
        self.assertIn("未验证", view["invocation_label"])
        self.assertIn("维护者", view["summary"])

    def test_conflict_reason_codes_are_translated_not_dumped_raw(self):
        view = present_preview(_minimal(
            "conflict", owner="unknown",
            reason_codes=["OWNER_UNKNOWN", "WRITE_NOT_PERMITTED"]))
        joined = " / ".join(view["reason_texts"])
        self.assertIn("维护者", joined)
        self.assertIn("只读", joined)
        # 原始代码不得作为唯一文案出现
        self.assertNotEqual(joined.strip(), "OWNER_UNKNOWN / WRITE_NOT_PERMITTED")

    def test_other_manager_owner_conflict(self):
        view = present_preview(_minimal(
            "conflict", owner="cc-switch", reason_codes=["OWNER_NOT_AGENTBUS"]))
        self.assertIn("保留", view["summary"])
        self.assertIn("cc-switch", view["summary"])

    def test_target_modified_conflict(self):
        view = present_preview(_minimal(
            "conflict", owner="agentbus", reason_codes=["TARGET_MODIFIED"]))
        self.assertIn("保留", view["summary"])
        self.assertIn("改动", view["summary"])

    def test_active_task_conflict(self):
        view = present_preview(_minimal(
            "conflict", owner="agentbus", reason_codes=["ACTIVE_TASK_PIN"]))
        self.assertIn("保留", view["summary"])
        self.assertIn("活跃", view["summary"])

    def test_error_code_conflict_does_not_claim_ready(self):
        view = present_preview(_minimal("conflict", reason_codes=["INVALID_TARGET"]))
        self.assertNotIn("已连接", view["title"] + view["summary"] + view["next_action"])
        self.assertIn("保留", view["summary"])


class ReadonlyStageTests(unittest.TestCase):
    """只读阶段：create/update 候选只能说尚未同步，不得有可点击同步或写权。"""

    def test_create_candidate_says_not_yet_synced(self):
        view = present_preview(_minimal(
            "create", owner="unknown", reason_codes=["TARGET_ABSENT"],
            scope="project", route="desktop"))
        self.assertIn("尚未同步", view["summary"])
        self.assertFalse(view["sync_clickable"])
        self.assertIn("只读", view["next_action"])

    def test_update_candidate_says_not_yet_synced(self):
        view = present_preview(_minimal(
            "update", owner="agentbus", reason_codes=["UPDATE_AVAILABLE"],
            scope="project", route="cli"))
        self.assertIn("尚未同步", view["summary"])
        self.assertFalse(view["sync_clickable"])

    def test_write_permitted_is_never_echoed_as_true(self):
        view = present_preview({"action": "create", "write_permitted": True})
        # 即使上游谎报写权，展示层也不得据此画可点击同步
        self.assertFalse(view["sync_clickable"])
        self.assertIn("只读", view["next_action"])

    def test_unchanged_candidate_is_not_called_not_yet_synced(self):
        view = present_preview(_minimal(
            "unchanged", owner="agentbus", reason_codes=["UNCHANGED"]))
        self.assertNotIn("尚未同步", view["summary"])
        self.assertIn("一致", view["summary"])

    def test_unknown_action_is_not_shown_as_ready(self):
        view = present_preview(_minimal("mystery-action"))
        self.assertIn("需要确认", view["title"])
        self.assertFalse(view["sync_clickable"])


class ThreeStateEvidenceTests(unittest.TestCase):
    """安装/发现/调用三态各自未验证起步，互不代证。"""

    def test_default_tri_state_is_unverified(self):
        view = present_preview({"action": "create"})
        for key in ("installation_label", "discovery_label", "invocation_label"):
            self.assertIn("未验证", view[key], key)

    def test_missing_states_key_falls_back_to_unverified(self):
        view = present_preview({"action": "conflict", "states": None})
        self.assertIn("未验证", view["discovery_label"])
        self.assertIn("未验证", view["invocation_label"])

    def test_installed_recorded_does_not_upgrade_discovery_or_call(self):
        view = present_preview({
            "action": "unchanged",
            "states": {"installed": "recorded", "discovered": "unverified",
                       "called": "unverified"},
        })
        self.assertIn("已记录", view["installation_label"])
        self.assertIn("未验证", view["discovery_label"])
        self.assertIn("未验证", view["invocation_label"])
        self.assertNotIn("调用通过", view["installation_label"])

    def test_called_requires_verified_state(self):
        view = present_preview({
            "action": "unchanged",
            "states": {"installed": "recorded", "discovered": "unverified",
                       "called": "verified"},
        })
        self.assertIn("调用", view["invocation_label"])
        self.assertNotIn("未验证", view["invocation_label"])


class ExecutionVersionTests(unittest.TestCase):
    """执行版本：global 与 desktop 一律不得显示已固定/已加载。"""

    def test_global_desktop_execution_version_unverified(self):
        view = present_preview(_minimal(
            "unchanged", scope="global", route="desktop", execution_fixed=False))
        self.assertIn("已记录版本", view["execution_version_label"])
        self.assertIn("实际加载未验证", view["execution_version_label"])

    def test_global_cli_never_fixed(self):
        view = present_preview(_minimal(
            "unchanged", scope="global", route="cli", execution_fixed=True))
        self.assertIn("未验证", view["execution_version_label"])
        self.assertNotIn("已固定", view["execution_version_label"])

    def test_desktop_project_never_fixed_even_if_upstream_claims(self):
        view = present_preview(_minimal(
            "unchanged", scope="project", route="desktop", execution_fixed=True))
        self.assertIn("未验证", view["execution_version_label"])
        self.assertNotIn("已固定", view["execution_version_label"])

    def test_project_independent_never_fixed_without_verified_carrier(self):
        """修正 2：当前无固定执行载体，上游 flags 不构成验收，一律实际加载未验证。"""
        view = present_preview(_minimal(
            "unchanged", scope="project", route="cli", execution_fixed=True))
        self.assertIn("未验证", view["execution_version_label"])
        self.assertNotIn("已固定", view["execution_version_label"])
        self.assertIn("实际加载未验证", view["execution_version_label"])
        self.assertIs(view["execution_fixed_accepted"], False)

    def test_content_checked_but_execution_unverified_note_present(self):
        view = present_preview({"action": "create", "content": {
            "source_digest": "a" * 64, "source_verified": True}})
        self.assertTrue(any("内容已校验" in n and "执行条件未验证" in n
                            for n in view["notes"]))


class LegendTests(unittest.TestCase):
    """六条图例：条件与来源证据齐备，禁止泛化已连接。"""

    def test_legend_has_six_ordered_entries(self):
        self.assertEqual(tuple(e["key"] for e in LEGEND), LEGEND_ORDER)
        self.assertEqual(LEGEND_ORDER, (
            "unconfigured", "manual_takeover", "running",
            "waiting", "blocked", "done"))

    def test_every_legend_has_condition_and_evidence_source(self):
        for entry in LEGEND:
            self.assertTrue(entry["condition"].strip(), entry["key"])
            self.assertTrue(entry["evidence_source"].strip(), entry["key"])
            # 每条图例必须显式禁止某件事；措辞不强求统一，只要求是禁止句。
            self.assertTrue(entry["never"].startswith("不得"), entry["key"])

    def test_no_generic_connected_anywhere(self):
        view = present_preview({"action": "create"})
        blob = repr(view)
        self.assertNotIn("已连接", blob)
        for entry in LEGEND:
            self.assertNotIn("已连接", repr(entry))

    def test_legend_is_copied_not_shared_mutable(self):
        view = present_preview({"action": "create"})
        view["legend"][0]["label"] = "MUTATED"
        self.assertNotEqual(LEGEND[0]["label"], "MUTATED")


class UserFlowContractTests(unittest.TestCase):
    """一页流程：选择技能→检查与预览→后续受控同步；首项无同步按钮。"""

    def test_stage_names_are_the_three_documented_steps(self):
        view = present_preview({"action": "create"})
        self.assertEqual(view["stage"], "inspect-preview")
        self.assertIn("选择技能", view["flow"][0])
        self.assertIn("检查与预览", view["flow"][1])
        self.assertIn("受控同步", view["flow"][2])

    def test_readonly_stage_note_matches_module_constant(self):
        view = present_preview({"action": "create"})
        self.assertEqual(view["notes"][-1], READONLY_STAGE_NOTE)
        self.assertIn("只读", READONLY_STAGE_NOTE)

    def test_no_fake_button_or_guarantee_language(self):
        view = present_preview({"action": "update", "owner": "agentbus"})
        blob = view["next_action"] + view["summary"]
        for forbidden in ("点击同步", "一键同步", "保证成功", "已同步完成"):
            self.assertNotIn(forbidden, blob)


class CorrectionTests(unittest.TestCase):
    """2026-10-05 正式修正任务 m-1791170402282-71e0df 的五项复核（反例先行）。"""

    # 修正 1：无效源不得无条件写“内容已校验”。
    def test_invalid_source_does_not_claim_content_checked(self):
        view = present_preview(_minimal("conflict", reason_codes=["INVALID_SOURCE"]))
        self.assertFalse(any("内容已校验" in n for n in view["notes"]))
        self.assertTrue(any("内容未校验" in n for n in view["notes"]))
        self.assertIsNone(view["content_digest"])

    def test_conflict_without_source_digest_not_claimed_checked(self):
        view = present_preview(_minimal(
            "conflict", owner="unknown", reason_codes=["OWNER_UNKNOWN"]))
        self.assertFalse(any("内容已校验" in n for n in view["notes"]))

    def test_valid_digest_still_reports_content_checked(self):
        view = present_preview({"action": "create", "content": {
            "source_digest": "a" * 64, "source_verified": True}})
        self.assertTrue(any("内容已校验" in n for n in view["notes"]))

    def test_malformed_digest_is_not_treated_as_checked(self):
        view = present_preview({"action": "create",
                                "content": {"source_digest": "not-a-digest"}})
        self.assertFalse(any("内容已校验" in n for n in view["notes"]))

    # 修正 2：无固定执行载体，任何组合都不得显示已固定。
    def test_global_and_desktop_still_unverified(self):
        view = present_preview(_minimal(
            "unchanged", scope="global", route="desktop", execution_fixed=True))
        self.assertIn("实际加载未验证", view["execution_version_label"])
        self.assertIs(view["execution_fixed_accepted"], False)

    def test_execution_fixed_upstream_flag_is_reported_but_not_accepted(self):
        view = present_preview(_minimal(
            "unchanged", scope="project", route="independent", execution_fixed=True))
        self.assertIn("不构成验收", view["execution_fixed_note"])
        self.assertIs(view["execution_fixed_accepted"], False)

    # 修正 3：空信箱 + 旧回执不证明“正在等待”。
    def test_waiting_legend_needs_current_wait_evidence(self):
        entry = [e for e in LEGEND if e["key"] == "waiting"][0]
        text = entry["condition"] + entry["evidence_source"] + entry["never"]
        self.assertIn("人工接手", text)
        self.assertIn("旧回执", text)
        self.assertNotIn("在线", entry["label"])

    def test_waiting_without_current_evidence_is_manual_takeover(self):
        view = present_preview({"action": "unchanged"})
        self.assertIn("人工接手", view["waiting_hint"])

    def test_waiting_with_current_evidence_can_show_waiting(self):
        view = present_preview({
            "action": "unchanged",
            "now_ts": 15.0,
            "wait_evidence": {"waiting_tool": "watchtower", "observed_at_ts": 10.0,
                              "expires_at_ts": 20.0, "verified": True},
        })
        self.assertIn("等待", view["waiting_hint"])
        self.assertNotIn("人工接手", view["waiting_hint"])

    def test_empty_mailbox_plus_old_receipt_is_not_waiting(self):
        view = present_preview({
            "action": "unchanged",
            "states": {"installed": "unverified", "discovered": "unverified",
                       "called": "unverified"},
            "mailbox_empty": True,
            "last_receipt_age_s": 86400,
        })
        self.assertIn("人工接手", view["waiting_hint"])

    # 修正 4：done 需要 任务成功 + 结果成功 + 产物核对，失败不得绿色。
    def test_done_legend_requires_all_three_conditions(self):
        entry = [e for e in LEGEND if e["key"] == "done"][0]
        text = entry["condition"] + entry["evidence_source"] + entry["never"]
        for token in ("成功", "产物核对", "失败"):
            self.assertIn(token, text)

    def test_completion_requires_success_result_and_artifacts(self):
        view = present_preview({
            "action": "unchanged", "source_status": "done",
            "result_outcome": "success",
            "artifacts_verified": True,
            "artifacts": [{"path": "a.py", "bytes": 1, "sha256": "b" * 64}],
        })
        self.assertIn("已完成", view["completion_label"])

    def test_done_without_result_or_artifacts_is_not_complete(self):
        view = present_preview({"action": "unchanged", "source_status": "done"})
        self.assertNotIn("已完成", view["completion_label"])

    def test_failed_result_is_never_green(self):
        view = present_preview({
            "action": "unchanged", "source_status": "done",
            "result_outcome": "failed",
            "artifacts": [{"path": "a.py", "bytes": 1, "sha256": "b" * 64}],
        })
        self.assertNotIn("已完成", view["completion_label"])
        self.assertIn("失败", view["completion_label"])

    # 修正 5：六状态与无虚假同步控件不变。
    def test_six_legend_and_no_sync_control_still_hold(self):
        view = present_preview({"action": "create"})
        self.assertEqual(len(view["legend"]), 6)
        self.assertEqual(tuple(e["key"] for e in view["legend"]), LEGEND_ORDER)
        self.assertFalse(view["sync_clickable"])
        self.assertIn("只读", view["next_action"])


class DefensiveInputTests(unittest.TestCase):
    """非 dict 与畸形输入不得抛出未处理异常，也不得伪造绿色。"""

    def test_non_dict_input_raises_value_error(self):
        with self.assertRaises(ValueError):
            present_preview(None)  # type: ignore[arg-type]

    def test_reason_codes_not_list_is_tolerated(self):
        view = present_preview({"action": "conflict", "reason_codes": "OWNER_UNKNOWN"})
        self.assertIn("保留", view["summary"])

    def test_unknown_reason_code_is_kept_visible(self):
        view = present_preview(_minimal("conflict", reason_codes=["SOMETHING_NEW"]))
        self.assertIn("SOMETHING_NEW", " ".join(view["reason_texts"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
