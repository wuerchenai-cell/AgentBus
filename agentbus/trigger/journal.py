# -*- coding: utf-8 -*-
"""journal.py: 投递日志、状态跃迁、去重幂等与崩溃不确定调和实现。"""

from __future__ import annotations

import datetime
import enum
import json
import os
import tempfile
from contextlib import suppress
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from functools import wraps
from typing import Any, Dict, List, Optional, Union

from agentbus.trigger.contracts import (
    TriggerContractError,
    TriggerErrorCode,
    WakeRequest,
)
from agentbus.operation_lock import task_lock


def _serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with task_lock(self.workspace / '.agentbus', 'trigger-journal', 'all'):
            return method(self, *args, **kwargs)
    return wrapped


class DeliveryStatus(str, enum.Enum):
    QUEUED = "queued"
    PREPARED = "prepared"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _atomic_write_json(target_path: Path, data: dict) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(target_path.parent),
        prefix=f".{target_path.stem}_",
        suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, str(target_path))
    finally:
        # Preserve the primary write error if Windows still holds the temp file.
        with suppress(OSError):
            os.remove(tmp_path)


@dataclass
class JournalEntry:
    entry_id: str = ""
    request_id: str = ""
    timestamp: str = ""
    from_status: Optional[str] = None
    to_status: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JournalEntry":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class EnqueueResult:
    request_id: str
    status: str
    is_duplicate: bool


