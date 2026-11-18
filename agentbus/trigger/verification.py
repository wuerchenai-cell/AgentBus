# -*- coding: utf-8 -*-
"""verification.py: 有界真实验证控制、随机算题生成与回报核验实现。"""

from __future__ import annotations

import datetime
import json
import os
import random
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Union

from agentbus.trigger.contracts import Contact, normalize_project_key
from agentbus.public_files import PublicFiles


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
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


@dataclass
class VerificationPlan:
    """验证计划（只读预览，不消耗模型回合）。"""
    plan_id: str
    mode: str = "member_only"  # member_only / roundtrip
    member_id: str = ""
    coordinator_id: Optional[str] = None
    max_submit_budget: int = 1
    sample_input_path: str = ""
    expected_output_path: str = ""
    expected_payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    binding_id: str = ""
    binding_revision: int = 0
    connector_id: str = ""
    installation_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VerificationPlan":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class VerificationRun:
    """执行中的验证记录。"""
    run_id: str
    plan_id: str
    status: str = "in_progress"  # in_progress / passed / failed / uncertain
    task_id: str = ""
    started_at: str = ""
    authorization_ref: str = ""
    task_message: Dict[str, Any] = field(default_factory=dict)
    output_root: str = ""
    dispatch_attempted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VerificationRun":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class VerificationReport:
    """验证完成报告。"""
    run_id: str
    plan_id: str
    member_id: str
    coordinator_id: Optional[str] = None
    status: str = "pending"
    member_delivered: bool = False
    artifact_verified: bool = False
    coordinator_processed: bool = False
    native_completed: bool = False
    actual_submits: int = 0
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VerificationReport":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def get_verify_dir(workspace: Union[str, Path]) -> Path:
    return PublicFiles(workspace).checked(".agentbus/triggers/verification")


def _identity(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"[0-9a-f]{12}", value):
        raise ValueError("Invalid verification identity")
    return value


def _stored_plan(workspace: Union[str, Path], plan_id: str) -> VerificationPlan:
    _identity(plan_id, "vplan_")
    data = PublicFiles(workspace).read_json(f".agentbus/triggers/verification/plans/{plan_id}.json")
    if data is None:
        raise ValueError("Verification plan not found")
    plan = VerificationPlan.from_dict(data)
    if (plan.plan_id != plan_id
            or plan.sample_input_path != f"inputs/verify_input_{plan_id}.json"
            or plan.expected_output_path != f"outputs/verify_output_{plan_id}.json"):
        raise ValueError("Verification plan paths or identity changed")
    PublicFiles(workspace).checked(plan.sample_input_path)
    return plan


def _actual_submits(public: PublicFiles, task_id: str) -> int:
    """Count recorded connector attempts for this source, never infer a call."""
    count = 0
    for path in public.list_files(".agentbus/triggers/requests"):
        if path.name.endswith(".state.json"):
            continue
        request = public.read_json(path.relative_to(public.root))
        if not request or task_id not in request.get("source_message_ids", []):
            continue
        request_id = request.get("request_id", "")
        if path.stem != request_id:
            raise ValueError("Verification request identity mismatch")
        raw = public.read_bytes(f".agentbus/triggers/journal/{request_id}/journal.jsonl",
                                limit=4 * 1024 * 1024)
        for line in (raw or b"").splitlines():
            row = json.loads(line)
            if row.get("request_id") != request_id:
                raise ValueError("Verification journal identity mismatch")
            if row.get("to_status") == "submitting":
                count += 1
    return count


def load_verification_run(workspace: Union[str, Path], run_id: str) -> Optional[VerificationRun]:
    """Bounded run lookup shared by the report reader and onboarding gate."""
    _identity(run_id, "vrun_")
    data = PublicFiles(workspace).read_json(f".agentbus/triggers/verification/runs/{run_id}.json")
    if data is None:
        return None
    run = VerificationRun.from_dict(data)
    if run.run_id != run_id:
        raise ValueError("Verification run identity mismatch")
    return run


def _contact_matches(public: PublicFiles, plan: VerificationPlan) -> bool:
    raw = public.read_json(f".agentbus/agents/{plan.member_id}/contact.json")
    if raw is None or not plan.binding_id:
        return False
    current = Contact.from_dict(raw)
    return (current.status == "bound" and current.member_id == plan.member_id
        and normalize_project_key(current.project_key) == normalize_project_key(public.root)
        and (current.binding_id, current.revision, current.connector_id, current.installation_key)
        == (plan.binding_id, plan.binding_revision, plan.connector_id, plan.installation_key))


