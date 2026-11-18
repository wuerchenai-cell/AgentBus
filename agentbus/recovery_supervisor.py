# -*- coding: utf-8 -*-
"""恢复监督器：只读检测 + 过门后给出恢复动作建议。

来源：OpenCode（平级成员），2026-10-08，任务 m-1791457584499-8893c2
（product-convergence，OC-RELIABILITY），reply_to=m-1791457574691-fa3254。
采用计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…80cc3b5。

定位：**只读**。本模块不认领、不移动、不删除任何 processing 消息，
不自动抢 processing，也不改任何任务状态。它只回答两个问题：

  1. 现状是什么（Observation）——无心跳、租约过期、会话停止、重复送达；
  2. 在所有闸门都允许的前提下，应该建议做什么（Proposal）。

自动执行被刻意禁止：Proposal.auto_execute 恒为 False，
且 action 取值里没有「抢占」这一类。要真正恢复必须由有权限的调用方
在人工确认原执行者已停止后，另行走 Bus.recover_claim 这条正规入口。

闸门顺序（照 CLAIM_FENCING_NEXT.md 与现有 gates.py 的精神）：
  STOP → 预算 → 期限 → 副作用 → 人工/适配器能力
任一闸门拒绝即返回 blocked_by，不给建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time

from . import claim_fencing as _fencing
from . import heartbeat as _heartbeat

# 四种观察类型
OBS_NO_HEARTBEAT = "no_heartbeat"
OBS_EXPIRED_LEASE = "expired_lease"
OBS_SESSION_STOPPED = "session_stopped"
OBS_DUPLICATE_DELIVERY = "duplicate_delivery"

# 建议动作类型。刻意不含「抢占 processing」。
ACTION_ASK_HUMAN = "ask_human_confirm_stopped"
ACTION_PROPOSE_RECOVER = "propose_recover_claim"
ACTION_DEDUPE_RECEIPT = "propose_dedupe_receipt"
ACTION_NONE = "none"

# 闸门名，顺序即检查顺序
GATE_STOP = "stop"
GATE_BUDGET = "budget"
GATE_DEADLINE = "deadline"
GATE_SIDE_EFFECT = "side_effect"
GATE_CAPABILITY = "capability"
GATE_ORDER = (GATE_STOP, GATE_BUDGET, GATE_DEADLINE,
              GATE_SIDE_EFFECT, GATE_CAPABILITY)


class SupervisorError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__("%s: %s" % (code, message) if message else code)


@dataclass
class Observation:
    """一条只读观察。绝不携带「可以执行」的含义。"""

    kind: str
    task_id: str
    agent: str
    detail: str = ""
    evidence: dict = field(default_factory=dict)
    observed_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "agent": self.agent,
            "detail": self.detail,
            "evidence": self.evidence,
            "observed_at": self.observed_at,
        }


@dataclass
class Proposal:
    """恢复动作建议。auto_execute 恒为 False，这是本模块的不变量。"""

    task_id: str
    agent: str
    observation: str
    action: str
    blocked_by: list
    reasons: list
    requires_human_confirmation: bool = True
    auto_execute: bool = False
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "observation": self.observation,
            "action": self.action,
            "blocked_by": list(self.blocked_by),
            "reasons": list(self.reasons),
            "requires_human_confirmation": self.requires_human_confirmation,
            "auto_execute": self.auto_execute,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# 只读观察
# --------------------------------------------------------------------------
def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _lease_age(record: dict, now: float) -> float | None:
    stamp = record.get("lease_expires_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    return round(float(stamp) - float(now), 3)


def lease_observation(bus_root, agent: str, rel_path: str, *,
                      now: float | None = None) -> Observation | None:
    """只读检查一条路径上的租约是否过期。

    租约的真身在 agentbus/lease.py 的记录里（含 expires_at），不在消息 JSON 里。
    本模块因此单独提供这个入口，由调用方把租约路径交给它——不猜、不从消息推断。
    """
    now = time.time() if now is None else float(now)
    manager_path = Path(bus_root) / "leases"
    record = None
    target = manager_path / (str(rel_path).replace("\\", "/") + ".json")
    if target.is_file():
        record = _load_json(target)
    elif manager_path.is_dir():
        for path in sorted(manager_path.glob("*.json")):
            data = _load_json(path)
            if isinstance(data, dict) and str(data.get("rel")) == str(rel_path):
                record = data
                break
    if not isinstance(record, dict):
        return None
    expires = record.get("expires_at")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None
    remaining = round(float(expires) - float(now), 3)
    if remaining >= 0:
        return None
    return Observation(
        OBS_EXPIRED_LEASE, str(record.get("rel") or rel_path),
        str(record.get("owner") or agent),
        "租约已过期 %.3fs" % abs(remaining),
        {"lease_remaining_s": remaining,
         "lease_path": str(target.relative_to(bus_root)).replace("\\", "/")},
        now)


def observe_processing(bus_root, agent: str, *, now: float | None = None,
                       heartbeat_timeout_s: float = 600.0,
                       task_records: dict | None = None) -> list[Observation]:
    """只读观察某成员 processing/ 下的每条消息，产出观察列表。"""
    now = time.time() if now is None else float(now)
    root = Path(bus_root)
    processing = root / "agents" / agent / "processing"
    done = root / "agents" / agent / "done"
    task_records = task_records or {}
    out: list[Observation] = []
    if not processing.is_dir():
        return out
    for path in sorted(processing.glob("*.json")):
        record = _load_json(path)
        if not isinstance(record, dict):
            out.append(Observation(OBS_NO_HEARTBEAT, path.stem, agent,
                                  "processing 消息不可解析，无法判断心跳",
                                  {"file": path.name}, now))
            continue
        task_id = str(record.get("id") or path.stem)
        task_record = task_records.get(task_id, record)
        snapshot = _heartbeat.state(root, task_id, agent,
                                    task_record=task_record, now=now)
        if snapshot.get("source") != _heartbeat.SOURCE_RECORD:
            out.append(Observation(
                OBS_NO_HEARTBEAT, task_id, agent,
                snapshot.get("reason") or "没有有效心跳",
                {"heartbeat": {k: snapshot.get(k) for k in
                               ("source", "age_s", "stale", "reason", "mtime_ignored")}},
                now))
        elif _heartbeat.is_stale(snapshot, heartbeat_timeout_s):
            out.append(Observation(
                OBS_NO_HEARTBEAT, task_id, agent,
                "心跳已超过门限 %.0fs" % heartbeat_timeout_s,
                {"heartbeat": {k: snapshot.get(k) for k in
                               ("source", "age_s", "stale", "reason")}},
                now))

        stop_marker = _stop_marker(root, task_id)
        if stop_marker:
            out.append(Observation(
                OBS_SESSION_STOPPED, task_id, agent,
                "执行者自报会话已停止",
                {"marker": stop_marker}, now))

        terminal = done / ("%s.json" % task_id)
        if terminal.is_file():
            out.append(Observation(
                OBS_DUPLICATE_DELIVERY, task_id, agent,
                "processing 与 done 同时存在该任务；先核对终态是否已提交",
                {"done_file": terminal.name}, now))
        results = _results_for(root, task_id)
        if len(results) > 1:
            out.append(Observation(
                OBS_DUPLICATE_DELIVERY, task_id, agent,
                "同一任务存在 %d 条 result" % len(results),
                {"result_ids": results}, now))
    return out


def _stop_marker(bus_root, task_id: str):
    """读取执行者显式留下的「我已停止」标记。没有标记就是没有，不猜。"""
    path = Path(bus_root) / "session-stops" / ("%s.json" % task_id)
    if not path.is_file():
        return None
    data = _load_json(path)
    if not isinstance(data, dict):
        return "unreadable"
    return data.get("reason") or "session_stopped"


def _results_for(bus_root, task_id: str) -> list[str]:
    """只读找出 reply_to 指向该任务的所有 result ID。"""
    root = Path(bus_root) / "agents"
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*/done/*.json")):
        data = _load_json(path)
        if isinstance(data, dict) and data.get("reply_to") == task_id \
                and data.get("type") == "result":
            found.append(str(data.get("id") or path.stem))
    return found


def observe_workspace(bus_root, agents, *, now: float | None = None,
                      heartbeat_timeout_s: float = 600.0,
                      task_records: dict | None = None) -> list[Observation]:
    """对多个成员做只读观察并汇总。"""
    out: list[Observation] = []
    for agent in sorted(agents or ()):
        out.extend(observe_processing(bus_root, agent, now=now,
                                      heartbeat_timeout_s=heartbeat_timeout_s,
                                      task_records=task_records))
    return out


# --------------------------------------------------------------------------
# 闸门
# --------------------------------------------------------------------------
def evaluate_gates(record: dict, *, stop_active: bool = False,
                   budget_exhausted: bool = False,
                   side_effect_risk: bool = False,
                   capability_available: bool = True,
                   human_confirmed_inactive: bool = False) -> tuple[list, list]:
    """按固定顺序检查五道闸门，返回 (blocked_by, reasons)。

    stop_active 等关键字参数都给了安全的缺省值：漏传时按「没有 STOP、
    没有已知风险」处理，而不是抛 TypeError 让监督器整体不可用。
    真正的 STOP 与风险由调用方显式传入。
    """
    blocked: list[str] = []
    reasons: list[str] = []

    if stop_active:
        blocked.append(GATE_STOP)
        reasons.append("STOP 存在；不得派发、不得恢复")
    if budget_exhausted:
        blocked.append(GATE_BUDGET)
        reasons.append("预算已用尽；恢复不续预算")
    deadline = record.get("deadline_ts") if isinstance(record, dict) else None
    # 0.0 / None 表示这条任务没有设期限，不得当成已过期——那会让恢复建议永远被拦死。
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and deadline > 0:
        if time.time() > float(deadline):
            blocked.append(GATE_DEADLINE)
            reasons.append("原期限已到；不自动延长")
    if side_effect_risk:
        blocked.append(GATE_SIDE_EFFECT)
        reasons.append("可能已产生副作用；须人工核对产物后再决定")
    if not capability_available:
        blocked.append(GATE_CAPABILITY)
        reasons.append("缺少人工或适配器能力；本席不能自行恢复")
    if blocked:
        reasons.append("恢复须人工明确确认原执行者已停止；当前 human_confirmed_inactive=%s"
                       % human_confirmed_inactive)
    return blocked, reasons


def _action_for(kind: str) -> str:
    if kind == OBS_DUPLICATE_DELIVERY:
        return ACTION_DEDUPE_RECEIPT
    if kind in (OBS_NO_HEARTBEAT, OBS_EXPIRED_LEASE, OBS_SESSION_STOPPED):
        return ACTION_PROPOSE_RECOVER
    return ACTION_NONE


def propose(observation: Observation, record: dict | None = None, *,
            stop_active: bool = False, budget_exhausted: bool = False,
            side_effect_risk: bool = False, capability_available: bool = True,
            human_confirmed_inactive: bool = False) -> Proposal:
    """把观察转成恢复建议。auto_execute 恒为 False。"""
    record = record if isinstance(record, dict) else {}
    blocked, reasons = evaluate_gates(
        record, stop_active=stop_active, budget_exhausted=budget_exhausted,
        side_effect_risk=side_effect_risk, capability_available=capability_available,
        human_confirmed_inactive=human_confirmed_inactive)
    action = ACTION_NONE if blocked else _action_for(observation.kind)
    if not blocked and action == ACTION_PROPOSE_RECOVER and not human_confirmed_inactive:
        # 未确认原执行者已停止时，降级为「请人工确认」而不是直接建议恢复。
        action = ACTION_ASK_HUMAN
        reasons.append("尚未确认原执行者已停止；建议先人工确认")
    return Proposal(
        task_id=observation.task_id,
        agent=observation.agent,
        observation=observation.kind,
        action=action,
        blocked_by=blocked,
        reasons=reasons or ["闸门全部通过；仍需人工确认后才可走正规恢复入口"],
        requires_human_confirmation=True,
        auto_execute=False,
        note="本监督器只读，不会也不能抢占 processing；恢复须调用方走正规入口",
    )


def quarantine_report(bus_root, task_id: str) -> dict:
    """只读汇总某任务的迟到证据隔离情况。"""
    items = _fencing.list_quarantined(bus_root, task_id)
    return {"task_id": task_id, "count": len(items), "items": items}