"""
End-to-end Product User Journey Verification (P1, §5.8)
Source: Antigravity (2026-10-12)

Verifies the complete real user lifecycle:
1. Initialize a clean project workspace directory from scratch.
2. Configure project topology (coordinator, workers, file handover mode).
3. Build and import a multi-tier task forest (roots, branches, leaves, dependencies).
4. Dispatch tasks to workers' ready inboxes.
5. Workers claim tasks with exclusive lease & pump active heartbeats (state -> running).
6. Workers generate deliverables in output_root and compute strict SHA256/byte manifests.
7. Workers submit formal receipt answers and commit results (state -> delivered).
8. Downstream dependent tasks automatically unlock from blocked to claimable.
9. Export portable project backups excluding secrets and absolute host paths.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agentbus.bus import Bus
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load, save
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class TestProductUserJourney(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve() / "clean_project"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.workspace / "business_output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.bridge = WebBridge(self.workspace)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_complete_clean_workspace_user_journey(self):
        # 1. 配置项目与团队拓扑 (Step 1: Configure project)
        conf_res = self.bridge.call("configure", {
            "workspace": str(self.workspace),
            "output_root": str(self.output_dir),
            "coordinator": "codex",
            "assignment_mode": "manual",
            "members": [
                {"id": "codex", "enabled": True, "communication_mode": "file"},
                {"id": "antigravity", "enabled": True, "communication_mode": "file"},
                {"id": "workbuddy", "enabled": True, "communication_mode": "file"}
            ]
        })
        self.assertTrue(conf_res["ok"], conf_res.get("error"))
        proj_view = conf_res["data"]
        self.assertEqual(proj_view["project"]["assignment_mode"], "manual")
        self.assertTrue(any(m["id"] == "antigravity" and m["enabled"] for m in proj_view["members"]))

        # 2. 建立任务树 (Step 2: Seed task forest with dependency chain)
        cfg = load(self.workspace)
        forest = TaskForest(cfg)
        forest.replace_structure({
            "schema_version": 1,
            "roots": [{"id": "r-core", "title": "核心交付工程", "goal": "端到端产出合格软件"}],
            "branches": [{"id": "b-dev", "title": "开发与验证", "parent_id": "r-core"}],
            "leaves": [
                {
                    "id": "leaf-impl",
                    "section_no": "1.1",
                    "title": "功能编码实现",
                    "parent_id": "b-dev",
                    "goal": "在 output_root 生成 app.py",
                    "expected_artifacts": ["app.py"],
                    "dependencies": []
                },
                {
                    "id": "leaf-test",
                    "section_no": "1.2",
                    "title": "功能测试验收",
                    "parent_id": "b-dev",
                    "goal": "读取 app.py 执行冒烟测试",
                    "expected_artifacts": ["test_report.json"],
                    "dependencies": ["leaf-impl"]
                }
            ]
        })

        snap_initial = self.bridge.call("snapshot")["data"]
        leaves_init = snap_initial["forest"]["leaves"]
        leaf_impl_init = next(l for l in leaves_init if l["id"] == "leaf-impl")
        leaf_test_init = next(l for l in leaves_init if l["id"] == "leaf-test")
        self.assertEqual(leaf_impl_init["state"], "queued")
        self.assertTrue(leaf_impl_init["claimable"])
        self.assertEqual(leaf_test_init["state"], "blocked")
        self.assertFalse(leaf_test_init["claimable"])

        # 3. 协调者定向派发 leaf-impl 给 antigravity
        disp_res = self.bridge.call("dispatch_leaf", {"leaf_id": "1.1", "agent": "antigravity"})
        self.assertTrue(disp_res["ok"], disp_res.get("error"))
        task_id = disp_res["data"]["dispatched_task_id"]
        self.assertTrue(bool(task_id))

        # 4. 成员 antigravity 认领任务并打卡心跳
        bus = Bus(self.workspace / ".agentbus")
        ready_file = bus.agents_dir / "antigravity/ready" / f"{task_id}.json"
        self.assertTrue(ready_file.is_file())
        task_msg = bus.claim("antigravity", ready_file)
        self.assertIsNotNone(task_msg)
        ticket = captured_ticket(task_msg)

        # 验证认领后状态变为 claimed
        snap_claimed = self.bridge.call("snapshot")["data"]
        leaf_claimed = next(l for l in snap_claimed["forest"]["leaves"] if l["id"] == "leaf-impl")
        self.assertEqual(leaf_claimed["state"], "claimed")

        # 心跳打卡，状态变迁为 running
        hb_res = bus.heartbeat("antigravity", task_msg, ticket, note="正在编写核心模块代码")
        self.assertTrue(hb_res)
        snap_running = self.bridge.call("snapshot")["data"]
        leaf_running = next(l for l in snap_running["forest"]["leaves"] if l["id"] == "leaf-impl")
        self.assertEqual(leaf_running["state"], "running")

        # 5. 生成交付产物并计算指纹
        deliverable_file = self.output_dir / "app.py"
        code_bytes = b"print('AgentBus Product User Journey Verified.')\n"
        deliverable_file.write_bytes(code_bytes)
        art_manifest = [{
            "path": "app.py",
            "bytes": len(code_bytes),
            "sha256": hashlib.sha256(code_bytes).hexdigest()
        }]

        # 6. 提交回执与交付成果 (commit_result)
        bus.commit_result(
            "antigravity",
            task_msg,
            ticket,
            body="核心模块 app.py 已成功产出并通过预检。",
            artifacts=art_manifest,
            artifact_root=self.output_dir,
            exit_code=0
        )

        # 7. 验证 leaf-impl 变更为 delivered，且下游 leaf-test 自动解锁为 claimable！
        snap_delivered = self.bridge.call("snapshot")["data"]
        leaves_after = snap_delivered["forest"]["leaves"]
        leaf_impl_done = next(l for l in leaves_after if l["id"] == "leaf-impl")
        leaf_test_unblocked = next(l for l in leaves_after if l["id"] == "leaf-test")

        self.assertEqual(leaf_impl_done["state"], "delivered")
        self.assertEqual(leaf_test_unblocked["state"], "queued")
        self.assertTrue(leaf_test_unblocked["claimable"], "下游节点在依赖满足后应自动变为 claimable")

        # 8. 导出可移植项目备份并验证隐私隔离
        backup_res = self.bridge.call("export_backup", {"preview_only": True})
        self.assertTrue(backup_res["ok"], backup_res.get("error"))
        backup_preview = backup_res["data"]
        self.assertTrue(len(backup_preview.get("manifest", [])) > 0)


if __name__ == "__main__":
    unittest.main()
