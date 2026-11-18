#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对 §3.8 AUDIT-HEARTBEAT-ENFORCEMENT 心跳刚性硬门禁与原子注入的定向单测。"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import shutil
import tempfile
import unittest
from pathlib import Path

from agentbus import bus as _bus
from agentbus import heartbeat as _heartbeat
from agentbus import claim_fencing as _fencing

ASSETS = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets"


class TestHeartbeatEnforcement(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="agentbus_test_hb_enforce_")
        self.workspace = Path(self.test_dir)
        self.bus = _bus.Bus(self.workspace)
        # 初始化目录结构
        self.bus.dirs("antigravity")
        self.bus.dirs("codex")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_explicit_heartbeat_activation_and_fencing(self):
        """测试认领后调用 heartbeat 显式激活活跃会话心跳，并绑定有效票据代次。"""
        # 1. 投递一条测试任务到 ready/
        task = self.bus.make("codex", "antigravity", "task", "测试任务", "测试目标",
                             result_to="codex", expected_artifacts=[])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        self.assertTrue(ready_file.exists())

        # 2. antigravity 认领任务，初态为 claimed
        claimed_msg = self.bus.claim("antigravity", ready_file)
        self.assertIsNotNone(claimed_msg)
        self.assertEqual(claimed_msg.status, "claimed")
        ticket = _fencing.captured_ticket(claimed_msg.to_dict())

        # 3. 显式录入健康心跳
        hb_res = self.bus.heartbeat("antigravity", claimed_msg, ticket, note="初始活跃心跳")
        self.assertTrue(hb_res)

        # 4. 验证首笔显式心跳已写入 heartbeats/ 目录
        hb_path = _heartbeat.heartbeat_path(self.workspace, claimed_msg.id, "antigravity")
        self.assertTrue(hb_path.exists(), "录入心跳后心跳文件必须存在")

        # 5. 验证心跳记录状态与票据一致
        state = _heartbeat.state(self.workspace, claimed_msg.id, "antigravity",
                                 task_record=claimed_msg.to_dict())
        self.assertEqual(state["source"], _heartbeat.SOURCE_RECORD)
        self.assertFalse(state["stale"], "新录入的心跳不能为 stale")
        self.assertEqual(state["epoch"], claimed_msg.claim_epoch)

    def test_commit_result_with_stale_heartbeat_is_blocked(self):
        """测试若执行会话心跳已超时（stale=True），工作流管道予以硬拦截熔断。"""
        task = self.bus.make("codex", "antigravity", "task", "测试任务2", "测试目标2",
                             result_to="codex", expected_artifacts=[])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        claimed_msg = self.bus.claim("antigravity", ready_file)
        ticket = _fencing.captured_ticket(claimed_msg.to_dict())

        # 录入一笔过期心跳（时间戳为 10000 秒前）
        self.bus.heartbeat("antigravity", claimed_msg, ticket, note="过期心跳")
        hb_path = _heartbeat.heartbeat_path(self.workspace, claimed_msg.id, "antigravity")
        import json, time
        from datetime import datetime, timezone, timedelta
        data = json.loads(hb_path.read_text(encoding="utf-8"))
        data["at_epoch_s"] = time.time() - 10000
        data["at"] = (datetime.now(timezone.utc) - timedelta(seconds=10000)).isoformat()
        hb_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        # 尝试通过工作流管道核验，必须被硬门禁拦截
        from agentbus import workflow_pipeline as _pipeline
        with self.assertRaises(_pipeline.WorkflowPipelineError) as ctx:
            _pipeline.verify_pipeline_prerequisites_for_delivery(
                self.workspace, claimed_msg.to_dict(), "antigravity", ticket)

        self.assertEqual(ctx.exception.code, "PIPELINE_HEARTBEAT_STALE")
        self.assertIn("会话心跳已超时", str(ctx.exception))

    def test_commit_result_with_valid_heartbeat_succeeds(self):
        """测试正常录入有效心跳的任务能够顺利提交回执并终结。"""
        task = self.bus.make("codex", "antigravity", "task", "测试任务3", "测试目标3",
                             result_to="codex", expected_artifacts=[])
        msg = self.bus.post(task)
        ready_file = self.bus.dirs("antigravity")["ready"] / f"{msg.id}.json"
        claimed_msg = self.bus.claim("antigravity", ready_file)
        ticket = _fencing.captured_ticket(claimed_msg.to_dict())

        # 录入合法心跳
        self.bus.heartbeat("antigravity", claimed_msg, ticket, note="合法心跳")

        # 提交结果成功
        result_msg = self.bus.commit_result("antigravity", claimed_msg, ticket,
                                            body="正式交付结论报告")
        self.assertIsNotNone(result_msg)
        self.assertEqual(result_msg.type, "result")

        # 任务已进入 done/ 目录
        done_file = self.bus.dirs("antigravity")["done"] / f"{claimed_msg.id}.json"
        self.assertTrue(done_file.exists())

    def test_frontend_heartbeat_ui_rules(self):
        """测试前端 JS 和 CSS 中包含了心跳活跃徽标与红黄警报选择器。"""
        js = (ASSETS / "app.js").read_text(encoding="utf-8")
        css = (ASSETS / "app.css").read_text(encoding="utf-8")

        self.assertIn("leaf-hb-badge", js)
        self.assertIn("hb-warning-yellow", js)
        self.assertIn("hb-warning-red", js)
        self.assertIn(".leaf-hb-badge", css)
        self.assertIn(".hb-warning-yellow", css)
        self.assertIn(".hb-warning-red", css)


if __name__ == "__main__":
    unittest.main()