def plan_verification(
    workspace: Union[str, Path],
    contact: Contact,
    mode: str = "member_only",
    coordinator_id: Optional[str] = None,
) -> VerificationPlan:
    """生成受控的算题验证计划（程序随机出题，严禁代写）。"""
    if mode not in ("member_only", "roundtrip"):
        raise ValueError("Unsupported verification mode")
    if normalize_project_key(contact.project_key) != normalize_project_key(workspace):
        raise ValueError("Verification contact belongs to another project")
    if contact.status != "bound":
        raise ValueError("Verification requires a bound contact")
    plan_id = f"vplan_{uuid.uuid4().hex[:12]}"
    max_budget = 1 if mode == "member_only" else 2

    a = random.randint(100, 999)
    b = random.randint(100, 999)
    nonce = f"nonce_{uuid.uuid4().hex[:8]}"

    input_rel = f"inputs/verify_input_{plan_id}.json"
    output_rel = f"outputs/verify_output_{plan_id}.json"

    plan = VerificationPlan(
        plan_id=plan_id,
        mode=mode,
        member_id=contact.member_id,
        coordinator_id=coordinator_id,
        max_submit_budget=max_budget,
        sample_input_path=input_rel,
        expected_output_path=output_rel,
        expected_payload={
            "a": a,
            "b": b,
            "nonce": nonce,
            "expected_sum": a + b,
        },
        created_at=_now_iso(),
        binding_id=contact.binding_id,
        binding_revision=contact.revision,
        connector_id=contact.connector_id,
        installation_key=contact.installation_key,
    )

    plans_dir = PublicFiles(workspace).checked(".agentbus/triggers/verification/plans")
    plans_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(PublicFiles(workspace).checked(
        f".agentbus/triggers/verification/plans/{plan_id}.json"), plan.to_dict())
    return plan


def start_verification(
    workspace: Union[str, Path],
    plan: VerificationPlan,
    authorization_ref: str = "",
) -> VerificationRun:
    """Publish one formal task; only its member may produce the answer.

    A plan has a single run. Repeated clicks/retries return that run without
    spending another submit budget, including after an uncertain dispatch.
    """
    from agentbus.bus import Bus, Message
    from agentbus.config import load
    from agentbus.operation_lock import task_lock
    from agentbus.trigger.service import TriggerDeliveryService

    public = PublicFiles(workspace)
    ws = public.root
    stored = _stored_plan(ws, plan.plan_id)
    if stored.to_dict() != plan.to_dict():
        raise ValueError("Client verification plan differs from the stored plan")
    cfg = load(ws)
    if stored.member_id not in cfg.enabled_workers():
        raise ValueError("Verification member is not enabled in this project")
    if not _contact_matches(public, stored):
        raise ValueError("Verification contact changed or is missing; create a new plan")
    output_public = PublicFiles(cfg.output_root)
    output_file = output_public.checked(stored.expected_output_path)
    input_file = public.checked(stored.sample_input_path)
    run_id = "vrun_" + stored.plan_id[len("vplan_"):]
    run_relative = f".agentbus/triggers/verification/runs/{run_id}.json"
    run_file = public.checked(run_relative)
    bus = Bus(ws / ".agentbus")
    with task_lock(bus.root, "verification", stored.plan_id):
        existing = public.read_json(run_relative)
        if existing is not None:
            return VerificationRun.from_dict(existing)
        if input_file.exists() or output_file.exists():
            raise ValueError("Verification challenge paths already exist; create a new plan")
        _atomic_write_json(input_file, {
            "nonce": stored.expected_payload["nonce"], "operation": "add",
            "a": stored.expected_payload["a"], "b": stored.expected_payload["b"],
            "target_output": str(output_file),
        })
        message = bus.make(cfg.director, stored.member_id, "task",
            title="AgentBus bounded verification",
            body=(f"一次有界验证。工作区：{ws}。读取输入：{input_file}。"
                  f"自行计算 a+b，把 JSON {{\"nonce\": 输入nonce, \"result\": 计算结果}}"
                  f"写入产出目录 {cfg.output_root} 下的 {stored.expected_output_path}。"
                  "必须通过正式 claim、heartbeat、answer 提交该文件的产物清单。"
                  "完成后结束本轮，不要扩展任务。"),
            expected_artifacts=[stored.expected_output_path], result_to=cfg.director,
            budget_s=300)
        run = VerificationRun(run_id=run_id, plan_id=stored.plan_id,
            task_id=message.id, started_at=_now_iso(), authorization_ref=authorization_ref,
            task_message=message.to_dict(), output_root=str(cfg.output_root))
        # Persist the task identity before publication. No answer is written here.
        _atomic_write_json(run_file, run.to_dict())
        bus.init()
        bus.post(Message.from_dict(run.task_message, strict=True))
        # Record the attempt before calling a connector. A crash never justifies
        # silently replaying an uncertain model turn.
        run.dispatch_attempted = True
        _atomic_write_json(public.checked(run_relative), run.to_dict())
        TriggerDeliveryService(ws).trigger_for_member(stored.member_id, message.id)
        return run


