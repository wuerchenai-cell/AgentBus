# -*- coding: utf-8 -*-
"""异常救援中心纯模型：根据真实状态只给一个下一动作。

来源：Trae（平级成员），2026-10-08；任务 m-1791457584497-b68c42（TR-RESCUE），
作业 j-b7ed91331128d934d22268834e306bd4；计划快照 r-cdef881858336918798c18bb8f096f25。

边界（写死）：
- 纯函数：不读文件、不启进程、不调模型、不访问网络、不代登录、不抢 processing。
- 六个互斥动作，按确定性优先级**只返回一个**：
  continue / new_chat_pack / wake_word / computer_use / adapter / blocked。
- Computer Use、桌面窗口、适配器都不产生执行证据：`can_fake_execution` 恒为 False，
  `execution_verified` 只在有真实心跳证据时为 True。
- 认领不等于执行；迟到结果只提示核验，不点亮「正在执行」。
"""
from __future__ import annotations

import re

__all__ = [
    "ACTION_CONTINUE",
    "ACTION_NEW_CHAT",
    "ACTION_WAKE",
    "ACTION_COMPUTER",
    "ACTION_ADAPTER",
    "ACTION_BLOCKED",
    "ACTIONS",
    "ACTION_LABELS",
    "RECOVERY_FAIL_LIMIT",
    "decide_recovery",
    "action_catalog",
    "execute_recovery_action",
    "project_handoff_index",
    "blocked_handover",
]

ACTION_CONTINUE = "continue"
ACTION_NEW_CHAT = "new_chat_pack"
ACTION_WAKE = "wake_word"
ACTION_COMPUTER = "computer_use"
ACTION_ADAPTER = "adapter"
ACTION_BLOCKED = "blocked"

ACTIONS = (ACTION_CONTINUE, ACTION_NEW_CHAT, ACTION_WAKE,
           ACTION_COMPUTER, ACTION_ADAPTER, ACTION_BLOCKED)

ACTION_LABELS = {
    ACTION_CONTINUE: "继续原对话",
    ACTION_NEW_CHAT: "生成新聊天续接包",
    ACTION_WAKE: "复制一次唤醒词",
    ACTION_COMPUTER: "经已授权 Computer Use 打开/恢复",
    ACTION_ADAPTER: "调用已验收适配器",
    ACTION_BLOCKED: "保持阻塞",
}

# 恢复反复失败到达该阈值即停止自动尝试，保持阻塞待人工。
RECOVERY_FAIL_LIMIT = 3

_CONVERSATION_ID = re.compile(r"cv-[0-9a-f]{24}\Z", re.ASCII)

_NEEDS_USER = {ACTION_NEW_CHAT, ACTION_WAKE, ACTION_COMPUTER, ACTION_BLOCKED}
_NEEDS_AUTH = {ACTION_COMPUTER}


def _flag(state: dict, key: str) -> bool:
    return state.get(key) is True


def _count(state: dict, key: str) -> int:
    value = state.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def _text(state: dict, key: str) -> str:
    value = state.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _result(action: str, reason: str, state: dict, used: list, *,
            wake_word: str = "", pack=None) -> dict:
    return {
        "action": action,
        "label": ACTION_LABELS[action],
        "reason": reason,
        "member_id": _text(state, "member_id"),
        "project_id": _text(state, "project_id"),
        "requires_user_action": action in _NEEDS_USER,
        "requires_authorization": action in _NEEDS_AUTH,
        "execution_verified": _flag(state, "heartbeat_verified"),
        "can_fake_execution": False,
        "desktop_window_proves_execution": False,
        "adapter_proves_execution": False,
        "evidence_used": used,
        "wake_word": wake_word,
        "pack": pack,
    }


def _continuation_pack(state: dict) -> dict:
    """新聊天续接包：只含公开事实，明确不继承权限、登录或 processing 所有权。"""
    return {
        "project": _text(state, "workspace"),
        "member_id": _text(state, "member_id"),
        "coordinator": _text(state, "coordinator"),
        "goal": _text(state, "goal") or "目标未确认",
        "task_id": _text(state, "task_id"),
        "checkpoint": _text(state, "checkpoint") or "尚无续接记录",
        "inbox_path": _text(state, "inbox_path"),
        "input_path": _text(state, "input_path"),
        "output_path": _text(state, "output_path"),
        "next_step": _text(state, "next_step") or "在新对话中重新确认项目、任务与身份",
        "inherits_permissions": False,
        "inherits_login": False,
        "inherits_processing_ownership": False,
        "copy_text": ("在新对话中打开项目 %s，成员 %s，任务 %s，续接点：%s。"
                      % (_text(state, "workspace") or "（项目未声明）",
                         _text(state, "member_id") or "（成员未声明）",
                         _text(state, "task_id") or "（任务未声明）",
                         _text(state, "checkpoint") or "尚无续接记录")),
    }


