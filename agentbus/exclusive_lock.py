# -*- coding: utf-8 -*-
"""§3.9 TASK-EXCLUSIVE-LOCKING: 任务原子排他独占锁与多 Agent 防竞态机制。

来源：用户明确要求（刚性防线），任务 m-tree-96fcf4080d3aaa5bd302a6c4。
针对跨 Agent 争用同一任务、并发认领脑裂、非持有者伪造心跳/回执等严重漏洞，
提供物理级 OS 文件锁 + 持久化排他凭证强绑定闭环。
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any

DEFAULT_LOCK_TTL_S = 1800.0

from . import claim_fencing as _fencing
from .public_files import PublicFiles

EXCLUSIVE_LOCKS_DIRNAME = "exclusive-locks"
RELEASED_LOCKS_DIRNAME = "released"

_mutex_registry_guard = threading.Lock()
_mutex_registry: dict[str, threading.RLock] = {}


class ExclusiveLockError(ValueError):
    """排他锁错误，继承 ValueError 与通用异常规范。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}" if message else code)


def _locks_dir(bus_root: str | Path) -> Path:
    public = PublicFiles(bus_root)
    d = public.checked(EXCLUSIVE_LOCKS_DIRNAME)
    public.checked(f'{EXCLUSIVE_LOCKS_DIRNAME}/{RELEASED_LOCKS_DIRNAME}')
    d.mkdir(parents=True, exist_ok=True)
    (d / RELEASED_LOCKS_DIRNAME).mkdir(parents=True, exist_ok=True)
    return d


def lock_record_path(bus_root: str | Path, task_id: str) -> Path:
    _fencing.validate_task_id(task_id)
    from .workspace_files import WorkspaceFiles
    WorkspaceFiles._relative(f'{task_id}.lock.json')
    _locks_dir(bus_root)
    return PublicFiles(bus_root).checked(f'{EXCLUSIVE_LOCKS_DIRNAME}/{task_id}.lock.json')


@contextmanager
def task_exclusive_mutex(bus_root: str | Path, task_id: str, timeout_s: float = 10.0):
    """基于 task_id（跨所有 Agent 唯一物理主键）的全局物理操作系统文件锁。
    杜绝 operation_lock 仅按 agent 隔离导致的跨 Agent 竞态盲区。
    """
    _fencing.validate_task_id(task_id)
    root = PublicFiles(bus_root).root.resolve()
    key = hashlib.sha256(f"global-task-mutex\0{task_id}".encode("utf-8")).hexdigest()
    _locks_dir(root)
    directory = PublicFiles(root).checked(f'{EXCLUSIVE_LOCKS_DIRNAME}/.mutex')
    directory.mkdir(parents=True, exist_ok=True)
    path = PublicFiles(root).checked(f'{EXCLUSIVE_LOCKS_DIRNAME}/.mutex/{key}.lock')
    identity = str(path)

    with _mutex_registry_guard:
        local_lock = _mutex_registry.setdefault(identity, threading.RLock())

    deadline = time.monotonic() + timeout_s
    if not local_lock.acquire(timeout=max(0, timeout_s)):
        raise ExclusiveLockError('MUTEX_BUSY', 'local wait exceeded the total lock deadline')
    try:
        with path.open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ExclusiveLockError(
                            "MUTEX_BUSY",
                            f"任务 {task_id} 的全局物理锁正忙，请稍后重试"
                        )
                    time.sleep(0.02)
            try:
                yield
            finally:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass  # 资源清理类：释放 flock 失败时文件句柄随关闭回收，无后续动作。
    finally:
        local_lock.release()


