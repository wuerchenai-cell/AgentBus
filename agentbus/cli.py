#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.cli —— 命令行入口。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .adapters import AdapterError, codex_version, resolve_codex
from .bus import Bus, BusError
from .config import (EXIT_ADAPTER, EXIT_CODES, EXIT_CONFIG, EXIT_INTERRUPT,
                     EXIT_OPERATION, ConfigError, describe, load, save)
from .director import DIRECTOR_INBOX, HUMAN_INBOX, Director
from .lease import LeaseManager
from . import claim_fencing as _fencing
from .operation_lock import task_lock
from .routing import resolve_message_result_to, resolve_result_to_arg
from .skill_install import SkillInstallError, install_for_current_user
from .lifecycle import install_workflows, inspect_workflows, uninstall_workflows
from .health import inspect_project
from .project_workflow import (WorkflowError, prepare_project_workflow,
                               resume_prompt, workflow_status)
from .silent_debug import note_silent


def _configure_stdio_utf8() -> None:
    """Keep CLI and MCP wire output UTF-8 even under legacy Windows consoles."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        configure = getattr(stream, "reconfigure", None)
        if callable(configure):
            try:
                configure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass  # 资源清理类：流不支持重配置时沿用原编码，不阻断 CLI。


def _load(args):
    return load(args.workspace, create=False)

_DRAFT_SUMMARY = "## 1. 完成内容摘要"
_DRAFT_PLACEHOLDER = "（填写实际完成内容；未完成或受阻请如实说明。）"


def _prepare_reply_draft(cfg, agent: str, claimed, leaf: dict | None = None, worktree_info: dict | None = None) -> str | None:
    """在认领任务时自动生成 UTF-8 回执草稿模板，并自动注入 Aider-Style RepoMap 与 Lessons Learned。"""
    try:
        rel_path = f"成果/任务书维护/receipts/{claimed.id}.reply.md"
        draft_path = cfg.workspace / rel_path
        if draft_path.exists():
            original = re.findall(r'<!--\s*agentbus-claim-v1:\s*(\{[^\r\n]*\})\s*-->',
                                  draft_path.read_text(encoding='utf-8-sig'))
            if (len(original) != 1 or json.loads(original[0]).get('claim_id') != claimed.claim_id):
                rel_path = f'成果/任务书维护/receipts/{claimed.id}.{claimed.claim_id[:12]}.reply.md'
                draft_path = cfg.workspace / rel_path
        if not draft_path.exists():
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            sec = ""
            if isinstance(leaf, dict) and (leaf.get("section_no") or leaf.get("id")):
                sec = f"§{leaf.get('section_no') or leaf.get('id')} "
            expected_arts = getattr(claimed, "expected_artifacts", None) or []
            art_lines = "\n".join(f"- `{a}`" for a in expected_arts) if expected_arts else "- （按任务要求填写实际产物路径）"
            wt_line = ""
            if isinstance(worktree_info, dict) and worktree_info.get("worktree_path"):
                wt_line = f"- **隔离沙箱 (Worktree)**：`{worktree_info['worktree_path']}`\n"

            extra_sections = ""
            from .open_patterns import build_repomap_slice, query_relevant_lessons
            rmap = build_repomap_slice(cfg.workspace, target_paths=list(expected_arts), keywords=[claimed.title])
            if rmap.get("markdown"):
                extra_sections += f"\n## 4. 关联代码结构地图 (Aider-Style RepoMap)\n{rmap['markdown']}\n"
            lessons = query_relevant_lessons(cfg.workspace, target_paths=list(expected_arts), keywords=[claimed.title], limit=3)
            if lessons:
                l_lines = "\n".join(f"- [{item.get('agent', 'team')}] {item.get('lesson')}" for item in lessons)
                extra_sections += f"\n## 5. 历史避坑经验 (Lessons Learned)\n{l_lines}\n"

            template = (
                f"# 任务交付回执：{sec}{claimed.title}\n\n"
                f"<!-- agentbus-claim-v1: {json.dumps(_fencing.captured_ticket(claimed).to_dict(), ensure_ascii=False)} -->\n"
                f"<!-- AGENTBUS_DRAFT_UNFINISHED：完成实际工作后删除本行，再填写以下结论。 -->\n\n"
                f"- **任务 ID**：`{claimed.id}`\n"
                f"- **执行成员**：`{agent}`\n"
                f"{wt_line}\n"
                f"{_DRAFT_SUMMARY}\n"
                f"{_DRAFT_PLACEHOLDER}\n\n"
                f"## 2. 产物路径与核验\n"
                f"{art_lines}\n\n"
                f"## 3. 验证结果与边界说明\n"
                f"- （填写实际运行的验证、结果，以及未验证项。）\n"
                f"{extra_sections}"
            )
            draft_path.write_text(template, encoding="utf-8")
        return rel_path
    except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError, ValueError):
        return None


def _validate_receipt_body(text, *, require_text=True):
    if not isinstance(text, str) or (require_text and not text.strip()):
        raise ValueError('回执正文不能为空或仅含空白；请填写实际完成内容或受阻原因')
    if text.strip() == 'PLACEHOLDER':
        raise ValueError('PLACEHOLDER cannot replace a real task or result body; keep the original file')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    draft = (
        len(lines) >= 2 and lines[0].startswith('# 任务交付回执：')
        and lines[1].startswith('<!-- agentbus-claim-v1:')
    )
    marker = lines[2] if draft and len(lines) > 2 else text.strip()
    if re.fullmatch(r'<!--\s*AGENTBUS_DRAFT_UNFINISHED\b[^\r\n]*-->', marker):
        raise ValueError('回执仍是未填写草稿；请填写实际结论并删除顶部草稿标记')
    if text.strip() == _DRAFT_PLACEHOLDER:
        raise ValueError('请替换模板占位文字，填写实际完成内容或受阻原因')
    if draft and _DRAFT_SUMMARY in lines:
        remainder = lines[lines.index(_DRAFT_SUMMARY) + 1:]
        section = []
        for line in remainder:
            if line.startswith('## '):
                break
            section.append(line)
        if not section or section == [_DRAFT_PLACEHOLDER]:
            raise ValueError('回执第 1 节仍未填写；请写明实际完成内容或受阻原因')


def _receipt_text(args, cfg, inline_field):
    """Source: Codex Task 5, 2026-10-05. Read before any receipt mutation.

    WorkspaceFiles retains separators and checks the caller's raw-byte digest
    on every page. Its 8 MiB input and 64 KiB page limits remain unchanged;
    the existing CLI's 128-page work bound also applies. No truncation/fallback.
    """
    relative = getattr(args, 'body_file', None)
    expected = getattr(args, 'expected_body_sha256', None)
    text = getattr(args, inline_field, None)
    auto_mode = bool(getattr(args, 'auto_manifest', False) or getattr(args, 'artifacts', None))
    if relative is None:
        if expected is not None:
            raise ValueError('--expected-body-sha256 requires --body-file')
        if text is None:
            raise ValueError('provide exactly one inline body or --body-file')
    else:
        if text is not None:
            raise ValueError('--body-file and the inline body are mutually exclusive')
        if expected is None or (isinstance(expected, str) and expected.strip().lower() == 'auto'):
            if (isinstance(expected, str) and expected.strip().lower() == 'auto') or auto_mode:
                expected = None
            else:
                raise ValueError('--body-file requires --expected-body-sha256 of the original bytes')
        from .workspace_files import WorkspaceFiles, MAX_TEXT_BYTES
        files = WorkspaceFiles(cfg.workspace)
        pieces, line, size = [], 1, 0
        for _ in range(128):
            page = files.read_text(relative, start_line=line, max_lines=200,
                                   expected_sha256=expected)
            if expected is None:
                expected = page.get('sha256')
                args.expected_body_sha256 = expected
            pieces.append(page['text'])
            size += len(page['text'].encode('utf8'))
            if size > MAX_TEXT_BYTES:
                raise ValueError('body file exceeds the existing 8 MiB text limit')
            next_line = page['next_line']
            if next_line is None:
                text = ''.join(pieces)
                break
            if next_line <= line:
                raise ValueError('body file pagination made no progress')
            line = next_line
        else:
            raise ValueError('body file exceeds 128 pages; no partial body was submitted')
    _validate_receipt_body(text, require_text=(inline_field == 'body' or getattr(args, 'type', None) == 'result'))
    claims = re.findall(r'<!--\s*agentbus-claim-v1:\s*(\{[^\r\n]*\})\s*-->', text)
    if len(claims) > 1:
        raise ValueError('回执含多个认领凭证，拒绝猜测当前任务')
    if claims:
        args._original_receipt_ticket = _fencing.normalize(json.loads(claims[0]))
    return text


# --------------------------------------------------------------------------- #
# §7.17 AUDIT-INIT-DRYRUN：init 会静默写出多份文件（配置、.agentbus/、AGENTS.md、
# TASKBOOK/MEMORY/STATE/LOG.md）。对已有项目目录误跑一次就会莫名多出文件。
# 下面这个纯函数在**任何写盘之前**算出「将创建 / 已存在」清单，供 --dry-run 使用。
_INIT_CONTINUITY_FILES = ("TASKBOOK.md", "MEMORY.md", "STATE.md", "LOG.md")


def _init_plan(ws: Path, output_root: Path | None) -> dict:
    """列出 init 会创建与已存在的路径。**只读**，不做任何写操作。"""
    would_create: list[str] = []
    existing: list[str] = []

    def classify(path: Path, is_dir: bool = False) -> None:
        target = (path.is_dir() if is_dir else path.exists())
        (existing if target else would_create).append(str(path))

    classify(ws / ".agentbus")
    classify(ws / ".agentbus" / "config.json")
    classify(ws / ".agentbus" / "agents")
    classify(ws / "AGENTS.md")
    for filename in _INIT_CONTINUITY_FILES:
        classify(ws / filename)
    if output_root is not None:
        classify(output_root, is_dir=True)
    return {"would_create": would_create, "existing": existing}


def cmd_init(args) -> int:
    ws = Path(args.workspace).expanduser().resolve()
    output_root = None
    if args.output_root:
        output_root = Path(args.output_root).expanduser()
        if not output_root.is_absolute():
            output_root = ws / output_root
    if getattr(args, "quickstart", False):
        from .quickstart import run_quickstart
        res = run_quickstart(ws, output_root=output_root,
                             coordinator_override=getattr(args, "coordinator", None),
                             print_output=not getattr(args, "json", False))
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if getattr(args, "dry_run", False):
        plan = _init_plan(ws, output_root)
        if args.json:
            print(json.dumps({"dry_run": True, "workspace": str(ws),
                              **plan}, ensure_ascii=False, indent=2))
            return 0
        print(f"[预演] 未创建任何文件：{ws}")
        if plan["would_create"]:
            print("\n将创建：")
            for item in plan["would_create"]:
                print(f"  + {item}")
        if plan["existing"]:
            print("\n已存在（不会覆盖）：")
            for item in plan["existing"]:
                print(f"  = {item}")
        print("\n[预演结束] 确认无误后去掉 --dry-run 执行。")
        return 0

    _requested = [m.strip() for m in (getattr(args, 'members', None) or '').split(',') if m.strip()]
    if getattr(args, 'coordinator', None):
        _requested.append(args.coordinator.strip())
    if _requested and not (ws / ".agentbus" / "config.json").exists():
        from .config import DEFAULTS as _DEFAULTS
        _known = sorted(_DEFAULTS.get('workers', {}))
        _unknown = [m for m in dict.fromkeys(_requested) if m not in _known]
        if _unknown:
            raise ConfigError(
                f"成员 {', '.join(_unknown)} 不是内置成员，未创建任何文件。内置可选：{_known}。\n"
                f"    [下一步建议] 先用内置成员初始化（如 --coordinator {_known[0] if _known else 'codex'}），再执行 "
                f"agentbus member add --workspace \"{ws}\" --id <新ID> --name <显示名> --preset generic 加入自定义成员。")
    cfg = load(ws, create=True)
    if output_root is not None:
        data = dict(cfg.data)
        data['output_root'] = str(output_root)
        cfg = save(cfg, data)

    # §4.6 TEAM-DEFAULT-MINIMAL: 如果指定了 --members 或 --coordinator，按用户指定配置；
    # 否则若为新初始化且未显式指定，保持协调者为唯一默认启用成员（最小化安全起点），
    # 避免自动呈现未配置/未连接的其他厂商成员。
    explicit_members = getattr(args, 'members', None)
    explicit_coordinator = getattr(args, 'coordinator', None)
    if explicit_members is not None or explicit_coordinator is not None:
        coordinator = explicit_coordinator or cfg.director
        if explicit_members:
            chosen = [m.strip() for m in explicit_members.split(',') if m.strip()]
        else:
            chosen = [coordinator]
        if coordinator not in chosen:
            chosen.append(coordinator)
        data = dict(cfg.data)
        workers = data.setdefault('workers', {})
        for name, worker in workers.items():
            worker['enabled'] = name in chosen
            worker['executor_enabled'] = name in chosen
        data['director'] = coordinator
        cfg = save(cfg, data)
    elif getattr(args, 'minimal', False):
        # 显式 --minimal: 仅启用协调者
        coordinator = cfg.director
        data = dict(cfg.data)
        workers = data.setdefault('workers', {})
        for name, worker in workers.items():
            worker['enabled'] = (name == coordinator)
            worker['executor_enabled'] = (name == coordinator)
        cfg = save(cfg, data)

    cfg.output_root.mkdir(parents=True, exist_ok=True)
    bus = Bus(ws / ".agentbus")
    bus.init()
    bus.register(DIRECTOR_INBOX)
    bus.register(HUMAN_INBOX)
    for name in cfg.enabled_workers():
        bus.register(name)

    agents_md = ws / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(
            "# AgentBus 项目协作契约\n\n"
            "来源：AgentBus 初始化模板；由工作区负责人持续维护。\n\n"
            "## 新成员先读\n"
            "1. `PROJECT.md`：项目任务书、最终目标、当前约束和待办。没有就先创建。\n"
            "2. `MEMORY.md`：长期有效的架构决定、环境事实和协作约定。没有就先创建。\n"
            "3. `AGENTBUS.md`：总线、回执、租约和恢复规则。\n"
            "4. 自己的 `.agentbus/workflows/<成员>/SKILL.md`（若存在）。\n\n"
            "## 项目目的与持续改进\n"
            "本项目用可审计的文件总线协调多个 Agent，共同完成真实项目产出。\n"
            "用户提出的新要求先研究、写入 `PROJECT.md` 的任务清单，再按顺序执行；\n"
            "完成一项就留下产物、回执和未验证项，不能因新要求而遗忘既定目标。\n"
            "`MEMORY.md` 只记录可复用的长期事实和决定，任务过程记录在项目日志中。\n\n"
            "## 目录边界\n"
            "- `.agentbus/`：控制目录，只放配置、信箱、审计和租约；不要当业务产出目录。\n"
            "- 业务产出目录：由 `.agentbus/config.json` 的 `output_root` 指定，可在另一块磁盘；\n"
            "  用 `agentbus status --workspace \"<项目目录>\"` 查看，不要与控制目录混用。\n"
            "- `资料/`：输入素材；`成果/`：项目文档和交付物（除非 output_root 另有指定）。\n\n"
            "## 默认文件协作入口\n"
            "文件路线是默认方式：先查看自己的待办，再认领并处理文件，不要求 CLI 或模型常驻。\n"
            "查看自己的待办：`agentbus inbox --workspace \"<项目目录>\" --mine --caller <成员>`\n"
            "认领一条待办：`agentbus claim --workspace \"<项目目录>\" --agent <成员> [--id <消息ID>]`\n"
            "完成后使用正规 `answer`/`report` 接口回执；不要直接编辑或移动 `.agentbus` 消息 JSON。\n"
            "CLI、MCP 或自动接力只有在该成员已明确验收时才启用。\n\n"
            "## 硬性规则\n"
            "1. 修改共享文件前先抢 AgentBus 租约，改完释放；不要覆盖其他成员正在处理的文件。\n"
            "2. 所有产出写明来源、时间、真实路径和验证状态。\n"
            "3. 不确定的事不要猜，在报告里明确写\"未验证\"。\n"
            "4. 每次阶段结束更新 `PROJECT.md`、`MEMORY.md` 或日志中的对应事实，保持下一次接手不丢目标。\n",
            encoding="utf-8")

    # Keep continuity documents small, local, and human-readable. Existing
    # project decisions always win; initialization only fills missing files.
    initialized_at = time.strftime("%Y-%m-%d %H:%M:%S")
    continuity_entries = {
        "TASKBOOK.md": (
            "# 项目任务书\n\n"
            f"来源：AgentBus 初始化模板；时间：{initialized_at}\n\n"
            "## 当前目标\n"
            "在这里写一句项目最终要交付什么，以及成功的验收条件。\n\n"
            "## 持续改进\n"
            "用户提出的新要求先研究，再追加到任务清单；按顺序完成并保留证据。\n\n"
            "## 任务清单\n"
            "- [ ] 建立当前目标和验收条件\n"
            "- [ ] 记录后续要求、研究结论和对应产物\n\n"
            "## 下一步\n"
            "把当前最先执行的具体事项、负责人和验收条件写在这里。\n"),
        "MEMORY.md": (
            "# 项目长期记忆\n\n"
            f"来源：AgentBus 初始化模板；时间：{initialized_at}\n\n"
            "## 稳定事实\n"
            "记录下一次接手仍然需要知道的环境、结构和约定。\n\n"
            "## 已确认决定\n"
            "记录已经采用的方案及原因；过时内容注明日期，不删除历史依据。\n"),
        "STATE.md": (
            "# 项目当前状态\n\n"
            f"来源：AgentBus 初始化模板；时间：{initialized_at}\n\n"
            "## 当前目标\n"
            "从 TASKBOOK.md 读取当前目标；每次切换重点时同步更新。\n\n"
            "## 下一步\n"
            "写下一项可执行动作、负责人和所需输入。\n\n"
            "## 阻塞与未验证\n"
            "只记录已观察到的阻塞；不确定内容明确标记为未验证。\n"),
        "LOG.md": (
            "# 项目变更日志\n\n"
            f"来源：AgentBus 初始化模板；时间：{initialized_at}\n\n"
            "## 事件记录\n"
            "按时间追加重要决定、产物、验证结果和交接信息；不要粘贴大段原始输出。\n\n"
            f"- 时间：{initialized_at}；来源：AgentBus init；事件：创建连续性入口。\n"),
    }
    for filename, template in continuity_entries.items():
        path = ws / filename
        if not path.exists():
            path.write_text(template, encoding="utf-8")

    if getattr(sys, 'frozen', False) and not (cfg.workspace / '.agentbus/workflow.json').exists():
        prepare_project_workflow(cfg)
    print(f"[ok] 工作区已初始化: {ws}")
    print(f"     配置: {cfg.path}")
    print(f"     约定: {agents_md}")
    print("     连续性入口:")
    for filename in continuity_entries:
        print(f"       {filename}: {ws / filename}")
    print()
    print(describe(cfg.data))
    return 0


def cmd_config(args) -> int:
    cfg = _load(args)
    print(f"配置文件: {cfg.path}\n")
    print(describe(cfg.data))
    if args.json:
        print()
        print(json.dumps(cfg.data, ensure_ascii=False, indent=2))
    return 0


def cmd_benchmark(args) -> int:
    from .benchmark import run_cli_benchmark, render_markdown_report, generate_fast_wrappers
    cfg = _load(args)
    bench = run_cli_benchmark(cfg.workspace, exe_path=getattr(args, 'exe', None), runs=getattr(args, 'runs', 3))
    if getattr(args, 'wrappers', False):
        target = cfg.workspace / "成果"
        wrappers = generate_fast_wrappers(cfg.workspace, target)
        bench["wrappers"] = wrappers
    if getattr(args, 'json', False):
        print(json.dumps(bench, ensure_ascii=False, indent=2))
    else:
        print(render_markdown_report(bench))
        if getattr(args, 'wrappers', False):
            print("\n[ok] 已生成免解包快速调用脚本：")
            for w in bench.get("wrappers", []):
                print(f"  + {w}")
    return 0


def cmd_compact(args) -> int:
    from .compaction import plan_compaction, apply_compaction, query_archive
    cfg = _load(args)
    if getattr(args, "query", None):
        res = query_archive(cfg.workspace, args.query)
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif res:
            print(f"[ok] 在冷存储归档中找到任务 {args.query}（归档包：{res['bundle']}）：")
            for rec in res["records"]:
                print(f"  - [{rec['file_type']}] {rec['rel_path']} (SHA256: {rec['sha256'][:16]}...)")
        else:
            print(f"[-] 未在冷存储归档中找到任务: {args.query}")
        return 0

    plan = plan_compaction(cfg.workspace, keep_recent=getattr(args, "keep_recent", 5))
    if not getattr(args, "apply", False):
        if getattr(args, "json", False):
            print(json.dumps({"dry_run": True, **plan}, ensure_ascii=False, indent=2))
        else:
            print(f"[预演] 信箱冷热分层规划：可归档历史文件 {plan['candidates_count']} 个，预计释放 {plan['total_bytes'] / 1024:.1f} KB")
            for item in plan["candidates"][:10]:
                print(f"  + [{item['file_type']}] {item['rel_path']} ({item['bytes']} 字节)")
            if len(plan["candidates"]) > 10:
                print(f"  ... 另有 {len(plan['candidates']) - 10} 项")
            print("\n[预演提示] 请确认无误后添加 --apply 正式执行压缩归档。")
        return 0

    res = apply_compaction(cfg.workspace, plan)
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"[ok] 压缩归档完成：归档包 {res['bundle_file']} (SHA256: {res['bundle_sha256'][:16]}...)")
        print(f"     成功归档 {res['archived_items']} 项记录，移除 {res['removed_files']} 个热信箱文件，释放空间 {res['saved_bytes'] / 1024:.1f} KB")
    return 0


def cmd_host_setup(args) -> int:
    cfg = _load(args)
    from .host_adapters import audit_host_compatibility, apply_host_patches

    host = getattr(args, "host", "all") or "all"
    is_json = getattr(args, "json", False)

    if not getattr(args, "apply", False):
        diag = audit_host_compatibility(cfg.workspace)
        if is_json:
            print(json.dumps(diag, ensure_ascii=False, indent=2))
        else:
            print(f"=== 多宿主 IDE 集成状态诊断 ({cfg.workspace}) ===")
            for h, info in diag.items():
                status_str = "[OK] 已配置" if info["configured"] else "[-] 未就绪"
                print(f"[{h:<8}] {status_str}")
                for rel_path, f_info in info["files"].items():
                    exists_str = "存在" if f_info["exists"] else "缺失"
                    print(f"  - {rel_path} ({exists_str})")
            print("\n[提示] 执行 agentbus host-setup --apply [--host HOST] 自动注入适配规则与配置。")
        return 0

    target = [host] if host != "all" else None
    written = apply_host_patches(cfg.workspace, target)
    if is_json:
        print(json.dumps({"written": written}, ensure_ascii=False, indent=2))
    else:
        print(f"[ok] 宿主集成规则与补丁注入完成，共生成/更新 {len(written)} 个文件：")
        for f in written:
            print(f"  + {f}")
    return 0


def cmd_status(args) -> int:
    cfg = _load(args)
    bus = Bus(cfg.workspace / ".agentbus")
    print(f"工作区: {cfg.workspace}")
    print(f"控制目录: {cfg.workspace}")
    print(f"产出目录: {cfg.output_root}")
    print(f"协调者: {cfg.director}（参与者平级）")
    print(f"{'agent':<12}{'待办':>6}{'处理中':>8}{'阻塞':>6}{'已完成':>8}  角色")
    stats_items = bus.stats().items()
    hidden_legacy = False
    for name, s in stats_items:
        w = cfg.data["workers"].get(name, {})
        is_empty = (s['ready'] == 0 and s['processing'] == 0 and s['blocked'] == 0 and s['done'] == 0)
        if name == DIRECTOR_INBOX:
            if is_empty and not getattr(args, 'all', False):
                hidden_legacy = True
                continue
            role = f"旧版保留收件箱（空，已归档；协调者见 {cfg.director}）" if is_empty else f"旧版保留收件箱（有历史消息；当前协调者见 {cfg.director}）"
        elif name == HUMAN_INBOX:
            role = "人工收件箱（需要你决定的事）"
        elif not w:
            role = "已注册但未在配置中"
        else:
            role = "可无头拉起" if w.get("can_spawn") else "需人工接手"
            if name == cfg.director:
                role = "协调者（平级）/ " + role
        print(f"{name:<12}{s['ready']:>6}{s['processing']:>8}"
              f"{s['blocked']:>6}{s['done']:>8}  {role}")
    if hidden_legacy:
        print("  · [提示] 旧版 director 信箱无消息已折叠（使用 --all 可查看，当前平级协调者为 " f"{cfg.director}）")
    events = bus.events()
    invalid = [e for e in events if e.get('event') == 'audit_invalid_line'
               and e.get('generated_by') == 'agentbus.events']
    events = [e for e in events if e not in invalid]
    kinds: dict[str, int] = {}
    for e in events:
        kind = e.get('event', '?')
        if not isinstance(kind, str):
            kind = '?'
        kinds[kind] = kinds.get(kind, 0) + 1
    print(f"\n审计事件 {len(events)} 条: " +
          ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items())))
    if invalid:
        print(f'[警告] 审计异常：{len(invalid)} 行无法解析；原文件已保留，作业验收须先核实。')
        for row in invalid:
            print(f"  第 {row['line']} 行：{row['error_type']}（程序生成提示，非原审计记录）")
    esc = []
    if (cfg.workspace / ".agentbus" / "agents" / HUMAN_INBOX).exists():
        esc = bus.escalations()
    if esc:
        print(f"\n[!] 有 {len(esc)} 条需要你决定：")
        for m in esc:
            print(f"  · [{m.id}] {m.title}")

    tree_file = cfg.workspace / ".agentbus" / "task-tree.json"
    if tree_file.is_file():
        try:
            from .task_tree import TaskForest
            from .task_assignment import assignment_mode
            forest = TaskForest(cfg)
            snap = forest.snapshot()
            leaves = snap.get("leaves", [])
            if leaves:
                mode = assignment_mode(cfg.data)
                mode_desc = {
                    "autonomous": "自由抢单 (Agent 自主 claim-next)",
                    "automatic": "组长自动派发",
                    "manual": "人工手动指定",
                }.get(mode, mode)
                delivered = sum(1 for l in leaves if l.get("state") == "delivered")
                in_flight = sum(1 for l in leaves if l.get("state") in ("running", "claimed"))
                claimable = sum(1 for l in leaves if l.get("claimable"))
                blocked = sum(1 for l in leaves if l.get("state") in ("blocked", "awaiting_receipt"))
                print(f"\n[任务树与全局调度看板]")
                print(f"  调度模式  : {mode} ({mode_desc})")
                print(f"  任务树进度: 共 {len(leaves)} 项 | 已交付 {delivered} | 在途 {in_flight} | 就绪可领 {claimable} | 待回执/受阻 {blocked}")
                budget = snap.get("budget", {})
                if budget.get("total_tokens", 0) > 0 or budget.get("total_cost", 0) > 0:
                    print(f"  累计消耗  : Tokens={budget.get('total_tokens', 0):,} | 成本=${budget.get('total_cost', 0.0):.4f}")
                diag = forest.diagnose_deadlock(snap)
                if diag.get("is_deadlocked"):
                    top_b = (diag.get("root_blockers") or [{}])[0]
                    b_ref = top_b.get("section_no") or top_b.get("id") or "?"
                    b_err = top_b.get("evidence_error") or top_b.get("blocked_reason") or "上游节点受阻"
                    print(f"  [!] 死锁告警: 根节点 {b_ref} ({top_b.get('title', '')}) 阻断了 {top_b.get('blocked_downstream_count', 0)} 个下游任务 ({b_err})")
                    print(f"  下一步建议: agentbus tree reset --leaf {b_ref} --reason \"重置超时或受阻节点\"  或  agentbus tree pass --leaf {b_ref} --reason \"确认放行\"")
                elif claimable > 0:
                    print(f"  下一步建议: agentbus claim-next --agent {cfg.director}  (自主认领最高优先级就绪任务)")
                elif in_flight > 0:
                    print(f"  下一步建议: agentbus wait --timeout 120 --notify  (低功耗等待在途任务完成)")
        except (FileNotFoundError, PermissionError, ValueError, KeyError, RuntimeError):
            pass  # 资源清理类：状态看板附加任务树摘要读取失败时跳过，不影响主状态输出。
    return 0


def cmd_boss(args) -> int:
    cfg = _load(args)
    d = Director(cfg)
    try:
        if not cfg.worker(cfg.director).get("can_spawn"):
            msg = d.queue_goal(args.goal)
            print(f"目标已排入平级协调者 {cfg.director} 的信箱（{msg.id}）。")
            print("请在对应 GUI 会话手动接手；自动执行循环尚未启动，"
                  "AgentBus 不会静默改由其他 agent 接手。")
            cmd_handoff(type("HandoffArgs", (), {
                "workspace": args.workspace, "agent": cfg.director})())
            return 0
        d._goal = args.goal
        print(f"目标: {args.goal}")
        print("正在拆解任务（调用主体）..." if not args.worker else "跳过拆解，直接派发")
        tasks = d.submit(args.goal, auto_plan=not args.worker, single_worker=args.worker)
        print(f"已派发 {len(tasks)} 个任务:")
        for m in tasks:
            flag = "" if cfg.worker(m.to).get("can_spawn") else "  ← 需要你在界面里接手"
            print(f"  · [{m.id}] -> {m.to}: {m.title}{flag}")

        if any(not cfg.worker(m.to).get("can_spawn") for m in tasks):
            print("\n以下是 GUI 成员可直接领取的完整提示：")
            cmd_handoff(type("HandoffArgs", (), {
                "workspace": args.workspace, "agent": None})())

        if args.seconds <= 0:
            print("\n（未指定 --seconds，只派发不等待。用 run 命令启动执行循环）")
            return 0

        print(f"\n开始执行循环（最多 {args.seconds:.0f}s）...\n")
        summary = d.run(seconds=args.seconds, review=not args.no_review,
                        continue_after_results=True,
                        await_manual=bool(getattr(args, "await_manual", False)))
        print(summary.render())
        if getattr(args, "await_manual", False) and d._manual_work_pending(
                list(cfg.spawnable_workers())):
            print("\n[待人工] 仍有 GUI 成员任务/评审未回执；"
                  "结果会由本运行继续监听，未标记为完成。")
        # Source: actual OpenCode UI review failure, 2026-10-04. Running
        # the loop successfully is different from executing its tasks.
        return 1 if summary.failed else 0
    finally:
        d.close()


def cmd_run(args) -> int:
    cfg = _load(args)
    only = getattr(args, "agent", None)
    if only:
        if only not in cfg.spawnable_workers():
            raise ConfigError(f"{only!r} 不是本项目可运行成员；请检查小组与接入状态")
        if getattr(args, "follow_ups", False):
            raise ConfigError("指定单成员运行时不能开启自动组长接力")
        # Keep group identities/routes intact; scope execution eligibility in
        # memory only. Active Codex chat work must not run in a second session.
        from copy import deepcopy
        cfg = deepcopy(cfg)
        cfg.data["gates"]["task_timeout_s"] = max(1, min(
            int(cfg.gates.get("task_timeout_s", 1800)), int(args.seconds)))
        for name, worker in cfg.data["workers"].items():
            if name != only:
                worker["can_spawn"] = False
    d = Director(cfg)
    try:
        summary = d.run(
            seconds=args.seconds, review=not args.no_review,
            continue_after_results=bool(getattr(args, "follow_ups", False)),
            await_manual=bool(getattr(args, "await_manual", False)))
        print(summary.render())
        if getattr(args, "await_manual", False) and d._manual_work_pending(
                list(cfg.spawnable_workers())):
            print("\n[待人工] 仍有 GUI 成员任务/评审未回执；"
                  "结果会由本运行继续监听，未标记为完成。")
        return 1 if summary.failed else 0
    finally:
        d.close()


_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _inbox_agent(args, cfg) -> str:
    """Resolve an explicit inbox member or the caller's own inbox."""
    explicit = getattr(args, "agent", None)
    mine = bool(getattr(args, "mine", False))
    caller = getattr(args, "caller", None)
    if mine:
        if not caller:
            raise ConfigError("inbox --mine 必须同时提供 --caller <已登记成员>")
        if explicit and explicit != caller:
            raise ConfigError("inbox --mine 不能与不同的 --agent 同时使用")
        explicit = caller
    if not explicit:
        raise ConfigError("inbox 需要 --agent，或使用 --mine --caller <成员>")
    if explicit not in cfg.enabled_workers():
        raise ConfigError(
            f"成员 {explicit!r} 未登记在当前小组中；可选：{sorted(cfg.enabled_workers())}")
    return explicit


