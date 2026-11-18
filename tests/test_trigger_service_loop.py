# -*- coding: utf-8 -*-
"""test_trigger_service_loop.py: 测试后台触发服务的任务信箱自动扫描、防重与派发循环。"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agentbus.trigger.contacts import Contact, confirm_contact, create_invite
from agentbus.trigger.contracts import TargetObservation
from agentbus.trigger.journal import DeliveryJournal, DeliveryStatus
from agentbus.trigger.service import TriggerDeliveryService, run_project_service


class TestTriggerServiceLoop(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentbus_test_loop_")
        self.workspace = Path(self.test_dir)
        (self.workspace / ".agentbus").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_bound_member(self, member_id: str = "worker1") -> Contact:
        invite = create_invite(
            self.workspace,
            member_id=member_id,
            connector_id="agentbus.codex-bridge",
            installation_key="inst_test_001",
        )
        obs = TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id="op_test_001",
            project_key=str(self.workspace),
            connector_id="agentbus.codex-bridge",
            connector_version="1.0.0",
            installation_key="inst_test_001",
            instance_id="inst_run_1",
            observed_at="2026-10-30T12:00:00Z",
            status="ready",
            native_conversation_id="conv_12345",
            identity_source="host_query",
            exists=True,
        )
        contact = confirm_contact(
            self.workspace,
            invite.invite_id,
            obs,
        )
        return contact

    def test_scan_and_deliver_no_contacts(self):
        service = TriggerDeliveryService(self.workspace)
        res = service.scan_and_deliver_pending()
        self.assertEqual(res["status"], "no_bound_contacts")
        self.assertEqual(res["delivered"], 0)

    def test_scan_and_deliver_stopped_policy(self):
        self._setup_bound_member("worker1")
        service = TriggerDeliveryService(self.workspace)
        service.set_stopped(True)
        res = service.scan_and_deliver_pending()
        self.assertEqual(res["status"], "stopped")
        self.assertEqual(res["delivered"], 0)

    def test_scan_and_deliver_ready_task(self):
        contact = self._setup_bound_member("worker1")

        # 写入 ready 任务卡
        ready_dir = self.workspace / ".agentbus" / "agents" / "worker1" / "ready"
        ready_dir.mkdir(parents=True, exist_ok=True)
        task_data = {
            "task_id": "task_abc_001",
            "title": "测试自动化构建",
            "goal": "执行单元测试并报告",
        }
        (ready_dir / "task_abc_001.json").write_text(json.dumps(task_data), encoding="utf-8")

        service = TriggerDeliveryService(self.workspace)
        res = service.scan_and_deliver_pending()

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["scanned"], 1)
        self.assertEqual(res["delivered"], 0)
        self.assertEqual(res["notified"], 1)

        # 验证 journal 状态已记录为 submitted
        journal = DeliveryJournal(self.workspace)
        submitted_entries = journal.list_by_status(DeliveryStatus.SUBMITTED.value)
        self.assertEqual(len(submitted_entries), 1)

        # 再次执行扫描，幂等防重检不应重复投递
        res2 = service.scan_and_deliver_pending()
        self.assertEqual(res2["status"], "ok")
        self.assertEqual(res2["delivered"], 0)
        self.assertEqual(len(journal.list_by_status(DeliveryStatus.SUBMITTED.value)), 1)

    def test_run_project_service_with_timeout(self):
        self._setup_bound_member("worker1")
        # 运行不超过 0.5 秒后安全返回
        run_project_service(self.workspace, seconds=0.5, once=False, poll_interval=0.1)
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
