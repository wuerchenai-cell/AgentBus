"""§7.37 AUDIT-BODY-FILE-PATH-GUIDANCE 回归测试。

背景
----
同伴实测：``report --body-file <绝对路径>`` 被安全拒绝，但当时的错误只有
``UNSAFE_PATH: path contains a forbidden component``——**没有告诉用户正确写法**。
用户（含本 AI 自己）会反复试错。

验收标准（来自任务书）：
  1. 错误信息提供一个**符合当前工作区边界**的相对路径示例；
  2. 非法绝对路径**仍安全拒绝**；
  3. 正确相对路径**可以提交**。

本测试只调用 ``WorkspaceFiles._relative`` 静态校验，**不触碰磁盘**。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentbus.workspace_files import WorkspaceFileError, WorkspaceFiles


def _reject(path):
    """断言 path 被拒，返回错误消息；未拒则 fail。"""
    with __import__("unittest").TestCase().assertRaises(WorkspaceFileError):
        try:
            WorkspaceFiles._relative(path)
        except WorkspaceFileError:
            raise
    try:
        WorkspaceFiles._relative(path)
    except WorkspaceFileError as exc:
        return str(exc)
    raise AssertionError("expected WorkspaceFileError for %r" % (path,))


class PathGuidanceTests(unittest.TestCase):
    """验收 1：错误信息必须自带正确写法示例。"""

    def _assert_has_example(self, message: str):
        # 示例锚点：我们写进提示里的那个具体相对路径。
        self.assertIn("成果/复函/", message,
                      "错误信息缺少可照抄的工作区相对路径示例")
        self.assertIn("工作区相对路径", message)

    def test_absolute_drive_path_has_example(self):
        msg = _reject("D:/示例空间/协作 样例/成果/x.md")
        self._assert_has_example(msg)
        self.assertIn("UNSAFE_PATH", msg)

    def test_leading_slash_has_example(self):
        self._assert_has_example(_reject("/成果/x.md"))

    def test_backslash_absolute_has_example(self):
        self._assert_has_example(_reject("C:\\Users\\example\\x.md"))

    def test_private_dir_names_the_segment(self):
        """命中私有目录时要点名是哪一段，不能只给通用错误。"""
        msg = _reject(".agentbus/tmp/art.json")
        self.assertIn(".agentbus", msg)
        self.assertIn("私有", msg)

    def test_illegal_component_has_example(self):
        self._assert_has_example(_reject("成果//x.md"))
        self._assert_has_example(_reject("../outside.md"))


class PathSafetyUnchangedTests(unittest.TestCase):
    """验收 2：安全性不得因改善提示而放松。"""

    def test_absolute_paths_still_rejected(self):
        for bad in ("I:/x.md", "/x.md", "C:\\x.md", "//server/share/x.md"):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkspaceFileError):
                    WorkspaceFiles._relative(bad)

    def test_traversal_still_rejected(self):
        for bad in ("../x.md", "成果/../../x.md", "..", "."):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkspaceFileError):
                    WorkspaceFiles._relative(bad)

    def test_private_parts_still_rejected(self):
        for bad in (".workbuddy/x", ".git/config", ".agentbus/tmp/a.json",
                    "不要访问/x", ".ssh/id_rsa"):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkspaceFileError):
                    WorkspaceFiles._relative(bad)

    def test_device_names_still_rejected(self):
        for bad in ("CON", "成果/NUL.txt", "LPT1.md", "com3/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkspaceFileError):
                    WorkspaceFiles._relative(bad)

    def test_control_and_reserved_chars_still_rejected(self):
        for bad in ("成果/a<b.md", '成果/a"b.md', "成果/a|b.md",
                    "成果/a?b.md", "成果/a*b.md", "成果/a\x01b.md"):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkspaceFileError):
                    WorkspaceFiles._relative(bad)


class ValidPathStillAcceptedTests(unittest.TestCase):
    """验收 3：正确相对路径必须可以提交。"""

    def test_plain_relative_accepted(self):
        self.assertEqual(
            WorkspaceFiles._relative("成果/复函/20261011-回执-说明.md"),
            "成果/复函/20261011-回执-说明.md")

    def test_backslash_is_normalized(self):
        self.assertEqual(
            WorkspaceFiles._relative("成果\\复函\\x.md"),
            "成果/复函/x.md")

    def test_pathlib_accepted(self):
        self.assertEqual(
            WorkspaceFiles._relative(Path("成果/复函/x.md")),
            "成果/复函/x.md")

    def test_example_in_message_is_itself_valid(self):
        """自洽性：提示里给的示例，必须自己就能通过校验。"""
        example = "成果/复函/20261011-回执-说明.md"
        self.assertEqual(WorkspaceFiles._relative(example), example)


if __name__ == "__main__":
    unittest.main()
