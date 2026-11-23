# -*- coding: utf-8 -*-
"""service.py: 触发投递服务、发前核检、策略管理、繁忙 FIFO 队列与组长协调互斥。"""

from __future__ import annotations

import datetime
import enum
import json
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from functools import wraps
from typing import Any, Dict, List, Optional, Union

from agentbus.trigger.contracts import (
    Contact,
    PreflightResult,
    TriggerContractError,
    TriggerErrorCode,
    WakeRequest,
    normalize_project_key,
)
from agentbus.trigger.journal import DeliveryJournal, DeliveryStatus
from agentbus.operation_lock import task_lock
from agentbus.trigger.contracts import validate_member_id
from agentbus.trigger.connectors._signals import stopped


def _policy_mutation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with task_lock(self.workspace / '.agentbus', 'trigger-policy', 'all'):
            return method(self, *args, **kwargs)
    return wrapped


class CoordinatorRouteMode(str, enum.Enum):
    DESKTOP = "desktop"
    EXISTING_EXECUTOR = "existing_executor"


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
class ProjectTriggerPolicy:
    """项目触发投递安全策略。"""
    schema_version: int = 1
    stopped: bool = False
    coordinator_route: str = CoordinatorRouteMode.DESKTOP.value
    active_scope: str = "default_scope"
    delivery_enabled: bool = True
    max_retries_per_request: int = 3
    max_auto_hops: int = 6
    current_hop_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectTriggerPolicy":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class TriggerDeliveryService:
    """项目后台投递服务（含繁忙 FIFO 排队与自动出队）。"""

    def __init__(self, workspace: Union[str, Path]):
        self.workspace = Path(workspace)
        self.norm_project = normalize_project_key(self.workspace)
        self.policy_file = self.workspace / ".agentbus" / "triggers" / "policy.json"
        self.busy_dir = self.workspace / ".agentbus" / "triggers" / "busy"
        self.journal = DeliveryJournal(self.workspace)

    def load_policy(self) -> ProjectTriggerPolicy:
        if not self.policy_file.is_file():
            return ProjectTriggerPolicy()
        try:
            data = json.loads(self.policy_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Invalid policy object")
            policy = ProjectTriggerPolicy.from_dict(data)
            if type(policy.stopped) is not bool or type(policy.delivery_enabled) is not bool:
                raise ValueError("Invalid policy flags")
            return policy
        except (OSError, ValueError, KeyError, TypeError):
            return ProjectTriggerPolicy(stopped=True, delivery_enabled=False)

    def save_policy(self, policy: ProjectTriggerPolicy) -> None:
        _atomic_write_json(self.policy_file, policy.to_dict())

    @_policy_mutation
    def set_stopped(self, stopped: bool) -> None:
        policy = self.load_policy()
        policy.stopped = stopped
        if not stopped:
            policy.delivery_enabled = True
            policy.current_hop_count = 0
        self.save_policy(policy)

    def is_stopped(self) -> bool:
        return stopped(self.workspace)

    def get_coordinator_route(self) -> str:
        return self.load_policy().coordinator_route

    @_policy_mutation
    def set_coordinator_route(self, route: str) -> None:
        policy = self.load_policy()
        policy.coordinator_route = route
        self.save_policy(policy)

    def should_wake_desktop_coordinator(self) -> bool:
        """检查是否应唤醒桌面组长（与独立 Director 严格互斥防双派发）。"""
        policy = self.load_policy()
        if self.is_stopped():
            return False
        return policy.coordinator_route == CoordinatorRouteMode.DESKTOP.value

    def set_member_busy(self, member_id: str, busy: bool, *, reason: str = "active_turn") -> None:
        """标记或解除某成员会话繁忙状态（繁忙期间新唤醒请求自动进入 FIFO 队列）。"""
        validate_member_id(member_id)
        marker = self.busy_dir / f"{member_id}.json"
        if busy:
            preserved = {}
            if marker.is_file():
                from agentbus.trigger.connectors._signals import _read_slot
                current = _read_slot(marker)
                preserved = {key: current[key] for key in ('notification_source_ids',
                    'notification_binding_id', 'notification_binding_revision') if key in current}
            _atomic_write_json(marker, {
                **preserved,
                "member_id": member_id,
                "busy": True,
                "reason": reason,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
        else:
            if marker.is_file():
                marker.unlink()

    def is_member_busy(self, contact: Contact) -> bool:
        """探测目标成员会话是否处于 busy 状态（支持显式 busy 标记与连接器 probe）。"""
        processing = self.workspace / ".agentbus/agents" / contact.member_id / "processing"
        if processing.is_dir() and next(processing.glob("*.json"), None) is not None:
            return True
        signal = self.workspace / ".agentbus/triggers/wake-signals" / (contact.member_id + ".json")
        if signal.exists():
            return True
        member_marker = self.busy_dir / f"{contact.member_id}.json"
        inst_marker = self.busy_dir / f"{contact.installation_key}.json"
        if member_marker.is_file() or inst_marker.is_file():
            return True
        if self._notification_work_pending(contact.member_id):
            return True
        connector = self._get_connector(contact.connector_id)
        if connector.describe().declared_capabilities.get("can_detect_busy"):
            obs = connector.probe(self.workspace, contact.installation_key)
            if obs.busy == "busy":
                return True
        return False

    def _notification_work_pending(self, member):
        """Notification consumption alone does not finish its source work."""
        def unfinished(identity):
            from agentbus.trigger.connectors._signals import validate_source_id
            from agentbus.bus import Bus
            validate_source_id(identity)
            done = self.workspace / '.agentbus/agents' / member / 'done' / (identity + '.json')
            if not done.is_file():
                return True  # Missing source evidence is not proof of idle.
            record = Bus(self.workspace / '.agentbus').read(done)
            return record.id != identity or record.status not in ('done', 'failed')
        marker = self.busy_dir / (member + '.json')
        if marker.is_file():
            from agentbus.trigger.connectors._signals import _read_slot
            saved = _read_slot(marker)
            if 'notification_source_ids' in saved:
                ids = saved['notification_source_ids']
                if not isinstance(ids, list) or not ids:
                    return True  # Unknown work requires explicit idle evidence.
                for identity in ids:
                    if unfinished(identity):
                        return True
        for request in self.journal.list_by_status('submitted'):
            if request.member_id != member:
                continue
            state = self.journal.get_request_status(request.request_id)
            result = state.get('details', {}).get('result', {})
            if result.get('accepted_by_host') is not False or result.get('status') not in ('queued', 'notified'):
                continue
            for identity in request.source_message_ids:
                if unfinished(identity):
                    return True
        return False

    def list_member_queued_requests(self, member_id: str) -> List[WakeRequest]:
        """按创建时间 FIFO 顺序返回指定成员处于 QUEUED 状态的唤醒请求。"""
        queued = [
            req for req in self.journal.list_by_status(DeliveryStatus.QUEUED.value)
            if req.member_id == member_id
        ]
        queued.sort(key=lambda r: (r.created_at or "", r.request_id))
        return queued

    def preflight(self, contact: Contact, request: WakeRequest) -> PreflightResult:
        """投递前发前重检（原子核对 STOP、工作区、绑定版本与就绪状态）。"""
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        policy = self.load_policy()

        # 1. Check workspace matching
        if normalize_project_key(contact.project_key) != self.norm_project:
            return PreflightResult(
                project_key=self.norm_project,
                connector_id=contact.connector_id,
                connector_version=contact.connector_version,
                installation_key=contact.installation_key,
                observed_at=now_iso,
                status="not_ready",
                reason_code=TriggerErrorCode.PROJECT_MISMATCH.value,
                human_message="Contact project does not match active service project",
            )

        # 2. Check STOP policy
        if self.is_stopped():
            return PreflightResult(
                project_key=self.norm_project,
                connector_id=contact.connector_id,
                connector_version=contact.connector_version,
                installation_key=contact.installation_key,
                observed_at=now_iso,
                status="not_ready",
                reason_code=TriggerErrorCode.STOPPED.value,
                human_message="Delivery is currently stopped by project trigger policy",
            )

        # 3. Check contact binding revision drift
        if (contact.revision != request.binding_revision or contact.binding_id != request.binding_id
                or contact.member_id != request.member_id or contact.installation_key != request.installation_key
                or contact.connector_version != request.connector_version):
            return PreflightResult(
                project_key=self.norm_project,
                connector_id=contact.connector_id,
                connector_version=contact.connector_version,
                installation_key=contact.installation_key,
                observed_at=now_iso,
                status="conflict",
                reason_code=TriggerErrorCode.VERSION_CHANGED.value,
                human_message=f"Contact revision ({contact.revision}) differs from request revision ({request.binding_revision})",
            )

        # 4. Check contact status
        if contact.status != "bound":
            return PreflightResult(
                project_key=self.norm_project,
                connector_id=contact.connector_id,
                connector_version=contact.connector_version,
                installation_key=contact.installation_key,
                observed_at=now_iso,
                status="not_ready",
                reason_code=TriggerErrorCode.TARGET_MISSING.value,
                human_message=f"Contact status is {contact.status}, not bound",
            )

        return PreflightResult(
            project_key=self.norm_project,
            connector_id=contact.connector_id,
            connector_version=contact.connector_version,
            installation_key=contact.installation_key,
            observed_at=now_iso,
            status="ready",
            actual_native_id=contact.native_conversation_id,
            actual_workspace=self.norm_project,
            reason_code=None,
            human_message="Preflight checks passed, target ready for delivery",
        )

    def _get_connector(self, connector_id: str):
        from agentbus.trigger.connectors.acp import ACPBridgeConnector
        from agentbus.trigger.connectors.codex import CodexBridgeConnector
        from agentbus.trigger.connectors.codearts import CodeArtsHookConnector
        from agentbus.trigger.connectors.universal import UniversalWakeupConnector

        mapping = {
            "agentbus.acp-bridge": ACPBridgeConnector,
            "agentbus.codex-bridge": CodexBridgeConnector,
            "agentbus.codearts-hook": CodeArtsHookConnector,
            "agentbus.universal-wakeup": UniversalWakeupConnector,
        }
        cls = mapping.get(connector_id)
        if cls is None:
            raise TriggerContractError("Unknown connector", TriggerErrorCode.CONNECTOR_UNSUPPORTED)
        return cls()

    @_policy_mutation
    def record_hop(self) -> bool:
        """记录一次自动连续投递跳数；若超过 max_auto_hops 触发防死循环熔断。"""
        policy = self.load_policy()
        policy.current_hop_count += 1
        if policy.current_hop_count >= policy.max_auto_hops:
            policy.delivery_enabled = False
            self.save_policy(policy)
            return False
        self.save_policy(policy)
        return True

    @_policy_mutation
    def reset_circuit_breaker(self) -> None:
        """当任务树产生实质状态推进（认领、交卷、人工干预）时重置连续跳数计数器。"""
        policy = self.load_policy()
        if policy.current_hop_count != 0 or not policy.delivery_enabled:
            policy.current_hop_count = 0
            policy.delivery_enabled = True
            self.save_policy(policy)

    def trigger_for_member(self, member_id, task_id=None, *, count_hop=True):
        from agentbus.trigger.contacts import load_contact
        from agentbus.trigger.wake import make_member_wakeup
        validate_member_id(member_id)
        try:
            with task_lock(self.workspace / ".agentbus", "trigger-member", member_id):
                if self.is_stopped():
                    return {"status":"stopped"}
                contact = load_contact(self.workspace, member_id)
                if not contact or contact.status != "bound":
                    return {"status":"no_bound_contact", "member_id":member_id}
                if not task_id:
                    ready = self.workspace / ".agentbus/agents" / member_id / "ready"
                    files = sorted(ready.glob("*.json")) if ready.is_dir() else []
                    if not files:
                        return {"status":"no_ready_task"}
                    task_id = files[0].stem
                req = make_member_wakeup(self.workspace, member_id, task_id, contact)
                pf = self.preflight(contact, req)
                if pf.status != "ready":
                    return {"status":"preflight_failed", "reason":pf.reason_code}
                enqueued = self.journal.enqueue(req)
                if enqueued.is_duplicate and enqueued.status not in ("queued", "prepared"):
                    return {"status":"duplicate", "request_id":enqueued.request_id,
                            "submit_status":enqueued.status}
            result = self._drain_member(contact, count_hop=count_hop)
            if result.get('status') == 'queued':
                ordered = self.list_member_queued_requests(member_id)
                result.update(request_id=req.request_id, queue_position=next(
                    (i for i, item in enumerate(ordered, 1) if item.request_id == req.request_id), None))
            return result
        except Exception as exc:
            return self._record_error(member_id, exc)

    def _record_error(self, member_id, exc):
        # Keep structured error categories, never private prompt/error text.
        error = {"status":"error", "member_id":member_id,
                 "error_type":type(exc).__name__, "observed_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}
        _atomic_write_json(self.workspace / ".agentbus/triggers/errors" / (member_id + ".json"), error)
        return error

    def _drain_member(self, contact, *, count_hop=False):
        """CAS before external I/O; never hold the member lock over a host call."""
        member = contact.member_id
        with task_lock(self.workspace / ".agentbus", "trigger-member", member):
            from agentbus.trigger.contacts import load_contact
            from agentbus.trigger.connectors._signals import reconcile_contact
            contact = load_contact(self.workspace, member)
            if contact is None or contact.status != 'bound':
                return {'status': 'no_bound_contact'}
            if self.is_stopped():
                return {'status': 'stopped'}
            reconciliation = reconcile_contact(self.workspace, contact)
            if reconciliation.get('needs_confirmation'):
                return {'status': 'preflight_failed', 'reason': reconciliation['status']}
            queued = self.list_member_queued_requests(member)
            # A legacy direct notification can be consumed before the scanner
            # journals the ready card. Never wake it again after actual finish.
            from agentbus.bus import Bus
            from agentbus.trigger.connectors._signals import validate_source_id
            for request in list(queued):
                terminal = bool(request.source_message_ids)
                for identity in request.source_message_ids:
                    validate_source_id(identity)
                    done = self.workspace / '.agentbus/agents' / member / 'done' / (identity + '.json')
                    if not done.is_file() or Bus(self.workspace / '.agentbus').read(done).status not in ('done', 'failed'):
                        terminal = False
                        break
                if terminal:
                    self.journal.transition(request.request_id, 'cancelled', {'reason': 'sources_already_terminal', 'non_delivery_verified': True})
                    queued.remove(request)
            if not queued:
                return {"status":"idle"}
            if self.is_stopped():
                return {"status":"stopped"}
            if self.is_member_busy(contact):
                for i, req in enumerate(queued, 1):
                    self.journal.transition(req.request_id, "queued", {"queued_reason":"target_busy", "queue_position":i})
                return {"status":"queued", "queued":len(queued), "submit_status":"queued"}
            req = queued[0]
            pf = self.preflight(contact, req)
            if pf.status != "ready":
                return {"status":"preflight_failed", "reason":pf.reason_code}
            if count_hop and not self.record_hop():
                return {"status":"circuit_breaker_tripped"}
            if not self.journal.claim_submission(req.request_id):
                return {"status":"duplicate", "request_id":req.request_id}
            self.set_member_busy(member, True, reason="submission_active")
        try:
            sub = self._get_connector(contact.connector_id).submit(self.workspace, contact, req)
            target = "submitted" if sub.status in ("accepted", "queued", "notified") else (
                "uncertain" if sub.status == "uncertain" else "rejected")
            with task_lock(self.workspace / ".agentbus", "trigger-member", member):
                self.journal.transition(req.request_id, target, {"result":sub.to_dict()})
                processing = self.workspace / ".agentbus/agents" / member / "processing"
                active = processing.is_dir() and next(processing.glob("*.json"), None) is not None
                # SDK/ACP run and CodeArts prompt return after their native turn.
                # A local notification still occupies its slot until consumed.
                pending = target == "uncertain" or sub.status in ("queued", "notified") or active
                self.set_member_busy(member, pending, reason=("submission_uncertain" if target == "uncertain" else "delivery_pending"))
            return {"status":"ok", "request_id":req.request_id, "submit_status":sub.status,
                    "notified_task_id":next(iter(req.source_message_ids), None),
                    "accepted_by_host":sub.accepted_by_host}
        except Exception as exc:
            with task_lock(self.workspace / ".agentbus", "trigger-member", member):
                self.journal.transition(req.request_id, "uncertain", {"uncertain_reason":"CONNECTOR_EXCEPTION", "error_type":type(exc).__name__})
                self.set_member_busy(member, True, reason="submission_uncertain")
            return self._record_error(member, exc)

    def on_work_state_changed(self, member_id, *, drain=True):
        """Bus lifecycle hook: derive processing/submission, clear idle, drain once."""
        from agentbus.trigger.contacts import load_contact
        validate_member_id(member_id)
        with task_lock(self.workspace / ".agentbus", "trigger-member", member_id):
            processing = self.workspace / ".agentbus/agents" / member_id / "processing"
            busy = ((processing.is_dir() and next(processing.glob("*.json"), None) is not None)
                or any(r.member_id == member_id for status in ('submitting', 'uncertain')
                       for r in self.journal.list_by_status(status))
                or self._notification_work_pending(member_id))
            self.set_member_busy(member_id, busy, reason="processing_or_submission")
            if busy:
                return {"status":"busy"}
            contact = load_contact(self.workspace, member_id)
        if not drain:
            return {"status":"idle", "drain_skipped":True}
        return self._drain_member(contact) if contact and contact.status == "bound" else {"status":"no_bound_contact"}

    def trigger_for_coordinator(self, result_msg=None):
        from agentbus.trigger.contacts import load_contact
        from agentbus.trigger.wake import make_result_wakeup
        if not self.should_wake_desktop_coordinator():
            return {"status":"skipped", "reason":"coordinator_wake_disabled"}
        try:
            cfg_path = self.workspace / ".agentbus/config.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
            member = cfg.get("coordinator") or cfg.get("director") or "director"
            contact = load_contact(self.workspace, member)
            if not contact or contact.status != "bound":
                return {"status":"no_bound_contact", "member_id":member}
            if not result_msg or not getattr(result_msg, "id", None):
                return {"status":"missing_result"}
            with task_lock(self.workspace / ".agentbus", "trigger-member", member):
                req = make_result_wakeup(self.workspace, member, getattr(result_msg,"sender","worker"),
                    result_msg.id, getattr(result_msg,"reply_to","") or "", contact)
                pf = self.preflight(contact, req)
                if pf.status != "ready":
                    return {"status":"preflight_failed", "reason":pf.reason_code}
                enqueued = self.journal.enqueue(req)
                if enqueued.is_duplicate and enqueued.status not in ("queued", "prepared"):
                    return {"status":"duplicate", "request_id":req.request_id}
            return self._drain_member(contact, count_hop=True)
        except Exception as exc:
            return self._record_error("coordinator", exc)

    def scan_and_deliver_pending(self):
        from agentbus.trigger.contacts import load_contact
        from agentbus.trigger.wake import make_member_wakeup
        if self.is_stopped():
            return {"status":"stopped", "delivered":0, "scanned":0, "queued":0, "errors":[]}
        result = {"status":"ok", "delivered":0, "notified":0, "scanned":0, "queued":0, "errors":[]}
        contacts = []
        directory = self.workspace / '.agentbus/agents'
        for member_path in sorted(directory.iterdir()) if directory.is_dir() else []:
            if not member_path.is_dir() or member_path.is_symlink() or member_path.name.startswith('.'):
                continue
            try:
                card = load_contact(self.workspace, member_path.name)
                if card is not None and card.status == 'bound':
                    contacts.append(card)
            except (OSError, ValueError, TriggerContractError) as exc:
                result['errors'].append(self._record_error(member_path.name, exc))
        if not contacts:
            result['status'] = 'no_bound_contacts'
            return result
        for contact in contacts:
            member = contact.member_id
            try:
                with task_lock(self.workspace / ".agentbus", "trigger-member", member):
                    contact = load_contact(self.workspace, member)
                    if contact is None or contact.status != 'bound':
                        continue
                    from agentbus.trigger.connectors._signals import reconcile_contact
                    reconcile_contact(self.workspace, contact)
                    ready = self.workspace / ".agentbus/agents" / member / "ready"
                    for path in sorted(ready.glob("*.json")) if ready.is_dir() else []:
                        result["scanned"] += 1
                        req = make_member_wakeup(self.workspace, member, path.stem, contact)
                        if self.preflight(contact, req).status == "ready":
                            try:
                                self.journal.enqueue(req)
                            except TriggerContractError as exc:
                                result["errors"].append(self._record_error(member, exc))
                outcome = self._drain_member(contact)
                result["queued"] += len(self.list_member_queued_requests(member))
                if outcome.get("status") == "error":
                    result["errors"].append(outcome)
                if outcome.get("accepted_by_host"):
                    result["delivered"] += 1
                elif outcome.get("status") == "ok" and outcome.get("submit_status") in ("queued", "notified"):
                    result["notified"] += 1
            except Exception as exc:
                result["errors"].append(self._record_error(member, exc))
        # Formal Bus result delivery calls trigger_for_coordinator directly.
        # late-results is not a verified result inbox and must not be inferred.
        return result


def run_project_service(
    workspace: Union[str, Path],
    seconds: float = 0,
    once: bool = False,
    poll_interval: float = 2.0,
) -> None:
    """运行触发投递服务主循环（纯后台监控，零 LLM Token 损耗）。"""
    service = TriggerDeliveryService(workspace)
    service.journal.reconcile_on_startup()
    start_time = datetime.datetime.now(datetime.timezone.utc)

    import time
    while True:
        try:
            service.scan_and_deliver_pending()
        except Exception as exc:
            service._record_error("service", exc)

        if once:
            break

        if seconds > 0:
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - start_time).total_seconds()
            if elapsed >= seconds:
                break

        time.sleep(poll_interval)
