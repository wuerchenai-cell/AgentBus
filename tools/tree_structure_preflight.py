#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tree_structure_preflight.py: 任务树结构写入前的**可定位**预检（只读）。

来源：2026-10-13 一次 P0 事故的自造教训 —— 本席给三叶写了非法字段 `priority`
（`priority` 是**投影字段**：`tree status` 输出里有，但**不可存储**），
于是**两个入口同时报错**、整棵树约 3 分钟对全体成员不可读：

    structure nodes accept goals and bindings, never state/results/owner

那一句报错**不告诉你是谁、哪个字段、哪一片叶**。本工具补的就是这一步。

定位 vs 判据
------------
* **判据（唯一真源）**：直接调用 `agentbus.task_tree.TaskForest._normalize`，
  与正式写入路径**同一份**校验代码，不做任何复制或重写；
* **定位（本工具的增量）**：并列一份**逐节点、逐字段**的问题清单，
  明确 `节点 id / 章节号 / 非法键名 / 实际值`，让写入者能**定点收窄**而不是整体回退；
* 二者都必须干净，才判 `clean`。**只过定位不等于能写**（真源没验过就不算数），
  反之真源报错而定位为空时，工具会如实说明"真源报错但定位器未覆盖该规则"，不假装干净。

纪律
----
* **只读**：不写任务树、不改配置、不移动文件；只报告。
* 支持 `--file` 校验**候选**结构（推荐：先写出候选文件，再预检，最后才落盘）；
  省略 `--file` 时校验**当前活动** `.agentbus/task-tree.json`（只读体检）。
* `--compare-current`：把候选与当前活动树对比（新增/删除/改动的节点与改动键名），
  防止"本想改一片叶、实际动了整棵树"。
* `--strict`：判定非 clean 时以退出码 1 结束（供写前门禁 / CI 使用）。
* 缺输入或无法导入真源时退出码 2（**无法验证 ≠ 通过**）。

用法
----
    python tools/tree_structure_preflight.py --file 候选树.json [--workspace .] [--json]
    python tools/tree_structure_preflight.py --workspace . --compare-current --strict
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent
_INTEGRATION = _TOOLS_DIR.parent
if str(_INTEGRATION) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION))


def _load_truth():
    """加载唯一真源校验器与常量；失败时返回 (None, 原因)。"""
    try:
        from agentbus import task_tree  # type: ignore
    except (ImportError, OSError) as exc:
        return None, "无法导入真源校验器 agentbus.task_tree：%s: %s" % (type(exc).__name__, exc)
    return task_tree, ""


def _bounded_text(value: Any, limit: int = 100000) -> bool:
    return isinstance(value, str) and len(value) <= limit


