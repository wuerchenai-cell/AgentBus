# -*- coding: utf-8 -*-
"""§7.18 AUDIT-ROOT-HYGIENE 定向回归：根目录卫生与两个入口文档的分工。

本测试不依赖真实项目，用临时工作区断言三条已写入 AGENTS.md 的契约：
  1. 根目录只放契约/入口文档与控制目录，不放散落的临时文件；
  2. 控制态（租约、锁、心跳）一律在 `.agentbus/` 下，根目录不出现影子目录；
  3. `AGENTS.md`（公共契约）与 `AGENTBUS.md`（EXE 管理入口）两个名字必须在
     `AGENTS.md` 首部显式区分，避免新成员读错走错恢复路径。

第 3 条对真实项目文件做只读断言；前两条对临时工作区做结构性断言。
"""
from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout


from project_paths import host_project_root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 根目录允许存在的文档（AGENTS.md §产出目录 已列明）
ALLOWED_ROOT_DOCS = {
    "AGENTS.md", "AGENTBUS.md", "PROJECT.md", "TASKBOOK.md", "STATE.md",
    "MEMORY.md", "HANDOFF.md", "LOG.md", "CLAIMS.md", "NEXT.md",
    "PROJECT_PLAN.md", "00-系统自我坦白与快速上手.md",
}
# 根目录允许存在的目录（控制/业务）
ALLOWED_ROOT_DIRS = {
    ".agentbus", ".codebuddy", ".opencode", ".trae", ".zcode", ".workbuddy",
    ".trae-html-share-packages", "成果", "资料", "测试", "templates",
}
# 根目录不得出现的控制影子目录（真身在 .agentbus/ 下）
SHADOW_CONTROL_DIRS = {"leases", "operation-locks", "heartbeats", "agents", "jobs"}
# 「资料_*.txt」这类前缀是上一版遗留命名，禁止再出现在根目录
LEGACY_PREFIXES = ("资料_",)


def _workspace_root() -> Path:
    """真实项目根：integration → pilot → 成果/agentbus/pilots/... → 项目根。"""
    return host_project_root()


class RealProjectRootHygieneTests(unittest.TestCase):
    """对真实项目根做**只读**断言。"""

    def setUp(self):
        self.root = _workspace_root()
        if self.root is None:
            raise unittest.SkipTest("Snapshot source tree has no project .agentbus workspace")

    def test_no_shadow_control_dirs_at_root(self):
        found = sorted(p.name for p in self.root.iterdir()
                       if p.is_dir() and p.name in SHADOW_CONTROL_DIRS)
        self.assertEqual(found, [],
                         f"根目录出现控制影子目录（真身在 .agentbus/ 下）：{found}")

    def test_no_legacy_prefixed_files_at_root(self):
        found = sorted(p.name for p in self.root.iterdir()
                       if p.is_file() and p.name.startswith(LEGACY_PREFIXES))
        self.assertEqual(found, [], f"根目录残留旧命名文件：{found}")

    def test_root_files_are_known_docs_or_dotfiles(self):
        for p in self.root.iterdir():
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            self.assertIn(p.name, ALLOWED_ROOT_DOCS,
                          f"根目录出现契约外的散落文件：{p.name}")

    def test_control_state_lives_under_agentbus(self):
        bus = self.root / ".agentbus"
        self.assertTrue((bus / "leases").is_dir(), ".agentbus/leases 必须存在")
        self.assertTrue((bus / "agents").is_dir(), ".agentbus/agents 必须存在")

    def test_agents_md_disambiguates_agentbus_md(self):
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        head = text[:2000]
        self.assertIn("AGENTBUS.md", head,
                      "AGENTS.md 首部必须显式提到 AGENTBUS.md 以区分二者")
        # 必须说清「谁维护 / 读它做什么」这层区分
        self.assertIn("EXE", head)
        self.assertIn("契约", head)

    def test_agentbus_md_still_matches_workflow_registration(self):
        """AGENTBUS.md 被哈希登记，清理不得改动它。"""
        import hashlib
        import json
        reg = self.root / ".agentbus" / "workflow.json"
        if not reg.is_file():
            self.skipTest("无 workflow.json 登记")
        data = json.loads(reg.read_text(encoding="utf-8"))
        expected = (data.get("files") or {}).get("AGENTBUS.md")
        if not expected:
            self.skipTest("workflow.json 未登记 AGENTBUS.md")
        actual = hashlib.sha256((self.root / "AGENTBUS.md").read_bytes()).hexdigest()
        self.assertEqual(actual, expected,
                         "AGENTBUS.md 与 workflow.json 登记不一致（不应被手工改动）")


class SyntheticHygieneContractTests(unittest.TestCase):
    """用临时工作区验证「归位」规则本身可判定。"""

    def test_shadow_and_legacy_detection_rules_fire(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "leases").mkdir()                 # 影子控制目录
            (ws / "资料_临时.txt").write_text("x", encoding="utf-8")  # 旧命名
            (ws / "AGENTS.md").write_text("# 契约", encoding="utf-8")

            shadows = sorted(p.name for p in ws.iterdir()
                             if p.is_dir() and p.name in SHADOW_CONTROL_DIRS)
            legacy = sorted(p.name for p in ws.iterdir()
                            if p.is_file() and p.name.startswith(LEGACY_PREFIXES))
            strays = sorted(p.name for p in ws.iterdir()
                            if p.is_file() and not p.name.startswith(".")
                            and p.name not in ALLOWED_ROOT_DOCS)
            self.assertEqual(shadows, ["leases"])
            self.assertEqual(legacy, ["资料_临时.txt"])
            self.assertEqual(strays, ["资料_临时.txt"])

    def test_after_tidy_the_same_rules_pass(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / ".agentbus" / "leases").mkdir(parents=True)
            (ws / "资料").mkdir()
            (ws / "资料" / "临时.txt").write_text("x", encoding="utf-8")
            (ws / "AGENTS.md").write_text("# 契约", encoding="utf-8")

            shadows = [p.name for p in ws.iterdir()
                       if p.is_dir() and p.name in SHADOW_CONTROL_DIRS]
            legacy = [p.name for p in ws.iterdir()
                      if p.is_file() and p.name.startswith(LEGACY_PREFIXES)]
            strays = [p.name for p in ws.iterdir()
                      if p.is_file() and not p.name.startswith(".")
                      and p.name not in ALLOWED_ROOT_DOCS]
            self.assertEqual(shadows, [])
            self.assertEqual(legacy, [])
            self.assertEqual(strays, [])

    def test_allowed_root_dirs_covers_real_layout(self):
        """清理后的真实布局必须落在允许集合内，防止规则与现状脱节。"""
        self.assertIn("资料", ALLOWED_ROOT_DIRS)
        self.assertIn("成果", ALLOWED_ROOT_DIRS)
        for shadow in ("leases", "operation-locks", "agents"):
            self.assertNotIn(shadow, ALLOWED_ROOT_DIRS)


if __name__ == "__main__":
    unittest.main()
