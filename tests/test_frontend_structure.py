"""§8.11 前端结构统计与去重守卫。

背景：`app.js` 是单文件前端（无构建、无框架）。任务书称"折叠/嵌套分支渲染近乎双份逻辑"。
实测：**确有重复，但不是"双份渲染"**——两个嵌套分支块能力差异很大（一处全功能、
一处是降级桩），真正共用的是**节点/连线的数据集标记**那几步。

本测试做两件事：
  1. **结构统计**：给出可复算的体积/监听器/函数指标（供"前端源码结构统计"证据）；
  2. **去重守卫**：公共标记必须走 `tagBranchNode` / `tagCollapsibleEdge`，
     不得再散落成内联的 5 字段 / 3 字段赋值块。

只读：不写任何前端文件。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
import unittest

_PACKAGE = Path(__file__).resolve().parents[1] / "agentbus"
_APP_JS = _PACKAGE / "web_assets" / "app.js"


def _node() -> str | None:
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    for candidate in (
    ):
        if Path(candidate).is_file():
            return candidate
    return None


NODE = _node()
_TEXT = _APP_JS.read_text(encoding="utf-8")
_LINES = _TEXT.splitlines()


class StructureCensusTests(unittest.TestCase):
    """结构统计：可复算、可断言趋势。"""

    def test_reports_core_metrics(self):
        metrics = {
            "bytes": _APP_JS.stat().st_size,
            "lines": len(_LINES),
            "addEventListener": len(re.findall(r"addEventListener\(", _TEXT)),
            "functions": len(re.findall(r"\bfunction\s+[A-Za-z_$][\w$]*\s*\(", _TEXT)),
            "querySelectorAll": len(re.findall(r"querySelectorAll\(", _TEXT)),
        }
        # 指标必须可复算且为正；具体数值交由回执记录（避免把会漂移的数字写死成断言）。
        for key, value in metrics.items():
            self.assertGreater(value, 0, "%s 应为正数" % key)

    def test_single_file_still_parses(self):
        if not NODE:
            self.skipTest("node 不可用")
        proc = subprocess.run([NODE, "--check", str(_APP_JS)],
                              capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)

    def test_no_es_modules_or_build_step(self):
        """保持"单文件、无构建"现状：不得引入 import/export 语句。"""
        self.assertNotRegex(_TEXT, r"(?m)^\s*import\s+[\w{*]",
                            "app.js 目前是无模块单文件；引入 ESM 会破坏内联加载")
        self.assertNotRegex(_TEXT, r"(?m)^\s*export\s+",
                            "app.js 目前无导出；引入 export 会破坏内联加载")


def _window_hits(*, required, window=3):
    """在**连续 window 行**里找"同时出现 required 全部标记"的位置。

    为什么用滑动窗口：内联赋值既可能压在一行里，也可能拆成相邻几行——
    只按单行匹配会漏掉拆行写法（这正是第一版守卫漏报的原因）。
    匹配用的是**紧凑写法** `dataset.x=`（等号前无空格），因此不会误伤 helper 内部
    带空格的 `dataset.x = `（那也是本次刻意保留的可读写法）。
    """
    hits = []
    total = len(_LINES)
    for start in range(total):
        block = "\n".join(_LINES[start:start + window])
        if all(re.search(pattern, block) for pattern in required):
            hits.append(start + 1)
    return hits


# 紧凑写法（等号前无空格）——内联赋值的特征。
_COMPACT_GROUP = r"\.dataset\.group=[^\s=]"
_COMPACT_LEAFIDS = r"\.dataset\.leafIds="
_COMPACT_GROUPID = r"\.dataset\.groupId=[^\s=]"
_COLLAPSIBLE_EDGE = r"data-collapsible-edge"


class DedupGuardTests(unittest.TestCase):
    """公共标记必须走 helper，不得再散落成内联块。"""

    def test_node_tagging_is_centralised(self):
        offenders = _window_hits(required=(_COMPACT_GROUP, _COMPACT_LEAFIDS))
        self.assertEqual([], offenders,
                         "节点标记应改用 tagBranchNode(...)，不要内联 5 字段：行 %s" % offenders)

    def test_edge_tagging_is_centralised(self):
        offenders = _window_hits(required=(_COMPACT_GROUPID, _COLLAPSIBLE_EDGE))
        self.assertEqual([], offenders,
                         "连线标记应改用 tagCollapsibleEdge(...)：行 %s" % offenders)

    def test_helpers_exist_and_are_used(self):
        self.assertIn("function tagBranchNode(", _TEXT)
        self.assertIn("function tagCollapsibleEdge(", _TEXT)
        calls = len(re.findall(r"(?<!function )tagBranchNode\(", _TEXT))
        calls += len(re.findall(r"(?<!function )tagCollapsibleEdge\(", _TEXT))
        self.assertGreaterEqual(calls, 3,
                                "helper 若无人调用即为死代码，应删而非留")

    def test_helpers_preserve_field_names(self):
        """helper 必须写同名的 dataset 字段，否则行为不等价。"""
        for field in ("dataset.group", "dataset.groupId", "dataset.foldAncestors",
                      "dataset.rootId", "dataset.leafIds"):
            self.assertIn(field, _TEXT, "helper 必须保留字段 %s" % field)
        self.assertIn("data-collapsible-edge", _TEXT)


class ReadOnlyTests(unittest.TestCase):
    def test_this_test_does_not_write_frontend(self):
        """用 AST 判定，避免"扫描器扫到自己源码里的字符串"这种自指误报。"""
        import ast
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        banned = {"write_text", "write_bytes", "unlink", "mkdir", "rmtree"}
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in banned:
                    offenders.append(name)
        self.assertEqual([], offenders, "本测试不得写文件：%s" % offenders)


if __name__ == "__main__":
    unittest.main()