def locate(tree: Any, task_tree) -> list[dict]:
    """逐节点、逐字段定位结构问题（与 `_NODE_KEYS` 等真源常量对齐）。"""
    problems: list[dict] = []

    def add(node_ref: str, code: str, detail: str) -> None:
        problems.append({"node": node_ref, "code": code, "detail": detail})

    if not isinstance(tree, dict):
        add("<root>", "not_object", "结构文件顶层必须是 JSON 对象，实测 %s" % type(tree).__name__)
        return problems
    if tree.get("schema_version", 1) != 1:
        add("<root>", "bad_schema_version",
            "schema_version 必须是 1，实测 %r" % (tree.get("schema_version"),))

    allowed = set(getattr(task_tree, "_NODE_KEYS", ()))
    section_re = getattr(task_tree, "_SECTION", None)
    safe_re = getattr(task_tree, "_SAFE", None)
    history_keys = set(getattr(task_tree, "_HANDOFF_HISTORY_KEYS", ()))

    registry = tree.get("section_registry", [])
    if not isinstance(registry, list) or len(registry) > 12000:
        add("section_registry", "bad_registry", "section_registry 必须是长度 ≤ 12000 的数组")
    elif section_re is not None:
        for value in registry:
            if not isinstance(value, str) or not section_re.fullmatch(value):
                add("section_registry", "bad_section_no",
                    "非法章节号 %r（必须形如 0 / 12 / 12.3）" % (value,))

    all_nodes: dict[str, tuple[str, dict]] = {}
    task_binding: dict[str, list[str]] = {}

    for kind in ("roots", "branches", "leaves"):
        values = tree.get(kind, [])
        if not isinstance(values, list) or len(values) > 1000:
            add(kind, "bad_list", "%s 必须是长度 ≤ 1000 的数组" % kind)
            continue
        for index, item in enumerate(values):
            ref = "%s[%d]" % (kind, index)
            if not isinstance(item, dict):
                add(ref, "not_object", "节点必须是 JSON 对象，实测 %s" % type(item).__name__)
                continue
            node_id = item.get("id")
            ref = "%s %s" % (kind, node_id if isinstance(node_id, str) else ref)

            # ---- 最要命的一条：非法键（原 P0 就是死在这里）----
            illegal = sorted(set(item) - allowed)
            if illegal:
                add(ref, "illegal_keys",
                    "含不可存储的键 %s —— 结构节点只接受 %s 这些键（提示：state/results/owner/priority/"
                    "status/claim/heartbeat 是**投影**字段，只能出现在 tree status 输出里，不可写回结构）"
                    % (illegal, sorted(allowed)))

            if not isinstance(node_id, str) or (safe_re is not None and not safe_re.fullmatch(node_id)):
                add(ref, "bad_id", "id 必须是 [A-Za-z0-9][A-Za-z0-9._:-]{0,127}，实测 %r" % (node_id,))
                continue
            if node_id in all_nodes:
                add(ref, "duplicate_id", "id %s 重复" % node_id)
                continue
            all_nodes[node_id] = (kind, item)

            if "section_no" in item and section_re is not None and not section_re.fullmatch(str(item["section_no"])):
                add(ref, "bad_section_no", "非法章节号 %r" % (item["section_no"],))

            for key in ("title", "goal", "handoff"):
                if key in item and not _bounded_text(item.get(key)):
                    add(ref, "bad_text", "%s 必须是有界文本（≤ 100000 字符），实测 %s" % (key, type(item.get(key)).__name__))

            if "progress_percent" in item:
                progress = item["progress_percent"]
                if kind != "leaves":
                    add(ref, "progress_on_non_leaf", "progress_percent 只能出现在 leaves，实测在 %s" % kind)
                elif type(progress) is not int or not 0 <= progress <= 100:
                    add(ref, "bad_progress", "progress_percent 必须是 0..100 的整数，实测 %r" % (progress,))

            parent_id = item.get("parent_id")
            if kind == "roots" and parent_id is not None:
                add(ref, "root_with_parent", "roots 不得有 parent_id，实测 %r" % (parent_id,))

            for key in ("task_id", "job_id", "result_to"):
                value = item.get(key)
                if value and (not isinstance(value, str) or (safe_re is not None and not safe_re.fullmatch(value))):
                    add(ref, "unsafe_" + key, "%s 不是安全标识，实测 %r" % (key, value))

            for key in ("dependencies", "history_task_ids", "required_capabilities"):
                values_list = item.get(key, [])
                if not isinstance(values_list, list) or len(values_list) > 1000:
                    add(ref, "bad_list", "%s 必须是长度 ≤ 1000 的数组" % key)
                    continue
                for entry in values_list:
                    if not isinstance(entry, str) or (safe_re is not None and not safe_re.fullmatch(entry)):
                        add(ref, "bad_" + key, "%s 含非法条目 %r" % (key, entry))

            if "handoff_history" in item:
                if kind != "leaves":
                    add(ref, "handoff_on_non_leaf", "只有 leaves 能存 handoff_history")
                else:
                    history = item["handoff_history"]
                    if not isinstance(history, list) or len(history) > 100:
                        add(ref, "bad_handoff_history", "handoff_history 必须是长度 ≤ 100 的数组")
                    else:
                        known_tasks = item.get("history_task_ids", [])
                        for record in history:
                            if not isinstance(record, dict) or set(record) != history_keys:
                                add(ref, "bad_handoff_record",
                                    "handoff_history 记录必须且只能含 %s，实测 %s"
                                    % (sorted(history_keys), sorted(record) if isinstance(record, dict) else record))
                                continue
                            if record.get("task_id") not in known_tasks:
                                add(ref, "orphan_handoff_task",
                                    "handoff_history.task_id %r 不在本叶 history_task_ids 里" % (record.get("task_id"),))
                            if type(record.get("owner_confirmed_inactive")) is not bool:
                                add(ref, "bad_handoff_flag", "owner_confirmed_inactive 必须是布尔值")

            if kind != "leaves" and item.get("task_id"):
                add(ref, "task_on_non_leaf", "只有 leaves 能绑定 task_id")
            if kind == "leaves" and isinstance(item.get("task_id"), str):
                task_binding.setdefault(item["task_id"], []).append(node_id)

    for task_id, owners in task_binding.items():
        if len(owners) > 1:
            problems.append({"node": ",".join(owners), "code": "task_bound_twice",
                             "detail": "同一条任务消息 %s 被 %d 片叶同时绑定" % (task_id, len(owners))})

    # ---- 引用完整性 ----
    for node_id, (kind, item) in all_nodes.items():
        parent = item.get("parent_id")
        if parent is not None:
            target = all_nodes.get(parent)
            if target is None or target[0] == "leaves":
                problems.append({"node": "%s %s" % (kind, node_id), "code": "bad_parent",
                                 "detail": "parent_id %r 不存在或指向叶子（父必须是 root/branch）" % (parent,)})
        elif kind != "roots":
            problems.append({"node": "%s %s" % (kind, node_id), "code": "missing_parent",
                             "detail": "%s 必须有 parent_id" % kind})
        for dependency in item.get("dependencies", []) or []:
            if dependency not in all_nodes:
                problems.append({"node": "%s %s" % (kind, node_id), "code": "bad_dependency",
                                 "detail": "依赖 %r 不在树里" % (dependency,)})
            elif dependency == node_id:
                problems.append({"node": "%s %s" % (kind, node_id), "code": "self_dependency",
                                 "detail": "节点不能依赖自己"})
    return problems


