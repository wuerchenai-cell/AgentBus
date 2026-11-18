"""
Continuation Pack Generator (续接包生成器与防目标漂移引擎)
Source: Antigravity (Product Convergence, task AG-MEMORY)
Date: 2026-10-08

核心规范：
1. 生成最多约 1200 字（含硬上限截断）的有界续接包，不复制整段聊天。
2. 结构清晰规范：
   - 总体目标 (项目范围与长期边界；L0 仅指日期日志)
   - 当前计划指针 (.agentbus/project-plan/current.json 不可变 revision + sha256)
   - 本人任务 (当前处理中或就绪的原子任务 ID、标题与截止)
   - 已验证事实 (稳定事实，必须显式引用 LOG 日期或回执 ID，不凭聊天猜测)
   - 当前阻塞与边界 (真实记录的阻塞或无阻塞)
   - 唯一下一步 (单一明确的行动指令，防止发散)
   - 来源与证据 (生成时间、快照哈希与白名单证据文件)
3. 安全红线：禁止私有目录引用（.workbuddy 等）、禁止符号链接、不执行文件代码。
"""
from __future__ import annotations

import os
import re
import stat
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from .member_adapter import validate_member_id, MemberAdapterError
from .public_files import PublicFiles, PublicFileError, PRIVATE_PARTS


MAX_CHAR_BUDGET = 1200
MAX_READ_BYTES = 64 * 1024

FORBIDDEN_PARTS = PRIVATE_PARTS


class ContinuationPackError(ValueError):
    """续接包生成异常"""
    pass


def _safe_read_text(path: Path, workspace: Path | None = None) -> Optional[str]:
    """安全且有界地读取 UTF-8 文本"""
    try:
        root = workspace or path.parent
        return PublicFiles(root).read_text(path.absolute().relative_to(root.absolute()))
    except (OSError, ValueError) as exc:
        raise ContinuationPackError(f'公开文件未验证: {path.name}: {exc}') from exc


def extract_pack_data(workspace: Path | str, member_id: Optional[str] = None) -> Dict[str, Any]:
    """从白名单文件中提取结构化续接数据"""
    try:
        public = PublicFiles(workspace)
        ws = public.root
        if member_id is None:
            config = public.read_json('.agentbus/config.json') or {}
            member_id = config.get('director') or config.get('coordinator')
        validate_member_id(member_id)
    except (OSError, PublicFileError, MemberAdapterError) as exc:
        raise ContinuationPackError(str(exc)) from exc

    data: Dict[str, Any] = {
        "workspace": str(ws).replace("\\", "/"),
        "member_id": member_id,
        "l0_goal": "",
        "current_plan_id": "未设置",
        "current_plan_sha": "未设置",
        "member_active_task": "无进行中任务",
        "verified_facts": [],
        "blockers": [],
        "single_next_step": "等待新任务派发",
        "source_date": "未知日期",
        "evidence_sources": []
    }

    # 1. 计划指针
    cur_path = ws / ".agentbus" / "project-plan" / "current.json"
    try:
        cur_json, revision = public.plan_head()
    except (OSError, PublicFileError) as exc:
        raise ContinuationPackError(str(exc)) from exc
    if cur_json:
        data["evidence_sources"].extend(['.agentbus/project-plan/current.json',
                                       f".agentbus/project-plan/revisions/{revision['revision_id']}.json"])
        data["current_plan_id"] = cur_json['revision_id']
        data["current_plan_sha"] = cur_json['revision_sha256']

    # 2. 项目目标（l0_goal 仅保留为旧字典调用兼容键；L0 是日志）
    proj_text = _safe_read_text(ws / "PROJECT.md", ws)
    if proj_text:
        data["evidence_sources"].append("PROJECT.md")
        m = re.search(r"## (?:项目目标|产品目标|总体目标)\s*\n+([^\n#]+)", proj_text)
        if m:
            data["l0_goal"] = m.group(1).strip()

    taskbook_text = _safe_read_text(ws / "TASKBOOK.md", ws)
    if taskbook_text:
        data["evidence_sources"].append("TASKBOOK.md")
        if not data["l0_goal"]:
            m = re.search(r"## 总体目标\s*\n+([^\n#]+)", taskbook_text)
            if m:
                data["l0_goal"] = m.group(1).strip()
        m_date = re.search(r"更新时间[：:]\s*([^\n\r;；]+)", taskbook_text)
        if m_date:
            data["source_date"] = m_date.group(1).strip()

    # 3. 本人当前任务（信箱真源）
    try:
        for status, label in [('processing', '进行中'), ('ready', '待认领')]:
            tasks = public.list_files(f'.agentbus/agents/{member_id}/{status}')
            for path in tasks:
                relative = path.relative_to(ws)
                task = public.read_json(relative)
                if task.get('type', 'task') not in ('task', 'goal', 'review'):
                    continue
                if task.get('to', member_id) != member_id:
                    raise PublicFileError('mailbox task belongs to another member')
                data['member_active_task'] = (f"[{task.get('id', path.stem)}] {task.get('title', '无标题')} "
                                              f"({label}；截止 {task.get('deadline_ts', '未设置')})")
                data['evidence_sources'].append(relative.as_posix())
                break
            else:
                continue
            break
    except (OSError, PublicFileError) as exc:
        raise ContinuationPackError(str(exc)) from exc

    # 4. 已验证事实 (MEMORY.md 提取，必须附带日期引用)
    mem_text = _safe_read_text(ws / "MEMORY.md", ws)
    if mem_text:
        data["evidence_sources"].append("MEMORY.md")
        for line in mem_text.splitlines():
            line = line.strip()
            if (line.startswith('-') and re.search(r'\d{4}-\d{2}-\d{2}', line)
                    and ('LOG.md' in line or re.search(r'm-\d+-[a-f0-9]+', line))):
                data["verified_facts"].append(line.lstrip("- ").strip())
                if len(data["verified_facts"]) >= 5:
                    break

    # 5. 阻塞与唯一步伐 (STATE.md)
    state_text = _safe_read_text(ws / "STATE.md", ws)
    if state_text:
        data["evidence_sources"].append("STATE.md")
        # 阻塞
        m_blk = re.search(r"## (?:阻塞与边界|阻塞|边界)\s*\n+([\s\S]*?)(?=\n##|$)", state_text)
        if m_blk:
            for l in m_blk.group(1).splitlines():
                l = l.strip()
                if l.startswith("-"):
                    data["blockers"].append(l.lstrip("- ").strip())
        # 下一步
        m_nxt = re.search(r"## (?:唯一)?下一步[^\n]*\s*\n+([\s\S]*?)(?=\n##|$)", state_text)
        if m_nxt:
            steps = [l.lstrip("- ").strip() for l in m_nxt.group(1).splitlines() if l.strip() and not l.startswith("#")]
            if steps:
                data["single_next_step"] = steps[0]
        else:
            inline_next = re.search(r'^(?:唯一)?下一步[：:]\s*(.+)$', state_text, re.M)
            if inline_next:
                data['single_next_step'] = inline_next.group(1).strip()

    return data


