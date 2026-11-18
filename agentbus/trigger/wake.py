# -*- coding: utf-8 -*-
"""wake.py: 短唤醒指令模板生成与规范请求构建。"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path
from typing import Optional, Union

from agentbus.trigger.contracts import (
    Contact,
    WakeRequest,
    derive_request_id,
    normalize_project_key,
)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def make_member_wakeup(
    workspace: Union[str, Path],
    member_id: str,
    task_id: str,
    contact: Contact,
    scope_id: str = "default_scope",
    deadline: Optional[str] = None,
) -> WakeRequest:
    """生成发送给成员的短任务通知（复用既有信箱与认领机制）。"""
    norm_project = normalize_project_key(workspace)
    prompt = (
        f"AgentBus：本项目有一条正式任务发给你。工作区为{norm_project}，成员为{member_id}，"
        f"任务ID为{task_id}。请读取该任务并按既有流程claim、heartbeat、执行和answer；"
        f"完成后结束本轮。不要自行扩大任务，不要等待或反复轮询。"
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    req_id = derive_request_id(
        project_key=norm_project,
        member_id=member_id,
        binding_id=contact.binding_id,
        binding_revision=contact.revision,
        purpose="task_ready",
        source_message_ids=[task_id],
        scope_id=scope_id,
    )

    return WakeRequest(
        schema_version=1,
        request_id=req_id,
        project_key=norm_project,
        member_id=member_id,
        binding_id=contact.binding_id,
        binding_revision=contact.revision,
        purpose="task_ready",
        source_message_ids=[task_id],
        scope_id=scope_id,
        created_at=_now_iso(),
        deadline=deadline,
        prompt_summary=prompt,
        prompt_utf8_sha256=prompt_sha256,
        execution_surface=contact.execution_surface,
        installation_key=contact.installation_key,
        connector_version=contact.connector_version,
    )


def make_result_wakeup(
    workspace: Union[str, Path],
    coordinator_id: str,
    member_id: str,
    result_id: str,
    reply_to_task_id: str,
    contact: Contact,
    scope_id: str = "default_scope",
    deadline: Optional[str] = None,
) -> WakeRequest:
    """生成发送给组长的正式回执短通知（复用正式回执校验与续派）。"""
    norm_project = normalize_project_key(workspace)
    prompt = (
        f"AgentBus：本项目收到成员{member_id}的正式回执，result_id为{result_id}，"
        f"reply_to为{reply_to_task_id}。请读取并核验结果，按现有计划处理验收、互审、"
        f"下一步派工或最终汇总；没有待办则结束本轮。"
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    req_id = derive_request_id(
        project_key=norm_project,
        member_id=coordinator_id,
        binding_id=contact.binding_id,
        binding_revision=contact.revision,
        purpose="result_ready",
        source_message_ids=[result_id],
        scope_id=scope_id,
    )

    return WakeRequest(
        schema_version=1,
        request_id=req_id,
        project_key=norm_project,
        member_id=coordinator_id,
        binding_id=contact.binding_id,
        binding_revision=contact.revision,
        purpose="result_ready",
        source_message_ids=[result_id],
        scope_id=scope_id,
        created_at=_now_iso(),
        deadline=deadline,
        prompt_summary=prompt,
        prompt_utf8_sha256=prompt_sha256,
        execution_surface=contact.execution_surface,
        installation_key=contact.installation_key,
        connector_version=contact.connector_version,
    )