def _normalize_artifact_path(p: str) -> str:
    """归一化产物相对路径：统一正斜杠，剥离前导 ./ 与 /，避免操作系统差异导致比对失败。"""
    cleaned = str(p or "").strip().replace("\\", "/")
    while cleaned.startswith("./") or cleaned.startswith("/"):
        cleaned = cleaned.lstrip("./").lstrip("/")
    return cleaned


def _cli_ticket(args, task_id, agent):
    epoch, claim_id = getattr(args, 'claim_epoch', None), getattr(args, 'claim_id', None)
    if epoch is None and claim_id is None:
        original = getattr(args, '_original_receipt_ticket', None)
        if original is not None:
            if original.task_id != task_id or original.agent != agent:
                raise BusError('回执认领凭证不属于本任务或本成员；请回到原任务继续')
            return original
        return None
    explicit = _fencing.normalize({'task_id': task_id, 'agent': agent,
                                  'claim_epoch': epoch, 'claim_id': claim_id})
    original = getattr(args, '_original_receipt_ticket', None)
    if original is not None and (original.task_id != explicit.task_id
            or original.agent != explicit.agent or original.claim_epoch != explicit.claim_epoch
            or original.claim_id != explicit.claim_id):
        raise BusError('回执草稿与显式认领票据冲突；不能使用另一对话的凭证替换旧草稿身份')
    return explicit


def cmd_evidence(args) -> int:
    from .evidence_service import execute
    cfg = _load(args)
    subject = getattr(args, 'task_id', None) or getattr(args, 'job_id', None)
    kind = 'job' if getattr(args, 'job_id', None) else 'task'
    print(execute(cfg, args.cmd, subject, format=args.format, kind=kind))
    return 0


def _check_delivery_destinations(cfg, verified, export, summary):
    """Validate the whole set before creating either the report or the archive."""
    from .public_files import _check_ancestors
    def destination(value):
        if not value:
            return None
        path = cfg.workspace / Path(value)
        if any(part.casefold() == '.agentbus' for part in path.parts):
            raise ValueError('导出位置不能在项目控制目录中')
        _check_ancestors(path)
        return path.resolve()
    archive, report = destination(export), destination(summary)
    folder = archive is not None and archive.suffix.lower() != '.zip'
    if folder and archive.exists() and (not archive.is_dir() or any(archive.iterdir())):
        raise ValueError('导出请选择空目录或新目录，避免混入旧文件。')
    targets = ([archive / name for name in verified] if folder else [archive]) if archive else []
    if report:
        targets.append(report)
    names = {cfg.output_root.resolve() / name for name in verified}
    identities = set()
    for name in names:
        stat = name.stat()
        identities.add((stat.st_dev, stat.st_ino))
    if folder and report and (report == archive or report in archive.parents):
        raise ValueError(f'报告路径与导出目录冲突：{report}')
    for target in targets:
        if target in names or any(parent in names for parent in target.parents):
            raise ValueError(f'导出路径与原件或另一项输出冲突：{target}')
        if target.exists():
            info = target.stat()
            identity = (info.st_dev, info.st_ino)
            if not target.is_file() or identity in identities or info.st_nlink > 1:
                raise ValueError(f'导出目标不是独立的普通文件，已保留原件：{target}')
            identities.add(identity)
        if any(parent.exists() and not parent.is_dir() for parent in target.parents):
            raise ValueError(f'导出目录与已有文件冲突：{target}')
        names.add(target)
    if report and any(report in target.parents for target in targets if target != report):
        raise ValueError(f'报告路径与导出文件冲突：{report}')


