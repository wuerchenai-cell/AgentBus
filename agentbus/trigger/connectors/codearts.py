# -*- coding: utf-8 -*-
"""codearts.py: 华为 CodeArts Agent Hook 连接器实现。"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import time
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


class CodeArtsHookConnector(BaseConnector):
    """华为 CodeArts Agent Hook 连接器。"""

    def __init__(self):
        manifest_path = Path(__file__).parents[1] / "manifests" / "agentbus.codearts-hook.json"
        self._desc = load_manifest(manifest_path)
        primary_asset = Path(__file__).parents[2] / "host_assets" / "codearts" / "agentbus_hook.js"
        fallback_asset = Path(__file__).parents[1] / "host_assets" / "codearts" / "agentbus_hook.js"
        self._host_asset = primary_asset if primary_asset.is_file() else fallback_asset

    def describe(self) -> ConnectorDescriptor:
        return self._desc

    def install_hook(self, workspace: Union[str, Path]) -> Dict[str, Any]:
        """将固定 Hook 资源安装到项目 .codearts/hooks/agentbus.js。"""
        ws = Path(workspace)
        target_dir = ws / ".codearts" / "hooks"
        target_file = target_dir / "agentbus.js"
        if not self._host_asset.is_file():
            raise TriggerContractError('Packaged CodeArts hook resource is missing', TriggerErrorCode.INSTALL_RELOAD_REQUIRED)
        content = self._host_asset.read_bytes()
        if target_file.exists() and target_file.read_bytes() != content:
            raise TriggerContractError('Existing hook differs; preserve it and require explicit migration', TriggerErrorCode.WORKFLOW_CONFLICT)
        target_dir.mkdir(parents=True, exist_ok=True)
        if not target_file.exists():
            with target_file.open('xb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

        content = target_file.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()

        return {
            "installed": True,
            "target": str(target_file),
            "bytes": len(content),
            "sha256": sha256,
            "installed_at": _now_iso(),
        }

    def probe(self, workspace: Union[str, Path], installation_key: str) -> ReadinessObservation:
        ws = Path(workspace)
        norm_ws = normalize_project_key(ws)
        hook_file = ws / ".codearts" / "hooks" / "agentbus.js"

        if not hook_file.is_file():
            return ReadinessObservation(
                protocol_version="agentbus-trigger/1",
                operation_id=f"op_{uuid.uuid4().hex[:8]}",
                project_key=norm_ws,
                connector_id=self._desc.connector_id,
                connector_version=self._desc.connector_version,
                installation_key=installation_key,
                instance_id="",
                observed_at=_now_iso(),
                status="not_ready",
                host_version="",
                native_target=None,
                busy="unknown",
                capabilities=dict(self._desc.declared_capabilities),
                error_code=TriggerErrorCode.INSTALL_RELOAD_REQUIRED.value,
                human_message="CodeArts AgentBus Hook not installed in .codearts/hooks/agentbus.js",
            )

        evidence = ws / '.agentbus/triggers/bridge/codearts/evidence/READY.json'
        fresh = False
        data = {}
        try:
            if evidence.stat().st_size <= 16384:
                data = json.loads(evidence.read_text(encoding='utf-8'))
                stamp = datetime.datetime.fromisoformat(data.get('at', ''))
                age = (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
                fresh = (0 <= age <= 10 and normalize_project_key(data.get('workspace', '')) == norm_ws
                    and data.get('client_get') is True and data.get('client_prompt') is True
                    and hook_file.read_bytes() == self._host_asset.read_bytes())
        except (OSError, ValueError, TypeError):
            fresh = False
        return ReadinessObservation(
            protocol_version="agentbus-trigger/1",
            operation_id=f"op_{uuid.uuid4().hex[:8]}",
            project_key=norm_ws,
            connector_id=self._desc.connector_id,
            connector_version=self._desc.connector_version,
            installation_key=installation_key,
            instance_id=f"inst_codearts_{uuid.uuid4().hex[:8]}",
            observed_at=_now_iso(),
            status='ready' if fresh else 'not_ready',
            host_version=str(data.get('host_version') or ''),
            native_target="active_session",
            busy=('busy' if data.get('busy') else 'idle') if fresh else 'unknown',
            capabilities=dict(self._desc.declared_capabilities) if fresh else {},
            error_code=None if fresh else TriggerErrorCode.BOOTSTRAP_REQUIRED.value,
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
            installation_key="inst_codearts",
            instance_id=f"inst_codearts_{uuid.uuid4().hex[:8]}",
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
        ws = Path(workspace)
        norm_ws = normalize_project_key(ws)
        hook_file = ws / ".codearts" / "hooks" / "agentbus.js"

        if not hook_file.is_file():
            return PreflightResult(
                project_key=norm_ws,
                connector_id=self._desc.connector_id,
                connector_version=self._desc.connector_version,
                installation_key=contact.installation_key,
                observed_at=_now_iso(),
                status="not_ready",
                reason_code=TriggerErrorCode.INSTALL_RELOAD_REQUIRED.value,
                human_message="CodeArts hook is missing",
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

    def submit(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        request: WakeRequest,
        *,
        timeout_s: float = 30.0
    ) -> SubmitResult:
        ws = Path(workspace)
        norm_ws = normalize_project_key(ws)
        from agentbus.trigger.connectors._signals import stopped
        if stopped(ws):
            return SubmitResult(status='rejected', request_id=request.request_id,
                accepted_by_host=False, error_code=TriggerErrorCode.STOPPED.value)
        bridge_dir = ws / ".agentbus" / "triggers" / "bridge" / "codearts"
        requests_dir = bridge_dir / "requests"
        outcomes_dir = bridge_dir / "outcomes"
        requests_dir.mkdir(parents=True, exist_ok=True)
        outcomes_dir.mkdir(parents=True, exist_ok=True)

        req_payload = {
            "request_id": request.request_id,
            "native_conversation_id": contact.native_conversation_id,
            "prompt_summary": request.prompt_summary,
            "submitted_at": _now_iso(),
            "binding_id":contact.binding_id,
            "binding_revision":contact.revision,
        }
        req_file = requests_dir / f"{request.request_id}.json"
        from agentbus.operation_lock import task_lock
        from agentbus.trigger.journal import _atomic_write_json
        with task_lock(ws / '.agentbus', 'codearts-request', request.request_id):
            if not req_file.exists():
                _atomic_write_json(req_file, req_payload)
            else:
                original = json.loads(req_file.read_text(encoding='utf-8'))
                if (original.get('native_conversation_id') != contact.native_conversation_id
                        or original.get('prompt_summary') != request.prompt_summary):
                    raise TriggerContractError('Request identity already has different payload')

        # Wait for outcome with timeout
        outcome_file = outcomes_dir / f"{request.request_id}.json"
        start_time = time.time()
        while time.time() - start_time < timeout_s:
            if outcome_file.is_file():
                try:
                    outcome = json.loads(outcome_file.read_text(encoding="utf-8"))
                    if (outcome.get('request_id') != request.request_id
                            or outcome.get('session_id') != contact.native_conversation_id):
                        raise ValueError('Hook outcome identity mismatch')
                    st = outcome.get("status")
                    if st == "sdk_returned":
                        return SubmitResult(
                            protocol_version="agentbus-trigger/1",
                            operation_id=f"op_{uuid.uuid4().hex[:8]}",
                            project_key=norm_ws,
                            connector_id=self._desc.connector_id,
                            connector_version=self._desc.connector_version,
                            installation_key=contact.installation_key,
                            instance_id=f"inst_codearts_{uuid.uuid4().hex[:8]}",
                            observed_at=_now_iso(),
                            status="accepted",
                            request_id=request.request_id,
                            accepted_by_host=True,
                            native_message_id=outcome.get("assistant_id"),
                            native_turn_id=None,
                            queue_position=0,
                        )
                    elif st in ("rejected", "uncertain"):
                        return SubmitResult(
                            protocol_version="agentbus-trigger/1",
                            operation_id=f"op_{uuid.uuid4().hex[:8]}",
                            project_key=norm_ws,
                            connector_id=self._desc.connector_id,
                            connector_version=self._desc.connector_version,
                            installation_key=contact.installation_key,
                            instance_id=f"inst_codearts_{uuid.uuid4().hex[:8]}",
                            observed_at=_now_iso(),
                            status=st,
                            request_id=request.request_id,
                            accepted_by_host=False,
                            error_code=outcome.get("reason"),
                        )
                except (ValueError, KeyError, TypeError):
                    pass  # 容错探测类：事件捕获异常容错忽略
            if stopped(ws):
                break
            time.sleep(0.05)

        # Timeout reached: must be reported as UNCERTAIN, never accepted
        return SubmitResult(
            protocol_version="agentbus-trigger/1",
            operation_id=f"op_{uuid.uuid4().hex[:8]}",
            project_key=norm_ws,
            connector_id=self._desc.connector_id,
            connector_version=self._desc.connector_version,
            installation_key=contact.installation_key,
            instance_id="",
            observed_at=_now_iso(),
            status="uncertain",
            request_id=request.request_id,
            accepted_by_host=False,
            error_code=TriggerErrorCode.SEND_UNCERTAIN.value,
            human_message="Timeout waiting for CodeArts Hook execution confirmation",
        )

    def observe(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        native_ref: Optional[str] = None
    ) -> List[NativeEvent]:
        norm_ws = normalize_project_key(workspace)
        ws = Path(workspace)
        last_turn_file = ws / ".agentbus" / "triggers" / "bridge" / "codearts" / "evidence" / "LAST_TURN_END.json"
        kind = None
        if last_turn_file.is_file():
            try:
                turn_data = json.loads(last_turn_file.read_text(encoding="utf-8"))
                if (native_ref and turn_data.get('request_id') == native_ref
                        and turn_data.get("session_id") == contact.native_conversation_id):
                    kind = "completed"
            except (ValueError, KeyError, TypeError):
                pass  # 容错探测类：状态解析失败回退默认
        if kind is None:
            return []
        return [
            NativeEvent(
                event_id=f"ev_{uuid.uuid4().hex[:8]}",
                instance_id="inst_codearts",
                sequence=1,
                project_key=norm_ws,
                native_conversation_id=contact.native_conversation_id,
                request_id=None,
                native_message_id=native_ref,
                kind=kind,
                observed_at=_now_iso(),
                source="codearts_hook",
            )
        ]

    def reconcile(
        self,
        workspace: Union[str, Path],
        request: WakeRequest
    ) -> ReconcileResult:
        ws = Path(workspace)
        outcome_file = ws / ".agentbus" / "triggers" / "bridge" / "codearts" / "outcomes" / f"{request.request_id}.json"
        if outcome_file.is_file():
            try:
                data = json.loads(outcome_file.read_text(encoding="utf-8"))
                if data.get("status") == "sdk_returned":
                    return ReconcileResult(
                        request_id=request.request_id,
                        status="accepted",
                        evidence_type="hook_outcome_file",
                        observed_at=_now_iso(),
                        native_message_id=data.get("assistant_id"),
                    )
            except (ValueError, KeyError, TypeError):
                pass  # 容错探测类：状态对齐解析异常回退
        return ReconcileResult(
            request_id=request.request_id,
            status="unknown",
            evidence_type="none",
            observed_at=_now_iso(),
        )
