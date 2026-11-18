#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inspect_tree.py: 只读打印任务树状态统计与未完成节点。

来源：§7.12 / §10.11。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读打印任务树拓扑与状态 (inspect_tree)")
    parser.add_argument("--workspace", default=".", help="工作区路径")
    parser.add_argument("--all", action="store_true", help="打印所有节点")
    args = parser.parse_args(argv)

    tree_path = Path(args.workspace) / ".agentbus" / "task-tree.json"
    if not tree_path.exists():
        print(f"[错误] 找不到任务树文件: {tree_path}", file=sys.stderr)
        return 1

    with open(tree_path, encoding="utf-8") as f:
        data = json.load(f)

    leaves = data.get("leaves", [])
    print(f"Total leaves: {len(leaves)}")
    counts = Counter(l.get("state") for l in leaves)
    print("States summary:", dict(counts))
    print("-" * 60)
    for l in leaves:
        if args.all or l.get("state") != "delivered":
            safe_title = l.get("title", "").encode("gbk", errors="replace").decode("gbk") if sys.platform == "win32" else l.get("title", "")
            print(f"[{l.get('state')}] {l.get('id')} (§{l.get('section_no')}) - {safe_title} | owner={l.get('owner')} claimable={l.get('claimable')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
