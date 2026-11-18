from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import json
import unittest
from pathlib import Path

WEB_ASSETS = Path(__file__).resolve().parents[1] / "agentbus" / "web_assets"


class PreviewSnapshotLeakTests(unittest.TestCase):
    def setUp(self):
        self.preview_path = WEB_ASSETS / "preview.json"
        if not self.preview_path.exists():
            self.skipTest("preview.json 未包含在当前源码发布包中（符合隐私脱敏防泄漏契约）")
        self.raw_text = self.preview_path.read_text(encoding="utf-8")
        self.data = json.loads(self.raw_text)

    def test_preview_json_is_valid_and_structured(self):
        """测试 preview.json 为合法且具备完整演示结构的快照数据。"""
        self.assertIn("roots", self.data)
        self.assertIn("branches", self.data)
        self.assertIn("leaves", self.data)
        self.assertIn("members", self.data)
        self.assertIn("project", self.data)
        self.assertIn("config", self.data)

        roots = self.data["roots"]
        branches = self.data["branches"]
        leaves = self.data["leaves"]
        members = self.data["members"]

        self.assertGreaterEqual(len(roots), 1, "至少包含 1 个演示主干目标")
        self.assertGreaterEqual(len(branches), 2, "至少包含 2 个演示业务分支")
        self.assertGreaterEqual(len(leaves), 4, "至少包含 4 项演示叶子任务")
        self.assertGreaterEqual(len(members), 2, "至少包含 2 个演示成员")

    def test_leaves_contain_diverse_states_and_valid_topology(self):
        """测试叶子节点覆盖已交付、进行中、阻塞等典型协同状态，并具备合法章节号。"""
        leaves = self.data["leaves"]
        states = {l.get("state") for l in leaves}
        self.assertIn("delivered", states, "必须包含已交付节点演示")
        self.assertIn("running", states, "必须包含进行中节点演示")
        self.assertIn("blocked", states, "必须包含等待依赖节点演示")

        for leaf in leaves:
            self.assertTrue(leaf.get("section_no"), f"叶子 {leaf.get('id')} 必须有章节号")
            self.assertTrue(leaf.get("title"), f"叶子 {leaf.get('id')} 必须有标题")
            self.assertTrue(leaf.get("goal"), f"叶子 {leaf.get('id')} 必须有目标")

    def test_absolute_privacy_and_zero_sensitive_leak(self):
        """测试 preview.json 100% 杜绝真实项目隐私与环境敏感信息泄漏。"""
        text_lower = self.raw_text.lower()

        # 1. 杜绝本地特定工程标识
        self.assertNotIn("vibecoding", text_lower, "严禁泄露 VibeCoding 项目标识")
        self.assertNotIn(".workbuddy", text_lower, "严禁泄露 .workbuddy 私有目录")

        # 2. 杜绝本地磁盘绝对路径
        self.assertNotIn("i:\\", text_lower, "严禁泄露 I 盘绝对路径")
        self.assertNotIn("c:\\users\\", text_lower, "严禁泄露本机用户主目录")
        self.assertNotIn("成果/", self.raw_text, "严禁包含物理成果目录路径")

        # 3. 杜绝真实消息号与作业号
        self.assertNotIn("m-17", self.raw_text, "严禁包含真实 m-17... 格式消息 ID")
        self.assertNotIn("j-b", self.raw_text, "严禁包含真实 j-b... 格式作业 ID")
        self.assertNotIn("j-8", self.raw_text, "严禁包含真实 j-8... 格式作业 ID")
        self.assertNotIn("cv-", self.raw_text, "严禁包含真实 cv-... 格式执行会话 ID")

        # 4. 验证规范脱敏占位符
        project = self.data.get("project", {})
        config = self.data.get("config", {})
        self.assertEqual(project.get("workspace"), "<项目目录>", "工作空间必须使用标准占位符")
        self.assertEqual(config.get("output_root"), "<业务产出目录>", "产出目录必须使用标准占位符")

    def test_frontend_normalize_contract_compatibility(self):
        """测试 preview.json 完全契合 app.js 中 normalize() 契约解析要求。"""
        forest = self.data.get("forest") or self.data
        roots = forest.get("roots", [])
        branches = forest.get("branches", [])
        leaves = forest.get("leaves", [])

        # 校验根引用
        branch_parents = {b.get("parent_id") for b in branches}
        root_ids = {r.get("id") for r in roots}
        for bp in branch_parents:
            self.assertIn(bp, root_ids, f"分支父节点 {bp} 必须在 roots 中存在")

        # 校验叶子父引用
        branch_ids = {b.get("id") for b in branches}
        for leaf in leaves:
            self.assertIn(leaf.get("parent_id"), branch_ids, f"叶子父分支 {leaf.get('parent_id')} 必须在 branches 中存在")


if __name__ == "__main__":
    unittest.main()
