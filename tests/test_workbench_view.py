# -*- coding: utf-8 -*-
"""workbench_view 反例测试：纯函数首页摘要与证据状态。

来源：ZCode（平级成员），2026-10-05，任务 m-1791187019713-284e2c（job j-534409...见任务信封）。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。
依据：DESIGN.md 接口约定 + Codex 实施确认契约（真实输入形状、认领只显示已认领运行未确认、
缺证默认当前会话未确认、空信箱不推导等待、action_id 优先级：草稿/无团队 team、阻塞 tasks、
ready access、已认领 tasks、全空 home）。
先红后绿：本文件先于实现编写并运行失败，实现后全部通过。
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy
import json
import unittest

from agentbus.workbench_view import NAV_IDS, build_overview


def make_config():
    return {
        "workspace": r"D:\示例空间\协作 样例",
        "draft_dirty": False,
        "director": "codex",
        "workers": {
            "codex": {"enabled": True},
            "workbuddy": {"enabled": True},
            "zcode": {"enabled": True},
            "opencode": {"enabled": False},
        },
    }


def make_health():
    return {
        "observed_at": "2026-10-05T16:00:00+08:00",
        "members": [
            {"name": "codex", "indicator": {"key": "idle"}, "last_receipt_at": "2026-10-05T15:00:00+08:00"},
            {"name": "zcode", "indicator": {"key": "idle"}, "last_receipt_at": "2026-10-05T14:00:00+08:00"},
        ],
    }


def task(worker, status):
    return {"worker": worker, "status": status}


def make_jobs():
    return [
        {"id": "j-1", "state": "working", "tasks": [task("zcode", "claimed"), task("workbuddy", "pending")]},
        {"id": "j-2", "state": "blocked", "tasks": [task("zcode", "blocked")]},
        {"id": "j-3", "state": "queued", "tasks": [task("codex", "pending")]},
    ]


class BuildOverviewTests(unittest.TestCase):
    def test_full_inputs_basic_fields(self):
        out = build_overview(make_config(), make_health(), make_jobs())
        self.assertEqual(out["route_label"], "文件协作")
        self.assertEqual(out["coordinator"], "codex")
        self.assertEqual(out["member_count"], 3)
        self.assertIn("协作 样例", out["project_title"])
        self.assertIn(out["action_id"], NAV_IDS)

    def test_priority_blocked_beats_ready_beats_claimed(self):
        out = build_overview(make_config(), make_health(), make_jobs())
        self.assertEqual(out["action_id"], "tasks")
        self.assertIn("阻塞", out["headline"])

    def test_draft_dirty_goes_team_first(self):
        cfg = make_config()
        cfg["draft_dirty"] = True
        out = build_overview(cfg, make_health(), make_jobs())
        self.assertEqual(out["action_id"], "team")
        self.assertIn("未保存", out["headline"])

    def test_no_team_goes_team(self):
        out = build_overview({"workspace": "", "draft_dirty": False, "workers": {}}, None, None)
        self.assertEqual(out["action_id"], "team")
        self.assertEqual(out["member_count"], 0)
        self.assertIn("未配置", out["headline"])

    def test_ready_only_goes_access(self):
        jobs = [{"id": "j-1", "tasks": [task("zcode", "pending")]}]
        out = build_overview(make_config(), make_health(), jobs)
        self.assertEqual(out["action_id"], "access")
        self.assertIn("待接手", out["headline"])

    def test_claimed_only_goes_tasks_and_label_exact(self):
        jobs = [{"id": "j-1", "tasks": [task("zcode", "claimed")]}]
        out = build_overview(make_config(), make_health(), jobs)
        self.assertEqual(out["action_id"], "tasks")
        zc = next(m for m in out["members"] if m["id"] == "zcode")
        self.assertEqual(zc["state_label"], "已认领，运行未确认")
        self.assertNotIn("执行中", zc["state_label"])
        self.assertTrue(any("jobs" in e for e in zc["evidence"]))
        self.assertEqual(zc["observed_at"], "2026-10-05T16:00:00+08:00")

    def test_all_empty_goes_home_without_waiting_inference(self):
        out = build_overview(make_config(), make_health(), [])
        self.assertEqual(out["action_id"], "home")
        self.assertIn("暂无", out["headline"])
        labels = " ".join(m["state_label"] for m in out["members"])
        self.assertNotIn("等待中", labels)

    def test_health_missing_means_unconfirmed_not_configured(self):
        out = build_overview(make_config(), None, [])
        members = {m["id"]: m for m in out["members"]}
        self.assertEqual(members["zcode"]["state_label"], "当前会话未确认")
        self.assertIsNone(members["zcode"]["observed_at"])
        self.assertTrue(any("health" in e for e in members["zcode"]["evidence"]))

    def test_health_entry_without_tasks_does_not_confirm_current_session(self):
        out = build_overview(make_config(), make_health(), [])
        members = {m["id"]: m for m in out["members"]}
        self.assertEqual(members["codex"]["state_label"], "当前会话未确认")
        self.assertEqual(members["codex"]["observed_at"], "2026-10-05T16:00:00+08:00")

    def test_health_gap_member_is_unconfirmed(self):
        out = build_overview(make_config(), make_health(), [])
        members = {m["id"]: m for m in out["members"]}
        self.assertEqual(members["workbuddy"]["state_label"], "当前会话未确认")

    def test_disabled_member_is_stopped(self):
        out = build_overview(make_config(), make_health(), [])
        members = {m["id"]: m for m in out["members"]}
        self.assertEqual(members["opencode"]["state_label"], "已停用")

    def test_member_evidence_and_tone_present(self):
        out = build_overview(make_config(), make_health(), make_jobs())
        for m in out["members"]:
            self.assertIsInstance(m["evidence"], list)
            self.assertIn(m["tone"], ("neutral", "info", "warn", "ok"))

    def test_malformed_inputs_degrade_without_exception(self):
        out = build_overview({}, None, None)
        self.assertIn("未选择", out["project_title"])
        self.assertEqual(out["action_id"], "team")
        out = build_overview(make_config(), "bad", "bad")
        self.assertEqual(out["action_id"], "home")
        out = build_overview(make_config(), make_health(), [{"id": "j-9", "tasks": "bad"}])
        self.assertEqual(out["action_id"], "home")

    def test_inputs_not_mutated(self):
        cfg, health, jobs = make_config(), make_health(), make_jobs()
        c2, h2, j2 = copy.deepcopy(cfg), copy.deepcopy(health), copy.deepcopy(jobs)
        build_overview(cfg, health, jobs)
        self.assertEqual(cfg, c2)
        self.assertEqual(health, h2)
        self.assertEqual(jobs, j2)

    def test_output_is_pure_json_and_deterministic(self):
        a = build_overview(make_config(), make_health(), make_jobs())
        b = build_overview(make_config(), make_health(), make_jobs())
        self.assertEqual(a, b)

        def check(value):
            if value is None or isinstance(value, (str, int, bool)):
                return
            if isinstance(value, list):
                for item in value:
                    check(item)
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertIsInstance(key, str)
                    check(item)
                return
            self.fail("非 JSON 兼容类型: %r" % (value,))

        check(a)

    def test_member_name_falls_back_to_id(self):
        out = build_overview(make_config(), make_health(), [])
        members = {m["id"]: m for m in out["members"]}
        self.assertEqual(members["zcode"]["name"], "zcode")


class CodexIntegrationOverviewTests(unittest.TestCase):
    """来源：Codex 集成修正，2026-10-05；作者原件另行保留。

    反例针对真实 connection_summary 字段及配置的已启用成员。
    """

    def public_health(self, *, ready=0, processing=0, blocked=0, key="idle", **extra):
        return {"paused": False, "truncated": False, "current_sessions_verified": False,
                "members": [{"name": "codex", "display_name": "Codex",
                             "task_counts": {"ready": ready, "processing": processing,
                                             "blocked": blocked, "done": 7},
                             "indicator": {"key": key, "label": "公开状态"},
                             "unreadable_count": 0, "current_session_verified": False,
                             "last_receipt_at": "2026-10-04T18:00:00+08:00", **extra}]}

    def test_disabled_coordinator_is_not_a_configured_team(self):
        cfg = make_config()
        cfg["workers"]["codex"]["enabled"] = False
        view = build_overview(cfg)
        self.assertEqual(view["member_count"], 2)
        self.assertIsNone(view["coordinator"])
        self.assertEqual(view["action_id"], "team")

    def test_nonmember_director_does_not_imply_configured_team(self):
        cfg = make_config()
        cfg["director"] = "nonmember"
        view = build_overview(cfg)
        self.assertIsNone(view["coordinator"])
        self.assertIn("未配置", view["headline"])

    def test_all_disabled_members_are_not_an_active_group(self):
        cfg = make_config()
        for worker in cfg["workers"].values():
            worker["enabled"] = False
        view = build_overview(cfg)
        self.assertEqual(view["member_count"], 0)
        self.assertEqual(view["action_id"], "team")

    def test_file_tasks_without_job_appear_on_overview(self):
        view = build_overview(make_config(), self.public_health(ready=2, key="pending"), [])
        self.assertEqual(view["headline"], "有 2 项任务待接手")
        self.assertEqual(view["action_id"], "access")
        self.assertEqual(view["members"][0]["state_label"], "有任务待接手")

    def test_processing_file_task_is_a_claim_not_a_running_confirmation(self):
        view = build_overview(make_config(), self.public_health(processing=1, key="claimed"), [])
        self.assertEqual(view["members"][0]["state_label"], "已认领，运行未确认")
        self.assertEqual(view["action_id"], "tasks")

    def test_overlapping_job_and_mailbox_counts_use_max_not_sum(self):
        jobs = [{"tasks": [task("codex", "pending"), task("codex", "pending")]}]
        view = build_overview(make_config(), self.public_health(ready=2, key="pending"), jobs)
        self.assertEqual(view["headline"], "有 2 项任务待接手")

    def test_larger_job_count_is_not_hidden_by_smaller_mailbox_count(self):
        jobs = [{"tasks": [task("codex", "blocked"), task("codex", "blocked")]}]
        view = build_overview(make_config(), self.public_health(blocked=1, key="blocked"), jobs)
        self.assertEqual(view["headline"], "有 2 项任务阻塞")

    def test_health_record_and_historical_receipt_are_not_current_session_verification(self):
        view = build_overview(make_config(), self.public_health(), [])
        member = view["members"][0]
        self.assertEqual(member["state_label"], "当前会话未确认")
        self.assertTrue(any("2026-10-04T18:00:00+08:00" in text and "历史" in text
                            for text in member["evidence"]))
        self.assertEqual(view["headline"], "团队已配置，暂无待办")

    def test_idle_next_action_starts_a_goal_instead_of_reopening_overview(self):
        view = build_overview(make_config(), self.public_health(), [])
        self.assertEqual(view["next_action"], "填写目标并复制任务指令")
        self.assertEqual(view["action_id"], "home")
        self.assertNotIn("就绪", view["headline"])

    def test_attention_indicator_does_not_turn_unreadable_records_into_idle(self):
        view = build_overview(make_config(), self.public_health(key="attention"), [])
        self.assertEqual(view["members"][0]["state_label"], "状态未知")
        self.assertIn("检查", view["headline"])
        self.assertNotEqual(view["action_id"], "home")

    def test_unreadable_count_is_preserved_even_when_other_indicator_says_idle(self):
        view = build_overview(make_config(), self.public_health(unreadable_count=2), [])
        self.assertEqual(view["members"][0]["state_label"], "状态未知")
        self.assertIn("检查", view["headline"])

    def test_truncated_health_is_not_evidence_of_empty_mailboxes(self):
        health = self.public_health()
        health["truncated"] = True
        view = build_overview(make_config(), health, [])
        enabled = [m for m in view["members"] if m["id"] != "opencode"]
        self.assertTrue(all(m["state_label"] == "状态未知" for m in enabled))
        self.assertIn("检查", view["headline"])

    def test_paused_project_never_reports_ready_or_idle(self):
        health = self.public_health(ready=1, key="paused")
        health["paused"] = True
        view = build_overview(make_config(), health, [])
        self.assertIn("暂停", view["headline"])
        self.assertIn("暂停", view["members"][0]["state_label"])
        self.assertNotIn("就绪", view["headline"])
        self.assertNotEqual(view["action_id"], "access")

    def test_nonstring_worker_keys_and_malformed_entries_are_json_safe(self):
        cfg = make_config()
        cfg["workers"][17] = {"enabled": True, "name": object()}
        cfg["workers"][("bad",)] = {"enabled": True}
        cfg["workers"]["malformed"] = [object()]
        health = self.public_health(ready=[3], key="unknown-vendor-state")
        jobs = [{"tasks": [None, {"worker": ["codex"], "status": {}},
                           {"worker": "codex", "status": object()}]}]
        view = build_overview(cfg, health, jobs)
        json.dumps(view, allow_nan=False)
        self.assertTrue(all(isinstance(item["id"], str) for item in view["members"]))
        self.assertEqual(view["member_count"], 3)

    def test_invalid_mailbox_counts_do_not_report_pending_or_idle(self):
        for value in (True, -1, "4", [1], float("nan")):
            with self.subTest(value=value):
                view = build_overview(make_config(), self.public_health(ready=value), [])
                self.assertEqual(view["members"][0]["state_label"], "状态未知")
                self.assertIn("检查", view["headline"])
                json.dumps(view, allow_nan=False)

    def test_public_health_and_job_input_are_not_mutated(self):
        cfg, health = make_config(), self.public_health(ready=2)
        jobs = [{"tasks": [task("codex", "pending")]}]
        expected = copy.deepcopy((cfg, health, jobs))
        build_overview(cfg, health, jobs)
        self.assertEqual((cfg, health, jobs), expected)

    def test_member_pause_indicator_is_order_independent_for_all_enabled_members(self):
        cfg = make_config()
        cfg["workers"] = {"workbuddy": {"enabled": True}, "codex": {"enabled": True}}
        health = self.public_health(key="paused")
        jobs = [{"tasks": [task("workbuddy", "pending")]}]
        view = build_overview(cfg, health, jobs)
        self.assertTrue(all("暂停" in member["state_label"] for member in view["members"]))
        self.assertIn("暂停", view["headline"])

    def test_invalid_enormous_mailbox_count_is_unknown_and_json_serializable(self):
        view = build_overview(make_config(), self.public_health(ready=2 ** 15000), [])
        self.assertEqual(view["members"][0]["state_label"], "状态未知")
        json.dumps(view, allow_nan=False)

    def test_recent_failure_record_needs_review_and_is_not_idle(self):
        view = build_overview(make_config(), self.public_health(key="recent_failure"), [])
        self.assertIn("失败", view["members"][0]["state_label"])
        self.assertIn("失败", view["headline"])
        self.assertEqual(view["action_id"], "tasks")


if __name__ == "__main__":
    unittest.main()
