#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.gui —— 图形启动器（双击 exe 就是这个界面）。

设计目标：选工作空间 → 勾选成员和组长 → 保存 → 在项目对话调用工作流。
GUI 不启动独立模型；CLI 中的独立运行器仍需另行验收。
"""
from __future__ import annotations

import ctypes
import copy
import json
import math
import os
import queue
import stat
import subprocess
import sys
import threading
import time
import uuid
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from . import __version__
from .bus import Bus, BusError, Message
from .config import DEFAULTS, KNOWN_AGENTS, Config, ConfigError, describe, load, save
from .detect import detect_group_agents, render, to_config_patch
from .director import DIRECTOR_INBOX, HUMAN_INBOX, Director
from .gui_state import (initial_workspace, save_group_config,
                        save_last_workspace)
from .lease import LeaseManager
from .skill_install import SkillInstallError, install_for_current_user
from .skill_install import install_peer_skills_for_current_user
from .skill_packages import inspect_package
from .skill_preview import build_sync_preview
from .skill_presentation import present_preview
from .workspace_files import WorkspaceFileError, WorkspaceFiles
from .lifecycle import install_workflows, inspect_workflows, uninstall_workflows
from .health import inspect_project, save_diagnostic
from .onboarding import connection_prompt, connection_summary, _checked, MAX_BYTES
from .profiles import validate_agent_id
from .project_workflow import (WorkflowError, prepare_project_workflow,
                               resume_prompt, workflow_status)
from .watchtower import Watchtower
from .workbench_theme import PALETTE, TYPE_SCALE, NAV_ITEMS, scaled_px
from .workbench_view import build_overview
from .enterprise_view import build_project_view
from .enterprise_shell import build_shell_view
from . import design_system, claim_fencing
from .onboarding_wizard import build_wizard
from .recovery_center import decide_recovery, execute_recovery_action
from .task_tree_view import build_forest_view, continuation_text

FONT = ("Microsoft YaHei UI", TYPE_SCALE['body'])
FONT_SMALL = ("Microsoft YaHei UI", TYPE_SCALE['small'])
FONT_MONO = ("Consolas", TYPE_SCALE['mono'])
FONT_TITLE = ("Microsoft YaHei UI", TYPE_SCALE['title'], "bold")
COLORS = dict(PALETTE)


# Source: Codex internal integration, 2026-10-05. These are inspection limits,
# not deployment permissions. The scanner's historical max_dir_bytes parameter
# currently bounds directory-node/listing counts, so show its real unit.
SKILL_CHECK_LIMITS = {'max_files': 256, 'max_total_bytes': 32 * 1024 * 1024,
                      'max_file_bytes': 8 * 1024 * 1024, 'max_dir_bytes': 1024}
SKILL_CHECK_BOUNDARY = ('每包最多 256 文件、总计 32 MiB、单文件 8 MiB；'
                        '目录节点和单目录条目各最多 1024。检查等待上限 30 秒。')


# Source: Codex internal GUI integration, 2026-10-05. These are read-only
# observation bounds, never a replacement for jobs.status_job acceptance.
OVERVIEW_MAX_JOBS = 20
OVERVIEW_MAX_MESSAGES = 2000
OVERVIEW_READ_WINDOW_S = 3.0


def read_public_snapshot(cfg, *, read_window_s=OVERVIEW_READ_WINDOW_S) -> dict:
    """Observe bounded public records without invoking a runner or all-job scan.

    The durable job file does not store status_job's current acceptance result.
    Consequently this lightweight snapshot leaves stage/verified-deliverables
    unconfirmed unless a stage was explicitly recorded. It never turns a
    finalization flag into fresh artifact verification. Full acceptance remains
    available through the existing task/plan page.
    """
    observed = datetime.now().astimezone().isoformat(timespec='seconds')
    deadline = time.monotonic() + max(0.01, read_window_s)
    root = cfg.workspace / '.agentbus'
    selected = cfg.enabled_workers()
    names = list(selected) + [n for n in ('director', 'human') if n not in selected]
    records, job_rows, errors = {}, [], []
    counts = {name: {state: 0 for state in ('ready', 'processing', 'blocked', 'done')}
              for name in names}
    unreadable = {name: 0 for name in names}
    read_count, truncated = 0, False
    member_evidence = {name: {'enabled': True, 'heartbeat_verified': False,
                             'workspace': str(cfg.workspace), 'member': name}
                       for name in selected}
    recovery_tasks = []
    now = time.time()
    heartbeat_timeout = cfg.gates.get('heartbeat_timeout_s', 600)
    if (isinstance(heartbeat_timeout, bool) or not isinstance(heartbeat_timeout, (int, float))
            or not math.isfinite(heartbeat_timeout) or heartbeat_timeout <= 0):
        heartbeat_timeout = 600

    def read_json(path):
        if time.monotonic() >= deadline:
            raise TimeoutError('公开记录读取窗口已到，已有快照保留')
        if not _checked(path) or path.stat().st_size > MAX_BYTES:
            raise ValueError('公开记录不是有界普通文件')
        before = path.stat()
        with path.open('rb') as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES or not _checked(path):
            raise ValueError('公开记录过大或路径发生变化')
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('公开记录在读取期间发生变化')
        value = json.loads(content.decode('utf-8'))
        if not isinstance(value, dict):
            raise ValueError('公开记录必须为对象')
        return value

    def observe_claim(name, row, task_path):
        """Bounded public heartbeat validation; never use mtime for activity."""
        heartbeat_verified = False
        task_id = claim_fencing.validate_task_id(row['id'])
        heartbeat_path = root / 'heartbeats' / task_id / (name + '.json')
        heartbeat_reason = '没有当前票据绑定的有效心跳；认领不等于执行'
        try:
            if _checked(heartbeat_path) and claim_fencing.is_fenced(row) and row.get('status') == 'claimed':
                hb = read_json(heartbeat_path)
                claim_fencing.check(row, hb)
                stamp = hb.get('at_epoch_s')
                heartbeat_verified = (
                    hb.get('task_id') == task_id and hb.get('agent') == name
                    and isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
                    and math.isfinite(stamp) and -0.01 <= time.time() - stamp <= heartbeat_timeout
                    and hb.get('project_id') in ('', str(cfg.workspace))
                    and row.get('workspace', str(cfg.workspace)) == str(cfg.workspace))
                current = read_json(task_path)
                if any(current.get(key) != row.get(key) for key in
                       ('id', 'to', 'status', 'claim_agent', 'claim_epoch', 'claim_id')):
                    heartbeat_verified = False
                    member_evidence[name]['evidence_conflict'] = True
                heartbeat_reason = ('已核对当前任务、成员、票据和心跳时限' if heartbeat_verified
                                    else '心跳归属或时间未通过校验')
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            heartbeat_reason = '心跳记录未通过校验；文件时间不证明执行'
        stopped_session = False
        try:
            marker = root / 'session-stops' / (task_id + '.json')
            if _checked(marker):
                mark = read_json(marker)
                # Only an explicit, task-bound record may show a stopped session.
                stopped_session = (mark.get('task_id') == task_id and
                                   mark.get('agent', name) == name and
                                   mark.get('stopped') is True)
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            pass  # 资源清理类：恢复视图中单条 stop 标记解析失败即按未停止处理，不中断快照。
        member_evidence[name]['heartbeat_verified'] |= heartbeat_verified
        deadline_ts = row.get('deadline_ts')
        deadline_expired = (isinstance(deadline_ts, (int, float))
                            and not isinstance(deadline_ts, bool) and deadline_ts > 0
                            and now > deadline_ts)
        budget_expired = False
        budget_s, claimed_at = row.get('budget_s'), row.get('claimed_at')
        if (isinstance(budget_s, (int, float)) and not isinstance(budget_s, bool)
                and math.isfinite(budget_s) and budget_s > 0 and isinstance(claimed_at, str)
                and claimed_at):
            try:
                started = datetime.fromisoformat(claimed_at)
                if started.tzinfo is not None:
                    budget_expired = now - started.timestamp() > budget_s
            except ValueError:
                pass  # 资源清理类：claimed_at 时间戳非法时 budget_expired 保持 False（不误报超支）。
        recovery_tasks.append({
            'member_id': name, 'task_id': task_id, 'task_title': row.get('title') or task_id,
            'project_id': str(cfg.workspace), 'workspace': str(cfg.workspace),
            'project_matches': row.get('workspace', str(cfg.workspace)) == str(cfg.workspace),
            'member_enabled': True, 'coordinator': cfg.director,
            'task_claimed': row.get('status') == 'claimed',
            'heartbeat_verified': heartbeat_verified, 'heartbeat_reason': heartbeat_reason,
            'process_exited': stopped_session, 'deadline_expired': deadline_expired,
            'budget_expired': budget_expired,
            'recovery_failed_count': row.get('attempt', 0), 'route': 'file',
            'computer_use_authorized': False, 'adapter_verified': False,
            'inbox_path': str(root / 'agents' / name / 'processing' / (task_id + '.json')),
            'input_path': '', 'output_path': str(cfg.output_root), 'checkpoint': '',
        })

    # Single bounded message pass. Do not read private host stores, audit
    # transcripts, arbitrary refs or task artifacts in this GUI snapshot.
    for name in names:
        if name in selected:
            validate_agent_id(name)
        for folder in ('done', 'processing', 'blocked', 'ready'):
            directory = root / 'agents' / name / folder
            try:
                if not _checked(directory, directory=True):
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not entry.name.endswith('.json'):
                            continue
                        if read_count >= OVERVIEW_MAX_MESSAGES:
                            truncated = True
                            break
                        if time.monotonic() >= deadline:
                            truncated = True
                            errors.append('公开消息读取超过时间窗口')
                            break
                        read_count += 1
                        try:
                            row = read_json(Path(entry.path))
                            mid = row.get('id')
                            if (not isinstance(mid, str) or mid + '.json' != entry.name
                                    or row.get('to') != name
                                    or row.get('type') not in ('goal', 'task', 'review', 'result', 'info', 'escalation')
                                    or not isinstance(row.get('status'), str)):
                                raise ValueError('公开消息标识或收件人不一致')
                            if mid in records:
                                # Crash-left duplicate ownership is not permission
                                # to choose a running instance or count it twice.
                                unreadable[name] += 1
                                continue
                            records[mid] = {key: row.get(key) for key in
                                            ('id', 'to', 'type', 'status', 'title')}
                            records[mid]['location'] = folder
                            if row['type'] in ('goal', 'task', 'review'):
                                counts[name][folder] += 1
                                if folder == 'processing' and name in selected:
                                    observe_claim(name, row, Path(entry.path))
                        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
                            unreadable[name] += 1
                            if len(errors) < 4:
                                errors.append('消息无法读取：' + type(exc).__name__)
                if truncated:
                    break
            except (OSError, ValueError) as exc:
                unreadable[name] += 1
                if len(errors) < 4:
                    errors.append('信箱无法读取：' + type(exc).__name__)
        if truncated:
            break

    directory = root / 'jobs'
    jobs_read = 0
    task_intents_read = 0
    try:
        if _checked(directory, directory=True):
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not (entry.name.startswith('j-') and entry.name.endswith('.json')):
                        continue
                    if jobs_read >= OVERVIEW_MAX_JOBS:
                        truncated = True
                        errors.append('作业超过 20 条；目标选择仅覆盖已读取窗口')
                        break
                    jobs_read += 1
                    try:
                        row = read_json(Path(entry.path))
                        if row.get('schema_version') != 1 or row.get('id') + '.json' != entry.name:
                            raise ValueError('作业标识与记录不一致')
                        tasks = []
                        for task in row.get('tasks', [])[:OVERVIEW_MAX_MESSAGES]:
                            if task_intents_read >= OVERVIEW_MAX_MESSAGES:
                                truncated = True
                                errors.append('作业任务摘要超过 2000 条；记录不完整')
                                break
                            task_intents_read += 1
                            if not isinstance(task, dict):
                                raise ValueError('任务摘要无效')
                            actual = records.get(task.get('id'))
                            item = {key: task.get(key) for key in
                                    ('id', 'worker', 'title', 'expected_artifacts')}
                            if actual is not None and actual['to'] == item['worker']:
                                item.update(status=actual['status'], location=actual['location'])
                            else:
                                item.update(status='unconfirmed', location='unreadable')
                            tasks.append(item)
                        job_rows.append({key: row.get(key) for key in
                                         ('id', 'goal', 'created_ts', 'created_at', 'finalization', 'checkpoints')})
                        checkpoints = job_rows[-1].get('checkpoints')
                        if not isinstance(checkpoints, list):
                            job_rows[-1]['checkpoints'] = []
                        elif len(checkpoints) > 100:
                            job_rows[-1]['checkpoints'] = checkpoints[-100:]
                            truncated = True
                            errors.append('续接记录超过轻量展示窗口；历史仍保留')
                        job_rows[-1].update(tasks=tasks, artifacts_verified=False,
                                            state='unconfirmed',
                                            stage_observation='轻量快照不重新执行作业验收')
                    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
                        if len(errors) < 5:
                            errors.append('作业无法读取：' + type(exc).__name__)
                        truncated = True
    except (OSError, ValueError) as exc:
        errors.append('作业目录无法读取：' + type(exc).__name__)
        truncated = True
    try:
        paused = _checked(root / 'STOP')
    except (OSError, ValueError):
        paused = True
        errors.append('暂停标记无法读取，不能据此放行')
    reason = '；'.join(dict.fromkeys(errors))
    if truncated and not reason:
        reason = '公开消息超过 2000 条；记录不完整，不能据此判断信箱为空'
    incomplete = bool(truncated or errors or any(unreadable.values()))
    for name, evidence in member_evidence.items():
        evidence.update(ready_count=counts[name]['ready'], claimed_count=counts[name]['processing'],
                        blocked_count=counts[name]['blocked'],
                        evidence_conflict=bool(unreadable[name]) or evidence.get('evidence_conflict', False))
        # A done mailbox count is archival state, not fresh artifact acceptance.
    for state in recovery_tasks:
        state['stop'] = paused or incomplete
        state['goal'] = next((j.get('goal') for j in job_rows
                              if any(t.get('id') == state['task_id'] for t in j.get('tasks', []))), '')
    health = {'observed_at': observed, 'paused': paused, 'truncated': incomplete,
              'members': [{'name': name, 'task_counts': counts[name],
                           'unreadable_count': unreadable[name]}
                          for name in selected]}
    return {'health': health, 'jobs': job_rows, 'observed_at': observed,
            'records_read': read_count, 'records_incomplete': incomplete,
            'incomplete_reason': reason, 'jobs_read': jobs_read, 'model_calls': 0,
            'member_evidence': member_evidence, 'stopped': paused,
            'recovery_tasks': recovery_tasks}


def read_dashboard_snapshot(cfg, **request) -> dict:
    """Read the public overview and task forest in the existing data worker."""
    value = read_public_snapshot(cfg, **request)
    try:
        from .task_tree import snapshot
        value['task_forest'] = snapshot(cfg)
        value['task_forest_error'] = None
    except Exception as exc:
        value['task_forest'] = {}
        value['task_forest_error'] = type(exc).__name__ + ': ' + str(exc)
    return value


def _task_tree_action(*, operation, cfg, leaf_id, agent) -> dict:
    """Recheck public project state off Tk before copying or claiming a leaf."""
    from .task_tree import snapshot, claim_leaf
    current = load(cfg.workspace)
    worker = current.enabled_workers().get(agent)
    if not worker or not worker.get('executor_enabled', True):
        raise ValueError('所选成员尚未加入当前项目的执行团队')
    value = snapshot(current)
    leaf = next((row for row in value.get('leaves', []) if row.get('id') == leaf_id), None)
    if leaf is None:
        raise ValueError('所选叶子已变化，请刷新任务树')
    ticket = None
    if operation == 'claim':
        if leaf.get('claimable') is not True:
            raise ValueError('该叶子当前不可认领，请按接手说明继续')
        claimed = claim_leaf(current, leaf_id, agent)
        ticket = claim_fencing.captured_ticket(claimed)
        leaf = dict(leaf, owner=agent, task_id=claimed.id, state='claimed', claimable=False)
    elif operation != 'copy':
        raise ValueError('未知任务树操作')
    text = continuation_text(str(current.workspace), leaf, agent)
    if ticket is not None:
        text += ('\n\n本次正式认领返回的原票据（供所选执行成员保留）：\n'
                 + 'task_id: ' + ticket.task_id + '\nagent: ' + ticket.agent
                 + '\nclaim_epoch: ' + str(ticket.claim_epoch)
                 + '\nclaim_id: ' + ticket.claim_id
                 + '\n心跳、进度与回执使用这份原票据；不得从后来读取的任务记录抄新票据。')
    return {'workspace': str(current.workspace), 'leaf_id': leaf_id,
            'operation': operation, 'text': text}


class OverviewSnapshotTask:
    """One data-only worker; bounded wait does not spawn replacement threads."""
    def __init__(self, *, checker=read_public_snapshot, timeout_s=5):
        self.checker = checker
        self.timeout_s = timeout_s
        self.worker = None
        self.results = queue.Queue()
        self.closed = threading.Event()
        self.pending = False
        self.started_at = 0.0

    def start(self, **request) -> bool:
        if self.closed.is_set() or (self.worker is not None and self.worker.is_alive()):
            return False
        while not self.results.empty():
            self.results.get_nowait()
        self.pending = True
        self.started_at = time.monotonic()
        def work():
            try:
                event = {'kind': 'result', 'value': self.checker(**request)}
            except Exception as exc:
                event = {'kind': 'error', 'message': type(exc).__name__ + ': ' + str(exc)}
            event['finished_at'] = time.monotonic()
            if not self.closed.is_set():
                self.results.put(event)
        self.worker = threading.Thread(target=work, name='agentbus-public-snapshot', daemon=True)
        self.worker.start()
        return True

    def poll(self):
        if self.closed.is_set() or not self.pending:
            return None
        try:
            event = self.results.get_nowait()
        except queue.Empty:
            event = None
        finished = event.get('finished_at') if event else time.monotonic()
        if finished - self.started_at >= self.timeout_s:
            self.pending = False
            return {'kind': 'timeout', 'message': '操作等待超时，旧记录保留；当前结果未确认'}
        if event is None:
            return None
        self.pending = False
        return event

    def close(self):
        self.closed.set()
        self.pending = False


def _invitation_action(*, operation, cfg, caller, **request):
    """Program action invoked in the data worker, never a model or Tk call."""
    from .member_invitation import prepare_invitation, record_invitation_event
    if operation == 'prepare':
        value = prepare_invitation(cfg, caller, **request)
        if value.get('state') == 'committed' and value.get('input_path') and value.get('output_path'):
            value['wizard'] = registered_wizard(cfg, request['member'], value,
                                                writable=request.get('writable', True))
        return value
    if operation == 'copy':
        record_invitation_event(cfg, caller, request['invite_id'], 'copied', request['source'])
        return {'copied_recorded': True}
    raise ValueError('Unknown invitation action')


def registered_wizard(cfg, member, value, *, writable=True):
    """Project the registered invitation; retain its immutable task instructions."""
    if value.get('state') != 'committed' or not value.get('task_id'):
        raise ValueError('接入任务尚未由登记接口建立')
    model = build_wizard({
        'workspace': str(cfg.workspace), 'member_id': member,
        'display_name': cfg.worker(member).get('display_name') or member,
        'coordinator': cfg.director, 'task_id': value['task_id'],
        'inbox_path': str(cfg.workspace / '.agentbus/agents' / member / 'ready'
                          / (value['task_id'] + '.json')),
        'input_path': str(cfg.workspace / value['input_path']),
        'output_path': str(cfg.workspace / value['output_path']),
        'writable': writable, 'saved': True, 'route': 'file',
        'budget_s': min(cfg.gates.get('task_timeout_s', 1800), 1800),
    })
    model['instructions'] = dict(value['instructions'])
    model['sample']['task_id'] = value['task_id']
    model['continuity_boundary'] = (
        '指令用于接入当前会话；每轮能否继续取决于宿主能力与真实回执。'
        '会话停止或更换聊天时，按续接包重新确认项目和任务。')
    return model


def skill_directory_relative(workspace: Path | str, selected: Path | str) -> str:
    """Validate an existing picker directory without following outside links."""
    files = WorkspaceFiles(workspace)
    path = Path(os.path.abspath(selected))
    if not path.is_relative_to(files.workspace):
        raise ValueError('请选择当前项目内的公共技能目录。')
    relative = files._relative(path.relative_to(files.workspace).as_posix())
    files._inspect(files.workspace / relative, directory=True)
    return relative


def check_skill_packages(workspace: Path | str, source: str, target: str, *,
                         host: str, scope: str, route: str) -> dict:
    """Inspect public source/target packages and compose the three pure layers.

    No deployment record is inferred from a directory, even with equal bytes.
    Missing targets are checked for safe ancestry, then stay absent.
    """
    files = WorkspaceFiles(workspace)
    source = files._relative(source)
    target = files._relative(target)
    target_path = files.workspace.joinpath(*target.split('/'))
    snapshot = files._inspect(target_path, missing=True, directory=True)
    source_package = inspect_package(files.workspace, source, **SKILL_CHECK_LIMITS)
    after = files._inspect(target_path, missing=True, directory=True)
    if any(after.get(node) != identity for node, identity in snapshot.items()):
        raise WorkspaceFileError('FILE_CHANGED', '预览目标路径在检查期间发生变化，请重新检查。')
    target_package = (None if snapshot.get(target_path) is None else
                      inspect_package(files.workspace, target, **SKILL_CHECK_LIMITS))
    preview = build_sync_preview(source_package, target_package, None,
                                 host=host, scope=scope, route=route)
    return {'source': source_package, 'target': target_package,
            'target_path': target, 'workspace': str(files.workspace),
            'preview': preview, 'presentation': present_preview(preview),
            'checked_at': datetime.now().astimezone().isoformat(timespec='seconds')}


def render_skill_check(result: dict) -> str:
    """Format inspected manifests and existing presentation text without writes."""
    view = result['presentation']
    preview = result['preview']
    lines = [view['title'], view['summary'],
             f"项目：{result['workspace']}",
             f"检查时间：{result['checked_at']}",
             f"宿主：{preview['host']}  作用域：{preview['scope']}  路线：{preview['route']}",
             f"维护者：{view['owner']}",
             '原因代码：' + '、'.join(preview['reason_codes']),
             '原因说明：' + '；'.join(view['reason_texts']), '',
             '安装：' + view['installation_label'],
             '发现：' + view['discovery_label'],
             '调用：' + view['invocation_label'],
             '执行版本：' + view['execution_version_label'], '',
             '当前只读检查与预览，不部署、不执行技能脚本。',
             SKILL_CHECK_BOUNDARY,
             '来源：完整目录检查 / 维护者预览 / 正式展示模块；Codex GUI 集成，2026-10-05。']
    for label, package in (('源技能', result['source']), ('目标技能', result['target'])):
        lines.extend(['', label])
        if package is None:
            lines.append(f"{result['target_path']}：不存在；本次不会创建目录。")
            continue
        lines.extend([f"目录：{package['source']}",
                      f"目录内容摘要：{package['content_digest']}",
                      f"文件：{len(package['files'])}  原始字节总计：{package['total_bytes']}",
                      '清单（路径 / 字节数 / SHA256）：'])
        lines.extend(f"{item['path']} / {item['bytes']} / {item['sha256']}"
                     for item in package['files'])
        lines.extend('提示：' + warning for warning in package.get('warnings', []))
    lines.extend(['', *view['notes'], '', '下一步：' + view['next_action']])
    return '\n'.join(lines)


class SkillCheckTask:
    """A single bounded inspection worker; only its queue crosses into Tk.

    Timeout stops accepting the result, not a blocked operating-system read.
    No replacement worker starts until the original has returned. Closing
    discards queued/late results and never schedules a Tk callback.
    """
    def __init__(self, *, checker=check_skill_packages, timeout_s=30):
        self.checker = checker
        self.timeout_s = timeout_s
        self.worker: threading.Thread | None = None
        self.results = queue.Queue()
        self.closed = threading.Event()
        self.started_at = 0.0
        self.pending = False

    def start(self, **request) -> bool:
        if self.closed.is_set() or (self.worker is not None and self.worker.is_alive()):
            return False
        while not self.results.empty():
            self.results.get_nowait()
        self.started_at = time.monotonic()
        self.pending = True

        def work():
            try:
                event = {'kind': 'result', 'value': self.checker(**request)}
            except Exception as exc:
                event = {'kind': 'error', 'message': f'{type(exc).__name__}: {exc}'}
            if not self.closed.is_set():
                self.results.put(event)

        self.worker = threading.Thread(target=work, name='agentbus-skill-check', daemon=True)
        self.worker.start()
        return True

    def poll(self) -> dict | None:
        if self.closed.is_set() or not self.pending:
            return None
        if time.monotonic() - self.started_at >= self.timeout_s:
            self.pending = False
            return {'kind': 'timeout', 'message': '检查等待超过 30 秒，结果未采用；现有内容已保留。'}
        try:
            event = self.results.get_nowait()
        except queue.Empty:
            return None
        self.pending = False
        return event

    def close(self) -> None:
        self.closed.set()
        self.pending = False


class SkillCheckDialog:
    """Saved-project read-only UI. All widget access stays on the Tk thread."""
    def __init__(self, root, cfg, cfg_guard):
        self.workspace = cfg.workspace
        self.cfg_guard = cfg_guard
        self.task = SkillCheckTask()
        self.closed = False
        self.after_id = None
        self.window = tk.Toplevel(root)
        self.window.title('技能检查 · AgentBus')
        self.window.geometry('960x720')
        self.window.minsize(700, 530)
        self.window.transient(root)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Destroy>', self._on_destroy, add='+')
        form = ttk.Frame(self.window, padding=(16, 12))
        form.pack(fill='x')
        ttk.Label(form, text='技能检查', style='Title.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(form, text='只读 · 不部署', style='Hint.TLabel').grid(row=0, column=1, sticky='w')
        ttk.Label(form, text=f'当前已保存项目：{self.workspace}', style='Hint.TLabel',
                  wraplength=870).grid(row=1, column=0, columnspan=3, sticky='w', pady=(4, 10))
        self.source = tk.StringVar()
        self.target = tk.StringVar()
        for row, title, variable in ((2, '源技能目录', self.source), (3, '预览目标目录', self.target)):
            ttk.Label(form, text=title).grid(row=row, column=0, sticky='w', pady=4)
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky='ew', padx=8)
            ttk.Button(form, text='选择目录', command=lambda v=variable: self._choose(v)).grid(
                row=row, column=2, sticky='e')
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='填写项目内公共相对路径（如 skills/demo）；目标可尚不存在。',
                  style='Hint.TLabel').grid(row=4, column=0, columnspan=3, sticky='w', pady=4)
        context = ttk.Frame(form)
        context.grid(row=5, column=0, columnspan=3, sticky='w', pady=(6, 0))
        hosts = tuple(cfg.enabled_workers())
        self.host = tk.StringVar(value=cfg.director if cfg.director in hosts else (hosts[0] if hosts else ''))
        self.scope = tk.StringVar(value='project')
        self.route = tk.StringVar(value='desktop')
        for title, variable, choices in (('宿主', self.host, hosts),
                                          ('作用域', self.scope, ('project', 'global')),
                                          ('路线', self.route, ('desktop', 'cli', 'independent'))):
            ttk.Label(context, text=title).pack(side='left', padx=(0, 6))
            ttk.Combobox(context, textvariable=variable, values=choices, state='readonly',
                         width=13).pack(side='left', padx=(0, 16))
        ttk.Label(form, text=('作用域和路线仅描述本次预览；两条路径始终限当前项目，'
                             '不会读取宿主全局技能或私有配置。\n' + SKILL_CHECK_BOUNDARY),
                  style='Hint.TLabel', wraplength=880, justify='left').grid(
                      row=6, column=0, columnspan=3, sticky='w', pady=(10, 0))
        actions = ttk.Frame(self.window, padding=(16, 8, 16, 12))
        actions.pack(side='bottom', fill='x')
        self.check_button = ttk.Button(actions, text='检查与预览', command=self._start)
        self.check_button.pack(side='left')
        ttk.Button(actions, text='关闭', command=self.close).pack(side='right')
        self.status = tk.StringVar(value='选择技能后检查；安装、发现、调用均未验证。')
        ttk.Label(actions, textvariable=self.status, style='Hint.TLabel', wraplength=660).pack(
            side='left', padx=12)
        self.output = scrolledtext.ScrolledText(self.window, font=FONT, wrap='word',
                                              state='disabled', bg=COLORS['surface'])
        self.output.pack(fill='both', expand=True, padx=16, pady=(0, 8))
        self._text('选择源技能和预览目标，点击“检查与预览”。当前只读，不提供同步动作。')
        self.after_id = self.window.after(100, self._poll)

    def _choose(self, variable):
        selected = filedialog.askdirectory(parent=self.window, initialdir=str(self.workspace),
                                           title='选择项目内公共技能目录', mustexist=True)
        if selected:
            try:
                variable.set(skill_directory_relative(self.workspace, selected))
            except (OSError, ValueError) as exc:
                messagebox.showwarning('目录不可用', str(exc), parent=self.window)

    def _start(self):
        if self.closed:
            return
        cfg = self.cfg_guard()
        if cfg is None or cfg.workspace != self.workspace:
            self.status.set('项目或团队有未保存变更；先保存团队并重新打开技能检查。')
            return
        if not self.source.get() or not self.target.get() or not self.host.get():
            self.status.set('请填写源技能目录、预览目标目录，并选择宿主。')
            return
        if not self.task.start(workspace=self.workspace, source=self.source.get(),
                               target=self.target.get(), host=self.host.get(),
                               scope=self.scope.get(), route=self.route.get()):
            self.status.set('前一次检查仍在收尾，请稍后重试。')
            return
        self.check_button.configure(state='disabled')
        self.status.set('正在只读检查，最多等待 30 秒…')
        self._text('正在读取公共技能目录；尚未产生检查结果。')

    def _text(self, text):
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.insert('end', text)
        self.output.configure(state='disabled')

    def _poll(self):
        self.after_id = None
        if self.closed:
            return
        event = self.task.poll()
        if event:
            if event['kind'] == 'result':
                self._text(render_skill_check(event['value']))
                self.status.set('检查完成 · 只读预览，尚未同步。')
            else:
                self._text('未取得可采用的检查结果。\n' + event['message'] +
                           '\n本次未部署，现有目标已保留。')
                self.status.set('检查超时；结果未采用。' if event['kind'] == 'timeout' else
                                '检查失败，请按原因修正路径或技能包。')
        if not self.task.pending and (self.task.worker is None or not self.task.worker.is_alive()):
            self.check_button.configure(state='normal')
        if not self.closed:
            self.after_id = self.window.after(100, self._poll)

    def _on_destroy(self, event):
        if event.widget is self.window:
            self.close(destroy=False)

    def close(self, *, destroy=True):
        if self.closed:
            return
        self.closed = True
        self.task.close()
        if self.after_id is not None:
            try:
                self.window.after_cancel(self.after_id)
            except tk.TclError:
                pass  # 资源清理类：窗口已销毁时 after_cancel 失败属预期。
            self.after_id = None
        if destroy:
            self.window.destroy()


def read_usage_guide() -> str:
    """Read the fixed, size-bounded application resource, never the project.

    Source: real WorkBuddy usability review, Codex, 2026-10-04.
    Frozen deployments must retain their own bundled resource; a missing
    bundle cannot fall back to another checkout or the selected workspace.
    """
    bundle = getattr(sys, '_MEIPASS', None)
    root = Path(bundle) if bundle else Path(__file__).absolute().parent.parent
    path = Path(os.path.abspath(root / 'share_assets' / '开始使用.md'))
    if any(part.casefold() == '.workbuddy' for part in path.parts):
        raise ValueError('使用指南资源不能位于私有配置目录。')
    for node in reversed((path, *path.parents)):
        info = node.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('使用指南资源不允许链接或重解析路径。')
        if node != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('使用指南资源目录不可用。')
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('使用指南资源必须是普通文件。')
    limit = 256 * 1024
    if info.st_size > limit:
        raise ValueError('使用指南资源过大，超过 256 KiB 限制。')
    with path.open('rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise ValueError('使用指南资源在读取前发生变化。')
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('使用指南资源大小超过限制。')
    return data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')


def render_connection_summary(value: dict, selected: str = '') -> str:
    """Readable file evidence, never a certification of a current chat.

    Source: user and independent Trae/ZCode product review, Codex, 2026-10-04.
    Native chat admission and unattended relay are independent acceptance
    checks; configuration and file receipts cannot certify either one.
    """
    members = value.get('members') or []
    lines = [f"当前项目：{value.get('workspace', '尚未选择')}",
             f"组长：{value.get('coordinator', '尚未选择')}",
             '配置已保存不代表当前会话已接入；历史回执不代表当前会话在线。',
             '当前会话未确认。文件消息状态与软件运行状态分别核验。']
    if not members:
        lines.append('先选择项目、勾选成员和组长，然后保存团队。')
        return '\n'.join(lines)
    lines.append('状态来源：本项目配置与文件总线记录，不认证软件原生能力或当前会话。')
    lines.append('验收下一步：在当前项目的新对话调用小组技能，核对任务 ID、认领和关联回执；独立接口还需核验连续多轮、空闲后接力和重启恢复。')
    if any(item.get('name') in ('trae', 'workbuddy', 'zcode') for item in members):
        lines.append('独立接口提示：厂商 CLI 的认证与权限需单独确认，不会自动继承桌面 GUI 会话的授权；桌面接入不能代替 CLI 验收。')
    for item in members:
        name = item.get('name', '')
        marker = ' → 当前选择' if name == selected else ''
        indicator = item.get('indicator') or {}
        mode_label = str(item.get('mode_label') or '已保存配置')
        if mode_label.startswith('独立接口'):
            relay = '未验证（未完成多轮/空闲/重启验收）'
        elif mode_label == '对话接入':
            relay = '未验证（桌面对话需人工接手）'
        else:
            relay = '未验证（接入方式未确认）'
        admission_at = item.get('admission_confirmation_at')
        admission = (f'已登记 {admission_at}（不是在线验证）'
                     if admission_at else '尚未登记（不是在线验证）')
        lines += [f"{indicator.get('symbol') or '●'} {item.get('display_name') or name} [{name}]{marker} · "
                  + str(indicator.get('label') or '状态未确认'),
                  '接入方式：' + mode_label + ' · 文件接入确认：' + admission,
                  '原生新聊天接入：未验证（需当前聊天调用技能）',
                  '长期无人接力：' + relay]
        lines.append(f"任务回执：{item.get('receipt_count', 0)} 条"
                     f"（成功 {item.get('successful_receipt_count', 0)} / 失败 {item.get('failed_receipt_count', 0)}） · 最近："
                     + str(item.get('last_receipt_at') or '尚无记录'))
        outcome = item.get('latest_task_outcome')
        if outcome in ('done', 'failed'):
            lines[-1] += (' · 最近任务结果：' + ('成功' if outcome == 'done' else '失败')
                         + ' · ' + str(item.get('latest_outcome_at') or '时间未确认'))
        elif item.get('failed_receipt_count', 0):
            lines[-1] += ' · 历史失败记录，完成时间未确认'
        lines.append('下一步：' + str(item.get('next_step') or
                     '打开当前项目，在成员新对话中发送接入指令，再核对任务回执。'))
    lines += ['接入流程：1. 在所选 Agent 中打开上面的项目并新建对话。',
              '2. 点击“复制接入指令”，在该对话粘贴并发送。复制不会派发任务。',
              '3. 本页刷新总线记录；正式任务应有任务 ID、认领和关联回执。',
              '任务可保存、等待人工接手只说明人工交接可用；再次需要人工唤醒不算无人接力通过。']
    return '\n'.join(lines)


def read_latest_receipt(cfg, member: str, item: dict) -> dict:
    """Explicit-click detail reader; no writes, directory creation or execution.

    Source: actual Trae product review and Codex, 2026-10-04. Automatic status
    summaries never include bodies. Only these two associated records are read.
    """
    import re
    validate_agent_id(member)
    if member not in cfg.enabled_workers():
        raise ValueError('成员未加入当前团队。')
    target = item.get('latest_receipt_to')
    if target not in [*cfg.enabled_workers(), 'director', 'human']:
        raise ValueError('回执收件人不属于当前项目。')
    validate_agent_id(target)
    task_id, receipt_id = item.get('latest_task_id'), item.get('latest_receipt_id')
    for identifier in (task_id, receipt_id):
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,96}', identifier):
            raise ValueError('没有可查看的任务或回执 ID。')

    def read(path):
        if not _checked(path):
            raise ValueError('回执记录不存在或路径未通过检查。')
        before = path.lstat()
        if before.st_size > MAX_BYTES:
            raise ValueError('回执记录超过读取上限。')
        with path.open('rb') as handle:
            current = os.fstat(handle.fileno())
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                raise ValueError('回执记录在读取前发生变化。')
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('回执记录超过读取上限。')
        raw = json.loads(data.decode('utf-8'))
        if not isinstance(raw, dict):
            raise ValueError('回执格式需要检查。')
        if not isinstance(raw.get('body', ''), str):
            raise ValueError('回执正文必须是文本。')
        message = Message.from_dict(raw)
        message.validate()
        return message

    base = cfg.workspace / '.agentbus/agents'
    task = read(base / member / 'done' / f'{task_id}.json')
    if task.id != task_id or task.to != member or task.type not in ('task', 'goal', 'review'):
        raise ValueError('原任务与成员关联不一致。')
    if task.status not in ('done', 'failed'):
        raise ValueError('原任务尚未完成，不能作为终态回执显示。')
    paths = [base / target / state / f'{receipt_id}.json'
             for state in ('ready', 'processing', 'blocked', 'done')]
    existing = [path for path in paths if _checked(path)]
    if len(existing) != 1:
        raise ValueError('回执缺失或存在重复副本，无法确定唯一记录。')
    receipt = read(existing[0])
    if (receipt.id != receipt_id or receipt.type != 'result' or receipt.sender != member
            or receipt.reply_to != task.id or receipt.to != target or task.result_to != target
            or receipt.result_to not in (None, target)):
        raise ValueError('回执与原任务、成员或路由关联不一致。')
    return {'task_id': task.id, 'receipt_id': receipt.id, 'member': member,
            'to': target, 'status': task.status, 'at': receipt.ts,
            'body': receipt.body}


def host_loading_hint(name: str) -> str:
    """File verification is independent of host loading; Trae peer, 2026-10-03."""
    if name == 'trae':
        return ('Trae：在设置「技能与命令」确认 agentbus-team 已启用。'
                '文件检查不能证明 TraeWork/当前会话已加载；未发现时复制接入句。')
    if name == 'opencode':
        return 'OpenCode：独立 ACP 记录与桌面聊天分开；在当前项目对话使用接入句。'
    return '文件检查不能证明宿主已加载技能；新聊天未发现时复制接入句。'


def render_maintenance(value: dict) -> str:
    """Readable first screen, with machine-readable details retained below."""
    labels = {'installed': '文件已安装', 'not_installed': '未安装',
              'partial': '部分完成 / 需要处理', 'invalid': '登记异常，保留原文件',
              'uninstalled': '已移除', 'missing': '未安装', 'missing_file': '文件缺失',
              'missing_launcher': '程序路径已失效', 'modified': '用户修改，予以保留',
              'invalid_launcher': '程序入口校验未通过',
              'unregistered': '未登记，予以保留', 'healthy': '检查通过',
              'attention': '需要处理'}
    status_label = labels.get(value.get('status'), value.get('status', '未知'))
    if (value.get('status') == 'partial' and value.get('applied')
            and value.get('preserved') and not value.get('errors')):
        # An intentional ownership boundary is not a failed removal.
        status_label = ('已移除归属文件；用户文件按约保留' if value.get('removed')
                        else '没有可移除的归属文件；其余文件按约保留')
    lines = [f"状态：{status_label}"]
    for host, item in value.get('hosts', {}).items():
        label = ('已移除' if value.get('applied') and item.get('skill') in value.get('removed', [])
                 else labels.get(item.get('status'), '文件安装完成'))
        lines.append(f"{host}：{label}\n  {item.get('skill', '')}")
        if item.get('launcher_reason'):
            lines.append('  ' + item['launcher_reason'])
    if 'removed' in value:
        lines.append(f"已移除：{len(value['removed'])} 个文件；可移除：{len(value.get('removable', []))} 个；保留：{len(value.get('preserved', []))} 个")
        if not value.get('applied'):
            lines.append('这是预览，尚未删除任何文件。')
    for check in value.get('checks', []):
        code = check.get('code', '')
        if code.startswith('member:'):
            lines.append(code.split(':', 1)[1] + '：' + check.get('message', ''))
        elif code.startswith('job:'):
            continue  # Compact translated job summary follows; raw reason stays in details.
        else:
            lines.append(check.get('message', ''))
    states = {'completed': '已完成', 'blocked': '阻塞，需要处理', 'queued': '待接手',
              'working': '工作中', 'awaiting_summary': '回执已到，等待验收汇总',
              'waiting_handoff': '等待对话接手',
              'awaiting_handoff': '等待对话接手', 'awaiting_review': '等待同伴评审'}
    for job in value.get('jobs', []):
        lines.append(f"作业 {job['id']}：{states.get(job['state'], job['state'])}")
    lines.extend(value.get('errors', []))
    lines.append('文本安装与检查不代表各软件已加载技能或自动运行通过。项目和任务记录不会被移除。')
    return '\n'.join(lines) + '\n\n技术详情：\n' + json.dumps(value, ensure_ascii=False, indent=2)


def configure_initial_window(root: tk.Tk) -> None:
    """Keep the status/job area visible at Windows display scaling.

    Source: actual frozen GUI at high DPI, Codex, 2026-10-03.
    A fixed pixel geometry could leave the entire jobs text area off-screen.
    Windows' maximized state uses the available work area, including taskbar.
    """
    root.geometry("1180x760")
    root.minsize(1100, 620)
    if sys.platform == "win32":
        try:
            root.state("zoomed")
        except tk.TclError:
            pass  # 资源清理类：非 Windows 或无 WM 支持 zoomed 时忽略。


def worker_label(name: str, conf: dict) -> str:
    """Show configured capability and the selected transport route."""
    display = conf.get('label') or conf.get('display_name') or name
    display = ' '.join(str(display).split())[:80]
    label = f'{display} [{name}]' if display != name else name
    transport = conf.get('transport', 'file')
    route = '文件主路线' if transport == 'file' else '独立 CLI（已验收）'
    if name == "anthropic-api" or conf.get("kind") == "http-llm":
        return f"{label}（API 通道·可能产生费用）"
    if conf.get("can_spawn"):
        if name == "opencode" and KNOWN_AGENTS[name]["can_spawn"]:
            return f"{label}（已配置·桌面文件交接·独立接口可选·{route}）"
        return f"{label}（启动入口·任务需验收·{route}）"
    if conf.get("candidate_command"):
        return f"{label}（{route}·GUI 接手·ACP 待验）"
    return f"{label}（{route}·GUI 会话·需人工接手）"


def jobs_summary(cfg: Config, jobs: list[dict]) -> str:
    """Human-facing progress; preserve the complete payload in technical view."""
    states = {'queued': '已排队', 'working': '正在执行',
              'waiting_handoff': '等待接手', 'awaiting_summary': '等待组长验收与汇总',
              'blocked': '受阻，需处理', 'stopped': '已暂停派发', 'completed': '已验收完成'}
    locations = {'ready': '待办信箱', 'processing': '执行中信箱',
                 'done': '已归档信箱', 'blocked': '受阻信箱', 'missing': '未找到消息'}
    outcomes = {'pending': '待执行', 'claimed': '已认领', 'done': '已完成',
                'failed': '执行失败', 'blocked': '受阻', 'missing': '消息缺失'}
    reasons = {
        'a selected GUI member needs its active conversation to receive this task':
            '需要把任务送入所选成员正在使用的桌面对话。',
        'coordinator needs an active GUI conversation': '需要把目标送入组长正在使用的桌面对话。',
        'associated peer review is unfinished': '同伴评审尚未完成，等待评审回执。',
        'tasks have receipts; coordinator must verify artifacts and finalize explicitly':
            '任务已有回执；组长须核对实际产物后明确结束作业。',
        'goal archived, but no verified job finalization exists': '目标已归档，组长尚未确认产物验收。',
        'STOP is active; existing work and checkpoints are preserved':
            'STOP 已阻止新派发，已有任务与续接记录保留；在途调用不会被强制中断。',
        'job wall-clock budget expired; budget does not reset after restart':
            '作业时限已到；换对话不会重新计算预算。',
    }
    lines = ['当前项目：' + str(cfg.workspace), '']
    if not jobs:
        lines += ['暂无已登记作业。', '在任一成员对话说：使用这个小组工作：你的目标。']
    for index, job in enumerate(jobs, 1):
        lines += [f"{index}. {job.get('goal', '未提供目标')}",
                  f"作业：{job.get('id', '未知')}  ·  状态：{states.get(job.get('state'), job.get('state', '未知'))}",
                  f"发起者：{job.get('caller', '未知')}  ·  组长：{job.get('coordinator', '未知')}",
                  f"待评审：{len(job.get('pending_reviews') or [])}"]
        if job.get('parent_job_id'):
            parent = job.get('parent_snapshot') or {}
            lines += [f"上一阶段：{job['parent_job_id']}  ·  "
                      f"已完成任务：{len(parent.get('done_task_ids') or [])}",
                      '这是明确开启的新阶段，旧阶段期限与记录保留。']
        if job.get('goal_location') == 'processing':
            lines += [f"目标认领者：{job.get('goal_claimed_by') or '未完整登记'}  ·  "
                      f"目标状态：{outcomes.get(job.get('goal_status'), '未知')}",
                      f"目标文件未更新：{job.get('goal_heartbeat_age_s', 0):.0f} 秒"]
            if not job.get('goal_claim_complete'):
                lines.append('认领未完整提交；先核对原执行者，不能抢占或直接重做。')
            if job.get('goal_heartbeat_stale'):
                lines.append('文件未更新不证明进程死亡；先核对原执行者，不能抢占或直接重做。')
        tasks = job.get('tasks') or []
        if not tasks:
            lines.append('尚未派发步骤；等待组长制定计划。')
        for task_index, task in enumerate(tasks, 1):
            lines += [f"  步骤 {task_index}：{task.get('title', '未命名任务')}",
                      f"  执行成员：{task.get('worker', '未知')}  ·  "
                      f"位置：{locations.get(task.get('location'), task.get('location', '未知'))}  ·  "
                      f"状态：{outcomes.get(task.get('status'), task.get('status', '未知'))}",
                      f"  任务 ID：{task.get('id', '未知')}  ·  结果收件人：{task.get('result_to', '未声明')}"]
            if task.get('heartbeat_stale'):
                lines.append('  心跳过期（文件未更新，不证明进程死亡）：请核对原认领者状态，不能抢占或直接重做。')
        checkpoints = job.get('checkpoints') or []
        if checkpoints:
            last = checkpoints[-1]
            lines += [f"最近续接记录：{last.get('caller', '未知')} · {last.get('at', '')}",
                      str(last.get('note', ''))]
        reason = job.get('reason')
        if reason:
            lines.append('当前说明：' + reasons.get(reason, str(reason)))
        finalization = job.get('finalization') or {}
        if finalization.get('committed'):
            lines.append('验收汇总：' + str(finalization.get('summary', '')))
        lines += ['', '────────────────────────────────────────', '']
    opencode = cfg.enabled_workers().get('opencode', {})
    if opencode.get('can_spawn'):
        records = cfg.workspace / '.agentbus/agents/opencode'
        lines += ['OpenCode 可使用当前项目桌面文件交接；独立接口为可选路线，须按所选路线验收。',
                  '任务与回执记录：' + str(records),
                  'ready=待办，processing=执行中，done=完成或失败的归档；回执按结果收件人保存。',
                  '作业记录：' + str(cfg.workspace / '.agentbus/jobs'),
                  '会话记录：' + str(cfg.workspace / '.agentbus/sessions'),
                  '可点击“打开OpenCode记录”；完整字段请点击“技术详情”。']
    return '\n'.join(lines)


def merge_probe_patch(data: dict, patch: dict[str, dict]) -> dict:
    """Refresh detected launch details while retaining the user's group choices."""
    workers = data.setdefault("workers", {})
    user_settings = ("enabled", "executor_enabled", "sandbox", "model",
                     "timeout_s", "startup_timeout_s", "permission_mode", "human_in_loop",
                     "can_spawn")
    for name, detected in patch.items():
        current = workers.get(name, {})
        merged = {**current, **detected}
        for key in user_settings:
            if key in current:
                merged[key] = current[key]
        workers[name] = merged
    return data


