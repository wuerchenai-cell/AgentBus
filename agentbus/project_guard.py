"""
Project Structure and Continuity Guard (项目结构守卫与防目标漂移引擎)
Source: Antigravity (Product Convergence, task AG-MEMORY)
Date: 2026-10-08

核心职责：
1. 检测多权威 current（防多指针冲突与伪计划事实源）
2. 任务书要求无负责人 / 验收标准缺失检查
3. STATE 陈旧（落后于最新完成事件或脱节）
4. HANDOFF 引用过期快照或不一致 revision
5. 已完成任务仍写待执行（幽灵任务）
6. 重复主题打转与目标漂移（服务小目标时脱离项目范围；L0为日期日志）
7. 私有路径引用（.workbuddy、凭据目录等越界防护）
8. 跨项目污染（非当前 workspace 路径逃逸）
"""
from __future__ import annotations

import os
import re
import json
import stat
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from .public_files import PublicFiles, PublicFileError
from .member_adapter import validate_member_id, MemberAdapterError
from .silent_debug import note_silent


FORBIDDEN_PRIVATE_KEYWORDS = [
    ".workbuddy",
    ".git/",
    ".ssh/",
    ".aws/",
    ".azure/",
    "id_rsa",
    ".env",
    "credentials.json",
    "token.secret"
]

WHITELIST_DOCS = [
    "AGENTS.md",
    "PROJECT.md",
    "TASKBOOK.md",
    "STATE.md",
    "MEMORY.md",
    "HANDOFF.md",
    "LOG.md"
]


