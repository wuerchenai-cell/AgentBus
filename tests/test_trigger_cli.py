#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trigger_cli.py: contact 与 trigger CLI 命令与帮助信息测试。"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agentbus.cli import main


class TriggerCLITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ws = Path(self.temp_dir.name) / "test_project"
        self.ws.mkdir(parents=True, exist_ok=True)

    def test_contact_invite_and_show(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["contact", "invite", "--workspace", str(self.ws), "--agent", "codearts"])
        self.assertEqual(code, 0)
        self.assertIn("邀请已生成", out.getvalue())

        out2 = io.StringIO()
        with redirect_stdout(out2):
            code2 = main(["contact", "show", "--workspace", str(self.ws), "--agent", "codearts"])
        self.assertEqual(code2, 0)
        self.assertIn("尚未绑定", out2.getvalue())

    def test_trigger_status_and_stop_resume(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["trigger", "status", "--workspace", str(self.ws)])
        self.assertEqual(code, 0)
        self.assertIn("AgentBus 项目桌面触发服务状态", out.getvalue())

        out_stop = io.StringIO()
        with redirect_stdout(out_stop):
            code_stop = main(["trigger", "stop", "--workspace", str(self.ws)])
        self.assertEqual(code_stop, 0)
        self.assertIn("已停止新唤醒请求投递", out_stop.getvalue())

        out_res = io.StringIO()
        with redirect_stdout(out_res):
            code_res = main(["trigger", "resume", "--workspace", str(self.ws)])
        self.assertEqual(code_res, 0)
        self.assertIn("已恢复项目触发投递服务", out_res.getvalue())

    def test_trigger_diagnostics_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["trigger", "diagnostics", "--workspace", str(self.ws), "--json"])
        self.assertEqual(code, 0)
        self.assertIn('"schema_version": 1', out.getvalue())


if __name__ == "__main__":
    unittest.main()
