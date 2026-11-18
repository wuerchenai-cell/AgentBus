"""Durable, model-free team jobs. Source: Codex jobs worker, 2026-10-02.

A job is a disk checkpoint over ordinary Bus goals/tasks/results. It neither
keeps a model alive nor claims that an empty inbox means successful work.
Per-job leases serialize cooperating calls; files are individually replaced
atomically. A multi-mailbox plan is recoverable, not a multi-file transaction.
Caller/leader checks express the selected role, not an authentication boundary:
any program with the same filesystem permissions can supply a caller string.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from .bus import Bus, BusError, Message, now_iso
from . import claim_fencing as _fencing
from . import heartbeat as _heartbeat
from .config import Config
from .gates import GateKeeper
from .lease import LeaseManager
from .project_plan import ProjectPlanError, plan_context, plan_snapshot
from .silent_debug import note_silent
from .workspace_files import WorkspaceFiles, WorkspaceFileError, MAX_ARTIFACT_ENTRIES

_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)


class JobError(ValueError):
    pass


def _safe(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise JobError(f"{label} must be 1-64 safe ASCII characters")
    return value


def _member(cfg: Config, caller: str, *, executor: bool = False) -> str:
    _safe(caller, 'caller')
    people = cfg.executor_workers() if executor else cfg.enabled_workers()
    if caller not in people:
        raise JobError(f"member {caller!r} is not selected for this group")
    return caller


def _root(cfg: Config) -> Path:
    return cfg.workspace / '.agentbus'


def same_project(recorded: Any, workspace: Path, job_id: str | None = None) -> bool:
    """Whether a job's recorded workspace refers to this project.

    A moved project retains its original config identity. Missing old paths
    alone do not establish ownership of an arbitrary copied job. A live old
    project with the same job still denotes a copy rather than a move.
    """
    if recorded is None:
        return True
    if not isinstance(recorded, str) or not recorded.strip():
        return False
    current = Path(workspace).resolve()
    try:
        old = Path(recorded)
        if old.resolve() == current or str(old) == str(current):
            return True
    except (OSError, ValueError):
        return False
    if os.name == 'nt' and recorded.casefold() == str(current).casefold():
        return True
    try:
        from .public_files import PublicFiles
        identity = (PublicFiles(current).read_json('.agentbus/config.json') or {}).get('workspace')
        if not isinstance(identity, str) or Path(identity).resolve() != old.resolve():
            return False
        # Validate the lexical and resolved directory boundaries before probing
        # a path from old metadata, including private or linked host folders.
        PublicFiles(old)
        if job_id is None:
            return not (old / '.agentbus').is_dir()
        return not (old / '.agentbus' / 'jobs' / f"{_safe(job_id, 'job id')}.json").is_file()
    except (OSError, ValueError):
        return False


def _path(cfg: Config, job_id: str) -> Path:
    return _root(cfg) / 'jobs' / f"{_safe(job_id, 'job id')}.json"


def _load(cfg: Config, job_id: str) -> dict[str, Any]:
    path = _path(cfg, job_id)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise JobError(f"cannot read job {job_id}: {exc}") from exc
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or data.get('id') != job_id):
        raise JobError(f"job {job_id} has an invalid record")
    return data


def _save(cfg: Config, data: dict[str, Any]) -> None:
    path = _path(cfg, data['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _locked(cfg: Config, job_id: str, caller: str, timeout_s: float = 15.0):
    manager = LeaseManager(_root(cfg))
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lease = manager.acquire(f'.agentbus/jobs/{job_id}.json', f'job:{caller}',
                                     ttl_s=60, note='serialize durable job updates')
        except PermissionError:
            # Windows can briefly deny a lease mutation during lock-file
            # initialization or concurrent readers. Retry only within this
            # fixed bound; persistent filesystem errors remain actionable.
            lease = None
        if lease is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise JobError(f"job {job_id} lease is busy/inaccessible; retry or check file permissions")
        time.sleep(min(0.05, remaining))
    try:
        yield
    finally:
        manager.release(lease)


def _bus(cfg: Config) -> Bus:
    bus = Bus(_root(cfg))
    bus.init()
    return bus


def _running_allowed(cfg: Config, data: dict[str, Any] | None = None) -> None:
    if (_root(cfg) / 'STOP').exists():
        raise JobError('STOP is active; no new job work may be published')
    if data and time.time() > float(data.get('deadline_ts', 0)):
        # A reader may hold an older snapshot while a writer has registered
        # another task. Never save that snapshot or silently grant a new budget.
        raise JobError('job wall-clock budget deadline expired; preserve the original record and obtain an explicitly authorized continuation')


def _publish(bus: Bus, data: dict[str, Any]) -> None:
    """Reuse only the exact previously allocated message after a crash."""
    message = Message.from_dict(data, strict=True)
    existing = bus.find_message(message.id)
    if existing:
        if not _task_intent_matches(existing, data, bus.events()):
            raise JobError(f"message id {message.id} already exists with different intent")
        return
    # Bus.post normally removes its temporary file. A process can die before
    # replace; only this job's exact preallocated payload may be re-published.
    temporary = bus.dirs(message.to)['tmp'] / f'{message.id}.json'
    if temporary.exists():
        try:
            unfinished = bus.read(temporary)
        except (OSError, ValueError, BusError) as exc:
            raise JobError(f"unfinished message {message.id} needs manual inspection") from exc
        if unfinished.to_dict() != message.to_dict():
            raise JobError(f"unfinished message {message.id} conflicts with this job")
        temporary.unlink()
    bus.post(message)


def _same_intent(message: Message, data: dict[str, Any]) -> bool:
    required = ('id', 'sender', 'to', 'type', 'body', 'title', 'reply_to', 'result_to')
    optional = ('job_id', 'plan_id', 'plan_sha256', 'expected_artifacts', 'refs')
    return all(getattr(message, key) == data.get(key) for key in required) and all(
        getattr(message, key) == data[key] for key in optional if key in data)


def _routing_intent_sha256(data: dict[str, Any]) -> str:
    """Digest immutable intent plus bounds, excluding claim/result lifecycle.

    Source: Codex, 2026-10-18, real autonomous-pull delivery regression.
    This is local audit evidence, not authentication against filesystem owners.
    """
    keys = ('id', 'sender', 'to', 'type', 'body', 'title', 'reply_to', 'result_to',
            'job_id', 'plan_id', 'plan_sha256', 'expected_artifacts',
            'deadline_ts', 'budget_s', 'attempt', 'hop')
    value = {key: data.get(key) for key in keys}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _task_intent_matches(message: Message, data: dict[str, Any],
                         events: list[dict[str, Any]]) -> bool:
    """Recognize one audited first-claim route without weakening _same_intent.

    Legacy v1 audit rows contain only task_id/to_agent, so they additionally
    require the matching first fenced claim fingerprint. Missing allocation,
    changed body/plan/artifacts, duplicate routes and later epochs stay blocked.
    """
    if _same_intent(message, data):
        return True
    if (message.type != 'task' or data.get('type') != 'task'
            or message.to == data.get('to') or data.get('status', 'pending') != 'pending'
            or data.get('claim_epoch', 0) or data.get('claimed_by')
            or message.attempt != data.get('attempt', 0)
            or not _same_intent(message, {**data, 'to': message.to})):
        return False
    routes = [(index, event) for index, event in enumerate(events)
              if event.get('event') == 'task_transferred_on_claim'
              and event.get('task_id') == message.id]
    if len(routes) != 1:
        return False
    position, route = routes[0]
    if route.get('to_agent') != message.to:
        return False
    v2 = 'source' in route or 'before_intent_sha256' in route or 'after_intent_sha256' in route
    if v2:
        if (route.get('source') != 'task_forest'
                or route.get('from_agent') != data.get('to')
                or route.get('job_id') != message.job_id
                or route.get('claim_epoch_before') != 0
                or route.get('before_intent_sha256') != _routing_intent_sha256(data)
                or route.get('after_intent_sha256') != _routing_intent_sha256(message.to_dict())
                or route.get('deadline_ts') != message.deadline_ts):
            return False
        if _fencing.record_epoch(message) == 0:
            return not message.claimed_by and message.status == 'pending'
    if (_fencing.record_epoch(message) != 1 or not _fencing.is_fenced(message)
            or message.claimed_by != message.to or message.claim_agent != message.to):
        return False
    claims = [(index, event) for index, event in enumerate(events)
              if event.get('event') == 'claim' and event.get('id') == message.id]
    return (len(claims) == 1 and claims[0][0] > position
            and claims[0][1].get('agent') == message.to
            and claims[0][1].get('claim_epoch') == 1
            and claims[0][1].get('claim_fingerprint') == _fencing.fingerprint(message.claim_id))


def job_message_context(cfg: Config, data: dict[str, Any]) -> str:
    """Source: Codex internal helper, 2026-10-04. Never bind old work to a new plan."""
    context = '原始作业目标：\n' + data['goal']
    snapshot = data.get('plan_snapshot')
    if snapshot is not None:
        try:
            context += '\n\n' + plan_context(cfg, snapshot)
        except ProjectPlanError as exc:
            raise JobError(f'bound project plan cannot be verified: {exc}') from exc
    else:
        context += '\n\n本作业发起时未绑定已采用的总体计划；不追溯套用后来采用的计划。'
    return context


def job_binding(data: dict[str, Any]) -> dict[str, Any]:
    # Missing key is an old job. It must remain unbound on retry/dispatch.
    if 'plan_snapshot' not in data:
        return {}
    snapshot = data.get('plan_snapshot')
    return {'job_id': data['id'],
            'plan_id': snapshot['revision_id'] if snapshot else None,
            'plan_sha256': snapshot['content_sha256'] if snapshot else None}


def message_job(cfg: Config, message: Message) -> dict[str, Any] | None:
    if not message.job_id:
        return None
    data = _load(cfg, message.job_id)
    if not same_project(data.get('workspace'), cfg.workspace, data.get('id')):
        raise JobError('message job belongs to another workspace (该作业属于另一个仍存在的项目副本)')
    expected = job_binding(data)
    if not expected or any(getattr(message, key) != value for key, value in expected.items()):
        raise JobError('message binding differs from its immutable job plan')
    return data


def _expected_artifacts(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ARTIFACT_ENTRIES:
        raise JobError('expected_artifacts must be a bounded list of relative paths')
    try:
        paths = [WorkspaceFiles._relative(path) for path in value]
    except WorkspaceFileError as exc:
        raise JobError(f'invalid expected_artifacts: {exc}') from exc
    keys = [os.path.normcase(path) for path in paths]
    if len(set(keys)) != len(keys):
        raise JobError('expected_artifacts cannot contain duplicate paths')
    return paths


def _artifact_verdict(cfg: Config, item: dict[str, Any], result: Message, *, versions=None):
    """Separate current bytes, authorized historical revisions and no outputs."""
    from .artifact_versions import ArtifactVersions
    expected = item.get('expected_artifacts', [])
    manifest = result.artifacts
    if not isinstance(manifest, list):
        return f"task {item.get('id', '')} artifact manifest must be a list", 'mismatch', []
    if not isinstance(expected, list):
        return f"task {item.get('id', '')} expected_artifacts must be a list", 'mismatch', []
    if expected and not manifest:
        return f"task {item.get('id', '')} artifact manifest omits declared paths", 'mismatch', []
    if not expected and not manifest:
        if expected != [] or manifest != []:
            return f"task {item.get('id', '')} invalid empty artifact state", 'mismatch', []
        return '', 'not_required', []
    try:
        # Source: Codex dual-directory verification, 2026-10-07. 产物清单里的
        # path 是业务产出目录的相对路径，不是控制目录的。旧代码用
        # cfg.workspace 校验，双目录项目收尾时必然 NOT_FOUND。
        files = WorkspaceFiles(cfg.output_root)
        verdict = (versions or ArtifactVersions(cfg)).verify(result)
        if verdict['verdict'] != 'match':
            return f"task {item['id']} artifact manifest mismatch: {verdict['mismatches']}", 'mismatch', []
        present = {os.path.normcase(files._path(entry['path'])[0]) for entry in manifest}
        required = {os.path.normcase(files._path(path)[0]) for path in expected}
        if not required.issubset(present):
            return f"task {item['id']} artifact manifest omits declared paths", 'mismatch', []
    except (WorkspaceFileError, KeyError, TypeError, ValueError, OSError) as exc:
        return f"task {item['id']} artifact verification failed: {exc}", 'mismatch', []
    return '', verdict['state'], verdict.get('replacements', [])


def _artifact_error(cfg: Config, item: dict[str, Any], result: Message, *, versions=None) -> str:
    return _artifact_verdict(cfg, item, result, versions=versions)[0]


def _parent_snapshot(cfg: Config, parent_id: str) -> dict[str, Any]:
    """Observe the parent under its job lease; never alter its work or budget."""
    try:
        return _inspect_parent(cfg, parent_id)
    except (KeyError, TypeError, AttributeError) as exc:
        raise JobError('parent job evidence is malformed; preserve it for inspection') from exc


def _inspect_parent(cfg: Config, parent_id: str) -> dict[str, Any]:
    path = _path(cfg, parent_id)
    root = cfg.workspace.resolve()
    if (not path.resolve().is_relative_to(root)
            or any(part.casefold() == '.workbuddy' for part in path.resolve().parts)):
        raise JobError('parent job must belong to this current project')
    parent = _load(cfg, parent_id)
    if parent.get('workspace') and not same_project(parent['workspace'], root, parent.get('id')):
        raise JobError('parent job record belongs to another project')
    bus = Bus(_root(cfg))
    related = {parent['goal_id'], *(task['id'] for task in parent['tasks'])}
    # status_job prefers committed terminal copies. A crash-left processing
    # duplicate still has a real owner, so check these paths independently.
    for agent in bus.agents_dir.iterdir() if bus.agents_dir.exists() else []:
        if not agent.resolve().is_relative_to(root):
            raise JobError('parent mailbox points outside this current project')
        for directory in ('ready', 'processing', 'blocked', 'done'):
            folder = agent / directory
            if not folder.resolve().is_relative_to(root):
                raise JobError('parent mailbox points outside this current project')
            for message_file in folder.glob('*.json'):
                if (not message_file.resolve().is_relative_to(root)
                        or any(part.casefold() == '.workbuddy'
                               for part in message_file.resolve().parts)):
                    raise JobError('parent mailbox file points outside this current project')
        for identity in related:
            processing = agent / 'processing' / f'{identity}.json'
            if processing.exists():
                raise JobError('parent work is processing; its owning conversation must close it first')
    state = status_job(cfg, parent_id)
    if state['pending_reviews']:
        raise JobError('parent peer reviews are unfinished; close them before continuation')
    if state['state'] not in ('completed', 'blocked', 'stopped'):
        raise JobError(f"parent job is still active: {state['state']}")
    goal, location = _message(bus, parent['goal_id'])
    if goal is None or not _same_intent(goal, parent['goal_message']):
        raise JobError('parent formal goal is missing or changed; inspect its evidence first')
    return {'id': parent_id, 'goal': parent['goal'], 'state': state['state'],
            'reason': state['reason'],
            'done_task_ids': [task['id'] for task in state['tasks']
                              if task['location'] == 'done' and task['status'] == 'done'],
            'task_states': [{**{key: task[key] for key in ('id', 'worker', 'status', 'location')},
                             'body_sha256': hashlib.sha256(task['body'].strip().encode('utf-8')).hexdigest()}
                            for task in state['tasks']],
            'latest_checkpoint': parent['checkpoints'][-1] if parent['checkpoints'] else None,
            'final_summary': (parent.get('finalization') or {}).get('summary', '')}


def _validate_retry_record(cfg: Config, data: dict[str, Any], parent_id: str | None) -> None:
    """A preallocated retry must remain local and readable before publication."""
    workspace = data.get('workspace')
    if workspace is not None and not same_project(workspace, cfg.workspace, data.get('id')):
        raise JobError('existing job record belongs to another project; inspect it before retry')
    error = 'existing job record is incomplete or malformed; preserve it and inspect it before retry'
    if parent_id is not None and not isinstance(workspace, str):
        raise JobError(error)
    strings = ('caller', 'goal', 'coordinator', 'goal_id')
    deadline = data.get('deadline_ts')
    if (any(not isinstance(data.get(key), str) or not data[key].strip() for key in strings)
            or type(deadline) not in (int, float) or not math.isfinite(deadline)
            or not isinstance(data.get('goal_message'), dict)
            or not isinstance(data.get('tasks'), list)
            or not isinstance(data.get('checkpoints'), list)
            or (data.get('finalization') is not None and not isinstance(data['finalization'], dict))
            or (data.get('plan') is not None and not isinstance(data['plan'], list))
            or (parent_id is not None and not isinstance(data.get('parent_snapshot'), dict))):
        raise JobError(error)
    try:
        goal = Message.from_dict(data['goal_message'], strict=True)
        goal.validate()
        _safe(data['goal_id'], 'goal message id')
        if goal.id != data['goal_id']:
            raise JobError(error)
        for task in data['tasks']:
            if (not isinstance(task, dict)
                    or any(not isinstance(task.get(key), str)
                           for key in ('id', 'worker', 'title', 'body', 'result_to'))
                    or not isinstance(task.get('message'), dict)):
                raise JobError(error)
            _safe(task['id'], 'task id')
    except (BusError, JobError, TypeError, ValueError, AttributeError) as exc:
        raise JobError(error) from exc


def _completed_parent_tasks(data: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = data.get('parent_snapshot')
    if snapshot is None:
        return []
    if not isinstance(snapshot, dict):
        raise JobError('parent snapshot is malformed; preserve it for inspection')
    tasks = snapshot.get('task_states', [])
    if (not isinstance(tasks, list) or any(not isinstance(item, dict)
            or any(not isinstance(item.get(key), str) for key in
                   ('id', 'status', 'location', 'body_sha256'))
            or re.fullmatch(r'[0-9a-f]{64}', item['body_sha256']) is None for item in tasks)):
        raise JobError('parent task states are malformed; preserve them for inspection')
    return [item for item in tasks if item['status'] == 'done' and item['location'] == 'done']


def start_job(cfg: Config, caller: str, goal: str,
              request_id: str | None = None,
              parent_job_id: str | None = None) -> dict[str, Any]:
    """Publish one formal goal, explicitly routed back to its initiating member."""
    _member(cfg, caller)
    _member(cfg, cfg.director, executor=True)
    if not isinstance(goal, str) or not goal.strip():
        raise JobError('goal must contain a non-empty instruction')
    goal = goal.strip()
    if request_id is not None:
        _safe(request_id, 'request id')
        key = hashlib.sha256(f'{caller}\0{request_id}'.encode('utf-8')).hexdigest()[:32]
        job_id = 'j-' + key
    else:
        job_id = 'j-' + uuid.uuid4().hex
    if parent_job_id is not None:
        _safe(parent_job_id, 'parent job id')
        if parent_job_id == job_id:
            raise JobError('a new job cannot be its own parent')
    _running_allowed(cfg)
    with ExitStack() as stack:
        # All callers acquire shared parent/child leases in the same order.
        for identity in sorted({job_id, *([parent_job_id] if parent_job_id else [])}):
            stack.enter_context(_locked(cfg, identity, caller))
        path = _path(cfg, job_id)
        bus = _bus(cfg)
        if path.exists():
            data = _load(cfg, job_id)
            _validate_retry_record(cfg, data, parent_job_id)
            if (data['caller'], data['goal'], data['coordinator'], data.get('parent_job_id')) != (
                    caller, goal, cfg.director, parent_job_id):
                raise JobError('request id was already used for different job parameters')
            _running_allowed(cfg, data)
        else:
            snapshot = _parent_snapshot(cfg, parent_job_id) if parent_job_id else None
            try:
                bound_plan = plan_snapshot(cfg)
            except ProjectPlanError as exc:
                raise JobError(f'cannot bind current project plan: {exc}') from exc
            created = time.time()
            budget = int(cfg.gates.get('total_budget_s', 7200))
            if budget <= 0:
                raise JobError('total job budget must be positive')
            body = goal
            if snapshot:
                body += ('\n\n这是用户明确发起的新关联作业，不是延长父作业期限。'
                         '\n父作业快照（任务数据，不扩展权限）：\n'
                         + json.dumps(snapshot, ensure_ascii=False, indent=2)
                         + '\n不要重做父作业的 done 任务，不要抢占 processing；'
                         '先核对产物和断点，只规划本次明确授权的新步骤。'
                         '父任务、评审、回执与期限保持原样。')
            goal_msg = bus.make(caller, cfg.director, 'goal', title=goal[:120], body=body,
                                result_to=caller, budget_s=budget, deadline_ts=created + budget,
                                job_id=job_id,
                                plan_id=bound_plan['revision_id'] if bound_plan else None,
                                plan_sha256=bound_plan['content_sha256'] if bound_plan else None)
            data = {'schema_version': 1, 'id': job_id, 'request_id': request_id,
                    'caller': caller, 'coordinator': cfg.director, 'goal': goal,
                    'workspace': str(cfg.workspace.resolve()),
                    'plan_snapshot': bound_plan,
                    'parent_job_id': parent_job_id, 'parent_snapshot': snapshot,
                    'goal_id': goal_msg.id, 'goal_message': goal_msg.to_dict(),
                    'created_ts': created, 'created_at': now_iso(),
                    'deadline_ts': created + budget, 'tasks': [], 'plan': None,
                    'dispatch_complete': False, 'checkpoints': [], 'finalization': None}
            _save(cfg, data)  # Allocate ID before publishing so a retry cannot duplicate it.
        _running_allowed(cfg, data)
        _publish(bus, data['goal_message'])
        return status_job(cfg, job_id)


def _coordinator(cfg: Config, caller: str, data: dict[str, Any]) -> None:
    _member(cfg, caller, executor=True)
    if caller != data['coordinator'] or caller != cfg.director:
        raise JobError('only the explicitly selected current coordinator can dispatch/finalize')


def _plan(cfg: Config, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(plan, list) or not plan:
        raise JobError('plan must contain at least one bounded task')
    decision = GateKeeper(cfg.gates).check_task_count(len(plan))
    if not decision:
        raise JobError(decision.reason)
    normalized = []
    for item in plan:
        if not isinstance(item, dict):
            raise JobError('every planned task must be an object')
        worker = item.get('worker')
        _member(cfg, worker, executor=True)
        result_to = item.get('result_to')
        if result_to != 'human':
            _member(cfg, result_to)
        body = item.get('body')
        title = item.get('title', '')
        if not isinstance(body, str) or not body.strip():
            raise JobError('every planned task must have a non-empty body')
        if not isinstance(title, str):
            raise JobError('task title must be text')
        normalized_item = {'worker': worker, 'title': title[:120],
                           'body': body.strip(), 'result_to': result_to}
        if 'expected_artifacts' in item:
            normalized_item['expected_artifacts'] = _expected_artifacts(item['expected_artifacts'])
        normalized.append(normalized_item)
    return normalized


def dispatch_job(cfg: Config, caller: str, job_id: str,
                 plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the entire plan, then resume publication using its recorded IDs."""
    with _locked(cfg, _safe(job_id, 'job id'), caller):
        data = _load(cfg, job_id)
        _coordinator(cfg, caller, data)
        _running_allowed(cfg, data)
        normalized = _plan(cfg, plan)
        # Source: WorkBuddy peer review, 2026-10-03. Catch exact mechanical
        # re-dispatch of completed parent work; semantic overlap still needs
        # the coordinator and peer review. A deliberate new verification
        # must describe its new scope/acceptance instead of copying old work.
        completed = _completed_parent_tasks(data)
        for item in normalized:
            digest = hashlib.sha256(item['body'].strip().encode('utf-8')).hexdigest()
            repeated = [old['id'] for old in completed if old.get('body_sha256') == digest]
            if repeated:
                raise JobError('completed parent task has the same body: ' + ', '.join(repeated)
                               + '; describe the explicitly authorized new verification scope '
                                 'and acceptance instead of copying finished work')
        if data.get('finalization'):
            raise JobError('this job already has a finalization record')
        bus = _bus(cfg)
        if data['plan'] is not None:
            if data['plan'] != normalized:
                raise JobError('job already has a different plan; do not silently replace work')
        else:
            prepared = []
            timeout = int(cfg.gates.get('task_timeout_s', 1800))
            if timeout <= 0:
                raise JobError('task timeout must be positive')
            for item in normalized:
                _running_allowed(cfg, data)
                body = item['body']
                if 'plan_snapshot' in data:
                    body = job_message_context(cfg, data) + '\n\n本次分工：\n' + body
                msg = bus.make(caller, item['worker'], 'task', title=item['title'],
                               body=body, result_to=item['result_to'],
                               reply_to=data['goal_id'], budget_s=timeout,
                               deadline_ts=min(time.time() + timeout, data['deadline_ts']),
                               expected_artifacts=item.get('expected_artifacts', []), **job_binding(data))
                decision = GateKeeper(cfg.gates).check_dispatch(msg, task_count=len(normalized),
                                                               include_total_budget=False)
                if not decision:
                    raise JobError(decision.reason)
                prepared.append({'id': msg.id, **item, 'message': msg.to_dict()})
            data['plan'], data['tasks'] = normalized, prepared
            _save(cfg, data)  # All IDs are durable before the first mailbox changes.
        for item in data['tasks']:
            _running_allowed(cfg, data)
            message = Message.from_dict(item['message'], strict=True)
            decision = GateKeeper(cfg.gates).check_dispatch(message, task_count=len(normalized),
                                                           include_total_budget=False)
            if not decision:
                raise JobError(decision.reason)
            _publish(bus, item['message'])
            try:
                from .trigger.service import TriggerDeliveryService
                TriggerDeliveryService(cfg.workspace).trigger_for_member(message.to, message.id)
            except Exception:
                pass  # 容错通知类：触发工位唤醒异常不阻断作业推进
        data['dispatch_complete'] = True
        _save(cfg, data)
        bus.audit(event='job_dispatched', job_id=job_id, caller=caller,
                  goal_id=data['goal_id'], task_ids=[x['id'] for x in data['tasks']])
        return status_job(cfg, job_id)


