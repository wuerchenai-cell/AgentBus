"""Install the AgentBus Codex skill without overwriting unrelated user data."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

SKILL_NAME = "agentbus-team"
SKILL_VERSION = 1
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_ACTIVE = threading.local()


class SkillInstallError(RuntimeError):
    """An installation conflict or invalid launcher setup."""


@dataclass(frozen=True)
class SkillInstallResult:
    skill_file: Path
    manifest_file: Path
    skill_sha256: str
    changed: bool


def supported_skill_roots(environ: Mapping[str, str] | None = None,
                          home: Path | str | None = None) -> list[Path]:
    env = os.environ if environ is None else environ
    user_home = Path(home or Path.home()).expanduser()
    roots: list[Path] = []
    codex_home = str(env.get("CODEX_HOME", "")).strip()
    if codex_home:
        roots.append(Path(codex_home).expanduser() / "skills")
    roots.extend((user_home / ".codex" / "skills",
                  user_home / ".agents" / "skills"))
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        normalized = str(root.resolve()).casefold()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(root)
    return unique


def resolve_skill_root(environ: Mapping[str, str] | None = None,
                       home: Path | str | None = None) -> Path:
    """Choose the active user skill root, honoring CODEX_HOME first."""
    roots = supported_skill_roots(environ, home)
    env = os.environ if environ is None else environ
    if str(env.get("CODEX_HOME", "")).strip():
        return roots[0]
    # Prefer a directory already populated by this Codex runtime. On this host
    # the desktop runtime currently loads ~/.codex/skills; new installs without
    # that directory use the documented ~/.agents/skills location.
    for root in roots[:-1]:
        if root.is_dir():
            try:
                if next(root.iterdir(), None) is not None:
                    return root
            except OSError:
                continue
    return roots[-1]


def default_state_dir(environ: Mapping[str, str] | None = None,
                      home: Path | str | None = None) -> Path:
    env = os.environ if environ is None else environ
    user_home = Path(home or Path.home()).expanduser()
    if os.name == "nt":
        # Packaged Codex processes may redirect LOCALAPPDATA into their private
        # package cache. Keep the launcher manifest in the shared per-user
        # LocalAppData directory so Codex and AgentBus.exe resolve one record.
        return user_home / "AppData" / "Local" / "AgentBus"
    local = str(env.get("LOCALAPPDATA", "")).strip()
    if local:
        return Path(local).expanduser() / "AgentBus"
    return user_home / ".agentbus" / "launcher"


def bundled_skill_source() -> Path:
    """Return the packaged skill resource or the source-checkout copy."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundled = Path(bundle_root) / "skills" / SKILL_NAME / "SKILL.md"
        if bundled.is_file():
            return bundled
    return (Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME
            / "SKILL.md")


def frozen_launcher_record(executable: Path | str) -> dict[str, str]:
    """Register a new frozen program with bounded reads; never start it.

    Codex, 2026-10-03: user-level bootstrapping needs the same recorded
    integrity as a newly saved project. Legacy records remain unchanged.
    """
    if (not isinstance(executable, (str, Path)) or not str(executable).strip()
            or any(ord(char) < 32 for char in str(executable))):
        raise SkillInstallError('新安装的 EXE 入口路径无效，请打开正确 EXE 后重新安装工作流')
    path = Path(executable)
    if (not path.is_absolute()
            or any(part.casefold() == '.workbuddy' for part in path.parts)):
        raise SkillInstallError('新安装的 EXE 入口必须是非私有目录中的完整文件路径')
    try:
        resolved = path.resolve()
        if (any(part.casefold() == '.workbuddy' for part in resolved.parts)
                or not resolved.is_file()):
            raise SkillInstallError('新安装的 EXE 入口文件不存在或不能安全访问')
        digest = hashlib.sha256()
        with resolved.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except (OSError, ValueError, TypeError) as exc:
        raise SkillInstallError('新安装的 EXE SHA256 无法安全读取，请打开正确 EXE 后重新安装工作流') from exc
    return {'mode': 'frozen', 'executable': str(resolved), 'sha256': digest.hexdigest()}


def install_for_current_user(version: str) -> SkillInstallResult:
    """Install the bundled skill and a stable launcher for this user account."""
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        launcher = frozen_launcher_record(sys.executable)
    else:
        entrypoint = Path(__file__).resolve().parent.parent / "entry.py"
        launcher = {
            "mode": "source",
            "python": str(Path(sys.executable).resolve()),
            "entrypoint": str(entrypoint.resolve()),
        }
    env = os.environ
    home = Path.home()
    roots = supported_skill_roots(env, home)
    return install_agentbus_skill(
        bundled_skill_source(), skill_root=resolve_skill_root(env, home),
        state_dir=default_state_dir(env, home), launcher=launcher,
        version=version, supported_skill_roots=roots)


