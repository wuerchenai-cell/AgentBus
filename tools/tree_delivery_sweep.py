#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全树交付物验证扫描（只读）：对每个 delivered 叶的 artifacts 逐件重算 SHA256。

判据（只做客观核对，不做主观评分）：
  一致     路径存在 + bytes + sha256 全部匹配
  漂移     路径存在但 bytes 或 sha256 不匹配（区分"变大/变小"）
  缺失     路径不存在
  无产物   该叶未登记 artifacts
另记录每叶的 evidence_error 字段（总线自己报的证据问题）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

DEFAULT_WORKSPACE = Path.cwd()


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="全树已交付产物的逐件 bytes/SHA256 复算体检（纯只读，不修复不改写）")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE),
                        help="工作区根目录（默认本项目根）")
    parser.add_argument("--status", default=None,
                        help="tree status 快照 JSON（默认 %%TEMP%%/ts30.json）")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    status_path = (Path(args.status) if args.status
                   else Path(os.environ.get("TEMP", ".")) / "ts30.json")
    data = json.loads(status_path.read_text(encoding="utf-8"))
    leaves = [l for l in data["leaves"] if l.get("state") == "delivered"]

    rows = []
    for leaf in leaves:
        arts = leaf.get("artifacts") or []
        rec = {
            "section_no": leaf.get("section_no"),
            "id": leaf.get("id"),
            "owner": leaf.get("owner"),
            "evidence_error": leaf.get("evidence_error") or "",
            "n_artifacts": len(arts),
            "ok": 0, "drift": 0, "missing": 0,
            "drift_detail": [],
        }
        seen = set()
        dup = 0
        for a in arts:
            if not isinstance(a, dict) or not a.get("path"):
                continue
            rel = a["path"].replace("\\", "/")
            key = os.path.normcase(rel)
            if key in seen:
                dup += 1
            seen.add(key)
            p = workspace / rel
            if not p.is_file():
                rec["missing"] += 1
                rec["drift_detail"].append({"path": rel, "verdict": "缺失"})
                continue
            ab = p.stat().st_size
            if ab == a.get("bytes") and sha256_of(p) == a.get("sha256"):
                rec["ok"] += 1
            else:
                rec["drift"] += 1
                rec["drift_detail"].append({
                    "path": rel, "verdict": "漂移",
                    "declared": a.get("bytes"), "actual": ab,
                    "dir": "变大" if ab > (a.get("bytes") or 0) else ("变小" if ab < (a.get("bytes") or 0) else "同大小异内容"),
                })
        rec["dup_entries"] = dup
        rows.append(rec)

    tot = Counter()
    for r in rows:
        tot["leaves"] += 1
        tot["artifacts"] += r["n_artifacts"]
        tot["ok"] += r["ok"]
        tot["drift"] += r["drift"]
        tot["missing"] += r["missing"]
        tot["dup_entries"] += r["dup_entries"]

    # 漂移方向统计
    dirs = Counter()
    for r in rows:
        for d in r["drift_detail"]:
            if d["verdict"] == "漂移":
                dirs[d["dir"]] += 1

    out = {
        "generated_from": str(status_path),
        "workspace": str(workspace),
        "totals": dict(tot),
        "drift_directions": dict(dirs),
        "leaves_with_missing": [r for r in rows if r["missing"]],
        "leaves_with_drift": sorted(
            [r for r in rows if r["drift"]],
            key=lambda r: -r["drift"]),
        "leaves_with_evidence_error": [r for r in rows if r["evidence_error"]],
        "leaves_no_artifacts": [r["section_no"] for r in rows if r["n_artifacts"] == 0],
        "leaves_with_duplicates": [(r["section_no"], r["dup_entries"]) for r in rows if r["dup_entries"]],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