def _message(bus: Bus, message_id: str) -> tuple[Message | None, str]:
    """Prefer committed terminal state over a crash-left active duplicate."""
    if not isinstance(message_id, str) or not _SAFE.fullmatch(message_id):
        return None, 'unreadable'
    for sub in ('done', 'processing', 'blocked', 'ready'):
        for agent in sorted(bus.agents_dir.iterdir() if bus.agents_dir.exists() else []):
            path = agent / sub / f'{message_id}.json'
            if path.is_file():
                try:
                    return bus.read(path), sub
                except (OSError, ValueError, BusError):
                    return None, 'unreadable'
    return None, 'missing'


def _receipts(bus: Bus, msg: Message) -> tuple[list[Message], str]:
    results = bus.find_results(msg.id)
    if len(results) != 1:
        return results, f"task {msg.id} needs exactly one formal result (found {len(results)})"
    result = results[0]
    if result.sender != msg.to or result.to != msg.result_to:
        return results, f"task {msg.id} result has wrong sender/route"
    error = receipt_binding_error(msg, result)
    if error:
        return results, error
    if result.exit_code not in (None, 0) or result.status in ('failed', 'blocked'):
        return results, f"task {msg.id} result reports failure: {result.body}"
    return results, ''


def receipt_binding_error(source: Message, result: Message) -> str:
    if result.sender != source.to or result.to != source.result_to:
        return f'task {source.id} result has wrong sender/route'
    strict_binding = bool(source.plan_id or source.expected_artifacts)
    for key in ('job_id', 'plan_id', 'plan_sha256'):
        value = getattr(result, key)
        if (strict_binding or value is not None) and value != getattr(source, key):
            return f'task {source.id} result has different {key} binding'
    # Claim fencing. Source: OpenCode, 2026-10-08, task m-1791457584499-8893c2.
    # A result produced under an older claim must never complete the task, open
    # a review, or let the job continue. Only a result carrying the *current*
    # ticket counts. Legacy unfenced tasks are untouched.
    if _fencing.record_epoch(source) > 0:
        if not _fencing.is_fenced(source):
            return f'task {source.id} claim was revoked or is invalid; no result can advance it'
        epoch = getattr(result, 'source_claim_epoch', 0)
        claim = getattr(result, 'source_claim_id', '') or ''
        if not claim:
            return f'task {source.id} result carries no source claim ticket'
        current_epoch = _fencing.record_epoch(source)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            return f'task {source.id} result has an invalid source claim epoch'
        if epoch != current_epoch:
            return (f'task {source.id} result carries claim epoch {epoch} '
                    f'but the task is at {current_epoch}')
        if claim != source.claim_id:
            return (f'task {source.id} result carries claim '
                    f'{_fencing.fingerprint(claim)} but the task holds '
                    f'{_fencing.fingerprint(source.claim_id)}')
        expected_cv = (source.claim_conversation_id
                       or _fencing.conversation_id_for_claim(source.claim_id))
        actual_cv = getattr(result, 'source_conversation_id', '') or ''
        if actual_cv and actual_cv != expected_cv:
            return f'task {source.id} result has a different conversation binding'
    return ''


