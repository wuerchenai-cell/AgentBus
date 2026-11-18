# -*- coding: utf-8 -*-
"""§7.16 AUDIT-COMMAND-TAXONOMY 定向回归。

覆盖三个可独立验证的契约：
  1. 稳定退出码表存在，且数值与历史行为逐字节一致（0/1/2/3/4/130）；
  2. 同一「配置缺失」错误类在所有子命令下返回同一个码（EXIT_CONFIG），
     尤其是 workflow status —— 它曾把 ConfigError 吞成 1；
  3. 「先执行 …」提示按运行形态分支：源码态给 python -m agentbus，
     冻结态给可执行文件本身；且 --help 尾注含退出码表与推荐命令。
"""
from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ExitCodeTableTests(unittest.TestCase):
    """退出码表是单一事实源，数值冻结。"""

    def test_canonical_values_are_frozen(self):
        from agentbus import config

        self.assertEqual(config.EXIT_OK, 0)
        self.assertEqual(config.EXIT_COMMAND_FALSE, 1)
        self.assertEqual(config.EXIT_CONFIG, 2)
        self.assertEqual(config.EXIT_ADAPTER, 3)
        self.assertEqual(config.EXIT_OPERATION, 4)
        self.assertEqual(config.EXIT_INTERRUPT, 130)

    def test_table_documents_every_code(self):
        from agentbus import config

        for code in (config.EXIT_OK, config.EXIT_COMMAND_FALSE, config.EXIT_CONFIG,
                     config.EXIT_ADAPTER, config.EXIT_OPERATION, config.EXIT_INTERRUPT):
            self.assertIn(code, config.EXIT_CODES)
            self.assertTrue(config.EXIT_CODES[code].strip())


class LauncherNameTests(unittest.TestCase):
    """提示文案必须与用户手上的运行形态一致。"""

    def test_source_tree_gives_module_invocation(self):
        from agentbus import config

        name = config.launcher_name()
        self.assertIn("python", name)
        self.assertIn("agentbus", name)
        self.assertNotIn(".exe", name.casefold())

    def test_frozen_runtime_gives_executable_path(self):
        from agentbus import config

        original_frozen = getattr(sys, "frozen", None)
        original_exe = getattr(sys, "executable", None)
        try:
            sys.frozen = True
            sys.executable = r"D:\frozen\AgentBus.exe"
            name = config.launcher_name()
            self.assertIn("AgentBus.exe", name)
            self.assertNotIn("python -m", name)
        finally:
            if original_frozen is None:
                del sys.frozen
            else:
                sys.frozen = original_frozen
            sys.executable = original_exe

    def test_missing_config_message_uses_launcher_name(self):
        from agentbus import config

        with self.assertRaises(config.ConfigError) as ctx:
            config.load(Path(os.environ.get("TEMP", "/tmp")) / "cb-no-such-ws-716", create=False)
        message = str(ctx.exception)
        self.assertIn(config.launcher_name(), message)
        self.assertIn("init", message)


