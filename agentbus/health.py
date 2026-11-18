"""Read-only project diagnostics. Source: Codex healthcheck, 2026-10-03."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .config import ConfigError, validate
from .jobs import list_jobs
from .profiles import ProfileError, project_skill_paths
from .project_workflow import WorkflowError, _manifest, workflow_status, _launcher_status


def _processing_summary(cfg, registered: set[str]) -> tuple[list[dict], bool]:
    """Observe physical files, never Bus.processing() which may clean duplicates.

    Source: WorkBuddy/Root maturity review, 2026-10-03. File age is evidence
    of an unchanged file, not a process liveness certificate.
    """
    records = []
    agents = cfg.workspace / '.agentbus/agents'
    _project_path(cfg.workspace, agents)
    if not agents.exists():
        return records, False
    for directory in sorted(agents.iterdir()):
        _project_path(cfg.workspace, directory)
        if not directory.is_dir():
            continue
        processing = directory / 'processing'
        _project_path(cfg.workspace, processing)
        if processing.exists() and not processing.is_dir():
            raise ValueError('processing directory is occupied by a file')
        for path in sorted(processing.glob('*.json')):
            if len(records) >= 200:
                return records, True
            _project_path(cfg.workspace, path)
            row = {'agent': directory.name, 'id': path.stem,
                   'path': path.relative_to(cfg.workspace).as_posix(),
                   'registered_job': path.stem in registered,
                   'claim_complete': False, 'claimed_by': '',
                   'heartbeat_stale': False, 'status': 'unreadable'}
            try:
                stat = path.stat()
                if stat.st_size > 1024 * 1024:
                    raise ValueError('message too large for diagnostic')
                value = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(value, dict):
                    raise ValueError('message object required')
                status = value.get('status')
                row['status'] = status if status in (
                    'pending', 'claimed', 'running', 'done', 'failed', 'blocked') else 'unknown'
                owner = value.get('claimed_by')
                # Export only recognized identity metadata, not arbitrary input.
                row['claimed_by'] = owner if owner == directory.name else ''
                row['claim_complete'] = bool(
                    row['status'] in ('claimed', 'running') and row['claimed_by']
                    and isinstance(value.get('claimed_at'), str) and value['claimed_at'])
                age = max(0.0, time.time() - stat.st_mtime)
                row['heartbeat_age_s'] = round(age, 2)
                row['heartbeat_stale'] = age > int(cfg.gates.get('heartbeat_timeout_s', 600))
                if not row['claim_complete']:
                    row['message'] = '认领未完整提交或残留终态；保留文件，先核对原认领者。'
                elif row['heartbeat_stale']:
                    row['message'] = '文件已较长时间未更新，不证明进程死亡；先核对原认领者，不能直接重做。'
                elif not row['registered_job']:
                    row['message'] = '此消息未关联已登记作业；保留认领，核对原会话与交接。'
                else:
                    row['message'] = '已记录认领；本检查不能证明原执行进程仍存活。'
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row['error_type'] = type(exc).__name__
                row['message'] = '执行中消息无法安全读取；保留证据，不认领或修复。'
            records.append(row)
    return records, False


def save_diagnostic(cfg, destination: Path) -> dict:
    """Save local allowlisted metadata at an explicitly chosen new path.

    No task text, results, checkpoints, commands, environments or credentials.
    This is not a repair operation and does not alter the project.
    """
    destination = Path(destination).expanduser()
    if any(p.casefold() == '.workbuddy' for p in (*destination.parts, *destination.resolve().parts)):
        raise ValueError('诊断不能写入私有 .workbuddy 目录')
    observed = inspect_project(cfg)
    def identifier(value, kind):
        value = str(value)
        if kind == 'agent' and value in cfg.data['workers']:
            return value
        if kind == 'message' and re.fullmatch(r'(?:m|rx)-[0-9]{10,20}-[0-9a-f]{6,32}', value):
            return value
        if kind == 'job' and re.fullmatch(r'j-[0-9a-f]{32}', value):
            return value
        return 'invalid-' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]

    def check_code(value):
        if value.startswith('processing:'):
            return 'processing:' + identifier(value[len('processing:'):], 'message')
        if value.startswith('job:'):
            return 'job:' + identifier(value[len('job:'):], 'job')
        return value  # Other check codes are program constants or validated member IDs.

    processing = []
    for row in observed.get('processing', []):
        item = {k: row[k] for k in (
            'registered_job', 'status', 'claim_complete', 'heartbeat_stale',
            'heartbeat_age_s', 'error_type', 'message') if k in row}
        item.update(agent=identifier(row['agent'], 'agent'),
                    id=identifier(row['id'], 'message'),
                    claimed_by=identifier(row['claimed_by'], 'agent') if row['claimed_by'] else '')
        processing.append(item)
    payload = {'schema_version': 1, 'source': 'AgentBus local read-only diagnostic',
               'observed_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
               'status': observed['status'], 'model_calls': 0,
               'native_loading_verified': False,
               'checks': [{'code': check_code(c['code']), 'status': c['status']}
                          for c in observed['checks']],
               'members': observed['members'],
               'jobs': [dict({k: j[k] for k in ('state', 'remaining_s', 'pending_reviews')},
                             id=identifier(j['id'], 'job'))
                        for j in observed['jobs']],
               'processing': processing,
               'processing_truncated': observed.get('processing_truncated', False)}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(text)
    return payload


def _project_path(root: Path, path: Path) -> None:
    """Reject links outside the selected project or into private host memory."""
    resolved = path.resolve()
    if (not resolved.is_relative_to(root.resolve())
            or any(part.casefold() == '.workbuddy' for part in resolved.parts)):
        raise ValueError('检查路径越出当前项目或进入私有目录，已停止读取')


def _job_paths(cfg) -> None:
    """Check only the fixed inputs consumed by list_jobs; no project tree walk."""
    root = cfg.workspace
    bus = root / '.agentbus'
    for path in (bus, bus / 'jobs', bus / 'agents', bus / 'events.jsonl', bus / 'STOP'):
        _project_path(root, path)
    for directory in (bus, bus / 'jobs', bus / 'agents'):
        if directory.exists() and not directory.is_dir():
            raise ValueError('作业目录被文件占用，不能判定为无作业：' + str(directory))
    for path in (bus / 'jobs').glob('j-*.json'):
        _project_path(root, path)
    agents = bus / 'agents'
    if not agents.exists():
        return
    for agent in agents.iterdir():
        _project_path(root, agent)
        if not agent.is_dir():
            continue
        for state in ('ready', 'processing', 'blocked', 'done'):
            directory = agent / state
            _project_path(root, directory)
            if directory.exists() and not directory.is_dir():
                raise ValueError('state directory is occupied by a file')
            for path in directory.glob('*.json'):
                _project_path(root, path)


def _launcher_ready(launcher) -> bool:
    return _launcher_status(launcher)[0]


def inspect_project(cfg) -> dict:
    """Observe configuration and durable work without executing or repairing it.

    Existing project APIs validate workflow hashes and task/receipt state.
    An automatic entry in config is not a local authentication/turn certificate.
    """
    report = {'status': 'healthy', 'workspace': str(cfg.workspace), 'model_calls': 0,
              'checks': [], 'members': {}, 'jobs': [], 'processing': [],
              'native_loading_verified': False}

    def add(code: str, status: str, message: str) -> None:
        report['checks'].append({'code': code, 'status': status, 'message': message})
        if status == 'invalid':
            report['status'] = 'invalid'
        elif status == 'attention' and report['status'] != 'invalid':
            report['status'] = 'attention'

    try:
        _project_path(cfg.workspace, cfg.path)
        if not cfg.path.is_file():
            raise ConfigError('当前项目尚未保存小组配置')
        validate(cfg.data)
        skill_paths = project_skill_paths(cfg)
    except (ConfigError, ProfileError, OSError, ValueError, TypeError, AttributeError) as exc:
        add('config', 'invalid', str(exc))
        return report
    add('config', 'ok', f'成员与组长配置有效；组长：{cfg.director}')

    for name, worker in cfg.enabled_workers().items():
        automatic = bool(worker.get('can_spawn') and worker.get('executor_enabled', True))
        report['members'][name] = {
            'mode': 'automatic-configured' if automatic else 'manual',
            'executor_enabled': bool(worker.get('executor_enabled', True)),
            'automatic_accepted': False,
        }
        if automatic:
            message = '已配置自动入口；本检查不验证认证、权限交互或真实任务回执'
        elif not worker.get('executor_enabled', True):
            message = '已加入小组但未选为执行成员'
        else:
            message = '需正在工作的桌面对话接手；文件保留进度，回合结束后可续接'
        add('member:' + name, 'info', message)

    manifest = {}
    try:
        _project_path(cfg.workspace, cfg.workspace / '.agentbus/workflow.json')
        for relative in ('AGENTBUS.md', *skill_paths.values()):
            _project_path(cfg.workspace, cfg.workspace / relative)
        status = workflow_status(cfg)
        manifest = _manifest(cfg.workspace)
        add('workflow', 'ok' if status['ready'] else 'attention',
            '项目工作流和登记哈希一致；原生加载仍需在新对话验证' if status['ready']
            else status.get('reason') or '项目工作流缺失或已变动；请检查并使用保存小组修复，检查不会覆盖文件')
    except (WorkflowError, OSError, ValueError, TypeError) as exc:
        add('workflow', 'attention', str(exc))
    try:
        launcher = manifest.get('launcher')
        launcher_exists, launcher_reason = _launcher_status(launcher)
    except (OSError, ValueError, TypeError):
        launcher_exists = False
        launcher_reason = '项目程序入口无法安全检查'
    integrity_recorded = (isinstance(manifest.get('launcher'), dict)
                          and manifest['launcher'].get('mode') == 'frozen'
                          and 'sha256' in manifest['launcher'])
    add('launcher', 'ok' if launcher_exists else 'attention',
        ('已登记的程序文件与摘要一致；检查没有运行该程序或验证认证' if integrity_recorded
         else '已登记的程序文件存在；未登记完整性摘要，检查没有运行该程序') if launcher_exists
        else launcher_reason + '；打开正确的 EXE 并重新保存当前小组')

    try:
        stop = cfg.workspace / '.agentbus/STOP'
        _project_path(cfg.workspace, stop)
        paused = stop.exists()
        add('stop', 'attention' if paused else 'ok',
            'STOP 正在阻止新派发；检查不会删除它，也不强制中断在途任务' if paused
            else '未设置 STOP')
    except (OSError, ValueError) as exc:
        add('stop', 'attention', str(exc))

    registered = set()
    try:
        _job_paths(cfg)
        from .bus import Bus
        try:
            audit = Bus(cfg.workspace / '.agentbus').events()
            damaged = sum(row.get('event') == 'audit_invalid_line' for row in audit)
            add('audit', 'attention' if damaged else 'ok',
                f'审计有 {damaged} 处无法读取的记录；保留证据，不能据此确认监督完成。' if damaged
                else '审计记录可读取；本检查不证明任务产物正确。')
        except (OSError, ValueError) as exc:
            add('audit', 'attention', '审计无法安全读取；保留证据：' + type(exc).__name__)
        jobs = list_jobs(cfg)
        for job in jobs:
            registered.add(job['goal_id'])
            registered.update(task['id'] for task in job.get('tasks', []))
        # Keep a compact observed summary rather than copying task bodies.
        report['jobs'] = [{'id': job['id'], 'state': job['state'],
                           'reason': job.get('reason', ''),
                           'remaining_s': job.get('remaining_s', 0),
                           'pending_reviews': len(job.get('pending_reviews') or [])}
                          for job in jobs]
        add('jobs', 'ok', f'已读取 {len(jobs)} 个持久作业；没有认领、恢复或执行任务')
        for job in report['jobs']:
            state = job['state']
            outcome = 'attention' if state in ('blocked', 'stopped') else 'info'
            message = job['reason'] or {
                'completed': '已验收完成', 'queued': '已排队',
                'working': '执行中；检查不会抢占认领者',
                'awaiting_summary': '等待组长验收或同伴评审',
                'waiting_handoff': '等待桌面对话接手',
            }.get(state, state)
            add('job:' + job['id'], outcome, message)
    except (ConfigError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        add('jobs', 'attention', '作业记录无法安全读取；已保留证据：' + str(exc))
    try:
        records, truncated = _processing_summary(cfg, registered)
        report['processing'] = records
        report['processing_truncated'] = truncated
        for row in records:
            attention = (not row['claim_complete'] or row['heartbeat_stale']
                         or not row['registered_job'])
            add('processing:' + row['id'], 'attention' if attention else 'info',
                row['agent'] + ' / ' + row['id'] + '：' + row['message'])
        if truncated:
            add('processing', 'attention', '执行中消息超过200条，本次仅显示前200条；没有丢弃或修复其余记录。')
    except (OSError, ValueError, TypeError) as exc:
        add('processing', 'attention', '执行中记录无法安全检查；已保留证据：' + type(exc).__name__)
    return report
