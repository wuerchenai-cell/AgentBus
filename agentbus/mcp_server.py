#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.mcp_server —— 通过 MCP 暴露文件总线。

外部交互式会话可使用完整的 AgentBus 工具。由 AgentBus 持有的持续会话只接收
身份绑定的 agentbus_send 工具，用于平级互通；AgentBus 主循环负责收任务、
把任务送入持有的会话并在结果到达后继续协调。

传输：stdio + JSON-RPC 2.0，一行一条消息（MCP 标准做法）。
注意：stdout 只能输出协议消息，任何调试信息都必须走 stderr，否则协议会碎。
"""
from __future__ import annotations

import codecs
import copy
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .bus import Bus, BusError
from .config import ConfigError, load
from .director import DIRECTOR_INBOX, HUMAN_INBOX, Director
from .lease import LeaseError, LeaseManager
from . import claim_fencing as _fencing
from .operation_lock import task_lock
from .routing import resolve_message_result_to, resolve_result_to_arg

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "agentbus"
WAITABLE_WORK_TYPES = ("task", "goal", "review", "result")
MAX_WAIT_TASK_SECONDS = 120

TOOLS: list[dict[str, Any]] = [
    {
        'name': 'agentbus_verify',
        'description': '只读核验当前项目任务或作业的实际回执、清单与评审。task_id/job_id 二选一。',
        'inputSchema': {'type': 'object', 'properties': {'task_id': {'type': 'string'}, 'job_id': {'type': 'string'}}},
    },
    {
        'name': 'agentbus_timeline', 'description': '只读回放当前项目公共事件；不执行日志命令。',
        'inputSchema': {'type': 'object', 'properties': {'job_id': {'type': 'string'},
                        'format': {'type': 'string', 'enum': ['json', 'markdown', 'html']}}},
    },
    {
        'name': 'agentbus_attest', 'description': '只读生成当前项目任务的产物履历；缺证据保持未验证。',
        'inputSchema': {'type': 'object', 'properties': {'task_id': {'type': 'string'},
                        'format': {'type': 'string', 'enum': ['json', 'markdown']}}, 'required': ['task_id']},
    },
    {
        'name': 'agentbus_heartbeat', 'description': '出示领取时票据更新活动任务的显式心跳。',
        'inputSchema': {'type': 'object', 'properties': {'agent': {'type': 'string'},
                        'msg_id': {'type': 'string'}, 'ticket': {'type': 'object'},
                        'note': {'type': 'string'}}, 'required': ['agent', 'msg_id']},
    },
    {
        "name": "agentbus_next_task",
        "description": (
            "立即检查并原子认领一条属于你的任务（别人抢不走）。"
            "没有任务时立即返回；若希望低功耗等待文件事件，请调用 agentbus_wait_task。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string",
                          "description": "你的 agent 名，例如 trae / workbuddy / zcode"},
                "peek": {"type": "boolean",
                         "description": "只看不认领（默认 false，即认领）"},
            },
            "required": ["agent"],
        },
    },
    {
        "name": "agentbus_wait_task",
        "description": (
            "低功耗等活：在你的 ready 信箱等待 task/goal/review/result 文件事件，最长 120 秒；"
            "有工作或结果回执时原子认领并返回消息，没有新消息时返回超时。"
            "不会唤醒桌面会话；这是一次有界等待。完成任务并 agentbus_report 后，"
            "若仍需值守，可再次调用本工具。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string",
                          "description": "你的 agent 名，例如 codex / workbuddy / trae / zcode"},
                "timeout_s": {"type": "integer", "minimum": 1,
                              "maximum": MAX_WAIT_TASK_SECONDS, "default": MAX_WAIT_TASK_SECONDS,
                              "description": "最长等待秒数；MCP 调用会在此期间保持阻塞"},
            },
            "required": ["agent"],
        },
    },
    {
        "name": "agentbus_report",
        "description": (
            "任务完成或失败后回投结果。结果按原任务的 result_to 字段送达；"
            "旧任务仅在兼容期内可从正文解析，无法唯一确定时任务会退回 ready 等待人工澄清。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "msg_id": {"type": "string", "description": "任务消息 id"},
                "result": {"type": "string", "description": "结论 / 产物说明"},
                "failed": {"type": "boolean", "description": "没做成时置 true"},
                "artifacts": {"type": "array", "items": {"type": "object"}, "description": "产物 path/bytes/sha256 清单"},
                "ticket": {"type": "object", "description": "领取时返回的原始票据；同一连接已领取时可省略"},
                "tokens": {"type": "integer", "description": "本次任务实际消耗的 Token 数量"},
                "cost": {"type": "number", "description": "本次任务产生的预估或实际成本"},
            },
            "required": ["agent", "msg_id", "result"],
        },
    },
    {
        "name": "agentbus_status",
        "description": "总览：每个 agent 的待办/处理中/已完成数量、待决事项、活跃租约。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agentbus_escalations",
        "description": "查看需要人类拍板的事项（闸门拦截、重试耗尽等）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agentbus_send",
        "description": "给项目中的平级 agent 发消息（不要把整份文件塞进来）。新 task/goal/review 必须明确指定 result_to。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sender": {"type": "string"},
                "to": {"type": "string", "description": "目标 agent 名，或 director"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "type": {"type": "string",
                         "enum": ["goal", "task", "result", "info", "review", "escalation"]},
                "result_to": {"type": "string",
                              "description": "任务/目标/评审的结果收件人；可显式填 sender 或 director 别名"},
                "reply_to": {"type": "string", "description": "result 对应的原任务 ID"},
                "ticket": {"type": "object", "description": "result 的领取时票据；禁止从任务磁盘反补"},
                "artifacts": {"type": "array", "items": {"type": "object"},
                              "description": "result 产物清单：path/bytes/sha256；任务必交产物由程序核对"},
            },
            "required": ["sender", "to", "body"],
            "allOf": [{
                "if": {"properties": {"type": {"enum": ["goal", "task", "review"]}},
                       "required": ["type"]},
                "then": {"required": ["result_to"]},
            }, {
                "if": {"properties": {"type": {"const": "result"}},
                       "required": ["type"]},
                "then": {"required": ["reply_to"]},
            }],
        },
    },
    {
        "name": "agentbus_lease",
        "description": (
            "文件租约：改共享文件前先抢租约，避免和别的 agent 互相覆盖。"
            "action=acquire 抢（抢不到说明别人正在改，先做别的）；"
            "action=release 放；action=list 看当前都有谁占着什么。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["acquire", "release", "list"]},
                "path": {"type": "string", "description": "相对工作空间的路径"},
                "owner": {"type": "string"},
                "ttl_s": {"type": "integer", "description": "租约有效期，默认 900 秒"},
            },
            "required": ["action"],
        },
    },
]

# Source: four actual peer assessments, Codex integration, 2026-10-04.
TOOLS.extend([
    {'name': 'agentbus_file_read', 'description': '统一 UTF-8 分页读项目文件，返回原换行、摘要与 next_line；不静默截断。',
     'annotations': {'readOnlyHint': True, 'idempotentHint': True},
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'},
        'start_line': {'type': 'integer', 'minimum': 1}, 'max_lines': {'type': 'integer', 'minimum': 1},
        'expected_sha256': {'type': 'string'}}, 'required': ['path']}},
    {'name': 'agentbus_file_write', 'description': '受限工作区写入：自动租约、原摘要 CAS 与原子发布。首次创建摘要填 MISSING。dry_run 不写文件。',
     'annotations': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': False},
     'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'}, 'text': {'type': 'string'},
        'owner': {'type': 'string'}, 'expected_sha256': {'type': 'string'}, 'dry_run': {'type': 'boolean'}},
        'required': ['path', 'text', 'expected_sha256']}},
    {'name': 'agentbus_artifacts_verify', 'description': '核对结构化产物清单相对路径、字节数和 SHA256；不等于内容质量验收。',
     'annotations': {'readOnlyHint': True, 'idempotentHint': True},
     'inputSchema': {'type': 'object', 'properties': {'entries': {'type': 'array', 'items': {'type': 'object'}}}, 'required': ['entries']}},
    {'name': 'agentbus_plan_status', 'description': '读取总体计划、版本和待收到的评估；没有计划不伪称已采用。',
     'annotations': {'readOnlyHint': True, 'idempotentHint': True},
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'agentbus_claim_next', 'description': '自由调度：从任务树公共就绪池原子抢单认领下一个任务（支持按 tier 成本复杂度筛选，死锁时自动返回根阻断节点诊断）。',
     'inputSchema': {'type': 'object', 'properties': {'agent': {'type': 'string'}, 'tier': {'type': 'string', 'enum': ['critical', 'standard', 'low', 'free']}}, 'required': ['agent']}},
    {'name': 'agentbus_diagnose_deadlock', 'description': '只读诊断当前任务树是否存在等待死锁，并提取根阻断节点（Root Blocker）及下游影响数量。',
     'annotations': {'readOnlyHint': True, 'idempotentHint': True},
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'agentbus_pass_leaf', 'description': '管理员裁决通道：强制放行指定任务节点（标记为 delivered），解除下游依赖阻塞。',
     'inputSchema': {'type': 'object', 'properties': {'leaf_id': {'type': 'string'}, 'reason': {'type': 'string'}, 'admin': {'type': 'string'}}, 'required': ['leaf_id', 'reason']}},
    {'name': 'agentbus_reset_leaf', 'description': '重置前保留作业期限；在途任务须明确确认原执行者已经停止。',
     'inputSchema': {'type': 'object', 'properties': {'leaf_id': {'type': 'string'}, 'reason': {'type': 'string'}, 'admin': {'type': 'string'}, 'owner_confirmed_inactive': {'type': 'boolean'}, 'expected_task_id': {'type': 'string'}, 'expected_owner': {'type': 'string'}, 'expected_claim_epoch': {'type': 'integer'}}, 'required': ['leaf_id', 'reason']}},
])



def session_tools_for(identity: str) -> list[dict[str, Any]]:
    """Expose only peer messaging, with the sender identity fixed by the host."""
    tool = copy.deepcopy(next(item for item in TOOLS
                              if item["name"] == "agentbus_send"))
    schema = tool["inputSchema"]
    schema["properties"].pop("sender", None)
    schema["required"] = [name for name in schema.get("required", [])
                          if name != "sender"]
    tool["description"] = (
        f"以固定身份 {identity} 向项目中的平级 agent 发消息或提议任务。"
        "发送 task/goal/review 时必须明确填写 result_to。")
    return [tool]


class AgentBusMcp:
    def __init__(self, workspace: str | Path, *, identity: str | None = None,
                 session_tools: bool = False):
        self.cfg = load(workspace)
        self.identity = None
        if identity is not None:
            lookup = {name.casefold(): name for name in self.cfg.enabled_workers()}
            canonical = lookup.get(identity.strip().casefold())
            if canonical is None:
                raise ValueError(f"AgentBus 会话身份 {identity!r} 未在当前小组启用")
            self.identity = canonical
        if session_tools and self.identity is None:
            raise ValueError("session_tools 必须绑定一个已启用 agent 身份")
        self.session_tools = bool(session_tools)
        self.bus = Bus(self.cfg.workspace / ".agentbus")
        self.bus.init()
        self.leases = LeaseManager(self.bus.root)
        self._director: Director | None = None
        self._tickets = {}  # Only populated from this connection's successful claim.

    def file_read(self, path, start_line=1, max_lines=200, expected_sha256=None) -> str:
        from .workspace_files import WorkspaceFiles, WorkspaceFileError
        try:
            value = WorkspaceFiles(self.cfg.workspace).read_text(path, start_line=start_line,
                                    max_lines=max_lines, expected_sha256=expected_sha256)
        except WorkspaceFileError as exc:
            self.bus.audit(event='workspace_file_rejected', action='read', code=exc.code)
            raise
        return json.dumps(value, ensure_ascii=False)

    def file_write(self, path, text, owner=None, expected_sha256=None, dry_run=False) -> str:
        from .workspace_files import WorkspaceFiles, WorkspaceFileError
        if self.identity is not None and owner is not None and owner != self.identity:
            raise ValueError('file owner conflicts with the fixed connection identity')
        effective = self.identity or owner
        if effective not in self.cfg.enabled_workers():
            raise ValueError('file owner must be a selected project member')
        try:
            value = WorkspaceFiles(self.cfg.workspace).write_text(path, text, owner=effective,
                                            expected_sha256=expected_sha256, dry_run=dry_run)
        except WorkspaceFileError as exc:
            if not dry_run:
                self.bus.audit(event='workspace_file_rejected', action='write', code=exc.code)
            raise
        return json.dumps(value, ensure_ascii=False)

    def artifacts_verify(self, entries) -> str:
        from .workspace_files import WorkspaceFiles
        return json.dumps(WorkspaceFiles(self.cfg.workspace).verify_artifacts(entries), ensure_ascii=False)

    def plan_status(self) -> str:
        from .project_plan import plan_summary
        return json.dumps(plan_summary(self.cfg), ensure_ascii=False)

    def claim_next(self, agent: str, tier: str | None = None) -> str:
        from .task_tree import TaskForest
        forest = TaskForest(self.cfg)
        res = forest.claim_next(agent, tier=tier)
        msg = res.get("message")
        if msg is not None and res.get("status") == "claimed":
            ticket = _fencing.captured_ticket(msg)
            self._tickets[(agent, msg.id)] = ticket
            from . import heartbeat as _heartbeat
            _heartbeat.record(self.bus.root, msg.id, agent, ticket, task_record=msg.to_dict(),
                              activity="claim", note="MCP claim_next")
            out = {
                "status": res["status"],
                "detail": res["detail"],
                "task_id": msg.id,
                "leaf": res.get("leaf"),
                "ticket": ticket.to_dict(),
            }
            return json.dumps(out, ensure_ascii=False)
        if msg is not None:
            # An existing claim belongs to its original connection. Never
            # recover its ticket from disk or refresh activity on its behalf.
            return json.dumps({"status": res["status"], "detail": res["detail"],
                               "task_id": msg.id, "leaf": res.get("leaf")}, ensure_ascii=False)
        return json.dumps(res, ensure_ascii=False)

    def diagnose_deadlock(self) -> str:
        from .task_tree import TaskForest
        forest = TaskForest(self.cfg)
        return json.dumps(forest.diagnose_deadlock(), ensure_ascii=False)

    def pass_leaf(self, leaf_id: str, reason: str, admin: str = "admin") -> str:
        from .task_tree import TaskForest
        forest = TaskForest(self.cfg)
        leaf = forest.pass_leaf(leaf_id, reason, admin=admin or "admin")
        return json.dumps({"ok": True, "leaf": leaf}, ensure_ascii=False)

    def reset_leaf(self, leaf_id: str, reason: str, admin: str = "admin", owner_confirmed_inactive: bool = False, expected_task_id=None, expected_owner=None, expected_claim_epoch=None) -> str:
        from .task_tree import TaskForest
        forest = TaskForest(self.cfg)
        leaf = forest.reset_leaf(leaf_id, reason, admin=admin or "admin", owner_confirmed_inactive=owner_confirmed_inactive is True, expected_task_id=expected_task_id, expected_owner=expected_owner, expected_claim_epoch=expected_claim_epoch)
        return json.dumps({"ok": True, "leaf": leaf}, ensure_ascii=False)

    def discover_task(self, agent: str, title: str, goal: str = "", from_task_id: str | None = None,
                      expected_artifacts: list[str] | None = None) -> str:
        from .open_patterns import discover_subtask
        res = discover_subtask(self.cfg, agent=agent, title=title, goal=goal,
                               from_task_id=from_task_id, expected_artifacts=expected_artifacts)
        return json.dumps(res, ensure_ascii=False)

    def repomap(self, target_paths: list[str] | None = None, keywords: list[str] | None = None) -> str:
        from .open_patterns import build_repomap_slice
        res = build_repomap_slice(self.cfg.workspace, target_paths=target_paths, keywords=keywords)
        return json.dumps(res, ensure_ascii=False)

    def record_lesson(self, agent: str, lesson: str, task_id: str = "", scope_paths: list[str] | None = None) -> str:
        from .open_patterns import record_lesson as _rec_lesson
        res = _rec_lesson(self.cfg.workspace, agent=agent, lesson=lesson, task_id=task_id, scope_paths=scope_paths)
        return json.dumps(res, ensure_ascii=False)

    def verify(self, task_id=None, job_id=None) -> str:
        from .evidence_service import verify
        return json.dumps(verify(self.cfg, task_id=task_id, job_id=job_id), ensure_ascii=False)

    def timeline(self, job_id=None, format='json') -> str:
        from .evidence_service import execute
        return execute(self.cfg, 'timeline', job_id, format=format)

    def attest(self, task_id, format='json') -> str:
        from .evidence_service import execute
        return execute(self.cfg, 'attest', task_id, format=format)

    def heartbeat(self, agent, msg_id, ticket=None, note='') -> str:
        msg = self.bus._active_record(agent, msg_id)
        if msg is None:
            raise ValueError('找不到活动任务；心跳未写入')
        original = _fencing.normalize(ticket) if ticket is not None else self._tickets.get((agent, msg_id))
        value = self.bus.heartbeat(agent, msg, original, note=note)
        return json.dumps(value, ensure_ascii=False)

    def session_send(self, *, to: str, body: str, title: str = "",
                     type: str = "info", result_to: str | None = None,
                     reply_to: str | None = None, artifacts=None, ticket=None) -> str:
        if not self.session_tools or self.identity is None:
            raise ValueError("当前 MCP 连接未启用会话内平级消息工具")
        return self.send(self.identity, to, body, title, type,
                         result_to, reply_to, artifacts, ticket)

    @property
    def director(self) -> Director:
        if self._director is None:
            self._director = Director(self.cfg, bus=self.bus)
        return self._director

    # ------------------------------------------------------------------ #
    # 工具实现
    # ------------------------------------------------------------------ #
    def _find(self, agent: str, msg_id: str) -> Path | None:
        for sub in ("processing", "ready", "blocked"):
            p = self.bus.dirs(agent)[sub] / f"{msg_id}.json"
            if p.exists():
                return p
        return None

    def _reuse_existing_report(self, agent: str, msg, existing, ticket=None) -> str:
        msg.result = existing.body
        msg.exit_code = existing.exit_code
        final_status = "failed" if existing.exit_code not in (None, 0) else "done"
        self.bus.finish(agent, msg, final_status, ticket=ticket)
        self.director.resolve_pending_escalations(msg.id, existing.id)
        self.bus.audit(event="duplicate_report_reused", agent=agent,
                       source_task_id=msg.id, result_id=existing.id,
                       recipient=existing.to)
        self.director.schedule_peer_review(msg, agent, final_status)
        return (f"已复用既有回执 {existing.id} → {existing.to}；"
                "原任务已归档，没有重复投递。")

    def next_task(self, agent: str, peek: bool = False,
                  message_types: Iterable[str] | None = None) -> str:
        pending = self.bus.pending(agent, message_types=message_types)
        if not pending:
            busy = len(self.bus.processing(agent))
            if busy:
                return (f"暂无新任务（你有 {busy} 条正在处理中）。"
                        f"处理完记得用 agentbus_report 回投。")
            return ("暂无任务。需要等待时，请调用 agentbus_wait_task 阻塞等待文件事件；"
                    "不要重复调用 agentbus_next_task 轮询。")
        path = pending[0]
        if peek:
            m = self.bus.read(path)
        else:
            m = self.bus.claim(agent, path)
            if m is None:
                return "这条任务刚被别人领走了，请重新调用一次。"
            ticket = _fencing.captured_ticket(m)
            self._tickets[(agent, m.id)] = ticket
            from . import heartbeat as _heartbeat
            _heartbeat.record(self.bus.root, m.id, agent, ticket,
                              task_record=m.to_dict(), note="MCP claimed", activity="claim")
        if m.type == "result":
            if not peek:
                self.bus.finish(agent, m, "done", ticket=self._tickets[(agent, m.id)])
                source = self.bus.find_message(m.reply_to) if m.reply_to else None
                if source is not None and source.type in {"task", "goal"}:
                    source.result = m.body
                    outcome = "failed" if m.exit_code not in (None, 0) else "done"
                    self.director.schedule_peer_review(source, m.sender, outcome)
            archived = "；本回执已归档" if not peek else "；当前仅预览，尚未归档"
            return (f"收到来自平级参与者 {m.sender} 的结果回执 [{m.id}]，"
                    f"对应任务 {m.reply_to or '未关联'}：\n{m.body}\n\n"
                    f"请据此汇总或安排下一步{archived}。")
        if m.type not in {"task", "goal", "review"}:
            if not peek:
                self.bus.finish(agent, m, "done", ticket=self._tickets[(agent, m.id)])
            return (f"收到平级消息 [{m.id}] type={m.type} from={m.sender}：\n"
                    f"{m.body}\n（已在你的信箱归档。）")
        lines = [
            f"任务 [{m.id}]",
            f"标题: {m.title}",
            f"来源: {m.sender}（第 {m.hop} 次转手，第 {m.attempt} 次尝试）",
            f"结果收件人: {m.result_to or '旧消息：按限期正文规则解析；不明确时暂停询问'}",
            f"时间预算: {m.budget_s}s",
            "",
            "【正文】",
            m.work_brief,
            "",
            "【干完之后】调用 agentbus_report：",
            f'  agent="{agent}", msg_id="{m.id}", result="<做了什么 / 产物路径 / 未验证的部分>"',
            "若任务涉及改共享文件，动手前先 agentbus_lease(acquire)。",
        ]
        if not peek:
            lines.append('领取时 ticket=' + json.dumps(self._tickets[(agent, m.id)].to_dict(), ensure_ascii=False))
        if m.hop or m.attempt:
            lines.insert(3, "⚠ 这是重做/转手的任务，正文里通常写了上次没过审的原因。")
        return "\n".join(lines)

    def wait_task(self, agent: str, timeout_s: int = MAX_WAIT_TASK_SECONDS) -> str:
        """Wait for one actionable mailbox message, then atomically claim it."""
        if agent not in self.cfg.enabled_workers():
            raise ValueError(f"agentbus_wait_task: agent {agent!r} 未在当前小组启用")
        if (not isinstance(timeout_s, int) or isinstance(timeout_s, bool)
                or not 1 <= timeout_s <= MAX_WAIT_TASK_SECONDS):
            raise ValueError(
                f"timeout_s 必须是 1 到 {MAX_WAIT_TASK_SECONDS} 之间的整数")

        watchers = {agent: self.bus.watcher(agent)}
        try:
            available = self.bus.wait_for_work(
                [agent], timeout_s, watchers, message_types=WAITABLE_WORK_TYPES)
        finally:
            self.bus.close_watchers(watchers)
        if available is None:
            return f"等待超时（{timeout_s} 秒）：没有新的 task/goal/review/result。"

        response = self.next_task(agent, message_types=WAITABLE_WORK_TYPES)
        if response.startswith("暂无"):
            return "任务到达后已被其他消费者领取或移走；可再次等待。"
        return response

    def _participants(self) -> list[str]:
        return list(self.cfg.enabled_workers()) + [HUMAN_INBOX]

    def _registered_participants(self) -> list[str]:
        return list(self.cfg.data.get("workers", {})) + [HUMAN_INBOX]

    def report(self, agent: str, msg_id: str,
               result: str, failed: bool = False, artifacts=None, ticket=None,
               tokens: int | None = None, cost: float | None = None) -> str:
        from .cli import _validate_receipt_body
        _validate_receipt_body(result)
        lease = self.leases.acquire(
            f"agentbus/report/{msg_id}", agent, ttl_s=300,
            note="serialize result receipt and prevent duplicate posts",
            msg_id=msg_id)
        if lease is None:
            return (f"任务 {msg_id} 的回投正在由其他调用处理；本次没有创建回执，"
                    "请稍后重试。")
        try:
            with task_lock(self.bus.root, agent, msg_id):
                return self._report_locked(agent, msg_id, result, failed, artifacts, ticket, tokens=tokens, cost=cost)
        finally:
            self.leases.release(lease)

    def _report_locked(self, agent: str, msg_id: str,
                       result: str, failed: bool = False, artifacts=None, ticket=None,
                       tokens: int | None = None, cost: float | None = None) -> str:
        from .cli import _result_artifacts, _check_receipt_reuse
        path = self._find(agent, msg_id)
        if path is None:
            done = self.bus.dirs(agent)["done"] / f"{msg_id}.json"
            if done.exists():
                return f"这条任务（{msg_id}）之前已经回投过了，不用重复提交。"
            return f"在 {agent} 的信箱里找不到 {msg_id}，确认一下 id 是否正确。"
        m = self.bus.read(path)
        ticket = _fencing.normalize(ticket) if ticket is not None else self._tickets.get((agent, msg_id))
        if ticket is None:
            raise ValueError(
                '交卷必须提供认领时保存的原票据 ticket；当前连接没有领取过此任务。'
                '请传入原认领票据，或在当前连接领取自己的待办；不会从磁盘反补票据。')
        self.bus.check_claim(agent, m, ticket, evidence={'body': result, 'artifacts': artifacts or []})
        existing_results = self.bus.find_results(m.id, sender=agent)
        cited_results = [item for item in existing_results if item.id in result]
        if len(cited_results) == 1:
            existing = cited_results[0]
            expected_recipient = m.result_to
            if expected_recipient is None:
                cited_route = resolve_message_result_to(m, self._registered_participants())
                if cited_route.recipient is None:
                    reason = cited_route.error or cited_route.source
                    return self._block_report(agent, m, result, reason, failed, ticket)
                expected_recipient = cited_route.recipient
            if existing.to.casefold() != expected_recipient.casefold():
                return self._block_report(
                    agent, m, result,
                    f"引用的既有回执收件人 {existing.to} 与任务目标 "
                    f"{expected_recipient} 冲突", failed, ticket)
            _check_receipt_reuse(self.cfg, m, existing, artifacts, failed)
            return self._reuse_existing_report(agent, m, existing, ticket)
        if len(cited_results) > 1:
            return self._block_report(
                agent, m, result,
                "report cites multiple existing result IDs", failed, ticket)
        decision = resolve_message_result_to(m, self._participants())
        if decision.recipient is None:
            reason = decision.error or decision.source
            return self._block_report(agent, m, result, reason, failed, ticket)
        existing = next((item for item in existing_results
                         if item.to.casefold() == decision.recipient.casefold()
                         and (item.body == result or item.id in result)), None)
        if existing is not None:
            _check_receipt_reuse(self.cfg, m, existing, artifacts, failed)
            return self._reuse_existing_report(agent, m, existing, ticket)
        if existing_results:
            ids = ", ".join(item.id for item in existing_results)
            return self._block_report(
                agent, m, result,
                f"已有不同回执（{ids}），拒绝创建第二份", failed, ticket)
        entries = _result_artifacts(self.cfg, m, [] if artifacts is None else artifacts, failed=failed)
        self.bus.record_submission(agent, m, ticket, origin="MCP 回执",
                                   heartbeat_timeout_s=self.cfg.gates.get("heartbeat_timeout_s", 600))
        m.result = result
        m.exit_code = 1 if failed else 0
        reply = self.bus.make(
            sender=agent, to=decision.recipient, type="result",
            title=f"[{'失败' if failed else '人工/工具结果'}] {m.title}",
            body=result, reply_to=m.id, result_to=decision.recipient,
            routed_by_fallback=decision.source if decision.source != "field" else None,
            hop=m.hop, attempt=m.attempt, exit_code=1 if failed else 0,
            job_id=m.job_id, plan_id=m.plan_id, plan_sha256=m.plan_sha256, artifacts=entries)
        reply = self.bus.commit_result(agent, m, ticket, body=result, result_to=decision.recipient,
                                       artifacts=entries, exit_code=1 if failed else 0,
                                       artifact_root=self.cfg.output_root,
                                       routed_by_fallback=decision.source if decision.source != 'field' else None)
        if not reply.awaiting_audit:
            self.director.schedule_peer_review(m, agent, "failed" if failed else "done")
        if tokens is not None or cost is not None:
            try:
                from .task_tree import TaskForest
                forest = TaskForest(self.cfg)
                with task_lock(forest.root, "task-forest", "structure"):
                    tree_struct = forest._structure()
                    updated = False
                    for leaf in tree_struct.get("leaves", []):
                        if leaf.get("task_id") == m.id or leaf.get("id") == m.id:
                            if tokens is not None:
                                leaf["tokens_consumed"] = int(tokens)
                            if cost is not None:
                                leaf["cost"] = float(cost)
                            updated = True
                            break
                    if updated:
                        forest._write(forest._normalize(tree_struct))
            except (FileNotFoundError, PermissionError, ValueError, KeyError, RuntimeError):
                pass  # 资源清理类：Token/成本回写可选扩展失败不阻断正式交付回执
        if reply.awaiting_audit:
            return f'成果已提交待独立审核（{reply.id}），审核后才会送达 {decision.recipient}。'
        return (f"已回投给 {decision.recipient}（消息 {reply.id}）。"
                "若当前会话需要继续值守，请再调 agentbus_wait_task 等待任务或结果；"
                "只需检查一次时可调 agentbus_next_task。")

    def _block_report(self, agent: str, msg, result: str,
                      reason: str, failed: bool = False, ticket=None) -> str:
        msg.result = result
        msg.exit_code = 1 if failed else 0
        if ticket is None:
            ticket = self._tickets.get((agent, msg.id))
        self.bus.block(agent, msg, f"result route requires clarification: {reason}", ticket=ticket)
        try:
            escalation = self.director.escalate_for_human(
                msg, f"结果路由需要澄清：{reason}")
        except Exception as exc:  # noqa: BLE001
            self.bus.audit(event="escalation_failed", id=msg.id, reason=str(exc))
            return (f"结果尚未投递：{reason}。任务已阻塞并保留结果；"
                    f"人工升级投递失败：{exc}。没有默认投给任何 agent。")
        self.director.schedule_peer_review(msg, agent, "blocked")
        return (f"结果尚未投递：{reason}。任务已移入 blocked/ 并升级给人 "
                f"[{escalation.id}]，结果已保留；没有默认投给任何 agent。")

    def status(self) -> str:
        lines = [f"工作空间: {self.cfg.workspace}",
                 f"协调者  : {self.cfg.director}（参与者平级）", ""]
        lines.append(f"{'agent':<14}{'待办':>6}{'处理中':>8}{'阻塞':>6}{'已完成':>8}  角色")
        for name, s in self.bus.stats().items():
            w = self.cfg.data["workers"].get(name, {})
            if name == DIRECTOR_INBOX:
                role = "主体收件箱"
            elif name == HUMAN_INBOX:
                role = "人工收件箱"
            elif not w:
                role = "已注册但未在配置中"
            else:
                role = "可无头拉起" if w.get("can_spawn") else "需人工接手"
            lines.append(f"{name:<14}{s['ready']:>6}{s['processing']:>8}"
                         f"{s['blocked']:>6}{s['done']:>8}  {role}")
        esc = self.director.escalations()
        if esc:
            lines += ["", f"⚠ 有 {len(esc)} 条需要人决定："]
            for m in esc:
                lines.append(f"  · [{m.id}] {m.title}")
        act = self.leases.active()
        if act:
            now = time.time()
            lines += ["", f"活跃租约 {len(act)} 个："]
            for lz in act:
                lines.append(f"  · {lz['rel']} ← {lz['owner']}"
                             f"（剩 {lz['expires_at'] - now:.0f}s）")
        return "\n".join(lines)

    def escalations(self) -> str:
        esc = self.director.escalations()
        if not esc:
            return "没有需要人决定的事项。"
        out = []
        for m in esc:
            out.append(f"[{m.id}] {m.title}\n原因/正文：\n{m.body}\n")
        return "\n".join(out)

    def send(self, sender: str, to: str, body: str, title: str = "",
             type: str = "info", result_to: str | None = None,
                     reply_to: str | None = None, artifacts=None, ticket=None) -> str:
        lease = None
        if type == "result":
            if not reply_to:
                raise ValueError("新 result 消息必须显式提供 reply_to")
            from .cli import _validate_receipt_body
            _validate_receipt_body(body)
            owner = (self.cfg.director if sender.strip().casefold() == "director"
                     else sender.strip())
            lease = self.leases.acquire(
                f"agentbus/report/{reply_to}", owner, ttl_s=300,
                note="serialize direct result sends", msg_id=reply_to)
            if lease is None:
                return (f"任务 {reply_to} 的回投正在由其他调用处理；"
                        "本次没有创建回执，请稍后重试。")
        try:
            if type == 'result' and reply_to:
                with task_lock(self.bus.root, sender, reply_to):
                    return self._send_locked(sender, to, body, title, type, result_to, reply_to, artifacts, ticket)
            return self._send_locked(sender, to, body, title, type, result_to, reply_to, artifacts, ticket)
        finally:
            if lease is not None:
                self.leases.release(lease)

    def _send_locked(self, sender: str, to: str, body: str, title: str = "",
                     type: str = "info", result_to: str | None = None,
                     reply_to: str | None = None, artifacts=None, ticket=None) -> str:
        from .cli import _result_artifacts, _check_receipt_reuse
        if not to.strip():
            raise ValueError("to must not be empty")
        if sender == "director":
            sender = self.cfg.director
        if to == "director":
            to = self.cfg.director
        lookup = {name.casefold(): name for name in self._participants()}
        sender = lookup.get(sender.strip().casefold())
        to = lookup.get(to.strip().casefold())
        if sender is None:
            raise ValueError("unknown or disabled sender")
        if to is None:
            raise ValueError("unknown or disabled recipient")
        if type not in ("goal", "task", "result", "info", "review", "escalation"):
            type = "info"
        executor_required = (type in ("task", "review")
                             or (type == "goal" and to != self.cfg.director))
        if (executor_required and to != HUMAN_INBOX
                and not self.cfg.worker(to).get("executor_enabled", True)):
            raise ValueError(f"收件人 {to} 未启用执行，不能接收 {type}")
        resolved_result_to = None
        if type in ("goal", "task", "review"):
            if result_to is None or not result_to.strip():
                raise ValueError(f"新 {type} 消息必须显式提供 result_to")
            resolved_result_to = resolve_result_to_arg(
                result_to, sender=sender, director=self.cfg.director,
                participants=self._participants())
        if type == "result" and not reply_to:
            raise ValueError("新 result 消息必须显式提供 reply_to")
        result_source = None
        result_route = None
        if type == "result":
            try:
                result_source = self.bus.validate_result_reference(reply_to or "")
                ticket = (_fencing.normalize(ticket) if ticket is not None
                          else self._tickets.get((sender, reply_to)))
                self.bus.check_claim(sender, result_source, ticket, evidence={'body': body, 'artifacts': artifacts or []})
                result_route = resolve_message_result_to(
                    result_source, [*self._participants()])
                if result_route.recipient is None:
                    raise ValueError(
                        f"原任务没有可解析的结果收件人，请先澄清：{result_route.error}")
            except BusError as exc:
                raise ValueError(str(exc)) from exc
            existing_results = self.bus.find_results(reply_to or "", sender=sender)
            cited = [item for item in existing_results if item.id in body]
            if len(cited) > 1:
                raise ValueError("result body cites multiple existing result IDs")
            if cited:
                existing = cited[0]
                if existing.to.casefold() != to.casefold():
                    raise ValueError(
                        f"cited result {existing.id} was already delivered to {existing.to}; "
                        "refusing a second delivery")
                _check_receipt_reuse(self.cfg, result_source, existing, artifacts)
                if result_source.status not in ('done', 'failed'):
                    raise ValueError(f'回执 {existing.id} 已存在，原任务仍未结束；请用 agentbus_report 核对并引用该回执。')
                return f"已复用既有回执 {existing.id} → {existing.to}；没有重复投递。"
            same = next((item for item in existing_results
                         if item.to.casefold() == to.casefold()
                         and item.body == body), None)
            if same is not None:
                _check_receipt_reuse(self.cfg, result_source, same, artifacts)
                if result_source.status not in ('done', 'failed'):
                    raise ValueError(f'回执 {same.id} 已存在，原任务仍未结束；请用 agentbus_report 核对并引用该回执。')
                return f"已复用既有回执 {same.id} → {same.to}；没有重复投递。"
            if existing_results:
                ids = ", ".join(item.id for item in existing_results)
                raise ValueError(
                    f"该任务已存在回执 {ids}；拒绝重复投递，请用 agentbus_report 明确复用。")
        entries = (_result_artifacts(self.cfg, result_source, [] if artifacts is None else artifacts)
                   if type == 'result' else [])
        m = self.bus.make(sender=sender, to=to, type=type,
                          title=title or body[:60], body=body,
                          result_to=(resolved_result_to if type != "result"
                                     else result_route.recipient if result_route else None),
                          reply_to=reply_to,
                          job_id=result_source.job_id if result_source else None,
                          plan_id=result_source.plan_id if result_source else None,
                          plan_sha256=result_source.plan_sha256 if result_source else None,
                          artifacts=entries,
                          routed_by_fallback=("body" if result_route is not None
                                              and result_route.source == "body" else None))
        try:
            if type == 'result':
                m = self.bus.commit_result(
                    sender, result_source, ticket, body=body, result_to=to,
                    artifacts=entries, artifact_root=self.cfg.output_root, exit_code=0,
                    routed_by_fallback=('body' if result_route.source == 'body' else None))
            else:
                self.bus.post(m)
        except BusError as exc:
            raise ValueError(str(exc)) from exc
        if m.awaiting_audit:
            return f'成果已提交待独立审核（{m.id}），审核后才会送达 {to}。'
        review_note = ""
        if type == "result":
            source = result_source
            assert source is not None and result_route is not None
            if (source.result_to is None and result_route.source == "body"
                    and result_route.recipient != m.to):
                self.bus.audit(event="route_mismatch", result_id=m.id,
                               source_task_id=source.id,
                               declared=result_route.recipient, actual=m.to,
                               routed_by_fallback="body")
            if (source.type in {"task", "goal"}
                    and result_route.recipient == m.to):
                source.result = body
                review = self.director.schedule_peer_review(source, sender, "done")
                if review is not None:
                    review_note = f"；已排入同伴评审 {review.id} → {review.to}"
        return f"已投递 {m.id} → {to}（类型 {type}）{review_note}"

    def lease(self, action: str, path: str = "", owner: str = "",
              ttl_s: int = 900) -> str:
        if action == "list":
            act = self.leases.active()
            if not act:
                return "当前没有任何活跃租约。"
            return "\n".join(f"· {x['rel']} ← {x['owner']}" for x in act)
        if not path or not owner:
            return "acquire / release 都需要 path 和 owner。"
        if action == "acquire":
            lz = self.leases.acquire(path, owner, ttl_s=ttl_s)
            if lz is None:
                cur = self.leases.holder(path) or {}
                return (f"抢不到：{path} 正被 {cur.get('owner', '未知')} 占着。"
                        f"先做别的部分，别硬改。")
            return f"租约到手：{lz.rel}，有效期 {ttl_s}s。改完记得 release。"
        if action == "release":
            cur = self.leases.read(path)
            if cur is None:
                return f"{path} 上没有租约。"
            if cur.owner != owner:
                return f"{path} 的租约属于 {cur.owner}，你不能释放。"
            self.leases.release(cur)
            return f"已释放 {path} 的租约。"
        return f"未知 action: {action}"


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP 协议
# --------------------------------------------------------------------------- #


def _ok(rid: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text(rid: Any, text: str, is_error: bool = False) -> dict[str, Any]:
    return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": is_error})


def _ensure_utf8(stream: Any) -> None:
    """MCP stdio is UTF-8 even when the Windows console code page is legacy GBK."""
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    encoding = getattr(stream, "encoding", None)
    try:
        if encoding and codecs.lookup(encoding).name == "utf-8":
            return
    except LookupError:
        pass  # 资源清理类：会话查询未命中，按约定静默返回。
    reconfigure(encoding="utf-8")


def serve(workspace: str | Path, stdin=None, stdout=None, *,
          identity: str | None = None, session_tools: bool = False) -> int:
    """跑 stdio 版 MCP 服务器。stdout 只允许出协议消息。"""
    if stdin is None:
        inp = sys.stdin
        _ensure_utf8(inp)
    else:
        inp = stdin
    if stdout is None:
        out = sys.stdout
        _ensure_utf8(out)
    else:
        out = stdout
    log = sys.stderr

    try:
        app = AgentBusMcp(workspace, identity=identity,
                          session_tools=session_tools)
    except (ConfigError, ValueError) as exc:
        print(f"[agentbus-mcp] 配置错误：{exc}", file=log)
        return 2

    def dispatch(method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            visible_tools = (session_tools_for(app.identity)
                             if app.session_tools and app.identity else TOOLS)
            return {"tools": visible_tools}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if app.session_tools:
                if name != "agentbus_send":
                    raise ValueError(f"会话内仅开放平级消息工具，不支持 {name!r}")
                response = app.session_send(
                    to=str(args.get("to", "")), body=str(args.get("body", "")),
                    title=str(args.get("title", "")),
                    type=str(args.get("type", "info")),
                    result_to=(str(args["result_to"])
                               if args.get("result_to") is not None else None),
                    reply_to=(str(args["reply_to"])
                              if args.get("reply_to") is not None else None),
                    artifacts=args.get('artifacts'),
                    ticket=args.get('ticket'),
                )
                return {"content": [{"type": "text", "text": response}],
                        "isError": False}
            fn: Callable[..., str] | None = {
                'agentbus_verify': lambda a: app.verify(a.get('task_id'), a.get('job_id')),
                'agentbus_timeline': lambda a: app.timeline(a.get('job_id'), a.get('format', 'json')),
                'agentbus_attest': lambda a: app.attest(a.get('task_id'), a.get('format', 'json')),
                'agentbus_heartbeat': lambda a: app.heartbeat(a.get('agent'), a.get('msg_id'), a.get('ticket'), a.get('note', '')),
                "agentbus_next_task": lambda a: app.next_task(
                    str(a.get("agent", "")), bool(a.get("peek", False))),
                "agentbus_wait_task": lambda a: app.wait_task(
                    str(a.get("agent", "")), a.get("timeout_s", MAX_WAIT_TASK_SECONDS)),
                "agentbus_report": lambda a: app.report(
                    str(a.get("agent", "")), str(a.get("msg_id", "")),
                    str(a.get("result", "")), bool(a.get("failed", False)), a.get("artifacts"), a.get('ticket'),
                    tokens=a.get("tokens"), cost=a.get("cost")),
                "agentbus_file_read": lambda a: app.file_read(a.get('path'), a.get('start_line', 1), a.get('max_lines', 200), a.get('expected_sha256')),
                "agentbus_file_write": lambda a: app.file_write(a.get('path'), a.get('text'), a.get('owner'), a.get('expected_sha256'), a.get('dry_run', False)),
                "agentbus_artifacts_verify": lambda a: app.artifacts_verify(a.get('entries')),
                "agentbus_plan_status": lambda a: app.plan_status(),
                "agentbus_claim_next": lambda a: app.claim_next(str(a.get("agent", "")), a.get("tier")),
                "agentbus_diagnose_deadlock": lambda _a: app.diagnose_deadlock(),
                "agentbus_pass_leaf": lambda a: app.pass_leaf(str(a.get("leaf_id", "")), str(a.get("reason", "")), str(a.get("admin", "admin"))),
                "agentbus_reset_leaf": lambda a: app.reset_leaf(str(a.get("leaf_id", "")), str(a.get("reason", "")), str(a.get("admin", "admin")), a.get("owner_confirmed_inactive") is True, a.get("expected_task_id"), a.get("expected_owner"), a.get("expected_claim_epoch")),
                "agentbus_status": lambda a: app.status(),
                "agentbus_escalations": lambda a: app.escalations(),
                "agentbus_send": lambda a: app.send(
                    str(a.get("sender", "unknown")), str(a.get("to", "")),
                    str(a.get("body", "")), str(a.get("title", "")),
                    str(a.get("type", "info")),
                    str(a["result_to"]) if a.get("result_to") is not None else None,
                    str(a["reply_to"]) if a.get("reply_to") is not None else None, a.get('artifacts'), a.get('ticket')),
                "agentbus_lease": lambda a: app.lease(
                    str(a.get("action", "list")), str(a.get("path", "")),
                    str(a.get("owner", "")), int(a.get("ttl_s", 900))),
            }.get(name)
            if fn is None:
                raise ValueError(f"未知工具 {name!r}")
            return {"content": [{"type": "text", "text": fn(args)}], "isError": False}
        if method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            return {key: []}
        raise ValueError(f"不支持的方法 {method!r}")

    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(f"[agentbus-mcp] 收到非 JSON 行，已忽略", file=log)
            continue

        rid = req.get("id")
        method = str(req.get("method", ""))
        params = req.get("params") or {}
        is_notification = rid is None

        try:
            result = dispatch(method, params)
        except LeaseError as exc:
            if not is_notification:
                out.write(json.dumps(_text(rid, str(exc), True), ensure_ascii=False) + "\n")
                out.flush()
            continue
        except ValueError as exc:
            print(f"[agentbus-mcp] {method} 失败：{exc}", file=log)
            if not is_notification:
                if method == "tools/call":
                    out.write(json.dumps(_text(rid, f"工具执行失败：{exc}", True),
                                         ensure_ascii=False) + "\n")
                else:
                    out.write(json.dumps(_err(rid, -32603, str(exc)),
                                         ensure_ascii=False) + "\n")
                out.flush()
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[agentbus-mcp] {method} 失败：\n{traceback.format_exc()}", file=log)
            if not is_notification:
                if method == "tools/call":
                    out.write(json.dumps(_text(rid, f"工具执行失败：{exc}", True),
                                         ensure_ascii=False) + "\n")
                else:
                    out.write(json.dumps(_err(rid, -32603, str(exc)),
                                         ensure_ascii=False) + "\n")
                out.flush()
            continue

        if is_notification or result is None:
            continue
        out.write(json.dumps(_ok(rid, result), ensure_ascii=False) + "\n")
        out.flush()

    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="agentbus mcp", description="以 MCP 服务器方式暴露 agentbus（stdio）")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--identity", default=None,
                    help="把 MCP 发送者固定到该已启用 agent")
    ap.add_argument("--session-tools", action="store_true",
                    help="仅向此 Agent 会话暴露身份固定的平级消息工具")
    args = ap.parse_args(argv)
    return serve(args.workspace, identity=args.identity,
                 session_tools=args.session_tools)
