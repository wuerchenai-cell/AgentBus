"""§10.13 工具链质量门禁最小测试 —— `tools/tree_delivery_sweep.py`。

该工具是**只读**体检器：对任务树中每个 `delivered` 叶登记的 `artifacts` 逐件重算
bytes 与 SHA256，并把结果分为 一致 / 漂移 / 缺失 / 无产物 四类。

本测试用**合成工作区**驱动它，覆盖：
1. 四类判定都能被正确区分，且非 delivered 的叶被排除；
2. 「漂移」区分变大 / 变小；
3. 同一路径被重复登记会被单独计数（历史真实出现过的登记缺陷）；
4. **只读性**：运行前后工作区所有文件的 bytes+sha256+mtime 完全不变。
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

from tools import tree_delivery_sweep as tds


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot(path: Path) -> dict:
    """记录工作区内全部文件的 bytes+sha256，用于断言只读性。"""
    out = {}
    for f in sorted(path.rglob("*")):
        if f.is_file():
            out[f.relative_to(path).as_posix()] = (f.stat().st_size, _sha256(f))
    return out


class TreeDeliverySweepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()

        # 一致：文件与登记完全相符
        self.good = self.ws / "good.txt"
        self.good.write_text("synthetic good", encoding="utf-8")
        # 漂移（变大）：登记 bytes 比实际小
        self.grew = self.ws / "grew.txt"
        self.grew.write_text("x" * 100, encoding="utf-8")
        # 漂移（变小）：登记 bytes 比实际大
        self.shrank = self.ws / "shrank.txt"
        self.shrank.write_text("y" * 10, encoding="utf-8")
        # 缺失：路径不存在
        self.missing_rel = "gone.txt"

        self.status_path = Path(self.tmp.name) / "ts30.json"
        self.status_path.write_text(json.dumps({
            "schema_version": 1,
            "leaves": [
                {"section_no": "1.1", "id": "A", "owner": "alpha", "state": "delivered",
                 "artifacts": [{"path": "good.txt", "bytes": self.good.stat().st_size,
                                "sha256": _sha256(self.good)}]},
                {"section_no": "1.2", "id": "B", "owner": "beta", "state": "delivered",
                 "artifacts": [
                     {"path": "grew.txt", "bytes": 1, "sha256": "0" * 64},
                     {"path": "shrank.txt", "bytes": 9999, "sha256": "0" * 64}]},
                {"section_no": "1.3", "id": "C", "owner": "gamma", "state": "delivered",
                 "artifacts": [{"path": self.missing_rel, "bytes": 5, "sha256": "0" * 64}]},
                {"section_no": "1.4", "id": "D", "owner": "delta", "state": "delivered",
                 "artifacts": []},
                {"section_no": "1.5", "id": "E", "owner": "epsilon", "state": "delivered",
                 "artifacts": [
                     {"path": "good.txt", "bytes": self.good.stat().st_size,
                      "sha256": _sha256(self.good)},
                     {"path": "good.txt", "bytes": self.good.stat().st_size,
                      "sha256": _sha256(self.good)}]},
                {"section_no": "1.6", "id": "F", "owner": "zeta", "state": "queued",
                 "artifacts": [{"path": "nope.txt", "bytes": 1, "sha256": "0" * 64}]},
            ],
        }, ensure_ascii=False), encoding="utf-8")

    def _run(self) -> dict:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = tds.main(["--workspace", str(self.ws), "--status", str(self.status_path)])
        self.assertEqual(0, code, "只读体检器应以 0 退出")
        return json.loads(buf.getvalue())

    def test_classification_of_four_verdicts(self):
        """按**条目**计数（同一文件被登记两次就计两次），据实测口径断言。"""
        rep = self._run()
        t = rep["totals"]
        self.assertEqual(5, t["leaves"], "6 个叶里 queued 的那个必须被排除")
        self.assertEqual(6, t["artifacts"], "1+2+1+0+2 = 6 条登记")
        self.assertEqual(3, t["ok"], "1.1 的 good.txt + 1.5 重复登记的两条 = 3 条一致")
        self.assertEqual(2, t["drift"], "1.2 的 grew / shrank")
        self.assertEqual(1, t["missing"], "1.3 的 gone.txt 不存在")
        self.assertEqual(1, t["dup_entries"], "1.5 把同一路径登记了两次")

    def test_drift_direction_is_split(self):
        rep = self._run()
        self.assertEqual({"变大": 1, "变小": 1}, rep["drift_directions"],
                         "必须区分变大与变小，否则无法判断是截断还是追加")
        detail = [r for r in rep["leaves_with_drift"] if r["section_no"] == "1.2"][0]["drift_detail"]
        self.assertEqual({"变大", "变小"}, {d["dir"] for d in detail})

    def test_non_delivered_leaf_is_excluded(self):
        rep = self._run()
        self.assertIn("1.4", rep["leaves_no_artifacts"], "零产物叶必须被列出")
        self.assertEqual(["1.3"], [r["section_no"] for r in rep["leaves_with_missing"]])
        self.assertEqual([["1.5", 1]], rep["leaves_with_duplicates"])
        # queued 叶 1.6 不得出现在任何统计分类里
        blob = json.dumps({k: v for k, v in rep.items() if k != "generated_from"},
                          ensure_ascii=False)
        self.assertNotIn("1.6", blob, "非 delivered 叶必须整体排除")

    def test_report_is_self_consistent(self):
        rep = self._run()
        t = rep["totals"]
        self.assertEqual(t["ok"] + t["drift"] + t["missing"], t["artifacts"])
        self.assertEqual(str(self.ws.resolve()), rep["workspace"])
        self.assertIn("generated_from", rep)

    def test_tool_is_read_only(self):
        before = _snapshot(self.ws)
        self._run()
        self.assertEqual(before, _snapshot(self.ws), "只读体检器不得改动工作区任何文件")

    def test_runs_as_subprocess_via_help(self):
        proc = subprocess.run(
            [sys.executable, str(Path(tds.__file__)), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=20)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("usage:", (proc.stdout + proc.stderr).lower())


if __name__ == "__main__":
    unittest.main()
