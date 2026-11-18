"""Source: Codex, 2026-10-04: bounded workspace file and artifact contract.

Participating writers use leases and raw-byte CAS. Host tools and unrelated
processes can bypass this workflow; this module is not a filesystem sandbox.
File fsync plus same-directory publication is not a hard-power-loss transaction.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from .lease import Lease, LeaseManager

MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_PAGE_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_ARTIFACT_ENTRIES = 128
MAX_ARTIFACT_TOTAL_BYTES = 512 * 1024 * 1024
_PRIVATE_PARTS = {'.workbuddy', '.git', '.agentbus', '.codex', '.ssh', '.aws',
                  '.azure', '.gnupg', '.claude', '.trae', '.zcode',
                  '不要访问', 'do not access'}
_DEVICE = re.compile(r'^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)
_HASH = re.compile(r'^[0-9a-f]{64}$')


class WorkspaceFileError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f'{code}: {message}')


def _error(code: str, message: str) -> None:
    raise WorkspaceFileError(code, message)


def _identity(info) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _version(info) -> tuple:
    return (*_identity(info), info.st_size, info.st_mtime_ns)


def _hash_argument(value, *, missing=False, optional=False):
    if optional and value is None:
        return
    if missing and value == 'MISSING':
        return
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _error('INVALID_HASH', 'expected_sha256 must be lowercase SHA256 or the creation sentinel MISSING')


class WorkspaceFiles:
    """No control directories are created until a real write obtains a lease."""

    def __init__(self, workspace: Path | str):
        self.workspace = Path(os.path.abspath(workspace))
        if any(part.casefold() in _PRIVATE_PARTS for part in self.workspace.parts):
            _error('UNSAFE_PATH', 'workspace is a private or reserved directory')
        try:
            for node in reversed((self.workspace, *self.workspace.parents)):
                info = node.lstat()
                self._no_link(node, info)
                if not stat.S_ISDIR(info.st_mode):
                    _error('NOT_DIRECTORY', 'workspace ancestry must contain directories')
            self.workspace = self.workspace.resolve()
            self._root_identity = _identity(self.workspace.lstat())
        except OSError as exc:
            _error('IO_ERROR', f'cannot inspect workspace ({type(exc).__name__})')

    @staticmethod
    def _no_link(node, info):
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _error('UNSAFE_PATH', f'links and reparse points are forbidden: {node.name}')

    def _root_check(self):
        info = self.workspace.lstat()
        self._no_link(self.workspace, info)
        if _identity(info) != self._root_identity:
            _error('FILE_CHANGED', 'workspace identity changed')
        # Also reject a newly introduced link above the selected root.
        for node in self.workspace.parents:
            self._no_link(node, node.lstat())

    @staticmethod
    def _relative(path) -> str:
        # §7.37 AUDIT-BODY-FILE-PATH-GUIDANCE：报错必须能自证「正确写法」。
        # 用工作区相对示例而非抽象描述——用户拿到的是可直接照抄的路径。
        _HINT = ('请使用工作区相对路径，例如 '
                 "'成果/复函/20261011-回执-说明.md'（正斜杠、不含盘符与开头斜杠）")
        if not isinstance(path, (str, Path)):
            _error('UNSAFE_PATH', 'path must be a relative string; ' + _HINT)
        raw = str(path).replace('\\', '/')
        if not raw or raw.startswith('/') or ':' in raw:
            _error('UNSAFE_PATH',
                   'absolute paths, namespaces and streams are forbidden; ' + _HINT)
        parts = raw.split('/')
        for part in parts:
            if (part in ('', '.', '..') or part[-1:] in (' ', '.')
                    or any(ord(char) < 32 or ord(char) == 127 for char in part)
                    or any(char in '<>"|?*' for char in part)
                    or part.casefold() in _PRIVATE_PARTS or _DEVICE.match(part)):
                # 命中私有目录时单独说清原因，避免用户以为是拼写问题。
                if part.casefold() in _PRIVATE_PARTS:
                    _error('UNSAFE_PATH',
                           '路径段 %r 属于私有/保留目录，禁止读写；%s'
                           % (part, _HINT))
                _error('UNSAFE_PATH',
                       '路径段 %r 非法（空段、. / ..、结尾空格点号、控制字符、'
                       '保留字符或设备名）；%s' % (part, _HINT))
        return '/'.join(parts)

    def _inspect(self, target: Path, *, missing=False, directory=False) -> dict:
        self._root_check()
        if not target.is_relative_to(self.workspace):
            _error('UNSAFE_PATH', 'path escapes workspace')
        snapshots = {self.workspace: self._root_identity}
        current = self.workspace
        parts = target.relative_to(self.workspace).parts
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if missing:
                    snapshots[target] = None
                    return snapshots
                _error('NOT_FOUND', 'file does not exist')
            self._no_link(current, info)
            snapshots[current] = _identity(info)
            if index < len(parts) - 1 or directory:
                if not stat.S_ISDIR(info.st_mode):
                    _error('NOT_DIRECTORY', 'parent path is not a directory')
            elif not stat.S_ISREG(info.st_mode):
                _error('NOT_FILE', 'target must be a regular file')
        return snapshots

    def _path(self, path, *, missing=False):
        rel = self._relative(path)
        target = self.workspace.joinpath(*rel.split('/'))
        snapshot = self._inspect(target, missing=missing)
        canonical = target.resolve()
        if (not canonical.is_relative_to(self.workspace)
                or any(part.casefold() in _PRIVATE_PARTS for part in canonical.relative_to(self.workspace).parts)):
            _error('UNSAFE_PATH', 'canonical path escapes workspace or enters a private directory')
        # Canonical spelling also collapses Windows short-name aliases for leases.
        rel = canonical.relative_to(self.workspace).as_posix()
        if canonical != target:
            snapshot = self._inspect(canonical, missing=missing)
        return rel, canonical, snapshot

    def _unchanged(self, before, target):
        after = self._inspect(target, missing=True)
        for node, identity in before.items():
            if after.get(node) != identity:
                _error('FILE_CHANGED', 'path identity changed during operation')

    def _raw(self, target, limit, *, snapshot=None):
        snapshot = snapshot or self._inspect(target)
        if snapshot.get(target) is None:
            _error('NOT_FOUND', 'file does not exist')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(target, flags), 'rb') as stream:
            before = os.fstat(stream.fileno())
            if _identity(before) != snapshot[target] or not stat.S_ISREG(before.st_mode):
                _error('FILE_CHANGED', 'opened file identity differs')
            if before.st_size > limit:
                _error('TOO_LARGE', 'file exceeds byte limit')
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                _error('TOO_LARGE', 'file grew beyond byte limit')
            if _version(os.fstat(stream.fileno())) != _version(before):
                _error('FILE_CHANGED', 'file changed while being read')
        self._unchanged(snapshot, target)
        return raw

    def read_text(self, path, start_line=1, max_lines=200, expected_sha256=None):
        _hash_argument(expected_sha256, optional=True)
        if (type(start_line) is not int or start_line < 1
                or type(max_lines) is not int or not 1 <= max_lines <= 200):
            _error('INVALID_ARGUMENT', 'line numbers must be positive; max_lines is at most 200')
        try:
            rel, target, snapshot = self._path(path)
            raw = self._raw(target, MAX_TEXT_BYTES, snapshot=snapshot)
            digest = hashlib.sha256(raw).hexdigest()
            if expected_sha256 is not None and digest != expected_sha256:
                _error('CONFLICT', 'file does not match the requested snapshot')
            try:
                text = raw.decode('utf-8-sig', errors='strict')
            except UnicodeError:
                _error('INVALID_UTF8', 'text must be valid UTF-8')
            # Count CRLF, CR and LF only; retain separators and the final no-NL line.
            lines = [match.group() for match in re.finditer(r'.*?(?:\r\n|\r|\n|$)', text, re.S)
                     if match.group()]
            endings = set(re.findall(r'\r\n|\r|\n', text))
            newline_style = ('none' if not endings else 'mixed' if len(endings) > 1
                             else {'\r\n': 'CRLF', '\r': 'CR', '\n': 'LF'}[next(iter(endings))])
            result = {'path': rel, 'text': '', 'sha256': digest,
                      'bytes': len(raw), 'bom': raw.startswith(b'\xef\xbb\xbf'),
                      'encoding': 'utf-8', 'newline_style': newline_style,
                      'total_lines': len(lines), 'next_line': len(lines) + 1}
            # Bound the entire JSON response, including escaping and metadata.
            # ASCII and pretty JSON is conservative for either CLI serializer.
            overhead = len(json.dumps(result, ensure_ascii=True, indent=2).encode('utf-8'))
            selected, page_bytes = [], overhead
            index = min(start_line - 1, len(lines))
            while index < len(lines) and len(selected) < max_lines:
                size = len(json.dumps(lines[index], ensure_ascii=True)[1:-1].encode('utf-8'))
                if page_bytes + size > MAX_PAGE_BYTES:
                    if not selected:
                        _error('PAGE_TOO_LARGE', 'one line exceeds output limit; use a smaller external representation')
                    break
                selected.append(lines[index])
                page_bytes += size
                index += 1
            result.update(text=''.join(selected), next_line=index + 1 if index < len(lines) else None)
            return result
        except OSError as exc:
            _error('IO_ERROR', f'cannot read text ({type(exc).__name__})')

    def _control(self, rel, *, check_stop=True):
        base = self.workspace / '.agentbus'
        self._inspect(base, missing=True, directory=True)
        self._inspect(base / 'leases', missing=True, directory=True)
        self._inspect(base / 'leases' / '.mutation.lock', missing=True)
        key = hashlib.sha1(LeaseManager.normalize(rel).encode('utf-8')).hexdigest()[:16]
        lease_file = base / 'leases' / (key + '.json')
        self._inspect(lease_file, missing=True)
        stop = base / 'STOP'
        stop_snapshot = self._inspect(stop, missing=True)
        if check_stop and stop_snapshot.get(stop) is not None:
            _error('STOPPED', 'workspace STOP marker blocks writes')
        return base, lease_file

    def _dry_lease(self, lease_file):
        info = self._inspect(lease_file, missing=True)
        if info.get(lease_file) is None:
            return
        try:
            record = json.loads(self._raw(lease_file, MAX_PAGE_BYTES, snapshot=info).decode('utf-8'))
            record.pop('expires_at', None)
            current = Lease(**record)
            expires = current.expires_at
            busy = not math.isfinite(expires) or not current.expired()
        except (ValueError, TypeError, AttributeError):
            busy = True
        if busy:
            _error('LEASE_BUSY', 'file already has a live or unreadable lease')

    def _cas(self, target, expected, snapshot):
        if snapshot.get(target) is None:
            current = 'MISSING'
        else:
            current = hashlib.sha256(self._raw(target, MAX_TEXT_BYTES, snapshot=snapshot)).hexdigest()
        if current != expected:
            _error('CONFLICT', 'current bytes differ from expected_sha256')

    def _parents(self, target):
        current = self.workspace
        for part in target.parent.relative_to(self.workspace).parts:
            current = current / part
            before = self._inspect(current, missing=True, directory=True)
            if before.get(current) is None:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass  # 资源清理类：并发下目录已被他人建成，属预期竞态，随后重新 inspect。
                self._inspect(current, directory=True)

    def _lease_check(self, manager, lease, rel):
        self._control(rel)
        current = manager.read(rel)
        try:
            valid = (current is not None and current.token == lease.token
                     and current.owner == lease.owner
                     and current.rel == manager.normalize(rel)
                     and math.isfinite(current.expires_at) and not current.expired())
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            _error('LEASE_LOST', 'lease token, identity or expiry changed before commit')

    def write_text(self, path, text, owner, expected_sha256, dry_run=False):
        _hash_argument(expected_sha256, missing=True)
        if (not isinstance(owner, str) or not owner.strip() or len(owner) > 128
                or any(ord(char) < 32 for char in owner) or not isinstance(text, str)
                or type(dry_run) is not bool):
            _error('INVALID_ARGUMENT', 'text, owner and dry_run arguments are invalid')
        try:
            raw = text.encode('utf-8', errors='strict')
        except UnicodeError:
            _error('INVALID_UTF8', 'text contains an invalid Unicode sequence')
        if len(raw) > MAX_TEXT_BYTES:
            _error('TOO_LARGE', 'text exceeds byte limit')
        manager, lease, staging = None, None, None
        try:
            rel, target, before = self._path(path, missing=True)
            base, lease_file = self._control(rel)
            self._dry_lease(lease_file)
            self._cas(target, expected_sha256, before)
            result = {'path': rel, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                      'committed': False, 'dry_run': dry_run}
            if dry_run:
                return result
            manager = LeaseManager(base)
            self._control(rel)
            lease = manager.acquire(rel, owner, ttl_s=120, note='workspace file CAS commit')
            if lease is None:
                _error('LEASE_BUSY', 'file already has a lease or control lock is busy')
            self._parents(target)
            # Original identities must survive parent creation and lease acquisition.
            self._unchanged(before, target)
            stable = self._inspect(target, missing=True)
            staging = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(staging, flags, 0o600), 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._control(rel)
            with manager._mutation_guard() as locked:
                if not locked:
                    _error('LEASE_BUSY', 'lease control lock is busy')
                self._lease_check(manager, lease, rel)
                self._unchanged(stable, target)
                self._cas(target, expected_sha256, self._inspect(target, missing=True))
                for attempt in range(50):
                    self._lease_check(manager, lease, rel)
                    self._unchanged(stable, target)
                    self._cas(target, expected_sha256, self._inspect(target, missing=True))
                    try:
                        if expected_sha256 == 'MISSING':
                            if os.name == 'nt':
                                os.rename(staging, target)
                            else:
                                os.link(staging, target)
                        else:
                            os.replace(staging, target)
                        break
                    except FileExistsError:
                        _error('CONFLICT', 'another file appeared before creation')
                    except PermissionError:
                        if attempt == 49:
                            raise
                        time.sleep(0.01)
            result['committed'] = True
            result['durability'] = 'file_fsynced; directory sync and hard-power-loss recovery not guaranteed'
            return result
        except OSError as exc:
            _error('IO_ERROR', f'write failed ({type(exc).__name__}); inspect current bytes before retry')
        finally:
            if staging is not None:
                try:
                    # Do not clean a path whose ancestry was replaced or redirected.
                    self._inspect(staging, missing=True)
                    staging.unlink(missing_ok=True)
                except (OSError, WorkspaceFileError):
                    pass  # 资源清理类：失败路径上的暂存文件清理，失败不改变主结果。
            if manager is not None and lease is not None:
                try:
                    self._control(lease.rel, check_stop=False)
                    manager.release(lease)
                except (OSError, WorkspaceFileError):
                    pass  # 资源清理类：租约释放失败交由 TTL 过期兜底，不覆盖主结果。

    def describe_artifact(self, path):
        try:
            rel, target, snapshot = self._path(path)
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            digest, size = hashlib.sha256(), 0
            with os.fdopen(os.open(target, flags), 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or _identity(before) != snapshot[target]:
                    _error('FILE_CHANGED', 'opened artifact identity differs')
                if before.st_size > MAX_ARTIFACT_BYTES:
                    _error('TOO_LARGE', 'artifact exceeds byte limit')
                while True:
                    chunk = stream.read(min(1024 * 1024, MAX_ARTIFACT_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        _error('TOO_LARGE', 'artifact grew beyond byte limit')
                    digest.update(chunk)
                if _version(os.fstat(stream.fileno())) != _version(before):
                    _error('FILE_CHANGED', 'artifact changed while being hashed')
            self._unchanged(snapshot, target)
            return {'path': rel, 'bytes': size, 'sha256': digest.hexdigest()}
        except OSError as exc:
            _error('IO_ERROR', f'cannot inspect artifact ({type(exc).__name__})')

    def verify_artifacts(self, entries):
        """Byte evidence only; does not imply artifact quality or task acceptance."""
        mismatches, seen, total = [], set(), 0
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ARTIFACT_ENTRIES:
            return {'verdict': 'mismatch', 'mismatches': [{'code': 'INVALID_MANIFEST', 'path': '',
                                                          'reason': 'provide 1 to 128 artifact entries'}]}
        for entry in entries:
            path = entry.get('path', '') if isinstance(entry, dict) else ''
            try:
                if not isinstance(entry, dict) or set(entry) != {'path', 'bytes', 'sha256'}:
                    _error('INVALID_MANIFEST', 'entry requires path, bytes and sha256 only')
                _hash_argument(entry['sha256'])
                if type(entry['bytes']) is not int or not 0 <= entry['bytes'] <= MAX_ARTIFACT_BYTES:
                    _error('INVALID_MANIFEST', 'artifact byte length is invalid')
                rel, _, _ = self._path(path)
                key = os.path.normcase(rel)
                if key in seen:
                    _error('INVALID_MANIFEST', 'duplicate canonical artifact path')
                seen.add(key)
                total += entry['bytes']
                if total > MAX_ARTIFACT_TOTAL_BYTES:
                    _error('TOO_LARGE', 'manifest cumulative byte limit exceeded')
                actual = self.describe_artifact(rel)
                if actual['bytes'] != entry['bytes'] or actual['sha256'] != entry['sha256']:
                    mismatches.append({'path': rel, 'code': 'CONTENT_MISMATCH',
                                       'expected': entry, 'actual': actual})
            except WorkspaceFileError as exc:
                mismatches.append({'path': path if isinstance(path, str) else '',
                                   'code': exc.code, 'reason': str(exc)})
            except OSError as exc:
                mismatches.append({'path': path if isinstance(path, str) else '',
                                   'code': 'IO_ERROR', 'reason': type(exc).__name__})
        return {'verdict': 'mismatch' if mismatches else 'match', 'mismatches': mismatches}