def _reviews(bus: Bus, related: set[str], audit_events: list[dict] | None = None) -> list[str]:
    pending = []
    # The audit's existing post records identify an associated review even if
    # its JSON was damaged/lost. Treat it as unfinished, never as acceptance.
    for event in bus.events() if audit_events is None else audit_events:
        if (event.get('event') == 'post' and event.get('type') == 'review'
                and event.get('reply_to') in related and event.get('id')):
            review, location = _message(bus, str(event['id']))
            if _review_unfinished(bus, review, location):
                pending.append(str(event['id']))
    for agent in sorted(bus.agents_dir.iterdir() if bus.agents_dir.exists() else []):
        for sub in ('ready', 'processing', 'blocked', 'done'):
            for path in sorted((agent / sub).glob('*.json')):
                try:
                    msg = bus.read(path)
                except (OSError, ValueError, BusError):
                    continue
                if (msg.type == 'review' and (msg.reply_to in related or related.intersection(msg.refs))
                        and _review_unfinished(bus, msg, sub)):
                    pending.append(msg.id)
    return sorted(set(pending))


def _review_unfinished(bus: Bus, review: Message | None, location: str) -> bool:
    if review is None or location != 'done' or review.status != 'done':
        return True
    _, error = _receipts(bus, review)
    return bool(error)