def decide_recovery(state: dict) -> dict:
    """按真实状态返回唯一确定的下一动作；畸形输入按证据不足处理。"""
    state = state if isinstance(state, dict) else {}

    project_matches = state.get("project_matches")
    project_matches = project_matches if isinstance(project_matches, bool) else project_matches is None
    member_enabled = state.get("member_enabled")
    member_enabled = member_enabled if isinstance(member_enabled, bool) else member_enabled is None
    conversation = state.get("conversation_responsive")
    conversation = conversation if isinstance(conversation, bool) else None

    stop = _flag(state, "stop")
    entry_missing = _flag(state, "entry_missing")
    login_required = _flag(state, "login_required")
    budget_expired = _flag(state, "budget_expired")
    deadline_expired = _flag(state, "deadline_expired")
    process_exited = _flag(state, "process_exited")
    task_claimed = _flag(state, "task_claimed")
    heartbeat = _flag(state, "heartbeat_verified")
    late_result = _flag(state, "late_result_present")
    adapter_verified = _flag(state, "adapter_verified")
    cu_authorized = _flag(state, "computer_use_authorized")
    route = _text(state, "route") or "file"
    wake_word = _text(state, "wake_word")
    fails = _count(state, "recovery_failed_count")

    claimed_without_heartbeat = task_claimed and not heartbeat
    needs_session = process_exited or conversation is False

    # 1) 状态不属于本项目：不在本项目内救援。
    if not project_matches:
        return _result(ACTION_BLOCKED, "该状态属于其他项目，不在本项目内救援；请切换到对应项目。",
                       state, ["project_matches"])
    # 2) 项目已 STOP：保持阻塞，等待人工恢复派发。
    if stop:
        return _result(ACTION_BLOCKED, "项目已 STOP，保持阻塞；不自动重派，等待人工恢复派发。",
                       state, ["stop"])
    # 3) 成员已停用。
    if not member_enabled:
        return _result(ACTION_BLOCKED, "成员已停用，不参与派工；请先启用或更换成员。",
                       state, ["member_enabled"])
    # 4) 入口缺失。
    if entry_missing:
        return _result(ACTION_BLOCKED, "成员入口缺失，先修复入口（程序不猜路径、不代安装）。",
                       state, ["entry_missing"])
    # 5) 需要登录：程序不代登录。
    if login_required:
        return _result(ACTION_BLOCKED, "需要登录；程序不代登录、不读取凭据，请人工处理后重试。",
                       state, ["login_required"])
    # 6) 预算/期限到期：记录真实待办，不延期。
    if budget_expired or deadline_expired:
        used = [k for k, v in (("budget_expired", budget_expired),
                               ("deadline_expired", deadline_expired)) if v]
        return _result(ACTION_BLOCKED, "预算或期限已到期，记录真实待办；不自动延期、不伪装完成。",
                       state, used)
    # 7) 恢复反复失败：停止自动尝试。
    if fails >= RECOVERY_FAIL_LIMIT:
        return _result(ACTION_BLOCKED, "恢复已连续失败 %d 次，保持阻塞待人工确认。" % fails,
                       state, ["recovery_failed_count"])

    # 8) 进程已退出：优先经已授权桌面控制重开，否则生成新聊天续接包。
    if process_exited:
        if cu_authorized:
            return _result(ACTION_COMPUTER, "成员进程已退出，经已授权 Computer Use 重新打开；"
                                            "打开窗口不等于任务已执行。",
                           state, ["process_exited", "computer_use_authorized"])
        return _result(ACTION_NEW_CHAT, "成员进程已退出且无授权桌面控制，生成新聊天续接包。",
                       state, ["process_exited"], pack=_continuation_pack(state))

    # 9) 对话无应答：适配器 → 唤醒词 → 桌面控制 → 新聊天包。
    if conversation is False:
        if adapter_verified and route == "adapter":
            return _result(ACTION_ADAPTER, "原对话无应答，调用已验收适配器继续；适配器不证明任务已执行。",
                           state, ["conversation_responsive", "adapter_verified", "route"])
        if wake_word:
            return _result(ACTION_WAKE, "原对话无应答，复制一次唤醒词到该成员当前对话。",
                           state, ["conversation_responsive", "wake_word"], wake_word=wake_word)
        if cu_authorized:
            return _result(ACTION_COMPUTER, "原对话无应答且无唤醒词，经已授权 Computer Use 打开/恢复；"
                                            "打开窗口不等于任务已执行。",
                           state, ["conversation_responsive", "computer_use_authorized"])
        return _result(ACTION_NEW_CHAT, "原对话无应答、无唤醒词且无授权桌面控制，生成新聊天续接包。",
                       state, ["conversation_responsive"], pack=_continuation_pack(state))

    # 10) 已认领但无心跳：回原会话报告，不判执行。
    if claimed_without_heartbeat:
        return _result(ACTION_CONTINUE, "任务已认领但无心跳证据（认领不等于执行），回原会话报告执行状态。",
                       state, ["task_claimed", "heartbeat_verified"])

    # 11) 迟到结果：回原对话核验，不点亮执行。
    if late_result:
        return _result(ACTION_CONTINUE, "存在迟到结果，回原对话核对是否验收；迟到不改变在途所有权。",
                       state, ["late_result_present"])

    # 12) 已验收适配器且走适配器路线：调用适配器继续。
    if adapter_verified and route == "adapter":
        return _result(ACTION_ADAPTER, "已验收适配器可用，调用它继续；适配器不证明任务已执行。",
                       state, ["adapter_verified", "route"])

    # 13) 有心跳证据：继续原对话。
    if heartbeat:
        return _result(ACTION_CONTINUE, "有心跳证据，继续原对话跟进。",
                       state, ["heartbeat_verified"])

    # 14) 证据不足：保持阻塞待人工确认。
    return _result(ACTION_BLOCKED, "证据不足或相互矛盾，保持阻塞待人工确认；不推断在线或执行。",
                   state, [])


