# -*- coding: utf-8 -*-
"""host_adapters: 多宿主实际桌面集成与无缝规则补丁矩阵。

来源：§10.5 AUDIT-CROSS-HOST-FIELD-OBSERVATION-LOG，2026-10-13。
为各大主流 AI IDE（Cursor, Windsurf, Trae, Codex CLI, WorkBuddy, Antigravity）
提供开箱即用的工作契约规则注入、MCP 配置生成与文件监听优化。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SUPPORTED_HOSTS = ("cursor", "windsurf", "trae", "codex", "all")

CURSOR_MDC_TEMPLATE = """---
description: AgentBus Local Multi-Agent Workspace Integration Rule
globs: *
alwaysApply: true
---

# AgentBus 宿主集成工作契约 (Cursor)

当前工作区已接入 AgentBus 本地多 Agent 协作工作台。

## 核心规约
1. 认领前核验：进入工作区后先阅读 `AGENTS.md`，执行 `agentbus inbox --mine --caller cursor` 检查就绪任务。
2. 先认领再动手：执行 `agentbus claim --workspace . --agent cursor --id <TASK_ID>`，禁止在未认领状态下改动共享文件。
3. 心跳保持：长任务每 30 秒执行一次 `agentbus heartbeat`，防止超时判定。
4. 交付与回执：任务完成后写入交付文件，计算 SHA256，生成独立回执并使用 `agentbus answer --body-file <回执> --expected-body-sha256 <哈希>` 交付。
5. 忽略控制态：严禁直接编辑 `.agentbus/leases/` 或 `.agentbus/operation-locks/` 下的锁文件。
"""

WINDSURF_RULES_TEMPLATE = """# AgentBus 宿主集成工作契约 (Windsurf Cascade)

当前工作区由 AgentBus 进行多 Agent 任务与状态编排。

## 协作生命周期
- 任务发现: 运行 `agentbus inbox --mine --caller windsurf`
- 认领锁定: 运行 `agentbus claim --workspace . --agent windsurf --id <TASK_ID>`
- 过程心跳: 定期执行 `agentbus heartbeat --workspace . --agent windsurf --id <TASK_ID> --claim-epoch <EPOCH> --claim-id <CLAIM_ID>`
- 成果交付: 编写交付回执，运行 `agentbus answer --workspace . --agent windsurf --id <TASK_ID> --body-file <RECEIPT.md> --expected-body-sha256 <SHA256>`

## 文件边界
- 控制文件位于 `.agentbus/`，业务产出位于配置的 `output_root`。
- 修改共享文件前必须先申请租约：`agentbus lease acquire <RESOURCE>`。
"""

TRAE_RULES_TEMPLATE = """# AgentBus 宿主集成工作契约 (Trae)

当前工作区接入 AgentBus 平级多 Agent 协作系统。

## Trae 成员守则
1. 遵循 `AGENTS.md` 刚性契约，与 Antigravity、Codex 平级协作。
2. 认领任务：`agentbus claim --workspace . --agent trae --id <TASK_ID>`。
3. 过程打卡：`agentbus heartbeat` 维持活跃。
4. 结果回执：严格提供精确字节数与 SHA256 指纹，使用 `agentbus answer` 原样交付。
"""

CODEX_RULES_TEMPLATE = """# AgentBus 宿主集成工作契约 (Codex CLI)

当前工作区为 AgentBus 本地文件协作环境。

## 执行指令
1. 查受信箱: `agentbus inbox --mine --caller codex`
2. 认领任务: `agentbus claim --workspace . --agent codex --id <TASK_ID>`
3. 交付产物: `agentbus answer --workspace . --agent codex --id <TASK_ID> --body-file <FILE> --expected-body-sha256 <SHA256>`
"""

VSCODE_SETTINGS_TEMPLATE = {
    "files.watcherExclude": {
        "**/.agentbus/leases/**": True,
        "**/.agentbus/operation-locks/**": True,
        "**/.agentbus/heartbeats/**": True,
        "**/.build-venv/**": True,
        "**/node_modules/**": True,
    },
    "search.exclude": {
        "**/.agentbus/leases/**": True,
        "**/.agentbus/archive/**": True,
    },
}

MCP_CONFIG_TEMPLATE = {
    "mcpServers": {
        "agentbus": {
            "command": "python",
            "args": ["-m", "agentbus.cli", "mcp-server"],
            "env": {
                "PYTHONUTF8": "1",
            },
        }
    }
}


def get_host_configs(workspace: Path) -> dict[str, dict[str, str]]:
    """生成各宿主配置与规则文件的相对路径及内容。"""
    ws = Path(workspace)
    return {
        "cursor": {
            ".cursor/rules/agentbus.mdc": CURSOR_MDC_TEMPLATE.strip(),
        },
        "windsurf": {
            ".windsurfrules": WINDSURF_RULES_TEMPLATE.strip(),
        },
        "trae": {
            ".trae/rules.md": TRAE_RULES_TEMPLATE.strip(),
        },
        "codex": {
            ".agentbus/hosts/codex-instructions.md": CODEX_RULES_TEMPLATE.strip(),
        },
        "vscode": {
            ".vscode/settings.json": json.dumps(VSCODE_SETTINGS_TEMPLATE, ensure_ascii=False, indent=2),
        },
        "mcp": {
            ".agentbus/mcp/agentbus-mcp.json": json.dumps(MCP_CONFIG_TEMPLATE, ensure_ascii=False, indent=2),
        },
    }


def audit_host_compatibility(workspace: Path) -> dict[str, Any]:
    """诊断当前工作区对各宿主的适配健全度。"""
    ws = Path(workspace)
    configs = get_host_configs(ws)
    status: dict[str, Any] = {}

    for host, files in configs.items():
        host_files = {}
        for rel_path, _ in files.items():
            full = ws / rel_path
            host_files[rel_path] = {
                "exists": full.exists(),
                "size": full.stat().st_size if full.exists() else 0,
            }
        status[host] = {
            "configured": all(f["exists"] for f in host_files.values()),
            "files": host_files,
        }

    return status


def apply_host_patches(workspace: Path, hosts: list[str] | None = None) -> list[str]:
    """将指定宿主的规则与适配补丁安全写入工作区（幂等写入）。"""
    ws = Path(workspace)
    configs = get_host_configs(ws)
    target_hosts = set(hosts or ["all"])
    if "all" in target_hosts:
        target_hosts = set(configs.keys())

    written_files: list[str] = []
    for host, files in configs.items():
        if host not in target_hosts:
            continue
        for rel_path, content in files.items():
            full_path = ws / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            # 如果是 JSON 且已存在，尝试合并或安全覆写
            if rel_path.endswith(".json") and full_path.exists():
                try:
                    existing = json.loads(full_path.read_text(encoding="utf-8"))
                    new_data = json.loads(content)
                except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"Existing JSON host config {rel_path} is invalid; original file was preserved"
                    ) from exc
                if not isinstance(existing, dict) or not isinstance(new_data, dict):
                    raise ValueError(
                        f"Existing JSON host config {rel_path} must be an object; original file was preserved"
                    )
                existing.update(new_data)
                full_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                written_files.append(rel_path)
                continue
            full_path.write_text(content, encoding="utf-8")
            written_files.append(rel_path)

    return sorted(written_files)
