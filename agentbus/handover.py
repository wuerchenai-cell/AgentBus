"""Human-readable handover digest for the live task forest.

Source: Owner 2026-10-11 (export a project handover summary as Markdown) / WorkBuddy.

This is a read-only projection of records that already exist. It never dispatches,
never claims, never writes and never invents progress or delivery evidence: a task
is reported as delivered only when the snapshot already carries a verified receipt.
"""
from __future__ import annotations

import hashlib

FILENAME = "项目进度与交付归档.md"
_KINDS = ("roots", "branches", "leaves")
MAX_ROWS = 2000
MAX_ARTIFACTS_PER_LEAF = 40
MAX_CELL = 240

STATE_LABELS = {
    "queued": "待认领",
    "claimed": "已认领，运行未确认",
    "running": "执行中（有心跳）",
    "delivered": "已交付（有正式回执）",
    "awaiting_receipt": "待核对回执",
    "awaiting_audit": "等待独立审核",
    "rejected": "审核未批准",
    "blocked": "受阻",
    "missing": "记录缺失",
    "stopped": "已暂停",
}

LEVEL_LABELS = {"roots": "项目目标", "branches": "工作分支", "leaves": "具体任务"}

# Branch/root state is an aggregate of its children; these are the only labels a
# reader needs to tell "everything below is done" from "something below is stuck".
_ATTENTION_STATES = ("blocked", "missing", "rejected", "awaiting_receipt", "awaiting_audit",
                     "claimed", "running", "stopped")


def state_label(state):
    """Human label for a snapshot state; unknown values stay explicitly unconfirmed."""
    text = str(state or "").strip()
    return STATE_LABELS.get(text, "未确认")


def handover_filename():
    return FILENAME


def _cell(value, maximum=MAX_CELL):
    text = "" if value is None else str(value)
    for chunk in ("\r\n", "\r", "\n"):
        text = text.replace(chunk, " ")
    text = text.replace("|", "\\|").strip()
    if len(text) > maximum:
        text = text[: maximum - 1] + "…"
    return text


def _section_key(node):
    """Numeric chapter order, so 1.2 precedes 1.10 and 2 precedes 10."""
    section = str(node.get("section_no") or "")
    numbers = []
    for piece in section.split("."):
        numbers.append(int(piece) if piece.isdigit() else 0)
    return (len(numbers), numbers, str(node.get("id") or ""))


def _ordered(nodes):
    return sorted([node for node in nodes if isinstance(node, dict)], key=_section_key)


def _artifact_rows(artifacts):
    rows = []
    for item in artifacts or []:
        if isinstance(item, dict):
            path = item.get("path") or item.get("name") or ""
            size = item.get("bytes")
            digest = item.get("sha256") or ""
        else:
            path, size, digest = str(item), "", ""
        rows.append({"path": path, "bytes": size, "sha256": digest})
    return rows


def _next_step(node):
    state = str(node.get("state") or "")
    if state == "blocked":
        return "先核对断点与阻塞原因，再决定恢复或安全交接。"
    if state in ("awaiting_receipt", "awaiting_audit"):
        return "核对产物与回执是否齐全，缺项补齐后再确认。"
    if state in ("claimed", "running"):
        return "由原认领会话续接；需要换人时先确认原会话已停止。"
    if state == "rejected":
        return "按审核意见修改后重新提交，不要绕过审核。"
    if state == "queued":
        return "按前置依赖就绪后派发；依赖未完成前不要抢跑。"
    return "已交付；仅在需要复验时重读回执。"


