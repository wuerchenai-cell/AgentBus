#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.bus —— 消息总线。

三条硬规则（踩过坑，别改）：
  1. 原子投递必须跨目录：暂存区在 <agent>/tmp/，成品 os.replace 到 <agent>/ready/。
     暂存区若在监听目录内，半成品会被自己的监听误判为"消息到达"。
  2. 认领用 os.replace 抢占：同一时刻只有一个消费者能成功。
  3. ReadDirectoryChangesW 没有超时参数，必须放进独立线程 + 队列带超时。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import json
import math
import os
import queue
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import claim_fencing as _fencing
from . import heartbeat as _heartbeat
from .operation_lock import task_lock, serialized_task
from . import exclusive_lock as _exclusive_lock
from . import workflow_pipeline as _pipeline

SCHEMA_VERSION = 1
HUMAN_INBOX = "human"
MESSAGE_TYPES = ("goal", "task", "result", "review", "escalation", "info")
MESSAGE_STATES = ("pending", "claimed", "running", "awaiting_audit",
                  "rejected", "done", "failed", "blocked")
MESSAGE_REPLACE_ATTEMPTS = 50
MESSAGE_REPLACE_RETRY_INTERVAL_S = 0.01
_SUPERVISION_FINISH_AUTHORITY = object()

# 别的 agent 手写消息时常见的字段名 → 我们的规范字段名。
# 互通的第一原则：接收要宽容，不要让一个好意的回执因为字段名不同而消失。
FIELD_ALIASES = {
    "from": "sender",
    "sender_name": "sender",
    "intent": "body",
    "content": "body",
    "message": "body",
    "subject": "title",
    "name": "title",
    "msg_id": "id",
    "message_id": "id",
    "replyTo": "reply_to",
    "in_reply_to": "reply_to",
    "resultTo": "result_to",
    "ref": "refs",
    "files": "refs",
    "paths": "refs",
    # 2026-09-26 第一次真实三方接力暴露的：Trae 在回执里自创了 artifacts 字段
    # 装产物路径，结果被静默丢弃。凡是"产物/交付物"类的字段名都收进来。
    "artifacts": "refs",
    "artifact": "refs",
    "deliverables": "refs",
    "outputs": "refs",
    "output_files": "refs",
}

IS_WINDOWS = os.name == "nt"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str = "m") -> str:
    return f"{prefix}-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"


class BusError(Exception):
    pass


class LateResultRejected(BusError):
    """A stale/forged ticket was refused. Evidence is preserved, not discarded.

    Source: OpenCode, 2026-10-08, task m-1791457584499-8893c2. The task is never
    advanced by a rejected submission.
    """

    def __init__(self, reason_code: str, message: str, *, agent: str = "",
                 task_id: str = "", quarantine_path=None):
        self.reason_code = reason_code
        self.agent = agent
        self.task_id = task_id
        self.quarantine_path = quarantine_path
        super().__init__("%s: %s" % (reason_code, message) if message else reason_code)


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #


@dataclass
class Message:
    id: str
    ts: str
    sender: str
    to: str
    type: str
    title: str = ""
    body: str = ""
    refs: list[str] = field(default_factory=list)
    reply_to: str | None = None
    result_to: str | None = None
    routed_by_fallback: str | None = None
    # Optional project-plan bindings; old messages remain unbound. Source: Codex, 2026-10-04.
    job_id: str | None = None
    plan_id: str | None = None
    plan_sha256: str | None = None
    expected_artifacts: list[str] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"
    # 闸门相关
    hop: int = 0
    attempt: int = 0
    budget_s: int = 0
    deadline_ts: float = 0.0
    # 执行产物
    result: str = ""
    duration_s: float = 0.0
    exit_code: int | None = None
    claimed_by: str = ""
    claimed_at: str = ""
    # Claim fencing. Source: OpenCode, 2026-10-08, task m-1791457584499-8893c2
    # (product-convergence/OC-RELIABILITY), design:
    # pilots/recovery-continuity-20261006/CLAIM_FENCING_NEXT.md.
    # Old messages simply lack these fields and stay legacy_unfenced.
    claim_epoch: int = 0
    claim_id: str = ""
    claim_agent: str = ""
    claim_conversation_id: str = ""
    # result messages record which claim produced them.
    source_claim_epoch: int = 0
    source_claim_id: str = ""
    source_conversation_id: str = ""
    # Strict-supervision case and evidence are bound to the source message.
    # Missing on legacy messages; the project setting decides whether it applies.
    supervision: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @property
    def awaiting_audit(self) -> bool:
        return (self.type == "result" and isinstance(self.supervision, dict)
                and self.supervision.get("state") == "awaiting_audit")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any], strict: bool = False) -> "Message":
        """把 JSON 反序列化成 Message。

        平级互通的关键设计：**接收要宽容**（Postel 定律）。
        因为别的 agent 是"手写"文件来通信的，它不可能正好命中我们的字段名。
        如果对未知字段直接报错，那些回执就会**静默消失**——这是最糟的失败方式。
        所以默认 strict=False：认别名、缺字段补默认、未知字段丢弃。

        契约说明：
        - strict=False（默认）：用于日常平级宽容接收与消息解析，容忍别名与多余字段（Postel 定律）；
        - strict=True：专用于计划审计（project_plan）与正式证据门禁，对未知字段零容忍，
          确保计划采用和正式回执严格合规，任何内部工具与脚本不得向信箱落盘协议外字段。
        """
        if strict:
            unknown = set(d) - set(Message.__dataclass_fields__)
            if unknown:
                raise BusError(f"消息含未知字段: {sorted(unknown)}")
            try:
                return Message(**d)
            except TypeError as exc:
                raise BusError(f"消息字段不合法: {exc}") from exc

        aliased: dict[str, Any] = {}
        for k, v in d.items():
            key = FIELD_ALIASES.get(k, k)
            if k == 'artifacts' and isinstance(v, list) and all(isinstance(item, dict) for item in v):
                key = 'artifacts'  # New structured manifest; legacy string paths still map to refs.
            if key in Message.__dataclass_fields__:
                aliased[key] = v
        aliased.setdefault("id", new_id("rx"))
        aliased.setdefault("ts", now_iso())
        aliased.setdefault("sender", str(d.get("from") or d.get("sender") or "unknown"))
        aliased.setdefault("to", str(d.get("to") or "unknown"))
        aliased.setdefault("type", "result")
        # 收窄到合法枚举，避免对方写了个奇怪的 type 就整条丢弃
        if aliased.get("type") not in MESSAGE_TYPES:
            aliased["type"] = "info"
        if aliased.get("status") not in MESSAGE_STATES:
            aliased["status"] = "pending"
        # refs 允许多种写法：列表、逗号/分号分隔的字符串、单一路径
        refs = aliased.get("refs")
        if isinstance(refs, str):
            aliased["refs"] = [x.strip() for x in refs.replace(";", ",").split(",") if x.strip()]
        elif not isinstance(refs, list):
            aliased["refs"] = []
        for num in ("hop", "attempt", "budget_s"):
            try:
                aliased[num] = int(aliased.get(num) or 0)
            except (TypeError, ValueError):
                aliased[num] = 0
        aliased["schema_version"] = SCHEMA_VERSION
        try:
            return Message(**aliased)
        except TypeError as exc:
            raise BusError(f"消息字段类型不合法: {exc}") from exc

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise BusError(f"schema_version={self.schema_version} 不被支持")
        if self.type not in MESSAGE_TYPES:
            raise BusError(f"未知消息类型 {self.type!r}，允许: {MESSAGE_TYPES}")
        if self.status not in MESSAGE_STATES:
            raise BusError(f"未知状态 {self.status!r}，允许: {MESSAGE_STATES}")
        for f in ("id", "sender", "to"):
            if not getattr(self, f):
                raise BusError(f"消息缺少必填字段 {f}")
        from .profiles import validate_agent_id, WINDOWS_DEVICES
        # Message IDs become filenames. Retain safe short legacy IDs, while
        # excluding paths, Windows streams/device names and lossy trailing dots.
        for f in ("id", "reply_to", "job_id", "plan_id"):
            value = getattr(self, f)
            if value is None or value == "":
                continue
            if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value)
                    or value.endswith('.') or value.split('.')[0].casefold() in WINDOWS_DEVICES):
                raise BusError(f"消息字段 {f} 不是安全的单个存储标识")
        for f in ("to", "result_to"):
            value = getattr(self, f)
            if value is not None:
                try:
                    validate_agent_id(value, allow_system=True)
                except ValueError as exc:
                    raise BusError(f"消息字段 {f} 不是安全的成员标识") from exc
        # sender remains a display/brand label for tolerant file interoperability.
        if (not isinstance(self.sender, str) or not self.sender.strip() or self.sender in ('.', '..')
                or any(ch in self.sender for ch in '/\\:') or any(ord(ch) < 32 for ch in self.sender)):
            raise BusError('消息 sender 必须为无路径或控制字符的成员标签')
        for f in ("result_to", "routed_by_fallback", "job_id", "plan_id", "plan_sha256"):
            value = getattr(self, f)
            if value is not None and not isinstance(value, str):
                raise BusError(f"消息字段 {f} 必须是字符串或 null")
        if self.plan_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', self.plan_sha256):
            raise BusError('plan_sha256 must be a lowercase SHA256')
        if not isinstance(self.expected_artifacts, list) or not all(isinstance(x, str) for x in self.expected_artifacts):
            raise BusError('expected_artifacts must be a list of relative paths')
        if not isinstance(self.artifacts, list) or not all(isinstance(x, dict) for x in self.artifacts):
            raise BusError('artifacts must be a structured list')
        if not isinstance(self.supervision, dict):
            raise BusError('supervision must be an object')


# --------------------------------------------------------------------------- #
# Windows 目录事件（零轮询等待）
# --------------------------------------------------------------------------- #

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
NOTIFY_FLAGS = 0x00000001 | 0x00000002 | 0x00000010  # FILE_NAME | DIR_NAME | LAST_WRITE


class WinDirWatcher:
    """后台线程阻塞在 ReadDirectoryChangesW，主线程用队列带超时接收。"""

    def __init__(self, path: Path):
        self.path = str(path)
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateFileW.restype = wintypes.HANDLE
        self._k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self._k32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
        self._buf = ctypes.create_string_buffer(64 * 1024)
        # 同步模式下 lpBytesReturned 必须是非空指针，传 NULL 会 access violation
        self._returned = wintypes.DWORD(0)
        self._handle = self._k32.CreateFileW(
            self.path, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None, OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS, None)
        if self._handle == ctypes.c_void_p(-1).value:
            raise OSError(f"无法打开目录句柄 {self.path} (err={ctypes.get_last_error()})")
        self._q: "queue.Queue[bool]" = queue.Queue()
        self._stopped = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        while not self._stopped.is_set():
            try:
                ok = self._k32.ReadDirectoryChangesW(
                    self._handle, self._buf, len(self._buf), False, NOTIFY_FLAGS,
                    ctypes.byref(self._returned), None, None)
            except OSError:
                ok = False
            self._q.put(bool(ok))
            if not ok:
                time.sleep(0.2)

    def wait(self, timeout_s: float) -> bool:
        try:
            return self._q.get(timeout=max(timeout_s, 0.001))
        except queue.Empty:
            return False

    def close(self) -> None:
        self._stopped.set()
        if self._handle:
            self._k32.CancelIoEx(self._handle, None)
            self._k32.CloseHandle(self._handle)
            self._handle = None


# --------------------------------------------------------------------------- #
# 总线
# --------------------------------------------------------------------------- #


