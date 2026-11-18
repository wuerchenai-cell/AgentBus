# -*- coding: utf-8 -*-
"""测试 AUDIT-CROSS-HOST-FIELD-OBSERVATION-LOG (§10.5) 多宿主适配器与规则注入矩阵"""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from agentbus.host_adapters import (
    SUPPORTED_HOSTS,
    get_host_configs,
    audit_host_compatibility,
    apply_host_patches,
)
from agentbus.cli import cmd_host_setup


class TestAuditHostAdapters(unittest.TestCase):
    def test_get_host_configs(self):
        """验证所有主流宿主模版及路径完整性"""
        ws = Path("/tmp/mock_ws")
        configs = get_host_configs(ws)
        self.assertIn("cursor", configs)
        self.assertIn("windsurf", configs)
        self.assertIn("trae", configs)
        self.assertIn("codex", configs)
        self.assertIn("vscode", configs)
        self.assertIn("mcp", configs)

        self.assertIn(".cursor/rules/agentbus.mdc", configs["cursor"])
        self.assertIn(".windsurfrules", configs["windsurf"])
        self.assertIn(".trae/rules.md", configs["trae"])
        self.assertIn(".vscode/settings.json", configs["vscode"])
        self.assertIn(".agentbus/mcp/agentbus-mcp.json", configs["mcp"])

    def test_audit_and_apply_patches(self):
        """测试审计诊断及实际补丁注入"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            # 未配置状态诊断
            diag1 = audit_host_compatibility(ws)
            self.assertFalse(diag1["cursor"]["configured"])
            self.assertFalse(diag1["windsurf"]["configured"])

            # 仅应用 cursor 补丁
            written_cursor = apply_host_patches(ws, ["cursor"])
            self.assertEqual(written_cursor, [".cursor/rules/agentbus.mdc"])
            self.assertTrue((ws / ".cursor/rules/agentbus.mdc").exists())

            diag2 = audit_host_compatibility(ws)
            self.assertTrue(diag2["cursor"]["configured"])
            self.assertFalse(diag2["windsurf"]["configured"])

            # 应用全部补丁
            written_all = apply_host_patches(ws, ["all"])
            self.assertIn(".windsurfrules", written_all)
            self.assertIn(".trae/rules.md", written_all)
            self.assertIn(".vscode/settings.json", written_all)
            self.assertIn(".agentbus/mcp/agentbus-mcp.json", written_all)

            diag3 = audit_host_compatibility(ws)
            self.assertTrue(diag3["cursor"]["configured"])
            self.assertTrue(diag3["windsurf"]["configured"])
            self.assertTrue(diag3["trae"]["configured"])
            self.assertTrue(diag3["vscode"]["configured"])
            self.assertTrue(diag3["mcp"]["configured"])

    def test_json_merge_capability(self):
        """测试已存在 JSON 配置文件时的安全合并"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            vscode_settings = ws / ".vscode" / "settings.json"
            vscode_settings.parent.mkdir(parents=True)
            vscode_settings.write_text(json.dumps({"editor.tabSize": 4}), encoding="utf-8")

            apply_host_patches(ws, ["vscode"])
            merged = json.loads(vscode_settings.read_text(encoding="utf-8"))
            self.assertEqual(merged["editor.tabSize"], 4)
            self.assertIn("files.watcherExclude", merged)

    def test_invalid_existing_json_is_preserved_and_reported(self):
        """Malformed user settings must never be silently overwritten by setup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            settings = ws / ".vscode" / "settings.json"
            settings.parent.mkdir(parents=True)
            original = b'{"editor.tabSize":'
            settings.write_bytes(original)

            with self.assertRaisesRegex(ValueError, "original file was preserved"):
                apply_host_patches(ws, ["vscode"])

            self.assertEqual(settings.read_bytes(), original)

    def test_cmd_host_setup_integration(self):
        """测试 CLI cmd_host_setup 命令入口"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / ".agentbus").mkdir(parents=True)
            (ws / ".agentbus" / "config.json").write_text("{}", encoding="utf-8")

            # 1. 预览/审计模式
            args1 = argparse.Namespace(workspace=str(ws), host="all", apply=False, json=True)
            self.assertEqual(cmd_host_setup(args1), 0)

            # 2. 注入模式
            args2 = argparse.Namespace(workspace=str(ws), host="all", apply=True, json=False)
            self.assertEqual(cmd_host_setup(args2), 0)

            # 验证补丁生效
            self.assertTrue((ws / ".cursor/rules/agentbus.mdc").exists())


if __name__ == "__main__":
    unittest.main()
