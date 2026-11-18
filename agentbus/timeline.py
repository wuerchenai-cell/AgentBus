# -*- coding: utf-8 -*-
"""时间线回放：从审计事件与不可变消息重建因果线。

来源：OpenCode（平级成员），2026-10-08，任务 m-1791457584502-3c65ce
（product-convergence，OC-EVIDENCE），reply_to=m-1791457574691-fa3254。
采用计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…80cc3b5。

定位：**只读**。重建因果线并输出**单文件** HTML/Markdown。不写消息、不改任务、
**不执行审计日志里的任何内容**。

四种证据来源在输出里必须一眼可分，不得混为一谈：
  transcript   聊天转录——由有权限成员忠实转录，非原生通道
  self_report  成员自报——只证明「有人这么说过」
  native       原生回执——正式关联回执，可核验
  unverified   未验证——缺证据或证据损坏，不得当作通过

确定性输出：同一份输入必须产出逐字节相同的输出。因此
  - 时间戳缺失时用稳定占位符，不用当前时间；
  - 排序键完整确定，不用集合迭代序；
  - 不嵌入随机 ID、不嵌入生成时刻；
  - HTML 全部转义，含 & < > " '。
"""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import re
import time
from typing import Any

from .delivery_verify import (Report, _read_json, collect_receipts, load_events,
                              safe_relative, verify_artifacts, JOB_ID_RE)

SOURCE_TRANSCRIPT = "transcript"
SOURCE_SELF_REPORT = "self_report"
SOURCE_NATIVE = "native"
SOURCE_UNVERIFIED = "unverified"

SOURCE_LABEL = {
    SOURCE_TRANSCRIPT: "转录",
    SOURCE_SELF_REPORT: "自报",
    SOURCE_NATIVE: "原生回执",
    SOURCE_UNVERIFIED: "未验证",
}

UNKNOWN_TS = "0000-00-00T00:00:00"
MAX_NODES = 2000
MAX_BODY_CHARS = 400

REPLY_ID_RE = re.compile(r"^(?:m|rx)-[0-9]{10,20}-[0-9a-f]{6,32}$")


def _text(value, limit: int = MAX_BODY_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit] + "…"


def _ts(value) -> str:
    return value if isinstance(value, str) and value.strip() else UNKNOWN_TS


def classify_source(record: dict, *, artifact_verified: bool = False) -> str:
    """判定一条记录属于哪种证据来源。不自报升级为原生。

    「原生 + 产物摘要核验通过」才配 A 级；只声明了原生运输但没有可核对的
    产物时，最高只能算转录——否则一个空的原生标记就能自称可信。
    """
    if not isinstance(record, dict):
        return SOURCE_UNVERIFIED
    transport = record.get("transport")
    native = isinstance(transport, str) and transport.strip().lower() in (
        "native", "program", "registration")
    if record.get("self_report") or record.get("self_reported"):
        return SOURCE_SELF_REPORT
    if record.get("type") != "result":
        return SOURCE_UNVERIFIED
    if native and artifact_verified:
        return SOURCE_NATIVE
    return SOURCE_TRANSCRIPT


def detect_cycles(records: list[dict]) -> list[list[str]]:
    """找出 reply_to 构成的环。返回若干条环路径，每条以起点重复结尾。

    循环必须报出来而不是静默截断——否则因果线会假装自己是一条链。
    """
    parent: dict[str, str] = {}
    for record in records:
        rid = str(record.get("id") or "")
        reply = record.get("reply_to")
        if rid and isinstance(reply, str) and reply:
            parent[rid] = reply
    cycles: list[list[str]] = []
    seen_cycles: set[tuple] = set()
    colour: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        colour[node] = 1
        stack.append(node)
        nxt = parent.get(node)
        if nxt is not None:
            if colour.get(nxt, 0) == 0:
                visit(nxt)
            elif colour.get(nxt) == 1:
                index = stack.index(nxt)
                cycle = stack[index:] + [nxt]
                key = tuple(sorted(set(cycle)))
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(cycle)
        stack.pop()
        colour[node] = 2

    for node in sorted(parent):
        if colour.get(node, 0) == 0:
            visit(node)
    return cycles


