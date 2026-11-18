# -*- coding: utf-8 -*-
"""contracts.py: 触发协议、数据模型与契约安全校验核心实现。"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class TriggerErrorCode(str, enum.Enum):
    PRODUCT_UNRESOLVED = "PRODUCT_UNRESOLVED"
    CONNECTOR_UNSUPPORTED = "CONNECTOR_UNSUPPORTED"
    INSTALL_RELOAD_REQUIRED = "INSTALL_RELOAD_REQUIRED"
    WORKFLOW_CONFLICT = "WORKFLOW_CONFLICT"
    INVITE_EXPIRED = "INVITE_EXPIRED"
    TARGET_UNKNOWN = "TARGET_UNKNOWN"
    TARGET_MISSING = "TARGET_MISSING"
    TARGET_CHANGED = "TARGET_CHANGED"
    PROJECT_MISMATCH = "PROJECT_MISMATCH"
    BOOTSTRAP_REQUIRED = "BOOTSTRAP_REQUIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    HOST_DENIED = "HOST_DENIED"
    BUSY = "BUSY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    BUSY_UNKNOWN = "BUSY_UNKNOWN"
    SEND_UNCERTAIN = "SEND_UNCERTAIN"
    VERSION_CHANGED = "VERSION_CHANGED"
    STOPPED = "STOPPED"
    SCOPE_EXPIRED = "SCOPE_EXPIRED"
    INSTANCE_CONFLICT = "INSTANCE_CONFLICT"


class TriggerContractError(Exception):
    """触发协议与契约异常。"""
    def __init__(self, message: str, code: Optional[TriggerErrorCode] = None):
        self.code = code
        self.message = message
        prefix = f"[{code.value}] " if code else ""
        super().__init__(f"{prefix}{message}")


CREDENTIAL_FIELD_PATTERNS = {
    "api_key", "apikey", "secret", "token", "password", "authorization",
    "auth_token", "private_key", "credential", "credentials"
}


def normalize_project_key(raw_path: str | Path) -> str:
    """规范化项目唯一 Key（Windows 驱动器小写、正斜杠、去尾部斜杠、拒绝越界）。"""
    s = str(raw_path).strip()
    if not s:
        raise TriggerContractError("Project key cannot be empty", TriggerErrorCode.PROJECT_MISMATCH)

    # Check for invalid relative traversal
    if ".." in s.replace("\\", "/").split("/"):
        raise TriggerContractError("Project key must not contain relative traversal '..'", TriggerErrorCode.PROJECT_MISMATCH)

    p = Path(s)
    # Check absolute
    try:
        norm = p.resolve()
    except Exception as exc:
        raise TriggerContractError(f"Cannot resolve project path: {exc}", TriggerErrorCode.PROJECT_MISMATCH)

    norm_str = str(norm).replace("\\", "/")
    # Windows drive letter lowercasing: e.g. I:/ -> i:/
    if len(norm_str) >= 2 and norm_str[1] == ":":
        norm_str = norm_str[0].lower() + norm_str[1:]

    norm_str = norm_str.rstrip("/")
    return norm_str


def validate_member_id(member_id: str) -> bool:
    """校验 AgentBus 逻辑成员 ID（字母、数字、下划线、减号，1-64 字符）。"""
    if not isinstance(member_id, str):
        raise TriggerContractError("Member ID must be a string")
    m = member_id.strip()
    if not m:
        raise TriggerContractError("Member ID cannot be empty")
    if len(m) > 64:
        raise TriggerContractError("Member ID length exceeds 64 characters")
    if not re.match(r"^[a-zA-Z0-9_\-]+$", m):
        raise TriggerContractError(f"Invalid member ID format: {m}")
    return True


def validate_native_id(native_id: str) -> bool:
    """校验原生会话 ID（不透明字符串，不可含斜杠与控制字符，长度 1-256）。"""
    if not isinstance(native_id, str):
        raise TriggerContractError("Native conversation ID must be a string")
    s = native_id.strip()
    if not s:
        raise TriggerContractError("Native conversation ID cannot be empty", TriggerErrorCode.TARGET_UNKNOWN)
    if len(s) > 256:
        raise TriggerContractError("Native conversation ID exceeds 256 characters", TriggerErrorCode.TARGET_UNKNOWN)
    if any(c in s for c in ("/", "\\", "\x00", "\r", "\n", "\t")):
        raise TriggerContractError("Native conversation ID contains invalid path or control characters", TriggerErrorCode.TARGET_UNKNOWN)
    return True


def derive_request_id(
    project_key: str,
    member_id: str,
    binding_id: str,
    binding_revision: int,
    purpose: str,
    source_message_ids: List[str],
    scope_id: str,
) -> str:
    """基于确定性输入生成唯一的规范请求 ID。"""
    norm_project = normalize_project_key(project_key)
    sorted_sources = sorted(source_message_ids)
    canonical = (
        f"{norm_project}|{member_id}|{binding_id}|{binding_revision}|"
        f"{purpose}|{','.join(sorted_sources)}|{scope_id}"
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"req_{digest[:16]}"


@dataclass
class RegistrationInvite:
    """登记邀请结构。"""
    schema_version: int = 1
    invite_id: str = ""
    project_key: str = ""
    member_id: str = ""
    connector_id: str = ""
    installation_key: str = ""
    protocol_version: str = "agentbus-trigger/1"
    expected_revision: int = 0
    created_at: str = ""
    expires_at: str = ""
    consumed: bool = False
    consumed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RegistrationInvite":
        if not isinstance(data, dict):
            raise TriggerContractError("Invite data must be a dict")
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Contact:
    """成员项目联系卡。"""
    schema_version: int = 1
    project_key: str = ""
    member_id: str = ""
    binding_id: str = ""
    revision: int = 1
    status: str = "bound"  # bound / retired / pending
    connector_id: str = ""
    connector_version: str = "1.0.0"
    installation_key: str = ""
    execution_surface: str = "desktop_existing"
    native_conversation_id: str = ""
    native_title: Optional[str] = None
    identity_source: str = "host_query"
    bound_at: str = ""
    updated_at: str = ""
    capability_evidence_ref: Optional[str] = None
    reason_code: Optional[str] = None

    def __post_init__(self):
        validate_member_id(self.member_id)
        if self.native_conversation_id:
            validate_native_id(self.native_conversation_id)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Contact":
        if not isinstance(data, dict):
            raise TriggerContractError("Contact data must be a dict")

        # Security check: strictly exclude credentials
        for k in data.keys():
            k_lower = k.lower()
            if any(cred in k_lower for cred in CREDENTIAL_FIELD_PATTERNS):
                raise TriggerContractError(f"Security violation: Contact cannot store credentials ({k})")

        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


@dataclass
class WakeRequest:
    """唤醒请求结构。"""
    schema_version: int = 1
    request_id: str = ""
    project_key: str = ""
    member_id: str = ""
    binding_id: str = ""
    binding_revision: int = 1
    purpose: str = "task_ready"  # goal, task_ready, review_ready, result_ready, explicit_resume
    source_message_ids: List[str] = field(default_factory=list)
    scope_id: str = "default_scope"
    created_at: str = ""
    deadline: Optional[str] = None
    prompt_summary: str = ""
    prompt_utf8_sha256: str = ""
    execution_surface: str = "desktop_existing"
    installation_key: str = ""
    connector_version: str = "1.0.0"

    def __post_init__(self):
        if len(self.prompt_summary) > 1500:
            raise TriggerContractError(
                f"prompt_summary length ({len(self.prompt_summary)}) exceeds Unicode limit of 1500 code points"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WakeRequest":
        if not isinstance(data, dict):
            raise TriggerContractError("WakeRequest data must be a dict")
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


@dataclass
class TargetObservation:
    """原生会话观察记录。"""
    protocol_version: str = "agentbus-trigger/1"
    operation_id: str = ""
    project_key: str = ""
    connector_id: str = ""
    connector_version: str = "1.0.0"
    installation_key: str = ""
    instance_id: str = ""
    observed_at: str = ""
    status: str = "ready"
    native_conversation_id: str = ""
    identity_source: str = "host_query"
    exists: bool = True
    is_replay: bool = False
    workspace_ref: Optional[str] = None
    evidence_ref: Optional[str] = None
    error_code: Optional[str] = None
    human_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TargetObservation":
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**filtered)


@dataclass
class PreflightResult:
    """发前就绪核检结果。"""
    protocol_version: str = "agentbus-trigger/1"
    operation_id: str = ""
    project_key: str = ""
    connector_id: str = ""
    connector_version: str = "1.0.0"
    installation_key: str = ""
    instance_id: str = ""
    observed_at: str = ""
    status: str = "ready"  # ready / busy / not_ready / conflict / unsupported
    actual_native_id: Optional[str] = None
    actual_workspace: Optional[str] = None
    reason_code: Optional[str] = None
    human_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubmitResult:
    """消息投递结果。"""
    protocol_version: str = "agentbus-trigger/1"
    operation_id: str = ""
    project_key: str = ""
    connector_id: str = ""
    connector_version: str = "1.0.0"
    installation_key: str = ""
    instance_id: str = ""
    observed_at: str = ""
    status: str = "accepted"  # accepted / rejected / uncertain
    request_id: str = ""
    accepted_by_host: bool = False
    native_message_id: Optional[str] = None
    native_turn_id: Optional[str] = None
    queue_position: Optional[int] = None
    error_code: Optional[str] = None
    human_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReadinessObservation:
    """连接器运行探针观察。"""
    protocol_version: str = "agentbus-trigger/1"
    operation_id: str = ""
    project_key: str = ""
    connector_id: str = ""
    connector_version: str = "1.0.0"
    installation_key: str = ""
    instance_id: str = ""
    observed_at: str = ""
    status: str = "ready"  # ready / not_ready / unknown
    host_version: str = ""
    native_target: Optional[str] = None
    busy: str = "idle"  # idle / busy / approval / unknown
    ready_until: Optional[str] = None
    capabilities: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    human_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NativeEvent:
    """宿主原生事件记录。"""
    event_id: str = ""
    instance_id: str = ""
    sequence: int = 0
    project_key: str = ""
    native_conversation_id: str = ""
    request_id: Optional[str] = None
    native_message_id: Optional[str] = None
    native_turn_id: Optional[str] = None
    kind: str = "submitted"
    observed_at: str = ""
    source: str = "host_connector"
    is_replay: bool = False
    error_code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReconcileResult:
    """断线调和结果。"""
    request_id: str = ""
    status: str = "unknown"  # accepted / not_accepted / unknown
    evidence_type: str = "none"
    observed_at: str = ""
    native_message_id: Optional[str] = None
    human_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