def get_task_lock_info(bus_root: str | Path, task_id: str) -> dict[str, Any] | None:
    """读取指定任务的排他锁元数据（若存在且未释放）。"""
    path = lock_record_path(bus_root, task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("task_id") == task_id:
            _fencing.validate_epoch(data.get('claim_epoch'))
            _fencing.validate_claim_id(data.get('claim_id'))
            _fencing.validate_agent_id(data.get('owner_agent'))
            return data
    except (OSError, ValueError) as exc:
        raise ExclusiveLockError('LOCK_CORRUPT', 'lock record is unreadable or invalid; preserved for recovery') from exc
    raise ExclusiveLockError('LOCK_CORRUPT', 'lock record does not bind this task; preserved for recovery')


def acquire_task_lock(bus_root: str | Path, task_id: str, agent: str,
                      ticket: Any, note: str = "", ttl_s: float = DEFAULT_LOCK_TTL_S) -> dict[str, Any]:
    """为任务申请原子排他独占锁。

    规则：
    1. 跨 Agent 唯一独占：若已被其他成员持有，立即抛出 TASK_ALREADY_LOCKED；
    2. 同会话票据可安全重入刷新；
    3. 写入不可伪造的 claim_id 会话票据强绑定。
    """
    _fencing.validate_task_id(task_id)
    _fencing.validate_agent_id(agent)
    try:
        normalized = _fencing.normalize(ticket)
        if normalized is None or normalized.task_id != task_id or normalized.agent != agent:
            raise _fencing.TicketError('LOCK_TICKET_MISMATCH', 'ticket identity differs')
        ticket_dict = normalized.to_dict()
    except (ValueError, TypeError) as exc:
        raise ExclusiveLockError('LOCK_TICKET_MISMATCH', 'invalid task lock ticket') from exc
    claim_id = str(ticket_dict.get("claim_id") or "")
    epoch = int(ticket_dict.get("claim_epoch") or 0)
    conv_id = str(ticket_dict.get("conversation_id") or "")

    with task_exclusive_mutex(bus_root, task_id):
        now_ts = time.time()
        expires_ts = round(now_ts + ttl_s, 3)
        expires_at = datetime.datetime.fromtimestamp(expires_ts, datetime.timezone.utc).astimezone().isoformat()

        existing = get_task_lock_info(bus_root, task_id)
        if existing is not None:
            # 检查是否为同成员同票据重入
            if (existing.get("owner_agent") == agent and
                    existing.get("claim_id") == claim_id and
                    existing.get("claim_epoch") == epoch):
                existing["updated_at"] = _fencing.iso_now()
                existing["expires_ts"] = expires_ts
                existing["expires_at"] = expires_at
                existing["ttl_s"] = ttl_s
                if note:
                    existing["note"] = note
                target = lock_record_path(bus_root, task_id)
                staging = target.with_name(f'.{target.name}.{secrets.token_hex(6)}.tmp')
                try:
                    staging.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')
                    os.replace(staging, target)
                finally:
                    staging.unlink(missing_ok=True)
                return existing

            # 检查现有锁是否已超时，超时则允许依法接管僵尸锁
            exp = existing.get("expires_ts")
            is_expired = exp is not None and now_ts > float(exp)
            if not is_expired:
                # 其他成员或不同票据，且未超时，发生跨 Agent 争抢！硬性拦截！
                other_owner = existing.get("owner_agent", "unknown")
                fp = existing.get("claim_fingerprint", "unknown")
                raise ExclusiveLockError(
                    "TASK_ALREADY_LOCKED",
                    f"任务 {task_id} 已被成员 {other_owner}（票据 {fp}）排他独占锁定，禁止并发争抢！"
                )

            # 超时接管：归档旧锁并释放
            released_dir = _locks_dir(bus_root) / RELEASED_LOCKS_DIRNAME
            released_path = released_dir / f"{task_id}.takeover.{secrets.token_hex(4)}.json"
            existing["released_at"] = _fencing.iso_now()
            existing["released_by"] = agent
            existing["release_reason"] = "takeover_expired"
            staging = released_path.with_name(f".{released_path.name}.{secrets.token_hex(6)}.tmp")
            try:
                staging.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(staging, released_path)
            finally:
                staging.unlink(missing_ok=True)

        payload = {
            "schema_version": 1,
            "task_id": task_id,
            "owner_agent": agent,
            "claim_epoch": epoch,
            "claim_id": claim_id,
            "claim_fingerprint": _fencing.fingerprint(claim_id),
            "conversation_id": conv_id,
            "locked_at": _fencing.iso_now(),
            "expires_ts": expires_ts,
            "expires_at": expires_at,
            "ttl_s": ttl_s,
            "note": str(note or "")[:500]
        }
        target = lock_record_path(bus_root, task_id)
        staging = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
        try:
            staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)
        return payload