def install_peer_skills_for_current_user(version: str, *, home: Path | None = None,
                                        state_dir: Path | None = None) -> dict:
    """Install text skills only; never touch .workbuddy or host settings.

    CodeBuddy's bundled runtime documents .codebuddy skills; WorkBuddy may
    override its home, so its project skill / explicit file entry is retained.
    Installed means written, not proof of native discovery.
    """
    from .project_workflow import peer_skill
    user_home = Path(home or Path.home()).resolve()
    state = Path(state_dir or default_state_dir(home=user_home))
    record = _read_json(state / 'install.json')
    launcher = record.get('launcher', {})
    _launcher_valid(launcher)
    targets = {'workbuddy': user_home / '.codebuddy' / 'skills',
               'trae': user_home / '.trae-cn' / 'skills',
               'zcode': user_home / '.zcode' / 'skills',
               'opencode': user_home / '.config' / 'opencode' / 'skills'}
    # No persistent shared source or private host data needs to be read.
    import tempfile
    results = {}
    with tempfile.TemporaryDirectory(prefix='agentbus-skill-') as temp:
        source = Path(temp) / 'SKILL.md'
        source.write_text(peer_skill(), encoding='utf-8')
        for agent, target in targets.items():
            try:
                result = install_agentbus_skill(
                    source, skill_root=target, state_dir=state / 'peers' / agent,
                    launcher=launcher, version=version)
                results[agent] = {'skill': str(result.skill_file), 'written': True,
                                 'native_verified': False}
            except (SkillInstallError, OSError) as exc:
                results[agent] = {'written': False, 'error': str(exc)}
    return results


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_path(path: Path | str) -> Path:
    """Normalize Windows device spelling returned during directory races."""
    value = str(Path(path).expanduser().resolve())
    if os.name == 'nt':
        if value.casefold().startswith('\\\\?\\unc\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
    return Path(value)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.agentbus-install-', dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_install_path(root: Path, target: Path) -> Path:
    """Trusted root may be relocated; its managed children must not be links."""
    root = canonical_path(root)
    if any(part.casefold() == '.workbuddy' for part in root.parts):
        raise SkillInstallError('安装目录指向 WorkBuddy 私有目录，已停止')
    if not target.absolute().is_relative_to(root):
        raise SkillInstallError(f'安装路径越出登记目录：{target}')
    current = target
    while current != root:
        attributes = getattr(current.lstat(), 'st_file_attributes', 0) if current.exists() else 0
        if (current.is_symlink() or getattr(current, 'is_junction', lambda: False)()
                or attributes & 0x400):
            raise SkillInstallError(f'安装路径包含链接，已保留：{current}')
        current = current.parent
    if not canonical_path(target).is_relative_to(root):
        raise SkillInstallError(f'安装路径通过链接越界：{target}')
    return target


@contextmanager
def installation_guard(state: Path):
    """Bounded, reentrant process/thread lock shared by install and uninstall."""
    state = canonical_path(state)
    key = str(state).casefold()
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=10):
        raise SkillInstallError('另一个安装/卸载操作仍在运行，请稍后重试')
    active = getattr(_ACTIVE, 'states', None)
    if active is None:
        active = _ACTIVE.states = set()
    if key in active:
        try:
            yield
        finally:
            lock.release()
        return
    fd = None
    locked = False
    try:
        state.mkdir(parents=True, exist_ok=True)
        lock_path = safe_install_path(state, state / '.lifecycle.lock')
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + 10
        while not locked:
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise SkillInstallError(f'另一个安装/卸载操作仍在运行，请稍后重试：{state}')
                time.sleep(.02)
        active.add(key)
        yield
    finally:
        active.discard(key)
        if fd is not None:
            if locked:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        lock.release()


def valid_install_record(record: dict, root: Path) -> bool:
    return (record.get('owner') == 'agentbus'
            and type(record.get('schema_version')) is int
            and record['schema_version'] == 1
            and record.get('skill_name') == SKILL_NAME
            and record.get('skill_root') == str(root)
            and isinstance(record.get('skill_sha256'), str)
            and len(record['skill_sha256']) == 64)