def get_verification(workspace: Union[str, Path], run_id: str) -> Optional[VerificationReport]:
    """Require a matching, fenced terminal task and its formal artifact receipt."""
    from agentbus.bus import Bus, Message
    from agentbus.config import load
    from agentbus.claim_fencing import record_epoch
    from agentbus.workspace_files import WorkspaceFiles

    public = PublicFiles(workspace)
    run = load_verification_run(workspace, run_id)
    if run is None:
        return None
    plan = _stored_plan(workspace, run.plan_id)
    cfg = load(workspace)
    output = PublicFiles(cfg.output_root)
    output.checked(plan.expected_output_path)
    report = VerificationReport(run_id=run_id, plan_id=plan.plan_id,
        member_id=plan.member_id, coordinator_id=plan.coordinator_id,
        actual_submits=_actual_submits(public, run.task_id),
        error_message="Pending formal member execution and delivery")
    if report.actual_submits > plan.max_submit_budget:
        report.status = "failed"
        report.error_message = "Recorded connector attempts exceeded the verification budget"
        return report
    if not _contact_matches(public, plan):
        report.error_message = "Contact binding changed or is missing; a new verification is required"
        return report
    if not run.task_message or run.output_root != str(cfg.output_root):
        return report  # Legacy fabricated runs and changed output roots cannot pass.
    original = Message.from_dict(run.task_message, strict=True)
    if original.id != run.task_id or original.to != plan.member_id:
        raise ValueError("Verification task identity mismatch")
    terminal = public.read_json(f".agentbus/agents/{plan.member_id}/done/{run.task_id}.json")
    if terminal is None:
        return report
    task = Message.from_dict(terminal, strict=True)
    immutable = ("id", "to", "sender", "type", "title", "body", "result_to", "expected_artifacts")
    if any(getattr(task, key) != getattr(original, key) for key in immutable):
        raise ValueError("Verification terminal task differs from the published challenge")
    if record_epoch(task) < 1 or task.status != "done" or task.exit_code not in (None, 0):
        report.status = "failed" if task.status == "failed" else "pending"
        return report
    # Validate each traversed mailbox path before the formal Bus reader.
    for state in ("ready", "processing", "blocked", "done"):
        public.list_files(f".agentbus/agents/{original.result_to}/{state}")
    receipt = Bus(public.root / ".agentbus")._valid_committed_result(task)
    if receipt is None or receipt.exit_code not in (None, 0):
        return report
    report.member_delivered = True
    files = WorkspaceFiles(cfg.output_root)
    if (not receipt.artifacts or files.verify_artifacts(receipt.artifacts)["verdict"] != "match"
            or plan.expected_output_path not in {item.get("path") for item in receipt.artifacts}):
        report.status = "failed"
        report.error_message = "Formal artifact manifest is missing or mismatched"
        return report
    answer = output.read_json(plan.expected_output_path)
    report.artifact_verified = bool(answer
        and answer.get("nonce") == plan.expected_payload["nonce"]
        and type(answer.get("result")) is int
        and answer.get("result") == plan.expected_payload["expected_sum"])
    if not report.artifact_verified:
        report.status = "failed"
        report.error_message = "Delivered challenge output is missing or incorrect"
    elif plan.mode == "member_only":
        report.status = "passed"
        report.error_message = None
    else:
        report.error_message = "Member delivered; coordinator processing has not been verified"
    # A file receipt does not establish native host completion.
    return report
