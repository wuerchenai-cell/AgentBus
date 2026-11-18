# -*- coding: utf-8 -*-
"""认领票据与迟到回执栅栏。

来源：OpenCode（平级成员），2026-10-08，任务 m-1791457584499-8893c2
（product-convergence，OC-RELIABILITY），reply_to=m-1791457574691-fa3254。
采用计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…80cc3b5。
设计依据：`成果/agentbus/pilots/recovery-continuity-20261006/CLAIM_FENCING_NEXT.md`
（该文件为待实施清单，未称已实现；本模块是它在作者候选中的实现）。

范围（严格照 CLAIM_FENCING_NEXT.md，不扩大）：
  - 任务保存 claim_epoch（初始 0，正式领取递增）与当次 UUID claim_id；
    恢复撤销 claim_id、保留 epoch。
  - result 保存 source_claim_epoch / source_claim_id，仍用 reply_to 关联原任务。
  - 调用者提供领取时票据；禁止从当前任务反补「最新票据」。
  - 旧票据不能修改新认领、投正式 result 或删除 processing。
  - 迟到证据保留在 .agentbus/late-results/<task-id>/<evidence-id>.json，
    以独立 late_result_rejected 审计关联原任务；不放普通 ready、不更新任务、
    不触发评审或后续。保留证据失败必须报错，不静默丢弃。

明确不做（范围内外）：认证、唤醒、心跳另验（见 heartbeat.py）；
不新增第二套队列；不设 claim_secret；不承诺副作用 exactly-once。

边界：本模块**不 import agentbus.bus**，因此不与 bus.py 形成循环依赖，
也不 import Message 类型。它只处理纯数据与严格校验，可独立单测。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

# claim_id 是不可猜的随机串，不是计数器、不是哈希、不是时间戳。
CLAIM_ID_BYTES = 16
CLAIM_ID_RE = re.compile(r"^[0-9a-f]{%d}$" % (CLAIM_ID_BYTES * 2))
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

#: 任务记录里承载票据的三个字段名。
EPOCH_FIELD = "claim_epoch"
CLAIM_ID_FIELD = "claim_id"
CLAIM_AGENT_FIELD = "claim_agent"
CLAIM_CONVERSATION_FIELD = "claim_conversation_id"

#: result 消息上记录来源票据的字段名。
SOURCE_EPOCH_FIELD = "source_claim_epoch"
SOURCE_CLAIM_ID_FIELD = "source_claim_id"
SOURCE_CONVERSATION_FIELD = "source_conversation_id"
CONVERSATION_ID_RE = re.compile(r"^cv-[0-9a-f]{24}$")

LATE_RESULTS_DIRNAME = "late-results"
LEGACY_STATE = "legacy_unfenced"


class TicketError(ValueError):
    """票据非法或与当前任务不符。code 供调用方与测试区分原因。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.detail = message
        super().__init__("%s: %s" % (code, message) if message else code)


def new_claim_id() -> str:
    """生成不可猜的 claim_id。使用 secrets 而非 uuid4/hash/时间戳。"""
    return secrets.token_hex(CLAIM_ID_BYTES)


