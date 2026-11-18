# -*- coding: utf-8 -*-
"""共享文件爆炸半径：`tools/shared_file_blast_radius.py` 的单测。

只读静态守卫：不跑 GUI、不跑模型、不访问网络、不改工作区（除临时目录）。
覆盖：
  * fan-in 统计（多少叶/清单/回执登记了同一路径）
  * 逐条声明的 match / mismatch / file_missing 判定
  * `--all` 的共享文件排名（按 fan-in 降序，只收 fan-in ≥ 2）
  * 复用 `artifact_drift_attribution` 的收集函数（不复制声明源逻辑）
  * 缺输入退出码 2；只读性
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

_TOOL = _ROOT / "tools" / "shared_file_blast_radius.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("shared_file_blast_radius", _TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sbr = _load_tool()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _Ws:
    def __init__(self, root: Path):
        self.root = root
        (root / "成果" / "复函" / "manifests").mkdir(parents=True, exist_ok=True)
        (root / "成果" / "任务书维护" / "receipts").mkdir(parents=True, exist_ok=True)
        (root / ".agentbus").mkdir(parents=True, exist_ok=True)

    def file(self, rel: str, payload: bytes) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def manifest_list(self, name: str, entries: list) -> None:
        (self.root / "成果" / "复函" / "manifests" / name).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def receipt(self, name: str, entries: list) -> None:
        (self.root / "成果" / "任务书维护" / "receipts" / name).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    def status(self, leaves) -> None:
        (self.root / ".agentbus" / "task-tree.json").write_text(
            json.dumps({"schema_version": 1, "leaves": leaves}, ensure_ascii=False), encoding="utf-8")


class BlastRadiusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = _Ws(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fan_in_counts_distinct_leaves_and_flags_mismatch(self):
        current = b"current-content"
        self.ws.file("shared.py", current)
        # 三处声明：一片叶的总线登记 + 一份清单 + 一份回执；两份对得上、一份过期
        self.ws.status([{"section_no": "7.1", "artifacts": [
            {"path": "shared.py", "bytes": len(current), "sha256": _sha(current)}]}])
        self.ws.manifest_list("art-9.9.json", [
            {"path": "shared.py", "bytes": len(current), "sha256": _sha(current)}])
        self.ws.receipt("X_ARTIFACTS.json", [
            {"path": "shared.py", "bytes": 3, "sha256": "a" * 64}])
        index = sbr.build_index(self.root, {"leaves": [
            {"section_no": "7.1", "artifacts": [
                {"path": "shared.py", "bytes": len(current), "sha256": _sha(current)}]}]})
        report = sbr.analyse(self.root, index, "shared.py")
        self.assertEqual(3, len(report["declarations"]))
        self.assertEqual(1, report["mismatched"])
        self.assertEqual("already_broken", report["verdict"])
        states = sorted(r["state"] for r in report["declarations"])
        self.assertEqual(["match", "match", "mismatch"], states)

    def test_clean_when_all_declarations_match(self):
        payload = b"stable"
        self.ws.file("solo.py", payload)
        self.ws.status([{"section_no": "1.1", "artifacts": [
            {"path": "solo.py", "bytes": len(payload), "sha256": _sha(payload)}]}])
        index = sbr.build_index(self.root, {"leaves": [
            {"section_no": "1.1", "artifacts": [
                {"path": "solo.py", "bytes": len(payload), "sha256": _sha(payload)}]}]})
        report = sbr.analyse(self.root, index, "solo.py")
        self.assertEqual(0, report["mismatched"])
        self.assertEqual("clean", report["verdict"])

    def test_missing_file_marks_every_declaration(self):
        self.ws.status([{"section_no": "1.1", "artifacts": [
            {"path": "gone.py", "bytes": 5, "sha256": "b" * 64}]}])
        index = sbr.build_index(self.root, {"leaves": [
            {"section_no": "1.1", "artifacts": [
                {"path": "gone.py", "bytes": 5, "sha256": "b" * 64}]}]})
        report = sbr.analyse(self.root, index, "gone.py")
        self.assertFalse(report["exists_on_disk"])
        self.assertEqual(["file_missing"], [r["state"] for r in report["declarations"]])
        self.assertEqual("clean", report["verdict"], "文件缺失不算 mismatch（由漂移归因负责判缺失）")

    def test_backslash_paths_are_normalised(self):
        payload = b"x"
        self.ws.file("a/b.py", payload)
        index = sbr.build_index(self.root, {"leaves": [
            {"section_no": "1.1", "artifacts": [
                {"path": "a\\b.py", "bytes": len(payload), "sha256": _sha(payload)}]}]})
        self.assertIn("a/b.py", index)

    def test_shared_ranking_only_lists_multi_leaf_files(self):
        payload = b"shared"
        self.ws.file("hot.py", payload)
        self.ws.file("cold.py", payload)
        leaves = [
            {"section_no": "1.1", "artifacts": [{"path": "hot.py", "bytes": len(payload), "sha256": _sha(payload)}]},
            {"section_no": "1.2", "artifacts": [{"path": "hot.py", "bytes": len(payload), "sha256": _sha(payload)}]},
            {"section_no": "1.3", "artifacts": [{"path": "cold.py", "bytes": len(payload), "sha256": _sha(payload)}]},
        ]
        index = sbr.build_index(self.root, {"leaves": leaves})
        rows = sbr.shared_ranking(index, top=10)
        paths = [r["path"] for r in rows]
        self.assertEqual(["hot.py"], paths, "只有被 ≥2 片叶登记的才叫共享文件")
        self.assertEqual(2, rows[0]["fan_in_leaves"])

    def test_ranking_is_sorted_by_fan_in_desc(self):
        payload = b"p"
        self.ws.file("a.py", payload)
        self.ws.file("b.py", payload)
        leaves = [{"section_no": s, "artifacts": [
            {"path": "a.py", "bytes": len(payload), "sha256": _sha(payload)}]} for s in ("1.1", "1.2")]
        leaves += [{"section_no": "2.1", "artifacts": [
            {"path": "b.py", "bytes": len(payload), "sha256": _sha(payload)}]}]
        index = sbr.build_index(self.root, {"leaves": leaves})
        rows = sbr.shared_ranking(index, top=10)
        self.assertEqual(2, rows[0]["fan_in_leaves"])
        self.assertEqual("a.py", rows[0]["path"])


class CliTests(unittest.TestCase):
    def test_missing_status_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(2, sbr.main(["--workspace", tmp, "--path", "x.py"]))

    def test_no_path_and_no_all_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".agentbus").mkdir()
            (Path(tmp) / ".agentbus" / "task-tree.json").write_text(
                json.dumps({"schema_version": 1, "leaves": []}), encoding="utf-8")
            self.assertEqual(2, sbr.main(["--workspace", tmp]))

    def test_all_mode_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".agentbus").mkdir()
            (Path(tmp) / ".agentbus" / "task-tree.json").write_text(
                json.dumps({"schema_version": 1, "leaves": []}), encoding="utf-8")
            self.assertEqual(0, sbr.main(["--workspace", tmp, "--all"]))

    def test_strict_exits_one_when_already_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Ws(Path(tmp))
            ws.file("f.py", b"now")
            ws.status([{"section_no": "1.1", "artifacts": [
                {"path": "f.py", "bytes": 99, "sha256": "c" * 64}]}])
            self.assertEqual(1, sbr.main(["--workspace", str(ws.root), "--path", "f.py", "--strict"]))

    def test_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = _Ws(Path(tmp))
            ws.file("g.py", b"content")
            ws.status([{"section_no": "1.1", "artifacts": [
                {"path": "g.py", "bytes": 7, "sha256": _sha(b"content")}]}])
            before = sorted((str(p.relative_to(ws.root)), p.read_bytes())
                            for p in ws.root.rglob("*") if p.is_file())
            sbr.main(["--workspace", str(ws.root), "--path", "g.py"])
            after = sorted((str(p.relative_to(ws.root)), p.read_bytes())
                           for p in ws.root.rglob("*") if p.is_file())
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
