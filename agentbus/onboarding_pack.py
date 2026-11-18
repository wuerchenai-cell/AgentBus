"""
Onboarding Pack Generator (通用成员接入包生成器)
Source: Antigravity (TEAM-20261008-IMPLEMENT, task AG-EXT)
Date: 2026-10-08

核心目标：
自动生成协调者指令、新成员指令、有界样例任务、能力卡、限制和跨项目可复用证据；
严格遵守隔离契约：不继承权限、登录状态、当前在线或 processing 任务。
"""
from __future__ import annotations

import json
import base64
import hashlib
import os
import tempfile
import time
import uuid
import copy
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agentbus.member_adapter import MemberAdapter, MemberRegistry, GLOBAL_REGISTRY
from .member_adapter import validate_member_id, DEFAULT_FILE_CAPABILITIES
from .public_files import PublicFiles, PublicFileError
from .capability_card import validate_card
from .silent_debug import note_silent


def _now_iso() -> str:
    """生成带有时区信息的当前 ISO 时间戳"""
    return datetime.now(timezone.utc).astimezone().isoformat()


def build_onboarding_context(workspace: Path | str, adapter: MemberAdapter,
                             coordinator: Optional[str] = None, task_id: Optional[str] = None) -> Dict[str, Any]:
    """构建新成员接入的标准上下文"""
    from .bus import new_id
    public = PublicFiles(workspace)
    ws = public.root
    config = public.read_json('.agentbus/config.json') or {}
    coordinator = coordinator or config.get('director') or config.get('coordinator')
    validate_member_id(coordinator)
    tid = task_id or new_id()
    if not isinstance(tid, str) or not re.fullmatch(r'm-[A-Za-z0-9-]{1,100}', tid):
        raise ValueError('task ID must be a portable message ID')
    head, revision = public.plan_head()
    sample_dir = f"成果/成员接入/{adapter.member_id}-onboarding"
    input_path = f"{sample_dir}/input.txt"
    output_path = f"{sample_dir}/output.txt"
    manifest_path = f"{sample_dir}/artifacts-manifest.json"
    inbox_path = f".agentbus/agents/{adapter.member_id}/ready/{tid}.json"
    launcher = None
    if public.read_bytes('.agentbus/workflow.json') is not None:
        launcher, _ = _registered_launcher(public)
    
    return {
        "workspace": str(ws).replace("\\", "/"),
        "member_id": adapter.member_id,
        "display_name": adapter.display_name,
        "coordinator": coordinator,
        "task_id": tid,
        "writable": adapter.writable,
        "file_capabilities": list(adapter.file_capabilities),
        "wake_adapter": adapter.wake_adapter,
        "skill_path": adapter.default_skill_path(),
        "input_path": input_path,
        "output_path": output_path,
        "manifest_path": manifest_path,
        "inbox_path": inbox_path,
        "plan_id": revision['revision_id'] if revision else None,
        "plan_sha256": revision['content_sha256'] if revision else None,
        "created_at": _now_iso(),
        "deadline_ts": time.time() + 1800,
        "launcher_command": launcher,
    }


def build_leader_instructions(context: Dict[str, Any]) -> str:
    """生成给协调者的操作与验收指令"""
    from .task_assignment import _quote
    prefix = context.get('launcher_command')
    verification = (f"`{prefix} files verify --workspace {_quote(context['workspace'])} "
                    f"--manifest-file {_quote(context['manifest_path'])}`" if prefix else
                    "项目 launcher 未登记；先在正确工作台保存设置，再通过正规 files verify 核验")
    return (
        f"# 给小组协调者 ({context['coordinator']}) 的接入引导\n\n"
        f"接入成员 `{context['display_name']}` (`{context['member_id']}`)，先核对本项目登记。\n\n"
        f"1. **发布摸底任务**: 请确保样例任务 `{context['task_id']}` 已投放至其就绪信箱:\n"
        f"   `{context['inbox_path']}`\n"
        f"2. **输入与预期产物**:\n"
        f"   - 输入文件: `{context['input_path']}`\n"
        f"   - 预期输出: `{context['output_path']}`\n"
        f"3. **独立核验标准**:\n"
        f"   待新成员提交正式回执后，通过 agentbus files verify 操作 {verification} 核对产物路径、字节数与 SHA256；\n"
        f"   核验一致后方可将接入事实记录进项目能力档案。\n"
    )


