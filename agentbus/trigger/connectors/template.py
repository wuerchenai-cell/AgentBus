# -*- coding: utf-8 -*-
"""template.py: 新软件连接器最小开发者模板。

所有未验证能力默认 unknown，submit 默认明确返回 unsupported。
严禁模板一生成就具有伪造的发送能力。
"""

from __future__ import annotations

import datetime
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
from agentbus.trigger.registry import ConnectorDescriptor


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class NewAgentConnectorTemplate(BaseConnector):
    """新软件连接器参考模板。"""

    def __init__(self, connector_id: str = "agentbus.custom-connector"):
        self.connector_id = connector_id

    def describe(self) -> ConnectorDescriptor:
        return ConnectorDescriptor(
            schema_version=1,
            connector_id=self.connector_id,
            connector_version="0.1.0",
            protocol_version="agentbus-trigger/1",
            display_name="Custom New Agent Connector Template",
            products=[
                {
                    "product_id": "custom_agent",
                    "vendor": "ThirdParty",
                    "channels": ["stable"],
                    "os": ["windows"],
                }
            ],
            entry_kind="manual",
            execution_surface="desktop_existing",
            transport="file_channel",
            declared_capabilities={
                "can_identify_target": False,
                "can_check_project": False,
                "can_submit_existing": False,
                "can_route_exact_id": False,
                "can_detect_busy": False,
                "host_queue_verified": False,
                "atomic_target_submit": False,
                "can_report_native_start": False,
                "can_report_native_finish": False,
                "can_reconcile": False,
            },
        )

    def probe(self, workspace: Union[str, Path], installation_key: str) -> ReadinessObservation:
        norm_ws = normalize_project_key(workspace)
        return ReadinessObservation(
            protocol_version="agentbus-trigger/1",
            project_key=norm_ws,
            connector_id=self.connector_id,
            installation_key=installation_key,
            observed_at=_now_iso(),
            status="not_ready",
            busy="unknown",
            error_code=TriggerErrorCode.CONNECTOR_UNSUPPORTED.value,
            human_message="This custom connector is a developer template and has not implemented native probe",
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
            project_key=norm_ws,
            connector_id=self.connector_id,
            installation_key="inst_custom",
            observed_at=_now_iso(),
            status="not_ready",
            native_conversation_id=candidate_id,
            identity_source="prompt_response",
            exists=False,
            error_code=TriggerErrorCode.TARGET_UNKNOWN.value,
        )

    def preflight(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        request: WakeRequest
    ) -> PreflightResult:
        norm_ws = normalize_project_key(workspace)
        return PreflightResult(
            project_key=norm_ws,
            connector_id=self.connector_id,
            installation_key=contact.installation_key,
            observed_at=_now_iso(),
            status="unsupported",
            reason_code=TriggerErrorCode.CONNECTOR_UNSUPPORTED.value,
            human_message="Custom connector submit is not implemented",
        )

    def submit(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        request: WakeRequest,
        *,
        timeout_s: float = 30.0
    ) -> SubmitResult:
        norm_ws = normalize_project_key(workspace)
        # Template explicitly rejects submissions safely
        return SubmitResult(
            protocol_version="agentbus-trigger/1",
            project_key=norm_ws,
            connector_id=self.connector_id,
            installation_key=contact.installation_key,
            observed_at=_now_iso(),
            status="rejected",
            request_id=request.request_id,
            accepted_by_host=False,
            error_code=TriggerErrorCode.CONNECTOR_UNSUPPORTED.value,
            human_message="Developer template does not support active dispatch",
        )

    def observe(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        native_ref: Optional[str] = None
    ) -> List[NativeEvent]:
        return []

    def reconcile(
        self,
        workspace: Union[str, Path],
        request: WakeRequest
    ) -> ReconcileResult:
        return ReconcileResult(
            request_id=request.request_id,
            status="not_accepted",
            evidence_type="template_unsupported",
            observed_at=_now_iso(),
        )
