"""Read-only team admission evidence. Source: Codex, 2026-10-04.

A saved group, configured interface or historical result is not a live-session
handshake. This module neither launches software nor claims/repairs messages.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .profiles import validate_agent_id
from .project_workflow import resume_prompt, workflow_status

MAX_RECORDS = 2000
MAX_BYTES = 1024 * 1024
STATES = ('ready', 'processing', 'blocked', 'done')
LABELS = {'codex': 'Codex', 'workbuddy': 'WorkBuddy', 'trae': 'Trae',
          'zcode': 'ZCode', 'opencode': 'OpenCode'}
ADMISSION_TITLE = 'AgentBus 文件接入确认'


def _config_digest(cfg) -> str:
    if not _checked(cfg.path) or cfg.path.stat().st_size > MAX_BYTES:
        raise ValueError('Cannot safely read current project configuration')
    return hashlib.sha256(cfg.path.read_bytes()).hexdigest()


def _aware_timestamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def confirm_connection(cfg, caller: str) -> dict:
    """Explicit peer self-report, not authentication or a live-process proof."""
    from .bus import Bus
    validate_agent_id(caller)
    if caller not in cfg.enabled_workers():
        raise ValueError(f'成员 {caller!r} 尚未加入当前项目；请先执行：python -m agentbus member add --workspace "{cfg.workspace}" --id {caller}')
    if not workflow_status(cfg).get('ready'):
        raise ValueError(f'当前项目小组尚未保存或工作流文件不完整；请先执行：python -m agentbus workflow prepare --workspace "{cfg.workspace}"')
    digest = _config_digest(cfg)
    bus = Bus(cfg.workspace / '.agentbus')
    bus.init()
    message = bus.make(caller, cfg.director, 'info', title=ADMISSION_TITLE,
        body=json.dumps({'format': 'agentbus.admission', 'schema_version': 1,
                         'member': caller, 'config_sha256': digest,
                         'scope': 'member self-report and local workflow file checks'}, ensure_ascii=False),
        status='done')
    bus.post(message)
    return {'id': message.id, 'member': caller, 'to': cfg.director,
            'confirmed_at': message.ts, 'model_calls': 0,
            'scope': '文件接入自报；不代表认证、当前在线或任务验收通过'}


def connection_prompt(cfg, member: str) -> str:
    validate_agent_id(member)
    if member not in cfg.enabled_workers():
        raise ValueError('Member is not selected for this project')
    return (resume_prompt(cfg, member) +
        f'\n\n文件接入确认（不是新的任务）：项目 {cfg.workspace}；身份 {member}。'
        '\n先读取本项目入口和自己的工作流，核对身份、项目及权限。'
        f'\n具备宿主程序工具时，用该工作流登记的同一 AgentBus 程序调用 team join，'
        f'workspace 为上述项目，caller 为 {member}；不要猜程序路径或手改消息文件。'
        '\n这会发布正式 info 确认，尚无任务时回复“文件接入已登记，尚未派发任务”。'
        '\n没有程序工具时，如实回复“接入未确认，需要工具或人工接手”，不要伪造确认。'
        '\n只在本轮接入时确认一次，不定时重复，不轮询空信箱。'
        '\n接入自报不代表当前持续在线、自动接力或真实任务验收通过。')


def _checked(path: Path, *, directory=False) -> bool:
    """Refuse links before reads; optional missing paths remain missing."""
    if any(part.casefold() == '.workbuddy' for part in path.parts):
        raise ValueError('Private host paths are not an admission surface')
    full = Path(os.path.abspath(path))
    for node in reversed((full, *full.parents)):
        try:
            info = node.lstat()
        except FileNotFoundError:
            return False
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Admission path uses a link or reparse point')
        if node != full or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('Admission directory is occupied by a file')
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError('Admission message must be a regular file')
    return True


def _read_records(cfg, names):
    records, unreadable, counts = [], Counter(), {}
    truncated = False
    for name in names:
        counts[name] = Counter()
        for state in STATES:
            directory = cfg.workspace / '.agentbus/agents' / name / state
            try:
                if not _checked(directory, directory=True):
                    continue
                for path in sorted(directory.glob('*.json')):
                    if len(records) + sum(unreadable.values()) >= MAX_RECORDS:
                        return records, unreadable, counts, True
                    try:
                        if not _checked(path) or path.stat().st_size > MAX_BYTES:
                            raise ValueError('unreadable admission record')
                        value = json.loads(path.read_text(encoding='utf-8'))
                        if not isinstance(value, dict):
                            raise ValueError('object required')
                        identifier = value.get('id')
                        if not isinstance(identifier, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,96}', identifier):
                            raise ValueError('identifier required')
                        row = {key: value.get(key) for key in
                               ('id', 'type', 'sender', 'to', 'reply_to', 'result_to', 'status', 'ts')}
                        row['admission_sha'] = ''
                        if row['type'] == 'info' and value.get('title') == ADMISSION_TITLE:
                            payload = json.loads(value.get('body', ''))
                            if (isinstance(payload, dict) and payload.get('format') == 'agentbus.admission'
                                    and payload.get('schema_version') == 1
                                    and payload.get('member') == row['sender']
                                    and isinstance(payload.get('config_sha256'), str)
                                    and re.fullmatch('[0-9a-f]{64}', payload['config_sha256'])):
                                row['admission_sha'] = payload['config_sha256']
                        for key in ('type', 'sender', 'to', 'status'):
                            if not isinstance(row[key], str):
                                raise ValueError('invalid message header')
                        for key in ('reply_to', 'result_to'):
                            if row[key] is not None and not isinstance(row[key], str):
                                raise ValueError('invalid route header')
                        stamp = row['ts']
                        # Timestamps are data, not free text for the UI.
                        try:
                            row['ts'] = datetime.fromisoformat(stamp).isoformat() if isinstance(stamp, str) else ''
                        except ValueError:
                            row['ts'] = ''
                        row.update(mailbox=name, folder=state)
                        records.append(row)
                        if row['to'] != name:
                            unreadable[name] += 1
                        elif row['type'] in ('goal', 'task', 'review'):
                            counts[name][state] += 1
                    except (OSError, UnicodeError, ValueError, TypeError):
                        unreadable[name] += 1
            except (OSError, ValueError):
                unreadable[name] += 1
    return records, unreadable, counts, truncated


def connection_summary(cfg) -> dict:
    """Observe selected members' file evidence, never report session liveness."""
    selected = cfg.enabled_workers()
    for name in selected:
        validate_agent_id(name)
    names = list(selected) + [x for x in ('director', 'human') if x not in selected]
    records, unreadable, counts, truncated = _read_records(cfg, names)
    frequency = Counter(row['id'] for row in records)
    unique = {row['id']: row for row in records if frequency[row['id']] == 1 and row['to'] == row['mailbox']}
    evidence = {name: [] for name in selected}
    for result in records:
        origin = unique.get(result['reply_to'])
        if (result['type'] == 'result' and origin is not None and result['sender'] in evidence
                and (origin['to'] != result['sender'] or origin['result_to'] != result['to'])):
            unreadable[result['sender']] += 1
        if (result['type'] != 'result' or result['id'] not in unique or origin is None
                or origin['type'] not in ('goal', 'task', 'review')
                or origin['folder'] != 'done' or origin['status'] not in ('done', 'failed')
                or result['sender'] not in evidence or origin['to'] != result['sender']
                or not origin['result_to'] or origin['result_to'] != result['to']):
            continue
        evidence[result['sender']].append(dict(result, task_outcome=origin['status']))
    try:
        paused = _checked(cfg.workspace / '.agentbus/STOP')
    except (ValueError, OSError):
        paused = True  # uncertain STOP cannot authorize dispatch
    members = []
    current_digest = _config_digest(cfg)
    for name, worker in selected.items():
        display = str(worker.get('display_name') or LABELS.get(name, name))[:80]
        total, consumed = len(evidence[name]), sum(row['folder'] == 'done' for row in evidence[name])
        succeeded = sum(row['task_outcome'] == 'done' for row in evidence[name])
        failed = sum(row['task_outcome'] == 'failed' for row in evidence[name])
        dated = [(stamp, row) for row in evidence[name]
                 if (stamp := _aware_timestamp(row['ts'])) is not None]
        latest_stamp, latest = max(dated, key=lambda item: item[0]) if dated else (None, None)
        now = datetime.now(timezone.utc)
        recent_failed = (latest is not None and latest['task_outcome'] == 'failed'
                         and 0 <= (now - latest_stamp).total_seconds() <= 86400)
        mode = '独立接口（已配置）' if worker.get('can_spawn') else '对话接入'
        if total:
            label = f'已记录 {total} 条有效关联回执（成功 {succeeded} / 失败 {failed}）；已收取 {consumed} 条'
            if consumed < total:
                label += f'，待收取 {total - consumed} 条'
        else:
            label = '尚无有效任务回执记录'
        count = counts.get(name, Counter())
        if paused:
            key, lamp, color = 'paused', '暂停新派发', '#D97706'
            step = '项目已暂停新派发；在途任务可能仍在执行，请先核对。'
        elif unreadable[name] or truncated:
            key, lamp, color = 'attention', '记录需要检查', '#DC2626'
            step = '存在无法读取或过多的记录，请先在管理页检查项目；不据此判断信箱为空。'
        elif count['blocked']:
            key, lamp, color = 'blocked', '任务受阻', '#D97706'
            step = f'查看 {display} 的阻塞任务；确认原因后再继续，不重复派发。'
        elif count['processing']:
            key, lamp, color = 'claimed', '已认领，运行未确认', '#7C3AED'
            step = f'请 {display} 原认领会话报告已有任务；未确认停止前不要重做或回收。'
        elif count['ready']:
            key, lamp, color = 'pending', '有任务待接手', '#2563EB'
            step = f'复制 {display} 的接入指令，发送到该软件的当前项目对话。'
        elif recent_failed:
            key, lamp, color = 'recent_failure', '最近任务失败', '#D97706'
            step = (f'选择 {display}，点击“查看最近任务回执”查看任务 {latest["reply_to"]} 的失败原因；'
                    '不自动重派，也不自动扩大权限。')
        else:
            key, lamp, color = 'idle', '暂无任务，会话未确认', '#6B7280'
            step = f'在 {display} 打开本项目，发送接入指令；然后从一个成员对话发起任务。'
        members.append({
            'name': name, 'display_name': display, 'is_coordinator': name == cfg.director,
            'mode_label': mode, 'evidence_label': label, 'receipt_count': total,
            'consumed_receipt_count': consumed, 'unreadable_count': unreadable[name],
            'successful_receipt_count': succeeded, 'failed_receipt_count': failed,
            'latest_task_outcome': latest['task_outcome'] if latest else '',
            'latest_outcome_at': latest['ts'] if latest else '',
            'latest_task_id': latest['reply_to'] if latest else '',
            'latest_receipt_id': latest['id'] if latest else '',
            'latest_receipt_to': latest['to'] if latest else '',
            'task_counts': {state: count[state] for state in STATES},
            'current_session_verified': False, 'next_step': step,
            'indicator': {'key': key, 'label': lamp, 'color': color,
                          'symbol': {'claimed': '◐', 'paused': 'Ⅱ'}.get(key, '●')},
            'last_receipt_at': max((str(row['ts'] or '') for row in evidence[name]), default=''),
            'admission_confirmation_at': max((str(row['ts'] or '') for row in unique.values()
                if row['sender'] == name and row['to'] == cfg.director and row['type'] == 'info'
                and row['admission_sha'] == current_digest), default=''),
            'admission_label': '文件接入自报记录，不代表当前在线或任务验收',
            'detail': '当前会话未确认。历史文件回执不证明软件在线、产物正确或持续自动接力通过。',
        })
    return {'workspace': str(cfg.workspace), 'coordinator': cfg.director, 'members': members,
            'current_sessions_verified': False, 'continuous_relay_verified': False,
            'paused': paused, 'truncated': truncated, 'model_calls': 0,
            'evidence_scope': '本项目的任务/回执关联与归档记录；不认证发件软件或当前进程。'}