def build_newcomer_instructions(context: Dict[str, Any]) -> str:
    """生成给新成员执行的两条自足自然语言指令"""
    from .task_assignment import _quote
    prefix = context.get('launcher_command')
    arguments = (f" --workspace {_quote(context['workspace'])} --agent {_quote(context['member_id'])}"
                 f" --id {_quote(context['task_id'])}")
    ticket = " --claim-epoch '<claim_epoch>' --claim-id '<claim_id>'"
    if prefix:
        claim_step = (f"认领（agentbus claim）：\n`{prefix} claim{arguments}`\n"
            "保存领取返回的原始 claim_epoch / claim_id；不从后续任务文件反补。立即发送票据绑定心跳：\n"
            f"`{prefix} heartbeat{arguments}{ticket}`\n")
        answer_step = (f"`{prefix} answer{arguments}{ticket} --body-file '<回执路径>'"
                       f" --expected-body-sha256 '<正文SHA256>' --artifacts-file {_quote(context['manifest_path'])}`\n")
    else:
        claim_step = ("认领（agentbus claim）前先在正确程序保存项目工作流，登记 launcher；"
                      "入口未登记，不猜命令或历史 EXE。没有正规执行工具时在当前对话自报待接手。\n")
        answer_step = ("正规入口尚未登记。正式 agentbus answer 需要 --claim-epoch、--claim-id、"
                       "--body-file、--expected-body-sha256 与 --artifacts-file；"
                       "先登记入口，否则只自报待转录，不冒称正规回投。\n")
    if context.get("writable", True):
        action_step = (
            f"2. **读取并写出产物**: 读取 `{context['input_path']}` 首行文本，"
            f"在 `{context['output_path']}` 写入完全相同的内容（不加多余换行），"
            f"将结论写到项目内 UTF-8 回执文件，计算其原始字节 SHA256 后回投给 `{context['coordinator']}`：\n"
            f"{answer_step}"
            f"清单仅列输出文件的项目相对 path、bytes、SHA256，不把清单自身列为摘要条目；"
            f"先写清单并核验后提交。认领票据需留在当前会话，processing 必须由拥有者提交。"
        )
    else:
        action_step = (
            f"2. **只读分析并回执**: 读取 `{context['input_path']}`，"
            f"在当前对话报告文本内容与 SHA256，由协调者转录回执（写入能力保持未验证）。"
        )

    return (
        f"# 给新成员 ({context['display_name']}) 的 AgentBus 接入与协作刚性指引\n\n"
        f"你已作为平级协作成员加入 AgentBus 工作台，成员 ID: `{context['member_id']}`。\n"
        f"项目控制工作区: `{context['workspace']}`。\n\n"
        f"## 核心协作规约（必读）\n"
        f"1. **方案与规范优先**：任务树（task-tree.json）是交付目录与契约，任务卡是简写；实现前必须先阅读项目根目录 `PROJECT.md`、`AGENTS.md` 以及规划设计文档（成果目录或 docs/），严格按定型设计实现，绝不凭空臆造；\n"
        f"2. **代码落地边界**：所有业务产物一律在工作区根目录声明的工程路径下创建与实现，**严禁修改外部参考库、临时素材或嵌套工程（如 skill辅助/ 等目录）**；\n"
        f"3. **标准原子认领**：严禁直接手动修改或删除信箱 JSON，必须通过命令行 claim 认领任务；\n"
        f"4. **长任务心跳**：执行长任务或分步调试时，定期执行 heartbeat 维持活跃，防止被判定超时；\n"
        f"5. **产物自测与双签回执**：代码必须在本地自测全部通过后，生成独立 UTF-8 清单 JSON，编写 Markdown 交付回执并通过 answer 提交。\n\n"
        f"## 接入执行步骤\n\n"
        f"1. **认领任务**: {claim_step}\n"
        f"{action_step}\n"
    )


def build_bounded_sample_task(context: Dict[str, Any]) -> Dict[str, Any]:
    """生成有界的新成员摸底任务 JSON"""
    expected = [context["output_path"]] if context.get("writable", True) else []
    return {
        "id": context["task_id"],
        "ts": context.get('created_at') or _now_iso(),
        "sender": context["coordinator"],
        "to": context["member_id"],
        "type": "task",
        "title": f"[接入摸底] 新成员 {context['display_name']} 文件协作样例",
        "body": (
            f"新成员接入摸底：请读取 {context['input_path']}，"
            f"在 {context['output_path']} 写出指定文本，并提交回执与 SHA256 清单。"
        ) if context.get('writable', True) else (
            f"新成员只读摸底：读取 {context['input_path']}，报告首行与 SHA256；"
            "在当前会话自报，由协调者注明转录来源，文件写能力保持未验证。"
        ),
        "result_to": context["coordinator"],
        "refs": [],
        "reply_to": None,
        "job_id": None,
        "plan_id": context.get('plan_id'),
        "plan_sha256": context.get('plan_sha256'),
        "expected_artifacts": expected,
        "artifacts": [],
        "status": "pending",
        "budget_s": 1800,
        "deadline_ts": context.get('deadline_ts', time.time() + 1800),
        "result": "",
        "schema_version": 1
    }


