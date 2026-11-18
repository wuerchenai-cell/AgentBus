#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for Section 4.8 POWERSHELL-UTF8-RECEIPTS:
Verifying round-trip UTF-8 file integrity, SHA256 stability, and avoiding ASCII pipeline corruption.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from project_paths import host_project_root

ROOT = host_project_root()
INTEGRATION_DIR = ROOT / "成果/agentbus/pilots/product-convergence-20261008/integration" if ROOT else None
PYTHON_EXE = INTEGRATION_DIR / ".build-venv/Scripts/python.exe" if INTEGRATION_DIR else None

if INTEGRATION_DIR:
    sys.path.insert(0, str(INTEGRATION_DIR))
from agentbus.workspace_files import WorkspaceFiles
from agentbus.project_workflow import peer_skill, resume_prompt
from agentbus.config import load


@unittest.skipIf(ROOT is None or not (ROOT / ".agentbus").is_dir(), "Host project root not configured or snapshot environment")
class TestPowershellUtf8Receipts(unittest.TestCase):
    def setUp(self):
        self.scratch_dir = ROOT / "成果" / "任务书维护" / "receipts" / "scratch_test_utf8"
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        self.test_file = self.scratch_dir / "sample_utf8_receipt.txt"

    def tearDown(self):
        if self.test_file.exists():
            self.test_file.unlink()
        if self.scratch_dir.exists():
            try:
                self.scratch_dir.rmdir()
            except OSError:
                pass

    def test_chinese_text_roundtrip_integrity(self):
        """Test Chinese text with quotes, linebreaks, spaces, and punctuation survives bitwise round-trip."""
        sample_text = (
            "【测试任务回执】\n"
            "1. 做了什么：完成 Section 4.8 Windows 中文回执防损机制；\n"
            "2. 产物路径：成果/测试/test.py；\n"
            "3. 限制与未验证项：未在 ASCII 单字节终端管道下直接测试，仅采用 --body-file 规避；\n"
            "特殊字符测试：\"双引号\"、'单引号'、￥¥€$、Tab\t对齐、标点符号！？（）—…\n"
        )
        raw_bytes = sample_text.encode("utf-8")
        expected_sha = hashlib.sha256(raw_bytes).hexdigest()

        # Write to file
        self.test_file.write_bytes(raw_bytes)

        # Read back via WorkspaceFiles
        wf = WorkspaceFiles(ROOT)
        rel_path = self.test_file.relative_to(ROOT).as_posix()
        read_res = wf.read_text(rel_path, expected_sha256=expected_sha)

        self.assertEqual(read_res["sha256"], expected_sha)
        self.assertEqual(read_res["bytes"], len(raw_bytes))
        self.assertEqual(read_res["text"], sample_text)
        self.assertNotIn("?", read_res["text"])

    def test_ascii_pipeline_replacement_demonstration(self):
        """Demonstrate that decoding UTF-8 bytes under ASCII replacement generates question marks,
        proving why direct ASCII pipeline passing is hazardous.
        """
        sample_text = "中文测试：不会变成问号"
        raw_bytes = sample_text.encode("utf-8")
        # Simulating Windows US-ASCII pipeline lossy transfer
        corrupted = raw_bytes.decode("ascii", errors="replace")
        self.assertIn("\ufffd", corrupted)  # Unicode replacement char (which becomes ? in ascii output)

    def test_handoff_command_template_references_body_file(self):
        """Test cmd_handoff output instructs using --body-file instead of inline --body."""
        import argparse
        from agentbus.cli import cmd_handoff

        ns = argparse.Namespace(workspace=str(ROOT), agent="antigravity")
        f = io.StringIO()
        with redirect_stdout(f):
            cmd_handoff(ns)
        out = f.getvalue()
        if "[空]" not in out:
            self.assertIn("--body-file", out)
            self.assertIn("--expected-body-sha256", out)
            self.assertIn("Windows 编码防损回执要求", out)

    def test_workflow_instructions_contain_encoding_boundary(self):
        """Test peer_skill contains explicit encoding boundary warnings."""
        skill_text = peer_skill("antigravity")
        self.assertIn("--body-file", skill_text)
        self.assertIn("Windows / PowerShell", skill_text)
        self.assertIn("默认管道编码可能将中文转换为问号", skill_text)


if __name__ == "__main__":
    unittest.main()