def build_timeline(bus_root, job_id: str | None = None, *,
                   now: float | None = None, output_root=None) -> dict:
    """只读重建因果线。

    job_id 给定时只收该作业的任务；不给则收全部。输出是纯数据，to_html/to_markdown
    再由它渲染，两者都必须确定性。
    """
    bus_root = Path(bus_root)
    if job_id is not None and not JOB_ID_RE.fullmatch(job_id):
        raise ValueError('invalid job ID')
    if output_root is None:
        cfg, error = _read_json(bus_root / 'config.json')
        configured = cfg.get('output_root') if isinstance(cfg, dict) else None
        output_root = Path(configured) if isinstance(configured, str) and configured else bus_root.parent
        if not output_root.is_absolute():
            output_root = bus_root.parent / output_root
    output_root = Path(output_root)
    events, damaged = load_events(bus_root)

    records: dict[str, dict] = {}
    agents_root = bus_root / "agents"
    task_ids: set[str] = set()
    job_data: dict = {}
    if isinstance(job_id, str) and job_id:
        data, error = _read_json(bus_root / "jobs" / ("%s.json" % job_id))
        if not error and isinstance(data, dict):
            job_data = data
            for task in data.get("tasks") or []:
                if isinstance(task, dict) and task.get("id"):
                    task_ids.add(str(task["id"]))

    if agents_root.is_dir():
        for path in sorted(agents_root.glob("*/*/*.json")):
            data, error = _read_json(path)
            if error or not isinstance(data, dict):
                continue
            rid = str(data.get("id") or path.stem)
            entry = {
                "id": rid,
                "ts": _ts(data.get("ts")),
                "sender": _text(data.get("sender"), 64),
                "to": _text(data.get("to"), 64),
                "type": _text(data.get("type"), 32),
                "title": _text(data.get("title"), 120),
                "body": _text(data.get("body")),
                "reply_to": data.get("reply_to") if isinstance(data.get("reply_to"), str) else "",
                "result_to": data.get("result_to") if isinstance(data.get("result_to"), str) else "",
                "status": _text(data.get("status"), 32),
                "claimed_by": _text(data.get("claimed_by"), 64),
                "claim_epoch": data.get("claim_epoch") if isinstance(data.get("claim_epoch"), int) else 0,
                "source_claim_id": _text(data.get("source_claim_id"), 64),
                "artifacts": data.get("artifacts") if isinstance(data.get("artifacts"), list) else [],
                # 运输方式与自报标记必须带进节点，否则 classify_source 无从判断来源。
                "transport": _text(data.get("transport"), 64),
                "self_report": bool(data.get("self_report") or data.get("self_reported")),
                "location": path.parent.name,
                "unreadable": False,
            }
            records[rid] = entry
            if not task_ids:
                task_ids.add(rid)
            elif rid in task_ids or entry.get("reply_to") in task_ids:
                pass

    if job_data and task_ids:
        # 作业模式：只保留作业任务及其关联回执。
        allowed = set(task_ids)
        for _ in range(4):  # 有限深度展开 reply 链，避免无限增长。
            grew = False
            for rid, entry in records.items():
                if entry["reply_to"] in allowed and rid not in allowed:
                    allowed.add(rid)
                    grew = True
            if not grew:
                break
        records = {rid: entry for rid, entry in records.items() if rid in allowed}

    nodes = sorted(records.values(),
                   key=lambda n: (n["ts"], n["type"], n["id"]))[:MAX_NODES]
    # 产物是否核验通过必须实测，不能只信声明：与 delivery_verify 用同一把尺子。
    for node in nodes:
        node["artifact_verified"] = False
        if node["type"] == "result" and node["artifacts"]:
            ok, _problems = verify_artifacts(output_root, node["artifacts"])
            node["artifact_verified"] = bool(ok)
        node["source"] = classify_source(node,
                                         artifact_verified=node["artifact_verified"])
        node["source_label"] = SOURCE_LABEL[node["source"]]

    truncated = len(records) > MAX_NODES
    duplicate_ids: list[str] = []
    if agents_root.is_dir():
        counts: dict[str, int] = {}
        for path in sorted(agents_root.glob("*/*/*.json")):
            data, error = _read_json(path)
            if error or not isinstance(data, dict):
                continue
            rid = str(data.get("id") or path.stem)
            counts[rid] = counts.get(rid, 0) + 1
        duplicate_ids = sorted(rid for rid, n in counts.items() if n > 1)

    event_nodes = []
    for event in events:
        if not isinstance(event, dict):
            continue
        eid = str(event.get("id") or "")
        if job_data and task_ids and eid not in task_ids and \
                event.get("agent") not in {n["sender"] for n in nodes}:
            continue
        event_nodes.append({
            "ts": _ts(event.get("ts")),
            "event": _text(event.get("event"), 64),
            "agent": _text(event.get("agent"), 64),
            "id": eid,
            "detail": {k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                       for k, v in sorted(event.items())
                       if k not in ("event", "ts", "agent", "id")},
        })
    event_nodes.sort(key=lambda e: (e["ts"], e["event"], e["id"], e["agent"]))

    cycles = detect_cycles([n for n in nodes])
    late = []
    late_dir = bus_root / "late-results"
    if late_dir.is_dir():
        for task_dir in sorted(late_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            for path in sorted(task_dir.glob("*.json")):
                data, error = _read_json(path)
                late.append({
                    "task_id": task_dir.name,
                    "file": path.name,
                    "reason_code": _text((data or {}).get("reason_code"), 64) if data else "unreadable",
                    "readable": not error,
                })

    return {
        "schema_version": 1,
        "tool": "timeline",
        "job_id": job_id if isinstance(job_id, str) else "",
        "job_state": _text(job_data.get("state"), 32),
        "nodes": nodes,
        "events": event_nodes[:MAX_NODES],
        "event_damaged": damaged,
        "cycles": cycles,
        "duplicate_ids": duplicate_ids,
        "late_results": late,
        "truncated": truncated,
        "counts": {
            "nodes": len(nodes),
            "events": min(len(event_nodes), MAX_NODES),
            "transcript": sum(1 for n in nodes if n["source"] == SOURCE_TRANSCRIPT),
            "self_report": sum(1 for n in nodes if n["source"] == SOURCE_SELF_REPORT),
            "native": sum(1 for n in nodes if n["source"] == SOURCE_NATIVE),
            "unverified": sum(1 for n in nodes if n["source"] == SOURCE_UNVERIFIED),
        },
        "notes": [
            "只读回放：未写任何消息、未改任务、未执行审计日志中的任何内容。",
            "时间戳缺失时使用固定占位符 %s，不使用当前时间，以保证输出确定性。" % UNKNOWN_TS,
        ],
    }


def to_markdown(timeline: dict) -> str:
    """渲染单文件 Markdown。确定性：不引入当前时间、不使用随机 ID。"""
    data = timeline if isinstance(timeline, dict) else {}
    counts = data.get("counts") or {}
    lines = ["# 交付时间线回放", ""]
    if data.get("job_id"):
        lines.append("作业：`%s`（状态 `%s`）" % (data["job_id"], data.get("job_state") or "未知"))
    else:
        lines.append("作业：未指定（全部记录）")
    lines.append("")
    lines.append("## 证据分布")
    lines.append("")
    lines.append("| 来源 | 数量 |")
    lines.append("|---|---|")
    for key in ("native", "transcript", "self_report", "unverified"):
        lines.append("| %s | %d |" % (SOURCE_LABEL[key], counts.get(key, 0)))
    lines.append("")
    if data.get("event_damaged"):
        lines.append("> 审计有 %d 条损坏行；已计数保留，未静默丢弃。" % data["event_damaged"])
        lines.append("")
    if data.get("duplicate_ids"):
        lines.append("> 重复消息 ID：%s" % "、".join(data["duplicate_ids"]))
        lines.append("")
    if data.get("cycles"):
        lines.append("> 检测到 reply 循环（因果不是一条链）：")
        for cycle in data["cycles"]:
            lines.append("> - %s" % " → ".join(cycle))
        lines.append("")
    if data.get("late_results"):
        lines.append("## 迟到回执（已隔离，未推进任务）")
        lines.append("")
        for item in data["late_results"]:
            lines.append("- `%s` / `%s`：%s%s" % (item["task_id"], item["file"],
                                                item["reason_code"],
                                                "" if item["readable"] else "（文件不可读）"))
        lines.append("")

    lines.append("## 因果线")
    lines.append("")
    lines.append("| 时间 | ID | 类型 | 来源 | from → to | 说明 |")
    lines.append("|---|---|---|---|---|---|")
    for node in data.get("nodes") or []:
        detail = node.get("title") or node.get("body") or ""
        detail = detail.replace("|", "\\|").replace("\n", " ")
        lines.append("| %s | `%s` | %s | %s | %s → %s | %s |" % (
            node.get("ts", ""), node.get("id", ""), node.get("type", ""),
            node.get("source_label", ""), node.get("sender", ""),
            node.get("to", ""), detail))
    lines.append("")
    lines.append("## 审计事件")
    lines.append("")
    lines.append("| 时间 | 事件 | 成员 | 关联 ID |")
    lines.append("|---|---|---|---|")
    for event in data.get("events") or []:
        lines.append("| %s | %s | %s | %s |" % (event.get("ts", ""), event.get("event", ""),
                                                event.get("agent", ""), event.get("id", "") or ""))
    lines.append("")
    lines.append("## 边界")
    lines.append("")
    for note in data.get("notes") or []:
        lines.append("- %s" % note)
    lines.append("")
    return "\n".join(lines)


def to_html(timeline: dict) -> str:
    """渲染单文件 HTML。全部动态内容经 html.escape 转义。"""
    data = timeline if isinstance(timeline, dict) else {}
    esc = html.escape

    def cell(value) -> str:
        return esc(str(value if value is not None else ""))

    counts = data.get("counts") or {}
    out = [
        "<!DOCTYPE html>",
        '<html lang="zh"><head><meta charset="utf-8">',
        "<title>交付时间线回放</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.6}",
        "table{border-collapse:collapse;width:100%;margin:1rem 0}",
        "th,td{border:1px solid #ccc;padding:.4rem .6rem;text-align:left;vertical-align:top}",
        "th{background:#f4f4f4}",
        ".native{background:#e6f4ea}.transcript{background:#fff4e5}",
        ".self_report{background:#eef}.unverified{background:#fdd}",
        ".warn{border-left:4px solid #c33;padding:.5rem 1rem;background:#fdf3f3}",
        "</style></head><body>",
        "<h1>交付时间线回放</h1>",
        "<p>作业：<code>%s</code>（状态 <code>%s</code>）</p>" % (
            cell(data.get("job_id") or "未指定"),
            cell(data.get("job_state") or "未知")),
        "<h2>证据分布</h2>",
        "<table><tr><th>来源</th><th>数量</th></tr>",
    ]
    for key in ("native", "transcript", "self_report", "unverified"):
        out.append('<tr class="%s"><td>%s</td><td>%d</td></tr>' % (
            esc(key), esc(SOURCE_LABEL[key]), int(counts.get(key, 0) or 0)))
    out.append("</table>")

    if data.get("event_damaged"):
        out.append('<p class="warn">审计有 %d 条损坏行；已计数保留，未静默丢弃。</p>'
                   % int(data["event_damaged"]))
    if data.get("duplicate_ids"):
        out.append('<p class="warn">重复消息 ID：<code>%s</code></p>'
                   % esc("、".join(data["duplicate_ids"])))
    if data.get("cycles"):
        out.append('<p class="warn">检测到 reply 循环（因果不是一条链）：<br>')
        for cycle in data["cycles"]:
            out.append("%s<br>" % esc(" → ".join(cycle)))
        out.append("</p>")

    out.append("<h2>因果线</h2>")
    out.append("<table><tr><th>时间</th><th>ID</th><th>类型</th><th>来源</th>"
               "<th>from &rarr; to</th><th>说明</th></tr>")
    for node in data.get("nodes") or []:
        detail = node.get("title") or node.get("body") or ""
        out.append('<tr class="%s"><td>%s</td><td><code>%s</code></td><td>%s</td>'
                   "<td>%s</td><td>%s &rarr; %s</td><td>%s</td></tr>" % (
                       esc(node.get("source", "")), cell(node.get("ts")),
                       cell(node.get("id")), cell(node.get("type")),
                       cell(node.get("source_label")), cell(node.get("sender")),
                       cell(node.get("to")), cell(detail)))
    out.append("</table>")

    if data.get("late_results"):
        out.append("<h2>迟到回执（已隔离，未推进任务）</h2><ul>")
        for item in data["late_results"]:
            out.append("<li><code>%s</code> / <code>%s</code>：%s%s</li>" % (
                cell(item["task_id"]), cell(item["file"]), cell(item["reason_code"]),
                "" if item.get("readable") else "（文件不可读）"))
        out.append("</ul>")

    out.append("<h2>审计事件</h2>")
    out.append("<table><tr><th>时间</th><th>事件</th><th>成员</th><th>关联 ID</th></tr>")
    for event in data.get("events") or []:
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td></tr>" % (
            cell(event.get("ts")), cell(event.get("event")),
            cell(event.get("agent")), cell(event.get("id") or "")))
    out.append("</table>")

    out.append("<h2>边界</h2><ul>")
    for note in data.get("notes") or []:
        out.append("<li>%s</li>" % esc(note))
    out.append("</ul></body></html>")
    return "\n".join(out)


def render(timeline: dict, fmt: str = "markdown") -> str:
    fmt = str(fmt or "markdown").lower()
    if fmt in ("md", "markdown"):
        return to_markdown(timeline)
    if fmt == "html":
        return to_html(timeline)
    raise ValueError("format must be markdown or html, got %r" % (fmt,))


def timeline_digest(text: str) -> str:
    """给确定性测试用：对渲染结果取摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
