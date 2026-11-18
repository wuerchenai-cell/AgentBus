#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.lease —— 文件租约。

解决什么问题：两个 agent 同时被派了活，都要改 README.md，
后写的把先写的覆盖掉，而且**没有任何报错**——这是多 agent 协作里最难查的一类事故。

做法：谁要改文件，先在 .agentbus/leases/ 下抢一个租约文件。
租约先在临时文件中完整写好，再以不覆盖已有目标的原子操作发布：
Windows 使用 rename，POSIX 使用 link；文件已存在就返回 FileExistsError。
变更操作还由 OS 文件锁串行保护，避免 token 校验与删除之间的竞态。

三个必须的细节：
  1. 租约必须带 TTL 并支持抢占过期租约 —— 否则 agent 崩了就永久锁死文件。
  2. 释放前必须校验 token —— 否则 A 超时后 B 拿到租约，A 醒来会把 B 的锁删掉。
  3. 租约文件内容要能被人看懂（谁、什么时候、干什么），方便事后查账。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

DEFAULT_TTL_S = 900
DELETE_RETRIES = 50
DELETE_RETRY_INTERVAL_S = 0.01


class LeaseError(RuntimeError):
    pass


@dataclass
class Lease:
    rel: str                 # 相对工作空间的路径，统一用正斜杠
    owner: str               # 谁持有（agent 名）
    token: str               # 释放凭证
    acquired_at: float
    ttl_s: int
    note: str = ""
    msg_id: str = ""

    @property
    def expires_at(self) -> float:
        return self.acquired_at + self.ttl_s

    def remaining_s(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def expired(self) -> bool:
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expires_at"] = self.expires_at
        return d


class LeaseManager:
    def __init__(self, root: Path | str):
        self.root = Path(root) / "leases"
        self.root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _mutation_guard(self):
        """Serialize lease mutations across processes; OS unlocks on crash."""
        fd = os.open(str(self.root / ".mutation.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        locked = False
        try:
            # Windows byte-range locks may cover bytes beyond EOF. Do not
            # write a sentinel before acquiring the lock: another fresh
            # process may already have locked that byte (PermissionError).
            # POSIX flock likewise does not need content in its lock file.
            for attempt in range(DELETE_RETRIES):
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if attempt + 1 < DELETE_RETRIES:
                        time.sleep(DELETE_RETRY_INTERVAL_S)
            yield locked
        finally:
            try:
                if locked:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # ---- 路径归一 ----
    @staticmethod
    def normalize(rel: str | Path) -> str:
        s = str(rel).replace("\\", "/").strip()
        while s.startswith("./"):
            s = s[2:]
        return s.strip("/").lower()

    def _file(self, rel: str) -> Path:
        key = hashlib.sha1(self.normalize(rel).encode("utf-8")).hexdigest()[:16]
        return self.root / f"{key}.json"

    # ---- 读 ----
    def read(self, rel: str | Path) -> Lease | None:
        f = self._file(str(rel))
        if not f.exists():
            return None
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        d.pop("expires_at", None)
        try:
            return Lease(**d)
        except TypeError:
            return None

    def holder(self, rel: str | Path) -> dict[str, Any] | None:
        """返回当前持有者信息；过期租约视为无人持有。"""
        lz = self.read(rel)
        if lz is None:
            return None
        if lz.expired():
            return None
        return lz.to_dict()

    # ---- 抢 ----
    def acquire(self, rel: str | Path, owner: str, ttl_s: int = DEFAULT_TTL_S,
                note: str = "", msg_id: str = "", steal_expired: bool = True) -> Lease | None:
        """抢到返回 Lease，被别人占着返回 None。

        抢不到就是抢不到 —— 调用方应该去做别的事，而不是死等。
        """
        with self._mutation_guard() as locked:
            if not locked:
                return None
            return self._acquire_locked(rel, owner, ttl_s, note, msg_id, steal_expired)

    def _acquire_locked(self, rel, owner, ttl_s, note, msg_id, steal_expired):
        f = self._file(str(rel))
        lease = Lease(rel=self.normalize(rel), owner=owner,
                      token=uuid.uuid4().hex, acquired_at=time.time(),
                      ttl_s=int(ttl_s), note=note, msg_id=msg_id)
        payload = json.dumps(lease.to_dict(), ensure_ascii=False, indent=2)

        temp = f.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in (0, 1):
                try:
                    if os.name == "nt":
                        os.rename(temp, f)  # Windows rename never replaces a destination.
                    else:
                        os.link(temp, f)  # POSIX rename would replace; link cannot.
                    return lease
                except FileExistsError:
                    if attempt or not steal_expired:
                        return None
                    cur = self.read(rel)
                    # Never infer expiry from inability to read an old lock.
                    if cur is None or not cur.expired():
                        return None
                    if not self._release_locked(cur, expired_only=True):
                        return None
            return None
        finally:
            temp.unlink(missing_ok=True)

    def acquire_wait(self, rel: str | Path, owner: str, ttl_s: int,
                     timeout_s: float, poll_interval_s: float = 0.05,
                     note: str = "", msg_id: str = "") -> Lease | None:
        """有界等待跨进程租约；等待期间不启动模型或执行任务。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            lease = self.acquire(rel, owner, ttl_s=ttl_s, note=note, msg_id=msg_id)
            if lease is not None:
                return lease
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(max(0.01, poll_interval_s), remaining))

    # ---- 续 / 放 ----
    def renew(self, lease: Lease, ttl_s: int | None = None) -> bool:
        with self._mutation_guard() as locked:
            if not locked:
                return False
            cur = self.read(lease.rel)
            if cur is None or cur.token != lease.token:
                return False
            renewed = replace(lease, ttl_s=int(ttl_s or lease.ttl_s), acquired_at=time.time())
            target = self._file(lease.rel)
            temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                temp.write_text(json.dumps(renewed.to_dict(), ensure_ascii=False, indent=2),
                                encoding="utf-8")
                for attempt in range(DELETE_RETRIES):
                    try:
                        os.replace(temp, target)
                        lease.ttl_s, lease.acquired_at = renewed.ttl_s, renewed.acquired_at
                        return True
                    except PermissionError:
                        if attempt + 1 < DELETE_RETRIES:
                            time.sleep(DELETE_RETRY_INTERVAL_S)
                return False
            except OSError:
                return False
            finally:
                temp.unlink(missing_ok=True)

    def release(self, lease: Lease, *, expired_only: bool = False) -> bool:
        """只有持有者本人能释放（token 校验），否则 A 会删掉 B 的锁。"""
        with self._mutation_guard() as locked:
            return self._release_locked(lease, expired_only=expired_only) if locked else False

    def _release_locked(self, lease: Lease, *, expired_only: bool = False) -> bool:
        f = self._file(lease.rel)
        for attempt in range(DELETE_RETRIES):
            cur = self.read(lease.rel)
            if cur is None:
                if not f.exists():
                    return True
                if attempt + 1 < DELETE_RETRIES:
                    time.sleep(DELETE_RETRY_INTERVAL_S)
                continue
            if cur.token != lease.token:
                return False
            if expired_only and not cur.expired():
                return False
            try:
                f.unlink(missing_ok=True)
                return True
            except PermissionError:
                # On Windows a concurrent read may temporarily deny delete
                # sharing. Recheck the token before each retry in case the
                # expired lease was replaced while this process was waiting.
                if attempt + 1 < DELETE_RETRIES:
                    time.sleep(DELETE_RETRY_INTERVAL_S)
        return False

    # ---- 维护 ----
    def sweep(self) -> list[str]:
        """清掉过期租约，返回被清掉的路径。"""
        cleared: list[str] = []
        for f in self.root.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                lz = Lease(**{k: v for k, v in d.items() if k != "expires_at"})
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                # A scan is observational; inability to read is not permission
                # to remove a possibly live, partially published lease.
                continue
            if lz.expired() and self.release(lz, expired_only=True):
                cleared.append(lz.rel)
        return cleared

    def active(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for f in self.root.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                lz = Lease(**{k: v for k, v in d.items() if k != "expires_at"})
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                continue
            if not lz.expired():
                out.append(lz.to_dict())
        return sorted(out, key=lambda x: x["acquired_at"])

    def guarded(self, rel: str | Path, owner: str, ttl_s: int = DEFAULT_TTL_S,
                note: str = "") -> "LeaseGuard":
        return LeaseGuard(self, str(rel), owner, ttl_s, note)


class LeaseGuard:
    """with 用法：抢不到就抛 LeaseError，抢到了退出时自动释放。"""

    def __init__(self, mgr: LeaseManager, rel: str, owner: str, ttl_s: int, note: str):
        self.mgr = mgr
        self.rel = rel
        self.owner = owner
        self.ttl_s = ttl_s
        self.note = note
        self.lease: Lease | None = None

    def __enter__(self) -> Lease:
        lz = self.mgr.acquire(self.rel, self.owner, self.ttl_s, self.note)
        if lz is None:
            cur = self.mgr.holder(self.rel)
            who = (cur or {}).get("owner", "未知")
            raise LeaseError(f"{self.rel} 正被 {who} 占用，不能同时改")
        self.lease = lz
        return lz

    def __exit__(self, *exc: Any) -> None:
        if self.lease is not None:
            self.mgr.release(self.lease)


def lease_block(rel: str, owner: str, ttl_s: int = DEFAULT_TTL_S,
                msg_id: str = "") -> str:
    """给执行者看的「先抢租约再动手」提示，会被拼进任务 prompt。"""
    return (
        "【动手前先抢租约】如果你要修改文件，先抢租约再改，避免和别人互相覆盖：\n"
        f'  python -m agentbus lease --workspace "<工作空间>" --path "{rel}" '
        f"--owner {owner} --ttl {ttl_s}\n"
        "抢到了就改，改完 release 掉；抢不到说明别人正在改，先做别的部分，"
        "不要硬改。\n"
    )
