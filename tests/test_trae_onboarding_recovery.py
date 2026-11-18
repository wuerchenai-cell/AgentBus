# -*- coding: utf-8 -*-
"""TR-RESCUE 定向测试：onboarding_wizard / recovery_center 纯模型与动作合同。

来源：Trae，2026-10-08；任务 m-1791457584497-b68c42（作业 j-b7ed91331128d934d22268834e306bd4）。
纯函数测试：不创建 Tk 窗口、不读写信箱、不起模型、不访问网络。
运行：C:/Python314/python.exe -B -X utf8 authors/trae/tests/test_onboarding_recovery.py
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

from agentbus import onboarding_wizard, recovery_center  # noqa: E402


def make_request(**overrides):
    request = {
        "workspace": "I:/项目/示例",
        "member_id": "newbie",
        "display_name": "新成员",
        "coordinator": "codex",
        "task_id": "m-100-onboard",
        "inbox_path": ".agentbus/agents/newbie/ready/m-100-onboard.json",
        "input_path": "成果/成员接入/newbie/input.txt",
        "output_path": "成果/成员接入/newbie/output.txt",
    }
    request.update(overrides)
    return request


class RecoveryTableTests(unittest.TestCase):
    # (说明, state, 期望动作, 是否要求授权)
    CASES = (
        ("跨项目状态→阻塞", {"project_matches": False}, "blocked", False),
        ("STOP→阻塞", {"stop": True}, "blocked", False),
        ("成员停用→阻塞", {"member_enabled": False}, "blocked", False),
        ("入口缺失→阻塞", {"entry_missing": True}, "blocked", False),
        ("需要登录→阻塞", {"login_required": True}, "blocked", False),
        ("预算到期→阻塞", {"budget_expired": True}, "blocked", False),
        ("期限到期→阻塞", {"deadline_expired": True}, "blocked", False),
        ("恢复连续失败3次→阻塞", {"recovery_failed_count": 3}, "blocked", False),
        ("进程退出+授权桌面→桌面控制", {"process_exited": True, "computer_use_authorized": True},
         "computer_use", True),
        ("进程退出无授权→新聊天包", {"process_exited": True}, "new_chat_pack", False),
        ("对话无应答+有唤醒词→复制唤醒词", {"conversation_responsive": False, "wake_word": "请继续"},
         "wake_word", False),
        ("无应答+适配器已验收→适配器",
         {"conversation_responsive": False, "adapter_verified": True, "route": "adapter"},
         "adapter", False),
        ("无应答+无唤醒词+授权桌面→桌面控制",
         {"conversation_responsive": False, "computer_use_authorized": True}, "computer_use", True),
        ("无应答且无任何手段→新聊天包", {"conversation_responsive": False}, "new_chat_pack", False),
        ("已认领无心跳→继续原对话", {"task_claimed": True}, "continue", False),
        ("已认领有心跳→继续原对话", {"task_claimed": True, "heartbeat_verified": True}, "continue", False),
        ("迟到结果→继续原对话核验", {"late_result_present": True}, "continue", False),
        ("适配器路线已验收→适配器", {"adapter_verified": True, "route": "adapter"}, "adapter", False),
        ("有心跳→继续原对话", {"heartbeat_verified": True}, "continue", False),
        ("空状态→阻塞", {}, "blocked", False),
        ("畸形状态(None)→阻塞", None, "blocked", False),
        ("已认领+进程退出→新聊天包", {"task_claimed": True, "process_exited": True},
         "new_chat_pack", False),
        ("无应答+唤醒词+授权桌面→优先唤醒词",
         {"conversation_responsive": False, "wake_word": "请继续", "computer_use_authorized": True},
         "wake_word", False),
        ("跨项目优先于STOP→阻塞", {"project_matches": False, "stop": True}, "blocked", False),
        ("STOP优先于停用→阻塞", {"stop": True, "member_enabled": False}, "blocked", False),
        ("登录优先于认领→阻塞", {"login_required": True, "task_claimed": True}, "blocked", False),
    )

    def test_single_action_table(self):
        self.assertGreaterEqual(len(self.CASES), 22)
        for note, state, expected, needs_auth in self.CASES:
            with self.subTest(note=note):
                decision = recovery_center.decide_recovery(state)
                self.assertEqual(decision["action"], expected)
                self.assertIn(decision["action"], recovery_center.ACTIONS)
                self.assertTrue(decision["label"])
                self.assertTrue(decision["reason"])
                self.assertEqual(decision["requires_authorization"], needs_auth)
                # 不变量：任何动作都不得伪造执行。
                self.assertFalse(decision["can_fake_execution"])
                self.assertFalse(decision["desktop_window_proves_execution"])
                self.assertFalse(decision["adapter_proves_execution"])

    def test_execution_verified_only_from_heartbeat(self):
        for note, state, _expected, _auth in self.CASES:
            with self.subTest(note=note):
                decision = recovery_center.decide_recovery(state)
                expected_verified = isinstance(state, dict) and state.get("heartbeat_verified") is True
                self.assertEqual(decision["execution_verified"], expected_verified)

    def test_claim_is_not_execution(self):
        decision = recovery_center.decide_recovery({"task_claimed": True})
        self.assertFalse(decision["execution_verified"])
        self.assertEqual(decision["action"], "continue")

    def test_computer_use_requires_authorization_flag(self):
        decision = recovery_center.decide_recovery({"process_exited": True, "computer_use_authorized": True})
        self.assertEqual(decision["action"], "computer_use")
        self.assertTrue(decision["requires_authorization"])
        self.assertFalse(decision["execution_verified"])

    def test_new_chat_pack_is_public_only(self):
        decision = recovery_center.decide_recovery(
            {"process_exited": True, "member_id": "trae", "workspace": "I:/p", "task_id": "m-1"})
        self.assertEqual(decision["action"], "new_chat_pack")
        pack = decision["pack"]
        self.assertIsInstance(pack, dict)
        for key in ("inherits_permissions", "inherits_login", "inherits_processing_ownership"):
            self.assertFalse(pack[key])
        self.assertIn("I:/p", pack["copy_text"])
        self.assertIn("trae", pack["copy_text"])

    def test_wake_word_single_copy_text(self):
        decision = recovery_center.decide_recovery(
            {"conversation_responsive": False, "wake_word": "请继续本项目任务"})
        self.assertEqual(decision["action"], "wake_word")
        self.assertEqual(decision["wake_word"], "请继续本项目任务")
        self.assertIsNone(decision["pack"])

    def test_state_not_mutated(self):
        state = {"process_exited": True, "member_id": "trae"}
        snapshot = copy.deepcopy(state)
        recovery_center.decide_recovery(state)
        self.assertEqual(state, snapshot)

    def test_action_catalog_invariants(self):
        catalog = recovery_center.action_catalog()
        self.assertEqual(set(catalog["actions"]), set(recovery_center.ACTIONS))
        self.assertEqual(len(catalog["actions"]), 6)
        self.assertTrue(catalog["invariants"]["single_action"])
        self.assertFalse(catalog["invariants"]["can_fake_execution"])

    def test_recovery_fail_below_limit_not_blocked(self):
        decision = recovery_center.decide_recovery({"recovery_failed_count": 2, "heartbeat_verified": True})
        self.assertEqual(decision["action"], "continue")


class OnboardingWizardTests(unittest.TestCase):
    def test_build_basic_shape(self):
        wizard = onboarding_wizard.build_wizard(make_request(now_ts=1000.0))
        self.assertTrue(wizard["ok"])
        self.assertEqual(wizard["route"], onboarding_wizard.ROUTE_FILE)
        self.assertEqual(wizard["member_id"], "newbie")
        self.assertFalse(wizard["readonly"])
        self.assertFalse(wizard["execution_verified"])
        self.assertEqual(wizard["model_calls"], 0)
        self.assertIn("leader", wizard["instructions"])
        self.assertIn("newcomer", wizard["instructions"])

    def test_missing_required_key_raises(self):
        for key in onboarding_wizard.REQUIRED_KEYS:
            with self.subTest(key=key):
                request = make_request()
                del request[key]
                with self.assertRaises(ValueError):
                    onboarding_wizard.build_wizard(request)

    def test_blank_required_value_raises(self):
        with self.assertRaises(ValueError):
            onboarding_wizard.build_wizard(make_request(member_id="   "))

    def test_invalid_member_id_raises(self):
        for bad in ("Newbie", "1abc", "a b", "con", "human", "-x", "x" * 65):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    onboarding_wizard.validate_member_id(bad)

    def test_invalid_coordinator_raises(self):
        with self.assertRaises(ValueError):
            onboarding_wizard.build_wizard(make_request(coordinator="director"))

    def test_invalid_route_raises(self):
        with self.assertRaises(ValueError):
            onboarding_wizard.build_wizard(make_request(route="cli"))

    def test_brand_neutral(self):
        wizard = onboarding_wizard.build_wizard(make_request())
        blob = " ".join(str(wizard["instructions"].values())) + str(wizard)
        lowered = blob.lower()
        for brand in ("codex", "workbuddy", "trae", "opencode", "zcode", "antigravity"):
            # coordinator 值由调用方给出，不算品牌硬编码；这里只检查模板文本不含其它品牌名。
            if brand == wizard["coordinator"].lower():
                continue
            self.assertNotIn(brand, lowered)

    def test_default_adapter_not_usable(self):
        wizard = onboarding_wizard.build_wizard(make_request())
        self.assertFalse(wizard["adapter"]["available"])
        self.assertFalse(wizard["adapter"]["usable_for_dispatch"])

    def test_unverified_adapter_not_usable(self):
        wizard = onboarding_wizard.build_wizard(make_request(adapter={"kind": "acp"}))
        self.assertTrue(wizard["adapter"]["available"])
        self.assertFalse(wizard["adapter"]["verified"])
        self.assertFalse(wizard["adapter"]["usable_for_dispatch"])

    def test_verified_adapter_usable(self):
        wizard = onboarding_wizard.build_wizard(
            make_request(route="adapter", adapter={"kind": "acp", "verified": True}))
        self.assertEqual(wizard["route"], onboarding_wizard.ROUTE_ADAPTER)
        self.assertTrue(wizard["adapter"]["usable_for_dispatch"])

    def test_readonly_sample(self):
        wizard = onboarding_wizard.build_wizard(make_request(writable=False))
        self.assertTrue(wizard["readonly"])
        self.assertEqual(wizard["sample"]["capability_family"], "readonly-review")
        self.assertEqual(wizard["sample"]["expected_artifacts"], [])
        self.assertEqual(tuple(wizard["acceptance"]), onboarding_wizard.READONLY_ACCEPTANCE)

    def test_writable_sample(self):
        wizard = onboarding_wizard.build_wizard(make_request(now_ts=1000.0))
        self.assertEqual(wizard["sample"]["capability_family"], "file-write")
        self.assertEqual(wizard["sample"]["expected_artifacts"],
                         ["成果/成员接入/newbie/output.txt"])
        self.assertEqual(wizard["sample"]["budget_s"], onboarding_wizard.SAMPLE_BUDGET_S)
        self.assertEqual(wizard["sample"]["deadline_ts"], 1000.0 + onboarding_wizard.SAMPLE_BUDGET_S)

    def test_sample_is_bounded(self):
        wizard = onboarding_wizard.build_wizard(make_request(now_ts=0.0, budget_s=60))
        self.assertEqual(wizard["sample"]["budget_s"], 60)
        self.assertEqual(wizard["sample"]["deadline_ts"], 60.0)

    def test_steps_all_present(self):
        wizard = onboarding_wizard.build_wizard(make_request())
        ids = [step["id"] for step in wizard["steps"]]
        self.assertEqual(ids, [sid for sid, _l, _d in onboarding_wizard.WIZARD_STEPS])
        self.assertEqual(len(ids), 5)
        for step in wizard["steps"]:
            self.assertTrue(step["label"])
            self.assertIsInstance(step["done"], bool)

    def test_steps_reflect_saved_flag(self):
        wizard = onboarding_wizard.build_wizard(make_request(saved=True))
        by_id = {step["id"]: step for step in wizard["steps"]}
        self.assertTrue(by_id["save"]["done"])
        self.assertTrue(by_id["sample"]["done"])
        self.assertTrue(by_id["instructions"]["done"])

    def test_instructions_contain_paths_and_task(self):
        wizard = onboarding_wizard.build_wizard(make_request())
        for text in wizard["instructions"].values():
            self.assertIn("m-100-onboard", text)
            self.assertIn("成果/成员接入/newbie/input.txt", text)
            self.assertIn("成果/成员接入/newbie/output.txt", text)
            self.assertIn("codex", text)

    def test_deterministic(self):
        first = onboarding_wizard.build_wizard(make_request(now_ts=1234.0))
        second = onboarding_wizard.build_wizard(make_request(now_ts=1234.0))
        self.assertEqual(first, second)

    def test_request_not_mutated(self):
        request = make_request()
        snapshot = copy.deepcopy(request)
        onboarding_wizard.build_wizard(request)
        self.assertEqual(request, snapshot)

    def test_returned_acceptance_is_copy(self):
        wizard = onboarding_wizard.build_wizard(make_request())
        wizard["acceptance"].append("篡改")
        self.assertNotIn("篡改", onboarding_wizard.DEFAULT_ACCEPTANCE)
        self.assertEqual(tuple(wizard["acceptance"][:len(onboarding_wizard.DEFAULT_ACCEPTANCE)]),
                         onboarding_wizard.DEFAULT_ACCEPTANCE)

    def test_bad_request_type_raises(self):
        with self.assertRaises(ValueError):
            onboarding_wizard.build_wizard("not a dict")


if __name__ == "__main__":
    unittest.main(verbosity=2)