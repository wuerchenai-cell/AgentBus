# -*- coding: utf-8 -*-
"""§10.21 正式入口唯一真源门禁：`tools/launcher_truth_check.py` 的单测。

只读静态守卫：不执行 GUI、不跑模型、不访问网络、不写工作区（除临时目录对账用例）。
覆盖：
  * `launcher.mode` 两种合法取值各自的必备字段；非法取值判红
  * 正式程序身份必须可读：顶层（+ `formal_binary_path`）/ `launcher.formal_binary`，两处冲突判红
  * 反例一：`frozen` 缺 `sha256`（及缺 `bytes`）必须判红
  * 反例二：`source` 且全项目无处登记 EXE 身份必须判红
  * 铁律一致性：AGENTS.md 铁律所指程序必须被登记，路径基名不符判红
  * `--verify-files` 与磁盘对账：摘要不符判红；文件缺失 = 不可核验（`--require-files` 才判红）
  * 只读性、`--strict` 退出码、输入缺失退出码 2
  * 对真实工作区逐字判定（结论由文件自身推出，不写死）
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_TOOL = _ROOT / "tools" / "launcher_truth_check.py"

_GOOD_SHA = "a" * 64
_OTHER_SHA = "b" * 64

AGENTS_MD = """# 契约

## 产出目录

- 历史构建产物 `成果/原型/legacy-old.exe` 仅作留档，不是正式程序。

## 唯一正式执行程序铁律（2026-10-13）

- **全项目唯一正式出厂程序**：`成果/AgentBus.exe`（单文件便携版；字节数与 SHA256 摘要以
  `.agentbus/workflow.json` 中的 `launcher` 登记值为唯一真源）。
- **历史版本全面封存**：散落旧版已加缀 `.deprecated_old_version`。

## 下一节