def export_verified_delivery(cfg, forest, *, export_target=None, summary_target=None, allow_partial=False, emit=print):
    snapshot = forest.snapshot()
    leaves = snapshot.get('leaves', [])
    total_leaves = len(leaves)
    delivered_leaves = [l for l in leaves if l.get('state') == 'delivered']
    delivered_count = len(delivered_leaves)
    completion_pct = (delivered_count / total_leaves * 100.0) if total_leaves > 0 else 0.0
    out_root = cfg.output_root
    total_files = 0
    total_bytes = 0
    issues = []
    verified_outputs = {}
    from .workspace_files import WorkspaceFiles, MAX_ARTIFACT_BYTES
    output_files = WorkspaceFiles(out_root)
    def export_bytes(rel):
        _, path, observed = output_files._path(rel)
        raw = output_files._raw(path, MAX_ARTIFACT_BYTES, snapshot=observed)
        if hashlib.sha256(raw).hexdigest() != verified_outputs[rel]:
            raise ValueError(f'产物在核验后发生变化，停止导出：{rel}')
        return raw
    emit(f"=== 产品交付产物与输出完整性核验 (产出根目录: {out_root}) ===")
    emit(f"[进度] 全项目正式交付: {delivered_count}/{total_leaves} 项 ({completion_pct:.1f}%)")
    if delivered_count < total_leaves:
        unreached = [l for l in leaves if l.get('state') != 'delivered']
        unreached_desc = ', '.join(f"§{l.get('section_no', l['id'])}={l.get('state')}" for l in unreached[:5])
        if len(unreached) > 5:
            unreached_desc += "..."
        emit(f"       待终态任务数：{len(unreached)} 个 (状态分布: {unreached_desc})")
    unresolved_evidence = {
        'admin_passed': '仅有管理员放行，缺少正式验收。',
        'awaiting_audit': '等待独立审核。',
        'rejected': '独立审核未通过。',
    }
    for leaf in leaves:
        reason = unresolved_evidence.get(leaf.get('state'))
        if reason:
            sec = leaf.get('section_no') or leaf['id']
            issues.append(f"[未验证] §{sec}「{leaf['title']}」{reason}")
    for leaf in delivered_leaves:
        title = leaf.get('title')
        sec = leaf.get('section_no') or leaf.get('id')
        arts = leaf.get('artifacts', [])
        if leaf.get('evidence_error') or leaf.get('receipt_state') != 'valid':
            issues.append(f"[未验证] §{sec}「{title}」：{leaf.get('evidence_error') or '回执尚未核验'}")
            continue
        replacements = {r['path']: r for r in leaf.get('artifact_replacements', [])}
        expected_set = set(leaf.get('expected_artifacts', []) or [])
        recorded_paths = {a.get('path') for a in arts if isinstance(a, dict)}
        missing_expected = expected_set - recorded_paths
        for exp in sorted(missing_expected):
            issues.append(f"[缺失回执] §{sec}「{title}」期望产物未包含在回执清单中：{exp}")

        if not arts:
            continue
        for art in arts:
            rel_path = art.get('path', '')
            expected_sha = replacements.get(rel_path, {}).get('current_sha256', art.get('sha256', ''))
            try:
                _, file_path, observed = output_files._path(rel_path)
                blob = output_files._raw(file_path, MAX_ARTIFACT_BYTES, snapshot=observed)
            except (OSError, ValueError) as exc:
                issues.append(f"[缺失或越界] §{sec}「{title}」：{rel_path} ({type(exc).__name__})")
                continue
            actual_bytes = len(blob)
            actual_sha = hashlib.sha256(blob).hexdigest()
            if actual_sha.lower() != expected_sha.lower():
                issues.append(f"[漂移] §{sec}「{title}」产物哈希不一致：{rel_path} (期望 {expected_sha[:8]}, 实际 {actual_sha[:8]})")
            else:
                if rel_path not in verified_outputs:
                    total_files += 1
                    total_bytes += actual_bytes
                verified_outputs[rel_path] = actual_sha
                emit(f"  [有效] §{sec}「{title}」-> {rel_path} ({actual_bytes} 字节, sha256 匹配)")
    emit("-" * 60)
    if issues:
        emit(f"[警告] 核验完成：发现 {len(issues)} 处产物异常：")
        for iss in issues:
            emit(f"    {iss}")
        return 1
    _check_delivery_destinations(cfg, verified_outputs, export_target, summary_target)
    partial = delivered_count < total_leaves
    if partial and (not total_files or not allow_partial):
        emit('[未完成] 全项目尚未交付，未生成归档或交付报告。'
              '已有核验文件时，可用 --allow-partial 只导出已交付部分。')
        return 1
    if not total_files:
        status = '没有文件产物，文件核验不适用。'
        report_title = '任务交付记录'
        emit(f'[不适用] {status}')
        if export_target:
            emit('未生成空归档。')
            return 1
    elif partial:
        status = f'部分交付：{delivered_count}/{total_leaves} 项正式交付，已核验 {total_files} 个文件。'
        report_title = '部分交付与产物清单'
        emit(f'[部分] {status}')
    else:
        status = f'完整交付：{delivered_count}/{total_leaves} 项正式交付，{total_files} 个文件摘要一致。'
        report_title = '产品全景交付白皮书与产物清单'
        emit(f'[通过] {status}')

    if export_target:
        target_p = Path(export_target)
        if not target_p.is_absolute():
            target_p = cfg.workspace / target_p
        target_p.parent.mkdir(parents=True, exist_ok=True)
        if target_p.suffix.lower() == '.zip':
            import tempfile
            import zipfile
            with tempfile.NamedTemporaryFile(dir=target_p.parent, prefix='.agentbus-export-',
                                             delete=False) as handle:
                staging = Path(handle.name)
            try:
                with zipfile.ZipFile(staging, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                    for rel in verified_outputs:
                        zf.writestr(rel, export_bytes(rel))
                os.replace(staging, target_p)
            finally:
                staging.unlink(missing_ok=True)
        else:
            if target_p.is_dir() and any(target_p.iterdir()):
                raise ValueError('导出请选择空目录或新目录，避免混入旧文件。')
            target_p.mkdir(parents=True, exist_ok=True)
            for rel in verified_outputs:
                dest = target_p / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(export_bytes(rel))
        emit(f"  [导出] {'部分交付' if partial else '完整交付'}，{total_files} 个文件：{target_p}")

    if summary_target:
        sum_p = Path(summary_target)
        if not sum_p.is_absolute():
            sum_p = cfg.workspace / sum_p
        sum_p.parent.mkdir(parents=True, exist_ok=True)
        report_lines = [
            f"# {report_title}",
            "",
            f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 正式交付节点：{delivered_count}/{total_leaves} 项",
            f"- 核验文件：{total_files} 个，共 {total_bytes} 字节",
            f"- 状态：{status}",
        ]
        if total_files:
            report_lines.extend([
                "- 合法历史摘要仅用于追溯，不代表当前版本摘要。",
                "",
                "## 交付产物明细清单",
                "",
                "| 章节 | 任务标题 | 产物相对路径 | 记录字节数 | 记录 SHA256 摘要 | 版本说明 |",
                "|---|---|---|---|---|---|",
            ])
        if not total_files:
            for leaf in delivered_leaves:
                report_lines.extend(["", f"## §{leaf.get('section_no') or leaf['id']} {leaf['title']}",
                                     str(leaf.get('results') or '仅有任务完成记录。')])
        for leaf in delivered_leaves:
            sec = leaf.get('section_no') or leaf.get('id')
            title = leaf.get('title')
            for art in leaf.get('artifacts', []):
                rel = art.get('path', '')
                size = art.get('bytes', 0)
                digest = art.get('sha256', '')
                historical = any(item.get('path') == rel for item in leaf.get('artifact_replacements', []))
                version = '历史版本（已被合法后继替代）' if historical else '当前版本'
                report_lines.append(f"| §{sec} | {title} | `{rel}` | {size} | `{digest}` | {version} |")
        from .skill_install import _atomic_write
        _atomic_write(sum_p, ('\n'.join(report_lines) + '\n').encode('utf-8'))
        emit(f"  [报告] 已生成交付记录：{sum_p}")
    return 0


def cmd_tree(args) -> int:
    """Public task forest actions; all execution state remains in the file bus."""
    from .task_tree import TaskForest
    cfg = _load(args)
    forest = TaskForest(cfg)
    if args.action == 'status':
        value = forest.snapshot()
    elif args.action == 'continue':
        from .leaf_continuation import continue_leaf
        if not args.leaf or not args.agent or not args.id:
            raise ValueError('续接需提供 --leaf、--agent 当前组长和 --id 原任务')
        value = continue_leaf(forest, args.leaf, caller=args.agent,
            expected_task_id=args.id, expected_claim_epoch=args.claim_epoch,
            goal=args.goal, request_id=args.request_id)
    elif args.action == 'show':
        if not args.leaf:
            raise ValueError('请使用 --leaf 指定任务章节号，如 1.2')
        value = forest._leaf(forest.snapshot(), args.leaf)
    elif args.action == 'templates' and getattr(args, 'preview', False):
        from .tree_templates import builtin_templates, render_template_outline
        items = builtin_templates()
        selected = (getattr(args, 'template', None) or '').strip()
        if selected:
            match = [item for item in items if item['id'] == selected]
            if not match:
                raise ValueError(f'没有内置模板 {selected}；可用：'
                                 + '、'.join(item['id'] for item in items))
            items = match
        blocks = [render_template_outline(item['document'], title=item['title']) for item in items]
        print('\n\n'.join(blocks))
        return 0
    elif args.action in ('backup', 'restore'):
        from .web_bridge import WebBridge
        from .public_files import PublicFiles
        bridge = WebBridge(cfg.workspace)
        if args.action == 'backup':
            payload = {}
            if args.file:
                payload['path'] = str(PublicFiles(cfg.workspace).checked(args.file))
            elif getattr(args, 'preview', False):
                payload['preview_only'] = True
            else:
                raise ValueError('备份须指定 --file 文件路径，或使用 --preview 查看清单预览')
            res = bridge.call('export_backup', payload)
            if not res['ok']:
                raise ValueError(res['error'])
            value = res['data']
        else: # restore
            if not args.file:
                raise ValueError('还原须指定备份文件 --file')
            payload = {'path': str(PublicFiles(cfg.workspace).checked(args.file))}
            if args.apply:
                # 预览并获取 preview_id
                prev_res = bridge.call('restore_backup', payload)
                if not prev_res['ok']:
                    raise ValueError(prev_res['error'])
                preview_id = prev_res['data']['preview']['preview_id']
                payload['confirm'] = True
                payload['preview_id'] = preview_id
            res = bridge.call('restore_backup', payload)
            if not res['ok']:
                raise ValueError(res['error'])
            value = res['data']
    elif args.action in ('templates', 'export', 'import', 'prompt'):
        from .web_bridge import WebBridge
        from .public_files import PublicFiles
        payload = {}
        action = {'templates': 'tree_templates', 'export': 'export_tree',
                  'import': 'import_tree', 'prompt': 'task_prompt'}[args.action]
        if args.action == 'import' and args.file and args.template:
            raise ValueError('请选择任务树文件或内置模板，不能同时指定两者。')
        if args.action in ('import', 'export'):
            if args.file:
                payload['path'] = str(PublicFiles(cfg.workspace).checked(args.file))
            elif args.action == 'import' and args.template:
                payload['template_id'] = args.template
            else:
                raise ValueError('命令行导入/导出须指定项目相对 --file；导入也可指定 --template')
            if args.action == 'import':
                if args.apply and not args.expected_sha256:
                    raise ValueError('导入前请先预览任务树，再使用 --expected-sha256 <预览中的 source_sha256> 确认。')
                if not args.apply and args.expected_sha256:
                    raise ValueError('--expected-sha256 只可与 --apply 一起使用')
        if args.action == 'prompt':
            if not args.leaf or not args.agent:
                raise ValueError('接手指令需要 --leaf 章节号和 --agent 成员')
            payload = {'leaf_id': args.leaf, 'agent': args.agent}
        bridge = WebBridge(cfg.workspace)
        if args.action == 'import' and args.apply:
            expected = str(args.expected_sha256).strip().casefold()
            if len(expected) != 64 or any(char not in '0123456789abcdef' for char in expected):
                raise ValueError('--expected-sha256 必须是 64 位十六进制 SHA256')
            preview_response = bridge.call(action, payload)
            if not preview_response['ok']:
                raise ValueError(preview_response['error'])
            preview_data = preview_response['data']
            if preview_data.get('source_sha256') != expected:
                if preview_data.get('template_id'):
                    raise ValueError('内置模板内容与上次预览不一致，请重新预览并核对摘要后再确认。')
                raise ValueError('任务树文件在预览后已变化，请重新预览并核对摘要后再确认。')
            payload.update(confirm=True, preview_id=preview_data.get('preview_id'))
        response = bridge.call(action, payload)
        if not response['ok']:
            raise ValueError(response['error'])
        value = response['data']
    elif args.action == 'import-deliveries':
        value = forest.ensure_product_forest()
    elif args.action in ('add', 'append'):
        if args.file:
            item = _workspace_json(cfg, args.file)
            if isinstance(item, dict) and 'leaves' in item:
                value = forest.replace_structure(item)
            else:
                value = forest.seed_leaf(item, branch_id=args.branch, project_goal=args.goal or '项目目标')
        elif getattr(args, 'title', None):
            deps = [d.strip() for d in (getattr(args, 'dependencies', '') or '').split(',') if d.strip()]
            branch = args.branch
            if not branch:
                tree_struct = forest.snapshot()
                if tree_struct.get('branches'):
                    branch = tree_struct['branches'][0]['id']
                elif tree_struct.get('roots'):
                    branch = tree_struct['roots'][0]['id']
                else:
                    branch = 'main'
            leaf_data = {
                'title': args.title,
                'goal': args.goal or args.title,
                'parent_id': branch,
            }
            if getattr(args, 'id', None):
                leaf_data['id'] = args.id
            if deps:
                leaf_data['dependencies'] = deps
            if getattr(args, 'artifacts', None):
                art_list = [_normalize_artifact_path(a) for a in str(args.artifacts).split(',') if a.strip()]
                if art_list:
                    leaf_data['expected_artifacts'] = art_list
            if getattr(args, 'capabilities', None):
                cap_list = [c.strip() for c in str(args.capabilities).split(',') if c.strip()]
                if cap_list:
                    leaf_data['required_capabilities'] = cap_list
            value = forest.seed_leaf(leaf_data, branch_id=branch, project_goal=args.goal or '项目目标')

            # 终端友好反馈卡片
            snap = forest.snapshot()
            created_leaf = next((l for l in snap.get('leaves', []) if l['id'] == leaf_data.get('id') or l['title'] == leaf_data['title']), None)
            sec = created_leaf.get('section_no', '-') if created_leaf else '-'
            leaf_id = created_leaf.get('id', '-') if created_leaf else leaf_data.get('id', '-')
            is_claimable = created_leaf.get('claimable', False) if created_leaf else False
            print(f"[ok] 成功创建任务节点 {leaf_id} [{leaf_data['title']}]")
            print(f"    所属章节：§{sec}")
            print(f"    挂载分支：{branch}")
            if deps:
                print(f"    前置依赖：{deps}")
            if leaf_data.get('expected_artifacts'):
                print(f"    预期产物：{json.dumps(leaf_data['expected_artifacts'], ensure_ascii=False)}")
            if leaf_data.get('required_capabilities'):
                print(f"    能力要求：{json.dumps(leaf_data['required_capabilities'], ensure_ascii=False)}")
            print(f"    就绪状态：{'就绪可抢单 (claimable)' if is_claimable else '等待前驱完成 (blocked)'}")
            return 0
        else:
            raise ValueError('tree add/append 需要 --file 文件或 --title 任务标题以动态追加任务')
    elif args.action in ('artifacts', 'verify'):
        return export_verified_delivery(cfg, forest, export_target=getattr(args, 'export', None),
            summary_target=getattr(args, 'summary_file', None), allow_partial=getattr(args, 'allow_partial', False))

    elif args.action == 'worker-loop':
        from .worker_loop import WorkerLoop
        if not args.agent:
            raise ValueError('tree worker-loop 需要使用 --agent 指定成员 ID')
        raise ValueError('命令行工蜂循环没有可执行的处理器：它不会替成员调用模型或完成任务。'
            '请在成员自己的对话里执行 claim-next → 完成产物 → answer --auto-manifest；'
            '需要无人值守时，由集成方在 Python 中以 WorkerLoop(handler=...) 显式注入处理器。')
        loop = WorkerLoop(
            cfg=cfg,
            agent=args.agent,
            poll_interval=getattr(args, 'poll_interval', 2.0) or 2.0,
            max_tasks=getattr(args, 'max_tasks', None),
            timeout_s=getattr(args, 'timeout_s', None),
            auto_claim=True,
        )
        continuous = getattr(args, 'continuous', False)
        result = loop.run(continuous=continuous)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["failed"] == 0 else 1
    elif args.action == 'spawn':
        from .headless_spawn import HeadlessSpawnEngine
        engine = HeadlessSpawnEngine(cfg, default_timeout_s=getattr(args, 'timeout_s', 300.0) or 300.0)
        if args.leaf:
            leaf_view = forest._leaf(forest.snapshot(), args.leaf)
            target_agent = args.agent or (leaf_view.get("eligible_agents") or [None])[0]
            if not target_agent:
                raise ValueError("未指定 --agent，且该任务无可用 eligible_agents")
            res = engine.spawn_leaf(leaf_view, target_agent, wait=True)
            print(json.dumps(res.__dict__, ensure_ascii=False, indent=2))
            return 0 if res.success else 1
        else:
            spawned = engine.auto_spawn_ready(max_spawn=getattr(args, 'max_tasks', 5) or 5)
            print(json.dumps([r.__dict__ for r in spawned], ensure_ascii=False, indent=2))
            return 0
    else:
        if args.action == 'diagnose':
            diag = forest.diagnose_deadlock()
            print(json.dumps(diag, ensure_ascii=False, indent=2))
            return 1 if diag.get('is_deadlocked') else 0
        if args.action == 'pass':
            if not args.leaf:
                raise ValueError('tree pass 需要 --leaf 指定目标叶子章节号或 ID')
            reason = getattr(args, 'reason', None) or getattr(args, 'note', None)
            if not reason:
                raise ValueError('tree pass 需要 --reason 提供明确的放行说明理由')
            leaf = forest.pass_leaf(args.leaf, reason, admin=getattr(args, 'agent', None) or 'admin')
            sec = leaf.get('section_no') or leaf.get('id')
            print(f"[ok] 成功放行节点 §{sec} [{leaf.get('title')}] (ID: {leaf.get('id')})")
            print(f"     放行理由: {reason}")
            print("     已登记依赖放行。下游仍须满足其他前序条件，成果按原证据核验。")
            return 0
        if args.action == 'reset':
            if not args.leaf:
                raise ValueError('tree reset 需要 --leaf 指定目标叶子章节号或 ID')
            reason = getattr(args, 'reason', None) or getattr(args, 'note', None)
            if not reason:
                raise ValueError('tree reset 需要 --reason 提供明确的重置重排说明理由')
            leaf = forest.reset_leaf(args.leaf, reason, admin=getattr(args, 'agent', None) or 'admin',
                                     owner_confirmed_inactive=getattr(args, 'owner_confirmed_inactive', False),
                                     expected_task_id=getattr(args, 'id', None), expected_owner=getattr(args, 'expected_owner', None),
                                     expected_claim_epoch=getattr(args, 'claim_epoch', None))
            sec = leaf.get('section_no') or leaf.get('id')
            print(f"[ok] 成功重置节点 §{sec} [{leaf.get('title')}] (ID: {leaf.get('id')})")
            print(f"     重置理由: {reason}")
            print(f"     该节点已重新开放为 queued 就绪态；重新认领仍受原期限、预算和重试限制。")
            return 0
        if not args.leaf or not args.agent:
            raise ValueError('tree action requires --leaf and --agent')
        if args.action == 'claim':
            leaf = forest.resolve_leaf(args.leaf)
            claimed = forest.claim_leaf(leaf["id"], args.agent)
            value = claimed.to_dict()
            draft = _prepare_reply_draft(cfg, args.agent, claimed, leaf)
            if draft:
                print(f"回执草稿：{draft}。填写后使用 answer --body-file 提交。", file=sys.stderr)
            else:
                print("任务已认领，但回执草稿未生成。请保留输出中的原票据，填写正文后显式提交。",
                      file=sys.stderr)
        elif args.action == 'dispatch':
            value = forest.dispatch_leaf(args.leaf, args.agent).to_dict()
            try:
                from .trigger.service import TriggerDeliveryService
                TriggerDeliveryService(cfg.workspace).trigger_for_member(args.agent, value.get('id') if isinstance(value, dict) else None)
            except Exception:
                pass  # 容错通知类：触发工位唤醒异常不阻断分派
        elif args.action == 'checkpoint':
            value = forest.checkpoint_leaf(args.leaf, args.agent, args.note,
                                          ticket=_cli_ticket(args, args.id, args.agent))
        elif args.action == 'takeover':
            if not args.leaf:
                raise ValueError('请使用 --leaf 指定目标叶子节点 ID')
            target_agent = args.agent or getattr(args, 'successor', None)
            if not target_agent:
                raise ValueError('请使用 --agent 指定接手成员 ID')
            force = getattr(args, 'force', False) or getattr(args, 'owner_confirmed_inactive', False)
            reason = getattr(args, 'note', '') or ''
            result = forest.takeover_leaf(args.leaf, target_agent, reason=reason, force=force,
                expected_task_id=getattr(args, 'id', None), expected_owner=getattr(args, 'expected_owner', None),
                expected_claim_epoch=getattr(args, 'claim_epoch', None))
            print(f"=== 任务接管成功 ===")
            print(f"节点 ID: {args.leaf}")
            print(f"接手成员: {target_agent}")
            print(f"新任务 ID: {result['new_task_id']}")
            print(f"\n--- 接手自驱动执行指令 ---")
            print(result['capsule']['prompt'])
            return 0
        else:
            handoff_ticket = (None if args.owner_confirmed_inactive and not getattr(args, 'claim_id', None)
                              else _cli_ticket(args, args.id, args.agent))
            value = forest.handoff_leaf(args.leaf, args.agent,
                ticket=handoff_ticket, reason=args.note,
                successor=args.successor, owner_confirmed_inactive=args.owner_confirmed_inactive,
                expected_task_id=getattr(args, 'id', None), expected_owner=getattr(args, 'expected_owner', None),
                expected_claim_epoch=getattr(args, 'claim_epoch', None))
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def cmd_worker_loop(args) -> int:
    """自主工蜂连续工作循环（§12.1 AUTONOMOUS-WORKER-LOOP）。"""
    from .worker_loop import WorkerLoop
    cfg = _load(args)
    agent = getattr(args, "agent", None)
    if not agent:
        raise ValueError("--agent 必须指定智能体 ID")
    raise ValueError('命令行工蜂循环没有可执行的处理器：它不会替成员调用模型或完成任务。'
            '请在成员自己的对话里执行 claim-next → 完成产物 → answer --auto-manifest；'
            '需要无人值守时，由集成方在 Python 中以 WorkerLoop(handler=...) 显式注入处理器。')
    loop = WorkerLoop(
        cfg=cfg,
        agent=agent,
        poll_interval=getattr(args, "poll_interval", 2.0) or 2.0,
        max_tasks=getattr(args, "max_tasks", None),
        timeout_s=getattr(args, "timeout_s", None),
        auto_claim=True,
    )
    continuous = getattr(args, "continuous", False)
    result = loop.run(continuous=continuous)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["failed"] == 0 else 1


def cmd_heartbeat(args) -> int:
    cfg = _load(args)
    bus = Bus(cfg.workspace / '.agentbus')
    agent = getattr(args, 'agent', None)
    task_id = getattr(args, 'id', None)
    if not task_id:
        proc = bus.processing(agent)
        if len(proc) == 1:
            task_id = proc[0].stem
        elif not proc:
            raise BusError(f'工位 {agent} 当前没有正在执行（processing）的在途任务')
        else:
            raise BusError(f'工位 {agent} 当前有多个在途任务，请使用 --id 明确指定')
    msg = bus._active_record(agent, task_id)
    if msg is None:
        raise BusError(f'找不到活动任务 {task_id}；心跳未写入')
    ticket = _cli_ticket(args, task_id, agent)
    if ticket is None:
        raise BusError('心跳需要本次认领输出的 --claim-epoch 和 --claim-id；不能用其他对话的当前票据续约')
    value = bus.heartbeat(agent, msg, ticket, note=getattr(args, 'note', '') or '')
    print(json.dumps(value, ensure_ascii=False))
    return 0



def cmd_wait(args) -> int:
    """免轮询长任务值守：等待指定任务或工位交付成果，消除模型高频轮询 Token 开销。"""
    cfg = _load(args)
    bus = Bus(cfg.workspace / ".agentbus")
    target_id = getattr(args, "id", None)
    agent = getattr(args, "agent", None)
    timeout_s = max(0.0, float(getattr(args, "timeout", 120.0) or 120.0))
    poll_interval = max(0.2, float(getattr(args, "poll_interval", 1.0) or 1.0))
    notify = bool(getattr(args, "notify", False))

    if not target_id and agent:
        proc = bus.processing(agent)
        if proc:
            target_id = proc[0].stem

    start_time = time.time()
    target_desc = f"任务 {target_id}" if target_id else (f"工位 {agent} 在途任务" if agent else "全树事件")
    print(f"[等待] 开始免轮询等待值守 (目标: {target_desc}, 最长: {timeout_s}s)...", file=sys.stderr)

    while True:
        elapsed = time.time() - start_time
        # 监听针对本工位的 wake-signals 唤醒信号文件
        if agent:
            sig_file = cfg.workspace / ".agentbus" / "triggers" / "wake-signals" / f"{agent}.json"
            if sig_file.is_file():
                try:
                    from .trigger.connectors._signals import consume_signal
                    sig_data = consume_signal(cfg.workspace, agent)
                    if sig_data is None:
                        continue
                    prompt_summary = sig_data.get("prompt_summary", "")
                    print(f"[唤醒] 捕获针对工位 {agent} 的即时任务唤醒信号！")
                    if prompt_summary:
                        print(f"      指令摘要: {prompt_summary}")
                    ready_tasks = bus.pending(agent)
                    if ready_tasks:
                        print(f"      当前信箱有 {len(ready_tasks)} 条待办就绪：")
                        for rt in ready_tasks[:3]:
                            print(f"        - {rt.stem}")
                    return 0
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    print(f'[唤醒未消费] {exc}', file=sys.stderr)
                    return 1
        if not target_id and agent:
            proc = bus.processing(agent)
            if proc:
                target_id = proc[0].stem
            else:
                done_dir = bus.dirs(agent)["done"]
                if done_dir.is_dir():
                    recent = [
                        p for p in done_dir.glob("*.json")
                        if p.is_file() and p.stat().st_mtime >= start_time - 0.5
                    ]
                    if recent:
                        target_id = recent[0].stem

        if target_id:
            done_file = None
            blocked_file = None
            for w in cfg.enabled_workers():
                d_cand = bus.dirs(w)["done"] / f"{target_id}.json"
                if d_cand.is_file():
                    done_file = d_cand
                    break
                b_cand = bus.dirs(w)["blocked"] / f"{target_id}.json"
                if b_cand.is_file():
                    blocked_file = b_cand
                    break
            if done_file:
                msg = bus.read(done_file)
                if msg.status in ("failed", "blocked") or msg.exit_code not in (None, 0):
                    print(f"[失败] 任务 {target_id} [{msg.title}] 执行失败 (状态: {msg.status})：{msg.result or '无详情'}", file=sys.stderr)
                    return 1
                print(f"[完成] 任务 {target_id} [{msg.title}] 已交付完成！")
                print(f"      执行人: {msg.to} | 耗时: {msg.duration_s}s | 状态: {msg.status}")
                if notify:
                    try:
                        from .desktop_wakeup import DesktopWakeupBridge
                        bridge = DesktopWakeupBridge(cfg)
                        notice = f"AgentBus 任务完成：{target_id} [{msg.title}] 已由 {msg.to} 交付！"
                        bridge.copy_to_clipboard(notice)
                        bridge.show_toast("任务交付完成", notice)
                        bridge.play_chime()
                    except (FileNotFoundError, PermissionError, ValueError, RuntimeError):
                        pass  # 资源清理类：桌面提醒降级失败不影响 CLI 等待返回
                return 0
            if blocked_file:
                msg = bus.read(blocked_file)
                print(f"[受阻] 任务 {target_id} [{msg.title}] 已进入 blocked 状态：{msg.result or '无详情'}", file=sys.stderr)
                return 1
        elif not agent:
            from .task_tree import TaskForest
            forest = TaskForest(cfg)
            snap = forest.snapshot()
            claimable_leaves = [l for l in snap.get("leaves", []) if l.get("claimable")]
            if claimable_leaves:
                first = claimable_leaves[0]
                print(f"[就绪] 发现公共任务池中存在新就绪任务：§{first.get('section_no', '-')} [{first.get('title')}]")
                if notify:
                    try:
                        from .desktop_wakeup import DesktopWakeupBridge
                        bridge = DesktopWakeupBridge(cfg)
                        notice = f"AgentBus 就绪任务：§{first.get('section_no', '-')} [{first.get('title')}] 等待认领！"
                        bridge.copy_to_clipboard(notice)
                        bridge.show_toast("新任务就绪", notice)
                        bridge.play_chime()
                    except (FileNotFoundError, PermissionError, ValueError, RuntimeError):
                        pass  # 资源清理类：桌面提醒降级失败不影响 CLI 等待返回
                return 0

        if elapsed >= timeout_s:
            print(f"[超时] 免轮询等待已达上限 {timeout_s}s，暂未观测到目标交付事件。", file=sys.stderr)
            return 1

        time.sleep(min(poll_interval, timeout_s - elapsed))

def cmd_claim_next(args) -> int:
    """让成员从就绪任务池中自主抢单认领下一个任务（自由调度 / Autonomous Pull）。"""
    cfg = _load(args)
    agent = getattr(args, "agent", None)
    if not agent or agent not in cfg.enabled_workers():
        raise ConfigError(
            f"成员 {agent!r} 未登记在当前小组中；可选：{sorted(cfg.enabled_workers())}")

    from .task_tree import TaskForest
    forest = TaskForest(cfg)
    wait_sec = max(0.0, float(getattr(args, "wait", 0.0) or 0.0))
    start_t = time.time()

    while True:
        try:
            res = forest.claim_next(agent, tier=getattr(args, 'tier', None))
        except (FileNotFoundError, PermissionError, ValueError, KeyError, RuntimeError, BusError, ConfigError) as exc:
            print(f"[错误] 自主认领失败: {exc}", file=sys.stderr)
            return 1

        status = res.get("status")
        if status == "no_available_tasks" and wait_sec > 0:
            snapshot = forest.snapshot()
            leaves = snapshot.get("leaves", [])
            active = sum(1 for l in leaves if l.get("state") in ("running", "claimed"))
            elapsed = time.time() - start_t
            if active > 0 and elapsed < wait_sec:
                time.sleep(min(1.0, wait_sec - elapsed))
                continue
        break
    if status == "already_has_processing":
        msg = res.get("message")
        print(f"[限制] {res['detail']}", file=sys.stderr)
        if msg:
            print(f"    在途任务：{msg.id} [{msg.title}]")
            print('    请在原认领对话继续；交付/心跳使用该次认领保存的原票据或回执草稿，当前窗口不会补发凭证。')
        return 1
    elif status == "no_available_tasks":
        diag = res.get("diagnostics") or forest.diagnose_deadlock()
        if diag.get("is_deadlocked"):
            blockers = diag.get("root_blockers", [])
            print(f"\n======================================================================", file=sys.stderr)
            print(f"[死锁警告] 流水线已陷入等待死锁！当前无在途执行任务，以下根节点阻断了全树推进：", file=sys.stderr)
            print(f"======================================================================", file=sys.stderr)
            for idx, b in enumerate(blockers[:5], 1):
                sec = f"§{b['section_no']} " if b['section_no'] else ""
                print(f"  {idx}. {sec}[{b['title']}] (ID: {b['id']}, 状态: {b['state']})", file=sys.stderr)
                print(f"     阻断根因: {b['evidence_error'] or b['blocked_reason']}", file=sys.stderr)
                print(f"     影响下游: 导致 {b['blocked_downstream_count']} 个后续任务无法认领！", file=sys.stderr)
                if b.get("expected_artifacts"):
                    print(f"     期望产物: {json.dumps(b['expected_artifacts'], ensure_ascii=False)}", file=sys.stderr)
                leaf_ref = b.get('section_no') or b.get('id')
                print(f"     解阻方案 A (成果已就绪): agentbus tree pass --workspace \"{cfg.workspace}\" --leaf {leaf_ref} --reason \"管理员确认放行\"", file=sys.stderr)
                print(f"     解阻方案 B (需重做重领): agentbus tree reset --workspace \"{cfg.workspace}\" --leaf {leaf_ref} --reason \"重置超时或受阻任务重新认领\"", file=sys.stderr)
            if len(blockers) > 5:
                print(f"  ... 另有 {len(blockers) - 5} 个根阻断节点未列出", file=sys.stderr)
            print(f"======================================================================\n", file=sys.stderr)
            return 1
        else:
            snapshot = forest.snapshot()
            leaves = snapshot.get("leaves", [])
            delivered = sum(1 for l in leaves if l.get("state") == "delivered")
            active = sum(1 for l in leaves if l.get("state") in ("running", "claimed"))
            blocked = sum(1 for l in leaves if l.get("state") == "blocked")
            print(f"[待续] {res['detail']}")
            print(f"    全树概况：已交付 {delivered} 项，在途执行 {active} 项，受阻待前序解锁 {blocked} 项。")
            return 0
    elif status == "claimed":
        claimed = res["message"]
        leaf = res["leaf"] or {}
        section = leaf.get("section_no") or leaf.get("id")
        ticket = _fencing.captured_ticket(claimed).to_dict()
        wt_info = None
        if getattr(args, "worktree", False) or cfg.data.get("worktree_isolation", False):
            from .open_patterns import create_task_worktree
            wt_info = create_task_worktree(cfg.workspace, agent, claimed.id, str(section or claimed.id))
        draft_rel = _prepare_reply_draft(cfg, agent, claimed, leaf, worktree_info=wt_info)
        print(f"[ok] 已自主认领 {claimed.id} [{claimed.type}] -> processing/ "
              f"(agent={agent}, status={claimed.status})")
        print(f"    任务标题：{claimed.title}")
        if section:
            print(f"    所属章节：§{section}")
        print(f"    控制目录：{cfg.workspace}")
        print(f"    业务产出：{cfg.output_root}")
        if wt_info and wt_info.get("worktree_path"):
            print(f"    隔离沙箱：{wt_info['worktree_path']} ({wt_info.get('note', '')})")
        print(f"    任务目标：{claimed.work_brief}")
        if claimed.expected_artifacts:
            print(f"    预期产物：{json.dumps(claimed.expected_artifacts, ensure_ascii=False)}")
        print(f"    票据凭证：{json.dumps(ticket, ensure_ascii=False)}")
        if draft_rel:
            print(f"    回执草稿：{draft_rel}（已自动生成含 RepoMap 与避坑经验的 UTF-8 模板，填写后免算 SHA256 直接交付）")
        print("\n【执行与极简交付指南】")
        print(f"  1. 维持心跳：agentbus heartbeat --agent \"{agent}\" --id \"{claimed.id}\" --claim-epoch {ticket['claim_epoch']} --claim-id \"{ticket['claim_id']}\"")
        if draft_rel:
            print(f"  2. 极简交付：agentbus answer --agent \"{agent}\" --body-file \"{draft_rel}\" --auto-manifest")
            print('     （无需手动计算 --expected-body-sha256；必须明确传入本次认领生成的回执文件，不能自动读取其他对话的草稿。）')
        else:
            print(f"  2. 交付回执：agentbus answer --agent \"{agent}\" --body-file <回执.md> --auto-manifest")
        return 0
    else:
        print(f"[未知结果] {res.get('detail', '未知状态')}", file=sys.stderr)
        return 1



def cmd_claim(args) -> int:
    """让成员通过正规 CLI 入口原子认领一条 ready 消息。若带 --next 则从就绪池自主抢单。"""
    if getattr(args, "next", False):
        return cmd_claim_next(args)

    cfg = _load(args)
    agent = getattr(args, "agent", None)
    if not agent or agent not in cfg.enabled_workers():
        raise ConfigError(
            f"成员 {agent!r} 未登记在当前小组中；可选：{sorted(cfg.enabled_workers())}")

    message_id = getattr(args, "id", None)
    if message_id is not None and not _MESSAGE_ID_RE.fullmatch(message_id):
        raise ValueError("消息 ID 格式无效；只能包含字母、数字、点、下划线和短横线")

    bus = Bus(cfg.workspace / ".agentbus")
    ready = bus.pending(agent)
    if message_id is None:
        if not ready:
            from .task_assignment import assignment_mode
            mode = assignment_mode(cfg.data)
            if mode == "autonomous":
                print(f"[提示] {agent} 的信箱当前为空。系统处于自由调度模式，正在为你从公共就绪池自主抢单...")
                return cmd_claim_next(args)
            print(f"[空] {agent} 没有待处理消息可认领（可使用 'agentbus claim-next --agent {agent}' 尝试自主接活）")
            return 0
        path = ready[0]
    else:
        path = bus.dirs(agent)["ready"] / f"{message_id}.json"
        if path not in ready:
            print(f"[错误] 在 {agent} 的 ready 信箱里找不到 {message_id}",
                  file=sys.stderr)
            return 1

    claimed = bus.claim(agent, path)
    if claimed is None:
        print(f"[冲突] 消息 {path.stem} 已被其他会话认领，请重新查看 inbox",
              file=sys.stderr)
        return 1
    wt_info = None
    if getattr(args, "worktree", False) or cfg.data.get("worktree_isolation", False):
        from .open_patterns import create_task_worktree
        wt_info = create_task_worktree(cfg.workspace, agent, claimed.id, claimed.id)
    draft_rel = _prepare_reply_draft(cfg, agent, claimed, worktree_info=wt_info)
    print(f"[ok] 已认领 {claimed.id} [{claimed.type}] -> processing/ "
          f"(agent={agent}, status={claimed.status})")
    print(f"    {claimed.title}")
    if claimed.handoff_note or claimed.handoff_reason:
        print(claimed.work_brief)
    if wt_info and wt_info.get("worktree_path"):
        print(f"    隔离沙箱：{wt_info['worktree_path']}")
    if draft_rel:
        print(f"    回执草稿：{draft_rel}（填写后可执行：agentbus answer --agent {agent} --body-file \"{draft_rel}\" --auto-manifest）")
    print(json.dumps(_fencing.captured_ticket(claimed).to_dict(), ensure_ascii=False))
    return 0



def _relative_time(ts: str, now: float | None = None) -> str:
    """把 ISO 时间戳渲染成「多久以前」。解析失败一律返回占位符，绝不猜。

    只做展示，不参与任何判定；无法解析时返回 '--' 而不是伪造一个时间。
    """
    import datetime as _dt
    if not isinstance(ts, str) or not ts.strip():
        return "--"
    text = ts.strip()
    stamp = None
    try:
        stamp = _dt.datetime.fromisoformat(text).timestamp()
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                stamp = _dt.datetime.strptime(text[:19], fmt).timestamp()
                break
            except ValueError:
                continue
    if stamp is None:
        return "--"
    if now is None:
        now = _dt.datetime.now().timestamp()
    delta = max(0.0, float(now) - float(stamp))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def _brief_lines(agent: str, messages: list) -> list[str]:
    """§7.12：三行内说清「我要干什么」——待办数 + 每条一行 + 回投目标。

    纯函数，便于单测；不读盘、不写盘。
    """
    if not messages:
        return [f"{agent}：没有待处理消息。"]
    lines = [f"{agent}：{len(messages)} 条待办"]
    for m in messages:
        recipient = getattr(m, "result_to", "") or "（未指定，须先核对正文路由）"
        lines.append(
            f"  [{m.type}] {m.title} — 来自 {m.sender}，{_relative_time(m.ts)}，回投→{recipient}")
    return lines


def cmd_inbox(args) -> int:
    cfg = _load(args)
    agent = _inbox_agent(args, cfg)
    bus = Bus(cfg.workspace / ".agentbus")
    files = bus.pending(agent)
    if getattr(args, "brief", False):
        messages = [bus.read(f) for f in files]
        for line in _brief_lines(agent, messages):
            print(line)
        return 0
    if not files:
        print(f"[空] {agent} 没有待处理消息")
        return 0
    for f in files:
        m = bus.read(f)
        print(f"--- {m.id} [{m.type}] from={m.sender} hop={m.hop} attempt={m.attempt}")
        print(f"    {m.title}")
        if args.body and m.body:
            print("    " + m.body.replace("\n", "\n    "))
    return 0


def cmd_escalations(args) -> int:
    cfg = _load(args)
    esc = Bus(cfg.workspace / ".agentbus").escalations()
    if not esc:
        print("[空] 没有需要你决定的事项")
        return 0
    print(f"[!] 有 {len(esc)} 条需要你决定：")
    for message in esc:
        print(f"  · [{message.id}] {message.title}")
        if message.work_brief:
            print("    " + message.work_brief.replace("\n", "\n    "))
    return 0


def _collect_board_data(workspace: Path, me: str | None = None, warn_hours: float = 48.0) -> dict:
    from collections import Counter
    from datetime import datetime, timezone
    agents_dir = workspace / ".agentbus/agents"
    if not agents_dir.is_dir():
        agents_dir = workspace / "agents"

    now_dt = datetime.now(timezone.utc)
    members_data = []
    total_ready = 0
    total_proc = 0
    total_done = 0
    total_blocked = 0
    stale_count = 0

    agent_names = sorted(d.name for d in agents_dir.iterdir() if d.is_dir()) if agents_dir.is_dir() else []
    if me:
        agent_names = [a for a in agent_names if a == me]

    for name in agent_names:
        base = agents_dir / name
        ready_dir = base / "ready"
        proc_dir = base / "processing"
        done_dir = base / "done"
        blk_dir = base / "blocked"

        ready_files = list(ready_dir.glob("*.json")) if ready_dir.is_dir() else []
        proc_files = list(proc_dir.glob("*.json")) if proc_dir.is_dir() else []
        done_files = list(done_dir.glob("*.json")) if done_dir.is_dir() else []
        blk_files = list(blk_dir.glob("*.json")) if blk_dir.is_dir() else []

        type_counter = Counter()
        ready_msgs = []
        oldest_age_hours = 0.0
        oldest_ts = None
        has_stale = False

        for f in ready_files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                data = {"type": "corrupt", "sender": "unknown"}

            m_id = data.get("id", f.stem)
            m_type = data.get("type", "unknown")
            sender = data.get("sender", "unknown")
            ts = data.get("ts")
            title = data.get("title", "")
            result_to = data.get("result_to", "")

            type_counter[m_type] += 1
            age_hours = 0.0
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_hours = max(0.0, (now_dt - dt).total_seconds() / 3600.0)
                    if age_hours > oldest_age_hours:
                        oldest_age_hours = age_hours
                        oldest_ts = ts
                except Exception:
                    pass  # 忽略格式异常的时间戳，按默认账本时间降级处理

            is_stale = age_hours >= warn_hours
            if is_stale:
                has_stale = True
                stale_count += 1

            ready_msgs.append({
                "id": m_id,
                "type": m_type,
                "sender": sender,
                "title": title,
                "result_to": result_to,
                "ts": ts,
                "age_hours": round(age_hours, 1),
                "age_days": round(age_hours / 24.0, 1),
                "is_stale": is_stale,
            })

        ready_count = len(ready_files)
        proc_count = len(proc_files)
        done_count = len(done_files)
        blk_count = len(blk_files)

        total_ready += ready_count
        total_proc += proc_count
        total_done += done_count
        total_blocked += blk_count

        if has_stale:
            status_flag = f"[积压 {round(oldest_age_hours / 24.0, 1)}d]"
            status_code = "BACKLOG"
        elif proc_count > 0:
            status_flag = "[处理中]"
            status_code = "PROCESSING"
        elif ready_count > 0:
            status_flag = "[待办]"
            status_code = "PENDING"
        elif blk_count > 0:
            status_flag = "[阻塞]"
            status_code = "BLOCKED"
        else:
            status_flag = "[空闲]"
            status_code = "IDLE"

        members_data.append({
            "member": name,
            "status_code": status_code,
            "status_flag": status_flag,
            "ready_count": ready_count,
            "processing_count": proc_count,
            "blocked_count": blk_count,
            "done_count": done_count,
            "oldest_age_hours": round(oldest_age_hours, 1),
            "oldest_age_days": round(oldest_age_hours / 24.0, 1),
            "oldest_ts": oldest_ts,
            "by_type": dict(type_counter),
            "messages": ready_msgs,
        })

    return {
        "schema_version": "1.0.0",
        "audited_at": now_dt.isoformat(),
        "workspace": str(workspace),
        "target_filter": me,
        "warn_hours": warn_hours,
        "totals": {
            "members": len(members_data),
            "ready": total_ready,
            "processing": total_proc,
            "blocked": total_blocked,
            "done": total_done,
            "stale": stale_count,
        },
        "members": members_data,
    }


def cmd_board(args) -> int:
    """§7.11：一屏待办看板——一条命令输出全组状态，标记超期积压，支持 --me 与 --json。"""
    cfg = _load(args)
    warn_hours = getattr(args, "warn_hours", 48.0)
    me = getattr(args, "me", None)
    data = _collect_board_data(cfg.workspace, me=me, warn_hours=warn_hours)

    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    totals = data["totals"]
    members = data["members"]

    if me:
        if not members:
            print(f"[看板] 未找到成员 [{me}] 的信箱目录")
            return 1
        m = members[0]
        types_str = ", ".join(f"{k}:{v}" for k, v in m["by_type"].items()) or "无待办"
        print("=" * 68)
        print(f" AgentBus 成员待办看板：[{m['member']}]")
        print("=" * 68)
        print(f"待办: {m['ready_count']} | 处理中: {m['processing_count']} | 阻塞: {m['blocked_count']} | 已归档: {m['done_count']} | 状态: {m['status_flag']}")
        print(f"待办类型细分: {types_str}")
        if m["messages"]:
            print("-" * 68)
            print("【待办消息明细】:")
            for item in m["messages"][:15]:
                ret = f" -> 回投 {item['result_to']}" if item["result_to"] else ""
                stale_mark = " [⚠️ 超期积压]" if item["is_stale"] else ""
                print(f"  - [{item['type']}] {item['title'] or '(无标题)'} ({item['sender']}, {_relative_time(item['ts'])}){ret}{stale_mark}")
            if len(m["messages"]) > 15:
                print(f"  ... 另有 {len(m['messages']) - 15} 条更早待办")
        print("=" * 68)
        return 0

    print("=" * 78)
    print(" AgentBus 全组一屏待办看板 (agentbus board)")
    print("=" * 78)
    print(f"工作空间: {data['workspace']}")
    print(f"全网汇总: ready={totals['ready']}, proc={totals['processing']}, blk={totals['blocked']}, done={totals['done']} | 积压告警(>{warn_hours:.0f}h): {totals['stale']} 条")
    print("-" * 78)
    print(f"{'成员代号':14} | {'状态':10} | {'待办':4} | {'处理':4} | {'阻塞':4} | {'最老待办':8} | {'待办类型分解'}")
    print("-" * 78)
    for m in members:
        types_str = ", ".join(f"{k}:{v}" for k, v in m["by_type"].items()) or "(空闲)"
        oldest_str = f"{m['oldest_age_days']} 天" if m["ready_count"] > 0 else "--"
        print(f"{m['member']:14} | {m['status_flag']:10} | {m['ready_count']:4} | {m['processing_count']:4} | {m['blocked_count']:4} | {oldest_str:8} | {types_str}")
    print("-" * 78)
    print("提示: 可使用 `agentbus board --me <成员名>` 聚焦个人待办；使用 `--json` 输出脚本接口。")
    print("=" * 78)
    return 0


def cmd_handoff(args) -> int:
    """把待人工接手的任务渲染成「可以直接粘贴到 GUI agent 里」的 prompt。"""
    cfg = _load(args)
    bus = Bus(cfg.workspace / ".agentbus")
    targets = [args.agent] if args.agent else sorted(cfg.human_workers())
    total = 0
    for name in targets:
        if name not in cfg.enabled_workers():
            continue
        for f in bus.pending(name):
            m = bus.read(f)
            total += 1
            answer_argv = [sys.executable]
            if getattr(sys, "frozen", False):
                answer_argv.append("answer")
            else:
                entrypoint = Path(__file__).resolve().parent.parent / "entry.py"
                answer_argv.extend([str(entrypoint), "answer"])
            receipt_path = f"成果/任务书维护/receipts/{name}_{m.id}_RECEIPT.md"
            answer_argv.extend([
                "--workspace", str(cfg.workspace), "--agent", name,
                "--id", m.id,
                "--claim-epoch", "<claim_epoch>",
                "--claim-id", "<claim_id>",
                "--body-file", receipt_path,
                "--expected-body-sha256", "<receipt_sha256>",
            ])
            answer_command = "& " + subprocess.list2cmdline(answer_argv)
            print("=" * 68)
            print(f"给 {name} 的任务  [{m.id}]  {m.title}")
            print(f"结果收件人：{m.result_to or '旧消息，须先核对正文路由'}")
            print(f"（把下面整段粘给它；做完后用 answer 命令把结果回投）")
            print("-" * 68)
            print(f"""你正在工作空间：{cfg.workspace}

【任务】{m.title}

{m.work_brief}

【完成后请输出】一段结论：做了什么、产物在哪（给出文件路径）、有没有没验证的部分。

【Windows 编码防损回执要求 (§4.8)】
不要使用管道将中文正文传给 CLI（PowerShell 默认管道编码会把中文变为问号）。
请将回执正文保存为 UTF-8 文件（如 `{receipt_path}`），计算其原始 SHA256，然后使用 `--body-file` 正式回投：
{answer_command}""")
            print("-" * 68)
            print("上面的交接提示已包含回执命令与明确结果收件人；不要直接编辑或移动 .agentbus 消息文件。")
            print()
    if total == 0:
        print("[空] 没有待人工接手的任务")
    return 0


def cmd_answer(args) -> int:
    """人工在 GUI 里干完活后，把结果回投给主体，让流程继续。"""
    if not getattr(args, "agent", None) and getattr(args, "caller", None):
        args.agent = args.caller
    cfg = _load(args)
    bus = Bus(cfg.workspace / ".agentbus")
    inferred_task_id = getattr(args, "id", None) is None
    if inferred_task_id:
        proc = bus.processing(args.agent)
        if len(proc) == 1:
            args.id = proc[0].stem
        elif not proc:
            print(f"[错误] 工位 {args.agent} 当前没有在途任务可提交回执", file=sys.stderr)
            return 1
        else:
            print(f"[错误] 工位 {args.agent} 当前有多个在途任务，请使用 --id 明确指定", file=sys.stderr)
            return 1
    args.body = _receipt_text(args, cfg, 'body')
    src = None
    # Source: Codex dual-directory verification, 2026-10-07. 公共契约是
    # "先 agentbus claim 认领，再用 answer --body-file 回执"；claim 会把
    # 任务原子移进 processing/。以前这里只搜 ready/ 和 blocked/，导致
    # 认领之后永远找不到源任务，文件路线在真实新项目上直接断掉。
    for sub in ("processing", "ready", "blocked"):
        candidate = bus.dirs(args.agent)[sub] / f"{args.id}.json"
        if candidate.exists():
            src = candidate
            break
    if src is None:
        # 也可能已经被处理过
        done = bus.dirs(args.agent)["done"] / f"{args.id}.json"
        if not done.exists():
            print(f"[错误] 在 {args.agent} 的信箱里找不到 {args.id}", file=sys.stderr)
            return 1
        print(f"[提示] {args.id} 已经处理过了")
        return 1

    leases = LeaseManager(bus.root)
    lease = leases.acquire(f"agentbus/report/{args.id}", args.agent, ttl_s=300,
                           note="serialize manual result receipt", msg_id=args.id)
    if lease is None:
        print(f"[提示] 任务 {args.id} 的回投正在处理中，请稍后重试。", file=sys.stderr)
        return 2
    try:
        with task_lock(bus.root, args.agent, args.id):
            return _answer_locked(args, cfg, bus, src, inferred_task_id=inferred_task_id)
    finally:
        leases.release(lease)



def cmd_retire(args) -> int:
    """Retire one stopped expired claim; preserve history and never requeue."""
    from .retirement import retire_stopped_claim
    cfg = _load(args)
    if args.agent not in cfg.data.get('workers', {}):
        raise BusError('退役成员未登记在当前项目')
    message = retire_stopped_claim(Bus(cfg.workspace / '.agentbus'), args.agent, args.id,
        expected_claim_epoch=args.claim_epoch,
        owner_confirmed_inactive=args.owner_confirmed_inactive, reason=args.reason)
    print(json.dumps({'id':message.id, 'agent':message.to, 'status':message.status,
        'retired':True, 'delivered':False, 'new_work_started':False,
        'notice':'旧认领已退役，期限与证据保留；后续工作需明确创建关联新作业。'},ensure_ascii=False))
    return 0


def cmd_recover(args) -> int:
    """Explicit single-message recovery; never an age-based sweep."""
    cfg = _load(args)
    if args.agent not in cfg.data.get('workers', {}):
        raise BusError('恢复成员未登记在当前小组配置中')
    msg = Bus(cfg.workspace / '.agentbus').recover_claim(
        args.agent, args.id, owner_confirmed_inactive=args.owner_confirmed_inactive)
    print(json.dumps({'id': msg.id, 'agent': msg.to, 'status': msg.status,
                      'attempt': msg.attempt, 'budget_s': msg.budget_s,
                      'deadline_ts': msg.deadline_ts,
                      'owner_confirmed_inactive': True,
                      'confirmation_source': 'caller_assertion',
                      'notice': '已恢复或此前同一恢复已完成；不验证进程存活，不重置预算'},
                     ensure_ascii=False))
    return 0


def _answer_locked(args, cfg, bus: Bus, src: Path, *, inferred_task_id: bool = False) -> int:
    auto_claimed_ticket = None
    if src.parent.name == "ready":
        if _fencing.record_epoch(bus.read(src)) > 0:
            raise BusError('该任务已回收；请先重新认领并执行，不能用旧回执自动接管')
        claimed = bus.claim(args.agent, src)
        if claimed is not None:
            src = bus.dirs(args.agent)["processing"] / src.name
            auto_claimed_ticket = _fencing.captured_ticket(claimed)

    m = bus.read(src)
    ticket = _cli_ticket(args, m.id, args.agent) or auto_claimed_ticket
    if ticket is None:
        if _fencing.record_epoch(m) > 0:
            raise BusError('在途任务必须提供原认领输出的 --claim-epoch 与 --claim-id，或使用含原票据的已填写回执草稿；未发送回执')
        # Keep the documented no-ID/single-current-task convenience, and the
        # pre-fencing legacy route. Never replace an explicitly named task's
        # missing original ticket with the current disk ticket.
        ticket = _fencing.captured_ticket(m)

    bus.check_claim(args.agent, m, ticket, evidence={'body': args.body,
                    'artifacts_file': getattr(args, 'artifacts_file', None),
                    'auto_manifest': getattr(args, 'auto_manifest', False)})
    failed = bool(getattr(args, "failed", False))
    outcome_code = 1 if failed else 0
    participants = [*cfg.enabled_workers(), HUMAN_INBOX]
    override = getattr(args, "result_to", None)
    if override:
        try:
            recipient = resolve_result_to_arg(
                override, sender=m.sender, director=cfg.director,
                participants=participants)
            m.result_to = recipient
            route_source = "field"
            route_error = ""
        except ValueError as exc:
            recipient = None
            route_source = "invalid"
            route_error = str(exc)
    else:
        decision = resolve_message_result_to(m, participants)
        recipient = decision.recipient
        route_source = decision.source
        route_error = decision.error
    if recipient is None:
        m.result = args.body
        m.exit_code = outcome_code
        m.duration_s = 0.0
        reason = route_error or route_source or "正文没有唯一结果收件人"
        bus.block(args.agent, m, f"result route requires clarification: {reason}", ticket=ticket)
        director = Director(cfg, bus=bus)
        try:
            escalation = director.escalate_for_human(
                m, f"结果路由需要澄清：{reason}")
        except Exception as exc:  # noqa: BLE001
            bus.audit(event="escalation_failed", id=m.id, reason=str(exc))
            print(f"[待澄清] 结果已保存在 blocked/，但人工升级失败：{exc}",
                  file=sys.stderr)
            return 1
        director.schedule_peer_review(m, args.agent, "blocked")
        print(f"[待澄清] 结果已保存在 blocked/ 并升级给人 [{escalation.id}]；"
              "没有猜测收件人。", file=sys.stderr)
        return 1

    entries = []
    if getattr(args, 'artifacts_file', None):
        entries = _manifest_json(cfg, args.artifacts_file)
    elif getattr(args, 'auto_manifest', False) or getattr(args, 'artifacts', None):
        target_paths = []
        if getattr(args, 'artifacts', None):
            target_paths = [p.strip() for p in str(args.artifacts).split(',') if p.strip()]
        elif getattr(m, 'expected_artifacts', None):
            target_paths = list(m.expected_artifacts)
        from .open_patterns import sync_and_cleanup_worktree
        wt_sync = sync_and_cleanup_worktree(cfg.workspace, m.id, artifact_paths=target_paths, cleanup=False)
        if wt_sync.get("synced") and wt_sync.get("copied_files"):
            print(f"     [Worktree] 已从隔离沙箱同步 {len(wt_sync['copied_files'])} 个产物文件回主工作区")
        if not target_paths:
            print("     [提示] 任务没有声明预期产物，本次交付清单为空；若有实际产物文件，"
                  "请改用 --artifacts \"相对路径1,相对路径2\" 一并登记。", file=sys.stderr)
        out_root = cfg.output_root
        for rel in target_paths:
            rel = _normalize_artifact_path(rel)
            fpath = out_root / rel
            if not fpath.is_file() and (cfg.workspace / rel).is_file():
                fpath = cfg.workspace / rel
            if not fpath.is_file():
                raise ValueError(f"auto-manifest: 声明的产物文件不存在：{rel}")
            entries.append({
                'path': rel,
                'bytes': fpath.stat().st_size,
                'sha256': hashlib.sha256(fpath.read_bytes()).hexdigest()
            })
    existing_results = bus.find_results(m.id, sender=args.agent)
    cited = [item for item in existing_results if item.id in args.body]
    existing = next((item for item in cited
                     if item.to.casefold() == recipient.casefold()), None)
    if existing is None:
        existing = next((item for item in existing_results
                         if item.to.casefold() == recipient.casefold()
                         and item.body == args.body), None)
    director = Director(cfg, bus=bus)
    if existing is not None:
        _check_receipt_reuse(cfg, m, existing, entries if (getattr(args, 'artifacts_file', None) or entries) else None, failed)
        m.result = existing.body
        m.exit_code = existing.exit_code
        final_status = "failed" if existing.exit_code not in (None, 0) else "done"
        bus.finish(args.agent, m, final_status, ticket=ticket)
        if final_status == "done":
            director.resolve_pending_escalations(m.id, existing.id)
        director.schedule_peer_review(m, args.agent, final_status)
        print(f"[提示] 已复用既有回执 {existing.id} → {existing.to}；没有重复投递。")
        return 0

    if existing_results:
        ids = ", ".join(item.id for item in existing_results)
        m.result = args.body
        m.exit_code = outcome_code
        m.duration_s = 0.0
        reason = f"已有不同回执（{ids}），拒绝创建第二份"
        bus.block(args.agent, m, f"duplicate result rejected: {reason}", ticket=ticket)
        try:
            escalation = director.escalate_for_human(
                m, f"{reason}。新内容已保存在 blocked/，请人工裁决。")
        except Exception as exc:  # noqa: BLE001
            bus.audit(event="escalation_failed", id=m.id, reason=str(exc))
            print(f"[待裁决] {reason}；人工升级失败：{exc}", file=sys.stderr)
            return 1
        director.schedule_peer_review(m, args.agent, "blocked")
        print(f"[待裁决] {reason}。任务已移入 blocked/ 并升级给人 "
              f"[{escalation.id}]；没有重复投递。", file=sys.stderr)
        return 1

    entries = _result_artifacts(cfg, m, entries, failed=failed)
    if not failed and getattr(args, "skip_verify", False):
        bus.audit(event="pre_answer_verification_skipped", id=m.id, agent=args.agent,
                  reason="explicit_cli_flag", checks_executed=False,
                  scope="static_syntax_and_configured_pre_answer_command")
        print("[说明] 已按 --skip-verify 跳过交单前语法及指定命令检查，这些检查未执行。"
              "原票据、排他锁、产物核验与独立审核仍适用。")
    if not failed and not getattr(args, "skip_verify", False):
        from .open_patterns import run_pre_answer_gate
        art_rel_list = [e["path"] for e in (entries or []) if isinstance(e, dict) and e.get("path")]
        verify_cmd = getattr(args, "verify_cmd", None)
        if art_rel_list or verify_cmd:
            gate_res = run_pre_answer_gate(cfg.output_root, art_rel_list, verify_cmd=verify_cmd)
            if not gate_res["passed"]:
                fail_lines = [f"  - [{f['kind']}] {f['path']}: {f['detail']}" for f in gate_res["failures"]]
                raise ValueError(
                    "交单前静态语法与验证门禁 (Pre-Answer Gate) 拦截了带病产物：\n"
                    + "\n".join(fail_lines)
                    + "\n请立即修复上述错误后重新执行 answer（如确需跳过门禁可传 --skip-verify）。"
                )
    bus.record_submission(args.agent, m, ticket, origin="CLI 回执",
                          heartbeat_timeout_s=cfg.gates.get("heartbeat_timeout_s", 600))
    reply = bus.make(sender=args.agent, to=recipient, type="result",
                     title=f"[人工结果] {m.title}", body=args.body,
                     reply_to=m.id, hop=m.hop, attempt=m.attempt,
                     job_id=m.job_id, plan_id=m.plan_id, plan_sha256=m.plan_sha256,
                     artifacts=entries,
                     result_to=recipient,
                     routed_by_fallback=("body" if route_source == "body" else None))
    # This is the peer's manually declared outcome, not a subprocess exit code.
    reply.exit_code = outcome_code
    reply = bus.commit_result(args.agent, m, ticket, body=args.body,
                              result_to=recipient, artifacts=entries, exit_code=outcome_code,
                              artifact_root=cfg.output_root,
                              routed_by_fallback='body' if route_source == 'body' else None)
    if reply.awaiting_audit:
        print(f"[待审核] 成果已提交（{reply.id}），独立审核后才会送达 {recipient}。")
    else:
        if not failed:
            director.resolve_pending_escalations(m.id, reply.id)
        director.schedule_peer_review(m, args.agent, "failed" if failed else "done")
        print(f"[ok] 已回投结果给 {recipient}（消息 {reply.id}）")
    if route_source == "body":
        print("     注：按限期正文兼容路由，已记录 routed_by_fallback=body")
    tokens_used = getattr(args, "tokens", None)
    cost = getattr(args, "cost", None)
    if tokens_used is not None or cost is not None:
        try:
            from .task_tree import TaskForest
            forest = TaskForest(cfg)
            with task_lock(forest.root, "task-forest", "structure"):
                tree_struct = forest._structure()
                updated = False
                for leaf in tree_struct.get("leaves", []):
                    if leaf.get("task_id") == m.id or leaf.get("id") == m.id:
                        if tokens_used is not None:
                            leaf["tokens_consumed"] = int(tokens_used)
                        if cost is not None:
                            leaf["cost"] = float(cost)
                        updated = True
                        break
                if updated:
                    forest._write(forest._normalize(tree_struct))
        except (FileNotFoundError, PermissionError, ValueError, KeyError, RuntimeError):
            pass  # 资源清理类：Token/成本回写可选扩展失败不阻断正式交付回执
        stat_msg = []
        if tokens_used is not None:
            stat_msg.append(f"消耗 Token: {tokens_used}")
        if cost is not None:
            stat_msg.append(f"成本: ￥{cost}")
        print(f"     [核算] {' | '.join(stat_msg)}")
    lesson_txt = getattr(args, "lesson", None)
    if lesson_txt and str(lesson_txt).strip():
        from .open_patterns import record_lesson
        rec = record_lesson(
            cfg.workspace,
            args.agent,
            str(lesson_txt).strip(),
            task_id=m.id,
            scope_paths=[e["path"] for e in (entries or []) if isinstance(e, dict) and e.get("path")],
        )
        print(f"     [经验沉淀] 已写入 .agentbus/lessons.jsonl ({rec['id']})")
    return 0


def cmd_discover(args) -> int:
    """Beads 风格任务衍生：在执行当前任务时发现新问题，动态在同分支挂载带 discovered_from 溯源的子任务。"""
    cfg = _load(args)
    from .open_patterns import discover_subtask
    arts = [p.strip() for p in str(getattr(args, "artifacts", "") or "").split(",") if p.strip()]
    res = discover_subtask(
        cfg=cfg,
        agent=getattr(args, "agent", None) or cfg.director,
        title=args.title,
        goal=getattr(args, "goal", "") or "",
        from_task_id=getattr(args, "from_task", None),
        expected_artifacts=arts,
    )
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"[ok] 已衍生新待办任务 §{res['section_no']} [{res['leaf_id']}]：{args.title}")
        print(f"     溯源链: discovered_from={res['discovered_from']} (发现人: {res['discovered_by']})")
    return 0


