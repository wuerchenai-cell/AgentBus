#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""artifact_drift_attribution.py: 交付产物漂移归因（只读）。

要解决的问题
------------
`tools/tree_delivery_sweep.py` 会把「已交付产物的 bytes/SHA 与回执声明不一致」一律记为 **漂移**。
但"漂移"至少有两种完全不同的性质，混在一起会得出错误结论：

* **良性后续改动**：该文件被**后续任务**合法修改过（例如 `gui_state.py` 先由 §7.29 改、
  再被 §13.11 改），交付时的声明自然过期——**内容没有丢失**；
* **真问题**：文件变小（内容可能被删）、同大小异内容（§10.16 的等长改写类）、或无人认账。

本工具只做一件事：**用"后来的某份清单是否声明了当前磁盘状态"来给漂移归因**，
把不可解释的漂移从可解释的漂移里分出来。**不写任何文件、不改任务树。**

判定（对每个"已交付但声明与磁盘不一致"的条目）
------------------------------------------------
| 判定 | 含义 |
|---|---|
| `attributed_later_edit` | 另有某叶的清单声明了**与当前磁盘完全一致**的 bytes+sha256 → 后续合法改动 |
| `unattributed_drift` | 没有任何声明对得上当前磁盘状态 → **需要人工核** |
| `shrunk` | 当前字节数小于声明值（未匹配到后续声明时）→ 疑似内容丢失，优先处理 |
| `same_size_diff_content` | 字节数相同但 sha256 不同 → §10.16 的等长改写类 |
| `missing` | 文件不存在（另有声明能对上时记 `attributed_missing`，通常是归档封存） |

声明来源（三处，全部只读）
--------------------------
1. 任务树/回执里登记的 `artifacts`（`--status` 快照；缺省时读活动 `.agentbus/task-tree.json` 的等价信息）；
2. `成果/复函/manifests/*.json`（含 list 型与 dict 型两种格式）；
3. `成果/任务书维护/receipts/*_ARTIFACTS.json`。

用法
----
    python tools/artifact_drift_attribution.py [--workspace .] [--status <快照.json>] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_SECTION = re.compile(r"§\s*(\d+(?:\.\d+)*)")
_ART = re.compile(r"\bart-(\d+(?:\.\d+)*)\.json\Z")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm(path: Any) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    return path.replace("\\", "/").strip()


def _leaf_of(declarer: str, payload: Any) -> str:
    """尽力从声明者标识里提取章节号；提不到就用标识本身。"""
    match = _SECTION.search(declarer)
    if match:
        return match.group(1)
    if isinstance(payload, dict):
        leaf = payload.get("leaf")
        if isinstance(leaf, str) and leaf.strip():
            return leaf.strip()
        task = payload.get("task")
        if isinstance(task, str):
            inner = _SECTION.search(task)
            if inner:
                return inner.group(1)
    match = _ART.search(declarer)
    if match:
        return match.group(1)
    return declarer


