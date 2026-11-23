"""A bounded WebView API over the formal local bus. Source: Codex, 2026-10-09.

Only ``call`` dispatches business actions. No command execution, model calls,
private host discovery, automatic recovery, or replacement task queue exists
here. Claim tickets are captured once at claim time and kept in this window.
"""
from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
import hashlib
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import stat
import shutil
import sys
import threading
import time
import uuid

from .bus import Bus, now_iso
from .claim_fencing import captured_ticket, normalize
from .config import Config, DEFAULTS, load, save
from .gui_state import (build_group_config, load_branch_folds, load_member_icon_paths,
                        load_notes, load_priorities, load_viewport, save_branch_folds,
                        save_last_workspace, save_member_icon_paths, save_notes,
                        save_priorities, save_viewport)
from .lease import LeaseManager
from .release_updates import ReleaseUpdates
from .member_adapter import DEFAULT_FILE_CAPABILITIES
from .member_identity_icons import identity_icon_data_uri
from .profiles import project_skill_paths, validate_agent_id
from .public_files import PublicFiles, validate_relative
from .task_tree import TaskForest, TaskTreeError
from .operation_lock import task_lock
from .task_assignment import assignment_mode, recommend, task_prompt, MAX_AUTOMATIC_BATCH


_PRIVATE = {'.workbuddy', '.git', '.agentbus', '.codex', '.ssh', '.aws', '.azure', '.gnupg',
            '.claude', '.trae', '.zcode', '不要访问', 'do not access', '.env', 'auth.json',
            'credentials.json', 'cookies.json', 'id_rsa', 'id_ed25519'}
_SECRET_KEYS = {'claim_id', 'source_claim_id', 'claim_fingerprint', 'token', 'access_token',
                'refresh_token', 'secret', 'api_key', 'apikey', 'password',
                'cookie', 'cookies', 'credentials', 'sha256'}
_IMPORT_PREVIEW_TTL_S = 15 * 60
_IMPORT_PREVIEW_LIMIT = 16


def _public(value):
    """Keep task evidence useful without putting credentials/tickets in views."""
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items()
                if str(k).casefold() not in _SECRET_KEYS
                and not str(k).casefold().endswith('_sha256')}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


def _directory(value):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError('请选择有效的本地文件夹')
    text = str(value).strip()
    win = PureWindowsPath(text)
    path = Path(text)
    if (any(ord(ch) < 32 for ch in text) or '://' in text or text.startswith('\\\\')
            or '..' in path.parts or '..' in win.parts
            or any(part.rstrip(' .').casefold() in _PRIVATE for part in [*path.parts, *win.parts])
            or not path.is_absolute()):
        raise ValueError('目录必须是明确的本地绝对路径，不能越界或进入私有目录')
    public = PublicFiles(path)  # Reject links/junctions before resolve/open.
    path = public.root.resolve()
    if not path.is_dir():
        raise ValueError('所选路径不是现有文件夹')
    return path


