# -*- coding: utf-8 -*-
"""codex.py: OpenAI Codex 宿主工具桥连接器实现。"""

from __future__ import annotations

import datetime
import json
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


class CodexBridgeConnector(BaseConnector):
    """Codex 宿主工具桥连接器。"""

    def __init__(self):
        manifest_path = Path(__file__).parents[1] / "manifests" / "agentbus.codex-bridge.json"
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
            instance_id=f"inst_codex_{uuid.uuid4().hex[:8]}",
            observed_at=_now_iso(),
            status="unknown",
            host_version="",
            native_target="active_thread",
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
            installation_key="inst_codex",
            instance_id=f"inst_codex_{uuid.uuid4().hex[:8]}",
            observed_at=_now_iso(),
            status="unknown",
            native_conversation_id=candidate_id,
            identity_source="user_candidate",
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
        from agentbus.trigger.connectors._signals import put_signal, stopped
        ws = Path(workspace)
        result = SubmitResult(project_key=normalize_project_key(ws),
            connector_id=self._desc.connector_id, installation_key=contact.installation_key,
            observed_at=_now_iso(), status="queued", request_id=request.request_id,
            accepted_by_host=False)
        if stopped(ws):
            result.status, result.error_code = "rejected", TriggerErrorCode.STOPPED.value
            return result
        cfg_file = ws / ".agentbus/config.json"
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
            worker = (cfg.get("workers") or {}).get(contact.member_id) or {}
            mode = str(worker.get("communication_mode") or worker.get("adapter") or "").lower()
            if mode in ("sdk", "codex-sdk"):
                from agentbus.adapters import resolve_codex
                from agentbus.codex_sdk import CodexSDKAdapter
                binary = resolve_codex()
                if not binary or not Path(binary).is_file():
                    result.status, result.human_message = "rejected", "Codex executable unavailable"
                    return result
                adapter = CodexSDKAdapter(sandbox=str(worker.get("sandbox") or "workspace-write"),
                    model=worker.get("model"), codex_bin=binary,
                    session_record_path=ws / ".agentbus/sessions" / (contact.member_id + ".json"),
                    agent_name=contact.member_id)
                adapter.thread_id = contact.native_conversation_id
                try:
                    native = adapter.run(request.prompt_summary, cwd=ws, timeout_s=int(max(5, timeout_s)))
                finally:
                    adapter.close()
                result.status = "accepted" if native.exit_code == 0 else "uncertain"
                result.accepted_by_host = native.exit_code == 0
                result.human_message = "Codex SDK returned" if result.accepted_by_host else "Codex SDK returned failure; do not resend automatically"
                return result
        except (OSError, ValueError, TypeError, AttributeError, ImportError, RuntimeError) as exc:
            result.status, result.human_message = "uncertain", "Codex dispatch failed: " + type(exc).__name__
            return result
        put_signal(ws, contact, request)
        result.human_message = "Local wake signal queued; host acknowledgement pending"
        return result

    def observe(self, workspace, contact, native_ref=None):
        # Local signal presence or disappearance proves no native model event.
        return []

    def reconcile(self, workspace, request):
        return ReconcileResult(request_id=request.request_id, status="unknown",
            evidence_type="local_notification_only", observed_at=_now_iso())