class DeliveryJournal:
    """投递日志管理类。"""

    def __init__(self, workspace: Union[str, Path]):
        self.workspace = Path(workspace)
        self.requests_dir = self.workspace / ".agentbus" / "triggers" / "requests"
        self.journal_dir = self.workspace / ".agentbus" / "triggers" / "journal"
        self.source_index_file = self.journal_dir / "source_index.json"

        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def _load_source_index(self) -> Dict[str, str]:
        if not self.source_index_file.is_file():
            return {}
        try:
            data = json.loads(self.source_index_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k,v in data.items()):
                raise ValueError('Invalid source index')
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise TriggerContractError('Delivery source index is corrupt; delivery paused') from exc

    def _save_source_index(self, index: Dict[str, str]) -> None:
        _atomic_write_json(self.source_index_file, index)

    def _get_state_file(self, request_id: str) -> Path:
        return self.requests_dir / f"{request_id}.state.json"

    def _get_request_file(self, request_id: str) -> Path:
        return self.requests_dir / f"{request_id}.json"

    def _get_journal_file(self, request_id: str) -> Path:
        req_journal_dir = self.journal_dir / request_id
        req_journal_dir.mkdir(parents=True, exist_ok=True)
        return req_journal_dir / "journal.jsonl"

    @_serialized
    def enqueue(self, request: WakeRequest) -> EnqueueResult:
        """注册并排队唤醒请求（带来源反查防重检与不可变请求持久化）。"""
        source_index = self._load_source_index()

        # Check in-flight sources
        active_statuses = {
            DeliveryStatus.QUEUED.value,
            DeliveryStatus.PREPARED.value,
            DeliveryStatus.SUBMITTING.value,
            DeliveryStatus.SUBMITTED.value,
            DeliveryStatus.UNCERTAIN.value,
            'unknown',
        }
        for src_id in request.source_message_ids:
            active_req_id = source_index.get(src_id)
            if active_req_id and active_req_id != request.request_id:
                active_status = self.get_request_status(active_req_id).get("status")
                if active_status in active_statuses:
                    raise TriggerContractError(
                        f"Source message {src_id} is already in flight in request {active_req_id} (status: {active_status})",
                        TriggerErrorCode.SEND_UNCERTAIN
                    )

        req_file = self._get_request_file(request.request_id)
        state_file = self._get_state_file(request.request_id)

        if req_file.is_file() and state_file.is_file():
            # Already enqueued
            status_info = self.get_request_status(request.request_id)
            return EnqueueResult(
                request_id=request.request_id,
                status=status_info.get("status", DeliveryStatus.QUEUED.value),
                is_duplicate=True
            )

        # Reserve source identities before request publication. A crash may leave
        # an unknown reservation, which is held for reconciliation, never resent.
        for src_id in request.source_message_ids:
            source_index[src_id] = request.request_id
        self._save_source_index(source_index)

        # Write immutable request payload
        _atomic_write_json(req_file, request.to_dict())

        # Write initial state
        state_data = {
            "request_id": request.request_id,
            "status": DeliveryStatus.QUEUED.value,
            "updated_at": _now_iso(),
            "details": {},
        }
        _atomic_write_json(state_file, state_data)

        # Log transition in journal
        entry = JournalEntry(
            entry_id=f"j_{uuid.uuid4().hex[:12]}",
            request_id=request.request_id,
            timestamp=_now_iso(),
            from_status=None,
            to_status=DeliveryStatus.QUEUED.value,
            details={},
        )
        self._append_journal(entry)

        # Update source index
        for src_id in request.source_message_ids:
            source_index[src_id] = request.request_id
        self._save_source_index(source_index)

        return EnqueueResult(
            request_id=request.request_id,
            status=DeliveryStatus.QUEUED.value,
            is_duplicate=False
        )

    @_serialized
    def transition(self, request_id: str, to_status: str, details: Optional[Dict[str, Any]] = None) -> None:
        """执行状态跃迁并记录不可篡改日志。"""
        valid_statuses = {s.value for s in DeliveryStatus}
        if to_status not in valid_statuses:
            raise TriggerContractError(f"Invalid delivery status: {to_status}")

        state_file = self._get_state_file(request_id)
        current_state = self.get_request_status(request_id)
        from_status = current_state.get("status")

        merged_details = dict(current_state.get("details", {}))
        if details:
            merged_details.update(details)

        if from_status == to_status and merged_details == current_state.get('details', {}):
            return
        if to_status in ('queued', 'prepared') and from_status not in ('queued', 'prepared'):
            raise TriggerContractError('Submitted or uncertain requests cannot return to the automatic queue')
        if to_status == DeliveryStatus.SUBMITTING.value and from_status not in (
                DeliveryStatus.QUEUED.value, DeliveryStatus.PREPARED.value):
            raise TriggerContractError('Only an unclaimed queued request can start submission')

        new_state = {
            "request_id": request_id,
            "status": to_status,
            "updated_at": _now_iso(),
            "details": merged_details,
        }
        if "uncertain_reason" in merged_details:
            new_state["uncertain_reason"] = merged_details["uncertain_reason"]

        _atomic_write_json(state_file, new_state)

        entry = JournalEntry(
            entry_id=f"j_{uuid.uuid4().hex[:12]}",
            request_id=request_id,
            timestamp=_now_iso(),
            from_status=from_status,
            to_status=to_status,
            details=details or {},
        )
        self._append_journal(entry)

    @_serialized
    def claim_submission(self, request_id: str) -> bool:
        """Atomic compare-and-set, shared across service processes."""
        if self.get_request_status(request_id).get('status') not in ('queued', 'prepared'):
            return False
        self.transition(request_id, DeliveryStatus.SUBMITTING.value)
        return True

    def _append_journal(self, entry: JournalEntry) -> None:
        journal_file = self._get_journal_file(entry.request_id)
        line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        with open(journal_file, "a", encoding="utf-8") as f:
            f.write(line)

    def get_request_status(self, request_id: str) -> Dict[str, Any]:
        """获取请求当前状态。"""
        state_file = self._get_state_file(request_id)
        if not state_file.is_file():
            return {"request_id": request_id, "status": "unknown", "details": {}}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {"request_id": request_id, "status": "unknown", "details": {}}

    def reconcile_on_startup(self) -> Dict[str, Any]:
        """系统启动时崩溃调和（将残留 submitting 转为 uncertain，禁止盲重发）。"""
        uncertain_requests = []
        for state_file in self.requests_dir.glob("*.state.json"):
            req_id = state_file.name.replace(".state.json", "")
            try:
                req = WakeRequest.from_dict(json.loads(self._get_request_file(req_id).read_text(encoding='utf-8')))
                with task_lock(self.workspace / '.agentbus', 'trigger-member', req.member_id):
                    status_data = self.get_request_status(req_id)
                    if status_data.get("status") == DeliveryStatus.SUBMITTING.value:
                        self.transition(req_id, DeliveryStatus.UNCERTAIN.value,
                            details={"uncertain_reason": "CRASH_DURING_SUBMISSION"})
                        uncertain_requests.append(req_id)
            except (OSError, ValueError, TypeError, TimeoutError):
                continue

        return {
            "reconciled_count": len(uncertain_requests),
            "uncertain_requests": uncertain_requests,
        }

    def can_auto_retry(self, request_id: str) -> bool:
        """检查请求是否允许自动重试（uncertain 状态刚性禁止自动重发）。"""
        st = self.get_request_status(request_id).get("status")
        if st == DeliveryStatus.UNCERTAIN.value:
            return False
        if st == DeliveryStatus.REJECTED.value:
            # Only allowed if explicit non-delivery verified
            return self.get_request_status(request_id).get('details', {}).get('non_delivery_verified') is True
        return False

    def get_history(self, request_id: str) -> List[JournalEntry]:
        """读取请求的不可变审计日志。"""
        journal_file = self._get_journal_file(request_id)
        if not journal_file.is_file():
            return []
        entries = []
        with open(journal_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(JournalEntry.from_dict(json.loads(line)))
                except Exception:
                    continue
        return entries

    def list_by_status(self, status: str) -> List[WakeRequest]:
        """按状态筛选请求。"""
        results = []
        for state_file in self.requests_dir.glob("*.state.json"):
            req_id = state_file.name.replace(".state.json", "")
            st = self.get_request_status(req_id).get("status")
            if st == status:
                req_file = self._get_request_file(req_id)
                if req_file.is_file():
                    try:
                        data = json.loads(req_file.read_text(encoding="utf-8"))
                        results.append(WakeRequest.from_dict(data))
                    except Exception:
                        continue
        return results
