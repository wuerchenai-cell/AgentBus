# -*- coding: utf-8 -*-
"""AgentBus 零配置 3 分钟快速上手与向导模块 (AUDIT-ZERO-CONFIG-QUICKSTART-GUIDE)

面向新工程师与多 Agent 协作工作区：
1. 自动探测本机已安装的模型 CLI、ACP 接口与 GUI 宿主环境；
2. 零手动编辑生成最佳协同团队配置（Director + Workers）；
3. 自动化初始化文件总线、持续性记忆文档（AGENTS.md, TASKBOOK.md 等）；
4. 植入首发 0.1 启程任务树自检节点；
5. 在终端输出极简清晰的 3 步开箱即用行动指引。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import detect
from .bus import Bus
from .director import DIRECTOR_INBOX, HUMAN_INBOX
from .config import Config, load, save, describe


def run_quickstart(
    workspace: Path | str,
    output_root: Path | str | None = None,
    coordinator_override: str | None = None,
    print_output: bool = True,
) -> dict[str, Any]:
    """执行 3 分钟零配置极速初始化。"""
    ws = Path(workspace).expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)

    out_root = None
    if output_root:
        out_root = Path(output_root).expanduser()
        if not out_root.is_absolute():
            out_root = ws / out_root
        out_root = out_root.resolve()
        out_root.mkdir(parents=True, exist_ok=True)

    # 1. 自动探测本机环境
    probes = detect.detect_all()
    installed = {name: p for name, p in probes.items() if p.installed and not name.startswith("__")}

    # 2. 加载或初始化配置
    cfg = load(ws, create=True)
    cfg_data = dict(cfg.data)

    if out_root is not None:
        cfg_data["output_root"] = str(out_root)

    # 3. 合并探测到的工作者补丁
    workers_patch = detect.to_config_patch(probes)
    current_workers = cfg_data.setdefault("workers", {})
    for w_name, w_conf in workers_patch.items():
        if w_name not in current_workers:
            current_workers[w_name] = w_conf
        else:
            # 增量保留并同步已安装信息
            current_workers[w_name]["installed"] = True
            if "path" in w_conf and w_conf["path"]:
                current_workers[w_name]["path"] = w_conf["path"]

    # 4. 智能推选初始协调者 (Director)
    if coordinator_override and coordinator_override in current_workers:
        director = coordinator_override
    elif "codex" in installed:
        director = "codex"
    else:
        # 首选可以自动 spawn 的已安装成员，否则取当前 director
        spawnables = [name for name, p in installed.items() if p.can_spawn]
        director = spawnables[0] if spawnables else cfg.director

    cfg_data["director"] = director
    if director in current_workers:
        current_workers[director]["enabled"] = True
        current_workers[director]["executor_enabled"] = True

    # 确保当前常用 GUI/ACP 成员也默认启用
    for name in ("antigravity", "opencode", "trae", "workbuddy"):
        if name in installed and name in current_workers:
            current_workers[name]["enabled"] = True
            current_workers[name]["executor_enabled"] = True

    cfg_data.setdefault("assignment_mode", "autonomous")
    cfg = save(cfg, cfg_data)

    # 5. 初始化信箱总线
    bus = Bus(ws / ".agentbus")
    bus.init()
    bus.register(DIRECTOR_INBOX)
    bus.register(HUMAN_INBOX)
    for name in cfg.enabled_workers():
        bus.register(name)

    # 6. 生成核心契约与记忆文档（若不存在）
    initialized_at = time.strftime("%Y-%m-%d %H:%M:%S")
    agents_md = ws / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(
            "# AgentBus 项目协作契约\n\n"
            "来源：AgentBus 零配置快速向导生成；由工作区负责人持续维护。\n\n"
            "## 新成员先读\n"
            "1. `PROJECT.md`：项目任务书、最终目标、当前约束和待办。\n"
            "2. `MEMORY.md`：长期有效的架构决定、环境事实和协作约定。\n"
            "3. `AGENTBUS.md`：总线、回执、租约和恢复规则。\n\n"
            "## 默认文件协作入口\n"
            "查看待办：`agentbus inbox --mine --caller <成员>`\n"
            "认领待办：`agentbus claim --agent <成员> --id <消息ID>`\n"
            "交付回执：`agentbus answer --caller <成员> --id <消息ID> --body-file <回执文件>`\n\n"
            "## 铁律\n"
            "1. 修改共享文件前先抢租约，改完释放；\n"
            "2. 所有产出物标注真实路径、字节数与 SHA256；\n"
            "3. 保持离线可审计，严禁凭臆想假装完成。\n",
            encoding="utf-8",
        )

    continuity_entries = {
        "TASKBOOK.md": (
            f"# 项目任务书\n\n来源：AgentBus Quickstart；时间：{initialized_at}\n\n"
            "## 当前目标\n完成项目初始化与首次多 Agent 协同打卡。\n\n"
            "## 任务清单\n- [ ] 0.1 环境与首条文件协作打卡自检\n"
        ),
        "MEMORY.md": (
            f"# 项目持久记忆\n\n来源：AgentBus Quickstart；时间：{initialized_at}\n\n"
            f"- 初始化时间：{initialized_at}\n"
            f"- 默认协调者：{director}\n"
            "- 存储规范：本地优先，UTF-8 原样保存\n"
        ),
        "STATE.md": (
            f"# 项目当前状态\n\n来源：AgentBus Quickstart；时间：{initialized_at}\n\n"
            "## 当前阶段\n工作区初始化就绪，进入团队自检阶段。\n"
        ),
        "LOG.md": (
            f"# 项目日志\n\n- 时间：{initialized_at}；事件：AgentBus quickstart 零配置初始化完成。\n"
        ),
        "PROJECT.md": (
            f"# 项目概要\n\n- 工作空间：`{ws}`\n- 协调者：`{director}`\n- 协同成员：{', '.join(sorted(cfg.enabled_workers()))}\n"
        ),
    }
    for filename, template in continuity_entries.items():
        p_file = ws / filename
        if not p_file.exists():
            p_file.write_text(template, encoding="utf-8")

    # 7. 若不存在任务树，注入 0.1 启程任务树
    tree_path = ws / ".agentbus" / "task-tree.json"
    if not tree_path.exists():
        init_tree = {
            "schema_version": 1,
            "roots": [
                {
                    "id": "project",
                    "parent_id": None,
                    "title": "项目总目标",
                    "goal": "完成工作区多 Agent 协同体系构建与业务目标交付。",
                }
            ],
            "branches": [
                {
                    "id": "welcome",
                    "parent_id": "project",
                    "title": "启程与自检树",
                    "goal": "完成工作区多 Agent 协同就绪性核验与首次文件协作。",
                }
            ],
            "leaves": [
                {
                    "id": "WELCOME-KICKOFF",
                    "section_no": "0.1",
                    "parent_id": "welcome",
                    "title": "环境与首条文件协作打卡自检",
                    "goal": "各协作 Agent 认领此首发打卡任务，出示心跳并完成回执，确认零配置工作台就绪。",
                    "handoff": "各成员可自主认领打卡并交付。",
                    "dependencies": [],
                    "history_task_ids": [],
                    "required_capabilities": [],
                }
            ],
        }
        tree_path.write_text(json.dumps(init_tree, ensure_ascii=False, indent=2), encoding="utf-8")

    # 7.1 准备项目正式工作流契约与入口
    workflow_error = ""
    try:
        from .project_workflow import prepare_project_workflow, WorkflowError
        prepare_project_workflow(cfg)
    except (WorkflowError, ValueError, TypeError, PermissionError,
            FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
        # Key write: entry registration did not commit; report it instead of
        # claiming a fully ready workspace.
        workflow_error = str(exc)

    # 8. 构造格式化终端向导文本
    guide_lines = [
        "",
        "+============================================================================+",
        "|             AgentBus 3 分钟零配置极速上手向导 (Quickstart Guide)           |",
        "+============================================================================+",
        "",
        "  [1. 本机环境自动探测发现]",
    ]
    for name, p in sorted(probes.items()):
        if name.startswith("__"):
            continue
        status_icon = "[OK] 已就绪" if p.installed else "[-] 未检测到"
        spawn_badge = " (可直接拉起)" if p.can_spawn else " (桌面/GUI 交互)" if p.installed else ""
        guide_lines.append(f"    {status_icon:<12} {name:<15} [{p.kind}]{spawn_badge}")

    guide_lines.extend([
        "",
        "  [2. 团队协同角色编排]",
        f"    * 核心协调者 (Director):  {director}",
        f"    * 活跃协作成员 (Workers):   {', '.join(sorted(cfg.enabled_workers()))}",
        f"    * 控制目录 (Control Root): {ws / '.agentbus'}",
        f"    * 业务目录 (Output Root):  {cfg.output_root}",
        "",
        "  [3. 极速起步 3 步操作路线]",
        "    步骤 1 (打开看板)：",
        f"      > agentbus gui --workspace \"{ws}\"",
        "      (可在浏览器或桌面中查看实时任务树拓扑与各 Agent 工位状态)",
        "",
        "    步骤 2 (查看信箱待办)：",
        f"      > agentbus inbox --workspace \"{ws}\" --mine --caller <你的成员ID>",
        "      (例如 caller 可以为 antigravity, opencode, trae 或 workbuddy)",
        "",
        "    步骤 3 (认领任务并正式交付)：",
        f"      > agentbus claim --workspace \"{ws}\" --agent <成员ID> --id <任务ID>",
        f"      > agentbus answer --workspace \"{ws}\" --caller <成员ID> --id <任务ID> --body \"已完成\"",
        "",
    ])
    if workflow_error:
        guide_lines.extend([
            "  [FAIL] 项目工作流入口准备未完成，请按下列原因处理后重新保存设置：",
            f"    {workflow_error}",
            "",
        ])
    guide_lines.extend([
        "+============================================================================+",
        (f"  [FAIL] 零配置初始化部分完成：工作流入口待处理。工作区目录：{ws}"
         if workflow_error else
         f"  [OK] 零配置初始化全部完成！工作区目录：{ws}"),
        "+============================================================================+",
        "",
    ])

    guide_text = "\n".join(guide_lines)
    if print_output:
        try:
            print(guide_text)
        except UnicodeEncodeError:
            print(guide_text.encode("ascii", errors="replace").decode("ascii"))

    return {
        "ok": not workflow_error,
        "workflow_error": workflow_error,
        "workspace": str(ws),
        "director": director,
        "enabled_workers": list(cfg.enabled_workers()),
        "detected_agents": list(installed.keys()),
        "guide_text": guide_text,
    }