def _offset() -> str:
    offset = -time.timezone
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return "%s%02d:%02d" % (sign, offset // 3600, (offset % 3600) // 60)


def iso_now() -> str:
    """本地时间的 ISO 风格时间戳，带时区偏移。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S") + _offset()


# --------------------------------------------------------------------------
# 严格字段校验：拒绝 bool、负数、浮点与非法 ID
# --------------------------------------------------------------------------
def validate_epoch(value) -> int:
    """claim_epoch 必须是 >=1 的整数。bool、负数、浮点、字符串一律拒绝。"""
    if isinstance(value, bool):
        raise TicketError("EPOCH_TYPE", "bool 不是合法代次: %r" % (value,))
    if not isinstance(value, int):
        raise TicketError("EPOCH_TYPE", "代次必须是整数: %r" % (value,))
    if value < 1:
        raise TicketError("EPOCH_RANGE", "正式认领的代次必须 >=1: %r" % (value,))
    return value


def validate_claim_id(value) -> str:
    """claim_id 必须是 32 位小写十六进制。"""
    if not isinstance(value, str):
        raise TicketError("CLAIM_ID_TYPE", "claim_id 必须是字符串: %r" % (value,))
    if CLAIM_ID_RE.match(value) is None:
        raise TicketError("CLAIM_ID_FORMAT", "claim_id 格式非法: %r" % (value,))
    return value


def conversation_id_for_claim(claim_id: str) -> str:
    """Return a safe, stable display/session key for one exact claim.

    The claim id remains the private fencing capability. This deterministic
    digest is safe to show in task cards and lets a claim keep the same
    AgentBus conversation identity across snapshots and restarts.
    """
    validate_claim_id(claim_id)
    digest = hashlib.sha256(("agentbus-conversation-v1:" + claim_id).encode("ascii")).hexdigest()
    return "cv-" + digest[:24]


def validate_conversation_id(value) -> str:
    if not isinstance(value, str) or CONVERSATION_ID_RE.fullmatch(value) is None:
        raise TicketError("CONVERSATION_ID_FORMAT", "conversation_id 格式非法")
    return value


def validate_task_id(value) -> str:
    if not isinstance(value, str) or TASK_ID_RE.match(value) is None:
        raise TicketError("TASK_ID_FORMAT", "任务 ID 非法: %r" % (value,))
    return value


def validate_agent_id(value) -> str:
    from .profiles import validate_agent_id as canonical_validator
    try:
        canonical_validator(value, allow_system=True)
    except ValueError as exc:
        raise TicketError("AGENT_ID_FORMAT", "成员 ID 非法: %r" % (value,)) from exc
    return value


# --------------------------------------------------------------------------
# 票据
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ClaimTicket:
    """一次正规认领所颁发的票据。不可变，字段全部严格校验过。"""

    task_id: str
    agent: str
    claim_epoch: int
    claim_id: str
    issued_at: str

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            EPOCH_FIELD: self.claim_epoch,
            CLAIM_ID_FIELD: self.claim_id,
            "conversation_id": self.conversation_id,
            "issued_at": self.issued_at,
        }

    @property
    def conversation_id(self) -> str:
        return conversation_id_for_claim(self.claim_id)

    def source_fields(self) -> dict:
        """result 消息上应记录的两个来源字段。"""
        return {SOURCE_EPOCH_FIELD: self.claim_epoch,
                SOURCE_CLAIM_ID_FIELD: self.claim_id,
                SOURCE_CONVERSATION_FIELD: self.conversation_id}

    def redacted(self) -> dict:
        """可安全展示的形状：只暴露票据指纹，不暴露完整 claim_id。"""
        return {"task_id": self.task_id, "agent": self.agent,
                EPOCH_FIELD: self.claim_epoch,
                CLAIM_ID_FIELD: fingerprint(self.claim_id),
                "conversation_id": self.conversation_id,
                "issued_at": self.issued_at}


def fingerprint(claim_id) -> str:
    """票据指纹：便于审计比对而不暴露原值。"""
    if isinstance(claim_id, str) and CLAIM_ID_RE.match(claim_id):
        return "fp:" + claim_id[:8]
    return "fp:invalid"


def captured_ticket(record):
    """Capture only the message returned by claim, never a later disk read."""
    data = as_mapping(record)
    if not is_fenced(data):
        return None
    return normalize({'task_id': data['id'], 'agent': data.get('claim_agent') or data.get('claimed_by'),
                      'claim_epoch': data['claim_epoch'], 'claim_id': data['claim_id'],
                      'conversation_id': data.get(CLAIM_CONVERSATION_FIELD),
                      'issued_at': data.get('claimed_at') or ''})


def issue(task_id: str, agent: str, epoch: int, *, now: str | None = None) -> ClaimTicket:
    """为一次正规认领颁发新票据。epoch 必须已由调用方递增后传入。"""
    validate_task_id(task_id)
    validate_agent_id(agent)
    validate_epoch(epoch)
    return ClaimTicket(task_id=task_id, agent=agent, claim_epoch=epoch,
                       claim_id=new_claim_id(),
                       issued_at=now or iso_now())


def normalize(ticket) -> ClaimTicket | None:
    """把 dict / ClaimTicket / None 归一化并严格校验。

    None 表示「调用方没有提交票据」。是否允许由调用方按任务是否已栅栏化判断。
    """
    if ticket is None:
        return None
    if isinstance(ticket, ClaimTicket):
        validate_task_id(ticket.task_id)
        validate_agent_id(ticket.agent)
        validate_epoch(ticket.claim_epoch)
        validate_claim_id(ticket.claim_id)
        return ticket
    if isinstance(ticket, dict):
        missing = [k for k in ("task_id", "agent", EPOCH_FIELD, CLAIM_ID_FIELD)
                   if k not in ticket]
        if missing:
            raise TicketError("TICKET_FIELDS", "票据缺字段 %s" % missing)
        claim_id = validate_claim_id(ticket[CLAIM_ID_FIELD])
        conversation_id = ticket.get("conversation_id")
        if conversation_id not in (None, ""):
            validate_conversation_id(conversation_id)
            if conversation_id != conversation_id_for_claim(claim_id):
                raise TicketError("CONVERSATION_MISMATCH",
                                  "conversation_id 与认领票据不匹配")
        return ClaimTicket(
            task_id=validate_task_id(ticket["task_id"]),
            agent=validate_agent_id(ticket["agent"]),
            claim_epoch=validate_epoch(ticket[EPOCH_FIELD]),
            claim_id=claim_id,
            issued_at=str(ticket.get("issued_at") or ""),
        )
    raise TicketError("TICKET_TYPE", "票据类型非法: %s" % type(ticket).__name__)


# --------------------------------------------------------------------------
# 任务记录上的票据状态
# --------------------------------------------------------------------------
def as_mapping(record) -> dict:
    """Accept either a plain dict or a Message-like object.

    bus.Message is a dataclass, not a dict, and claim_fencing must not import
    bus (that would be circular). Reading through a tiny accessor keeps the
    dependency direction one-way: bus -> claim_fencing.
    """
    if isinstance(record, dict):
        return record
    fields = getattr(type(record), "__dataclass_fields__", None)
    if fields is not None:
        return {name: getattr(record, name) for name in fields}
    if hasattr(record, "id") and hasattr(record, "__dict__"):
        return dict(vars(record))
    raise TicketError("RECORD_TYPE", "任务记录必须是 dict 或 dataclass 对象")


def record_epoch(record) -> int:
    """读取任务记录的代次；未栅栏化时返回 0。"""
    if not isinstance(record, dict) and not hasattr(record, "__dataclass_fields__"):
        return 0
    value = as_mapping(record).get(EPOCH_FIELD)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def is_fenced(record) -> bool:
    """任务是否已经过一次正式栅栏化认领。"""
    if not isinstance(record, dict) and not hasattr(record, "__dataclass_fields__"):
        return False
    if record_epoch(record) < 1:
        return False
    claim = as_mapping(record).get(CLAIM_ID_FIELD)
    return isinstance(claim, str) and CLAIM_ID_RE.match(claim) is not None


def fencing_state(record: dict) -> str:
    """给 UI/审计用的状态标签。"""
    if is_fenced(record):
        return "fenced_epoch_%d" % record_epoch(record)
    if record_epoch(record) > 0:
        return "epoch_without_claim_id"
    return LEGACY_STATE


def apply_ticket(record, ticket: ClaimTicket):
    """把票据写进任务记录（就地修改并返回同一对象）。

    调用方负责原子写。本函数只做字段赋值与自洽性检查，不做任何 I/O。
    接受 dict 或 Message 这样的 dataclass 对象。
    """
    data = as_mapping(record)
    if ticket.task_id != data.get("id"):
        raise TicketError("TASK_MISMATCH",
                          "票据任务 %s 与记录 %r 不一致"
                          % (ticket.task_id, data.get("id")))
    previous = record_epoch(record)
    if ticket.claim_epoch <= previous:
        raise TicketError("EPOCH_NOT_ADVANCING",
                          "代次必须递增：当前 %d，提交 %d" % (previous, ticket.claim_epoch))
    data[EPOCH_FIELD] = ticket.claim_epoch
    data[CLAIM_ID_FIELD] = ticket.claim_id
    data[CLAIM_AGENT_FIELD] = ticket.agent
    data[CLAIM_CONVERSATION_FIELD] = ticket.conversation_id
    if isinstance(record, dict):
        return record
    record.claim_epoch = ticket.claim_epoch
    record.claim_id = ticket.claim_id
    record.claim_agent = ticket.agent
    record.claim_conversation_id = ticket.conversation_id
    return record


def revoke(record, *, keep_epoch: bool = True):
    """恢复撤销票据：撤销 claim_id，保留 epoch（照 CLAIM_FENCING_NEXT.md）。

    保留 epoch 是为了让旧执行者的票据永远无法再次通过校验。
    """
    data = as_mapping(record)
    epoch = record_epoch(record)
    data[CLAIM_ID_FIELD] = ""
    data[CLAIM_AGENT_FIELD] = ""
    data[CLAIM_CONVERSATION_FIELD] = ""
    if keep_epoch:
        data[EPOCH_FIELD] = max(epoch, 1)
    if not isinstance(record, dict):
        record.claim_epoch = data[EPOCH_FIELD]
        record.claim_id = ""
        record.claim_agent = ""
        record.claim_conversation_id = ""
    return record


# --------------------------------------------------------------------------
# 校验提交
# --------------------------------------------------------------------------
def check(record, ticket, *, require_when_fenced: bool = True) -> ClaimTicket | None:
    """校验提交方提交的票据是否仍是当前任务的有效票据。

    抛 TicketError 即表示拒绝；绝不返回「差不多就行」的宽松结果。
    关键规则：代次相同但 claim_id 不同也拒绝——恢复后新一代的 claim_id 必然不同。
    """
    data = as_mapping(record)
    presented = normalize(ticket)
    fenced = is_fenced(data)
    if record_epoch(data) > 0 and not fenced:
        raise TicketError('REVOKED_TICKET', '认领票据已撤销或损坏；必须重新正式领取')
    if presented is None:
        if fenced and require_when_fenced:
            raise TicketError("MISSING_TICKET",
                              "任务 %s 已栅栏化，必须提交领取时票据" % data.get("id"))
        return None
    if presented.task_id != data.get("id"):
        raise TicketError("TASK_MISMATCH",
                          "票据任务 %s 与记录 %r 不一致" % (presented.task_id, data.get("id")))
    if not fenced:
        if require_when_fenced:
            raise TicketError("LEGACY_UNFENCED",
                              "任务 %s 仍是 %s，须先走一次正规认领升级"
                              % (data.get("id"), LEGACY_STATE))
        return presented
    current_epoch = record_epoch(data)
    if presented.claim_epoch > current_epoch:
        raise TicketError("EPOCH_AHEAD",
                          "提交代次 %d 高于当前 %d，票据不可信"
                          % (presented.claim_epoch, current_epoch))
    if presented.claim_epoch < current_epoch:
        raise TicketError("STALE_EPOCH",
                          "旧代次票据：提交 %d，当前 %d" % (presented.claim_epoch, current_epoch))
    current_claim = data.get(CLAIM_ID_FIELD)
    if presented.claim_id != current_claim:
        raise TicketError("TICKET_MISMATCH",
                          "同代次 claim_id 不匹配（提交 %s，当前 %s）"
                          % (fingerprint(presented.claim_id), fingerprint(current_claim)))
    current_conversation = data.get(CLAIM_CONVERSATION_FIELD)
    if current_conversation and presented.conversation_id != current_conversation:
        raise TicketError("CONVERSATION_MISMATCH",
                          "提交会话身份与当前认领不一致")
    owner = data.get(CLAIM_AGENT_FIELD) or data.get("claimed_by") or ""
    if owner and presented.agent != owner:
        raise TicketError("AGENT_MISMATCH",
                          "票据成员 %s 不是当前认领者 %s" % (presented.agent, owner))
    return presented


def check_result_source(record: dict, source_epoch, source_claim_id,
                        source_conversation_id=None) -> None:
    """校验 result 消息上的来源票据字段是否对应当前任务。

    与 check() 同规则，但用于「已经构造好的 result 消息」这种入口，
    以便 report / post / answer 等不同提交路径共用同一把尺子。
    """
    if not isinstance(record, dict):
        raise TicketError("RECORD_TYPE", "任务记录必须是 dict")
    if record_epoch(record) > 0 and not is_fenced(record):
        raise TicketError('REVOKED_TICKET', '认领票据已撤销或损坏')
    if not is_fenced(record):
        return
    validate_epoch(source_epoch)
    validate_claim_id(source_claim_id)
    expected_conversation = conversation_id_for_claim(source_claim_id)
    if source_conversation_id not in (None, ""):
        validate_conversation_id(source_conversation_id)
        if source_conversation_id != expected_conversation:
            raise TicketError("CONVERSATION_MISMATCH",
                              "result 对话身份与来源认领票据不匹配")
    current_epoch = record_epoch(record)
    if source_epoch < current_epoch:
        raise TicketError("STALE_EPOCH",
                          "迟到结果：来源代次 %d 低于当前 %d" % (source_epoch, current_epoch))
    if source_epoch > current_epoch:
        raise TicketError("EPOCH_AHEAD",
                          "结果来源代次 %d 高于当前 %d" % (source_epoch, current_epoch))
    if source_claim_id != record.get(CLAIM_ID_FIELD):
        raise TicketError("TICKET_MISMATCH",
                          "结果来源 claim_id 与当前票据不符（提交 %s，当前 %s）"
                          % (fingerprint(source_claim_id), fingerprint(record.get(CLAIM_ID_FIELD))))
    current_conversation = record.get(CLAIM_CONVERSATION_FIELD)
    if current_conversation and expected_conversation != current_conversation:
        raise TicketError("CONVERSATION_MISMATCH", "result 不属于当前认领对话")


# --------------------------------------------------------------------------
# 迟到证据隔离区
# --------------------------------------------------------------------------
def late_results_dir(bus_root, task_id: str) -> Path:
    validate_task_id(task_id)
    return Path(bus_root) / LATE_RESULTS_DIRNAME / task_id


def quarantine(bus_root, task_id: str, evidence_id: str, payload: dict,
               *, reason_code: str, note: str = "") -> Path:
    """把迟到证据写入隔离区并返回落盘路径。

    保留证据失败必须报错，不静默丢弃（CLAIM_FENCING_NEXT.md 明文要求）。
    """
    if not isinstance(payload, dict):
        raise TicketError("PAYLOAD_TYPE", "迟到证据必须是 dict")
    directory = late_results_dir(bus_root, task_id)
    directory.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(evidence_id))[:96] or "evidence"
    target = directory / ("%s.json" % safe_id)
    counter = 1
    while target.exists():
        target = directory / ("%s.%d.json" % (safe_id, counter))
        counter += 1
    record = {
        "task_id": task_id,
        "evidence_id": safe_id,
        "reason_code": reason_code,
        "note": note,
        "quarantined_at": iso_now(),
        "advanced_task": False,
        "evidence": payload,
    }
    staging = target.with_name(".%s.%s.tmp" % (target.name, secrets.token_hex(6)))
    try:
        with staging.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return target


def list_quarantined(bus_root, task_id: str) -> list[dict]:
    """只读列出某任务的迟到证据，按文件名排序。不删除、不修改。"""
    directory = late_results_dir(bus_root, task_id)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            out.append({"task_id": task_id, "file": path.name, "verdict": "unreadable"})
    return out