def cmd_repomap(args) -> int:
    """Aider 风格 AST 代码结构地图生成。"""
    cfg = _load(args)
    from .open_patterns import build_repomap_slice
    paths = [p.strip() for p in str(getattr(args, "paths", "") or "").split(",") if p.strip()]
    kws = [k.strip() for k in str(getattr(args, "query", "") or "").split(",") if k.strip()]
    res = build_repomap_slice(cfg.workspace, target_paths=paths, keywords=kws, max_files=int(getattr(args, "max_files", 8) or 8))
    if getattr(args, "json", False):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(res["markdown"] or "[空] 未匹配到包含符号定义的代码文件")
    return 0


def cmd_lessons(args) -> int:
    """Reflexion / OpenHands 风格实战避坑经验库管理。"""
    cfg = _load(args)
    from .open_patterns import record_lesson, list_lessons, query_relevant_lessons
    action = getattr(args, "action", "list") or "list"
    if action == "add":
        if not getattr(args, "lesson", None):
            raise ValueError("请通过 --lesson 提供要记录的实战避坑经验正文")
        paths = [p.strip() for p in str(getattr(args, "paths", "") or "").split(",") if p.strip()]
        item = record_lesson(
            cfg.workspace,
            agent=getattr(args, "agent", None) or cfg.director,
            lesson=args.lesson,
            task_id=getattr(args, "task_id", "") or "",
            scope_paths=paths,
        )
        if getattr(args, "json", False):
            print(json.dumps(item, ensure_ascii=False, indent=2))
        else:
            print(f"[ok] 已沉淀实战经验 {item['id']}：{item['lesson']}")
        return 0

    paths = [p.strip() for p in str(getattr(args, "paths", "") or "").split(",") if p.strip()]
    kws = [k.strip() for k in str(getattr(args, "query", "") or "").split(",") if k.strip()]
    if paths or kws:
        items = query_relevant_lessons(cfg.workspace, target_paths=paths, keywords=kws, limit=20)
    else:
        items = list_lessons(cfg.workspace, limit=30)
    if getattr(args, "json", False):
        print(json.dumps({"count": len(items), "lessons": items}, ensure_ascii=False, indent=2))
    else:
        if not items:
            print("[空] 当前尚未沉淀避坑经验（可在交单时加 --lesson \"...\" 或使用 agentbus lessons add 录入）")
            return 0
        print(f"=== 团队实战避坑经验库 (共 {len(items)} 条) ===")
        for it in items:
            scopes = f" [范围: {', '.join(it.get('scope_paths', []))}]" if it.get("scope_paths") else ""
            print(f"  - [{it.get('agent', 'team')}] {it.get('lesson')}{scopes}")
    return 0


