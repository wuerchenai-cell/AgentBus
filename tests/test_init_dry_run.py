"""§7.17 AUDIT-INIT-DRYRUN 回归测试。

背景
----
``agentbus init`` 会静默写出多份文件：``.agentbus/``（含 config.json、agents/、
events.jsonl、logs/）、``AGENTS.md``、``TASKBOOK.md``、``MEMORY.md``、
``STATE.md``、``LOG.md``。对已有项目目录误跑一次就会莫名多出 5 份文档。

任务书验收（隐含）：加 ``--dry-run`` 先列出将创建的文件；且

  * ``--dry-run`` **不得创建任何东西**（含不产生空目录）；
  * 已存在的文件必须报「不会覆盖」而不是「将创建」；
  * 正常 ``init``（去掉 --dry-run）**行为不变**，且**不覆盖已有文件**。

全部用 ``tempfile`` 合成目录，**不触碰真实项目**。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus.cli import main as cli_main


class InitDryRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(["init", "--workspace", str(self.ws), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_creates_nothing_on_fresh_dir(self):
        code, out, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.ws.iterdir()), [],
                         "--dry-run 不得在空目录留下任何文件或目录")
        self.assertIn("预演", out)

    def test_dry_run_lists_all_targets(self):
        _, out, _ = self._run("--dry-run")
        for name in ("AGENTS.md", "TASKBOOK.md", "MEMORY.md",
                     "STATE.md", "LOG.md", "config.json"):
            self.assertIn(name, out, "预演清单缺少 %s" % name)

    def test_dry_run_json_mode(self):
        code, out, _ = self._run("--dry-run", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["dry_run"])
        joined = "\n".join(data["would_create"])
        for name in ("AGENTS.md", "TASKBOOK.md", "LOG.md"):
            self.assertIn(name, joined)
        self.assertEqual(sorted(self.ws.iterdir()), [])

    def test_dry_run_reports_existing_without_overwrite(self):
        """预置文件后跑 dry-run：应列为「已存在」而非「将创建」。"""
        (self.ws / "AGENTS.md").write_text("# 我的约定\n", encoding="utf-8")
        code, out, _ = self._run("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("已存在", out)
        # AGENTS.md 应出现在 existing 段，内容不得被动过
        self.assertEqual((self.ws / "AGENTS.md").read_text(encoding="utf-8"),
                         "# 我的约定\n")
        self.assertNotIn("TASKBOOK.md: " + str(self.ws), out)  # 未真的打印创建行

    def test_normal_init_still_creates_files(self):
        code, out, _ = self._run()
        self.assertEqual(code, 0)
        for name in ("AGENTS.md", "TASKBOOK.md", "MEMORY.md",
                     "STATE.md", "LOG.md"):
            self.assertTrue((self.ws / name).is_file(), "%s 未被创建" % name)
        self.assertTrue((self.ws / ".agentbus" / "config.json").is_file())

    def test_normal_init_does_not_overwrite_existing(self):
        (self.ws / "AGENTS.md").write_text("# 我的约定\n", encoding="utf-8")
        (self.ws / "TASKBOOK.md").write_text("# 我的任务书\n", encoding="utf-8")
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual((self.ws / "AGENTS.md").read_text(encoding="utf-8"),
                         "# 我的约定\n", "init 覆盖了已有 AGENTS.md")
        self.assertEqual((self.ws / "TASKBOOK.md").read_text(encoding="utf-8"),
                         "# 我的任务书\n", "init 覆盖了已有 TASKBOOK.md")

    def test_dry_run_and_real_agree_on_target_set(self):
        """预演说的「将创建」，与真正创建出来的，必须一致。"""
        _, out, _ = self._run("--dry-run")
        planned = set()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("+ "):
                planned.add(Path(line[2:].strip()).name)
        code, _, _ = self._run()
        self.assertEqual(code, 0)
        actual = {p.name for p in self.ws.iterdir()}
        # 预演含 .agentbus 与 config.json 等嵌套项，去重后应被 actual 覆盖
        for name in planned:
            if name in (".agentbus", "agents", "config.json"):
                continue
            self.assertIn(name, actual, "预演承诺创建 %s，实际未创建" % name)


if __name__ == "__main__":
    unittest.main()
