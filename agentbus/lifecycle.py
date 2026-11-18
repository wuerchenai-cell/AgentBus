"""File-level workflow installation and safe removal; Codex, 2026-10-03.

The EXE is portable. This module manages text skills and launcher records only;
it never enumerates projects, deletes the EXE, or accesses host credentials.
"""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path
from . import skill_install as si


class LifecycleError(si.SkillInstallError):
    pass


def _locations(home=None, state_dir=None):
    explicit = home is not None
    home = si.canonical_path(home or Path.home())
    state = si.canonical_path(state_dir or si.default_state_dir(home=home))
    env = {} if explicit else None
    codex_roots = [si.canonical_path(p) for p in si.supported_skill_roots(env, home)]
    codex = si.canonical_path(si.resolve_skill_root(env, home))
    try:
        si.safe_install_path(state, state / 'install.json')
        record = si._read_json(state / 'install.json')
    except si.SkillInstallError:
        record = {}  # inspect reports unsafe state without dereferencing it.
    if record.get('skill_root') in {str(p) for p in codex_roots}:
        codex = Path(record['skill_root'])
    roots = {'codex': codex, 'workbuddy': home / '.codebuddy/skills',
             'trae': home / '.trae-cn/skills', 'zcode': home / '.zcode/skills',
             'opencode': home / '.config/opencode/skills'}
    return home, state, {host: si.canonical_path(root) for host, root in roots.items()}, codex_roots


def _manifest_path(state, host):
    return state / 'install.json' if host == 'codex' else state / 'peers' / host / 'install.json'


def _launcher():
    if getattr(sys, 'frozen', False):
        return si.frozen_launcher_record(sys.executable)
    return {'mode': 'source', 'python': str(Path(sys.executable).resolve()),
            'entrypoint': str(Path(__file__).resolve().parent.parent / 'entry.py')}


def install_workflows(version: str, *, home=None, state_dir=None) -> dict:
    """All five hosts under one lock; failure restores our previous exact bytes."""
    from .project_workflow import peer_skill
    import tempfile
    _, state, roots, codex_roots = _locations(home, state_dir)
    with si.installation_guard(state):
        return _install_locked(version, state, roots, codex_roots, peer_skill, tempfile)


def _install_locked(version, state, roots, codex_roots, peer_skill, tempfile):
    completed = []
    hosts = {}
    try:
        launcher = _launcher()
        with tempfile.TemporaryDirectory(prefix='agentbus-workflow-') as temp:
            peer_source = Path(temp) / 'SKILL.md'
            peer_source.write_text(peer_skill(), encoding='utf-8')
            for host, root in roots.items():
                skill = si.safe_install_path(root, root / si.SKILL_NAME / 'SKILL.md')
                manifest = si.safe_install_path(state, _manifest_path(state, host))
                previous = {p: p.read_bytes() if p.is_file() else None for p in (skill, manifest)}
                source = si.bundled_skill_source() if host == 'codex' else peer_source
                result = si.install_agentbus_skill(source, skill_root=root,
                    state_dir=manifest.parent, launcher=launcher, version=version,
                    supported_skill_roots=codex_roots if host == 'codex' else None)
                completed.extend((p, previous[p], p.read_bytes()) for p in previous)
                hosts[host] = {'skill': str(skill), 'manifest': str(manifest),
                               'changed': result.changed, 'native_verified': False}
            return {'schema_version': 1, 'status': 'installed', 'state_dir': str(state), 'hosts': hosts,
                    'native_loading': '文本安装完成；宿主发现和实际调用需单独验证'}
    except (OSError, si.SkillInstallError) as exc:
        rollback_errors = []
        for path, old, written in reversed(completed):
            try:
                if path.is_file() and path.read_bytes() == written:
                    if old is None:
                        path.unlink()
                    else:
                        si._atomic_write(path, old)
                else:
                    rollback_errors.append(f'回滚时文件已变化，已保留：{path}')
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        detail = ('；' + '；'.join(rollback_errors)) if rollback_errors else ''
        raise LifecycleError(f'工作流安装未完成，已回滚本次未被修改的输出：{exc}{detail}') from exc


