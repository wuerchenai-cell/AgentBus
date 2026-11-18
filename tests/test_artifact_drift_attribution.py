# -*- coding: utf-8 -*-
"""漂移归因：`tools/artifact_drift_attribution.py` 的单测。

只读静态守卫：不跑 GUI、不跑模型、不访问网络、不改工作区（除临时目录）。
覆盖：
  * 一致条目不进入报告；后续合法改动被归因到具体叶
  * 收缩（内容疑失）/ 等长异内容 / 无人认账漂移 / 文件缺失 四种坏情况分别判出
  * 归档孪生（`.deprecated_old_version`）与交付声明逐字节一致 → 判"未丢失"
  * 两种清单格式（list / dict）都能解析；章节号能从 `§x.y` / `leaf` / `art-x.y.json` 提取
  * 总线登记的 artifacts 也计入声明源（这是归因完整性的关键）
  * 只读性、缺输入退出码 2
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

_TOOL = _ROOT / "tools" / "artifact_drift_attribution.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("artifact_drift_attribution", _TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ada = _load_tool()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _Ws:
    """构造一个最小工作区：成果/复函/manifests + 成果/任务书维护/receipts。"""

    def __init__(self, root: Path):
        self.root = root
        (root / "成果" / "复函" / "manifests").mkdir(parents=True, exist_ok=True)
        (root / "成果" / "任务书维护" / "receipts").mkdir(parents=True, exist_ok=True)

    def manifest_list(self, name: str, entries: list) -> None:
        path = self.root / "成果" / "复函" / "manifests" / name
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def manifest_dict(self, name: str, payload: dict) -> None:
        path = self.root / "成果" / "复函" / "manifests" / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def receipt(self, name: str, entries: list) -> None:
        path = self.root / "成果" / "任务书维护" / "receipts" / name
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def file(self, rel: str, payload: bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path


def _status(leaves):
    return {"schema_version": 1, "leaves": leaves}


def _leaf(section, artifacts):
    return {"section_no": section, "id": "L-" + section.replace(".", "_"), "artifacts": artifacts}


def _art(path, size, sha):
    return {"path": path, "bytes": size, "sha256": sha}


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = _Ws(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_match_is_not_reported(self):
        payload = b"same-bytes"
        path = self.ws.file("a.py", payload)
        status = _status([_leaf("1.1", [_art("a.py", len(payload), _sha(payload))])])
        report = ada.analyse(self.root, status)
        self.assertEqual([], report["rows"])
        self.assertEqual("clean", report["verdict"])

    def test_later_edit_is_attributed_to_the_leaf_that_declares_current_state(self):
        old, new = b"old-content", b"new-and-longer-content"
        self.ws.file("b.py", new)
        # 交付时声明的是旧状态；另一叶的清单声明了当前新状态
        self.ws.manifest_list("art-9.9.json", [_art("b.py", len(new), _sha(new))])
        status = _status([_leaf("7.1", [_art("b.py", len(old), _sha(old))])])
        report = ada.analyse(self.root, status)
        self.assertEqual(1, len(report["rows"]))
        row = report["rows"][0]
        self.assertEqual("attributed_later_edit", row["verdict"])
        self.assertEqual(["9.9"], row["attributed_to"])
        self.assertEqual("all_attributed", report["verdict"])

    def test_bus_registration_counts_as_a_declaration(self):
        """后续叶只在总线登记 artifacts（没有独立清单）时，也必须能归因。"""
        old, new = b"aaaa", b"bbbbbb"
        self.ws.file("c.py", new)
        status = _status([
            _leaf("1.1", [_art("c.py", len(old), _sha(old))]),
            _leaf("2.2", [_art("c.py", len(new), _sha(new))]),
        ])
        report = ada.analyse(self.root, status)
        self.assertEqual(1, len(report["rows"]))
        row = report["rows"][0]
        self.assertEqual("attributed_later_edit", row["verdict"])
        self.assertEqual(["2.2"], row["attributed_to"])

    def test_shrunk_is_flagged_as_possible_loss(self):
        old, new = b"0123456789", b"0123"
        self.ws.file("d.py", new)
        status = _status([_leaf("1.1", [_art("d.py", len(old), _sha(old))])])
        report = ada.analyse(self.root, status)
        row = report["rows"][0]
        self.assertEqual("shrunk", row["verdict"])
        self.assertEqual([], row["attributed_to"])
        self.assertEqual("needs_attention", report["verdict"])

    def test_same_size_different_content_is_its_own_class(self):
        old, new = b"aaaa", b"bbbb"
        self.ws.file("e.py", new)
        status = _status([_leaf("1.1", [_art("e.py", len(old), _sha(old))])])
        report = ada.analyse(self.root, status)
        row = report["rows"][0]
        self.assertEqual("same_size_diff_content", row["verdict"])

    def test_unattributed_drift_when_no_one_claims_current_state(self):
        old, new = b"short", b"much-longer-than-declared"
        self.ws.file("f.py", new)
        status = _status([_leaf("1.1", [_art("f.py", len(old), _sha(old))])])
        report = ada.analyse(self.root, status)
        self.assertEqual("unattributed_drift", report["rows"][0]["verdict"])

    def test_archived_twin_match_proves_preservation(self):
        payload = b"exact-archived-payload"
        sha = _sha(payload)
        # 原文件不存在，只有加缀归档的孪生，且与交付声明逐字节一致
        twin = self.ws.file("gone.py.deprecated_old_version", payload)
        status = _status([_leaf("1.4", [_art("gone.py", len(payload), sha)])])
        report = ada.analyse(self.root, status)
        row = report["rows"][0]
        self.assertEqual("archived_twin_matched", row["verdict"])
        self.assertIn("gone.py.deprecated_old_version", row["detail"])
        self.assertIn(sha[:16], row["detail"])
        self.assertEqual("all_attributed", report["verdict"])

    def test_archived_twin_mismatch_is_still_missing(self):
        declared, actual = b"declared", b"different"
        self.ws.file("gone2.py.deprecated_old_version", actual)
        status = _status([_leaf("1.4", [_art("gone2.py", len(declared), _sha(declared))])])
        report = ada.analyse(self.root, status)
        self.assertEqual("missing", report["rows"][0]["verdict"])

    def test_plain_missing_without_twin(self):
        status = _status([_leaf("1.1", [_art("nowhere.py", 10, "a" * 64)])])
        report = ada.analyse(self.root, status)
        row = report["rows"][0]
        self.assertEqual("missing", row["verdict"])
        self.assertEqual("needs_attention", report["verdict"])


class DeclarationParsingTests(unittest.TestCase):
    def test_leaf_number_from_various_declarer_forms(self):
        self.assertEqual("7.29", ada._leaf_of("manifests/art-7.29.json", None))
        self.assertEqual("10.21", ada._leaf_of("receipts/x.json", {"task": "§10.21 AUDIT-X"}))
        self.assertEqual("5.6", ada._leaf_of("receipts/x.json", {"leaf": "5.6"}))
        self.assertEqual("receipts/x.json", ada._leaf_of("receipts/x.json", {}))

    def test_both_manifest_formats_are_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Ws(Path(tmp))
            ws.manifest_list("art-1.1.json", [{"path": "a.py", "bytes": 1, "sha256": "a" * 64}])
            ws.manifest_dict("art-2.2.json", {"manifest_version": 1, "leaf": "2.2",
                                              "artifacts": [{"path": "b.py", "bytes": 2, "sha256": "b" * 64}]})
            index = ada.collect_declarations(ws.root)
            self.assertIn("a.py", index)
            self.assertIn("b.py", index)
            self.assertEqual("1.1", index["a.py"][0]["leaf"])
            self.assertEqual("2.2", index["b.py"][0]["leaf"])

    def test_backslash_paths_are_normalised(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Ws(Path(tmp))
            ws.manifest_list("art-1.1.json",
                             [{"path": "成果\\x\\y.py", "bytes": 1, "sha256": "a" * 64}])
            index = ada.collect_declarations(ws.root)
            self.assertIn("成果/x/y.py", index)


class CliTests(unittest.TestCase):
    def test_missing_status_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, ada.main(["--workspace", tmp, "--status", str(Path(tmp) / "nope.json")]))

    def test_read_only_inputs_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Ws(Path(tmp))
            payload = b"payload"
            ws.file("g.py", payload)
            entries = [_art("g.py", len(payload), _sha(payload))]
            ws.manifest_list("art-1.1.json", entries)
            status_path = Path(tmp) / "ts.json"
            status_path.write_text(json.dumps(_status([_leaf("1.1", entries)]), ensure_ascii=False),
                                   encoding="utf-8")
            before = (ws.root / "g.py").read_bytes(), status_path.read_bytes(), \
                (ws.root / "成果" / "复函" / "manifests" / "art-1.1.json").read_bytes()
            ada.main(["--workspace", str(ws.root), "--status", str(status_path), "--json"])
            after = (ws.root / "g.py").read_bytes(), status_path.read_bytes(), \
                (ws.root / "成果" / "复函" / "manifests" / "art-1.1.json").read_bytes()
            self.assertEqual(before, after)


class RealWorkspaceTests(unittest.TestCase):
    def test_real_workspace_verdict_is_consistent_with_rows(self):
        ws = next((p for p in [_ROOT, *_ROOT.parents] if (p / ".agentbus" / "config.json").is_file()), None)
        if not ws or not (ws / ".agentbus").is_dir():
            self.skipTest("未找到真实工作区，跳过")
        import subprocess
        import tempfile
        entry = _ROOT / "entry.py"
        proc = subprocess.run([sys.executable, str(entry), "tree", "status", "--workspace", str(ws)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not proc.stdout.strip():
            self.skipTest("无法取得 tree status 快照，跳过")
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "ts.json"
            status_path.write_text(proc.stdout, encoding="utf-8")
            report = ada.analyse(ws, json.loads(proc.stdout))
        # 只断言结构与自洽，不断言具体数量（项目在演进）
        self.assertIn(report["verdict"], ("clean", "all_attributed", "needs_attention"))
        bad = [r for r in report["rows"] if r["verdict"] in
               ("shrunk", "same_size_diff_content", "unattributed_drift")]
        self.assertEqual(len(report["rows"]) == 0, report["verdict"] == "clean")
        if bad:
            self.assertEqual("needs_attention", report["verdict"],
                             "存在收缩/等长改写/无人认账时判定必须升级")
        else:
            self.assertIn(report["verdict"], ("clean", "all_attributed"))
        for row in report["rows"]:
            self.assertIn(row["verdict"],
                          ("attributed_later_edit", "archived_twin_matched", "missing",
                           "shrunk", "same_size_diff_content", "unattributed_drift"))
            if row["verdict"] == "attributed_later_edit":
                self.assertTrue(row.get("attributed_to"), "归因为后续改动必须指名具体叶")


if __name__ == "__main__":
    unittest.main()