def initial_gui_group(data: dict, patch: dict[str, dict] | None = None) -> dict:
    """Default a fresh GUI workspace to a Codex-only group.

    Detected peers remain present as selectable members. The optional API
    channel and future non-agent entries retain their existing defaults.
    """
    workers = data.setdefault("workers", {})
    for name, worker in workers.items():
        if name in {"codex", "workbuddy", "trae", "zcode", "opencode"}:
            selected = name == "codex"
            worker["enabled"] = selected
            worker["executor_enabled"] = selected
    data["director"] = "codex"
    return merge_probe_patch(data, patch or {})


def launch_argv() -> list[str]:
    """返回"怎么再拉起一个 agentbus"的命令行。

    打包后要特别小心：GUI 版 exe 是窗口子系统的，stdout 可能为 None，
    拿它当 MCP 服务器会握手失败。所以优先用同目录下的控制台版 exe。
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        cli = exe.with_name("agentbus-cli.exe")
        return [str(cli if cli.exists() else exe)]
    return [sys.executable, "-m", "agentbus"]


def _enable_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass  # 资源清理类：非 Windows 平台无 DPI API 时忽略。


# --------------------------------------------------------------------------- #
# 计划审批对话框
# --------------------------------------------------------------------------- #


class PlanDialog(tk.Toplevel):
    """组长完成任务计划后，由用户确认派发。"""

    def __init__(self, master, tasks: list[dict], goal: str):
        super().__init__(master)
        self.title("确认任务清单")
        self.transient(master)
        self.resizable(True, True)
        self.geometry("760x560")
        self.decision: list[dict] | None = None

        # Reserve the action row before expandable content. A long goal must
        # never consume the space needed to approve or cancel the dispatch.
        btns = ttk.Frame(self)
        btns.pack(side="bottom", fill="x", padx=14, pady=(0, 14))
        ttk.Button(btns, text="取消（不派发）", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="批准并开始", style="Accent.TButton",
                   command=self._approve).pack(side="right", padx=8)

        goal_preview = goal if len(goal) <= 240 else goal[:240] + "…（完整目标见主窗口）"
        ttk.Label(self, text=f"目标：{goal_preview}", font=FONT,
                  wraplength=720, justify="left").pack(anchor="w", padx=14, pady=(12, 6))
        ttk.Label(self, text=f"组长拆出 {len(tasks)} 个任务。批准并开始后才会投递到成员信箱。",
                  font=FONT_SMALL, foreground="#555").pack(anchor="w", padx=14)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=14, pady=8)

        self.listbox = tk.Listbox(body, font=FONT, activestyle="none",
                                  selectmode="browse", height=8)
        self.listbox.pack(side="left", fill="both", expand=False)
        for i, t in enumerate(tasks, 1):
            self.listbox.insert("end", f" {i}. [{t['worker']}] {t['title']}")
        self.listbox.selection_set(0)

        self.detail = scrolledtext.ScrolledText(body, wrap="word", font=FONT_MONO,
                                                state="disabled", bg="#fbfbfb")
        self.detail.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self._tasks = tasks
        self.listbox.bind("<<ListboxSelect>>", self._show)

        self._show()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        self.wait_window(self)

    def _show(self, _evt=None) -> None:
        sel = self.listbox.curselection()
        i = sel[0] if sel else 0
        if i >= len(self._tasks):
            return
        t = self._tasks[i]
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end",
                           f"执行者：{t['worker']}\n结果收件人：{t.get('result_to', '待组长确认')}\n\n{t['body']}")
        self.detail.configure(state="disabled")

    def _approve(self) -> None:
        self.decision = self._tasks
        self.destroy()

    def _cancel(self) -> None:
        self.decision = None
        self.destroy()


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #


class Launcher:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker: threading.Thread | None = None
        self._close_requested = False
        self._closed = False
        self.probes: dict = {}
        self.cfg = None
        self.bus: Bus | None = None
        self.director: Director | None = None
        self.watchtower: Watchtower | None = None
        self.watch_stop: threading.Event | None = None
        self.watch_thread: threading.Thread | None = None
        self.worker_vars: dict[str, tk.BooleanVar] = {}
        self._skill_dialog: SkillCheckDialog | None = None
        self._wrap_labels = []
        self._subnavigation = {}
        self._layout_scale = float(root.tk.call('tk', 'scaling'))
        self.pages = {}
        self.nav_buttons = {}
        self.current_page = 'home'
        self._overview_task = OverviewSnapshotTask(checker=read_dashboard_snapshot)
        self._overview_poll_id = None
        self._overview_snapshot = None
        self._overview_read_error = None
        self._overview_request_key = None
        self._invitation_task = None
        self._invitation_request = None
        self._invitation_poll_id = None
        self._task_tree_action_task = None
        self._task_tree_action_context = None
        self.root.bind('<Destroy>', self._close_display_tasks, add='+')

        root.title(f"AgentBus · 项目工作台 · {__version__}")
        configure_initial_window(root)
        root.geometry('1150x760')
        root.minsize(960, 620)

        self._init_style()
        self._build_header()
        self._build_status()
        self._build_shell()
        self._build_tabs()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.log("开始使用：①选择项目 ②勾选成员、指定组长并保存 ③按‘团队接入’引导给各成员发送指令。"
                 "打开窗口只读载入配置，不启动模型或自动修改项目。", "info")
        self.log("提示：agentbus 会在这个工作空间下建 .agentbus/ 目录，"
                 "所有消息、审计、租约都在那里，随时可查。", "dim")
        self.root.after(120, self._drain)
        self.root.after(1500, self._auto_detect)
        self.root.after(200, self._overview_tick)

    # ------------------------------------------------------------------ #
    # 样式与骨架
    # ------------------------------------------------------------------ #
    def _init_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass  # 资源清理类：ttk 主题不可用时沿用默认主题。
        self.root.configure(background=COLORS['background'])
        style.configure(".", font=FONT, background=COLORS['background'],
                        foreground=COLORS['text'])
        style.configure("TFrame", background=COLORS['background'])
        style.configure("Surface.TFrame", background=COLORS['surface'])
        style.configure("Surface.TLabel", background=COLORS['surface'])
        style.configure("Section.TLabel", font=(FONT[0], TYPE_SCALE['subtitle'], 'bold'))
        style.configure("Title.TLabel", font=FONT_TITLE, foreground=COLORS['text'])
        style.configure("Hint.TLabel", font=FONT_SMALL, foreground=COLORS['secondary'])
        style.configure("Card.TLabelframe", bordercolor=COLORS['border'], relief='solid')
        style.configure("Card.TLabelframe.Label", font=FONT, foreground=COLORS['text'])
        style.configure("TButton", padding=(12, 7))
        style.configure("Accent.TButton", font=FONT, background=COLORS['accent'],
                        foreground='#ffffff', borderwidth=0, padding=(16, 7))
        style.map("Accent.TButton", background=[('disabled', '#d9e1ee'),
                                                 ('active', COLORS['accent_hover'])],
                  foreground=[('disabled', '#6b7280'), ('!disabled', '#ffffff')])
        style.configure("TNotebook", borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(16, 8))
        style.map("TNotebook.Tab", background=[('selected', COLORS['surface'])],
                  foreground=[('selected', COLORS['accent'])])
        # Real Notebook page selection remains available to existing callbacks;
        # only this shell's tab chrome is hidden. No nested tab bars are shown.
        style.layout('Workbench.TNotebook.Tab', [])
        style.configure('Workbench.TNotebook', borderwidth=0, tabmargins=0)
        style.configure('Nav.TButton', anchor='w', padding=(18, 12),
                        relief='flat', borderwidth=0, background=COLORS['surface'],
                        foreground=COLORS['secondary'])
        style.map('Nav.TButton', background=[('active', COLORS['background'])])
        style.configure('NavSelected.TButton', anchor='w', padding=(18, 12),
                        relief='flat', borderwidth=0, background=COLORS['accent'],
                        foreground=COLORS['on_accent'])
        style.map('NavSelected.TButton',
                  background=[('active', COLORS['accent_hover'])],
                  foreground=[('active', COLORS['on_accent'])])
        style.configure('Subnav.TButton', padding=(12, 6), relief='flat', borderwidth=0)
        style.configure('SubnavSelected.TButton', padding=(12, 6),
                        background=COLORS['accent'], foreground=COLORS['on_accent'])
        style.map('SubnavSelected.TButton',
                  background=[('active', COLORS['accent_hover'])],
                  foreground=[('active', COLORS['on_accent'])])
        style.configure('Treeview', font=FONT, rowheight=self._px(34),
                        background=COLORS['surface'], fieldbackground=COLORS['surface'],
                        borderwidth=0)
        style.configure('Treeview.Heading', font=(FONT[0], TYPE_SCALE['small'], 'bold'),
                        background=COLORS['background'], foreground=COLORS['secondary'],
                        padding=(8, 8))

    def _build_header(self) -> None:
        head = ttk.Frame(self.root, padding=(24, 14, 24, 14), style='Surface.TFrame')
        head.pack(fill="x")
        ttk.Label(head, text="AgentBus", font=(FONT[0], 18, 'bold'),
                  style='Surface.TLabel').pack(side="left")
        self.shell_project_var = tk.StringVar(value='选择项目，开始协作')
        self.shell_draft_var = tk.StringVar(value='团队未配置')
        context = ttk.Frame(head, style='Surface.TFrame')
        context.pack(side='left', padx=(24, 0), fill='x', expand=True)
        ttk.Label(context, textvariable=self.shell_project_var,
                  style='Surface.TLabel').pack(anchor='w')
        ttk.Label(context, textvariable=self.shell_draft_var, font=FONT_SMALL,
                  foreground=COLORS['secondary'], style='Surface.TLabel').pack(anchor='w')
        ttk.Label(head, text=f"v{__version__}", font=FONT_SMALL,
                  foreground=COLORS['secondary'], style='Surface.TLabel').pack(side="right")
        ttk.Button(head, text='使用指南', command=self._show_usage_guide).pack(side='right', padx=12)

    def _show_usage_guide(self) -> None:
        try:
            text = read_usage_guide()
        except (OSError, ValueError) as exc:
            messagebox.showwarning('使用指南不可用',
                '随程序提供的使用指南缺失或未通过读取检查。请重新获取完整分发包。\n' + str(exc))
            return
        self._show_readonly_text('使用指南', text)

    def _show_readonly_text(self, title: str, text: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title + ' · AgentBus')
        dialog.geometry('900x640')
        dialog.minsize(660, 420)
        dialog.transient(self.root)
        header = ttk.Frame(dialog, padding=(16, 12, 16, 8))
        header.pack(fill='x')
        ttk.Label(header, text=title, style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='  只读', style='Hint.TLabel').pack(side='left')
        actions = ttk.Frame(dialog, padding=(16, 8, 16, 12))
        actions.pack(side='bottom', fill='x')
        ttk.Button(actions, text='关闭', command=dialog.destroy).pack(side='right')

        def copy_guide():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

        ttk.Button(actions, text='复制全文', command=copy_guide).pack(side='left')
        box = scrolledtext.ScrolledText(dialog, wrap='word', font=FONT,
                                      state='normal', bg=COLORS['surface'])
        box.pack(fill='both', expand=True, padx=16, pady=(0, 8))
        box.insert('end', text)
        box.configure(state='disabled')

    def _px(self, value: int) -> int:
        return design_system.scaled(value, getattr(self, '_layout_scale', 96 / 72))

    def _build_shell(self) -> None:
        shell = ttk.Frame(self.root)
        shell.pack(fill='both', expand=True)
        self.sidebar = ttk.Frame(shell, width=self._px(200),
                                 padding=(12, 20), style='Surface.TFrame')
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)
        ttk.Label(self.sidebar, text='项目工作区', font=FONT_SMALL,
                  foreground=COLORS['secondary'], style='Surface.TLabel').pack(
                      anchor='w', padx=12, pady=(0, 16))
        for item in NAV_ITEMS:
            button = ttk.Button(self.sidebar, text=item['title'], style='Nav.TButton',
                                command=lambda key=item['id']: self._show_page(key))
            button.pack(fill='x', pady=(0, 6))
            self.nav_buttons[item['id']] = button
        self.root.bind('<Alt-n>', lambda _event: self._navigate_next_action())
        for index, item in enumerate(NAV_ITEMS, 1):
            self.root.bind(f'<Alt-Key-{index}>',
                           lambda _event, key=item['id']: self._show_page(key))
        ttk.Label(self.sidebar, text='文件协作\n成员平级 · 进度可追溯',
                  font=FONT_SMALL, justify='left', foreground=COLORS['secondary'],
                  style='Surface.TLabel').pack(side='bottom', anchor='w', padx=12, pady=12)
        self.content_frame = ttk.Frame(shell)
        self.content_frame.pack(side='left', fill='both', expand=True)

    def _show_page(self, page_id: str) -> None:
        page = self.pages.get(page_id)
        if page is None:
            return
        self.current_page = page_id
        self.tabs.select(page)
        self._sync_navigation()
        self._update_shell_context()
        if page_id == 'home':
            self._refresh_workbench_overview()
            focus = getattr(self.overview_next_btn, 'focus_set', None)
            if callable(focus):
                focus()

    def _sync_navigation(self, _event=None) -> None:
        selected = self.tabs.select()
        for page_id, page in self.pages.items():
            if str(page) == str(selected):
                self.current_page = page_id
                break
        for page_id, button in self.nav_buttons.items():
            button.configure(style='NavSelected.TButton' if page_id == self.current_page
                             else 'Nav.TButton')

    def _build_subnavigation(self, parent, entries: list[tuple[str, str]]) -> ttk.Notebook:
        bar = ttk.Frame(parent)
        bar.pack(fill='x', pady=(0, 12))
        notebook = ttk.Notebook(parent, style='Workbench.TNotebook')
        notebook.pack(fill='both', expand=True)
        buttons = {}
        for key, title in entries:
            buttons[key] = ttk.Button(bar, text=title, style='Subnav.TButton',
                                     command=lambda k=key, n=notebook: self._show_subpage(n, k))
            buttons[key].pack(side='left', padx=(0, 6))
        self._subnavigation[notebook] = {'buttons': buttons, 'pages': {}}
        notebook.bind('<<NotebookTabChanged>>', lambda _event, n=notebook: self._sync_subnavigation(n))
        self._wrap_action_bar(bar)
        return notebook

    def _show_subpage(self, notebook, key: str) -> None:
        item = self._subnavigation[notebook]
        if key in item['pages']:
            notebook.select(item['pages'][key])
            self._sync_subnavigation(notebook)

    def _sync_subnavigation(self, notebook) -> None:
        item = self._subnavigation[notebook]
        selected = str(notebook.select())
        for key, button in item['buttons'].items():
            active = key in item['pages'] and str(item['pages'][key]) == selected
            button.configure(style='SubnavSelected.TButton' if active else 'Subnav.TButton')

    def _wrap_action_bar(self, bar) -> None:
        """Wrap existing action widgets locally without changing their commands."""
        bar.bind('<Configure>', lambda _event, frame=bar: self._layout_action_bar(frame))

    def _layout_action_bar(self, bar) -> None:
        width = max(1, bar.winfo_width())
        children = bar.winfo_children()
        widths = tuple(child.winfo_reqwidth() for child in children)
        signature = (width, widths)
        if signature == getattr(bar, '_action_layout_signature', None):
            return
        bar._action_layout_signature = signature
        row = column = used = 0
        # Release every pack-managed sibling before introducing grid. Mixing
        # managers during the first child raises a Tcl callback error even
        # when a withdrawn-window construction test appears green.
        for child in children:
            if child.winfo_manager() == 'pack':
                child.pack_forget()
            elif child.winfo_manager() == 'grid':
                child.grid_forget()
        for child, request in zip(children, widths):
            if column and used + request + self._px(8) > width:
                row += 1
                column = used = 0
            child.grid(row=row, column=column, sticky='w', padx=(0, self._px(8)),
                       pady=(0, self._px(6)))
            used += request + self._px(8)
            column += 1

    def _register_wrap(self, label) -> None:
        if not hasattr(self, '_wrap_labels'):
            self._wrap_labels = []
        self._wrap_labels.append(label)

    def _refresh_wraplength(self, _event=None) -> None:
        width = max(self._px(160), self.content_frame.winfo_width() - self._px(64))
        for label in self._wrap_labels:
            if label.winfo_exists():
                label.configure(wraplength=width)

    def _update_shell_context(self) -> None:
        if not hasattr(self, 'shell_project_var'):
            return
        raw = self.ws_var.get().strip() if hasattr(self, 'ws_var') else ''
        title = Path(raw).name if raw else '选择项目，开始协作'
        self.shell_project_var.set(title[:80])
        cfg = getattr(self, 'cfg', None)
        if cfg is None:
            self.shell_draft_var.set('团队未配置 · 请先保存团队')
            return
        selected = {name for name, value in self.worker_vars.items() if value.get()}
        try:
            dirty = (not raw or Path(raw).expanduser().resolve() != cfg.workspace or
                     (Path(self.output_var.get().strip()).expanduser().resolve()
                      if self.output_var.get().strip() else cfg.workspace) != cfg.output_root or
                     selected != set(cfg.enabled_workers()) or
                     self.director_var.get() != cfg.director)
        except (OSError, ValueError):
            dirty = True
        self.shell_draft_var.set('有未保存修改' if dirty else
                                f'负责人 {cfg.director} · 文件协作 · 进度可追溯')

    def _overview_tick(self) -> None:
        if getattr(self, '_closed', False):
            return
        self._update_shell_context()
        if self.current_page == 'home':
            self._refresh_workbench_overview()
        self.root.after(10000, self._overview_tick)

    def _schedule_overview(self, *_args) -> None:
        if getattr(self, '_overview_pending', False):
            return
        self._overview_pending = True
        def refresh():
            self._overview_pending = False
            if not getattr(self, '_closed', False):
                self._update_shell_context()
                if self.current_page == 'home':
                    self._refresh_workbench_overview()
        self.root.after_idle(refresh)

    def _refresh_workbench_overview(self) -> None:
        if not hasattr(self, 'overview_tree'):
            return
        cfg = getattr(self, 'cfg', None)
        raw = self.ws_var.get().strip() if hasattr(self, 'ws_var') else ''
        data = dict(cfg.data) if cfg is not None else {}
        data['workspace'] = raw
        data['output_root'] = self.output_var.get().strip() if hasattr(self, 'output_var') else data.get('output_root', '.')
        try:
            data['draft_dirty'] = bool(cfg is not None and
                (not raw or Path(raw).expanduser().resolve() != cfg.workspace or
                 (Path(data['output_root']).expanduser().resolve()
                  if data['output_root'] else cfg.workspace) != cfg.output_root or
                 {name for name, var in self.worker_vars.items() if var.get()} !=
                 set(cfg.enabled_workers()) or self.director_var.get() != cfg.director))
        except (OSError, ValueError):
            data['draft_dirty'] = True
        if cfg is None or data['draft_dirty']:
            self._render_project_overview(build_project_view(data, None, None))
            return
        if getattr(self, '_task_forest_workspace', None) != str(cfg.workspace):
            self._task_forest_snapshot = {}
        key = (str(cfg.workspace), json.dumps(data, ensure_ascii=False, sort_keys=True))
        snapshot = self._overview_snapshot
        current = snapshot if snapshot and snapshot['key'] == key else None
        if current:
            self._render_snapshot(data, current['value'], read_error=self._overview_read_error)
            if time.monotonic() - current['received_at'] < 10:
                return
        else:
            self._render_project_overview(build_project_view(data, None, None),
                                           read_error='正在读取公开记录；当前状态未确认')
        if self._overview_task.start(cfg=Config(path=cfg.path, workspace=cfg.workspace,
                                                data=copy.deepcopy(cfg.data))):
            self._overview_request_key = key
            self._overview_request_data = data
            self._poll_workbench_snapshot()

    def _close_display_tasks(self, event) -> None:
        if event.widget is not self.root:
            return
        self._overview_task.close()
        task = getattr(self, '_invitation_task', None)
        if task is not None:
            task.close()
        task = getattr(self, '_recovery_task', None)
        if task is not None:
            task.close()
        task = getattr(self, '_task_tree_action_task', None)
        if task is not None:
            task.close()

    def _poll_workbench_snapshot(self) -> None:
        self._overview_poll_id = None
        if getattr(self, '_closed', False) or getattr(self, '_close_requested', False):
            self._overview_task.close()
            return
        event = self._overview_task.poll()
        if event is None:
            if self._overview_task.pending:
                self._overview_poll_id = self.root.after(80, self._poll_workbench_snapshot)
            return
        cfg = getattr(self, 'cfg', None)
        if cfg is None:
            return
        try:
            unchanged = (str(cfg.workspace) == self._overview_request_key[0]
                and Path(self.ws_var.get().strip()).expanduser().resolve() == cfg.workspace
                and (Path(self.output_var.get().strip()).expanduser().resolve()
                     if self.output_var.get().strip() else cfg.workspace) == cfg.output_root
                and {name for name, var in self.worker_vars.items() if var.get()} == set(cfg.enabled_workers())
                and self.director_var.get() == cfg.director
                and cfg.data.get('workers') == self._overview_request_data.get('workers'))
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            self._refresh_workbench_overview()
            return  # Draft/project changes invalidate the in-flight display.
        data = self._overview_request_data
        if event['kind'] == 'result':
            value = event['value']
            self._overview_read_error = None
            self._overview_snapshot = {'key': self._overview_request_key, 'value': value,
                                       'received_at': time.monotonic()}
            self._render_snapshot(data, value)
        else:
            self._overview_read_error = event['message']
            previous = self._overview_snapshot
            if previous and previous['key'] == self._overview_request_key:
                value = previous['value']
                view = build_project_view(data, value.get('health'), value.get('jobs'))
                self._render_project_overview(view, observed_at=value.get('observed_at'),
                                               read_error=event['message'])
                self._render_task_forest(value.get('task_forest'), read_error=event['message'])
            else:
                self._render_project_overview(build_project_view(data, None, None),
                                               read_error=event['message'])

    def _render_snapshot(self, data: dict, snapshot: dict, *, read_error=None) -> None:
        view = build_project_view(data, snapshot.get('health'), snapshot.get('jobs'))
        view['stopped'] = snapshot.get('stopped') is True
        view['member_evidence'] = snapshot.get('member_evidence') or {}
        incomplete = snapshot.get('records_incomplete') or view.get('records_incomplete')
        reason = snapshot.get('incomplete_reason') or view.get('incomplete_reason')
        self._render_project_overview(view, observed_at=snapshot.get('observed_at'),
                                       read_error=read_error or ((reason or '记录不完整') if incomplete else None))
        self._render_task_forest(snapshot.get('task_forest'),
                                read_error=read_error or snapshot.get('task_forest_error')
                                or ((reason or '记录不完整') if incomplete else None))

    def _render_project_overview(self, view: dict, *, observed_at=None,
                                 read_error: str | None = None) -> None:
        # This method is only called by Tk callbacks. Worker threads return
        # ordinary dictionaries and have no widget or Tk variable references.
        view = dict(view)
        if read_error and view.get('action_id') != 'team':
            view.update(action_id='tasks', next_action='查看记录与续接说明')
        light_evidence = view.get('member_evidence')
        if read_error and isinstance(light_evidence, dict):
            light_evidence = {key: dict(value, heartbeat_verified=False,
                session_active_verified=False, evidence_conflict=True)
                for key, value in light_evidence.items() if isinstance(value, dict)}
        shell = build_shell_view(
            view, width_px=self.content_frame.winfo_width(),
            scaling=getattr(self, '_layout_scale', 96 / 72),
            stopped=view.get('stopped') is True,
            member_evidence=light_evidence,
            workspace=str(getattr(getattr(self, 'cfg', None), 'workspace', '')))
        self._overview_shell = shell
        view['action_id'] = shell['primary_action']['id']
        self._overview_view = view
        self.overview_summary_var.set(view['headline'] if not read_error else '公开记录未确认')
        stage = view.get('stage_label', '阶段未确认')
        self.overview_scope_var.set(f"{stage} · 负责人 {view.get('coordinator') or '尚未选择'} · {view['route_label']}")
        self.overview_goal_var.set('当前目标：' + shell['goal'])
        source = view.get('goal_source') or {}
        source_id = str(source.get('job_id') or '尚无已确认作业')
        self.overview_goal_source_var.set(f"{stage} · 目标来源 {source_id}")
        self.overview_goal_origin_var.set(f"{stage} · " +
            ('当前项目作业' if source.get('job_id') else '尚无已确认作业'))
        for item in view.get('cards') or []:
            variable = self.overview_card_vars.get(item['id'])
            if variable is not None:
                variable.set('—' if read_error else str(item.get('count', 0)))
        self.overview_cards_note_var.set('任务交付计数需独立产物验收；轻量快照不重新验收产物。')
        self.overview_next_btn.configure(text=shell['primary_action']['label'], takefocus=True)
        if shell['empty_state'] is not None and not read_error:
            self.overview_cards_note_var.set(shell['empty_state']['label'] + ' · '
                                              + shell['empty_state']['action'])
        responsibilities = shell['visible_members']
        rows = tuple((item['member_id'], item['name'], item.get('task') or '尚无已确认分工',
                      '状态未确认' if read_error else item['state_label'],
                      item.get('checkpoint') or '尚无续接记录', item.get('next_step') or '无需动作')
                     for item in responsibilities)
        for item in responsibilities:
            light = item['status_light']
            self.overview_tree.tag_configure('light:' + light['state'], foreground=light['color'])
        if rows != getattr(self, '_overview_rows', None):
            selected = set(self.overview_tree.selection())
            for item in self.overview_tree.get_children():
                self.overview_tree.delete(item)
            tones = {item['member_id']: 'light:' + item['status_light']['state']
                     for item in responsibilities}
            for row in rows:
                self.overview_tree.insert('', 'end', iid=row[0], values=row[1:],
                                          tags=('warn' if read_error else tones.get(row[0], 'neutral'),))
            retained = [item for item in selected if self.overview_tree.exists(item)]
            if retained:
                self.overview_tree.selection_set(retained)
            self._overview_rows = rows
        note = '公开记录未确认：' + read_error if read_error else '认领不等于执行；当前会话需要真实回执确认。'
        if shell['overflow_count']:
            note += f" · 共 {shell['member_count']} 名成员，另 {shell['overflow_count']} 名见成员详情"
        self.overview_evidence_var.set(note + ' · 观察：' + str(observed_at or '尚未观察'))
        # A draft switch invalidates the displayed tree and its action target.
        if view.get('action_id') == 'team' or read_error:
            previous = getattr(self, '_task_forest_snapshot', {}) if read_error else {}
            self._render_task_forest(previous, read_error=read_error)

    def _show_member_details(self) -> None:
        view = getattr(self, '_overview_view', {})
        source = view.get('goal_source') or {}
        lines = ['项目成员与公开记录', '记录不认证身份、当前在线或持续运行。',
                 '当前目标：' + str(view.get('goal') or '未确认'),
                 '目标来源：' + str(source.get('job_id') or '未确认') + ' · '
                 + str(source.get('reason') or '来源未确认'), '']
        shell_members = getattr(self, '_overview_shell', {}).get('members', [])
        for item in shell_members:
            lines.append(f"{item['name_full']}（{item['member_id']}） · {item['role']} · {item['state_label']}\n"
                         + str(item.get('task') or '尚无已确认分工') + ' · '
                         + str(item.get('checkpoint') or '尚无续接记录') + '\n'
                         + item['status_light']['reason'])
        if not shell_members:
            lines.append('请先选择项目并保存团队。')
        self._show_readonly_text('成员详情', '\n'.join(lines))

    def _prepare_member_invitation(self) -> None:
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        member = self.connection_agent.get()
        if member not in cfg.enabled_workers():
            messagebox.showwarning('成员尚未保存', '请选择已保存团队中的成员，再生成接入指令。')
            return
        writable = self.invitation_writable.get()
        key = (str(cfg.workspace), cfg.director, member, writable)
        if self._invitation_request is None or self._invitation_request['key'] != key:
            self._invitation_request = {'key': key, 'id': 'gui-' + uuid.uuid4().hex}
        request = {'operation': 'prepare',
                   'cfg': Config(path=cfg.path, workspace=cfg.workspace, data=copy.deepcopy(cfg.data)),
                   'caller': cfg.director, 'member': member,
                   'request_id': self._invitation_request['id'], 'writable': writable}
        if self._invitation_task is None:
            self._invitation_task = OverviewSnapshotTask(checker=_invitation_action, timeout_s=15)
        if self._invitation_task.start(**request):
            self._invitation_context = request
            self.invitation_prepare_btn.configure(state='disabled')
            self._poll_member_invitation()

    def _poll_member_invitation(self) -> None:
        self._invitation_poll_id = None
        task = self._invitation_task
        if task is None:
            return
        if getattr(self, '_closed', False) or getattr(self, '_close_requested', False):
            task.close()
            return
        event = task.poll()
        if event is None:
            if task.pending:
                self._invitation_poll_id = self.root.after(80, self._poll_member_invitation)
            return
        self.invitation_prepare_btn.configure(state='normal')
        request = self._invitation_context
        if event['kind'] != 'result':
            self.log('成员接入操作未确认：' + event['message'] + '；重试保留原请求 ID。', 'warn')
            return
        if request['operation'] == 'copy':
            self.log('复制记录已保存；复制不是发送，不认证成员能力。', 'dim')
            return
        current = getattr(self, 'cfg', None)
        if current is None or current.workspace != request['cfg'].workspace:
            self.log(f"接入记录属于 {request['cfg'].workspace}；已切换项目，未显示旧项目指令。", 'warn')
            return
        self._show_invitation_dialog(request['cfg'], request['caller'], request['member'], event['value'])
        self._schedule_overview()

    def _copy_member_invitation(self, cfg, caller, invite_id, role, text) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        if self._invitation_task is None:
            self._invitation_task = OverviewSnapshotTask(checker=_invitation_action, timeout_s=15)
        request = {'operation': 'copy', 'cfg': cfg, 'caller': caller, 'invite_id': invite_id,
                   'source': f'AgentBus GUI 复制 {role} 指令；非送达、非能力认证'}
        if self._invitation_task.start(**request):
            self._invitation_context = request
            self._poll_member_invitation()
        else:
            self.log('指令已复制；已有接入记录操作未结束，本次复制事件尚未保存。', 'warn')

    def _show_invitation_dialog(self, cfg, caller, member, value) -> None:
        if value.get('state') != 'committed' or not value.get('task_id'):
            self._show_readonly_text('成员接入尚未就绪',
                f'项目：{cfg.workspace}\n成员：{member}\n\n'
                + value.get('reason', '这不是可执行邀请。请先保存团队并修复项目工作流。'))
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('成员接入指令 · AgentBus')
        dialog.geometry('960x700')
        dialog.minsize(760, 520)
        dialog.transient(self.root)
        head = ttk.Frame(dialog, padding=(16, 12))
        head.pack(fill='x')
        ttk.Label(head, text='成员接入', style='Title.TLabel').pack(anchor='w')
        project = ttk.Label(head, text=f'项目：{cfg.workspace}\n成员：{member} · 结果收件人：{cfg.director}',
                             style='Hint.TLabel', wraplength=850, justify='left')
        project.pack(anchor='w', pady=(6, 0))
        ttk.Label(head, text=f"样例任务已建立：{value['task_id']} · 送达与能力均待确认",
                  style='Hint.TLabel').pack(anchor='w', pady=(4, 0))
        wizard = value.get('wizard')
        if isinstance(wizard, dict):
            ttk.Label(head, text=wizard['continuity_boundary'], style='Hint.TLabel',
                      wraplength=850, justify='left').pack(anchor='w', pady=(4, 0))
        footer = ttk.Frame(dialog, padding=(16, 8, 16, 12))
        footer.pack(side='bottom', fill='x')
        ttk.Button(footer, text='关闭', command=dialog.destroy).pack(side='right')
        ttk.Label(footer, text='可由获授权的负责人使用电脑操作送达；无可用工具时只需粘贴一次。复制不等于发送。',
                  style='Hint.TLabel', wraplength=700).pack(side='left')
        panes = ttk.Panedwindow(dialog, orient='horizontal')
        panes.pack(fill='both', expand=True, padx=16, pady=(0, 8))
        for role, title in (('leader', '给负责人'), ('newcomer', '给新成员')):
            frame = ttk.Frame(panes)
            panes.add(frame, weight=1)
            bar = ttk.Frame(frame)
            bar.pack(fill='x', pady=(0, 8))
            ttk.Label(bar, text=title, style='Section.TLabel').pack(side='left')
            text = value['instructions'][role]
            ttk.Button(bar, text='复制指令', command=lambda r=role, t=text:
                self._copy_member_invitation(cfg, caller, value['invite_id'], r, t)).pack(side='right')
            box = scrolledtext.ScrolledText(frame, wrap='word', font=FONT, bg=COLORS['surface'])
            box.pack(fill='both', expand=True)
            box.insert('1.0', text)
            box.configure(state='disabled')

    def _show_overview_evidence(self, _event=None) -> None:
        selected = set(self.overview_tree.selection())
        member = next((row for row in getattr(self, '_overview_shell', {}).get('members', [])
                       if row['member_id'] in selected), None)
        if member is not None:
            light = member['status_light']
            self.overview_evidence_var.set(light['reason'] + ' · 依据字段：'
                + '、'.join(light['evidence_used']) + '。窗口、复制与文件时间不证明执行。')
            return
        item = next((item for item in getattr(self, '_overview_view', {}).get('members', [])
                     if item['id'] in selected), None)
        if item is not None:
            self.overview_evidence_var.set('记录依据：' + '；'.join(item.get('evidence') or []) +
                                           '。观察时间：' + str(item.get('observed_at') or '未观察'))

    def _navigate_next_action(self) -> None:
        action = getattr(self, '_overview_view', {}).get('action_id', 'team')
        self._show_page(action)
        if action == 'home':
            self.goal_text.focus_set()

    def _page_heading(self, parent, title: str, subtitle: str, *, spacing: int = 12) -> None:
        header = ttk.Frame(parent)
        header.pack(fill='x', pady=(0, self._px(spacing)))
        ttk.Label(header, text=title, style='Title.TLabel').pack(anchor='w')
        hint = ttk.Label(header, text=subtitle, style='Hint.TLabel', wraplength=760)
        hint.pack(anchor='w', pady=(self._px(4), 0))
        self._register_wrap(hint)

    def _build_home(self, page) -> None:
        # Avoid a second oversized title/subtitle stack on the information-rich
        # home. The shell already names the project and collaboration route.
        ttk.Label(page, text='项目工作台', style='Section.TLabel').pack(anchor='w', pady=(0, 8))
        self.overview_summary_var = tk.StringVar(value='团队未配置')
        self.overview_scope_var = tk.StringVar(value='文件协作')
        self.overview_evidence_var = tk.StringVar(value='尚未观察成员记录。')
        self.overview_goal_var = tk.StringVar(value='当前目标：目标未确认')
        self.overview_goal_source_var = tk.StringVar(value='来源：尚无已确认作业')
        self.overview_goal_origin_var = tk.StringVar(value='尚无已确认作业')
        self.overview_cards_note_var = tk.StringVar(value='任务交付需独立验收。')
        self.overview_card_vars = {}
        summary = ttk.Frame(page, padding=(14, 6), style='Surface.TFrame')
        summary.pack(fill='x', pady=(0, 12))
        self.overview_next_btn = ttk.Button(summary, text='配置并保存团队',
            command=self._navigate_next_action, style='Accent.TButton')
        self.overview_next_btn.pack(side='right', padx=(12, 0))
        ttk.Label(summary, textvariable=self.overview_summary_var,
                  style='Surface.TLabel', font=(FONT[0], TYPE_SCALE['subtitle'], 'bold')).pack(anchor='w')
        goal_label = ttk.Label(summary, textvariable=self.overview_goal_var, style='Surface.TLabel',
                               wraplength=760, justify='left')
        goal_label.pack(anchor='w', pady=(6, 0))
        self._register_wrap(goal_label)
        source_label = ttk.Label(summary, textvariable=self.overview_goal_origin_var,
                                 font=FONT_SMALL, foreground=COLORS['secondary'], style='Surface.TLabel',
                                 wraplength=760, justify='left')
        source_label.pack(anchor='w', pady=(3, 0))
        self._register_wrap(source_label)
        cards = ttk.Frame(page)
        cards.pack(fill='x', pady=(0, 6))
        for column, (key, title) in enumerate((('tasks', '任务'), ('deliverables', '已验证任务交付'),
                                              ('ready', '待接手'), ('blocked', '阻塞'))):
            card = ttk.Frame(cards, style='Surface.TFrame', padding=(10, 6))
            card.grid(row=0, column=column, sticky='nsew', padx=(0, 6 if column < 3 else 0))
            cards.columnconfigure(column, weight=1)
            variable = tk.StringVar(value='—')
            self.overview_card_vars[key] = variable
            ttk.Label(card, text=title, style='Surface.TLabel', font=FONT_SMALL,
                      foreground=COLORS['secondary']).pack(side='left')
            ttk.Label(card, textvariable=variable, style='Surface.TLabel',
                      font=(FONT[0], TYPE_SCALE['small'], 'bold')).pack(side='right', padx=(6, 0))
        self._build_goal(page)
        self._build_task_forest(page)
        # Retain the member projection for existing callbacks and diagnostics;
        # the visible overview is the project forest, with a member detail button.
        table = ttk.Frame(page)
        self.overview_tree = ttk.Treeview(table, columns=('member', 'task', 'state', 'checkpoint', 'next'),
                                          show='headings', height=2, selectmode='browse')
        for key, label, width in (('member', '成员', 110), ('task', '当前任务', 180),
                                  ('state', '当前记录', 155), ('checkpoint', '续接记录', 180),
                                  ('next', '下一步', 145)):
            self.overview_tree.heading(key, text=label, anchor='w')
            self.overview_tree.column(key, width=self._px(width), minwidth=self._px(90),
                                      stretch=True, anchor='w')
        self.overview_tree.tag_configure('warn', foreground=COLORS['warn'])
        self.overview_tree.tag_configure('info', foreground=COLORS['secondary'])
        self.overview_tree.bind('<<TreeviewSelect>>', self._show_overview_evidence)

    def _build_task_forest(self, page) -> None:
        overview = ttk.Frame(page)
        overview.pack(fill='both', expand=True)
        heading = ttk.Frame(overview)
        heading.pack(fill='x', pady=(0, 6))
        ttk.Label(heading, text='任务树看板', style='Section.TLabel').pack(side='left')
        self.overview_members_btn = ttk.Button(heading, text='成员详情', command=self._show_member_details)
        self.overview_members_btn.configure(padding=(8, 3))
        self.overview_members_btn.pack(side='right')
        ttk.Button(heading, text='刷新', command=self._refresh_task_forest,
                   padding=(8, 3)).pack(side='right', padx=(0, 8))
        self.task_forest_note_var = tk.StringVar(value='尚无任务树。保存团队后发布任务。')
        self.task_forest_status_var = tk.StringVar(value='选择叶子查看工作留痕')
        # Reserve the board's full width for the controls before allocating the
        # height left over to the tree and its scrollable detail pane.
        actions = ttk.Frame(overview)
        actions.pack(fill='x', pady=(0, 4))
        panes = ttk.Frame(overview)
        panes.pack(fill='both', expand=True)
        panes.rowconfigure(0, weight=1)
        panes.columnconfigure(0, weight=2, uniform='forest')
        panes.columnconfigure(1, weight=3, uniform='forest')
        left, right = ttk.Frame(panes), ttk.Frame(panes, padding=(12, 0, 0, 0))
        left.grid(row=0, column=0, sticky='nsew')
        right.grid(row=0, column=1, sticky='nsew')
        self.task_tree = ttk.Treeview(left, columns=('state', 'owner'), show='tree headings',
                                     height=3, selectmode='browse')
        for key, title, width in (('#0', '主干 / 子树 / 叶子', 195),
                                  ('state', '进度', 94), ('owner', '当前接手者', 86)):
            self.task_tree.heading(key, text=title, anchor='w')
            self.task_tree.column(key, width=self._px(width), minwidth=self._px(58),
                                  anchor='w', stretch=True)
        self.overview_scroll = ttk.Scrollbar(left, orient='vertical', command=self.task_tree.yview)
        self.task_tree.configure(yscrollcommand=self.overview_scroll.set)
        self.overview_scroll.pack(side='right', fill='y')
        self.task_tree.pack(side='left', fill='both', expand=True)
        for state, color in (('delivered', 'ok'), ('blocked', 'err'), ('stopped', 'warn'),
                             ('unconfirmed', 'warn'), ('running', 'accent')):
            self.task_tree.tag_configure(state, foreground=COLORS[color])
        self.task_tree.bind('<<TreeviewSelect>>', self._select_task_leaf)
        ttk.Label(right, textvariable=self.task_forest_status_var,
                  style='Section.TLabel').pack(anchor='w', pady=(0, 6))
        self.task_leaf_member_var = tk.StringVar()
        ttk.Label(actions, text='接手成员', style='Hint.TLabel').pack(side='left', padx=(0, 4))
        self.task_leaf_member_box = ttk.Combobox(actions, textvariable=self.task_leaf_member_var,
                                                state='readonly', width=12)
        self.task_leaf_member_box.pack(side='left', padx=(0, 6))
        self.task_leaf_member_box.bind('<<ComboboxSelected>>', self._select_task_leaf)
        self.task_leaf_claim_btn = ttk.Button(actions, text='认领叶子', padding=(8, 3),
            command=lambda: self._start_task_tree_action('claim'), state='disabled')
        self.task_leaf_claim_btn.pack(side='left', padx=(0, 6))
        self.task_leaf_copy_btn = ttk.Button(actions, text='复制接手说明', padding=(8, 3),
            command=lambda: self._start_task_tree_action('copy'), state='disabled')
        self.task_leaf_copy_btn.pack(side='left', padx=(0, 6))
        self.task_leaf_technical_btn = ttk.Button(actions, text='技术详情', padding=(8, 3),
            command=self._show_task_leaf_technical, state='disabled')
        self.task_leaf_technical_btn.pack(side='left')
        self._wrap_action_bar(actions)
        self.task_leaf_detail = scrolledtext.ScrolledText(right, wrap='word', font=FONT,
            height=5, width=25, relief='flat', background=COLORS['surface'],
            foreground=COLORS['text'], padx=12, pady=8, state='disabled')
        self.task_leaf_detail.pack(fill='both', expand=True)
        self.task_leaf_detail.tag_configure('section', font=(FONT[0], TYPE_SCALE['subtitle'], 'bold'),
                                           foreground=COLORS['accent'])
        note = ttk.Label(overview, textvariable=self.task_forest_note_var,
                         style='Hint.TLabel', wraplength=760, justify='left')
        note.pack(anchor='w', pady=(6, 0))
        self._register_wrap(note)
        self._task_forest_snapshot = {}
        self._task_forest_view = build_forest_view({})
        self._task_forest_nodes = {}
        self._render_task_forest({})

    def _refresh_task_forest(self) -> None:
        self._overview_snapshot = None
        self._refresh_workbench_overview()

    def _render_task_forest(self, snapshot, *, read_error=None) -> None:
        if not hasattr(self, 'task_tree'):
            return
        self._task_forest_snapshot = snapshot if isinstance(snapshot, dict) else {}
        self._task_forest_workspace = str(getattr(getattr(self, 'cfg', None), 'workspace', ''))
        view = build_forest_view(self._task_forest_snapshot, read_error=read_error)
        self._task_forest_view = view
        self._task_forest_nodes = {node['id']: node for node in view['nodes']}
        selected = self.task_tree.selection()
        expansion = {node: self.task_tree.item(node, 'open') for node in self._task_forest_nodes
                     if self.task_tree.exists(node)}
        for node in self.task_tree.get_children():
            self.task_tree.delete(node)
        remaining = list(view['nodes'])
        while remaining:
            inserted = []
            for node in remaining:
                parent = node['parent_id'] or ''
                if parent and not self.task_tree.exists(parent):
                    continue
                self.task_tree.insert(parent, 'end', iid=node['id'], text=node['title'],
                    values=(node['state_label'], node['owner'] or ('待接手' if node['kind'] == 'leaf' else '')),
                    open=expansion.get(node['id'], node['kind'] != 'leaf'),
                    tags=('unconfirmed' if read_error else node['state'],))
                inserted.append(node)
            if not inserted:
                # Malformed cycles remain visible but never gain actions.
                for node in remaining:
                    node['claimable'] = False
                    node['parent_id'] = None
                continue
            remaining = [node for node in remaining if node not in inserted]
        retained = [identity for identity in selected if self.task_tree.exists(identity)]
        if retained:
            self.task_tree.selection_set(retained)
        elif view['nodes']:
            preferred = next((node for node in view['nodes'] if node['kind'] == 'leaf'), view['nodes'][0])
            self.task_tree.selection_set(preferred['id'])
        self.task_forest_note_var.set(view['note'])
        cfg = getattr(self, 'cfg', None)
        agents = tuple(name for name, worker in cfg.enabled_workers().items()
                       if worker.get('executor_enabled', True)) if cfg is not None else ()
        self.task_leaf_member_box.configure(values=agents)
        if self.task_leaf_member_var.get() not in agents:
            self.task_leaf_member_var.set(agents[0] if agents else '')
        self._select_task_leaf()

    def _select_task_leaf(self, _event=None) -> None:
        selected = self.task_tree.selection()
        node = self._task_forest_nodes.get(selected[0]) if selected else None
        selected_id = node['id'] if node is not None else None
        eligibility = node['source'].get('eligible_agents') if node is not None else None
        agents = tuple(self.task_leaf_member_box.cget('values'))
        if selected_id != getattr(self, '_selected_task_leaf_id', None):
            current_owner = node['owner'] if node is not None else ''
            if current_owner in agents:
                self.task_leaf_member_var.set(current_owner)
            elif eligibility:
                candidate = next((agent for agent in eligibility if agent in agents), None)
                if candidate:
                    self.task_leaf_member_var.set(candidate)
        self._selected_task_leaf_id = selected_id
        self.task_leaf_detail.configure(state='normal')
        self.task_leaf_detail.delete('1.0', 'end')
        if node is None:
            self.task_forest_status_var.set('项目任务树')
            self.task_leaf_detail.insert('end', self._task_forest_view['note'])
        else:
            self.task_forest_status_var.set(node['state_label'])
            for title, text in node['sections'].items():
                self.task_leaf_detail.insert('end', title + '\n', 'section')
                self.task_leaf_detail.insert('end', text + '\n\n')
        self.task_leaf_detail.configure(state='disabled')
        task = getattr(self, '_task_tree_action_task', None)
        busy = task is not None and task.worker is not None and task.worker.is_alive()
        available = (node is not None and node['kind'] == 'leaf'
                     and not self._task_forest_view['read_error'] and not busy
                     and bool(self.task_leaf_member_var.get()))
        self.task_leaf_copy_btn.configure(state='normal' if available else 'disabled')
        eligible = eligibility is None or self.task_leaf_member_var.get() in eligibility
        self.task_leaf_claim_btn.configure(state='normal' if available and node['claimable']
                                           and eligible else 'disabled')
        self.task_leaf_technical_btn.configure(state='normal' if node is not None else 'disabled')

    def _show_task_leaf_technical(self) -> None:
        selected = self.task_tree.selection()
        node = self._task_forest_nodes.get(selected[0]) if selected else None
        if node is not None:
            self._show_readonly_text('任务叶子技术详情', json.dumps(node['source'], ensure_ascii=False, indent=2))

    def _start_task_tree_action(self, operation) -> None:
        cfg = self._workflow_cfg()
        selected = self.task_tree.selection()
        node = self._task_forest_nodes.get(selected[0]) if selected else None
        if cfg is None or node is None or node['kind'] != 'leaf' or self._task_forest_view['read_error']:
            return
        if self._task_tree_action_task is None:
            self._task_tree_action_task = OverviewSnapshotTask(checker=_task_tree_action, timeout_s=15)
        request = {'operation': operation, 'cfg': Config(path=cfg.path, workspace=cfg.workspace,
                    data=copy.deepcopy(cfg.data)), 'leaf_id': node['id'],
                   'agent': self.task_leaf_member_var.get()}
        if self._task_tree_action_task.start(**request):
            self._task_tree_action_context = request
            self.task_leaf_claim_btn.configure(state='disabled')
            self.task_leaf_copy_btn.configure(state='disabled')
            self.task_forest_note_var.set('正在核对叶子当前状态…')
            self._poll_task_tree_action()

    def _poll_task_tree_action(self) -> None:
        task = self._task_tree_action_task
        if task is None:
            return
        if getattr(self, '_closed', False) or getattr(self, '_close_requested', False):
            task.close()
            return
        event = task.poll()
        if event is None:
            if task.pending:
                self.root.after(80, self._poll_task_tree_action)
            return
        context = self._task_tree_action_context
        current = getattr(self, 'cfg', None)
        if current is None or current.workspace != context['cfg'].workspace:
            return
        try:
            same_displayed_project = Path(self.ws_var.get().strip()).expanduser().resolve() == current.workspace
        except (OSError, ValueError):
            same_displayed_project = False
        if not same_displayed_project:
            self.task_forest_note_var.set('操作属于此前的项目；切换项目后请重新读取当前任务树。')
            return
        if event['kind'] != 'result':
            self.task_forest_note_var.set('操作结果未确认：' + event['message'] + '；刷新后按实际状态继续。')
            self._select_task_leaf()
            self._overview_snapshot = None
            return
        result = event['value']
        if result.get('workspace') != str(current.workspace):
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(result['text'])
        if context['operation'] == 'claim':
            self.task_forest_note_var.set('叶子已认领，接手说明已复制。将说明发送到所选成员的项目对话。')
            self._refresh_task_forest()
        else:
            self.task_forest_note_var.set('接手说明已复制。发送到所选成员的项目对话后，由实际回执确认进度。')
        self._select_task_leaf()

    def _build_config(self, parent=None) -> None:
        card = ttk.Frame(parent or self.root, padding=(12, 4))
        card.pack(fill="both", expand=True)

        row = ttk.Frame(card)
        row.pack(fill="x", pady=(0, 4))
        ttk.Label(row, text="项目目录", width=10).pack(side="left")
        self.ws_var = tk.StringVar(value=initial_workspace(Path.cwd()))
        self.ws_var.trace_add('write', self._on_team_draft_changed)
        self.ws_var.trace_add('write', self._schedule_overview)
        ttk.Entry(row, textvariable=self.ws_var, font=FONT).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", command=self._pick_ws).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="打开目录", command=self._open_ws).pack(side="left", padx=(6, 0))

        output_row = ttk.Frame(card)
        output_row.pack(fill="x", pady=(0, 4))
        ttk.Label(output_row, text="业务产出目录", width=10).pack(side="left")
        self.output_var = tk.StringVar()
        self.output_var.trace_add('write', self._on_team_draft_changed)
        self.output_var.trace_add('write', self._schedule_overview)
        ttk.Entry(output_row, textvariable=self.output_var, font=FONT).pack(
            side="left", fill="x", expand=True)
        ttk.Button(output_row, text="浏览…", command=self._pick_output).pack(side="left", padx=(8, 0))
        ttk.Button(output_row, text="打开目录", command=self._open_output).pack(side="left", padx=(6, 0))
        output_hint = ttk.Label(card,
                                text="控制目录保存计划、成员信箱和记忆；业务产出可放在另一块磁盘。",
                                style="Hint.TLabel", wraplength=760)
        output_hint.pack(anchor='w', pady=(0, 4))
        self._register_wrap(output_hint)

        row2 = ttk.Frame(card)
        row2.pack(fill="x", pady=(0, 4))
        ttk.Label(row2, text="组长", width=10).pack(side="left")
        self.director_var = tk.StringVar()
        self.director_var.trace_add('write', self._on_team_draft_changed)
        self.director_var.trace_add('write', self._schedule_overview)
        self.director_box = ttk.Combobox(row2, textvariable=self.director_var,
                                         state="readonly", width=20, font=FONT)
        self.director_box.pack(side="left")
        hint = ttk.Label(card, text="负责拆分任务、派发和汇总；不增加权限，接受同伴评审",
                         style="Hint.TLabel", wraplength=760)
        hint.pack(anchor='w', pady=(0, 4))
        self._register_wrap(hint)

        row3 = ttk.Frame(card)
        ttk.Label(row3, text="团队成员", width=10).pack(side="left", anchor="n")
        # Source: dynamic-member GUI audit, Codex, 2026-10-02. A bounded
        # viewport keeps goal and workflow actions reachable for large teams.
        workers_view = ttk.Frame(row3)
        workers_view.pack(side="left", fill="both", expand=True)
        # Keep a usable member viewport when the dual-root guidance and
        # high-DPI font metrics consume the ordinary-window height.
        self.workers_canvas = tk.Canvas(workers_view, height=self._px(120),
                                        highlightthickness=0,
                                        background=self.root.cget('background'))
        workers_y = ttk.Scrollbar(workers_view, orient='vertical',
                                   command=self.workers_canvas.yview)
        workers_x = ttk.Scrollbar(workers_view, orient='horizontal',
                                   command=self.workers_canvas.xview)

        def update_horizontal_scroll(first, last):
            workers_x.set(first, last)
            overflow = float(first) > 0 or float(last) < 1
            if overflow == bool(workers_x.winfo_manager()):
                return
            if overflow:
                workers_x.grid(row=1, column=0, sticky='ew')
            else:
                workers_x.grid_remove()

        self.workers_canvas.configure(yscrollcommand=workers_y.set,
                                      xscrollcommand=update_horizontal_scroll)
        self.workers_canvas.grid(row=0, column=0, sticky='nsew')
        workers_y.grid(row=0, column=1, sticky='ns')
        workers_view.columnconfigure(0, weight=1)
        workers_view.rowconfigure(0, weight=1)
        self.workers_frame = ttk.Frame(self.workers_canvas)
        self.workers_window = self.workers_canvas.create_window(
            (0, 0), window=self.workers_frame, anchor='nw')
        self.workers_frame.bind('<Configure>', self._layout_workers)
        self.workers_frame.bind('<MouseWheel>', self._scroll_workers)
        self.workers_canvas.bind('<Configure>', self._layout_workers)
        self.workers_canvas.bind('<MouseWheel>', self._scroll_workers)
        self.workers_hint = ttk.Label(self.workers_frame, text="（载入工作空间后选择组员）",
                                      style="Hint.TLabel")
        self.workers_hint.pack(anchor="w")

        row4 = ttk.Frame(card)
        ttk.Button(row4, text="保存团队", style='Accent.TButton',
                   command=self._save_group).pack(side="left")
        ttk.Button(row4, text="添加 Agent", command=self._add_member).pack(side='left', padx=(8,0))
        # Reserve save/help before giving remaining height to member scrolling.
        # The actual ordinary EXE exposed clipping that withdrawn fixtures missed.
        row4.pack(side='bottom', fill='x', pady=(4, 0))
        row3.pack(fill='both', expand=True)

    def _build_goal(self, parent=None) -> None:
        card = ttk.LabelFrame(parent or self.root, text=" 交给团队的目标 ",
                              style="Card.TLabelframe", padding=(14, 8, 14, 12))
        card.pack(side='bottom', fill="x", pady=(12, 0))

        row = ttk.Frame(card)
        row.pack(fill="x")
        self.goal_text = tk.Text(row, height=2, font=FONT, wrap="word",
                                 relief="solid", borderwidth=1,
                                 background=COLORS['input_bg'], foreground=COLORS['text'])
        self.goal_text.pack(side="left", fill="x", expand=True)

        row2 = ttk.Frame(card)
        row2.pack(fill="x", pady=(8, 0))
        self.goal_agent = tk.StringVar(value='codex')
        ttk.Label(row2, text='发起成员').pack(side='left',padx=(0,8))
        self.goal_agent_box = ttk.Combobox(row2,textvariable=self.goal_agent,
                                        state='readonly',width=14)
        self.goal_agent_box.pack(side='left',padx=(0,8))
        self.start_btn = ttk.Button(row2, text="复制任务指令", style="Accent.TButton",
                                    command=self._start)
        self.start_btn.pack(side="left")
        # The conversation owns execution. Do not show a stop button whose
        # event only affects the unused independent GUI runner.
        self.stop_btn = None

        hint = ttk.Label(card, text="复制不派发；在所选成员的当前项目对话中发送。团队继续通过文件交接。",
                         style="Hint.TLabel", wraplength=760)
        hint.pack(anchor='w', pady=(8, 0))
        self._register_wrap(hint)
        self.approve_var = tk.BooleanVar(value=False)

    def _build_tabs(self) -> None:
        if not hasattr(self, 'content_frame'):
            self.content_frame = ttk.Frame(self.root)
            self.content_frame.pack(fill='both', expand=True)
        if not hasattr(self, '_subnavigation'):
            self._subnavigation = {}
        if not hasattr(self, 'nav_buttons'):
            self.nav_buttons = {}
        nb = ttk.Notebook(self.content_frame, style='Workbench.TNotebook')
        nb.pack(fill='both', expand=True)
        self.tabs = nb
        self.pages = {}
        for item in NAV_ITEMS:
            page = ttk.Frame(nb, padding=self._px(20))
            nb.add(page, text=item['title'])
            self.pages[item['id']] = page
        self.current_page = 'home'
        nb.bind('<<NotebookTabChanged>>', self._sync_navigation)
        self.content_frame.bind('<Configure>', self._refresh_wraplength)
        self.pages['home'].configure(padding=(self._px(16), self._px(8)))
        self._build_home(self.pages['home'])

        # Source: user/group enterprise review, Codex, 2026-10-04.
        # A visible onboarding surface comes before diagnostics and setup.
        self.connection_page = self.pages['access']
        self._page_heading(self.connection_page, '接入与技能', '逐一接入成员；技能检查与续接共用当前项目配置。')
        access_nb = self._build_subnavigation(self.connection_page,
            [('connection', '团队接入'), ('workflow', '对话续接'), ('skills', '技能检查')])
        connection = ttk.Frame(access_nb, padding=6)
        access_nb.add(connection, text='团队接入')
        connection_bar = ttk.Frame(connection)
        connection_bar.pack(side='bottom', fill='x', pady=(8, 0))
        self.connection_copy_btn = ttk.Button(connection_bar, text='复制接入指令', style='Accent.TButton',
                                              command=self._copy_connection)
        self.connection_copy_btn.pack(side='left')
        self.invitation_prepare_btn = ttk.Button(connection_bar, text='生成成员接入指令',
                                                 command=self._prepare_member_invitation)
        self.invitation_prepare_btn.pack(side='left', padx=8)
        ttk.Button(connection_bar, text='刷新接入记录',
                   command=self._refresh_connections).pack(side='left', padx=8)
        ttk.Button(connection_bar, text='查看最近任务回执',
                   command=self._show_recent_receipt).pack(side='left')
        ttk.Button(connection_bar, text='检查项目工作流',
                   command=self._prepare_workflow).pack(side='left')
        selection = ttk.Frame(connection)
        selection.pack(fill='x', pady=(0, 8))
        ttk.Label(selection, text='接入成员').pack(side='left', padx=(0, 8))
        self.connection_agent = tk.StringVar(value='codex')
        self.connection_agent_box = ttk.Combobox(selection, textvariable=self.connection_agent,
                                                state='readonly', width=20)
        self.connection_agent_box.pack(side='left')
        self.connection_agent_box.bind('<<ComboboxSelected>>',
                                       lambda _event: self._refresh_connections())
        self.invitation_writable = tk.BooleanVar(value=True)
        ttk.Checkbutton(selection, text='包含写入样例', variable=self.invitation_writable).pack(
            side='left', padx=(12, 0))
        self.connection_indicator = ttk.Label(selection, text='● 待接入', foreground='#6b7280')
        self.connection_indicator.pack(side='left', padx=16)
        ttk.Label(connection, text='按成员逐一接入：打开本项目 → 新建对话并发送指令 → 核对总线回执。',
                  style='Hint.TLabel', wraplength=960).pack(anchor='w', pady=(0, 8))
        self.connection_box = scrolledtext.ScrolledText(connection, wrap='word', font=FONT,
                                                       state='disabled', bg=COLORS['surface'])
        self.connection_box.pack(fill='both', expand=True)
        self._refresh_connections()

        progress = self.pages['tasks']
        self._page_heading(progress, '任务与计划', '保留目标、分工、交接与审查意见；未确认不会标为完成。')
        tasks_nb = self._build_subnavigation(progress,
            [('jobs', '作业与续接'), ('plan', '总体计划'),
             ('handoff', '待接手任务'), ('decisions', '待决事项')])

        # --- 运行记录 ---
        f1 = self.pages['logs']
        self._page_heading(f1, '运行记录', '查看原始过程与问题说明，保留失败和同伴意见。')
        self.log_box = scrolledtext.ScrolledText(
            f1, wrap="word", font=FONT_MONO, state="disabled",
            bg=COLORS['surface'], fg=COLORS['text'], insertbackground=COLORS['text'])
        self.log_box.pack(fill="both", expand=True)
        for tag, color in (("info", COLORS['text']), ("ok", "#23764a"),
                           ("warn", "#986a15"), ("err", "#b42318"),
                           ("dim", COLORS['secondary']), ("head", COLORS['accent'])):
            self.log_box.tag_config(tag, foreground=color)

        management = self.pages['settings']
        self.management_page = management
        self._page_heading(management, '设置', '独立接口按需扩展；工作流维护、诊断与应用分发。')
        manage_nb = self._build_subnavigation(management,
            [('interfaces', '接口配置'), ('maintenance', '维护'), ('distribution', '应用分发')])
        members = self.pages['team']
        # The team page carries two directory rows plus onboarding actions;
        # keep its outer inset compact so the member picker retains a usable
        # viewport at high DPI.
        members.configure(padding=(self._px(12), self._px(4)))
        self._page_heading(members, '项目与团队',
                           '保存后按团队接入步骤操作；成员选择不表示当前会话已连接。', spacing=4)
        members_footer = ttk.Frame(members)
        members_footer.pack(side='bottom', fill='x')
        members_bar = ttk.Frame(members_footer)
        members_bar.pack(fill='x')
        ttk.Button(members_bar, text='检测本机入口', command=self._detect).pack(side='left')
        ttk.Button(members_bar, text='添加 Agent', command=self._add_member).pack(side='left', padx=8)
        ttk.Button(members_bar, text='导入成员模板', command=self._import_team).pack(side='left')
        self.detect_label = ttk.Label(members_footer, text='尚未检测；入口检测不代表认证或任务执行通过。', style='Hint.TLabel',
                                      wraplength=960, justify='left')
        self.detect_label.pack(anchor='w')
        self._build_config(members)

        f6 = ttk.Frame(tasks_nb, padding=6)
        tasks_nb.add(f6, text='作业与续接')
        bar6 = ttk.Frame(f6)
        bar6.pack(fill='x')
        ttk.Button(bar6, text='刷新任务进度', command=lambda: self._refresh_jobs(technical=False)).pack(side='left')
        ttk.Button(bar6, text='技术详情', command=lambda: self._refresh_jobs(technical=True)).pack(side='left', padx=8)
        ttk.Button(bar6, text='打开 OpenCode 记录', command=self._open_opencode_records).pack(side='left')
        ttk.Label(f6, text='换对话可继续已有作业；等待接手、暂停和失败不会标为完成。',
                  style='Hint.TLabel').pack(anchor='w', pady=(6, 0))
        self.jobs_box = scrolledtext.ScrolledText(f6, wrap='word', font=FONT, state='disabled',
                                                 bg=COLORS['surface'])
        self.jobs_box.pack(fill='both', expand=True, pady=(8, 0))

        plan_page = ttk.Frame(tasks_nb, padding=6)
        tasks_nb.add(plan_page, text='总体计划')
        plan_bar = ttk.Frame(plan_page); plan_bar.pack(fill='x')
        ttk.Button(plan_bar, text='刷新总体计划', command=self._refresh_project_plan).pack(side='left')
        ttk.Label(plan_page, text='先评估再执行；目标、意见与取舍长期保留。更新计划不改写在途任务。',
                  style='Hint.TLabel', wraplength=900).pack(anchor='w', pady=(8, 0))
        self.project_plan_box = scrolledtext.ScrolledText(plan_page, wrap='word', font=FONT,
                                                          state='disabled', bg=COLORS['surface'])
        self.project_plan_box.pack(fill='both', expand=True, pady=(8, 0))

        # --- 人工接手 ---
        f2 = ttk.Frame(tasks_nb, padding=6)
        tasks_nb.add(f2, text='待接手任务')
        self.handoff_box = scrolledtext.ScrolledText(
            f2, wrap="word", font=FONT_MONO, state="disabled", bg="#fbfbfb")
        bar2 = ttk.Frame(f2)
        bar2.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(bar2, text="刷新", command=self._refresh_handoff).pack(side="left")
        ttk.Button(bar2, text="复制首条任务指令",
                   command=self._copy_handoff).pack(side="left", padx=8)
        ttk.Label(bar2, text="在该任务指定成员的当前项目对话中发送；发送后核对关联回执。",
                  style="Hint.TLabel").pack(side="left", padx=8)
        self.handoff_box.pack(fill="both", expand=True)

        # --- 待决事项 ---
        f3 = ttk.Frame(tasks_nb, padding=6)
        tasks_nb.add(f3, text='待决事项')
        self.esc_box = scrolledtext.ScrolledText(
            f3, wrap="word", font=FONT_MONO, state="disabled", bg="#fffdf5")
        bar3 = ttk.Frame(f3)
        bar3.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(bar3, text="刷新", command=self._refresh_esc).pack(side="left")
        ttk.Label(bar3, text="权限、路由或重试异常在此保留，等待用户处理。",
                  style="Hint.TLabel").pack(side="left", padx=8)
        self.esc_box.pack(fill="both", expand=True)

        # --- MCP 接入 ---
        f4 = ttk.Frame(manage_nb, padding=6)
        self.mcp_box = scrolledtext.ScrolledText(f4, wrap="none", font=FONT_MONO,
                                                 height=14, bg="#fbfbfb")
        bar4 = ttk.Frame(f4)
        bar4.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(bar4, text="生成配置", command=self._gen_mcp).pack(side="left")
        ttk.Button(bar4, text="复制", command=self._copy_mcp).pack(side="left", padx=8)
        ttk.Button(bar4, text="自检 MCP 是否可用",
                   command=self._check_mcp).pack(side="left")
        # Reserve actions before the explanatory label too: frozen Windows
        # DPI scaling can leave less room than the source Tk layout.
        ttk.Label(
            f4, font=FONT_SMALL, justify="left", wraplength=960,
            text=("外部 MCP 接入只提供信箱工具，不接管或唤醒旧桌面会话。\n"
                  "SDK / ACP 后台入口须另行验收；日常使用对话工作流。")
        ).pack(anchor="w", pady=(0, 6))
        self.mcp_box.pack(fill="both", expand=True)

        # Fresh conversations get an explicit, project-scoped entrypoint.
        f5 = ttk.Frame(access_nb, padding=6)
        access_nb.add(f5, text='对话续接')
        actions = ttk.Frame(f5)
        actions.pack(side='bottom', fill='x', pady=(6, 0))
        self.workflow_agent = tk.StringVar(value='codex')
        self.workflow_agent_box = ttk.Combobox(actions, textvariable=self.workflow_agent,
                     state='readonly', values=[], width=13)
        self.workflow_agent_box.pack(side='left')
        ttk.Button(actions, text='检查项目工作流',
                   command=self._prepare_workflow).pack(side='left', padx=8)
        ttk.Button(actions, text='复制续接指令',
                   command=self._copy_resume).pack(side='left', padx=8)
        ttk.Label(f5, text='仅恢复任务，不登记文件接入确认；换聊天接入请用团队接入的复制接入指令。',
                  style='Hint.TLabel', wraplength=960).pack(side='bottom', anchor='w', pady=(6, 0))
        self.workflow_box = scrolledtext.ScrolledText(
            f5, wrap='word', font=FONT_MONO, state='disabled', bg='#fbfbfb')
        self.workflow_box.pack(fill='both', expand=True)
        self._refresh_workflow()
        manage_nb.add(f4, text='接口配置')

        # Source: frozen ordinary-window clipping at display scaling,
        # Codex native observation and internal integration, 2026-10-05.
        # The top configuration cards leave a small notebook viewport after
        # restoring a normal window. Keep the maintenance controls in their
        # own scrolling page instead of relying on the initial zoom request.
        maintenance_page = ttk.Frame(manage_nb, padding=6)
        self.maintenance_page = maintenance_page
        manage_nb.add(maintenance_page, text='维护')
        maintenance_page.bind('<Map>', self._queue_maintenance_fit)
        maintenance_page.bind('<Configure>', self._queue_maintenance_fit)
        self.maintenance_canvas = tk.Canvas(
            maintenance_page, height=180, highlightthickness=0,
            background=COLORS['background'], yscrollincrement=24)
        maintenance_scroll = ttk.Scrollbar(
            maintenance_page, orient='vertical', command=self.maintenance_canvas.yview)
        self.maintenance_canvas.configure(yscrollcommand=maintenance_scroll.set)
        self.maintenance_canvas.grid(row=0, column=0, sticky='nsew')
        maintenance_scroll.grid(row=0, column=1, sticky='ns')
        maintenance_page.rowconfigure(0, weight=1)
        maintenance_page.columnconfigure(0, weight=1)
        f7 = ttk.Frame(self.maintenance_canvas, padding=6)
        self.maintenance_window = self.maintenance_canvas.create_window(
            (0, 0), window=f7, anchor='nw')
        f7.bind('<Configure>', self._layout_maintenance)
        self.maintenance_canvas.bind('<Configure>', self._layout_maintenance)
        self.maintenance_canvas.bind('<MouseWheel>', self._scroll_maintenance)
        bar7 = ttk.Frame(f7); bar7.pack(fill='x')
        ttk.Button(bar7, text='检查安装', command=self._inspect_workflows).pack(side='left')
        ttk.Button(bar7, text='检查当前团队', command=self._inspect_project).pack(side='left', padx=8)
        ttk.Button(bar7, text='保存诊断', command=self._save_diagnostic).pack(side='left', padx=8)
        ttk.Button(bar7, text='技能检查', command=self._show_skill_check).pack(side='left')
        install_bar = ttk.Frame(f7)
        install_bar.pack(fill='x', pady=(6, 0))
        ttk.Button(install_bar, text='安装 / 修复工作流', command=self._install_codex_skill).pack(side='left')
        ttk.Button(install_bar, text='预览移除', command=self._preview_uninstall_workflows).pack(side='left', padx=8)
        ttk.Button(install_bar, text='移除已安装工作流', command=self._uninstall_workflows).pack(side='left')
        recovery_bar = ttk.Frame(f7)
        recovery_bar.pack(fill='x', pady=(6, 0))
        self.recovery_open_btn = ttk.Button(recovery_bar, text='恢复中断任务',
                                            command=self._open_recovery_center)
        self.recovery_open_btn.pack(side='left')
        ttk.Label(recovery_bar, text='须确认原执行者已停止；不自动判断存活或延长预算。',
                  style='Hint.TLabel').pack(side='left', padx=8)
        ttk.Label(f7, text='程序免安装；此处管理各软件的工作流文件。移除保留项目、任务和用户修改。',
                  style='Hint.TLabel').pack(anchor='w', pady=(6,0))
        self.maintenance_box = scrolledtext.ScrolledText(
            f7, wrap='word', font=FONT, state='disabled')
        self.maintenance_box.pack(fill='both', expand=True, pady=(8,0))
        self._bind_maintenance_scroll(f7)

        distribution = ttk.Frame(manage_nb, padding=12)
        manage_nb.add(distribution, text='应用分发')
        distribution_bar = ttk.Frame(distribution)
        distribution_bar.pack(fill='x')
        ttk.Button(distribution_bar, text='导出分发包', command=self._export_team).pack(side='left')
        ttk.Button(distribution_bar, text='导入成员模板', command=self._import_team).pack(side='left', padx=8)
        ttk.Label(distribution, text=(
            '分发包包含应用程序、成员模板和使用说明，不包含项目内容、会话、凭据或本机权限。\n'
            '接收方需在自己的电脑安装并登录所选 Agent，保存团队后逐一完成接入和任务验证。\n'
            '模板导入默认采用人工接手，不启用模板提供的命令或扩大权限。'),
            justify='left', wraplength=960).pack(anchor='w', pady=(16, 0))
        # Source: actual frozen UI check, Codex, 2026-10-04. An unread view
        # must not look like a verified empty queue. Opening it does not scan,
        # claim tasks, construct a runner, or start a model.
        for box, label, action in ((self.project_plan_box, '总体计划', '刷新总体计划'),
                                   (self.jobs_box, '作业记录', '刷新任务进度'),
                                   (self.handoff_box, '待接手任务', '刷新'),
                                   (self.esc_box, '待决事项', '刷新')):
            box.configure(state='normal')
            box.insert('end', f'尚未读取当前项目的{label}。点击“{action}”读取；空白不代表没有记录。\n')
            box.configure(state='disabled')
        skills_page = ttk.Frame(access_nb, padding=12)
        access_nb.add(skills_page, text='技能检查')
        ttk.Label(skills_page, text='完整技能目录检查', style='Section.TLabel').pack(anchor='w')
        skill_hint = ttk.Label(skills_page,
            text='检查目录中的全部文件、原始字节摘要和目标冲突。当前仅检查与预览，不部署、不执行脚本。',
            style='Hint.TLabel', wraplength=760)
        skill_hint.pack(anchor='w', pady=(12, 16))
        self._register_wrap(skill_hint)
        ttk.Button(skills_page, text='技能检查', style='Accent.TButton',
                   command=self._show_skill_check).pack(anchor='w')
        self._subnavigation[tasks_nb]['pages'] = {'jobs': f6, 'plan': plan_page,
                                                'handoff': f2, 'decisions': f3}
        self._subnavigation[access_nb]['pages'] = {'connection': connection,
                                                  'workflow': f5, 'skills': skills_page}
        self._subnavigation[manage_nb]['pages'] = {'interfaces': f4,
                                                  'maintenance': maintenance_page,
                                                  'distribution': distribution}
        for notebook in (tasks_nb, access_nb, manage_nb):
            self._sync_subnavigation(notebook)
        for bar in (connection_bar, members_bar, bar6, plan_bar, bar2, bar3,
                    bar4, actions, bar7, install_bar, recovery_bar, distribution_bar):
            self._wrap_action_bar(bar)
        for container in (connection, members, f6, plan_page, f2, f3, f4, f5, f7, distribution):
            for child in container.winfo_children():
                if isinstance(child, ttk.Label) and child.cget('wraplength'):
                    self._register_wrap(child)
        self._show_page('home')

    def _layout_maintenance(self, _event=None) -> None:
        canvas = self.maintenance_canvas
        canvas.itemconfigure(self.maintenance_window, width=max(1, canvas.winfo_width()))
        canvas.configure(scrollregion=canvas.bbox('all'))

    def _queue_maintenance_fit(self, event=None) -> None:
        if event is not None and event.widget is not self.maintenance_page:
            return
        if not getattr(self, '_maintenance_fit_pending', False):
            self._maintenance_fit_pending = True
            self.root.after_idle(self._fit_maintenance_viewport)

    def _fit_maintenance_viewport(self) -> None:
        """Fit the page content locally; never enlarge the whole application."""
        self._maintenance_fit_pending = False
        if getattr(self, '_closed', False) or not self.maintenance_page.winfo_ismapped():
            return
        self._layout_maintenance()

    def _scroll_maintenance(self, event) -> str:
        delta = getattr(event, 'delta', 0)
        number = getattr(event, 'num', None)
        units = (-1 if number == 4 else 1 if number == 5 else
                 -max(1, abs(delta) // 120) if delta > 0 else
                 max(1, abs(delta) // 120) if delta < 0 else 0)
        if units and self.maintenance_canvas.yview() != (0.0, 1.0):
            self.maintenance_canvas.yview_scroll(units, 'units')
        return 'break'

    def _bind_maintenance_scroll(self, widget) -> None:
        # Wheel events over buttons/labels scroll the surrounding page. The
        # diagnostic Text and its scrollbar retain their own text scrolling.
        if not isinstance(widget, (tk.Text, ttk.Scrollbar, tk.Scrollbar)):
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
                widget.bind(sequence, self._scroll_maintenance, add='+')
        for child in widget.winfo_children():
            self._bind_maintenance_scroll(child)

    def _build_status(self) -> None:
        bar = ttk.Frame(self.root, padding=(16, 0, 16, 10))
        bar.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, style="Hint.TLabel").pack(side="left")
        self.watch_status_var = tk.StringVar(value="邮箱值守：未启动")
        ttk.Label(bar, textvariable=self.watch_status_var,
                  style="Hint.TLabel").pack(side="right")

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def log(self, text: str, tag: str = "info") -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text.rstrip() + "\n", tag)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                kind, data = self.q.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass  # 资源清理类：队列空为正常轮询间隙。
        if not self._closed:
            self.root.after(120, self._drain)

    def _handle_event(self, kind: str, d: dict) -> None:
        if kind == "plan_start":
            self.log(f"\n组长开始拆解目标：{d.get('goal','')}", "head")
            self.status_var.set("组长正在拆解任务…")
        elif kind == "plan_done":
            tasks = d.get("tasks", [])
            fb = d.get("fallback")
            if fb:
                self.log(f"  拆解降级（{fb}），按单一任务处理", "warn")
            self.log(f"  拆出 {len(tasks)} 个任务：", "ok")
            for i, t in enumerate(tasks, 1):
                self.log(f"    {i}. [{t['worker']}] {t['title']}", "dim")
        elif kind == "plan_rejected":
            self.log("  你取消了这次派发；任务没有写入成员信箱", "warn")
        elif kind == "need_approval":
            self.log("  计划待确认；点击“批准并开始”后才会投递任务。", "warn")
            self.status_var.set("等待确认派发计划")
            self._show_plan_dialog(d)
        elif kind == "permission_request":
            self._show_permission_dialog(d)
        elif kind == "session_ready":
            self.log(f"  会话已就绪：{d.get('agent')}（{d.get('session_id', '')}）", "ok")
        elif kind == "session_error":
            self.log(f"  会话启动失败：{d.get('agent')} —— {d.get('error', '')}", "err")
        elif kind == "session_preflight_failed":
            detail = "\n".join(f"{name}: {error}" for name, error in d.get("errors", {}).items())
            self.log("\n小组未启动：至少一个已勾选成员无法建立会话。\n" + detail, "err")
            self.status_var.set("有成员未就绪；修复或取消勾选后再启动")
            self._finish_run()
        elif kind == "dispatch":
            flag = "" if d.get("can_spawn") else "  ← 需人工接手"
            self.log(f"  → 派给 {d['agent']}: {d['title']}{flag}", "info")
        elif kind == "exec_start":
            self.log(f"\n⚙ {d['agent']} 开始执行：{d['title']}", "head")
            self.status_var.set(f"{d['agent']} 正在执行…")
        elif kind == "exec_done":
            rc = d.get("rc")
            tag = "ok" if rc == 0 else "err"
            self.log(f"  完成 rc={rc}  耗时 {d.get('duration_s',0):.1f}s", tag)
            if d.get("preview"):
                self.log("  产出摘要：" + d["preview"].replace("\n", " ")[:220], "dim")
        elif kind == "review":
            tag = "ok" if d.get("ok") else "warn"
            self.log(f"  验收：{'通过' if d.get('ok') else '未通过'}"
                     f"  {d.get('reason','')[:160]}", tag)
        elif kind == "peer_review_result":
            reviewer = d.get("reviewer", "未知成员")
            title = d.get("title", "未命名任务")
            result = d.get("result", "")
            self.log(f"\n同伴评审回执（{reviewer}）：{title}", "head")
            self.log(result or "（评审未提供正文）", "info")
        elif kind == "retry":
            self.log(f"  ↻ 重做第 {d.get('attempt')} 次（{d.get('reason','')[:100]}）", "warn")
        elif kind == "escalate":
            self.log(f"  ⚠ 升级给你决定：{d.get('title')} —— {d.get('reason','')[:140]}",
                     "err")
            self._refresh_esc()
        elif kind == "stale":
            self.log(f"  回收卡死任务 {d.get('msg_id')}（来自 {d.get('agent')}）", "warn")
        elif kind == "lease_expired":
            self.log(f"  清理过期租约：{d.get('path')}", "dim")
        elif kind == "idle":
            self.log("  当前没有新任务；小组继续等文件事件。", "dim")
            self.status_var.set("小组运行中，等待任务")
        elif kind == "run_start":
            self.log(f"执行循环启动，参与的执行者：{', '.join(d.get('agents', []))}", "ok")
            self.status_var.set("小组运行中，等待或处理任务")
        elif kind == "goal_complete":
            self.log(f"\n组长判断当前目标已完成：{d.get('summary', '')}", "ok")
            self.log("小组仍保持运行，可继续接收文件任务。", "dim")
        elif kind == "coordinator_followup_dispatched":
            self.log(f"组长依据新结果派出后续任务：{d.get('count', 0)} 项。", "info")
        elif kind == "coordinator_followup_error":
            self.log(f"组长无法安全生成后续任务：{d.get('error', '')}", "err")
        elif kind == "stop":
            s = d.get("summary")
            self.log(f"\n执行结束：{d.get('reason','')}", "head")
            if s is not None:
                self.log(s.render(), "info")
            self.status_var.set("已停止")
            self._finish_run()
        elif kind == "error":
            self.log(f"\n✖ {d.get('text','')}", "err")
            self._finish_run()
        elif kind == "pipeline_finished":
            if self._close_requested:
                self._finalize_close()
        elif kind == "coordinator_queued":
            self.log(f"\n已将目标排入组长 {d['agent']} 的待办信箱。", "warn")
            self.log(d["text"], "info")
            self.status_var.set("等待用户手动唤醒组长")
            self._finish_run()
        elif kind == "watchtower_update":
            total = d["total"]
            bad_count = len(d["bad"])
            self.watch_status_var.set(
                f"邮箱值守：运行中 · 待办 {total} 条"
                + (f" · 异常文件 {bad_count} 个" if bad_count else ""))
            signature = (str(getattr(getattr(self, 'cfg', None), 'workspace', '')),
                         total, tuple(str(item) for item in d['bad']),
                         tuple((agent, tuple(sorted(m.id for m in messages)))
                               for agent, messages in sorted(d.get('pending', {}).items())))
            if (total or bad_count) and signature != getattr(self, '_watch_log_signature', None):
                self.log(f"邮箱有更新：待办 {total} 条，异常文件 {bad_count} 个；"
                         "进度见「作业与续接」，GUI 任务见「人工接手」，升级事项见「待决事项」。", "warn")
            self._watch_log_signature = signature
            self._refresh_handoff()
            self._refresh_esc()
            self._refresh_connections()
            if hasattr(self, 'jobs_box'):
                self._refresh_jobs(silent=True)
                if hasattr(self, 'project_plan_box'):
                    self._refresh_project_plan(silent=True)
        elif kind == "watchtower_stale":
            for warning in d.get("warnings", []):
                self.log(warning, "warn")
        elif kind == "watchtower_error":
            self.watch_status_var.set("邮箱值守：出错")
            self.log(f"邮箱值守出错：{d.get('text', '')}", "err")

    # ------------------------------------------------------------------ #
    # 工作空间 / 探测
    # ------------------------------------------------------------------ #
    def _start_watchtower(self, cfg) -> None:
        current = self.watch_thread
        if (current is not None and current.is_alive() and self.watchtower is not None
                and self.watchtower.cfg.workspace.resolve() == cfg.workspace.resolve()
                and getattr(self, 'watch_cfg_data', None) == cfg.data):
            return
        self._stop_watchtower()
        stop = threading.Event()
        watchtower = Watchtower(cfg)
        thread = threading.Thread(
            target=self._watch_loop, args=(watchtower, stop),
            name="agentbus-inbox-watch", daemon=True)
        self.watchtower = watchtower
        self.watch_stop = stop
        self.watch_thread = thread
        self.watch_cfg_data = json.loads(json.dumps(cfg.data))
        self.watch_status_var.set("邮箱值守：启动中")
        thread.start()

    def _stop_watchtower(self) -> None:
        stop = getattr(self, "watch_stop", None)
        if stop is not None:
            stop.set()
        thread = getattr(self, "watch_thread", None)
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=2.0)
        if hasattr(self, "watchtower"):
            self.watchtower = None
        if hasattr(self, "watch_thread"):
            self.watch_thread = None
        if hasattr(self, "watch_stop"):
            self.watch_stop = None
        self.watch_cfg_data = None
        status = getattr(self, "watch_status_var", None)
        if status is not None:
            status.set("邮箱值守：已停止")

    def _watch_loop(self, watchtower: Watchtower,
                    stop: threading.Event) -> None:
        agents = watchtower.watched_agents()
        watchers = {}
        previous_signature = None
        previous_ids: set[str] = set()
        previous_bad: set[tuple[str, str, str]] = set()

        def refresh() -> None:
            nonlocal previous_signature, previous_ids, previous_bad
            pending, bad = watchtower.scan()
            signature = (
                tuple((agent, tuple(sorted(m.id for m in messages)))
                      for agent, messages in sorted(pending.items())),
                tuple(sorted(bad)),
                watchtower.observation_signature(),
            )
            if signature == previous_signature:
                return
            current_ids = {m.id for messages in pending.values() for m in messages}
            new_ids = current_ids - previous_ids
            new_bad = set(bad) - previous_bad
            self.q.put(("watchtower_update", {
                "pending": pending, "bad": bad,
                "total": sum(len(messages) for messages in pending.values()),
            }))
            if new_ids or new_bad:
                brief = "；".join(
                    f"{agent}×{len(messages)}" for agent, messages in pending.items())
                if new_bad:
                    brief += ("；" if brief else "") + f"异常文件×{len(new_bad)}"
                watchtower.notify("AgentBus：有新待办", brief)
            previous_signature = signature
            previous_ids = current_ids
            previous_bad = set(bad)

        try:
            watchers = watchtower.event_watchers()
            event_keys = list(watchers)
            event_driven = all(watcher is not None for watcher in watchers.values())
            refresh()
            last_sweep = time.monotonic()
            while not stop.is_set():
                now = time.monotonic()
                if now - last_sweep >= 60.0:
                    warnings = watchtower._sweep_stale(agents)
                    if warnings:
                        self.q.put(("watchtower_stale", {"warnings": warnings}))
                    last_sweep = now
                who = watchtower.bus.wait_for_change(event_keys, 1.0, watchers)
                if who is not None or not event_driven:
                    refresh()
        except Exception as exc:  # noqa: BLE001
            self.q.put(("watchtower_error", {"text": str(exc)}))
        finally:
            watchtower.bus.close_watchers(watchers)

    def _pick_ws(self) -> None:
        p = filedialog.askdirectory(title="选择项目工作空间")
        if p:
            self._stop_watchtower()
            self.cfg = None
            self.bus = None
            self.ws_var.set(p)
            if hasattr(self, 'output_var'):
                self.output_var.set(p)
            save_last_workspace(p)
            if (Path(p) / ".agentbus" / "config.json").exists():
                self._load_cfg(silent=True)
            else:
                self.log(f"已选择 {p}，勾选成员并点击「保存团队」后创建项目配置。", "info")
                self._refresh_workflow()

    def _open_ws(self) -> None:
        p = Path(self.ws_var.get())
        if not p.exists():
            messagebox.showwarning("目录不存在", f"{p} 不存在")
            return
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打不开", str(exc))

    def _pick_output(self) -> None:
        base = self.output_var.get().strip() or self.ws_var.get().strip()
        p = filedialog.askdirectory(title="选择业务产出目录", initialdir=base or None)
        if p:
            self.output_var.set(p)

    def _open_output(self) -> None:
        p = Path(self.output_var.get().strip() or self.ws_var.get().strip())
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showwarning("目录不存在", f"{p} 不存在，且无法创建：{exc}")
                return
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("打不开", str(exc))

    def _init_ws(self) -> int:
        ws = Path(self.ws_var.get()).expanduser()
        try:
            ws.mkdir(parents=True, exist_ok=True)
            config_path = ws / ".agentbus" / "config.json"
            is_new_config = not config_path.exists()
            cfg = load(ws, create=True)
            if is_new_config:
                cfg = save(cfg, initial_gui_group(cfg.data))
        except (ConfigError, OSError) as exc:
            messagebox.showerror("初始化失败", str(exc))
            return 1
        self.cfg = cfg
        self.bus = Bus(ws / ".agentbus")
        self.bus.init()
        self.bus.register(DIRECTOR_INBOX)
        self.bus.register(HUMAN_INBOX)
        for name in cfg.enabled_workers():
            self.bus.register(name)
        agents_md = ws / "AGENTS.md"
        if not agents_md.exists():
            agents_md.write_text(
                "# 项目约定（所有 agent 都会读这个文件）\n\n"
                "## 这是什么项目\n（一句话说明）\n\n"
                "## 产出放哪里\n- 成果/  交付物\n- 资料/  输入素材\n\n"
                "## 硬性规则\n"
                "1. 改共享文件前先抢租约（agentbus lease），不要整文件覆盖。\n"
                "2. 所有产出写上来源与时间。\n"
                "3. 不确定的事不要猜，在报告里明确写\"未验证\"。\n",
                encoding="utf-8")
        self.log(f"\n[初始化] 工作空间就绪：{ws}", "ok")
        self.log(describe(cfg.data), "dim")
        self._apply_cfg_to_ui(cfg)
        self._refresh_product_probes()
        self._gen_mcp()
        save_last_workspace(ws)
        self._start_watchtower(cfg)
        return 0

    def _auto_detect(self) -> None:
        ws = Path(self.ws_var.get())
        if (ws / ".agentbus" / "config.json").exists():
            self._load_cfg(silent=True, monitor=False)
            if self.cfg is not None:
                self._refresh_product_probes()
        else:
            # Startup probes only the five selected products. It does not inspect
            # third-party API credential files.
            self._refresh_product_probes()
            self._refresh_workflow()

    def _load_cfg(self, silent: bool = False, monitor: bool = True) -> None:
        ws = Path(self.ws_var.get()).expanduser()
        try:
            cfg = load(ws)
        except ConfigError as exc:
            self._stop_watchtower()
            self.cfg = None
            self.bus = None
            self._refresh_workflow(error=str(exc))
            self._refresh_connections(error=str(exc))
            if not silent:
                messagebox.showinfo("团队尚未保存", "此项目尚无团队配置。请勾选成员、指定组长并点击「保存团队」。")
            return
        self.cfg = cfg
        self.bus = Bus(ws / ".agentbus")
        self._apply_cfg_to_ui(cfg)
        self._gen_mcp()
        if monitor:
            save_last_workspace(ws)
            self._start_watchtower(cfg)
        if not silent:
            self.log(f"\n[载入配置] {cfg.path}", "ok")

    def _detect(self) -> None:
        self.status_var.set("正在探测本机 agent…")
        self.root.update_idletasks()
        try:
            self.probes = detect_group_agents()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("探测失败", str(exc))
            return
        self.log("\n" + render(self.probes), "info")

        spawnable = [p.name for p in self.probes.values() if p.can_spawn]
        candidates = [p.name for p in self.probes.values()
                      if p.candidate_adapter and not p.can_spawn]
        gui = [p.name for p in self.probes.values() if p.kind == "gui" and p.installed]
        self.detect_label.configure(
            text=f"已找到启动入口 {len(spawnable)} 个：{', '.join(spawnable) or '无'}"
                 f"　|　待验收候选：{', '.join(candidates) or '无'}"
                 f"　|　GUI {len(gui)} 个：{', '.join(gui) or '无'}")

        self.log("检测仅显示本机入口；点击「保存团队」才更新项目配置。", "dim")
        self.status_var.set("探测完成")

    def _refresh_product_probes(self) -> None:
        """Refresh only the five group products; do not inspect credentials."""
        try:
            self.probes = detect_group_agents()
        except Exception as exc:  # noqa: BLE001
            self.log(f"\n本机 Agent 探测失败：{exc}", "warn")
            return
        # Source: Trae peer review, 2026-09-30. Detection is observational;
        # persist its draft only on an explicit save, never on app startup.
        available = sorted(name for name, probe in self.probes.items()
                           if probe.can_spawn)
        candidates = sorted(name for name, probe in self.probes.items()
                            if probe.candidate_adapter and not probe.can_spawn)
        missing = sorted(name for name, probe in self.probes.items()
                         if not probe.installed)
        accepted = [name for name in available if name == "opencode"]
        accepted_display = [self.probes[name].version or name for name in accepted]
        unaccepted = [name for name in available if name not in accepted]
        self.detect_label.configure(
            text=(f"独立接口短任务已实测：{', '.join(accepted_display)}（不代表整组自动接力）　|　" if accepted else "")
                 + f"启动入口（任务需验收）：{', '.join(unaccepted) or '无'}"
                 f"　|　待验收候选：{', '.join(candidates) or '无'}"
                 + (f"　|　未找到：{', '.join(missing)}" if missing else ""))

    def _apply_cfg_to_ui(self, cfg) -> None:
        self.cfg = cfg
        if hasattr(self, 'output_var'):
            self.output_var.set(str(cfg.output_root))
        workers = cfg.data.get("workers", {})
        names = sorted(workers)

        for w in self.workers_frame.winfo_children():
            w.destroy()
        self.worker_vars.clear()
        for index, n in enumerate(names):
            conf = workers[n]
            var = tk.BooleanVar(value=bool(conf.get("enabled", True)))
            self.worker_vars[n] = var
            label = worker_label(n, conf)
            button = ttk.Checkbutton(
                self.workers_frame, text=label, variable=var,
                command=self._refresh_director_choices,
            )
            button.grid(row=index // 2, column=index % 2, sticky="w",
                        padx=(0, 16), pady=(0, 4))
            button.bind('<MouseWheel>', self._scroll_workers)

        canvas = getattr(self, 'workers_canvas', None)
        if canvas is not None:
            canvas.yview_moveto(0)
            canvas.xview_moveto(0)

        self.director_var.set(cfg.director)
        self._refresh_director_choices()
        self._refresh_workflow(cfg)
        self._refresh_connections(cfg)

    def _layout_workers(self, _event=None) -> None:
        canvas = self.workers_canvas
        width = max(canvas.winfo_width(), self.workers_frame.winfo_reqwidth())
        canvas.itemconfigure(self.workers_window, width=width)
        canvas.configure(scrollregion=canvas.bbox('all'))

    def _scroll_workers(self, event) -> str:
        # Bind only the member picker, never a global wheel handler that
        # would hijack scrolling in goal text or logs.
        delta = getattr(event, 'delta', 0)
        if delta:
            units = -int(delta / 120) or (-1 if delta > 0 else 1)
            canvas = self.workers_canvas
            if getattr(event, 'state', 0) & 1:
                canvas.xview_scroll(units, 'units')
            else:
                canvas.yview_scroll(units, 'units')
        return 'break'

    def _refresh_director_choices(self) -> None:
        """Keep the group-lead picker limited to checked group members."""
        selected = [name for name in sorted(self.worker_vars)
                    if self.worker_vars[name].get()]
        self.director_box.configure(values=selected)
        for box_name, var_name in [('goal_agent_box','goal_agent'),
                                   ('workflow_agent_box','workflow_agent'),
                                   ('connection_agent_box','connection_agent')]:
            box=getattr(self,box_name,None); var=getattr(self,var_name,None)
            if box is not None:
                box.configure(values=selected)
                if var.get() not in selected:
                    var.set('codex' if 'codex' in selected else selected[0] if selected else '')
        current = self.director_var.get()
        if current not in selected:
            current = ("codex" if "codex" in selected
                       else selected[0] if selected else "")
            self.director_var.set(current)
        self._on_team_draft_changed()

    def _on_team_draft_changed(self, *_args) -> None:
        """Invalidate observations after editing, without persisting a draft."""
        if getattr(self, 'connection_box', None) is None:
            return
        root = getattr(self, 'root', None)
        if root is None or getattr(self, '_draft_refresh_pending', False):
            return
        self._draft_refresh_pending = True

        def refresh():
            self._draft_refresh_pending = False
            if not getattr(self, '_closed', False):
                self._refresh_connections()
                self._refresh_workflow()

        root.after_idle(refresh)

    # ------------------------------------------------------------------ #
    # 开始 / 停止
    # ------------------------------------------------------------------ #
    def _collect_ui_config(self) -> dict | None:
        raw_ws = self.ws_var.get().strip()
        if not raw_ws:
            messagebox.showwarning("缺工作空间", "先选一个工作空间")
            return None
        ws = Path(raw_ws).expanduser()
        raw_output = self.output_var.get().strip() if hasattr(self, 'output_var') else ''
        output = Path(raw_output).expanduser() if raw_output else ws
        if not output.is_absolute():
            output = ws / output
        # Preserve the user's draft before initialization redraws default controls.
        chosen = [n for n, v in self.worker_vars.items() if v.get()]
        director = self.director_var.get()
        if self.cfg is None or self.cfg.workspace != ws.resolve():
            self.ws_var.set(str(ws))
            if self._init_ws():
                return None

        try:
            # Source: Trae peer review, 2026-09-30. Explicit save is the only
            # point that persists detected entrypoints. Keep permission and
            # automatic-execution choices, even when detection says runnable.
            manager=LeaseManager(self.cfg.workspace/'.agentbus')
            lease=manager.acquire('.agentbus/config.json','agentbus-gui-save',ttl_s=120)
            if lease is None:
                raise ConfigError('小组配置正在由其他成员编辑，未保存。请稍后重试。')
            try:
                fresh=load(self.cfg.workspace)
                if set(fresh.data['workers']) != set(self.cfg.data['workers']):
                    raise ConfigError('成员登记已变化，请重新载入项目后选择小组。')
                draft=json.loads(json.dumps(fresh.data))
                draft['output_root'] = str(output.resolve())
                output.resolve().mkdir(parents=True, exist_ok=True)
                probes=getattr(self,'probes',{})
                if probes:
                    merge_probe_patch(draft,to_config_patch(probes))
                self.cfg=save_group_config(Config(fresh.path,fresh.workspace,draft),chosen,director)
            finally:
                manager.release(lease)
        except ConfigError as exc:
            messagebox.showwarning("小组配置无效", str(exc))
            return None
        except OSError as exc:
            messagebox.showerror("配置不合法", str(exc))
            return None
        return self.cfg.data

    def _save_group(self) -> None:
        if self._collect_ui_config() is None:
            return
        if not self._refresh_saved_group('团队已保存'):
            return
        self._refresh_connections()
        tabs = getattr(self, 'tabs', None)
        if tabs is not None:
            tabs.select(self.connection_page)
        self.status_var.set('团队已保存 · 按团队接入步骤给各成员发送指令')

    def _refresh_saved_group(self, action: str, parent=None) -> bool:
        """Refresh committed membership even when workflow files conflict."""
        self._apply_cfg_to_ui(self.cfg)
        save_last_workspace(self.cfg.workspace)
        self._start_watchtower(self.cfg)
        try:
            prepare_project_workflow(self.cfg)
        except (WorkflowError, OSError) as exc:
            self._refresh_workflow(self.cfg, error=str(exc))
            self.log(f'[{action}] 配置已保存，成员列表和邮箱值守已同步；工作流尚未就绪：{exc}', 'warn')
            messagebox.showwarning('小组已保存，工作流未就绪',
                '小组配置已保存，成员列表和邮箱值守已同步。\n'
                '项目工作流尚未就绪；请处理下面的冲突后，到「管理 → 工作流」重新检查：\n'
                + str(exc), parent=parent)
            return False
        self._refresh_workflow(self.cfg)
        self._refresh_connections(self.cfg)
        self.log(f'[{action}] 成员列表、邮箱值守和项目工作流已同步。没有创建目标或启动 Agent。', 'ok')
        return True

    def _install_codex_skill(self) -> None:
        try:
            result = install_workflows(__version__)
        except (SkillInstallError, OSError) as exc:
            messagebox.showerror("安装失败", str(exc))
            return
        self._show_maintenance(result)
        messagebox.showinfo(
            "小组工作流文件已安装",
            '\n'.join(f"{name}：{info['skill']}" for name, info in result['hosts'].items()) + '\n\n'
            "保存团队后，在本项目的新对话调用 agentbus-team。\n"
            "文件安装不代表宿主已加载；未发现时到「团队接入」复制指令。\n"
            + (host_loading_hint('trae') if self.cfg is not None
               and 'trae' in self.cfg.enabled_workers() else ''))

    def _show_maintenance(self, value: dict) -> None:
        self.maintenance_box.configure(state='normal')
        self.maintenance_box.delete('1.0', 'end')
        self.maintenance_box.insert('end', render_maintenance(value))
        self.maintenance_box.configure(state='disabled')

    def _inspect_workflows(self) -> None:
        try:
            self._show_maintenance(inspect_workflows())
        except (SkillInstallError, OSError) as exc:
            messagebox.showerror('检查未完成', str(exc))

    def _inspect_project(self) -> None:
        cfg = self._workflow_cfg()
        if cfg is not None:
            try:
                path = cfg.path.resolve()
                if (not path.is_relative_to(cfg.workspace.resolve())
                        or any(p.casefold() == '.workbuddy' for p in path.parts)):
                    raise ConfigError('配置路径越界或进入私有目录，停止检查')
                self._show_maintenance(inspect_project(load(cfg.workspace)))
            except (ConfigError, OSError, ValueError) as exc:
                messagebox.showerror('小组检查未完成', str(exc))

    def _open_recovery_center(self) -> None:
        """Observe public facts off Tk's thread, then offer one actual action."""
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        task = getattr(self, '_recovery_task', None)
        if task is None:
            task = self._recovery_task = OverviewSnapshotTask(timeout_s=5)
        if task.start(cfg=Config(path=cfg.path, workspace=cfg.workspace, data=copy.deepcopy(cfg.data))):
            self._recovery_cfg = cfg
            self.recovery_open_btn.configure(state='disabled')
            self._poll_recovery_center()

    def _poll_recovery_center(self) -> None:
        task = self._recovery_task
        if getattr(self, '_closed', False) or getattr(self, '_close_requested', False):
            task.close()
            return
        event = task.poll()
        if event is None:
            if task.pending:
                self.root.after(80, self._poll_recovery_center)
            return
        self.recovery_open_btn.configure(state='normal')
        if event['kind'] != 'result':
            messagebox.showwarning('救援状态未确认', event['message'], parent=self.root)
            return
        cfg = self._recovery_cfg
        if getattr(self, 'cfg', None) is None or self.cfg.workspace != cfg.workspace:
            return
        snapshot = event['value']
        if snapshot.get('records_incomplete'):
            self._show_readonly_text('救援状态未确认', snapshot.get('incomplete_reason') or '记录不完整')
            return
        rows = snapshot.get('recovery_tasks') or []
        if not rows:
            messagebox.showinfo('救援中心', '没有当前项目的在途任务；状态保持未确认。', parent=self.root)
            return
        selected = simpledialog.askstring('选择一条救援任务',
            '\n'.join(f"{r['member_id']}：{r['task_id']}" for r in rows[:15])
            + '\n\n输入完整任务 ID：', parent=self.root,
            initialvalue=rows[0]['task_id'] if len(rows) == 1 else '')
        if not selected:
            return
        matches = [row for row in rows if row['task_id'] == selected.strip()]
        if len(matches) != 1:
            messagebox.showwarning('任务未确认', '未找到唯一的当前项目在途任务。', parent=self.root)
            return
        self._show_recovery_dialog(cfg, matches[0])

    def _show_recovery_dialog(self, cfg, state) -> None:
        decision = decide_recovery(state)
        dialog = tk.Toplevel(self.root)
        dialog.title('异常救援中心 · AgentBus')
        dialog.transient(self.root)
        dialog.geometry('720x330')
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=f"{state['member_id']} · {state['task_id']}",
                  style='Section.TLabel').pack(anchor='w')
        ttk.Label(body, text=decision['reason'] + '\n' + state.get('heartbeat_reason', '')
                  + '\n\n打开窗口、复制指令、认领任务均不能证明执行。'
                  + '\n当前应用只提供文件续接；桌面控制和适配器须有可用回调及本次授权或验收。',
                  wraplength=670, justify='left', style='Hint.TLabel').pack(anchor='w', pady=12)
        actions = ttk.Frame(body)
        actions.pack(side='bottom', fill='x')
        ttk.Button(actions, text='关闭', command=dialog.destroy).pack(side='right')
        available = decision['action'] in ('continue', 'new_chat_pack', 'wake_word')
        button = ttk.Button(actions, text=decision['label'], style='Accent.TButton',
            command=lambda: self._execute_recovery(cfg, state['task_id'], decision['action']))
        button.pack(side='left')
        if not available:
            button.configure(state='disabled')
        self.recovery_action_btn = button

    def _execute_recovery(self, cfg, task_id, expected_action) -> None:
        """Refresh before acting; file callbacks do not transfer processing."""
        try:
            if Path(self.ws_var.get().strip()).resolve() != cfg.workspace:
                raise ValueError('项目已切换；请在当前项目重新检查救援状态')
            cfg = load(cfg.workspace)
            snapshot = read_public_snapshot(cfg)
            if snapshot.get('records_incomplete'):
                raise ValueError('公开记录不完整，救援状态未确认')
            matches = [row for row in snapshot.get('recovery_tasks', []) if row['task_id'] == task_id]
            if len(matches) != 1:
                raise ValueError('在途任务已变化，不能使用旧救援动作')
            state = matches[0]

            def continue_original(decision):
                prompt = resume_prompt(cfg, state['member_id'])
                if isinstance(prompt, dict):
                    prompt = prompt.get('prompt', '')
                self._show_readonly_text('继续原对话', str(prompt) + '\n\n'
                    + f"本次只核对任务 {task_id}。仅交给原认领会话处理；本次操作未转移任务归属。")

            def new_chat(decision):
                pack = decision['pack']
                self._show_readonly_text('新聊天续接包', pack['copy_text'] + '\n\n'
                    + '新聊天不继承权限、登录状态或 processing 所有权。'
                    + '\n原认领仍保留；需按公共契约确认原会话停止后再处理归属。')

            def copy_wake(decision):
                self.root.clipboard_clear()
                self.root.clipboard_append(decision['wake_word'])
                self._show_readonly_text('一次唤醒词已复制', decision['wake_word']
                    + '\n\n复制尚未送达，执行仍需真实回执确认。')

            execute_recovery_action(state, expected_action,
                {'continue': continue_original, 'new_chat_pack': new_chat, 'wake_word': copy_wake})
        except (ConfigError, WorkflowError, OSError, ValueError) as exc:
            messagebox.showwarning('救援动作未执行', str(exc), parent=self.root)

    def _recover_processing(self) -> None:
        """Recover one selected claim only after an explicit owner assertion."""
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        try:
            cfg = load(cfg.workspace)
            rows = inspect_project(cfg).get('processing', [])
            if not rows:
                messagebox.showinfo('恢复中断任务', '没有可选择的执行中记录；未修改任何任务。', parent=self.root)
                return
            options = '\n'.join(f"{r['agent']}：{r['id']}" for r in rows[:15])
            selected = simpledialog.askstring('选择一条中断任务',
                options + '\n\n输入要恢复的完整任务 ID（不处理其他记录）：',
                parent=self.root, initialvalue=rows[0]['id'] if len(rows) == 1 else '')
            if not selected:
                return
            matches = [r for r in rows if r['id'] == selected.strip()]
            if len(matches) != 1:
                raise ValueError('未找到唯一执行中记录；请先检查当前小组')
            row = matches[0]
            if not messagebox.askyesno('确认原执行者已停止',
                    f"成员：{row['agent']}\n任务：{row['id']}\n\n"
                    '请确认原会话或执行程序已停止，并已核对现有产物。\n'
                    '程序不会判断进程存活；误恢复可能重复执行并产生副作用。\n'
                    '只把这条认领交回待办，不重置截止时间或预算。确认恢复？',
                    parent=self.root, default='no'):
                return
            Bus(cfg.workspace / '.agentbus').recover_claim(
                row['agent'], row['id'], owner_confirmed_inactive=True)
        except (ConfigError, BusError, OSError, ValueError) as exc:
            messagebox.showwarning('未恢复', str(exc), parent=self.root)
            return
        messagebox.showinfo('恢复已登记',
            '这条任务已回待办；原产物保留，截止时间与预算未重置。\n'
            '继续同一任务时须先核对已有产物，恢复不代表任务完成。', parent=self.root)

    def _save_diagnostic(self) -> None:
        """Explicit local export, no task repair or automatic transmission."""
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        destination = filedialog.asksaveasfilename(
            parent=self.root, title='保存本地诊断（不包含任务正文）',
            initialdir=str(cfg.workspace),
            initialfile=time.strftime('AgentBus-diagnostic-%Y%m%d-%H%M%S.json'),
            defaultextension='.json', filetypes=[('JSON诊断', '*.json')])
        if not destination:
            return
        try:
            save_diagnostic(load(cfg.workspace), Path(destination))
        except (ConfigError, OSError, ValueError) as exc:
            messagebox.showwarning('诊断未保存',
                '保留原文件；请选一个新的文件名。\n' + type(exc).__name__, parent=self.root)
            return
        messagebox.showinfo('诊断已保存', str(destination) + '\n\n'
            '不含任务正文、结果、命令或凭据；没有修改作业、认领或预算。', parent=self.root)

    def _preview_uninstall_workflows(self) -> None:
        try:
            self._show_maintenance(uninstall_workflows(apply=False))
        except (SkillInstallError, OSError) as exc:
            messagebox.showerror('预览未完成', str(exc))

    def _uninstall_workflows(self) -> None:
        try:
            preview = uninstall_workflows(apply=False)
        except (SkillInstallError, OSError) as exc:
            messagebox.showerror('移除未完成', str(exc))
            return
        self._show_maintenance(preview)
        if preview.get('errors') or not preview.get('removable'):
            return
        if not messagebox.askyesno('移除小组工作流',
                f"将移除 {len(preview['removable'])} 个未修改且归属已确认的工作流文件。\n"
                '项目、任务记录和你修改过的文件会保留。确认移除？'):
            return
        try:
            self._show_maintenance(uninstall_workflows(apply=True))
        except (SkillInstallError, OSError) as exc:
            messagebox.showerror('移除未完成', str(exc))

    def _add_member(self) -> None:
        cfg=self._workflow_cfg()
        if cfg is None: return
        from .catalog import catalog
        dialog=tk.Toplevel(self.root); dialog.title('添加小组成员'); dialog.transient(self.root)
        dialog.geometry('740x620')
        recommendations = {f"{entry['name']}（{entry['id']}）": entry
                           for entry in catalog()}
        selected = tk.StringVar(value='自定义成员')
        ttk.Label(dialog, text='推荐目录（只填名称与ID；本机均未验收）').pack(
            anchor='w', padx=16, pady=(12, 2))
        selector = ttk.Combobox(dialog, textvariable=selected, state='readonly',
                                values=['自定义成员', *recommendations], width=68)
        selector.pack(fill='x', padx=16)
        fields={}
        for label,key in [('成员ID（英文小写，例如 cursor）','id'),
                          ('显示名称','name'),('ACP候选命令（可留空；JSON数组）','command')]:
            ttk.Label(dialog,text=label).pack(anchor='w',padx=16,pady=(10,2))
            var=tk.StringVar(); fields[key]=var
            ttk.Entry(dialog,textvariable=var,width=65).pack(fill='x',padx=16)
        details = scrolledtext.ScrolledText(dialog, height=8, wrap='word',
                                            font=FONT_SMALL)
        details.pack(fill='both', expand=True, padx=16, pady=(12, 0))

        def show_recommendation(_event=None):
            entry = recommendations.get(selected.get())
            # Never carry a different agent's candidate command across choices.
            fields['command'].set('')
            if entry is not None:
                fields['id'].set(entry['id'])
                fields['name'].set(entry['name'])
                text = (f"{entry['description']}\n\n"
                        f"上游接入声明：{entry['integration']}\n"
                        f"来源：{entry['source_url']}\n"
                        f"来源核对日期：{entry['checked_at']}\n"
                        '本机验收：未验收（not_checked）。\n'
                        '来源与协议声明不代表当前电脑已安装、已鉴权或已通过接入验收。')
            else:
                text = ('自定义成员：填写你使用的软件名称与成员ID。\n'
                        '推荐目录不执行安装、登录或启动；选择推荐只填名称与ID。\n'
                        '默认由该软件的项目对话接手；CLI 与桌面会话需分别验收。')
            details.configure(state='normal')
            details.delete('1.0', 'end')
            details.insert('end', text)
            details.configure(state='disabled')

        selector.bind('<<ComboboxSelected>>', show_recommendation)
        show_recommendation()
        ttk.Label(dialog,text='默认通过该软件的对话接手；登记ACP不代表自动执行已验收。',
                  style='Hint.TLabel').pack(anchor='w',padx=16,pady=10)
        def register():
            try:
                raw=fields['command'].get().strip()
                command=json.loads(raw) if raw else None
            except ValueError as exc:
                messagebox.showwarning('未添加',str(exc),parent=dialog)
                return
            if self._register_member(fields['id'].get().strip(),
                    fields['name'].get().strip() or fields['id'].get().strip(),
                    command, parent=dialog):
                dialog.destroy()
        ttk.Button(dialog,text='登记成员',command=register).pack(side='right',padx=16,pady=8)

    def _register_member(self, name: str, display_name: str, command, parent=None) -> bool:
        cfg = self._workflow_cfg()
        if cfg is None:
            return False
        from .profiles import add_profile
        try:
            self.cfg = add_profile(cfg, name, display_name,
                                   'acp' if command is not None else 'gui', command)
        except (ValueError, OSError, ConfigError) as exc:
            messagebox.showwarning('未添加', str(exc), parent=parent)
            return False
        self._refresh_saved_group('成员已登记并加入小组，组长保持原选择', parent=parent)
        # This return value means the profile was committed, not that a
        # workflow conflict disappeared. Re-registering would be incorrect.
        return True

    def _import_team(self) -> None:
        raw_ws = self.ws_var.get().strip()
        if not raw_ws:
            messagebox.showwarning('先选择项目', '先选择你自己的项目目录，再导入成员模板。')
            return
        ws = Path(raw_ws).expanduser().resolve()
        if any(part.casefold() == '.workbuddy' for part in ws.parts):
            messagebox.showwarning('未导入', '不能访问或使用.workbuddy私有目录作为项目。')
            return
        if getattr(self, 'cfg', None) is not None and self.cfg.workspace == ws:
            if self._workflow_cfg() is None:
                return
        path=filedialog.askopenfilename(title='选择分发包中的 team-template.json',
                                        filetypes=[('成员模板','*.json')])
        if not path: return
        from .sharing import import_template, new_template_config
        def saved_or_preview():
            config_path = ws / '.agentbus/config.json'
            if config_path.exists():
                return load(ws)
            # Validate a first import before creating any project/runtime
            # file. Local launch commands are not inferred or auto-detected.
            return new_template_config(ws)
        try:
            payload=json.loads(Path(path).read_text(encoding='utf-8-sig'))
            cfg = saved_or_preview()
            updated=import_template(cfg,payload,replace=True)
            if not messagebox.askyesno('导入成员选择',
                '将成员选择改为：'+', '.join(updated.enabled_workers())+'\n组长：'+updated.director+
                '\n保留本机权限、已有任务和项目文件。是否导入？'): return
            lm=LeaseManager(cfg.workspace/'.agentbus')
            lease=lm.acquire('.agentbus/config.json','agentbus-gui-import',ttl_s=120)
            if not lease: raise ValueError('配置正在编辑，请稍后导入')
            try:
                self.cfg=save(import_template(saved_or_preview(),payload,replace=True))
            finally: lm.release(lease)
        except (ValueError,OSError,ConfigError,WorkflowError) as exc:
            messagebox.showwarning('未导入',str(exc))
            return
        self._refresh_saved_group('成员模板已导入')

    def _export_team(self) -> None:
        cfg=self._workflow_cfg()
        if cfg is None: return
        if not getattr(sys,'frozen',False):
            messagebox.showinfo('请使用交付程序','请在 AgentBus.exe 中导出分发包；源码入口不包含运行时。'); return
        path=filedialog.asksaveasfilename(title='保存应用分发包',
                    initialfile='AgentBus-Distribution.zip',defaultextension='.zip',filetypes=[('ZIP','*.zip')])
        if not path: return
        from .sharing import build_share_package
        try:
            result=build_share_package(Path(sys.executable),cfg,Path(path))
            messagebox.showinfo('分发包已保存',str(path)+'\n包含程序、成员模板和说明；不包含项目、会话与凭据。')
            self.log('已生成分发包：'+str(result),'ok')
        except (ValueError,OSError,RuntimeError) as exc:
            messagebox.showwarning('未导出',str(exc))

    def _refresh_project_plan(self, silent: bool = False) -> None:
        cfg = getattr(self, 'cfg', None) if silent else self._workflow_cfg()
        if cfg is None or Path(self.ws_var.get().strip()).expanduser().resolve() != cfg.workspace:
            return
        from .project_plan import plan_summary
        try:
            summary = plan_summary(load(cfg.workspace))
            current = summary['current']
            if current:
                text = ('当前总体计划：' + current['revision_id'] + '\n'
                        + '采用时间：' + current.get('created_at', current.get('at', '已登记')) + '\n'
                        + '固定采用原文（文内进度按采用当时解释）：\n' + current['content_md'] + '\n\n小组意见与取舍\n'
                        + '\n'.join(member + '：' + reason for member, reason in current['dispositions'].items()))
            else:
                text = '总体计划尚未采用。先提出整体目标与验收，再收取指定成员意见。\n'
            readable = cfg.workspace / 'PROJECT_PLAN.md'
            if readable.is_file():
                from .workspace_files import WorkspaceFiles
                if readable.stat().st_size > 64 * 1024:
                    raise ValueError('可读计划超过64KiB；请缩短当前状态，历史保留在计划账本')
                files=WorkspaceFiles(cfg.workspace);line=1;digest=None;parts=[]
                for _ in range(128):
                    page=files.read_text('PROJECT_PLAN.md',start_line=line,expected_sha256=digest)
                    digest=page['sha256'];parts.append(page['text']);line=page['next_line']
                    if line is None: break
                else:
                    raise ValueError('可读计划分页过多；未静默截断')
                label = ('当前执行状态（可更新进度，不改变已采用验收）：\n' if current else
                         '项目草案（不代表程序已采用）：\n')
                text = label + ''.join(parts) + '\n\n' + text
            for proposal in summary['proposals']:
                text += ('\n\n提案 ' + proposal['id'] + ' · ' + proposal['state']
                         + '\n已登记：' + ', '.join(proposal['received'])
                         + '\n待收到：' + (', '.join(proposal['missing']) or '无'))
                if proposal.get('invalid'):
                    text += '\n证据失效：' + json.dumps(proposal['invalid'],ensure_ascii=False)
            box = self.project_plan_box
            box.configure(state='normal');box.delete('1.0','end');box.insert('end',text);box.configure(state='disabled')
        except (ValueError, OSError, RuntimeError) as exc:
            if not silent:
                messagebox.showwarning('计划记录无法读取', str(exc))
            else:
                self.log('总体计划读取失败：' + str(exc), 'warn')

    def _refresh_jobs(self, technical: bool | None = None, silent: bool = False) -> None:
        # Automatic observations use the saved project, not unsaved controls;
        # a directory event must never summon a modal permission/save prompt.
        if silent:
            cfg = getattr(self, 'cfg', None)
            if cfg is None or Path(self.ws_var.get().strip()).expanduser().resolve() != cfg.workspace:
                return
        else:
            cfg = self._workflow_cfg()
            if cfg is None:
                return
        from .jobs import list_jobs
        try:
            cfg = load(cfg.workspace)
            jobs=list_jobs(cfg)
            if hasattr(self, 'project_plan_box'):
                self._refresh_project_plan(silent=True)
            if technical is not None:
                self._jobs_technical = technical
            text=(json.dumps(jobs,ensure_ascii=False,indent=2)
                  if getattr(self, '_jobs_technical', False) else jobs_summary(cfg, jobs))
            if text == getattr(self, '_jobs_rendered', None):
                return
            self.jobs_box.configure(state='normal'); self.jobs_box.delete('1.0','end')
            self.jobs_box.insert('end',text); self.jobs_box.configure(state='disabled')
            self._jobs_rendered = text
            self._jobs_error = None
        except (ValueError,OSError,RuntimeError) as exc:
            if silent:
                error = str(exc)
                if error != getattr(self, '_jobs_error', None):
                    self.log('作业记录暂时无法读取：' + error, 'warn')
                self._jobs_error = error
            else:
                messagebox.showwarning('无法查看',str(exc))

    def _open_opencode_records(self) -> None:
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        path = cfg.workspace / '.agentbus/agents/opencode'
        if not path.is_dir():
            messagebox.showinfo('暂无OpenCode记录', '当前项目还没有OpenCode信箱。先保存包含它的小组并派发任务。')
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showwarning('打不开记录目录', str(exc))

    def _workflow_cfg(self):
        if self.cfg is None or Path(self.ws_var.get().strip()).expanduser().resolve() != self.cfg.workspace:
            messagebox.showwarning('先保存团队', '请先选择当前项目并保存团队')
            return None
        chosen = {n for n, value in self.worker_vars.items() if value.get()}
        saved = {n for n, value in self.cfg.data['workers'].items() if value.get('enabled', True)}
        if chosen != saved or self.director_var.get() != self.cfg.director:
            messagebox.showwarning('先保存团队', '成员或组长有未保存修改，请先保存')
            return None
        return self.cfg

    def _show_skill_check(self) -> None:
        """Source: Codex internal integration, 2026-10-05; saved project only."""
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        dialog = getattr(self, '_skill_dialog', None)
        if (dialog is not None and dialog.closed and dialog.task.worker is not None
                and dialog.task.worker.is_alive()):
            messagebox.showwarning('检查仍在收尾', '前一次检查仍在完成文件读取，请稍后重新打开。')
            return
        if dialog is not None and not dialog.closed:
            if dialog.workspace == cfg.workspace:
                dialog.window.lift()
                return
            dialog.close()
        self._skill_dialog = SkillCheckDialog(self.root, cfg, self._workflow_cfg)

    def _refresh_connections(self, cfg=None, summary=None, error: str | None = None) -> None:
        """Observe only the saved project's bus; no write or model invocation."""
        box = getattr(self, 'connection_box', None)
        if box is None:
            return
        cfg = cfg or getattr(self, 'cfg', None)
        raw = self.ws_var.get().strip() if hasattr(self, 'ws_var') else ''
        try:
            workspace = Path(raw).expanduser().resolve() if raw else None
        except (OSError, ValueError):
            workspace = None
        chosen = {name for name, value in getattr(self, 'worker_vars', {}).items() if value.get()}
        leader = getattr(self, 'director_var', None)
        dirty = bool(cfg is not None and (workspace != cfg.workspace
                     or chosen != set(cfg.enabled_workers())
                     or (leader is not None and leader.get() != cfg.director)))
        unavailable = cfg is None or dirty
        copy_button = getattr(self, 'connection_copy_btn', None)
        if copy_button is not None:
            copy_button.configure(state='disabled' if unavailable else 'normal')
        if unavailable:
            summary = {'workspace': workspace or raw or '尚未选择', 'coordinator': '尚未选择',
                       'members': []}
        elif summary is None:
            try:
                summary = connection_summary(cfg)
            except (OSError, ValueError, TypeError) as exc:
                summary = {'workspace': cfg.workspace, 'coordinator': cfg.director, 'members': []}
                error = str(exc)
        members = summary.get('members') or []
        names = [str(item.get('name', '')) for item in members]
        selector = getattr(self, 'connection_agent_box', None)
        var = getattr(self, 'connection_agent', None)
        if selector is not None:
            selector.configure(values=names)
        selected = var.get() if var is not None else ''
        if selected not in names:
            selected = ('codex' if 'codex' in names else names[0] if names else '')
            if var is not None:
                var.set(selected)
        active = next((item for item in members if item.get('name') == selected), {})
        indicator = active.get('indicator') or {'label': '待保存' if dirty else '待接入', 'color': '#6b7280'}
        if error:
            indicator = {'label': '检查失败', 'color': '#b42318'}
        state_label = getattr(self, 'connection_indicator', None)
        if state_label is not None:
            who = ' '.join(str(active.get('display_name') or active.get('name') or '所选成员').split())[:40]
            state_label.configure(text=str(indicator.get('symbol') or '●') + ' ' + who + ' · '
                                  + str(indicator.get('label', '待接入')),
                                  foreground=str(indicator.get('color') or '#6b7280'))
        text = render_connection_summary(summary, selected)
        if dirty:
            text = '项目、成员或组长有未保存修改。旧接入证据已隐藏，请先保存团队再发送指令。\n\n' + text
        if error:
            text = '接入记录无法完整读取：' + error + '\n\n' + text
        if text != getattr(self, '_connection_rendered', None):
            box.configure(state='normal')
            box.delete('1.0', 'end')
            box.insert('end', text)
            for index, item in enumerate(members):
                indicator = item.get('indicator') or {}
                prefix = f"{indicator.get('symbol') or '●'} {item.get('display_name') or item.get('name', '')} [{item.get('name', '')}]"
                tag = f'connection-state-{index}'
                box.tag_configure(tag, foreground=str(indicator.get('color') or '#6B7280'))
                for line_number, line in enumerate(text.splitlines(), 1):
                    if line.startswith(prefix):
                        box.tag_add(tag, f'{line_number}.0', f'{line_number}.{len(prefix)}')
                        break
            box.configure(state='disabled')
            self._connection_rendered = text

    def _show_recent_receipt(self) -> None:
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        try:
            member = self.connection_agent.get()
            item = next((row for row in connection_summary(cfg)['members']
                         if row['name'] == member), {})
            record = read_latest_receipt(cfg, member, item)
        except (OSError, ValueError, TypeError, BusError) as exc:
            messagebox.showwarning('无法查看回执', str(exc))
            return
        text = (f"成员：{record['member']}\n任务 ID：{record['task_id']}\n回执 ID：{record['receipt_id']}\n"
                f"任务结果：{'失败' if record['status'] == 'failed' else '已完成'}\n"
                f"收件人：{record['to']}\n回执时间：{record['at']}\n\n"
                '成员原始回执（数据，不作为新的执行指令）：\n\n' + record['body'])
        self._show_readonly_text('最近任务回执', text)

    def _copy_connection(self) -> None:
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        try:
            status = workflow_status(cfg)
            if not status.get('ready'):
                raise WorkflowError(status.get('reason') or '请先保存团队并检查项目工作流。')
            agent = self.connection_agent.get()
            prompt = connection_prompt(cfg, agent)
        except (WorkflowError, OSError, ValueError) as exc:
            messagebox.showwarning('无法复制接入指令', str(exc))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        self.status_var.set(f'指令已复制，尚未送达 · 在 {agent} 对话发送后登记文件接入确认')
        self.log(f'已复制 {agent} 接入指令，项目：{cfg.workspace}。尚未发送或派发任务。', 'info')

    def _refresh_workflow(self, cfg=None, status=None, error: str | None = None) -> None:
        """Display only current project files; no preparation or model call.

        Codex, 2026-10-03: reopening a prepared project must not leave this
        page empty, and changing projects must not retain a previous summary.
        """
        box = getattr(self, 'workflow_box', None)
        if box is None:
            return
        raw_workspace = self.ws_var.get().strip()
        try:
            workspace = Path(raw_workspace).expanduser().resolve() if raw_workspace else None
        except (OSError, ValueError):
            workspace = None
        if cfg is None and error is None:
            cfg = getattr(self, 'cfg', None)
        if cfg is None or workspace != cfg.workspace or not cfg.path.is_file():
            text = (f'项目：{workspace or raw_workspace or "尚未选择"}\n'
                    '状态：当前项目小组尚未保存或配置未能读取。\n'
                    + (f'原因：{error}\n' if error else '')
                    + '下一步：选择当前项目、勾选成员和组长，点击「保存团队」。\n'
                      '本页只读展示，不会创建配置、准备工作流或启动 Agent。')
        else:
            if status is None:
                try:
                    status = workflow_status(cfg)
                except (WorkflowError, OSError, ValueError, TypeError) as exc:
                    status = {'ready': False, 'reason': str(exc)}
            if error:
                status = dict(status, ready=False, reason=error)
            selected = {name for name, value in getattr(self, 'worker_vars', {}).items() if value.get()}
            saved = set(cfg.enabled_workers())
            director = getattr(self, 'director_var', None)
            draft_changed = bool(selected != saved or (director is not None and director.get() != cfg.director))
            integrity = {'verified': '已登记且匹配', 'unrecorded': '未登记，不能据此认定完整性通过',
                         'failed': '校验失败'}.get(status.get('launcher_integrity'), '尚未检查')
            ready = bool(status.get('ready'))
            lines = [f'项目：{cfg.workspace}', f'组长：{cfg.director}',
                     '成员：' + ', '.join(cfg.enabled_workers()),
                     '状态：' + ('已准备（文件检查通过）' if ready else '尚未就绪'),
                     '程序摘要：' + integrity]
            if draft_changed:
                lines.append('当前成员或组长有未保存修改；以上显示磁盘团队，使用新选择前请先保存团队。')
            if not ready:
                lines.append('原因：' + (status.get('reason') or '项目工作流或程序入口未就绪'))
                lines.append('下一步：打开正确位置的 EXE → 选择当前项目 → 保存团队；用户改动须先核对。')
            elif draft_changed:
                lines.append('下一步：先保存团队，再复制接入指令或在成员对话发起目标。')
            else:
                lines.append('下一步：在本项目的成员新对话说「使用这个小组工作：<目标>」；继续旧任务说「使用这个小组工作：继续」。')
                lines.append('宿主未发现技能时，到「团队接入」选择成员并复制接入指令。')
                for member in cfg.enabled_workers():
                    if member in ('trae', 'opencode'):
                        lines.append(host_loading_hint(member))
            if status.get('files'):
                lines.extend(['', '项目工作流文件：', *status['files']])
            lines.extend(['', status.get('native_loading') or '文件检查不代表宿主已加载、认证或真实任务执行通过。'])
            text = '\n'.join(lines)
        box.configure(state='normal')
        box.delete('1.0', 'end')
        box.insert('end', text)
        box.configure(state='disabled')

    def _prepare_workflow(self) -> None:
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        try:
            prepare_project_workflow(cfg)
            status = workflow_status(cfg)
            if not status['ready']:
                raise WorkflowError(status.get('reason') or '程序入口尚未就绪，请保存团队')
        except (WorkflowError, OSError) as exc:
            self._refresh_workflow(cfg, error=str(exc))
            messagebox.showwarning('工作流需处理', str(exc))
            return
        self._refresh_workflow(cfg, status)

    def _copy_resume(self) -> None:
        cfg = self._workflow_cfg()
        if cfg is None:
            return
        try:
            status = workflow_status(cfg)
            if not status['ready']:
                raise WorkflowError(status.get('reason') or
                                    '项目工作流缺失或被改动，请先准备 / 检查工作流')
            prompt = resume_prompt(cfg, self.workflow_agent.get())
        except (WorkflowError, OSError) as exc:
            messagebox.showwarning('无法复制', str(exc))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        self.log(f'已复制 {self.workflow_agent.get()} 新对话入口，项目：{cfg.workspace}', 'ok')

    def _start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("正在运行", "已有任务正在执行，请等待结束或请求停止。")
            return
        goal = self.goal_text.get("1.0", "end").strip()
        if not goal:
            messagebox.showwarning("缺目标", "写一句你想要的结果，比如"
                                            "「把本地 SD 部署起来并跑通一张测试图」")
            return
        # Source: ZCode peer review, 2026-09-30. A copy action must not
        # initialize a workspace or persist a draft group.
        if self.cfg is None:
            messagebox.showwarning("先保存团队", "请先核对项目并点击「保存团队」，再复制目标。")
            return
        selected = {name for name, value in self.worker_vars.items() if value.get()}
        saved = {name for name, worker in self.cfg.data.get("workers", {}).items()
                 if worker.get("enabled", True)}
        if (Path(self.ws_var.get().strip()).expanduser().resolve() != self.cfg.workspace
                or selected != saved or self.director_var.get() != self.cfg.director):
            messagebox.showwarning("团队尚未保存", "项目或团队选择已改变，请先点击「保存团队」。")
            return
        self.root.clipboard_clear()
        caller=getattr(self,'goal_agent',None)
        caller=caller.get() if caller is not None else 'codex'
        prefix = '$agentbus-team ' if caller == 'codex' else '使用这个小组工作：'
        self.root.clipboard_append(prefix + goal +
            f'。项目：{self.cfg.workspace}；当前成员身份：{caller}。先读本项目AGENTBUS.md并按工作流登记目标，组长按配置，不改权限。')
        self.log(f"目标已复制。请在项目 {self.cfg.workspace} 的 {caller} 对话粘贴执行。", "info")
        messagebox.showinfo("目标已复制", f"项目：{self.cfg.workspace}\n\n"
                            f"到该项目的 {caller} 对话粘贴并发送。"
                            "本窗口没有启动独立模型；其他组长仍按项目配置执行。")

    def _stop(self) -> None:
        self.stop_flag.set()
        self.log("\n已请求停止；当前调用结束后停止派发。", "warn")
        self.status_var.set("正在停止…")

    def _close(self) -> None:
        if self._closed or self._close_requested:
            return
        self._close_requested = True
        skill_dialog = getattr(self, '_skill_dialog', None)
        if skill_dialog is not None:
            skill_dialog.close()
        self.stop_flag.set()
        worker = self.worker
        if worker is not None and worker.is_alive():
            self.log("\n正在安全收尾：当前 Agent 调用结束并清理会话后关闭窗口。", "warn")
            self.status_var.set("正在安全收尾…")
            return
        self._finalize_close()

    def _finalize_close(self) -> None:
        if self._closed:
            return
        worker = self.worker
        if worker is not None and worker.is_alive():
            # pipeline_finished is queued at the end of the worker's finally
            # block. Defer destruction until that thread has actually returned.
            self.root.after(25, self._finalize_close)
            return
        task = getattr(self, '_task_tree_action_task', None)
        if task is not None and task.worker is not None and task.worker.is_alive():
            self.root.after(25, self._finalize_close)
            return
        self._closed = True
        self._stop_watchtower()
        try:
            save_last_workspace(self.ws_var.get().strip())
        except OSError as exc:
            self.log(f"未能记住工作区：{exc}", "warn")
        self.root.destroy()

    def _finish_run(self) -> None:
        self.start_btn.configure(state="normal")
        if self.stop_btn is not None:
            self.stop_btn.configure(state="disabled")
        self._refresh_handoff()
        self._refresh_esc()

    def _run_pipeline(self, goal: str, approve: bool) -> None:
        """Legacy regression path; no current GUI control calls this method.

        Keep independent execution in the explicitly selected CLI runner.
        Do not reconnect this method to the conversation-workflow copy button.
        Source: WorkBuddy and ZCode peer reviews, 2026-09-30.
        """
        def emit(kind: str, **data) -> None:
            self.q.put((kind, data))

        d = None
        try:
            d = Director(self.cfg, on_event=emit)
            self.director = d
            d._goal = goal

            session_errors = d.start_sessions()
            if session_errors:
                d.close()
                emit("session_preflight_failed", errors=session_errors)
                return

            gate = self._approve_plan if approve else None
            tasks = d.submit(goal, auto_plan=True, approve_plan=gate)
            if not tasks:
                d.close()
                emit("error", text="没有派发任务（计划被取消或没有可用执行者）；小组已停止")
                return

            d.run(keepalive=True, should_stop=self.stop_flag.is_set, on_event=emit)
        except ConfigError as exc:
            emit("error", text=f"配置错误：{exc}")
        except Exception as exc:  # noqa: BLE001
            import traceback
            emit("error", text=f"运行出错：{exc}\n{traceback.format_exc()[-1500:]}")
        finally:
            close = getattr(d, "close", None) if d is not None else None
            if callable(close):
                # run(keepalive=True) closes adapters itself; this covers failures
                # during preflight, planning, or dispatch.
                try:
                    close()
                except Exception as exc:  # noqa: BLE001
                    emit("error", text=f"关闭 Agent 会话时出错：{exc}")
            emit("pipeline_finished")

    def _approve_plan(self, tasks: list[dict]) -> list[dict] | None:
        """在后台线程里被调用 —— 必须把对话框甩回主线程，然后在这里等。"""
        ev = threading.Event()
        box: dict = {}
        self.q.put(("need_approval", {"tasks": tasks, "ev": ev, "box": box}))
        while not ev.wait(0.4):
            if self.stop_flag.is_set():
                return None
        return box.get("decision")

    def _show_plan_dialog(self, d: dict) -> None:
        dlg = PlanDialog(self.root, d["tasks"], self.goal_text.get("1.0", "end").strip())
        d["box"]["decision"] = dlg.decision
        d["ev"].set()

    def _show_permission_dialog(self, d: dict) -> None:
        request = d.get("request") or {}
        tool = request.get("toolCall") or {}
        title = str(tool.get("title") or tool.get("name") or "工具操作")[:240]
        kind = str(tool.get("kind") or "other")
        locations = tool.get("locations") or []
        paths = [str(item.get("path")) for item in locations
                 if isinstance(item, dict) and item.get("path")]
        detail = (f"来自成员：{d.get('agent', '未知')}\n"
                  f"操作类型：{kind}\n操作：{title}")
        if paths:
            detail += "\n路径：" + "、".join(paths[:5])
        allowed = next((item.get("optionId") for item in request.get("options", [])
                        if isinstance(item, dict)
                        and item.get("kind") == "allow_once"), None)
        rejected = next((item.get("optionId") for item in request.get("options", [])
                         if isinstance(item, dict)
                         and item.get("kind") == "reject_once"), None)
        if allowed and rejected and messagebox.askyesno(
                "Agent 权限请求", detail + "\n\n仅允许这一次操作？"):
            d["decision"]["option_id"] = allowed
        elif rejected:
            d["decision"]["option_id"] = rejected
        d["ready"].set()

    # ------------------------------------------------------------------ #
    # 人工接手 / 待决 / MCP
    # ------------------------------------------------------------------ #
    def _refresh_handoff(self) -> None:
        if self.cfg is None:
            return
        bus = self.bus or Bus(self.cfg.workspace / ".agentbus")
        self.handoff_box.configure(state="normal")
        self.handoff_box.delete("1.0", "end")
        total = 0
        self._manual_prompts = []
        human_members = self.cfg.human_workers()
        for name in sorted(self.cfg.enabled_workers()):
            blocked = sorted((bus.agents_dir / name / "blocked").glob("*.json"))
            pending = bus.pending(name) if name in human_members else []
            for f in [*pending, *blocked]:
                try:
                    m = bus.read(f)
                except Exception:  # noqa: BLE001
                    continue
                if m.type not in {"task", "goal", "review"}:
                    continue
                if f.parent.name == "blocked":
                    # Source: independent release review, 2026-10-04. A crash
                    # after terminal commit can leave a blocked copy. Read the
                    # terminal record without deleting either audit artifact.
                    terminal = bus.agents_dir / name / "done" / f.name
                    try:
                        finished = bus.read(terminal)
                    except (OSError, ValueError, BusError):
                        finished = None
                    if (finished is not None and finished.id == m.id
                            and finished.status in {"done", "failed"}):
                        continue
                total += 1
                self.handoff_box.insert("end", "=" * 70 + "\n")
                state = "阻塞·先澄清原因" if f.parent.name == "blocked" else "待接手"
                self.handoff_box.insert("end", f"给 {name} 的任务  [{m.id}]  {m.title}（{state}）\n")
                self.handoff_box.insert("end", "-" * 70 + "\n")
                prompt = f"""你是本项目成员 {name}，工作空间：{self.cfg.workspace}
任务 ID：{m.id}；状态：{state}。只处理本条，遵守任务范围、项目租约与权限。

【任务】{m.title}

{m.body}

【完成后请输出】一段结论：做了什么、产物在哪（给出文件路径）、有没有没验证的部分。

【回执收件人】{m.result_to or '请先按任务正文核对'}
【提交回执】不要直接编辑或移动 .agentbus 消息文件。已有原生回执工具则按任务 ID 正式报告；否则仅回复本对话，由本轮协调会话（项目组长：{self.cfg.director}）在观察到真实回复后通过正式接口登记结果。人工传递不代表各软件已自动互联。
"""
                self._manual_prompts.append(prompt)
                self.handoff_box.insert("end", prompt + "\n")
        if total == 0:
            self.handoff_box.insert("end", "（当前没有待人工接手的任务）\n")
        self.handoff_box.configure(state="disabled")
        self._handoff_text = self.handoff_box.get("1.0", "end")

    def _copy_handoff(self) -> None:
        # Source: Codex release check, 2026-10-04. A cached handoff may belong
        # to the previous project or to a task that has already finished.
        if self._workflow_cfg() is None:
            return
        self._refresh_handoff()
        prompts = getattr(self, "_manual_prompts", [])
        text = prompts[0] if prompts else ""
        if not text.strip() or text.strip().startswith("（当前没有"):
            messagebox.showinfo("没有内容", "当前没有待人工接手的任务")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log("  已把人工接手的 prompt 复制到剪贴板", "ok")

    def _refresh_esc(self) -> None:
        if self.cfg is None:
            return
        try:
            d = self.director or Director(self.cfg)
            esc = d.escalations()
        except Exception:  # noqa: BLE001
            return
        self.esc_box.configure(state="normal")
        self.esc_box.delete("1.0", "end")
        if not esc:
            self.esc_box.insert("end", "（没有需要你决定的事项）\n")
        for m in esc:
            self.esc_box.insert("end", f"--- [{m.id}] {m.title}\n{m.body}\n\n")
        self.esc_box.configure(state="disabled")

    def _mcp_config(self) -> dict:
        ws = str(Path(self.ws_var.get()).expanduser().resolve())
        return {
            "mcpServers": {
                "agentbus": {
                    "command": launch_argv()[0],
                    "args": [*launch_argv()[1:], "mcp", "--workspace", ws],
                }
            }
        }

    def _gen_mcp(self) -> None:
        text = json.dumps(self._mcp_config(), ensure_ascii=False, indent=2)
        self.mcp_box.delete("1.0", "end")
        self.mcp_box.insert("end", text)
        self._mcp_text = text

    def _copy_mcp(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(getattr(self, "_mcp_text", ""))
        self.log("  MCP 配置已复制到剪贴板", "ok")

    def _check_mcp(self) -> None:
        """真的起一个 MCP 子进程，握手一次，确认配置可用。"""
        ws = Path(self.ws_var.get()).expanduser()
        if not (ws / ".agentbus" / "config.json").exists():
            messagebox.showwarning("团队尚未保存", "请先选择项目并点击「保存团队」。")
            return
        argv = [*launch_argv(), "mcp", "--workspace", str(ws.resolve())]
        reqs = (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05",
                                       "capabilities": {},
                                       "clientInfo": {"name": "gui", "version": "0"}}})
                + "\n"
                + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        try:
            r = subprocess.run(argv, input=reqs, capture_output=True, text=True,
                               timeout=60, encoding="utf-8", errors="replace",
                               stdin=None, cwd=str(ws))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("MCP 自检失败", str(exc))
            return
        lines = [json.loads(x) for x in (r.stdout or "").splitlines() if x.strip()]
        tools = []
        for x in lines:
            if x.get("id") == 2:
                tools = [t["name"] for t in x.get("result", {}).get("tools", [])]
        if tools:
            self.log(f"\n[MCP 自检] 通过，暴露 {len(tools)} 个工具：{', '.join(tools)}", "ok")
            messagebox.showinfo("MCP 自检通过",
                                f"暴露 {len(tools)} 个工具：\n" + "\n".join(tools))
        else:
            err = (r.stderr or r.stdout or "").strip()[:600]
            self.log(f"\n[MCP 自检] 失败：{err}", "err")
            messagebox.showerror("MCP 自检失败", err or "没有收到工具列表")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="agentbus gui", description="图形启动器")
    ap.add_argument("--workspace", default=None, help="预填工作空间")
    args = ap.parse_args(argv)

    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("这个 Python 没带 tkinter，无法启动图形界面。\n"
              "可以改用命令行：python -m agentbus boss --workspace <目录> \"<目标>\"",
              file=sys.stderr)
        return 4

    _enable_dpi()
    root = tk.Tk()
    app = Launcher(root)
    if args.workspace:
        app.ws_var.set(str(Path(args.workspace).expanduser()))
        app.root.after(300, lambda: app._load_cfg(silent=True, monitor=False))
        app.root.after(400, app._detect)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
