# -*- coding: utf-8 -*-
"""contacts.py: 邮箱联系卡、登记邀请、CAS版本管理与历史快照。"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from contextlib import suppress
import uuid
import heapq
from pathlib import Path
from typing import List, Optional, Union
from functools import wraps
from itertools import islice
from agentbus.operation_lock import task_lock

from agentbus.trigger.contracts import (
    Contact,
    RegistrationInvite,
    TargetObservation,
    TriggerContractError,
    TriggerErrorCode,
    normalize_project_key,
    validate_member_id,
    validate_native_id,
)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _contact_lock(method):
    @wraps(method)
    def wrapped(workspace, identity=None, *args, **kwargs):
        if identity is None:
            identity = kwargs.pop('invite_id' if method.__name__ == 'confirm_contact' else 'member_id')
        member = identity
        if method.__name__ == 'confirm_contact':
            invite = get_invite(workspace, identity)
            if invite is None:
                raise TriggerContractError('Invite not found', TriggerErrorCode.INVITE_EXPIRED)
            member = invite.member_id
        validate_member_id(member)
        with task_lock(Path(workspace) / '.agentbus', 'trigger-member', member), \
                task_lock(Path(workspace) / '.agentbus', 'trigger-contact', member):
            return method(workspace, identity, *args, **kwargs)
    return wrapped


def _bounded_json(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError('Contact briefing record exceeds size limit')
    value = json.loads(raw.decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Contact briefing record must be an object')
    return value


def _completion_times(workspace, member_id):
    path = Path(workspace) / '.agentbus/events.jsonl'
    if not path.is_file():
        return {}
    with path.open('rb') as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - 524288)
        stream.seek(start)
        if start:
            stream.readline()
        lines = stream.read(524288).splitlines()
    result = {}
    for line in lines:
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            if event.get('event') == 'done' and event.get('agent') == member_id:
                result[event.get('id')] = event.get('ts', '')
        except (ValueError, TypeError):
            continue
    return result


def _atomic_write_json(target_path: Path, data: dict) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(target_path.parent),
        prefix=f".{target_path.stem}_",
        suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, str(target_path))
    finally:
        # Preserve the primary write error if Windows still holds the temp file.
        with suppress(OSError):
            os.remove(tmp_path)


def get_invites_dir(workspace: Union[str, Path]) -> Path:
    return Path(workspace) / ".agentbus" / "triggers" / "invitations"


def get_member_agent_dir(workspace: Union[str, Path], member_id: str) -> Path:
    validate_member_id(member_id)
    return Path(workspace) / ".agentbus" / "agents" / member_id


def create_invite(
    workspace: Union[str, Path],
    member_id: str,
    connector_id: str,
    installation_key: str,
    *,
    ttl_s: int = 1200,
    expected_revision: int = 0,
) -> RegistrationInvite:
    """创建一次性登记邀请（默认有效期 20 分钟）。"""
    norm_project = normalize_project_key(workspace)
    validate_member_id(member_id)

    invite_id = f"inv_{uuid.uuid4().hex[:16]}"
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = now + datetime.timedelta(seconds=ttl_s)

    invite = RegistrationInvite(
        schema_version=1,
        invite_id=invite_id,
        project_key=norm_project,
        member_id=member_id,
        connector_id=connector_id,
        installation_key=installation_key,
        protocol_version="agentbus-trigger/1",
        expected_revision=expected_revision,
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
        consumed=False,
        consumed_at=None,
    )

    invite_path = get_invites_dir(workspace) / f"{invite_id}.json"
    _atomic_write_json(invite_path, invite.to_dict())
    return invite


def get_invite(workspace: Union[str, Path], invite_id: str) -> Optional[RegistrationInvite]:
    """读取指定邀请，不存在或损坏返回 None。"""
    invite_path = get_invites_dir(workspace) / f"{invite_id}.json"
    if not invite_path.is_file():
        return None
    try:
        data = json.loads(invite_path.read_text(encoding="utf-8"))
        return RegistrationInvite.from_dict(data)
    except (ValueError, KeyError, TypeError):
        return None


def load_contact(workspace: Union[str, Path], member_id: str) -> Optional[Contact]:
    """读取成员项目联系卡。"""
    contact_path = get_member_agent_dir(workspace, member_id) / "contact.json"
    if not contact_path.is_file():
        return None
    try:
        data = json.loads(contact_path.read_text(encoding="utf-8"))
        return Contact.from_dict(data)
    except (ValueError, KeyError, TypeError):
        return None


@_contact_lock
def confirm_contact(
    workspace: Union[str, Path],
    invite_id: str,
    observation: Union[TargetObservation, dict],
    *,
    connector_version: str = "1.0.0",
    execution_surface: str = "desktop_existing",
) -> Contact:
    """确认登记邀请并原子提交联系卡（带 CAS 冲突检查与快照归档）。"""
    norm_project = normalize_project_key(workspace)
    invite = get_invite(workspace, invite_id)
    if invite is None:
        raise TriggerContractError(f"Invite {invite_id} not found", TriggerErrorCode.INVITE_EXPIRED)

    if isinstance(observation, dict):
        obs = TargetObservation.from_dict(observation)
    elif isinstance(observation, TargetObservation):
        obs = observation
    else:
        raise TriggerContractError("Observation must be TargetObservation or dict")

    if invite.project_key != norm_project:
        raise TriggerContractError(
            f"Workspace mismatch: invite is for {invite.project_key}, requested {norm_project}",
            TriggerErrorCode.PROJECT_MISMATCH
        )

    if obs.project_key and normalize_project_key(obs.project_key) != norm_project:
        raise TriggerContractError(
            f"Observation workspace mismatch: observation is for {obs.project_key}, requested {norm_project}",
            TriggerErrorCode.PROJECT_MISMATCH
        )

    if obs.exists and (obs.connector_id != invite.connector_id or obs.installation_key != invite.installation_key
            or obs.status != 'ready'):
        raise TriggerContractError('Observation does not match invited connector/installation', TriggerErrorCode.TARGET_CHANGED)

    # Check expiration
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        expires_at = datetime.datetime.fromisoformat(invite.expires_at)
        if now > expires_at:
            raise TriggerContractError(f"Invite {invite_id} has expired", TriggerErrorCode.INVITE_EXPIRED)
    except (ValueError, TypeError):
        raise TriggerContractError(f"Invalid invite expiration format", TriggerErrorCode.INVITE_EXPIRED)

    if not obs.exists:
        raise TriggerContractError(
            f"Target conversation {obs.native_conversation_id} existence not verified",
            TriggerErrorCode.TARGET_UNKNOWN
        )

    validate_native_id(obs.native_conversation_id)

    # Check existing contact
    existing = load_contact(workspace, invite.member_id)

    # Check if this invite was already consumed
    if invite.consumed:
        if (
            existing is not None
            and existing.native_conversation_id == obs.native_conversation_id
            and existing.installation_key == invite.installation_key
        ):
            # Idempotent return
            return existing
        raise TriggerContractError(f"Invite {invite_id} has already been consumed", TriggerErrorCode.INVITE_EXPIRED)

    current_rev = existing.revision if existing is not None else 0
    if invite.expected_revision != current_rev:
        raise TriggerContractError(
            f"CAS revision conflict: expected revision {invite.expected_revision}, but current contact is revision {current_rev}",
            TriggerErrorCode.VERSION_CHANGED
        )

    # Archive old snapshot if existing
    agent_dir = get_member_agent_dir(workspace, invite.member_id)
    if existing is not None:
        history_dir = agent_dir / "contact-history"
        snapshot_name = f"{existing.binding_id}-{existing.revision}.json"
        _atomic_write_json(history_dir / snapshot_name, existing.to_dict())
        new_rev = current_rev + 1
    else:
        new_rev = 1

    new_binding_id = f"bnd_{uuid.uuid4().hex[:16]}"
    now_iso = now.isoformat()

    new_contact = Contact(
        schema_version=1,
        project_key=norm_project,
        member_id=invite.member_id,
        binding_id=new_binding_id,
        revision=new_rev,
        status="bound",
        connector_id=invite.connector_id,
        connector_version=connector_version,
        installation_key=invite.installation_key,
        execution_surface=execution_surface,
        native_conversation_id=obs.native_conversation_id,
        native_title=obs.human_message,
        identity_source=obs.identity_source,
        bound_at=now_iso,
        updated_at=now_iso,
        capability_evidence_ref=obs.evidence_ref,
        reason_code=None,
    )

    contact_path = agent_dir / "contact.json"
    _atomic_write_json(contact_path, new_contact.to_dict())

    # Mark invite consumed
    invite.consumed = True
    invite.consumed_at = now_iso
    invite_path = get_invites_dir(workspace) / f"{invite.invite_id}.json"
    _atomic_write_json(invite_path, invite.to_dict())

    return new_contact


@_contact_lock
def retire_contact(
    workspace: Union[str, Path],
    member_id: str,
    expected_revision: int,
    *,
    reason: str = "retired",
) -> Contact:
    """退役现有联系卡（CAS 保护、版本递增并归档历史快照）。"""
    existing = load_contact(workspace, member_id)
    if existing is None:
        raise TriggerContractError(f"Contact for member {member_id} not found", TriggerErrorCode.TARGET_MISSING)

    if existing.revision != expected_revision:
        raise TriggerContractError(
            f"CAS revision conflict: expected revision {expected_revision}, current revision is {existing.revision}",
            TriggerErrorCode.VERSION_CHANGED
        )

    agent_dir = get_member_agent_dir(workspace, member_id)
    history_dir = agent_dir / "contact-history"
    snapshot_name = f"{existing.binding_id}-{existing.revision}.json"
    _atomic_write_json(history_dir / snapshot_name, existing.to_dict())

    now_iso = _now_iso()
    existing.status = "retired"
    existing.revision += 1
    existing.reason_code = reason
    existing.updated_at = now_iso

    _atomic_write_json(agent_dir / "contact.json", existing.to_dict())
    return existing


@_contact_lock
def prepare_rebind(
    workspace: Union[str, Path],
    member_id: str,
    expected_revision: int,
    connector_id: str,
    installation_key: str,
    *,
    ttl_s: int = 1200,
) -> RegistrationInvite:
    """准备换绑邀请（严格核验当前版本以防竞态覆盖）。"""
    existing = load_contact(workspace, member_id)
    current_rev = existing.revision if existing is not None else 0
    if current_rev != expected_revision:
        raise TriggerContractError(
            f"CAS revision conflict: expected revision {expected_revision}, current revision is {current_rev}",
            TriggerErrorCode.VERSION_CHANGED
        )

    return create_invite(
        workspace,
        member_id=member_id,
        connector_id=connector_id,
        installation_key=installation_key,
        ttl_s=ttl_s,
        expected_revision=expected_revision,
    )


def list_contacts(workspace: Union[str, Path]) -> List[Contact]:
    """列出项目下所有已登记的成员联系卡。"""
    ws = Path(workspace)
    agents_dir = ws / ".agentbus" / "agents"
    if not agents_dir.is_dir():
        return []
    results = []
    for d in sorted(agents_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            c = load_contact(ws, d.name)
            if c is not None:
                results.append(c)
    return results


@_contact_lock
def checkin_contact(
    workspace: Union[str, Path],
    member_id: str,
    *,
    session_id: Optional[str] = None,
    connector_id: Optional[str] = None,
    installation_key: Optional[str] = None,
    capabilities: Optional[List[str]] = None,
    notify_coordinator: bool = True,
    expected_revision: Optional[int] = None,
) -> dict:
    """Agent 自主向组长报到与动态换绑（免繁琐 invite，支持自动探测与平滑换绑 + 上下文压缩简报）。"""
    import hashlib
    import time
    from agentbus.bus import Message

    norm_project = normalize_project_key(workspace)
    validate_member_id(member_id)
    ws = Path(workspace)

    # 1. 智能探测会话 ID
    detected_session = session_id
    if not detected_session:
        host_env = ('CODEARTS_SESSION_ID',) if (connector_id == 'agentbus.codearts-hook' or 'codearts' in member_id.lower()) else (
            ('CODEX_THREAD_ID',) if (connector_id == 'agentbus.codex-bridge' or 'codex' in member_id.lower()) else ())
        for env_var in ('AGENTBUS_SESSION_ID',) + host_env:
            val = os.environ.get(env_var)
            if val and val.strip():
                detected_session = val.strip()
                break

    if not detected_session and (connector_id == 'agentbus.codearts-hook' or 'codearts' in member_id.lower()):
        ready_evidence = ws / ".agentbus" / "triggers" / "bridge" / "codearts" / "evidence" / "READY.json"
        if ready_evidence.is_file():
            try:
                ev_data = _bounded_json(ready_evidence)
                cand = ev_data.get("last_active_session_id") or ev_data.get("session_id")
                observed = datetime.datetime.fromisoformat(ev_data.get('at', ''))
                age = (datetime.datetime.now(datetime.timezone.utc) - observed).total_seconds()
                if (cand and isinstance(cand, str) and cand.strip() and 0 <= age <= 300
                        and normalize_project_key(ev_data.get('workspace', '')) == norm_project
                        and ev_data.get('client_get') is True and ev_data.get('client_prompt') is True):
                    detected_session = cand.strip()
            except (ValueError, KeyError, TypeError):
                pass  # 容错探测类：本地会话文件读取异常回退

    if not detected_session:
        raise TriggerContractError('未找到宿主的真实会话编号。你可以继续文件协作；精确唤醒需从宿主取得当前聊天的真实编号，或使用已激活的 CodeArts Hook 证据，不能自行编造编号。', TriggerErrorCode.TARGET_UNKNOWN)

    validate_native_id(detected_session)

    # 2. 推断连接器与安装标识
    existing = load_contact(ws, member_id)
    if expected_revision is not None and expected_revision != (existing.revision if existing else 0):
        raise TriggerContractError('Contact revision changed', TriggerErrorCode.VERSION_CHANGED)
    target_connector = connector_id
    if not target_connector:
        if existing is not None and existing.connector_id:
            target_connector = existing.connector_id
        else:
            m_lower = member_id.lower()
            w_comm = ""
            cfg_file = ws / ".agentbus" / "config.json"
            if cfg_file.is_file():
                try:
                    c_obj = json.loads(cfg_file.read_text(encoding="utf-8"))
                    w_info = (c_obj.get("workers") or {}).get(member_id) or {}
                    w_comm = str(w_info.get("communication_mode") or w_info.get("adapter") or "").lower()
                except (ValueError, KeyError, TypeError):
                    pass  # 容错探测类：读取成员通信模式回退名称推断

            if w_comm == "acp" or any(k in m_lower for k in ("acp", "opencode", "gemini", "cline", "qwen")):
                target_connector = "agentbus.acp-bridge"
            elif "codex" in m_lower:
                target_connector = "agentbus.codex-bridge"
            elif "codearts" in m_lower:
                target_connector = "agentbus.codearts-hook"
            else:
                target_connector = "agentbus.universal-wakeup"

    if target_connector not in {'agentbus.codearts-hook', 'agentbus.codex-bridge', 'agentbus.acp-bridge', 'agentbus.universal-wakeup'}:
        raise TriggerContractError('Unsupported connector', TriggerErrorCode.CONNECTOR_UNSUPPORTED)

    target_installation = installation_key
    if not target_installation:
        if existing is not None and existing.installation_key:
            target_installation = existing.installation_key
        else:
            target_installation = f"inst_{member_id}"

    # 3. 免 invite / 自动 CAS 换绑
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    agent_dir = get_member_agent_dir(ws, member_id)

    rebound = False
    if (
        existing is not None
        and existing.status == "bound"
        and existing.native_conversation_id == detected_session
        and existing.connector_id == target_connector
        and existing.installation_key == target_installation
    ):
        new_contact = existing
    else:
        if existing is not None:
            history_dir = agent_dir / "contact-history"
            snapshot_name = f"{existing.binding_id}-{existing.revision}.json"
            _atomic_write_json(history_dir / snapshot_name, existing.to_dict())
            new_rev = existing.revision + 1
            rebound = True
        else:
            new_rev = 1
            rebound = True

        new_binding_id = f"bnd_{uuid.uuid4().hex[:16]}"
        new_contact = Contact(
            schema_version=1,
            project_key=norm_project,
            member_id=member_id,
            binding_id=new_binding_id,
            revision=new_rev,
            status="bound",
            connector_id=target_connector,
            connector_version="1.0.0",
            installation_key=target_installation,
            execution_surface="desktop_existing",
            native_conversation_id=detected_session,
            native_title=f"Checkin for {member_id}",
            identity_source="self_checkin",
            bound_at=now_iso,
            updated_at=now_iso,
            capability_evidence_ref=None,
            reason_code=None,
        )
        contact_path = agent_dir / "contact.json"
        _atomic_write_json(contact_path, new_contact.to_dict())

    from .connectors._signals import reconcile_contact
    try:
        trigger_reconciliation = reconcile_contact(ws, new_contact)
    except (ValueError, OSError) as exc:
        trigger_reconciliation = {'status': 'needs_confirmation', 'error': str(exc)}

    # 4. 生成工作现场简报与换窗上下文摘要（Auto-Compaction Handoff Briefing）
    processing_dir = agent_dir / "processing"
    ready_dir = agent_dir / "ready"
    done_dir = agent_dir / "done"

    processing_tasks = []
    if processing_dir.is_dir():
        for p in islice(processing_dir.glob("*.json"), 500):
            try:
                t_data = _bounded_json(p)
                processing_tasks.append({
                    "id": t_data.get("id", p.stem),
                    "title": t_data.get("title", ""),
                    "deadline_ts": t_data.get("deadline_ts", 0.0),
                })
            except (ValueError, KeyError, TypeError):
                pass  # 容错探测类：任务卡解析异常忽略

    ready_tasks = []
    if ready_dir.is_dir():
        for r in islice(ready_dir.glob("*.json"), 500):
            try:
                r_data = _bounded_json(r)
                ready_tasks.append({
                    "id": r_data.get("id", r.stem),
                    "title": r_data.get("title", ""),
                    "type": r_data.get("type", "task"),
                })
            except (ValueError, KeyError, TypeError):
                pass  # 容错探测类：就绪任务解析异常忽略

    recent_done_tasks = []
    if done_dir.is_dir():
        from agentbus.public_files import PublicFiles
        public = PublicFiles(ws)
        public.checked(done_dir.relative_to(ws))
        completion_times = _completion_times(ws, member_id)
        def completion_key(path):
            recorded = completion_times.get(path.stem)
            try:
                at = datetime.datetime.fromisoformat(recorded).timestamp() if recorded else path.lstat().st_mtime
                return at, path.stem
            except (OSError, ValueError, TypeError):
                return float('-inf'), path.stem
        # Enumerate metadata, not JSON bodies. Keep bounded look-ahead so old
        # archives cannot hide the latest completion or grow memory unbounded.
        done_files = heapq.nlargest(32, done_dir.glob('*.json'), key=completion_key)
        for d_file in done_files:
            try:
                public.checked(d_file.relative_to(ws))
                d_data = _bounded_json(d_file)
                conclusion = d_data.get('result')
                if not conclusion and d_data.get('type') in ('result', 'info'):
                    conclusion = d_data.get('body')
                body_str = str(conclusion or '未提供完成结论').strip().replace("\n", " ")
                recent_done_tasks.append({
                    "id": d_data.get("id", d_file.stem),
                    "title": d_data.get("title", ""),
                    "summary": body_str[:120] + ("..." if len(body_str) > 120 else ""),
                    "completed_at": completion_times.get(d_data.get('id', d_file.stem)) or d_data.get('completed_at') or d_data.get('finished_at') or '',
                })
                if len(recent_done_tasks) == 5:
                    break
            except (OSError, ValueError, KeyError, TypeError):
                pass  # 容错探测类：已完成历史任务摘要读取异常忽略

    recent_done_tasks.reverse()  # chronological display of the selected latest records
    compaction_lines = [
        f"[Context Compaction Handoff] 成员 {member_id} (rev_{new_contact.revision})："
        f"历史已完成 {len(recent_done_tasks)} 项近期任务，当前在途 {len(processing_tasks)} 项，待办 {len(ready_tasks)} 项。"
    ]
    if recent_done_tasks:
        done_titles = "; ".join(f"{t['id']}({t['title']})：{t['summary']}" for t in recent_done_tasks[-3:])
        compaction_lines.append(f"近期已完成：{done_titles}")
    if processing_tasks:
        proc_titles = ", ".join(f"{t['id']}({t['title']})" for t in processing_tasks)
        compaction_lines.append(f"在途需按原认领票据续接，当前报到不授予接管权限：{proc_titles}")

    briefing = {
        "member_id": member_id,
        "session_id": detected_session,
        "processing_count": len(processing_tasks),
        "processing_tasks": processing_tasks,
        "ready_count": len(ready_tasks),
        "ready_tasks": ready_tasks,
        "recent_done_count": len(recent_done_tasks),
        "recent_done_tasks": recent_done_tasks,
        "context_compaction_summary": " | ".join(compaction_lines),
    }

    # 5. 主动向组长（Coordinator/Director）信箱汇报
    notified_coordinator = False
    if notify_coordinator:
        coordinator_id = "director"
        cfg_file = ws / ".agentbus" / "config.json"
        if cfg_file.is_file():
            try:
                cfg_obj = json.loads(cfg_file.read_text(encoding="utf-8"))
                coordinator_id = cfg_obj.get("coordinator") or cfg_obj.get("director") or "director"
            except (ValueError, KeyError, TypeError):
                pass  # 容错探测类：配置文件读取组长配置异常回退默认

        coord_dir = ws / ".agentbus" / "agents" / coordinator_id
        if coord_dir.is_dir() and coordinator_id != member_id:
            now_ts = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")
            msg_id = f"m-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"
            title = f"[成员就位] {member_id} 已绑定当前对话并可接活"
            body_lines = [
                f"成员: {member_id}",
                f"会话标识: {detected_session}",
                f"唤醒连接器: {target_connector}",
                f"联系卡版本: rev_{new_contact.revision} ({new_contact.binding_id})",
                f"在途任务数: {len(processing_tasks)}",
                f"待认领任务数: {len(ready_tasks)}",
                f"近期完成数: {len(recent_done_tasks)}",
                f"报到时间: {now_ts}",
                briefing['context_compaction_summary'],
            ]
            if capabilities:
                body_lines.append(f"声明能力: {', '.join(capabilities)}")
            body_text = "\n".join(body_lines)

            notice_msg = Message(
                id=msg_id,
                ts=now_ts,
                sender=member_id,
                to=coordinator_id,
                type="info",
                title=title,
                body=body_text,
            )
            validated_dict = notice_msg.to_dict()
            Message.from_dict(validated_dict, strict=True)

            coord_ready = coord_dir / "ready"
            coord_ready.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(coord_ready / f"{msg_id}.json", validated_dict)
            notified_coordinator = True

    return {
        "status": "ok",
        "member_id": member_id,
        "session_id": detected_session,
        "connector_id": target_connector,
        "binding_id": new_contact.binding_id,
        "revision": new_contact.revision,
        "rebound": rebound,
        "notified_coordinator": notified_coordinator,
        "briefing": briefing,
        "contact": new_contact.to_dict(),
        "trigger_reconciliation": trigger_reconciliation,
    }
