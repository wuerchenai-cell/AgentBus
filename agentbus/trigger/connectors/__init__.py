# -*- coding: utf-8 -*-
"""Connectors package for desktop agents."""

from agentbus.trigger.connectors.base import BaseConnector
from agentbus.trigger.connectors.codex import CodexBridgeConnector
from agentbus.trigger.connectors.codearts import CodeArtsHookConnector
from agentbus.trigger.connectors.universal import UniversalWakeupConnector

__all__ = [
    "BaseConnector",
    "CodexBridgeConnector",
    "CodeArtsHookConnector",
    "UniversalWakeupConnector",
]
