"""Extensible member profiles without accessing private host configuration.

Source: Codex extension_design, 2026-10-02.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from .lease import LeaseManager
from .member_adapter import GLOBAL_REGISTRY, MemberAdapter, MemberRegistry, DEFAULT_FILE_CAPABILITIES, READONLY_FILE_CAPABILITIES
from .public_files import PublicFileError, PublicFiles

if TYPE_CHECKING:
    from .config import Config

AGENTS = ('codex', 'workbuddy', 'trae', 'zcode', 'opencode', 'antigravity')
PROJECT_SKILLS = {
    'codex': '.agentbus/workflows/codex/SKILL.md',
    'workbuddy': '.codebuddy/skills/agentbus-team/SKILL.md',
    'trae': '.trae/skills/agentbus-team/SKILL.md',
    'zcode': '.zcode/skills/agentbus-team/SKILL.md',
    'opencode': '.opencode/skills/agentbus-team/SKILL.md',
    'antigravity': '.agentbus/workflows/antigravity/SKILL.md',
}
SYSTEM_AGENTS = {'human', 'director'}
WINDOWS_DEVICES = {'con', 'prn', 'aux', 'nul', 'clock$',
                   *(f'com{i}' for i in range(1, 10)),
                   *(f'lpt{i}' for i in range(1, 10))}


class ProfileError(ValueError):
    """Invalid member identity, path or registration request."""


def validate_agent_id(name: str, allow_system: bool = False) -> str:
    """A member ID is a portable single directory component, not a label."""
    if (not isinstance(name, str)
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name)
            or name in WINDOWS_DEVICES):
        raise ProfileError('成员标识需为1–64位小写字母/数字/短横线，以字母开头；不能是路径或系统设备名')
    if name in SYSTEM_AGENTS and not allow_system:
        raise ProfileError(f'{name!r} 是系统信箱，不能注册为小组成员')
    return name


def _safe_project_skill(root: Path, relative: str) -> str:
    if not isinstance(relative, str) or not relative.strip():
        raise ProfileError('成员工作流路径不能为空')
    win = PureWindowsPath(relative)
    normalized = relative.replace('\\', '/')
    path = Path(normalized)
    if (win.drive or win.root or path.is_absolute()
            or '..' in path.parts or path.name != 'SKILL.md'
            or any(part.casefold() == '.workbuddy' for part in path.parts)
            or ':' in normalized or any(ord(ch) < 32 for ch in normalized)):
        raise ProfileError(f'成员工作流必须是项目内 SKILL.md，相对路径不能越界或进入私有目录：{relative}')
    try:
        PublicFiles(root).checked(path)
    except PublicFileError as exc:
        raise ProfileError(str(exc)) from exc
    return path.as_posix()


def project_skill_paths(cfg: Config) -> dict[str, str]:
    """Return configured profiles, including disabled profiles for onboarding."""
    result: dict[str, str] = {}
    locations: set[str] = set()
    for name, worker in cfg.data.get('workers', {}).items():
        validate_agent_id(name)
        if not isinstance(worker, dict):
            raise ProfileError(f'成员 {name!r} 的配置必须是对象')
        # The optional legacy model API is not a host that can load a skill.
        if worker.get('kind') == 'http-llm':
            continue
        adapter = GLOBAL_REGISTRY.get(name)
        proposed = worker.get('project_skill') or (adapter.default_skill_path() if adapter else
                    f'.agentbus/workflows/{name}/SKILL.md')
        relative = _safe_project_skill(cfg.workspace, proposed)
        location = str((cfg.workspace / relative).resolve()).casefold()
        if location in locations:
            raise ProfileError(f'两个成员不能共用同一工作流文件：{relative}')
        locations.add(location)
        result[name] = relative
    return result


def adapters_for_project(cfg: Config) -> MemberRegistry:
    """Build only this project's public descriptors; runtime state is excluded."""
    registry = MemberRegistry(include_builtin=False)
    for name, path in project_skill_paths(cfg).items():
        worker = cfg.data['workers'][name]
        known = GLOBAL_REGISTRY.get(name)
        caps = worker.get('file_capabilities', known.file_capabilities if known else DEFAULT_FILE_CAPABILITIES)
        registry.register(MemberAdapter(
            member_id=name, display_name=worker.get('display_name') or name,
            file_capabilities=caps, wake_adapter=worker.get('wake_adapter', 'manual'),
            project_skill_path=path, host_version=worker.get('host_version'),
            model_id=worker.get('model_id')))
    return registry