def collect_declarations(workspace: Path) -> dict[str, list[dict]]:
    """path -> 声明列表（每条含 declarer/leaf/bytes/sha256）。"""
    out: dict[str, list[dict]] = defaultdict(list)
    sources: list[tuple[str, Any]] = []

    for manifest in sorted((workspace / "成果" / "复函" / "manifests").glob("*.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sources.append(("manifests/" + manifest.name, payload))

    for receipt in sorted((workspace / "成果" / "任务书维护" / "receipts").glob("*_ARTIFACTS.json")):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sources.append(("receipts/" + receipt.name, payload))

    for declarer, payload in sources:
        entries: list[Any] = []
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict) and isinstance(payload.get("artifacts"), list):
            entries = payload["artifacts"]
        leaf = _leaf_of(declarer, payload)
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = _norm(entry.get("path"))
            if not path:
                continue
            out[path].append({
                "declarer": declarer,
                "leaf": leaf,
                "bytes": entry.get("bytes"),
                "sha256": (entry.get("sha256") or "").lower() if isinstance(entry.get("sha256"), str) else "",
            })
    return out


def collect_bus_declarations(status: dict) -> list[dict]:
    """从 tree status 快照取回执登记的 artifacts（权威交付声明）。"""
    rows: list[dict] = []
    for leaf in status.get("leaves") or []:
        section = leaf.get("section_no") or leaf.get("id")
        for entry in leaf.get("artifacts") or []:
            if not isinstance(entry, dict):
                continue
            path = _norm(entry.get("path"))
            if not path:
                continue
            rows.append({
                "leaf": section,
                "path": path,
                "bytes": entry.get("bytes"),
                "sha256": (entry.get("sha256") or "").lower() if isinstance(entry.get("sha256"), str) else "",
            })
    return rows


def analyse(workspace: Path, status: dict) -> dict[str, Any]:
    declarations = collect_declarations(workspace)
    bus_rows = collect_bus_declarations(status)
    # 总线里登记的 artifacts 同样是对某个时间点磁盘状态的声明：某叶若登记了
    # path=cli.py bytes=152851，就说明它在其交付时刻见到过这个状态。
    # 把 bus 声明并入索引，"后续合法改动"的归因才完整。
    for row in bus_rows:
        declarations.setdefault(row["path"], []).append({
            "declarer": "bus:" + str(row["leaf"]),
            "leaf": str(row["leaf"]),
            "bytes": row["bytes"],
            "sha256": row["sha256"],
        })
    rows: list[dict] = []
    for row in bus_rows:
        path = workspace / row["path"]
        declared_bytes = row["bytes"] if isinstance(row["bytes"], int) else None
        declared_sha = row["sha256"]
        if not path.is_file():
            matches = [d for d in declarations.get(row["path"], [])
                       if d["bytes"] == declared_bytes and d["sha256"] == declared_sha]
            twin = path.with_name(path.name + ".deprecated_old_version")
            twin_ok = False
            twin_detail = ""
            if twin.is_file():
                twin_bytes = twin.stat().st_size
                twin_sha = sha256_of(twin)
                twin_ok = (twin_bytes == declared_bytes and twin_sha == declared_sha)
                twin_detail = "归档孪生 %s bytes=%d sha=%s" % (
                    twin.name, twin_bytes, twin_sha[:16] + "…")
            if twin_ok:
                rows.append({"leaf": row["leaf"], "path": row["path"], "verdict": "archived_twin_matched",
                             "declared_bytes": declared_bytes, "actual_bytes": None,
                             "attributed_to": ["archived"],
                             "detail": "文件已被 §10.6 加缀归档，且与交付声明**逐字节一致**（%s）→ 内容未丢失"
                                       % twin_detail})
            else:
                rows.append({"leaf": row["leaf"], "path": row["path"], "verdict": "missing",
                             "declared_bytes": declared_bytes, "actual_bytes": None,
                             "attributed_to": sorted({d["leaf"] for d in matches}),
                             "detail": ("文件不存在；有其它声明与交付声明一致（%s）" % twin_detail if twin.is_file()
                                        else "文件不存在且无其它声明")})
            continue
        actual_bytes = path.stat().st_size
        actual_sha = sha256_of(path)
        if actual_bytes == declared_bytes and actual_sha == declared_sha:
            continue  # 一致，不进入本报告
        current_matches = [d for d in declarations.get(row["path"], [])
                           if d["bytes"] == actual_bytes and d["sha256"] == actual_sha]
        later = sorted({d["leaf"] for d in current_matches} - {str(row["leaf"])})
        base = {"leaf": row["leaf"], "path": row["path"],
                "declared_bytes": declared_bytes, "actual_bytes": actual_bytes,
                "attributed_to": later}
        if later:
            base["verdict"] = "attributed_later_edit"
            base["detail"] = "当前磁盘状态与 %s 的声明一致 → 后续合法改动，内容未丢失" % ",".join(later)
        elif declared_bytes is not None and actual_bytes < declared_bytes:
            base["verdict"] = "shrunk"
            base["detail"] = "字节数变小（%d → %d）且无后续声明认账 → 疑似内容丢失，优先人工核" % (declared_bytes, actual_bytes)
        elif declared_bytes == actual_bytes:
            base["verdict"] = "same_size_diff_content"
            base["detail"] = "字节数相同但 sha256 不同 → §10.16 等长改写类，需人工核"
        else:
            base["verdict"] = "unattributed_drift"
            base["detail"] = "字节数变化（%s → %d）且无后续声明认账 → 需人工核" % (declared_bytes, actual_bytes)
        rows.append(base)

    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["verdict"]] += 1
    ok_verdicts = {"attributed_later_edit", "archived_twin_matched"}
    return {
        "workspace_checked": True,
        "declaration_sources": sorted(declarations.keys()) and {
            "manifest_entries": sum(len(v) for v in declarations.values()),
            "distinct_paths": len(declarations),
            "bus_rows": len(bus_rows),
        },
        "rows": rows,
        "counts": dict(sorted(counts.items())),
        "verdict": "clean" if not rows else (
            "all_attributed" if all(r["verdict"] in ok_verdicts for r in rows) else "needs_attention"),
    }


def render(report: dict) -> str:
    lines = ["交付产物漂移归因（只读）", "=" * 66]
    info = report.get("declaration_sources") or {}
    lines.append("声明来源：清单条目 %s ｜ 去重路径 %s ｜ 回执登记 %s"
                 % (info.get("manifest_entries"), info.get("distinct_paths"), info.get("bus_rows")))
    counts = report.get("counts") or {}
    lines.append("需解释条目：%d" % len(report.get("rows") or []))
    for key in ("attributed_later_edit", "unattributed_drift", "shrunk",
                "same_size_diff_content", "missing"):
        if key in counts:
            lines.append("  %-24s %d" % (key, counts[key]))
    lines.append("")
    order = {"shrunk": 0, "same_size_diff_content": 1, "unattributed_drift": 2,
             "missing": 3, "attributed_later_edit": 4, "archived_twin_matched": 5}
    for row in sorted(report.get("rows") or [], key=lambda r: (order.get(r["verdict"], 9), str(r.get("leaf")))):
        lines.append("[%-24s] §%-6s %s" % (row["verdict"], row.get("leaf"), row["path"]))
        lines.append("      %s" % row["detail"])
    lines.append("")
    lines.append("判定: %s" % report["verdict"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", default=".", help="工作空间目录")
    parser.add_argument("--status", default=None, help="tree status 快照 JSON（缺省用活动 .agentbus/task-tree.json）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    status: dict[str, Any]
    if args.status:
        path = Path(args.status)
        if not path.is_file():
            print("缺少输入文件：%s" % path, file=sys.stderr)
            return 2
        status = json.loads(path.read_text(encoding="utf-8"))
    else:
        path = workspace / ".agentbus" / "task-tree.json"
        if not path.is_file():
            print("缺少输入文件：%s" % path, file=sys.stderr)
            return 2
        status = json.loads(path.read_text(encoding="utf-8"))
    # 活动树不含回执登记的 artifacts，需经 tree status 视图；缺省路径下直接用它自己的 leaves。
    if "artifacts" not in (status.get("leaves") or [{}])[0] and args.status is None:
        print("[提示] 活动 task-tree.json 不含回执登记的 artifacts；请改用 --status 传 tree status 快照。",
              file=sys.stderr)
    report = analyse(workspace, status)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