def _bound_handoff_successor(cfg: Config, bus: Bus, source: Message,
                             job: dict[str, Any], events: list[dict[str, Any]]) -> str | None:
    """Recognize a retired assignment only from formal evidence and a successor.

    A tree display state cannot discharge a job obligation. Both immutable task
    allocations and their original goal/plan/deadline boundaries remain binding.
    Source: Owner task tree handoff decision, Codex integration 2026-10-08.
    """
    if source.status != 'blocked' or source.claim_id or source.claimed_by:
        return None
    source_hash = hashlib.sha256(json.dumps(source.to_dict(), ensure_ascii=False,
                                           sort_keys=True).encode('utf-8')).hexdigest()
    if not any(event.get('event') == 'relinquished_claim'
               and event.get('agent') == source.to and event.get('id') == source.id
               and event.get('after_sha256') == source_hash for event in events):
        return None
    allocations = {item['id']: item for item in job['tasks']}
    for event in reversed(events):
        if (event.get('event') != 'task_tree_successor_bound'
                or event.get('source') != 'task_forest' or not event.get('leaf_id')
                or event.get('job_id') != job['id'] or event.get('old_task_id') != source.id
                or event.get('reason') != source.result):
            continue
        task_id = event.get('task_id')
        item = allocations.get(task_id)
        if not item or task_id == source.id:
            continue
        successor, location = _message(bus, task_id)
        if successor is None or location not in ('ready', 'processing', 'blocked', 'done'):
            continue
        if not _same_intent(successor, item.get('message', {})):
            continue
        if (event.get('successor') != successor.to
                or successor.type != source.type or successor.reply_to != source.id
                or source.id not in successor.refs or successor.job_id != job['id']
                or source.job_id != job['id'] or successor.plan_id != source.plan_id
                or successor.plan_sha256 != source.plan_sha256
                or successor.body != source.body or successor.expected_artifacts != source.expected_artifacts
                or successor.result_to != source.result_to or successor.hop != source.hop
                or type(successor.attempt) is not int or successor.attempt != source.attempt + 1
                or successor.attempt > cfg.gates['max_retries']
                or successor.deadline_ts <= 0
                or successor.deadline_ts > min(source.deadline_ts, job['deadline_ts'])
                or successor.budget_s > source.budget_s):
            continue
        return task_id
    return None


