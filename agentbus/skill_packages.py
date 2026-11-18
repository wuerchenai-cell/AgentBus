"""Source: OpenCode, 2026-10-05: read-only full-directory skill package inspection.

Provenance: Task 1 of 成果/agentbus/pilots/skill-library-20261005/IMPLEMENTATION_PLAN.md,
task m-1791169024286-6204ed, plan r-cec9c20aaaf9fa20f215ab8338a7efd7
(content_sha256 faa96eaf4fe8137d85d5670c25d8c64614d2a9c07d489425083f5af5525ac926).

What this module proves and what it does not:

* It proves that every regular file in a package directory has the same relative
  path, byte length and raw SHA256 as a previously recorded manifest, and it
  proves the package tree did not change identity or contents during the scan.
* It does NOT prove host discovery, host invocation, executable permission, ACLs,
  POSIX mode bits, interpreter selection or authentication. Same bytes can be
  interpreted differently by different hosts. Those are execution conditions and
  stay unverified here (DESIGN_RESOLUTIONS.md 1).
* It never writes, installs, executes or imports anything from the scanned
  directory. It reads raw bytes only. It is not a sandbox: a concurrent writer
  that follows no contract can still race, and a change can land between the
  final verification and the caller acting on the returned digest.
* Reuses WorkspaceFiles for ancestry, lstat, link/reparse and private-directory
  rejection, and for raw bounded reads with per-file identity checks.

Digest algorithm (DISCUSSION_DECISIONS.md contract 1): the manifest is a
strictly three-field list of {path, bytes, sha256} entries sorted by UTF-8
relative POSIX path; content_digest is the SHA256 of the canonical JSON encoding
of {"schema_version": 1, "files": manifest}. Encoding and execution metadata are
deliberately excluded from that digest and reported separately.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Any

# Codex internal independent audit integration corrections, 2026-10-05:
# every initial/final read and directory re-list uses this scan's own budget.
# Original author delivery bytes are preserved in author-corrections-snapshot.

from .workspace_files import (
    _PRIVATE_PARTS,
    WorkspaceFileError,
    WorkspaceFiles,
    _identity,
    _version,
)

SKILL_ENTRY = 'SKILL.md'
DEFAULT_MAX_FILES = 256
DEFAULT_MAX_TOTAL_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_DIR_BYTES = 1 * 1024 * 1024
MANIFEST_FIELDS = ('path', 'bytes', 'sha256')
_ENTRY_NAME = 'SKILL.md'
_SCRIPT_SUFFIXES = ('.py', '.ps1', '.sh', '.bat', '.cmd', '.js', '.ts', '.exe', '.dll')
# Names Windows folds or refuses regardless of case; recorded so the reason is
# explicit rather than a generic failure.
_WINDOWS_RESERVED = {'con', 'prn', 'aux', 'nul', 'com1', 'com2', 'com3', 'com4',
                     'com5', 'com6', 'com7', 'com8', 'com9', 'lpt1', 'lpt2',
                     'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9'}


class SkillPackageError(ValueError):
    """Raised instead of returning a partial or untrustworthy manifest."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f'{code}: {message}')


def _error(code: str, message: str) -> None:
    raise SkillPackageError(code, message)


def _wrap(exc: WorkspaceFileError) -> 'SkillPackageError':
    return SkillPackageError(exc.code, str(exc))


def _canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _sort_key(path: str) -> bytes:
    # The contract says "sorted by the UTF-8 relative POSIX path", so compare the
    # whole encoded path. Comparing per-segment tuples is a different order: it
    # sorts "a/x" before "a-/y" because b'' < b'-', while the byte order of the
    # full path puts "a-/y" first because b'-' (0x2d) < b'/' (0x2f).
    return path.encode('utf-8')


def _check_component(part: str, *, where: str) -> None:
    if part in ('', '.', '..'):
        _error('UNSAFE_PATH', f'{where}: empty or relative component is forbidden')
    if part[-1:] in (' ', '.'):
        _error('UNSAFE_PATH', f'{where}: component must not end with a space or dot')
    if any(ord(char) < 32 or ord(char) == 127 for char in part):
        _error('UNSAFE_PATH', f'{where}: control characters are forbidden')
    if any(char in '<>"|?*' for char in part):
        _error('UNSAFE_PATH', f'{where}: reserved characters are forbidden')
    if any(unicodedata.category(char) == 'Cc' for char in part):
        _error('UNSAFE_PATH', f'{where}: control characters are forbidden')
    # Reject before descending, so a private directory is never enumerated.
    # A file inside one is not proof of safety: an empty private directory would
    # otherwise be skipped silently and the manifest would understate the tree.
    if part.casefold() in _PRIVATE_PARTS:
        _error('UNSAFE_PATH',
               f'{where}: private or reserved directory components are forbidden')
    stem = part.split('.', 1)[0].casefold()
    if stem in _WINDOWS_RESERVED:
        _error('UNSAFE_PATH', f'{where}: Windows reserved device name is forbidden')


