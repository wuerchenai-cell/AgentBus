"""Source: Codex, 2026-10-03. Export an explicit, reproducible source whitelist.

This standalone tool imports no AgentBus runtime and never walks a checkout.
The manifest hashes every payload file; it cannot hash itself recursively.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tempfile
import zipfile

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 1024
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
ROOT_FILES = (
    'entry.py', 'entry_tower.py', 'requirements-build.txt',
    'SOURCE_LICENSE_STATUS.md', '.gitignore',
)
PUBLIC_DOCS = ('README.md', 'ECOSYSTEM.md', 'RELEASE.md', 'SECURITY.md')
TOOL_FILES = ('export_source.py', 'check_offline.py', 'build_release.py', 'smoke_frozen.py',
              'handover_screen.py', 'skill_manifest_audit.py', 'check_formal_sync.py',
              'generate_build_manifest.py', 'delivery_self_proof.py', 'archived_binary_inventory.py',
              'deploy_formal_release.py')
# Explicit public test dependencies. No recursive tools/data discovery and no
# inference from arbitrary filenames supplied by a caller or source manifest.
# Minimal exporter fixtures need only dependencies of tests they actually ship.
AUDIT_TOOL_TESTS = {
    'build_dependency_audit.py': 'test_build_dependency_audit.py',
    'audit_mailbox_backlog.py': 'test_audit_mailbox_backlog.py',
    'artifact_drift_attribution.py': 'test_artifact_drift_attribution.py',
    'broad_except_locator.py': 'test_broad_except_locator.py',
    'cli_surface_check.py': 'test_audit_cli_cognitive_load.py',
    'host_file_registration_check.py': 'test_host_file_registration_check.py',
    'launcher_truth_check.py': 'test_launcher_truth_check.py',
    'shared_file_blast_radius.py': 'test_shared_file_blast_radius.py',
    'test_layer_budget.py': 'test_layer_budget.py',
    'tree_dedup_check.py': 'test_audit_tree_dedup.py',
    'tree_capability_audit.py': 'test_tree_capability_audit.py',
    'tree_structure_preflight.py': 'test_tree_structure_preflight.py',
    'tree_delivery_sweep.py': 'test_tree_delivery_sweep.py',
    'contrast_audit.py': 'test_contrast_audit.py',
    'evidence_backfill_index.py': 'test_evidence_backfill_index.py',
    'gen_broad_exception_triage.py': 'test_mutex_degradation_guard.py',
    'check_tools_quality.py': 'test_tools_quality_gate.py',
    'inspect_tree.py': 'test_tools_quality_gate.py',
}
CI_FILE = '.github/workflows/offline-tests.yml'
SKILL_FILE = 'skills/agentbus-team/SKILL.md'
REQUIRED_SOURCES = (
    'agentbus/__init__.py', 'agentbus/__main__.py',
    'schemas/plan.schema.json', 'schemas/review.schema.json',
    'schemas/continuation.schema.json',
)
LICENSE_NAMES = frozenset((
    'annotated-types-LICENSE.txt', 'codex-LICENSE.txt', 'codex-NOTICE.txt',
    'packaging-LICENSE.APACHE.txt', 'packaging-LICENSE.BSD.txt',
    'packaging-LICENSE.txt', 'pydantic-LICENSE.txt', 'pydantic-core-LICENSE.txt',
    'pyinstaller-COPYING.txt', 'python-LICENSE.txt', 'ratatui-LICENSE.txt',
    'ripgrep-LICENSE-MIT.txt', 'ripgrep-UNLICENSE.txt', 'tcl-license.terms',
    'tk-license.terms', 'typing-extensions-LICENSE.txt', 'typing-inspection-LICENSE.txt',
    'pywebview-LICENSE.txt', 'pythonnet-LICENSE.txt', 'clr-loader-LICENSE.txt',
    'cffi-LICENSE.txt', 'bottle-LICENSE.txt', 'proxy-tools-LICENSE.txt',
    'pycparser-LICENSE.txt', 'webview2-sdk-LICENSE.txt',
    'altgraph-LICENSE.txt',
    'pefile-LICENSE.txt',
    'pywin32-ctypes-LICENSE.txt',
    'setuptools-LICENSE.txt',
    'setuptools-config-NOTICE.txt',
    'setuptools-validate-pyproject-NOTICE.txt',
    'setuptools-vendor-autocommand-LICENSE.txt',
    'setuptools-vendor-backports-tarfile-LICENSE.txt',
    'setuptools-vendor-importlib-metadata-LICENSE.txt',
    'setuptools-vendor-jaraco-context-LICENSE.txt',
    'setuptools-vendor-jaraco-functools-LICENSE.txt',
    'setuptools-vendor-jaraco-text-LICENSE.txt',
    'setuptools-vendor-more-itertools-LICENSE.txt',
    'setuptools-vendor-packaging-LICENSE.APACHE.txt',
    'setuptools-vendor-packaging-LICENSE.BSD.txt',
    'setuptools-vendor-packaging-LICENSE.txt',
    'setuptools-vendor-platformdirs-LICENSE.txt',
    'setuptools-vendor-tomli-LICENSE.txt',
    'setuptools-vendor-wheel-LICENSE.txt',
    'setuptools-vendor-zipp-LICENSE.txt',
))
LICENSE_FILES = tuple(f'licenses/{name}' for name in sorted(LICENSE_NAMES))
PROVENANCE_FILE = 'share_assets/licenses/provenance.json'
MANIFEST_FILE = 'source-manifest.json'
WEB_ASSET_FILES = ('agentbus/web_assets/index.html', 'agentbus/web_assets/app.css',
                   'agentbus/web_assets/app.js')
OPTIONAL_WEB_ASSET_FILES = ('agentbus/web_assets/trigger_onboarding.js',)
APP_ICON_FILE = 'agentbus/assets/agentbus.ico'


class SourceReleaseError(ValueError):
    """The candidate cannot be exported under the source release contract."""


def _validate_ico(blob: bytes) -> bool:
    if len(blob) < 22 or blob[:4] != b'\x00\x00\x01\x00':
        return False
    count = struct.unpack_from('<H', blob, 4)[0]
    directory_end = 6 + count * 16
    if count < 1 or directory_end > len(blob):
        return False
    for index in range(count):
        _width, _height, _colors, reserved, _planes, _bits, size, offset = struct.unpack_from(
            '<BBBBHHII', blob, 6 + index * 16)
        if (reserved != 0 or size <= 0 or offset < directory_end
                or offset + size > len(blob)):
            return False
    return True


def _is_link(info) -> bool:
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)))


def _clean_stat(path: Path, label: str, *, directory: bool = False):
    try:
        info = path.lstat()
    except OSError as exc:
        raise SourceReleaseError(f'Missing or unreadable required path: {label}') from exc
    if _is_link(info):
        raise SourceReleaseError(f'Links and reparse points are forbidden: {label}')
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        kind = 'directory' if directory else 'regular file'
        raise SourceReleaseError(f'Required path must be a {kind}: {label}')
    return info


def _reject_private_paths(*paths: Path) -> None:
    # Include UNC share components, which pathlib folds into a single anchor.
    if any(part.casefold() == '.workbuddy' for path in paths
           for part in os.fspath(path).replace('\\', '/').split('/')):
        raise SourceReleaseError('Private WorkBuddy directories cannot be accessed')


def validate_source_root(root: Path) -> Path:
    """Source: Codex, 2026-10-04. Check every ancestor without following links.

    Only directory metadata is inspected, from the filesystem anchor to the
    checkout. Reject literal private components before abspath can remove them.
    """
    supplied = Path(root)
    _reject_private_paths(supplied)
    root = Path(os.path.abspath(os.fspath(supplied)))
    _reject_private_paths(root)
    for path in reversed((root, *root.parents)):
        label = 'source root' if path == root else 'source root ancestor'
        _clean_stat(path, label, directory=True)
    return root


def _safe_relative(relative: str) -> tuple[str, ...]:
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or ':' in relative or any(ord(char) < 32 for char in relative)):
        raise SourceReleaseError('Unsafe relative source path')
    value = PurePosixPath(relative)
    parts = tuple(relative.split('/'))
    if (value.is_absolute() or any(part in ('', '.', '..') for part in parts)
            or any(part.casefold() == '.workbuddy' for part in parts)):
        raise SourceReleaseError('Unsafe relative source path')
    return parts


def _source_path(root: Path, relative: str, *, directory: bool = False):
    parts = _safe_relative(relative)
    root = validate_source_root(root)
    path = root
    for index, part in enumerate(parts):
        path = path / part
        info = _clean_stat(path, '/'.join(parts[:index + 1]),
                           directory=index < len(parts) - 1 or directory)
    return path, info


def _read(root: Path, relative: str) -> bytes:
    path, before = _source_path(root, relative)
    if before.st_size > MAX_FILE_BYTES:
        raise SourceReleaseError(f'File exceeds {MAX_FILE_BYTES} bytes: {relative}')
    try:
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            _, current = _source_path(root, relative)
            if (_is_link(opened) or not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise SourceReleaseError(f'Source changed while opening: {relative}')
            blob = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise SourceReleaseError(f'Cannot read required source: {relative}') from exc
    if len(blob) > MAX_FILE_BYTES:
        raise SourceReleaseError(f'File exceeds {MAX_FILE_BYTES} bytes: {relative}')
    if (after.st_size != len(blob) or before.st_size != len(blob)
            or after.st_mtime_ns != before.st_mtime_ns):
        raise SourceReleaseError(f'Source changed while reading: {relative}')
    return blob


def _flat_sources(root: Path, directory: str, suffix: str) -> list[str]:
    path, _ = _source_path(root, directory, directory=True)
    found = []
    try:
        # Read names from this one known directory. Never descend into entries,
        # including __pycache__, private histories, and unfamiliar subdirectories.
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.endswith(suffix):
                    relative = f'{directory}/{entry.name}'
                    _source_path(root, relative)
                    found.append(relative)
                    if len(found) > MAX_FILES:
                        raise SourceReleaseError('Source file count exceeds release limit')
    except OSError as exc:
        raise SourceReleaseError(f'Cannot list required source directory: {directory}') from exc
    if not found:
        raise SourceReleaseError(f'Required source directory is empty: {directory}')
    return sorted(found)


def _license_hashes(blob: bytes) -> dict[str, str]:
    def unique_object(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SourceReleaseError('Duplicate key in license provenance')
            result[key] = value
        return result

    try:
        provenance = json.loads(blob.decode('utf-8'), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SourceReleaseError('License provenance must be valid UTF-8 JSON') from exc
    if (not isinstance(provenance, dict)
            or set(provenance) != {'schema_version', 'collected_at', 'coverage', 'files'}
            or type(provenance['schema_version']) is not int
            or provenance['schema_version'] != 1
            or not isinstance(provenance['coverage'], str)
            or not provenance['coverage'].strip()
            or not isinstance(provenance['collected_at'], str)
            or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', provenance['collected_at'])):
        raise SourceReleaseError('Invalid license provenance schema')
    try:
        date.fromisoformat(provenance['collected_at'])
    except ValueError as exc:
        raise SourceReleaseError('Invalid license provenance date') from exc
    items = provenance['files']
    if not isinstance(items, list) or len(items) != len(LICENSE_NAMES):
        raise SourceReleaseError('License provenance must contain the complete fixed license set')
    expected = set(LICENSE_FILES)
    hashes = {}
    # Validate the entire list before opening any provenance-selected resource.
    for item in items:
        if (not isinstance(item, dict)
                or set(item) != {'path', 'component', 'version', 'source', 'sha256'}
                or any(not isinstance(item[key], str) or not item[key].strip()
                       for key in ('path', 'component', 'version', 'source', 'sha256'))):
            raise SourceReleaseError('Invalid license provenance item')
        relative = item['path']
        _safe_relative(relative)
        if relative not in expected or relative in hashes:
            raise SourceReleaseError('License provenance contains unknown or duplicate files')
        if not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise SourceReleaseError(f'Invalid license SHA256: {relative}')
        hashes[relative] = item['sha256']
    if set(hashes) != expected:
        raise SourceReleaseError('License provenance does not match the fixed license set')
    return hashes


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _root_readme(blob: bytes) -> bytes:
    """Adapt the four known links when the public README moves to the ZIP root."""
    for name in ('ECOSYSTEM.md', 'RELEASE.md', 'SECURITY.md'):
        blob = blob.replace(f']({name})'.encode(), f'](docs/public/{name})'.encode())
    return blob.replace(b'](../../SOURCE_LICENSE_STATUS.md)', b'](SOURCE_LICENSE_STATUS.md)')


def _destination(output: Path) -> None:
    if output.suffix.casefold() != '.zip':
        raise SourceReleaseError('Source output must be a .zip file')
    # Do not follow a destination link, including a link in any parent directory.
    for path in reversed((output, *output.parents)):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SourceReleaseError('Cannot inspect source output destination') from exc
        if _is_link(info):
            raise SourceReleaseError('Output destination cannot contain links or reparse points')
        if not (stat.S_ISREG(info.st_mode) if path == output else stat.S_ISDIR(info.st_mode)):
            raise SourceReleaseError('Invalid source output destination')


def export_source(root: Path, output: Path) -> dict:
    """Validate the explicit whitelist, then atomically replace a source ZIP.

    Unknown directories are neither listed nor read. Missing or invalid inputs
    leave an existing output intact. Returned metadata includes the output path;
    the archive and its manifest contain only relative names.
    """
    root, output = Path(root), Path(output)
    _reject_private_paths(root, output)
    root = validate_source_root(root)
    output = Path(os.path.abspath(os.fspath(output)))
    _reject_private_paths(output)
    _destination(output)
    content: dict[str, bytes] = {}
    folded = set()
    total = 0

    def add(relative: str, target: str | None = None, blob: bytes | None = None):
        nonlocal total
        target = target or relative
        _safe_relative(target)
        if target.casefold() in folded:
            raise SourceReleaseError(f'Duplicate portable archive name: {target}')
        data = _read(root, relative) if blob is None else blob
        if len(data) > MAX_FILE_BYTES:
            raise SourceReleaseError(f'File exceeds {MAX_FILE_BYTES} bytes: {target}')
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise SourceReleaseError(f'Source total exceeds {MAX_TOTAL_BYTES} bytes')
        if len(content) >= MAX_FILES:
            raise SourceReleaseError('Source file count exceeds release limit')
        content[target] = data
        folded.add(target.casefold())

    for relative in ROOT_FILES:
        add(relative)
    # Include the chosen project license when present; older source fixtures
    # without a license retain their original behavior.
    for optional in ('LICENSE', '.gitattributes'):
        if os.path.lexists(root / optional):
            add(optional)
    for directory, suffix in (('agentbus', '.py'), ('tests', '.py'), ('schemas', '.json')):
        for relative in _flat_sources(root, directory, suffix):
            add(relative)
    for relative in REQUIRED_SOURCES:
        if relative not in content:
            raise SourceReleaseError(f'Missing required source resource: {relative}')
    # Old source fixtures remain valid; the new desktop can never be exported
    # without its complete, explicit asset whitelist. Unknown assets are ignored.
    if 'agentbus/web_desktop.py' in content:
        for relative in WEB_ASSET_FILES:
            add(relative)
        for relative in OPTIONAL_WEB_ASSET_FILES:
            if (root / relative).is_file():
                add(relative)
        try:
            icon = _read(root, APP_ICON_FILE)
        except SourceReleaseError as exc:
            raise SourceReleaseError('Missing required application icon: ' + APP_ICON_FILE) from exc
        if not _validate_ico(icon):
            raise SourceReleaseError('Invalid required application icon: ' + APP_ICON_FILE)
        add(APP_ICON_FILE, blob=icon)
    if (root / 'agentbus/trigger').is_dir():
        for directory, suffix in (('agentbus/trigger', '.py'),
                                  ('agentbus/trigger/connectors', '.py'),
                                  ('agentbus/trigger/manifests', '.json')):
            if (root / directory).is_dir():
                for relative in _flat_sources(root, directory, suffix):
                    add(relative)
    if (root / 'agentbus/host_assets/codearts').is_dir():
        for relative in _flat_sources(root, 'agentbus/host_assets/codearts', '.js'):
            add(relative)
    add(SKILL_FILE)
    for name in PUBLIC_DOCS:
        add(f'docs/public/{name}')
    add('docs/public/README.md', 'README.md', _root_readme(content['docs/public/README.md']))
    for name in TOOL_FILES:
        add(f'tools/{name}')
    for name, consumer in AUDIT_TOOL_TESTS.items():
        relative = f'tools/{name}'
        if f'tests/{consumer}' in content or os.path.lexists(root / relative):
            add(relative)  # Missing required input or any link fails closed.
    if 'tests/test_tools_quality_gate.py' in content:
        add('tools/README.md')
    if 'tests/test_workbench_invariants.py' in content:
        fixture = 'tests/data/workbench_contract_surface.json'
        blob = _read(root, fixture)
        try:
            json.loads(blob.decode('utf-8'))
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise SourceReleaseError('Invalid public workbench test fixture JSON') from exc
        add(fixture, blob=blob)
    add(CI_FILE)
    for name in ('开始使用.md', 'THIRD_PARTY_NOTICES.md'):
        add(f'share_assets/{name}')
    provenance_blob = _read(root, PROVENANCE_FILE)
    hashes = _license_hashes(provenance_blob)
    add(PROVENANCE_FILE, blob=provenance_blob)
    for relative, expected_hash in sorted(hashes.items()):
        blob = _read(root, f'share_assets/{relative}')
        if hashlib.sha256(blob).hexdigest() != expected_hash:
            raise SourceReleaseError(f'License hash mismatch: {relative}')
        add(f'share_assets/{relative}', blob=blob)

    manifest = {
        'format': 'agentbus.source-package', 'schema_version': 1,
        'zip_timestamp': '1980-01-01T00:00:00',
        'timestamp_policy': 'fixed metadata for reproducible archives; source dates are retained in files',
        'source_license_status': 'See SOURCE_LICENSE_STATUS.md and LICENSE when present',
        'files': {name: {'sha256': hashlib.sha256(blob).hexdigest(), 'size': len(blob)}
                  for name, blob in sorted(content.items())},
    }
    content[MANIFEST_FILE] = _json_bytes(manifest)
    temporary = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        _destination(output)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='.agentbus-source-', suffix='.tmp', dir=output.parent)
        temporary = Path(temporary_name)
        os.close(descriptor)
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            for name, blob in sorted(content.items()):
                info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, blob, compresslevel=9)
        with temporary.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        os.replace(temporary, output)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise SourceReleaseError('Could not atomically write source archive; previous output retained') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'path': str(output), 'sha256': digest, 'file_count': len(content),
            'files': sorted(content), 'manifest': manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='AgentBus source checkout')
    parser.add_argument('--output', type=Path, required=True, help='Destination source ZIP')
    args = parser.parse_args(argv)
    try:
        result = export_source(args.root, args.output)
    except SourceReleaseError as exc:
        print(f'Source export rejected: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