def status_job(cfg: Config, job_id: str) -> dict[str, Any]:
    """Observe durable work, receipts and reviews without executing or claiming it."""
    data = _load(cfg, job_id)
    bus = Bus(_root(cfg))
    tasks = []
    errors, active = [], []
    bound = data.get('plan_snapshot')
    current = None
    if 'plan_snapshot' in data:
        try:
            current = plan_snapshot(cfg)
            if bound is not None:
                plan_context(cfg, bound)
        except ProjectPlanError as exc:
            errors.append(f'bound/current project plan cannot be verified: {exc}')
    artifact_required = any(item.get('expected_artifacts') for item in data['tasks'])
    artifacts_verified = artifact_required
    audit_events = bus.events()
    if any(event.get('event') == 'audit_invalid_line'
           and event.get('generated_by') == 'agentbus.events' for event in audit_events):
        errors.append('bus audit has invalid lines; review completeness cannot be established; '
                      'verify the preserved audit evidence before acceptance')
    goal, goal_location = _message(bus, data['goal_id'])
    goal_age_s = 0.0
    goal_heartbeat_source = 'none'
    goal_stale = False
    if goal and goal_location == 'processing':
        snapshot = _heartbeat.state(bus.root, goal.id, goal.to, task_record=goal.to_dict())
        if snapshot['source'] != 'none':
            goal_age_s = snapshot['age_s'] or 0.0
            goal_heartbeat_source = snapshot['source']
            goal_stale = _heartbeat.is_stale(snapshot, cfg.gates.get('heartbeat_timeout_s', 600))
        elif _fencing.record_epoch(goal) > 0:
            goal_age_s = 0.0
            goal_heartbeat_source = 'none'
            goal_stale = True
        else:
            try:
                goal_age_s = max(0.0, time.time() - (
                    bus.agents_dir / goal.to / 'processing' / f'{goal.id}.json').stat().st_mtime)
            except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError):
                # 功能类：取 goal 文件 mtime 失败（文件被删/权限）时 age 保持 0，
                # 后续据 age 判 stale；此处留痕以区分"真新鲜"与"读不到"。
                note_silent("jobs.py:682", "functional",
                            "goal 文件 mtime 读取失败，age 归零但需与真新鲜区分")
                pass
            goal_stale = goal_age_s > int(cfg.gates.get('heartbeat_timeout_s', 600))
            goal_heartbeat_source = 'mtime_only'
    goal_claim_complete = bool(goal and goal_location == 'processing'
                               and goal.status in ('claimed', 'running')
                               and isinstance(goal.claimed_by, str) and goal.claimed_by.strip()
                               and isinstance(goal.claimed_at, str) and goal.claimed_at.strip())
    if goal and not _same_intent(goal, data['goal_message']):
        errors.append(f"goal {goal.id} has different formal intent from its job record")
    if goal and goal_location == 'processing' and not goal_claim_complete:
        errors.append(f"goal {goal.id} processing has an incomplete claim; owner confirmation is required; "
                      "do not automatically restart it")
    if goal_stale:
        # A model/tool call may be alive without updating this file. The age
        # is observational evidence, never permission to reclaim its work.
        errors.append(f"goal {data['goal_id']} processing file has not been updated within the heartbeat "
                      "limit; this does not prove the process is dead; manual owner check is required; "
                      "do not automatically restart it")
    if (goal and goal_location in ('ready', 'processing')
            and goal.deadline_ts and time.time() > goal.deadline_ts):
        errors.append(f"goal {goal.id} deadline expired; owner confirmation is required; "
                      "do not automatically restart it")
    for item in data['tasks']:
        msg, location = _message(bus, item['id'])
        successor_id = (_bound_handoff_successor(cfg, bus, msg, data, audit_events)
                        if msg is not None else None)
        age_s = 0.0
        heartbeat_source = 'none'
        stale = False
        if msg and location == 'processing':
            snapshot = _heartbeat.state(bus.root, msg.id, msg.to, task_record=msg.to_dict())
            if snapshot['source'] != 'none':
                age_s = snapshot['age_s'] or 0.0
                heartbeat_source = snapshot['source']
                stale = _heartbeat.is_stale(snapshot, cfg.gates.get('heartbeat_timeout_s', 600))
            elif _fencing.is_fenced(msg):
                age_s = 0.0
                heartbeat_source = 'none'
                stale = True
            else:
                try:
                    age_s = max(0.0, time.time() - (
                        bus.agents_dir / msg.to / location / f'{msg.id}.json').stat().st_mtime)
                except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError):
                    # 功能类：同 goal，mtime 读不到时 age 保持 0（相当于"看起来新鲜"），
                    # 是最危险的静默点之一，必须留痕。
                    note_silent("jobs.py:726", "functional",
                                "任务文件 mtime 读取失败，age 归零可能误判为新鲜")
                    pass
                stale = age_s > int(cfg.gates.get('heartbeat_timeout_s', 600))
                heartbeat_source = 'mtime_only'
        task = {key: item[key] for key in ('id', 'worker', 'title', 'body', 'result_to')}
        task.update(status=msg.status if msg else 'missing', location=location,
                    result=msg.result if msg else '', exit_code=msg.exit_code if msg else None,
                    claimed_by=msg.claimed_by if msg else '', claimed_at=msg.claimed_at if msg else '',
                    heartbeat_stale=stale, heartbeat_age_s=round(age_s, 2),
                    heartbeat_source=heartbeat_source)
        if successor_id:
            task.update(status='transferred', successor_task_id=successor_id)
        if 'expected_artifacts' in item:
            task['expected_artifacts'] = item['expected_artifacts']
        tasks.append(task)
        if msg is None:
            errors.append(f"task {item['id']} is {location}; retry the same plan to recover publication")
        elif not _task_intent_matches(msg, item['message'], audit_events):
            errors.append(f"task {msg.id} has different formal intent from its allocated plan")
        elif successor_id:
            pass  # The successor's own task row retains the full obligation.
        elif msg.status in ('failed', 'blocked', 'rejected'):
            errors.append(f"task {msg.id} {msg.status}: {msg.result or msg.body}")
        elif location == 'done' and msg.status == 'done':
            receipts, error = _receipts(bus, msg)
            if not error:
                error = _artifact_error(cfg, item, receipts[0])
            if error:
                errors.append(error)
                if item.get('expected_artifacts'):
                    artifacts_verified = False
        elif msg.to not in cfg.executor_workers():
            errors.append(f"task {msg.id} member {msg.to} is no longer selected for execution")
        elif stale:
            errors.append(f"task {msg.id} processing heartbeat is stale; manual owner check before recovery")
        elif msg.deadline_ts and time.time() > msg.deadline_ts:
            errors.append(f"task {msg.id} deadline expired; do not automatically restart it")
        else:
            active.append((msg, location))
        if item.get('expected_artifacts') and not successor_id and not (msg and location == 'done' and msg.status == 'done'):
            artifacts_verified = False
    pending_reviews = _reviews(bus, {data['goal_id'], *(x['id'] for x in tasks)}, audit_events)
    finalization = data.get('finalization')
    verified = bool(finalization and finalization.get('committed'))
    if verified and goal:
        _, error = _receipts(bus, goal)
        if error:
            errors.append(error)
    state, reason = 'queued', ''
    if verified and goal and goal_location == 'done' and goal.status == 'done' and not (
            errors or active or pending_reviews):
        state = 'completed'
    elif (_root(cfg) / 'STOP').exists():
        state, reason = 'stopped', 'STOP is active; existing work and checkpoints are preserved'
    elif time.time() > float(data.get('deadline_ts', 0)):
        # Status is observational, not a renewal or a whole-job write. Keep the
        # existing blocked vocabulary used by explicit parent-stage handling.
        state = 'blocked'
        reason = 'job wall-clock budget deadline expired; original task records and deadline are preserved'
    elif errors:
        state, reason = 'blocked', '\n'.join(errors)
    elif goal is None:
        state, reason = 'blocked', f'formal goal is {goal_location}; retry the same request'
    elif goal.status in ('failed', 'blocked'):
        state, reason = 'blocked', f'goal {goal.status}: {goal.result or goal.body}'
    elif any(location == 'processing' for _, location in active):
        state = 'working'
    elif any(not cfg.worker(msg.to).get('can_spawn') for msg, _ in active):
        state = 'waiting_handoff'
        reason = 'a selected GUI member needs its active conversation to receive this task'
    elif active:
        state = 'queued'
    elif data['tasks']:
        state = 'awaiting_summary'
        reason = ('associated peer review is unfinished' if pending_reviews else
                  'tasks have receipts; coordinator must verify artifacts and finalize explicitly')
    elif goal_location == 'processing':
        state = 'working'
    elif goal_location == 'ready' and not cfg.worker(goal.to).get('can_spawn'):
        state, reason = 'waiting_handoff', 'coordinator needs an active GUI conversation'
    elif goal_location == 'done':
        state, reason = 'awaiting_summary', 'goal archived, but no verified job finalization exists'
    return {**data, 'tasks': tasks, 'state': state, 'reason': reason,
            'plan_binding': {'bound_id': bound.get('revision_id') if bound else None,
                             'bound_sha256': bound.get('content_sha256') if bound else None,
                             'current_id': current.get('revision_id') if current else None,
                             'current_sha256': current.get('content_sha256') if current else None,
                             'differs_from_current': bool('plan_snapshot' in data and
                                 (bound or {}).get('revision_id') != (current or {}).get('revision_id')),
                             'legacy_unbound': 'plan_snapshot' not in data},
            'artifact_contract': 'required_verified' if artifact_required else 'legacy_external',
            'artifacts_verified': artifacts_verified,
            'pending_reviews': pending_reviews, 'remaining_s': max(0, data['deadline_ts'] - time.time()),
            'goal_location': goal_location,
            'goal_status': goal.status if goal else 'missing',
            'goal_claimed_by': goal.claimed_by if goal else '',
            'goal_claimed_at': goal.claimed_at if goal else '',
            'goal_claim_complete': goal_claim_complete,
            'goal_heartbeat_age_s': round(goal_age_s, 2),
            'goal_heartbeat_stale': goal_stale,
            'goal_heartbeat_source': goal_heartbeat_source,
            'role_checks_are_authentication': False}


