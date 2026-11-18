"""Bounded project-public reads; Codex integration, 2026-10-08.

Never follows links, walks a private tree, writes files, or executes content.
The caller chooses specific public files or a single machine-state directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat

MAX_READ_BYTES = 64 * 1024
DEFAULT_DIRECTORY_PAGE_SIZE = 256
MAX_MEMBER_DIRECTORIES = 512
PRIVATE_PARTS = {'.workbuddy', '.git', '.ssh', '.aws', '.azure', '.gnupg',
                 '不要访问', 'do not access'}
PRIVATE_FILES = {'.env', 'credentials.json', 'token.secret', 'id_rsa', 'id_ed25519',
                 'auth.json', 'cookies.json'}


class PublicFileError(ValueError):
    pass


def validate_relative(relative: str | Path) -> Path:
    text = str(relative).replace('\\', '/')
    win = PureWindowsPath(text)
    rel = Path(text)
    if (not text or win.drive or win.root or rel.is_absolute() or '..' in rel.parts
            or ':' in text or any(ord(ch) < 32 for ch in text)):
        raise PublicFileError('public file must be a safe project-relative path')
    if any(part.casefold() in PRIVATE_PARTS | PRIVATE_FILES for part in rel.parts):
        raise PublicFileError('private paths are not public inputs')
    return rel


def _check_ancestors(path: Path) -> None:
    # Check lexical components before resolve/open: checking only the final
    # file misses a junction or symlink in a parent directory.
    if any(part.casefold() in PRIVATE_PARTS | PRIVATE_FILES for part in path.parts):
        raise PublicFileError('private path is not a public workspace')
    for node in [*reversed(path.parents), path]:
        try:
            info = node.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)):
            raise PublicFileError('symlink/reparse public inputs are refused')


class PublicFiles:
    def __init__(self, workspace: Path | str):
        self.root = Path(workspace).absolute()
        _check_ancestors(self.root)

    def checked(self, relative: str | Path) -> Path:
        rel = validate_relative(relative)
        target = self.root / rel
        _check_ancestors(target)
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise PublicFileError('public file escapes this workspace')
        return target

    def read_bytes(self, relative: str | Path, limit: int = MAX_READ_BYTES) -> bytes | None:
        target = self.checked(relative)
        try:
            info = target.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise PublicFileError('public input must be a regular file')
        if info.st_size > limit:
            raise PublicFileError('public input exceeds its byte budget')
        # A bounded stream prevents a growing file from defeating the stat
        # check. Recheck after opening to detect replacement before reading.
        with target.open('rb') as stream:
            self.checked(relative)
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise PublicFileError('public input changed while opening')
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise PublicFileError('public input exceeds its byte budget')
        return raw

    def read_text(self, relative: str | Path) -> str | None:
        raw = self.read_bytes(relative)
        if raw is None:
            return None
        try:
            return raw.decode('utf-8-sig')
        except UnicodeError as exc:
            raise PublicFileError('public input must be UTF-8') from exc

    def read_json(self, relative: str | Path) -> dict | None:
        text = self.read_text(relative)
        if text is None:
            return None
        try:
            value = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise PublicFileError('malformed public JSON') from exc
        if not isinstance(value, dict):
            raise PublicFileError('public JSON must be an object')
        return value

    def list_files(self, relative: str | Path, suffix: str = '.json') -> list[Path]:
        """Compatibility collector. Large observers should consume pages."""
        return sorted((path for page in self.iter_file_pages(relative, suffix) for path in page),
                      key=lambda path: path.name)

    def iter_file_pages(self, relative: str | Path, suffix: str = '.json', *,
                        page_size: int = DEFAULT_DIRECTORY_PAGE_SIZE):
        """Single metadata pass; bounded page memory, no history-count cutoff.

        Page order follows the filesystem; callers needing a global order sort
        their selected records. Each returned path still passes private/reparse
        checks, including after a page boundary.
        """
        if type(page_size) is not int or not 1 <= page_size <= 1024:
            raise PublicFileError('directory page size must be between 1 and 1024')
        folder = self.checked(relative)
        if not folder.exists():
            return
        if not folder.is_dir():
            raise PublicFileError('machine-state input must be a directory')
        found = []
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.name.endswith(suffix):
                    continue
                path = self.checked(Path(relative) / entry.name)
                if path.is_file():
                    found.append(path)
                    if len(found) == page_size:
                        yield found
                        found = []
        if found:
            yield found

    def list_member_directories(self) -> list[str]:
        from .member_adapter import validate_member_id, MemberAdapterError
        folder = self.checked('.agentbus/agents')
        if not folder.exists():
            return []
        members = []
        with os.scandir(folder) as entries:
            for number, entry in enumerate(entries):
                if number >= MAX_MEMBER_DIRECTORIES:
                    raise PublicFileError('member directory exceeds entry budget')
                try:
                    validate_member_id(entry.name)
                except MemberAdapterError:
                    continue  # Do not inspect any private/invalid directory.
                target = self.checked(f'.agentbus/agents/{entry.name}')
                if target.is_dir():
                    members.append(entry.name)
        return sorted(members)

    def recent_events(self) -> list[dict]:
        """Read only a bounded tail of the public audit log, without execution."""
        target = self.checked('.agentbus/events.jsonl')
        try:
            info = target.lstat()
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(info.st_mode):
            raise PublicFileError('audit input must be a regular file')
        with target.open('rb') as stream:
            self.checked('.agentbus/events.jsonl')
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise PublicFileError('audit input changed while opening')
            offset = max(0, opened.st_size - MAX_READ_BYTES)
            stream.seek(offset)
            raw = stream.read(MAX_READ_BYTES)
        if offset:
            _, _, raw = raw.partition(b'\n')
        events = []
        for line in raw.splitlines():
            try:
                event = json.loads(line.decode('utf-8'))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise PublicFileError('audit tail contains malformed JSON') from exc
            if not isinstance(event, dict):
                raise PublicFileError('audit event must be an object')
            events.append(event)
        return events

    def plan_head(self) -> tuple[dict | None, dict | None]:
        """Validate the current pointer against the formal revisions directory."""
        head = self.read_json('.agentbus/project-plan/current.json')
        if head is None:
            return None, None
        import re
        identity = head.get('revision_id')
        digest = head.get('revision_sha256')
        if (not isinstance(identity, str) or not re.fullmatch(r'r-[A-Za-z0-9._-]{1,62}', identity)
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise PublicFileError('current pointer has invalid revision identity/digest')
        rel = f'.agentbus/project-plan/revisions/{identity}.json'
        raw = self.read_bytes(rel)
        if raw is None:
            raise PublicFileError('current pointer references a missing formal revision')
        if hashlib.sha256(raw).hexdigest() != digest:
            raise PublicFileError('current revision byte digest differs')
        try:
            revision = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise PublicFileError('malformed current revision') from exc
        if not isinstance(revision, dict):
            raise PublicFileError('current revision must be an object')
        if (revision.get('kind') != 'revision' or revision.get('revision_id') != identity
                or revision.get('workspace') != str(self.root.resolve())):
            raise PublicFileError('revision does not belong to this project/current pointer')
        content = revision.get('content_md')
        if (not isinstance(content, str)
                or hashlib.sha256(content.encode('utf-8')).hexdigest() != revision.get('content_sha256')):
            raise PublicFileError('current revision content digest differs')
        return head, revision
