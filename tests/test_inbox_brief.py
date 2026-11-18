# -*- coding: utf-8 -*-
"""§7.12 AUDIT-INBOX-BRIEF 定向回归：inbox --brief 三行简报。

契约：
  1. _relative_time 只做展示，绝不猜时间；无法解析时返回 '--'；
  2. _brief_lines 是纯函数：空 → 一行；非空 → 首行待办数 + 每条一行；
     行内必须包含 类型 / 标题 / 来源 / 相对时间 / 回投目标；
  3. --brief 与默认（非 brief）输出互不干扰：默认不带 --brief 时一条消息仍打两行以上；
  4. result_to 缺失时如实写「未指定」，不得伪造收件人。
"""
from __future__ import annotations

import datetime
import io
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class RelativeTimeTests(unittest.TestCase):
    """相对时间是展示层：可解析就给人话，不可解析就认怂。"""

    def setUp(self):
        from agentbus.cli import _relative_time
        self.fn = _relative_time
        self.now = datetime.datetime(2026, 10, 11, 12, 0, 0).timestamp()

    def test_minutes(self):
        ts = "2026-10-11T11:45:00"
        self.assertEqual(self.fn(ts, now=self.now), "15 分钟前")

    def test_hours(self):
        ts = "2026-10-11T09:00:00"
        self.assertEqual(self.fn(ts, now=self.now), "3 小时前")

    def test_days(self):
        ts = "2026-10-09T12:00:00"
        self.assertEqual(self.fn(ts, now=self.now), "2 天前")

    def test_just_now_under_a_minute(self):
        ts = "2026-10-11T11:59:30"
        self.assertEqual(self.fn(ts, now=self.now), "刚刚")

    def test_unparseable_returns_placeholder_not_a_guess(self):
        for bad in ("", "   ", "not-a-time", None, "2026-13-45T99:99:99"):
            self.assertEqual(self.fn(bad, now=self.now), "--",
                             f"无法解析的时间 {bad!r} 必须返回占位符")

    def test_accepts_space_separated_form(self):
        ts = "2026-10-11 11:00:00"
        self.assertEqual(self.fn(ts, now=self.now), "1 小时前")


class BriefLinesTests(unittest.TestCase):
    """简报是纯函数，可直接断言。"""

    def _msg(self, **kw):
        base = dict(id="m-x", type="task", title="标题", sender="codex",
                    ts="2026-10-11T11:00:00", result_to="codex",
                    hop=0, attempt=0, body="")
        base.update(kw)
        return SimpleNamespace(**base)

    def test_empty_inbox_is_one_line(self):
        from agentbus.cli import _brief_lines
        lines = _brief_lines("workbuddy", [])
        self.assertEqual(len(lines), 1)
        self.assertIn("workbuddy", lines[0])
        self.assertIn("没有待处理", lines[0])

    def test_count_line_then_one_line_per_message(self):
        from agentbus.cli import _brief_lines
        msgs = [self._msg(id="m-1", title="甲"), self._msg(id="m-2", title="乙")]
        lines = _brief_lines("workbuddy", msgs)
        self.assertEqual(len(lines), 3)          # 1 摘要 + 2 明细
        self.assertIn("2 条待办", lines[0])
        self.assertIn("甲", lines[1])
        self.assertIn("乙", lines[2])

    def test_each_line_carries_type_title_sender_recipient(self):
        from agentbus.cli import _brief_lines
        msg = self._msg(type="review", title="复核 7.38", sender="trae", result_to="codex")
        line = _brief_lines("workbuddy", [msg])[1]
        for token in ("review", "复核 7.38", "trae", "codex"):
            self.assertIn(token, line)

    def test_missing_result_to_is_stated_not_faked(self):
        from agentbus.cli import _brief_lines
        msg = self._msg(result_to=None)
        line = _brief_lines("workbuddy", [msg])[1]
        self.assertIn("未指定", line)
        self.assertNotIn("回投→codex", line)


class BriefCliWiringTests(unittest.TestCase):
    """--brief 已注册，且不改变默认输出形状。"""

    def test_brief_flag_is_registered(self):
        from agentbus import cli
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            cli.main(["inbox", "--help"])
        self.assertIn("--brief", stdout.getvalue())

    def test_default_output_is_unchanged_without_flag(self):
        """不带 --brief 时仍是「--- 头 + 标题」两行结构。"""
        import tempfile
        from agentbus import cli
        from agentbus.config import DEFAULTS
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agentbus").mkdir()
            data = dict(DEFAULTS)
            data["workspace"] = str(root)
            (root / ".agentbus" / "config.json").write_text(
                _json.dumps(data, ensure_ascii=False), encoding="utf-8")
            ready = root / ".agentbus" / "agents" / "workbuddy" / "ready"
            ready.mkdir(parents=True)
            msg = {
                "id": "m-1791000000000-abcdef", "ts": "2026-10-11T11:00:00+08:00",
                "sender": "codex", "to": "workbuddy", "type": "task",
                "title": "演示任务", "body": "正文", "result_to": "codex",
            }
            (ready / "m-1791000000000-abcdef.json").write_text(
                _json.dumps(msg, ensure_ascii=False), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["inbox", "--workspace", str(root), "--agent", "workbuddy"])
            self.assertEqual(code, 0)
            text = out.getvalue()
            self.assertIn("--- m-1791000000000-abcdef", text)
            self.assertIn("演示任务", text)
            self.assertNotIn("条待办", text)   # 默认模式不是简报

            out2 = io.StringIO()
            with redirect_stdout(out2):
                code2 = cli.main(["inbox", "--workspace", str(root),
                                  "--agent", "workbuddy", "--brief"])
            self.assertEqual(code2, 0)
            brief = out2.getvalue()
            self.assertIn("1 条待办", brief)
            self.assertIn("演示任务", brief)
            self.assertNotIn("--- m-1791000000000-abcdef", brief)


if __name__ == "__main__":
    unittest.main()