def build_continuation_pack(workspace: Path | str, member_id: Optional[str] = None,
                            max_chars: int = MAX_CHAR_BUDGET) -> str:
    """生成严格受限在 max_chars 以内的标准续接包"""
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_CHAR_BUDGET:
        raise ContinuationPackError('续接字数预算需为1–1200整数')
    data = extract_pack_data(workspace, member_id=member_id)
    def short(text, count):
        return text if len(text) <= count else text[:count - 1] + '…'

    prioritized = ['.agentbus/project-plan/current.json', 'PROJECT.md', 'STATE.md', 'MEMORY.md']
    sources = [s for s in prioritized if s in data['evidence_sources']]
    sources.extend(s for s in data['evidence_sources'] if s not in sources)

    sections = [
        f"# AgentBus 结构化续接包 (Continuation Pack)",
        f"- 成员: `{data['member_id']}` | 来源时间: {short(data['source_date'], 40)}",
        f"- 计划指针: `{data['current_plan_id']}` (SHA: `{data['current_plan_sha']}`)",
        "",
        "## 1. 总体目标（项目范围）",
        f"> {short(data['l0_goal'] or '未验证：公开文件尚未给出目标', 160)}",
        "*防漂移提示：工作项关联总体目标；L0 是日期日志，L1 为事实，L2 为状态摘要。*",
        "",
        "## 2. 本人当前任务",
        f"- {short(data['member_active_task'], 160)}",
        "",
        "## 3. 唯一下一步",
        f"> {short(data['single_next_step'], 160)}",
        "",
        "## 4. 证据溯源",
        f"- 来源白名单: {short(', '.join(sources), 260)}",
        f"- 机器真源: 本人信箱 JSON；Markdown 为投影",
        "",
        "## 5. 当前阻塞与边界",
        '- ' + (short('；'.join(data['blockers'][:3]), 120) if data['blockers'] else
                '未验证：公开 STATE 尚未明确记录阻塞状态'),
        "",
        "## 6. 有来源事实摘要（未独立重验；引用日志日期）",
    ]

    if data["verified_facts"]:
        for f in data["verified_facts"][:2]:
            sections.append(f"- {short(f, 100)}")
    else:
        sections.append("- 尚无稳定记忆事实记录，以信箱已归档回执为准")

    pack_text = "\n".join(sections).strip()

    # 硬字符预算截断控制
    if len(pack_text) > max_chars:
        suffix = '\n[预算截断]'
        pack_text = (pack_text[:max_chars - len(suffix)].rstrip() + suffix
                     if max_chars > len(suffix) else pack_text[:max_chars])

    return pack_text
