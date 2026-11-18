#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补充证据索引（只读）：为「已交付但未登记 artifacts」的叶子重建可机器核验的证据链。

背景：`agentbus answer` 支持 `--artifacts-file`（工作区相对 JSON，逐项 path/bytes/sha256），
但相当一部分交付（含本席的 23 叶）**未使用它**，导致任务树的 `artifacts` 为空 —— 
「有回执、有清单文件，但总线看不到」。且管道门禁禁止终态重交，无法补登记。

本工具不写总线、不改任务树，只做一件事：**把散落在回执正文与 `manifests/` 里的证据找出来并逐件复核**，
产出一份外部可引用的索引，使这部分交付从"不可核验"变成"可机器核验"。

判定：
  verified   清单存在，且清单内每一项在磁盘上 bytes+sha256 均匹配
  drifted    清单存在，但至少一项不匹配（记录方向）
  no_manifest 未找到对应清单文件（只有回执正文，无法机械核验）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

DEFAULT_WORKSPACE = Path.cwd()


def default_manifest_dirs(workspace: Path) -> list[Path]:
    return [
        workspace / "成果" / "复函" / "manifests",
        workspace / "成果" / "任务书维护",
    ]


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_manifest(section_no: str, manifest_dirs) -> Path | None:
    if not section_no:
        return None
    cands = []
    for d in manifest_dirs:
        if not d.is_dir():
            continue
        for pat in (f"art-{section_no}.json", f"*{section_no}*ARTIFACTS*.json",
                    f"*ARTIFACTS*{section_no}*.json", f"art-{section_no}-*.json"):
            cands.extend(d.rglob(pat))
    # 去掉目录本身与重复
    uniq = []
    seen = set()
    for c in cands:
        k = str(c).casefold()
        if c.is_file() and k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq[0] if uniq else None


def load_entries(path: Path):
    """容忍多种清单形态：list、{'artifacts':[...]}、{'files':[...]}。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"清单无法解析: {type(exc).__name__}"
    if isinstance(data, list):
        return data, ""
    if isinstance(data, dict):
        for key in ("artifacts", "files", "items", "deliverables"):
            if isinstance(data.get(key), list):
                return data[key], ""
    return None, "清单结构未识别"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="为『已交付但未登记 artifacts』的叶子重建可机器核验的证据链（纯只读）")
    parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE),
                        help="工作区根目录（默认本项目根）")
    parser.add_argument("--sweep", default=None,
                        help="DELIVERY_SWEEP.json 路径（默认 <workspace>/成果/验收/20261013/DELIVERY_SWEEP.json）")
    parser.add_argument("--status", default=None,
                        help="tree status 快照 JSON（默认 %%TEMP%%/ts30.json）")
    parser.add_argument("--manifest-dir", action="append", default=None,
                        help="额外清单目录，可重复；默认使用工作区内两个约定目录")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    manifest_dirs = ([Path(p).resolve() for p in args.manifest_dir]
                     if args.manifest_dir else default_manifest_dirs(workspace))
    sweep_path = (Path(args.sweep) if args.sweep
                  else workspace / "成果" / "验收" / "20261013" / "DELIVERY_SWEEP.json")
    status_path = (Path(args.status) if args.status
                   else Path(os.environ.get("TEMP", ".")) / "ts30.json")

    sweep = json.loads(sweep_path.read_text(encoding="utf-8"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    by_sec = {l.get("section_no"): l for l in status["leaves"]}

    targets = sweep["leaves_no_artifacts"]
    rows = []
    for sec in targets:
        leaf = by_sec.get(sec) or {}
        rec = {"section_no": sec, "owner": leaf.get("owner"), "title": (leaf.get("title") or "")[:60],
               "manifest": None, "verdict": "no_manifest", "entries": 0, "ok": 0, "drift": 0, "missing": 0,
               "drift_detail": []}
        mf = find_manifest(sec, manifest_dirs)
        if mf is None:
            rows.append(rec)
            continue
        try:
            rec["manifest"] = mf.relative_to(workspace).as_posix()
        except ValueError:
            rec["manifest"] = str(mf)
        entries, err = load_entries(mf)
        if entries is None:
            rec["verdict"] = "no_manifest"
            rec["note"] = err
            rows.append(rec)
            continue
        rec["entries"] = len(entries)
        for e in entries:
            if not isinstance(e, dict) or not e.get("path"):
                continue
            rel = str(e["path"]).replace("\\", "/")
            p = workspace / rel
            if not p.is_file():
                rec["missing"] += 1
                rec["drift_detail"].append({"path": rel, "verdict": "缺失"})
                continue
            ab = p.stat().st_size
            if ab == e.get("bytes") and sha256_of(p) == e.get("sha256"):
                rec["ok"] += 1
            else:
                rec["drift"] += 1
                rec["drift_detail"].append({"path": rel, "verdict": "漂移",
                                            "declared": e.get("bytes"), "actual": ab})
        rec["verdict"] = "verified" if (rec["drift"] == 0 and rec["missing"] == 0) else "drifted"
        rows.append(rec)

    c = Counter(r["verdict"] for r in rows)
    out = {
        "purpose": "为『已交付但未登记 artifacts』的叶子重建可机器核验的证据链（只读，不写总线）",
        "generated_at_note": "基于 DELIVERY_SWEEP.json 快照；数字随项目演进变化",
        "target_leaves": len(rows),
        "verdicts": dict(c),
        "by_owner": dict(Counter(r["owner"] for r in rows)),
        "rows": rows,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
