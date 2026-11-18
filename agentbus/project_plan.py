"""Local immutable project plans. Source: Codex internal helper, 2026-10-04.

Caller checks describe selected roles, not authentication. Cooperative leases
and CAS do not protect against another program with identical filesystem access.
Raw plan/opinion text is limited to 64 KiB. This module never runs a model,
publishes a task, changes an existing job, or renews its deadline.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .bus import Bus, BusError, Message, now_iso
from .config import Config, ConfigError, load
from .lease import LeaseManager

_SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z', re.ASCII)
_SHA = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_LIMIT = 64 * 1024
_RECORD_LIMIT = 4 * 1024 * 1024
_VERDICTS = {'accept', 'accept with changes', 'reject'}


class ProjectPlanError(ValueError):
    pass


def _safe(value, label='id'):
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise ProjectPlanError(f'{label} must contain 1-64 safe ASCII characters')
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > _LIMIT:
        raise ProjectPlanError(f'{label} must be nonempty UTF-8 text of at most 64 KiB')
    return value


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _comparison_path(resolved):
    """Compare equivalent Windows final paths without weakening containment.

    Windows realpath may retain its extended prefix when a file is created
    between its two final-path queries. Normalize only ordinary drive/UNC
    spellings; device namespaces remain distinct and are not rewritten.
    """
    text = str(resolved)
    if os.name == 'nt':
        if text.casefold().startswith('\\\\?\\unc\\'):
            text = '\\\\' + text[8:]
        elif re.match(r'^\\\\\?\\[A-Za-z]:\\', text):
            text = text[4:]
    return Path(text)


def _checked(cfg, relative):
    root = cfg.workspace.absolute()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or any(x.casefold() == '.workbuddy' for x in (*root.parts, *rel.parts)):
        raise ProjectPlanError('path must stay inside the project and outside private directories')
    target = root / rel
    for node in [*reversed(root.parents), root, *[root.joinpath(*rel.parts[:n]) for n in range(1, len(rel.parts) + 1)]]:
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
            raise ProjectPlanError('symlink/reparse paths are not accepted')
    if not _comparison_path(target.resolve()).is_relative_to(_comparison_path(root.resolve())):
        raise ProjectPlanError('path escapes the current project')
    return target


def _path(cfg, *parts):
    return _checked(cfg, Path('.agentbus/project-plan').joinpath(*parts))


def _read(cfg, path):
    path = _checked(cfg, path.relative_to(cfg.workspace.absolute()))
    try:
        if path.stat().st_size > _RECORD_LIMIT:
            raise ProjectPlanError('stored record exceeds its bounded size')
        raw = path.read_bytes()
        value = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ProjectPlanError(f'cannot read preserved plan record: {path.name}') from exc
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or value.get('workspace') != str(cfg.workspace.resolve())):
        raise ProjectPlanError('plan record is malformed or belongs to another project')
    return value, _sha(raw)


def _member(cfg, caller, coordinator=False):
    _safe(caller, 'caller')
    if caller not in cfg.enabled_workers() or (coordinator and caller != cfg.director):
        raise ProjectPlanError('caller is not the selected member/current coordinator')


def _stop(cfg):
    if _checked(cfg, '.agentbus/STOP').exists():
        raise ProjectPlanError('STOP is active; plan mutations are blocked')


@contextmanager
def _locked(cfg, caller):
    _member(cfg, caller)
    _stop(cfg)
    _path(cfg)
    _checked(cfg, '.agentbus/leases/.mutation.lock')
    manager = LeaseManager(_checked(cfg, '.agentbus'))
    until = time.monotonic() + 5
    lease = None
    while lease is None and time.monotonic() < until:
        lease = manager.acquire('.agentbus/project-plan/current.json', 'plan:' + caller,
                                ttl_s=90, note='serialize local plan history and CAS')
        if lease is None:
            time.sleep(0.02)
    if lease is None:
        raise ProjectPlanError('project plan lease is busy; retry')
    try:
        _stop(cfg)
        yield lease
    finally:
        manager.release(lease)


def _write(cfg, path, value, replace=False, commit_check=None):
    _stop(cfg)
    path = _checked(cfg, path.relative_to(cfg.workspace.absolute()))
    raw = _bytes(value)
    if len(raw) > _RECORD_LIMIT:
        raise ProjectPlanError('aggregate record exceeds its bounded size')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _checked(cfg, path.relative_to(cfg.workspace.absolute()))
        _stop(cfg)
        if commit_check:
            commit_check()
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)  # Atomic create, never overwrite history.
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise ProjectPlanError('immutable plan history already exists with different content')
    finally:
        temporary.unlink(missing_ok=True)


def _head(cfg):
    path = _path(cfg, 'current.json')
    if not path.exists():
        return None, None
    value, digest = _read(cfg, path)
    if value.get('kind') != 'current' or not _SHA.fullmatch(str(value.get('revision_sha256', ''))):
        raise ProjectPlanError('invalid current plan pointer; preserve it for inspection')
    _safe(value.get('revision_id'), 'revision id')
    return value, digest


def _proposal(cfg, identity):
    value, _ = _read(cfg, _path(cfg, 'proposals', _safe(identity) + '.json'))
    if (value.get('id') != identity or value.get('kind') != 'proposal'
            or _sha(_text(value.get('content_md'), 'plan').encode('utf-8')) != value.get('content_sha256')
            or not isinstance(value.get('reviewers'), list) or not value['reviewers']
            or any(not isinstance(x, str) or not _SAFE.fullmatch(x) for x in value['reviewers'])
            or len(set(value['reviewers'])) != len(value['reviewers'])):
        raise ProjectPlanError('invalid preserved proposal')
    return value


def _record(cfg, kind, identity, caller):
    return {'schema_version': 1, 'kind': kind, 'id': identity,
            'workspace': str(cfg.workspace.resolve()), 'created_at': now_iso(),
            'caller': caller, 'source': 'AgentBus local project plan',
            'role_checks_are_authentication': False}


def propose_plan(cfg: Config, caller: str, content_md: str, reason: str,
                 reviewers: list[str], expected_current_sha256: str | None = None,
                 request_id: str | None = None) -> dict[str, Any]:
    _member(cfg, caller)
    _text(content_md, 'plan')
    _text(reason, 'reason')
    if not isinstance(reviewers, list) or not 1 <= len(reviewers) <= 20 or len(set(reviewers)) != len(reviewers):
        raise ProjectPlanError('reviewers must contain 1-20 distinct selected members')
    for member in reviewers:
        _member(cfg, member)
    request = _safe(request_id, 'request id') if request_id is not None else uuid.uuid4().hex
    identity = 'p-' + _sha((caller + '\0' + request).encode('utf-8'))[:32]
    intent = {'content_md': content_md, 'content_sha256': _sha(content_md.encode('utf-8')),
              'reason': reason, 'reviewers': reviewers, 'request_id': request,
              'base_current_sha256': expected_current_sha256}
    with _locked(cfg, caller):
        path = _path(cfg, 'proposals', identity + '.json')
        if path.exists():
            previous = _proposal(cfg, identity)
            if any(previous.get(key) != value for key, value in intent.items()):
                raise ProjectPlanError('request id already names a different proposal')
            return previous
        head, digest = _head(cfg)
        plan_snapshot(cfg)  # Validate the pointer target before accepting a baseline.
        if digest != expected_current_sha256:
            raise ProjectPlanError('current plan CAS mismatch; reread its summary')
        duplicates = 0
        folder = _path(cfg, 'proposals')
        for existing in folder.glob('*.json') if folder.exists() else []:
            old = _proposal(cfg, existing.stem)
            duplicates += (old['content_sha256'] == intent['content_sha256']
                           and old['base_current_sha256'] == expected_current_sha256)
        value = {**_record(cfg, 'proposal', identity, caller), **intent,
                 'coordinator': cfg.director, 'base_revision_id': head['revision_id'] if head else None,
                 'duplicate_count': duplicates + 1,
                 'state': 'blocked' if duplicates >= 2 else 'proposed',
                 'blocked_reason': 'duplicate_proposal' if duplicates >= 2 else ''}
        _write(cfg, path, value)
        return value


def _intent(message):
    fields = ('id', 'ts', 'sender', 'to', 'type', 'title', 'body', 'refs', 'reply_to',
              'result_to', 'exit_code', 'hop', 'attempt', 'budget_s', 'deadline_ts')
    return _sha(_bytes({key: getattr(message, key) for key in fields}))


def _formal(cfg, proposal, task_id):
    _safe(task_id, 'task id')
    bus = Bus(_checked(cfg, '.agentbus'))
    agents = _checked(cfg, '.agentbus/agents')
    tasks, results = [], []
    for agent in sorted(agents.iterdir() if agents.exists() else []):
        if not _SAFE.fullmatch(agent.name):
            continue  # Never inspect a private/unknown directory.
        _checked(cfg, agent.relative_to(cfg.workspace.absolute()))
        for state in ('ready', 'processing', 'blocked', 'done'):
            folder = _checked(cfg, (agent / state).relative_to(cfg.workspace.absolute()))
            for path in sorted(folder.glob('*.json')) if folder.exists() else []:
                _checked(cfg, path.relative_to(cfg.workspace.absolute()))
                try:
                    if path.stat().st_size > _RECORD_LIMIT:
                        raise ProjectPlanError('oversized formal bus evidence')
                    message = Message.from_dict(json.loads(path.read_text(encoding='utf-8')), strict=True)
                    message.validate()
                except (OSError, ValueError, BusError) as exc:
                    rel_path = path.relative_to(cfg.workspace.absolute()).as_posix()
                    raise ProjectPlanError(f'formal bus evidence is unreadable ({rel_path}: {exc}); preserve it') from exc
                if message.to != agent.name:
                    raise ProjectPlanError('formal message physical route differs from its recipient')
                if message.id == task_id:
                    tasks.append((message, state))
                if message.type == 'result' and message.reply_to == task_id:
                    results.append(message)
    terminal = [task for task, state in tasks if state == 'done' and task.status == 'done']
    if len(terminal) != 1 or len(results) != 1:
        raise ProjectPlanError('review needs a successful terminal task and exactly one formal result')
    task, result = terminal[0], results[0]
    if (task.type not in ('task', 'review') or task.to not in proposal['reviewers']
            or task.result_to != proposal['coordinator'] or result.sender != task.to
            or result.to != proposal['coordinator'] or task.exit_code not in (None, 0)
            or result.exit_code not in (None, 0) or result.status in ('blocked', 'failed')
            or any(_intent(copy) != _intent(task) for copy, _ in tasks)):
        raise ProjectPlanError('review has wrong member, route, formal intent or execution outcome')
    _text(result.body, 'review opinion')
    return task, result


def _verdict(body):
    try:
        value = json.loads(body)
        explicit = value.get('verdict') if isinstance(value, dict) else None
    except ValueError:
        explicit = None
    token = r'(?:\*\*)?(accept with changes|accept|reject)(?:\*\*)?(?![A-Za-z])'
    prefix = r'(?im)^\s*(?:[-*#>]\s*)*(?:[一二三四五六七八九十]+[、.]\s*)?(?:\*\*)?'
    matches = re.findall(prefix + r'(?:verdict|总评|总体判定|总体结论|评审结论|结论)\s*[:：]\s*' + token, body)
    if not matches and explicit is None:
        matches = re.findall(prefix + token, body)
    verdicts = {match.lower() for match in matches}
    if explicit in _VERDICTS:
        verdicts.add(explicit)
    if len(verdicts) != 1:
        raise ProjectPlanError('opinion needs one explicit verifiable verdict')
    return verdicts.pop()


def _evidence(cfg, relative=None):
    if relative is None:
        return cfg
    root = _checked(cfg, relative)
    config = _checked(cfg, Path(relative) / '.agentbus/config.json')
    if root == cfg.workspace.absolute() or not config.is_file():
        raise ProjectPlanError('evidence must identify an existing child workspace configuration')
    try:
        child = load(root)
    except (OSError, ValueError, ConfigError) as exc:
        raise ProjectPlanError('child evidence configuration is invalid') from exc
    if child.director != cfg.director or child.workspace.resolve() != root.resolve():
        raise ProjectPlanError('child evidence uses another workspace/coordinator')
    return child


def _review(cfg, proposal, member):
    review, _ = _read(cfg, _path(cfg, 'reviews', proposal['id'], member + '.json'))
    evidence = _evidence(cfg, review.get('evidence_workspace'))
    task, result = _formal(evidence, proposal, review.get('task_id'))
    if (review.get('member') != member or review.get('content_sha256') != proposal['content_sha256']
            or review.get('original_workspace', str(evidence.workspace.resolve())) != str(evidence.workspace.resolve())
            or review.get('task_sha256') != _intent(task) or review.get('result_sha256') != _intent(result)
            or review.get('body') != result.body or review.get('verdict') != _verdict(result.body)):
        raise ProjectPlanError('recorded opinion no longer matches its formal evidence')
    if review.get('reviewed_file') and _sha(_checked(evidence, review['reviewed_file']).read_bytes()) != proposal['content_sha256']:
        raise ProjectPlanError('observed frozen review file changed')
    return review


def record_review(cfg: Config, caller: str, proposal_id: str, task_id: str,
                  verdict: str, *, reviewed_file: str | None = None,
                  evidence_workspace: str | None = None) -> dict[str, Any]:
    with _locked(cfg, caller):
        proposal = _proposal(cfg, proposal_id)
        evidence_cfg = _evidence(cfg, evidence_workspace)
        task, result = _formal(evidence_cfg, proposal, task_id)
        if caller not in (cfg.director, task.to) or verdict not in _VERDICTS or _verdict(result.body) != verdict:
            raise ProjectPlanError('caller/verdict does not match the actual review')
        digest = proposal['content_sha256']
        association = 'content_sha256'
        if not re.search(r'(?i)(?:sha-?256|plan_sha256|content_sha256)\s*[:=：]\s*[`\"\']?' + digest + r'(?![0-9a-f])', task.body):
            if reviewed_file is None:
                raise ProjectPlanError('review task does not identify this frozen plan content')
            supplied = Path(reviewed_file)
            relative = supplied.relative_to(evidence_cfg.workspace.absolute()) if supplied.is_absolute() else supplied
            checked = _checked(evidence_cfg, relative)
            names = {str(relative).replace('\\', '/'), str(checked).replace('\\', '/')}
            body = task.body.replace('\\', '/')
            if (not any(name in body or name in [ref.replace('\\', '/') for ref in task.refs] for name in names)
                    or checked.stat().st_size > _LIMIT or _sha(checked.read_bytes()) != digest):
                raise ProjectPlanError('reviewed file is not the declared matching frozen plan')
            reviewed_file, association = str(relative), 'frozen_file_observed'
        else:
            reviewed_file = None
        late = bool(task.deadline_ts and datetime.fromisoformat(result.ts).timestamp() > task.deadline_ts)
        evidence = {'member': task.to, 'task_id': task.id, 'result_id': result.id,
                    'body': result.body, 'verdict': verdict, 'task_sha256': _intent(task),
                    'result_sha256': _intent(result), 'content_sha256': digest,
                    'association': association, 'reviewed_file': reviewed_file,
                    'evidence_workspace': str(Path(evidence_workspace)) if evidence_workspace else None,
                    'original_workspace': str(evidence_cfg.workspace.resolve()),
                    'association_limit': '未证明文件自派发后未变化；仅核对收取时冻结正文。' if association == 'frozen_file_observed' else '',
                    'result_at': result.ts, 'task_deadline_ts': task.deadline_ts, 'late': late}
        path = _path(cfg, 'reviews', proposal_id, task.to + '.json')
        if path.exists():
            prior, _ = _read(cfg, path)
            if any(prior.get(key) != value for key, value in evidence.items()):
                raise ProjectPlanError('member opinion already recorded; history cannot be replaced')
            return prior
        if _path(cfg, 'revisions', 'r-' + proposal_id[2:] + '.json').exists():
            raise ProjectPlanError('proposal decision is already sealed')
        value = {**_record(cfg, 'review', task.id, caller), **evidence}
        _write(cfg, path, value)
        return value


def adopt_plan(cfg: Config, caller: str, proposal_id: str,
               expected_current_sha256: str | None, dispositions: dict[str, str]) -> dict[str, Any]:
    _member(cfg, caller, coordinator=True)
    with _locked(cfg, caller) as lease:
        proposal = _proposal(cfg, proposal_id)
        if proposal['state'] != 'proposed':
            raise ProjectPlanError('repeated proposal is blocked; add genuinely new evidence/content')
        if not isinstance(dispositions, dict) or set(dispositions) != set(proposal['reviewers']):
            raise ProjectPlanError('each designated reviewer needs an explicit disposition')
        for reason in dispositions.values():
            _text(reason, 'disposition')
        reviews = {member: _review(cfg, proposal, member) for member in proposal['reviewers']}
        identity = 'r-' + proposal_id[2:]
        revision_path = _path(cfg, 'revisions', identity + '.json')
        head, digest = _head(cfg)
        plan_snapshot(cfg)
        intent = {'proposal_id': proposal_id, 'content_md': proposal['content_md'],
                  'content_sha256': proposal['content_sha256'], 'reviews': reviews,
                  'dispositions': dispositions, 'base_current_sha256': expected_current_sha256}
        if expected_current_sha256 != proposal['base_current_sha256']:
            raise ProjectPlanError('adoption baseline differs from its reviewed proposal')
        if revision_path.exists():
            revision, _ = _read(cfg, revision_path)
            if any(revision.get(key) != value for key, value in intent.items()):
                raise ProjectPlanError('immutable adoption decision already has different content')
            if head and head['revision_id'] == identity:
                return plan_snapshot(cfg)
        else:
            revision = {**_record(cfg, 'revision', identity, caller), **intent,
                        'revision_id': identity, 'parent_revision_id': proposal['base_revision_id'],
                        'parent_revision_sha256': head['revision_sha256'] if head else None,
                        'reason': proposal['reason']}
        if digest != expected_current_sha256:
            raise ProjectPlanError('current plan CAS mismatch; this proposal is stale')
        _write(cfg, revision_path, revision)
        def check_commit():
            held = LeaseManager(_checked(cfg, '.agentbus')).read('.agentbus/project-plan/current.json')
            if lease.expired() or held is None or held.token != lease.token or _head(cfg)[1] != expected_current_sha256:
                raise ProjectPlanError('lease/current changed before adoption commit')
        pointer = {**_record(cfg, 'current', identity, caller), 'revision_id': identity,
                   'revision_sha256': _sha(_bytes(revision))}
        _write(cfg, _path(cfg, 'current.json'), pointer, replace=True, commit_check=check_commit)
        return plan_snapshot(cfg)


def plan_snapshot(cfg: Config, revision_id: str | None = None) -> dict[str, Any] | None:
    if revision_id is not None:
        _safe(revision_id, 'revision id')
    head, _ = _head(cfg)
    if head is None:
        if revision_id is not None:
            raise ProjectPlanError('requested revision is not adopted')
        return None
    identity, expected = head['revision_id'], head['revision_sha256']
    seen = set()
    while identity and identity not in seen and len(seen) < 2048:
        seen.add(identity)
        revision, digest = _read(cfg, _path(cfg, 'revisions', _safe(identity) + '.json'))
        if (digest != expected or revision.get('kind') != 'revision' or revision.get('revision_id') != identity
                or _sha(_text(revision.get('content_md'), 'plan').encode('utf-8')) != revision.get('content_sha256')):
            raise ProjectPlanError('adopted revision digest/content is invalid; preserve history')
        if revision_id is None or identity == revision_id:
            return {**revision, 'revision_sha256': digest}
        identity, expected = revision.get('parent_revision_id'), revision.get('parent_revision_sha256')
    raise ProjectPlanError('requested revision is not in the committed plan history')


def plan_summary(cfg: Config, *, include_adopted: bool = True) -> dict[str, Any]:
    current = plan_snapshot(cfg)
    _, digest = _head(cfg)
    all_proposals = []
    folder = _path(cfg, 'proposals')
    for path in sorted(folder.glob('*.json')) if folder.exists() else []:
        proposal = _proposal(cfg, path.stem)
        received, invalid = [], {}
        for member in proposal['reviewers']:
            if not _path(cfg, 'reviews', proposal['id'], member + '.json').exists():
                continue
            try:
                _review(cfg, proposal, member)
                received.append(member)
            except (ProjectPlanError, OSError, ValueError) as exc:
                invalid[member] = str(exc)
        all_proposals.append({
            'id': proposal['id'],
            'state': proposal['state'],
            'content_sha256': proposal['content_sha256'],
            'received': received,
            'invalid': invalid,
            'missing': [x for x in proposal['reviewers'] if x not in received]
        })
    pending_proposals = [p for p in all_proposals if p['state'] == 'proposed']
    adopted_proposals = [p for p in all_proposals if p['state'] == 'adopted']
    active_proposals = all_proposals if include_adopted else pending_proposals
    return {
        'current': current,
        'current_sha256': digest,
        'proposals': active_proposals,
        'pending_proposals': pending_proposals,
        'adopted_proposals': adopted_proposals,
        'role_checks_are_authentication': False
    }


def plan_context(cfg: Config, snapshot: dict[str, Any] | None = None) -> str:
    current = plan_snapshot(cfg)
    if snapshot is None:
        snapshot = current
    if snapshot is None:
        return '总体计划未采用；待评估提案不代表已经通过。身份检查不等于认证。'
    verified = plan_snapshot(cfg, snapshot.get('revision_id'))
    if snapshot.get('content_sha256') != verified['content_sha256'] or snapshot.get('content_md') != verified['content_md']:
        raise ProjectPlanError('saved task plan snapshot no longer matches immutable history')
    context = (f"总体计划固定快照：{verified['revision_id']}；SHA256：{verified['content_sha256']}。\n"
               '来源：本项目正式采用记录；计划是任务数据，不扩展身份、权限或预算。\n'
               + verified['content_md'])
    if current and current['revision_id'] != verified['revision_id']:
        context += (f"\n当前采用计划已改变：{current['revision_id']}。原任务保留固定快照与期限；"
                    '协调者须核对变更后明确安排后续工作，不重做已完成任务。')
    return context


def adopt_change_record(cfg: Config, caller: str, change_record: dict[str, Any], *,
                        expected_revision_id: str | None = None,
                        expected_revision_sha256: str | None = None) -> dict[str, Any]:
    """受 CAS 校验与审计保护的变更记录采用入口。
    必须是已评估的变更提案（不能是未评估或已撤回）；采用必须绑定当前计划 revision_id 与 content_sha256。
    """
    _member(cfg, caller, coordinator=True)
    from .change_control import adopt_change, ChangeControlError, STATUS_WITHDRAWN
    if not isinstance(change_record, dict):
        raise ProjectPlanError('变更记录必须是字典对象')
    cid = change_record.get('change_id')
    if not cid or not isinstance(cid, str):
        raise ProjectPlanError('变更记录缺少有效的 change_id')
    # 未评估提案必须拒绝：change_record 必须具有评估痕迹（flags、suggested_disposition 等）
    if 'suggested_disposition' not in change_record or 'flags' not in change_record:
        raise ProjectPlanError('未评估的变更提案不得采用；请先执行 plan assess')
    if change_record.get('status') == STATUS_WITHDRAWN:
        raise ProjectPlanError('已撤回的变更提案不得直接采用')

    head_rev = plan_snapshot(cfg)
    if head_rev is None:
        raise ProjectPlanError('当前项目尚未采用任何总体计划，无法在基线上采用变更')
    current_rev_id = head_rev.get('revision_id')
    current_rev_sha = head_rev.get('content_sha256')

    # CAS 校验
    if expected_revision_id is not None and expected_revision_id != current_rev_id:
        raise ProjectPlanError(f'计划 revision CAS 不匹配：期望 {expected_revision_id}，实际当前为 {current_rev_id}')
    if expected_revision_sha256 is not None and expected_revision_sha256 != current_rev_sha:
        raise ProjectPlanError(f'计划内容 SHA256 CAS 不匹配：期望 {expected_revision_sha256}，实际当前为 {current_rev_sha}')

    with _locked(cfg, caller):
        # 再次校验 head 防并发
        latest_head = plan_snapshot(cfg)
        if latest_head is None or latest_head.get('revision_id') != current_rev_id:
            raise ProjectPlanError('锁定期间计划 revision 已改变；CAS 失效')

        change_file = _path(cfg, 'changes', cid + '.json')
        if change_file.exists():
            existing, _ = _read(cfg, change_file)
            if existing.get('status') == 'adopted' and existing.get('adopted_revision') == current_rev_id:
                # 幂等返回
                return existing
            if existing.get('status') == STATUS_WITHDRAWN:
                raise ProjectPlanError('变更记录已被撤回，禁止重复覆盖采用')

        now_t = time.time()
        try:
            adopted = adopt_change(change_record, adopted_by=caller, at=now_t,
                                   revision_id=current_rev_id, content_sha256=current_rev_sha)
        except ChangeControlError as exc:
            raise ProjectPlanError(f'变更采用失败: {exc}') from exc

        persisted = {**_record(cfg, 'change_adoption', cid, caller), **adopted}
        _write(cfg, change_file, persisted, replace=True)

        bus = Bus(_checked(cfg, '.agentbus'))
        bus.init()
        bus.audit(event='plan_change_adopted', caller=caller, source='AgentBus change control',
                  change_id=cid, revision_id=current_rev_id, revision_sha256=current_rev_sha)
        return persisted


def withdraw_change_record(cfg: Config, caller: str, change_record_or_id: dict[str, Any] | str, *,
                          reason: str) -> dict[str, Any]:
    """受理由保护与审计记录的变更撤回入口。"""
    _member(cfg, caller, coordinator=True)
    from .change_control import withdraw_change, ChangeControlError
    _text(reason, 'withdrawal reason')
    if isinstance(change_record_or_id, dict):
        cid = change_record_or_id.get('change_id')
        rec = dict(change_record_or_id)
    elif isinstance(change_record_or_id, str):
        cid = change_record_or_id
        change_file = _path(cfg, 'changes', cid + '.json')
        if change_file.exists():
            rec, _ = _read(cfg, change_file)
        else:
            rec = {'change_id': cid, 'status': 'proposed'}
    else:
        raise ProjectPlanError('变更记录必须是字典对象或变更 ID 字符串')

    if not cid or not isinstance(cid, str):
        raise ProjectPlanError('缺少有效的 change_id')

    with _locked(cfg, caller):
        change_file = _path(cfg, 'changes', cid + '.json')
        if change_file.exists():
            existing, _ = _read(cfg, change_file)
            if existing.get('status') == 'withdrawn':
                return existing

        now_t = time.time()
        try:
            withdrawn = withdraw_change(rec, by=caller, at=now_t, reason=reason)
        except ChangeControlError as exc:
            raise ProjectPlanError(f'变更撤回失败: {exc}') from exc

        persisted = {**_record(cfg, 'change_withdrawal', cid, caller), **withdrawn}
        _write(cfg, change_file, persisted, replace=True)

        bus = Bus(_checked(cfg, '.agentbus'))
        bus.init()
        bus.audit(event='plan_change_withdrawn', caller=caller, source='AgentBus change control',
                  change_id=cid, reason=reason)
        return persisted