def build_initial_capability_card(adapter: MemberAdapter, project_id: str,
                                 sample_evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """生成符合 capability_card 规范的标准公开能力卡"""
    samples = []
    if sample_evidence:
        allowed = {'task_id', 'result_id', 'capability_family', 'outcome', 'verified',
                   'observed_at', 'version', 'model', 'artifact_sha256'}
        if not isinstance(sample_evidence, dict) or set(sample_evidence) - allowed:
            raise ValueError('sample evidence accepts only public scalar fields')
        if sample_evidence.get('verified') is True and not sample_evidence.get('result_id'):
            raise ValueError('verified sample needs an explicit formal receipt reference')
        sample_entry = {
            "project_id": project_id,
            "task_id": sample_evidence.get("task_id", "sample-task"),
            "result_id": sample_evidence.get("result_id"),
            "capability_family": sample_evidence.get("capability_family", "file_collaboration"),
            "outcome": sample_evidence.get("outcome", "done"),
            "verified": sample_evidence.get("verified", False),
            "observed_at": sample_evidence.get("observed_at", _now_iso()),
            "transport": "file",
            "route": "file",
            "version": sample_evidence.get("version", adapter.host_version),
            "model": sample_evidence.get("model", adapter.model_id)
        }
        if "artifact_sha256" in sample_evidence:
            sample_entry["artifact_sha256"] = sample_evidence["artifact_sha256"]
        samples.append(sample_entry)

    card = {
        "schema_version": 1,
        "member_id": adapter.member_id,
        "display_name": adapter.display_name,
        "host_version": adapter.host_version,
        "model_id": adapter.model_id,
        "route": "file",
        "tools": list(adapter.file_capabilities),
        "specialties": ["file_handover", "manifest_verification"],
        "limitations": ["no_unauthorized_credentials", "no_desktop_control"],
        "samples": samples,
        "source": f"onboarding_pack:{project_id}",
        "updated_at": _now_iso()
    }
    return _validate_public_card(card)


def _validate_public_card(card: Dict[str, Any]) -> Dict[str, Any]:
    """Strict card schema rejects nested secrets and state; copy public history."""
    try:
        if len(json.dumps(card, ensure_ascii=False).encode('utf-8')) > 64 * 1024:
            raise ValueError('public evidence exceeds 64 KiB')
    except (TypeError, RecursionError) as exc:
        raise ValueError('public evidence must be bounded JSON metadata') from exc
    result = validate_card(card)
    validate_member_id(result['member_id'])
    for sample in result['samples']:
        if sample['verified'] and not sample.get('result_id'):
            raise ValueError('verified sample needs a formal receipt reference')
    return copy.deepcopy(result)


def export_reusable_evidence(card: Dict[str, Any]) -> Dict[str, Any]:
    """导出可跨项目复用的纯元数据证据包（绝对不含凭据、本地路径或在途状态）"""
    card = _validate_public_card(card)
    return {
        "schema_version": 1,
        "member_id": card["member_id"],
        "display_name": card["display_name"],
        "tools": list(card.get("tools", [])),
        "specialties": list(card.get("specialties", [])),
        "limitations": list(card.get("limitations", [])),
        "reusable_samples": copy.deepcopy(card.get("samples", [])),
        "exported_at": _now_iso()
    }


def import_card_to_second_project(evidence_pack: Dict[str, Any], target_project_id: str,
                                  new_host_version: Optional[str] = None) -> Dict[str, Any]:
    """在第二个干净项目导入能力卡，确保权限、会话及 processing 绝不继承"""
    fields = {'schema_version', 'member_id', 'display_name', 'tools', 'specialties',
              'limitations', 'reusable_samples', 'exported_at'}
    if (not isinstance(evidence_pack, dict) or set(evidence_pack) != fields
            or type(evidence_pack.get('schema_version')) is not int
            or evidence_pack['schema_version'] != 1):
        raise ValueError('evidence pack must contain exactly the public schema')
    if not isinstance(target_project_id, str) or not target_project_id.strip():
        raise ValueError('target project identity is required')
    # 严格重构为新项目的独立卡片。历史 project_id 保持原来源，不升级为本项目验收。
    imported = {
        "schema_version": 1,
        "member_id": evidence_pack["member_id"],
        "display_name": evidence_pack["display_name"],
        "host_version": new_host_version,
        "model_id": None,
        "route": "file",
        "tools": list(evidence_pack.get("tools", [])),
        "specialties": list(evidence_pack.get("specialties", [])),
        "limitations": list(evidence_pack.get("limitations", [])),
        "samples": copy.deepcopy(evidence_pack.get("reusable_samples", [])),
        "source": f"imported_from_evidence_pack:{target_project_id}",
        "updated_at": _now_iso()
    }
    # 显式禁止凭据、在途任务或授权继承
    assert "credentials" not in imported
    assert "processing" not in imported
    assert "token" not in imported
    return _validate_public_card(imported)


def generate_onboarding_pack(workspace: Path | str, member_id: str,
                             coordinator: Optional[str] = None,
                             task_id: Optional[str] = None) -> Dict[str, Any]:
    """为指定成员生成包含上下文、组长说明、新成员指令及摸底任务的完整接入包"""
    from .profiles import adapters_for_project
    from .config import load
    from .silent_debug import note_silent
    public = PublicFiles(workspace)
    cfg = load(public.root)
    registry = adapters_for_project(cfg)
    adapter = registry.get(member_id) or GLOBAL_REGISTRY.get(member_id)
    if adapter is None:
        worker = cfg.data.get('workers', {}).get(member_id)
        if worker:
            from .member_adapter import DEFAULT_FILE_CAPABILITIES, READONLY_FILE_CAPABILITIES
            writable = worker.get('writable', True)
            caps = worker.get('file_capabilities', DEFAULT_FILE_CAPABILITIES if writable else READONLY_FILE_CAPABILITIES)
            adapter = MemberAdapter(
                member_id=member_id,
                display_name=worker.get('display_name') or member_id,
                file_capabilities=caps,
                wake_adapter=worker.get('wake_adapter', 'manual'),
                host_version=worker.get('host_version'),
                model_id=worker.get('model_id')
            )
        else:
            raise ValueError(f'UNKNOWN_MEMBER: 成员 {member_id!r} 尚未加入当前项目')

    context = build_onboarding_context(public.root, adapter, coordinator=coordinator, task_id=task_id)
    leader_inst = build_leader_instructions(context)
    newcomer_inst = build_newcomer_instructions(context)
    task = build_bounded_sample_task(context)

    return {
        "context": context,
        "leader_instructions": leader_inst,
        "newcomer_instructions": newcomer_inst,
        "sample_task": task,
        "member_id": member_id,
        "display_name": adapter.display_name,
        "workspace": context["workspace"],
        "task_id": context["task_id"],
        "input_path": context["input_path"],
        "output_path": context["output_path"],
        "manifest_path": context["manifest_path"],
        "inbox_path": context["inbox_path"],
        "writable": context["writable"],
    }


def deploy_onboarding_pack(workspace: Path | str, member_id: str,
                           coordinator: Optional[str] = None,
                           task_id: Optional[str] = None) -> Dict[str, Any]:
    """生成并将摸底任务与输入文件安全部署至工作区（投放至就绪信箱和成果目录）"""
    pack = generate_onboarding_pack(workspace, member_id, coordinator=coordinator, task_id=task_id)
    public = PublicFiles(workspace)
    root = public.root
    from .bus import Bus, Message

    # 1. 确保输入文件落盘
    input_rel = pack["input_path"]
    input_file = root / input_rel
    input_file.parent.mkdir(parents=True, exist_ok=True)
    if not input_file.exists():
        marker_text = f"AgentBus Onboarding Sample for {member_id}: {pack['task_id']}\n"
        input_file.write_text(marker_text, encoding="utf-8")

    # 2. 投放任务到 ready 信箱
    bus = Bus(root / ".agentbus")
    bus.init()
    msg = Message.from_dict(pack["sample_task"])
    bus.post(msg)

    pack["deployed"] = True
    return pack


def build_one_click_join_prompt(workspace: Path | str, member_id: str,
                                display_name: Optional[str] = None,
                                preset: Optional[str] = None, *, _prepared_launcher: Optional[dict] = None) -> str:
    """生成给新 Agent 粘贴即可自驱动接入的开箱即用 Prompt"""
    validate_member_id(member_id)
    public = PublicFiles(workspace)       # Inspect lexical ancestors before resolving junctions.
    ws = public.root.resolve()
    config = public.read_json('.agentbus/config.json') or {}
    worker = config.get('workers', {}).get(member_id, {})
    disp = display_name or worker.get('display_name') or member_id
    prefix, launcher = _registered_launcher(public, _prepared_launcher)
    from .task_assignment import _quote
    arguments = f" --workspace {_quote(ws)} --agent {_quote(member_id)} --id '<任务ID>'"
    ticket = " --claim-epoch '<claim_epoch>' --claim-id '<claim_id>'"
    integrity_note = launcher.get('sha256') or ('未登记摘要；完整性未验证' if launcher['mode'] == 'frozen'
                                                else '源码登记；EXE摘要不适用')
    preset_tip = ("Claude Code 提示：按已登记入口执行命令。" if preset == 'claude' else
                  f"{preset.capitalize()} 提示：在正确项目窗口读取任务。" if preset in ('cursor', 'windsurf') else '')
    return (
        f"# AgentBus 新成员接手指令\n\n成员 ID：`{member_id}`；显示名称：`{disp}`。\n"
        f"项目：`{ws}`；信箱：`.agentbus/agents/{member_id}/`。\n"
        f"登记入口：`{launcher.get('executable') or launcher.get('entrypoint')}`；"
        f"SHA256：`{integrity_note}`。\n{preset_tip}\n\n"
        "先读 AGENTS.md（公共契约）、AGENTBUS.md（程序入口）、MEMORY.md 与当前计划指针。"
        "核对本项目成员和任务；STOP 存在时暂停新开工。文件生成不代表宿主已连接或执行能力已验收。\n\n"
        "【第一步（自主向组长报到与绑定当前对话）】无论你在哪个对话窗口收到本指令，请先执行一次报到命令，"
        "将当前对话窗口绑定为你在本项目的唤醒目标，并向组长报告就绪状态：\n"
        f"`{prefix} contact checkin --workspace {_quote(ws)} --agent {_quote(member_id)}`\n\n"
        "认领（agentbus claim）：以真实任务 ID 替换占位值，已在 processing 的任务由原会话续接。\n"
        f"`{prefix} claim{arguments}`\n"
        "保存领取返回的 claim_epoch 与 claim_id，不从后续任务文件反补新的票据。"
        "立即发送票据绑定心跳，长任务在当前项目配置的 heartbeat_timeout_s 内续发；停止执行后不得继续假心跳。\n"
        f"`{prefix} heartbeat{arguments}{ticket}`\n"
        "完成后按任务 result_to 回投；Windows 正文使用项目内 UTF-8 文件和原始字节摘要，"
        "产物清单列项目相对 path/bytes/sha256，不列清单自身。回执（agentbus answer）：\n"
        f"`{prefix} answer{arguments}{ticket} --body-file '<回执路径>'"
        " --expected-body-sha256 '<正文SHA256>' --artifacts-file '<产物清单路径>'`\n"
        "禁止编辑/移动信箱 JSON；任务规划先读成果/任务书维护/TASK_TREE_AUTHORING_GUIDE.md。"
        "只有实际读取契约和信箱后才报告该观察；没有正规执行入口时如实自报待接手，不声称已连接。\n"
    )


def _registered_launcher(public: PublicFiles, prepared: Optional[dict] = None) -> tuple[str, dict]:
    from .project_workflow import _launcher_status
    from .task_assignment import _quote
    workflow = (public.read_json('.agentbus/workflow.json') or {}) if prepared is None else {}
    launcher = prepared if prepared is not None else workflow.get('launcher') or {}
    valid, reason = _launcher_status(launcher)
    if not valid:
        raise ValueError('项目 launcher 未通过核验：' + reason)
    if launcher['mode'] == 'frozen':
        return '& ' + _quote(launcher['executable']), launcher
    return '& ' + _quote(launcher['python']) + ' -X utf8 -B ' + _quote(launcher['entrypoint']), launcher


def _public_config(public: PublicFiles):
    """Load without config.load's optional .bak recovery writes during preflight."""
    from .config import Config, DEFAULTS, _deep_merge, validate
    config = public.read_json('.agentbus/config.json')
    if config is None:
        raise ValueError('请先初始化并保存项目')
    loaded = _deep_merge(DEFAULTS, config)
    loaded.setdefault('workspace', str(public.root.resolve()))
    loaded.setdefault('output_root', '.')
    validate(loaded)
    return Config(public.checked('.agentbus/config.json'), public.root.resolve(), loaded), config


def auto_onboard_agent(workspace: Path | str, member_id: str,
                       display_name: Optional[str] = None,
                       preset: Optional[str] = None) -> Dict[str, Any]:
    """Prepare one project member without changing existing permissions or adopting conflicts."""
    from .config import Config, validate
    from .lease import LeaseManager
    from .operation_lock import task_lock
    from .profiles import project_skill_paths
    from .project_workflow import peer_skill, workflow_status
    validate_member_id(member_id)
    if display_name is not None and (not isinstance(display_name, str) or not display_name.strip()
            or len(display_name) > 80 or any(ord(char) < 32 for char in display_name)):
        raise ValueError('成员显示名需为1–80个可显示字符')
    if preset not in (None, 'generic', 'claude', 'cursor', 'windsurf'):
        raise ValueError('不支持的成员预设')
    public = PublicFiles(workspace)     # Must precede resolve: a workspace junction is not an authorization.
    ws = public.root.resolve()
    if not (ws / '.agentbus' / 'config.json').exists():
        from .quickstart import run_quickstart
        run_quickstart(ws, print_output=False)
    for rel in ('.agentbus/config.json', '.agentbus/workflow.json', 'AGENTBUS.md',
                '.agentbus/leases/.mutation.lock', '.agentbus/onboarding-transaction.json',
                '.agentbus/operation-locks'):
        public.checked(rel)
    with task_lock(ws / '.agentbus', 'onboarding', 'project'):
        _recover_onboard(public)
        observed = {rel: public.read_bytes(rel) for rel in
                    ('.agentbus/config.json', '.agentbus/workflow.json', 'AGENTBUS.md')}
        cfg, config = _public_config(public)
        existing = config.get('workers', {}).get(member_id)
        if existing is not None:
            if not existing.get('enabled', True) or not existing.get('executor_enabled', True):
                raise ValueError('已有成员未启用或未选为执行成员；接入操作不会自动改变权限')
            disp = existing.get('display_name') or member_id
            selected_preset = existing.get('preset') or 'generic'
            if ((display_name is not None and display_name.strip() != disp)
                    or (preset is not None and preset != selected_preset)):
                raise ValueError('传入显示名或预设与已有配置冲突，原配置已保留')
        else:
            disp = display_name.strip() if display_name is not None else member_id
            selected_preset = preset or 'generic'
            config.setdefault('workers', {})[member_id] = {
                'display_name': disp, 'enabled': True, 'executor_enabled': True,
                'kind': 'gui', 'can_spawn': False, 'human_in_loop': True,
                'transport': 'file', 'communication_mode': 'file',
                'file_capabilities': list(DEFAULT_FILE_CAPABILITIES),
                'onboarded_at': _now_iso(), 'preset': selected_preset}
        planned_data = copy.deepcopy(cfg.data)
        planned_data['workers'][member_id] = copy.deepcopy(config['workers'][member_id])
        validate(planned_data)
        paths = project_skill_paths(Config(cfg.path, ws, planned_data))
        skill_rel = paths[member_id]
        directories = [f'.agentbus/agents/{member_id}/{state}' for state in ('ready', 'processing', 'done', 'blocked')]
        for rel in [skill_rel, *directories]:
            public.checked(rel)
        initial_outputs = {}
        if observed['.agentbus/workflow.json'] is None:
            initial_outputs, workflow = _initial_onboard_workflow(public, cfg)
            for rel in initial_outputs:
                observed[rel] = public.read_bytes(rel)
        else:
            status = workflow_status(cfg)
            if not status['ready']:
                raise ValueError('工作流存在摘要冲突或入口未就绪，未写入：' + status['reason'])
            workflow = public.read_json('.agentbus/workflow.json')
            # Every registered asset was used to decide readiness. Keep those reads in the CAS,
            # including other members' skills, rather than only the selected member's file.
            for rel, digest in workflow['files'].items():
                raw = public.read_bytes(rel)
                if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                    raise ValueError('工作流登记资产在预检期间变化，未写入：' + rel)
                observed.setdefault(rel, raw)
        outputs = dict(initial_outputs)
        if existing is None:
            outputs['.agentbus/config.json'] = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        existing_skill = public.read_bytes(skill_rel)
        observed[skill_rel] = existing_skill
        registered = {**workflow.get('retired_files', {}), **workflow['files']}
        if existing_skill is not None:
            if registered.get(skill_rel) != hashlib.sha256(existing_skill).hexdigest():
                raise ValueError('已有技能文件不是登记原文，已保留：' + skill_rel)
            skill = existing_skill
        else:
            if skill_rel in workflow['files'] and skill_rel not in initial_outputs:
                raise ValueError('已登记技能文件缺失，已保留工作流登记')
            skill = initial_outputs.get(skill_rel) or peer_skill(member_id).encode('utf-8')
            outputs[skill_rel] = skill
        updated_workflow = copy.deepcopy(workflow)
        updated_workflow['files'][skill_rel] = hashlib.sha256(skill).hexdigest()
        updated_workflow.setdefault('retired_files', {}).pop(skill_rel, None)
        updated_workflow['agents'] = list(paths)
        entry = initial_outputs.get('AGENTBUS.md') or public.read_bytes('AGENTBUS.md')
        if entry is None:
            raise ValueError('项目入口 AGENTBUS.md 缺失')
        if skill_rel not in workflow['files']:
            entry += (f'\n## 新成员 `{member_id}`\n\n项目工作流：`{skill_rel}`。'
                      '仅生成接入文件；宿主发现、任务执行与回执须分别验收。\n').encode('utf-8')
            outputs['AGENTBUS.md'] = entry
        updated_workflow['files']['AGENTBUS.md'] = hashlib.sha256(entry).hexdigest()
        workflow_bytes = (json.dumps(updated_workflow, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        if workflow_bytes != public.read_bytes('.agentbus/workflow.json'):
            outputs['.agentbus/workflow.json'] = workflow_bytes
        # Prompt generation is a read-only precondition, before publishing a single file.
        if initial_outputs:
            prompt = build_one_click_join_prompt(ws, member_id, display_name=disp, preset=selected_preset,
                                                _prepared_launcher=updated_workflow['launcher'])
        else:
            prompt = build_one_click_join_prompt(ws, member_id, display_name=disp, preset=selected_preset)
        manager = LeaseManager(ws / '.agentbus')
        leases = []
        try:
            for rel in dict.fromkeys([*outputs, *observed, '.agentbus/config.json', '.agentbus/workflow.json',
                                     'AGENTBUS.md', skill_rel, f'.agentbus/agents/{member_id}',
                                     '.agentbus/onboarding-transaction.json']):
                lease = manager.acquire(rel, 'agentbus-onboarding', ttl_s=120,
                                        note='atomic onboarding ' + member_id)
                if lease is None:
                    raise ValueError('接入文件租约冲突，未写入：' + rel)
                leases.append(lease)
            _commit_onboard(public, outputs, directories, member_id, observed)
        finally:
            for lease in reversed(leases):
                manager.release(lease)
        # 触发联系准备与 checkin 命令生成
        from .task_assignment import _quote
        prefix, _ = _registered_launcher(public, updated_workflow.get('launcher'))
        if prefix:
            checkin_cmd = f"{prefix} contact checkin --workspace {_quote(ws)} --agent {_quote(member_id)}"
        else:
            checkin_cmd = f"agentbus contact checkin --workspace {_quote(ws)} --agent {_quote(member_id)}"

        return {'success': True, 'prepared': True, 'execution_verified': False,
            'member_id': member_id, 'display_name': disp, 'workspace': str(ws),
            'inbox': str(ws / '.agentbus/agents' / member_id), 'skill_path': skill_rel,
            'prompt': prompt, 'checkin_command': checkin_cmd,
            'message': f'成员 {disp} ({member_id}) 的接入文件已就位；宿主连接与执行未验证。'}


def _initial_onboard_workflow(public: PublicFiles, cfg) -> tuple[dict[str, bytes], dict]:
    """An unprepared initialized project gets its first workflow inside the same transaction."""
    from .profiles import project_skill_paths
    relatives = ['AGENTBUS.md', *project_skill_paths(cfg).values()]
    for relative in relatives:
        if public.read_bytes(relative) is not None:
            raise ValueError('未登记的工作流文件已存在，拒绝自动认领：' + relative)
    return _render_initial_onboard_workflow(cfg, public.root.resolve())


def _render_initial_onboard_workflow(cfg, target_root: Path) -> tuple[dict[str, bytes], dict]:
    """Render the first workflow without inspecting or changing target assets."""
    from .config import Config
    from .project_workflow import prepare_project_workflow
    with tempfile.TemporaryDirectory(prefix='agentbus-onboard-workflow-') as folder:
        stage = Path(folder).resolve()
        control = stage / '.agentbus'
        control.mkdir()
        staged_cfg = Config(control / 'config.json', stage, copy.deepcopy(cfg.data))
        staged_cfg.path.write_text(json.dumps(staged_cfg.data), encoding='utf-8')
        prepare_project_workflow(staged_cfg)
        manifest = json.loads((control / 'workflow.json').read_text(encoding='utf-8'))
        outputs = {relative: (stage / relative).read_bytes() for relative in manifest['files']}
        outputs['AGENTBUS.md'] = outputs['AGENTBUS.md'].replace(str(stage).encode('utf-8'), str(target_root).encode('utf-8'))
        manifest['workspace'] = str(target_root)
        manifest['files']['AGENTBUS.md'] = hashlib.sha256(outputs['AGENTBUS.md']).hexdigest()
        return outputs, manifest


def _onboard_write(public: PublicFiles, relative: str, data: bytes | None) -> None:
    path = public.checked(relative)
    if data is None:
        path.unlink(missing_ok=True)
        return
    fd, name = tempfile.mkstemp(prefix='.onboarding-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        public.checked(temporary.relative_to(public.root))
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        public.checked(relative)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rollback_onboard(public: PublicFiles, record: dict) -> None:
    changes = record['changes']
    decoded = {rel: (base64.b64decode(item['before'], validate=True) if item['before'] is not None else None,
                     base64.b64decode(item['after'], validate=True)) for rel, item in changes.items()}
    for rel, (before, after) in decoded.items():
        if public.read_bytes(rel) not in (before, after):
            raise ValueError('接入事务 CAS 冲突，当前修改与事务均保留：' + rel)
    for rel, (before, after) in reversed(list(decoded.items())):
        if public.read_bytes(rel) == after and before != after:
            _onboard_write(public, rel, before)
    for rel in reversed(record['created_directories']):
        path = public.checked(rel)
        if path.exists():
            path.rmdir()               # Empty only: never remove concurrent files or live mailboxes.
    public.checked('.agentbus/onboarding-transaction.json').unlink(missing_ok=True)


def _validate_onboard_journal(public: PublicFiles, record: dict) -> dict[str, bytes | None]:
    """Reconstruct this operation's permitted writes; a path allowlist cannot trust before bytes."""
    from .config import Config, DEFAULTS, _deep_merge, validate
    from .profiles import project_skill_paths
    from .project_workflow import peer_skill, _launcher_status
    if (set(record) != {'schema_version', 'member_id', 'created_directories', 'changes'}
            or type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or not isinstance(record['changes'], dict)
            or not isinstance(record['created_directories'], list)
            or len(record['changes']) > 128 or len(record['created_directories']) > 128):
        raise ValueError('接入事务 schema 无效，现场保留')
    member = validate_member_id(record['member_id'])
    decoded = {}
    for rel, change in record['changes'].items():
        public.checked(rel)
        if not isinstance(change, dict) or set(change) != {'before', 'after'}:
            raise ValueError('接入事务变更 schema 无效，现场保留')
        try:
            before = base64.b64decode(change['before'], validate=True) if change['before'] is not None else None
            after = base64.b64decode(change['after'], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError('接入事务字节格式无效，现场保留') from exc
        decoded[rel] = (before, after)
    dependencies = {}
    def image(rel, side):
        current = public.read_bytes(rel)
        dependencies[rel] = current
        if rel in decoded:
            if current not in decoded[rel]:
                raise ValueError('接入事务 CAS 冲突，现场保留：' + rel)
            return decoded[rel][side]
        return current
    def config_from(raw):
        if raw is None:
            raise ValueError('接入事务不能创建或删除项目配置')
        value = json.loads(raw.decode('utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('workers'), dict):
            raise ValueError('接入事务配置无效，现场保留')
        loaded = _deep_merge(DEFAULTS, value)
        validate(loaded)
        return value, Config(public.checked('.agentbus/config.json'), public.root.resolve(), loaded)
    old_config, old_cfg = config_from(image('.agentbus/config.json', 0))
    new_config, new_cfg = config_from(image('.agentbus/config.json', 1))
    expected_config = copy.deepcopy(old_config)
    if member not in old_config['workers']:
        worker = new_config['workers'].get(member)
        fields = {'display_name', 'enabled', 'executor_enabled', 'kind', 'can_spawn', 'human_in_loop',
                  'transport', 'communication_mode', 'file_capabilities', 'onboarded_at', 'preset'}
        if (not isinstance(worker, dict) or set(worker) != fields
                or not isinstance(worker.get('display_name'), str) or not worker['display_name'].strip()
                or len(worker['display_name']) > 80 or any(ord(ch) < 32 for ch in worker['display_name'])
                or worker.get('preset') not in ('generic', 'claude', 'cursor', 'windsurf')
                or not isinstance(worker.get('onboarded_at'), str)):
            raise ValueError('接入事务新成员不是允许的安全 profile，现场保留')
        try:
            if datetime.fromisoformat(worker['onboarded_at']).tzinfo is None:
                raise ValueError('missing timezone')
        except ValueError as exc:
            raise ValueError('接入事务时间无效，现场保留') from exc
        permitted = {'display_name': worker['display_name'], 'enabled': True, 'executor_enabled': True,
            'kind': 'gui', 'can_spawn': False, 'human_in_loop': True, 'transport': 'file',
            'communication_mode': 'file', 'file_capabilities': list(DEFAULT_FILE_CAPABILITIES),
            'onboarded_at': worker['onboarded_at'], 'preset': worker['preset']}
        if worker != permitted:
            raise ValueError('接入事务新成员权限不匹配，现场保留')
        expected_config['workers'][member] = permitted
    elif (not old_config['workers'][member].get('enabled', True)
          or not old_config['workers'][member].get('executor_enabled', True)):
        raise ValueError('接入事务不能启用既有成员，现场保留')
    if new_config != expected_config:
        raise ValueError('接入事务改变了既有成员或其他配置，现场保留')
    old_paths = project_skill_paths(old_cfg)
    new_paths = project_skill_paths(new_cfg)
    skill_rel = new_paths[member]
    old_workflow_raw = image('.agentbus/workflow.json', 0)
    expected = {}
    if old_workflow_raw is None:
        expected, workflow = _render_initial_onboard_workflow(old_cfg, public.root.resolve())
        for rel in expected:
            if image(rel, 0) is not None:
                raise ValueError('首次接入事务不能认领已有资产，现场保留：' + rel)
    else:
        workflow = json.loads(old_workflow_raw.decode('utf-8'))
        if (not isinstance(workflow, dict) or workflow.get('owner') != 'agentbus'
                or type(workflow.get('schema_version')) is not int or workflow['schema_version'] != 1
                or workflow.get('workspace') != str(public.root.resolve())
                or not isinstance(workflow.get('files'), dict)
                or not isinstance(workflow.get('retired_files', {}), dict)
                or set(workflow['files']) != {'AGENTBUS.md', *old_paths.values()}):
            raise ValueError('接入事务原工作流登记无效，现场保留')
        valid, _ = _launcher_status(workflow.get('launcher'))
        if not valid:
            raise ValueError('接入事务原 launcher 无效，现场保留')
        for rel, digest in workflow['files'].items():
            raw = image(rel, 0)
            if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('接入事务原资产与登记不一致，现场保留：' + rel)
    if member not in old_config['workers']:
        expected['.agentbus/config.json'] = (json.dumps(expected_config, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    prior_skill = image(skill_rel, 0)
    if prior_skill is not None:
        registered = {**workflow.get('retired_files', {}), **workflow['files']}
        if registered.get(skill_rel) != hashlib.sha256(prior_skill).hexdigest():
            raise ValueError('接入事务技能原文未登记，现场保留')
        skill = prior_skill
    else:
        if skill_rel in workflow['files'] and skill_rel not in expected:
            raise ValueError('接入事务已登记技能缺失，现场保留')
        skill = expected.get(skill_rel) or peer_skill(member).encode('utf-8')
        expected[skill_rel] = skill
    updated = copy.deepcopy(workflow)
    updated['files'][skill_rel] = hashlib.sha256(skill).hexdigest()
    updated.setdefault('retired_files', {}).pop(skill_rel, None)
    updated['agents'] = list(new_paths)
    entry = expected.get('AGENTBUS.md') or image('AGENTBUS.md', 0)
    if skill_rel not in workflow['files']:
        entry += (f'\n## 新成员 `{member}`\n\n项目工作流：`{skill_rel}`。'
                  '仅生成接入文件；宿主发现、任务执行与回执须分别验收。\n').encode('utf-8')
        expected['AGENTBUS.md'] = entry
    updated['files']['AGENTBUS.md'] = hashlib.sha256(entry).hexdigest()
    rendered = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if rendered != old_workflow_raw:
        expected['.agentbus/workflow.json'] = rendered
    material_changes = {rel: after for rel, (before, after) in decoded.items() if before != after}
    if material_changes != expected:
        raise ValueError('接入事务 before/after 不符合本次登记语义，现场保留')
    possible = [public.checked(f'.agentbus/agents/{member}/{state}')
                for state in ('ready', 'processing', 'done', 'blocked')]
    possible += [public.checked(rel).parent for rel in expected]
    for rel in record['created_directories']:
        target = public.checked(rel)
        if target == public.root or not any(target == base or target in base.parents for base in possible):
            raise ValueError('接入事务创建目录不符合登记语义，现场保留')
    return dependencies


def _recover_onboard(public: PublicFiles) -> None:
    raw = public.read_bytes('.agentbus/onboarding-transaction.json', limit=1024 * 1024)
    if raw is None:
        return
    record = json.loads(raw.decode('utf-8'))
    if not isinstance(record, dict):
        raise ValueError('接入事务记录必须是对象，原记录保留')
    dependencies = _validate_onboard_journal(public, record)
    member = record['member_id']
    from .lease import LeaseManager
    manager = LeaseManager(public.root / '.agentbus')
    leases = []
    try:
        for rel in [*dependencies, f'.agentbus/agents/{member}', '.agentbus/onboarding-transaction.json']:
            lease = manager.acquire(rel, 'agentbus-onboarding', ttl_s=120, note='recover interrupted onboarding')
            if lease is None:
                raise ValueError('中断接入事务仍有有效租约，保留事务等待正式释放或到期：' + rel)
            leases.append(lease)
        if public.read_bytes('.agentbus/onboarding-transaction.json', limit=1024 * 1024) != raw:
            raise ValueError('恢复事务在租约期间改变，现场保留')
        for rel, previous in dependencies.items():
            if public.read_bytes(rel) != previous:
                raise ValueError('恢复事务依赖资产在租约期间改变，现场保留：' + rel)
        _rollback_onboard(public, record)
    finally:
        for lease in reversed(leases):
            manager.release(lease)


def _commit_onboard(public: PublicFiles, outputs: dict[str, bytes], directories: list[str], member: str,
                    observed: dict[str, bytes | None]) -> None:
    for rel, raw in observed.items():
        if public.read_bytes(rel) != raw:
            raise ValueError('接入预览到租约取得之间 CAS 冲突，未写入：' + rel)
    originals = {rel: public.read_bytes(rel) for rel in outputs}
    created = []
    record = {'schema_version': 1, 'member_id': member, 'created_directories': created,
              'changes': {rel: {'before': base64.b64encode(originals[rel]).decode('ascii') if originals[rel] is not None else None,
                               'after': base64.b64encode(data).decode('ascii')} for rel, data in outputs.items()}}
    journal = '.agentbus/onboarding-transaction.json'
    try:
        # Record intent before changing directories or files, including each new empty directory.
        _onboard_write(public, journal, json.dumps(record).encode('utf-8'))
        for rel in [*(str(Path(name).parent).replace('\\', '/') for name in outputs), *directories]:
            target = public.checked(rel)
            absent = []
            cursor = target
            while not cursor.exists():
                absent.append(cursor)
                cursor = cursor.parent
            for path in reversed(absent):
                relative = path.relative_to(public.root).as_posix()
                created.append(relative)
                _onboard_write(public, journal, json.dumps(record).encode('utf-8'))
                public.checked(relative).mkdir()
        for rel, data in outputs.items():
            if public.read_bytes(rel) != originals[rel]:
                raise ValueError('接入提交前 CAS 冲突：' + rel)
            if originals[rel] != data:
                _onboard_write(public, rel, data)
        public.checked(journal).unlink()
    except BaseException:
        _rollback_onboard(public, record)
        raise

