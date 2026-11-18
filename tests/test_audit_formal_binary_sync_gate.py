#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_audit_formal_binary_sync_gate.py: 正式 EXE 与源码树同步门禁测试。

来源：§10.7 AUDIT-360-FORMAL-BINARY-SYNC-GATE（P0）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 导入待测模块
TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent
TOOLS_DIR = PROJECT_DIR / "tools"
sys.path.insert(0, str(PROJECT_DIR))

from tools import check_formal_sync
from project_paths import host_project_root, host_source_root
HOST_PROJECT = host_project_root()
HOST_SOURCE = host_source_root()


class TestFormalBinarySyncGate(unittest.TestCase):
    @unittest.skipUnless(HOST_SOURCE is not None, "Explicit host release audit required; not performed by portable source tests")
    def test_current_manifest_is_synchronized(self):
        """验证当前 packaging/BUILD-MANIFEST.json 中正式源码树与集成树 100% 同步。"""
        manifest_path = HOST_SOURCE / "packaging" / "BUILD-MANIFEST.json"
        self.assertTrue(manifest_path.is_file(), f"Manifest file not found: {manifest_path}")

        res = check_formal_sync.audit_formal_sync(manifest_path)
        self.assertTrue(res["manifest_exists"])
        self.assertTrue(res["synchronized"], "formal_source_tree_audit.synchronized 应为 True")
        self.assertEqual(res["missing_in_formal_count"], 0, f"缺失模块数应为 0，实测: {res['missing_in_formal_count']}")
        self.assertEqual(res["drift_in_formal_count"], 0, f"漂移模块数应为 0，实测: {res['drift_in_formal_count']}")
        self.assertTrue(res["passed"])

    @unittest.skipUnless(HOST_SOURCE is not None, "Explicit host release audit required; not performed by portable source tests")
    def test_check_formal_sync_cli_strict(self):
        """验证 check_formal_sync.py 在 CLI --strict 模式下运行成功（退出码 0）。"""
        script = TOOLS_DIR / "check_formal_sync.py"
        manifest_path = HOST_SOURCE / "packaging" / "BUILD-MANIFEST.json"

        proc = subprocess.run(
            [sys.executable, str(script), "--manifest", str(manifest_path), "--strict"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, f"CLI exited with {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("[OK]", proc.stdout)

    def test_negative_gate_fails_on_drift(self):
        """反向注入验证：若 manifest 中存在 drift 或未同步，门禁必然失败拦截。"""
        script = TOOLS_DIR / "check_formal_sync.py"
        with tempfile.TemporaryDirectory() as td:
            bad_manifest = Path(td) / "BAD-MANIFEST.json"
            bad_data = {
                "formal_source_tree_audit": {
                    "synchronized": False,
                    "summary": {
                        "missing_in_formal_count": 5,
                        "drift_in_formal_count": 2,
                    },
                    "missing_in_formal": ["mod1"],
                    "drift_in_formal": ["mod2"],
                }
            }
            bad_manifest.write_text(json.dumps(bad_data), encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, str(script), "--manifest", str(bad_manifest), "--strict"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertNotEqual(proc.returncode, 0, "存在漂移时门禁必须非零拦截！")
            self.assertIn("[FAIL]", proc.stdout)

    @unittest.skipUnless(HOST_PROJECT is not None, "Explicit host release audit required; not performed by portable source tests")
    def test_workflow_json_launcher_bytes_field(self):
        """验证 .agentbus/workflow.json 中 launcher 具备合法 bytes 字段。"""
        repo_root = HOST_PROJECT
        wf_file = repo_root / ".agentbus" / "workflow.json"
        self.assertTrue(wf_file.is_file(), f"workflow.json not found: {wf_file}")

        wf_data = json.loads(wf_file.read_text(encoding="utf-8"))
        launcher = wf_data.get("launcher", {})
        self.assertIn("bytes", launcher, "workflow.json launcher 缺失 bytes 字段！")
        self.assertIsInstance(launcher["bytes"], int)
        self.assertGreater(launcher["bytes"], 100_000_000)

        # 验证与实际 EXE 大小吻合
        exe_path = repo_root / "成果" / "AgentBus.exe"
        if exe_path.exists():
            self.assertEqual(launcher["bytes"], exe_path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