def transactional_write(outputs: Mapping[Path, bytes]) -> None:
    """Restore only our exact writes on failure, leaving concurrent edits intact."""
    old = {path: path.read_bytes() if path.is_file() else None for path in outputs}
    written = []
    try:
        for path, data in outputs.items():
            if old[path] != data:
                # Record the attempted path before replace: a writer can raise
                # after it has already published the new bytes.
                written.append(path)
                _atomic_write(path, data)
    except Exception:
        for path in reversed(written):
            if path.is_file() and path.read_bytes() == outputs[path]:
                if old[path] is None:
                    path.unlink()
                else:
                    _atomic_write(path, old[path])
        raise


def _launcher_valid(launcher: Mapping[str, str]) -> None:
    if not isinstance(launcher, Mapping):
        raise SkillInstallError('AgentBus 启动信息不是有效对象')
    mode = launcher.get("mode")
    required = ("executable",) if mode == "frozen" else (
        ("python", "entrypoint") if mode == "source" else ())
    if not required or any(not str(launcher.get(key, "")).strip()
                           for key in required):
        raise SkillInstallError("AgentBus 启动信息不完整；请从 AgentBus 安装入口重试")


def install_agentbus_skill(
    source: Path | str,
    *,
    skill_root: Path | str,
    state_dir: Path | str,
    launcher: Mapping[str, str],
    version: str,
    supported_skill_roots: list[Path | str] | None = None,
) -> SkillInstallResult:
    """Install/update only an AgentBus-owned skill and user-level launcher."""
    with installation_guard(Path(state_dir)):
        return _install_agentbus_skill(source, skill_root=skill_root,
            state_dir=state_dir, launcher=launcher, version=version,
            supported_skill_roots=supported_skill_roots)


def _install_agentbus_skill(source: Path | str, *, skill_root: Path | str,
                           state_dir: Path | str, launcher: Mapping[str, str],
                           version: str,
                           supported_skill_roots: list[Path | str] | None = None
                           ) -> SkillInstallResult:
    source_path = Path(source)
    if not source_path.is_file():
        raise SkillInstallError(f"找不到随包提供的 Codex skill：{source_path}")
    _launcher_valid(launcher)
    content = source_path.read_bytes()
    digest = _sha256(content)
    root = canonical_path(skill_root)
    state = canonical_path(state_dir)
    skill_dir = root / SKILL_NAME
    skill_file = skill_dir / "SKILL.md"
    manifest_file = state / "install.json"
    safe_install_path(root, skill_file)
    safe_install_path(state, manifest_file)
    old_manifest = _read_json(manifest_file)
    if manifest_file.exists() and not valid_install_record(old_manifest, root):
        raise SkillInstallError(f'安装登记损坏或归属不符，已保留：{manifest_file}')

    roots = ([canonical_path(p)
              for p in supported_skill_roots]
             if supported_skill_roots is not None else [root])
    for other_root in roots:
        if other_root == root:
            continue
        duplicate = other_root / SKILL_NAME / "SKILL.md"
        if duplicate.exists():
            raise SkillInstallError(
                f"另一个 Codex skill 目录已存在 {duplicate}；为避免重复发现，没有安装")

    existing = skill_file.read_bytes() if skill_file.is_file() else None
    # Explicit install/update may replace a registered, unchanged package
    # revision. Deletion ownership is a separate contract: a pre-existing
    # identical skill remains owned=False even after this permitted upgrade.
    recorded_content_matches = (
        valid_install_record(old_manifest, root)
        and old_manifest.get("skill_sha256") == (_sha256(existing) if existing else None))
    if existing is not None and existing != content and not recorded_content_matches:
        raise SkillInstallError(
            f"检测到未登记或被修改过的同名 skill，已保留原文件：{skill_file}")
    if existing is None and skill_dir.exists() and any(skill_dir.iterdir()):
        raise SkillInstallError(
            f"同名 skill 目录已有其他文件，未覆盖：{skill_dir}")

    changed = False
    if existing != content:
        changed = True

    manifest = {
        "schema_version": 1,
        "owner": "agentbus",
        "version": version,
        "skill_name": SKILL_NAME,
        "skill_root": str(root),
        "skill_sha256": digest,
        # Identical pre-existing unregistered files are usable, but not ours to
        # delete. Legacy manifests lack provenance, so deletion remains opt-in.
        "owned": (existing is None or old_manifest.get('owned') is True),
        "launcher": dict(launcher),
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2)
                      + "\n").encode("utf-8")
    if not manifest_file.is_file() or manifest_file.read_bytes() != manifest_bytes:
        changed = True
    transactional_write({skill_file: content, manifest_file: manifest_bytes})

    return SkillInstallResult(skill_file, manifest_file, digest, changed)
