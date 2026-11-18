"""Offline member recommendations, with no installation or launch metadata.

Source: product documentation and ACP Registry manifests, checked 2026-10-03.
Registry presence describes an upstream interface, not local acceptance.
"""
from __future__ import annotations

from copy import deepcopy


_CHECKED_AT = '2026-10-07'
_REGISTRY = 'https://github.com/agentclientprotocol/registry/blob/main/'

# This is an independently written evidence index. Do not import executable
# commands, environment variables or distributions from the upstream registry.
_ENTRIES = (
    {'id': 'codex', 'name': 'Codex', 'integration': 'codex-sdk',
     'source_url': 'https://github.com/openai/codex',
     'description': '官方 Codex 产品；AgentBus 独立 SDK 通道需单独验收，桌面对话与独立运行分别核验。'},
    {'id': 'workbuddy', 'name': 'WorkBuddy', 'integration': 'manual',
     'source_url': 'https://www.workbuddy.cn/',
     'description': 'WorkBuddy 桌面对话接手；不是 CodeBuddy Code CLI。独立 ACP 入口未验收，接入以项目验收为准。'},
    {'id': 'trae', 'name': 'Trae', 'integration': 'manual',
     'source_url': 'https://www.trae.cn/',
     'description': 'Trae 桌面对话接手；当前会话是否加载项目工作流需单独核验，目录不证明自动执行能力。'},
    {'id': 'zcode', 'name': 'ZCode', 'integration': 'manual',
     'source_url': 'https://github.com/zai-org/ZCode',
     'description': 'ZCode 桌面对话接手；不是 GLM 社区 ACP wrapper，独立会话入口需另行验收。'},
    {'id': 'opencode', 'name': 'OpenCode', 'integration': 'acp-native',
     'source_url': 'https://opencode.ai/docs/acp/',
     'description': '官方原生 ACP 接口；独立 ACP 与桌面聊天分别验收，目录不代替当前项目的运行证据。'},
    {'id': 'antigravity', 'name': 'Google Antigravity', 'integration': 'manual-gui',
     'source_url': 'https://antigravity.google/',
     'description': 'Google Antigravity 桌面对话接手；默认走文件路线，当前项目的真实任务、产物与回执闭环尚未验收。'},
    {'id': 'gemini', 'name': 'Gemini CLI', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'gemini/agent.json',
     'description': 'Google 官方 Gemini CLI 的原生 ACP 接口；需自行安装并由用户完成鉴权，本机接入未验收。'},
    {'id': 'github-copilot-cli', 'name': 'GitHub Copilot CLI', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'github-copilot-cli/agent.json',
     'description': 'GitHub 官方 CLI 的原生 ACP 接口；ACP 仍为公开预览，CLI 与编辑器内 Copilot 分别核验。'},
    {'id': 'cursor', 'name': 'Cursor CLI', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'cursor/agent.json',
     'description': 'Cursor 官方 CLI 的原生 ACP 接口；不代表已接入 Cursor 桌面聊天，登录及工具审批需用户处理。'},
    {'id': 'cline', 'name': 'Cline CLI', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'cline/agent.json',
     'description': 'Cline 项目自带的 CLI 原生 ACP 接口；CLI 与 IDE 扩展分别核验，需自行配置模型和鉴权。'},
    {'id': 'qwen-code', 'name': 'Qwen Code', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'qwen-code/agent.json',
     'description': 'Qwen 官方项目自带的原生 ACP 接口；需自行安装、登录或配置模型，本机接入未验收。'},
    {'id': 'kimi', 'name': 'Kimi CLI（旧版）', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'kimi/agent.json',
     'description': 'Registry 登记的旧 Kimi CLI 原生 ACP 接口；上游仓库已归档，替代产品 Kimi Code CLI 需重新核验。'},
    {'id': 'mistral-vibe', 'name': 'Mistral Vibe', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'mistral-vibe/agent.json',
     'description': 'Mistral 官方项目提供的原生 ACP 入口；需自行配置账户或模型，目录不代替本机和项目验收。'},
    {'id': 'factory-droid', 'name': 'Factory Droid CLI', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'factory-droid/agent.json',
     'description': 'Factory 官方 Droid CLI 的原生 ACP 接口；CLI、桌面和云会话分别核验，需由用户完成鉴权。'},
    {'id': 'auggie', 'name': 'Auggie CLI', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'auggie/agent.json',
     'description': 'Augment 官方 Auggie CLI 的原生 ACP 接口；需自行安装及鉴权，本机接入未验收。'},
    {'id': 'goose', 'name': 'goose CLI', 'integration': 'acp-native',
     'source_url': _REGISTRY + 'goose/agent.json',
     'description': 'goose 项目自带的 CLI 原生 ACP 服务；桌面与 CLI 分别核验，模型提供方及权限需自行配置。'},
    {'id': 'claude-acp', 'name': 'Claude Agent（ACP SDK 桥接）', 'integration': 'acp-bridge',
     'source_url': _REGISTRY + 'claude-acp/agent.json',
     'description': 'ACP 项目维护的 Claude Agent SDK 适配器，使用 Anthropic SDK；不是 Anthropic 原生 ACP CLI，本机未验收。'},
    {'id': 'pi-acp', 'name': 'pi ACP（社区桥接）', 'integration': 'acp-bridge',
     'source_url': _REGISTRY + 'pi-acp/agent.json',
     'description': 'svkozak 维护的社区 ACP 适配器，桥接 pi 的 RPC 接口；部分功能受限，不代表 pi 原生 ACP 或本机验收。'},
)


def catalog() -> list[dict]:
    """Return independent recommendation records without inspecting the host."""
    return deepcopy([dict(entry, checked_at=_CHECKED_AT, local_validation='not_checked')
                     for entry in _ENTRIES])


def get_entry(agent_id: str) -> dict:
    """Return a recommendation by its own identity, never by a desktop alias."""
    for entry in catalog():
        if entry['id'] == agent_id:
            return entry
    raise ValueError(f'推荐目录中不存在成员 {agent_id!r}')
