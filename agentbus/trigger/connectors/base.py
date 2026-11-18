# -*- coding: utf-8 -*-
"""base.py: 通用连接器抽象基类与接口契约。"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentbus.trigger.contracts import (
    Contact,
    NativeEvent,
    PreflightResult,
    ReadinessObservation,
    ReconcileResult,
    SubmitResult,
    TargetObservation,
    WakeRequest,
)
from agentbus.trigger.registry import ConnectorDescriptor


class BaseConnector(abc.ABC):
    """所有宿主连接器的通用抽象基类。"""

    @abc.abstractmethod
    def describe(self) -> ConnectorDescriptor:
        """返回连接器静态清单描述符。"""
        pass

    @abc.abstractmethod
    def probe(self, workspace: Union[str, Path], installation_key: str) -> ReadinessObservation:
        """探针核检当前实例与就绪状态（只读、无模型调用）。"""
        pass

    @abc.abstractmethod
    def resolve_target(
        self,
        workspace: Union[str, Path],
        invite_id: str,
        candidate_id: str
    ) -> TargetObservation:
        """核验目标聊天会话有效性。"""
        pass

    @abc.abstractmethod
    def preflight(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        request: WakeRequest
    ) -> PreflightResult:
        """提交前发前重检。"""
        pass

    @abc.abstractmethod
    def submit(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        request: WakeRequest,
        *,
        timeout_s: float = 30.0
    ) -> SubmitResult:
        """向目标聊天提交唤醒（唯一允许启动模型调用的方法）。"""
        pass

    @abc.abstractmethod
    def observe(
        self,
        workspace: Union[str, Path],
        contact: Contact,
        native_ref: Optional[str] = None
    ) -> List[NativeEvent]:
        """观察原生事件。"""
        pass

    @abc.abstractmethod
    def reconcile(
        self,
        workspace: Union[str, Path],
        request: WakeRequest
    ) -> ReconcileResult:
        """断线后核验调和（未知响应不得推导为未发送）。"""
        pass