def cmd_worktree(args) -> int:
    """Git Worktree 并发物理沙箱隔离管理。"""
    cfg = _load(args)
    from .open_patterns import create_task_worktree, sync_and_cleanup_worktree, list_worktrees
    action = getattr(args, "action", "list") or "list"
    if action == "create":
        agent = getattr(args, "agent", None) or cfg.director
        task_id = getattr(args, "id", None) or f"wt-{int(time.time())}"
        res = create_task_worktree(cfg.workspace, agent, task_id, task_id)
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"[ok] 已创建隔离沙箱: {res['worktree_path']} ({res.get('note', '')})")
        return 0
    if action == "sync":
        task_id = getattr(args, "id", None)
        if not task_id:
            raise ValueError("worktree sync 需要通过 --id 指定任务 ID")
        res = sync_and_cleanup_worktree(cfg.workspace, task_id, cleanup=not getattr(args, "keep", False))
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"[ok] 沙箱同步结果: synced={res['synced']}, 同步文件数={len(res.get('copied_files', []))}")
        return 0

    items = list_worktrees(cfg.workspace)
    if getattr(args, "json", False):
        print(json.dumps({"count": len(items), "worktrees": items}, ensure_ascii=False, indent=2))
    else:
        if not items:
            print("[空] 当前无已登记的任务隔离沙箱（认领时加 --worktree 可自动创建）")
            return 0
        print(f"=== 任务并发隔离沙箱列表 (共 {len(items)} 个) ===")
        for it in items:
            print(f"  - [{it.get('status')}] task={it.get('task_id')} agent={it.get('agent')} path={it.get('worktree_path')}")
    return 0


def desktop_app_processes() -> list[str]:
    """检测 Codex 桌面版是否在跑。

    它会和无头 codex exec 抢同一套 code-mode 命名管道，表现是执行工具时报
    「所有的管道范例都在使用中」(Windows 错误 231)，而模型对话却完全正常，
    非常难查。所以在 doctor 里主动提示。
    """
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=30, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    names = []
    for line in (out.stdout or "").splitlines():
        if not line.strip():
            continue
        name = line.split(",")[0].strip('"').lower()
        if name in ("chatgpt.exe", "codex-windows-sandbox-service.exe"):
            names.append(name)
    return names


def _formal_executable(ws: Path) -> Path | None:
    """The registered release program for this project, if any.

    Ordinary user projects run AgentBus from wherever it is installed, so a
    missing project-local EXE is not an error. Prefer the workflow launcher
    record; fall back to the developer layout ``成果/AgentBus.exe``.
    """
    wf_p = ws / ".agentbus" / "workflow.json"
    try:
        launcher = json.loads(wf_p.read_text(encoding="utf-8")).get("launcher") or {}
    except (OSError, ValueError, AttributeError):
        launcher = {}
    exe = launcher.get("executable") if isinstance(launcher, dict) else None
    if isinstance(exe, str) and exe.strip() and launcher.get("mode", "frozen") == "frozen":
        candidate = Path(exe)
        if not candidate.is_absolute():
            candidate = ws / candidate
        if candidate.is_file():
            return candidate
    legacy = ws / "成果" / "AgentBus.exe"
    return legacy if legacy.is_file() else None


def verify_all_metrics(ws: Path) -> dict:
    """Collect project-wide health metrics (Section 7.13 verify --all)."""
    start_t = time.perf_counter()
    metrics = {
        "schema_version": "1.0.0",
        "workspace": str(ws),
        "package_integrity": {},
        "cli_performance": {},
        "stop_state": {},
        "inflight_backlog": {},
        "recent_delivery_evidence": [],
        "unaccepted_summary": {}
    }

    # 1. Package integrity
    exe_p = _formal_executable(ws) or (ws / "成果" / "AgentBus.exe")
    wf_p = ws / ".agentbus" / "workflow.json"
    manifest_p = ws / "packaging" / "BUILD-MANIFEST.json"

    exe_sha = ""
    exe_bytes = 0
    if exe_p.is_file():
        try:
            data = exe_p.read_bytes()
            exe_bytes = len(data)
            exe_sha = hashlib.sha256(data).hexdigest()
        except Exception:
            pass  # 文件被占用或不可读时降级

    wf_sha = ""
    if wf_p.is_file():
        try:
            wf_data = json.loads(wf_p.read_text(encoding='utf-8'))
            wf_sha = wf_data.get('launcher', {}).get('sha256', '')
        except Exception:
            pass  # workflow.json 损坏或格式异常时降级

    metrics["package_integrity"] = {
        "executable_exists": exe_p.is_file(),
        "executable_bytes": exe_bytes,
        "executable_sha256": exe_sha,
        "workflow_sha256": wf_sha,
        "is_aligned": bool(exe_sha and exe_sha == wf_sha),
        "manifest_exists": manifest_p.is_file()
    }

    # 2. Stop state
    state_file = ws / ".agentbus" / "state.json"
    is_stopped = False
    stop_note = ""
    if state_file.is_file():
        try:
            sd = json.loads(state_file.read_text(encoding='utf-8'))
            is_stopped = sd.get('is_stopped', False) or sd.get('stopped', False)
            stop_note = sd.get('stop_reason', '')
        except Exception:
            pass  # state.json 损坏或格式异常时降级
    metrics["stop_state"] = {
        "is_stopped": is_stopped,
        "stop_note": stop_note
    }

    # 3. Inflight backlog
    agents_dir = ws / ".agentbus" / "agents"
    ready_count = 0
    proc_count = 0
    if agents_dir.is_dir():
        for ad in agents_dir.iterdir():
            if ad.is_dir():
                ready_files = list((ad / "ready").glob("*.json")) if (ad / "ready").is_dir() else []
                proc_files = list((ad / "processing").glob("*.json")) if (ad / "processing").is_dir() else []
                ready_count += len(ready_files)
                proc_count += len(proc_files)
    metrics["inflight_backlog"] = {
        "total_ready": ready_count,
        "total_processing": proc_count
    }

    # 4. Task forest unaccepted summary
    try:
        from .task_tree import TaskForest
        cfg = load(ws, create=False)
        forest = TaskForest(cfg)
        snap = forest.snapshot()
        leaves = snap.get('leaves', [])
        delivered = [l for l in leaves if l.get('state') == 'delivered']
        unaccepted = [l for l in leaves if l.get('state') != 'delivered']
        metrics["unaccepted_summary"] = {
            "total_leaves": len(leaves),
            "delivered_count": len(delivered),
            "unaccepted_count": len(unaccepted),
            "queued_count": len([l for l in unaccepted if l.get('state') == 'queued']),
            "claimed_count": len([l for l in unaccepted if l.get('state') == 'claimed']),
            "running_count": len([l for l in unaccepted if l.get('state') == 'running']),
            "blocked_count": len([l for l in unaccepted if l.get('state') == 'blocked'])
        }
        # Recent deliveries (last 5 delivered)
        metrics["recent_delivery_evidence"] = [
            {
                "id": l.get('id'),
                "title": l.get('title'),
                "section": l.get('section_no'),
                "result_id": l.get('result_id'),
                "has_artifacts": len(l.get('artifacts', [])) > 0
            }
            for l in delivered[-5:]
        ]
    except Exception as exc:
        # Report, never hide: a corrupt tree must surface in the health check.
        metrics["task_tree_error"] = f"{type(exc).__name__}: {exc}"[:500]

    # Performance
    elapsed_ms = round((time.perf_counter() - start_t) * 1000, 2)
    metrics["cli_performance"] = {
        "scan_latency_ms": elapsed_ms,
        "health": "EXCELLENT" if elapsed_ms < 500 else "DEGRADED"
    }

    return metrics


def cmd_verify(args) -> int:
    if not getattr(args, 'all', False):
        if getattr(args, 'task_id', None) or getattr(args, 'job_id', None):
            return cmd_evidence(args)
        print("请指定 --all 进行项目级全景体检，或指定 --task-id / --job-id 检查单项证据", file=sys.stderr)
        return 1

    ws = Path(getattr(args, 'workspace', None) or '.').resolve()
    metrics = verify_all_metrics(ws)
    tree_error = metrics.get("task_tree_error")
    if getattr(args, 'json', False) or getattr(args, 'format', None) == 'json' and any(a in sys.argv for a in ('--json', 'json')):
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 1 if tree_error else 0

    print(f"=== AgentBus 全景体检报告 (§7.13) ===")
    print(f"工作空间: {metrics['workspace']}")
    print(f"扫描耗时: {metrics['cli_performance']['scan_latency_ms']} ms [{metrics['cli_performance']['health']}]")

    pkg = metrics["package_integrity"]
    print("\n[1. 出厂包摘要一致性]")
    print(f"  正式程序存在: {'✓' if pkg['executable_exists'] else '✗'} ({pkg['executable_bytes']:,} bytes)")
    print(f"  程序 SHA256 : {pkg['executable_sha256'] or '未找到'}")
    print(f"  清单对齐状态: {'✓ 一致对齐' if pkg['is_aligned'] else '⚠ 哈希不一致或未登记'}")

    stop = metrics["stop_state"]
    print("\n[2. 系统停机状态 (STOP Guard)]")
    if stop["is_stopped"]:
        print(f"  ⚠ 当前处于暂停态: {stop['stop_note']}")
    else:
        print("  ✓ 正常运行中 (未触发全局暂停)")

    backlog = metrics["inflight_backlog"]
    print("\n[3. 在途任务与信箱积压]")
    print(f"  待处理 (ready)     : {backlog['total_ready']}")
    print(f"  进行中 (processing): {backlog['total_processing']}")

    tree = metrics.get("unaccepted_summary", {})
    if tree_error:
        print("\n[4. 任务全景树验收状态]")
        print(f"  ✗ 任务树无法读取：{tree_error}")
        print("    [下一步] 运行 agentbus tree status 查看原因；可用 agentbus tree restore 从备份恢复。")
    if tree:
        print("\n[4. 任务全景树验收状态]")
        print(f"  总叶子任务: {tree.get('total_leaves', 0)}")
        print(f"  已交付    : {tree.get('delivered_count', 0)}")
        print(f"  待验收/在途: {tree.get('unaccepted_count', 0)} (排队: {tree.get('queued_count', 0)}, 已认领: {tree.get('claimed_count', 0)}, 运行中: {tree.get('running_count', 0)}, 阻塞: {tree.get('blocked_count', 0)})")

    recent = metrics.get("recent_delivery_evidence", [])
    if recent:
        print("\n[5. 最近交付证据快照 (最新 5 项)]")
        for item in recent:
            art_mark = "有产物清单" if item.get('has_artifacts') else "无产物清单"
            print(f"  - [{item.get('section', '')}] {item.get('id', '')}: {item.get('title', '')} ({art_mark})")

    return 1 if tree_error else 0


def cmd_doctor(args) -> int:
    print(f"agentbus {__version__}")
    exe = resolve_codex()
    if exe is None:
        print("codex.exe : 找不到（已试 config.toml / bin 目录 / PATH）")
    else:
        print(f"codex.exe : {exe}")
        print(f"版本      : {codex_version(exe)}")

    ws_arg = getattr(args, 'workspace', None)
    if ws_arg is not None:
        ws = Path(ws_arg).resolve()
        print(f"\n工作空间信息: {ws}")
        try:
            cfg = load(ws, create=False)
            project_id = cfg.data.get('project_id', getattr(cfg, 'project_id', ''))
            print(f"  项目 ID       : {project_id or '默认'}")
            workers = cfg.enabled_workers() if callable(cfg.enabled_workers) else cfg.enabled_workers
            print(f"  已启用 agent  : {', '.join(workers)}")
            bus = Bus(ws / ".agentbus")
            total_ready = 0
            total_proc = 0
            for w in workers:
                d = bus.dirs(w)
                ready_files = list(d["ready"].glob("*.json")) if d["ready"].is_dir() else []
                proc_files = list(d["processing"].glob("*.json")) if d["processing"].is_dir() else []
                total_ready += len(ready_files)
                total_proc += len(proc_files)
            print(f"  信箱状态      : ready={total_ready}, processing={total_proc}")
        except Exception as e:
            print(f"  加载工作空间异常: {e}")

    if getattr(args, 'env', False):
        print("\n环境变量诊断:")
        print(f"  PYTHONPATH: {os.environ.get('PYTHONPATH', '未设置')}")
        print(f"  PATH 长度 : {len(os.environ.get('PATH', ''))} 字符")

    print("\n环境预检:")
    procs = desktop_app_processes()
    if procs:
        from collections import Counter
        detail = ", ".join(f"{k}×{v}" for k, v in Counter(procs).items())
        print(f"  ⚠ 检测到 Codex 桌面版正在运行（{detail}）")
        print("    桌面版与无头调用抢同一套 code-mode 命名管道，")
        print("    表现是能对话、但一执行工具就报「所有的管道范例都在使用中」(错误 231)。")
        print("    → 跑批量任务前先退出桌面版；详见报告第十一节。")
    else:
        print("  ✓ 未检测到 Codex 桌面版占用，无头调用环境较干净")

    from .config import KNOWN_AGENTS
    print("\n已知 agent 能力画像:")
    for k, v in KNOWN_AGENTS.items():
        mark = "可无头拉起" if v["can_spawn"] else "只能人工接手"
        print(f"  {k:<14} {mark}   {v['note']}")
    return 0


def cmd_preflight(args) -> int:
    """Pre-flight 刚性体检（§9.1）：执行环境与历史二进制绝育刚性防线。"""
    ws = Path(getattr(args, 'workspace', '.')).resolve()
    print(f"=== AgentBus Pre-flight 刚性体检 [工作区: {ws}] ===")

    # 1. 检查控制目录完整性
    ctrl_dir = ws / ".agentbus"
    if not ctrl_dir.is_dir():
        print("  [FAIL] 缺少 .agentbus 控制目录，必须先执行 agentbus init")
        return 1
    print("  [PASS] 控制目录 (.agentbus) 存在且受控")

    # 2. 检查唯一正式出厂程序与 launcher 摘要
    wf_file = ctrl_dir / "workflow.json"
    formal_exe = _formal_executable(ws)
    if formal_exe is None:
        print("  [SKIP] 本项目未登记项目内正式程序（从安装位置或源码运行），跳过程序摘要核对")
        return _preflight_leases(ctrl_dir)

    import hashlib
    actual_exe_sha = hashlib.sha256(formal_exe.read_bytes()).hexdigest()
    if wf_file.is_file():
        try:
            wf_data = json.loads(wf_file.read_text(encoding='utf-8'))
            reg_sha = wf_data.get('launcher', {}).get('sha256', '')
            if actual_exe_sha != reg_sha:
                print(f"  [FAIL] launcher 摘要漂移：磁盘({actual_exe_sha[:8]}...) != 登记({reg_sha[:8]}...)")
                return 1
            print(f"  [PASS] 唯一正式出厂程序物理核对无误 ({actual_exe_sha[:8]}...)")
        except Exception as e:
            print(f"  [WARN] workflow.json 解析异常: {e}")
    else:
        print("  [PASS] 唯一正式出厂程序就绪（未绑定 workflow.json）")

    # 3. 检查子目录历史二进制绝育情况（防绕过防线）
    import glob
    active_bypasses = []
    for f in glob.glob(str(ws / "成果" / "**" / "*.exe"), recursive=True):
        norm = os.path.normpath(f)
        if norm == os.path.normpath(str(formal_exe)):
            continue
        if any(ignored in norm for ignored in ('.build-venv', 'venv', 'node_modules')):
            continue
        if norm.endswith('.deprecated_old_version'):
            continue
        active_bypasses.append(norm)

    if active_bypasses:
        print(f"  [FAIL] 发现 {len(active_bypasses)} 个未绝育的历史版本 EXE，严禁旁路运行！")
        for bp in active_bypasses[:3]:
            print(f"    - {os.path.relpath(bp, ws)}")
        return 1
    print("  [PASS] 历史版本物理绝育检查通过（所有历史二进制已加缀 .deprecated_old_version）")

    return _preflight_leases(ctrl_dir)


def _preflight_leases(ctrl_dir: Path) -> int:
    # 4. 检查活跃租约冲突
    try:
        lm = LeaseManager(ctrl_dir)
        lm.sweep()
        leases = lm.active()
        if leases:
            print(f"  [WARN] 当前有 {len(leases)} 个活跃租约占用中")
        else:
            print("  [PASS] 无排他性悬挂租约冲突")
    except Exception as exc:
        print(f"  [FAIL] 无法核验活跃租约：{exc}")
        return 1

    print("[SUCCESS] Pre-flight 刚性体检全部通过！环境符合 AgentBus 流程执行契约。")
    return 0


def cmd_install_skill(args) -> int:
    """Install the user-level Codex workflow without touching project config."""
    try:
        installed = install_for_current_user(__version__)
    except (SkillInstallError, OSError) as exc:
        print(f"[错误] Codex 工作流安装失败：{exc}", file=sys.stderr)
        return 1
    action = "已更新" if installed.changed else "已经是最新"
    print(f"[ok] AgentBus Codex skill {action}")
    print(f"     skill: {installed.skill_file}")
    print(f"     install record: {installed.manifest_file}")
    print("     如当前对话还看不到 agentbus-team，请新开一轮 Codex 对话后调用。")
    return 0


def cmd_setup(args) -> int:
    """Manage only owned workflow files; no model call or project deletion."""
    options = {'home': Path(args.home) if args.home else None,
               'state_dir': Path(args.state_dir) if args.state_dir else None}
    if args.apply and args.action != 'uninstall':
        raise ValueError('--apply 只用于 setup uninstall；其他操作无需此参数')
    if args.action == 'check':
        if not args.workspace:
            raise ValueError('检查小组需要 --workspace 指向当前项目')
        value = inspect_project(_load(args))
    elif args.action == 'install':
        value = install_workflows(__version__, **options)
    elif args.action == 'status':
        value = inspect_workflows(**options)
    else:
        value = uninstall_workflows(**options, apply=args.apply)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 1 if value.get('status') in ('partial', 'invalid', 'attention') else 0


def cmd_workflow(args) -> int:
    try:
        cfg = _load(args)
        if args.action == 'prepare':
            value = prepare_project_workflow(
                cfg, frozen_executable=getattr(args, 'frozen_executable', None))
        elif args.action == 'prompt':
            if getattr(args, 'frozen_executable', None):
                raise WorkflowError('--frozen-executable 仅用于 workflow prepare')
            print(resume_prompt(cfg, args.agent))
            return 0
        else:
            if getattr(args, 'frozen_executable', None):
                raise WorkflowError('--frozen-executable 仅用于 workflow prepare')
            value = workflow_status(cfg)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 1 if args.action == 'status' and not value.get('ready', False) else 0
    except ConfigError:
        # §7.16：配置缺失是全 CLI 统一的错误类，必须走 main 的 EXIT_CONFIG(2)。
        # 以前这里把 ConfigError 和 WorkflowError 一锅端成 1，导致同样「缺配置」
        # 在 workflow status 返回 1、在 tree status 返回 2 —— 同一类错两种码。
        raise
    except (WorkflowError, OSError) as exc:
        print(f'[错误] {exc}', file=sys.stderr)
        return 1


def _saved_configuration(cfg, value: dict) -> int:
    """Report committed config and separately report workflow publication."""
    value.update(saved=True, workflow_ready=False, model_calls=0)
    code=0
    try:
        prepare_project_workflow(cfg)
        status = workflow_status(cfg)
        if not status['ready']:
            raise WorkflowError(status.get('reason') or '程序入口尚未就绪，请保存小组')
        value['workflow_ready']=True
    except (WorkflowError, OSError) as exc:
        value['workflow_error']=str(exc)
        print(f'[待处理] 配置已保存，工作流尚未更新：{exc}',file=sys.stderr)
        code=4
    print(json.dumps(value,ensure_ascii=False,indent=2))
    return code


def cmd_team(args) -> int:
    """Host-neutral entry. Source: Codex, 2026-10-02; no model calls."""
    from copy import deepcopy
    from .config import DEFAULTS, Config
    from .gui_state import build_group_config
    parent_job_id = getattr(args, 'from_job', None)
    if parent_job_id is not None and args.action != 'start':
        raise ValueError('--from-job 仅用于明确开启新的关联阶段；普通继续请用 team status')
    if args.action == 'configure':
        ws = Path(args.workspace).expanduser().resolve()
        path = ws / '.agentbus/config.json'
        data = load(ws).data if path.exists() else deepcopy(DEFAULTS)
        data['workspace'] = str(ws)
        names = [s.strip() for s in (args.members or '').split(',') if s.strip()]
        if not args.coordinator:
            raise ConfigError('配置小组时请明确 --coordinator 与 --members')
        data = build_group_config(data, names, args.coordinator)
        path.parent.mkdir(parents=True, exist_ok=True)
        manager = LeaseManager(ws / '.agentbus')
        lease = manager.acquire('.agentbus/config.json', 'agentbus-team-config', ttl_s=120)
        if not lease:
            raise ConfigError('项目配置正在编辑，请稍后重试')
        try:
            # Another writer may have changed permissions while this caller
            # waited for the lease. Reapply only the explicit group selection.
            fresh=load(ws).data if path.exists() else deepcopy(DEFAULTS)
            fresh['workspace']=str(ws)
            data=build_group_config(fresh,names,args.coordinator)
            cfg = save(Config(path, ws, data))
            bus = Bus(ws / '.agentbus'); bus.init()
            for name in [*cfg.enabled_workers(), HUMAN_INBOX]:
                bus.register(name)
        finally:
            manager.release(lease)
        value = {'workspace': str(ws), 'coordinator': cfg.director,
                 'members': list(cfg.enabled_workers()), 'model_calls': 0}
        return _saved_configuration(cfg,value)
    else:
        from .jobs import (start_job, status_job, list_jobs, dispatch_job,
                           checkpoint_job, finalize_job)
        cfg = _load(args)
        if args.action == 'start':
            value = start_job(cfg, args.caller, args.goal, args.request_id,
                              parent_job_id=parent_job_id)
        elif args.action == 'status':
            value = status_job(cfg, args.job_id) if args.job_id else list_jobs(cfg)
        elif args.action == 'connections':
            from .onboarding import connection_summary
            value = connection_summary(cfg)
        elif args.action == 'join':
            if not getattr(args, 'caller', None):
                raise ValueError('确认加入团队需要 --caller 指定成员标识')
            from .onboarding import confirm_connection
            value = confirm_connection(cfg, args.caller)
        elif args.action == 'dispatch':
            if not args.plan_file:
                raise ValueError('派工需要 --plan-file（含 tasks 数组的 JSON）')
            payload = json.loads(Path(args.plan_file).read_text(encoding='utf-8-sig'))
            value = dispatch_job(cfg, args.caller, args.job_id,
                                 payload.get('tasks') if isinstance(payload, dict) else payload)
        elif args.action == 'checkpoint':
            value = checkpoint_job(cfg, args.caller, args.job_id, args.note)
        else:
            value = finalize_job(cfg, args.caller, args.job_id, args.note,
                                 verified=args.verified)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def _workspace_text(cfg, relative):
    from .workspace_files import WorkspaceFiles
    files = WorkspaceFiles(cfg.workspace)
    pieces, line, digest = [], 1, None
    for _ in range(128):
        page = files.read_text(relative, start_line=line, max_lines=200, expected_sha256=digest)
        digest = page['sha256']; pieces.append(page['text'])
        if sum(len(piece.encode('utf-8')) for piece in pieces) > 256 * 1024:
            raise ValueError('structured input exceeds 256 KiB')
        line = page['next_line']
        if line is None:
            return ''.join(pieces)
    raise ValueError('structured input exceeds pagination limit')


def _root_text(root, relative):
    """Read a bounded UTF-8 JSON/text input from an explicit root."""
    from .workspace_files import WorkspaceFiles
    files = WorkspaceFiles(root)
    pieces, line, digest = [], 1, None
    for _ in range(128):
        page = files.read_text(relative, start_line=line, max_lines=200, expected_sha256=digest)
        digest = page['sha256']; pieces.append(page['text'])
        if sum(len(piece.encode('utf-8')) for piece in pieces) > 256 * 1024:
            raise ValueError('structured input exceeds 256 KiB')
        line = page['next_line']
        if line is None:
            return ''.join(pieces)
    raise ValueError('structured input exceeds pagination limit')


def _workspace_json(cfg, relative, *, root=None):
    return json.loads(_root_text(root or cfg.workspace, relative))


def _manifest_json(cfg, relative):
    """Read an artifact manifest from the control directory, then the output root.

    Source: Codex candidate acceptance, 2026-10-07. `files describe --root
    output --manifest-file` writes the manifest into the business output
    directory, but `answer`/`post --artifacts-file` used to resolve that
    relative path only against the control directory. Members following the
    public describe -> verify -> answer flow hit NOT_FOUND on the last step.
    The control directory stays authoritative; the output root is a fallback.
    """
    from .workspace_files import WorkspaceFileError
    try:
        return _workspace_json(cfg, relative)
    except WorkspaceFileError:
        if Path(relative).resolve() == (cfg.output_root / relative).resolve():
            raise
        return _workspace_json(cfg, relative, root=cfg.output_root)