def add_profile(cfg: Config, name: str, display_name: str, kind: str = 'gui',
                command: list[str] | None = None, transport: str = 'file', *,
                communication_mode: str | None = None, writable: bool = True,
                skill_path: str | None = None) -> Config:
    """Register a peer under a configuration lease; ACP starts as a candidate.

    Re-read the saved config after taking its lease to retain changes committed
    since the caller's snapshot. This method never launches the supplied argv.
    """
    from .config import Config, load, validate

    validate_agent_id(name)
    if type(writable) is not bool:
        raise ProfileError('文件写能力必须明确为布尔值')
    if communication_mode is not None:
        if transport != 'file' and transport != communication_mode:
            raise ProfileError('路线字段互相冲突')
        transport = communication_mode
    if (not isinstance(display_name, str) or not display_name.strip()
            or len(display_name) > 80 or any(ord(ch) < 32 for ch in display_name)):
        raise ProfileError('成员显示名需为1–80个可显示字符')
    if kind not in ('gui', 'acp'):
        raise ProfileError('新成员接入方式只支持 GUI 会话或待验收 ACP')
    if transport not in ('file', 'cli'):
        raise ProfileError('成员默认路线只能是 file 或 cli')
    if transport == 'cli':
        raise ProfileError('新成员必须先以 file 路线接入；CLI 需真实验收后再启用')
    if kind == 'acp' and (not isinstance(command, (list, tuple)) or not command
                         or any(not isinstance(x, str) or not x.strip()
                                or any(ord(ch) < 32 for ch in x) for x in command)):
        raise ProfileError('ACP 候选入口需是非空参数数组；不接受拼接的命令字符串')
    if kind == 'gui' and command is not None:
        raise ProfileError('GUI 成员无需填写 ACP 命令')

    try:
        public = PublicFiles(cfg.workspace)
        public.checked('.agentbus/config.json')
        public.checked('.agentbus/leases/.mutation.lock')
    except PublicFileError as exc:
        raise ProfileError(str(exc)) from exc
    root = public.root
    canonical = root / '.agentbus/config.json'
    if cfg.path.absolute() != canonical.absolute() or not canonical.is_file():
        raise ProfileError('请先为当前项目初始化并保存小组（执行：python -m agentbus init --workspace "<项目目录>"），不能在未配置项目中注册成员')
    manager = LeaseManager(root / '.agentbus')
    lease = manager.acquire('.agentbus/config.json', 'agentbus-profiles',
                            ttl_s=60, note=f'register peer {name}')
    if lease is None:
        raise ProfileError('小组配置正在由其他成员修改，请等待该操作完成')
    temporary = public.checked(f'.agentbus/config.json.profile-{uuid.uuid4().hex}.tmp')
    try:
        fresh = load(root)
        workers = fresh.data['workers']
        if name in workers:
            existing = workers[name]
            if isinstance(existing, dict) and not existing.get('enabled'):
                # 处于未启用状态的预置成员：正式激活加入团队
                existing['enabled'] = True
                existing['executor_enabled'] = True
                if display_name and display_name.strip() != name:
                    existing['display_name'] = display_name.strip()
            else:
                raise ProfileError(f'成员 {name!r} 已存在并已启用；注册不会覆盖其权限或设置')
        else:
            worker = {'display_name': display_name.strip(), 'enabled': True,
                      'executor_enabled': True, 'kind': kind,
                      'can_spawn': False, 'human_in_loop': True,
                      'transport': transport, 'communication_mode': transport,
                      'file_capabilities': list(DEFAULT_FILE_CAPABILITIES if writable else READONLY_FILE_CAPABILITIES)}
            if skill_path is not None:
                worker['project_skill'] = _safe_project_skill(root, skill_path)
            if kind == 'acp':
                worker['candidate_command'] = list(command)
            workers[name] = worker
        validate(fresh.data)
        project_skill_paths(fresh)
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(fresh.data, ensure_ascii=False, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        public.checked('.agentbus/config.json')
        os.replace(temporary, canonical)
        return Config(path=canonical, workspace=root, data=fresh.data)
    finally:
        temporary.unlink(missing_ok=True)
        manager.release(lease)