def action_catalog() -> dict:
    """返回六个互斥动作的只读目录（用于说明与测试）。"""
    return {
        "actions": list(ACTIONS),
        "labels": dict(ACTION_LABELS),
        "recovery_fail_limit": RECOVERY_FAIL_LIMIT,
        "invariants": {
            "single_action": True,
            "can_fake_execution": False,
            "computer_use_requires_authorization": True,
        },
    }


def execute_recovery_action(state: dict, expected_action: str, callbacks: dict):
    """Re-evaluate fresh facts at click time and invoke one explicit callback.

    Source: Codex desktop integration, 2026-10-08. Callback availability is
    separate from evidence. This router does not acquire or release claims.
    """
    decision = decide_recovery(state)
    action = decision['action']
    if action == ACTION_BLOCKED or action != expected_action:
        raise ValueError('救援状态已变化或受阻；请重新检查：' + decision['reason'])
    callback = callbacks.get(action) if isinstance(callbacks, dict) else None
    if not callable(callback):
        raise ValueError('当前没有可用的救援回调：' + decision['label'])
    if action == ACTION_COMPUTER and state.get('computer_use_authorized') is not True:
        raise ValueError('桌面控制缺少本次明确授权')
    if action == ACTION_ADAPTER and state.get('adapter_verified') is not True:
        raise ValueError('适配器缺少当前验收证据')
    return callback(decision)


