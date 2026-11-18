"""Local notification slots. A slot is never evidence of host acceptance."""
import json
import os
import tempfile
import re
import uuid
from pathlib import Path
from agentbus.operation_lock import task_lock
from agentbus.trigger.contracts import TriggerContractError, TriggerErrorCode, validate_member_id


def _read_slot(path):
    if path.is_symlink():
        raise ValueError('CORRUPT：唤醒信号不能是链接')
    with path.open('rb') as stream:
        raw = stream.read(262145)
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('CORRUPT：唤醒信号 JSON 损坏，原文件已保留') from exc
    if len(raw) > 262144 or not isinstance(data, dict):
        raise ValueError('CORRUPT：唤醒信号格式或大小无效，原文件已保留')
    return data


def _matches(data, contact):
    return (data.get('member_id') == contact.member_id
            and data.get('session_id') == contact.native_conversation_id
            and data.get('binding_id', contact.binding_id) == contact.binding_id
            and data.get('binding_revision', contact.revision) == contact.revision)


def _request_id(data):
    identity = data.get('request_id')
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity):
        raise ValueError('CORRUPT：唤醒请求编号无效')
    return identity


def validate_source_id(identity):
    # Trigger-only legacy IDs can be shorter than fenced task IDs. They still
    # must be a single ordinary filename, without traversal or Windows ADS.
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', identity):
        raise ValueError('CORRUPT：唤醒来源编号不是安全文件名')
    return identity


def _archive_slot(root, member, path, reason):
    directory = root / 'triggers/signal-history' / member
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (reason + '-' + uuid.uuid4().hex + '.json')
    os.replace(path, destination)
    return destination


def _check_journal_binding(journal, data, contact):
    identity = _request_id(data)
    path = journal.requests_dir / (identity + '.json')
    if not path.is_file():
        return
    request = _read_slot(path)
    if (request.get('member_id') != contact.member_id or request.get('binding_id') != contact.binding_id
            or request.get('binding_revision') != contact.revision
            or request.get('source_message_ids') != data.get('source_message_ids')):
        raise ValueError('CORRUPT：信号与不可变请求绑定不一致，未改变投递状态')


