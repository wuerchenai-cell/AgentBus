# -*- coding: utf-8 -*-
"""测试 AUDIT-360-DISK-DEADWEIGHT (§10.6) 归档二进制清点与口径盲区治理"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.archived_binary_inventory import (
    find_archived_binaries,
    run_inventory,
    calculate_sha256,
)


class TestAuditDiskDeadweight(unittest.TestCase):
    def test_find_and_classify(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            # 建立一个正常的 deprecated 文件
            f1 = ws / "old_app.exe.deprecated_old_version"
            f1.write_bytes(b"mock binary content 1")

            # 建立一个 dist 目录残留文件
            dist_dir = ws / "dist-sample"
            dist_dir.mkdir()
            f2 = dist_dir / "output.exe"
            f2.write_bytes(b"mock binary content 2")

            # 建立一个普通源码文件（不应被命中）
            f3 = ws / "regular.py"
            f3.write_bytes(b"print('hello')")

            items = find_archived_binaries(ws)
            self.assertEqual(len(items), 2)
            categories = {x["category"] for x in items}
            self.assertEqual(categories, {"deprecated_binary", "dist_residual"})

    def test_run_inventory_readonly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            f1 = ws / "test.deprecated_old_version"
            f1.write_bytes(b"12345678")

            res = run_inventory(ws, compute_sha=True)
            self.assertEqual(res["summary"]["total_files"], 1)
            self.assertEqual(res["summary"]["total_bytes"], 8)
            self.assertEqual(res["items"][0]["bytes"], 8)
            self.assertIn("retention_policy", res)
            self.assertTrue(f1.exists())  # 绝对只读，绝不删除


if __name__ == "__main__":
    unittest.main()
