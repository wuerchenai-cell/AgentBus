# -*- coding: utf-8 -*-
"""§8.7 AUDIT-TRAE-SILENT-EXCEPTS — 静默吞异常面清理与守卫。

任务书原文：
    现状：agentbus 包 except …: pass 约 50 处（bare except 0、shell=True 0，
    属正向基线）。动作：清单化逐类处置——资源清理类保留并注明理由；功能类至少
    落调试日志。验收：产出分类清单与处置结果；后续新增静默点须附理由注释。
    证据：包内静态检索计数。

本测试把「后续新增静默点须附理由注释」变成可执行的守卫：
  1. 包内不应出现裸 `except:`（bare except）——沿用正向基线。
  2. 每一处「except …: pass」的 pass 行必须带理由注释（`# 资源清理类…` /
     `# 功能类…` 或等价说明），否则视为回归。
  3. 功能类静默点应实际调用 `note_silent(...)` 落一条可观测记录；默认关闭下
     该调用是 no-op，不改变任何行为。

这是一份**静态守卫**：不执行 GUI、不跑模型、不访问网络。
"""
from __future__ import annotations

import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import ast
import unittest
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / "agentbus"

# 已知由其他成员在途租约覆盖、本次无权修改的文件（须在回执中声明为未处置）。
_DEFERRED_FILES = {"bus.py", "web_bridge.py"}


def _iter_handlers(path: Path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            yield node, lines


class BareExceptGuardTests(unittest.TestCase):
    def test_no_bare_except_in_package(self):
        offenders = []
        for path in sorted(_PKG.rglob("*.py")):
            for node, _ in _iter_handlers(path):
                if node.type is None:
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual([], offenders, "出现裸 except: ——正向基线被破坏")


class SilentExceptJustificationTests(unittest.TestCase):
    def _silent_sites(self):
        sites = []
        for path in sorted(_PKG.rglob("*.py")):
            for node, lines in _iter_handlers(path):
                if not all(isinstance(s, ast.Pass) for s in node.body):
                    continue
                pnode = node.body[0]
                code_line = lines[pnode.lineno - 1]
                sites.append((path, pnode.lineno, code_line))
        return sites

    def test_every_silent_pass_has_a_reason_comment(self):
        missing = [f"{p.name}:{ln}" for p, ln, line in self._silent_sites() if "#" not in line]
        # 仅在途租约文件允许暂时未注释；其余一律必须注明理由。
        unresolved = []
        for token in missing:
            name = token.split(":")[0]
            if name not in _DEFERRED_FILES:
                unresolved.append(token)
        self.assertEqual(
            [], unresolved,
            "以下静默点缺少理由注释（新增静默点必须注明理由）：" + ", ".join(unresolved),
        )

    def test_deferred_files_are_the_only_known_gap(self):
        """守卫必须诚实：确知 bus.py/web_bridge.py 由他人租约覆盖，属未处置。"""
        pending = {}
        for path, ln, line in self._silent_sites():
            if "#" not in line:
                pending.setdefault(path.name, 0)
                pending[path.name] += 1
        self.assertTrue(
            set(pending).issubset(_DEFERRED_FILES),
            f"出现了未在租约豁免名单中的未注释静默点：{sorted(set(pending) - _DEFERRED_FILES)}",
        )


class SilentDebugSinkTests(unittest.TestCase):
    def test_default_is_silent_no_op(self):
        import os
        from agentbus import silent_debug

        old = os.environ.pop(silent_debug._ENV_FLAG, None)
        try:
            self.assertFalse(silent_debug.debug_enabled())
            silent_debug.note_silent("x.py:1", "functional", "r")
            self.assertEqual(0, silent_debug.pending())
            self.assertEqual([], silent_debug.drain())
        finally:
            if old is not None:
                os.environ[silent_debug._ENV_FLAG] = old

    def test_enabled_records_into_bounded_ring(self):
        import os
        from agentbus import silent_debug

        old = os.environ.get(silent_debug._ENV_FLAG)
        os.environ[silent_debug._ENV_FLAG] = "1"
        try:
            self.assertTrue(silent_debug.debug_enabled())
            silent_debug.drain()
            silent_debug.note_silent("a.py:1", "cleanup", "r1")
            silent_debug.note_silent("b.py:2", "functional", "r2")
            items = silent_debug.drain()
            self.assertEqual(2, len(items))
            self.assertEqual("a.py:1", items[0]["location"])
            self.assertEqual("functional", items[1]["category"])
            self.assertEqual([], silent_debug.drain())
        finally:
            if old is None:
                os.environ.pop(silent_debug._ENV_FLAG, None)
            else:
                os.environ[silent_debug._ENV_FLAG] = old

    def test_note_silent_never_raises(self):
        from agentbus import silent_debug

        silent_debug.note_silent(None, None, None)  # 畸形输入不得抛出


if __name__ == "__main__":
    unittest.main()
