# -*- coding: utf-8 -*-
"""登记一致性门禁：`tools/host_file_registration_check.py` 的单测。

只读静态守卫：不跑 GUI、不跑模型、不访问网络、不改工作区（除临时目录）。
覆盖：
  * 全部一致 → pass；launcher 判定被复用（非复制）
  * 登记过期（实测场景：SKILL.md 被改写）→ `stale_registration` 并给出登记值与实测值
  * 登记文件缺失 → `missing_file`
  * **退役文件的正确语义**：`retired_files` 是"曾被登记、本次不再写"的审计留痕，
    文件**应当仍存在**；不存在只记 note；存在但内容与登记不符才算脱节
  * launcher 非法 mode 会经本门禁一并判红（复用 §10.21）
  * 缺 workflow.json → unverifiable；`--strict` 退出码；只读性
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.project_paths import host_project_root

_TOOL = _ROOT / "tools" / "host_file_registration_check.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("host_file_registration_check", _TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


hfrc = _load_tool()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _Ws:
    def __init__(self, root: Path):
        self.root = root
        (root / ".agentbus").mkdir(parents=True, exist_ok=True)
        (root / "AGENTS.md").write_text(
            "## 唯一正式执行程序铁律（2026-10-13）\n\n- 正式程序：`成果/AgentBus.exe`。\n",
            encoding="utf-8")

    def file(self, rel: str, payload: bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def workflow(self, payload: dict) -> None:
        (self.root / ".agentbus" / "workflow.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _base(ws: _Ws) -> dict:
    a = ws.file("a.md", b"alpha")
    b = ws.file("b.md", b"beta")
    retired = ws.file("gone.md", b"retired-content")
    ws.workflow({
        "owner": "agentbus", "schema_version": 1,
        "launcher": {"mode": "frozen", "bytes": 167559460, "sha256": "a" * 64,
                     "formal_binary_path": "成果/AgentBus.exe"},
        "files": {"a.md": _digest(b"alpha"), "b.md": _digest(b"beta")},
        "retired_files": {"gone.md": _digest(b"retired-content")},
    })
    return {"a": a, "b": b, "retired": retired}


class RegistrationCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = _Ws(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_consistent_passes(self):
        _base(self.ws)
        report = hfrc.check(self.root, self.ws.root.joinpath("AGENTS.md").read_text(encoding="utf-8"))
        self.assertEqual([], report["errors"])
        self.assertEqual("pass", report["verdict"])
        self.assertEqual(2, report["checked_files"])
        self.assertEqual(1, report["checked_retired"])

    def test_stale_registration_is_named_with_both_digests(self):
        """实测场景：宿主文件被改写后登记静默过期 → 必须给出登记值与实测值。"""
        _base(self.ws)
        self.ws.file("b.md", b"beta-edited-and-longer")
        report = hfrc.check(self.root, "")
        codes = [e["code"] for e in report["errors"]]
        self.assertIn("stale_registration", codes)
        err = next(e for e in report["errors"] if e["code"] == "stale_registration")
        self.assertEqual("b.md", err["path"])
        self.assertEqual(_digest(b"beta"), err["registered"])
        self.assertEqual(_digest(b"beta-edited-and-longer"), err["actual"])
        self.assertEqual("contradiction", report["verdict"])

    def test_missing_registered_file(self):
        _base(self.ws)
        (self.root / "a.md").unlink()
        report = hfrc.check(self.root, "")
        self.assertIn("missing_file", [e["code"] for e in report["errors"]])

    def test_retired_file_absent_is_only_a_note(self):
        """退役留痕文件不在磁盘上不算错误（没有可核验对象）。"""
        _base(self.ws)
        (self.root / "gone.md").unlink()
        report = hfrc.check(self.root, "")
        self.assertEqual([], report["errors"])
        self.assertIn("retired_absent", [n["code"] for n in report["notes"]])

    def test_retired_file_preserved_and_matching_passes(self):
        """退役文件**应当仍存在**且内容与登记一致（project_workflow 明确"已保留"）。"""
        _base(self.ws)
        report = hfrc.check(self.root, "")
        self.assertIn("retired_preserved", [n["code"] for n in report["notes"]])
        self.assertEqual([], report["errors"])

    def test_retired_file_drift_is_an_error(self):
        _base(self.ws)
        self.ws.file("gone.md", b"retired-content-modified")
        report = hfrc.check(self.root, "")
        self.assertIn("retired_file_drift", [e["code"] for e in report["errors"]])

    def test_launcher_defect_surfaces_through_this_gate(self):
        """launcher 非法 mode → 本门禁一并判红（复用 §10.21，不复制规则）。"""
        _base(self.ws)
        wf = json.loads((self.root / ".agentbus" / "workflow.json").read_text(encoding="utf-8"))
        wf["launcher"]["mode"] = "hybrid"
        self.ws.workflow(wf)
        report = hfrc.check(self.root, "")
        codes = [e["code"] for e in report["errors"]]
        self.assertTrue(any(c.startswith("launcher:") for c in codes), codes)
        self.assertEqual("contradiction", report["verdict"])

    def test_missing_workflow_is_unverifiable(self):
        report = hfrc.check(self.root, "")
        self.assertEqual("unverifiable", report["verdict"])
        self.assertIn("error", report)

    def test_strict_exit_codes(self):
        _base(self.ws)
        self.assertEqual(0, hfrc.main(["--workspace", str(self.root), "--strict"]))
        self.ws.file("a.md", b"changed")
        self.assertEqual(1, hfrc.main(["--workspace", str(self.root), "--strict"]))
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(2, hfrc.main(["--workspace", empty, "--strict"]))

    def test_read_only(self):
        _base(self.ws)
        before = {p.name: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        hfrc.main(["--workspace", str(self.root), "--json"])
        after = {p.name: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_render_mentions_codes_and_verdict(self):
        _base(self.ws)
        self.ws.file("b.md", b"beta-edited-and-longer")
        text = hfrc.render(hfrc.check(self.root, ""))
        self.assertIn("stale_registration", text)
        self.assertIn("contradiction", text)


class RealWorkspaceTests(unittest.TestCase):
    def test_real_workspace_gate_runs_and_is_self_consistent(self):
        ws = host_project_root()
        if ws is None:
            self.skipTest("Real project registration requires explicit host scope")
        if not (ws / ".agentbus" / "workflow.json").is_file():
            self.skipTest("未找到真实 .agentbus/workflow.json，跳过")
        before = (ws / ".agentbus" / "workflow.json").read_bytes()
        report = hfrc.check(ws)
        self.assertIn(report["verdict"], ("pass", "contradiction", "unverifiable"))
        self.assertEqual(before, (ws / ".agentbus" / "workflow.json").read_bytes(),
                         "门禁不得改动 workflow.json")
        if report["verdict"] == "contradiction":
            self.assertTrue(report["errors"], "判红必须给出具体条目")


if __name__ == "__main__":
    unittest.main()
