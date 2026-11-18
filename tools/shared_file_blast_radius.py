#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shared_file_blast_radius.py: 共享文件「爆炸半径」预检（只读）。

要解决的问题（2026-10-14 早晨的实证）
------------------------------------
`tools/README.md` 被**至少 6 片在途/已交付叶**当作交付产物登记了 bytes+SHA256。
本席为登记 3 个新工具而编辑它（+2,099 字节）时**没有先看有哪些回执依赖它**，
结果这 6 片叶的产物哈希全部失配，整棵任务树从 172/172 退回 159/13。

**根因不是"改错了"，而是"改之前不知道爆炸半径"。** 租约只防并发写入，
不防"我这一改会让多少别人的回执作废"。

本工具把这件事变成改共享文件前的**必读一步**。

输出
----
1. `--path <相对路径>`：该文件被哪些叶/清单/回执登记（fan-in），逐条给出声明值与当前磁盘状态；
2. `--all`：按 fan-in 降序列出全库"被多处登记的共享文件"——这些就是**高危共享文件**；
3. `--strict`：目标路径已存在失配声明时退出码 1（说明它已经在流血，别再动）。

纪律
----
* **只读**：不写任何文件、不改任务树。
* 声明源与 `tools/artifact_drift_attribution.py` 完全一致
  （manifests + receipts/*_ARTIFACTS.json + 总线登记的 artifacts），**复用其收集函数，不复制**。
* **不代改登记**：更新登记应由改动该文件的成员负责。

用法
----
    python tools/shared_file_blast_radius.py --path tools/README.md [--workspace .] [--json]
    python tools/shared_file_blast_radius.py --all [--top 20] [--workspace .]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent
_INTEGRATION = _TOOLS_DIR.parent
if str(_INTEGRATION) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION))


def _load_sibling(name: str):
    path = _TOOLS_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_status(workspace: Path, status_path: str | None) -> dict[str, Any]:
    if status_path:
        return json.loads(Path(status_path).read_text(encoding="utf-8"))
    path = workspace / ".agentbus" / "task-tree.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def build_index(workspace: Path, status: dict) -> dict[str, list[dict]]:
    """path -> 声明列表（含来源类别）。"""
    ada = _load_sibling("artifact_drift_attribution.py")
    index: dict[str, list[dict]] = {}
    for path, entries in ada.collect_declarations(workspace).items():
        index.setdefault(path, []).extend(entries)
    for row in ada.collect_bus_declarations(status):
        index.setdefault(row["path"], []).append({
            "declarer": "bus:" + str(row["leaf"]),
            "leaf": str(row["leaf"]),
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "kind": "bus",
        })
    return index


def fan_in(index: dict[str, list[dict]], path: str) -> list[dict]:
    return sorted(index.get(path.replace("\\", "/"), []),
                  key=lambda d: (str(d.get("leaf")), str(d.get("declarer"))))


def shared_ranking(index: dict[str, list[dict]], top: int = 20) -> list[dict]:
    rows = []
    for path, entries in index.items():
        leaves = sorted({str(e.get("leaf")) for e in entries})
        if len(leaves) < 2:
            continue
        rows.append({"path": path, "fan_in_leaves": len(leaves), "leaves": leaves,
                     "declarations": len(entries)})
    rows.sort(key=lambda r: (-r["fan_in_leaves"], r["path"]))
    return rows[:top]


def analyse(workspace: Path, index: dict[str, list[dict]], target: str) -> dict[str, Any]:
    entries = fan_in(index, target)
    disk = workspace / target.replace("\\", "/")
    actual_bytes = disk.stat().st_size if disk.is_file() else None
    actual_sha = None
    if disk.is_file():
        ada = _load_sibling("artifact_drift_attribution.py")
        actual_sha = ada.sha256_of(disk)
    rows = []
    for entry in entries:
        declared = entry.get("bytes")
        digest = entry.get("sha256")
        if actual_bytes is None:
            state = "file_missing"
        elif declared == actual_bytes and digest == actual_sha:
            state = "match"
        else:
            state = "mismatch"
        rows.append({"leaf": entry.get("leaf"), "declarer": entry.get("declarer"),
                     "kind": entry.get("kind", "manifest"), "declared_bytes": declared,
                     "declared_sha256": digest, "state": state})
    mismatched = [r for r in rows if r["state"] == "mismatch"]
    leaves = sorted({str(r["leaf"]) for r in rows})
    return {
        "path": target.replace("\\", "/"),
        "exists_on_disk": disk.is_file(),
        "actual_bytes": actual_bytes,
        "actual_sha256": actual_sha,
        "fan_in_leaves": len(leaves),
        "leaves": leaves,
        "declarations": rows,
        "mismatched": len(mismatched),
        "verdict": "clean" if not mismatched else "already_broken",
    }


def render(report: dict) -> str:
    lines = ["共享文件爆炸半径（只读）", "=" * 66]
    lines.append("路径            : %s" % report["path"])
    lines.append("磁盘            : %s" % (("存在，%d 字节" % report["actual_bytes"])
                                          if report["exists_on_disk"] else "不存在"))
    lines.append("fan-in（依赖叶）: %d  →  %s" % (report["fan_in_leaves"], "、".join(report["leaves"])))
    lines.append("已失配声明      : %d" % report["mismatched"])
    lines.append("")
    for row in report["declarations"]:
        lines.append("  [%-8s] §%-6s %-34s 声明 %s" % (
            row["state"], row["leaf"], row["declarer"], row["declared_bytes"]))
    lines.append("")
    lines.append("判定: %s" % report["verdict"])
    if report["mismatched"]:
        lines.append("→ 该文件已让 %d 条声明失配；**编辑前必须先与相关叶的所有者协调**。"
                     % report["mismatched"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--status", default=None, help="tree status 快照 JSON")
    parser.add_argument("--path", default=None, help="要评估的共享文件（工作区相对）")
    parser.add_argument("--all", action="store_true", help="列出全库被多处登记的共享文件")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="目标已失配时退出码 1")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    try:
        status = load_status(workspace, args.status)
    except (OSError, ValueError) as exc:
        print("读取状态失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    index = build_index(workspace, status)

    if args.all:
        rows = shared_ranking(index, top=args.top)
        if args.json:
            print(json.dumps({"shared_files": rows}, ensure_ascii=False, indent=2))
        else:
            print("全库被多处登记的共享文件（按 fan-in 降序，前 %d）" % len(rows))
            print("=" * 66)
            for row in rows:
                print("  fan-in=%-3d %-62s %s" % (row["fan_in_leaves"], row["path"],
                                                  "、".join(row["leaves"][:12])))
        return 0

    if not args.path:
        print("需提供 --path 或 --all", file=sys.stderr)
        return 2

    report = analyse(workspace, index, args.path)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    if args.strict and report["mismatched"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