class ConfigErrorExitCodeTests(unittest.TestCase):
    """同一错误类不得有两个退出码。"""

    def _run(self, *argv: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([sys.executable, "-m", "agentbus.cli", *argv],
                              capture_output=True, text=True, encoding="utf-8",
                              cwd=str(ROOT), env=env)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_config_missing_is_consistent_across_commands(self):
        from agentbus import config

        missing = str(Path(os.environ.get("TEMP", "/tmp")) / "cb-716-missing-ws")
        codes = {}
        for argv in (("workflow", "status"), ("tree", "status"),
                     ("status",), ("inbox",)):
            code, out = self._run(*argv, "--workspace", missing)
            codes[" ".join(argv)] = code
            self.assertEqual(code, config.EXIT_CONFIG,
                             f"{argv} 在缺配置时返回了 {code}，输出：{out[:200]}")
        self.assertEqual(len(set(codes.values())), 1)

    def test_workflow_error_keeps_command_false_code(self):
        """WorkflowError 仍是「命令级否定」=1，不能被误并进配置码。"""
        import agentbus.cli as cli
        from agentbus.config import EXIT_COMMAND_FALSE
        from agentbus.project_workflow import WorkflowError

        original = cli._load

        def fake_load(args):
            raise WorkflowError("未就绪")

        try:
            cli._load = fake_load
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = cli.main(["workflow", "status", "--workspace", "."])
        finally:
            cli._load = original
        self.assertEqual(code, EXIT_COMMAND_FALSE)

    def test_config_error_from_workflow_reaches_main_handler(self):
        """配置错误必须冒泡到 main，由 EXIT_CONFIG 处理。"""
        import agentbus.cli as cli
        from agentbus.config import EXIT_CONFIG, ConfigError

        original = cli._load

        def fake_load(args):
            raise ConfigError("找不到配置")

        try:
            cli._load = fake_load
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = cli.main(["workflow", "status", "--workspace", "."])
        finally:
            cli._load = original
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("配置错误", stderr.getvalue())


class HelpSurfaceTests(unittest.TestCase):
    """--help 必须给出可行动的导引，而不是 33 条平铺。"""

    def _help(self) -> str:
        from agentbus import cli

        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["--help"])
        return stdout.getvalue()

    def test_help_lists_exit_code_table(self):
        text = self._help()
        self.assertIn("退出码", text)
        for marker in ("配置缺失或非法", "操作未完成", "用户中断"):
            self.assertIn(marker, text)

    def test_help_marks_recommended_and_legacy(self):
        text = self._help()
        self.assertIn("常用命令", text)
        self.assertIn("兼容保留", text)
        # 推荐入口里的几条必须出现
        for name in ("gui", "init", "tree", "inbox", "answer"):
            self.assertIn(name, text)

    def test_preflight_fails_closed_when_lease_inspection_is_unavailable(self):
        import agentbus.cli as cli

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            control = ws / ".agentbus"
            control.mkdir()
            formal_exe = ws / "成果" / "AgentBus.exe"
            formal_exe.parent.mkdir()
            binary = b"test executable bytes"
            formal_exe.write_bytes(binary)
            (control / "workflow.json").write_text(json.dumps({
                "launcher": {"sha256": hashlib.sha256(binary).hexdigest()}
            }), encoding="utf-8")

            stdout = io.StringIO()
            with patch.object(cli, "LeaseManager", side_effect=OSError("lease store unavailable")):
                with redirect_stdout(stdout):
                    code = cli.cmd_preflight(type("Args", (), {"workspace": str(ws)})())

        self.assertNotEqual(code, 0)
        self.assertIn("无法核验活跃租约", stdout.getvalue())
        self.assertNotIn("[SUCCESS]", stdout.getvalue())

    def test_preflight_succeeds_when_lease_store_is_available(self):
        import agentbus.cli as cli

        with tempfile.TemporaryDirectory() as tmpdir:
            ws = Path(tmpdir)
            control = ws / ".agentbus"
            control.mkdir()
            formal_exe = ws / "成果" / "AgentBus.exe"
            formal_exe.parent.mkdir()
            binary = b"test executable bytes"
            formal_exe.write_bytes(binary)
            (control / "workflow.json").write_text(json.dumps({
                "launcher": {"sha256": hashlib.sha256(binary).hexdigest()}
            }), encoding="utf-8")

            stdout = io.StringIO()
            with patch.object(cli, "LeaseManager") as manager:
                manager.return_value.active.return_value = []
                with redirect_stdout(stdout):
                    code = cli.cmd_preflight(type("Args", (), {"workspace": str(ws)})())

        self.assertEqual(code, 0, stdout.getvalue())
        self.assertIn("[PASS] 无排他性悬挂租约冲突", stdout.getvalue())
        self.assertIn("[SUCCESS]", stdout.getvalue())

    def test_help_does_not_remove_legacy_commands(self):
        """标注兼容保留 ≠ 删除；命令仍必须注册。"""
        text = self._help()
        for name in ("boss", "run", "team", "handoff", "skills", "install-skill", "setup"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
