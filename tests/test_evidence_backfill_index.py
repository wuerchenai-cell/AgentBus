"""§10.13 工具链质量门禁最小测试 —— `tools/evidence_backfill_index.py`。

该工具是**只读**证据回找器：为「已交付但登记产物为 0」的叶子，去约定目录里找回外部清单，
并对清单内每一项再核验 bytes+SHA256，把「不可核验」变成「可机器核验」。

本测试用**合成工作区**驱动它，覆盖：
1. `verified` / `drifted` / `no_manifest` 三种判定；
2. 清单结构无法解析时判 `no_manifest` 并附原因，而**不是**抛异常；
3. 兼容多种清单形态（顶层数组 / `{"artifacts":[...]}`）；
4. **只读性**：运行前后工作区所有文件完全不变。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import evidence_backfill_index as ebi


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(path: Path) -> dict:
    return {f.relative_to(path).as_posix(): (f.stat().st_size, _sha256(f))
            for f in sorted(path.rglob("*")) if f.is_file()}


class EvidenceBackfillIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.manifests = self.ws / "成果" / "复函" / "manifests"
        self.manifests.mkdir(parents=True)

        self.doc = self.ws / "doc.txt"
        self.doc.write_text("synthetic evidence body", encoding="utf-8")
        size, digest = self.doc.stat().st_size, _sha256(self.doc)

        # verified：清单与磁盘逐字节相符
        (self.manifests / "art-10.1.json").write_text(json.dumps(
            [{"path": "doc.txt", "bytes": size, "sha256": digest}]), encoding="utf-8")
        # drifted：声明与磁盘不符
        (self.manifests / "art-10.2.json").write_text(json.dumps(
            {"artifacts": [{"path": "doc.txt", "bytes": 1, "sha256": "0" * 64}]}),
            encoding="utf-8")
        # 清单损坏：必须判 no_manifest 并附原因，而不是抛异常
        (self.manifests / "art-10.3.json").write_text("{not json", encoding="utf-8")
        # 10.4 没有清单文件

        self.sweep_path = Path(self.tmp.name) / "sweep.json"
        self.sweep_path.write_text(json.dumps(
            {"leaves_no_artifacts": ["10.1", "10.2", "10.3", "10.4"]}), encoding="utf-8")

        self.status_path = Path(self.tmp.name) / "ts30.json"
        self.status_path.write_text(json.dumps({
            "schema_version": 1,
            "leaves": [{"section_no": s, "owner": "alpha", "title": "T" + s}
                       for s in ("10.1", "10.2", "10.3", "10.4")],
        }, ensure_ascii=False), encoding="utf-8")

    def _run(self) -> dict:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ebi.main(["--workspace", str(self.ws), "--sweep", str(self.sweep_path),
                             "--status", str(self.status_path),
                             "--manifest-dir", str(self.manifests)])
        self.assertEqual(0, code, "只读回找器应以 0 退出")
        return json.loads(buf.getvalue())

    def test_three_verdicts_are_distinguished(self):
        rep = self._run()
        self.assertEqual(4, rep["target_leaves"])
        self.assertEqual({"verified": 1, "drifted": 1, "no_manifest": 2}, rep["verdicts"])
        by = {r["section_no"]: r for r in rep["rows"]}
        self.assertEqual("verified", by["10.1"]["verdict"])
        self.assertEqual(1, by["10.1"]["ok"])
        self.assertEqual("drifted", by["10.2"]["verdict"])
        self.assertEqual(1, by["10.2"]["drift"])
        self.assertIsNone(by["10.4"]["manifest"], "没有清单就是 no_manifest，不得编造")

    def test_drift_records_direction_evidence(self):
        rep = self._run()
        row = [r for r in rep["rows"] if r["section_no"] == "10.2"][0]
        detail = row["drift_detail"][0]
        self.assertEqual("漂移", detail["verdict"])
        self.assertEqual(1, detail["declared"])
        self.assertEqual(self.doc.stat().st_size, detail["actual"])

    def test_malformed_manifest_is_graceful(self):
        rep = self._run()
        row = [r for r in rep["rows"] if r["section_no"] == "10.3"][0]
        self.assertEqual("no_manifest", row["verdict"])
        self.assertIn("无法解析", row.get("note", ""), "必须如实说明原因，不得静默")

    def test_duplicate_manifest_patterns_do_not_crash(self):
        """同一叶在两个约定目录各有一份清单时，工具仍须稳定返回单个结果。"""
        extra = self.ws / "成果" / "任务书维护"
        extra.mkdir(parents=True)
        (extra / "art-10.1.json").write_text(json.dumps([]), encoding="utf-8")
        rep = self._run()
        rows = [r for r in rep["rows"] if r["section_no"] == "10.1"]
        self.assertEqual(1, len(rows), "每个目标叶只应产出一行")

    def test_tool_is_read_only(self):
        before = _snapshot(self.ws)
        self._run()
        self.assertEqual(before, _snapshot(self.ws), "只读回找器不得改动工作区任何文件")

    def test_runs_as_subprocess_via_help(self):
        proc = subprocess.run(
            [sys.executable, str(Path(ebi.__file__)), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=20)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("usage:", (proc.stdout + proc.stderr).lower())


if __name__ == "__main__":
    unittest.main()
