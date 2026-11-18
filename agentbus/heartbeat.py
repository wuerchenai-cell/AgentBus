# -*- coding: utf-8 -*-
"""显式心跳：只有持有当前票据的执行者可刷新。

来源：OpenCode（平级成员），2026-10-08，任务 m-1791457584499-8893c2
（product-convergence，OC-RELIABILITY），reply_to=m-1791457574691-fa3254。
采用计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…80cc3b5。

为什么需要独立心跳文件：普通文件 mtime 会因为任何读取、复制、同步、备份、
反病毒扫描而变化。**文件 mtime 不构成心跳证据**——这是一个可以零成本伪造的量。
本模块因此把心跳写成一份带票据的显式记录，并让 state() 如实区分三种来源：

  heartbeat-record  有一份通过票据校验的心跳记录，年龄取记录里的 at 字段
  mtime-only         目录里有文件但没有有效记录（例如被 touch 过），一律判为无心跳
  none               完全没有心跳痕迹

无有效记录时，无论 mtime 多新，stale 均为 True。这是本模块的核心不变量。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import time

from . import claim_fencing as _fencing
from .claim_fencing import ClaimTicket, TicketError

HEARTBEATS_DIRNAME = "heartbeats"
AGENT_FILE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}\.json$")

SOURCE_RECORD = "heartbeat-record"
SOURCE_MTIME_ONLY = "mtime-only"
SOURCE_NONE = "none"


class HeartbeatError(_fencing.TicketError):
    """心跳被拒绝。

    刻意继承 TicketError：心跳拒绝本质就是票据拒绝，调用方用同一个
    ``except TicketError`` 就能覆盖两条路径，不会漏掉其中一条。
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(code, message)


def heartbeats_dir(bus_root, task_id: str) -> Path:
    _fencing.validate_task_id(task_id)
    return Path(bus_root) / HEARTBEATS_DIRNAME / task_id


def heartbeat_path(bus_root, task_id: str, agent: str) -> Path:
    _fencing.validate_agent_id(agent)
    return heartbeats_dir(bus_root, task_id) / ("%s.json" % agent)


def _parse_iso(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(text[:19], fmt))
        except ValueError:
            continue
    return None


def record(bus_root, task_id: str, agent: str, ticket: ClaimTicket,
           *, task_record: dict, note: str = "", now: float | None = None,
           activity: str = "execution") -> dict:
    """写入一次显式心跳。

    只有持有**当前**票据的执行者能成功写入；旧票据、心跳伪造、跨项目票据
    都会在这里抛 HeartbeatError/TicketError。心跳不推进任务，也不改 processing 归属。
    """
    if not isinstance(task_record, dict):
        raise HeartbeatError("RECORD_TYPE", "任务记录必须是 dict")
    if activity not in ("execution", "submission", "claim"):
        raise HeartbeatError("ACTIVITY_TYPE", "活动类型必须是 execution、submission 或 claim")
    # 与 claim_fencing.check 同一把尺子：这里不放宽任何一条。
    # 统一抛 HeartbeatError 并保留原始 code，调用方只需处理一种异常类型。
    try:
        presented = _fencing.check(task_record, ticket)
    except TicketError as exc:
        raise HeartbeatError(exc.code, exc.detail) from exc
    if presented is None:
        raise HeartbeatError("MISSING_TICKET", "心跳必须提交领取时票据")
    if presented.agent != agent:
        raise HeartbeatError("AGENT_MISMATCH",
                             "票据成员 %s 与写入成员 %s 不一致" % (presented.agent, agent))
    stamp = time.time() if now is None else float(now)
    if (not math.isfinite(stamp) or stamp < 0 or task_id != presented.task_id
            or task_record.get('id') != task_id):
        raise HeartbeatError('HEARTBEAT_ID_OR_TIME', '心跳任务 ID 或时间无效')
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "agent": agent,
        _fencing.EPOCH_FIELD: presented.claim_epoch,
        _fencing.CLAIM_ID_FIELD: presented.claim_id,
        "conversation_id": presented.conversation_id,
        "at": _fencing.iso_now(),
        "at_epoch_s": round(stamp, 3),
        "note": str(note or "")[:500],
        "activity": activity,
        "project_id": task_record.get("workspace") or task_record.get("project_id") or "",
    }
    target = heartbeat_path(bus_root, task_id, agent)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(".%s.%s.tmp" % (target.name, secrets.token_hex(6)))
    try:
        with staging.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return payload


