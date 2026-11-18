# -*- coding: utf-8 -*-
"""§3.4 独立监督机制 · 对抗审计测试（已接入 integration 回归套件）。

来源：OpenCode（平级成员），2026-10-11，任务 m-tree-e0d2b89207d6f0dc62a1381f。
对抗审计实锤发现 CHECKS-1 任意命令执行与破坏漏洞；经 Antigravity 实施白名单防线加固后，
红例已全部翻转为严格的 assertRaises 拦截断言，作为永久回归防线纳入官方测试套件。
"""
from __future__ import annotations
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _arg_value(flag: str):
    argv = list(sys.argv)
    value = None
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            value = argv[index + 1]
            del argv[index:index + 2]
            break
        if item.startswith(flag + "="):
            value = item.split("=", 1)[1]
            del argv[index]
            break
    sys.argv = argv
    return value


def resolve_runtime():
    raw = _arg_value("--target-runtime") or os.environ.get("AGENTBUS_TEST_RUNTIME") or str(HERE.parent)
    root = os.path.abspath(os.path.expanduser(str(raw).strip()))
    if not os.path.isfile(os.path.join(root, "agentbus", "__init__.py")):
        raise unittest.SkipTest("PENDING：指定运行时不含 agentbus/__init__.py，runtime=%s" % root)
    return root


_PENDING_REASON = ""
RUNTIME = None
try:
    RUNTIME = resolve_runtime()
except unittest.SkipTest as _exc:
    _PENDING_REASON = str(_exc)

if RUNTIME is not None:
    if RUNTIME not in sys.path:
        sys.path.insert(0, RUNTIME)
    try:
        from agentbus import supervision as S  # noqa: E402
    except ImportError as _exc:
        RUNTIME = None
        _PENDING_REASON = ("PENDING：指定运行时缺少 supervision（%s）；runtime=%s" % (_exc, RUNTIME))


def setUpModule() -> None:
    if RUNTIME is None:
        raise unittest.SkipTest(_PENDING_REASON)


class AmbientBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="adv-")
        self.workspace = Path(self._tmp.name) / "project"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()


# ==========================================================================
# 绿例：已确认并修复的命令白名单防线（CHECKS 任意程序漏洞已封堵）
# ==========================================================================
class KnownVulnerability_ChecksAllowlist(AmbientBase):
    """CHECKS-1 / CHECKS-2：argv[0] 已建立白名单，严格拦截任意破坏程序。"""

    def test_arbitrary_executable_is_rejected(self):
        """漏洞已修复：未授权与危险可执行名被严格拦截，允许受控测试运行器。"""
        for program in ("cmd", "powershell", "curl", "del", "rm", "shutdown", "bash"):
            check = {"name": "c", "argv": [program, "arg"], "timeout_seconds": 5}
            with self.subTest(argv0=program):
                with self.assertRaises(S.SupervisionError):
                    S._validated_checks([check], required=True)
        for program in (sys.executable, "python", "node"):
            check = {"name": "c", "argv": [program, "-c", "pass"], "timeout_seconds": 5}
            with self.subTest(argv0=program):
                res = S._validated_checks([check], required=True)
                self.assertEqual(len(res), 1)

    def test_destructive_command_is_rejected_and_file_preserved(self):
        """漏洞已修复：`cmd /c del` 被拒，受害文件得以保全。"""
        victim = self.workspace / "IMPORTANT.md"
        victim.write_text("原始内容\n", encoding="utf-8")
        check = {"name": "c", "argv": ["cmd", "/c", "del", "IMPORTANT.md"],
                 "timeout_seconds": 20}
        with self.assertRaises(S.SupervisionError):
            S._validated_checks([check], required=True)
        self.assertTrue(victim.exists(), "受害文件必须完好无损")

    def test_shell_metacharacters_are_not_executed(self):
        """对照绿例：shell=False 生效，元字符不触发命令。"""
        check = {"name": "c",
                 "argv": [sys.executable, "-c", "pass", "; echo x > EVIL.txt"],
                 "timeout_seconds": 20}
        normalized = S._validated_checks([check], required=True)
        S.run_allowlisted_checks(self.workspace, normalized)
        self.assertFalse((self.workspace / "EVIL.txt").exists(),
                         "shell 元字符不得被当命令执行")

    def test_environment_does_not_leak_host_credentials(self):
        """对照绿例：环境白名单不含 API key。"""
        os.environ["ADVERSARY_API_KEY"] = "should-not-leak"
        try:
            check = {"name": "c",
                     "argv": [sys.executable, "-c",
                              "import os,sys;sys.stdout.write(os.environ.get('ADVERSARY_API_KEY','absent'))"],
                     "timeout_seconds": 20}
            normalized = S._validated_checks([check], required=True)
            out = S.run_allowlisted_checks(self.workspace, normalized)[0]["stdout"]
            self.assertNotIn("should-not-leak", out)
        finally:
            del os.environ["ADVERSARY_API_KEY"]

    def test_check_timeout_is_bounded(self):
        check = {"name": "c", "argv": [sys.executable, "-c", "pass"],
                 "timeout_seconds": 0}
        with self.assertRaises(S.SupervisionError):
            S._validated_checks([check], required=True)


