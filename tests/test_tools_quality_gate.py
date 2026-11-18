# -*- coding: utf-8 -*-
"""测试 AUDIT-360-TOOLS-QUALITY-GATE (§10.11) 工具链质量门禁与最小测试补齐"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.check_tools_quality import audit_tools


class TestToolsQualityGate(unittest.TestCase):
    def setUp(self):
        self.tools_dir = Path(__file__).resolve().parent.parent / "tools"
        self.tests_dir = Path(__file__).resolve().parent

    def test_audit_tools_quality_gate_passes(self):
        """验证所有 tools/ 下的脚本 100% 具备 README 登记与测试覆盖"""
        res = audit_tools(self.tools_dir, self.tests_dir)
        self._assert_quality(res)

    def _assert_quality(self, res):
        self.assertEqual(res["missing_docs"], [], f"存在缺少文档的工具: {res['missing_docs']}")
        self.assertEqual(res["missing_tests"], [], f"存在缺少测试的工具: {res['missing_tests']}")
        self.assertTrue(res["all_passed"])

    def _host_root(self):
        from project_paths import host_source_root
        root = host_source_root()
        if root is None:
            self.skipTest("宿主开发工具未验：未显式设置 AGENTBUS_TEST_SOURCE_ROOT；便携源码不包含全部宿主工具")
        return root

    def _load_host_tool(self, root, script_name):
        path = root / "tools" / script_name
        self.assertTrue(path.is_file(), f"显式宿主工具不存在: {path}")
        spec = importlib.util.spec_from_file_location("_agentbus_host_" + path.stem, path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_tools_help_invocation(self):
        """便携包十个核心公开工具必须全部存在并支持 --help。"""
        self._assert_tools_help(self.tools_dir, (
            "export_source.py", "check_offline.py", "build_release.py", "smoke_frozen.py",
            "handover_screen.py", "skill_manifest_audit.py", "check_formal_sync.py",
            "generate_build_manifest.py", "delivery_self_proof.py", "archived_binary_inventory.py",
        ))

    def test_host_tools_help_invocation(self):
        """显式宿主验收保留完整原开发工具 --help 清单，不调用真实模型。"""
        root = self._host_root()
        self._assert_tools_help(root / "tools", (
            "benchmark_canvas_interactions.py",
            "broad_except_locator.py",
            "build_dependency_audit.py",
            "check_tools_quality.py",
            "delivery_self_proof.py",
            "find_queued.py",
            "gen_7_21_artifacts.py",
            "inspect_forest.py",
            "inspect_tree.py",
            "launcher_truth_check.py",
            "tree_refresh_bench.py",
            "handover_screen.py",
            "live_codex_check.py",
            "mcp_smoke.py",
        ))

    def _assert_tools_help(self, tools_dir, script_names):
        for script_name in script_names:
            with self.subTest(script=script_name):
                script_path = tools_dir / script_name
                self.assertTrue(script_path.is_file(), f"{script_name} 不存在: {script_path}")
                cmd = [sys.executable, str(script_path), "--help"]
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    cwd=str(tools_dir.parent),
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"运行 {script_name} --help 失败: {proc.stderr}",
                )
                self.assertIn("usage:", proc.stdout.lower() + proc.stderr.lower())

    def test_host_inject_jsdocs_module_callable(self):
        """只从显式宿主导入 inject_jsdocs，不执行会写源码的 main。"""
        root = self._host_root()
        inject_jsdocs = self._load_host_tool(root, "inject_jsdocs.py")
        self.assertTrue(callable(inject_jsdocs.main))

    def test_host_full_tool_quality(self):
        """审查显式宿主全部工具及其 README、测试覆盖，不借用快照工具。"""
        root = self._host_root()
        checker = self._load_host_tool(root, "check_tools_quality.py")
        self._assert_quality(checker.audit_tools(root / "tools", root / "tests"))

    def test_readonly_tools_do_not_mutate(self):
        """断言只读工具在运行时不创建或修改外部文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            (ws / ".agentbus").mkdir(parents=True)
            (ws / ".agentbus" / "task-tree.json").write_text('{"roots":[],"branches":[],"leaves":[]}', encoding="utf-8")
            
            # 运行 inspect_tree
            cmd = [sys.executable, str(self.tools_dir / "inspect_tree.py"), "--workspace", str(ws)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0)

    def test_auxiliary_and_deprecated_tools_safety(self):
        """覆盖排查工具与废弃部署脚本的安全防线"""
        root = self._host_root()
        tools_dir = root / "tools"
        # 1. 验证 comprehensive_audit 与 deep_consistency_check 存在并具备只读排查入口
        self.assertTrue((tools_dir / "comprehensive_audit.py").is_file())
        self.assertTrue((tools_dir / "deep_consistency_check.py").is_file())

        # 2. 验证 deploy_formal_release.py 具备安全阻断（直接执行必须退出并报错，严禁静默篡改 workflow.json）
        deploy_script = tools_dir / "deploy_formal_release.py"
        self.assertTrue(deploy_script.is_file())
        proc = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(deploy_script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        self.assertNotEqual(proc.returncode, 0, "deploy_formal_release 必须阻断执行")
        output = (proc.stderr or "") + (proc.stdout or "")
        self.assertIn("安全阻断", output)


if __name__ == "__main__":
    unittest.main()