def list_jobs(cfg: Config) -> list[dict[str, Any]]:
    return [status_job(cfg, path.stem) for path in
            sorted((_root(cfg) / 'jobs').glob('j-*.json'))]


def checkpoint_job(cfg: Config, caller: str, job_id: str, note: str) -> dict[str, Any]:
    """Preserve next action or blocker; does not extend any deadline or run a model."""
    _member(cfg, caller)
    if not isinstance(note, str) or not note.strip():
        raise JobError('checkpoint must contain a non-empty next action or blocker')
    with _locked(cfg, _safe(job_id, 'job id'), caller):
        data = _load(cfg, job_id)
        data['checkpoints'].append({'caller': caller, 'at': now_iso(), 'note': note.strip()})
        _save(cfg, data)
        return status_job(cfg, job_id)


def register_job_tasks(cfg: Config, caller: str, job_id: str,
                       messages: list[Message], bodies: list[str] | None = None) -> None:
    """Allocate followup/retry intent before publication, preserving the original plan."""
    with _locked(cfg, _safe(job_id, 'job id'), caller):
        data = _load(cfg, job_id)
        _coordinator(cfg, caller, data)
        _running_allowed(cfg, data)
        if data.get('finalization'):
            raise JobError('finalized job cannot accept automatic continuation tasks')
        if bodies is not None and len(bodies) != len(messages):
            raise JobError('followup task body count differs from messages')
        existing = {item['id']: item for item in data['tasks']}
        new = [message for message in messages if message.id not in existing]
        count = len(existing) + len(new)
        decision = GateKeeper(cfg.gates).check_task_count(count)
        if not decision:
            raise JobError(decision.reason)
        for index, message in enumerate(messages):
            if (message.type != 'task' or message.job_id != job_id
                    or message.deadline_ts <= 0 or message.deadline_ts > data['deadline_ts']):
                raise JobError('automatic task binding/type/deadline differs from its job')
            message_job(cfg, message)
            if message.id in existing:
                if not _same_intent(message, existing[message.id]['message']):
                    raise JobError('automatic task id was reused for different intent')
                continue
            item = {'id': message.id, 'worker': message.to, 'title': message.title,
                    'body': bodies[index] if bodies is not None else message.body,
                    'result_to': message.result_to, 'message': message.to_dict()}
            if message.expected_artifacts:
                item['expected_artifacts'] = _expected_artifacts(message.expected_artifacts)
            data['tasks'].append(item)
        _save(cfg, data)