- 这里提到 `别的正式程序.exe` 不应被门禁当成铁律所指。
"""


def _load_tool():
    spec = importlib.util.spec_from_file_location("launcher_truth_check", _TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ltc = _load_tool()


def _wf(launcher):
    return {"schema_version": 1, "launcher": launcher}


def _codes(report):
    return [e["code"] for e in report["errors"]]


class DoctrineExtractionTests(unittest.TestCase):
    def test_only_the_doctrine_section_is_scanned(self):
        """铁律小节之外的 *.exe 不得被当成铁律所指的正式程序。"""
        declared = ltc.declared_formal_programs(AGENTS_MD)
        self.assertEqual(["成果/AgentBus.exe"], declared)
        self.assertNotIn("成果/原型/legacy-old.exe", declared)
        self.assertNotIn("别的正式程序.exe", declared)

    def test_missing_doctrine_yields_empty_not_crash(self):
        self.assertEqual([], ltc.declared_formal_programs("# 空文档\n"))
        self.assertEqual([], ltc.declared_formal_programs(""))


class ModeFieldTests(unittest.TestCase):
    def test_frozen_full_registration_passes(self):
        report = ltc.check(_wf({"mode": "frozen", "bytes": 167559460, "sha256": _GOOD_SHA}), AGENTS_MD)
        self.assertEqual([], report["errors"])
        self.assertEqual("pass", report["verdict"])

    def test_frozen_missing_sha256_is_red(self):
        """反例：frozen 缺 sha256 必须判红。"""
        report = ltc.check(_wf({"mode": "frozen", "bytes": 167559460}), AGENTS_MD)
        self.assertIn("formal_identity_missing_sha256", _codes(report))
        self.assertEqual("contradiction", report["verdict"])

    def test_frozen_missing_bytes_is_red(self):
        report = ltc.check(_wf({"mode": "frozen", "sha256": _GOOD_SHA}), AGENTS_MD)
        self.assertIn("formal_identity_missing_bytes", _codes(report))
        self.assertEqual("contradiction", report["verdict"])

    def test_source_missing_python_is_red(self):
        report = ltc.check(_wf({"mode": "source", "entrypoint": "e.py",
                                "bytes": 1, "sha256": _GOOD_SHA}), AGENTS_MD)
        self.assertIn("source_missing_python", _codes(report))

    def test_source_missing_entrypoint_is_red(self):
        report = ltc.check(_wf({"mode": "source", "python": "py.exe",
                                "bytes": 1, "sha256": _GOOD_SHA}), AGENTS_MD)
        self.assertIn("source_missing_entrypoint", _codes(report))

    def test_invalid_mode_is_red(self):
        report = ltc.check(_wf({"mode": "hybrid", "bytes": 1, "sha256": _GOOD_SHA}), AGENTS_MD)
        self.assertIn("launcher_mode_invalid", _codes(report))
        self.assertEqual("contradiction", report["verdict"])

    def test_missing_launcher_is_red(self):
        report = ltc.check({"schema_version": 1}, AGENTS_MD)
        self.assertEqual(["launcher_missing"], _codes(report))


class FormalBinaryIdentityTests(unittest.TestCase):
    def test_source_without_any_identity_is_red(self):
        """反例：source 合法字段齐备，但全项目无处登记 EXE 身份 → 判红。"""
        report = ltc.check(_wf({"mode": "source", "python": "py.exe",
                                "entrypoint": "entry.py"}), AGENTS_MD)
        self.assertEqual(2, _codes(report).count("formal_binary_identity_unregistered"))
        self.assertEqual("contradiction", report["verdict"])

    def test_source_with_top_level_identity_binds_to_doctrine_program(self):
        """顶层登记 + 铁律只声明一个程序 → 自动绑定，可通过。"""
        report = ltc.check(_wf({"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                                "bytes": 167559460, "sha256": _GOOD_SHA}), AGENTS_MD)
        self.assertEqual([], report["errors"])
        reg = report["registered_identities"][0]
        self.assertEqual("成果/AgentBus.exe", reg["path"])
        self.assertEqual("pass", report["verdict"])

    def test_source_with_nested_identity_passes(self):
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                    "formal_binary": {"path": "成果/AgentBus.exe",
                                      "bytes": 167559460, "sha256": _GOOD_SHA}}
        report = ltc.check(_wf(launcher), AGENTS_MD)
        self.assertEqual([], report["errors"])
        self.assertEqual(["formal_binary"], [r["source"] for r in report["registered_identities"]])

    def test_top_level_identity_with_explicit_path_passes(self):
        """顶层身份 + `formal_binary_path` 显式声明文件：无需嵌套重复登记。"""
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                    "formal_binary_path": "成果/AgentBus.exe",
                    "bytes": 167559460, "sha256": _GOOD_SHA}
        report = ltc.check(_wf(launcher), AGENTS_MD)
        self.assertEqual([], report["errors"])
        self.assertEqual("成果/AgentBus.exe", report["registered_identities"][0]["path"])
        self.assertEqual("pass", report["verdict"])

    def test_top_level_identity_with_mismatched_path_is_red(self):
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                    "formal_binary_path": "成果/别的程序.exe",
                    "bytes": 167559460, "sha256": _GOOD_SHA}
        report = ltc.check(_wf(launcher), AGENTS_MD)
        self.assertIn("formal_binary_path_mismatch", _codes(report))
        self.assertIn("formal_binary_identity_unregistered", _codes(report))

    def test_formal_binary_path_must_be_nonempty_string(self):
        launcher = {"mode": "frozen", "bytes": 167559460, "sha256": _GOOD_SHA,
                    "formal_binary_path": ""}
        report = ltc.check(_wf(launcher), AGENTS_MD)
        self.assertIn("formal_binary_path_invalid", _codes(report))

    def test_nested_identity_without_path_is_red(self):
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                    "formal_binary": {"bytes": 167559460, "sha256": _GOOD_SHA}}
        report = ltc.check(_wf(launcher), AGENTS_MD)
        self.assertIn("formal_binary_missing_path", _codes(report))

    def test_duplicate_identity_conflict_is_red(self):
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                    "bytes": 167559460, "sha256": _GOOD_SHA,
                    "formal_binary": {"path": "成果/AgentBus.exe",
                                      "bytes": 167559460, "sha256": _OTHER_SHA}}
        report = ltc.check(_wf(launcher), AGENTS_MD)
        self.assertIn("formal_identity_conflict", _codes(report))

    def test_duplicate_identity_agreement_passes(self):
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                    "bytes": 167559460, "sha256": _GOOD_SHA,
                    "formal_binary": {"path": "成果/AgentBus.exe",
                                      "bytes": 167559460, "sha256": _GOOD_SHA}}
        report = ltc.check(_wf(launcher), AGENTS_MD)
        self.assertEqual([], report["errors"])
        self.assertEqual(2, len(report["registered_identities"]))

    def test_doctrine_path_mismatch_is_red(self):
        """登记声明的正式程序与铁律所指并非同一文件 → 判红。"""
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                    "formal_binary": {"path": "成果/另一個程序.exe",
                                      "bytes": 167559460, "sha256": _GOOD_SHA}}
        report = ltc.check(_wf(launcher), AGENTS_MD)
        self.assertIn("formal_binary_path_mismatch", _codes(report))
        self.assertIn("formal_binary_identity_unregistered", _codes(report))

    def test_identity_must_be_real_numbers(self):
        """bytes 必须是正整数、sha256 必须是 64 位十六进制——不接受字符串或短摘要。"""
        report = ltc.check(_wf({"mode": "frozen", "bytes": "167559460", "sha256": "deadbeef"}), AGENTS_MD)
        self.assertIn("formal_identity_missing_bytes", _codes(report))
        self.assertIn("formal_identity_missing_sha256", _codes(report))


class FileVerificationTests(unittest.TestCase):
    def _workspace(self, root: Path, bytes_payload: bytes, launcher_bytes: int, sha: str):
        (root / ".agentbus").mkdir(parents=True, exist_ok=True)
        (root / "成果").mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
        (root / "成果" / "AgentBus.exe").write_bytes(bytes_payload)
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py",
                    "formal_binary": {"path": "成果/AgentBus.exe",
                                      "bytes": launcher_bytes, "sha256": sha}}
        (root / ".agentbus" / "workflow.json").write_text(
            json.dumps(_wf(launcher), ensure_ascii=False), encoding="utf-8")
        return launcher

    def test_digest_mismatch_is_red(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root, b"payload", 7, _GOOD_SHA)
            report = ltc.check(json.loads((root / ".agentbus" / "workflow.json").read_text(encoding="utf-8")),
                               AGENTS_MD, root, verify_files=True)
            self.assertIn("formal_binary_digest_mismatch", _codes(report))
            self.assertEqual("contradiction", report["verdict"])

    def test_matching_digest_passes(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = b"payload-bytes"
            sha = hashlib.sha256(payload).hexdigest()
            self._workspace(root, payload, len(payload), sha)
            report = ltc.check(json.loads((root / ".agentbus" / "workflow.json").read_text(encoding="utf-8")),
                               AGENTS_MD, root, verify_files=True)
            self.assertEqual([], report["errors"])
            self.assertIn("formal_binary_verified", [n["code"] for n in report["notes"]])

    def test_missing_file_is_unverifiable_not_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root, b"x", 1, _GOOD_SHA)
            (root / "成果" / "AgentBus.exe").unlink()
            report = ltc.check(json.loads((root / ".agentbus" / "workflow.json").read_text(encoding="utf-8")),
                               AGENTS_MD, root, verify_files=True)
            self.assertEqual([], report["errors"])
            self.assertEqual("pass", report["verdict"])
            self.assertEqual(1, len(report["unverifiable"]))

    def test_require_files_upgrades_missing_to_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._workspace(root, b"x", 1, _GOOD_SHA)
            (root / "成果" / "AgentBus.exe").unlink()
            report = ltc.check(json.loads((root / ".agentbus" / "workflow.json").read_text(encoding="utf-8")),
                               AGENTS_MD, root, verify_files=True, require_files=True)
            self.assertIn("formal_binary_file_missing", _codes(report))


class ReadOnlyAndCliTests(unittest.TestCase):
    def test_check_does_not_modify_inputs(self):
        launcher = {"mode": "source", "python": "py.exe", "entrypoint": "entry.py"}
        workflow = _wf(launcher)
        before = json.dumps(workflow, sort_keys=True, ensure_ascii=False)
        ltc.check(workflow, AGENTS_MD)
        self.assertEqual(before, json.dumps(workflow, sort_keys=True, ensure_ascii=False))

    def test_strict_exit_code_nonzero_on_contradiction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agentbus").mkdir()
            (root / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
            (root / ".agentbus" / "workflow.json").write_text(
                json.dumps(_wf({"mode": "source", "python": "py.exe", "entrypoint": "e.py"})),
                encoding="utf-8")
            self.assertEqual(1, ltc.main(["--workspace", str(root), "--strict"]))

    def test_strict_exit_code_zero_on_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agentbus").mkdir()
            (root / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
            (root / ".agentbus" / "workflow.json").write_text(
                json.dumps(_wf({"mode": "frozen", "bytes": 167559460, "sha256": _GOOD_SHA})),
                encoding="utf-8")
            self.assertEqual(0, ltc.main(["--workspace", str(root), "--strict"]))

    def test_missing_workflow_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, ltc.main(["--workspace", tmp]))

    def test_render_reports_verdict_and_codes(self):
        report = ltc.check(_wf({"mode": "source", "python": "py.exe", "entrypoint": "e.py"}), AGENTS_MD)
        text = ltc.render(report)
        self.assertIn("formal_binary_identity_unregistered", text)
        self.assertIn("contradiction", text)


from project_paths import host_project_root


class RealWorkspaceTests(unittest.TestCase):
    """对真实工作区逐字判定：结论由文件自身推出，不写死期望值。"""

    def _paths(self):
        ws = host_project_root()
        if ws is None:
            self.skipTest('Explicit host launcher audit not performed in portable scope')
        return ws, ws / ".agentbus" / "workflow.json", ws / "AGENTS.md"

    def test_real_workspace_is_consistent_with_its_own_files(self):
        ws, wf_path, md_path = self._paths()
        if not (wf_path.is_file() and md_path.is_file()):
            self.skipTest("未找到真实 .agentbus/workflow.json 或 AGENTS.md，跳过")
        workflow = json.loads(wf_path.read_text(encoding="utf-8"))
        md_text = md_path.read_text(encoding="utf-8", errors="replace")

        report = ltc.check(workflow, md_text)

        # 1) 铁律所指程序必须被提取到（这是本叶要守住的事实）
        self.assertIn("成果/AgentBus.exe", report["doctrine_declared_programs"])
        # 2) mode 如实反映文件
        self.assertEqual(workflow.get("launcher", {}).get("mode"), report["launcher_mode"])
        # 3) 判定与"登记里到底有没有正式程序身份"完全一致（不写死 pass/contradiction）
        launcher = workflow.get("launcher", {})
        has_identity = ("bytes" in launcher and "sha256" in launcher) or isinstance(
            launcher.get("formal_binary"), dict)
        expected = "pass" if has_identity else "contradiction"
        self.assertEqual(expected, report["verdict"])

    def test_real_workspace_file_verification_if_binary_present(self):
        ws, wf_path, md_path = self._paths()
        exe = ws / "成果" / "AgentBus.exe"
        if not (wf_path.is_file() and md_path.is_file() and exe.is_file()):
            self.skipTest("真实工作区缺 workflow.json / AGENTS.md / AgentBus.exe，跳过")
        report = ltc.check(json.loads(wf_path.read_text(encoding="utf-8")),
                           md_path.read_text(encoding="utf-8", errors="replace"), ws, verify_files=True)
        # 只要登记里存在正式程序身份，就必须与磁盘一致（否则判红）
        if report["registered_identities"]:
            self.assertNotIn("formal_binary_digest_mismatch", _codes(report))


if __name__ == "__main__":
    unittest.main()
