#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus —— 多 Agent 互通调度器。

一句话用法：
    python -m agentbus gui                       # 图形启动器（推荐）
    python -m agentbus init  --workspace "D:\\示例空间\\协作 样例"
    python -m agentbus boss  --workspace "D:\\示例空间\\协作 样例" "把本地 SD 部署起来"

核心分工：
    director  主体，拆解目标 + 验收结果（必须能被无头拉起）
    workers   执行者。可无头拉起的直接 spawn；GUI 应用通过 MCP 自己领任务
    你        只下一条命令、只看结论与需要拍板的事
"""
from .bus import Bus, Message, BusError, WinDirWatcher, new_id, now_iso
from .config import Config, ConfigError, load as load_config, save as save_config, describe
from .gates import GateKeeper, Decision
from .adapters import (get_adapter, resolve_codex, codex_version, AdapterError, Result,
                       CliAdapter, CodexAdapter, HttpLLMAdapter, MockAdapter,
                       list_models, read_claude_endpoint)
from .detect import detect_all, render as render_detect, summary as detect_summary
from .director import Director, Summary, HUMAN_INBOX, DIRECTOR_INBOX
from .lease import Lease, LeaseError, LeaseManager

__version__ = "1.2.2"
__all__ = [
    "Bus", "Message", "BusError", "WinDirWatcher", "new_id", "now_iso",
    "Config", "ConfigError", "load_config", "save_config", "describe",
    "GateKeeper", "Decision",
    "get_adapter", "resolve_codex", "codex_version", "AdapterError", "Result",
    "CliAdapter", "CodexAdapter", "HttpLLMAdapter", "MockAdapter",
    "list_models", "read_claude_endpoint",
    "detect_all", "render_detect", "detect_summary",
    "Director", "Summary", "HUMAN_INBOX", "DIRECTOR_INBOX",
    "Lease", "LeaseError", "LeaseManager",
]