def finalize_job(cfg: Config, caller: str, job_id: str, summary: str,
                 verified: bool = False) -> dict[str, Any]:
    """Record explicit human/agent verification, then publish the goal summary.

No automatic artifact acceptance is implied. A claimed goal is owned by its
original executing turn and must be reported there; this function never steals
it. An unclaimed goal is finalized with the Bus ready-claim operation.
"""
    if verified is not True:
        raise JobError('finalization requires explicit verified=True after artifact checks（作业收尾前需显式添加 --verified 确认完成产物与回执人工复核）')
    if not isinstance(summary, str) or not summary.strip():
        raise JobError('verified summary must be non-empty')
    summary = summary.strip()
    with _locked(cfg, _safe(job_id, 'job id'), caller):
        data = _load(cfg, job_id)
        _coordinator(cfg, caller, data)
        state = status_job(cfg, job_id)
        finalization = data.get('finalization')
        if finalization and finalization['summary'] != summary:
            raise JobError('this job has a different finalization summary; preserve the audit record')
        if state['state'] == 'completed':
            return state
        _running_allowed(cfg, data)
        _member(cfg, data['caller'])
        if (not data['tasks'] or state['state'] != 'awaiting_summary' or state['pending_reviews']):
            raise JobError(f"job cannot be finalized: {state['reason'] or state['state']}")
        bus = _bus(cfg)
        # Source: actual OpenCode peer assessment, Codex, 2026-10-04. Explicit
        # verified=True cannot bypass declarations and raw-byte manifests.
        for item in data['tasks']:
            task, _ = _message(bus, item['id'])
            if task is None:
                raise JobError('task has no formal message')
            receipts, error = _receipts(bus, task)
            if not error:
                error = _artifact_error(cfg, item, receipts[0])
            if error:
                raise JobError(error)
        goal, location = _message(bus, data['goal_id'])
        if goal is None or location in ('processing', 'blocked'):
            raise JobError('goal is claimed/blocked; its owning conversation must report it')
        if location not in ('ready', 'done'):
            raise JobError('goal is not in a finalizable location')
        existing_results = bus.find_results(goal.id)
        if existing_results:
            _, error = _receipts(bus, goal)
            if error:
                raise JobError(error)
            if existing_results[0].body != summary:
                raise JobError('existing goal result has a different summary; do not duplicate it')
        if not finalization:
            receipt = (existing_results[0] if existing_results else
                       bus.make(caller, data['caller'], 'result', body=summary,
                                reply_to=data['goal_id'], **job_binding(data)))
            if not existing_results:
                receipt.exit_code = 0
            finalization = {'summary': summary, 'declared_by': caller, 'at': now_iso(),
                            'automatic_artifact_verification': state['artifact_contract'] == 'required_verified',
                            'artifact_contract': state['artifact_contract'], 'committed': False,
                            'result_message': receipt.to_dict()}
            data['finalization'] = finalization
            _save(cfg, data)
        elif existing_results:
            finalization['result_message'] = existing_results[0].to_dict()
            _save(cfg, data)
        if location == 'ready':
            goal.result, goal.exit_code = summary, 0
            if bus.finish_if_ready(goal.to, goal) is None:
                raise JobError('goal was claimed concurrently; leave it to its owning conversation')
        elif goal.status != 'done' or goal.exit_code not in (None, 0):
            raise JobError('goal was archived with failure; cannot declare completed')
        _running_allowed(cfg, data)
        # Goal completion itself participates in periodic oversight. Queue that
        # review now, rather than announcing success and discovering it later
        # when the initiating member consumes the goal's summary result.
        from .director import Director
        director = Director(cfg, bus=bus)
        review_key = f"peer-review/{goal.reply_to or goal.id}"
        try:
            busy_before = director.leases.holder(review_key) is not None
            scheduled = director.schedule_peer_review(goal, caller, 'done')
            busy_after = director.leases.holder(review_key) is not None
        finally:
            director.close()
        state = status_job(cfg, job_id)
        if state['pending_reviews']:
            return state  # Intent/checkpoint is durable; no completed declaration or summary result yet.
        if scheduled is None and (busy_before or busy_after):
            state['reason'] = 'peer review scheduling is in progress; resume after its lease is released'
            return state
        if state['state'] != 'awaiting_summary':
            raise JobError(f"job changed during finalization: {state['reason'] or state['state']}")
        _running_allowed(cfg, data)
        _publish(bus, finalization['result_message'])
        _running_allowed(cfg, data)
        data['finalization']['committed'] = True
        _save(cfg, data)
        bus.audit(event='job_completed', job_id=job_id, caller=caller,
                  verification='explicit declaration; artifact checks external',
                  summary_result_id=finalization['result_message']['id'])
        return status_job(cfg, job_id)