@dataclass
class Violation:
    code: str
    message: str
    file: str
    severity: str = "error"  # "error" | "warning"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProjectGuard:
    def __init__(self, workspace: Path | str):
        self.public = PublicFiles(workspace)
        self.workspace = self.public.root

    def _read(self, relative: str) -> Optional[str]:
        return self.public.read_text(relative)

    def _done_records(self):
        """Only the public machine mailbox directories; never a workspace walk."""
        for member in self.public.list_member_directories():
            for path in self.public.list_files(f'.agentbus/agents/{member}/done'):
                value = self.public.read_json(path.relative_to(self.workspace))
                if value.get('status') == 'done' and value.get('type', 'task') in ('task', 'goal', 'review'):
                    yield path, value

    def check_all(self) -> Dict[str, Any]:
        """运行所有 8 项结构与连续性守卫检查"""
        violations: List[Violation] = []
        for check in (self.check_multiple_authorities, self.check_taskbook_work_items,
                      self.check_state_freshness, self.check_handoff_consistency,
                      self.check_ghost_tasks, self.check_goal_drift_and_looping,
                      self.check_private_path_references, self.check_cross_project_pollution):
            try:
                violations.extend(check())
            except (OSError, PublicFileError) as exc:
                violations.append(Violation('PUBLIC_INPUT_UNVERIFIED', str(exc), check.__name__))

        errors = [v for v in violations if v.severity == "error"]
        warnings = [v for v in violations if v.severity == "warning"]

        return {
            "passed": len(errors) == 0,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "violations": [v.to_dict() for v in violations],
            "workspace": str(self.workspace).replace("\\", "/")
        }

    # 1. 多权威 current
    def check_multiple_authorities(self) -> List[Violation]:
        violations: List[Violation] = []
        plan_dir = self.public.checked('.agentbus/project-plan')
        if not plan_dir.exists():
            return violations

        # 检查是否有多余的 current 指针文件
        current_candidates = [p for p in self.public.list_files('.agentbus/project-plan')
                              if p.name.startswith('current')]
        if len(current_candidates) > 1:
            violations.append(Violation(
                code="MULTIPLE_CURRENT_POINTERS",
                message=f"发现多个 current 指针文件: {[f.name for f in current_candidates]}，计划指针必须唯一定义在 current.json",
                file=".agentbus/project-plan"
            ))

        # 检查根目录下是否有非法试图充当计划源的文件
        for root_plan in ["plan.json", "current.json", "project-plan.json", "plan_active.json"]:
            if self.public.checked(root_plan).exists():
                violations.append(Violation(
                    code="ROOT_PLAN_AUTHORITY_CONFLICT",
                    message=f"根目录下存在非法计划文件 {root_plan}，项目计划事实源必须位于 .agentbus/project-plan/current.json",
                    file=root_plan
                ))

        # 检查 current.json 是否指向合法存在的 revision
        cur_file = self.public.checked('.agentbus/project-plan/current.json')
        if cur_file.exists():
            try:
                cur_data = self.public.read_json('.agentbus/project-plan/current.json')
                rev_id = cur_data.get("revision_id") or cur_data.get("revision") or cur_data.get("id")
                if not isinstance(rev_id, str) or not re.fullmatch(r'r-[A-Za-z0-9._-]{1,62}', rev_id):
                    raise PublicFileError('invalid revision identity')
                if rev_id:
                    rev_path = self.public.checked(f'.agentbus/project-plan/revisions/{rev_id}.json')
                    if not rev_path.exists():
                        violations.append(Violation(
                            code="DANGLING_CURRENT_REVISION",
                            message=f"current.json 指向不存在的 revision: {rev_id}",
                            file=".agentbus/project-plan/current.json"
                        ))
                    else:
                        self.public.plan_head()
            except (OSError, PublicFileError, ValueError) as exc:
                violations.append(Violation(
                    code="MALFORMED_CURRENT_JSON",
                    message=f"current.json 解析失败: {exc}",
                    file=".agentbus/project-plan/current.json"
                ))

        return violations

    # 2. 任务书要求无负责人 / 验收标准
    def check_taskbook_work_items(self) -> List[Violation]:
        violations: List[Violation] = []
        content = self._read('TASKBOOK.md')
        if content is None:
            return violations

        # 检测章节工作项 (### 标题)
        sections = re.split(r"\n(?=###\s+)", content)
        for sec in sections:
            if not sec.strip().startswith("###"):
                continue
            lines = sec.strip().splitlines()
            title = lines[0].strip()

            # 排除非任务章节
            if any(k in title for k in ["背景", "总览", "变更历史", "说明", "附录"]):
                continue

            sec_lower = sec.lower()
            # 检查负责人
            has_owner = False
            for pat in [r"负责人[：:]\s*([^\n\r]+)", r"owner[：:]\s*([^\n\r]+)", r"指派[：:]\s*([^\n\r]+)"]:
                m = re.search(pat, sec, re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    if val and val.lower() not in ["待定", "未分配", "none", "tbd", "无", "待确认"]:
                        has_owner = True
                    break

            if not has_owner:
                violations.append(Violation(
                    code="WORK_ITEM_MISSING_OWNER",
                    message=f"工作项 [{title}] 缺少明确指派的负责人，不能成为可派工的有效任务",
                    file="TASKBOOK.md"
                ))

            # 检查验收标准
            has_acceptance = False
            for pat in [r"验收[标准准则]*[：:]\s*([^\n\r]+)", r"acceptance[：:]\s*([^\n\r]+)"]:
                m = re.search(pat, sec, re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    if val and val.lower() not in ["待定", "无", "tbd", "待补充"]:
                        has_acceptance = True
                    break

            if not has_acceptance:
                violations.append(Violation(
                    code="WORK_ITEM_MISSING_ACCEPTANCE",
                    message=f"工作项 [{title}] 缺少明确验收标准，违反结果可证契约",
                    file="TASKBOOK.md"
                ))

        return violations

    # 3. STATE 陈旧
    def check_state_freshness(self) -> List[Violation]:
        violations: List[Violation] = []
        content = self._read('STATE.md')
        if content is None:
            return violations

        # 提取 STATE 更新时间
        state_dt: Optional[datetime] = None
        m = re.search(r"更新时间[：:]\s*(\d{4}-\d{2}-\d{2}(?:T[0-9:.+Z\-]+)?)", content)
        if m:
            try:
                state_dt = datetime.fromisoformat(m.group(1).strip())
                if state_dt.tzinfo is None:
                    state_dt = state_dt.replace(tzinfo=timezone(timedelta(hours=8)))
            except Exception:
                # 功能类：STATE.md 时间戳格式不标准时回落到下方 YYYY-MM-DD 解析；
                # 留痕以便发现上游写入格式漂移。
                note_silent("project_guard.py:227", "functional",
                            "完整时间戳解析失败，回落简单日期格式")
                pass

        if not state_dt:
            # 尝试简单格式 YYYY-MM-DD
            m2 = re.search(r"更新时间[：:]\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", content)
            if m2:
                try:
                    state_dt = datetime.fromisoformat(m2.group(1).strip() + "T00:00:00+08:00")
                except Exception:
                    # 功能类：两种格式都解析失败则 state_dt 保持 None（视为无时间戳），
                    # 留痕以便区分"确实没写"与"写了但读不懂"。
                    note_silent("project_guard.py:236", "functional",
                                "简单日期格式亦解析失败，按无时间戳处理")
                    pass

        # 检查最新完成任务的时间
        latest_done_dt: Optional[datetime] = None
        latest_task_id = ""
        done_records = list(self._done_records())
        done_ids = {record.get('id', path.stem) for path, record in done_records}
        completion_times = [(path.stem, td.get('completed_at') or td.get('finished_at'))
                            for path, td in done_records]
        completion_times.extend((event.get('id'), event.get('ts'))
                                for event in self.public.recent_events()
                                if event.get('event') == 'done' and event.get('id') in done_ids
                                and event.get('type') in ('task', 'goal', 'review'))
        for identity, ts_str in completion_times:
            if not ts_str:
                continue  # Creation/claim time does not prove completion time.
            try:
                task_dt = datetime.fromisoformat(ts_str)
                if task_dt.tzinfo is None:
                    continue
                if latest_done_dt is None or task_dt > latest_done_dt:
                    latest_done_dt = task_dt
                    latest_task_id = identity
            except (ValueError, TypeError):
                continue

        if state_dt and latest_done_dt:
            if m and 'T' not in m.group(1) and latest_done_dt.astimezone(state_dt.tzinfo).date() == state_dt.date():
                return [Violation('STATE_FRESHNESS_UNVERIFIED',
                                  'STATE 仅声明日期；同日完成事件无法证明状态陈旧或已同步',
                                  'STATE.md', severity='warning')]
            # 如果最新任务完成时间显著晚于 STATE 更新时间 (> 300 秒)
            if (latest_done_dt - state_dt).total_seconds() > 300:
                violations.append(Violation(
                    code="STALE_STATE",
                    message=f"STATE.md 更新时间 ({state_dt.isoformat()}) 滞后于最新已完成任务 {latest_task_id} ({latest_done_dt.isoformat()})，状态未及时同步",
                    file="STATE.md"
                ))

        return violations

    # 4. HANDOFF 引用过期摘要
    def check_handoff_consistency(self) -> List[Violation]:
        violations: List[Violation] = []
        handoff_text = self._read('HANDOFF.md')
        cur_data = self.public.read_json('.agentbus/project-plan/current.json')
        if handoff_text is None or cur_data is None:
            return violations

        cur_rev = cur_data.get("revision_id") or cur_data.get("revision") or cur_data.get("id")
        cur_sha = cur_data.get("revision_sha256") or cur_data.get("content_sha256") or cur_data.get("sha256")

        if cur_rev and cur_rev not in handoff_text:
            violations.append(Violation(
                code="HANDOFF_EXPIRED_REVISION",
                message=f"HANDOFF.md 中引用的计划版本与当前采用指针 current.json ({cur_rev}) 不一致",
                file="HANDOFF.md"
            ))

        if cur_sha and len(cur_sha) >= 16:
            short_sha = cur_sha[:16]
            if short_sha not in handoff_text and cur_sha not in handoff_text:
                violations.append(Violation(
                    code="HANDOFF_EXPIRED_PLAN_SHA",
                    message=f"HANDOFF.md 缺少当前采用计划快照摘要证据 ({cur_sha[:8]}...)",
                    file="HANDOFF.md",
                    severity="warning"
                ))

        return violations

    # 5. 已完成任务仍写待执行 (幽灵任务)
    def check_ghost_tasks(self) -> List[Violation]:
        violations: List[Violation] = []
        content = self._read('STATE.md')
        if content is None:
            return violations

        # 收集所有已在 done/ 中的任务 ID
        done_ids: Set[str] = set()
        for path, record in self._done_records():
            done_ids.add(record.get('id', path.stem))

        # 提取 STATE.md 中“当前进行”或“下一步”小节提及的任务
        active_sections = []
        m_curr = re.search(r"## 当前[进行任务目标]*\s*\n+([\s\S]*?)(?=\n##|$)", content)
        if m_curr:
            active_sections.append(m_curr.group(1))
        m_next = re.search(r"## 下一步[^\n]*\s*\n+([\s\S]*?)(?=\n##|$)", content)
        if m_next:
            active_sections.append(m_next.group(1))

        combined = "\n".join(active_sections)
        # 寻找匹配 m- 开头的任务 ID
        found_ids = set(re.findall(r"m-[a-zA-Z0-9_\-]+", combined))
        for fid in found_ids:
            if fid in done_ids:
                violations.append(Violation(
                    code="COMPLETED_TASK_STILL_PENDING",
                    message=f"任务 {fid} 已经在 done/ 目录中终态归档，但在 STATE.md 中仍被标记为当前或下一步待办（幽灵任务）",
                    file="STATE.md"
                ))

        return violations

    # 6. 重复主题打转与目标漂移
    def check_goal_drift_and_looping(self) -> List[Violation]:
        violations: List[Violation] = []
        project_text = self._read('PROJECT.md')

        # 检查项目范围锚定；L0 是日期日志，不是宪法。
        if project_text is None:
            violations.append(Violation(
                code="MISSING_PROJECT_ANCHOR",
                message="缺少 PROJECT.md 顶层宗旨与边界定义文件，项目容易发生目标漂移",
                file="PROJECT.md"
            ))

        log_text = self._read('LOG.md')
        if log_text is None:
            return violations

        # 检测日志中是否在局部小议题上连续打转 (例如同名小目标出现 >= 3 次而未收敛)
        counts: Dict[str, int] = {}
        for line in log_text.splitlines():
            resolved = re.search(r'(?:已收敛|已完成|resolved|completed)\s*[:：]\s*(.+)', line, re.I)
            if resolved:
                counts.pop(resolved.group(1).strip(), None)
                continue
            topic = re.search(r'(?:围绕|针对|反复|重试|修复|调试|讨论)\s*[:：]\s*([^\n\r,，。]+)', line)
            if topic:
                key = topic.group(1).strip()
                counts[key] = counts.get(key, 0) + 1

        for k, cnt in counts.items():
            if cnt >= 3:
                violations.append(Violation(
                    code="REPETITIVE_THEME_LOOPING",
                    message=f"检测到在局部小议题 [{k}] 上连续重复记录 {cnt} 次，存在脱离主目标打转风险，需强制收敛至主目标",
                    file="LOG.md",
                    severity="warning"
                ))

        return violations

    # 7. 私有路径引用安全拦截
    def check_private_path_references(self) -> List[Violation]:
        violations: List[Violation] = []
        for doc_name in WHITELIST_DOCS:
            text = self._read(doc_name)
            if text is None:
                continue
            # Interpret each clause. A prohibition in one clause cannot exempt
            # a positive access instruction in another clause of the same line.
            for clause in re.split(r'[\n\r;；。]', text):
                normalized = clause.replace('\\', '/').casefold()
                for kw in FORBIDDEN_PRIVATE_KEYWORDS:
                    boundary = '' if kw.endswith('/') else r'(?![\w-]|\.[\w])'
                    occurrence = re.search(r'(?<![\w.-])' + re.escape(kw) + boundary, normalized)
                    if occurrence:
                        directives = list(re.finditer(
                            r'(?P<deny>不(?:要)?(?:读取|读|访问|扫描|修改)|'
                            r'(?:禁止|严禁|不得|不可)(?:读取|读|访问|扫描|修改)?|'
                            r'(?:do not|don.t|never|must not)\s+(?:read|access|scan|write|modify|open))|'
                            r'(?:读取|读|访问|参考|扫描|打开|复制|\b(?:read|access|scan|write|modify|open|copy)\b)',
                            normalized[:occurrence.start()]))
                        if directives and directives[-1].group('deny'):
                            continue
                        violations.append(Violation(
                            code='FORBIDDEN_PRIVATE_PATH_REFERENCE',
                            message=f'{doc_name} 存在未声明为禁止访问的私有路径引用 {kw}',
                            file=doc_name))

        return violations

    # 8. 跨项目路径污染
    def check_cross_project_pollution(self) -> List[Violation]:
        violations: List[Violation] = []
        current_ws_str = str(self.workspace).replace("\\", "/").lower()
        allowed_roots = [current_ws_str]
        config = self.public.read_json('.agentbus/config.json') or {}
        output_root = config.get('output_root')
        if isinstance(output_root, str):
            normalized_output = output_root.replace('\\', '/').rstrip('/').casefold()
            if re.match(r'^[a-z]:/', normalized_output) or normalized_output.startswith('/'):
                allowed_roots.append(normalized_output)

        for doc_name in WHITELIST_DOCS:
            text = self._read(doc_name)
            if text is None:
                continue

            # 检测明显的其他盘符或项目路径逃逸 (例如 d:/other_project, /var/other, c:/users/secret)
            # 排除当前 workspace 本身
            text = text.replace('\\', '/')
            # Quoted paths may contain spaces, notably the actual project root.
            matches = re.findall(r"[\"'`]([A-Za-z]:/[^\n\r\"'`<>]+)[\"'`]", text)
            without_quoted = re.sub(r"[\"'`][A-Za-z]:/[^\n\r\"'`<>]+[\"'`]", '', text)
            matches.extend(re.findall(r"([A-Za-z]:/[^ \n\r\"'<>`]+|(?<![:\w/])/[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-/]+)", without_quoted))
            for m in matches:
                norm_m = m.replace("\\", "/").lower()
                # 排除系统通用路径与当前 workspace 前缀
                if any(norm_m == root or norm_m.startswith(root + '/') for root in allowed_roots):
                    continue
                if any(norm_m.startswith(p) for p in ["c:/python", "/usr", "/bin", "/tmp", "http://", "https://"]):
                    continue
                # 若包含其他项目特征
                if "project" in norm_m or "workspace" in norm_m or "vibe" in norm_m:
                    violations.append(Violation(
                        code="CROSS_PROJECT_PATH_POLLUTION",
                        message=f"{doc_name} 引用了外部项目的绝对工作区路径 '{m}'，存在跨项目状态污染",
                        file=doc_name
                    ))

        return violations
