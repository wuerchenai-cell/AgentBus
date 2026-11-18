"""Allowlisted team templates and offline friend packages.

Source: Codex AgentBus group implementation, 2026-10-02. Templates carry
product membership only. Local commands, capabilities, permissions and
credentials are deliberately never inferred from an imported template.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from .config import Config, validate

FORMAT = 'agentbus.team-template'
SCHEMA_VERSION = 1
IDENTITY = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
RESERVED = {'human', 'director'}
TOP_FIELDS = {'format', 'schema_version', 'coordinator', 'members'}
MEMBER_FIELDS = {'id', 'label'}


class ShareError(ValueError):
    """Malformed template, unsafe output or missing portable resources."""


def _identity(value: Any) -> str:
    from .profiles import validate_agent_id
    try:
        return validate_agent_id(value)
    except ValueError as exc:
        raise ShareError(f'成员 ID 不合法：{value!r}；{exc}') from exc


def _label(value: Any, identity: str) -> str:
    # Do not export accidental file paths, e-mail account names or multiline
    # instructions in a product display label. Invalid local labels fall back
    # to the product ID; imported labels are separately rejected below.
    if (not isinstance(value, str) or not value.strip() or len(value) > 64
            or any(c in value for c in '\\/:@\r\n\t')
            or any(ord(c) < 32 for c in value)):
        return identity
    return value.strip()


def _checked_template(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != TOP_FIELDS:
        raise ShareError('小组模板仅允许 format、schema_version、coordinator、members')
    if (payload['format'] != FORMAT or type(payload['schema_version']) is not int
            or payload['schema_version'] != SCHEMA_VERSION):
        raise ShareError('不支持此小组模板格式或 schema_version')
    raw_members = payload['members']
    if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= 64:
        raise ShareError('小组模板需要 1 至 64 位成员')
    members, seen = [], set()
    for member in raw_members:
        if not isinstance(member, dict) or set(member) != MEMBER_FIELDS:
            raise ShareError('模板成员只允许 id 与 label；不能导入命令、权限或认证')
        identity = _identity(member['id'])
        if identity in seen:
            raise ShareError(f'小组模板成员重复：{identity}')
        seen.add(identity)
        label = member['label']
        if _label(label, identity) != label:
            raise ShareError(f'成员 {identity} 的显示名称无效，不能包含路径、账号或控制字符')
        members.append({'id': identity, 'label': label})
    coordinator = _identity(payload['coordinator'])
    if coordinator not in seen:
        raise ShareError('组长必须是模板中选中的成员')
    return {'format': FORMAT, 'schema_version': SCHEMA_VERSION,
            'coordinator': coordinator, 'members': members}


def export_template(cfg: Config) -> dict[str, Any]:
    """Return a portable product-membership template, never local settings."""
    members = []
    for name, worker in cfg.enabled_workers().items():
        identity = _identity(name)
        # Display names are optional local profile metadata, not user identity.
        label = _label(worker.get('label', worker.get('display_name', identity)), identity)
        members.append({'id': identity, 'label': label})
    return _checked_template({'format': FORMAT, 'schema_version': SCHEMA_VERSION,
                              'coordinator': cfg.director, 'members': members})


def new_template_config(workspace: Path) -> Config:
    """Build a write-free first-import baseline with no accepted adapters.

    Source: relocated package test, Codex, 2026-10-02. DEFAULTS describe
    available built-in adapters; they are not this friend's local acceptance.
    Keep the ordinary permission and budget defaults, only clear automatic
    launch capability until local setup has actually been verified.
    """
    from .config import DEFAULTS
    workspace = Path(workspace).expanduser().resolve()
    data = copy.deepcopy(DEFAULTS)
    data['workspace'] = str(workspace)
    for worker in data['workers'].values():
        worker.update(kind='gui', can_spawn=False, human_in_loop=True)
        worker.pop('command', None)
        worker.pop('candidate_command', None)
    return Config(workspace / '.agentbus/config.json', workspace, data)


def import_template(cfg: Config, payload: dict, replace: bool = False) -> Config:
    """Prepare a new Config; caller persists it under a configuration lease.

    Existing local profiles retain commands, permissions and accepted adapter
    capabilities. New product IDs start as manual GUI members. Importing a
    template never grants execution permission or copies another user's paths.
    """
    template = _checked_template(payload)
    if cfg.path.exists() and not replace:
        raise ShareError('项目已有小组；确认替换选组后使用 replace=True，原任务仍保留')
    data = copy.deepcopy(cfg.data)
    workers = data.setdefault('workers', {})
    selected = {member['id'] for member in template['members']}
    for name, worker in workers.items():
        worker['enabled'] = name in selected
        worker['executor_enabled'] = name in selected
    for member in template['members']:
        name = member['id']
        worker = workers.setdefault(name, {'kind': 'gui', 'can_spawn': False,
                                          'human_in_loop': True})
        worker.update(enabled=True, executor_enabled=True, label=member['label'])
    data['director'] = template['coordinator']
    data['workspace'] = str(cfg.workspace)
    validate(data)
    return Config(path=cfg.path, workspace=cfg.workspace, data=data)


def _assets() -> Path:
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle:
        # An incomplete frozen distribution must not borrow developer files.
        return Path(bundle) / 'share_assets'
    return Path(__file__).resolve().parent.parent / 'share_assets'


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _portable_public_files(source: Path) -> dict[str, bytes]:
    # The source exporter is stdlib-only and is explicitly included in the
    # frozen app. Reuse its bounded reads and complete, fixed license contract.
    from tools.export_source import _read, _license_hashes, SourceReleaseError
    try:
        content = {name: _read(source, name)
                   for name in ('LICENSE', 'SOURCE_LICENSE_STATUS.md')}
        for name in ('README.md', 'RELEASE.md', 'ECOSYSTEM.md', 'SECURITY.md'):
            relative = 'docs/public/' + name
            content[relative] = _read(source, relative)
        for name in ('开始使用.md', 'THIRD_PARTY_NOTICES.md'):
            content[name] = _read(source, 'share_assets/' + name)
        provenance = _read(source, 'share_assets/licenses/provenance.json')
        hashes = _license_hashes(provenance)
        content['licenses/provenance.json'] = provenance
        for name, expected in hashes.items():
            blob = _read(source, 'share_assets/' + name)
            if hashlib.sha256(blob).hexdigest() != expected:
                raise ShareError(f'随包许可证校验失败：{name}')
            content[name] = blob
        return content
    except (OSError, SourceReleaseError) as exc:
        raise ShareError('公开分发资源校验失败；请使用完整 AgentBus 分发包') from exc


def build_portable_package(executable: Path, output_zip: Path, *,
                           asset_root: Path | None = None, template: dict | None = None) -> dict:
    """One allowlisted, verified writer for official and member-template exports."""
    executable, output_zip = Path(executable).resolve(), Path(output_zip).resolve()
    if not executable.is_file() or executable.suffix.lower() != '.exe':
        raise ShareError('找不到可分享的 AgentBus.exe；请使用冻结版的分享入口')
    if executable == output_zip or output_zip.suffix.lower() != '.zip':
        raise ShareError('分享输出必须是独立的 .zip 文件，不能覆盖启动程序')
    content = _portable_public_files(Path(asset_root) if asset_root is not None else _assets().parent)
    if template is not None:
        content['team-template.json'] = _json_bytes(_checked_template(template))
    hashes = {name: hashlib.sha256(blob).hexdigest() for name, blob in content.items()}
    hashes['AgentBus.exe'] = _file_hash(executable)
    manifest = {'format': 'agentbus.portable-package', 'schema_version': 1,
                'files': hashes, 'origin': 'AgentBus',
                'contents': ('程序、产品成员模板、公开说明与已收集的许可材料；无项目数据'
                             if template is not None else
                             '程序、公开说明与已收集的许可材料；无工程配置、任务或成员模板')}
    content['package-manifest.json'] = _json_bytes(manifest)
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.agentbus-share-', suffix='.zip', dir=output_zip.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(executable, arcname='AgentBus.exe')
            for name, blob in content.items():
                archive.writestr(name, blob)
        with zipfile.ZipFile(temporary) as archive:
            expected_names = set(content) | {'AgentBus.exe'}
            if (len(archive.namelist()) != len(expected_names)
                    or set(archive.namelist()) != expected_names or archive.testzip() is not None):
                raise OSError('ZIP content verification failed')
            expected_hashes = {**hashes, 'package-manifest.json':
                               hashlib.sha256(content['package-manifest.json']).hexdigest()}
            for name, expected in expected_hashes.items():
                digest = hashlib.sha256()
                with archive.open(name) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise OSError(f'ZIP content verification failed: {name}')
        if _file_hash(executable) != hashes['AgentBus.exe']:
            raise OSError('Formal executable changed while packaging')
        with Path(temporary).open('r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output_zip)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result = {'path': str(output_zip), 'sha256': _file_hash(output_zip),
              'files': ['AgentBus.exe', *content]}
    if template is not None:
        result['template'] = json.loads(content['team-template.json'])
    return result


def build_share_package(executable: Path, cfg: Config, output_zip: Path) -> dict:
    """Explicitly include only sanitized product membership, never local settings."""
    return build_portable_package(executable, output_zip, template=export_template(cfg))
