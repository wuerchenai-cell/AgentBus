# -*- coding: utf-8 -*-
"""acp.py: ACP (Agent Client Protocol) 标准协议通用触发连接器。

借鉴 OpenBot 统一 ACP 桥接思路，结合 AgentBus 本地文件总线与零轮询唤醒：
- 支持 OpenCode (`opencode acp`)、Gemini CLI (`gemini --acp`)、Cursor CLI (`agent acp`)、
  Cline CLI (`cline --acp`)、Qwen Code (`qwen --acp`)、Claude ACP 等标准 ACP 协议客户端；
- 优先通过 `agentbus.acp.ACPAdapter` 向已绑定的 `sessionId` 发送 `session/prompt`（支持 `session/load` 恢复）；
- 同时原子落盘 `.agentbus/triggers/wake-signals/<member_id>.json` 本地唤醒信号，确保交互终端内
  `agentbus wait` 也能零延迟唤醒。
"""

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
        suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, str(target_path))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


class ACPBridgeConnector(BaseConnector):
    """ACP 标准协议宿主桥连接器（支持 session/load + session/prompt 与本地信号双通道）。"""

    def __init__(self):
        manifest_path = Path(__file__).parents[1] / "manifests" / "agentbus.acp-bridge.json"
        self._desc = load_manifest(manifest_path)

    def describe(self) -> ConnectorDescriptor:
        return self._desc

    def probe(self, workspace: Union[str, Path], installation_key: str) -> ReadinessObservation:
        ws = Path(workspace)
        norm_ws = normalize_project_key(ws)
        busy_state = "idle"
        busy_marker = ws / ".agentbus" / "triggers" / "busy" / f"{installation_key}.json"
        if busy_marker.is_file():
            busy_state = "busy"

        return ReadinessObservation(
            protocol_version="agentbus-trigger/1",
            operation_id=f"op_{uuid.uuid4().hex[:8]}",
            project_key=norm_ws,
            connector_id=self._desc.connector_id,
            connector_version=self._desc.connector_version,
            installation_key=installation_key,
            instance_id=f"inst_acp_{uuid.uuid4().hex[:8]}",
            observed_at=_now_iso(),
            status="unknown",
            host_version="",
            native_target="acp_session",
            busy=busy_state,
            capabilities={},
        )

    def resolve_target(
        self,
        workspace: Union[str, Path],
        invite_id: str,
        candidate_id: str,
    ) -> TargetObservation:
        norm_ws = normalize_project_key(workspace)
        return TargetObservation(
            protocol_version="agentbus-trigger/1",
            operation_id=f"op_{uuid.uuid4().hex[:8]}",
            project_key=norm_ws,
            connector_id=self._desc.connector_id,
            connector_version=self._desc.connector_version,
            installation_key="inst_acp",
            instance_id=f"inst_acp_{uuid.uuid4().hex[:8]}",
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
        request: WakeRequest,
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
                human_message="Contact workspace does not match current workspace",
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
        try:
            cfg_file = ws / ".agentbus/config.json"
            cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
            worker = (cfg.get("workers") or {}).get(contact.member_id) or {}
            mode = str(worker.get("communication_mode") or worker.get("adapter") or "").lower()
            command = (worker.get("acp_command") or worker.get("command")) if mode == "acp" else None
            if command:
                if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
                    result.status, result.human_message = "rejected", "ACP command must be a nonempty argument list"
                    return result
                from agentbus.acp import ACPAdapter
                adapter = ACPAdapter(command, startup_timeout_s=min(15, max(2, timeout_s)),
                    session_record_path=ws / ".agentbus/sessions" / (contact.member_id + ".json"),
                    agent_name=contact.member_id)
                adapter.session_id = contact.native_conversation_id
                try:
                    native = adapter.run(request.prompt_summary, cwd=ws, timeout_s=int(max(5, timeout_s)))
                finally:
                    adapter.close()
                result.status = "accepted" if native.exit_code == 0 else "uncertain"
                result.accepted_by_host = native.exit_code == 0
                result.human_message = "ACP session/prompt returned" if result.accepted_by_host else "ACP dispatch failed; original session was not acknowledged"
                return result
        except (OSError, ValueError, TypeError, AttributeError, ImportError, RuntimeError) as exc:
            result.status, result.human_message = "uncertain", "ACP dispatch failed: " + type(exc).__name__
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