def authoritative(tree: Any, task_tree) -> tuple[bool, str]:
    """调用真源校验器；返回 (是否通过, 报错文本)。"""
    try:
        task_tree.TaskForest._normalize(tree)
    except Exception as exc:  # noqa: BLE001 - 真源用 ValueError 子类表达全部结构问题
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, ""


def compare(candidate: Any, current: Any) -> dict:
    """候选与当前活动树的结构差异（节点级 + 键级）。"""
    def index(tree: Any) -> dict:
        out = {}
        if not isinstance(tree, dict):
            return out
        for kind in ("roots", "branches", "leaves"):
            for node in tree.get(kind) or []:
                if isinstance(node, dict) and isinstance(node.get("id"), str):
                    out[node["id"]] = (kind, node)
        return out

    before, after = index(current), index(candidate)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = []
    for node_id in sorted(set(before) & set(after)):
        old_kind, old = before[node_id]
        new_kind, new = after[node_id]
        keys = set(old) | set(new)
        delta = {}
        for key in sorted(keys):
            if old.get(key) != new.get(key):
                delta[key] = {"before": old.get(key), "after": new.get(key)}
        if old_kind != new_kind:
            delta["<kind>"] = {"before": old_kind, "after": new_kind}
        if delta:
            changed.append({"id": node_id, "keys": delta})
    return {"added": added, "removed": removed, "changed": changed}