def inspect_workflows(*, home=None, state_dir=None) -> dict:
    """Read only fixed host paths; manifest paths cannot extend this allowlist."""
    from .project_workflow import _launcher_status
    _, state, roots, _ = _locations(home, state_dir)
    hosts, errors, removable, preserved = {}, [], [], []
    for host, root in roots.items():
        skill = root / si.SKILL_NAME / 'SKILL.md'
        manifest = _manifest_path(state, host)
        entry = {'skill': str(skill), 'manifest': str(manifest), 'status': 'missing',
                 'owned': False, 'native_verified': False,
                 'launcher_ready': False, 'launcher_reason': '程序入口尚未登记',
                 'launcher_integrity': 'unrecorded'}
        hosts[host] = entry
        try:
            si.safe_install_path(root, skill)
            si.safe_install_path(state, manifest)
            if not manifest.exists():
                if skill.exists():
                    entry['status'] = 'unregistered'
                    preserved.append(str(skill))
                continue
            record = si._read_json(manifest)
            if not si.valid_install_record(record, root):
                raise LifecycleError(f'安装登记损坏或不属于此固定目录：{manifest}')
            try:
                int(record['skill_sha256'], 16)
                si._launcher_valid(record.get('launcher', {}))
            except (ValueError, TypeError, si.SkillInstallError) as exc:
                raise LifecycleError(f'安装登记字段无效：{manifest}') from exc
            entry['owned'] = record.get('owned') is True
            entry['launcher'] = record['launcher']
            launcher = record['launcher']
            ready, reason = _launcher_status(launcher)
            # The shared validator rejects private/invalid paths before stat or
            # reading. Strip only the digest to distinguish an absent program
            # from a present program whose recorded integrity has failed.
            entry['launcher_exists'] = _launcher_status(
                {key: value for key, value in launcher.items() if key != 'sha256'})[0]
            recorded_digest = launcher.get('mode') == 'frozen' and 'sha256' in launcher
            entry['launcher_ready'] = ready
            entry['launcher_integrity'] = ('verified' if ready else 'failed') if recorded_digest else 'unrecorded'
            entry['launcher_reason'] = reason or (
                ('文件与登记摘要一致' if recorded_digest else '程序入口文件存在；未登记完整性摘要')
                + '；检查未运行该程序，不代表认证或真实任务执行通过')
            if not skill.exists():
                entry['status'] = 'missing_file'
                if entry['owned']:
                    removable.append(str(manifest))
                else:
                    preserved.append(str(manifest))
            elif not skill.is_file():
                raise LifecycleError(f'技能目标不是普通文件：{skill}')
            elif hashlib.sha256(skill.read_bytes()).hexdigest() != record['skill_sha256']:
                entry['status'] = 'modified'
                preserved.extend((str(skill), str(manifest)))
            else:
                entry['status'] = 'installed'
                if entry['owned']:
                    removable.extend((str(skill), str(manifest)))
                else:
                    preserved.extend((str(skill), str(manifest)))
            if entry['status'] == 'installed' and not ready:
                entry['status'] = 'invalid_launcher' if entry['launcher_exists'] else 'missing_launcher'
        except (OSError, si.SkillInstallError) as exc:
            entry['status'] = 'invalid'
            errors.append(str(exc))
            preserved.extend((str(skill), str(manifest)))
    status = ('invalid' if errors else 'installed' if all(h['status'] == 'installed' for h in hosts.values())
              else 'not_installed' if all(h['status'] == 'missing' for h in hosts.values()) else 'partial')
    return {'schema_version': 1, 'status': status, 'state_dir': str(state), 'hosts': hosts,
            'removable': removable, 'preserved': preserved, 'errors': errors,
            'project_data': '不枚举或修改任何项目、任务、回执、配置和原有 AGENTS.md'}


def uninstall_workflows(*, home=None, state_dir=None, apply: bool = False) -> dict:
    """Preview by default. Remove only unchanged, explicitly owned fixed files."""
    def operation():
        value = inspect_workflows(home=home, state_dir=state_dir)
        value.update(applied=apply, removed=[])
        if not apply or value['errors']:
            return value
        # Recheck each exact path/hash under the same lifecycle lock. No recursive
        # deletion and no removal above the dedicated agentbus-team directory.
        for entry in value['hosts'].values():
            if not entry['owned'] or entry['status'] not in ('installed', 'missing_file', 'missing_launcher', 'invalid_launcher'):
                continue
            skill, manifest = Path(entry['skill']), Path(entry['manifest'])
            try:
                record = si._read_json(manifest)
                if skill.exists():
                    si.safe_install_path(skill.parent.parent, skill)
                    if hashlib.sha256(skill.read_bytes()).hexdigest() != record['skill_sha256']:
                        value['preserved'].extend((str(skill), str(manifest)))
                        continue
                    skill.unlink()
                    value['removed'].append(str(skill))
                manifest.unlink()
                value['removed'].append(str(manifest))
                try:
                    skill.parent.rmdir()  # Only if empty; user additions remain.
                except OSError:
                    pass  # 资源清理类：仅当目录为空时删除；非空（用户附加内容）会抛错，静默即代表保留。
            except (OSError, si.SkillInstallError) as exc:
                value['errors'].append(str(exc))
        # Keep action history, but report host ownership and launcher readiness
        # from disk after this removal, under the same lifecycle lock.
        current = inspect_workflows(home=home, state_dir=state_dir)
        value['hosts'] = current['hosts']
        value['removable'] = current['removable']
        for key in ('preserved', 'errors'):
            value[key] = list(dict.fromkeys((*value[key], *current[key])))
        value['status'] = 'partial' if value['preserved'] or value['errors'] else 'uninstalled'
        return value
    # Preview does not create even a lock file. A second removal of a missing
    # installation likewise needs no new directory or registry write.
    _, state, _, _ = _locations(home, state_dir)
    if not apply or not state.exists():
        return operation()
    try:
        with si.installation_guard(state):
            return operation()
    except (OSError, si.SkillInstallError) as exc:
        raise LifecycleError(str(exc)) from exc
