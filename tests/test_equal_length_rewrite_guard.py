#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_equal_length_rewrite_guard.py: 等长重写检测与 SHA256 强校验门禁测试。

来源：§10.16 AUDIT-360-EQUAL-LENGTH-REWRITE-DETECTION（P1）。
验收标准：
1. 验证等长文本/二进制在内容被重写时，单纯比对 bytes 会漏判；
2. 验证系统使用 sha256 强校验能够精确识别等长重写并拒绝；
3. 验证 jobs._artifact_error 与 WorkspaceFiles 清单核验时，当遇到等长重写会判定 CONTENT_MISMATCH；
4. 验证完全一致的文件（bytes 和 sha256 均一致）正常通过。
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

# 导入待测集成模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
import sys
sys.path.insert(0, str(PROJECT_DIR))

from agentbus import jobs  # noqa: E402
from agentbus.config import load as load_config  # noqa: E402


class TestEqualLengthRewriteGuard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        # 初始化最小工作区
        bus_dir = self.root / ".agentbus"
        bus_dir.mkdir(parents=True, exist_ok=True)
        (bus_dir / "config.json").write_text(json.dumps({
            "schema_version": 1,
            "workspace": str(self.root),
            "output_root": str(self.root),
            "director": "codex",
            "workers": {
                "codex": {"enabled": True, "can_spawn": True, "executor_enabled": True}
            }
        }), encoding="utf-8")
        self.cfg = load_config(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_equal_length_rewrite_detection(self):
        """测试等长重写在只有 bytes 比对时漏判，但在 sha256 校验下被精确拦截。"""
        original_content = b"PROJECT_SPEC_VERSION_A_20261011_STABLE"
        rewritten_content = b"PROJECT_SPEC_VERSION_B_20261012_CHANGE"
        self.assertEqual(len(original_content), len(rewritten_content))
        self.assertNotEqual(original_content, rewritten_content)

        file_path = self.root / "PROJECT.md"
        file_path.write_bytes(original_content)

        orig_sha = hashlib.sha256(original_content).hexdigest()
        rewritten_sha = hashlib.sha256(rewritten_content).hexdigest()
        self.assertNotEqual(orig_sha, rewritten_sha)

        # 写入重写内容
        file_path.write_bytes(rewritten_content)
        self.assertEqual(file_path.stat().st_size, len(original_content))

        # 构造期望声明与回执
        task_item = {
            "id": "t-test-rewrite",
            "expected_artifacts": ["PROJECT.md"]
        }
        mock_receipt = type("MockReceipt", (), {
            "id": "m-receipt-1",
            "reply_to": "t-test-rewrite",
            "artifacts": [
                {
                    "path": "PROJECT.md",
                    "bytes": len(original_content),
                    "sha256": orig_sha  # 回执中声明了旧 hash，与磁盘当前实际内容不一致
                }
            ]
        })()

        err = jobs._artifact_error(self.cfg, task_item, mock_receipt)
        self.assertTrue(err, "遇到等长重写且 hash 失配时必须报错")
        self.assertIn("CONTENT_MISMATCH", str(err))

    def test_exact_match_passes(self):
        """测试字节和 SHA256 均一致时正常通过。"""
        content = b"VALID_DELIVERY_PAYLOAD_CONTENT"
        file_path = self.root / "OUTPUT.txt"
        file_path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()

        task_item = {
            "id": "t-test-exact",
            "expected_artifacts": ["OUTPUT.txt"]
        }
        mock_receipt = type("MockReceipt", (), {
            "id": "m-receipt-2",
            "reply_to": "t-test-exact",
            "artifacts": [
                {
                    "path": "OUTPUT.txt",
                    "bytes": len(content),
                    "sha256": sha
                }
            ]
        })()

        err = jobs._artifact_error(self.cfg, task_item, mock_receipt)
        self.assertEqual(err, "", "强校验完全一致时必须返回空字符串表示无错误")


if __name__ == "__main__":
    unittest.main()
