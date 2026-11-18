# -*- coding: utf-8 -*-
"""enterprise_shell：品牌中立的企业桌面首页纯视图层（零 I/O，复用既有视图数据）。

来源：Trae（平级成员），2026-10-08；任务 m-1791457584495-f44d5b（TR-DESKTOP，作业 j-b7ed91331128d934d22268834e306bd4）。
复用：直接消费 enterprise_view.build_project_view 的公开输出（config/health/jobs 投影），
不复制总线事实、不新增事实源；状态灯来自 gui_state.derive_status_light。

边界（写死）：
- 首页围绕项目目标、当前责任、交付、阻塞与“唯一下一步”；成员列表完全动态，不出现硬编码成员数。
- 状态灯只能由明确证据得到；claim、窗口存在、文件 mtime、复制指令都不得点亮“正在执行”。
- 纯函数、确定性、坏输入降级为“目标未确认/状态未知”，不判空、不判离线、不抛异常。
"""
from __future__ import annotations

from . import design_system
from . import gui_state

__all__ = ["DEFAULT_NAV_IDS", "ACTION_LABELS", "GOAL_PLACEHOLDER", "build_shell_view"]

DEFAULT_NAV_IDS: tuple[str, ...] = ("home", "team", "tasks", "access", "settings", "logs")

# 目标占位符：上游未给真实目标时的哨兵，不视为“已确认目标”。
GOAL_PLACEHOLDER = "目标未确认"

ACTION_LABELS: dict[str, tuple[str, str]] = {
    "team": ("配置并保存团队", "还没有可派工的团队或项目未保存。"),
    "tasks": ("查看并处理任务", "有阻塞、暂停、读取错误或需要确认的任务。"),
    "access": ("获取说明并接手", "有任务待接手，需要接入说明。"),
    "home": ("输入目标并复制指令", "团队已配置，暂无待办；填写目标开始协作。"),
}

# workbench_view 的成员状态文案 → 本模块的十个状态灯之一。
_LABEL_TO_STATE = {
    "已停用": "disabled",
    "阻塞": "blocked",
    "有任务待接手": "ready",
    "已认领，运行未确认": "claimed",
    "暂停新派发": "handoff",
    "最近任务失败": "unknown",
    "状态未知": "unknown",
    "当前会话未确认": "unknown",
    "已配置": "unknown",
}


def _finite_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _fit(text, limit: int) -> str:
    """按显示宽度截断长名称（中文长路径/长名称可读），超长加省略号。"""
    if not isinstance(text, str):
        return ""
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _primary_action(view: dict, stopped: bool) -> dict:
    if stopped:
        return {"id": "tasks", "label": "查看暂停/阻塞原因",
                "reason": "项目已 STOP 或暂停新派发；先核对在途任务，不自动恢复。"}
    action_id = view.get("action_id")
    if action_id not in ACTION_LABELS:
        action_id = "home"
    label, reason = ACTION_LABELS[action_id]
    return {"id": action_id, "label": label, "reason": reason}


def _member_light(row: dict, evidence: dict, workspace: str) -> dict:
    if isinstance(evidence, dict):
        payload = dict(evidence)
        payload.setdefault("member", row.get("member_id"))
        payload.setdefault("workspace", workspace)
        return gui_state.derive_status_light(payload)
    state = _LABEL_TO_STATE.get(row.get("state_label"), "unknown")
    label, tone, token_key, symbol, reason = gui_state.STATUS_LIGHTS[state]
    return {
        "state": state, "label": label, "tone": tone,
        "color": design_system.TOKENS.get(token_key, design_system.TOKENS["muted"]),
        "symbol": symbol, "reason": reason,
        "next_action": gui_state.MEMBER_NEXT_ACTION[state],
        "contradiction": False, "execution_verified": False,
        "evidence_used": [], "member": row.get("member_id") or "",
        "workspace": workspace,
    }


