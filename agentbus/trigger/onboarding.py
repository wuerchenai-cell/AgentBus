# -*- coding: utf-8 -*-
"""onboarding.py: 新成员接入向导状态机、可恢复事务与未知软件评估包实现。"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from functools import wraps
from agentbus.operation_lock import task_lock

from agentbus.trigger.capabilities import CANONICAL_CAPABILITIES
from agentbus.trigger.contacts import confirm_contact, create_invite, load_contact
from agentbus.trigger.connectors.codearts import CodeArtsHookConnector
from agentbus.trigger.contracts import (
    TargetObservation,
    TriggerContractError,
    TriggerErrorCode,
    normalize_project_key,
    validate_member_id,
)


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
class FlowState:
    """向导流当前状态。"""
    flow_id: str
    project_key: str
    member_id: str
    step: str = "S0"  # S0..S6, COMPLETED
    revision: int = 1
    selected_product: Optional[str] = None
    selected_installation: Optional[str] = None
    connector_id: Optional[str] = None
    invite_id: Optional[str] = None
    install_transaction_id: Optional[str] = None
    verification_run_id: Optional[str] = None
    completed: bool = False
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FlowState":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def get_flows_dir(workspace: Union[str, Path]) -> Path:
    return Path(workspace) / ".agentbus" / "triggers" / "onboarding"




def _ensure_worker_onboarded(workspace: Union[str, Path], member_id: str) -> None:
    """确保成员已在 config.json 与 workflow.json 登记，若未登记则原子执行 auto_onboard_agent。"""
    ws = Path(workspace)
    cfg_file = ws / ".agentbus" / "config.json"
    if cfg_file.is_file():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
            workers = data.get("workers", {})
            if member_id not in workers:
                from agentbus.onboarding_pack import auto_onboard_agent
                auto_onboard_agent(ws, member_id)
        except Exception:
            pass  # 容错探测类：自动接入静默探测容错忽略

def start_flow(workspace: Union[str, Path], member_id: str) -> FlowState:
    """初始化接入向导流（S0：确认项目与身份）。"""
    norm_project = normalize_project_key(workspace)
    validate_member_id(member_id)
    _ensure_worker_onboarded(workspace, member_id)

    flow_id = f"flow_{uuid.uuid4().hex[:12]}"
    now_iso = _now_iso()

    state = FlowState(
        flow_id=flow_id,
        project_key=norm_project,
        member_id=member_id,
        step="S0",
        revision=1,
        completed=False,
        updated_at=now_iso,
    )

    flow_file = get_flows_dir(workspace) / f"{flow_id}.json"
    _atomic_write_json(flow_file, state.to_dict())
    return state


def resume_flow(workspace: Union[str, Path], flow_id: str) -> FlowState:
    """从真实文件恢复中断的向导流。"""
    flow_file = get_flows_dir(workspace) / f"{flow_id}.json"
    if not flow_file.is_file():
        raise TriggerContractError(f"Onboarding flow {flow_id} not found")
    data = json.loads(flow_file.read_text(encoding="utf-8"))
    return FlowState.from_dict(data)


def _locked_flow(method):
    @wraps(method)
    def wrapped(workspace, flow_id, *args, **kwargs):
        with task_lock(Path(workspace) / '.agentbus', 'trigger-onboarding', flow_id):
            return method(workspace, flow_id, *args, **kwargs)
    return wrapped


@_locked_flow
def advance_flow(
    workspace: Union[str, Path],
    flow_id: str,
    expected_revision: int,
    action: str,
    payload: Dict[str, Any],
) -> FlowState:
    """按规范单步推进向导流程（严格 CAS 版本控制与状态持久化）。"""
    ws = Path(workspace)
    state = resume_flow(ws, flow_id)

    if state.revision != expected_revision:
        raise TriggerContractError(
            f"CAS conflict: expected flow revision {expected_revision}, current revision is {state.revision}",
            TriggerErrorCode.VERSION_CHANGED
        )

    now_iso = _now_iso()

    required_step = {'confirm_identity':'S0', 'select_software':'S1', 'confirm_scope':'S2',
        'apply_install':'S3', 'bind_chat':'S4', 'complete_verification':'S5',
        'enable_collaboration':'S6'}
    if action in required_step and state.step != required_step[action]:
        raise TriggerContractError('Onboarding action is not valid at current step')
    if state.completed:
        raise TriggerContractError('Onboarding flow is already completed')

    if action == "confirm_identity":
        # S0 -> S1 (Select software)
        _ensure_worker_onboarded(workspace, state.member_id)
        state.step = "S1"

    elif action == "select_software":
        # S1 -> S2 (Select scope & mode)
        state.selected_product = payload.get("product_id")
        state.selected_installation = payload.get("installation_key")
        if not state.selected_product or not state.selected_installation:
            raise TriggerContractError('Product and installation are required')
        state.step = "S2"

    elif action == "confirm_scope":
        # S2 -> S3 (Install/activate connector)
        state.connector_id = payload.get("connector_id")
        if state.connector_id not in {'agentbus.codearts-hook', 'agentbus.codex-bridge', 'agentbus.acp-bridge', 'agentbus.universal-wakeup'}:
            raise TriggerContractError('Unsupported connector', TriggerErrorCode.CONNECTOR_UNSUPPORTED)
        from agentbus.trigger.registry import load_manifest
        manifest = Path(__file__).parent / 'manifests' / (str(state.connector_id) + '.json')
        if not manifest.is_file():
            raise TriggerContractError('Unsupported connector', TriggerErrorCode.CONNECTOR_UNSUPPORTED)
        descriptor = load_manifest(manifest)
        products = {p.get('product_id') for p in descriptor.products}
        if (state.selected_product not in products and '*' not in products
                and not (state.selected_product == 'other' and state.connector_id == 'agentbus.universal-wakeup')):
            raise TriggerContractError('Selected connector does not match selected product')
        state.step = "S3"

    elif action == "apply_install":
        # S3 -> S4 (Bind chat invitation)
        if state.connector_id == "agentbus.codearts-hook":
            connector = CodeArtsHookConnector()
            connector.install_hook(ws)

        existing = load_contact(ws, state.member_id)
        # Generate registration invite with current revision
        inv = create_invite(
            ws,
            member_id=state.member_id,
            connector_id=state.connector_id or "generic",
            installation_key=state.selected_installation or "inst_01",
            expected_revision=(existing.revision if existing is not None else 0),
        )
        state.invite_id = inv.invite_id
        state.step = "S4"

    elif action == "bind_chat":
        # S4 -> S5 (Verify)
        if not state.invite_id:
            raise TriggerContractError("Cannot bind chat without an active invite")

        existing = load_contact(ws, state.member_id)
        identity_src = payload.get("identity_source", "host_query")
        target_sess = payload.get("native_conversation_id")
        if not target_sess:
            raise TriggerContractError('A native conversation ID is required')

        if identity_src == "self_checkin" and existing is not None and existing.status == "bound":
            from agentbus.trigger.contracts import validate_native_id
            validate_native_id(target_sess)
            if (existing.native_conversation_id != target_sess
                    or existing.member_id != state.member_id
                    or existing.connector_id != state.connector_id
                    or existing.installation_key != state.selected_installation
                    or normalize_project_key(existing.project_key) != state.project_key):
                raise TriggerContractError('Check-in contact does not match this onboarding flow', TriggerErrorCode.TARGET_CHANGED)
            from agentbus.trigger.contacts import get_invite, get_invites_dir
            inv = get_invite(ws, state.invite_id)
            if inv is not None and not inv.consumed:
                inv.consumed = True
                inv.consumed_at = now_iso
                _atomic_write_json(get_invites_dir(ws) / f"{inv.invite_id}.json", inv.to_dict())
            state.step = "S5"
        else:
            obs = TargetObservation(
                protocol_version="agentbus-trigger/1",
                operation_id=f"op_{uuid.uuid4().hex[:8]}",
                project_key=state.project_key,
                connector_id=state.connector_id or "generic",
                installation_key=state.selected_installation or "inst_01",
                instance_id=f"inst_{uuid.uuid4().hex[:8]}",
                observed_at=now_iso,
                status="ready",
                native_conversation_id=target_sess,
                identity_source=identity_src,
                exists=True,
            )
            confirm_contact(ws, state.invite_id, obs)
            state.step = "S5"

    elif action == "complete_verification":
        # S5 -> S6 (Enable collaboration)
        from agentbus.trigger.verification import get_verification, load_verification_run
        run_id = payload.get('run_id')
        report = get_verification(ws, run_id) if isinstance(run_id, str) and run_id else None
        run = load_verification_run(ws, run_id) if report is not None else None
        try:
            fresh = (run is not None and datetime.datetime.fromisoformat(run.started_at)
                     >= datetime.datetime.fromisoformat(state.updated_at))
        except (ValueError, TypeError):
            fresh = False
        if (report is None or report.status != 'passed' or report.member_id != state.member_id
                or not report.member_delivered or not report.artifact_verified or not fresh):
            raise TriggerContractError('Verification requires an actual matching passed report')
        state.verification_run_id = run_id
        state.step = "S6"

    elif action == "enable_collaboration":
        state.completed = True
        state.step = "COMPLETED"

    elif action == "choose_file_collaboration":
        state.completed = True
        state.step = "COMPLETED"

    else:
        raise TriggerContractError(f"Unsupported onboarding action: {action}")

    state.revision += 1
    state.updated_at = now_iso

    flow_file = get_flows_dir(ws) / f"{flow_id}.json"
    _atomic_write_json(flow_file, state.to_dict())
    return state


def generate_evaluation_pack(
    product_name: str,
    executable_path: str,
    detected_version: str,
) -> Dict[str, Any]:
    """为未知或无匹配连接器的软件生成标准接入评估包（只读生成，无模型调用，0 真实消息发送）。"""
    return {
        "schema_version": 1,
        "product_name": product_name,
        "executable_path": executable_path,
        "detected_version": detected_version,
        "generated_at": _now_iso(),
        "required_capabilities": CANONICAL_CAPABILITIES,
        "experimental_design": {
            "phase_1": "Read-only process and target identification query",
            "phase_2": "Isolated local challenge file verification",
            "phase_3": "Single bounded test turn with explicit timeout",
            "max_call_budget": 1,
            "timeout_seconds": 30,
        },
        "developer_guide_ref": "docs/guides/new-agent-connector.md",
        "automated_message_sent": 0,
    }
