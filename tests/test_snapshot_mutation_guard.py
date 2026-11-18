from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import unittest
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets"


class SnapshotMutationGuardTests(unittest.TestCase):
    def setUp(self):
        self.js = (ASSETS / "app.js").read_text(encoding="utf-8")

    def test_mutating_actions_set_comprehensiveness(self):
        """测试所有修改账本、树结构、拓扑与状态的动作均纳入 MUTATING_ACTIONS 白名单。"""
        required_actions = [
            "append_leaf",
            "add_leaf",
            "add_task",
            "update_leaf_topology",
            "submit_leaf_result",
            "configure",
            "start_goal",
            "claim_leaf",
            "dispatch_leaf",
            "dispatch_ready",
            "stop",
            "resume",
            "checkpoint_leaf",
            "handoff_leaf",
            "ensure_quickstart",
            "recovery",
            "set_workspace",
        ]
        self.assertIn("const MUTATING_ACTIONS = new Set([", self.js)
        for action in required_actions:
            self.assertIn(f"'{action}'", self.js, f"动作 {action} 必须在 MUTATING_ACTIONS 集合中")

    def test_is_mutating_action_helper_logic(self):
        """测试 isMutatingAction 辅助函数，涵盖常规动作及带 confirm 的 import_tree。"""
        self.assertIn("function isMutatingAction(action, payload={})", self.js)
        self.assertIn("MUTATING_ACTIONS.has(action)", self.js)
        self.assertIn("action === 'import_tree' && payload && payload.confirm === true", self.js)

    def test_call_function_advances_generation_via_guard(self):
        """测试 call() 中统一接入 isMutatingAction 推进 mutationGeneration++。"""
        self.assertIn("if (isMutatingAction(action, payload)) mutationGeneration++;", self.js)

    def test_set_snapshot_implements_generation_and_temporal_guard(self):
        """测试 setSnapshot 实现了代次落后拦截与时间戳倒流拦截的双重防线。"""
        self.assertIn("function setSnapshot(data, requestGen = null)", self.js)
        self.assertIn("requestGen !== null && requestGen < mutationGeneration", self.js)
        self.assertIn("reason: 'stale_generation'", self.js)
        self.assertIn("incomingTime < currentTime", self.js)
        self.assertIn("reason: 'stale_timestamp'", self.js)

    def test_refresh_discards_inflight_stale_snapshot(self):
        """测试 refresh() 在异步 snapshot 返回时代次已过时将丢弃旧数据并留痕。"""
        self.assertIn("const generation=mutationGeneration;", self.js)
        self.assertIn("generation===mutationGeneration", self.js)
        self.assertIn("reason: 'stale_generation_inflight'", self.js)
        self.assertIn("[SnapshotGuard] Discarding stale snapshot", self.js)

    def test_inspect_and_ui_exports_mutation_guard(self):
        """测试 inspect() 与 window.AgentBusUI 正式暴露代次状态与拒绝留痕。"""
        self.assertIn("mutationGeneration,", self.js)
        self.assertIn("lastRejectedSnapshot,", self.js)
        self.assertIn("mutationGeneration:()=>mutationGeneration", self.js)
        self.assertIn("advanceMutationGeneration:()=>++mutationGeneration", self.js)
        self.assertIn("isMutatingAction,", self.js)
        self.assertIn("lastRejectedSnapshot:()=>lastRejectedSnapshot", self.js)

    def test_branch_switch_popover_mounted_safely(self):
        """测试分支切换弹窗提升挂载至 forestStage 且具有安全 z-index，避免被局部重绘刷掉。"""
        self.assertIn("$('forestStage').appendChild(pop)", self.js)
        self.assertIn("pop.style.zIndex = '150'", self.js)


if __name__ == "__main__":
    unittest.main()