def _members(view: dict, member_evidence, workspace: str, layout: dict) -> list:
    rows = view.get("responsibilities")
    base_members = view.get("members")
    coordinator = view.get("coordinator")
    by_id = {}
    for item in base_members if isinstance(base_members, list) else []:
        if isinstance(item, dict) and _finite_str(item.get("id")):
            by_id[item["id"]] = item

    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        mid = row.get("member_id")
        if not _finite_str(mid):
            continue
        base = by_id.get(mid, {})
        label_source = row.get("state") if _finite_str(row.get("state")) else base.get("state_label")
        light_row = {"member_id": mid, "state_label": label_source}
        evidence = member_evidence.get(mid) if isinstance(member_evidence, dict) else None
        light = _member_light(light_row, evidence, workspace)
        name_full = row.get("member") if _finite_str(row.get("member")) else mid
        out.append({
            "member_id": mid,
            "name": _fit(name_full, layout["wrap_title_chars"]),
            "name_full": name_full,
            "role": "负责人" if mid == coordinator else "成员",
            "task": row.get("task"),
            "checkpoint": row.get("checkpoint"),
            "state_label": light["label"],
            "status_light": light,
            "next_step": (row.get("next_step") if _finite_str(row.get("next_step"))
                          and not isinstance(evidence, dict) else light["next_action"]),
            "write_capability": row.get("write_capability"),
        })
    return out


def _cards(view: dict) -> list:
    cards = view.get("cards")
    out = []
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        out.append({
            "id": card.get("id"),
            "label": card.get("label"),
            "count": card.get("count") if isinstance(card.get("count"), int) else 0,
            "pending_verify": card.get("pending_verify") is True,
            "proxy": card.get("proxy"),
        })
    return out


def _empty_state(member_count: int, goal_confirmed: bool) -> dict | None:
    if member_count == 0:
        return {"id": "team", "label": "还没有成员",
                "action": "选择或创建项目，并勾选至少一名成员。"}
    if not goal_confirmed:
        return {"id": "home", "label": "还没有目标",
                "action": "输入目标，然后复制任务指令给成员。"}
    return None


def build_shell_view(view, *, layout: str | None = None, scaling: float = 1.0,
                     width_px=None, stopped: bool = False, member_evidence=None,
                     workspace: str = "", nav_ids=None) -> dict:
    """由 enterprise_view.build_project_view 输出构建企业桌面首页纯视图。

    - layout：显式档位（compact/normal/wide）；未给时按 width_px/scaling 自动选择。
    - member_evidence：可选 {member_id: evidence}，用于注入真实证据得到真实状态灯；
      未提供时按既有状态文案映射，缺证一律“未知”，不推断在线。
    - stopped：项目 STOP/暂停新派发时置 True，仅改主动作，不伪造执行。
    """
    view = view if isinstance(view, dict) else {}
    if layout is None:
        layout = design_system.choose_layout(width_px, scaling) if width_px is not None else "normal"
    profile = design_system.layout_profile(layout)

    raw_goal = view.get("goal")
    goal_confirmed = _finite_str(raw_goal) and raw_goal.strip() != GOAL_PLACEHOLDER
    goal = raw_goal if goal_confirmed else GOAL_PLACEHOLDER
    members = _members(view, member_evidence, workspace, profile)
    member_count = len(members)

    visible_limit = profile["max_member_rows"] * profile["member_columns"]
    visible_members = members[:visible_limit]
    overflow_count = max(0, member_count - visible_limit)

    shell = {
        "project_title": view.get("project_title") if _finite_str(view.get("project_title")) else "未选择项目",
        "goal": _fit(goal, profile["wrap_goal_chars"]),
        "goal_full": goal,
        "goal_confirmed": goal_confirmed,
        "stage": view.get("stage"),
        "stage_label": view.get("stage_label") if _finite_str(view.get("stage_label")) else "状态未确认",
        "headline": view.get("headline") if _finite_str(view.get("headline")) else "团队已配置，暂无待办",
        "primary_action": _primary_action(view, stopped),
        "cards": _cards(view),
        "members": members,
        "visible_members": visible_members,
        "overflow_count": overflow_count,
        "member_count": member_count,
        "light_summary": gui_state.status_light_summary([m["status_light"] for m in members]),
        "layout": profile,
        "stopped": bool(stopped),
        "records_incomplete": view.get("records_incomplete") is True,
        "incomplete_reason": view.get("incomplete_reason") or "",
        "workspace": workspace,
        "nav_ids": list(nav_ids) if isinstance(nav_ids, (list, tuple)) else list(DEFAULT_NAV_IDS),
    }
    shell["empty_state"] = _empty_state(member_count, goal_confirmed)
    shell["accessibility"] = {
        "focus_order": design_system.focus_order(shell),
        "shortcuts": design_system.keyboard_shortcuts(),
        "min_target_px": profile["min_target_px"],
        "font_points": dict(design_system.TYPE_SCALE),
    }
    return shell