def _result_artifacts(cfg, source, entries, *, failed=False):
    from .workspace_files import WorkspaceFiles
    if not isinstance(entries, list):
        raise ValueError('artifacts must be a list')
    if not failed:
        paths = {item.get('path') for item in entries if isinstance(item, dict)}
        missing = set(source.expected_artifacts) - paths
        if missing:
            raise ValueError('required artifact manifest missing: ' + ', '.join(sorted(missing)))
    if entries:
        report = WorkspaceFiles(cfg.output_root).verify_artifacts(entries)
        if report['verdict'] != 'match':
            raise ValueError('artifact verification failed: ' + json.dumps(report, ensure_ascii=False))
    return entries


def _check_receipt_reuse(cfg, source, existing, entries=None, failed=None):
    outcome_failed = existing.exit_code not in (None, 0)
    _result_artifacts(cfg, source, existing.artifacts, failed=outcome_failed)
    if failed is not None and bool(failed) != outcome_failed:
        raise ValueError('existing receipt outcome differs; preserve the original result')
    if entries is not None:
        _result_artifacts(cfg, source, entries, failed=outcome_failed)
        if entries != existing.artifacts:
            raise ValueError('existing receipt has a different artifact manifest; preserve both requests explicitly')


def cmd_files(args) -> int:
    from .workspace_files import WorkspaceFiles, WorkspaceFileError
    cfg = _load(args)
    root = cfg.output_root if args.root == 'output' else cfg.workspace
    files = WorkspaceFiles(root)
    try:
        if args.action == 'read':
            value = files.read_text(args.path, start_line=args.start_line, max_lines=args.max_lines,
                                    expected_sha256=args.expected_sha256)
        elif args.action == 'write':
            if args.caller not in cfg.enabled_workers():
                raise ValueError('file owner must be a selected group member')
            if args.text is None:
                raise ValueError('file write needs --text')
            value = files.write_text(args.path, args.text, owner=args.caller,
                                     expected_sha256=args.expected_sha256, dry_run=args.dry_run)
        elif args.action == 'describe':
            value = files.describe_artifact(args.path)
            if args.manifest_file:
                if args.caller not in cfg.enabled_workers():
                    raise ValueError('manifest output needs --caller naming a selected group member')
                manifest = files.write_text(args.manifest_file,
                    json.dumps([value], ensure_ascii=False, indent=2), owner=args.caller,
                    expected_sha256=args.expected_sha256, dry_run=args.dry_run)
                value = {'artifact': value, 'manifest': manifest}
        else:
            if not args.manifest_file:
                raise ValueError('artifact verification needs --manifest-file')
            value = files.verify_artifacts(_workspace_json(cfg, args.manifest_file, root=root))
    except WorkspaceFileError as exc:
        if not args.dry_run:
            Bus(cfg.workspace / '.agentbus').audit(event='workspace_file_rejected', action=args.action, code=exc.code)
        raise
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 1 if value.get('verdict') == 'mismatch' else 0


def cmd_skills(args) -> int:
    """Source: WorkBuddy/Codex, 2026-10-08. Project skill operations."""
    from .skill_packages import inspect_package
    from .skill_preview import build_sync_preview
    from .skill_presentation import present_preview
    from .workspace_files import WorkspaceFiles
    cfg = _load(args)
    if args.action == 'targets':
        from .host_skill_adapters import host_public_paths
        print(json.dumps({'hosts': host_public_paths(), 'read_only': True,
                          'native_discovery_claimed': False}, ensure_ascii=False, indent=2))
        return 0
    if not args.path:
        raise ValueError('skills requires --path relative to this project')
    if args.action in ('sync', 'rollback'):
        from .skill_deployer import deploy, rollback, plan
        if not args.host:
            raise ValueError('skills sync/rollback requires --host')
        if args.scope != 'project':
            raise ValueError('This command manages project skills only; no private host directories')
        files = WorkspaceFiles(cfg.workspace)
        rel = files._relative(args.path)
        source_dir = files.workspace.joinpath(*rel.split('/'))
        files._inspect(source_dir, directory=True)
        bus = Bus(cfg.workspace / '.agentbus')
        active = [path.name for name in cfg.enabled_workers() for path in bus.processing(name)]
        kwargs = dict(source_dir=source_dir, project_root=cfg.workspace,
                      state_root=cfg.workspace / '.agentbus' / 'skill-center', route=args.route)
        if args.action == 'rollback':
            value = rollback(args.host, confirm=args.confirm, **kwargs)
        elif not args.confirm:
            value = plan(args.host, active_task_ids=active, **kwargs)
        else:
            value = deploy(args.host, confirm=True, active_task_ids=active, **kwargs)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 4 if args.confirm and not value.get('deployed', value.get('rolled_back', False)) else 0
    # The scanner's historical max_dir_bytes argument currently represents
    # directory-node/listing counts. Keep interactive work bounded at 1024;
    # expose the actual limits in the report, never claim these are byte units.
    source = inspect_package(cfg.workspace, args.path, max_dir_bytes=1024)
    if args.action == 'inspect':
        value = source
    else:
        if not args.target_path or not args.host:
            raise ValueError('skills preview requires --target-path and --host')
        files = WorkspaceFiles(cfg.workspace)
        rel = files._relative(args.target_path)
        target_path = files.workspace.joinpath(*rel.split('/'))
        # Missing is meaningful only after controlled ancestry inspection.
        snapshot = files._inspect(target_path, missing=True, directory=True)
        target = (None if snapshot.get(target_path) is None else
                  inspect_package(cfg.workspace, rel, max_dir_bytes=1024))
        record = _workspace_json(cfg, args.record_file) if args.record_file else None
        value = build_sync_preview(source, target, record, host=args.host,
                                   scope=args.scope, route=args.route)
        value['presentation'] = present_preview(value)
        value['read_only'] = True
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def cmd_plan(args) -> int:
    from .project_plan import (propose_plan, record_review, adopt_plan, plan_summary, plan_snapshot)
    from .workspace_files import WorkspaceFiles
    cfg = _load(args)
    if args.action in ('assess', 'convergence'):
        if not args.file:
            raise ValueError('plan assess/convergence requires an explicit project-relative snapshot --file')
        import time
        snapshot = _workspace_json(cfg, args.file)
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('taskbook'), dict)
                or not isinstance(snapshot['taskbook'].get('work_items'), list)
                or not isinstance(snapshot.get('changes', {}), dict)):
            raise ValueError('plan snapshot requires taskbook.work_items list and changes object')
        if args.action == 'convergence':
            from .plan_convergence import convergence_report
            value = convergence_report(snapshot, now_ts=time.time())
        else:
            from .change_control import classify_change
            if not args.reason:
                raise ValueError('plan assess requires --reason containing the new requirement')
            value = classify_change(args.reason, snapshot=snapshot, now_ts=time.time(),
                                    project_id=str(cfg.workspace), source=args.caller)
        value['read_only'] = True
        value['dispatch_performed'] = False
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    expected = None if args.expected_current_sha256 == 'MISSING' else args.expected_current_sha256
    if args.action == 'status':
        value = plan_summary(cfg)
    elif args.action == 'snapshot':
        value = plan_snapshot(cfg, revision_id=args.plan_id)
    elif args.action == 'propose':
        if not args.file:
            raise ValueError('plan propose needs --file relative to the workspace')
        content = _workspace_text(cfg, args.file)
        value = propose_plan(cfg, args.caller, content, args.reason,
                             [x.strip() for x in args.reviewers.split(',') if x.strip()],
                             expected_current_sha256=expected, request_id=args.request_id)
    elif args.action == 'review':
        value = record_review(cfg, args.caller, args.proposal_id, args.task_id, args.verdict,
                              reviewed_file=args.reviewed_file, evidence_workspace=args.evidence_workspace)
    elif args.action == 'adopt-change':
        from .project_plan import adopt_change_record
        if not args.file:
            raise ValueError('plan adopt-change 需要 --file 提供已评估的变更提案 JSON 文件')
        record = _workspace_json(cfg, args.file)
        value = adopt_change_record(
            cfg, args.caller, record,
            expected_revision_id=args.plan_id,
            expected_revision_sha256=expected
        )
    elif args.action == 'withdraw-change':
        from .project_plan import withdraw_change_record
        if not args.reason:
            raise ValueError('plan withdraw-change 需要 --reason 给出撤回原因')
        target = args.change_id or args.file
        if not target:
            raise ValueError('plan withdraw-change 需要 --change-id 或 --file 提供变更标识')
        if args.file and not args.change_id:
            record_or_id = _workspace_json(cfg, args.file)
        else:
            record_or_id = args.change_id
        value = withdraw_change_record(cfg, args.caller, record_or_id, reason=args.reason)
    else:
        if args.expected_current_sha256 is None:
            raise ValueError('plan adopt requires --expected-current-sha256 (MISSING for first adoption)')
        value = adopt_plan(cfg, args.caller, args.proposal_id, expected,
                           json.loads(args.dispositions_json))
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def cmd_member(args) -> int:
    # Source: offline recommendations, Codex, 2026-10-03. This branch must
    # precede project loading so browsing recommendations never creates state.
    if args.action == 'catalog':
        from .catalog import catalog, get_entry
        value = get_entry(args.id) if args.id else catalog()
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if args.action == 'onboard':
        from .onboarding_pack import auto_onboard_agent
        target_member = args.id or getattr(args, 'member', None)
        if not target_member:
            raise ValueError('一键接入新智能体需要通过 --id 或 --member 指定成员标识')
        if not args.workspace:
            raise ValueError('一键接入新智能体需要通过 --workspace 指定项目工作区')
        res = auto_onboard_agent(
            workspace=args.workspace,
            member_id=target_member,
            display_name=getattr(args, 'name', None),
            preset=getattr(args, 'preset', None)
        )
        if getattr(args, 'json', False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(res['prompt'])
        return 0
    if args.action not in {'add', 'catalog'}:
        return cmd_member_workflow(args)
    if not args.id:
        raise ValueError('登记成员需要 --id')
    if not args.workspace:
        raise ValueError('登记成员需要 --workspace')
    from .profiles import add_profile
    cfg = _load(args)
    command = json.loads(args.command_json) if args.command_json else None
    cfg = add_profile(cfg, args.id, args.name or args.id, args.kind, command,
                      transport=args.transport)
    return _saved_configuration(cfg,{'id':args.id,'worker':cfg.worker(args.id)})


def cmd_member_workflow(args) -> int:
    """Public member workflow. Codex 2026-10-05; no agent authentication."""
    from .member_invitation import prepare_invitation, invitation_status, record_invitation_event
    from .member_library import MemberLibrary, store_project_card, _identity
    if not args.workspace:
        raise ValueError('请指定 --workspace，不能猜当前项目')
    cfg = _load(args)
    if args.action == 'invite':
        if not args.caller or not args.member or not args.request_id:
            raise ValueError('邀请需要 --caller、--member、--request-id；重试复用同一request-id')
        value = prepare_invitation(cfg,args.caller,args.member,args.request_id,writable=not args.readonly)
    elif args.action == 'pack':
        from .onboarding_pack import generate_onboarding_pack, deploy_onboarding_pack
        target_member = args.member or args.id
        if not target_member:
            raise ValueError('生成接入包需要 --member 指定成员')
        if getattr(args, 'deploy', False):
            value = deploy_onboarding_pack(cfg.workspace, target_member, coordinator=args.coordinator, task_id=getattr(args, 'task_id', None))
        else:
            value = generate_onboarding_pack(cfg.workspace, target_member, coordinator=args.coordinator, task_id=getattr(args, 'task_id', None))
    elif args.action == 'status':
        if not args.invite_id:raise ValueError('请指定 --invite-id')
        value = invitation_status(cfg,args.invite_id)
    elif args.action == 'observe':
        if not all((args.caller,args.invite_id,args.event,args.source)):
            raise ValueError('观察记录需要 --caller、--invite-id、--event、--source')
        record_invitation_event(cfg,args.caller,args.invite_id,args.event,args.source)
        value = invitation_status(cfg,args.invite_id)
    elif args.action == 'card':
        if not args.member:raise ValueError('请指定 --member')
        member = _identity(args.member)
        if member not in cfg.enabled_workers():raise ValueError('UNKNOWN_MEMBER: 成员未加入当前小组')
        value = MemberLibrary(cfg.workspace/'.agentbus/member-cards').get(member)
        if value is None:raise ValueError('INVALID_CARD: 当前项目尚无公开档案')
    else:
        if not args.library_root:raise ValueError('请明确选择 AgentBus 公共档案目录 --library-root')
        library=MemberLibrary(Path(args.library_root))
        if args.action == 'library-get':
            if not args.member:raise ValueError('请指定 --member')
            value=library.get(args.member)
        elif args.action == 'library-save':
            if not args.card_file or not args.expected_sha256:
                raise ValueError('保存档案需要 --card-file 与 --expected-sha256（首次MISSING）')
            from .member_library import _unique_object
            card=json.loads(_workspace_text(cfg,args.card_file),object_pairs_hook=_unique_object)
            value=library.save(card,args.expected_sha256)
        else:
            if not all((args.caller,args.member,args.expected_sha256)):
                raise ValueError('导入需要 --caller、--member、--expected-sha256（首次MISSING）')
            card=library.get(args.member)
            if card is None:raise ValueError('INVALID_CARD: 公共档案不存在')
            value=store_project_card(cfg,args.caller,card,args.expected_sha256)
            value['notice']='仅复用公共历史，不继承本项目权限、会话、执行者或认证；必要验证仍须本地完成。'
    print(json.dumps(value,ensure_ascii=False,indent=2))
    return 4 if isinstance(value,dict) and value.get('state')=='conflict' else 0


def cmd_share(args) -> int:
    from .sharing import import_template, build_share_package, new_template_config
    if args.action == 'import':
        if not args.template:
            raise ValueError('请指定 --template')
        payload = json.loads(Path(args.template).read_text(encoding='utf-8-sig'))
        # Source: native friend-package first-use test, Codex, 2026-10-02.
        # Validate without writes before initializing an empty project. Reload
        # after the lease so a concurrently created config still needs replace.
        workspace = Path(args.workspace).expanduser().resolve()
        config_path = workspace / '.agentbus' / 'config.json'
        fresh = new_template_config(workspace)
        cfg = load(workspace) if config_path.exists() else fresh
        import_template(cfg, payload, replace=args.replace)
        manager = LeaseManager(cfg.workspace / '.agentbus')
        lease = manager.acquire('.agentbus/config.json', 'agentbus-share-import', ttl_s=120)
        if not lease:
            raise ValueError('配置正在修改，未导入')
        try:
            current = load(workspace) if config_path.exists() else fresh
            cfg = save(import_template(current,payload,replace=args.replace))
        finally:
            manager.release(lease)
        value = {'members': list(cfg.enabled_workers()), 'coordinator': cfg.director}
        return _saved_configuration(cfg,value)
    else:
        cfg = _load(args)
        executable = args.executable or (sys.executable if getattr(sys,'frozen',False) else None)
        if not executable or not args.output:
            raise ValueError('导出需要 --output；源代码运行还需 --executable 指向交付EXE')
        value = build_share_package(Path(executable), cfg, Path(args.output))
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def cmd_detect(args) -> int:
    """探测本机装了哪些 agent。比 doctor 全 —— doctor 只看 codex。"""
    from .detect import detect_all, render, summary, to_config_patch

    probes = detect_all()
    if args.models:
        from .adapters import AdapterError, list_models
        try:
            models = list_models()
        except (AdapterError, OSError) as exc:
            print(f"[错误] 取不到模型列表：{exc}", file=sys.stderr)
            return 1
        print("HTTP 通道可用模型：")
        for m in models:
            print(f"  {m}")
        return 0

    if args.json:
        print(json.dumps(summary(probes), ensure_ascii=False, indent=2))
    else:
        print(render(probes))

    if not args.write:
        return 0

    if not args.workspace:
        print("\n[错误] --write 需要同时给 --workspace", file=sys.stderr)
        return 2
    cfg = load(args.workspace, create=True)
    data = json.loads(json.dumps(cfg.data))
    workers = data.setdefault("workers", {})
    patch = to_config_patch(probes)
    added = []
    for name, conf in patch.items():
        if name not in workers:
            workers[name] = conf
            added.append(name)
    spawnable = [n for n, w in workers.items() if w.get("can_spawn") and w.get("enabled", True)]
    if data.get("director") not in workers and spawnable:
        data["director"] = spawnable[0]
    cfg = save(cfg, data)
    print(f"\n[ok] 已并入配置 {cfg.path}")
    print(f"     新增: {', '.join(added) if added else '（无）'}")
    print(f"     主体: {cfg.director}")
    return 0


def cmd_post(args) -> int:
    """平级模型的基础动作：任何一方都可以往别人的信箱投一条消息。

    和 boss 的区别：boss 是"主体拆解 + 派发"（主从），
    post 是谁都能用的"投一封信"（平级）。
    """
    cfg = _load(args)
    args.intent = _receipt_text(args, cfg, 'intent')
    bus = Bus(cfg.workspace / ".agentbus")
    bus.init()
    lease = None
    if args.type == "result" and args.reply_to:
        lease = LeaseManager(bus.root).acquire(
            f"agentbus/report/{args.reply_to}", args.sender, ttl_s=300,
            note="serialize direct result sends", msg_id=args.reply_to)
        if lease is None:
            print(f"[提示] 任务 {args.reply_to} 的回投正在处理中，请稍后重试。",
                  file=sys.stderr)
            return 2
    try:
        if args.type == 'result' and args.reply_to:
            with task_lock(bus.root, args.sender, args.reply_to):
                return _cmd_post_locked(args, cfg, bus)
        return _cmd_post_locked(args, cfg, bus)
    finally:
        if lease is not None:
            LeaseManager(bus.root).release(lease)


def _cmd_post_locked(args, cfg, bus: Bus) -> int:
    participants = [*cfg.enabled_workers(), HUMAN_INBOX]
    lookup = {name.casefold(): name for name in participants}
    raw_sender = cfg.director if args.sender.casefold() == "director" else args.sender
    raw_to = cfg.director if args.to.casefold() == "director" else args.to
    sender = lookup.get(raw_sender.strip().casefold())
    to = lookup.get(raw_to.strip().casefold())
    if sender is None:
        print(f"[错误] 未知或未启用 sender：{args.sender}", file=sys.stderr)
        return 2
    if to is None:
        print(f"[错误] 未知或未启用收件人：{args.to}", file=sys.stderr)
        return 2
    executor_required = (args.type in ("task", "review")
                         or (args.type == "goal" and to != cfg.director))
    if (executor_required and to != HUMAN_INBOX
            and not cfg.worker(to).get("executor_enabled", True)):
        print(f"[错误] 收件人 {to} 未启用为执行者，不能接收 {args.type}",
              file=sys.stderr)
        return 2
    refs = [r.strip() for r in (args.refs or "").split(",") if r.strip()]
    result_to = None
    result_source = None
    result_route = None
    if args.type in ("task", "goal", "review"):
        if not args.result_to:
            print(f"[错误] 新 {args.type} 消息必须显式提供 --result-to", file=sys.stderr)
            return 2
        try:
            result_to = resolve_result_to_arg(
                args.result_to, sender=sender, director=cfg.director,
                participants=[*cfg.enabled_workers(), HUMAN_INBOX])
        except ValueError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2
    if args.type == "result":
        if not args.reply_to:
            print("[错误] result 消息必须使用 --reply-to 指向原任务", file=sys.stderr)
            return 2
        try:
            result_source = bus.validate_result_reference(args.reply_to)
            ticket = _cli_ticket(args, args.reply_to, sender)
            bus.check_claim(sender, result_source, ticket, evidence={'body': args.intent})
            result_route = resolve_message_result_to(
                result_source, [*cfg.enabled_workers(), HUMAN_INBOX])
            if result_route.recipient is None:
                raise BusError(
                    f"原任务没有可解析的结果收件人，请先澄清：{result_route.error}")
        except BusError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2
        existing_results = bus.find_results(args.reply_to, sender=sender)
        cited = [item for item in existing_results if item.id in args.intent]
        if len(cited) > 1:
            print("[错误] 正文引用了多个既有结果 ID，拒绝重复投递", file=sys.stderr)
            return 2
        if cited:
            existing = cited[0]
            if existing.to.casefold() != to.casefold():
                print(f"[错误] 既有回执 {existing.id} 已投给 {existing.to}，拒绝重复投递",
                      file=sys.stderr)
                return 2
            _check_receipt_reuse(cfg, result_source, existing,
                _manifest_json(cfg, args.artifacts_file) if getattr(args, 'artifacts_file', None) else None)
            if result_source.status not in ('done', 'failed'):
                print(f'[待核对] 回执 {existing.id} 已存在，原任务仍未结束。请用 answer 核对并引用该回执。', file=sys.stderr)
                return 2
            print(f"[提示] 已复用既有回执 {existing.id} → {existing.to}；没有重复投递。")
            return 0
        same = next((item for item in existing_results
                     if item.to.casefold() == to.casefold()
                     and item.body == args.intent), None)
        if same is not None:
            _check_receipt_reuse(cfg, result_source, same,
                _manifest_json(cfg, args.artifacts_file) if getattr(args, 'artifacts_file', None) else None)
            if result_source.status not in ('done', 'failed'):
                print(f'[待核对] 回执 {same.id} 已存在，原任务仍未结束。请用 answer 核对并引用该回执。', file=sys.stderr)
                return 2
            print(f"[提示] 已复用既有回执 {same.id} → {same.to}；没有重复投递。")
            return 0
        if existing_results:
            ids = ", ".join(item.id for item in existing_results)
            print(f"[错误] 该任务已存在回执 {ids}；拒绝重复投递，"
                  "请使用 agentbus_report 明确复用。", file=sys.stderr)
            return 2
    entries = []
    if args.type == 'result':
        entries = _result_artifacts(cfg, result_source,
            _manifest_json(cfg, args.artifacts_file) if getattr(args, 'artifacts_file', None) else [])
    msg = bus.make(sender=sender, to=to, type=args.type,
                   title=args.title or args.intent[:80],
                   body=args.intent, refs=refs,
                   hop=args.hop, attempt=0,
                   result_to=(result_to if args.type != "result"
                              else result_route.recipient if result_route else None),
                   reply_to=args.reply_to,
                   job_id=result_source.job_id if result_source else None,
                   plan_id=result_source.plan_id if result_source else None,
                   plan_sha256=result_source.plan_sha256 if result_source else None,
                   artifacts=entries,
                   routed_by_fallback=("body" if result_route is not None
                                       and result_route.source == "body" else None))
    try:
        if args.type == 'result':
            msg = bus.commit_result(
                sender, result_source, ticket, body=args.intent, result_to=to,
                artifacts=entries, artifact_root=cfg.output_root, exit_code=0,
                routed_by_fallback=('body' if result_route.source == 'body' else None))
        else:
            bus.post(msg)
    except BusError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    if msg.awaiting_audit:
        print(f'[待审核] 成果已提交（{msg.id}），独立审核后才会送达 {to}。')
        return 0
    print(f"[ok] 已投递 {msg.id}  {sender} -> {to}")
    try:
        from .trigger.service import TriggerDeliveryService
        tds = TriggerDeliveryService(cfg.workspace)
        if msg.type in ("task", "review"):
            tds.trigger_for_member(msg.to, msg.id)
    except Exception:
        pass  # 容错通知类：触发组长通知异常不阻断结果提交流程
    if msg.type == "result":
        source = result_source
        assert source is not None and result_route is not None
        if (source.result_to is None and result_route.source == "body"
                and result_route.recipient != msg.to):
            bus.audit(event="route_mismatch", result_id=msg.id,
                      source_task_id=source.id, declared=result_route.recipient,
                      actual=msg.to, routed_by_fallback="body")
        if (source.type in {"task", "goal"}
                and result_route.recipient == msg.to):
            source.result = msg.body
            review = Director(cfg, bus=bus).schedule_peer_review(source, sender, "done")
            if review is not None:
                print(f"     已排入同伴评审：{review.id} → {review.to}")
    if result_to:
        print(f"     结果明确投给：{result_to}")
    print(f"     对方读这里：{bus.dirs(to)['ready']}")
    return 0


def cmd_watchtower(args) -> int:
    """唤醒者：盯所有信箱，有活就通知人并给出"该对谁说哪句话"。"""
    cfg = _load(args)
    from .watchtower import Watchtower
    wt = Watchtower(cfg)
    n = wt.run(seconds=args.seconds, toast=not args.no_toast, once=args.once)
    if args.once:
        print(f"[watchtower] 本次检查：{n} 条待处理")
    return 0


def cmd_handshake(args) -> int:
    """互通协议协商：生成给各 agent 的指令，或汇总它们各自的回答。

    立场：协议不该由某一个 agent 单独设计。这里只负责"发问"和"收卷"。
    """
    from . import handshake as hs
    ws = Path(args.workspace or ".").expanduser().resolve()

    if args.collect:
        replies = hs.collect(ws)
        if args.json:
            print(hs.to_json(replies))
            return 0
        text = hs.summarize(replies)
        print(text)
        if replies:
            p = hs.write_summary(ws, text)
            print(f"\n[ok] 汇总已写入 {p}")
        return 0

    agents = [a.strip() for a in (args.agents or "").split(",") if a.strip()]
    target = hs.emit(ws, agents, short=args.short)
    print(hs.build_request(ws, agents, short=args.short))
    print()
    print("-" * 68)
    print(f"[ok] 指令已写入 {target}")
    print("用法：把上面整段粘贴给每个 agent 各一次（Codex / WorkBuddy / Trae / ZCode）。")
    print("它们各写好自己的 <名字>.reply.md 后，执行：")
    print(f'  python -m agentbus handshake --workspace "{ws}" --collect')
    return 0


def cmd_contact(args) -> int:
    """管理成员桌面联系卡（invite/show/retire）。"""
    ws = Path(getattr(args, 'workspace', '.') or '.').expanduser().resolve()
    from .trigger.contacts import create_invite, load_contact, retire_contact
    action = getattr(args, 'action', 'show')
    agent = getattr(args, 'agent', None)
    if not agent:
        print("[错误] 请通过 --agent 指定成员 ID", file=sys.stderr)
        return EXIT_CONFIG
    if action == 'invite':
        conn = getattr(args, 'connector', None) or 'agentbus.codearts-hook'
        inst = getattr(args, 'installation', None) or f'inst_{agent}'
        inv = create_invite(ws, agent, conn, inst)
        print(f"[OK] 邀请已生成：{inv.invite_id} (有效期至 {inv.expires_at})")
        return 0
    elif action == 'show':
        c = load_contact(ws, agent)
        if c is None:
            print(f"[提示] 成员 {agent} 尚未绑定联系卡")
            return 0
        print(f"成员：{c.member_id} | 状态：{c.status} | 版本：{c.revision}")
        print(f"连接器：{c.connector_id} (v{c.connector_version})")
        print(f"原生会话 ID：{c.native_conversation_id} (来源: {c.identity_source})")
        return 0
    elif action == 'retire':
        rev = getattr(args, 'revision', None)
        if rev is None:
            c = load_contact(ws, agent)
            if not c:
                print(f"[错误] 成员 {agent} 无联系卡", file=sys.stderr)
                return EXIT_CONFIG
            rev = c.revision
        c_ret = retire_contact(ws, agent, int(rev))
        print(f"[OK] 成员 {agent} 联系卡已退役 (新版本 {c_ret.revision})")
        return 0
    elif action == 'checkin':
        session_id = getattr(args, 'session', None)
        conn = getattr(args, 'connector', None)
        inst = getattr(args, 'installation', None)
        quiet = getattr(args, 'quiet', False)
        from .trigger.contacts import checkin_contact
        res = checkin_contact(ws, agent, session_id=session_id, connector_id=conn, installation_key=inst)
        if not quiet:
            print(f"[OK] 成员 {agent} 报到就位！")
            print(f"  会话标识：{res['session_id']}")
            print(f"  连接器：{res['connector_id']} | 绑定ID：{res['binding_id']} (rev_{res['revision']})")
            if res.get('rebound'):
                print(f"  状态：换窗换绑成功 (已归档历史快照)")
            else:
                print(f"  状态：绑定确认 (活跃)")
            if res.get('notified_coordinator'):
                print(f"  已向组长信箱投递就位报到")
            bf = res.get('briefing', {})
            p_cnt = bf.get('processing_count', 0)
            r_cnt = bf.get('ready_count', 0)
            print(f"  当前现场简报：在途任务 {p_cnt} 条，待认领任务 {r_cnt} 条")
            if p_cnt > 0:
                for t in bf.get('processing_tasks', []):
                    print(f"    - [在途] {t.get('id')}: {t.get('title')}")
            if r_cnt > 0:
                for t in bf.get('ready_tasks', []):
                    print(f"    - [待认领] {t.get('id')}: {t.get('title')}")
            if bf.get('context_compaction_summary'):
                print(bf['context_compaction_summary'])
        return 0
    return 0


def cmd_trigger(args) -> int:
    """管理项目自动触发服务与策略（status/stop/resume/reconcile/diagnostics）。"""
    ws = Path(getattr(args, 'workspace', '.') or '.').expanduser().resolve()
    from .trigger.service import TriggerDeliveryService
    from .trigger.journal import DeliveryJournal
    from .trigger.diagnostics import export_diagnostic_bundle
    service = TriggerDeliveryService(ws)
    action = getattr(args, 'action', 'status')
    if action == 'status':
        policy = service.load_policy()
        journal = DeliveryJournal(ws)
        print("=== AgentBus 项目桌面触发服务状态 ===")
        print(f"项目：{service.norm_project}")
        print(f"状态：{'[已暂停/停止]' if policy.stopped else '[运行中]'}")
        print(f"组长协调路线：{policy.coordinator_route}")
        for st in ['queued', 'submitting', 'submitted', 'uncertain', 'rejected']:
            count = len(journal.list_by_status(st))
            print(f"  - 队列 {st}: {count}")
        return 0
    elif action == 'stop':
        service.set_stopped(True)
        print("[OK] 已停止新唤醒请求投递（在途任务保持有效）")
        return 0
    elif action == 'resume':
        service.set_stopped(False)
        print("[OK] 已恢复项目触发投递服务")
        return 0
    elif action == 'reconcile':
        journal = DeliveryJournal(ws)
        res = journal.reconcile_on_startup()
        print(f"[OK] 崩溃调和完成，{res['reconciled_count']} 条残留 submitting 请求已安全转为 uncertain")
        return 0
    elif action == 'diagnostics':
        bundle = export_diagnostic_bundle(ws)
        if getattr(args, 'json', False):
            print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"[OK] 诊断包已生成 (项目: {bundle.project_key}, 导出时间: {bundle.exported_at})")
            print(f"  - 策略: {bundle.policy_state}")
            print(f"  - 请求统计: {bundle.requests_summary}")
        return 0
    elif action == 'run':
        from .trigger.service import run_project_service
        once = getattr(args, 'once', False)
        seconds = getattr(args, 'seconds', 0.0) or 0.0
        interval = getattr(args, 'interval', 2.0) or 2.0
        if once:
            res = service.scan_and_deliver_pending()
            print(f"[OK] 单次投递扫描完成：scanned={res['scanned']}, delivered={res['delivered']}, status={res['status']}")
            if res.get('errors'):
                for err in res['errors']:
                    print(f"  - [警告] {err}")
            return 0
        else:
            print(f"[OK] 正在启动 AgentBus 纯后台触发投递循环（项目: {service.norm_project}, 轮询间隔: {interval}s）...")
            try:
                run_project_service(ws, seconds=seconds, once=False, poll_interval=interval)
            except KeyboardInterrupt:
                print("\n[OK] 触发投递服务已正常停止")
            return 0
    return 0


