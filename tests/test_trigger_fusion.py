# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus, Message
from agentbus.config import Config
from agentbus.web_bridge import WebBridge
from agentbus.trigger.connectors.acp import ACPBridgeConnector
from agentbus.trigger.connectors.codearts import CodeArtsHookConnector
from agentbus.trigger.connectors.universal import UniversalWakeupConnector
from agentbus.trigger.contacts import checkin_contact, load_contact
from agentbus.trigger.contracts import Contact, WakeRequest
from agentbus.trigger.onboarding import start_flow
from agentbus.trigger.service import ProjectTriggerPolicy, TriggerDeliveryService


class TestTriggerFusion(unittest.TestCase):
    def setUp(self):
        os.environ["AGENTBUS_DISABLE_DESKTOP_POPUP"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        # 初始化工作区
        (self.ws / ".agentbus" / "agents" / "worker1" / "ready").mkdir(parents=True)
        (self.ws / ".agentbus" / "agents" / "worker1" / "processing").mkdir(parents=True)
        (self.ws / ".agentbus" / "agents" / "worker1" / "done").mkdir(parents=True)
        (self.ws / ".agentbus" / "agents" / "worker1" / "tmp").mkdir(parents=True)
        (self.ws / ".agentbus" / "agents" / "coord-worker" / "ready").mkdir(parents=True)
        (self.ws / ".agentbus" / "triggers" / "wake-signals").mkdir(parents=True)
        cfg = {
            "coordinator": "coord-worker",
            "workers": {
                "worker1": {
                    "display_name": "Worker 1",
                    "enabled": True,
                    "executor_enabled": True,
                    "communication_mode": "file",
                },
                "coord-worker": {
                    "display_name": "Coordinator Worker",
                    "enabled": True,
                    "executor_enabled": True,
                    "communication_mode": "file",
                }
            },
        }
        (self.ws / ".agentbus" / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("AGENTBUS_DISABLE_DESKTOP_POPUP", None)

    def test_universal_connector_submits_wake_signal(self):
        connector = UniversalWakeupConnector()
        desc = connector.describe()
        self.assertEqual(desc.connector_id, "agentbus.universal-wakeup")

        # 报到获得联系卡
        checkin_contact(self.ws, "worker1", session_id="test_sess_univ")
        contact = load_contact(self.ws, "worker1")
        self.assertIsNotNone(contact)

        req = WakeRequest(
            request_id="req_test_01",
            project_key=str(self.ws),
            member_id="worker1",
            binding_id=contact.binding_id,
            binding_revision=contact.revision,
            prompt_summary="请处理任务 01",
        )

        sub_res = connector.submit(self.ws, contact, req)
        self.assertEqual(sub_res.status, "queued")
        self.assertFalse(sub_res.accepted_by_host)

        # 核实 wake-signals 文件生成
        sig_file = self.ws / ".agentbus" / "triggers" / "wake-signals" / "worker1.json"
        self.assertTrue(sig_file.is_file())
        sig_data = json.loads(sig_file.read_text(encoding="utf-8"))
        self.assertEqual(sig_data["member_id"], "worker1")
        self.assertEqual(sig_data["prompt_summary"], "请处理任务 01")

        # 核实 observe 观察
        events = connector.observe(self.ws, contact)
        self.assertEqual(events, [])

        # 模拟信号被消费
        sig_file.unlink()
        events_consumed = connector.observe(self.ws, contact)
        self.assertEqual(events_consumed, [])

    def test_codearts_install_hook_copies_real_asset(self):
        """验证 CodeArtsHookConnector.install_hook 真实复制完整 agentbus_hook.js 而非 26 字节空注释。"""
        connector = CodeArtsHookConnector()
        res = connector.install_hook(self.ws)
        self.assertTrue(res.get("installed"))
        self.assertGreater(res.get("bytes", 0), 4000, "Installed hook file should be the full JS script (>4000 bytes), not the 26-byte fallback")
        hook_text = (self.ws / ".codearts" / "hooks" / "agentbus.js").read_text(encoding="utf-8")
        self.assertIn("AgentBus Official Hook for Huawei CodeArts Agent", hook_text)

    def test_acp_bridge_connector_and_checkin_auto_inference(self):
        """验证 ACP 协议成员自动推断为 agentbus.acp-bridge 并支持唤醒投递。"""
        (self.ws / ".agentbus" / "agents" / "opencode-dev" / "ready").mkdir(parents=True)
        checkin_res = checkin_contact(self.ws, "opencode-dev", session_id="sess_opencode_101")
        self.assertEqual(checkin_res["connector_id"], "agentbus.acp-bridge")

        contact = load_contact(self.ws, "opencode-dev")
        self.assertIsNotNone(contact)
        self.assertEqual(contact.connector_id, "agentbus.acp-bridge")

        connector = ACPBridgeConnector()
        req = WakeRequest(
            request_id="req_acp_01",
            project_key=str(self.ws),
            member_id="opencode-dev",
            binding_id=contact.binding_id,
            binding_revision=contact.revision,
            prompt_summary="请通过 ACP 处理任务",
        )
        sub_res = connector.submit(self.ws, contact, req)
        self.assertEqual(sub_res.status, "queued")
        self.assertFalse(sub_res.accepted_by_host)
        sig_file = self.ws / ".agentbus" / "triggers" / "wake-signals" / "opencode-dev.json"
        self.assertTrue(sig_file.is_file())
        sig_data = json.loads(sig_file.read_text(encoding="utf-8"))
        self.assertEqual(sig_data.get("protocol"), "acp")

    def test_busy_fifo_queueing_and_auto_drain(self):
        """验证成员会话繁忙时新唤醒请求进入 FIFO 队列，恢复空闲后自动按序投递。"""
        service = TriggerDeliveryService(self.ws)
        checkin_contact(self.ws, "worker1", session_id="test_sess_busy")

        for idx in (1, 2):
            tid = f"m-task-q-0{idx}"
            msg = Message(
                id=tid,
                ts=f"2026-10-30T10:0{idx}:00+00:00",
                sender="coord-worker",
                to="worker1",
                type="task",
                title=f"排队任务 {idx}",
                body=f"排队内容 {idx}",
            )
            p = self.ws / ".agentbus" / "agents" / "worker1" / "ready" / f"{tid}.json"
            p.write_text(json.dumps(msg.to_dict(), ensure_ascii=False), encoding="utf-8")

        # 标记 worker1 正在忙
        service.set_member_busy("worker1", True, reason="executing_turn")
        res1 = service.trigger_for_member("worker1", "m-task-q-01", count_hop=False)
        res2 = service.trigger_for_member("worker1", "m-task-q-02", count_hop=False)

        self.assertEqual(res1["status"], "queued")
        self.assertEqual(res1["queue_position"], 1)
        self.assertEqual(res2["status"], "queued")
        self.assertEqual(res2["queue_position"], 2)

        # 繁忙状态下 scan_and_deliver_pending 仍保持排队不投递
        scan_busy = service.scan_and_deliver_pending()
        self.assertEqual(scan_busy["delivered"], 0)
        self.assertEqual(scan_busy["queued"], 2)

        # 解除繁忙状态后，自动按 FIFO 顺序出队投递
        service.set_member_busy("worker1", False)
        scan_idle = service.scan_and_deliver_pending()
        self.assertEqual(scan_idle["delivered"], 0)
        self.assertEqual(scan_idle["notified"], 1)
        self.assertEqual(scan_idle["queued"], 1)
        signal = self.ws / '.agentbus/triggers/wake-signals/worker1.json'
        first = json.loads(signal.read_text(encoding='utf-8'))
        self.assertEqual(first['source_message_ids'], ['m-task-q-01'])
        from agentbus.trigger.connectors._signals import consume_signal
        from agentbus.bus import Bus
        from agentbus.claim_fencing import captured_ticket
        consume_signal(self.ws, 'worker1')
        service.on_work_state_changed('worker1')
        self.assertFalse(signal.exists(), 'consumption alone is not an idle event')
        bus = Bus(self.ws / '.agentbus')
        claimed = bus.claim('worker1', bus.dirs('worker1')['ready'] / 'm-task-q-01.json')
        ticket = captured_ticket(claimed)
        bus.heartbeat('worker1', claimed, ticket, note='actual fixture work')
        # This legacy fixture predates mandatory result_to. Keep the original
        # explicit parent task and declare the receipt recipient for delivery.
        bus.commit_result('worker1', claimed, ticket, body='Actually completed fixture work', result_to='coord-worker')
        second = json.loads(signal.read_text(encoding='utf-8'))
        self.assertEqual(second['source_message_ids'], ['m-task-q-02'])
        self.assertEqual(len(service.list_member_queued_requests('worker1')), 0)

    def test_checkin_briefing_includes_compaction_summary(self):
        """验证 checkin_contact 简报自动汇总历史已完成任务（Auto-Compaction Handoff）。"""
        done_msg = {
            "id": "m-done-001",
            "title": "接口契约设计",
            "body": "已完成全部 REST 与 JSON-RPC 字段定义并通过校验。",
        }
        done_path = self.ws / ".agentbus" / "agents" / "worker1" / "done" / "m-done-001.json"
        done_path.write_text(json.dumps(done_msg, ensure_ascii=False), encoding="utf-8")

        res = checkin_contact(self.ws, "worker1", session_id="sess_rebind_compaction")
        briefing = res["briefing"]
        self.assertEqual(briefing["recent_done_count"], 1)
        self.assertEqual(briefing["recent_done_tasks"][0]["id"], "m-done-001")
        self.assertIn("Context Compaction Handoff", briefing["context_compaction_summary"])
        self.assertIn("m-done-001", briefing["context_compaction_summary"])

    def test_trigger_for_member_and_circuit_breaker(self):
        service = TriggerDeliveryService(self.ws)
        checkin_contact(self.ws, "worker1", session_id="test_sess_flow")

        # 投放一个 ready 任务
        task_msg = Message(
            id="m-task-cb-01",
            ts="2026-10-30T10:00:00+00:00",
            sender="coord-worker",
            to="worker1",
            type="task",
            title="测试任务",
            body="请完成测试",
        )
        task_path = self.ws / ".agentbus" / "agents" / "worker1" / "ready" / "m-task-cb-01.json"
        task_path.write_text(json.dumps(task_msg.to_dict(), ensure_ascii=False), encoding="utf-8")

        # 触发投递
        res = service.trigger_for_member("worker1", "m-task-cb-01")
        self.assertEqual(res["status"], "ok")

        # 连续投递测试熔断器 (max_auto_hops = 6)
        # 前面已有 1 次跳数，再投 4 次
        for _ in range(4):
            service.trigger_for_member("worker1", "m-task-cb-01")

        # Idempotent duplicate notifications do not consume a new automatic hop.
        self.assertEqual(service.load_policy().current_hop_count, 1)
        self.assertEqual(service.trigger_for_member('worker1','m-task-cb-01')['status'], 'duplicate')
        # Only new send attempts advance the circuit breaker.
        for _ in range(5): service.record_hop()
        self.assertFalse(service.load_policy().delivery_enabled)

        # 再次触发仍被熔断拦截
        res_blocked = service.trigger_for_member("worker1", "m-task-cb-01")
        self.assertEqual(res_blocked["status"], "stopped")

        # 重置熔断器
        service.reset_circuit_breaker()
        policy = service.load_policy()
        self.assertTrue(policy.delivery_enabled)
        self.assertEqual(policy.current_hop_count, 0)

    def test_onboarding_wizard_auto_onboards_missing_worker(self):
        # 初始 config 中没有 worker-new
        flow = start_flow(self.ws, "worker-new")
        self.assertEqual(flow.step, "S0")
        self.assertEqual(flow.member_id, "worker-new")

        # 验证 worker-new 已被自动纳入 config.json
        cfg_data = json.loads((self.ws / ".agentbus" / "config.json").read_text(encoding="utf-8"))
        self.assertIn("worker-new", cfg_data.get("workers", {}))
        # 验证信箱已就绪
        self.assertTrue((self.ws / ".agentbus" / "agents" / "worker-new" / "ready").is_dir())

    def test_wizard_s0_to_s5_with_self_checkin_no_cas_conflict(self):
        """端到端验证向导流程：S0 不伪造联系卡，S4 配合自主报到不触发 CAS 冲突，成功进入 S5。"""
        bridge = WebBridge(self.ws)
        # 1. 启动向导
        res0 = bridge.call("trigger_onboarding_start", {"member_id": "cursor-new"})
        self.assertTrue(res0.get("ok"))
        flow0 = res0["data"]
        self.assertEqual(flow0["step"], "S0")

        # 2. 断言此时绝不能提前被伪造为 bound 联系卡！
        contact_s0 = load_contact(self.ws, "cursor-new")
        self.assertIsNone(contact_s0)

        # 3. 推进 S0 -> S1
        res1 = bridge.call("trigger_onboarding_advance", {
            "flow_id": flow0["flow_id"],
            "expected_revision": flow0["revision"],
            "action": "confirm_identity",
            "payload": {}
        })
        self.assertTrue(res1.get("ok"))
        flow1 = res1["data"]
        self.assertEqual(flow1["step"], "S1")

        # 4. 推进 S1 -> S2 (选择 other 客户端)
        res2 = bridge.call("trigger_onboarding_advance", {
            "flow_id": flow1["flow_id"],
            "expected_revision": flow1["revision"],
            "action": "select_software",
            "payload": {"product_id": "other", "installation_key": "inst_other_01"}
        })
        self.assertTrue(res2.get("ok"))
        flow2 = res2["data"]
        self.assertEqual(flow2["step"], "S2")

        # 5. 推进 S2 -> S3 (绑定通用连接器 agentbus.universal-wakeup)
        res3 = bridge.call("trigger_onboarding_advance", {
            "flow_id": flow2["flow_id"],
            "expected_revision": flow2["revision"],
            "action": "confirm_scope",
            "payload": {"connector_id": "agentbus.universal-wakeup"}
        })
        self.assertTrue(res3.get("ok"))
        flow3 = res3["data"]
        self.assertEqual(flow3["step"], "S3")

        # 6. 推进 S3 -> S4 (部署邀请)
        res4 = bridge.call("trigger_onboarding_advance", {
            "flow_id": flow3["flow_id"],
            "expected_revision": flow3["revision"],
            "action": "apply_install",
            "payload": {}
        })
        self.assertTrue(res4.get("ok"))
        flow4 = res4["data"]
        self.assertEqual(flow4["step"], "S4")
        self.assertTrue(bool(flow4["invite_id"]))

        # 7. 模拟 Agent 在窗口中自主执行 checkin_contact 报到
        checkin_res = checkin_contact(self.ws, "cursor-new", session_id="cursor_win_99", connector_id="agentbus.universal-wakeup", installation_key="inst_other_01")
        self.assertEqual(checkin_res["status"], "ok")
        self.assertEqual(checkin_res["session_id"], "cursor_win_99")

        # 8. 前端通过 contact_show 探测报到状态
        show_res = bridge.call("contact_show", {"agent": "cursor-new"})
        self.assertTrue(show_res.get("ok"))
        contact_info = show_res["data"]
        self.assertEqual(contact_info["status"], "bound")
        self.assertEqual(contact_info["native_conversation_id"], "cursor_win_99")

        # 9. 推进 S4 -> S5 (bind_chat 携带 self_checkin)
        res5 = bridge.call("trigger_onboarding_advance", {
            "flow_id": flow4["flow_id"],
            "expected_revision": flow4["revision"],
            "action": "bind_chat",
            "payload": {"native_conversation_id": "cursor_win_99", "identity_source": "self_checkin"}
        })
        self.assertTrue(res5.get("ok"), f"Advancing to S5 failed: {res5}")
        flow5 = res5["data"]
        self.assertEqual(flow5["step"], "S5")

    def test_circuit_breaker_resumes_on_set_stopped_false_and_claim(self):
        """验证熔断器被连续触发后，通过 set_stopped(False) 或 bus.claim 均可完全恢复。"""
        service = TriggerDeliveryService(self.ws)
        checkin_contact(self.ws, "worker1", session_id="test_sess_cb")

        # 连续触发 6 次引发熔断
        for _ in range(6):
            service.record_hop()
        policy = service.load_policy()
        self.assertFalse(policy.delivery_enabled)
        self.assertGreaterEqual(policy.current_hop_count, 6)

        # 途径 1：调用 set_stopped(False) 恢复
        service.set_stopped(False)
        policy_restored = service.load_policy()
        self.assertTrue(policy_restored.delivery_enabled)
        self.assertEqual(policy_restored.current_hop_count, 0)

        # 再次触发 6 次引发熔断
        for _ in range(6):
            service.record_hop()
        self.assertFalse(service.load_policy().delivery_enabled)

        # 途径 2：工位成功认领任务产生实质推进，自动复位熔断器
        bus = Bus(self.ws / ".agentbus")
        bus.init()
        # 准备一个任务
        msg = bus.make(sender="coord-worker", to="worker1", type="task", title="新任务", body="开工", result_to="coord-worker")
        bus.post(msg)
        ready_path = bus.dirs("worker1")["ready"] / f"{msg.id}.json"

        # 执行认领
        claimed = bus.claim("worker1", ready_path)
        self.assertIsNotNone(claimed)

        # 验证熔断器已被自动复位！
        policy_claimed = service.load_policy()
        self.assertTrue(policy_claimed.delivery_enabled)
        self.assertEqual(policy_claimed.current_hop_count, 0)


if __name__ == "__main__":
    unittest.main()
