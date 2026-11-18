#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务树能力条件审计（只读）。

来源：§10.17 `AUDIT-360-CAPABILITY-ROUTING-GUARD`
      （由 2026-10-13 对 `.agentbus/config.json` 与 `.agentbus/task-tree.json` 的实测审计得出）

检出的缺陷类别
--------------
本项目用 `required_capabilities` 做路由：叶子的候选成员 = 具备**全部**所需能力的成员。
因此存在两类静默失效：

  1. **死叶**：叶子声明了能力注册表中**不存在**的能力名 → 候选成员为空
     → 该叶**永久无人可认领**，而在 `tree status` 里它看起来与正常 queued 叶一模一样。
     实测实例：§10.15 曾用 `bus-protocol`；§12 用 `architecture/ui/automation`（均不在注册表）。
  2. **单点路由**：某能力只有 1 个成员具备时，带该条件的叶实际上是「只派给那一个人」。
     实测：7 个成员中只有 1 个登记了 capabilities → 全树 15 个能力条件节点**全部仅对那一个成员开放**。
     这不是能力路由，而是把并行度压成了串行。

纪律
----
- **只读**：不写任务树、不改配置、不移动文件；只报告。
- **不猜**：能力是否"应该"存在由报告呈现，判定交由 Owner/协调者；本工具只做集合比较。
- `--strict` 时：存在死叶即非零退出（供 CI/回归门使用）。

用法
----
    python tools/tree_capability_audit.py [--workspace .] [--json] [--strict]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# 任务树里带能力条件的节点集合（roots 一般不带，但一并纳入以防遗漏）
NODE_KINDS = ("roots", "branches", "leaves")


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def registered_capabilities(config: dict[str, Any]) -> dict[str, list[str]]:
    """能力 -> 具备该能力的成员列表。"""
    index: dict[str, list[str]] = defaultdict(list)
    for member, profile in (config.get("workers") or {}).items():
        for cap in (profile.get("capabilities") or []):
            if isinstance(cap, str) and cap.strip():
                index[cap].append(member)
    return {cap: sorted(members) for cap, members in index.items()}


def audit(tree: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    caps = registered_capabilities(config)
    known = set(caps)

    dead: list[dict[str, Any]] = []
    single_holder: list[dict[str, Any]] = []
    gated: list[dict[str, Any]] = []
    unconditional = 0

    for kind in NODE_KINDS:
        for node in tree.get(kind) or []:
            req = [c for c in (node.get("required_capabilities") or []) if isinstance(c, str)]
            node_ref = {
                "kind": kind,
                "id": node.get("id"),
                "section_no": node.get("section_no"),
                "title": (node.get("title") or "")[:70],
                "required_capabilities": sorted(req),
            }
            if not req:
                unconditional += 1
                continue
            gated.append(node_ref)

            unknown = sorted(set(req) - known)
            # 候选成员 = 具备全部所需能力者（与总线路由语义一致）
            eligible: set[str] | None = None
            for cap in req:
                holders = set(caps.get(cap, ()))
                eligible = holders if eligible is None else (eligible & holders)
            eligible = sorted(eligible or ())

            if unknown or not eligible:
                entry = dict(node_ref)
                entry["unknown_capabilities"] = unknown
                entry["eligible_agents"] = eligible
                entry["reason"] = (
                    "含未登记能力名" if unknown else "无任何成员同时具备全部所需能力"
                )
                dead.append(entry)
            else:
                # 单点：某个所需能力只有 1 个持有者
                narrow = {c: caps[c] for c in req if len(caps.get(c, ())) == 1}
                if len(eligible) == 1:
                    entry = dict(node_ref)
                    entry["eligible_agents"] = eligible
                    entry["single_holder_capabilities"] = narrow
                    single_holder.append(entry)

    def _order(item: dict[str, Any]) -> tuple:
        sec = item.get("section_no") or ""
        try:
            return tuple(int(x) for x in sec.split(".")) if sec else (10**9,)
        except ValueError:
            return (10**9,)

    dead.sort(key=_order)
    single_holder.sort(key=_order)

    return {
        "workspace_checked": True,
        "registered_capabilities": sorted(known),
        "capability_holders": {c: caps[c] for c in sorted(caps)},
        "members_without_capabilities": sorted(
            m for m, p in (config.get("workers") or {}).items()
            if not (p.get("capabilities") or [])
        ),
        "counts": {
            "nodes_total": sum(len(tree.get(k) or []) for k in NODE_KINDS),
            "nodes_without_conditions": unconditional,
            "nodes_with_conditions": len(gated),
            "dead_leaves": len(dead),
            "single_holder_nodes": len(single_holder),
        },
        "dead_nodes": dead,
        "single_holder_nodes": single_holder,
        "verdict": "clean" if not dead else "dead_nodes_present",
    }


def render(report: dict[str, Any]) -> str:
    c = report["counts"]
    lines = []
    lines.append("任务树能力条件审计（只读）")
    lines.append("=" * 62)
    lines.append("已登记能力 %d 个；未登记任何能力的成员: %s"
                 % (len(report["registered_capabilities"]),
                    ", ".join(report["members_without_capabilities"]) or "（无）"))
    lines.append("")
    lines.append("节点统计：总 %d ｜ 无条件 %d ｜ 有条件 %d"
                 % (c["nodes_total"], c["nodes_without_conditions"], c["nodes_with_conditions"]))
    lines.append("")
    if report["dead_nodes"]:
        lines.append("【缺陷一】死叶：候选成员为空 → 永久无人可认领（%d 个）" % c["dead_leaves"])
        for d in report["dead_nodes"]:
            lines.append("  §%-8s %s" % (d.get("section_no"), d["title"]))
            if d.get("unknown_capabilities"):
                lines.append("        未登记能力名: %s" % d["unknown_capabilities"])
            lines.append("        全部所需: %s" % d["required_capabilities"])
        lines.append("")
    else:
        lines.append("【缺陷一】无死叶。")
        lines.append("")

    if report["single_holder_nodes"]:
        lines.append("【缺陷二】单点路由：所需能力仅 1 个成员具备（%d 个）" % c["single_holder_nodes"])
        lines.append("  （这些叶在效果上等于「只派给该成员」，并行度被压成串行）")
        shown = report["single_holder_nodes"][:12]
        for d in shown:
            lines.append("  §%-8s 仅 %s ｜ %s"
                         % (d.get("section_no"), ",".join(d["eligible_agents"]), d["title"]))
        if len(report["single_holder_nodes"]) > len(shown):
            lines.append("  … 其余 %d 个见 --json"
                         % (len(report["single_holder_nodes"]) - len(shown)))
    else:
        lines.append("【缺陷二】无单点路由节点。")
    lines.append("")
    lines.append("判定: %s" % report["verdict"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", default=".", help="工作空间目录（含 .agentbus/）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--strict", action="store_true",
                        help="存在死叶时以非零退出（供回归门使用）")
    args = parser.parse_args(argv)

    root = Path(args.workspace).resolve()
    tree_path = root / ".agentbus" / "task-tree.json"
    config_path = root / ".agentbus" / "config.json"
    for path in (tree_path, config_path):
        if not path.is_file():
            print("缺少输入文件：%s" % path, file=sys.stderr)
            return 2

    try:
        report = audit(_load(tree_path), _load(config_path))
    except (OSError, ValueError) as exc:
        print("审计失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    if args.strict and report["verdict"] != "clean":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