def state(bus_root, task_id: str, agent: str, *, task_record: dict | None = None,
          now: float | None = None) -> dict:
    """只读读取心跳状态。**绝不把文件 mtime 当作心跳证据。**

    返回 source / age_s / stale / epoch / claim_fingerprint / note。
    无有效记录时 stale 恒为 True，即使目录里刚被 touch 过。
    """
    now = time.time() if now is None else float(now)
    path = heartbeat_path(bus_root, task_id, agent)
    out = {
        "task_id": task_id,
        "agent": agent,
        "path": str(path),
        "source": SOURCE_NONE,
        "age_s": None,
        "stale": True,
        "epoch": None,
        "conversation_id": "",
        "claim_fingerprint": "",
        "note": "",
        "activity": "",
        "mtime_ignored": True,
        "reason": "",
    }
    if not path.is_file():
        out["reason"] = "没有任何心跳记录；文件 mtime 不作为心跳证据"
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        out["source"] = SOURCE_MTIME_ONLY
        out["reason"] = "心跳文件存在但不可解析，判为无有效心跳"
        return out
    if not isinstance(data, dict):
        out["source"] = SOURCE_MTIME_ONLY
        out["reason"] = "心跳文件不是对象，判为无有效心跳"
        return out
    try:
        epoch = _fencing.validate_epoch(data.get(_fencing.EPOCH_FIELD))
        claim_id = _fencing.validate_claim_id(data.get(_fencing.CLAIM_ID_FIELD))
    except TicketError as exc:
        out["source"] = SOURCE_MTIME_ONLY
        out["reason"] = "心跳票据字段非法（%s），判为无有效心跳" % exc.code
        return out
    expected_conversation = _fencing.conversation_id_for_claim(claim_id)
    recorded_conversation = data.get("conversation_id")
    if recorded_conversation not in (None, ""):
        try:
            _fencing.validate_conversation_id(recorded_conversation)
        except TicketError as exc:
            out["source"] = SOURCE_MTIME_ONLY
            out["reason"] = "心跳对话身份非法（%s），判为无有效心跳" % exc.code
            return out
        if recorded_conversation != expected_conversation:
            out["source"] = SOURCE_MTIME_ONLY
            out["reason"] = "心跳对话身份与认领票据不匹配，判为无有效心跳"
            return out
    # 若给了任务记录，则心跳必须对应当前票据；旧票据的心跳视为无效。
    if isinstance(task_record, dict):
        current = task_record.get(_fencing.CLAIM_ID_FIELD)
        current_conversation = task_record.get(_fencing.CLAIM_CONVERSATION_FIELD)
        if (not _fencing.is_fenced(task_record) or not current or data.get(_fencing.CLAIM_ID_FIELD) != current
                or epoch != _fencing.record_epoch(task_record)
                or data.get('task_id') != task_id or data.get('agent') != agent
                or (current_conversation and recorded_conversation != current_conversation)):
            out["source"] = SOURCE_MTIME_ONLY
            out["epoch"] = epoch
            out["conversation_id"] = recorded_conversation or ""
            out["claim_fingerprint"] = _fencing.fingerprint(data.get(_fencing.CLAIM_ID_FIELD))
            out["reason"] = "心跳来自已被取代的票据，判为无有效心跳"
            return out
    stamp = data.get("at_epoch_s")
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        parsed = _parse_iso(data.get("at"))
        if parsed is None:
            out["source"] = SOURCE_MTIME_ONLY
            out["reason"] = "心跳缺少可解析时间，判为无有效心跳"
            return out
        stamp = parsed
    if (not math.isfinite(now) or not math.isfinite(float(stamp))
            or float(stamp) < 0 or float(stamp) > now + 5):
        out['source'] = SOURCE_MTIME_ONLY
        if math.isfinite(now) and math.isfinite(float(stamp)):
            out['age_s'] = max(0.0, now - float(stamp))
        out['reason'] = '心跳时间非法或超出允许的五秒时钟误差，判为无有效心跳'
        return out
    activity = data.get("activity", "execution")
    if activity not in ("execution", "submission", "claim"):
        out["source"] = SOURCE_MTIME_ONLY
        out["reason"] = "活动类型非法，判为无有效心跳"
        return out
    out["source"] = SOURCE_RECORD
    out["activity"] = activity
    out["epoch"] = epoch
    out["conversation_id"] = recorded_conversation or expected_conversation
    out["claim_fingerprint"] = _fencing.fingerprint(data.get(_fencing.CLAIM_ID_FIELD))
    out["note"] = str(data.get("note") or "")[:500]
    out["age_s"] = round(max(0.0, now - float(stamp)), 3)
    out["stale"] = False
    out["reason"] = "存在通过票据校验的显式心跳记录"
    return out


def is_stale(snapshot: dict, timeout_s: float) -> bool:
    """按门限判定是否过期。mtime-only 与 none 一律为过期。"""
    if not isinstance(snapshot, dict):
        return True
    if snapshot.get("source") != SOURCE_RECORD:
        return True
    age = snapshot.get("age_s")
    if not isinstance(age, (int, float)) or isinstance(age, bool):
        return True
    return float(age) > float(timeout_s)


def scan(bus_root, task_id: str, *, agents, now: float | None = None,
         heartbeat_timeout_s: float = 600.0,
         task_records: dict | None = None) -> list[dict]:
    """只读扫描一个任务下多个成员的心跳。

    ``task_id`` 是必需位置参数：跨多个任务复用同一批成员名会得出没有意义的
    年龄，因此这里不接受「当前任务」这种隐含输入。
    """
    task_records = task_records or {}
    out = []
    for agent in sorted(agents or ()):
        snapshot = state(bus_root, task_id, agent,
                         task_record=task_records.get(task_id), now=now)
        snapshot["stale"] = snapshot["stale"] or is_stale(snapshot, heartbeat_timeout_s)
        out.append(snapshot)
    return out


def clear(bus_root, task_id: str, agent: str) -> bool:
    """删除某个成员的心跳记录。用于恢复流程显式清理，不在只读路径调用。"""
    path = heartbeat_path(bus_root, task_id, agent)
    if not path.is_file():
        return False
    path.unlink()
    return True
