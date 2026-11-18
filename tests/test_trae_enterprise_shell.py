# -*- coding: utf-8 -*-
"""TR-DESKTOP 定向测试：design_system / gui_state / enterprise_shell 纯函数与布局。

来源：Trae，2026-10-08；任务 m-1791457584495-f44d5b（作业 j-b7ed91331128d934d22268834e306bd4）。
纯函数测试：不创建 Tk 窗口、不读写信箱、不起模型、不访问网络。
运行：C:/Python314/python.exe -B -X utf8 authors/trae/tests/test_enterprise_shell.py
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

# Codex integration migration, 2026-10-08: target this checkout only.
INTEGRATION = Path(__file__).resolve().parents[1]
if str(INTEGRATION) not in sys.path:
    sys.path.insert(0, str(INTEGRATION))
import agentbus
assert Path(agentbus.__file__).resolve().parent == INTEGRATION / "agentbus", "wrong runtime"

from agentbus import design_system, enterprise_shell, gui_state  # noqa: E402


def make_view(n=2, *, goal="发布企业桌面", coordinator="m0", action_id="home",
              state_label="已配置", title="演示项目", incomplete=False):
    members = [{"id": "m%d" % i, "name": "成员%d" % i, "state_label": state_label,
                "tone": "neutral", "evidence": [], "observed_at": None}
               for i in range(n)]
    resp = [{"member_id": "m%d" % i, "member": "成员%d" % i, "task": None,
             "checkpoint": None, "state": state_label, "next_step": "无需动作",
             "write_capability": "未验证"} for i in range(n)]
    return {
        "project_title": title, "goal": goal, "stage": "working",
        "stage_label": "作业进行中", "headline": "h", "next_action": "n",
        "action_id": action_id,
        "cards": [
            {"id": "tasks", "label": "任务", "count": n},
            {"id": "deliverables", "label": "已验证任务交付", "count": 0, "pending_verify": True},
            {"id": "ready", "label": "待接手", "count": 0},
            {"id": "blocked", "label": "阻塞", "count": 0},
        ],
        "responsibilities": resp, "members": members, "members_collapsed": [],
        "coordinator": coordinator, "records_incomplete": incomplete,
        "incomplete_reason": "测试" if incomplete else "",
    }


class DesignSystemTests(unittest.TestCase):
    def test_tokens_brand_neutral_and_complete(self):
        tokens = design_system.get_tokens()
        for key in ("background", "surface", "text", "accent", "ok", "warn", "err", "disabled_text"):
            self.assertIn(key, tokens)
        joined = " ".join(tokens.values()).lower()
        for brand in ("codex", "workbuddy", "trae", "opencode", "zcode", "antigravity"):
            self.assertNotIn(brand, joined)
        # 返回副本，外部改动不影响共享令牌。
        tokens["text"] = "#000000"
        self.assertNotEqual(design_system.TOKENS["text"], "#000000")

    def test_layout_profile_fallback(self):
        self.assertEqual(design_system.layout_profile("compact")["name"], "compact")
        self.assertEqual(design_system.layout_profile("nope")["name"], "normal")
        self.assertEqual(design_system.layout_profile(None)["name"], "normal")

    def test_choose_layout_thresholds(self):
        self.assertEqual(design_system.choose_layout(600), "compact")
        self.assertEqual(design_system.choose_layout(1000), "normal")
        self.assertEqual(design_system.choose_layout(1600), "wide")

    def test_choose_layout_150_percent_dpi(self):
        # 150% DPI ≈ scaling 2.0；1800 物理像素 ≈ 1200 逻辑像素 → normal。
        self.assertEqual(design_system.choose_layout(1800, 2.0), "normal")
        self.assertEqual(design_system.choose_layout(2400, 2.0), "wide")

    def test_scaled_150_percent_dpi(self):
        self.assertEqual(design_system.scaled(16, 2.0), 24)
        self.assertEqual(design_system.scaled(16, design_system.BASELINE_SCALING), 16)

    def test_scaled_bounds(self):
        self.assertEqual(design_system.scaled(4096, 4.0), 4096)
        self.assertEqual(design_system.scaled(0, 1.0), 0)
        with self.assertRaises(TypeError):
            design_system.scaled(1.5, 1.0)
        with self.assertRaises(ValueError):
            design_system.scaled(-1, 1.0)

    def test_clamp_scaling_bad_input(self):
        self.assertEqual(design_system.clamp_scaling("x"), design_system.BASELINE_SCALING)
        self.assertEqual(design_system.clamp_scaling(-3), design_system.BASELINE_SCALING)
        self.assertEqual(design_system.clamp_scaling(9.9), design_system.MAX_SCALING)

    def test_contrast_requirements(self):
        self.assertTrue(design_system.contrast_ok("#1f2937", "#ffffff"))
        self.assertTrue(design_system.contrast_ok("#ffffff", "#2459b8"))
        self.assertFalse(design_system.contrast_ok("#ffffff", "#fbfbfb"))

    def test_contrast_invalid_input(self):
        self.assertFalse(design_system.contrast_ok("bad", "#ffffff"))
        self.assertFalse(design_system.contrast_ok("#12345", "#ffffff"))

    def test_keyboard_shortcuts_copy(self):
        shortcuts = design_system.keyboard_shortcuts()
        self.assertIn("next_action", shortcuts)
        shortcuts["next_action"] = "x"
        self.assertNotEqual(design_system.KEYBOARD_SHORTCUTS["next_action"], "x")


class StatusLightTableTests(unittest.TestCase):
    CASES = (
        ("停用优先", {"enabled": False}, "disabled"),
        ("阻塞优先", {"blocked_count": 2, "ready_count": 3}, "blocked"),
        ("证据矛盾→未知", {"evidence_conflict": True, "ready_count": 1}, "unknown"),
        ("需人工接手", {"needs_handoff": True}, "handoff"),
        ("有心跳证据→执行中", {"heartbeat_verified": True}, "running"),
        ("已认领运行未确认", {"claimed_count": 1}, "claimed"),
        ("待认领", {"ready_count": 1}, "ready"),
        ("等待文件事件", {"waiting_file_event": True}, "waiting"),
        ("待派发", {"dispatched": True}, "dispatch_pending"),
        ("完成", {"done_count": 1}, "done"),
        ("空证据→未知", {}, "unknown"),
    )

    def test_state_table(self):
        self.assertGreaterEqual(len(self.CASES), 10)
        for note, evidence, expected in self.CASES:
            with self.subTest(note=note):
                light = gui_state.derive_status_light(dict(evidence))
                self.assertEqual(light["state"], expected)
                self.assertIn(light["tone"], ("neutral", "info", "warn", "ok"))
                self.assertTrue(light["label"])

    def test_claim_is_not_running(self):
        light = gui_state.derive_status_light({"claimed_count": 1})
        self.assertEqual(light["state"], "claimed")
        self.assertFalse(light["execution_verified"])

    def test_mtime_is_not_heartbeat(self):
        light = gui_state.derive_status_light({"file_mtime_seen": True, "claimed_count": 1})
        self.assertNotEqual(light["state"], "running")
        self.assertFalse(light["execution_verified"])
        self.assertEqual(light["state"], "claimed")

    def test_mtime_alone_is_unknown_not_running(self):
        light = gui_state.derive_status_light({"file_mtime_seen": True})
        self.assertEqual(light["state"], "unknown")

    def test_blocked_beats_running_and_marks_contradiction(self):
        light = gui_state.derive_status_light(
            {"blocked_count": 1, "ready_count": 5, "heartbeat_verified": True})
        self.assertEqual(light["state"], "blocked")
        self.assertTrue(light["contradiction"])

    def test_disabled_beats_heartbeat_and_marks_contradiction(self):
        light = gui_state.derive_status_light({"enabled": False, "heartbeat_verified": True})
        self.assertEqual(light["state"], "disabled")
        self.assertTrue(light["contradiction"])

    def test_empty_is_not_done(self):
        light = gui_state.derive_status_light({})
        self.assertNotEqual(light["state"], "done")
        self.assertEqual(light["state"], "unknown")

    def test_zero_counts_are_not_done(self):
        light = gui_state.derive_status_light(
            {"ready_count": 0, "claimed_count": 0, "blocked_count": 0, "done_count": 0})
        self.assertNotEqual(light["state"], "done")

    def test_cross_project_same_state(self):
        ev = {"ready_count": 1}
        a = gui_state.derive_status_light({**ev, "workspace": r"I:\A"})
        b = gui_state.derive_status_light({**ev, "workspace": r"I:\B"})
        self.assertEqual(a["state"], b["state"])
        self.assertNotEqual(a["workspace"], b["workspace"])

    def test_evidence_not_mutated(self):
        ev = {"enabled": False, "ready_count": 2, "heartbeat_verified": True}
        snap = copy.deepcopy(ev)
        gui_state.derive_status_light(ev)
        self.assertEqual(ev, snap)

    def test_bad_input_no_raise(self):
        for ev in (None, "x", 3, {"ready_count": -1}, {"heartbeat_verified": "yes"}):
            with self.subTest(ev=ev):
                self.assertIn("state", gui_state.derive_status_light(ev))

    def test_summary_counts(self):
        members = [{"state": "ready"}, {"state": "ready"}, {"state": "blocked"}, {"state": "nope"}]
        summary = gui_state.status_light_summary(members)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["counts"]["ready"], 2)
        self.assertEqual(summary["counts"]["blocked"], 1)

    def test_member_next_action_shape(self):
        view = gui_state.build_member_next_action("trae", {"ready_count": 1}, r"I:\A")
        for key in ("state", "label", "action", "reason", "member", "workspace",
                    "execution_verified", "resume_note"):
            self.assertIn(key, view)
        self.assertEqual(view["state"], "ready")


class ShellTests(unittest.TestCase):
    def test_zero_members_empty_state(self):
        shell = enterprise_shell.build_shell_view(make_view(0, goal="目标未确认"))
        self.assertEqual(shell["member_count"], 0)
        self.assertEqual(shell["empty_state"]["id"], "team")

    def test_one_member(self):
        shell = enterprise_shell.build_shell_view(make_view(1))
        self.assertEqual(shell["member_count"], 1)
        self.assertEqual(shell["members"][0]["member_id"], "m0")

    def test_two_members(self):
        shell = enterprise_shell.build_shell_view(make_view(2))
        self.assertEqual(shell["member_count"], 2)

    def test_twenty_members_dynamic_and_overflow(self):
        shell = enterprise_shell.build_shell_view(make_view(20), layout="compact")
        self.assertEqual(shell["member_count"], 20)
        # compact: 4 行 × 1 列 = 4 可见，其余折叠；不硬编码成员数。
        self.assertEqual(len(shell["visible_members"]), 4)
        self.assertEqual(shell["overflow_count"], 16)
        self.assertEqual(shell["light_summary"]["total"], 20)

    def test_coordinator_role(self):
        shell = enterprise_shell.build_shell_view(make_view(3, coordinator="m1"))
        roles = {row["member_id"]: row["role"] for row in shell["members"]}
        self.assertEqual(roles["m1"], "负责人")
        self.assertEqual(roles["m0"], "成员")

    def test_stop_sets_single_primary_action(self):
        shell = enterprise_shell.build_shell_view(make_view(2), stopped=True)
        self.assertTrue(shell["stopped"])
        self.assertEqual(shell["primary_action"]["id"], "tasks")
        self.assertIn("暂停", shell["primary_action"]["label"])

    def test_goal_unconfirmed_empty_state(self):
        shell = enterprise_shell.build_shell_view(make_view(2, goal="目标未确认"))
        self.assertFalse(shell["goal_confirmed"])
        self.assertEqual(shell["empty_state"]["id"], "home")

    def test_primary_action_team(self):
        shell = enterprise_shell.build_shell_view(make_view(0, goal="目标未确认", action_id="team"))
        self.assertEqual(shell["primary_action"]["id"], "team")

    def test_long_name_truncated_but_preserved(self):
        view = make_view(1)
        long_name = "非常长的中文成员显示名称" * 3
        view["responsibilities"][0]["member"] = long_name
        view["members"][0]["name"] = long_name
        shell = enterprise_shell.build_shell_view(view, layout="compact")
        row = shell["members"][0]
        self.assertLessEqual(len(row["name"]), 24)
        self.assertTrue(row["name"].endswith("…"))
        self.assertEqual(row["name_full"], long_name)

    def test_layout_profile_applied(self):
        compact = enterprise_shell.build_shell_view(make_view(1), layout="compact")
        wide = enterprise_shell.build_shell_view(make_view(1), layout="wide")
        self.assertEqual(compact["layout"]["card_columns"], 1)
        self.assertEqual(wide["layout"]["card_columns"], 4)
        self.assertFalse(compact["layout"]["show_member_evidence"])
        self.assertTrue(wide["layout"]["show_member_evidence"])

    def test_layout_auto_by_width(self):
        shell = enterprise_shell.build_shell_view(make_view(1), width_px=1600, scaling=1.0)
        self.assertEqual(shell["layout"]["name"], "wide")

    def test_member_evidence_injection_running(self):
        shell = enterprise_shell.build_shell_view(
            make_view(1), member_evidence={"m0": {"heartbeat_verified": True}})
        self.assertEqual(shell["members"][0]["status_light"]["state"], "running")

    def test_member_label_mapping_claim_not_running(self):
        shell = enterprise_shell.build_shell_view(
            make_view(1, state_label="已认领，运行未确认"))
        light = shell["members"][0]["status_light"]
        self.assertEqual(light["state"], "claimed")
        self.assertFalse(light["execution_verified"])

    def test_member_label_mapping_disabled(self):
        shell = enterprise_shell.build_shell_view(make_view(1, state_label="已停用"))
        self.assertEqual(shell["members"][0]["status_light"]["state"], "disabled")

    def test_records_incomplete_passthrough(self):
        shell = enterprise_shell.build_shell_view(make_view(1, incomplete=True))
        self.assertTrue(shell["records_incomplete"])
        self.assertEqual(shell["incomplete_reason"], "测试")

    def test_focus_order_primary_first(self):
        shell = enterprise_shell.build_shell_view(make_view(2, action_id="access"))
        order = shell["accessibility"]["focus_order"]
        self.assertEqual(order[0], "action:access")
        self.assertIn("card:tasks", order)
        self.assertIn("member:m0", order)

    def test_cards_passthrough(self):
        shell = enterprise_shell.build_shell_view(make_view(2))
        self.assertEqual([c["id"] for c in shell["cards"]],
                         ["tasks", "deliverables", "ready", "blocked"])
        self.assertTrue(shell["cards"][1]["pending_verify"])

    def test_bad_input_no_raise(self):
        for view in (None, "x", 3, {}, {"responsibilities": "x", "cards": None}):
            with self.subTest(view=view):
                shell = enterprise_shell.build_shell_view(view)
                self.assertIn("primary_action", shell)
                self.assertIn("members", shell)

    def test_nav_ids_default(self):
        shell = enterprise_shell.build_shell_view(make_view(1))
        self.assertEqual(shell["nav_ids"],
                         ["home", "team", "tasks", "access", "settings", "logs"])

    def test_view_not_mutated(self):
        view = make_view(2)
        snap = copy.deepcopy(view)
        enterprise_shell.build_shell_view(view, member_evidence={"m0": {"ready_count": 1}})
        self.assertEqual(view, snap)


if __name__ == "__main__":
    unittest.main(verbosity=2)