def verify_task_lock(bus_root: str | Path, task_id: str, agent: str,
                     ticket: Any = None, *, allow_expired: bool = False) -> dict[str, Any]:
    """校验任务排他独占锁的合法持有权。

    规则：
    1. 任务必须已排他锁定；
    2. 持有者 owner_agent 必须严格一致；
    3. 若提供了 ticket，claim_id 必须严格与锁定凭证一致。
    """
    _fencing.validate_task_id(task_id)
    _fencing.validate_agent_id(agent)

    existing = get_task_lock_info(bus_root, task_id)
    if existing is None:
        raise ExclusiveLockError(
            "LOCK_NOT_HELD",
            f"任务 {task_id} 缺乏排他锁记录，处于非受控状态"
        )

    if existing.get("owner_agent") != agent:
        actual_owner = existing.get("owner_agent", "unknown")
        raise ExclusiveLockError(
            "LOCK_OWNER_MISMATCH",
            f"任务 {task_id} 的排他锁归属于 {actual_owner}，成员 {agent} 无权操作！"
        )

    if ticket is not None:
        try:
            normalized = _fencing.normalize(ticket)
            if normalized.task_id != task_id or normalized.agent != agent:
                raise _fencing.TicketError('LOCK_TICKET_MISMATCH', 'ticket identity differs')
            ticket_dict = normalized.to_dict()
        except (ValueError, TypeError) as exc:
            raise ExclusiveLockError('LOCK_TICKET_MISMATCH', 'invalid task lock ticket') from exc
        claim_id = str(ticket_dict.get("claim_id") or "")
        if (not claim_id or existing.get("claim_id") != claim_id
                or existing.get('claim_epoch') != ticket_dict.get('claim_epoch')
                or type(ticket_dict.get('claim_epoch')) is not int):
            raise ExclusiveLockError(
                "LOCK_TICKET_MISMATCH",
                f"提交的会话票据与任务 {task_id} 当前排他锁凭据不一致！"
            )
    expires = existing.get('expires_ts')
    if (not allow_expired and (type(expires) not in (int, float)
                              or expires <= time.time())):
        raise ExclusiveLockError(
            'LOCK_EXPIRED',
            '任务独占锁已过期，当前不能交付或续约。请保留成果，确认原执行者停止后恢复并重新认领，'
            '保存新票据再继续。任务时限与锁有效期分别计算，恢复不会延长原任务时限。')
    return existing


def release_task_lock(bus_root: str | Path, task_id: str, agent: str,
                      ticket: Any = None, *, expected_claim_epoch: int | None = None,
                      expected_claim_fingerprint: str | None = None) -> bool:
    """原子释放并归档；管理员收尾可绑定原代次及完整票据摘要，不能释放新锁。"""
    _fencing.validate_task_id(task_id)
    _fencing.validate_agent_id(agent)

    with task_exclusive_mutex(bus_root, task_id):
        existing = verify_task_lock(bus_root, task_id, agent, ticket, allow_expired=True)
        if expected_claim_epoch is not None or expected_claim_fingerprint is not None:
            claim = existing.get('claim_id')
            if (type(expected_claim_epoch) is not int or expected_claim_epoch < 1
                    or not isinstance(expected_claim_fingerprint, str)
                    or len(expected_claim_fingerprint) != 64
                    or not isinstance(claim, str)
                    or existing.get('claim_epoch') != expected_claim_epoch
                    or hashlib.sha256(claim.encode('utf-8')).hexdigest() != expected_claim_fingerprint):
                raise ExclusiveLockError('LOCK_GENERATION_CHANGED', '原任务物理锁已变更；保留锁记录')
        target = lock_record_path(bus_root, task_id)
        released_dir = _locks_dir(bus_root) / RELEASED_LOCKS_DIRNAME
        released_path = released_dir / f"{task_id}.released.json"
        existing["released_at"] = _fencing.iso_now()
        existing["released_by"] = agent
        staging = released_path.with_name(f".{released_path.name}.{secrets.token_hex(6)}.tmp")
        try:
            staging.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(staging, released_path)
            target.unlink(missing_ok=True)
        finally:
            staging.unlink(missing_ok=True)
        return True