def build_handover_markdown(snapshot, *, project_name="当前项目", workspace="",
                            output_root="", generated_at="", stopped=False):
    """Return a deterministic Markdown digest of the current forest snapshot."""
    if isinstance(snapshot, dict) and isinstance(snapshot.get("forest"), dict):
        forest = snapshot["forest"]
    elif isinstance(snapshot, dict):
        forest = snapshot
    else:
        forest = {}

    project_name = project_name or "当前项目"
    roots = _ordered(forest.get("roots") or [])
    branches = _ordered(forest.get("branches") or [])
    leaves = _ordered(forest.get("leaves") or [])
    nodes = roots + branches + leaves

    delivered = [node for node in leaves if node.get("state") == "delivered"]
    outstanding = [node for node in leaves if node.get("state") != "delivered"]
    attention = [node for node in leaves if node.get("state") in _ATTENTION_STATES]
    artifact_total = sum(len(_artifact_rows(node.get("artifacts"))) for node in leaves)

    lines = [
        "# 项目进度与交付归档",
        "",
        f"- 项目：{_cell(project_name, 200)}",
    ]
    if workspace:
        lines.append(f"- 控制目录：{_cell(workspace, 400)}")
    if output_root:
        lines.append(f"- 业务产出目录：{_cell(output_root, 400)}")
    lines.append(f"- 生成时间：{_cell(generated_at or '未记录', 80)}")
    lines.append(f"- 派工状态：{'已暂停新派发' if stopped else '正常运行'}")
    lines.append(
        f"- 任务节点：目标 {len(roots)} · 分支 {len(branches)} · 任务 {len(leaves)}；"
        f"已交付 {len(delivered)}／{len(leaves)}，待推进 {len(outstanding)}，"
        f"需关注 {len(attention)}，已登记产物 {artifact_total} 项")
    lines.append("")

    lines += [
        "## 一、交付进度总览",
        "",
        "| 章节号 | 层级 | 标题 | 状态 | 责任成员 | 前置依赖 | 正式回执 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    if not nodes:
        lines.append("| — | — | 尚未创建任务树 | 未确认 | — | — | — |")
    for node in nodes[:MAX_ROWS]:
        kind = "roots" if node in roots else "branches" if node in branches else "leaves"
        dependencies = node.get("dependencies") or []
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            _cell(node.get("section_no") or "—", 40),
            LEVEL_LABELS[kind],
            _cell(node.get("title"), 120),
            _cell(state_label(node.get("state")), 40),
            _cell(node.get("owner") or "—", 60),
            _cell("、".join(str(item) for item in dependencies) or "—", 120),
            _cell(node.get("result_id") or "—", 80),
        ))
    if len(nodes) > MAX_ROWS:
        lines.append(f"| … | … | 另有 {len(nodes) - MAX_ROWS} 个节点未列出 | … | … | … | … |")
    lines.append("")

    lines += [
        "## 二、交付成果与摘要（SHA256）",
        "",
        "| 章节号 | 任务 | 产物路径 | 字节数 | SHA256 |",
        "| --- | --- | --- | --- | --- |",
    ]
    artifact_rows = 0
    for node in leaves:
        rows = _artifact_rows(node.get("artifacts"))
        for row in rows[:MAX_ARTIFACTS_PER_LEAF]:
            artifact_rows += 1
            lines.append("| %s | %s | %s | %s | %s |" % (
                _cell(node.get("section_no") or "—", 40),
                _cell(node.get("title"), 120),
                _cell(row["path"] or "未记录", 400),
                _cell(row["bytes"] if row["bytes"] not in (None, "") else "未记录", 40),
                _cell(row["sha256"] or "未记录", 80),
            ))
    if not artifact_rows:
        lines.append("| — | — | 尚无已核验产物记录 | — | — |")
    lines.append("")

    lines += [
        "## 三、待推进与需关注项",
        "",
        "| 章节号 | 标题 | 状态 | 现有结论 | 下一步 |",
        "| --- | --- | --- | --- | --- |",
    ]
    if not outstanding:
        lines.append("| — | — | 全部任务已交付 | — | 需要复验时重读回执即可 |")
    for node in outstanding[:MAX_ROWS]:
        lines.append("| %s | %s | %s | %s | %s |" % (
            _cell(node.get("section_no") or "—", 40),
            _cell(node.get("title"), 120),
            _cell(state_label(node.get("state")), 40),
            _cell(node.get("results") or node.get("evidence_error") or "尚无正式成果回执。", 240),
            _cell(_next_step(node), 200),
        ))
    lines.append("")

    lines += [
        "## 四、边界说明",
        "",
        "- 本归档由**已完成记录**投影生成，不触发派发、认领、执行或任何状态变更。",
        "- 状态来自正式总线记录；只有存在通过核验的正式回执才记为「已交付」。",
        "- 「已认领」不等于正在执行；文件时间或窗口打开不能作为执行证明。",
        "- 产物清单为空只表示尚无已核验产物，不等于没有工作；请结合回执正文核对。",
        "- 未验证项按原样保留；本归档不替任何人补写结论。",
    ]
    return "\n".join(lines) + "\n"


def markdown_digest(text):
    """Raw UTF-8 byte digest of the rendered markdown, for the receipt manifest."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()
