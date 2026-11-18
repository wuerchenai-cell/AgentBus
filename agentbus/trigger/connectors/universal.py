# -*- coding: utf-8 -*-
"""universal.py: 通用自适应唤醒连接器实现（CLI Wait 信号机制 + 桌面 Toast/剪贴板联动）。"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentbus.trigger.connectors.base import BaseConnector
from agentbus.trigger.contracts import (
    Contact,
    NativeEvent,
    PreflightResult,
    ReadinessObservation,
    ReconcileResult,
    SubmitResult,
    TargetObservation,
    TriggerContractError,
    TriggerErrorCode,
    WakeRequest,
    normalize_project_key,
)
from agentbus.trigger.registry import ConnectorDescriptor, load_manifest


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


class UniversalWakeupConnector(BaseConnector):
    """通用自适应唤醒连接器（支持任意客户端通过 agentbus wait 或桌面通知响应唤醒）。"""

    def __init__(self):
        manifest_path = Path(__file__).parents[1] / "manifests" / "agentbus.universal-wakeup.json"
        self._desc = load_manifest(manifest_path)

    def describe(self) -> ConnectorDescriptor:
        return self._desc

    def probe(self, workspace: Union[str, Path], installation_key: str) -> ReadinessObservation:
        norm_ws = normalize_project_key(workspace)
        return ReadinessObservation(
            protocol_version="agentbus-trigger/1",
            operation_id=f"op_{uuid.uuid4().hex[:8]}",
            project_key=norm_ws,
            connector_id=self._desc.connector_id,
            connector_version=self._desc.connector_version,
            installation_key=installation_key,
            instance_id=f"inst_universal_{uuid.uuid4().hex[:8]}",
            observed_at=_now_iso(),
            status="unknown",
            host_version="",
            native_target="signal_file_and_desktop",
            busy="unknown",
            capabilities={},
        )

    def resolve_target(
        self,
        workspace: Union[str, Path],
        invite_id: str,
        candidate_id: str
    ) -> TargetObservation:
        norm_ws = normalize_project_key(workspace)
        return TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id=f"op_{uuid.uuid4().hex[:8]}",
            project_key=norm_ws,
            connector_id=self._desc.connector_id,
            connector_version=self._desc.connector_version,
            installation_key="inst_universal",
            instance_id=f"inst_universal_{uuid.uuid4().hex[:8]}",
            observed_at=_now_iso(),
            status="unknown",
            native_conversation_id=candidate_id,
            identity_source="self_checkin",
            exists=False,
        )

    def preflight(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        request: WakeRequest
    ) -> PreflightResult:
        norm_ws = normalize_project_key(workspace)
        if normalize_project_key(contact.project_key) != norm_ws:
            return PreflightResult(
                project_key=norm_ws,
                connector_id=self._desc.connector_id,
                connector_version=self._desc.connector_version,
                installation_key=contact.installation_key,
                observed_at=_now_iso(),
                status="not_ready",
                reason_code=TriggerErrorCode.PROJECT_MISMATCH.value,
            )

        return PreflightResult(
            project_key=norm_ws,
            connector_id=self._desc.connector_id,
            connector_version=self._desc.connector_version,
            installation_key=contact.installation_key,
            observed_at=_now_iso(),
            status="ready",
            actual_native_id=contact.native_conversation_id,
            actual_workspace=norm_ws,
        )

    def submit(self, workspace, contact, request, *, timeout_s=30.0):
        from agentbus.trigger.connectors._signals import put_signal
        put_signal(workspace, contact, request)
        return SubmitResult(project_key=normalize_project_key(workspace),
            connector_id=self._desc.connector_id, installation_key=contact.installation_key,
            observed_at=_now_iso(), status="queued", request_id=request.request_id,
            accepted_by_host=False, human_message="Local wake signal queued; host acknowledgement pending")

    def observe(self, workspace, contact, native_ref=None):
        # Local signal presence or disappearance proves no native model event.
        return []

    def reconcile(self, workspace, request):
        return ReconcileResult(request_id=request.request_id, status="unknown",
            evidence_type="local_notification_only", observed_at=_now_iso())