# ==========================================================================
# 绿例：确认守住的防线
# ==========================================================================
class ConfirmedDefenses(AmbientBase):
    """确认守住的防线——这些不得在报告中写成漏洞。"""

    def _artifact(self, rel="out/r.md", text="# 原始\n"):
        import hashlib
        target = self.workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = text.encode("utf-8")
        target.write_bytes(raw)
        return {"path": rel, "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}

    def test_toctou_is_caught(self):
        entry = self._artifact()
        first = S.verify_artifacts(self.workspace, [entry])
        self.assertEqual(first["verdict"], "pass")
        (self.workspace / "out" / "r.md").write_text("# 篡改\n", encoding="utf-8")
        second = S.verify_artifacts(self.workspace, [entry])
        self.assertEqual(second["verdict"], "fail")
        self.assertIn("CONTENT_MISMATCH", [i["code"] for i in second["issues"]])

    def test_path_traversal_is_rejected(self):
        entry = {"path": "../escape.md", "bytes": 1, "sha256": "a" * 64}
        result = S.verify_artifacts(self.workspace, [entry])
        self.assertEqual(result["verdict"], "fail")

    def test_placeholder_scan_flags_question_marks(self):
        self._artifact("out/q.md", "标题 ???\nTODO\n")
        findings = S.scan_text_artifacts(self.workspace, [self._artifact("out/q.md", "标题 ???\nTODO\n")])
        codes = {f["code"] for f in findings}
        self.assertIn("QUESTION_MARKS", codes)
        self.assertTrue(all(f.get("is_quality_proof") is False for f in findings))

    def test_binary_artifact_is_not_treated_as_verified_text(self):
        import hashlib
        target = self.workspace / "out.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = b"\xff\xfe\x00\x01binary"
        target.write_bytes(raw)
        entry = {"path": "out.bin", "bytes": len(raw),
                 "sha256": hashlib.sha256(raw).hexdigest()}
        result = S.verify_artifacts(self.workspace, [entry])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["artifacts"][0]["content_kind"], "binary")

    def test_null_byte_in_argv_is_rejected(self):
        check = {"name": "c", "argv": ["cmd\0/x"], "timeout_seconds": 5}
        with self.assertRaises(S.SupervisionError):
            S._validated_checks([check], required=True)


# ==========================================================================
# 未验证：本席明确无法完成的部分
# ==========================================================================
class UnverifiedByThisAuthor(AmbientBase):
    """未验证项必须显式标记，不得当成通过或漏洞。"""

    def test_end_to_end_forged_check_cannot_be_completed_here(self):
        try:
            from agentbus.bus import Bus  # noqa: F401
        except ImportError as exc:
            self.skipTest("PENDING：候选运行时缺 bus 栅栏接口：%s" % exc)
        self.skipTest("未验证：需 integration 侧 bus.py 才能坐实全链路")


if __name__ == "__main__":
    unittest.main(verbosity=2)