class WebBridge:
    """Public surface for pywebview: bind_window and a finite action dispatcher."""

    def __init__(self, workspace=None, *, remember_workspace=False):
        # Opening the window must not write into an unchosen folder (often the
        # EXE or Downloads directory). Unconfigured folders show the setup flow;
        # scaffolding happens only when the user explicitly picks a folder.
        self._workspace = _directory(workspace if workspace is not None else Path.cwd())
        self._window = None
        self._tickets = {}
        self._import_previews = OrderedDict()
        self._desktop_icon_cache = {}
        self._remember_workspace = bool(remember_workspace)
        self._lock = threading.RLock()
        self._release_updates = ReleaseUpdates()

    def bind_window(self, window):
        self._window = window

    def dispatch(self, action, payload=None):
        res = self.call(action, payload)
        if isinstance(res, dict) and res.get('ok'):
            return res.get('data') or {}
        if isinstance(res, dict) and not res.get('ok') and 'error' in res:
            raise ValueError(res['error'])
        return res

    def call(self, action, payload=None):
        actions = {
            'snapshot': self._snapshot, 'configure': self._configure,
            'start_goal': self._start_goal, 'claim_leaf': self._claim_leaf,
            'dispatch_leaf': self._dispatch_leaf,
            'dispatch_ready': self._dispatch_ready, 'task_prompt': self._task_prompt,
            'submit_leaf_result': self._submit_leaf_result,
            'audit_context': self._audit_context, 'claim_audit': self._claim_audit,
            'submit_audit': self._submit_audit,
            'reveal_artifact': self._reveal_artifact,
            'scan_project_trees': self._scan_project_trees,
            'load_viewport': self._load_viewport,
            'save_viewport': self._save_viewport,
            'load_branch_folds': self._load_branch_folds,
            'save_branch_folds': self._save_branch_folds,
            'load_notes': self._load_notes,
            'save_notes': self._save_notes,
            'load_priorities': self._load_priorities,
            'save_priorities': self._save_priorities,
            'tree_templates': self._tree_templates, 'export_tree': self._export_tree,
            'export_handover': self._export_handover, 'export_delivery': self._export_delivery,
            'import_tree': self._import_tree,
            'checkpoint_leaf': self._checkpoint_leaf, 'handoff_leaf': self._handoff_leaf, 'takeover_leaf': self._takeover_leaf, 'takeover_capsule': self._takeover_capsule,
            'pass_leaf': self._pass_leaf, 'reset_leaf': self._reset_leaf, 'diagnose_deadlock': self._diagnose_deadlock,
            'gc_plan': self._gc_plan, 'gc_apply': self._gc_apply,
            'discover_leaf': self._discover_leaf, 'repomap_slice': self._repomap_slice,
            'lessons_list': self._lessons_list, 'lessons_add': self._lessons_add,
            'worktree_list': self._worktree_list,
            'trigger_desktop_wakeup': self._trigger_desktop_wakeup,
            'choose_workspace': self._choose_workspace, 'choose_output': self._choose_output,
            'set_workspace': self._set_workspace, 'skills': self._skills,
            'skills_list': self._skills_list, 'skills_inspect': self._skills_inspect,
            'workflow_prompt': self._workflow_prompt,
            'onboarding_capsule': self._onboarding_capsule,
            'system_check': self._system_check,
            'release_preferences': self._release_preferences,
            'set_release_preferences': self._set_release_preferences,
            'check_release': self._check_release,
            'open_release_page': self._open_release_page,
            'ensure_quickstart': self._ensure_quickstart,
            'recovery': self._recovery, 'activity': self._activity,
            'stop': self._stop, 'resume': self._resume,
            'append_leaf': self._append_leaf, 'add_leaf': self._append_leaf, 'add_task': self._append_leaf,
            'update_leaf_topology': self._update_leaf_topology,
            'adopt_change': self._adopt_change,
            'withdraw_change': self._withdraw_change,
            'member_pack': self._member_pack,
            'export_backup': self._export_backup,
            'restore_backup': self._restore_backup,
            'open_artifact': self._open_artifact,
            'read_artifact_diff': self._read_artifact_diff,
            'recover_task': self._recover_task,
            'console_mirror': self._console_mirror,
            'export_canvas_image': self._export_canvas_image,
            'show_desktop_notification': self._show_desktop_notification,
            'one_click_onboard': self._one_click_onboard,
            'auto_onboard': self._one_click_onboard,
            'trigger_onboarding_start': self._trigger_onboarding_start,
            'trigger_onboarding_advance': self._trigger_onboarding_advance,
            'trigger_onboarding_resume': self._trigger_onboarding_resume,
            'trigger_status': self._trigger_status,
            'trigger_set_stopped': self._trigger_set_stopped,
            'trigger_diagnostics': self._trigger_diagnostics,
            'trigger_verification_plan': self._trigger_verification_plan,
            'trigger_verification_run': self._trigger_verification_run,
            'trigger_verification_start': self._trigger_verification_start,
            'trigger_verification_check': self._trigger_verification_check,
            'trigger_reconcile': self._trigger_reconcile,
            'trigger_scan_now': self._trigger_scan_now,
            'contact_show': self._contact_show,
            'contact_checkin': self._contact_checkin,
        }
        try:
            if not isinstance(action, str) or action not in actions:
                raise ValueError('此界面不支持该动作')
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise ValueError('动作参数必须是对象')
            # Every action has its own field allowlist as well.
            if len(json.dumps(payload, ensure_ascii=False)) > 100000:
                raise ValueError('动作参数过长')
            if action in {'release_preferences', 'set_release_preferences', 'check_release',
                          'open_release_page'}:
                return {'ok': True, 'data': actions[action](payload)}
            with self._lock:
                return {'ok': True, 'data': actions[action](payload)}
        except Exception as exc:
            # Never serialize traceback, host environment or arbitrary objects.
            return {'ok': False, 'error': str(exc)[:1200], 'code': type(exc).__name__}

    @staticmethod
    def _fields(payload, allowed):
        if set(payload) - set(allowed):
            raise ValueError('动作包含未支持的参数')

    def _release_preferences(self, payload):
        self._fields(payload, ())
        return self._release_updates.preferences()

    def _set_release_preferences(self, payload):
        self._fields(payload, ('automatic',))
        return self._release_updates.set_preferences(payload.get('automatic'))

    def _check_release(self, payload):
        self._fields(payload, ('automatic',))
        return self._release_updates.check(payload.get('automatic', False))

    def _open_release_page(self, payload):
        self._fields(payload, ())
        return self._release_updates.open_release_page()

    @staticmethod
    def _ensure_workspace_initialized(workspace: Path) -> None:
        """用户明确选择的文件夹若尚无 .agentbus/config.json，执行快速初始化；失败如实报错。"""
        if not (workspace / '.agentbus' / 'config.json').exists():
            from .quickstart import run_quickstart
            try:
                run_quickstart(workspace, print_output=False)
            except Exception as exc:
                raise ValueError(f'无法在所选文件夹初始化项目：{exc}') from exc

    def _cfg(self):
        public = PublicFiles(self._workspace)
        public.read_json('.agentbus/config.json')
        return load(self._workspace)

    @staticmethod
    def _member(cfg, value, *, executor=False):
        validate_agent_id(value)
        workers = cfg.executor_workers() if executor else cfg.enabled_workers()
        if value not in workers:
            raise ValueError('该成员没有加入当前项目或未启用执行权限')
        return value

    @staticmethod
    def _mailboxes(cfg):
        """Read formal records using Bus, without Bus.dirs/register side effects."""
        public = PublicFiles(cfg.workspace)
        bus = Bus(cfg.workspace / '.agentbus')
        folder = public.checked('.agentbus/agents')
        if folder.exists():
            # Existing observers use glob over every mailbox. Refuse invalid
            # directory names before those observers can traverse a private tree.
            with os.scandir(folder) as entries:
                for index, entry in enumerate(entries):
                    if index >= 512:
                        raise ValueError('成员信箱目录超过界面观察数量上限')
                    validate_agent_id(entry.name, allow_system=True)
        members = public.list_member_directories()
        records = {}
        for agent in members:
            records[agent] = {}
            for state in ('ready', 'processing', 'blocked', 'done'):
                rows = []
                for page in public.iter_file_pages(f'.agentbus/agents/{agent}/{state}'):
                    for path in page:
                        public.read_bytes(path.relative_to(cfg.workspace), limit=4 * 1024 * 1024)
                        message = bus.read(path)
                        message.validate()
                        if message.id != path.stem or message.to != agent:
                            raise ValueError('正式信箱消息与所在路径不一致；请核对原记录')
                        rows.append(message)
                rows.sort(key=lambda message: message.id)
                records[agent][state] = rows
        return records

    @staticmethod
    def _machine_paths(cfg, records):
        public = PublicFiles(cfg.workspace)
        public.checked('.agentbus/task-tree.json')
        public.checked('.agentbus/project-plan/current.json')
        public.checked('.agentbus/project-plan/revisions')
        for path in public.list_files('.agentbus/jobs'):
            public.checked(path.relative_to(cfg.workspace))
        for agent, box in records.items():
            for message in box.get('processing', []):
                public.checked(f'.agentbus/heartbeats/{message.id}/{agent}.json')
                public.checked(f'.agentbus/session-stops/{message.id}.json')

    @staticmethod
    def _pending_goals(cfg, records):
        public = PublicFiles(cfg.workspace)
        pending = []
        for agent, box in records.items():
            for state in ('ready', 'processing'):
                for message in box.get(state, []):
                    if message.type != 'goal':
                        continue
                    if message.job_id:
                        raw = public.read_bytes(f'.agentbus/jobs/{message.job_id}.json', limit=4 * 1024 * 1024)
                        if raw is None:
                            continue  # A missing job is unverified, not a new goal.
                        job = json.loads(raw.decode('utf-8'))
                        if (not isinstance(job, dict) or job.get('id') != message.job_id
                                or job.get('goal_id') != message.id or not isinstance(job.get('tasks'), list)):
                            raise ValueError('目标与正式作业绑定不一致；请核对原记录')
                        if job['tasks']:
                            continue
                    pending.append({'id': message.id, 'job_id': message.job_id,
                                    'title': message.title, 'goal': message.body[:20000],
                                    'recipient': agent, 'caller': message.sender,
                                    'status': '待分解', 'location': state, 'deadline_ts': message.deadline_ts})
        return pending

    def _snapshot(self, payload):
        self._fields(payload, ())
        public = PublicFiles(self._workspace)
        configured = public.checked('.agentbus/config.json').exists()
        if configured:
            cfg = self._cfg()
            records = self._mailboxes(cfg)
            self._machine_paths(cfg, records)
            tf_inst = TaskForest(cfg)
            raw_snap = tf_inst.snapshot()
            raw_snap['deadlock_diagnostics'] = tf_inst.diagnose_deadlock(raw_snap)
            forest = _public(raw_snap)
            cards = {}
            for agent in cfg.executor_workers():
                try:
                    cards[agent] = public.read_json(f'.agentbus/member-cards/{agent}.json')
                except (ValueError, OSError):
                    cards[agent] = {}  # Invalid public evidence remains unverified.
            for leaf in forest['leaves']:
                leaf.update(recommend(cfg, leaf, records, cards=cards))
                owner = leaf.get('owner')
                if owner and (leaf.get('id'), owner) in self._tickets:
                    # A boolean UI affordance only; the claim ticket remains private
                    # and the commit path revalidates it against the live mailbox.
                    leaf['operator_can_record'] = True
            pending_goals = self._pending_goals(cfg, records)
            members = []
            for agent, worker in cfg.data['workers'].items():
                box = records.get(agent, {})
                processing = [m for m in box.get('processing', []) if m.type in ('task', 'goal', 'review')]
                pending = [m for m in box.get('ready', []) if m.type in ('task', 'goal', 'review')]
                blocked = [m for m in box.get('blocked', []) if m.type in ('task', 'goal', 'review')]
                running = any(leaf.get('owner') == agent and leaf.get('state') == 'running'
                              for leaf in forest['leaves'])
                active_conversations = [
                    {'conversation_id': leaf['conversation_id'], 'leaf_id': leaf['id'],
                     'task_id': leaf.get('task_id'), 'title': leaf.get('title', ''),
                     'state': leaf.get('state')}
                    for leaf in forest['leaves']
                    if leaf.get('owner') == agent
                    and leaf.get('state') in {'claimed', 'running'}
                    and leaf.get('conversation_id')]
                enabled = bool(worker.get('enabled', True))
                status = ('停用' if not enabled else '执行权限已停用' if not worker.get('executor_enabled', True)
                          else '运行中' if running else '已认领' if processing
                          else '阻塞' if blocked else '待认领' if pending else '待接入')
                mode = worker.get('communication_mode') or worker.get('transport', 'file')
                # Use bundled, brand-neutral SVG identities. Displaying a member
                # must not inspect a user-selected executable or expose its path.
                identity_icon = identity_icon_data_uri(
                    agent, worker.get('display_name') or agent)
                members.append({'id': agent, 'label': worker.get('display_name') or agent,
                    'display_name': worker.get('display_name') or agent, 'mode': mode,
                    'communication_mode': mode, 'enabled': enabled,
                    'identity_icon_data': identity_icon,
                    'executor_enabled': bool(worker.get('executor_enabled', True)), 'status': status,
                    'active_tasks': [{'id': m.id, 'title': m.title, 'type': m.type,
                                      'status': '已认领'} for m in processing],
                    'active_conversations': active_conversations,
                    'active_conversation_count': len(active_conversations),
                    'pending_tasks': [{'id': m.id, 'title': m.title, 'type': m.type,
                                       'status': '待认领'} for m in pending],
                    'mailbox': {state: len(box.get(state, [])) for state in
                                ('ready', 'processing', 'blocked', 'done')}})
            project = {'name': cfg.data.get('project_name') or self._workspace.name,
                       'workspace': str(cfg.workspace), 'output_root': str(cfg.output_root),
                       'coordinator': cfg.director, 'stopped': forest['stopped'], 'configured': True,
                       'assignment_mode': assignment_mode(cfg.data)}
            summary = {'workspace': str(cfg.workspace), 'output_root': str(cfg.output_root),
                       'director': cfg.director, 'coordinator': cfg.director,
                       'selected_members': list(cfg.enabled_workers()),
                       'supervision': {
                           'strict_supervision': bool(cfg.data.get('supervision', {}).get('strict_supervision', False)),
                           'designated_auditor': cfg.data.get('supervision', {}).get('designated_auditor', ''),
                           'check_count': len(cfg.data.get('supervision', {}).get('checks', [])),
                           'checks': _public(cfg.data.get('supervision', {}).get('checks', []))},
                       'members': [{k: m[k] for k in ('id', 'display_name', 'enabled', 'communication_mode')}
                                   for m in members]}
        else:
            forest = {'schema_version': 1, 'roots': [], 'branches': [], 'leaves': [],
                      'stopped': os.path.lexists(public.checked('.agentbus/STOP')),
                      'source': 'AgentBus project not configured', 'generated_at': now_iso()}
            members = []
            pending_goals = []
            project = {'name': self._workspace.name, 'workspace': str(self._workspace),
                       'output_root': str(self._workspace), 'coordinator': '',
                       'stopped': forest['stopped'], 'configured': False, 'assignment_mode': 'automatic'}
            summary = {'workspace': str(self._workspace), 'output_root': str(self._workspace),
                       'director': '', 'coordinator': '', 'selected_members': [],
                       'supervision': {'strict_supervision': False, 'designated_auditor': '',
                                       'check_count': 0, 'checks': []},
                       'members': []}
        return {**forest, 'forest': forest, 'nodes': [*forest['roots'], *forest['branches'], *forest['leaves']],
                'project': project, 'members': members, 'config': summary,
                'ui_state': {'collapsed_branches': load_branch_folds(self._workspace),
                             'notes': load_notes(self._workspace),
                             'priorities': load_priorities(self._workspace)},
                'pending_goals': pending_goals,
                'capabilities': {'model_calls': False, 'shell': False, 'automatic_recovery': False}}

    @staticmethod
    def _selection(payload):
        source = payload.get('members', payload.get('selected_members'))
        if not isinstance(source, list) or not source or len(source) > 128:
            raise ValueError('请提供有限的成员列表')
        entries, seen = [], set()
        for item in source:
            item = {'id': item} if isinstance(item, str) else item
            if not isinstance(item, dict):
                raise ValueError('成员条目必须是标识或对象')
            WebBridge._fields(item, ('id', 'display_name', 'label', 'enabled', 'communication_mode',
                                     'mode'))
            agent = validate_agent_id(item.get('id'))
            if agent in seen:
                raise ValueError('成员标识不能重复')
            seen.add(agent)
            enabled = item.get('enabled', True)
            if type(enabled) is not bool:
                raise ValueError('成员启用状态必须是布尔值')
            label = item.get('display_name', item.get('label'))
            if label is not None and (not isinstance(label, str) or not label.strip()
                                      or len(label) > 80 or any(ord(c) < 32 for c in label)):
                raise ValueError('成员名称需为1–80个可显示字符')
            mode = item.get('communication_mode', item.get('mode'))
            if mode is not None and mode not in ('file', 'cli', 'acp', 'sdk'):
                raise ValueError('成员接入方式无效')
            entries.append({'id': agent, 'display_name': label.strip() if label else None,
                            'enabled': enabled, 'mode': mode})
        return entries

    @staticmethod
    def _member_icon_path(value):
        if not isinstance(value, (str, Path)) or not str(value).strip() or len(str(value)) > 2048:
            raise ValueError('请选择一个本机桌面应用程序文件')
        text = str(value).strip()
        path, win = Path(text), PureWindowsPath(text)
        components = [part.rstrip(' .').casefold() for part in [*path.parts, *win.parts]]
        if (not path.is_absolute() or not win.is_absolute() or text.startswith('\\\\')
                or '://' in text or any(ord(char) < 32 for char in text)
                or '..' in path.parts or '..' in win.parts
                or any(part in _PRIVATE for part in components)
                or path.suffix.casefold() != '.exe'):
            raise ValueError('图标只能从允许访问的本机 .exe 文件中选择')
        ancestors = (path, *path.parents)
        if any(candidate.is_symlink()
               or (callable(getattr(candidate, 'is_junction', None))
                   and candidate.is_junction()) for candidate in ancestors):
            raise ValueError('所选桌面应用程序文件不可访问')
        reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        for candidate in ancestors:
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise ValueError('所选桌面应用程序文件不可访问') from None
            if getattr(info, 'st_file_attributes', 0) & reparse_flag:
                raise ValueError('所选桌面应用程序文件不可访问')
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError('所选桌面应用程序文件不存在') from None
        if (not resolved.is_file() or resolved.is_symlink()
                or any(part.rstrip(' .').casefold() in _PRIVATE for part in resolved.parts)):
            raise ValueError('所选桌面应用程序文件不可访问')
        return resolved

    def _member_icon(self, value):
        from .desktop_app_icon import NEUTRAL_PLACEHOLDER_DATA_URI, get_desktop_app_icon
        if not isinstance(value, str) or not value:
            return {'data_uri': NEUTRAL_PLACEHOLDER_DATA_URI, 'status': 'placeholder'}
        try:
            safe = self._member_icon_path(value)
        except (TypeError, ValueError):
            return {'data_uri': NEUTRAL_PLACEHOLDER_DATA_URI, 'status': 'placeholder'}
        try:
            stat = safe.stat()
            key = (str(safe), stat.st_size, stat.st_mtime_ns)
            cached = self._desktop_icon_cache.get(key)
            if cached is not None:
                return cached
        except OSError:
            key = None
        result = get_desktop_app_icon(safe)
        icon = {'data_uri': result.data_uri, 'status': result.status}
        if key is not None:
            if icon['status'] == 'ok':
                if len(self._desktop_icon_cache) >= 128:
                    self._desktop_icon_cache.clear()
                self._desktop_icon_cache[key] = icon
        return icon

    @staticmethod
    def _configured_data(fresh, entries, coordinator, workspace, output, supervision=None):
        data = deepcopy(fresh)
        for worker in data.get('workers', {}).values():
            if isinstance(worker, dict):
                worker.pop('desktop_icon_path', None)
        for entry in entries:
            name, mode = entry['id'], entry['mode']
            worker = data['workers'].get(name)
            if worker is None:
                mode = mode or 'file'
                if mode != 'file':
                    raise ValueError('新成员先使用文件路线；独立适配器须另行正式验收')
                worker = data['workers'][name] = {'can_spawn': False, 'kind': 'gui',
                    'human_in_loop': True, 'transport': 'file', 'communication_mode': 'file',
                    'file_capabilities': list(DEFAULT_FILE_CAPABILITIES)}
            current = worker.get('communication_mode') or worker.get('transport', 'file')
            mode = mode or current
            accepted = worker.get('accepted_program_mode')
            if current != 'file' and worker.get('transport') == 'cli' and worker.get('can_spawn'):
                accepted = current
                worker['accepted_program_mode'] = current
            if mode != 'file' and (accepted != mode or not worker.get('can_spawn')):
                raise ValueError('此界面不能启用尚未正式验收的独立适配器')
            if entry['display_name'] is not None:
                worker['display_name'] = entry['display_name']
            worker['communication_mode'] = mode
            if mode == 'file':
                worker['transport'] = 'file'
            else:
                worker['transport'] = 'cli'
        data['workspace'] = str(workspace)
        data['output_root'] = str(output)
        if supervision is not None:
            if not isinstance(supervision, dict):
                raise ValueError('独立审核设置必须是对象')
            allowed = {'strict_supervision', 'designated_auditor', 'checks'}
            if set(supervision) - allowed:
                raise ValueError('独立审核设置含有不支持的字段')
            data['supervision'] = deepcopy(supervision)
        return build_group_config(data, [e['id'] for e in entries if e['enabled']], coordinator)

    def _configure(self, payload):
        self._fields(payload, ('workspace', 'output_root', 'director', 'coordinator', 'members',
                               'selected_members', 'assignment_mode', 'supervision'))
        workspace = _directory(payload.get('workspace', self._workspace))
        public = PublicFiles(workspace)
        path = public.checked('.agentbus/config.json')
        existing = path.exists()
        fresh = load(workspace).data if existing else deepcopy(DEFAULTS)
        output = _directory(payload.get('output_root', Config(path, workspace, fresh).output_root))
        entries = self._selection(payload)
        coordinator = payload.get('coordinator', payload.get('director'))
        validate_agent_id(coordinator)
        if 'director' in payload and 'coordinator' in payload and payload['director'] != coordinator:
            raise ValueError('协调者字段不一致')
        self._configured_data(fresh, entries, coordinator, workspace, output,
                              payload.get('supervision'))
        assignment_mode({'assignment_mode': payload.get('assignment_mode', assignment_mode(fresh))})
        self._check_output_change(Config(path, workspace, fresh), output, existing)
        public.checked('.agentbus/leases/.mutation.lock')
        manager = LeaseManager(workspace / '.agentbus')
        with task_lock(workspace / '.agentbus', 'task-forest', 'structure'), \
                manager.guarded('.agentbus/config.json', 'agentbus-web-config', ttl_s=120):
            fresh = load(workspace).data if path.exists() else deepcopy(DEFAULTS)
            self._check_output_change(Config(path, workspace, fresh), output, path.exists())
            icon_paths = load_member_icon_paths(workspace)
            for agent, worker in fresh.get('workers', {}).items():
                legacy_path = worker.get('desktop_icon_path') if isinstance(worker, dict) else None
                if agent not in icon_paths and isinstance(legacy_path, str) and legacy_path:
                    try:
                        icon_paths[agent] = str(self._member_icon_path(legacy_path))
                    except (TypeError, ValueError):
                        pass
            for entry in entries:
                icon_path = entry.get('desktop_icon_path')
                if icon_path is None:
                    continue
                if icon_path:
                    icon_paths[entry['id']] = icon_path
                else:
                    icon_paths.pop(entry['id'], None)
            data = self._configured_data(fresh, entries, coordinator, workspace, output,
                                         payload.get('supervision'))
            data['assignment_mode'] = payload.get('assignment_mode', assignment_mode(fresh))
            self._check_member_removal(Config(path, workspace, fresh), data, path.exists())
            self._check_supervision_change(Config(path, workspace, fresh), data, path.exists())
            if not save_member_icon_paths(workspace, icon_paths):
                raise ValueError('无法保存本机图标偏好；项目配置未写入')
            public.checked('.agentbus/config.json')
            cfg = save(Config(path, workspace, data))
            bus = Bus(workspace / '.agentbus')
            bus.init()
            for agent in [*cfg.enabled_workers(), 'human']:
                public.checked(f'.agentbus/agents/{agent}')
                bus.register(agent)
            bus.audit(event='web_configure', caller=coordinator, members=list(cfg.enabled_workers()),
                      source='AgentBus WebView', output_root=str(output))
        if workspace != self._workspace:
            self._tickets.clear()
        self._workspace = workspace
        view = self._snapshot({})
        view['saved'] = True
        view['model_calls'] = 0
        # Formal workflow creation may fail independently of the saved config.
        try:
            from .project_workflow import prepare_project_workflow, workflow_status
            prepare_project_workflow(cfg)
            status = workflow_status(cfg)
            view['workflow_ready'] = bool(status['ready'])
            if not status['ready']:
                view['workflow_error'] = status.get('reason', '工作流入口待准备')
        except Exception as exc:
            view['workflow_ready'] = False
            view['workflow_error'] = str(exc)[:500]
        if self._remember_workspace:
            view['workspace_remembered'] = save_last_workspace(workspace)
            if not view['workspace_remembered']:
                view['workspace_warning'] = '项目已保存；本次未能保存下次启动时的恢复位置。'
        return view

    @staticmethod
    def _check_member_removal(cfg, updated, configured):
        if not configured:
            return
        selected = {agent for agent, worker in updated['workers'].items()
                    if worker.get('enabled', True) and worker.get('executor_enabled', True)}
        records = WebBridge._mailboxes(cfg)
        for agent, box in records.items():
            if agent not in cfg.data['workers']:
                continue  # Human escalation inbox is not a selectable member.
            if agent in selected:
                continue
            outstanding = [m for state in ('ready', 'processing', 'blocked')
                           for m in box.get(state, []) if m.type in ('task', 'goal', 'review')]
            # A decomposed goal stays as the immutable job's planning record;
            # it is not executable work and must not pin the old coordinator.
            unresolved = []
            public = PublicFiles(cfg.workspace)
            for message in outstanding:
                if message.type == 'goal' and message.job_id:
                    job = public.read_json(f'.agentbus/jobs/{message.job_id}.json')
                    if (job and job.get('id') == message.job_id
                            and job.get('goal_id') == message.id and isinstance(job.get('tasks'), list)
                            and job['tasks']):
                        continue
                unresolved.append(message)
            outstanding = unresolved
            if outstanding:
                raise ValueError(f'{agent} 仍有 {len(outstanding)} 项待办、在途或阻塞工作；'
                                 '先完成或正式交接这些任务，再停用或移出成员')

    @staticmethod
    def _check_supervision_change(cfg, updated, configured):
        """Keep the review policy stable while existing work is in flight."""
        if not configured:
            return

        def signature(data):
            raw = data.get('supervision')
            raw = raw if isinstance(raw, dict) else {}
            return {'strict_supervision': raw.get('strict_supervision', False),
                    'designated_auditor': raw.get('designated_auditor', '') or '',
                    'checks': raw.get('checks', [])}

        if signature(cfg.data) == signature(updated):
            return
        records = WebBridge._mailboxes(cfg)
        count = sum(1 for box in records.values()
                    for state in ('ready', 'processing', 'blocked')
                    for message in box.get(state, [])
                    if message.type in ('task', 'goal', 'review'))
        if count:
            raise ValueError(f'当前仍有 {count} 项待办、在途或阻塞工作；完成或正式交接后再更改独立复核设置')

    @staticmethod
    def _check_output_change(cfg, output, configured):
        if not configured or cfg.output_root == output:
            return
        records = WebBridge._mailboxes(cfg)
        for box in records.values():
            for state in ('ready', 'processing', 'blocked', 'done'):
                if any((m.type == 'task' and m.expected_artifacts) or m.artifacts
                       for m in box.get(state, [])):
                    raise ValueError('现有任务或历史产物绑定原业务目录；请保留该目录，另建项目后更换产出目录')

    def _start_goal(self, payload):
        self._fields(payload, ('caller', 'goal', 'request_id', 'parent_job_id'))
        cfg = self._cfg()
        self._machine_paths(cfg, self._mailboxes(cfg))
        caller = self._member(cfg, payload.get('caller', cfg.director))
        goal = payload.get('goal')
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 20000:
            raise ValueError('目标需为1–20000个字符')
        from .jobs import start_job
        with task_lock(cfg.workspace / '.agentbus', 'task-forest', 'structure'):
            cfg = self._cfg()
            caller = self._member(cfg, payload.get('caller', cfg.director))
            result = _public(start_job(cfg, caller, goal, payload.get('request_id'), payload.get('parent_job_id')))
        result.update(status='待分解' if not result.get('tasks') else '已派发', model_calls=0,
                      automatic_execution=False)
        return result

    def _append_leaf(self, payload):
        self._fields(payload, ('title', 'goal', 'parent', 'parent_id', 'branch', 'dependencies', 'id', 'caller', 'expected_artifacts'))
        title = str(payload.get('title') or '').strip()
        if not title:
            raise ValueError('请提供新增任务的标题')
        goal = str(payload.get('goal') or title).strip()
        parent = str(payload.get('parent') or payload.get('parent_id') or payload.get('branch') or 'desktop').strip()
        deps = payload.get('dependencies') or []
        if isinstance(deps, str):
            deps = [d.strip() for d in deps.split(',') if d.strip()]
        expected_artifacts = payload.get('expected_artifacts') or []

        cfg = self._cfg()
        forest = TaskForest(cfg)
        leaf_data = {
            'title': title,
            'goal': goal,
            'parent_id': parent,
            'dependencies': deps,
        }
        if payload.get('id'):
            leaf_data['id'] = str(payload['id']).strip()
        if expected_artifacts:
            leaf_data['expected_artifacts'] = expected_artifacts

        snapshot = _public(forest.seed_leaf(leaf_data, branch_id=parent))

        added_leaf = None
        for leaf in snapshot.get('leaves', []):
            if leaf.get('title') == title or (payload.get('id') and leaf.get('id') == payload.get('id')):
                added_leaf = leaf
                break
        return {'leaf': added_leaf, 'forest': snapshot}

    def _update_leaf_topology(self, payload):
        self._fields(payload, ('leaf_id', 'id', 'parent_id', 'parent', 'branch', 'dependencies', 'title'))
        leaf_id = str(payload.get('leaf_id') or payload.get('id') or '').strip()
        if not leaf_id:
            raise ValueError('请指定要更新的任务节点')
        parent_id = payload.get('parent_id') or payload.get('parent') or payload.get('branch')
        if parent_id is not None:
            parent_id = str(parent_id).strip()
        deps = payload.get('dependencies')
        if isinstance(deps, str):
            deps = [d.strip() for d in deps.split(',') if d.strip()]
        title = payload.get('title')
        if title is not None:
            title = str(title).strip()
        cfg = self._cfg()
        forest = TaskForest(cfg)
        try:
            snapshot = _public(forest.update_leaf_topology(leaf_id, parent_id=parent_id, dependencies=deps, title=title))
        except TaskTreeError as exc:
            if deps is not None and str(exc) in ('cyclic forest dependencies', 'cyclic forest readiness'):
                raise ValueError('无法建立连接：这会形成循环依赖，请调整前置任务后重试') from exc
            raise
        return {'ok': True, 'forest': snapshot}

    def _leaf_context(self, payload, allowed):
        self._fields(payload, allowed)
        cfg = self._cfg()
        agent = self._member(cfg, payload.get('agent', payload.get('caller')), executor=True)
        identity = payload.get('leaf_id', payload.get('id'))
        if not isinstance(identity, str) or not identity:
            raise ValueError('请指定叶子任务')
        forest = TaskForest(cfg)
        self._machine_paths(cfg, self._mailboxes(cfg))
        return forest, identity, agent

    def _claim_leaf(self, payload):
        forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller'))
        with task_lock(forest.root, 'task-forest', 'structure'):
            forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller'))
            leaf = forest.resolve_leaf(identity)
            leaf_id = leaf['id']
            message = forest.claim_leaf(leaf_id, agent)
            if message is None:
                raise ValueError('正式任务未认领；请刷新状态')
            ticket = captured_ticket(message)
            if ticket is None:
                raise ValueError('正式认领缺少原始票据；需要核对，不自动补取票据')
            self._tickets[(leaf_id, agent)] = ticket
            self._tickets[(identity, agent)] = ticket
            updated = next((n for n in forest.snapshot()['leaves'] if n['id'] == leaf_id), None)
            if updated is None:
                raise ValueError('认领后找不到对应的叶子节点')
            return {'leaf': _public(updated), 'task_id': message.id}

    def _submit_leaf_result(self, payload):
        forest, identity, agent = self._leaf_context(payload, (
            'leaf_id', 'id', 'agent', 'caller', 'summary', 'artifact_paths',
            'operator_confirmed'))
        if payload.get('operator_confirmed') is not True:
            raise ValueError('提交前请明确确认：这是你代录成员报告的完成结果')
        summary = payload.get('summary')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 20000:
            raise ValueError('请填写 1–20000 字的成果说明')
        from .cli import _result_artifacts, _validate_receipt_body
        _validate_receipt_body(summary)
        paths = payload.get('artifact_paths', [])
        if (not isinstance(paths, list) or len(paths) > 1000
                or not all(isinstance(path, str) and path.strip() for path in paths)):
            raise ValueError('产物路径必须是有限的相对路径列表')
        with task_lock(forest.root, 'task-forest', 'structure'):
            forest, identity, agent = self._leaf_context(payload, (
                'leaf_id', 'id', 'agent', 'caller', 'summary', 'artifact_paths',
                'operator_confirmed'))
            leaf = forest.resolve_leaf(identity)
            if not leaf.get('task_id'):
                raise ValueError('该任务还没有正式派发，不能提交成果')
            source, location = forest._message(leaf['task_id'])
            if source is None or location != 'processing' or source.to != agent:
                raise ValueError('当前成员没有持有这项任务；请由原认领会话提交或先走正式交接')
            if not source.result_to:
                raise ValueError('原任务没有明确的成果收件地址；请先核对任务记录')
            ticket = self._ticket(forest, leaf['id'], agent, payload)
            from .workspace_files import WorkspaceFiles
            files = WorkspaceFiles(forest.cfg.output_root)
            artifacts = [files.describe_artifact(path) for path in paths]
            artifacts = _result_artifacts(forest.cfg, source, artifacts)
            body = ('由项目操作人代录。此记录表示操作人确认成员报告完成，'
                    '不代表 AgentBus 独立验证了外部会话的执行过程。\n\n' + summary.strip())
            forest.bus.record_submission(agent, source, ticket, origin="操作人代录",
                heartbeat_timeout_s=forest.cfg.gates.get("heartbeat_timeout_s", 600))
            receipt = forest.bus.commit_result(
                agent, source, ticket, body=body, result_to=source.result_to,
                artifacts=artifacts, artifact_root=forest.cfg.output_root,
                exit_code=0)
            if not receipt.awaiting_audit:
                from .director import Director
                director = Director(forest.cfg, bus=forest.bus)
                director.resolve_pending_escalations(source.id, receipt.id)
                director.schedule_peer_review(source, agent, 'done')
            for key in [key for key in self._tickets if key[0] in (identity, leaf['id'])]:
                self._tickets.pop(key, None)
            forest.bus.audit(event='ui_operator_submitted_result', leaf_id=leaf['id'],
                             task_id=source.id, result_id=receipt.id, agent=agent,
                             operator='human', result_to=source.result_to,
                             artifact_count=len(artifacts))
        return {**self._snapshot({}), 'submitted_result_id': receipt.id,
                'result_to': source.result_to, 'operator_recorded': True,
                'submission_state': 'awaiting_audit' if receipt.awaiting_audit else 'delivered'}

    def _audit_record(self, payload):
        if not isinstance(payload.get('leaf_id'), str) or not payload['leaf_id'].strip():
            raise ValueError('请先选择待审任务')
        cfg = self._cfg()
        forest = TaskForest(cfg)
        self._machine_paths(cfg, self._mailboxes(cfg))
        leaf = next((row for row in forest.snapshot()['leaves']
                     if row['id'] == payload.get('leaf_id') or row.get('section_no') == payload.get('leaf_id')), None)
        if leaf is None or leaf.get('state') != 'awaiting_audit':
            raise ValueError('此任务不在等待独立审核状态，请刷新任务树。')
        policy = forest.bus._supervision_context()
        if not policy or not policy.get('strict_supervision'):
            raise ValueError('当前项目未启用独立审核')
        auditor = policy['designated_auditor']
        if 'agent' in payload and payload['agent'] != auditor:
            raise ValueError('只有指定监督员可以认领或提交审核，执行成员不能自审。')
        source = forest.bus._active_record(leaf['owner'], leaf['task_id'])
        case = source.supervision if source else {}
        if not isinstance(case, dict) or case.get('executor') == auditor:
            raise ValueError('审核成员与执行成员必须不同')
        review = forest.bus._active_record(auditor, case.get('review_task_id', ''))
        if review is None or review.type != 'review':
            archived = forest.bus.find_message(case.get('review_task_id', ''))
            if archived is not None and archived.type == 'review' and archived.status == 'done':
                raise ValueError('审核决定已归档，源成果收尾尚未完成。请由原执行成员在原认领会话使用原票据重试 answer，'
                                 '系统会复用已固定的审核决定；不要重新认领、改判或管理员放行。')
            raise ValueError('找不到对应的正式审核任务，请先核对审核信箱。')
        pending, evidence = forest.bus._validate_supervision_review_binding(source, review, case)
        return forest, leaf, review, pending, evidence, auditor

    def _audit_context(self, payload):
        self._fields(payload, ('leaf_id',))
        forest, leaf, review, pending, evidence, auditor = self._audit_record(payload)
        ready = forest.bus.dirs(auditor)['ready'] / (review.id + '.json')
        return {'leaf_id': leaf['id'], 'auditor': auditor, 'executor': leaf['owner'],
                'review_id': review.id, 'claimable': ready.is_file(),
                'can_submit': ('audit:' + review.id, auditor) in self._tickets,
                'can_approve': evidence.get('reviewable') is True,
                'evidence': _public(evidence), 'result_body': pending.body,
                'artifacts': _public(pending.artifacts)}

    def _claim_audit(self, payload):
        self._fields(payload, ('leaf_id', 'agent'))
        with task_lock(self._workspace / '.agentbus', 'task-forest', 'structure'):
            forest, leaf, review, _pending, _evidence, auditor = self._audit_record(payload)
            if payload.get('agent') != auditor:
                raise ValueError('请明确指定监督成员')
            key = ('audit:' + review.id, auditor)
            if key not in self._tickets:
                ready = forest.bus.dirs(auditor)['ready'] / (review.id + '.json')
                if not ready.is_file():
                    raise ValueError('审核已由其他窗口认领；请在原窗口提交，不从盘上补票据。')
                claimed = forest.bus.claim(auditor, ready)
                if claimed is None:
                    raise ValueError('审核已被其他窗口认领，请刷新。')
                self._tickets[key] = captured_ticket(claimed)
        return {**self._snapshot({}), 'audit': self._audit_context({'leaf_id': leaf['id']})}

    def _submit_audit(self, payload):
        self._fields(payload, ('leaf_id', 'agent', 'decision', 'reason', 'operator_confirmed',
                               'artifacts_reviewed', 'checks_reviewed', 'text_scan_reviewed'))
        if any(payload.get(key) is not True for key in ('operator_confirmed', 'artifacts_reviewed',
                                                       'checks_reviewed', 'text_scan_reviewed')):
            raise ValueError('请逐项核对产物、文本及运行检查，并确认已收到监督员结论。')
        decision, reason = payload.get('decision'), payload.get('reason')
        if decision not in ('approve', 'reject') or not isinstance(reason, str) or not reason.strip() or len(reason) > 3800:
            raise ValueError('请填写明确的批准或驳回结论及理由。')
        with task_lock(self._workspace / '.agentbus', 'task-forest', 'structure'):
            forest, leaf, review, pending, _evidence, auditor = self._audit_record(payload)
            if payload.get('agent') != auditor:
                raise ValueError('请明确指定监督成员')
            key = ('audit:' + review.id, auditor)
            ticket = self._tickets.get(key)
            if ticket is None:
                raise ValueError('本窗口没有原审核票据，请先认领审核或回到原认领窗口。')
            body = json.dumps({'schema_version': 1, 'task_id': leaf['task_id'], 'result_id': pending.id,
                               'decision': decision, 'reason': f'操作人代录监督员 {auditor} 的复核结论：{reason.strip()}',
                               'artifacts_reviewed': True, 'checks_reviewed': True, 'text_scan_reviewed': True},
                              ensure_ascii=False)
            forest.bus.record_submission(auditor, review, ticket, origin='桌面审核结论代录')
            receipt = forest.bus.commit_result(auditor, review, ticket, body=body,
                                               artifact_root=forest.cfg.output_root)
            forest.bus.audit(event='ui_operator_submitted_audit', agent=auditor, operator='human',
                             review_id=review.id, task_id=leaf['task_id'], decision=decision, result_id=receipt.id)
            self._tickets.pop(key, None)
        return {**self._snapshot({}), 'audit_decision': decision}

    def _reveal_artifact(self, payload):
        self._fields(payload, ('leaf_id', 'path'))
        identity, path = payload.get('leaf_id'), payload.get('path')
        if not isinstance(identity, str) or not identity:
            raise ValueError('请先选择任务')
        if not isinstance(path, str) or not path.strip():
            raise ValueError('产物路径为空')
        cfg = self._cfg()
        forest = TaskForest(cfg)
        leaf = next((item for item in forest.snapshot()['leaves']
                     if item.get('id') == identity or item.get('section_no') == identity), None)
        if leaf is None:
            raise ValueError('没有找到这个任务')
        if leaf.get('state') != 'delivered' or not leaf.get('result_id'):
            raise ValueError('只有已核验正式回执中的产物可以在资源管理器中定位')
        task = forest.bus.find_message(leaf.get('task_id'))
        receipt = forest.bus.find_message(leaf.get('result_id'))
        if (task is None or receipt is None or receipt.type != 'result'
                or receipt.reply_to != task.id or receipt.sender != task.to
                or receipt.to != task.result_to):
            raise ValueError('正式回执路由需要核对；未打开任何文件')
        artifact = next((item for item in receipt.artifacts
                         if isinstance(item, dict) and item.get('path') == path), None)
        if artifact is None:
            raise ValueError('该文件不在正式回执的产物清单中')
        from .workspace_files import WorkspaceFiles
        files = WorkspaceFiles(cfg.output_root)
        actual = files.describe_artifact(path)
        if (artifact.get('bytes') != actual['bytes']
                or artifact.get('sha256') != actual['sha256']):
            raise ValueError('产物内容已变化，与正式回执摘要不一致；未打开文件')
        target = files.workspace.joinpath(*actual['path'].split('/')).resolve(strict=True)
        if not target.is_relative_to(files.workspace) or not target.is_file():
            raise ValueError('产物路径已变化；未打开文件')
        if os.name != 'nt':
            raise ValueError('资源管理器定位仅支持 Windows 桌面版')
        subprocess.Popen(['explorer.exe', '/select,', str(target)], shell=False,
                         close_fds=True)
        return {'revealed': True, 'path': actual['path']}

    def _open_artifact(self, payload):
        self._fields(payload, ('leaf_id', 'path', 'app'))
        identity, path, app = payload.get('leaf_id'), payload.get('path'), payload.get('app', 'vscode')
        if not isinstance(identity, str) or not identity:
            raise ValueError('请先选择任务')
        if not isinstance(path, str) or not path.strip():
            raise ValueError('产物路径为空')
        if app not in ('vscode', 'cursor', 'system'):
            raise ValueError('仅支持在 VS Code、Cursor 或系统默认程序中打开')
        cfg = self._cfg()
        forest = TaskForest(cfg)
        leaf = next((item for item in forest.snapshot()['leaves']
                     if item.get('id') == identity or item.get('section_no') == identity), None)
        if leaf is None:
            raise ValueError('没有找到这个任务')
        from .workspace_files import WorkspaceFiles
        files = WorkspaceFiles(cfg.output_root)
        actual = files.describe_artifact(path)
        target = files.workspace.joinpath(*actual['path'].split('/')).resolve(strict=True)
        if not target.is_relative_to(files.workspace) or not target.is_file():
            raise ValueError('产物路径已变化或文件不存在；未打开')

        cmd = []
        if app == 'vscode':
            cmd = ['code', str(target)]
        elif app == 'cursor':
            cmd = ['cursor', str(target)]
        elif app == 'system':
            if os.name == 'nt':
                os.startfile(str(target))
                return {'opened': True, 'path': actual['path'], 'app': app}
            else:
                cmd = ['xdg-open', str(target)]

        if cmd:
            launcher = shutil.which(cmd[0])
            if not launcher:
                raise ValueError('没有找到所选编辑器，请先安装其桌面应用或选择其他打开方式。')
            native = Path(launcher)
            if native.suffix.casefold() in ('.cmd', '.bat'):
                # Never pass artifact names to a Windows command shim. Locate
                # the adjacent native application without parsing shim code.
                name = 'Code.exe' if app == 'vscode' else 'Cursor.exe'
                native = next((parent / name for parent in list(native.parents)[:4]
                               if (parent / name).is_file()), None)
                if native is None:
                    raise ValueError('仅找到编辑器命令脚本，未找到桌面 EXE；未通过命令解释器打开文件。')
            if os.name == 'nt' and native.suffix.casefold() != '.exe':
                raise ValueError('需要编辑器的原生桌面 EXE，不能执行命令脚本。')
            cmd[0] = str(native)
            try:
                subprocess.Popen(cmd, shell=False, close_fds=True)
            except Exception as exc:
                raise ValueError(f'打开外部程序失败: {exc}') from exc
        return {'opened': True, 'path': actual['path'], 'app': app}

    def _read_artifact_diff(self, payload):
        self._fields(payload, ('leaf_id', 'path'))
        identity, path = payload.get('leaf_id'), payload.get('path')
        if not isinstance(identity, str) or not identity:
            raise ValueError('请先选择任务')
        if not isinstance(path, str) or not path.strip():
            raise ValueError('产物路径为空')
        cfg = self._cfg()
        if not any(item.get('id') == identity or item.get('section_no') == identity
                   for item in TaskForest(cfg).snapshot()['leaves']):
            raise ValueError('没有找到这个任务')
        from .workspace_files import WorkspaceFiles
        files = WorkspaceFiles(cfg.output_root)
        relative, target, inspected = files._path(path)
        # Reuse the file API's identity checks and no-link policy. Preview has
        # a much smaller bound than the delivery artifact hashing limit.
        raw = files._raw(target, 8 * 1024 * 1024, snapshot=inspected)
        actual = {'path': relative, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        suffix = target.suffix.casefold()
        mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
                '.ico': 'image/x-icon'}.get(suffix)
        if mime:
            signatures = {
                'image/png': raw.startswith(b'\x89PNG\r\n\x1a\n'),
                'image/jpeg': raw.startswith(b'\xff\xd8\xff'),
                'image/gif': raw.startswith((b'GIF87a', b'GIF89a')),
                'image/webp': raw.startswith(b'RIFF') and raw[8:12] == b'WEBP',
                'image/bmp': raw.startswith(b'BM'),
                'image/x-icon': raw.startswith(b'\x00\x00\x01\x00'),
            }
            if not signatures[mime]:
                raise ValueError('图片内容与扩展名不符，未载入预览')
            return {**actual, 'content': '', 'diff': '', 'is_image': True,
                    'image_src': f'data:{mime};base64,' + base64.b64encode(raw).decode('ascii')}
        # SVG is displayed as text: no scripts, remote fonts or linked images.
        content = raw.decode('utf-8', errors='replace')

        # 检查是否是 git 仓库，尝试获取 HEAD 对比
        diff_text = ''
        try:
            res = subprocess.run(['git', 'diff', 'HEAD', '--', str(target)],
                                 cwd=str(files.workspace), capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                diff_text = res.stdout
        except Exception:
            diff_text = ''

        return {
            'path': actual['path'],
            'bytes': actual['bytes'],
            'sha256': actual['sha256'],
            'content': content if len(content) <= 500000 else content[:500000] + '\n\n... (内容已截断)',
            'diff': diff_text,
            'is_image': False,
        }

    def _recover_task(self, payload):
        self._fields(payload, ('task_id', 'agent', 'owner_confirmed_inactive', 'expected_claim_epoch'))
        task_id = payload.get('task_id')
        agent = payload.get('agent')
        confirmed = payload.get('owner_confirmed_inactive')
        if confirmed is not True:
            raise ValueError('恢复前必须明确确认原执行者已停止；owner_confirmed_inactive 需为 true')
        if not task_id or not isinstance(task_id, str):
            raise ValueError('必须提供有效 task_id')
        if not agent or not isinstance(agent, str):
            raise ValueError('必须提供执行成员 agent')
        cfg = self._cfg()
        bus = Bus(cfg.workspace / '.agentbus')
        expected_epoch = payload.get('expected_claim_epoch')
        if type(expected_epoch) is not int or expected_epoch < 1:
            raise ValueError('缺少认领代次，请重新打开任务并确认')
        recovered = bus.recover_claim(agent, task_id, owner_confirmed_inactive=confirmed,
                                      expected_claim_epoch=expected_epoch)
        return {
            'recovered': True,
            'task_id': task_id,
            'agent': agent,
            'status': recovered.status,
            'message': f'任务 {task_id} 已由 {agent} 的 processing 恢复并重新投递至待办'
        }

    def _console_mirror(self, payload):
        self._fields(payload, ('limit',))
        limit = payload.get('limit', 50)
        if not isinstance(limit, int) or not (1 <= limit <= 200):
            limit = 50
        events = PublicFiles(self._workspace).recent_events()[-limit:]
        lines = []
        for e in events:
            raw_ts = e.get('ts') or ''
            ts = raw_ts[11:19] if len(raw_ts) >= 19 else '--:--:--'
            evt = str(e.get('event') or e.get('type') or 'info').upper()
            agent = str(e.get('agent') or e.get('caller') or 'system')
            task = str(e.get('task_id') or e.get('leaf_id') or '')
            reason = str(e.get('reason') or e.get('reason_code') or e.get('note') or '')
            text = f"[{ts}] [{evt}] [{agent}]"
            if task:
                text += f" task={task}"
            if reason:
                text += f" - {reason}"
            level = 'warn' if any(w in evt.lower() for w in ('stale', 'timeout', 'blocked', 'error', 'reject')) else 'info'
            lines.append({'text': text, 'level': level, 'ts': e.get('ts')})
        return {'lines': lines, 'count': len(lines)}

    def _export_canvas_image(self, payload):
        self._fields(payload, ('path', 'format', 'data_base64', 'width', 'height', 'save'))
        fmt = str(payload.get('format', 'png')).lower()
        if fmt not in ('png', 'svg'):
            raise ValueError('导出格式仅支持 png 或 svg')
        data_base64 = payload.get('data_base64')
        if not data_base64 or not isinstance(data_base64, str):
            raise ValueError('缺少有效的图像或矢量数据')

        # 如果是 data URL，去除前缀
        if ',' in data_base64:
            data_base64 = data_base64.split(',', 1)[1]

        import base64
        try:
            raw = base64.b64decode(data_base64)
        except Exception as exc:
            raise ValueError(f'图像 Base64 数据解码失败: {exc}') from exc

        if len(raw) > 50 * 1024 * 1024:
            raise ValueError('导出图像大小超出 50MB 上限')

        digest = hashlib.sha256(raw).hexdigest()
        filename = f"task_forest_4k_{now_iso()[:10].replace('-', '')}_{uuid.uuid4().hex[:6]}.{fmt}"
        result = {
            'format': fmt,
            'bytes': len(raw),
            'sha256': digest,
            'filename': filename,
            'width': payload.get('width', 3840),
            'height': payload.get('height', 2160),
            'path': None,
            'cancelled': False,
        }

        save_requested = payload.get('save', True)
        selected = payload.get('path')
        if selected is None and save_requested:
            # 尝试通过原生窗口弹出保存对话框
            if self._window is not None:
                import webview
                dialogs = getattr(webview, 'FileDialog', None)
                kind = getattr(dialogs, 'SAVE', None) or getattr(webview, 'SAVE_DIALOG', None)
                file_types = ('PNG 图像 (*.png)',) if fmt == 'png' else ('SVG 矢量图 (*.svg)',)
                selected = self._window.create_file_dialog(kind, directory=str(self._workspace),
                                                          save_filename=filename, file_types=file_types)
                if isinstance(selected, (tuple, list)):
                    selected = selected[0] if selected else None
            else:
                # 默认写入工作空间下的成果目录或当前目录
                target_dir = self._workspace / '成果'
                target_dir.mkdir(parents=True, exist_ok=True)
                selected = str(target_dir / filename)

        if not selected:
            result['cancelled'] = True
            return result

        target = Path(selected)
        temp = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

        result['path'] = str(target)
        return result

    def _show_desktop_notification(self, payload):
        self._fields(payload, ('title', 'message', 'sound', 'task_id'))
        title = str(payload.get('title', 'AgentBus 任务交付提醒')).strip()[:100]
        message = str(payload.get('message', '任务已正式交付')).strip()[:300]
        requested = payload.get('sound', True)
        if type(requested) is not bool:
            raise ValueError('sound must be a boolean')
        from .desktop_wakeup import DesktopWakeupBridge
        native = DesktopWakeupBridge(self._cfg())
        sound_ok, sound_error = native.play_chime() if requested else (False, '')
        toast_ok, toast_error = native.show_toast(title, message)
        return {'notified': toast_ok, 'title': title, 'message': message,
                'task_id': payload.get('task_id', ''), 'sound_played': sound_ok,
                'sound': {'requested': requested, 'completed': sound_ok, 'error': sound_error},
                'toast': {'requested': True, 'completed': toast_ok, 'error': toast_error}}

    def _scan_project_trees(self, payload):
        self._fields(payload, ())
        root = self._workspace
        candidates, seen, budget = [], set(), {'files': 0, 'bytes': 0}
        from .tree_templates import adapt_external_tree, preview_template

        def add(path, *, active=False):
            if len(candidates) >= 64 or budget['files'] >= 160:
                return
            item = {'path': path.relative_to(root).as_posix(), 'active': active,
                    'bytes': 0, 'title': path.stem, 'root_count': 0,
                    'branch_count': 0, 'leaf_count': None, 'warnings': [], 'error': None}
            try:
                info = path.lstat()
                if (path.is_symlink() or not path.is_file()
                        or getattr(info, 'st_file_attributes', 0) & 0x400
                        or info.st_size > 8 * 1024 * 1024
                        or budget['bytes'] + info.st_size > 32 * 1024 * 1024):
                    return
                relative = path.relative_to(root).as_posix()
                if relative in seen:
                    return
                seen.add(relative)
                budget['files'] += 1
                budget['bytes'] += info.st_size
                item.update(path=relative, bytes=info.st_size)
                if active:
                    view = TaskForest(self._cfg()).snapshot()
                    item.update(title=(view.get('roots') or [{}])[0].get('title', '当前任务树'),
                                root_count=len(view.get('roots', [])),
                                branch_count=len(view.get('branches', [])),
                                leaf_count=len(view.get('leaves', [])),
                                warnings=['这是当前项目正在使用的任务树。'])
                else:
                    raw = PublicFiles(root).read_bytes(relative, limit=8 * 1024 * 1024)
                    if raw is None:
                        raise ValueError('任务树文件已移走')
                    def unique(pairs):
                        result = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError('JSON 中存在重复字段')
                            result[key] = value
                        return result
                    document = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique)
                    document, warnings = adapt_external_tree(document)
                    preview = preview_template(document)
                    item.update(title=preview.get('title') or document.get('title') or path.stem,
                                root_count=preview.get('roots', 0),
                                branch_count=preview.get('branches', 0),
                                leaf_count=preview.get('leaves', 0), warnings=warnings)
            except json.JSONDecodeError as exc:
                item['error'] = f'JSON 格式有误（第 {exc.lineno} 行，第 {exc.colno} 列）'
            except (OSError, UnicodeError, ValueError) as exc:
                item['error'] = str(exc)[:240]
            candidates.append(item)

        # The active tree is an exact path, not a recursive scan of control data.
        active = root / '.agentbus' / 'task-tree.json'
        if active.exists():
            add(active, active=True)
        for entry in root.iterdir():
            if entry.is_file() and entry.name.casefold().endswith('.tree.json'):
                add(entry)

        # Only these public project folders are searched. Symlinks, hidden/private
        # paths and arbitrary application/config directories are not traversed.
        for folder_name in ('templates', '成果'):
            base = root / folder_name
            try:
                base_info = base.lstat()
                if (base.is_symlink() or not base.is_dir()
                        or getattr(base_info, 'st_file_attributes', 0) & 0x400):
                    continue
                stack = [(base, 0)]
                while stack and len(candidates) < 64 and budget['files'] < 160:
                    directory, depth = stack.pop()
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            candidate = Path(entry.path)
                            try:
                                if (entry.is_symlink()
                                        or getattr(entry, 'is_junction', lambda: False)()):
                                    continue
                                if entry.is_file(follow_symlinks=False):
                                    if entry.name.casefold().endswith('.tree.json'):
                                        add(candidate)
                                elif (depth < 3 and entry.is_dir(follow_symlinks=False)
                                      and not entry.name.startswith('.')
                                      and entry.name.casefold() not in _PRIVATE):
                                    stack.append((candidate, depth + 1))
                            except OSError:
                                continue
            except OSError:
                continue
        return {'candidates': candidates,
                'truncated': budget['files'] >= 160 or len(candidates) >= 64,
                'scanned_files': budget['files'], 'scanned_bytes': budget['bytes']}

    def _viewport_workspace(self, payload):
        self._fields(payload, ('workspace',))
        workspace = _directory(payload.get('workspace'))
        if workspace != self._workspace:
            raise ValueError('本地界面状态只能读取或保存到当前项目')
        return workspace

    def _load_viewport(self, payload):
        workspace = self._viewport_workspace(payload)
        return {'viewport': load_viewport(workspace)}

    def _save_viewport(self, payload):
        self._fields(payload, ('workspace', 'viewport'))
        workspace = self._viewport_workspace({'workspace': payload.get('workspace')})
        if not save_viewport(workspace, payload.get('viewport')):
            raise ValueError('视野位置未能保存；本次运行仍会保留当前视图')
        return {'saved': True}

    def _load_branch_folds(self, payload):
        workspace = self._viewport_workspace(payload)
        return {'branch_ids': load_branch_folds(workspace)}

    def _save_branch_folds(self, payload):
        self._fields(payload, ('workspace', 'branch_ids'))
        workspace = self._viewport_workspace({'workspace': payload.get('workspace')})
        if not save_branch_folds(workspace, payload.get('branch_ids')):
            raise ValueError('分支折叠状态无效或未能保存')
        return {'saved': True, 'branch_ids': load_branch_folds(workspace)}

    def _load_notes(self, payload):
        """§7.25 画布便签是设备本地偏好；这里只读本机 UI 状态，不读项目共享文件。"""
        workspace = self._viewport_workspace(payload)
        return {'notes': load_notes(workspace)}

    def _save_notes(self, payload):
        self._fields(payload, ('workspace', 'notes'))
        workspace = self._viewport_workspace({'workspace': payload.get('workspace')})
        if not save_notes(workspace, payload.get('notes')):
            raise ValueError('便签数据无效或未能保存（数量/字段/范围超限）')
        return {'saved': True, 'notes': load_notes(workspace)}

    def _load_priorities(self, payload):
        """§7.30 卡片视觉优先级标记是设备本地偏好；从本机 UI 状态读取。"""
        workspace = self._viewport_workspace(payload)
        return {'priorities': load_priorities(workspace)}

    def _save_priorities(self, payload):
        self._fields(payload, ('workspace', 'priorities'))
        workspace = self._viewport_workspace({'workspace': payload.get('workspace')})
        if not save_priorities(workspace, payload.get('priorities')):
            raise ValueError('卡片优先级数据无效或未能保存（格式/条目超限）')
        return {'saved': True, 'priorities': load_priorities(workspace)}

    @staticmethod
    def _wake_after_dispatch(service, member, task_id, **options):
        outcome = service.trigger_for_member(member, task_id, **options)
        status = outcome.get('status')
        if status == 'ok':
            # The head of a busy member's FIFO may be an older assignment.
            if outcome.get('notified_task_id') != task_id:
                return 'waiting'
            submitted = outcome.get('submit_status')
            if submitted == 'accepted' and outcome.get('accepted_by_host') is True:
                return 'accepted'
            if submitted == 'notified':
                return 'notified'
            if submitted == 'queued':
                return 'waiting'
        if status in ('duplicate', 'queued', 'idle', 'no_bound_contact', 'no_ready_task'):
            return 'waiting'
        return 'failed'

    def _dispatch_leaf(self, payload):
        forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller'))
        with task_lock(forest.root, 'task-forest', 'structure'):
            forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller'))
            leaf = forest.resolve_leaf(identity)
            leaf_id = leaf['id']
            message = forest.dispatch_leaf(leaf_id, agent)
            refreshed = forest.resolve_leaf(leaf_id)
            prompt = task_prompt(forest.cfg, refreshed, agent)['prompt']
        result = self._snapshot({})
        result['dispatched_task_id'] = message.id
        result['dispatched_to'] = message.to
        result['prompt'] = prompt
        result['model_calls'] = 0
        result['automatic_execution'] = False
        try:
            from .trigger.service import TriggerDeliveryService
            result['wake_status'] = self._wake_after_dispatch(TriggerDeliveryService(forest.cfg.workspace), message.to, message.id)
        except Exception as exc:
            # Task is dispatched on disk; only the optional wake-up failed.
            result['wake_status'] = 'failed'
            result['wake_error'] = f'任务已派发，但未能通知成员：{exc}'[:300]
        return result

    def _task_prompt(self, payload):
        forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller'))
        leaf = forest.resolve_leaf(identity)
        return {**task_prompt(forest.cfg, leaf, agent), 'workspace': str(forest.cfg.workspace)}

    def _dispatch_ready(self, payload):
        self._fields(payload, ())
        cfg = self._cfg()
        mode = assignment_mode(cfg.data)
        if mode == 'autonomous':
            raise ValueError('当前处于自由调度（智能体自主接活）模式，无需集中批量派发；智能体空闲时执行 agentbus claim-next 即可开工')
        elif mode != 'automatic':
            raise ValueError('当前采用架构师定向分派策略；请选定目标任务节点并指定责任成员')
        self._machine_paths(cfg, self._mailboxes(cfg))
        forest = TaskForest(cfg)
        assigned, waiting, blocked, assignments = 0, 0, 0, []
        limit = min(MAX_AUTOMATIC_BATCH, int(cfg.gates.get('max_tasks', MAX_AUTOMATIC_BATCH)))
        # A shared structure lock serializes this batch with STOP and config.
        # dispatch_leaf reacquires the same reentrant key, never claims a task.
        with task_lock(cfg.workspace / '.agentbus', 'task-forest', 'structure'):
            cfg = self._cfg()
            mode = assignment_mode(cfg.data)
            if mode == 'autonomous':
                raise ValueError('当前处于自由调度（智能体自主接活）模式，无需集中批量派发；智能体空闲时执行 agentbus claim-next 即可开工')
            elif mode != 'automatic':
                raise ValueError('调度策略已切换为定向分派；本次自适应批量流转已安全挂起')
            forest = TaskForest(cfg)
            view = forest.snapshot()
            if view['stopped']:
                raise ValueError('项目已暂停；恢复后才能派发新任务')
            records = self._mailboxes(cfg)
            cards = {}
            for member in cfg.executor_workers():
                try:
                    cards[member] = PublicFiles(cfg.workspace).read_json(f'.agentbus/member-cards/{member}.json')
                except (ValueError, OSError):
                    cards[member] = {}
            for leaf in view['leaves']:
                if leaf.get('task_id'):
                    waiting += int(leaf.get('state') != 'delivered')
                    continue
                if leaf.get('state') != 'queued' or not leaf.get('claimable'):
                    if (leaf.get('state') == 'blocked' and leaf.get('unmet_dependencies')
                            and ('前序' in leaf.get('evidence_error', '') or not leaf.get('evidence_error'))):
                        waiting += 1
                    else:
                        blocked += 1
                    continue
                if assigned >= limit:
                    waiting += 1
                    continue
                suggestion = recommend(cfg, leaf, records, cards=cards)
                member = suggestion['recommended_agent']
                if not member or member not in leaf.get('eligible_agents', []):
                    blocked += 1
                    continue
                message = forest.dispatch_leaf(leaf['id'], member)
                assigned += 1
                records.setdefault(member, {}).setdefault('ready', []).append(message)
                assignments.append({'leaf_id': leaf['id'], 'section_no': leaf.get('section_no'),
                                    'task_id': message.id, 'agent': member})
        result = self._snapshot({})
        result['dispatch_summary'] = {'assigned': assigned, 'waiting': waiting, 'blocked': blocked}
        result['assignments'] = assignments
        result['model_calls'] = 0
        result['automatic_execution'] = False
        result['file_event_only'] = True
        try:
            from .trigger.service import TriggerDeliveryService
            tds = TriggerDeliveryService(cfg.workspace)
            tds.reset_circuit_breaker()
            for item in assignments:
                item['wake_status'] = self._wake_after_dispatch(tds, item['agent'], item['task_id'], count_hop=False)
            if assignments:
                tds.record_hop()
            statuses = {item['wake_status'] for item in assignments}
            result['wake_status'] = (next(iter(statuses)) if len(statuses) == 1 else 'mixed' if statuses else 'none')
        except Exception as exc:
            result['wake_status'] = 'failed'
            result['wake_error'] = f'任务已派发，但未能通知成员：{exc}'[:300]
        result['dispatch_truth'] = {
            'frontend_state': 'foreground_only',
            'background_policy': 'paused_on_hidden_or_minimize',
            'model_execution': 'manual_or_external_only',
            'file_event_only': True,
        }
        return result

    def _tree_templates(self, payload):
        self._fields(payload, ())
        from .tree_templates import builtin_templates
        return {'templates': [{key: item[key] for key in ('id', 'title', 'description')}
                              for item in builtin_templates()]}

    def _file_dialog(self, *, saving=False, member_icon=False, markdown=False, delivery=False):
        if self._window is None:
            raise ValueError('当前没有原生窗口；请明确选择一个任务树模板文件路径')
        import webview
        dialogs = getattr(webview, 'FileDialog', None)
        kind = getattr(dialogs, 'SAVE' if saving else 'OPEN', None)
        if kind is None:
            kind = getattr(webview, 'SAVE_DIALOG' if saving else 'OPEN_DIALOG')
        if markdown:
            file_types = ('Markdown 文档 (*.md)',)
        elif delivery:
            file_types = ('已验收成果 (*.zip)',)
        elif member_icon:
            file_types = ('Windows 应用程序 (*.exe)',)
        else:
            file_types = ('任务树 JSON (*.tree.json;*.json)',)
        kwargs = {'allow_multiple': False, 'directory': str(self._workspace),
                  'file_types': file_types}
        if saving:
            kwargs['save_filename'] = '项目进度与交付归档.md' if markdown else ('已验收成果.zip' if delivery else 'project.tree.json')
        result = self._window.create_file_dialog(kind, **kwargs)
        return (result[0] if isinstance(result, (tuple, list)) else result) if result else None

    @staticmethod
    def _template_path(value):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError('请选择任务树模板文件')
        text = str(value)
        path = Path(text)
        win = PureWindowsPath(text)
        if (not path.is_absolute() or text.startswith('\\\\') or '://' in text
                or any(ord(c) < 32 for c in text) or '..' in path.parts or '..' in win.parts
                or any(part.rstrip(' .').casefold() in _PRIVATE for part in [*path.parts, *win.parts])
                or not path.name.casefold().endswith('.tree.json')):
            raise ValueError('模板必须是公开本地目录中的 .tree.json 文件，不能进入控制目录或私有目录')
        parent = _directory(path.parent)
        return PublicFiles(parent).checked(path.name)

    @staticmethod
    def _import_path(value):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError('请选择任务树 JSON 文件')
        text = str(value)
        path = Path(text)
        win = PureWindowsPath(text)
        if (not path.is_absolute() or text.startswith('\\\\') or '://' in text
                or any(ord(c) < 32 for c in text) or '..' in path.parts or '..' in win.parts
                or any(part.rstrip(' .').casefold() in _PRIVATE for part in [*path.parts, *win.parts])
                or not path.name.casefold().endswith('.json')):
            raise ValueError('任务树必须是公开本地目录中的 JSON 文件，不能进入控制或私有目录')
        parent = _directory(path.parent)
        return PublicFiles(parent).checked(path.name)

    def _export_tree(self, payload):
        self._fields(payload, ('path',))
        selected = payload.get('path') or self._file_dialog(saving=True)
        if selected is None:
            return {'cancelled': True}
        target = self._template_path(selected)
        from .tree_templates import export_template, preview_template
        cfg = self._cfg()
        self._machine_paths(cfg, self._mailboxes(cfg))
        document = export_template(TaskForest(cfg))
        raw = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        if target.exists() and not target.is_file():
            raise ValueError('模板目标必须是普通文件')
        temp = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._template_path(target)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        preview = preview_template(document)
        return {'path': str(target), 'node_count': preview['node_count'],
                'warnings': preview.get('warnings', []),
                'description': document.get('description', ''), 'cancelled': False}

    @staticmethod
    def _handover_path(value):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError('请选择交接归档的保存位置')
        text = str(value)
        path = Path(text)
        win = PureWindowsPath(text)
        if (not path.is_absolute() or text.startswith('\\\\') or '://' in text
                or any(ord(c) < 32 for c in text) or '..' in path.parts or '..' in win.parts
                or any(part.rstrip(' .').casefold() in _PRIVATE for part in [*path.parts, *win.parts])
                or not path.name.casefold().endswith('.md')):
            raise ValueError('归档必须是公开本地目录中的 .md 文件，不能进入控制目录或私有目录')
        parent = _directory(path.parent)
        return PublicFiles(parent).checked(path.name)

    def _export_delivery(self, payload):
        from .cli import export_verified_delivery
        self._fields(payload, ('path',))
        cfg = self._cfg()
        forest = TaskForest(cfg)
        leaves = forest.snapshot().get('leaves', [])
        if not leaves or any(leaf.get('state') != 'delivered' for leaf in leaves):
            raise ValueError('还有任务尚未交付。全部通过验收后即可导出。')
        files = {item['path'] for leaf in leaves for item in leaf.get('artifacts', [])}
        selected = payload.get('path') or self._file_dialog(saving=True, delivery=True, markdown=not files)
        if selected is None:
            return {'cancelled': True}
        path = Path(selected)
        suffix = '.zip' if files else '.md'
        if not path.is_absolute() or path.suffix.casefold() != suffix:
            raise ValueError(f'请选择 {suffix} 文件的完整保存位置')
        target = PublicFiles(_directory(path.parent)).checked(path.name)
        if target.name.rstrip(' .').casefold() in _PRIVATE:
            raise ValueError('请选择公开的成果保存位置')
        lines = []
        code = export_verified_delivery(cfg, forest, export_target=target if files else None,
            summary_target=None if files else target, emit=lines.append)
        if code:
            raise ValueError('未生成交付文件。' + '\\n'.join(lines[-5:]))
        return {'cancelled': False, 'path': str(target), 'kind': 'archive' if files else 'summary'}

    def _export_handover(self, payload):
        """Render the live forest as a Markdown handover digest.

        Read-only unless the caller explicitly asks to save: rendering never
        writes, never dispatches and never upgrades a task to delivered.
        """
        self._fields(payload, ('path', 'save'))
        save_requested = payload.get('save', False)
        if type(save_requested) is not bool:
            raise ValueError('保存选项必须是布尔值')
        cfg = self._cfg()
        records = self._mailboxes(cfg)
        self._machine_paths(cfg, records)
        from .handover import build_handover_markdown, handover_filename
        view = self._snapshot({})
        text = build_handover_markdown(
            view,
            project_name=(view.get('project') or {}).get('name') or self._workspace.name,
            workspace=str(cfg.workspace), output_root=str(cfg.output_root),
            generated_at=now_iso(), stopped=bool(view.get('stopped')))
        raw = text.encode('utf-8')
        result = {'markdown': text, 'filename': handover_filename(),
                  'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                  'node_count': len(view.get('leaves') or []),
                  'path': None, 'cancelled': False}
        selected = payload.get('path')
        if selected is None and save_requested:
            selected = self._file_dialog(saving=True, markdown=True)
            if selected is None:
                result['cancelled'] = True
                return result
        if selected is None:
            return result
        target = self._handover_path(selected)
        if target.exists() and not target.is_file():
            raise ValueError('归档目标必须是普通文件')
        temp = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._handover_path(target)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        result['path'] = str(target)
        return result

    @staticmethod
    def _template_digest(document):
        canonical = json.dumps(document, ensure_ascii=False, sort_keys=True,
                                separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest()

    def _remember_import_preview(self, *, path, template_id, digest, impact_revision=None):
        now = time.monotonic()
        for key in [key for key, item in self._import_previews.items()
                    if now - item['created'] > _IMPORT_PREVIEW_TTL_S]:
            self._import_previews.pop(key, None)
        while len(self._import_previews) >= _IMPORT_PREVIEW_LIMIT:
            self._import_previews.popitem(last=False)
        identity = uuid.uuid4().hex
        self._import_previews[identity] = {
            'workspace': str(self._workspace.resolve()),
            'path': str(path) if path is not None else None,
            'template_id': template_id,
            'digest': digest,
            'created': now,
            'impact_revision': impact_revision,
        }
        return identity

    def _consume_import_preview(self, preview_id, *, path, template_id, digest):
        if not isinstance(preview_id, str) or not preview_id:
            raise ValueError('请先预览这份任务树，再从预览页确认导入。')
        binding = self._import_previews.pop(preview_id, None)
        if binding is None or time.monotonic() - binding['created'] > _IMPORT_PREVIEW_TTL_S:
            raise ValueError('任务树预览已过期或已使用，请重新预览后确认。')
        if (binding['workspace'] != str(self._workspace.resolve())
                or binding['path'] != (str(path) if path is not None else None)
                or binding['template_id'] != template_id):
            raise ValueError('确认来源与预览不一致，请重新预览后确认。')
        if binding['digest'] != digest:
            raise ValueError('任务树文件在预览后发生变化；为避免导入未核对内容，请重新预览。')
        return binding

    @staticmethod
    def _import_outline(document, limit=120):
        labels = {node['id']: node['title'] for kind in ('roots', 'branches', 'leaves')
                  for node in document.get(kind, [])}
        rows = []
        for kind, level, label in (('roots', 0, '项目目标'), ('branches', 1, '工作分支'),
                                   ('leaves', 2, '具体任务')):
            for node in document.get(kind, []):
                if len(rows) >= limit:
                    total = sum(len(document.get(name, [])) for name in ('roots', 'branches', 'leaves'))
                    return {'items': rows, 'truncated': max(0, total - len(rows))}
                dependency_ids = node.get('dependencies', [])
                dependencies = [labels[item] for item in dependency_ids if item in labels]
                artifacts = list(node.get('expected_artifacts', []))
                rows.append({
                    'level': level, 'kind': label, 'title': node.get('title', '')[:200],
                    'goal': node.get('goal', '')[:600],
                    'parent': labels.get(node.get('parent_id'), ''),
                    'dependencies': dependencies[:12],
                    'dependency_count': len(dependency_ids),
                    'dependencies_truncated': max(0, len(dependencies) - 12),
                    'expected_artifacts': artifacts[:8],
                    'artifact_count': len(artifacts),
                    'artifacts_truncated': max(0, len(artifacts) - 8),
                })
        return {'items': rows, 'truncated': 0}

    def _import_tree(self, payload):
        self._fields(payload, ('path', 'template_id', 'confirm', 'preview_id', 'replace', 'replace_existing'))
        confirm = payload.get('confirm', False)
        replace = payload.get('replace', payload.get('replace_existing', False))
        if type(confirm) is not bool:
            raise ValueError('模板导入确认必须是布尔值')
        if type(replace) is not bool:
            raise ValueError('请选择追加或替换，替换选项必须是布尔值')
        if not confirm and payload.get('preview_id') is not None:
            raise ValueError('请重新选择任务树并打开新的预览。')
        if payload.get('path') and payload.get('template_id'):
            raise ValueError('请选择一个模板文件或一个内置模板')
        from .tree_templates import (builtin_templates, adapt_external_tree, preview_template,
                                     import_template, replacement_impact)
        template_id = payload.get('template_id')
        path = None
        digest = None
        adapter_warnings = []
        if template_id is not None:
            if not isinstance(template_id, str):
                raise ValueError('内置模板标识必须是文本')
            match = next((t for t in builtin_templates() if t['id'] == template_id), None)
            if match is None:
                raise ValueError('没有这个内置任务树模板')
            document = match['document']
            digest = self._template_digest(document)
        else:
            selected = payload.get('path') or self._file_dialog()
            if selected is None:
                return {'cancelled': True}
            path = self._import_path(selected)
            # Allow the pretty-printed representation of a bounded template;
            # validate_template still enforces the canonical document budget.
            raw = PublicFiles(path.parent).read_bytes(path.name, limit=8 * 1024 * 1024)
            if raw is None:
                raise ValueError('任务树模板文件不存在')
            digest = hashlib.sha256(raw).hexdigest()
            def unique(pairs):
                data = {}
                for key, value in pairs:
                    if key in data:
                        raise ValueError('模板含重复 JSON 字段')
                    data[key] = value
                return data
            try:
                document = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique)
            except UnicodeDecodeError as exc:
                raise ValueError('无法读取任务树文件：请将文件保存为 UTF-8 JSON。') from exc
            except json.JSONDecodeError as exc:
                raise ValueError(f'任务树 JSON 格式有误（第 {exc.lineno} 行，第 {exc.colno} 列）。') from exc
            document, adapter_warnings = adapt_external_tree(document)
        preview = preview_template(document)
        if adapter_warnings:
            preview['warnings'] = list(dict.fromkeys([*preview.get('warnings', []), *adapter_warnings]))
            preview['source_format'] = '通用 JSON 任务树（已安全转换）'
        preview['leaf_count'] = preview['leaves']
        context = {'preview': preview, 'outline': self._import_outline(document),
                   'impact': replacement_impact(TaskForest(self._cfg())),
                   'document': document, 'path': str(path) if path else None,
                   'template_id': template_id, 'source_sha256': digest, 'cancelled': False}
        if not confirm:
            context['preview_id'] = self._remember_import_preview(
                path=path, template_id=template_id, digest=digest,
                impact_revision=context['impact']['revision'])
            return context
        binding = self._consume_import_preview(payload.get('preview_id'), path=path,
                                              template_id=template_id, digest=digest)
        cfg = self._cfg()
        self._machine_paths(cfg, self._mailboxes(cfg))
        replacement = {}
        import_template(TaskForest(cfg), document, replace=replace, replacement_info=replacement,
                        expected_impact=binding['impact_revision'] if replace else None)
        if replace:
            # This API reports a boolean failure itself. Do not swallow unrelated
            # programming errors, or race the frontend's explicit snapshot refresh.
            if not save_branch_folds(cfg.workspace, []):
                preview.setdefault('warnings', []).append('任务树已导入，但未能保存折叠状态。')
        return {**self._snapshot({}), 'imported': True,
                'preview': preview, 'path': context['path'], 'template_id': template_id,
                'import_mode': 'replace' if replace else 'append', **replacement}

    def _export_backup(self, payload):
        self._fields(payload, ('path', 'preview_only'))
        preview_only = payload.get('preview_only', False)
        from .backup import create_backup_bundle, preview_backup_export
        preview = preview_backup_export(self._workspace)
        if preview_only:
            return preview
        selected = payload.get('path') or self._file_dialog(saving=True)
        if selected is None:
            return {'cancelled': True}
        target = self._import_path(selected)
        bundle = create_backup_bundle(self._workspace)
        raw = (json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
        if target.exists() and not target.is_file():
            raise ValueError('备份目标必须是普通文件')
        temp = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return {
            'path': str(target),
            'bundle_sha256': bundle['bundle_sha256'],
            'manifest': preview['manifest'],
            'node_count': preview['node_count'],
            'cancelled': False,
        }

    def _restore_backup(self, payload):
        self._fields(payload, ('path', 'bundle', 'confirm', 'preview_id', 'mode', 'confirm_replace'))
        confirm = payload.get('confirm', False) is True
        bundle = payload.get('bundle')
        path_str = payload.get('path')
        from .backup import preview_backup_restore, apply_backup_restore, MAX_BACKUP_BYTES, DEFAULT_RESTORE_MODE
        mode = payload.get('mode', DEFAULT_RESTORE_MODE)
        if bundle is None:
            if not path_str:
                selected = self._file_dialog()
                if selected is None:
                    return {'cancelled': True}
                path_str = str(selected)
            path = self._import_path(path_str)
            raw = PublicFiles(path.parent).read_bytes(path.name, limit=MAX_BACKUP_BYTES)
            if raw is None:
                raise ValueError('备份文件不存在或无法读取')
            try:
                bundle = json.loads(raw.decode('utf-8-sig'))
            except Exception as exc:
                raise ValueError(f'备份文件 JSON 解析失败: {exc}') from exc

        if confirm is not True:
            preview = preview_backup_restore(self._workspace, bundle, mode=mode)
            return {
                'preview': preview,
                'path': path_str,
                'cancelled': False,
            }
        result = apply_backup_restore(self._workspace, bundle, preview_id=payload.get('preview_id'),
                                      mode=mode, confirm_replace=payload.get('confirm_replace') is True)
        return {
            **self._snapshot({}),
            'restored': True,
            'result': result,
            'path': path_str,
            'cancelled': False,
        }

    def _ticket(self, forest, identity, agent, payload):
        ticket = self._tickets.get((identity, agent))
        if ticket is None and isinstance(identity, str):
            try:
                leaf = forest.resolve_leaf(identity)
                ticket = self._tickets.get((leaf['id'], agent))
            except Exception:
                pass
        if ticket is None and 'ticket' in payload:
            ticket = normalize(payload['ticket'])
        if ticket is None:
            raise ValueError('当前工作台没有原认领票据；回到原会话继续')
        if 'ticket' in payload and normalize(payload['ticket']) != ticket:
            raise ValueError('所传票据与本地原认领票据不一致')
        return ticket

    def _checkpoint_leaf(self, payload):
        forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller', 'note', 'progress', 'ticket'))
        leaf = forest.resolve_leaf(identity)
        ticket = self._ticket(forest, leaf['id'], agent, payload)
        return _public(forest.checkpoint_leaf(leaf['id'], agent, payload.get('note'), ticket,
                                              progress=payload.get('progress')))

    def _handoff_leaf(self, payload):
        forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller',
            'reason', 'successor', 'ticket', 'owner_confirmed_inactive', 'expected_task_id', 'expected_owner', 'expected_claim_epoch'))
        leaf = forest.resolve_leaf(identity)
        confirmed = payload.get('owner_confirmed_inactive', False)
        if type(confirmed) is not bool:
            raise ValueError('原会话停止确认必须是明确的布尔值')
        if confirmed:
            ticket = self._ticket(forest, leaf['id'], agent, payload) if 'ticket' in payload else None
            if ticket is not None:
                source = forest.bus.find_message(ticket.task_id)
                if source is None:
                    raise ValueError('原票据对应任务不存在；保留原记录')
                forest.bus.check_claim(agent, source, ticket)
        else:
            ticket = self._ticket(forest, leaf['id'], agent, payload)
        result = forest.handoff_leaf(leaf['id'], agent, ticket=ticket, reason=payload.get('reason'),
                                     successor=payload.get('successor'),
                                     owner_confirmed_inactive=confirmed,
                                     expected_task_id=payload.get('expected_task_id'),
                                     expected_owner=payload.get('expected_owner'),
                                     expected_claim_epoch=payload.get('expected_claim_epoch'))
        for key in [key for key in self._tickets if key[0] in (identity, leaf['id'])]:
            self._tickets.pop(key, None)
        return _public(result)

    def _takeover_leaf(self, payload):
        forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller',
            'reason', 'force', 'expected_task_id', 'expected_owner', 'expected_claim_epoch'))
        leaf = forest.resolve_leaf(identity)
        force = payload.get('force', False)
        if type(force) is not bool:
            raise ValueError('force must be a boolean stopped-owner confirmation')
        reason = payload.get('reason', '') or ''
        result = forest.takeover_leaf(leaf['id'], agent, reason=reason, force=force,
                                      expected_task_id=payload.get('expected_task_id'),
                                      expected_owner=payload.get('expected_owner'),
                                      expected_claim_epoch=payload.get('expected_claim_epoch'))
        for key in [key for key in self._tickets if key[0] in (identity, leaf['id'])]:
            self._tickets.pop(key, None)
        return _public(result)

    def _takeover_capsule(self, payload):
        forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller'))
        leaf = forest.resolve_leaf(identity)
        snap_leaf = forest._leaf(forest.snapshot(), leaf['id'])
        capsule = forest.generate_takeover_capsule(snap_leaf, agent)
        return _public({'ok': True, 'capsule': capsule})

    def _pass_leaf(self, payload):
        self._fields(payload, ('leaf_id', 'id', 'reason', 'admin'))
        identity = payload.get('leaf_id') or payload.get('id')
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError('请指定要放行的任务节点 ID 或章节号')
        reason = payload.get('reason', '')
        admin = payload.get('admin') or 'admin'
        forest = TaskForest(self._cfg())
        leaf = forest.pass_leaf(identity.strip(), reason, admin=admin)
        return _public({**self._snapshot({}), 'ok': True, 'leaf': leaf})

    def _reset_leaf(self, payload):
        self._fields(payload, ('leaf_id', 'id', 'reason', 'admin', 'owner_confirmed_inactive', 'expected_task_id', 'expected_owner', 'expected_claim_epoch'))
        identity = payload.get('leaf_id') or payload.get('id')
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError('请指定要重置的任务节点 ID 或章节号')
        reason = payload.get('reason', '')
        admin = payload.get('admin') or 'admin'
        confirmation = payload.get('owner_confirmed_inactive', False)
        if type(confirmation) is not bool:
            raise ValueError('停止确认必须为布尔值')
        forest = TaskForest(self._cfg())
        leaf = forest.reset_leaf(identity.strip(), reason, admin=admin,
                                 owner_confirmed_inactive=confirmation,
                                 expected_task_id=payload.get('expected_task_id'),
                                 expected_owner=payload.get('expected_owner'),
                                 expected_claim_epoch=payload.get('expected_claim_epoch'))
        for key in [key for key in self._tickets if key[0] in (identity.strip(), leaf['id'])]:
            self._tickets.pop(key, None)
        return _public({**self._snapshot({}), 'ok': True, 'leaf': leaf})

    def _diagnose_deadlock(self, payload):
        self._fields(payload, ())
        forest = TaskForest(self._cfg())
        return _public(forest.diagnose_deadlock())

    def _gc_plan(self, payload):
        self._fields(payload, ('keep', 'include_packages', 'include_deprecated'))
        from .gc import DEFAULT_KEEP, plan as gc_plan
        cfg = self._cfg()
        keep = int(payload.get('keep') or DEFAULT_KEEP)
        include_packages = bool(payload.get('include_packages', False))
        include_deprecated = bool(payload.get('include_deprecated', True))
        p = gc_plan(cfg.workspace, keep=keep, include_packages=include_packages,
                    include_deprecated=include_deprecated)
        return _public(p.to_dict())

    def _gc_apply(self, payload):
        self._fields(payload, ('keep', 'include_packages', 'include_deprecated', 'confirmed'))
        if payload.get('confirmed') is not True:
            raise ValueError('请明确确认执行磁盘清理')
        from .gc import DEFAULT_KEEP, plan as gc_plan, apply as gc_apply
        cfg = self._cfg()
        keep = int(payload.get('keep') or DEFAULT_KEEP)
        include_packages = bool(payload.get('include_packages', False))
        include_deprecated = bool(payload.get('include_deprecated', True))
        p = gc_plan(cfg.workspace, keep=keep, include_packages=include_packages,
                    include_deprecated=include_deprecated)
        removed = gc_apply(p, confirmed=True)
        return _public({'ok': True, 'removed': removed, 'reclaimed_bytes': p.reclaim_bytes})

    def _discover_leaf(self, payload):
        self._fields(payload, ('agent', 'caller', 'title', 'goal', 'from_task_id', 'expected_artifacts'))
        cfg = self._cfg()
        agent = str(payload.get('agent') or payload.get('caller') or cfg.director).strip()
        title = str(payload.get('title') or '').strip()
        if not title:
            raise ValueError('请填写新发现任务的标题')
        goal = str(payload.get('goal') or '').strip()
        from_id = payload.get('from_task_id')
        arts = payload.get('expected_artifacts')
        if isinstance(arts, str):
            arts = [a.strip() for a in arts.split(',') if a.strip()]
        elif not isinstance(arts, list):
            arts = []
        from .open_patterns import discover_subtask
        res = discover_subtask(cfg, agent=agent, title=title, goal=goal,
                               from_task_id=from_id, expected_artifacts=arts)
        return _public({**self._snapshot({}), 'ok': True, 'discovered': res})

    def _repomap_slice(self, payload):
        self._fields(payload, ('target_paths', 'keywords', 'max_files'))
        cfg = self._cfg()
        paths = payload.get('target_paths') or []
        kws = payload.get('keywords') or []
        max_f = int(payload.get('max_files') or 6)
        from .open_patterns import build_repomap_slice
        return _public(build_repomap_slice(cfg.workspace, target_paths=paths, keywords=kws, max_files=max_f))

    def _lessons_list(self, payload):
        self._fields(payload, ('target_paths', 'keywords', 'limit'))
        cfg = self._cfg()
        paths = payload.get('target_paths') or []
        kws = payload.get('keywords') or []
        limit = int(payload.get('limit') or 20)
        from .open_patterns import list_lessons, query_relevant_lessons
        if paths or kws:
            items = query_relevant_lessons(cfg.workspace, target_paths=paths, keywords=kws, limit=limit)
        else:
            items = list_lessons(cfg.workspace, limit=limit)
        return _public({'count': len(items), 'lessons': items})

    def _lessons_add(self, payload):
        self._fields(payload, ('agent', 'caller', 'lesson', 'task_id', 'scope_paths'))
        cfg = self._cfg()
        agent = str(payload.get('agent') or payload.get('caller') or cfg.director).strip()
        lesson = str(payload.get('lesson') or '').strip()
        if not lesson:
            raise ValueError('请填写要沉淀的避坑经验内容')
        task_id = str(payload.get('task_id') or '').strip()
        scopes = payload.get('scope_paths') or []
        if isinstance(scopes, str):
            scopes = [s.strip() for s in scopes.split(',') if s.strip()]
        from .open_patterns import record_lesson
        item = record_lesson(cfg.workspace, agent=agent, lesson=lesson, task_id=task_id, scope_paths=scopes)
        return _public({'ok': True, 'item': item})

    def _worktree_list(self, payload):
        self._fields(payload, ())
        cfg = self._cfg()
        from .open_patterns import list_worktrees
        items = list_worktrees(cfg.workspace)
        return _public({'count': len(items), 'worktrees': items})

    def _trigger_desktop_wakeup(self, payload):
        forest, identity, agent = self._leaf_context(payload, ('leaf_id', 'id', 'agent', 'caller', 'auto_copy', 'sound'))
        leaf = forest.resolve_leaf(identity)
        snap_leaf = forest._leaf(forest.snapshot(), leaf['id'])
        from .desktop_wakeup import DesktopWakeupBridge
        bridge = DesktopWakeupBridge(self._cfg())
        auto_copy = bool(payload.get('auto_copy', True))
        play_sound = bool(payload.get('sound', True))
        result = bridge.notify_task_wakeup(snap_leaf, agent, auto_copy=auto_copy, play_sound=play_sound)
        return _public(result)

    def _set_workspace(self, payload):
        self._fields(payload, ('workspace',))
        workspace = _directory(payload.get('workspace'))
        self._ensure_workspace_initialized(workspace)
        old = self._workspace
        self._workspace = workspace
        try:
            result = self._snapshot({})
        except Exception:
            self._workspace = old
            raise
        if workspace != old:
            self._tickets.clear()
        if self._remember_workspace:
            remembered = save_last_workspace(workspace)
            result['workspace_remembered'] = remembered
            if not remembered:
                result['workspace_warning'] = '项目已切换；本次未能保存下次启动时的恢复位置。'
        return result

    def _pick(self):
        if self._window is None:
            raise ValueError('当前没有原生窗口，无法打开文件夹选择器')
        import webview
        dialog = getattr(getattr(webview, 'FileDialog', None), 'FOLDER', None)
        if dialog is None:
            dialog = webview.FOLDER_DIALOG
        result = self._window.create_file_dialog(dialog, allow_multiple=False, directory=str(self._workspace))
        if not result:
            return None
        value = result[0] if isinstance(result, (list, tuple)) else result
        return _directory(value)

    def _choose_workspace(self, payload):
        self._fields(payload, ())
        selected = self._pick()
        return {'cancelled': True} if selected is None else self._set_workspace({'workspace': str(selected)})

    def _choose_output(self, payload):
        self._fields(payload, ())
        selected = self._pick()
        return {'cancelled': selected is None, 'path': str(selected) if selected else None,
                'output_root': str(selected) if selected else None}

    def _choose_member_icon(self, payload):
        self._fields(payload, ())
        selected = self._file_dialog(member_icon=True)
        if selected is None:
            return {'cancelled': True}
        path = self._member_icon_path(selected)
        from .desktop_app_icon import get_desktop_app_icon
        icon = get_desktop_app_icon(path)
        return {'cancelled': False, 'path': str(path), 'status': icon.status,
                'data_uri': icon.data_uri}

    def _skills_list(self, payload):
        self._fields(payload, ())
        return self._skills({})

    def _skills_inspect(self, payload):
        self._fields(payload, ('path',))
        return self._skills({'operation': 'inspect', **payload})

    def _skills(self, payload):
        self._fields(payload, ('operation', 'path'))
        cfg = self._cfg()
        public = PublicFiles(cfg.workspace)
        operation = payload.get('operation', 'list')
        if operation == 'list':
            from .skill_registry import SkillRegistry
            registry = SkillRegistry()
            stored = public.read_json('.agentbus/skill-registry.json')
            if stored is not None:
                entries = stored.get('entries')
                if not isinstance(entries, list) or len(entries) > 128:
                    raise ValueError('项目技能登记格式无效或过长')
                for entry in entries:
                    registry.add(entry)
            paths = project_skill_paths(cfg)
            return {'entries': _public(registry.list()), 'project_workflows': [
                {'member': agent, 'path': relative, 'present': public.checked(relative).is_file()}
                for agent, relative in paths.items()], 'read_only': True,
                'source': '当前项目技能登记与公共工作流', 'execution_verified': False}
        if operation == 'inspect':
            relative = validate_relative(payload.get('path', ''))
            if any(part.casefold() in _PRIVATE for part in relative.parts):
                raise ValueError('私有路径不是技能输入')
            public.checked(relative)
            from .skill_packages import inspect_package
            return {**inspect_package(cfg.workspace, relative.as_posix()), 'execution_verified': False}
        if operation == 'scan':
            from .skill_registry import SkillRegistry
            import re
            registry = SkillRegistry()
            stored = public.read_json('.agentbus/skill-registry.json')
            if stored is not None:
                entries = stored.get('entries')
                if isinstance(entries, list):
                    for entry in entries[:128]:
                        registry.add(entry)
            discovered = []
            registered_ids = {e.get('id') or e.get('name') for e in registry.list()}
            candidate_dirs = [cfg.workspace / 'skills', cfg.workspace / '.skills']
            try:
                home = Path.home()
                for host_dir in [home / '.gemini/antigravity/skills', home / '.codex/skills', home / '.claude/skills']:
                    if host_dir.is_dir():
                        candidate_dirs.append(host_dir)
            except (RuntimeError, ValueError):
                pass
            for base in candidate_dirs:
                if not base.is_dir():
                    continue
                for skill_md in base.glob('*/SKILL.md'):
                    try:
                        text = skill_md.read_text(encoding='utf-8')
                        name = skill_md.parent.name
                        desc = '本地自动发现的 Agent 技能'
                        m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
                        if m:
                            for line in m.group(1).splitlines():
                                if line.startswith('name:'):
                                    name = line.split(':', 1)[1].strip()
                                elif line.startswith('description:'):
                                    desc = line.split(':', 1)[1].strip()
                        status = 'active' if name in registered_ids else 'discovered'
                        discovered.append({
                            'name': name,
                            'description': desc,
                            'scope': 'workspace' if base.is_relative_to(cfg.workspace) else 'host',
                            'status': status,
                            'path': skill_md.parent.name
                        })
                    except (UnicodeError, ValueError, IndexError):
                        continue
            active_count = sum(1 for d in discovered if d["status"] == "active")
            return {
                'entries': _public(registry.list()),
                'discovered': discovered,
                'discovered_count': len(discovered),
                'active_count': active_count,
                'scan_summary': f'已完成自动扫描：共发现 {len(discovered)} 个本地与宿主技能包（{active_count} 个已激活）',
                'read_only': True,
                'execution_verified': False
            }
        raise ValueError('技能中心支持 list、inspect 与 scan')

    def _workflow_prompt(self, payload):
        self._fields(payload, ('agent', 'caller'))
        cfg = self._cfg()
        agent = self._member(cfg, payload.get('agent', payload.get('caller', cfg.director)))
        from .project_workflow import resume_prompt
        prompt = resume_prompt(cfg, agent)
        mode = assignment_mode(cfg.data)
        route = cfg.worker(agent).get('communication_mode') or cfg.worker(agent).get('transport', 'file')
        prompt += (f'\n\n接入说明：当前成员路线为 {route}；任务指派采用'
                   + ('组长自动指派' if mode == 'automatic' else ('自由调度（智能体自主接活）' if mode == 'autonomous' else '用户逐项手动指派'))
                   + '。初始化不等于任务已执行。'
                   + ('自由调度下请直接运行 agentbus claim-next 自主抢单就绪任务；' if mode == 'autonomous' else '只读取实际分配给你的任务叶子；')
                   + '任务章节号以 task-tree.json 的 section_no 为准，不按显示顺序重编号。')
        if agent == cfg.director:
            prompt += (' 你是本项目平级组长；负责把目标制成或导入任务树、明确依赖与验收，'
                       '按成员能力和负载协调工作。EXE 的自动指派只投递就绪叶子，不替你规划或替 GUI 成员启动对话。')
        return {'prompt': prompt, 'agent': agent, 'workspace': str(cfg.workspace),
                'path': str(cfg.workspace / 'AGENTBUS.md'),
                'skill_path': str(cfg.workspace / project_skill_paths(cfg)[agent]),
                'executed': False, 'model_calls': 0}

    def _manifest_context(self, cfg=None):
        """Build a small project-only context from AgentBus' public records."""
        cfg = cfg or self._cfg()
        records = self._mailboxes(cfg)
        forest = _public(TaskForest(cfg).snapshot())
        roots = forest.get('roots', [])
        root = roots[0] if roots else {}
        target = (root.get('goal') or root.get('title')
                  or cfg.data.get('project_name')
                  or '请在任务树中明确本项目的交付目标')
        members = []
        inbox_counts = {}
        for agent, worker in cfg.data.get('workers', {}).items():
            mode = worker.get('communication_mode') or worker.get('transport', 'file')
            members.append({'id': agent,
                            'name': worker.get('display_name') or worker.get('name') or agent,
                            'mode': mode})
            box = records.get(agent, {})
            inbox_counts[agent] = {state: len(box.get(state, []))
                                   for state in ('ready', 'processing', 'blocked', 'done')}
        tasks = []
        for leaf in forest.get('leaves', [])[:256]:
            tasks.append({'id': leaf.get('task_id') or leaf.get('id'),
                          'section': leaf.get('section_no') or leaf.get('section') or '',
                          'title': leaf.get('title') or leaf.get('name') or '未命名任务',
                          'state': leaf.get('state') or 'ready',
                          'assignee': leaf.get('owner') or leaf.get('assignee') or ''})
        return {'workspace': str(cfg.workspace), 'output_root': str(cfg.output_root),
                'target': str(target), 'tree_id': str(forest.get('tree_id') or ''),
                'tree_title': str(root.get('title') or cfg.data.get('project_name') or ''),
                'coordinator': cfg.director, 'members': members, 'tasks': tasks,
                'inbox_counts': inbox_counts}

    def _onboarding_capsule(self, payload):
        self._fields(payload, ('agent',))
        cfg = self._cfg()
        agent = self._member(cfg, payload.get('agent'), executor=False)
        from .system_manifest import render_onboarding_capsule
        from .onboarding_pack import generate_onboarding_pack
        pack = None
        pack_error = ''
        try:
            pack = generate_onboarding_pack(cfg.workspace, agent)
        except Exception as exc:
            pack_error = f'接入包生成失败：{exc}'[:500]
        return {
            'pack_error': pack_error,
            'agent': agent,
            'prompt': render_onboarding_capsule(self._manifest_context(cfg), agent),
            'pack': pack,
            'leader_instructions': pack['leader_instructions'] if pack else '',
            'newcomer_instructions': pack['newcomer_instructions'] if pack else '',
            'executed': False,
            'model_calls': 0
        }

    def _member_pack(self, payload):
        self._fields(payload, ('agent', 'member', 'deploy', 'task_id', 'coordinator'))
        cfg = self._cfg()
        target = payload.get('member') or payload.get('agent')
        agent = self._member(cfg, target, executor=False)
        from .onboarding_pack import generate_onboarding_pack, deploy_onboarding_pack
        task_id = payload.get('task_id')
        coord = payload.get('coordinator')
        if payload.get('deploy') is True:
            result = deploy_onboarding_pack(cfg.workspace, agent, coordinator=coord, task_id=task_id)
        else:
            result = generate_onboarding_pack(cfg.workspace, agent, coordinator=coord, task_id=task_id)
        return {**result, 'model_calls': 0}

    def _one_click_onboard(self, payload):
        self._fields(payload, ('member_id', 'agent', 'display_name', 'preset'))
        self._ensure_workspace_initialized(self._workspace)
        cfg = self._cfg()
        target = payload.get('member_id') or payload.get('agent')
        if not target:
            raise ValueError('新成员 ID 不能为空')
        from .onboarding_pack import auto_onboard_agent
        result = auto_onboard_agent(
            workspace=cfg.workspace,
            member_id=str(target).strip(),
            display_name=payload.get('display_name'),
            preset=payload.get('preset')
        )
        return {**result, 'model_calls': 0}


    def _contact_show(self, payload):
        self._fields(payload, ('agent', 'member_id'))
        cfg = self._cfg()
        member_id = str(payload.get('agent') or payload.get('member_id') or '').strip()
        if not member_id:
            raise ValueError('请指定成员标识 (agent 或 member_id)')
        from .trigger.contacts import load_contact
        contact = load_contact(cfg.workspace, member_id)
        if contact is not None:
            return {**contact.to_dict(), 'model_calls': 0}
        return {'status': 'unbound', 'member_id': member_id, 'model_calls': 0}

    def _contact_checkin(self, payload):
        self._fields(payload, ('agent', 'member_id', 'session_id', 'connector_id'))
        cfg = self._cfg()
        member_id = str(payload.get('agent') or payload.get('member_id') or '').strip()
        if not member_id:
            raise ValueError('请指定成员标识 (agent 或 member_id)')
        from .trigger.contacts import checkin_contact
        res = checkin_contact(
            cfg.workspace,
            member_id,
            session_id=payload.get('session_id'),
            connector_id=payload.get('connector_id'),
        )
        return {**res, 'model_calls': 0}

    def _trigger_onboarding_start(self, payload):
        self._fields(payload, ('member_id',))
        cfg = self._cfg()
        member_id = str(payload.get('member_id', '')).strip()
        from .trigger.onboarding import start_flow
        flow = start_flow(cfg.workspace, member_id)
        return self._trigger_flow_data(cfg, flow)

    def _trigger_onboarding_advance(self, payload):
        self._fields(payload, ('flow_id', 'expected_revision', 'action', 'payload'))
        cfg = self._cfg()
        flow_id = payload['flow_id']
        expected_rev = payload['expected_revision']
        action = payload['action']
        act_payload = payload.get('payload', {})
        from .trigger.onboarding import advance_flow
        flow = advance_flow(cfg.workspace, flow_id, expected_rev, action, act_payload)
        return self._trigger_flow_data(cfg, flow)

    def _trigger_onboarding_resume(self, payload):
        self._fields(payload, ('flow_id',))
        cfg = self._cfg()
        flow_id = payload['flow_id']
        from .trigger.onboarding import resume_flow
        flow = resume_flow(cfg.workspace, flow_id)
        return self._trigger_flow_data(cfg, flow)

    @staticmethod
    def _trigger_flow_data(cfg, flow):
        """Public command context from the registered launcher, independent of CWD."""
        data = flow.to_dict()
        workflow = PublicFiles(cfg.workspace).read_json('.agentbus/workflow.json') or {}
        registered = (workflow.get('launcher') or {}).get('executable')
        if not registered and getattr(sys, 'frozen', False):
            registered = sys.executable
        executable = None
        if isinstance(registered, str) and Path(registered).is_absolute():
            candidate = PublicFiles(Path(registered).parent).checked(Path(registered).name)
            if candidate.is_file():
                executable = str(candidate)
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        command = None
        if executable and data.get('connector_id') and data.get('selected_installation'):
            command = (f"& {quote(executable)} contact checkin --workspace {quote(cfg.workspace)}"
                       f" --agent {quote(data['member_id'])} --connector {quote(data['connector_id'])}"
                       f" --installation {quote(data['selected_installation'])}"
                       f" --session {quote('<当前宿主真实会话ID>')}")
        return {**data, 'workspace': str(cfg.workspace), 'executable': executable,
                'checkin_command': command, 'checkin_requires_session': True, 'model_calls': 0}

    def _trigger_status(self, payload):
        self._fields(payload, ())
        cfg = self._cfg()
        from .trigger.service import TriggerDeliveryService
        service = TriggerDeliveryService(cfg.workspace)
        policy = service.load_policy()
        return {'stopped': policy.stopped, 'coordinator_route': policy.coordinator_route, 'delivery_enabled': policy.delivery_enabled, 'model_calls': 0}

    def _trigger_set_stopped(self, payload):
        self._fields(payload, ('stopped',))
        cfg = self._cfg()
        stopped = bool(payload.get('stopped'))
        from .trigger.service import TriggerDeliveryService
        service = TriggerDeliveryService(cfg.workspace)
        service.set_stopped(stopped)
        return {'stopped': stopped, 'model_calls': 0}

    def _trigger_diagnostics(self, payload):
        self._fields(payload, ())
        cfg = self._cfg()
        from .trigger.diagnostics import export_diagnostic_bundle
        bundle = export_diagnostic_bundle(cfg.workspace)
        return {**bundle.to_dict(), 'model_calls': 0}

    def _trigger_verification_plan(self, payload):
        self._fields(payload, ('member_id',))
        cfg = self._cfg()
        member_id = str(payload.get('member_id', '')).strip()
        from .trigger.contacts import load_contact
        contact = load_contact(cfg.workspace, member_id)
        if not contact:
            raise ValueError(f"Member contact not found: {member_id}")
        from .trigger.verification import plan_verification
        plan = plan_verification(cfg.workspace, contact)
        return {**plan.to_dict(), 'model_calls': 0}

    def _trigger_verification_start(self, payload):
        self._fields(payload, ('plan',))
        cfg = self._cfg()
        plan_dict = payload['plan']
        from .trigger.verification import VerificationPlan, start_verification
        plan = VerificationPlan.from_dict(plan_dict)
        vrun = start_verification(cfg.workspace, plan)
        return {**vrun.to_dict(), 'model_calls': None}

    def _trigger_verification_check(self, payload):
        self._fields(payload, ('run_id',))
        cfg = self._cfg()
        run_id = str(payload['run_id']).strip()
        from .trigger.verification import get_verification
        report = get_verification(cfg.workspace, run_id)
        if not report:
            return {'status': 'not_found', 'passed': False, 'model_calls': 0}
        return {**report.to_dict(), 'passed': report.status == 'passed', 'model_calls': 0}

    def _trigger_verification_run(self, payload):
        self._fields(payload, ('plan',))
        cfg = self._cfg()
        plan_dict = payload['plan']
        from .trigger.verification import VerificationPlan, start_verification, get_verification
        plan = VerificationPlan.from_dict(plan_dict)
        vrun = start_verification(cfg.workspace, plan)
        report = get_verification(cfg.workspace, vrun.run_id)
        if report:
            return {**report.to_dict(), 'passed': report.status == 'passed', 'model_calls': None}
        return {**vrun.to_dict(), 'passed': False, 'member_id': plan.member_id, 'model_calls': None}

    def _trigger_reconcile(self, payload):
        self._fields(payload, ())
        cfg = self._cfg()
        from .trigger.journal import DeliveryJournal
        journal = DeliveryJournal(cfg.workspace)
        res = journal.reconcile_on_startup()
        return {**res, 'model_calls': 0}

    def _trigger_scan_now(self, payload):
        self._fields(payload, ())
        cfg = self._cfg()
        from .trigger.service import TriggerDeliveryService
        service = TriggerDeliveryService(cfg.workspace)
        res = service.scan_and_deliver_pending()
        return {**res, 'model_calls': 0}

    def _ensure_quickstart(self, payload):
        self._fields(payload, ())
        cfg = self._cfg()
        from .system_manifest import ensure_quickstart
        result = ensure_quickstart(cfg.workspace, self._manifest_context(cfg))
        return {**_public(result), 'model_calls': 0}

    def _system_check(self, payload):
        """Read-only, credential-free self-check for the current desktop project."""
        self._fields(payload, ())
        checks = []
        workspace = self._workspace
        checks.append({'id': 'workspace', 'title': '项目文件夹',
                       'ok': workspace.is_dir(),
                       'detail': '当前项目文件夹可访问' if workspace.is_dir() else '当前项目文件夹不可访问'})
        checks.append({'id': 'python', 'title': '本地运行环境',
                       'ok': Path(sys.executable).is_file(),
                       'detail': f'Python {sys.version_info.major}.{sys.version_info.minor} 可运行'
                       if Path(sys.executable).is_file() else '本地运行环境不可用'})
        checks.append({'id': 'desktop', 'title': '桌面窗口连接',
                       'ok': self._window is not None,
                       'detail': '当前桌面窗口已连接' if self._window is not None
                       else '桌面桥梁未连接；请从 AgentBus 桌面程序打开此页'})
        from .web_desktop import runtime_diagnostics
        runtime = runtime_diagnostics()
        webview_ready = runtime.get('platform') == 'win32' and bool(runtime.get('webview2_installed'))
        if webview_ready:
            runtime_version = runtime.get('webview2_version') or '版本未识别'
            detail = f'Windows 桌面运行时已就绪 · WebView2 {runtime_version}'
            pywebview_version = runtime.get('pywebview_version')
            if pywebview_version:
                detail += f' · pywebview {pywebview_version}'
        elif runtime.get('platform') == 'win32':
            detail = runtime.get('guidance') or '未找到 Microsoft Edge WebView2 Evergreen Runtime。'
        else:
            detail = '当前桌面版需要 Windows 和 Microsoft Edge WebView2。'
        checks.append({'id': 'webview2', 'title': '桌面运行组件', 'ok': webview_ready,
                       'detail': str(detail)[:300]})
        public = PublicFiles(workspace)
        configured = public.checked('.agentbus/config.json').is_file()
        checks.append({'id': 'project_config', 'title': '小组设置', 'ok': configured,
                       'detail': '已找到项目小组设置' if configured else '尚未配置小组'})
        tree_count = ready_count = active_count = blocked_count = 0
        if configured:
            cfg = self._cfg()
            records = self._mailboxes(cfg)
            forest = TaskForest(cfg).snapshot()
            tree_count = len(forest.get('roots', [])) + len(forest.get('branches', [])) + len(forest.get('leaves', []))
            ready_count = sum(len(box.get('ready', [])) for box in records.values())
            active_count = sum(len(box.get('processing', [])) for box in records.values())
            blocked_count = sum(len(box.get('blocked', [])) for box in records.values())
            checks.append({'id': 'task_tree', 'title': '任务树', 'ok': tree_count > 0,
                           'detail': f'已载入 {tree_count} 个任务节点' if tree_count else '尚未导入任务树'})
            checks.append({'id': 'mailboxes', 'title': '小组收件箱', 'ok': True,
                           'detail': f'待处理 {ready_count} · 处理中 {active_count} · 受阻 {blocked_count}'})
            supervision = cfg.data.get('supervision', {})
            strict = bool(supervision.get('strict_supervision', False))
            checks.append({'id': 'supervision', 'title': '独立成果复核', 'ok': not strict or bool(supervision.get('designated_auditor')),
                           'detail': (f"已开启 · 复核成员 {supervision.get('designated_auditor')} · {len(supervision.get('checks', []))} 项检查"
                                      if strict else '未开启 · 成果按普通交付流程处理')})
        return {'checks': checks, 'workspace': str(workspace), 'configured': configured,
                'task_nodes': tree_count, 'ready': ready_count, 'processing': active_count,
                'blocked': blocked_count, 'read_only': True, 'model_calls': 0}

    def _recovery(self, payload):
        self._fields(payload, ())
        cfg = self._cfg()
        records = self._mailboxes(cfg)
        self._machine_paths(cfg, records)
        public = PublicFiles(cfg.workspace)
        for folder in ('heartbeats', 'leases', 'late-results'):
            public.checked(f'.agentbus/{folder}')
        from .recovery_supervisor import observe_workspace, propose
        from .recovery_center import action_catalog, project_handoff_index
        forest = TaskForest(cfg)
        handoff_index = project_handoff_index(forest.snapshot(), forest.bus.events())
        observations = observe_workspace(cfg.workspace / '.agentbus', cfg.data['workers'],
                                         heartbeat_timeout_s=cfg.gates.get('heartbeat_timeout_s', 600))
        stopped = os.path.lexists(public.checked('.agentbus/STOP'))
        task_records = {m.id: m.to_dict() for box in records.values() for m in box.get('processing', [])}
        return {'observations': _public([o.to_dict() for o in observations]),
                'proposals': _public([propose(o, record=task_records.get(o.task_id),
                                             stop_active=stopped).to_dict() for o in observations]),
                'handoff_index': _public(handoff_index),
                'catalog': action_catalog(), 'auto_execute': False, 'read_only': True}

    def _activity(self, payload):
        self._fields(payload, ('limit',))
        limit = payload.get('limit', 40)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('动态记录数量需在1–100之间')
        events = PublicFiles(self._workspace).recent_events()[-limit:]
        keys = ('ts', 'event', 'agent', 'caller', 'task_id', 'leaf_id', 'job_id', 'id',
                'to', 'sender', 'type', 'status', 'successor', 'reason_code')
        return {'events': [{k: e[k] for k in keys if k in e and isinstance(e[k], (str, int, bool))}
                           for e in events], 'limit': limit, 'source': 'AgentBus events.jsonl'}

    def _stop(self, payload):
        return self._marker(payload, True)

    def _resume(self, payload):
        return self._marker(payload, False)

    def _marker(self, payload, stopped):
        self._fields(payload, ('caller', 'reason'))
        cfg = self._cfg()
        caller = self._member(cfg, payload.get('caller', cfg.director))
        reason = payload.get('reason', '用户通过桌面工作台停止新派发' if stopped else '用户通过桌面工作台恢复派发')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('停止或恢复原因需为1–1000个字符')
        public = PublicFiles(cfg.workspace)
        marker = public.checked('.agentbus/STOP')
        public.checked('.agentbus/leases/.mutation.lock')
        public.checked('.agentbus/events.jsonl')
        manager = LeaseManager(cfg.workspace / '.agentbus')
        with task_lock(cfg.workspace / '.agentbus', 'task-forest', 'structure'), \
                manager.guarded('.agentbus/STOP', 'agentbus-web-stop', ttl_s=60):
            cfg = self._cfg()
            caller = self._member(cfg, payload.get('caller', cfg.director))
            public.checked('.agentbus/STOP')
            bus = Bus(cfg.workspace / '.agentbus')
            bus.init()
            if stopped:
                if not marker.exists():
                    with marker.open('x', encoding='utf-8') as stream:
                        stream.write(json.dumps({'caller': caller, 'reason': reason.strip(),
                                                'created_at': now_iso(), 'source': 'AgentBus WebView'},
                                               ensure_ascii=False))
            else:
                marker.unlink(missing_ok=True)
            bus.audit(event='stop' if stopped else 'resume', caller=caller,
                      source='AgentBus WebView', reason=reason.strip(),
                      existing_processing_preserved=True)
        return self._snapshot({})

    def _adopt_change(self, payload):
        self._fields(payload, ('caller', 'change_record', 'expected_revision_id', 'expected_revision_sha256'))
        cfg = self._cfg()
        caller = self._member(cfg, payload.get('caller', cfg.director))
        change_record = payload.get('change_record')
        if not isinstance(change_record, dict):
            raise ValueError('change_record 必须是字典对象')
        from .project_plan import adopt_change_record
        return adopt_change_record(
            cfg, caller, change_record,
            expected_revision_id=payload.get('expected_revision_id'),
            expected_revision_sha256=payload.get('expected_revision_sha256')
        )

    def _withdraw_change(self, payload):
        self._fields(payload, ('caller', 'change_id', 'change_record', 'reason'))
        cfg = self._cfg()
        caller = self._member(cfg, payload.get('caller', cfg.director))
        reason = payload.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('撤回必须提供有效的原因 (reason)')
        target = payload.get('change_record') or payload.get('change_id')
        if not target:
            raise ValueError('必须提供 change_id 或 change_record')
        from .project_plan import withdraw_change_record
        return withdraw_change_record(cfg, caller, target, reason=reason.strip())