def _check_collisions(entries: list[str]) -> None:
    """Refuse two names that differ only by case or Unicode normalisation.

    Callers pass every directory and file relative path, not only files: a
    case-only directory pair can hide a whole subtree and would otherwise be
    invisible to a file-only comparison.
    """
    seen: dict[str, str] = {}
    for rel in entries:
        folded = unicodedata.normalize('NFC', rel).casefold()
        previous = seen.get(folded)
        if previous is not None:
            _error('PATH_COLLISION',
                   f'paths differ only by case or Unicode normalization: '
                   f'{previous!r} and {rel!r}')
        seen[folded] = rel


def _bounded(value, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        _error('INVALID_ARGUMENT', f'{name} must be an integer >= {minimum}')
    return value


def _describe_execution(files: list[dict], entry_meta: dict) -> dict:
    """Execution conditions are observations, never part of content identity."""
    scripts = sorted(entry['path'] for entry in files
                     if entry['path'].casefold().endswith(_SCRIPT_SUFFIXES))
    unknown = [entry['path'] for entry in files
               if entry['path'].casefold() not in {SKILL_ENTRY.casefold(), 'references/'}
               and entry['path'].casefold().endswith(_SCRIPT_SUFFIXES)]
    notes = {
        'entry_encoding': entry_meta['encoding'],
        'entry_bom': entry_meta['bom'],
        'entry_newline_style': entry_meta['newline_style'],
        'script_like_files': scripts,
        'executable_bit': 'unknown',
        'posix_mode': 'unknown',
        'acl': 'unknown',
        'interpreter': 'unknown',
        'host_discovery': 'unverified',
        'host_invocation': 'unverified',
    }
    if unknown:
        notes['scripts_not_executed'] = unknown
    return notes


class _Scanner:
    def __init__(self, files: WorkspaceFiles, root: Path, source: str, *,
                 max_files: int, max_total_bytes: int, max_file_bytes: int,
                 max_dir_bytes: int):
        self.files = files
        self.root = root
        self.source = source
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_file_bytes = max_file_bytes
        self.max_dir_bytes = max_dir_bytes
        self.identities: dict[Path, tuple] = {}
        self.versions: dict[Path, tuple] = {}
        self.manifest: list[dict] = []
        self.total_bytes = 0
        self.dir_nodes = 0
        # Relative POSIX paths of every directory, for the collision check.
        self.dir_paths: list[str] = []
        # Directory nodes mapped to the exact child names seen during the walk.
        # Inode identity does not change when a child is added or removed, so
        # the manifest set has to be re-listed to notice a late arrival.
        self.children: dict[Path, frozenset] = {}

    def _directory(self, node: Path, rel: str) -> None:
        try:
            info = node.lstat()
        except OSError as exc:
            _error('IO_ERROR', f'cannot inspect {rel} ({type(exc).__name__})')
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _error('UNSAFE_PATH', f'links and reparse points are forbidden: {rel or "."}')
        if not stat.S_ISDIR(info.st_mode):
            _error('NOT_DIRECTORY', f'package path is not a directory: {rel}')
        # The scan root counts too: an empty package must not be able to expand
        # an unbounded tree just because the root carried no budget.
        self.dir_nodes += 1
        if self.dir_nodes > self.max_dir_bytes:
            _error('TOO_LARGE',
                   f'package exceeds the {self.max_dir_bytes} directory node limit')
        # The root is recorded as well: an empty package must not be able to
        # expand an unbounded tree, and the root must be re-checked like any
        # other node. It contributes no relative path, so it is not a collision
        # candidate.
        self.identities[node] = _identity(info)
        self.versions[node] = _version(info)
        if rel:
            self.dir_paths.append(rel)

    def _walk(self, node: Path, rel: str, depth: int) -> None:
        if depth > 32:
            _error('TOO_DEEP', 'package directory nesting exceeds 32 levels')
        # Enumerate under the same budget as the walk: a directory holding more
        # entries than the node limit must be refused while listing, not after
        # an unbounded list has already been materialised.
        listing: list = []
        try:
            with os.scandir(node) as scan:
                for item in scan:
                    listing.append(item.name)
                    if len(listing) > self.max_dir_bytes:
                        _error('TOO_LARGE',
                               f'{rel or "."}: directory holds more than '
                               f'{self.max_dir_bytes} entries')
        except OSError as exc:
            _error('IO_ERROR', f'cannot list {rel} ({type(exc).__name__})')
        listing.sort()
        self.children[node] = frozenset(listing)
        for name in listing:
            if not isinstance(name, str) or not name:
                _error('UNSAFE_PATH', f'{rel or "."}: unusable directory entry name')
            _check_component(name, where=f'{rel}/{name}' if rel else name)
            child_rel = f'{rel}/{name}' if rel else name
            child = node / name
            try:
                info = child.lstat()
            except OSError as exc:
                _error('IO_ERROR', f'cannot inspect {child_rel} ({type(exc).__name__})')
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                _error('UNSAFE_PATH', f'links and reparse points are forbidden: {child_rel}')
            if stat.S_ISDIR(info.st_mode):
                self._directory(child, child_rel)
                self._walk(child, child_rel, depth + 1)
            elif stat.S_ISREG(info.st_mode):
                self._read(child, child_rel, info)
            else:
                _error('UNSAFE_PATH',
                       f'special files (fifo/socket/device) are forbidden: {child_rel}')
            if len(self.manifest) > self.max_files:
                _error('TOO_LARGE',
                       f'package exceeds the {self.max_files} file limit')

    def _read(self, node: Path, rel: str, info) -> None:
        if len(self.manifest) >= self.max_files:
            _error('TOO_LARGE', f'package exceeds the {self.max_files} file limit')
        if info.st_size > self.max_file_bytes:
            _error('TOO_LARGE', f'{rel}: file exceeds {self.max_file_bytes} bytes')
        # Refuse before reading, not after the whole tree was read: a total-byte
        # budget that is only checked at the end does not bound actual reading.
        if self.total_bytes + info.st_size > self.max_total_bytes:
            _error('TOO_LARGE',
                   f'{rel}: package exceeds the {self.max_total_bytes} total byte limit')
        try:
            target_rel = f'{self.source}/{rel}'
            entry = self._bounded_artifact(target_rel,
                                           self.max_total_bytes - self.total_bytes)
        except WorkspaceFileError as exc:
            raise _wrap(exc) from exc
        if entry['path'] != target_rel:
            _error('UNSAFE_PATH', f'{rel}: canonical path differs from the requested path')
        if entry['bytes'] != info.st_size:
            _error('FILE_CHANGED', f'{rel}: size changed between enumeration and read')
        # Re-stat before opening: if the path was replaced between enumeration,
        # the read below would either fail obscurely or describe the new object.
        try:
            reopened = node.lstat()
        except OSError as exc:
            _error('FILE_CHANGED', f'{rel}: vanished before reading ({type(exc).__name__})')
        if (stat.S_ISLNK(reopened.st_mode)
                or getattr(reopened, 'st_file_attributes', 0) & 0x400
                or _identity(reopened) != _identity(info)):
            _error('FILE_CHANGED', f'{rel}: replaced between enumeration and read')
        digest = hashlib.sha256()
        try:
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(node, flags), 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(info):
                    _error('FILE_CHANGED', f'{rel}: opened file identity differs')
                if opened.st_size > self.max_file_bytes:
                    _error('TOO_LARGE', f'{rel}: file exceeds {self.max_file_bytes} bytes')
                size = 0
                while True:
                    chunk = stream.read(min(1024 * 1024,
                                            self.max_file_bytes + 1 - size,
                                            self.max_total_bytes - self.total_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        _error('TOO_LARGE', f'{rel}: file grew beyond {self.max_file_bytes} bytes')
                    if self.total_bytes + size > self.max_total_bytes:
                        _error('TOO_LARGE', f'{rel}: file grew beyond the remaining package byte limit')
                    digest.update(chunk)
                if _version(os.fstat(stream.fileno())) != _version(opened):
                    _error('FILE_CHANGED', f'{rel}: file changed while being read')
        except OSError as exc:
            _error('IO_ERROR', f'{rel}: cannot read ({type(exc).__name__})')
        if size != info.st_size or digest.hexdigest() != entry['sha256']:
            _error('FILE_CHANGED', f'{rel}: bytes differ between two reads')
        self.identities[node] = _identity(info)
        self.versions[node] = _version(info)
        self.total_bytes += size
        self.manifest.append({'path': rel, 'bytes': size, 'sha256': digest.hexdigest()})

    def _bounded_artifact(self, relative: str, remaining: int) -> dict:
        """Reuse controlled identity checks with the scanner's actual budget.

        WorkspaceFiles._raw reads at most limit+1 bytes (the extra byte detects
        growth); describe_artifact's broader 256 MiB limit does not apply here.
        """
        try:
            rel, target, snapshot = self.files._path(relative)
            raw = self.files._raw(target, min(self.max_file_bytes, remaining),
                                  snapshot=snapshot)
        except OSError as exc:
            _error('IO_ERROR', f'{relative}: cannot read ({type(exc).__name__})')
        return {'path': rel, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

    def verify_unchanged(self) -> None:
        for node, identity in self.identities.items():
            try:
                current = node.lstat()
            except OSError as exc:
                _error('FILE_CHANGED',
                       f'{node.name} disappeared during inspection ({type(exc).__name__})')
            if stat.S_ISLNK(current.st_mode) or getattr(current, 'st_file_attributes', 0) & 0x400:
                _error('UNSAFE_PATH', f'{node.name} became a link or reparse point')
            if _identity(current) != identity:
                _error('FILE_CHANGED', f'{node.name} changed identity during inspection')
            # Identity alone survives an in-place rewrite or a retimed directory,
            # so compare the full version stamp as well.
            if self.versions.get(node) != _version(current):
                _error('FILE_CHANGED',
                       f'{node.name} changed size or timestamp during inspection')
        # A directory keeps its inode when a child appears or disappears, so re-list
        # every directory and require the same child names.
        for node, seen in self.children.items():
            try:
                with os.scandir(node) as scan:
                    names = []
                    for item in scan:
                        names.append(item.name)
                        if len(names) > self.max_dir_bytes:
                            _error('TOO_LARGE', f'{node.name}: verification listing exceeds directory entry limit')
                        _check_component(item.name, where=node.name)
                    now = frozenset(names)
            except OSError as exc:
                _error('FILE_CHANGED',
                       f'{node.name} cannot be re-listed ({type(exc).__name__})')
            if now != seen:
                _error('FILE_CHANGED',
                       f'{node.name}: entries changed during inspection '
                       f'(added {sorted(now - seen)}, removed {sorted(seen - now)})')
        # Identity alone does not catch an in-place rewrite that keeps the inode,
        # so re-hash every file once more and compare against the manifest.
        verified_bytes = 0
        for entry in self.manifest:
            try:
                again = self._bounded_artifact(f'{self.source}/{entry["path"]}',
                                               self.max_total_bytes - verified_bytes)
            except WorkspaceFileError as exc:
                raise _wrap(exc) from exc
            if again['bytes'] != entry['bytes'] or again['sha256'] != entry['sha256']:
                _error('FILE_CHANGED',
                       f'{entry["path"]}: content changed during inspection')
            verified_bytes += again['bytes']


def inspect_package(workspace: Path | str, relative: str, *,
                    max_files: int = DEFAULT_MAX_FILES,
                    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
                    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                    max_dir_bytes: int = DEFAULT_MAX_DIR_BYTES) -> dict[str, Any]:
    """Inspect one skill package directory and return a raw-byte manifest.

    Read-only. Returns schema_version, source, name, files (strict three-field
    manifest sorted by UTF-8 relative POSIX path), total_bytes, content_digest,
    digest_covers and warnings. Raises SkillPackageError instead of returning a
    partial manifest.
    """
    _bounded(max_files, 'max_files', 1)
    _bounded(max_total_bytes, 'max_total_bytes', 1)
    _bounded(max_file_bytes, 'max_file_bytes', 1)
    _bounded(max_dir_bytes, 'max_dir_bytes', 1)
    try:
        files = WorkspaceFiles(workspace)
    except WorkspaceFileError as exc:
        raise _wrap(exc) from exc
    if not isinstance(relative, (str, Path)):
        _error('UNSAFE_PATH', 'relative must be a relative path string')
    raw = str(relative).replace('\\', '/')
    if not raw or raw.startswith('/') or ':' in raw:
        _error('UNSAFE_PATH', 'package path must be relative and inside the workspace')
    for part in raw.split('/'):
        _check_component(part, where='package path')
    source = '/'.join(raw.split('/'))
    # _path() insists the last component is a regular file, so resolve the
    # package directory with the same ancestry rules but directory=True.
    try:
        snapshot = files._inspect(files.workspace.joinpath(*source.split('/')),
                                  missing=True, directory=True)
    except WorkspaceFileError as exc:
        raise _wrap(exc) from exc
    target = files.workspace.joinpath(*source.split('/'))
    if snapshot.get(target) is None:
        _error('NOT_FOUND', 'package directory does not exist')
    canonical = target.resolve()
    if (not canonical.is_relative_to(files.workspace)
            or any(part.casefold() in _PRIVATE_PARTS
                   for part in canonical.relative_to(files.workspace).parts)):
        _error('UNSAFE_PATH',
               'canonical path escapes workspace or enters a private directory')
    if canonical != target:
        _error('UNSAFE_PATH',
               f'canonical spelling differs: {source!r} resolves to '
               f'{canonical.relative_to(files.workspace).as_posix()!r}')
    root = canonical
    # The root directory itself must survive its own component rules, otherwise
    # a package legitimately sitting under a reserved-looking name would be
    # scanned without the private-component check that protects every child.
    for part in source.split('/'):
        if part.casefold() in _PRIVATE_PARTS:
            _error('UNSAFE_PATH',
                   f'package path enters a private directory: {source!r}')

    scanner = _Scanner(files, root, source, max_files=max_files,
                       max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
                       max_dir_bytes=max_dir_bytes)
    scanner._directory(root, '')
    scanner._walk(root, '', 0)
    if not scanner.manifest:
        _error('EMPTY_PACKAGE', 'package directory contains no regular files')
    paths = [entry['path'] for entry in scanner.manifest]
    if _ENTRY_NAME not in paths:
        _error('NO_SKILL_ENTRY', f'package must contain {_ENTRY_NAME} at its root')
    # Directories participate: a case-only directory pair can hide a subtree and
    # would be invisible to a file-only comparison.
    _check_collisions(paths + scanner.dir_paths)
    total_bytes = scanner.total_bytes
    if total_bytes > max_total_bytes:
        _error('TOO_LARGE',
               f'package exceeds the {max_total_bytes} total byte limit')

    entry_meta = {'encoding': 'unknown', 'bom': False, 'newline_style': 'unknown'}
    warnings: list[str] = []
    try:
        _, entry_path, entry_snapshot = files._path(f'{source}/{_ENTRY_NAME}')
        entry_raw = files._raw(entry_path, min(max_file_bytes, max_total_bytes),
                               snapshot=entry_snapshot)
    except WorkspaceFileError as exc:
        _error(exc.code, f'{_ENTRY_NAME}: {exc}')
    except OSError as exc:
        _error('IO_ERROR', f'{_ENTRY_NAME}: cannot read ({type(exc).__name__})')
    try:
        entry_text = entry_raw.decode('utf-8-sig', errors='strict')
    except UnicodeError:
        _error('INVALID_UTF8', f'{_ENTRY_NAME}: entry must be valid UTF-8')
    endings = set(re.findall(r'\r\n|\r|\n', entry_text))
    newline_style = ('none' if not endings else 'mixed' if len(endings) > 1
                     else {'\r\n': 'CRLF', '\r': 'CR', '\n': 'LF'}[next(iter(endings))])
    entry_meta = {'encoding': 'utf-8', 'bom': entry_raw.startswith(b'\xef\xbb\xbf'),
                  'newline_style': newline_style}
    if not entry_meta['bom']:
        warnings.append(f'{_ENTRY_NAME} has no UTF-8 BOM; hosts may still differ in interpretation')
    if newline_style in ('CRLF', 'CR', 'mixed'):
        warnings.append(f'{_ENTRY_NAME} newline style is {newline_style}; bytes are hashed as stored')

    manifest = sorted(scanner.manifest, key=lambda entry: _sort_key(entry['path']))
    scanner.verify_unchanged()

    return {
        'schema_version': 1,
        'source': source,
        'name': source.split('/')[-1],
        'files': manifest,
        'total_bytes': total_bytes,
        'content_digest': hashlib.sha256(
            _canonical_json({'schema_version': 1, 'files': manifest})).hexdigest(),
        'digest_covers': 'paths_and_raw_bytes_only',
        'warnings': warnings,
        'execution_metadata': _describe_execution(manifest, entry_meta),
        'read_only': True,
        'installed': False,
        'discovered': 'unverified',
        'invoked': 'unverified',
        'limits': {'max_files': max_files, 'max_total_bytes': max_total_bytes,
                   'max_file_bytes': max_file_bytes, 'max_dir_bytes': max_dir_bytes},
    }
