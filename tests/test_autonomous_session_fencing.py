#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_autonomous_session_fencing.py: 自动执行保留精确会话票据与心跳测试。

来源：§13.2 CX13Q-SESSION-OWNERSHIP（P1）。
验收标准：
1. HeartbeatKeeper 持有精确票据并在更新心跳前校验；
2. 旧 keeper 遇到后继 epoch（或 claim_id 变更）时立即停止，绝不将后继代次由 stale 刷成 fresh；
3. 新 claim 产生真实执行心跳；
4. 不凭 owner 同名盲目复用其他窗口票据。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

# 导入待测集成模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
import sys
sys.path.insert(0, str(PROJECT_DIR))

from agentbus.worker_loop import HeartbeatKeeper  # noqa: E402
from agentbus.heartbeat import heartbeat_path  # noqa: E402
from agentbus.bus import Bus
from agentbus.config import load
from agentbus.claim_fencing import captured_ticket
from agentbus.operation_lock import task_lock


class TestAutonomousSessionFencing(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bus_dir = self.root / ".agentbus"
        self.bus_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_heartbeat_keeper_stops_on_epoch_advance(self):
        """测试当心跳文件中的 epoch 递增后，旧 keeper 自动停止，不覆盖新代次。"""
        load(self.root, create=True)
        bus = Bus(self.bus_dir)
        bus.init()
        msg = bus.make("codex", "codex", "task", body="Harmless fencing fixture", result_to="codex")
        bus.post(msg)
        msg = bus.claim("codex", bus.dirs("codex")["ready"] / (msg.id + ".json"))
        ticket = captured_ticket(msg)
        bus.heartbeat("codex", msg, ticket)
        hb_file = heartbeat_path(self.bus_dir, msg.id, "codex")
        keeper = HeartbeatKeeper(self.bus_dir, msg.id, "codex", ticket=ticket.to_dict(), interval_s=.02)
        keeper.start()
        try:
            limit = time.monotonic() + 1
            while time.monotonic() < limit:
                with task_lock(self.bus_dir, "codex", msg.id):
                    note = json.loads(hb_file.read_text(encoding="utf-8"))["note"]
                if note == "worker_loop_active":
                    break
                time.sleep(.01)
            else:
                self.fail("The genuine original keeper never refreshed its heartbeat")
            bus.recover_claim("codex", msg.id, owner_confirmed_inactive=True)
            successor = bus.claim("codex", bus.dirs("codex")["ready"] / (msg.id + ".json"))
            new_ticket = captured_ticket(successor)
            bus.heartbeat("codex", successor, new_ticket, note="successor")
            before = hb_file.read_bytes()
            time.sleep(.08)
            self.assertEqual(hb_file.read_bytes(), before)
            data = json.loads(before)
            self.assertEqual(data["claim_epoch"], 2)
            self.assertEqual(data["claim_id"], new_ticket.claim_id)
        finally:
            keeper.stop()
        self.assertFalse(keeper._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
