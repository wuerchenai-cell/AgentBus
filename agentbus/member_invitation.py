"""Recoverable project invitations. Codex integration, 2026-10-05.

No model calls, desktop actions, credentials or permission inheritance.
Prepared intent is saved before posting. A retry reuses the exact task.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .bus import Bus, BusError, Message, now_iso
from .invitation_copy import build_instructions, describe_invitation
from .lease import LeaseManager
from .member_library import _safe
from .project_workflow import workflow_status
from .silent_debug import note_silent
from .workspace_files import WorkspaceFiles
from .profiles import validate_agent_id


_REQUIRED = {'schema_version', 'id', 'workspace', 'member', 'coordinator',
             'request_id', 'writable', 'creator', 'created_at', 'deadline_ts',
             'state', 'message', 'events', 'input_path', 'output_path'}
_OPTIONAL = {'input_sha256', 'config_sha256', 'workflow_sha256', 'reason', 'committed_at'}
_TASK_INTENT = ('id', 'sender', 'to', 'type', 'title', 'body', 'result_to',
                'refs', 'expected_artifacts', 'deadline_ts', 'budget_s')


def _checked(path):
    """Reject links before opening data or lease control files."""
    path = _safe(path)
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise ValueError('UNSAFE_PATH: hard-linked invitation or control file')
    return path


def _unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError('INVITE_CONFLICT: duplicate JSON key')
        data[key] = value
    return data


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('INVITE_CONFLICT: invalid timestamp')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('timezone required')
    except ValueError as exc:
        raise ValueError('INVITE_CONFLICT: invalid timestamp') from exc


def _validate_record(data, path, cfg):
    if (not isinstance(data, dict) or not _REQUIRED <= set(data)
            or not set(data) <= _REQUIRED | _OPTIONAL
            or type(data['schema_version']) is not int or data['schema_version'] != 1):
        raise ValueError('INVITE_CONFLICT: invalid record fields')
    for name in ('member', 'coordinator', 'creator'):
        try:
            validate_agent_id(data[name])
        except (TypeError, ValueError) as exc:
            raise ValueError('INVITE_CONFLICT: invalid member identity') from exc
    if (not isinstance(data['request_id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', data['request_id'])
            or type(data['writable']) is not bool
            or data['state'] not in {'prepared', 'committed', 'conflict'}
            or not isinstance(data['workspace'], str)):
        raise ValueError('INVITE_CONFLICT: invalid intent fields')
    root = _safe(cfg.workspace)
    expected_id = 'i-' + _hash((str(root) + '\0' + data['member'] + '\0'
                               + data['request_id']).encode('utf-8'))[:32]
    if (data['id'] != expected_id or data['id'] != path.stem
            or data['workspace'] != str(root)
            or data['input_path'] != f'成果/成员接入/{expected_id}/input.txt'
            or data['output_path'] != f'成果/成员接入/{expected_id}/output.txt'):
        raise ValueError('INVITE_CONFLICT: project, record ID or sample path mismatch')
    if (type(data['deadline_ts']) not in (int, float)
            or not math.isfinite(data['deadline_ts']) or data['deadline_ts'] <= 0):
        raise ValueError('INVITE_CONFLICT: invalid deadline')
    _timestamp(data['created_at'])
    if 'committed_at' in data:
        _timestamp(data['committed_at'])
    if 'reason' in data and not isinstance(data['reason'], str):
        raise ValueError('INVITE_CONFLICT: invalid conflict reason')
    for name in ('input_sha256', 'config_sha256', 'workflow_sha256'):
        if name in data and (not isinstance(data[name], str)
                or not re.fullmatch(r'[0-9a-f]{64}', data[name])):
            raise ValueError('INVITE_CONFLICT: invalid digest')
    events = data['events']
    if not isinstance(events, list) or len(events) > 100:
        raise ValueError('INVITE_CONFLICT: invalid event collection')
    for event in events:
        if (not isinstance(event, dict) or set(event) != {'event', 'source', 'at', 'caller'}
                or event['event'] not in {'copied', 'sent', 'self_reported'}
                or not isinstance(event['source'], str) or not event['source'].strip()
                or len(event['source']) > 512):
            raise ValueError('INVITE_CONFLICT: invalid observation event')
        _timestamp(event['at'])
        try:
            validate_agent_id(event['caller'])
        except (TypeError, ValueError) as exc:
            raise ValueError('INVITE_CONFLICT: invalid observation identity') from exc
    message = data['message']
    if message is None:
        if data['state'] == 'committed' or 'committed_at' in data:
            raise ValueError('INVITE_CONFLICT: committed record has no task')
        return
    if not isinstance(message, dict):
        raise ValueError('INVITE_CONFLICT: invalid task snapshot')
    try:
        task = Message.from_dict(message, strict=True)
        task.validate()
    except (TypeError, ValueError, BusError) as exc:
        raise ValueError('INVITE_CONFLICT: invalid task snapshot') from exc
    if (not isinstance(task.id, str) or not re.fullmatch(r'm-[0-9]+-[0-9a-f]{6}', task.id)
            or task.sender != data['creator'] or task.to != data['member']
            or task.result_to != data['coordinator'] or task.type != 'task'
            or task.status != 'pending' or not isinstance(task.body, str) or not task.body.strip()
            or task.title != '成员接入：有界文件样例'
            or task.refs != [data['input_path']]
            or task.expected_artifacts != ([data['output_path']] if data['writable'] else [])
            or type(task.deadline_ts) not in (int, float) or task.deadline_ts != data['deadline_ts']
            or type(task.budget_s) is not int or not 1 <= task.budget_s <= 1800
            or not {'input_sha256', 'config_sha256', 'workflow_sha256'} <= set(data)
            or data['input_sha256'] != _hash(('AgentBus sample ' + data['id'] + '\n').encode('utf-8'))):
        raise ValueError('INVITE_CONFLICT: task snapshot has different intent')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _save_record(path,data):
    path=_checked(path);path.parent.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(data,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False).encode('utf-8')
    if len(raw)>65536:raise ValueError('INVITE_CONFLICT: record exceeds 64 KiB')
    temp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        _checked(path);os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


def _read_record(path,cfg):
    path=_checked(path)
    with path.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('INVITE_CONFLICT: oversized record')
    try:data=json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_object)
    except (ValueError,UnicodeError) as exc:raise ValueError('INVITE_CONFLICT: unreadable record') from exc
    _validate_record(data,path,cfg)
    return data


def _path(cfg,invite_id):
    if not isinstance(invite_id,str) or not re.fullmatch(r'i-[0-9a-f]{32}',invite_id):
        raise ValueError('INVITE_CONFLICT: invalid invitation ID')
    return _safe(cfg.workspace/'.agentbus/invites'/(invite_id+'.json'))


@contextmanager
def _locked(cfg,path,caller):
    path=_checked(path)
    lease_root=_safe(cfg.workspace/'.agentbus'); _safe(lease_root/'leases')
    _checked(lease_root/'leases'/'.mutation.lock')
    manager=LeaseManager(lease_root)
    relative=path.relative_to(cfg.workspace).as_posix()
    _checked(manager._file(relative))
    lease=manager.acquire(relative,caller,ttl_s=60,note='serialize invitation intent')
    if lease is None:raise ValueError('LEASE_BUSY: invitation is being updated')
    try:yield
    finally:
        _checked(lease_root/'leases'/'.mutation.lock');_checked(manager._file(relative))
        manager.release(lease)


def _selected(cfg,caller,member):
    people=cfg.enabled_workers()
    if (not isinstance(caller,str) or not isinstance(member,str)
            or caller not in people or member not in people or cfg.director not in people
            or any(people[item].get('enabled') is not True for item in (caller,member,cfg.director))):
        raise ValueError('UNKNOWN_MEMBER: save and select the member and coordinator first')


def _render(cfg,record):
    message=record.get('message')
    if record['state']!='committed' or not message:
        return {'invite_id':record['id'],'state':'conflict','task_id':None,
                'reason':'这不是可执行邀请。'+record.get('reason','项目工作流未就绪；请先保存团队。')}
    context={'workspace':str(cfg.workspace),'member':record['member'],
        'display_name':cfg.worker(record['member']).get('display_name') or record['member'],
        'coordinator':record['coordinator'],'task_id':message['id'],
        'inbox_path':str(cfg.workspace/'.agentbus/agents'/record['member']/'ready'/(message['id']+'.json')),
        'input_path':str(cfg.workspace/record['input_path']),
        'output_path':str(cfg.workspace/record['output_path']),
        'writable':record['writable'],'request_id':record['id']}
    instructions=build_instructions(context)
    # Publication has already happened; the leader must not create it twice.
    instructions['leader']=instructions['leader'].replace('1) 只登记并勾选','1) 核对已保存团队中的')
    instructions['leader']=instructions['leader'].replace(
        '2) 由程序按登记接口创建本次接入样例任务（task ID 必须是上面这一个）；',
        '2) 本次样例任务已由程序建立；只处理上述 task ID，不再重复创建；')
    instructions['leader']=instructions['leader'].replace(
        '3) 把「给新成员」的指令原文交给用户，请用户粘贴到',
        '3) 有用户已授权的 Computer Use 时，自行在正确项目对话送达「给新成员」指令；无可用电脑操作时只请用户粘贴一次到')
    return {'invite_id':record['id'],'task_id':message['id'],'state':'committed',
            'input_path':record['input_path'],'output_path':record['output_path'],
            'deadline_ts':record['deadline_ts'],'instructions':instructions,'reason':''}


def prepare_invitation(cfg,caller,member,request_id,*,writable=True):
    """Create one real bounded sample, or return a non-executable conflict."""
    _selected(cfg,caller,member)
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',request_id):
        raise ValueError('INVITE_CONFLICT: an explicit portable request ID is required')
    if type(writable) is not bool:raise ValueError('INVITE_CONFLICT: writable must be boolean')
    root=_safe(cfg.workspace)
    identity='i-'+_hash((str(root)+'\0'+member+'\0'+request_id).encode('utf-8'))[:32]
    path=_path(cfg,identity)
    intent={'workspace':str(root),'member':member,'coordinator':cfg.director,'request_id':request_id,'writable':writable}
    with _locked(cfg,path,caller):
        record=_read_record(path,cfg) if path.exists() else {
            'schema_version':1,'id':identity,**intent,'creator':caller,'created_at':now_iso(),
            'deadline_ts':time.time()+min(int(cfg.gates.get('task_timeout_s',1800)),1800),
            'state':'prepared','message':None,'events':[],
            'input_path':f'成果/成员接入/{identity}/input.txt',
            'output_path':f'成果/成员接入/{identity}/output.txt'}
        if any(record.get(k)!=v for k,v in intent.items()):
            raise ValueError('INVITE_CONFLICT: request ID already has different intent')
        reason=''
        if _safe(root/'.agentbus/STOP').exists():reason='STOP 已阻止派发；目标及断点保留。'
        elif time.time()>record['deadline_ts']:reason='本次样例已到期；保留原任务，重新验证需要新的明确请求。'
        else:
            try:
                config_raw=_checked(cfg.path).read_bytes()
                saved=json.loads(config_raw.decode('utf-8'),object_pairs_hook=_unique_object)
                if saved.get('workers')!=cfg.data.get('workers') or saved.get('director')!=cfg.director:
                    reason='团队草稿尚未保存；请先保存团队。'
                else:
                    status=workflow_status(cfg)
                    if not status['ready']:reason=status['reason']
                    elif record.get('config_sha256') and record['config_sha256']!=_hash(config_raw):
                        reason='团队配置与本次已发布样例不一致；请明确发起新的接入请求。'
                    elif record.get('workflow_sha256') and record['workflow_sha256']!=_hash(
                            _checked(root/'.agentbus/workflow.json').read_bytes()):
                        reason='工作流登记与本次已发布样例不一致；请明确发起新的接入请求。'
            except (OSError,ValueError) as exc:reason='配置或工作流读取失败：'+str(exc)
        if reason:
            record.update(state='conflict',reason=reason);_save_record(path,record)
            return _render(cfg,record)
        bus=Bus(root/'.agentbus');bus.init();bus.dirs(member);bus.dirs(cfg.director)
        if record['message'] is not None:
            task=Message.from_dict(record['message'],strict=True)
            existing=bus.find_message(task.id)
            if existing is None and 'committed_at' in record:
                record.update(state='conflict',reason='原始已发布任务缺失；保留断点，不重新创建任务。')
                _save_record(path,record);return _render(cfg,record)
            if existing is not None and any(getattr(existing,k)!=getattr(task,k) for k in _TASK_INTENT):
                raise ValueError('INVITE_CONFLICT: existing task has different intent')
        marker='AgentBus sample '+identity
        files=WorkspaceFiles(root)
        target=_checked(root/record['input_path']);raw=(marker+'\n').encode('utf-8')
        if target.exists():
            if target.read_bytes()!=raw:raise ValueError('INVITE_CONFLICT: sample input changed; original preserved')
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            with _locked(cfg,target,caller):
                if target.exists():raise ValueError('INVITE_CONFLICT: sample input appeared')
                temp=target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
                try:
                    with temp.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                    _checked(target);os.replace(temp,target)
                finally:temp.unlink(missing_ok=True)
        record['input_sha256']=_hash(raw)
        record['config_sha256']=_hash(_safe(cfg.path).read_bytes())
        record['workflow_sha256']=_hash(_safe(root/'.agentbus/workflow.json').read_bytes())
        if record.get('message') is None:
            output_step=(f'仅在 {record["output_path"]} 写入 UTF-8 内容，必须严格等于输入第一行（无换行）。'
                         if writable else '只读输入，完整回复第一行内容；不得写入输出，写能力未验证。')
            body=(f'来源：AgentBus 接入样例，{now_iso()}。成员平级，结果收件人：{cfg.director}。\n'
                  f'项目：{root}。只处理本任务。\n读取 {record["input_path"]}，核对 SHA256 {record["input_sha256"]}。\n'
                  f'{output_step}\n自述可用文件/程序工具与限制，不猜版本和认证。\n'
                  '优先登记程序 answer/report 回执；无程序工具时聊天完整回复，由获授权运输方忠实转录。\n'
                  '写产物前取租约，产物按 path/bytes/sha256 交付；不裸写总线JSON、不扩大权限、不读凭据。')
            msg=bus.make(caller,member,'task',title='成员接入：有界文件样例',body=body,result_to=cfg.director,
                deadline_ts=record['deadline_ts'],budget_s=min(int(cfg.gates.get('task_timeout_s',1800)),1800),
                expected_artifacts=[record['output_path']] if writable else [],refs=[record['input_path']])
            record['message']=msg.to_dict()
        record.update(state='prepared',reason='');_save_record(path,record)
        msg=Message.from_dict(record['message'],strict=True);existing=bus.find_message(msg.id)
        if existing is None:
            if _safe(root/'.agentbus/STOP').exists():
                record.update(state='conflict',reason='STOP 已阻止派发。');_save_record(path,record);return _render(cfg,record)
            bus.post(msg)
        elif any(getattr(existing,k)!=getattr(msg,k) for k in _TASK_INTENT):
            raise ValueError('INVITE_CONFLICT: existing task has different intent')
        record['state']='committed';record.setdefault('committed_at',now_iso());_save_record(path,record)
        return _render(cfg,record)


def record_invitation_event(cfg,caller,invite_id,event,source):
    if event not in {'copied','sent','self_reported'} or not isinstance(source,str) or not source.strip():
        raise ValueError('INVITE_CONFLICT: explicit observation source required')
    if event == 'sent':
        authorized = source == 'user' or source == 'authorized-computer-use:' + caller
        if not authorized:
            raise ValueError('INVITE_CONFLICT: controlled observation source required')
    path=_path(cfg,invite_id)
    with _locked(cfg,path,caller):
        record=_read_record(path,cfg);_selected(cfg,caller,record['member'])
        if len(record['events'])>=100:raise ValueError('INVITE_CONFLICT: observation limit reached')
        record['events'].append({'event':event,'source':source[:512],'at':now_iso(),'caller':caller})
        _save_record(path,record)


def invitation_status(cfg,invite_id):
    """Derive sample evidence from real files; never infer global online state."""
    record=_read_record(_path(cfg,invite_id),cfg); evidence={'task_id':(record.get('message') or {}).get('id')}
    for event in record.get('events',[]):
        if event['event']=='copied':evidence['copied_at']=event['at']
        elif event['event']=='sent' and event['source']=='user':evidence['sent_confirmed_by']='user'
        elif event['event']=='sent' and event['source'].startswith('authorized-computer-use:'):
            evidence['observed_sent_by']=event['source'].split(':',1)[1]
            evidence['observed_at']=event['at']
        elif event['event']=='self_reported':evidence['self_report']=event['source']
    bus=Bus(cfg.workspace/'.agentbus');task=bus.find_message(evidence.get('task_id') or '')
    results=bus.find_results(evidence.get('task_id') or '',sender=record['member'],recipient=record['coordinator'])
    if task and any(getattr(task,k)!=record['message'][k] for k in _TASK_INTENT):
        raise ValueError('INVITE_CONFLICT: existing task has different intent')
    if len(results)==1 and task:
        result=results[0];evidence.update(receipt_id=result.id,linked_task_id=result.reply_to,outcome=task.status)
        valid=False;artifact_digest=''
        if task.status=='done' and result.status=='done' and record['writable']:
            files=WorkspaceFiles(cfg.workspace);check=files.verify_artifacts(result.artifacts)
            try:
                output=_checked(cfg.workspace/record['output_path']).read_bytes()
                original=_checked(cfg.workspace/record['input_path']).read_bytes()
                valid=(check['verdict']=='match' and any(a['path']==record['output_path'] for a in result.artifacts)
                       and _hash(original)==record['input_sha256'] and output==original.rstrip(b'\n'))
                if valid:artifact_digest=_hash(output)
            except (OSError,ValueError):
                # 功能类：读取输入/输出文件失败时 valid 保持 False → 采信为 unverified
                #（fail-closed，安全方向）。但必须留痕，否则"证据读取失败"与
                #"证据确实不匹配"在外观上完全相同。
                note_silent("member_invitation.py:352", "functional",
                            "能力样本证据读取失败，回落 unverified（fail-closed）")
                pass
        elif task.status=='done' and result.status=='done' and not record['writable']:
            # Read-only analysis is valid only when the actual marker is present.
            marker='AgentBus sample '+record['id']
            try:
                original=_checked(cfg.workspace/record['input_path']).read_bytes()
                valid=marker in result.body and _hash(original)==record['input_sha256']
            except (OSError,ValueError):
                # 功能类：同上，读取失败即 unverified（fail-closed）；留痕以区分原因。
                note_silent("member_invitation.py:359", "functional",
                            "只读复核证据读取失败，回落 unverified（fail-closed）")
                pass
        evidence['capability_sample']={'outcome':'passed' if valid else 'unverified',
            'transport':'formal-result','verified':valid,'acceptance_verified':valid,
            'project_id':str(cfg.workspace),'task_id':task.id,'result_id':result.id,
            'capability_family':'file-write' if record['writable'] else 'readonly-review',
            'artifact_verified':valid if record['writable'] else False,
            'artifact_sha256':artifact_digest}
    description=describe_invitation(evidence)
    return {**description,'invite_id':record['id'],'state':record['state'],
            'workspace':str(cfg.workspace),'member':record['member'],'observed_at':now_iso(),
            'multiple_results_conflict':len(results)>1}