def cmd_gui(args) -> int:
    from .web_desktop import main as gui_main
    return gui_main(["--workspace", args.workspace] if args.workspace else [])


def cmd_mcp(args) -> int:
    from .mcp_server import serve
    return serve(args.workspace, identity=args.identity,
                 session_tools=args.session_tools)


def cmd_lease(args) -> int:
    """文件租约：多 agent 改同一文件时的互斥。"""
    cfg = _load(args)
    from .lease import LeaseManager
    mgr = LeaseManager(cfg.workspace / ".agentbus")

    if args.action == "list":
        act = mgr.active()
        if not act:
            print("[空] 当前没有活跃租约")
            return 0
        now = time.time()
        for x in act:
            print(f"  {x['rel']:<40} ← {x['owner']:<12} "
                  f"剩 {x['expires_at'] - now:5.0f}s  {x.get('note','')}")
        return 0

    if args.action == "sweep":
        cleared = mgr.sweep()
        print(f"清理了 {len(cleared)} 个过期租约" +
              (f"：{', '.join(cleared)}" if cleared else ""))
        return 0

    if not args.path or not args.owner:
        print("[错误] acquire / release 需要 --path 和 --owner", file=sys.stderr)
        return 2

    if args.action == "acquire":
        lz = mgr.acquire(args.path, args.owner, ttl_s=args.ttl, note=args.note)
        if lz is None:
            cur = mgr.holder(args.path) or {}
            print(f"[占用] {args.path} 正被 {cur.get('owner','未知')} 持有"
                  f"（剩 {cur.get('expires_at', 0) - time.time():.0f}s）", file=sys.stderr)
            return 1
        print(f"[ok] 租约到手 {lz.rel}  ttl={args.ttl}s  token={lz.token[:8]}")
        return 0

    if args.action == "release":
        cur = mgr.read(args.path)
        if cur is None:
            print(f"[空] {args.path} 上没有租约", file=sys.stderr)
            return 1
        if cur.owner != args.owner:
            print(f"[错误] {args.path} 的租约属于 {cur.owner}，你不能释放", file=sys.stderr)
            return 1
        mgr.release(cur)
        print(f"[ok] 已释放 {args.path}")
        return 0

    print(f"[错误] 未知 action {args.action}", file=sys.stderr)
    return 2