def _safe_handoff_note(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    # Free-form notes can contain credentials in formats we cannot recognize.
    # Keep the public index secret-safe by disclosing only a fixed explanation.
    return "交接备注保存在项目记录中；为避免显示自由文本内的敏感信息，此索引不展示备注原文。"


def project_handoff_index(forest: dict, events=()) -> dict:
    """Build a user-visible, secret-free index from a TaskForest snapshot.

    The index distinguishes concurrent chats belonging to one member and keeps
    previous handoffs visible. AgentBus conversation ids are local correlation
    ids, not vendor chat ids and never grant ownership or permission.
    """
    forest = forest if isinstance(forest, dict) else {}
    leaves = forest.get("leaves", [])
    if not isinstance(leaves, list):
        leaves = []
    entries = []
    leaves_by_task = {}
    seen_previous = set()
    for leaf in leaves:
        if not isinstance(leaf, dict):
            continue
        for identity in (leaf.get("task_id"), *(leaf.get("history_task_ids", [])
                                                  if isinstance(leaf.get("history_task_ids"), list) else [])):
            if isinstance(identity, str) and identity:
                leaves_by_task[identity] = leaf
    for leaf in leaves:
        if not isinstance(leaf, dict):
            continue
        leaf_id = leaf.get("id") if isinstance(leaf.get("id"), str) else ""
        title = leaf.get("title") if isinstance(leaf.get("title"), str) else ""
        section_no = leaf.get("section_no") if isinstance(leaf.get("section_no"), str) else ""
        history = leaf.get("handoff_history", [])
        if isinstance(history, list):
            for item in history:
                if not isinstance(item, dict):
                    continue
                task_id = item.get("task_id") if isinstance(item.get("task_id"), str) else ""
                if not task_id:
                    continue
                conversation_id = item.get("conversation_id") if isinstance(item.get("conversation_id"), str) else ""
                seen_previous.add((task_id, conversation_id))
                entries.append({
                    "leaf_id": leaf_id,
                    "section_no": section_no,
                    "title": title,
                    "task_id": task_id,
                    "agent": item.get("agent") if isinstance(item.get("agent"), str) else "",
                    "conversation_id": conversation_id,
                    "relation": "previous",
                    "state": "handed_off",
                    "timestamp": item.get("timestamp") if isinstance(item.get("timestamp"), str) else "",
                    "owner_confirmed_inactive": item.get("owner_confirmed_inactive") is True,
                    "note": _safe_handoff_note(item.get("reason")),
                    "next_action": "这是已交出的旧会话；不要从这里提交。由当前任务持有者在新会话继续，旧回执会被拒绝。",
                })
        task_id = leaf.get("task_id") if isinstance(leaf.get("task_id"), str) else ""
        if not task_id:
            continue
        state = leaf.get("state") if isinstance(leaf.get("state"), str) else "unknown"
        conversation_id = leaf.get("conversation_id") if isinstance(leaf.get("conversation_id"), str) else ""
        if state == "delivered":
            next_action = "成果已正式回执；如需继续，请建立新的后续任务。"
        elif state in {"blocked", "stopped", "rejected", "awaiting_audit", "awaiting_receipt"}:
            next_action = "此任务尚未完成；先查看阻塞或审核说明，再按正式流程处理。"
        elif conversation_id:
            next_action = "先在此 AgentBus 会话继续并等待正式回执；确认原会话已停止后，才可发起接手。"
        else:
            next_action = "任务尚未在成员会话中认领；先正式认领，系统才会生成本地会话编号。"
        entries.append({
            "leaf_id": leaf_id,
            "section_no": section_no,
            "title": title,
            "task_id": task_id,
            "agent": leaf.get("owner") if isinstance(leaf.get("owner"), str) else "",
            "conversation_id": conversation_id,
            "relation": "current",
            "state": state,
            "timestamp": "",
            "owner_confirmed_inactive": False,
            "note": _safe_handoff_note(leaf.get("handoff")),
            "next_action": next_action,
        })
    if isinstance(events, (list, tuple)):
        for event in events:
            if not isinstance(event, dict) or event.get("event") not in ("recovered", "relinquished_claim"):
                continue
            task_id = event.get("id")
            conversation_id = event.get("previous_conversation_id", event.get("conversation_id", ""))
            if (not isinstance(task_id, str) or not isinstance(conversation_id, str)
                    or not _CONVERSATION_ID.fullmatch(conversation_id)
                    or (task_id, conversation_id) in seen_previous):
                continue
            leaf = leaves_by_task.get(task_id)
            if not leaf:
                continue
            agent = event.get("agent") if isinstance(event.get("agent"), str) else ""
            entries.append({
                "leaf_id": leaf.get("id", ""),
                "section_no": leaf.get("section_no", ""),
                "title": leaf.get("title", ""),
                "task_id": task_id,
                "agent": agent,
                "conversation_id": conversation_id,
                "relation": "previous",
                "state": "handed_off",
                "timestamp": event.get("ts", "") if isinstance(event.get("ts"), str) else "",
                "owner_confirmed_inactive": event.get("owner_confirmed_inactive") is True,
                "note": _safe_handoff_note(event.get("reason")),
                "next_action": "这是已经撤销的旧会话；新会话持有新的认领代次，旧回执会被拒绝。",
            })
            seen_previous.add((task_id, conversation_id))
    entries.sort(key=lambda item: (item["relation"] != "current", item["section_no"], item["task_id"]))
    return {"schema_version": 1, "source": "AgentBus task tree and formal task state",
            "read_only": True, "entries": entries}


# ---------------------------------------------------------------------------
# §6.10 「一屏说清卡住的任务该怎么接手」
# ---------------------------------------------------------------------------

# 需要人来看一眼才能决定接手的状态；其余状态（queued/running/delivered）不属"卡住"。
BLOCKING_STATES = ("blocked", "stopped", "rejected", "awaiting_audit",
                   "awaiting_receipt", "missing")

STATE_LABELS = {
    "blocked": "已阻塞",
    "stopped": "已停止",
    "rejected": "审核未批准",
    "awaiting_audit": "等待独立审核",
    "awaiting_receipt": "等待正式回执",
    "missing": "任务记录缺失",
    "queued": "待认领",
    "claimed": "已认领（心跳未确认）",
    "running": "执行中",
    "delivered": "已交付",
}


def _public_path(value) -> str:
    """只保留可公开的相对路径；机器绝对路径一律不进入这一屏。"""
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip().replace("\\", "/")
    if re.match(r"^[A-Za-z]:", text) or text.startswith("//") or text.startswith("/"):
        # 绝对路径：只回显最后两段，避免泄漏机器目录结构。
        parts = [p for p in text.split("/") if p]
        return ".../" + "/".join(parts[-2:]) if parts else ""
    return text


def _artifact_rows(artifacts) -> list:
    rows = []
    if not isinstance(artifacts, (list, tuple)):
        return rows
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        rows.append({
            "path": _public_path(item.get("path")),
            "bytes": item.get("bytes") if isinstance(item.get("bytes"), int) else None,
            "sha256": item.get("sha256") if isinstance(item.get("sha256"), str) else "",
        })
    return rows


def _heartbeat_view(leaf: dict) -> dict:
    """把心跳快照归一成"最后有效心跳"这一屏字段。

    只认**通过票据校验的显式心跳记录**；文件 mtime / 目录被 touch 都不算证据。
    """
    observed = leaf.get("heartbeat")
    base = {
        "available": False,
        "valid": False,
        "age_s": None,
        "stale": True,
        "conversation_id": "",
        "claim_fingerprint": "",
        "source": "none",
        "reason": "没有任何心跳记录；文件 mtime 不作为心跳证据",
    }
    if not isinstance(observed, dict):
        return base
    source = observed.get("source") if isinstance(observed.get("source"), str) else "none"
    age = observed.get("age_s")
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        age = None
    stale = observed.get("stale")
    if not isinstance(stale, bool):
        stale = source != "record"
    return {
        "available": True,
        "valid": source == "record" and stale is False,
        "age_s": None if age is None else round(float(age), 3),
        "stale": stale,
        "conversation_id": observed.get("conversation_id")
        if isinstance(observed.get("conversation_id"), str) else "",
        "claim_fingerprint": observed.get("claim_fingerprint")
        if isinstance(observed.get("claim_fingerprint"), str) else "",
        "source": source,
        "reason": observed.get("reason") if isinstance(observed.get("reason"), str) else "",
    }


def _stop_evidence(leaf: dict, events, stop_flag: bool) -> dict:
    """判定"原会话是否**可验证地**已停止"。

    关键纪律：**心跳过期不是停止证据**——它只说明没人报平安，不说明那个会话真的停了。
    因此只在下列**正面证据**存在时才判为可验证停止：
      * 工作区根有 STOP 标志（全局请求停止）；
      * 该叶子从未被认领过（不存在会继续执行的会话）；
      * 存在该任务的 `relinquished_claim` / `recovered` 事件且 `owner_confirmed_inactive=true`
        （即"已确认原持有者停止"这一正式记录）。
    否则一律判为**未确认**，因此不得释放票据、不得声称已经接手。
    """
    task_id = leaf.get("task_id") if isinstance(leaf.get("task_id"), str) else ""
    leaf_id = leaf.get("id") if isinstance(leaf.get("id"), str) else ""
    conversation_id = leaf.get("conversation_id") if isinstance(leaf.get("conversation_id"), str) else ""
    evidence = []
    if stop_flag:
        evidence.append({"kind": "workspace_stop_flag",
                         "detail": "工作区根存在 STOP 标志：已请求停止，未完成工作不应继续推进。"})
    if not conversation_id:
        evidence.append({"kind": "never_claimed",
                         "detail": "该叶子当前没有认领会话，不存在仍在执行的持有者。"})
    if isinstance(events, (list, tuple)):
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("event") not in ("relinquished_claim", "recovered"):
                continue
            if event.get("id") not in (task_id, leaf_id):
                continue
            if event.get("owner_confirmed_inactive") is not True:
                continue
            evidence.append({
                "kind": "confirmed_inactive_event",
                "detail": "存在正式交接事件 %s 且已记录『原持有者已确认停止』。" % event.get("event"),
            })
    return {
        "verifiable": bool(evidence),
        "value": "confirmed_stopped" if evidence else "unconfirmed",
        "evidence": evidence,
    }


def _blocker(leaf: dict, stop_flag: bool, heartbeat: dict) -> dict:
    state = leaf.get("state") if isinstance(leaf.get("state"), str) else "unknown"
    unmet = leaf.get("unmet_dependencies")
    unmet = unmet if isinstance(unmet, list) else []
    if stop_flag:
        return {"kind": "workspace_stop_flag",
                "detail": "工作区被整体置停（STOP）：在解除前不要接手任何叶子。",
                "unmet_dependencies": unmet}
    if unmet:
        return {"kind": "unmet_dependency",
                "detail": "仍有未满足的前置依赖：接手前必须先完成 %s。" % ", ".join(
                    str(item)[:40] for item in unmet[:6]),
                "unmet_dependencies": unmet}
    if state in BLOCKING_STATES:
        results = leaf.get("results") if isinstance(leaf.get("results"), str) else ""
        return {"kind": "task_state:%s" % state,
                "detail": results.strip()[:600] or ("任务处于『%s』状态。" % STATE_LABELS.get(state, state)),
                "unmet_dependencies": unmet}
    if heartbeat.get("available") and heartbeat.get("stale") and leaf.get("conversation_id"):
        return {"kind": "stale_heartbeat",
                "detail": "已认领但心跳未确认（%s）。" % (
                    heartbeat.get("reason") or "无有效心跳记录"),
                "unmet_dependencies": unmet}
    return {"kind": "none",
            "detail": "该叶子当前不是阻塞态。",
            "unmet_dependencies": unmet}


def blocked_handover(forest: dict, *, leaf_id: str = "", section_no: str = "",
                     events=(), stop_flag: bool = False) -> dict:
    """为**卡住的叶子**生成一屏接手说明（只读、纯函数、可公开）。

    覆盖任务书要求的六项：阻塞原因 / 最后有效心跳 / 现有成果 / 原认领成员与会话 /
    可验证的停止状态 / 下一句指令。**不释放任何票据、不代任何成员执行**。
    """
    forest = forest if isinstance(forest, dict) else {}
    leaves = forest.get("leaves", [])
    leaves = leaves if isinstance(leaves, list) else []
    leaf = None
    for item in leaves:
        if not isinstance(item, dict):
            continue
        if leaf_id and item.get("id") == leaf_id:
            leaf = item
            break
        if section_no and item.get("section_no") == section_no:
            leaf = item
            break
    if leaf is None:
        return {
            "schema_version": 1,
            "read_only": True,
            "found": False,
            "error": "未找到该叶子；请核对 leaf_id / section_no。",
            "next_instruction": "先在任务树中确认叶子的章节号或 id，再生成接手说明。",
        }

    state = leaf.get("state") if isinstance(leaf.get("state"), str) else "unknown"
    heartbeat = _heartbeat_view(leaf)
    stop = _stop_evidence(leaf, events, stop_flag)
    blocker = _blocker(leaf, stop_flag, heartbeat)
    artifacts = _artifact_rows(leaf.get("artifacts"))
    owner = leaf.get("owner") if isinstance(leaf.get("owner"), str) else ""
    conversation_id = leaf.get("conversation_id") if isinstance(leaf.get("conversation_id"), str) else ""
    title = leaf.get("title") if isinstance(leaf.get("title"), str) else ""
    section = leaf.get("section_no") if isinstance(leaf.get("section_no"), str) else ""
    goal = leaf.get("goal") if isinstance(leaf.get("goal"), str) else ""

    is_blocked = (state in BLOCKING_STATES or bool(blocker["unmet_dependencies"])
                  or stop_flag or blocker["kind"] == "stale_heartbeat")
    # 只有"原会话可验证地停了"且"没有活动心跳"且"工作区未整体置停"才允许接手。
    live = bool(heartbeat.get("available") and not heartbeat.get("stale"))
    takeover_allowed = bool(is_blocked and stop["verifiable"] and not live and not stop_flag)

    if not is_blocked:
        next_instruction = ("该叶子状态为『%s』，不属于卡住的任务，无需接手。"
                            % STATE_LABELS.get(state, state))
    elif stop_flag:
        next_instruction = (
            "**工作区被整体置停（STOP）**：先解除停止，再谈接手。"
            "停止期间不要认领、不要推进任何叶子，也不要认为旧的认领已经失效。")
    elif live:
        next_instruction = (
            "原会话仍在心跳窗口内（成员 %s / 会话 %s，最近心跳 %s 秒前）：**不要重新认领**。"
            "若确认它已经停了，先让原会话正式交出，或等心跳过期后再评估。"
            % (owner or "未知", conversation_id or "未记录",
               heartbeat.get("age_s") if heartbeat.get("age_s") is not None else "未知"))
    elif not stop["verifiable"]:
        age = heartbeat.get("age_s")
        if age is not None:
            heartbeat_text = "最近一次有效心跳在约 %.0f 秒前，已超过心跳门限，判为过期" % age
        else:
            heartbeat_text = heartbeat.get("reason") or "无有效心跳记录"
        next_instruction = (
            "**停止状态未确认**：成员 %s / 会话 %s：%s。"
            "但这**不是**停止证据——心跳过期只说明没人报平安。没有『已确认停止』的正式记录，"
            "因此不释放票据、不视为已接手。未确认前不要重新认领，也不要把旧回执当作自己的交付。"
            "确认方式：让原会话正式交出，或由协调者记录一次带 owner_confirmed_inactive 的交接事件。"
            % (owner or "未知", conversation_id or "未记录", heartbeat_text))
    elif blocker["unmet_dependencies"]:
        next_instruction = (
            "原会话已确认停止。但仍有前置依赖未满足：%s。"
            "先处理依赖，再在新会话执行 `agentbus tree claim --leaf %s --agent <新成员>` 接手。"
            % (", ".join(str(i)[:40] for i in blocker["unmet_dependencies"][:6]),
               section or leaf.get("id", "")))
    else:
        next_instruction = (
            "原会话已确认停止（证据：%s）。可在新会话执行 "
            "`agentbus tree claim --leaf %s --agent <新成员>` 接手；"
            "已有产物 %d 项，接手后先核对 sha256 再继续。"
            % ("；".join(item["detail"] for item in stop["evidence"]) or "未记录",
               section or leaf.get("id", ""), len(artifacts)))

    return {
        "schema_version": 1,
        "read_only": True,
        "found": True,
        "leaf": {
            "id": leaf.get("id", "") if isinstance(leaf.get("id"), str) else "",
            "section_no": section,
            "title": title,
            "goal": goal[:600],
            "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "is_blocked": is_blocked,
        },
        "blocked_reason": {
            "kind": blocker["kind"],
            "detail": blocker["detail"],
            "unmet_dependencies": blocker["unmet_dependencies"][:12],
        },
        "last_valid_heartbeat": heartbeat,
        "existing_artifacts": {
            "count": len(artifacts),
            "complete_hashes": sum(1 for row in artifacts if row["sha256"]),
            "rows": artifacts[:20],
        },
        "original_claimer": {
            "agent": owner,
            "conversation_id": conversation_id,
            "claim_epoch": leaf.get("claim_epoch") if isinstance(leaf.get("claim_epoch"), int) else None,
            "claim_id_fingerprint": heartbeat.get("claim_fingerprint") or "",
            "note": ("AgentBus 会话编号只是本地关联标识，不代表厂商聊天身份，也不授予任何权限。"),
        },
        "stop_state": stop,
        "ticket": {
            "released": False,
            "note": ("本说明是只读视图：不释放原会话票据、不改变认领代次。"
                     "释放/接手必须走正式流程。"),
        },
        "takeover_allowed": takeover_allowed,
        "handoff_history_count": len(leaf.get("handoff_history"))
        if isinstance(leaf.get("handoff_history"), list) else 0,
        "next_instruction": next_instruction,
    }
