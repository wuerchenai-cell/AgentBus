# -*- coding: utf-8 -*-
import json
import datetime
import os
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Message
from agentbus.trigger.contacts import checkin_contact, load_contact
from agentbus.trigger.contracts import Contact


class TestTriggerCheckin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        # 初始化基础目录
        (self.ws / ".agentbus" / "agents" / "worker1" / "ready").mkdir(parents=True)
        (self.ws / ".agentbus" / "agents" / "worker1" / "processing").mkdir(parents=True)
        (self.ws / ".agentbus" / "agents" / "director" / "ready").mkdir(parents=True)
        cfg = {
            "coordinator": "director",
            "workers": {"worker1": {"display_name": "Worker 1"}},
        }
        (self.ws / ".agentbus" / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_checkin_auto_creates_contact_and_notifies_director(self):
        res = checkin_contact(self.ws, "worker1", session_id="test_sess_01")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["member_id"], "worker1")
        self.assertEqual(res["session_id"], "test_sess_01")
        self.assertEqual(res["revision"], 1)
        self.assertTrue(res["rebound"])
        self.assertTrue(res["notified_coordinator"])

        # 验证联系卡落盘
        contact = load_contact(self.ws, "worker1")
        self.assertIsNotNone(contact)
        self.assertEqual(contact.native_conversation_id, "test_sess_01")
        self.assertEqual(contact.revision, 1)
        self.assertEqual(contact.status, "bound")

        # 验证组长信箱收到了合法的 Message (strict=True 校验)
        director_ready = self.ws / ".agentbus" / "agents" / "director" / "ready"
        msgs = list(director_ready.glob("*.json"))
        self.assertEqual(len(msgs), 1)
        msg_data = json.loads(msgs[0].read_text(encoding="utf-8"))
        msg = Message.from_dict(msg_data, strict=True)
        self.assertEqual(msg.type, "info")
        self.assertEqual(msg.sender, "worker1")
        self.assertEqual(msg.to, "director")
        self.assertIn("已绑定当前对话并可接活", msg.title)

    def test_idempotent_checkin_preserves_revision(self):
        res1 = checkin_contact(self.ws, "worker1", session_id="test_sess_01")
        self.assertEqual(res1["revision"], 1)
        self.assertTrue(res1["rebound"])

        # 再次执行相同报到
        res2 = checkin_contact(self.ws, "worker1", session_id="test_sess_01")
        self.assertEqual(res2["revision"], 1)
        self.assertFalse(res2["rebound"])

    def test_session_handover_rebinds_with_cas_increment_and_archives_history(self):
        res1 = checkin_contact(self.ws, "worker1", session_id="test_sess_01")
        bnd1 = res1["binding_id"]

        # 换到新窗口
        res2 = checkin_contact(self.ws, "worker1", session_id="test_sess_02")
        self.assertEqual(res2["revision"], 2)
        self.assertTrue(res2["rebound"])
        self.assertEqual(res2["session_id"], "test_sess_02")

        # 验证旧快照归档
        hist_file = self.ws / ".agentbus" / "agents" / "worker1" / "contact-history" / f"{bnd1}-1.json"
        self.assertTrue(hist_file.is_file())
        old_data = json.loads(hist_file.read_text(encoding="utf-8"))
        self.assertEqual(old_data["native_conversation_id"], "test_sess_01")
        self.assertEqual(old_data["revision"], 1)

    def test_auto_detect_session_from_env(self):
        os.environ["AGENTBUS_SESSION_ID"] = "env_sess_999"
        try:
            res = checkin_contact(self.ws, "worker1")
            self.assertEqual(res["session_id"], "env_sess_999")
        finally:
            del os.environ["AGENTBUS_SESSION_ID"]

    def test_auto_detect_session_from_ready_evidence(self):
        evidence_dir = self.ws / ".agentbus" / "triggers" / "bridge" / "codearts" / "evidence"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "READY.json").write_text(
            json.dumps({"last_active_session_id": "codearts_live_window", "workspace":str(self.ws),
                "at":datetime.datetime.now(datetime.timezone.utc).isoformat(), "client_get":True, "client_prompt":True}),
            encoding="utf-8"
        )
        res = checkin_contact(self.ws, "worker1", connector_id="agentbus.codearts-hook")
        self.assertEqual(res["session_id"], "codearts_live_window")

    def test_briefing_reports_processing_and_ready(self):
        # 放置一个在途任务和一个待办任务
        p_file = self.ws / ".agentbus" / "agents" / "worker1" / "processing" / "m-proc-01.json"
        p_file.write_text(json.dumps({"id": "m-proc-01", "title": "任务处理中", "claim_id": "c-1"}), encoding="utf-8")
        r_file = self.ws / ".agentbus" / "agents" / "worker1" / "ready" / "m-ready-01.json"
        r_file.write_text(json.dumps({"id": "m-ready-01", "title": "任务待处理", "type": "task"}), encoding="utf-8")

        res = checkin_contact(self.ws, "worker1", session_id="test_sess_bf")
        bf = res["briefing"]
        self.assertEqual(bf["processing_count"], 1)
        self.assertEqual(bf["ready_count"], 1)
        self.assertEqual(bf["processing_tasks"][0]["id"], "m-proc-01")
        self.assertEqual(bf["ready_tasks"][0]["id"], "m-ready-01")


if __name__ == "__main__":
    unittest.main()
