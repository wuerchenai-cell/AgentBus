"""§7.4 AUDIT-DISK-BLOAT 回归测试。

背景
----
源码全局检索 ``backup|retention|prune|cleanup|keep_last`` **零命中**——
备份与归档纯人工，无轮转、无上限、无自动回收。实测 19 份 EXE 备份 / 2.97 GB、
pilots 12.60 GB。本测试锁定新增的 ``agentbus.gc`` 模块三条纪律：

1. 默认只报告、不删除（``apply`` 无 ``confirmed=True`` 必须抛错）；
2. 硬红线路径永不入选（``.agentbus`` / ``.workbuddy`` / ``资料`` / ``skills``）；
3. 保留规则保守：每组保留最新 N 份 + 被文档引用者一律保留。

全部用 ``tempfile`` 合成夹具，**不触碰真实项目**。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus.gc import DEFAULT_KEEP, GcError, apply as gc_apply, plan as gc_plan


class GcPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "成果").mkdir()
        self.base = self.root / "成果"

    def tearDown(self):
        self.tmp.cleanup()

    def _mk(self, name: str, size: int, mtime: float):
        p = self.base / name
        p.write_bytes(b"x" * size)
        import os
        os.utime(p, (mtime, mtime))
        return p

    def test_keeps_newest_n_per_group(self):
        for i, mt in enumerate([100, 200, 300, 400, 500]):
            self._mk(f"App.exe.backup-2026{i}.exe", 10, mt)
        p = gc_plan(self.root, keep=2)
        self.assertEqual(len(p.keep), 2)
        self.assertEqual(len(p.delete), 3)
        kept = {c.path.name for c in p.keep}
        self.assertIn("App.exe.backup-20263.exe", kept)
        self.assertIn("App.exe.backup-20264.exe", kept)

    def test_doc_referenced_backup_is_protected(self):
        """被 .md 提及的旧备份必须保留，即便超出 keep 窗口。"""
        old = self._mk("App.exe.backup-20260.exe", 10, 100)
        self._mk("App.exe.backup-20261.exe", 10, 200)
        self._mk("App.exe.backup-20262.exe", 10, 300)
        (self.root / "HANDOFF.md").write_text(
            "回滚请用 成果/App.exe.backup-20260.exe", encoding="utf-8")
        p = gc_plan(self.root, keep=1)
        kept = {c.path.name for c in p.keep}
        self.assertIn(old.name, kept, "被文档引用的备份不应被计划删除")
        reasons = {c.path.name: c.reason for c in p.keep}
        self.assertIn("引用", reasons[old.name])

    def test_forbidden_paths_never_selected(self):
        """硬红线目录下的同名备份永不入选。"""
        bus = self.root / ".agentbus"
        bus.mkdir()
        (bus / "App.exe.backup-20260.exe").write_bytes(b"y" * 10)
        (self.root / "资料").mkdir()
        (self.root / "资料" / "App.exe.backup-20261.exe").write_bytes(b"y" * 10)
        p = gc_plan(self.root, keep=1)
        for c in p.delete + p.keep:
            self.assertNotIn(".agentbus", c.path.parts)
            self.assertNotIn("资料", c.path.parts)

    def test_packages_excluded_by_default(self):
        self._mk("AgentBus-分享包.zip", 10, 100)
        p = gc_plan(self.root)
        self.assertEqual(p.scanned, 0, "分发包默认不应纳入候选")

    def test_packages_included_on_request(self):
        for i, mt in enumerate([100, 200, 300]):
            self._mk(f"AgentBus-pkg{i}.zip", 10, mt)
        p = gc_plan(self.root, keep=1, include_packages=True)
        self.assertEqual(p.scanned, 3)
        self.assertEqual(len(p.delete), 2)

    def test_keep_below_one_rejected(self):
        with self.assertRaises(GcError):
            gc_plan(self.root, keep=0)

    def test_scanned_bytes_counted(self):
        self._mk("App.exe.backup-a.exe", 1024, 100)
        self._mk("App.exe.backup-b.exe", 2048, 200)
        p = gc_plan(self.root, keep=1)
        self.assertEqual(p.scanned_bytes, 3072)
        self.assertEqual(p.reclaim_bytes, 1024)

    def test_delete_sorted_oldest_first(self):
        for i, mt in enumerate([500, 100, 300]):
            self._mk(f"App.exe.backup-{i}.exe", 10, mt)
        p = gc_plan(self.root, keep=1)
        mtimes = [c.mtime for c in p.delete]
        self.assertEqual(mtimes, sorted(mtimes), "删除顺序应为最老优先")


class GcApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "成果").mkdir()
        self.base = self.root / "成果"

    def tearDown(self):
        self.tmp.cleanup()

    def _mk(self, name, size, mtime):
        import os
        p = self.base / name
        p.write_bytes(b"x" * size)
        os.utime(p, (mtime, mtime))
        return p

    def test_apply_without_confirmation_raises_and_deletes_nothing(self):
        for i, mt in enumerate([100, 200, 300]):
            self._mk(f"App.exe.backup-{i}.exe", 10, mt)
        p = gc_plan(self.root, keep=1)
        targets = [c.path for c in p.delete]
        with self.assertRaises(GcError):
            gc_apply(p, confirmed=False)
        for t in targets:
            self.assertTrue(t.exists(), "未确认时不得删除任何文件")

    def test_apply_confirmed_removes_only_planned(self):
        for i, mt in enumerate([100, 200, 300]):
            self._mk(f"App.exe.backup-{i}.exe", 10, mt)
        p = gc_plan(self.root, keep=1)
        survivors = {c.path for c in p.keep}
        removed = gc_apply(p, confirmed=True)
        self.assertEqual(removed, 2)
        for s in survivors:
            self.assertTrue(s.exists())
        for c in p.delete:
            self.assertFalse(c.path.exists())

    def test_plan_is_pure_no_side_effects(self):
        for i, mt in enumerate([100, 200, 300]):
            self._mk(f"App.exe.backup-{i}.exe", 10, mt)
        before = sorted(self.base.iterdir())
        gc_plan(self.root, keep=1)
        self.assertEqual(before, sorted(self.base.iterdir()),
                         "plan() 不应改动磁盘")


class DefaultKeepTests(unittest.TestCase):
    def test_default_is_conservative(self):
        self.assertGreaterEqual(DEFAULT_KEEP, 1)


if __name__ == "__main__":
    unittest.main()