def _old_contact(workspace, member, binding, revision):
    if (not isinstance(binding, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', binding)
            or type(revision) is not int or revision < 1):
        return None
    path = Path(workspace) / '.agentbus/agents' / member / 'contact-history' / f'{binding}-{revision}.json'
    if not path.is_file() or path.is_symlink():
        return None
    from agentbus.trigger.contracts import Contact
    return Contact.from_dict(_read_slot(path))


def reconcile_contact(workspace, contact):
    """Retire only provably local, unconsumed notifications from an old binding."""
    from agentbus.trigger.journal import DeliveryJournal
    root, member = Path(workspace) / '.agentbus', contact.member_id
    retired = []
    with task_lock(root, 'trigger-member', member), task_lock(root, 'trigger-signal', member):
        if stopped(workspace):
            return {'status': 'stopped', 'retired': []}
        journal = DeliveryJournal(workspace)
        path = root / 'triggers/wake-signals' / (member + '.json')
        if path.is_file():
            data = _read_slot(path)
            if not _matches(data, contact):
                old = _old_contact(workspace, member, data.get('binding_id'), data.get('binding_revision'))
                if old is None or not _matches(data, old):
                    return {'status': 'target_changed', 'retired': [], 'needs_confirmation': True}
                identity = _request_id(data)
                _check_journal_binding(journal, data, old)
                state = journal.get_request_status(identity)
                result = state.get('details', {}).get('result', {})
                local = (result.get('accepted_by_host') is False
                         and result.get('status') in ('queued', 'notified'))
                if state['status'] in ('submitting', 'uncertain') or (state['status'] == 'submitted' and not local):
                    return {'status': 'needs_reconcile', 'retired': [], 'needs_confirmation': True}
                if state['status'] != 'unknown':
                    journal.transition(identity, 'cancelled', {'reason': 'binding_replaced_before_notification_consumption',
                                       'non_delivery_verified': True, 'notification_retired': True})
                _archive_slot(root, member, path, 'obsolete')
                retired.append(identity)
        # Queued requests have never been submitted. Known old bindings cannot
        # remain at the queue head or reserve the same source forever.
        for request in journal.list_by_status('queued') + journal.list_by_status('prepared'):
            if request.member_id != member or (request.binding_id == contact.binding_id and request.binding_revision == contact.revision):
                continue
            previous = _old_contact(workspace, member, request.binding_id, request.binding_revision)
            if previous is None:
                continue
            journal.transition(request.request_id, 'cancelled',
                               {'reason': 'binding_replaced_before_submission', 'non_delivery_verified': True})
            retired.append(request.request_id)
        if retired:
            processing = root / 'agents' / member / 'processing'
            uncertain = any(r.member_id == member for status in ('submitting', 'uncertain') for r in journal.list_by_status(status))
            marker = root / 'triggers/busy' / (member + '.json')
            if marker.is_file() and not uncertain and not (processing.is_dir() and next(processing.glob('*.json'), None)):
                busy = _read_slot(marker)
                if busy.get('reason') in ('delivery_pending', 'notification_consumed', 'processing_or_submission'):
                    marker.unlink()
        return {'status': 'reconciled', 'retired': retired}


def consume_signal(workspace, member):
    """A consumed notification is not host acceptance or an idle indication."""
    from agentbus.trigger.contacts import load_contact
    from agentbus.trigger.journal import DeliveryJournal
    validate_member_id(member)
    root = Path(workspace) / '.agentbus'
    with task_lock(root, 'trigger-member', member), task_lock(root, 'trigger-signal', member):
        if stopped(workspace):
            raise ValueError('STOPPED：项目已停止或策略不可用，信号保留')
        path = root / 'triggers/wake-signals' / (member + '.json')
        if not path.is_file():
            return None
        data = _read_slot(path)
        contact = load_contact(workspace, member)
        if contact is None:
            raise ValueError('TARGET_MISSING：成员没有有效联系卡，信号保留')
        if not _matches(data, contact):
            reconciliation = reconcile_contact(workspace, contact)
            if not path.is_file():
                raise ValueError('TARGET_CHANGED：旧绑定通知已归档，请等待当前聊天的新通知')
            raise ValueError(f'TARGET_CHANGED：信号不属于当前绑定，状态 {reconciliation["status"]}，需核实')
        identity = _request_id(data)
        journal = DeliveryJournal(workspace)
        state = journal.get_request_status(identity)
        _check_journal_binding(journal, data, contact)
        from agentbus.trigger.service import _atomic_write_json
        sources = data.get('source_message_ids')
        if not isinstance(sources, list):
            raise ValueError('CORRUPT：唤醒信号缺少来源清单')
        for source in sources:
            validate_source_id(source)
        marker = root / 'triggers/busy' / (member + '.json')
        observed = _read_slot(marker) if marker.is_file() else {}
        observed.update(member_id=member, busy=True,
                        reason=observed.get('reason') or 'notification_consumed',
                        notification_source_ids=sources, notification_binding_id=contact.binding_id,
                        notification_binding_revision=contact.revision)
        _atomic_write_json(marker, observed)
        if state.get('details', {}).get('notification_consumed') is True:
            _archive_slot(root, member, path, 'consumed-recovery')
            raise ValueError('ALREADY_CONSUMED：通知已记录消费，残留副本已归档，未重复唤醒')
        if state['status'] != 'unknown':
            journal.transition(identity, state['status'], {'notification_consumed': True,
                               'consumed_binding_id': contact.binding_id, 'consumed_binding_revision': contact.revision})
        _archive_slot(root, member, path, 'consumed')
        return data


def stopped(workspace):
    root = Path(workspace) / '.agentbus'
    if (root / 'STOP').exists():
        return True
    policy = root / 'triggers/policy.json'
    if not policy.exists():
        return False
    try:
        data = json.loads(policy.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            for key in ('max_retries_per_request', 'max_auto_hops'):
                value = data.get(key, 1)
                if type(value) is not int or value < 1:
                    return True
            hops = data.get('current_hop_count', 0)
            if type(hops) is not int or hops < 0:
                return True
        return (not isinstance(data, dict) or data.get('stopped', False) is not False
                or data.get('delivery_enabled', True) is not True)
    except (OSError, ValueError):
        return True


def put_signal(workspace, contact, request):
    validate_member_id(contact.member_id)
    root = Path(workspace) / '.agentbus'
    with task_lock(root, 'trigger-member', contact.member_id), task_lock(root, 'trigger-signal', contact.member_id):
        if stopped(workspace):
            raise TriggerContractError('Delivery stopped', TriggerErrorCode.STOPPED)
        from agentbus.trigger.contacts import load_contact
        current = load_contact(workspace, contact.member_id)
        if current is not None and (current.binding_id != contact.binding_id or current.revision != contact.revision):
            raise TriggerContractError('Contact binding changed before notification', TriggerErrorCode.TARGET_CHANGED)
        target = root / 'triggers/wake-signals' / (contact.member_id + '.json')
        if target.exists():
            old = json.loads(target.read_text(encoding='utf-8'))
            if old.get('request_id') != request.request_id:
                raise TriggerContractError('Notification slot is busy', TriggerErrorCode.BUSY)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'schema_version':1, 'request_id':request.request_id,
                    'member_id':contact.member_id, 'native_conversation_id':contact.native_conversation_id,
                    'session_id':contact.native_conversation_id, 'binding_id':contact.binding_id,
                    'protocol':'acp' if contact.connector_id == 'agentbus.acp-bridge' else 'agentbus-trigger/1',
                    'binding_revision':contact.revision, 'purpose':request.purpose,
                    'source_message_ids':request.source_message_ids, 'prompt_summary':request.prompt_summary,
                    'created_at':request.created_at, 'status':'pending'}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
