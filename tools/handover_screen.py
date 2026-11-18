#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§6.10 「一屏说清卡住的任务该怎么接手」的只读命令行入口。

职责边界：
  * **只读**：构建任务树快照 + 组装接手说明，不认领、不释放票据、不写任何文件。
  * 复用 `recovery_center.blocked_handover`（纯函数），此处只负责"取数据 + 排版"。
  * 不访问网络、不调模型、不读凭据。

用法：
    python tools/handover_screen.py --workspace <项目根>            # 列出所有卡住的叶子
    python tools/handover_screen.py --workspace <项目根> --leaf 6.2 # 单页接手说明
    python tools/handover_screen.py --workspace <项目根> --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbus import recovery_center as rc          # noqa: E402
from agentbus.config import load as load_config      # noqa: E402
from agentbus.task_tree import TaskForest            # noqa: E402

_STOP_FILE = "STOP"


def load_forest(workspace: Path) -> dict:
    """只读地取一份任务树快照（与 `agentbus tree status` 同源）。"""
    cfg = load_config(workspace, create=False)
    return TaskForest(cfg).snapshot()


def stop_flag_present(workspace: Path) -> bool:
    import os
    return os.path.lexists(workspace / _STOP_FILE)


def _fmt_bytes(value) -> str:
    if not isinstance(value, int):
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "-"


def render(payload: dict) -> str:
    lines = []
    leaf = payload["leaf"]
    lines.append("=" * 72)
    lines.append("§%s  %s" % (leaf["section_no"], leaf["title"]))
    lines.append("状态：%s%s" % (leaf["state_label"],
                             "" if leaf["is_blocked"] else "（非卡住）"))
    lines.append("=" * 72)
    lines.append("")
    lines.append("[1] 阻塞原因  %s" % payload["blocked_reason"]["kind"])
    lines.append("    %s" % payload["blocked_reason"]["detail"])
    deps = payload["blocked_reason"]["unmet_dependencies"]
    if deps:
        lines.append("    未满足前置：%s" % ", ".join(str(d) for d in deps))
    lines.append("")
    hb = payload["last_valid_heartbeat"]
    lines.append("[2] 最后有效心跳")
    if not hb["available"]:
        lines.append("    无（%s）" % hb["reason"])
    else:
        age = hb["age_s"] if hb["age_s"] is not None else "未知"
        lines.append("    来源=%s  有效=%s  距上次=%s 秒  过期=%s"
                     % (hb["source"], "是" if hb["valid"] else "否", age,
                        "是" if hb["stale"] else "否"))
        if hb["reason"]:
            lines.append("    判定依据：%s" % hb["reason"])
    lines.append("")
    art = payload["existing_artifacts"]
    lines.append("[3] 现有成果  %d 项（其中 %d 项带完整 sha256）"
                 % (art["count"], art["complete_hashes"]))
    for row in art["rows"]:
        lines.append("    - %s  %s  %s" % (row["path"] or "(未记录路径)",
                                          _fmt_bytes(row["bytes"]),
                                          (row["sha256"][:16] + "…") if row["sha256"] else "无摘要"))
    if art["count"] > len(art["rows"]):
        lines.append("    …（仅显示前 %d 项）" % len(art["rows"]))
    lines.append("")
    claimer = payload["original_claimer"]
    lines.append("[4] 原认领成员 / 会话")
    lines.append("    成员=%s  会话=%s  认领代次=%s"
                 % (claimer["agent"] or "无", claimer["conversation_id"] or "无",
                    claimer["claim_epoch"] if claimer["claim_epoch"] is not None else "-"))
    lines.append("    %s" % claimer["note"])
    lines.append("")
    stop = payload["stop_state"]
    lines.append("[5] 可验证的停止状态：%s" % stop["value"])
    if stop["evidence"]:
        for item in stop["evidence"]:
            lines.append("    ✓ [%s] %s" % (item["kind"], item["detail"]))
    else:
        lines.append("    ✗ 没有任何正面停止证据（心跳过期**不算**停止证据）")
    lines.append("    票据释放：%s（%s）"
                 % ("是" if payload["ticket"]["released"] else "否",
                    payload["ticket"]["note"]))
    lines.append("    允许接手：%s" % ("是" if payload["takeover_allowed"] else "否"))
    lines.append("")
    lines.append("[6] 下一句指令")
    lines.append("    %s" % payload["next_instruction"])
    lines.append("")
    return "\n".join(lines)


def _blocked_sections(forest: dict, stop_flag: bool, events) -> list:
    out = []
    for leaf in forest.get("leaves", []):
        if not isinstance(leaf, dict):
            continue
        section = leaf.get("section_no") or ""
        if not section:
            continue
        summary = rc.blocked_handover(forest, section_no=section,
                                      events=events, stop_flag=stop_flag)
        if summary.get("found") and summary["leaf"]["is_blocked"]:
            out.append(summary)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="卡住任务的接手说明（只读）")
    parser.add_argument("--workspace", required=True, help="项目根目录")
    parser.add_argument("--leaf", default="", help="章节号，如 6.2；缺省则列出全部卡住的叶子")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print("工作区不存在：%s" % workspace, file=sys.stderr)
        return 2
    forest = load_forest(workspace)
    stop_flag = stop_flag_present(workspace)
    events = forest.get("events") if isinstance(forest.get("events"), list) else []

    if args.leaf:
        payload = rc.blocked_handover(forest, section_no=args.leaf,
                                      events=events, stop_flag=stop_flag)
        if args.json:
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 0 if payload.get("found") else 1
        if not payload.get("found"):
            print(payload["error"], file=sys.stderr)
            return 1
        print(render(payload))
        return 0

    summaries = _blocked_sections(forest, stop_flag, events)
    if args.json:
        json.dump({"schema_version": 1, "read_only": True,
                   "stop_flag": stop_flag, "count": len(summaries),
                   "items": summaries}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    print("卡住的叶子：%d 个%s" % (len(summaries), "（工作区处于 STOP）" if stop_flag else ""))
    if not summaries:
        return 0
    for summary in summaries:
        print("")
        print("  §%-6s %-38s 停止=%s  可接手=%s"
              % (summary["leaf"]["section_no"],
                 summary["leaf"]["title"][:38],
                 "已确认" if summary["stop_state"]["verifiable"] else "未确认",
                 "是" if summary["takeover_allowed"] else "否"))
        print("      原因: %s" % summary["blocked_reason"]["kind"])
        print("      指令: %s" % summary["next_instruction"][:120])
    print("")
    print("用 --leaf <章节号> 查看某一页的完整接手说明。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
