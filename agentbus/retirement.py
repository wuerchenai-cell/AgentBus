"""Explicit administrative retirement; never requeue or grant a new budget.

Source: Codex, 2026-11-16. Does not establish actual process death.
"""
import hashlib
import json
import math
import time
import stat
from itertools import islice
from agentbus.bus import BusError, Message
from agentbus import claim_fencing, jobs, exclusive_lock
from agentbus.trigger.service import TriggerDeliveryService
from agentbus.config import load
from agentbus.operation_lock import task_lock
from agentbus.public_files import PublicFiles


def _now():
    return time.time()


def _digest(message):
    return hashlib.sha256(json.dumps(message.to_dict(),ensure_ascii=False,sort_keys=True).encode('utf8')).hexdigest()



def _checked(public, relative):
    path = public.checked(relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise BusError('退役控制文件存在硬链接；拒绝读取或修改')
    return path


def _read(public, path):
    relative = path.relative_to(public.root)
    _checked(public, relative)
    raw = public.read_bytes(relative, limit=4 * 1024 * 1024)
    if raw is None:
        raise BusError('原任务记录缺失')
    _checked(public, relative)
    value = Message.from_dict(json.loads(raw.decode('utf-8')), strict=True)
    value.validate()
    return value


def _guard_controls(public, agent, message_id):
    operation = hashlib.sha256((agent+'\0'+message_id).encode()).hexdigest()
    mutex = hashlib.sha256(('global-task-mutex\0'+message_id).encode()).hexdigest()
    for relative in ('events.jsonl', 'config.json', 'STOP',
            'operation-locks/'+operation+'.lock',
            'exclusive-locks/'+message_id+'.lock.json',
            'exclusive-locks/released/'+message_id+'.released.json',
            'exclusive-locks/.mutex/'+mutex+'.lock'):
        path = _checked(public,relative)
        if path.is_file() and path.stat().st_size > 64 * 1024 * 1024:
            raise BusError('控制记录过大；先核对原证据')


def retire_stopped_claim(bus, agent, message_id, *, expected_claim_epoch,
                         owner_confirmed_inactive=False, reason):
    if owner_confirmed_inactive is not True:
        raise BusError('退役前必须明确确认原执行者已停止')
    claim_fencing.validate_agent_id(agent)
    claim_fencing.validate_task_id(message_id)
    if type(expected_claim_epoch) is not int or expected_claim_epoch < 1:
        raise BusError('退役必须绑定原认领代次')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>10000:
        raise BusError('必须填写有界的退役原因和断点')
    reason=reason.strip()
    public=PublicFiles(bus.root)
    _guard_controls(public,agent,message_id)
    paths={key:_checked(public,f'agents/{agent}/{key}/{message_id}.json') for key in ('processing','ready','blocked','done')}
    with task_lock(bus.root,agent,message_id):
        for key in paths:
            paths[key]=_checked(public,f'agents/{agent}/{key}/{message_id}.json')
        agents=public.checked('agents')
        members=list(islice(agents.iterdir(),513))
        if len(members)>512:
            raise BusError('成员目录过多，不能可靠核对任务唯一性')
        for member in members:
            if member.name.casefold() in ('.workbuddy','不要访问','do not access'):
                continue
            public.checked(f'agents/{member.name}')
            if member.name==agent:
                continue
            for location in ('processing','ready','blocked','done'):
                duplicate=public.checked(f'agents/{member.name}/{location}/{message_id}.json')
                if duplicate.exists():
                    raise BusError('同ID存在其他成员副本；保留全部记录并先核对')
        if paths['done'].exists() or paths['ready'].exists():
            raise BusError('任务已有完成或待办记录；拒绝退役')
        _guard_controls(public,agent,message_id)
        events=bus.events()
        if any(event.get('event')=='audit_invalid_line' for event in events):
            raise BusError('审计记录不完整；先核对原证据')
        if paths['blocked'].exists():
            closed=_read(public,paths['blocked'])
            intent=next((event for event in reversed(events) if
                event.get('event')=='claim_retirement_intent' and event.get('id')==message_id and
                event.get('agent')==agent and event.get('claim_epoch')==expected_claim_epoch and
                event.get('reason')==reason and event.get('after_sha256')==_digest(closed)),None)
            if (not intent or closed.id!=message_id or closed.to!=agent or
                    closed.status!='blocked' or closed.claim_id or closed.claim_epoch!=expected_claim_epoch):
                raise BusError('已有blocked不是本次退役事务；保留记录')
            lock_fingerprint = intent.get('lock_fingerprint')
            if not isinstance(lock_fingerprint,str) or len(lock_fingerprint)!=64:
                raise BusError('退役意图缺少物理锁绑定；保留原证据')
            if paths['processing'].exists():
                active=_read(public,paths['processing'])
                if _digest(active)!=intent.get('before_sha256'):
                    raise BusError('退役期间原认领已变化；保留两份证据')
                paths['processing'].unlink()
        else:
            if not paths['processing'].is_file():
                raise BusError('没有唯一的在途原任务')
            current=_read(public,paths['processing'])
            if (current.id!=message_id or current.to!=agent or current.claim_epoch!=expected_claim_epoch or
                    current.type not in ('task','goal') or current.status not in ('claimed','running') or
                    not claim_fencing.is_fenced(current) or current.claim_agent!=agent or
                    current.claimed_by!=agent or current.supervision):
                raise BusError('原认领身份、代次或审核状态不允许退役')
            if bus._valid_committed_result(current) is not None:
                raise BusError('已有合法提交，须先完成回执归档')
            deadline=current.deadline_ts
            if type(deadline) not in (int,float) or not math.isfinite(deadline) or deadline<=0:
                raise BusError('原任务期限无效；保留记录')
            expired=_now()>=deadline
            _checked(public,'config.json')
            cfg=load(bus.root.parent)
            if current.job_id:
                _checked(public,f'jobs/{current.job_id}.json')
                job=jobs.message_job(cfg,current)
                job_deadline=job.get('deadline_ts')
                if type(job_deadline) not in (int,float) or not math.isfinite(job_deadline) or job_deadline<=0:
                    raise BusError('原作业期限无效；保留记录')
                allocated = (job['goal_message'] if current.id == job['goal_id'] else
                             next((item['message'] for item in job['tasks'] if item.get('id') == current.id), None))
                if (not isinstance(allocated, dict) or
                        not jobs._task_intent_matches(current, allocated, events) or
                        current.deadline_ts != allocated.get('deadline_ts') or
                        current.budget_s != allocated.get('budget_s')):
                    raise BusError('原任务意图或期限与正式分配不一致；保留记录')
                expired=expired or _now()>=job_deadline
            if not expired:
                raise BusError('任务尚未超期；使用正常交接流程')
            lock = exclusive_lock.get_task_lock_info(bus.root,message_id)
            if (not lock or lock.get('owner_agent') != agent or
                    lock.get('claim_epoch') != expected_claim_epoch or lock.get('claim_id') != current.claim_id):
                raise BusError('原认领与物理锁不一致；保留原任务及锁')
            lock_fingerprint = hashlib.sha256(current.claim_id.encode('utf-8')).hexdigest()
            before=_digest(current)
            closed=Message.from_dict(current.to_dict(),strict=True)
            closed.status='blocked'
            closed.result=(current.result+'\n' if current.result else '')+'[停止退役；未完成] '+reason
            closed.claimed_by=closed.claimed_at=''
            claim_fencing.revoke(closed)
            # The appended checkpoint must not turn a readable source into
            # an unreadable terminal record. Check the Windows text form too.
            closed_text=json.dumps(closed.to_dict(),ensure_ascii=False,indent=2)
            if len(closed_text.replace('\n','\r\n').encode('utf-8'))>4*1024*1024:
                raise BusError('退役后的记录将超过读取上限；保留原认领，请先缩短原因或整理断点')
            _guard_controls(public,agent,message_id)
            bus.audit(event='claim_retirement_intent',agent=agent,id=message_id,
                      claim_epoch=expected_claim_epoch,reason=reason,before_sha256=before,
                      after_sha256=_digest(closed),lock_fingerprint=lock_fingerprint,confirmation_source='caller_assertion')
            if _digest(_read(public,paths['processing']))!=before:
                raise BusError('退役期间原任务已变化；保留记录')
            bus._write(paths['blocked'],closed)
            paths['processing'].unlink()
        _guard_controls(public,agent,message_id)
        if exclusive_lock.get_task_lock_info(bus.root,message_id) is not None:
            exclusive_lock.release_task_lock(bus.root,message_id,agent,
                expected_claim_epoch=expected_claim_epoch,
                expected_claim_fingerprint=lock_fingerprint)
        if not any(event.get('event')=='claim_retired' and event.get('id')==message_id and
                   event.get('agent')==agent and event.get('after_sha256')==_digest(closed) for event in bus.events()):
            bus.audit(event='claim_retired',agent=agent,id=message_id,claim_epoch=expected_claim_epoch,
                      after_sha256=_digest(closed),reason=reason,confirmation_source='caller_assertion')
        TriggerDeliveryService(bus.root.parent).on_work_state_changed(agent,drain=False)
        return closed