def cmd_gc(args) -> int:
    """磁盘回收：备份与分发产物的保守轮转（默认只报告，不删除）。"""
    from .gc import DEFAULT_KEEP, GcError, apply as gc_apply, plan as gc_plan
    cfg = _load(args)

    keep = args.keep if args.keep is not None else DEFAULT_KEEP
    include_deprecated = bool(getattr(args, "include_deprecated", False))
    try:
        plan_obj = gc_plan(cfg.workspace, keep=keep,
                           include_packages=args.include_packages,
                           include_deprecated=include_deprecated)
    except GcError as exc:
        print(f"[操作未完成] {exc}", file=sys.stderr)
        return 4

    mb = lambda n: "%.1f MB" % (n / 1024 / 1024)
    if args.json:
        print(json.dumps({
            "dry_run": not args.delete,
            "keep": keep,
            "include_deprecated": include_deprecated,
            "scanned": plan_obj.scanned,
            "scanned_bytes": plan_obj.scanned_bytes,
            "reclaim_bytes": plan_obj.reclaim_bytes,
            "delete": [c.to_dict() for c in plan_obj.delete],
            "keep_list": [c.to_dict() for c in plan_obj.keep],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"扫描 {plan_obj.scanned} 个备份/产物（{mb(plan_obj.scanned_bytes)}），"
          f"每组保留最新 {keep} 份")
    if not include_deprecated:
        print("[提示] 如需一并清理历史封存的 .deprecated_old_version 二进制文件（释放数 GB 空间），请附加 --include-deprecated 参数。")
    if not plan_obj.delete:
        print("[空] 没有可回收的文件")
        return 0

    print(f"\n可回收 {len(plan_obj.delete)} 个，共 {mb(plan_obj.reclaim_bytes)}：")
    for cand in plan_obj.delete:
        print(f"  {mb(cand.size):>12}  {cand.path.name}")

    if not args.delete:
        print("\n[只报告] 未删除任何文件。确认无误后加 --delete 执行。")
        return 0

    try:
        removed = gc_apply(plan_obj, confirmed=True)
    except GcError as exc:
        print(f"[操作未完成] {exc}", file=sys.stderr)
        return 4
    print(f"\n[ok] 已删除 {removed} 个文件，回收 {mb(plan_obj.reclaim_bytes)}")
    return 0


def cmd_test(args) -> int:
    """§5.7 TESTS-DIRECT-ENTRY / §7.21 AUDIT-PERF-VERIFICATION-LAYER: 统一官方分层测试入口。
    
    无论从根目录还是集成目录运行，均提供开箱即用的测试运行与发现能力，并支持按分层档位（targeted / contract / full）高速执行。
    """
    integ_dir = Path(__file__).resolve().parent.parent
    tools_dir = integ_dir / "tools"
    check_offline_script = tools_dir / "check_offline.py"
    
    cmd = [sys.executable, str(check_offline_script)]
    if getattr(args, 'tier', None):
        cmd.extend(["--tier", args.tier])
    if getattr(args, 'pattern', None):
        cmd.extend(["--pattern", args.pattern])
        
    res = subprocess.run(cmd)
    return res.returncode


# --------------------------------------------------------------------------- #
# §7.16 命令家族导引：33 个子命令里有历史残留，用户需要知道「从哪几条开始」。
# 本段只是 --help 的说明文字，不改变任何命令的注册与行为。
_RECOMMENDED_COMMANDS = (
    ("gui", "图形启动器：日常从这里进（看板、任务树、信箱一屏看全）"),
    ("init", "初始化工作空间（生成配置与 AGENTS.md 模板）；支持 --dry-run 预演"),
    ("tree", "项目任务树：status / show / claim / dispatch / checkpoint / handoff"),
    ("inbox", "查看某个成员的待办（--mine 看自己）"),
    ("answer", "把任务成果按原任务指定收件人正式回投"),
    ("status", "总览：各成员待办与审计统计"),
    ("test", "统一官方测试入口（§5.7）：开箱即用运行单测与离线回归套件"),
    ("lease", "多成员改同一文件前的互斥租约"),
    ("doctor", "环境自检：解析执行器路径与版本"),
)

# 兼容保留族：仍在、仍可用，但已被上面某条统一入口取代。这里如实标注，不删命令。
_LEGACY_COMMAND_FAMILIES = (
    ("boss / run", "早期「主从下发 → 执行循环」；平级协作请用 team + tree"),
    ("workflow / team / status", "三处都能看工作流；统一入口是 status"),
    ("handoff ↔ tree handoff", "两处交接；面向任务树请用 tree ... handoff"),
    ("install-skill / setup / skills", "三套技能安装；跨项目 Codex 工作流用 install-skill"),
)


def _HELP_EPILOG() -> str:
    """--help 尾注：高频5场景快速入口 + 功能域分组 + 兼容保留家族 + 稳定退出码表。"""
    lines = [
        "",
        "================================================================================",
        "【高频快速入口 · 常用命令 · 一个成员从接活到交付】",
        "  1. 打开桌面工作台:            agentbus gui [--workspace .]",
        "  2. 新项目一键初始化:          agentbus init --quickstart [--workspace .]",
        "  3. 找活（两处都看）:          agentbus inbox --mine --caller <AGENT>      # 别人投给你的",
        "                                agentbus claim-next --agent <AGENT>         # 任务树公共就绪池",
        "  4. 认领信箱任务:              agentbus claim --agent <AGENT> --id <TASK_ID>",
        "     （认领输出里的 claim_epoch / claim_id 与回执草稿路径要记下）",
        "  5. 交付（编辑草稿后）:        agentbus answer --agent <AGENT> --body-file <草稿> --auto-manifest",
        "",
        "【功能域分组】",
        "  [任务与流转]  tree · claim-next · wait · inbox · claim · answer · heartbeat · post · recover · discover · boss · run",
        "  [协作与接入]  member · contact · team · workflow · host-setup · handshake · mcp · lease · watchtower · share · handoff · trigger",
        "  [诊断与体检]  preflight · doctor · verify · timeline · attest · status · board · escalations · detect",
        "  [性能与维护]  benchmark · compact · gc · files · skills · setup · install-skill · worktree",
        "  [计划与配置]  gui · init · config · plan · test · lessons · repomap",
        "  [集成用]      worker（需在 Python 中注入处理器，命令行不会代成员干活）",
        "================================================================================",
        "",
        "兼容保留（仍可用，已被上面的统一入口取代）：",
    ]
    lines += [f"  {name:<26} {desc}" for name, desc in _LEGACY_COMMAND_FAMILIES]
    lines += ["", "退出码："]
    lines += [f"  {code:<3} {EXIT_CODES[code]}" for code in sorted(EXIT_CODES)]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agentbus",
        description="AgentBus 项目协作工作台：配置团队、分配任务、跟踪回执",
        epilog=_HELP_EPILOG(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"agentbus {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_ws(p):
        p.add_argument("--workspace", default=os.environ.get("AGENTBUS_WORKSPACE", "."), help="工作空间目录（默认当前目录）")

    p = sub.add_parser('test', help='统一官方测试入口（§5.7/§7.21）：运行单测与分层离线回归套件')
    p.add_argument('--pattern', default=None, help='测试匹配模式（如 test_*.py 或 test_agent_id_safety.py）')
    p.add_argument('--tier', choices=['targeted', 'contract', 'full'], default='full',
                   help='验证分层档位（§7.21）：targeted（定向测试，秒级）、contract（合同测试，分钟级）、full（全量发布前回归）')
    p.set_defaults(func=cmd_test)

    p = sub.add_parser('tree', help='项目任务树：查看、挂叶、认领、留痕和动态移交')
    add_ws(p)
    p.add_argument('action', choices=['status', 'show', 'add', 'append', 'claim', 'dispatch', 'checkpoint', 'continue',
                                   'handoff', 'takeover', 'worker-loop', 'spawn', 'import-deliveries', 'templates', 'import', 'export', 'prompt',
                                   'backup', 'restore', 'artifacts', 'verify', 'pass', 'reset', 'diagnose'])
    p.add_argument('--leaf', help='任务章节号（如 1.2）或稳定叶子 ID')
    p.add_argument('--reason', help='管理员裁决放行理由（配合 tree pass）')
    p.add_argument('--agent', help='本次动作的成员 ID')
    p.add_argument('--id', help='认领票据所属的原任务 ID')
    p.add_argument('--request-id', help='tree continue：同一新工作请求的稳定ID，重复执行不会重复建叶')
    p.add_argument('--claim-epoch', type=int)
    p.add_argument('--claim-id')
    p.add_argument('--file', help='项目相对叶子/树结构 JSON；不是信箱消息')
    p.add_argument('--template', help='内置任务树模板 ID，使用 tree templates 查看')
    p.add_argument('--preview', action='store_true',
                   help='tree templates：在终端渲染模板层级树（只读，不导入）')
    p.add_argument('--apply', action='store_true', help='确认导入；须同时提供预览输出的 --expected-sha256')
    p.add_argument('--expected-sha256', help='确认此前已核对的任务树预览摘要（source_sha256）')
    p.add_argument('--branch', help='新增叶子的业务子树 ID 或父节点章节号')
    p.add_argument('--title', help='新增叶子的任务标题')
    p.add_argument('--dependencies', help='任务依赖列表（逗号分隔，支持章节号如 1.2 或节点 ID）')
    p.add_argument('--goal', help='新项目主干目标或新增任务目标')
    p.add_argument('--artifacts', help='期望产物列表（逗号分隔相对路径，如 成果/foo.md）')
    p.add_argument('--capabilities', help='任务所需能力工种列表（逗号分隔，如 python,frontend）')
    p.add_argument('--export', help='将通过核验的交付文件导出到目标目录或 .zip 压缩包，默认要求完整交付')
    p.add_argument('--allow-partial', action='store_true', help='允许导出已有核验文件，明确标注为部分交付')
    p.add_argument('--summary-file', help='导出 Markdown 格式的产品交付汇总验收报告文件路径')
    p.add_argument('--note', default='', help='成果、断点或接手说明')
    p.add_argument('--successor', help='可选接手成员；省略时开放给合格空闲成员')
    p.add_argument('--owner-confirmed-inactive', action='store_true', help='已由用户明确确认原执行者停止，不可仅凭mtime使用')
    p.add_argument('--expected-owner', help='已核对的原执行成员；配合 --id 与 --claim-epoch 防止重置或接管已变化的任务')
    p.add_argument('--force', action='store_true', help='强制接管超时或挂起的任务')
    p.add_argument('--continuous', action='store_true', help='开启持续工蜂轮询模式')
    p.add_argument('--poll-interval', type=float, default=2.0, help='工蜂轮询周期（秒）')
    p.add_argument('--max-tasks', type=int, default=None, help='最多执行的任务数量上限')
    p.add_argument('--timeout-s', type=float, default=None, help='工蜂轮询总最大执行时间（秒）')
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser('worker', help='[集成用] 工蜂循环需由 Python 注入处理器；命令行直接运行会说明原因并退出')
    add_ws(p)
    p.add_argument('--agent', required=True, help='执行工蜂的成员 ID')
    p.add_argument('--continuous', action='store_true', help='开启持续监听模式')
    p.add_argument('--poll-interval', type=float, default=2.0, help='轮询周期秒数')
    p.add_argument('--max-tasks', type=int, default=None, help='最大执行任务数量')
    p.add_argument('--timeout-s', type=float, default=None, help='最长运行秒数')
    p.set_defaults(func=cmd_worker_loop)

    for command in ('verify', 'timeline', 'attest'):
        p = sub.add_parser(command, help='只读项目证据与全景体检：' + command)
        add_ws(p)
        if command == 'verify':
            p.add_argument('--all', action='store_true', help='执行项目级全景体检（§7.13）')
            p.add_argument('--json', action='store_true', help='以 JSON 格式输出全景体检结果')
            subject = p.add_mutually_exclusive_group(required=False)
            subject.add_argument('--task-id', help='当前项目中的任务 ID')
            subject.add_argument('--job-id', help='当前项目中的作业 ID')
        elif command == 'timeline':
            p.add_argument('--job-id', help='限定当前项目的一个作业；省略时显示项目时间线')
        else:
            p.add_argument('--task-id', required=True, help='当前项目中的任务 ID')
        formats = ['json', 'markdown', 'html'] if command == 'timeline' else ['json', 'markdown'] if command in ('attest', 'verify') else ['json']
        p.add_argument('--format', choices=formats, default='json')
        p.set_defaults(func=cmd_verify if command == 'verify' else cmd_evidence)

    p = sub.add_parser('heartbeat', help='出示领取时票据，写入真实执行会话心跳')
    add_ws(p)
    p.add_argument('--agent', required=True, help='执行该任务的智能体 ID')
    p.add_argument('--id', help='任务消息 id；若省略则自动推断当前工位唯一的在途任务')
    p.add_argument('--claim-epoch', type=int, help='本次认领时返回的原代次，续约时必填')
    p.add_argument('--claim-id', help='本次认领时返回的原票据，续约时必填')
    p.add_argument('--note', default='', help='心跳附言或执行进度说明')
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("init", help="初始化工作空间（生成配置与 AGENTS.md 模板）")
    add_ws(p)
    p.add_argument('--output-root', help='业务产出目录；可为绝对路径（例如另一块磁盘）')
    p.add_argument('--dry-run', action='store_true',
                   help='只列出将创建 / 已存在的文件，不写任何内容')
    p.add_argument('--quickstart', action='store_true',
                   help='零配置 3 分钟极速上手向导：自动探测本机已安装 Agent 并一键组建协同团队')
    p.add_argument('--json', action='store_true', help='以 JSON 输出（配 --dry-run 或 --quickstart）')
    p.add_argument('--members', help='明确选择初始启用的成员 ID（逗号分隔；§4.6）')
    p.add_argument('--coordinator', help='明确指定初始协调者（默认 codex；§4.6）')
    p.add_argument('--minimal', action='store_true', help='极简初始化：仅启用协调者，不激活未配置/未连接成员（§4.6）')
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("config", help="查看当前配置")
    add_ws(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("benchmark", help="测量 CLI 启动性能基准与生成免解包加速包装器")
    add_ws(p)
    p.add_argument('--exe', help='单文件 AgentBus.exe 路径（默认自动查找）')
    p.add_argument('--runs', type=int, default=3, help='每项测试运行轮次（默认 3）')
    p.add_argument('--wrappers', action='store_true', help='生成免解包快速调用脚本 (agentbus-fast)')
    p.add_argument('--json', action='store_true', help='以 JSON 格式输出测量数据')
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("compact", help="信箱冷热分层归档与安全压缩 (Safe Compaction)")
    add_ws(p)
    p.add_argument('--keep-recent', type=int, default=5, help='每个成员保留的最新热任务数（默认 5）')
    p.add_argument('--apply', action='store_true', help='确认执行压缩归档并清理已归档文件')
    p.add_argument('--query', help='从冷存储归档中查询指定历史任务 ID')
    p.add_argument('--json', action='store_true', help='以 JSON 格式输出规划或查询结果')
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("host-setup", help="多宿主 IDE 规则注入与无缝适配矩阵 (Cursor, Windsurf, Trae, Codex)")
    add_ws(p)
    p.add_argument('--host', choices=["cursor", "windsurf", "trae", "codex", "all"], default="all", help="目标宿主环境（默认 all）")
    p.add_argument('--apply', action='store_true', help='安全写入/注入对应宿主的规则与配置文件')
    p.add_argument('--json', action='store_true', help='以 JSON 格式输出诊断或文件清单')
    p.set_defaults(func=cmd_host_setup)

    p = sub.add_parser("status", help="总览：各 agent 待办与审计统计")
    add_ws(p)
    p.add_argument("--all", action="store_true", help="显示全部信箱（包含空的旧版保留信箱）")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("boss", help="下发一个目标（拆解 → 派发 → 执行 → 汇报）")
    add_ws(p)
    p.add_argument("goal", help="一句话目标")
    p.add_argument("--seconds", type=float, default=0,
                   help="执行循环时长（0 = 只派发不等待）")
    p.add_argument("--worker", default=None, help="跳过拆解，直接派给指定执行者")
    p.add_argument("--no-review", action="store_true",
                   help="旧参数兼容；独立同伴监督仍按策略运行")
    p.add_argument("--await-manual", action="store_true",
                   help="有 GUI 成员待办时继续低频等待 routed result，受 --seconds 限制")
    p.set_defaults(func=cmd_boss)

    p = sub.add_parser("run", help="启动执行循环（消费邮箱里的任务）")
    add_ws(p)
    p.add_argument("--seconds", type=float, default=300)
    p.add_argument("--agent", help="只运行此已接入成员，保留其他成员任务给其对话")
    p.add_argument("--no-review", action="store_true",
                   help="旧参数兼容；独立同伴监督仍按策略运行")
    p.add_argument("--follow-ups", action="store_true",
                   help="处理结果后允许协调者派发有限后续任务，不延长 --seconds")
    p.add_argument("--await-manual", action="store_true",
                   help="有 GUI 成员待办时继续低频等待 routed result，受 --seconds 限制")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("inbox", help="查看某个 agent 的待办")
    add_ws(p)
    p.add_argument("--agent", default=None,
                   help="已登记成员；与 --mine 二选一")
    p.add_argument("--mine", action="store_true",
                   help="查看调用者自己的信箱；必须同时提供 --caller")
    p.add_argument("--caller", default=None,
                   help="当前已登记成员身份（配合 --mine）")
    p.add_argument("--body", action="store_true", help="连正文一起打印")
    p.add_argument("--brief", action="store_true",
                   help="三行简报：待办数 + 每条一行（类型/标题/来源/相对时间/回投目标）")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("claim", help="原子认领一个成员 ready 信箱里的待办，或从就绪池自主抢单")
    add_ws(p)
    p.add_argument("--agent", required=True, help="已登记成员身份")
    p.add_argument("--id", default=None,
                   help="指定消息 ID；省略时认领 ready 中排序第一条")
    p.add_argument("--next", action="store_true",
                   help="自由调度模式下自主抢单认领公共就绪池中的下一个任务（Autonomous Pull）")
    p.add_argument("--wait", type=float, default=0.0,
                   help="自主抢单时若暂无就绪任务，等待新任务解锁的最长秒数（默认 0 不等待）")
    p.add_argument("--worktree", action="store_true",
                   help="认领时自动在 .agentbus/worktrees/ 创建物理隔离的 Git Worktree 沙箱目录")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("claim-next", help="自由调度：从公共就绪任务池中自主抢单认领下一个任务（Autonomous Pull）")
    add_ws(p)
    p.add_argument("--agent", required=True, help="已登记成员身份")
    p.add_argument("--wait", type=float, default=0.0,
                   help="若暂无就绪任务，等待新任务解锁的最长秒数（默认 0 不等待）")
    p.add_argument("--tier", choices=["critical", "standard", "low", "free"],
                   help="优先认领的复杂度/成本等级（如 critical 留给主脑，low 优先由廉价模型接单）")
    p.add_argument("--worktree", action="store_true",
                   help="认领时自动在 .agentbus/worktrees/ 创建物理隔离的 Git Worktree 沙箱目录")
    p.set_defaults(func=cmd_claim_next)

    p = sub.add_parser("wait", help="免轮询等待在途任务交付或全树更新（低功耗长任务值守，不消耗模型 Token）")
    add_ws(p)
    p.add_argument("--id", help="等待指定的任务 ID（默认推断当前工位的在途任务）")
    p.add_argument("--agent", help="指定等待该 Agent 工位的在途任务交付")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="最长等待秒数（默认 120 秒）")
    p.add_argument("--poll-interval", type=float, default=1.0,
                   help="后台文件变动探测间隔秒数（默认 1.0 秒）")
    p.add_argument("--notify", action="store_true",
                   help="任务交付完成时触发 Windows 桌面原生 Toast 提示音并热装载剪贴板")
    p.set_defaults(func=cmd_wait)


    p = sub.add_parser("escalations", help="查看需要你决定的事项")
    add_ws(p)
    p.set_defaults(func=cmd_escalations)

    p = sub.add_parser("handoff", help="把待人工接手的任务渲染成可粘贴的 prompt")
    add_ws(p)
    p.add_argument("--agent", default=None, help="只处理指定 agent（默认全部 GUI agent）")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("answer", help="把任务成果按原任务指定收件人正式回投")
    add_ws(p)
    p.add_argument('--claim-epoch', type=int, help='claim 返回的原始认领代次')
    p.add_argument('--claim-id', help='claim 返回的原始票据；禁止从当前任务反补')
    p.add_argument("--agent", required=True, help="接手该任务的 agent 名")
    p.add_argument("--id", help="任务消息 id；若省略则自动推断当前工位唯一的在途任务")
    body = p.add_mutually_exclusive_group(required=False)
    body.add_argument("--body", help="结论/产物说明（警告：在 Windows PowerShell 管道中中文可能被转换为问号，推荐使用 --body-file）")
    body.add_argument('--body-file', help='工作区相对 UTF-8 正文文件；请显式指定已填写的原认领 .reply.md 草稿，配合 --auto-manifest 免算正文 SHA256')
    p.add_argument('--expected-body-sha256', help='body-file 原始字节 SHA256（含BOM）；配合 --auto-manifest 或传 auto 时由系统自动计算')
    p.add_argument("--artifacts-file", "--manifest", dest="artifacts_file",
                   help="本工作区相对路径的产物清单 JSON，逐项 path/bytes/sha256")
    p.add_argument("--auto-manifest", action="store_true",
                   help="根据任务声明的 expected_artifacts 自动校验物理文件并生成产物清单")
    p.add_argument("--artifacts",
                   help="直接以逗号分隔指定实际产物路径，自动校验物理文件并生成产物清单（如 成果/app.py）")
    p.add_argument("--failed", action="store_true",
                   help="成员明确报告失败：保存 failed 终态及失败回执；命令成功仅表示投递成功")
    p.add_argument("--result-to", default=None,
                   help="人工明确指定结果收件人，用于补救旧任务路由缺失/冲突")
    p.add_argument("--tokens", type=int, default=None,
                   help="本次任务实际消耗的 Token 数量（用于全项目成本核算与预算追踪）")
    p.add_argument("--cost", type=float, default=None,
                   help="本次任务预估或实际产生的成本数值（元/美元）")
    p.add_argument("--lesson", default=None,
                   help="顺手沉淀本次任务的实战避坑经验到 .agentbus/lessons.jsonl（供后续同模块任务自动注入）")
    p.add_argument("--verify-cmd", default=None,
                   help="交单前自动运行的验收测试命令（非零返回码将拦截交单）")
    p.add_argument("--skip-verify", action="store_true",
                   help="跳过交单前语法和指定命令检查并记录豁免；不跳过票据、锁、产物及独立审核")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("discover", help="Beads 风格任务衍生：执行期间发现新问题时，动态在同分支挂载带溯源链的待办任务")
    add_ws(p)
    p.add_argument("--agent", default=None, help="发现人成员 ID")
    p.add_argument("--title", required=True, help="新发现任务的简明标题")
    p.add_argument("--goal", default="", help="新发现任务的详细描述与验收条件")
    p.add_argument("--from", dest="from_task", default=None, help="源任务 ID 或章节号（省略则自动关联当前工位在途任务）")
    p.add_argument("--artifacts", default="", help="预期产物相对路径，逗号分隔")
    p.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("repomap", help="Aider 风格 AST 代码结构地图：提取关联文件的类与函数签名切片")
    add_ws(p)
    p.add_argument("--paths", default="", help="目标相对路径列表，逗号分隔")
    p.add_argument("--query", default="", help="检索关键词，逗号分隔")
    p.add_argument("--max-files", type=int, default=8, help="最大扫描文件数（默认 8）")
    p.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    p.set_defaults(func=cmd_repomap)

    p = sub.add_parser("lessons", help="Reflexion 风格实战避坑经验库：查看或录入项目级避坑经验")
    add_ws(p)
    p.add_argument("action", nargs="?", choices=["list", "add"], default="list", help="list=查看经验库，add=新增一条避坑经验")
    p.add_argument("--agent", default=None, help="提交经验的成员 ID")
    p.add_argument("--lesson", default=None, help="add 模式下要记录的避坑经验正文")
    p.add_argument("--task-id", default="", help="关联的任务 ID（可选）")
    p.add_argument("--paths", default="", help="关联的目录或文件相对路径，逗号分隔")
    p.add_argument("--query", default="", help="按关键词筛选经验")
    p.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    p.set_defaults(func=cmd_lessons)

    p = sub.add_parser("worktree", help="Git Worktree 并发物理沙箱管理：列出、创建或同步隔离沙箱")
    add_ws(p)
    p.add_argument("action", nargs="?", choices=["list", "create", "sync"], default="list", help="list=查看沙箱，create=创建沙箱，sync=同步产物并清理")
    p.add_argument("--agent", default=None, help="关联的成员 ID")
    p.add_argument("--id", default=None, help="关联的任务 ID")
    p.add_argument("--keep", action="store_true", help="sync 后保留沙箱目录不删除")
    p.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    p.set_defaults(func=cmd_worktree)

    p = sub.add_parser('retire', help='确认原执行者停止后退役单条超期认领；保留历史，不重排执行')
    add_ws(p)
    p.add_argument('--agent', required=True, help='原执行成员')
    p.add_argument('--id', required=True, help='已核对的原任务ID')
    p.add_argument('--claim-epoch', type=int, required=True, help='已核对的原认领代次')
    p.add_argument('--reason', required=True, help='退役原因与已有断点')
    p.add_argument('--owner-confirmed-inactive', action='store_true', help='明确确认原会话已停止；心跳超时不是停止证明')
    p.set_defaults(func=cmd_retire)

    p = sub.add_parser('recover', help='确认原认领者已停止后恢复指定单条 processing；不验活、不重置预算')
    p.add_argument('--workspace', default='.', help='当前项目')
    p.add_argument('--agent', required=True, help='已登记成员')
    p.add_argument('--id', required=True, help='仅这一条消息 ID')
    p.add_argument('--owner-confirmed-inactive', action='store_true',
                   help='明确确认原执行会话已停止；文件年龄不构成此确认')
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("doctor", help="自检：解析 codex 路径与版本，支持工作空间与环境变量诊断")
    p.add_argument("--workspace", default=None, help="工作空间目录（可选）")
    p.add_argument("--env", action="store_true", help="打印关键环境变量诊断")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("preflight", help="Pre-flight 刚性体检（§9.1）：环境合规与二进制绝育防线")
    add_ws(p)
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("install-skill", help="安装跨项目 Codex 工作流 skill")
    p.set_defaults(func=cmd_install_skill)

    p = sub.add_parser('setup', help='安装、检查和移除成员工作流，保留用户项目')
    p.add_argument('action', choices=['install', 'status', 'uninstall', 'check'])
    p.add_argument('--workspace', help='check 操作要检查的项目')
    p.add_argument('--home', help='指定用户根目录；测试可用隔离目录，不覆盖系统 HOME')
    p.add_argument('--state-dir', help='指定安装登记目录')
    p.add_argument('--apply', action='store_true', help='实际移除；不写此参数只预览')
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser('workflow', help='准备/检查小组项目工作流，生成新对话接入句')
    p.add_argument('action', choices=['prepare', 'status', 'prompt'])
    p.add_argument('--workspace', default=os.environ.get("AGENTBUS_WORKSPACE", "."), help="工作空间目录（默认当前目录）")
    p.add_argument('--agent', default='codex', help='本项目任一成员ID')
    p.add_argument('--frozen-executable',
                   help='显式登记正式冻结版 EXE 的完整路径；相对路径以工作区为基准')
    p.set_defaults(func=cmd_workflow)

    p = sub.add_parser('team', help='任意软件发起/继续持久小组作业（不调用模型）')
    p.add_argument('action', choices=['configure','start','status','connections','join','dispatch','checkpoint','finish'])
    add_ws(p)
    p.add_argument('--members', help='配置时明确选择的成员ID，逗号分隔')
    p.add_argument('--coordinator')
    p.add_argument('--caller', help='当前软件成员ID，组长始终取项目配置')
    p.add_argument('--goal')
    p.add_argument('--request-id', help='同一请求重入时复用的ID')
    p.add_argument('--from-job', help='明确关联本项目已结束/受阻的旧阶段；不延长旧期限或复制旧任务')
    p.add_argument('--job-id')
    p.add_argument('--plan-file')
    p.add_argument('--note', default='')
    p.add_argument('--verified', action='store_true', help='产物和回执已实际验收')
    p.set_defaults(func=cmd_team)

    p = sub.add_parser('member', help='成员登记、成对接入邀请、一键新Agent就位及公开能力档案')
    p.add_argument('action', choices=['add', 'catalog', 'pack', 'invite', 'status', 'observe', 'card', 'library-save', 'library-get', 'import', 'onboard'])
    p.add_argument('--workspace', help='登记成员时必填；查看目录不读取项目')
    p.add_argument('--id', help='登记成员时必填；查看目录可按ID筛选')
    p.add_argument('--name')
    p.add_argument('--preset', choices=['generic', 'claude', 'cursor', 'windsurf'], default='generic', help='预设环境模板')
    p.add_argument('--json', action='store_true', help='以 JSON 格式输出结果')
    p.add_argument('--kind', choices=['gui','acp'], default='gui')
    p.add_argument('--transport', choices=['file', 'cli'], default='file',
                   help='默认文件协作；CLI 仅允许已验收的可启动成员')
    p.add_argument('--command-json', help='ACP候选 argv 的JSON数组，非shell命令')
    p.add_argument('--caller', help='已选组员身份，不因组长身份扩大权限')
    p.add_argument('--member', help='已保存团队中的成员ID')
    p.add_argument('--coordinator', help='协调者成员ID')
    p.add_argument('--task-id', help='接入样例任务ID')
    p.add_argument('--deploy', action='store_true', help='实际投放摸底任务到成员就绪信箱')
    p.add_argument('--request-id', help='相同邀请重试复用此ID，不重复创建任务')
    p.add_argument('--readonly', action='store_true', help='只读样例，不要求写工具或创建输出')
    p.add_argument('--invite-id');p.add_argument('--event', choices=['copied','sent','self_reported'])
    p.add_argument('--source', help='观察来源，不把复制当送达')
    p.add_argument('--card-file', help='当前工作区相对公共JSON档案文件')
    p.add_argument('--library-root', help='明确指定的AgentBus公共档案目录，不读取厂商私有目录')
    p.add_argument('--expected-sha256', help='保存/导入目标的原始摘要，首次MISSING')
    p.set_defaults(func=cmd_member)

    p = sub.add_parser('share', help='导出应用分发包或导入团队模板')
    p.add_argument('action', choices=['export','import']); add_ws(p)
    p.add_argument('--output'); p.add_argument('--executable'); p.add_argument('--template')
    p.add_argument('--replace', action='store_true', help='明确替换所选成员，保留本机权限与任务')
    p.set_defaults(func=cmd_share)

    p = sub.add_parser('files', help='统一 UTF-8 分页读、摘要比较写入与产物核对',
        description='read 保留原换行并返回 next_line；write 必须带 caller 与旧摘要。'
        'describe 默认只回显；加 manifest-file、caller、expected-sha256 可直接保存单项清单。'
        '产物清单是 JSON 数组，每项只允许 path/bytes/sha256 三字段；'
        '默认访问控制目录，业务产物请显式使用 --root output。')
    p.add_argument('action', choices=['read', 'write', 'describe', 'verify']); add_ws(p)
    p.add_argument('--root', choices=['control', 'output'], default='control',
                   help='文件根：control=项目控制目录（默认），output=业务产出目录')
    p.add_argument('--path', help='read/write/describe 的工作区相对文件路径')
    p.add_argument('--text'); p.add_argument('--caller', help='写文件/清单的已选小组成员')
    p.add_argument('--expected-sha256', help='原文件摘要；首次创建用 MISSING')
    p.add_argument('--start-line', type=int, default=1); p.add_argument('--max-lines', type=int, default=200)
    p.add_argument('--manifest-file', help='verify 的输入清单；describe 的可选输出清单（须 caller 与 CAS）')
    p.add_argument('--dry-run', action='store_true'); p.set_defaults(func=cmd_files)

    p = sub.add_parser('skills', help='技能包检查、宿主目标与项目级事务同步',
        description='读取所选项目内完整技能目录，含 scripts/references/assets；'
        '拒绝私有配置、链接、越界或扫描中变化。只读阶段没有同步动作。')
    p.add_argument('action', choices=['inspect', 'preview', 'targets', 'sync', 'rollback']); add_ws(p)
    p.add_argument('--path', help='项目相对源技能包目录，必须含SKILL.md')
    p.add_argument('--confirm', action='store_true', help='执行项目级同步或回滚；未指定时仅预览')
    p.add_argument('--target-path', help='预览的项目相对目标目录，不存在也不会创建')
    p.add_argument('--host', help='宿主成员标识，支持扩展ID')
    p.add_argument('--scope', choices=['project', 'global'], default='project',
                   help='仅标记预览意图；global不会读取用户私有目录')
    p.add_argument('--route', choices=['desktop', 'cli', 'independent'], default='desktop')
    p.add_argument('--record-file', help='项目相对部署记录JSON；不自动读取用户配置')
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser('plan', help='总体计划：提案、小组实际评估、带理由采用与快照')
    p.add_argument('action', choices=['status', 'snapshot', 'propose', 'review', 'adopt', 'assess', 'convergence', 'adopt-change', 'withdraw-change']); add_ws(p)
    p.add_argument('--caller'); p.add_argument('--file'); p.add_argument('--reason', default='')
    p.add_argument('--reviewers', default=''); p.add_argument('--request-id')
    p.add_argument('--expected-current-sha256', help='当前计划摘要；首个计划用 MISSING')
    p.add_argument('--proposal-id'); p.add_argument('--task-id'); p.add_argument('--plan-id')
    p.add_argument('--change-id', help='待采用或撤回的变更提案标识，如 CH-xxxx')
    p.add_argument('--verdict', choices=['accept', 'accept with changes', 'reject'])
    p.add_argument('--reviewed-file', help='既有评估明确引用的同工作区冻结正文')
    p.add_argument('--evidence-workspace', help='明确导入当前项目内子工作区的原始正式意见，保留来源')
    p.add_argument('--dispositions-json', default='{}', help='逐成员取舍理由对象')
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("detect", help="探测本机装了哪些 agent（可 --write 并入配置）")
    p.add_argument("--workspace", default=None, help="配合 --write 使用")
    p.add_argument("--write", action="store_true", help="把探测结果并入该工作空间的配置")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--models", action="store_true", help="列出 HTTP 通道可用模型")
    p.set_defaults(func=cmd_detect)

    p = sub.add_parser("gui", help="打开图形启动器")
    p.add_argument("--workspace", default=None, help="预填工作空间")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("mcp", help="以 MCP 服务器方式暴露总线（给 TRAE/WorkBuddy 用）")
    add_ws(p)
    p.add_argument("--identity", default=None,
                   help="把 MCP 发送者固定到该已启用 agent")
    p.add_argument("--session-tools", action="store_true",
                   help="仅向此 Agent 会话开放身份固定的平级消息工具")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("lease", help="文件租约：多 agent 改同一文件时的互斥")
    add_ws(p)
    p.add_argument("action", choices=["acquire", "release", "list", "sweep"])
    p.add_argument("--path", default=None, help="相对工作空间的路径")
    p.add_argument("--owner", default=None, help="谁要持有")
    p.add_argument("--ttl", type=int, default=900, help="租约有效期（秒）")
    p.add_argument("--note", default="", help="备注")
    p.set_defaults(func=cmd_lease)

    p = sub.add_parser("board",
                       help="一屏待办看板：一条命令输出全组成员待办/处理中/阻塞及超期积压（§7.11）")
    add_ws(p)
    p.add_argument("--me", "--caller", dest="me", default=None,
                   help="只查看指定成员的待办看板")
    p.add_argument("--warn-hours", type=float, default=48.0,
                   help="判定积压的超期小时阈值（默认 48.0 小时，即 2 天）")
    p.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("gc", help="磁盘回收：备份与分发产物的保守轮转（默认只报告，不删除）")
    add_ws(p)
    p.add_argument("--keep", type=int, default=None,
                   help="每个备份组保留最新几份（默认 3）")
    p.add_argument("--include-packages", action="store_true",
                   help="同时把分发包 ZIP 纳入候选（默认不含）")
    p.add_argument("--include-deprecated", action="store_true",
                   help="同时把历史封存的 .deprecated_old_version 二进制纳入候选（默认不含）")
    p.add_argument("--delete", action="store_true",
                   help="真正执行删除；不加此开关仅报告")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("post", help="平级投递：往某个 agent 的信箱投一条消息（谁都能用）")
    add_ws(p)
    p.add_argument('--claim-epoch', type=int, help='result 的原始认领代次')
    p.add_argument('--claim-id', help='result 的领取时票据')
    p.add_argument("--from", dest="sender", required=True, help="发送者名字")
    p.add_argument("--to", required=True, help="接收者名字")
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--intent", help="一句话：要对方干什么；长正文优先使用 --body-file")
    body.add_argument('--body-file', help='工作区相对 UTF-8 正文文件；保留空格/换行/引号，最多8MiB和128页，单页界限同files read')
    p.add_argument('--expected-body-sha256', help='body-file 原始字节 SHA256（含BOM）；文件入口必须提供，不删除或改写原文')
    p.add_argument("--title", default=None)
    p.add_argument("--refs", default=None, help="相关文件，逗号分隔")
    p.add_argument("--type", default="task", choices=["task", "goal", "review", "info", "result"])
    p.add_argument("--result-to", default=None,
                   help="task/goal/review 的明确结果收件人；可显式写 sender 或 director")
    p.add_argument("--reply-to", default=None, help="result 对应的原任务消息 ID")
    p.add_argument('--artifacts-file', '--manifest', dest='artifacts_file',
                   help='result 的工作区相对产物清单，每项仅 path/bytes/sha256')
    p.add_argument("--hop", type=int, default=0, help="转手次数（防互相派活停不下来）")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser("watchtower",
                       help="唤醒者：盯所有信箱，有活就通知你，并给出该对谁说哪句话")
    add_ws(p)
    p.add_argument("--seconds", type=float, default=0,
                   help="运行时长（0 = 一直盯着，Ctrl+C 退出）")
    p.add_argument("--once", action="store_true", help="只检查一次就退出（适合配合定时任务）")
    p.add_argument("--no-toast", action="store_true", help="不发 Windows 气泡通知")
    p.set_defaults(func=cmd_watchtower)

    p = sub.add_parser("handshake",
                       help="互通协议协商：生成给各 agent 的指令 / 汇总它们的回答")
    p.add_argument("--workspace", default=".", help="工作空间（默认当前目录）")
    p.add_argument("--agents", default=None,
                   help="可选，逗号分隔，只是让指令里点名提示一下")
    p.add_argument("--collect", action="store_true", help="汇总已收到的回复")
    p.add_argument("--short", action="store_true",
                   help="极简版：只问 1/2/3/4 四个决定性问题")
    p.add_argument("--json", action="store_true", help="以 JSON 输出（配 --collect）")
    p.set_defaults(func=cmd_handshake)
    p = sub.add_parser("contact", help="管理成员桌面联系卡（invite/show/retire/checkin）")
    add_ws(p)
    p.add_argument("action", nargs="?", choices=["show", "invite", "retire", "checkin"], default="show", help="操作类型")
    p.add_argument("--agent", required=True, help="成员 ID")
    p.add_argument("--session", default=None, help="原生聊天会话标识（若缺省则自动探测）")
    p.add_argument("--connector", default=None, help="连接器 ID")
    p.add_argument("--installation", default=None, help="安装标识")
    p.add_argument("--revision", type=int, default=None, help="CAS 预期版本号")
    p.add_argument("--quiet", action="store_true", help="静默模式")
    p.set_defaults(func=cmd_contact)

    p = sub.add_parser("trigger", help="管理项目自动触发服务与策略（status/stop/resume/reconcile/diagnostics）")
    add_ws(p)
    p.add_argument("action", nargs="?", choices=["status", "stop", "resume", "reconcile", "diagnostics", "run"], default="status", help="操作类型")
    p.add_argument("--json", action="store_true", help="以 JSON 输出诊断包")
    p.add_argument("--once", action="store_true", help="仅执行单次扫描投递后退出")
    p.add_argument("--seconds", type=float, default=0.0, help="最多运行指定秒数后退出")
    p.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数（默认 2.0s）")
    p.set_defaults(func=cmd_trigger)

    return ap


def main(argv: list[str] | None = None) -> int:
    _configure_stdio_utf8()
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except AdapterError as exc:
        print(f"[执行器错误] {exc}\n    [下一步建议] 请执行 'agentbus doctor' 检查执行器环境与路径，或切回 file 纯文件协作路线。", file=sys.stderr)
        return EXIT_ADAPTER
    except (BusError, ValueError, RuntimeError, WorkflowError, SkillInstallError, OSError) as exc:
        msg = str(exc)
        print(f"[操作未完成] {msg}", file=sys.stderr)
        lowered = msg.lower()
        if args.func is cmd_tree and args.action == 'import':
            print("    [下一步建议] 按上面的原因处理后重新预览。跨项目复用可使用「文件 → 导出当前任务树…」生成的模板。", file=sys.stderr)
        elif "lock_expired" in lowered:
            print(
                "    [下一步建议] 任务独占锁已过期。先保留成果和原票据，确认原执行者已停止后，"
                "使用 agentbus recover --agent <成员ID> --id <任务ID> --owner-confirmed-inactive 恢复，"
                "再重新 claim 并保存新票据。旧票据不能继续交付。", file=sys.stderr)
        elif "lock" in lowered or "lease" in lowered:
            print("    [下一步建议] 目标资源正被其他成员独占锁定，请执行 'agentbus lease list' 检查持有者并等待释放。", file=sys.stderr)
        elif "--claim-epoch" in msg or "--claim-id" in msg:
            print("    [下一步建议] 缺少本次认领的票据参数：把 claim 输出里的 claim_epoch / claim_id 原样传入 "
                  "--claim-epoch / --claim-id。不要因此重新认领。", file=sys.stderr)
        elif "ticket" in lowered or "claim" in lowered or "票据" in msg:
            print("    [下一步建议] 认领票据不匹配或已失效：先用 'agentbus inbox --mine --caller <你的ID>' 确认任务是否仍在你名下；"
                  "确属失效再重新 claim。", file=sys.stderr)
        elif "not_found" in lowered and "artifact" in lowered:
            folder = _load(args).output_root
            print(f"    [下一步建议] 请把回执列出的文件保存到业务产出目录 {folder} 下，再提交。", file=sys.stderr)
        elif "不存在" in msg and ("产物" in msg or "artifact" in lowered):
            print("    [下一步建议] 声明的产物文件还不存在：先把产物写到任务声明的路径（相对业务产出目录），或用 --artifacts 指定实际路径。", file=sys.stderr)
        elif "artifact" in lowered or "manifest" in lowered or "产物" in msg:
            print("    [下一步建议] 交付产物未通过核验：确认文件已写完且未在交付后被改动；一般用 --auto-manifest 自动生成清单即可。", file=sys.stderr)
        elif "dependency" in lowered or "blocked" in lowered:
            print("    [下一步建议] 前序依赖任务尚未全部交付，请执行 'agentbus tree status' 查看前序依赖拓扑。", file=sys.stderr)
        return EXIT_OPERATION
    except KeyboardInterrupt:
        print("\n中断", file=sys.stderr)
        return EXIT_INTERRUPT


if __name__ == "__main__":
    sys.exit(main())