def run(candidate_path: Path | None, workspace: Path | None) -> tuple[dict, int]:
    task_tree, reason = _load_truth()
    if task_tree is None:
        return {"ok": False, "error": reason, "verdict": "unverifiable"}, 2

    current_file = None
    if candidate_path is not None:
        target, current_path = candidate_path, workspace
    else:
        if workspace is None:
            return {"ok": False, "error": "需提供 --file 或 --workspace 之一", "verdict": "unverifiable"}, 2
        target, current_path = workspace / ".agentbus" / "task-tree.json", None
    try:
        tree = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "读取 %s 失败：%s: %s" % (target, type(exc).__name__, exc),
                "verdict": "unverifiable"}, 2

    located = locate(tree, task_tree)
    passed, message = authoritative(tree, task_tree)
    report: dict[str, Any] = {
        "ok": passed and not located,
        "source": str(target),
        "located_problems": located,
        "authoritative_ok": passed,
        "authoritative_error": message,
        "verdict": "clean" if (passed and not located) else "rejected",
    }
    if current_path is not None:
        current_file = current_path / ".agentbus" / "task-tree.json"
        if current_file.is_file():
            try:
                report["compare_to_current"] = compare(tree, json.loads(current_file.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                report["compare_to_current"] = {"error": "读取当前活动树失败：%s" % exc}
    if passed and located:
        report["note"] = "真源校验通过，但定位器报告了问题；两者不一致时以真源为准，并请复查定位器规则。"
    if not passed and not located:
        report["note"] = "真源报错但定位器未覆盖该规则；以真源为准，请补齐定位器。"
    return report, (0 if report["verdict"] == "clean" else 1)


def render(report: dict) -> str:
    lines = ["任务树结构写入前预检（只读）", "=" * 62]
    if "error" in report:
        lines.append("[无法验证] %s" % report["error"])
        lines.append("")
        lines.append("判定: unverifiable（无法验证 ≠ 通过）")
        return "\n".join(lines)
    lines.append("来源            : %s" % report.get("source"))
    lines.append("真源校验(_normalize): %s" % ("通过" if report.get("authoritative_ok") else "未通过"))
    if not report.get("authoritative_ok"):
        lines.append("  真源报错      : %s" % report.get("authoritative_error"))
    located = report.get("located_problems") or []
    lines.append("定位器问题数    : %d" % len(located))
    if located:
        lines.append("")
        lines.append("【定位明细】逐节点 / 逐字段（写回前必须清零）")
        for item in located:
            lines.append("  - [%s] %s" % (item["code"], item["node"]))
            lines.append("      %s" % item["detail"])
    diff = report.get("compare_to_current")
    if diff:
        lines.append("")
        lines.append("【与当前活动树对比】新增 %d ｜ 删除 %d ｜ 改动 %d"
                     % (len(diff.get("added") or []), len(diff.get("removed") or []), len(diff.get("changed") or [])))
        for node_id in (diff.get("added") or [])[:20]:
            lines.append("  + %s" % node_id)
        for node_id in (diff.get("removed") or [])[:20]:
            lines.append("  - %s" % node_id)
        for entry in (diff.get("changed") or [])[:20]:
            lines.append("  ~ %s  改动键: %s" % (entry["id"], ", ".join(entry["keys"])))
    if report.get("note"):
        lines.append("")
        lines.append("[提示] %s" % report["note"])
    lines.append("")
    lines.append("判定: %s" % report["verdict"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", default=None, help="候选结构 JSON（缺省时校验活动 .agentbus/task-tree.json）")
    parser.add_argument("--workspace", default=".", help="工作空间目录（含 .agentbus/）")
    parser.add_argument("--compare-current", action="store_true", help="把候选与当前活动树对比")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--strict", action="store_true", help="判定非 clean 时以非零退出")
    args = parser.parse_args(argv)

    candidate = Path(args.file) if args.file else None
    workspace = Path(args.workspace)
    if candidate is not None and not candidate.is_file():
        print("缺少输入文件：%s" % candidate, file=sys.stderr)
        return 2
    report, code = run(candidate, workspace if args.compare_current or candidate is None else None)
    if candidate is not None and not args.compare_current:
        # 未要求对比时不要附带对比结果，保持输出最小
        report.pop("compare_to_current", None)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    if args.strict and report.get("verdict") != "clean":
        return 1 if report.get("verdict") != "unverifiable" else 2
    return code


if __name__ == "__main__":
    sys.exit(main())