class Bus:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.agents_dir = self.root / "agents"
        self.events_path = self.root / "events.jsonl"
        self.logs_dir = self.root / "logs"

    # ---- 初始化 ----
    def init(self) -> None:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)
        self.events_path.touch(exist_ok=True)

    def register(self, agent: str) -> Path:
        from .profiles import validate_agent_id
        try:
            validate_agent_id(agent, allow_system=True)
        except ValueError as exc:
            raise BusError(str(exc)) from exc
        base = self.agents_dir / agent
        if not base.resolve().is_relative_to(self.agents_dir.resolve()):
            raise BusError('成员信箱路径越出总线目录')
        for sub in ("ready", "tmp", "processing", "blocked", "done"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        return base

    def dirs(self, agent: str) -> dict[str, Path]:
        base = self.register(agent)
        return {name: base / name for name in
                ("ready", "tmp", "processing", "blocked", "done")}

    def clear_mailboxes(self, folders: tuple[str, ...] = ("ready", "processing", "blocked", "tmp")) -> int:
        """清空所有成员信箱中的未完成任务文件。
        
        用于切换任务树或重新开始时彻底擦除历史在途卡片，保证信箱一张白纸。
        """
        removed = 0
        if not self.agents_dir.exists():
            return 0
        for agent_dir in self.agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            for folder_name in folders:
                folder = agent_dir / folder_name
                if not folder.is_dir():
                    continue
                for item in folder.glob("*.json"):
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        pass
        if removed:
            self.audit(event="mailboxes_cleared", count=removed)
        return removed

    # ---- 审计 ----
    def audit(self, **kw: Any) -> None:
        kw.setdefault("ts", now_iso())
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(kw, ensure_ascii=False) + "\n")

    def _audit_best_effort(self, **kw: Any) -> bool:
        """Secondary diagnostics cannot undo an already committed transition.

        Authoritative claim/finish audit calls still use audit() directly and
        propagate failures. Only notification/rollback diagnostics use this.
        """
        try:
            self.audit(**kw)
        except Exception:
            return False
        return True

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events = []
        with self.events_path.open('rb') as stream:
            for number, raw in enumerate(stream, 1):
                if not raw.strip():
                    continue
                error = ''
                try:
                    value = json.loads(raw.decode('utf-8'))
                    if not isinstance(value, dict):
                        error = 'NonObject'
                except (UnicodeError, ValueError) as exc:
                    error = type(exc).__name__
                if error:
                    # Observation metadata only: never append this marker to
                    # audit evidence or copy the invalid line/exception text.
                    events.append({'event': 'audit_invalid_line', 'line': number,
                                   'error_type': error, 'generated_by': 'agentbus.events'})
                else:
                    events.append(value)
        return events

    # ---- 投递 ----
    def validate_result_reference(self, reply_to: str) -> Message:
        """确保 result 关联到总线中存在的派发消息，而不是孤立回执。"""
        origin = self.find_message(reply_to)
        if origin is None:
            raise BusError(f"result 的 reply_to 指向不存在的消息：{reply_to}")
        if origin.type not in ("task", "goal", "review"):
            raise BusError(
                f"result 的 reply_to 必须指向 task/goal/review，实际为 {origin.type}：{reply_to}")
        return origin

    def post(self, msg: Message) -> Message:
        return self._post_with_supervision_authority(msg, authorized=False)

    def _post_supervised_result(self, msg: Message) -> Message:
        """Publish the already verified result from the strict-audit commit path."""
        return self._post_with_supervision_authority(msg, authorized=True)

    def _post_with_supervision_authority(self, msg: Message, *, authorized: bool) -> Message:
        if msg.type == 'result':
            with task_lock(self.root, msg.sender, msg.reply_to):
                source = self.validate_result_reference(msg.reply_to or '')
                if _fencing.record_epoch(source) > 0:
                    if msg.source_claim_epoch == 0 and not msg.source_claim_id:
                        ticket = None
                    else:
                        ticket = {'task_id': msg.reply_to, 'agent': msg.sender,
                                  'claim_epoch': msg.source_claim_epoch, 'claim_id': msg.source_claim_id,
                                  'conversation_id': msg.source_conversation_id}
                    try:
                        if msg.sender != source.to:
                            raise _fencing.TicketError('AGENT_MISMATCH', '结果成员不是原任务执行者')
                        validated_ticket = _fencing.check(source.to_dict(), ticket)
                        if validated_ticket is not None:
                            # Canonicalize older submitters that still send only
                            # the claim capability; the persisted result carries
                            # its safe conversation identity as well.
                            msg.source_conversation_id = validated_ticket.conversation_id
                    except _fencing.TicketError as exc:
                        raise self._reject_late(msg.sender, source, reason_code=exc.code,
                                                note=str(exc), evidence=msg.to_dict()) from exc
                return self._post_locked(msg, supervisor_authorized=authorized)
        return self._post_locked(msg, supervisor_authorized=authorized)

    def _supervision_context(self):
        """Load strict-audit policy from the project config, never a message."""
        config_path = self.root / "config.json"
        if not config_path.is_file():
            return None
        from .config import ConfigError, load
        from .supervision import SupervisionError, validate_settings
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("project config must be an object")
            value = raw.get("supervision")
            if value is None:
                return None
            if not isinstance(value, dict):
                raise ValueError("supervision config must be an object")
            settings = dict(value)
            settings.setdefault("strict_supervision", False)
            settings.setdefault("designated_auditor", "")
            settings.setdefault("checks", [])
            cfg = load(self.root.parent)
            normalized = validate_settings(settings, cfg.data.get("workers", {}))
        except (OSError, UnicodeError, ValueError, TypeError, ConfigError,
                SupervisionError) as exc:
            raise BusError("严格监督配置无效；拒绝绕过审核") from exc
        if normalized["strict_supervision"] and cfg.director.casefold() == normalized["designated_auditor"]:
            raise BusError("指定监督员必须与项目协调者不同")
        return {**normalized, "workspace": cfg.workspace,
                "output_root": cfg.output_root}

    @staticmethod
    def _supervision_digest(value) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _write_active_record(self, agent: str, msg: Message) -> Path:
        for folder in ("processing", "ready", "blocked"):
            path = self.dirs(agent)[folder] / f"{msg.id}.json"
            if path.is_file():
                self._write(path, msg)
                return path
        raise BusError(f"活动消息不存在，拒绝写入监督记录：{msg.id}")

    def _approved_supervision_result(self, source: Message, result: Message) -> bool:
        case = source.supervision if isinstance(source.supervision, dict) else {}
        expected = case.get("result")
        if (case.get("state") != "approved" or not isinstance(expected, dict)
                or expected.get("id") != result.id
                or expected != result.to_dict()):
            return False
        review_task_data = case.get("review_task")
        review_receipt_id = case.get("review_receipt_id")
        if not isinstance(review_task_data, dict) or not isinstance(review_receipt_id, str):
            return False
        try:
            review = Message.from_dict(review_task_data, strict=True)
            receipt = next((item for item in self.find_results(
                review.id, sender=case.get("auditor"), recipient=review.result_to)
                           if item.id == review_receipt_id), None)
            if receipt is None or receipt.type != "result":
                return False
            decision = json.loads(receipt.body)
            linked = json.loads(review.body)
            if (decision.get("decision") != "approve"
                    or decision.get("task_id") != source.id
                    or decision.get("result_id") != case.get("pending_result_id")
                    or linked.get("task_id") != source.id
                    or linked.get("result_id") != case.get("pending_result_id")):
                return False
            active_review = self.find_message(review.id)
            if active_review is None:
                return False
            _fencing.check(active_review.to_dict(), {
                "task_id": review.id, "agent": case.get("auditor"),
                "claim_epoch": receipt.source_claim_epoch,
                "claim_id": receipt.source_claim_id,
                "conversation_id": receipt.source_conversation_id,
            })
            return True
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                BusError, _fencing.TicketError):
            return False

    def _post_locked(self, msg: Message, *, supervisor_authorized: bool = False) -> Message:
        msg.validate()
        policy = self._supervision_context()
        strict = bool(policy and policy["strict_supervision"])
        if strict and msg.type in ("task", "goal") and msg.to.casefold() == policy["designated_auditor"]:
            raise BusError("严格监督员只接收审核任务，不能被分派普通 task/goal")
        if msg.type in ("goal", "task", "review"):
            if (not msg.result_to or
                    msg.result_to.casefold() in {"sender", "director"}):
                raise BusError(
                    f"新 {msg.type} 消息必须显式填写具体结果收件人 result_to")
        if msg.type == "result" and not msg.reply_to:
            raise BusError("新 result 消息必须用 reply_to 关联原任务")
        if msg.type == "result":
            origin = self.validate_result_reference(msg.reply_to or "")
            origin_supervision = origin.supervision or {}
            supervision_state = origin_supervision.get("state")
            if origin.type in ("task", "goal") and supervision_state in {
                    "awaiting_audit", "decision_recorded", "approved", "rejected"}:
                if (supervision_state != "approved"
                        or not supervisor_authorized
                        or not self._approved_supervision_result(origin, msg)):
                    raise BusError(
                        "任务已进入严格监督流程：成果回执必须匹配独立审核批准票据并通过正式提交入口")
            elif strict and origin.type in ("task", "goal"):
                if not self._approved_supervision_result(origin, msg):
                    raise BusError("严格监督开启：成果回执必须先经指定监督员正式审核批准")
            if (origin.type == "review"
                    and origin_supervision.get("kind") == "audit_review"
                    and not supervisor_authorized):
                raise BusError("监督审核回执必须由带认领票据的正式提交入口创建")
        d = self.dirs(msg.to)
        targets = [d[sub] / f"{msg.id}.json"
                   for sub in ("ready", "processing", "blocked", "done")]
        if (self.find_message(msg.id) is not None or any(p.exists() for p in targets)
                or d["tmp"].joinpath(f"{msg.id}.json").exists()):
            raise BusError(f"收件箱已存在消息 id {msg.id}；拒绝覆盖，换一个唯一 id")
        tmp = d["tmp"] / f"{msg.id}.json"
        tmp.write_text(json.dumps(msg.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, d["ready"] / f"{msg.id}.json")  # 原子、跨目录
        self.audit(event="post", id=msg.id, **{"from": msg.sender, "to": msg.to},
                   type=msg.type, result_to=msg.result_to,
                   routed_by_fallback=msg.routed_by_fallback,
                   reply_to=msg.reply_to, hop=msg.hop, attempt=msg.attempt)
        if msg.type == "result" and msg.reply_to:
            origin = self.find_message(msg.reply_to)
            if origin is not None and origin.result_to and origin.result_to != msg.to:
                self.audit(event="route_mismatch", result_id=msg.id,
                           source_task_id=origin.id, declared=origin.result_to,
                           actual=msg.to)
        return msg

    def find_message(self, msg_id: str) -> Message | None:
        """在各 agent 信箱中按消息 ID 查找，供结果路由审计使用。"""
        for base in sorted(self.agents_dir.iterdir() if self.agents_dir.exists() else []):
            if not base.is_dir():
                continue
            for sub in ("ready", "processing", "blocked", "done"):
                path = base / sub / f"{msg_id}.json"
                if not path.is_file():
                    continue
                try:
                    msg = self.read(path)
                except (OSError, ValueError, BusError):
                    continue
                if msg.id == msg_id:
                    return msg
        return None

    def find_results(self, reply_to: str, *, sender: str | None = None,
                     recipient: str | None = None) -> list[Message]:
        """Find every already-posted result so retries can match an explicit ID."""
        found: list[Message] = []
        if recipient is not None:
            indexed = self.index_results(recipient)
            found = indexed.get(reply_to, [])
            if sender is not None:
                found = [msg for msg in found
                         if msg.sender.casefold() == sender.casefold()]
            return found
        else:
            bases = sorted(self.agents_dir.iterdir() if self.agents_dir.exists() else [])
        for base in bases:
            if not base.is_dir():
                continue
            for sub in ("ready", "processing", "blocked", "done"):
                for path in sorted((base / sub).glob("*.json")):
                    try:
                        msg = self.read(path)
                    except (OSError, ValueError, BusError):
                        continue
                    if (msg.type == "result" and msg.reply_to == reply_to
                            and (sender is None or msg.sender.casefold() == sender.casefold())
                            and (recipient is None or msg.to.casefold() == recipient.casefold())):
                        found.append(msg)
        return found

    def index_results(self, recipient: str) -> dict[str, list[Message]]:
        """Read one recipient's mailbox once and group its results by source task ID."""
        from .profiles import validate_agent_id
        try:
            recipient_id = validate_agent_id(recipient.casefold(), allow_system=True)
        except (AttributeError, ValueError) as exc:
            raise BusError(f"invalid result recipient: {recipient!r}") from exc
        base = self.agents_dir / recipient_id
        if not base.is_dir():
            return {}
        if not base.resolve().is_relative_to(self.agents_dir.resolve()):
            raise BusError("result recipient mailbox escapes the bus root")

        grouped: dict[str, list[Message]] = {}
        for sub in ("ready", "processing", "blocked", "done"):
            folder = base / sub
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    msg = self.read(path)
                except (OSError, ValueError, BusError):
                    continue
                if (msg.type == "result" and msg.reply_to
                        and msg.to.casefold() == recipient_id):
                    grouped.setdefault(msg.reply_to, []).append(msg)
        return grouped

    def find_result(self, reply_to: str, *, sender: str | None = None,
                    recipient: str | None = None) -> Message | None:
        """Find the first posted result (compatibility helper for callers)."""
        return next(iter(self.find_results(reply_to, sender=sender,
                                           recipient=recipient)), None)

    def make(self, sender: str, to: str, type: str, title: str = "", body: str = "",
             **kw: Any) -> Message:
        return Message(id=new_id(), ts=now_iso(), sender=sender, to=to, type=type,
                       title=title, body=body, **kw)

    # ---- 读取 ----
    def pending(self, agent: str,
                message_types: Iterable[str] | None = None) -> list[Path]:
        ready = self.dirs(agent)["ready"]
        paths = sorted(p for p in ready.glob("*.json") if p.is_file()
                       and not self._discard_terminal_duplicate(agent, p))
        if message_types is None:
            return paths
        allowed = set(message_types)
        out = []
        for path in paths:
            try:
                if self.read(path).type in allowed:
                    out.append(path)
            except (OSError, ValueError, BusError):
                continue
        return out

    def read(self, path: Path) -> Message:
        data = json.loads(path.read_text(encoding="utf-8"))
        msg = Message.from_dict(data)
        msg.validate()
        return msg

    @serialized_task
    def claim(self, agent: str, path: Path) -> Message | None:
        """原子抢占。抢不到说明别人先拿走了。"""
        with task_lock(self.root, "member-claims", agent):
            msg = self._claim_member_locked(agent, path)
        if msg is None:
            return None
        try:
            ws_root = self.root if self.root.name != ".agentbus" else self.root.parent.resolve()
            from .trigger.service import TriggerDeliveryService
            TriggerDeliveryService(ws_root).reset_circuit_breaker()
            TriggerDeliveryService(ws_root).on_work_state_changed(agent)
        except Exception as exc:
            self._audit_best_effort(event='trigger_work_state_sync_failed', agent=agent,
                                   id=msg.id, reason=type(exc).__name__)
        return msg

    def active_work(self, agent: str) -> list[Message]:
        """Observe work claims without clearing or recovering existing records."""
        d = self.dirs(agent)
        active = []
        for path in sorted(d["processing"].glob("*.json")):
            terminal = d["done"] / path.name
            if terminal.is_file():
                try:
                    self.read(terminal)
                    continue
                except (OSError, ValueError, BusError):
                    pass
            try:
                message = self.read(path)
            except (OSError, ValueError, BusError) as exc:
                raise BusError(f"工位 {agent} 的在途记录 {path.stem} 无法核对；请先检查原记录。") from exc
            if message.type in ("task", "goal", "review"):
                active.append(message)
        return active

    def check_work_slot(self, agent: str) -> None:
        active = self.active_work(agent)
        if active:
            identities = ", ".join(message.id for message in active[:3])
            raise BusError(f"工位 {agent} 已有 {len(active)} 项在途工作（{identities}），"
                           "请在原认领会话完成或正式交接后再领新任务。")

    def _claim_member_locked(self, agent: str, path: Path) -> Message | None:
        d = self.dirs(agent)
        target = d["processing"] / path.name
        if target.exists():
            return None
        if not path.is_file():
            return None
        try:
            candidate = self.read(path)
        except (OSError, ValueError, BusError) as exc:
            codes = {key: value if type(value := getattr(exc, key, None)) is int else None
                     for key in ('errno', 'winerror')}
            self._audit_best_effort(event='claim_failed', stage='read', agent=agent,
                                   id=path.stem, filename=path.name,
                                   error_type=type(exc).__name__, **codes)
            raise
        if candidate.type in ("task", "goal", "review"):
            self.check_work_slot(agent)
        stage = 'read_bytes'
        try:
            original = path.read_bytes()
            stage = 'move'
            os.replace(path, target)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError) and not path.exists():
                return None  # Normal race: another claimant moved the source.
            codes = {key: value if type(value := getattr(exc, key, None)) is int else None
                     for key in ('errno', 'winerror')}
            self._audit_best_effort(event='claim_failed', stage=stage, agent=agent,
                                   id=path.stem, filename=path.name,
                                   error_type=type(exc).__name__, **codes)
            raise
        stage = "read"
        acquired_ticket = None
        try:
            msg = self.read(target)
            # §3.10 工作流管道法定门禁：核验前序依赖是否就绪
            try:
                _pipeline.verify_pipeline_prerequisites_for_claim(self.root, msg.to_dict(), agent)
            except _pipeline.WorkflowPipelineError as pipe_err:
                self.audit(event="claim_pipeline_violation", id=msg.id, agent=agent, error=str(pipe_err))
                raise BusError(f"工作流管道熔断拦截：{pipe_err}") from pipe_err

            msg.status = "claimed"
            msg.claimed_by = agent
            msg.claimed_at = now_iso()
            # Claim fencing: every formal claim advances the epoch and issues a
            # fresh unguessable claim_id. The caller must keep this ticket and
            # present it at commit time; it can never be reconstructed from the
            # current task afterwards. Old epochs can never win again.
            ticket = _fencing.issue(msg.id, agent, _fencing.record_epoch(msg) + 1)
            _fencing.apply_ticket(msg, ticket)
            msg.claim_epoch = ticket.claim_epoch
            msg.claim_id = ticket.claim_id
            msg.claim_agent = ticket.agent
            stage = "lock"
            try:
                _exclusive_lock.acquire_task_lock(self.root, msg.id, agent, ticket,
                                                 note=f"由成员 {agent} 成功认领任务并建立唯一排他独占锁定")
                acquired_ticket = ticket
            except _exclusive_lock.ExclusiveLockError as lock_exc:
                self.audit(event="claim_locked_conflict", id=msg.id, agent=agent, error=str(lock_exc))
                raise BusError(f"TASK_ALREADY_LOCKED: {lock_exc}") from lock_exc
            stage = "write"
            self._write(target, msg)
            stage = "audit"
            self.audit(event="claim", agent=agent, id=msg.id, hop=msg.hop,
                       attempt=msg.attempt, claim_epoch=ticket.claim_epoch,
                       claim_fingerprint=_fencing.fingerprint(ticket.claim_id))
        except Exception as exc:
            # E6 (CLAIM_FENCING_NEXT.md): a claim that fails after the atomic
            # move must not leave a half-claimed task that nobody can fence.
            # Put the original bytes back in ready/ so the next attempt sees an
            # unclaimed task, then audit the failure. Requeue failures are
            # reported but never silently swallow the original error.
            stage_now = stage
            codes = {}
            for key in ("errno", "winerror"):
                value = getattr(exc, key, None)
                codes[key] = value if type(value) is int else None
            self._audit_best_effort(event="claim_failed", stage=stage_now, agent=agent,
                                   id=target.stem, filename=target.name,
                                   error_type=type(exc).__name__, **codes)
            if stage == 'audit':
                # The signed claim is complete on disk. Audit failure does not
                # authorize a second executor; preserve it for explicit recovery.
                raise
            if acquired_ticket is not None:
                try:
                    _exclusive_lock.release_task_lock(self.root, target.stem, agent, acquired_ticket)
                except (OSError, ValueError) as cleanup_exc:
                    raise BusError('认领失败且无法释放本次排他锁；保留 processing 现场供恢复') from cleanup_exc
            try:
                staging = target.with_name('.' + target.name + '.rollback.tmp')
                staging.write_bytes(original)
                os.replace(staging, target)
                os.replace(target, path)
                self._audit_best_effort(event="claim_requeued", agent=agent,
                                       id=target.stem, stage=stage_now)
            except OSError as rollback_exc:
                self._audit_best_effort(event="claim_requeue_failed", agent=agent,
                                       id=target.stem, stage=stage_now,
                                       error_type=type(rollback_exc).__name__)
            raise exc
        return msg

    def _active_record(self, agent: str, msg_id: str) -> Message | None:
        """Read the on-disk record for a task across the three active folders."""
        d = self.dirs(agent)
        for sub in ("processing", "ready", "blocked"):
            path = d[sub] / ("%s.json" % msg_id)
            if path.is_file():
                try:
                    return self.read(path)
                except (OSError, ValueError, BusError):
                    return None
        return None

    def check_claim(self, agent: str, msg: Message, ticket, *, evidence=None) -> Any:
        """Verify a presented ticket against the on-disk record.

        The on-disk record is the only authority. A ticket is never taken from
        the caller's message object, so a stale caller cannot smuggle in the
        current epoch by re-reading the task.
        """
        current = self._active_record(agent, msg.id)
        if current is None:
            done_path = self.dirs(agent)['done'] / f"{msg.id}.json"
            if done_path.is_file():
                try:
                    current = self.read(done_path)
                except (ValueError, TypeError, BusError):
                    current = None
        if current is None:
            raise BusError(f"无法校验票据：{agent} 信箱中找不到活动记录 {msg.id}")
        try:
            return _fencing.check(current.to_dict(), ticket)
        except _fencing.TicketError as exc:
            self.audit(event="claim_ticket_rejected", agent=agent, id=msg.id,
                       reason_code=exc.code,
                       claim_epoch=_fencing.record_epoch(current.to_dict()))
            if evidence is not None:
                raise self._reject_late(agent, current, reason_code=exc.code,
                                        note=str(exc), evidence=evidence) from exc
            raise LateResultRejected(exc.code, str(exc), agent=agent,
                                     task_id=msg.id, quarantine_path=None) from exc

    def _reject_late(self, agent: str, msg: Message, *, reason_code: str,
                     note: str, evidence: dict) -> LateResultRejected:
        """Quarantine late evidence and audit it. Never advances the task.

        Preserving the evidence must succeed or this raises; it is never
        silently dropped (CLAIM_FENCING_NEXT.md, "提交和兼容").
        """
        try:
            path = _fencing.quarantine(self.root, msg.id,
                                      evidence_id=msg.id + "-" + reason_code.lower(),
                                      payload=evidence, reason_code=reason_code,
                                      note=note)
        except OSError as exc:
            self.audit(event="late_result_evidence_lost", agent=agent, id=msg.id,
                       reason_code=reason_code, error_type=type(exc).__name__)
            raise BusError(f"迟到证据保留失败，不得静默丢弃: {exc}") from exc
        self.audit(event="late_result_rejected", agent=agent, id=msg.id,
                   reason_code=reason_code,
                   quarantine=str(path.relative_to(self.root)).replace("\\", "/"))
        return LateResultRejected(reason_code, note, agent=agent,
                                  task_id=msg.id, quarantine_path=path)

    @serialized_task
    def commit_result(self, agent: str, msg: Message, ticket, *,
                      body: str = "", result_to: str | None = None,
                      artifacts: list[dict[str, Any]] | None = None,
                      artifact_root: str | Path | None = None,
                      exit_code: int | None = None,
                      duration_s: float = 0.0, routed_by_fallback=None) -> Message:
        """Fenced submission: post the formal result, then close the task.

        Order matters: the result is persisted first and the terminal record
        second, so a crash between them leaves a recoverable valid result and
        never a "completed" task without a receipt.
        """
        current = self._active_record(agent, msg.id)
        if current is None:
            terminal = self.dirs(agent)['done'] / (msg.id + '.json')
            if terminal.is_file():
                current = self.read(terminal)
                self.check_terminal_ticket(current, ticket)
                self._release_terminal_task_lock(agent, msg.id, ticket)
                raise BusError('任务已终结；拒绝重复提交，调用方可只读复用已有回执')
        if current is None:
            raise BusError(f"无法提交结果：{agent} 信箱中找不到活动记录 {msg.id}")
        evidence = {"agent": agent, "task_id": msg.id,
                    "presented": ticket.to_dict() if hasattr(ticket, "to_dict") else ticket,
                    "body_bytes": len(body.encode("utf-8")),
                    "artifacts": artifacts or [],
                    "exit_code": exit_code,
                    "duration_s": duration_s}
        try:
            presented = _fencing.check(current.to_dict(), ticket)
        except _fencing.TicketError as exc:
            raise self._reject_late(agent, current, reason_code=exc.code,
                                    note=str(exc), evidence=evidence) from exc

        from . import workflow_pipeline as _workflow_pipeline
        try:
            ws_root = self.root if self.root.name != ".agentbus" else self.root.parent.resolve()
            _workflow_pipeline.verify_pipeline_prerequisites_for_delivery(
                self.root,
                current.to_dict(),
                agent,
                ticket,
                artifacts=artifacts,
                workspace_root=ws_root,
                artifact_root=artifact_root,
                require_artifacts=exit_code in (None, 0),
            )
        except _workflow_pipeline.WorkflowPipelineError as pipe_err:
            raise BusError(f"工作流管道门禁硬拦截：{pipe_err.detail or pipe_err.code}") from pipe_err

        policy = self._supervision_context()
        supervision_state = ((current.supervision or {}).get("state")
                             if isinstance(current.supervision, dict) else None)
        is_pending_supervision = (
            current.type in ("task", "goal")
            and supervision_state in (
                "awaiting_audit", "decision_recorded", "approved", "rejected"))
        is_audit_review = (
            current.type == "review"
            and isinstance(current.supervision, dict)
            and current.supervision.get("kind") == "audit_review")
        if (is_pending_supervision or is_audit_review) and not (
                policy and policy.get("strict_supervision")):
            raise BusError(
                "该任务已进入严格监督流程；配置关闭或缺失不能绕过独立审核")
        if policy and policy["strict_supervision"]:
            if current.type in ("task", "goal"):
                return self._commit_supervised_work(
                    agent, current, presented, body=body, result_to=result_to,
                    artifacts=artifacts, exit_code=exit_code,
                    duration_s=duration_s, routed_by_fallback=routed_by_fallback,
                    policy=policy)
            if current.type == "review" and (current.supervision or {}).get("kind") == "audit_review":
                return self._commit_audit_review(
                    agent, current, presented, body=body, result_to=result_to,
                    artifacts=artifacts, exit_code=exit_code, duration_s=duration_s,
                    routed_by_fallback=routed_by_fallback, policy=policy)

        prior = self._valid_committed_result(current)
        if prior is not None:
            self._finish_receipt(agent, current, prior, presented)
            return prior
        target = result_to or current.result_to or agent
        route_changed = current.result_to != target or (routed_by_fallback and current.routed_by_fallback != routed_by_fallback)
        current.result_to = target
        if routed_by_fallback:
            current.routed_by_fallback = routed_by_fallback
        if route_changed:
            # Persist an explicit routing decision before posting the receipt.
            # Otherwise a crash before finish leaves a valid routed result
            # which the old active record can no longer bind to its source.
            for folder in ('processing', 'ready', 'blocked'):
                path = self.dirs(agent)[folder] / (current.id + '.json')
                if path.is_file():
                    self._write(path, current)
                    break
        result = Message(
            id=new_id("m"), ts=now_iso(), sender=agent, to=target, type="result",
            title=current.title, body=body, reply_to=current.id, result_to=target,
            artifacts=list(artifacts or []), exit_code=exit_code, duration_s=duration_s,
            routed_by_fallback=routed_by_fallback,
            hop=current.hop, attempt=current.attempt,
            job_id=current.job_id, plan_id=current.plan_id,
            plan_sha256=current.plan_sha256,
        )
        if presented is not None:
            result.source_claim_epoch = presented.claim_epoch
            result.source_claim_id = presented.claim_id
            result.source_conversation_id = presented.conversation_id
        self.post(result)
        self._finish_receipt(agent, current, result, presented)
        # 触发唤醒组长挂载点（异常隔离）
        try:
            ws_root = self.root if self.root.name != ".agentbus" else self.root.parent.resolve()
            from .trigger.service import TriggerDeliveryService
            tds = TriggerDeliveryService(ws_root)
            tds.reset_circuit_breaker()
            tds.trigger_for_coordinator(result)
        except Exception as exc:
            # The receipt is already durable; make the missed coordinator wake-up visible.
            self._audit_best_effort(event='coordinator_wakeup_failed', agent=agent,
                                   id=result.id, reason=type(exc).__name__)
        return result

    def _commit_supervised_work(self, agent, current, presented, *, body,
                                result_to, artifacts, exit_code, duration_s,
                                routed_by_fallback, policy):
        auditor = policy["designated_auditor"]
        if agent.casefold() == auditor:
            raise BusError("执行者与指定监督员相同；严格监督禁止自审")
        if current.to.casefold() != agent.casefold():
            raise BusError("任务执行身份与认领成员不一致")
        target = result_to or current.result_to
        if not isinstance(target, str) or not target.strip():
            raise BusError("严格监督任务必须有明确的结果收件人")

        existing = current.supervision if isinstance(current.supervision, dict) else {}
        if existing:
            if existing.get("state") == "approved":
                review_data = existing.get("review_task")
                receipt_data = existing.get("review_result")
                if not isinstance(review_data, dict) or not isinstance(receipt_data, dict):
                    raise BusError("已批准任务缺少独立审核凭证；保留当前记录")
                try:
                    expected_review = Message.from_dict(review_data, strict=True)
                    review_receipt = Message.from_dict(receipt_data, strict=True)
                except (TypeError, ValueError, KeyError) as exc:
                    raise BusError("已批准任务的审核凭证无法解析；保留当前记录") from exc
                review = self.find_message(expected_review.id)
                if review is None:
                    raise BusError("已批准任务找不到原审核记录；保留当前记录")

                # Reuse the same locked physical verification and completion
                # path as the original approval. A retry must not turn a
                # previously approved, now-mutated artifact into a delivery.
                self._complete_supervision_decision(
                    agent, current, review, review_receipt)
                saved_result = existing.get("result")
                if not isinstance(saved_result, dict):
                    raise BusError("已批准任务缺少正式回执；保留当前记录")
                final = Message.from_dict(saved_result, strict=True)
                prior = self.find_result(current.id, sender=agent,
                                         recipient=final.to)
                if prior is None or prior.to_dict() != final.to_dict():
                    raise BusError("已批准任务结果回执缺失或不一致；保留证据并人工核对")
                return prior
            if existing.get("state") != "awaiting_audit":
                raise BusError("该监督任务已有终态；不得重复提交或覆盖")
            saved_result = existing.get("result")
            same_submission = (isinstance(saved_result, dict)
                              and saved_result.get("body") == body
                              and saved_result.get("artifacts", []) == list(artifacts or [])
                              and saved_result.get("exit_code") == exit_code
                              and saved_result.get("duration_s") == duration_s)
            if not same_submission:
                raise BusError("待审成果已固定；不得以同一认领票据替换内容")
            self._ensure_supervision_review(current, agent)
            return Message.from_dict(saved_result, strict=True)

        from .supervision import (SupervisionError, assess_check_for_audit,
                                  run_allowlisted_checks,
                                  scan_text_artifacts, verify_artifacts)
        manifest = list(artifacts or [])
        try:
            artifact_evidence = verify_artifacts(policy["output_root"], manifest)
            raw_checks = run_allowlisted_checks(policy["workspace"], policy["checks"])
            # Checks are trusted owner-configured commands, but they still run
            # as subprocesses against the workspace and may touch declared
            # outputs. Re-read the artifacts after those commands so the audit
            # evidence cannot certify an earlier byte snapshot.
            artifact_evidence_after_checks = verify_artifacts(
                policy["output_root"], manifest)
            text_findings_after_checks = scan_text_artifacts(
                policy["output_root"], manifest)
        except (OSError, ValueError, TypeError, SupervisionError) as exc:
            raise BusError("严格监督证据收集失败；任务保持未完成") from exc
        check_evidence = []
        for configured, item in zip(policy["checks"], raw_checks):
            assessment = assess_check_for_audit(configured, item)
            check_evidence.append({
                "name": item.get("name", ""), "exit_code": item.get("exit_code"),
                "timed_out": item.get("timed_out") is True,
                "duration_seconds": item.get("duration_seconds"),
                "error": item.get("error", ""), "ok": item.get("ok") is True,
                **assessment,
            })
        evidence = {
            "artifact_verification": artifact_evidence_after_checks,
            "artifact_verification_before_checks": artifact_evidence,
            "artifact_stable_during_checks": (
                artifact_evidence == artifact_evidence_after_checks),
            "text_findings": text_findings_after_checks,
            "checks": check_evidence,
            "checks_sha256": self._supervision_digest(policy["checks"]),
            "exit_code": exit_code,
            "reviewable": (artifact_evidence.get("verdict") == "pass"
                           and artifact_evidence_after_checks.get("verdict") == "pass"
                           and artifact_evidence == artifact_evidence_after_checks
                           and all(item["ok"] and item["audit_qualifying"]
                                   for item in check_evidence)
                           and exit_code == 0),
        }
        evidence_sha256 = self._supervision_digest(evidence)
        result = Message(
            id=new_id("m"), ts=now_iso(), sender=agent, to=target.strip(),
            type="result", title=current.title, body=body, reply_to=current.id,
            result_to=target.strip(), artifacts=manifest, exit_code=exit_code,
            duration_s=duration_s, routed_by_fallback=routed_by_fallback,
            hop=current.hop, attempt=current.attempt, job_id=current.job_id,
            plan_id=current.plan_id, plan_sha256=current.plan_sha256,
            supervision={"state": "awaiting_audit", "evidence_sha256": evidence_sha256},
            source_claim_epoch=presented.claim_epoch if presented else 0,
            source_claim_id=presented.claim_id if presented else "",
            source_conversation_id=presented.conversation_id if presented else "",
        )
        case_id = uuid.uuid4().hex
        review_id = new_id("m")
        review_link = {
            "kind": "audit_review", "case_id": case_id,
            "task_id": current.id, "result_id": result.id,
            "executor": agent, "auditor": auditor,
            "evidence_sha256": evidence_sha256,
        }
        review_payload = {
            "schema_version": 1, "protocol": "agentbus.strict-supervision.review.v1",
            "task_id": current.id, "result_id": result.id,
            "executor": agent, "auditor": auditor,
            "result_body": body, "artifacts": manifest,
            "evidence": evidence,
            "required_decision": {
                "decision": "approve or reject",
                "artifacts_reviewed": True, "checks_reviewed": True,
                "text_scan_reviewed": True,
                "reason": "nonempty explanation",
            },
            "evidence_sha256": evidence_sha256,
        }
        review = Message(
            id=review_id, ts=now_iso(), sender=agent, to=auditor, type="review",
            title=f"独立审核：{current.title}"[:240],
            body=json.dumps(review_payload, ensure_ascii=False, indent=2),
            reply_to=current.id, result_to=target.strip(), refs=[result.id],
            supervision=review_link,
        )
        current.result_to = target.strip()
        if routed_by_fallback:
            current.routed_by_fallback = routed_by_fallback
        current.result = body
        current.exit_code = exit_code
        current.duration_s = duration_s
        current.artifacts = manifest
        current.supervision = {
            **review_link, "state": "awaiting_audit",
            "pending_result_id": result.id, "result": result.to_dict(),
            "evidence": evidence, "review_task_id": review.id,
            "review_task": review.to_dict(),
        }
        current.status = "awaiting_audit"
        self._write_active_record(agent, current)
        self._ensure_supervision_review(current, agent)
        self.audit(event="awaiting_audit", agent=agent, id=current.id,
                   result_id=result.id, review_task_id=review.id,
                   auditor=auditor, evidence_sha256=evidence_sha256,
                   evidence_reviewable=evidence["reviewable"])
        return result

    def _ensure_supervision_review(self, source, executor):
        case = source.supervision if isinstance(source.supervision, dict) else {}
        review_data = case.get("review_task")
        if not isinstance(review_data, dict):
            raise BusError("监督记录缺少绑定的审核任务")
        review = Message.from_dict(review_data, strict=True)
        existing = self.find_message(review.id)
        if existing is not None:
            if (existing.type != "review"
                    or (existing.supervision or {}).get("case_id") != case.get("case_id")
                    or existing.to != case.get("auditor")):
                raise BusError("审核任务 ID 已被其他记录占用")
            return existing
        self.post(review)
        self.audit(event="supervision_review_dispatched", agent=executor,
                   id=source.id, review_task_id=review.id,
                   auditor=case.get("auditor"), result_id=case.get("pending_result_id"))
        return review

    def _validate_supervision_review_binding(self, source, review, case):
        """Require the claimed audit task to be the exact review of this case."""
        review_data = case.get("review_task")
        result_data = case.get("result")
        evidence = case.get("evidence")
        if (not isinstance(review_data, dict) or not isinstance(result_data, dict)
                or not isinstance(evidence, dict)):
            raise BusError("监督记录缺少不可变审核证据")
        try:
            expected_review = Message.from_dict(review_data, strict=True)
            pending = Message.from_dict(result_data, strict=True)
            payload = json.loads(review.body)
        except (TypeError, ValueError, KeyError) as exc:
            raise BusError("监督任务或审核证据无法解析") from exc

        expected_link = {
            "kind": "audit_review", "case_id": case.get("case_id"),
            "task_id": source.id, "result_id": pending.id,
            "executor": case.get("executor"), "auditor": case.get("auditor"),
            "evidence_sha256": case.get("evidence_sha256"),
        }
        if (review.id != expected_review.id
                or review.sender != expected_review.sender
                or review.to != expected_review.to
                or review.type != "review"
                or review.body != expected_review.body
                or review.reply_to != source.id
                or review.result_to != pending.to
                or review.supervision != expected_link
                or expected_review.supervision != expected_link):
            raise BusError("审核任务与源任务绑定不一致；拒绝空操作或伪造通过")

        required_keys = {
            "schema_version", "protocol", "task_id", "result_id", "executor",
            "auditor", "result_body", "artifacts", "evidence",
            "required_decision", "evidence_sha256",
        }
        expected_decision = {
            "decision": "approve or reject",
            "artifacts_reviewed": True, "checks_reviewed": True,
            "text_scan_reviewed": True, "reason": "nonempty explanation",
        }
        if (not isinstance(payload, dict) or set(payload) != required_keys
                or payload.get("schema_version") != 1
                or payload.get("protocol") != "agentbus.strict-supervision.review.v1"
                or payload.get("task_id") != source.id
                or payload.get("result_id") != pending.id
                or payload.get("executor") != case.get("executor")
                or payload.get("auditor") != case.get("auditor")
                or payload.get("result_body") != pending.body
                or payload.get("artifacts") != pending.artifacts
                or payload.get("evidence") != evidence
                or payload.get("required_decision") != expected_decision
                or payload.get("evidence_sha256") != case.get("evidence_sha256")
                or self._supervision_digest(evidence) != case.get("evidence_sha256")):
            raise BusError("审核内容未绑定原始产物摘要或检查证据；拒绝通过")
        return pending, evidence

    def _verify_supervision_artifacts_unchanged(self, pending, evidence, policy):
        """Recheck physical bytes immediately before accepting an audit decision."""
        from .supervision import SupervisionError, scan_text_artifacts, verify_artifacts

        if evidence.get("checks_sha256") != self._supervision_digest(policy["checks"]):
            raise BusError("审核期间允许的检查配置已变化；需要重新提交审核")
        checks = evidence.get("checks")
        if (not isinstance(checks, list) or len(checks) != len(policy["checks"])
                or any(not isinstance(item, dict) or item.get("ok") is not True
                       or item.get("exit_code") != 0 or item.get("timed_out") is True
                       or item.get("error") or item.get("audit_qualifying") is not True
                       for item in checks)):
            raise BusError("原始自动检查证据不完整或未通过；不能批准")
        try:
            current_artifacts = verify_artifacts(policy["output_root"], pending.artifacts)
            current_findings = scan_text_artifacts(policy["output_root"], pending.artifacts)
        except (OSError, ValueError, TypeError, SupervisionError) as exc:
            raise BusError("批准前无法重新核验产物；任务保持待审") from exc
        if (current_artifacts.get("verdict") != "pass"
                or current_artifacts != evidence.get("artifact_verification")
                or current_findings != evidence.get("text_findings")):
            raise BusError("审核期间产物或文本检查结果发生变化；拒绝批准")

    def _commit_audit_review(self, agent, current, presented, *, body,
                             result_to, artifacts, exit_code, duration_s,
                             routed_by_fallback, policy):
        link = current.supervision if isinstance(current.supervision, dict) else {}
        auditor = policy["designated_auditor"]
        if (agent.casefold() != auditor or current.to.casefold() != auditor
                or link.get("auditor") != auditor
                or link.get("executor", "").casefold() == auditor):
            raise BusError("只有不同于执行者的指定监督员可以审核此任务")
        executor, task_id = link.get("executor"), link.get("task_id")
        if not isinstance(executor, str) or not isinstance(task_id, str):
            raise BusError("监督审核任务缺少源任务绑定")
        source = self._active_record(executor, task_id)
        if (source is None or source.status != "awaiting_audit"
                or (source.supervision or {}).get("state") not in ("awaiting_audit", "decision_recorded")
                or (source.supervision or {}).get("review_task_id") != current.id
                or (source.supervision or {}).get("case_id") != link.get("case_id")
                or (source.supervision or {}).get("pending_result_id") != link.get("result_id")):
            raise BusError("源任务不处于这条审核任务对应的待审状态")
        try:
            decision = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise BusError("审核回执必须是结构化 JSON") from exc
        required = {"schema_version", "task_id", "result_id", "decision",
                    "artifacts_reviewed", "checks_reviewed", "text_scan_reviewed", "reason"}
        if (not isinstance(decision, dict) or set(decision) != required
                or decision.get("schema_version") != 1
                or decision.get("task_id") != task_id
                or decision.get("result_id") != link.get("result_id")
                or decision.get("decision") not in ("approve", "reject")
                or any(decision.get(key) is not True for key in (
                    "artifacts_reviewed", "checks_reviewed", "text_scan_reviewed"))
                or not isinstance(decision.get("reason"), str)
                or not decision["reason"].strip() or len(decision["reason"]) > 4000):
            raise BusError("审核回执字段无效，或未逐项确认独立检查证据")
        case = source.supervision
        pending, evidence = self._validate_supervision_review_binding(
            source, current, case)
        if decision["decision"] == "approve" and evidence.get("reviewable") is not True:
            raise BusError("物理产物或允许的自动检查未通过；只能驳回，不能批准")
        if decision["decision"] == "approve":
            policy = self._supervision_context()
            if not policy or not policy.get("strict_supervision"):
                raise BusError("严格监督配置已关闭或不可用；拒绝接受批准")
            self._verify_supervision_artifacts_unchanged(
                pending, evidence, policy)
        expected_target = current.result_to
        if not isinstance(expected_target, str) or not expected_target.strip():
            raise BusError("审核回执缺少正式结果收件人")
        if result_to is not None and result_to != expected_target:
            raise BusError("审核结果收件人与审核任务声明不一致；未记录决定")
        target = expected_target

        if case.get("state") == "decision_recorded":
            if decision != case.get("decision"):
                raise BusError("审核决定已固定；同一审核任务不能更换结论")
            receipt_data = case.get("review_result")
            if not isinstance(receipt_data, dict):
                raise BusError("已记录的审核决定缺少正式回执")
            receipt = Message.from_dict(receipt_data, strict=True)
            posted = self.find_result(current.id, sender=auditor,
                                      recipient=receipt.to)
            if posted is None:
                self._post_locked(receipt, supervisor_authorized=True)
                posted = receipt
            elif posted.to_dict() != receipt.to_dict():
                raise BusError("审核结果收件箱与已记录的决定不一致；保留证据")
            self._finish_receipt(agent, current, posted, presented)
            return self._complete_supervision_decision(executor, source, current, posted)

        receipt = Message(
            id=new_id("m"), ts=now_iso(), sender=agent, to=target.strip(),
            type="result", title=current.title, body=body, reply_to=current.id,
            result_to=target.strip(), artifacts=list(artifacts or []),
            exit_code=exit_code, duration_s=duration_s,
            routed_by_fallback=routed_by_fallback, hop=current.hop,
            attempt=current.attempt, supervision={
                "kind": "audit_decision", "case_id": case["case_id"],
                "decision": decision["decision"],
                "evidence_sha256": case["evidence_sha256"],
            },
            source_claim_epoch=presented.claim_epoch if presented else 0,
            source_claim_id=presented.claim_id if presented else "",
            source_conversation_id=presented.conversation_id if presented else "",
        )
        # Record the signed decision intent before the result write. A crash
        # here leaves the original result unapprovable; a retry can reconcile it.
        case["decision"] = decision
        case["review_result"] = receipt.to_dict()
        case["review_result_id"] = receipt.id
        case["state"] = "decision_recorded"
        source.supervision = case
        self._write_active_record(executor, source)
        self._post_locked(receipt, supervisor_authorized=True)
        self._finish_receipt(agent, current, receipt, presented)
        return self._complete_supervision_decision(executor, source, current, receipt)

    def _complete_supervision_decision(self, executor, source, review, receipt):
        case = source.supervision if isinstance(source.supervision, dict) else {}
        if case.get("state") not in ("decision_recorded", "approved", "rejected"):
            raise BusError("没有可完成的正式监督决定")
        if (case.get("review_result_id") != receipt.id
                or case.get("review_result") != receipt.to_dict()
                or receipt.reply_to != review.id
                or receipt.sender != case.get("auditor")):
            raise BusError("监督回执与待审任务记录不匹配")
        try:
            decision = json.loads(receipt.body)
            recorded = case.get("decision")
            if not isinstance(recorded, dict) or decision != recorded:
                raise ValueError("decision mismatch")
            if receipt.supervision != {
                    "kind": "audit_decision", "case_id": case.get("case_id"),
                    "decision": decision.get("decision"),
                    "evidence_sha256": case.get("evidence_sha256")}:
                raise ValueError("audit evidence binding mismatch")
            _fencing.check(review.to_dict(), {
                "task_id": review.id, "agent": case["auditor"],
                "claim_epoch": receipt.source_claim_epoch,
                "claim_id": receipt.source_claim_id,
                "conversation_id": receipt.source_conversation_id,
            })
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                _fencing.TicketError) as exc:
            raise BusError("监督员回执无法通过认领票据与内容核对") from exc
        expected_recipient = review.result_to
        posted_receipt = self.find_result(
            review.id, sender=case.get("auditor"),
            recipient=expected_recipient)
        if (not expected_recipient or receipt.to != expected_recipient
                or posted_receipt is None
                or posted_receipt.id != receipt.id
                or posted_receipt.to_dict() != receipt.to_dict()):
            raise BusError("正式监督回执缺失或不一致；拒绝继续完成任务")
        pending, evidence = self._validate_supervision_review_binding(
            source, review, case)
        case["review_receipt_id"] = receipt.id
        if decision["decision"] == "reject":
            if case.get("state") == "rejected" and source.status == "rejected":
                return receipt
            case["state"] = "rejected"
            source.supervision = case
            source.status = "rejected"
            source.result = decision["reason"].strip()
            if _fencing.record_epoch(source) > 0:
                _fencing.revoke(source)
            blocked = self.dirs(executor)["blocked"] / f"{source.id}.json"
            processing = self.dirs(executor)["processing"] / f"{source.id}.json"
            if blocked.exists():
                existing = self.read(blocked)
                if (existing.status == "rejected"
                        and existing.supervision.get("case_id") == case.get("case_id")
                        and existing.to_dict() == source.to_dict()):
                    # A crash may occur after the blocked terminal copy was
                    # atomically written but before the processing copy was
                    # removed. Retry only the exact same rejected record.
                    processing.unlink(missing_ok=True)
                    self.audit(event="rejected_duplicate_copy_removed",
                               agent=executor, id=source.id,
                               auditor=case["auditor"],
                               review_result_id=receipt.id)
                    return receipt
                raise BusError("拒绝归档目标与待处理证据不一致；保留原任务与证据")
            self._write(blocked, source)
            processing.unlink(missing_ok=True)
            self.audit(event="rejected", agent=executor, id=source.id,
                       auditor=case["auditor"], review_result_id=receipt.id,
                       reason=decision["reason"].strip())
            return receipt

        if evidence.get("reviewable") is not True:
            raise BusError("证据未通过；不能完成批准流程")
        policy = self._supervision_context()
        if not policy or not policy.get("strict_supervision"):
            raise BusError("严格监督配置已关闭或不可用；拒绝归档已批准任务")
        with self._hold_supervision_artifact_leases(
                pending, policy, case["auditor"]):
            self._verify_supervision_artifacts_unchanged(
                pending, evidence, policy)
            return self._complete_approved_supervision_decision(
                executor, source, review, receipt, pending, case)

    @contextmanager
    def _hold_supervision_artifact_leases(self, pending, policy, auditor):
        """Exclude cooperative writers from final verification through commit.

        The output workspace has its own lease namespace, which may be on a
        different volume from the control workspace. These short leases are
        owned by the independent auditor and are released on every exit path.
        """
        from .lease import LeaseManager

        # Both supported writer routes have to be excluded: `agentbus lease`
        # addresses the control workspace with an absolute resource path, while
        # `agentbus files write --root output` addresses output_root/.agentbus
        # with a workspace-relative path.
        output_root = Path(policy["output_root"]).resolve()
        control_workspace = self.root.parent.resolve()
        prepared_paths = []
        held = []
        try:
            artifact_paths = sorted({
                item["path"] for item in pending.artifacts
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }, key=str.casefold)
            if len(artifact_paths) != len(pending.artifacts):
                raise BusError("审核产物清单路径无效；拒绝创建最终交付")
            for rel in artifact_paths:
                target = (output_root / rel).resolve()
                if not target.is_relative_to(output_root):
                    raise BusError("审核产物路径越界；拒绝创建最终交付")
                local_rel = (target.relative_to(control_workspace).as_posix()
                             if target.is_relative_to(control_workspace) else None)
                prepared_paths.append((rel, target, local_rel))

            control_manager = LeaseManager(self.root)
            output_manager = LeaseManager(output_root / ".agentbus")
            requests_by_key = {}
            for rel, target, local_rel in prepared_paths:
                candidates = [(control_manager, str(target))]
                if local_rel is not None:
                    candidates.append((control_manager, local_rel))
                candidates.append((output_manager, rel))
                for manager, resource_key in candidates:
                    lock_key = (str(manager.root.resolve()).casefold(),
                                manager.normalize(resource_key).casefold())
                    requests_by_key.setdefault(
                        lock_key, (manager, resource_key, rel))

            for lock_key in sorted(requests_by_key):
                manager, resource_key, rel = requests_by_key[lock_key]
                lease = manager.acquire(
                    resource_key, auditor, ttl_s=1800,
                    note="strict supervision final verification and commit",
                    msg_id=pending.reply_to)
                if lease is None:
                    holder = manager.holder(resource_key)
                    owner = (holder or {}).get("owner", "其他写入者")
                    raise BusError(
                        f"产物 {rel} 当前由 {owner} 持有写入租约；审核未提交，请释放后重试")
                held.append((manager, lease))
            yield
        except OSError as exc:
            raise BusError("无法锁定审核产物；严格监督拒绝完成交付") from exc
        finally:
            for manager, lease in reversed(held):
                if not manager.release(lease):
                    self.audit(event="supervision_artifact_lease_release_failed",
                               agent=auditor, id=pending.reply_to,
                               path=lease.rel)

    def _complete_approved_supervision_decision(
            self, executor, source, review, receipt, pending, case):
        case["state"] = "approved"
        pending.supervision = {
            "case_id": case["case_id"], "state": "approved",
            "review_receipt_id": receipt.id,
            "evidence_sha256": case["evidence_sha256"],
        }
        case["result"] = pending.to_dict()
        source.supervision = case
        self._write_active_record(executor, source)
        existing_result = self.find_result(source.id, sender=executor,
                                           recipient=pending.to)
        if existing_result is None:
            self._post_supervised_result(pending)
            existing_result = pending
        elif existing_result.to_dict() != pending.to_dict():
            raise BusError("已批准任务存在不同成果回执；保留证据并人工核对")
        self._finish_receipt(executor, source, existing_result,
                             _fencing.captured_ticket(source),
                             supervisor_authorized=True)
        try:
            self.resolve_pending_escalations(source.id, pending.id)
        except (OSError, BusError) as exc:
            self._audit_best_effort(event="escalation_resolution_failed",
                                   source_task_id=source.id, result_id=pending.id,
                                   reason=type(exc).__name__)
        self.audit(event="supervision_approved", agent=executor, id=source.id,
                   auditor=case["auditor"], review_result_id=receipt.id,
                   result_id=pending.id, evidence_sha256=case["evidence_sha256"])
        return receipt

    def check_terminal_ticket(self, current, ticket):
        try:
            return _fencing.check(current.to_dict(), ticket)
        except _fencing.TicketError as exc:
            raise LateResultRejected(exc.code, str(exc), agent=current.to, task_id=current.id) from exc

    def _valid_committed_result(self, source):
        from .jobs import receipt_binding_error
        if not source.result_to:
            return None  # A route must be explicit before receipt reconciliation.
        valid = [r for r in self.find_results(source.id, sender=source.to, recipient=source.result_to)
                 if not receipt_binding_error(source, r)]
        policy = self._supervision_context()
        if policy and policy["strict_supervision"] and source.type in ("task", "goal"):
            if not isinstance(source.supervision, dict) or source.supervision.get("state") != "approved":
                return None
            valid = [r for r in valid if self._approved_supervision_result(source, r)]
        if len(valid) > 1:
            raise BusError('该任务有多个合法回执；保留证据并人工裁决')
        return valid[0] if valid else None

    def _finish_receipt(self, agent, current, result, ticket, *,
                        supervisor_authorized=False):
        current.result, current.exit_code = result.body, result.exit_code
        current.duration_s, current.artifacts = result.duration_s, list(result.artifacts)
        from .artifact_versions import record_delivery
        record_delivery(self, current, result)
        self.finish(agent, current, status='failed' if result.exit_code not in (None, 0) else 'done',
                    ticket=ticket,
                    _supervisor_authority=(
                        _SUPERVISION_FINISH_AUTHORITY
                        if supervisor_authorized else None))

    @serialized_task
    def record_submission(self, agent, msg, ticket, *, origin, heartbeat_timeout_s=600):
        current = self._active_record(agent, msg.id)
        self.check_claim(agent, msg, ticket)
        _exclusive_lock.verify_task_lock(self.root, msg.id, agent, ticket)
        observed = _heartbeat.state(self.root, msg.id, agent, task_record=current.to_dict())
        # Delivery currently has a 600-second upper bound. A more lenient UI
        # warning threshold must not preserve a heartbeat delivery will reject.
        if (not _heartbeat.is_stale(observed, min(heartbeat_timeout_s, 600))
                and observed.get("activity") == "execution"):
            return observed
        return _heartbeat.record(self.root, msg.id, agent, ticket,
                                 task_record=current.to_dict(),
                                 activity="submission", note=f"提交活动 · {origin}；执行进展另计")

    @serialized_task
    def heartbeat(self, agent, msg, ticket, *, note=''):
        from . import heartbeat
        current = self._active_record(agent, msg.id)
        self.check_claim(agent, msg, ticket)
        _exclusive_lock.verify_task_lock(self.root, msg.id, agent, ticket)
        _exclusive_lock.acquire_task_lock(self.root, msg.id, agent, ticket,
                                          note='renewed by original claim heartbeat')
        return heartbeat.record(self.root, msg.id, agent, ticket,
                                task_record=current.to_dict(), note=note)

    def _write(self, path: Path, msg: Message) -> None:
        # Codex, 2026-10-03: never truncate an existing processing message on
        # a caught write error. This is a file update, not a durable transaction
        # or additional process ownership. Staging names cannot match *.json.
        content = json.dumps(msg.to_dict(), ensure_ascii=False, indent=2)
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.write.tmp")
        created = False
        try:
            with staging.open('x', encoding='utf-8') as stream:
                created = True
                stream.write(content)
            # Codex, 2026-10-03: Windows concurrent readers may transiently
            # deny replacement with winerror 5 as well as 32/33. Retry only
            # PermissionError, at most 49 sleeps; this is not a task retry.
            for attempt in range(MESSAGE_REPLACE_ATTEMPTS):
                try:
                    os.replace(staging, path)
                    break
                except PermissionError:
                    if attempt + 1 == MESSAGE_REPLACE_ATTEMPTS:
                        raise
                    time.sleep(MESSAGE_REPLACE_RETRY_INTERVAL_S)
        finally:
            if created:
                staging.unlink(missing_ok=True)

    @serialized_task
    def finish(self, agent: str, msg: Message, status: str = "done",
               ticket=None, *, _supervisor_authority=None) -> Path:
        """把消息标记完成并归档到 done/。

        注意：消息可能在 processing/（正常执行路径），也可能还在 ready/
        （人工接手后直接回投的路径）。两处都要清掉，否则 ready/ 里会残留
        一条"已完成但还显示待办"的幽灵任务。

        Claim fencing (OpenCode, 2026-10-08, m-1791457584499-8893c2): when the
        on-disk task is already fenced, a ticket is mandatory. Legacy unfenced
        tasks keep working unchanged; a legacy task may not be finished while
        presenting a ticket, because that would let a caller invent an epoch.
        """
        current = self._active_record(agent, msg.id)
        policy = self._supervision_context()
        supervision_state = ((current.supervision or {}).get("state")
                             if current is not None and isinstance(current.supervision, dict)
                             else None)
        strict_supervision_active = bool(
            policy and policy.get("strict_supervision"))
        supervision_recorded = supervision_state in (
            "awaiting_audit", "decision_recorded", "approved", "rejected")
        if (strict_supervision_active and msg.type in ("task", "goal")
                and current is None):
            raise BusError("严格监督开启：找不到活动任务记录，拒绝直接归档")
        if (current is not None
                and current.type in ("task", "goal")
                and (strict_supervision_active or supervision_recorded)):
            if _supervisor_authority is not _SUPERVISION_FINISH_AUTHORITY:
                raise BusError(
                    "监督任务必须通过正式审核收尾流程归档；不能直接变更终态")
            if (status != "done" or supervision_state != "approved"
                    or not strict_supervision_active
                    or self._valid_committed_result(current) is None):
                raise BusError("严格监督授权缺少有效批准回执；拒绝归档")
        if current is not None and _fencing.record_epoch(current) > 0:
            if ticket is None and (getattr(msg, "claim_id", None) or (isinstance(msg, dict) and msg.get("claim_id"))):
                ticket = _fencing.captured_ticket(msg)
            try:
                _fencing.check(current.to_dict(), ticket)
            except _fencing.TicketError as exc:
                self.audit(event="finish_ticket_rejected", agent=agent,
                           id=msg.id, reason_code=exc.code)
                raise LateResultRejected(exc.code, str(exc), agent=agent,
                                         task_id=msg.id) from exc
        elif ticket is not None:
            raise LateResultRejected("LEGACY_UNFENCED",
                                     "未栅栏化任务不得凭票据收尾",
                                     agent=agent, task_id=msg.id)
        d = self.dirs(agent)
        msg.status = status
        dst = d["done"] / f"{msg.id}.json"
        if dst.exists():
            raise BusError(f"done/ 已存在消息 id {msg.id}；拒绝覆盖")
        tmp = d["tmp"] / f"{msg.id}.json"      # 经 tmp 中转，保证 done/ 里不会出现半截文件
        self._write(tmp, msg)
        os.replace(tmp, dst)
        # Persist the terminal record before removing the only active copy.
        # If the write/rename fails, the message remains recoverable in its
        # original ready/processing/blocked location.
        for sub in ("processing", "ready", "blocked"):
            stale = d[sub] / f"{msg.id}.json"
            if stale.exists():
                stale.unlink()
        self._release_terminal_task_lock(agent, msg.id, ticket)
        self.audit(event=status, agent=agent, id=msg.id, type=msg.type,
                   sender=msg.sender, result_to=msg.result_to, rc=msg.exit_code,
                   duration_s=round(msg.duration_s, 3), hop=msg.hop, attempt=msg.attempt)
        self._sync_trigger_work_state(agent, msg.id)
        return dst

    def _release_terminal_task_lock(self, agent, message_id, ticket=None):
        if _exclusive_lock.get_task_lock_info(self.root, message_id) is None:
            return
        try:
            _exclusive_lock.release_task_lock(self.root, message_id, agent, ticket)
        except (OSError, ValueError) as exc:
            raise BusError('终态已保存但排他锁清理失败；保留证据并重试正式收尾') from exc

    def _sync_trigger_work_state(self, agent, message_id):
        try:
            from .trigger.service import TriggerDeliveryService
            ws = self.root.parent if self.root.name == '.agentbus' else self.root
            TriggerDeliveryService(ws).on_work_state_changed(agent)
        except Exception as exc:
            self._audit_best_effort(event='trigger_work_state_sync_failed', agent=agent,
                                   id=message_id, reason=type(exc).__name__)

    def resolve_pending_escalations(self, source_task_id: str, result_id: str) -> int:
        """Resolve still-ready human notices only after formal completion."""
        resolved = 0
        for path in self.pending(HUMAN_INBOX):
            try:
                escalation = self.read(path)
            except (OSError, ValueError, BusError):
                continue
            if escalation.type != "escalation" or escalation.reply_to != source_task_id:
                continue
            if self.finish_if_ready(HUMAN_INBOX, escalation, "done") is None:
                self.audit(event="escalation_resolution_skipped", id=escalation.id,
                           source_task_id=source_task_id, result_id=result_id, reason="already_claimed")
                continue
            self.audit(event="escalation_resolved", id=escalation.id,
                       source_task_id=source_task_id, result_id=result_id, reason="result_completed")
            resolved += 1
        return resolved

    @serialized_task
    def finish_if_ready(self, agent: str, msg: Message,
                        status: str = "done") -> Path | None:
        """Finish only if this call atomically wins the ready/ claim race.

        This is for resolving an unclaimed notice. If another agent has already
        moved it to processing/, leave that work untouched.
        """
        d = self.dirs(agent)
        source = d["ready"] / f"{msg.id}.json"
        processing = d["processing"] / f"{msg.id}.json"
        if processing.exists() or not source.is_file():
            return None
        if _fencing.record_epoch(self.read(source)) > 0:
            # Only pristine unfenced notices can be finalized without an
            # executor ticket. A recovered claim must be formally reclaimed.
            return None
        try:
            os.replace(source, processing)
        except OSError:
            return None
        return self.finish(agent, msg, status)

    @serialized_task
    def block(self, agent: str, msg: Message, reason: str, ticket=None) -> Path:
        """隔离需要人工澄清的消息，避免等待环反复认领，并保留审计记录。

        Claim fencing (OpenCode, 2026-10-08, m-1791457584499-8893c2): an old
        executor must not be able to block a task that a newer claim owns.
        """
        current = self._active_record(agent, msg.id)
        if current is not None and _fencing.record_epoch(current) > 0:
            if ticket is None and (getattr(msg, "claim_id", None) or (isinstance(msg, dict) and msg.get("claim_id"))):
                ticket = _fencing.captured_ticket(msg)
            try:
                _fencing.check(current.to_dict(), ticket)
            except _fencing.TicketError as exc:
                self.audit(event="block_ticket_rejected", agent=agent,
                           id=msg.id, reason_code=exc.code)
                raise LateResultRejected(exc.code, str(exc), agent=agent,
                                         task_id=msg.id) from exc
        elif ticket is not None:
            raise LateResultRejected('LEGACY_UNFENCED', '未栅栏化任务不得凭票据阻塞',
                                     agent=agent, task_id=msg.id)
        d = self.dirs(agent)
        source = d["processing"] / f"{msg.id}.json"
        if not source.exists():
            source = d["ready"] / f"{msg.id}.json"
        if not source.exists():
            blocked_target = d["blocked"] / f"{msg.id}.json"
            if blocked_target.exists():
                msg.status = "blocked"
                msg.claimed_by = ""
                msg.claimed_at = ""
                tmp = d["tmp"] / f"{msg.id}.json"
                self._write(tmp, msg)
                os.replace(tmp, blocked_target)
                self.audit(event="blocked_updated", agent=agent, id=msg.id,
                           reason=reason)
                return blocked_target
            raise BusError(f"无法阻塞：{agent} 信箱中找不到 {msg.id}")
        blocked_target = d["blocked"] / f"{msg.id}.json"
        if blocked_target.exists():
            raise BusError(f"无法阻塞：blocked/ 已有同 ID 消息 {msg.id}")
        msg.status = "blocked"
        msg.claimed_by = ""
        msg.claimed_at = ""
        tmp = d["tmp"] / f"{msg.id}.json"
        self._write(tmp, msg)
        os.replace(tmp, blocked_target)
        source.unlink(missing_ok=True)
        self.audit(event="blocked", agent=agent, id=msg.id, reason=reason)
        self._sync_trigger_work_state(agent, msg.id)
        return blocked_target

    def processing(self, agent: str) -> list[Path]:
        d = self.dirs(agent)
        return sorted(p for p in d["processing"].glob("*.json")
                      if not self._discard_terminal_duplicate(agent, p))

    def _discard_terminal_duplicate(self, agent: str, active_path: Path) -> bool:
        """Ignore an active copy left behind if a crash followed terminal commit."""
        terminal = self.dirs(agent)["done"] / active_path.name
        if not terminal.is_file():
            return False
        try:
            self.read(terminal)
        except (OSError, ValueError, BusError):
            return False
        try:
            active_path.unlink(missing_ok=True)
            self.audit(event="duplicate_terminal_copy_removed", agent=agent,
                       id=active_path.stem, location=active_path.parent.name)
        except OSError as exc:
            self.audit(event="duplicate_terminal_copy_cleanup_failed", agent=agent,
                       id=active_path.stem, location=active_path.parent.name,
                       error=str(exc))
        return True

    @serialized_task
    def relinquish_claim(self, agent: str, message_id: str, *, ticket=None,
                         reason: str = '', owner_confirmed_inactive: bool = False) -> Message:
        """Retire an old tree assignment without requeuing or claiming success.

        A live processing task needs its original ticket or the caller's literal
        inactive-owner assertion. Unclaimed ready tasks need neither. The old
        intent, deadline and attempt remain history; successors are new tasks.
        Source: project Owner task tree decision, Codex integration 2026-10-08.
        """
        from .profiles import validate_agent_id
        from .skill_install import canonical_path
        try:
            validate_agent_id(agent, allow_system=True)
        except ValueError as exc:
            raise BusError(str(exc)) from exc
        if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', message_id):
            raise BusError('释放消息 ID 格式无效')
        if not isinstance(reason, str) or len(reason) > 10000:
            raise BusError('接手说明必须是最多 10000 字的文本')
        root = canonical_path(self.root)
        base = self.agents_dir / agent
        paths = {key: base / key / (message_id + '.json')
                 for key in ('ready', 'processing', 'blocked', 'done')}
        for path in (self.root, base, self.events_path, *paths.values()):
            resolved = canonical_path(path)
            if (not resolved.is_relative_to(root) or any(
                    part.casefold() in ('.workbuddy', '不要访问', 'do not access')
                    for part in (*path.parts, *resolved.parts))):
                raise BusError('释放路径越出总线或进入私有目录；原记录保留')
        digest = lambda value: hashlib.sha256(json.dumps(
            value.to_dict(), ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        intents = [event for event in self.events()
                   if event.get('event') == 'relinquish_authorized'
                   and event.get('agent') == agent and event.get('id') == message_id
                   and event.get('reason') == reason]
        if paths['done'].exists():
            raise BusError('任务已完成；拒绝释放完成记录')
        if paths['blocked'].is_file():
            closed = self.read(paths['blocked'])
            intent = next((event for event in reversed(intents)
                           if event.get('after_sha256') == digest(closed)), None)
            if not intent or closed.status != 'blocked' or closed.claim_id:
                raise BusError('已有 blocked 不是本次正式释放记录；保留证据')
            for location in ('ready', 'processing'):
                active = paths[location]
                if active.exists():
                    observed = self.read(active)
                    if digest(observed) != intent.get('before_sha256'):
                        raise BusError('释放期间活动记录已变化；保留两份证据')
                    active.unlink()
            if not any(event.get('event') == 'relinquished_claim'
                       and event.get('agent') == agent and event.get('id') == message_id
                       and event.get('after_sha256') == digest(closed) for event in self.events()):
                self.audit(event='relinquished_claim', agent=agent, id=message_id,
                           reason=reason, after_sha256=digest(closed),
                           claim_epoch=closed.claim_epoch,
                           owner_confirmed_inactive=intent.get('owner_confirmed_inactive') is True,
                           conversation_id=intent.get('conversation_id', ''))
            self._release_recovered_task_lock(agent, message_id)
            return closed
        locations = [location for location in ('ready', 'processing') if paths[location].is_file()]
        if len(locations) != 1:
            raise BusError('原任务缺失或存在多份活动记录；拒绝释放')
        location = locations[0]
        current = self.read(paths[location])
        if (current.id != message_id or current.to != agent
                or current.type not in ('task', 'goal', 'review')
                or current.status not in ('pending', 'claimed', 'running')):
            raise BusError('原任务身份或状态不允许接力；保留记录')
        if location == 'processing' or _fencing.record_epoch(current) > 0:
            if owner_confirmed_inactive is not True:
                presented = self.check_claim(agent, current, ticket)
                if presented is None:
                    raise BusError('释放 processing 须原领取票据或明确确认原执行者停止')
        elif ticket is not None:
            raise BusError('未认领任务不得提交虚构领取票据')
        if self._valid_committed_result(current) is not None:
            raise BusError('原任务已有合法回执；先核对并完成该回执，禁止重复接力执行')
        conversation_id = (current.claim_conversation_id
                           or (_fencing.conversation_id_for_claim(current.claim_id)
                               if current.claim_id else ''))
        # The next attempt must fit the original retry/deadline/job gates, even
        # though this historical assignment does not itself increment attempt.
        self._check_recovery_gates(current, attempt_after=current.attempt + 1,
                                   allow_disabled_origin=True)
        before_hash = digest(current)
        closed = Message.from_dict(current.to_dict(), strict=True)
        closed.status, closed.claimed_by, closed.claimed_at = 'blocked', '', ''
        closed.result = reason
        if _fencing.record_epoch(closed) > 0:
            _fencing.revoke(closed)
        after_hash = digest(closed)
        self.audit(event='relinquish_authorized', agent=agent, id=message_id,
                   reason=reason, before_sha256=before_hash, after_sha256=after_hash,
                   owner_confirmed_inactive=owner_confirmed_inactive is True,
                   conversation_id=conversation_id,
                   confirmation_source='caller_assertion' if owner_confirmed_inactive is True else 'original_ticket' if location == 'processing' else 'unclaimed_ready')
        self._check_recovery_gates(current, attempt_after=current.attempt + 1,
                                   allow_disabled_origin=True)
        if digest(self.read(paths[location])) != before_hash:
            raise BusError('释放期间原任务已变化；保留记录')
        self._write(paths['blocked'], closed)
        paths[location].unlink()
        self._release_recovered_task_lock(agent, message_id)
        self.audit(event='relinquished_claim', agent=agent, id=message_id,
                   reason=reason, after_sha256=after_hash, claim_epoch=closed.claim_epoch,
                   owner_confirmed_inactive=owner_confirmed_inactive is True,
                   conversation_id=conversation_id)
        return closed

    # ---- 显式确认原认领者停止后的超时回收 ----
    def recover_claim(self, agent: str, message_id: str, *,
                      owner_confirmed_inactive: bool = False,
                      expected_claim_epoch: int | None = None) -> Message:
        """Recover one claim after an explicit caller assertion, never by age.

        Source: WorkBuddy review / Codex, 2026-10-03. This does not prove the
        old process is dead or undo earlier side effects. Existing report
        leases serialize controllers; audit hashes let a caught write, move,
        or final audit failure be retried without incrementing twice.
        """
        if owner_confirmed_inactive is not True:
            raise BusError('拒绝恢复：请先确认原认领者已停止；文件时间不是停止证明')
        from .profiles import validate_agent_id
        from .lease import LeaseManager
        from .skill_install import canonical_path
        try:
            validate_agent_id(agent, allow_system=True)
        except ValueError as exc:
            raise BusError(str(exc)) from exc
        if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', message_id):
            raise BusError('恢复消息 ID 格式无效')
        if os.name != 'nt':
            raise BusError('此平台尚未提供不可覆盖的原子恢复；原认领已保留')
        root = canonical_path(self.root)
        base = self.agents_dir / agent
        paths = {key: base / key / f'{message_id}.json'
                 for key in ('processing', 'ready', 'done', 'blocked')}
        key = f'agentbus/report/{message_id}'
        lease_name = hashlib.sha1(LeaseManager.normalize(key).encode('utf-8')).hexdigest()[:16] + '.json'
        for path in (self.root, self.agents_dir, base, self.root / 'leases', self.events_path,
                     self.root / 'config.json', self.root / 'STOP',
                     self.root / 'leases' / '.mutation.lock', self.root / 'leases' / lease_name,
                     *paths.values()):
            resolved = canonical_path(path)
            private_parts = [part for part in (*path.parts, *resolved.parts)
                             if part.casefold() in ('.workbuddy', '不要访问', 'do not access')]
            if (not resolved.is_relative_to(root) or private_parts):
                raise BusError('恢复路径越出总线或进入私有目录；原文件已保留')
        manager = LeaseManager(self.root)
        deadline = time.monotonic() + 5
        lease = None
        while lease is None:
            lease = manager.acquire(key, 'claim-recovery', ttl_s=60,
                                    note='explicit inactive owner recovery', msg_id=message_id)
            if lease is None:
                if time.monotonic() >= deadline:
                    raise BusError('消息正被回投或恢复；请稍后重新核对原认领者')
                time.sleep(0.05)
        try:
            with task_lock(self.root, agent, message_id):
                if expected_claim_epoch is not None:
                    active = self.read(paths['processing']) if paths['processing'].is_file() else None
                    if (type(expected_claim_epoch) is not int or active is None or
                            active.claim_epoch != expected_claim_epoch or active.to != agent):
                        raise BusError('原任务或认领状态已变更，请重新核对并确认')
                recovered = self._recover_claim_locked(agent, message_id, paths)
                self._sync_trigger_work_state(agent, message_id)
                return recovered
        finally:
            manager.release(lease)

    def _check_recovery_gates(self, msg: Message, *, attempt_after: int,
                              allow_disabled_origin: bool = False) -> None:
        """Source: Codex, 2026-10-06. Check the locked, validated claim.

        Only an exact matching recovery intent is an idempotent retry. The
        caller supplies its final attempt, never an exemption from an old log.
        Recheck immediately before state changes; no mtime or model call.
        """
        if os.path.lexists(self.root / 'STOP'):
            raise BusError('STOP 生效；禁止创建新的恢复代次')
        from .config import ConfigError, load
        from .skill_install import canonical_path
        path = self.root / 'config.json'
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError('config is too large')
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or not isinstance(raw.get('gates', {}), dict):
                raise ValueError('config must be an object')
            cfg = load(self.root.parent)
        except (OSError, ValueError, TypeError, AttributeError, ConfigError) as exc:
            raise BusError('项目配置或门禁无效；拒绝恢复任务') from exc
        if (msg.to not in cfg.executor_workers()
                and not (allow_disabled_origin and msg.to in cfg.data.get('workers', {}))):
            raise BusError('恢复成员未启用为当前项目执行者')
        max_retries = cfg.gates['max_retries']
        if type(attempt_after) is not int or attempt_after > max_retries:
            raise BusError(f'恢复后重试 {attempt_after} 次超过上限 {max_retries}')
        deadline = msg.deadline_ts
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline < 0):
            raise BusError('任务截止期限无效；拒绝恢复任务')
        if deadline and time.time() >= deadline:
            raise BusError('任务已超过原截止期限；拒绝恢复任务')
        if not msg.job_id:
            return
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', msg.job_id):
            raise BusError('恢复任务的作业 ID 无效')
        job_path = self.root / 'jobs' / (msg.job_id + '.json')
        root = canonical_path(self.root)
        resolved = canonical_path(job_path)
        if (not resolved.is_relative_to(root)
                or any(part.casefold() in ('.workbuddy', '不要访问', 'do not access')
                       for part in (*job_path.parts, *resolved.parts))):
            raise BusError('作业记录越出总线或进入私有目录')
        try:
            from .jobs import message_job, _same_intent
            job = message_job(cfg, msg)
            job_deadline = job['deadline_ts']
            if (type(job_deadline) not in (int, float) or not math.isfinite(job_deadline)
                    or job_deadline <= 0 or time.time() >= job_deadline):
                raise ValueError('job deadline expired or invalid')
            if job.get('finalization') or job.get('state') in ('completed', 'blocked', 'stopped'):
                raise ValueError('job is finalized or explicitly blocked')
            goal_data = job['goal_message']
            goal = Message.from_dict(goal_data, strict=True)
            goal.validate()
            if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', goal.id)
                    or goal.id != job['goal_id']
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', goal.to)):
                raise ValueError('invalid goal identity')
            expected = goal_data if msg.id == goal.id else next(
                item['message'] for item in job['tasks'] if item['id'] == msg.id)
            if not _same_intent(msg, expected):
                raise ValueError('claim differs from immutable job task')
            if allow_disabled_origin and (
                    msg.deadline_ts > expected.get('deadline_ts', msg.deadline_ts)
                    or msg.budget_s > expected.get('budget_s', msg.budget_s)):
                raise ValueError('handoff cannot extend original task deadline or budget')
            goal_paths = [self.agents_dir / goal.to / location / (goal.id + '.json')
                          for location in ('ready', 'processing', 'done', 'blocked')]
            for goal_path in goal_paths:
                resolved = canonical_path(goal_path)
                if (not resolved.is_relative_to(root)
                        or any(part.casefold() in ('.workbuddy', '不要访问', 'do not access')
                               for part in (*goal_path.parts, *resolved.parts))):
                    raise ValueError('unsafe goal path')
            active = [p for p in goal_paths[:2] if p.exists()]
            if any(p.exists() for p in goal_paths[2:]) or len(active) != 1:
                raise ValueError('goal is terminal, missing or conflicting')
            actual_goal = Message.from_dict(json.loads(active[0].read_text(encoding='utf-8')), strict=True)
            actual_goal.validate()
            if (actual_goal.status not in ('pending', 'claimed', 'running')
                    or not _same_intent(actual_goal, goal_data)):
                raise ValueError('goal intent or state is not recoverable')
        except (OSError, ValueError, KeyError, TypeError, AttributeError, BusError) as exc:
            raise BusError('作业记录、原期限或状态不允许恢复') from exc
        except StopIteration as exc:
            raise BusError('恢复任务不在其作业的原始分工中') from exc

    def _recover_claim_locked(self, agent: str, message_id: str,
                              paths: dict[str, Path]) -> Message:
        source, ready = paths['processing'], paths['ready']
        if paths['done'].exists():
            raise BusError('同 ID 已有 done 记录；拒绝覆盖或重做')
        if paths['blocked'].exists():
            if source.exists() and not ready.exists():
                return self._reconcile_rejected_supervision_copy(
                    agent, message_id, source, paths['blocked'])
            raise BusError('同 ID 已有 blocked 记录；拒绝覆盖或重做')
        if source.exists() and ready.exists():
            raise BusError('同 ID 已有 ready 冲突；两份证据均保留')
        authorized, completed = None, set()
        if self.events_path.is_file():
            with self.events_path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # A trailing incomplete audit line is not authorization.
                    if not isinstance(row, dict) or row.get('agent') != agent or row.get('id') != message_id:
                        continue
                    if row.get('event') == 'recovery_authorized':
                        authorized = row
                    elif row.get('event') == 'recovered' and isinstance(row.get('recovery_id'), str):
                        completed.add(row.get('recovery_id'))
        path = source if source.is_file() else ready if ready.is_file() else None
        if path is None:
            raise BusError('指定 processing 消息不存在；没有恢复任何任务')
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                raise ValueError('object required')
            msg = Message.from_dict(raw, strict=True)
            msg.validate()
            if (msg.id != message_id or msg.to != agent
                    or type(msg.attempt) is not int or msg.attempt < 0
                    or (msg.status not in ('pending', 'claimed', 'running')
                        and not (msg.status == 'awaiting_audit'
                                 and isinstance(msg.supervision, dict)
                                 and msg.supervision.get('state') in (
                                     'approved', 'decision_recorded')))):
                raise ValueError('identity or claim mismatch')
        except (OSError, ValueError, TypeError, BusError) as exc:
            raise BusError('消息损坏、身份不符或不是可恢复认领；原文件已保留') from exc
        previous_conversation_id = (msg.claim_conversation_id
                                    or (_fencing.conversation_id_for_claim(msg.claim_id)
                                        if msg.claim_id else ''))
        if (path == source and msg.status == 'awaiting_audit'
                and (msg.supervision or {}).get('state') == 'decision_recorded'):
            case = msg.supervision
            decision = case.get('decision')
            if (not isinstance(decision, dict)
                    or decision.get('decision') not in ('approve', 'reject')):
                raise BusError('已记录的审核结论无效；保留证据供人工处理')
            if (decision.get('decision') == 'approve'
                    and case.get('evidence', {}).get('reviewable') is not True):
                raise BusError('自动检查或产物核验未通过；拒绝恢复并保留证据')
            review_id = case.get('review_task_id')
            receipt_id = case.get('review_result_id')
            receipt_data = case.get('review_result')
            auditor = case.get('auditor')
            if (not isinstance(review_id, str) or not isinstance(receipt_id, str)
                    or not isinstance(receipt_data, dict) or not isinstance(auditor, str)):
                raise BusError('监督审核的正式批准记录不完整；拒绝恢复')
            review = self.find_message(review_id)
            receipt = Message.from_dict(receipt_data, strict=True)
            posted = self.find_result(review_id, sender=auditor, recipient=receipt.to)
            if (review is None or review.type != 'review' or review.to != auditor
                    or posted is None or posted.id != receipt_id
                    or posted.to_dict() != receipt.to_dict()
                    or (receipt.supervision or {}).get('kind') != 'audit_decision'
                    or (receipt.supervision or {}).get('case_id') != case.get('case_id')):
                raise BusError('独立批准回执与任务记录不一致；拒绝恢复')
            # Reconcile an already persisted and ticket-bound audit decision.
            # A rejection is finalized as rejected/blocked; only an approval
            # can complete the task as delivered. No new execution is started.
            self._complete_supervision_decision(agent, msg, review, receipt)
            return msg
        if (path == source and msg.status == 'awaiting_audit'
                and isinstance(msg.supervision, dict)
                and msg.supervision.get('state') == 'approved'):
            case = msg.supervision
            review_id = case.get('review_task_id')
            receipt_id = case.get('review_receipt_id')
            receipt_data = case.get('review_result')
            auditor = case.get('auditor')
            if (not isinstance(review_id, str) or not isinstance(receipt_id, str)
                    or not isinstance(receipt_data, dict) or not isinstance(auditor, str)):
                raise BusError('已批准审核的恢复记录不完整；保留原任务')
            review = self.find_message(review_id)
            receipt = Message.from_dict(receipt_data, strict=True)
            posted = self.find_result(review_id, sender=auditor, recipient=receipt.to)
            if (review is None or review.type != 'review' or review.to != auditor
                    or posted is None or posted.id != receipt_id
                    or posted.to_dict() != receipt.to_dict()
                    or (receipt.supervision or {}).get('kind') != 'audit_decision'
                    or (receipt.supervision or {}).get('case_id') != case.get('case_id')):
                raise BusError('已批准审核回执与活动任务不一致；拒绝恢复')
            # Recovery shares the ordinary approval path: it reacquires the
            # output-root artifact leases, rechecks the physical bytes, and
            # holds those leases through result publication and task archive.
            self._complete_supervision_decision(agent, msg, review, receipt)
            self.audit(event='result_commit_reconciled', agent=agent, id=msg.id,
                       result_id=(case.get('result') or {}).get('id'))
            return msg
        digest = lambda value: hashlib.sha256(json.dumps(
            value.to_dict(), ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        if path == source:
            receipt = self._valid_committed_result(msg)
            if (receipt is None and msg.status == 'awaiting_audit'
                    and isinstance(msg.supervision, dict)
                    and msg.supervision.get('state') == 'approved'):
                pending = msg.supervision.get('result')
                if not isinstance(pending, dict):
                    raise BusError('已批准监督记录缺少正式成果回执；保留原认领')
                receipt = Message.from_dict(pending, strict=True)
                self.post(receipt)
            if receipt is not None:
                # The persisted, ticket-bound receipt proves the commit. Recovery
                # finishes that commit without issuing another execution attempt.
                self._finish_receipt(agent, msg, receipt, _fencing.captured_ticket(msg))
                self.audit(event='result_commit_reconciled', agent=agent, id=msg.id, result_id=receipt.id)
                return msg
        current_hash = digest(msg)
        if authorized:
            required = ('before_sha256', 'after_sha256', 'recovery_id')
            if (any(not isinstance(authorized.get(k), str) for k in required)
                    or type(authorized.get('attempt_after')) is not int):
                raise BusError('恢复意图审计不完整；保留当前记录供核对')
        retry = bool(authorized and current_hash in (
            authorized['before_sha256'], authorized['after_sha256']))
        attempt_after = (msg.attempt if retry and current_hash == authorized['after_sha256']
                         else msg.attempt + 1)
        if retry and (authorized['attempt_after'] != attempt_after
                      or authorized.get('owner_confirmed_inactive') is not True
                      or authorized.get('confirmation_source') != 'caller_assertion'):
            raise BusError('恢复意图与实际重试代次或确认记录不一致；保留证据')
        if path == ready and not (retry and current_hash == authorized['after_sha256']):
            raise BusError('已有 ready 消息未匹配本次恢复意图；拒绝覆盖')
        if authorized and authorized['recovery_id'] not in completed and not retry:
            raise BusError('未完成恢复的消息已有变化；请重新核对原认领者，不自动重做')
        # An already published matching recovery only needs audit completion.
        # This creates no new ready copy or attempt, even under STOP/expiry.
        if path == ready:
            if authorized['recovery_id'] not in completed:
                self._recovery_audit(event='recovered', **{k: authorized[k] for k in (
                    'agent', 'id', 'recovery_id', 'attempt_after',
                    'owner_confirmed_inactive', 'confirmation_source')},
                    previous_conversation_id=authorized.get('previous_conversation_id', ''))
            self._release_recovered_task_lock(agent, message_id)
            return msg
        self._check_recovery_gates(msg, attempt_after=attempt_after)
        if not retry:
            before_hash, original_owner = current_hash, msg.claimed_by
            msg.status, msg.claimed_by, msg.claimed_at = 'pending', '', ''
            if _fencing.record_epoch(msg) > 0:
                _fencing.revoke(msg)
            msg.attempt += 1
            authorized = {'agent': agent, 'id': message_id, 'recovery_id': uuid.uuid4().hex,
                          'before_sha256': before_hash, 'after_sha256': digest(msg),
                          'attempt_after': msg.attempt,
                          'owner_confirmed_inactive': True, 'confirmation_source': 'caller_assertion',
                          'previous_conversation_id': previous_conversation_id}
            # Do not export arbitrary owner text from a damaged message.
            if original_owner == agent:
                authorized['previous_owner'] = agent
            self._recovery_audit(event='recovery_authorized', **authorized)
        elif current_hash == authorized['before_sha256']:
            msg.status, msg.claimed_by, msg.claimed_at = 'pending', '', ''
            if _fencing.record_epoch(msg) > 0:
                _fencing.revoke(msg)
            msg.attempt += 1
            if digest(msg) != authorized['after_sha256']:
                raise BusError('恢复意图与当前消息不一致；原文件已保留')
        if path == source:
            try:
                current = Message.from_dict(json.loads(source.read_text(encoding='utf-8')), strict=True)
                current.validate()
            except (OSError, ValueError, TypeError, BusError) as exc:
                raise BusError('恢复期间原认领记录已损坏；已停止并保留证据') from exc
            if digest(current) != current_hash:
                raise BusError('恢复期间原认领记录已变化；已停止，不覆盖其他写入')
            self._check_recovery_gates(current, attempt_after=attempt_after)
            if current_hash != authorized['after_sha256']:
                self._write(source, msg)
            self._check_recovery_gates(msg, attempt_after=attempt_after)
            # Windows rename fails if ready was created concurrently. Never
            # overwrite a ready copy; leave the updated processing for retry.
            os.rename(source, ready)
            from .heartbeat import clear
            clear(self.root, message_id, agent)
        if authorized['recovery_id'] not in completed:
            self._recovery_audit(event='recovered', **{k: authorized[k] for k in (
                'agent', 'id', 'recovery_id', 'attempt_after',
                'owner_confirmed_inactive', 'confirmation_source')},
                previous_conversation_id=authorized.get('previous_conversation_id', ''))
        self._release_recovered_task_lock(agent, message_id)
        return msg

    def _reconcile_rejected_supervision_copy(self, agent: str, message_id: str,
                                             processing: Path,
                                             blocked: Path) -> Message:
        """Finish an interrupted rejected-audit transition without requeueing.

        The caller must use recover_claim's explicit inactive-owner confirmation.
        Both copies are preserved unless the source binds to the same persisted,
        posted auditor rejection as the blocked terminal record.
        """
        try:
            source = self.read(processing)
            terminal = self.read(blocked)
        except (OSError, ValueError, BusError) as exc:
            raise BusError('拒绝审核双副本无法读取；保留原证据') from exc
        case = source.supervision if isinstance(source.supervision, dict) else {}
        decision = case.get('decision')
        if (source.id != message_id or source.to != agent
                or source.status != 'awaiting_audit'
                or case.get('state') != 'decision_recorded'
                or not isinstance(decision, dict)
                or decision.get('decision') != 'reject'
                or terminal.id != message_id or terminal.to != agent
                or terminal.status != 'rejected'
                or not isinstance(terminal.supervision, dict)
                or terminal.supervision.get('state') != 'rejected'
                or terminal.supervision.get('case_id') != case.get('case_id')):
            raise BusError('blocked/ 与 processing/ 不是同一条待恢复的拒绝审核；保留两份证据')
        review_id = case.get('review_task_id')
        receipt_data = case.get('review_result')
        auditor = case.get('auditor')
        if (not isinstance(review_id, str) or not isinstance(receipt_data, dict)
                or not isinstance(auditor, str)):
            raise BusError('拒绝审核回执绑定缺失；保留两份证据')
        review = self.find_message(review_id)
        try:
            receipt = Message.from_dict(receipt_data, strict=True)
            receipt.validate()
        except (ValueError, TypeError, BusError) as exc:
            raise BusError('拒绝审核回执损坏；保留两份证据') from exc
        if (review is None or review.type != 'review' or review.to != auditor
                or receipt.id != case.get('review_result_id')
                or receipt.reply_to != review_id or receipt.sender != auditor):
            raise BusError('拒绝审核回执与审计任务不匹配；保留两份证据')
        self._complete_supervision_decision(agent, source, review, receipt)
        return self.read(blocked)

    def _release_recovered_task_lock(self, agent: str, message_id: str) -> None:
        """Release only the inactive, confirmed predecessor's physical lock.

        The message has already been validated and moved to ready by the
        confirmed recovery path. Releasing afterward keeps a failed publish
        from making still-processing work appear claimable; retrying a ready
        recovery also repairs a crash between publish and lock cleanup.
        """
        info = _exclusive_lock.get_task_lock_info(self.root, message_id)
        if info is None:
            return
        if info.get('owner_agent') != agent:
            raise BusError('恢复后的排他锁持有者与任务成员不符；保留记录供核对')
        try:
            _exclusive_lock.release_task_lock(self.root, message_id, agent)
        except _exclusive_lock.ExclusiveLockError as exc:
            raise BusError('恢复后无法释放旧认领的排他锁；请重试恢复并核对记录') from exc

    def _recovery_audit(self, **event: Any) -> None:
        # A caught audit write may leave a trailing partial line. Preserve it,
        # adding only a separator so the new recovery intent remains readable.
        if self.events_path.is_file() and self.events_path.stat().st_size:
            with self.events_path.open('rb') as stream:
                stream.seek(-1, os.SEEK_END)
                trailing = stream.read(1)
            if trailing != b'\n':
                with self.events_path.open('a', encoding='utf-8') as stream:
                    stream.write('\n')
        self.audit(**event)

    def reclaim_stale(self, agent: str, timeout_s: float, *,
                      owner_confirmed_inactive: bool = False) -> list[str]:
        """Recover aged work only after the caller confirms its owner is inactive.

        Source: Codex / WorkBuddy review, 2026-10-03. File age alone cannot
        establish owner death. This assertion is not automatic PID validation.
        Legacy library API only: production recovery uses recover_claim for
        one specified message. Automatic schedulers must not call this sweep.
        """
        if owner_confirmed_inactive is not True:
            raise BusError("拒绝回收：先确认原认领者已停止，再显式设置 "
                           "owner_confirmed_inactive=True；文件时间不代表任务已停止")
        d = self.dirs(agent)
        recovered = []
        now = time.time()
        for p in self.processing(agent):
            if now - p.stat().st_mtime > timeout_s:
                try:
                    msg = self.read(p)
                except BusError:
                    continue
                if msg.status in ("awaiting_audit", "rejected"):
                    continue
                msg.status = "pending"
                msg.claimed_by = ""
                msg.claimed_at = ""
                msg.attempt += 1
                tmp = d["tmp"] / p.name
                self._write(tmp, msg)
                os.replace(tmp, d["ready"] / p.name)
                p.unlink(missing_ok=True)
                recovered.append(msg.id)
                self.audit(event="reclaim", agent=agent, id=msg.id, attempt=msg.attempt,
                           owner_confirmed_inactive=True, confirmation_source="caller_assertion")
        return recovered

    # ---- 事件驱动等待 ----
    def watcher(self, agent: str) -> WinDirWatcher | None:
        if not IS_WINDOWS:
            return None
        try:
            return WinDirWatcher(self.dirs(agent)["ready"])
        except OSError:
            return None

    def wait_for_work(self, agents: Iterable[str], timeout_s: float,
                      watchers: dict[str, WinDirWatcher | None],
                      message_types: Iterable[str] | None = None) -> str | None:
        """阻塞等待任意一个 agent 的信箱有新消息，返回该 agent 名。"""
        deadline = time.monotonic() + timeout_s
        while True:
            for a in agents:                      # 先扫一遍，避免消息早到丢失
                if self.pending(a, message_types=message_types):
                    return a
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None
            slice_s = min(remain, 1.0)
            for a in agents:
                w = watchers.get(a)
                if w is not None:
                    w.wait(slice_s)
                else:
                    time.sleep(slice_s)

    def wait_for_change(self, agents: Iterable[str], timeout_s: float,
                        watchers: dict[str, WinDirWatcher | None]) -> str | None:
        """Block for an inbox directory event without spinning on old ready files.

        Unlike ``wait_for_work``, this deliberately does not treat a message
        already present in ``ready`` as a wake event. Watchers use it to report
        a backlog once and then sleep until an agent or publisher changes a file.
        On systems without native watchers, callers rescan after the timeout.
        """
        ordered = list(agents)
        if timeout_s <= 0 or not ordered:
            return None
        if any(watchers.get(agent) is None for agent in ordered):
            time.sleep(timeout_s)
            return None

        deadline = time.monotonic() + timeout_s
        for index, agent in enumerate(ordered):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            slice_s = remaining / (len(ordered) - index)
            watcher = watchers[agent]
            if watcher.wait(slice_s):
                return agent
        return None

    def close_watchers(self, watchers: dict[str, WinDirWatcher | None]) -> None:
        for w in watchers.values():
            if w is not None:
                w.close()

    # ---- 人类可读统计 ----
    def stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        if not self.agents_dir.exists():
            return out
        for base in sorted(self.agents_dir.iterdir()):
            if not base.is_dir():
                continue
            out[base.name] = {
                s: len(list((base / s).glob("*.json")))
                for s in ("ready", "processing", "blocked", "done")}
        return out

    def escalations(self) -> list[Message]:
        """Observe existing human notices without registering or cleaning mailboxes."""
        from .public_files import PublicFiles
        files = PublicFiles(self.root)
        ready = files.checked(f"agents/{HUMAN_INBOX}/ready")
        notices = []
        for path in sorted(ready.glob("*.json")):
            try:
                files.checked(path.relative_to(self.root))
                terminal = files.checked(f"agents/{HUMAN_INBOX}/done/{path.name}")
                if terminal.is_file():
                    try:
                        self.read(terminal)
                        continue
                    except (OSError, ValueError, BusError):
                        pass
                notices.append(self.read(path))
            except (OSError, ValueError, BusError):
                continue
        return notices
