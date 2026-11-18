"""Small per-user state shared by the AgentBus desktop entry points."""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from .config import Config, ConfigError, save, validate


_STATE_LOCK = threading.RLock()
_BRANCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)

# §7.29 线程锁挡不住**两个独立 EXE 进程**：它们各自持有自己的 RLock，
# 于是"读→改→写"可交错，后写者覆盖先写者（丢更新）。这里再加一层**进程间**
# 互斥（同目录 .lock 文件），与原子 os.replace 配合：
#     进程内串行 + 进程间串行 + 替换原子  =  不丢更新，且不会读到半截文件。
LOCK_TIMEOUT_S = 5.0

if os.name == "nt":
    import msvcrt

    def _lock_acquire(handle, deadline: float) -> None:
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _lock_release(handle) -> None:
        # 释放失败无需上报：句柄可能已关闭或已失效。
        # 用 suppress 而非 try/except-pass，明确这是「有意忽略这一种错误」。
        with contextlib.suppress(OSError):
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:                                          # POSIX
    import fcntl

    def _lock_acquire(handle, deadline: float) -> None:
        # 非阻塞 + 期限轮询：与 Windows 分支保持同一套「总期限」语义。
        # （若直接 flock(LOCK_EX) 会无限阻塞，锁时限就无法覆盖加锁这一步。）
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _lock_release(handle) -> None:
        # 同 Windows 分支：释放失败无需上报。
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StateLockUnavailable(RuntimeError):
    """本机偏好状态文件的进程间锁在期限内不可用。"""


_LAST_LOCK_ERROR = {"reason": ""}


def last_lock_error() -> str:
    """最近一次锁获取失败的可解释原因；空串表示最近一次获取成功。

    供诊断与测试使用：锁失败不再只表现为"保存悄悄没生效"。
    """
    return _LAST_LOCK_ERROR["reason"]


def _acquire_interprocess_lock(lock_path: Path, deadline: float):
    """在**同一个总期限**内完成「打开锁文件 + 取得文件锁」。

    §13.11：原实现把 `_open_lock_handle`（5s）与 `_lock_acquire`（5s）各自计时，
    总获取时间最长可达 10s，与调用方设定的锁时限不一致。这里两者共用一个 `deadline`。

    返回已持锁的 handle；期限用尽则返回 None（**不由本函数决定是否降级**，
    降级与否交给调用方，避免"锁坏了"被静默当成"锁没问题"）。
    """
    while True:
        handle = None
        try:
            os.close(os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600))
            handle = open(lock_path, "r+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")            # 字节区间锁要求文件至少 1 字节
                handle.flush()
            _lock_acquire(handle, deadline)
            handle.seek(0)
            return handle
        except OSError:
            if handle is not None:
                # 期限已到或句柄已失效；关闭失败不应掩盖主因。
                with contextlib.suppress(OSError):
                    handle.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)


@contextlib.contextmanager
def _interprocess_lock(state_file: Path):
    """跨进程串行化对 `state_file` 的"读→改→写"；**拿不到锁就不进入临界区**。

    §13.11：锁失败**不得退化为无锁保存**。做法是把"是否持锁"显式交给调用方——
    本上下文管理器 **yield 一个布尔值**；为 False 时调用方必须直接返回失败，
    绝不可继续读改写。这与 §7.29 修复前的行为有本质区别：
    那时 `except OSError: handle = None` 之后**照样 yield**，
    于是"没锁上"与"锁上了"在调用方看来完全一样，只能靠"丢更新"这种事后现象发现。

    §7.29 的成果保留：打开锁文件失败要重试（首次创建竞态），
    且**总期限覆盖「打开 + 加锁」全过程**（见 `_acquire_interprocess_lock`）。
    """
    handle = None
    reason = ""
    try:
        lock_path = state_file.with_name(state_file.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = _acquire_interprocess_lock(lock_path, time.monotonic() + LOCK_TIMEOUT_S)
        if handle is None:
            reason = f"锁在 {LOCK_TIMEOUT_S:g}s 内不可用：{lock_path}"
    except OSError as exc:
        reason = f"{type(exc).__name__}: {exc}"
        handle = None
    _LAST_LOCK_ERROR["reason"] = reason
    try:
        yield handle is not None
    finally:
        if handle is not None:
            try:
                _lock_release(handle)
            finally:
                handle.close()


def state_path() -> Path:
    """Return the local UI state file without storing project data or secrets."""
    return Path.home() / ".agentbus_ui.json"


def _read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _workspace_key(workspace: Path | str) -> str:
    if not isinstance(workspace, (Path, str)) or not str(workspace).strip():
        raise ValueError('项目目录必须是有效的本地路径')
    root = Path(workspace).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('项目路径不是现有文件夹')
    return os.path.normcase(str(root))


def _validated_viewport(value: dict) -> dict | None:
    if not isinstance(value, dict) or value.get('version') != 1:
        return None
    fields = ('scale', 'panX', 'panY', 'width', 'height')
    numbers = [value.get(field) for field in fields]
    if any(isinstance(number, bool) or not isinstance(number, (int, float))
           or not math.isfinite(number) for number in numbers):
        return None
    scale, pan_x, pan_y, width, height = map(float, numbers)
    if (not 0.00001 <= scale <= 8 or abs(pan_x) > 10_000_000
            or abs(pan_y) > 10_000_000 or not 0 < width <= 100_000
            or not 0 < height <= 100_000):
        return None
    return {'version': 1, 'scale': scale, 'panX': pan_x, 'panY': pan_y,
            'width': width, 'height': height}


def _write_state(state_file: Path, state: dict) -> bool:
    temporary = state_file.with_name(state_file.name + f'.tmp-{os.getpid()}-{threading.get_ident()}')
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
        os.replace(temporary, state_file)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True


def automatic_release_checks() -> bool:
    return _read_state(state_path()).get('automatic_release_checks') is True


def save_automatic_release_checks(enabled: bool, path: Path | str | None = None) -> bool:
    if type(enabled) is not bool:
        return False
    state_file = Path(path) if path is not None else state_path()
    with _STATE_LOCK, _interprocess_lock(state_file) as locked:
        if not locked:
            return False
        state = _read_state(state_file)
        state['automatic_release_checks'] = enabled
        return _write_state(state_file, state)


def begin_automatic_release_check(repository: str, now: float) -> str:
    """Reserve today's automatic check across windows and restarts."""
    state_file = state_path()
    if not automatic_release_checks():
        return 'disabled'
    with _STATE_LOCK, _interprocess_lock(state_file) as locked:
        if not locked:
            return 'deferred'
        state = _read_state(state_file)
        if state.get('automatic_release_checks') is not True:
            return 'disabled'
        previous = state.get('release_check_at')
        if (state.get('release_check_repository') == repository
                and type(previous) in (int, float) and 0 <= now - previous < 86400):
            return 'deferred'
        state.update(release_check_at=now, release_check_repository=repository)
        return 'due' if _write_state(state_file, state) else 'deferred'


def load_last_workspace(path: Path | str | None = None) -> str:
    state_file = Path(path) if path is not None else state_path()
    workspace = _read_state(state_file).get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        return ""
    return str(Path(workspace).expanduser())


def initial_workspace(default: Path | str,
                      path: Path | str | None = None) -> str:
    return load_last_workspace(path) or str(Path(default).expanduser())


def save_last_workspace(workspace: Path | str,
                        path: Path | str | None = None) -> bool:
    state_file = Path(path) if path is not None else state_path()
    try:
        canonical = str(Path(workspace).expanduser().resolve(strict=True))
        if not Path(canonical).is_dir():
            return False
        with _STATE_LOCK, _interprocess_lock(state_file) as locked:
            if not locked:
                # §13.11 失败封口：拿不到锁就绝不读改写，返回可解释失败
                return False
            state = _read_state(state_file)
            state['workspace'] = canonical
            return _write_state(state_file, state)
    except (OSError, ValueError):
        return False


def load_viewport(workspace: Path | str, path: Path | str | None = None) -> dict | None:
    """Load bounded pan/zoom preferences for one project; no project files are read."""
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return None
    with _STATE_LOCK:
        entries = _read_state(state_file).get('viewports')
        if not isinstance(entries, dict):
            return None
        return _validated_viewport(entries.get(key))


def save_viewport(workspace: Path | str, value: dict,
                  path: Path | str | None = None) -> bool:
    """Atomically save one project's viewport while retaining other UI preferences."""
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return False
    viewport = _validated_viewport(value)
    if viewport is None:
        return False
    with _STATE_LOCK, _interprocess_lock(state_file) as locked:
        if not locked:
            # §13.11 失败封口：拿不到锁就绝不读改写，返回可解释失败
            return False
        state = _read_state(state_file)
        entries = state.get('viewports')
        if not isinstance(entries, dict):
            entries = {}
        entries.pop(key, None)
        entries[key] = viewport
        while len(entries) > 32:
            entries.pop(next(iter(entries)))
        state['viewports'] = entries
        return _write_state(state_file, state)


def _validated_branch_folds(value: object) -> list[str] | None:
    if not isinstance(value, list) or len(value) > 1000:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for branch_id in value:
        if not isinstance(branch_id, str) or not _BRANCH_ID.fullmatch(branch_id):
            return None
        if branch_id not in seen:
            result.append(branch_id)
            seen.add(branch_id)
    return result


def load_branch_folds(workspace: Path | str,
                      path: Path | str | None = None) -> list[str]:
    """Load this user's collapsed task-tree branches for one project."""
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return []
    with _STATE_LOCK:
        entries = _read_state(state_file).get('collapsed_branches')
        if not isinstance(entries, dict):
            return []
        validated = _validated_branch_folds(entries.get(key, []))
        return validated if validated is not None else []


def save_branch_folds(workspace: Path | str, branch_ids: list[str],
                      path: Path | str | None = None) -> bool:
    """Atomically save collapsed task-tree branches in per-user UI state."""
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return False
    validated = _validated_branch_folds(branch_ids)
    if validated is None:
        return False
    with _STATE_LOCK, _interprocess_lock(state_file) as locked:
        if not locked:
            # §13.11 失败封口：拿不到锁就绝不读改写，返回可解释失败
            return False
        state = _read_state(state_file)
        entries = state.get('collapsed_branches')
        if not isinstance(entries, dict):
            entries = {}
        entries.pop(key, None)
        if validated:
            entries[key] = validated
        while len(entries) > 32:
            entries.pop(next(iter(entries)))
        state['collapsed_branches'] = entries
        return _write_state(state_file, state)


def _validated_member_icon_paths(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 128:
        return {}
    from .profiles import validate_agent_id
    result: dict[str, str] = {}
    for agent, raw_path in value.items():
        try:
            validate_agent_id(agent)
        except (TypeError, ValueError):
            continue
        if (not isinstance(raw_path, str) or not raw_path.strip()
                or len(raw_path) > 2048 or not Path(raw_path).is_absolute()
                or Path(raw_path).suffix.casefold() != '.exe'
                or raw_path.startswith('\\\\') or '://' in raw_path):
            continue
        result[agent] = raw_path
    return result


def load_member_icon_paths(workspace: Path | str,
                           path: Path | str | None = None) -> dict[str, str]:
    """Load this user's desktop-icon sources for one project, never from shared config."""
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return {}
    with _STATE_LOCK:
        entries = _read_state(state_file).get('member_icon_paths')
        if not isinstance(entries, dict):
            return {}
        return _validated_member_icon_paths(entries.get(key))


def save_member_icon_paths(workspace: Path | str, values: dict[str, str],
                           path: Path | str | None = None) -> bool:
    """Save local-only icon source paths, keyed by project and member."""
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return False
    if not isinstance(values, dict) or len(values) > 128:
        return False
    validated = _validated_member_icon_paths(values)
    if len(validated) != len(values):
        return False
    with _STATE_LOCK, _interprocess_lock(state_file) as locked:
        if not locked:
            # §13.11 失败封口：拿不到锁就绝不读改写，返回可解释失败
            return False
        state = _read_state(state_file)
        entries = state.get('member_icon_paths')
        if not isinstance(entries, dict):
            entries = {}
        entries.pop(key, None)
        if validated:
            entries[key] = validated
        while len(entries) > 32:
            entries.pop(next(iter(entries)))
        state['member_icon_paths'] = entries
        return _write_state(state_file, state)


# §7.25 便签（画布便签层）是**设备本地偏好**，不是共享任务事实：
# 因此与 viewport / collapsed_branches / member_icon_paths 一样只写本机
# `~/.agentbus_ui.json`，按工作区隔离，绝不写入项目共享目录。
_NOTE_ID = re.compile(r"note-[A-Za-z0-9._-]{1,80}\Z", re.ASCII)
MAX_NOTES = 64
MAX_NOTE_TEXT = 4000
STICKY_COLORS = ("yellow", "green", "blue", "pink", "purple")
STICKY_DEFAULT_COLOR = "yellow"


def _finite_number(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _validated_note(item: object) -> dict | None:
    """单个便签的字段/范围校验。不合法返回 None（由调用方决定丢弃还是整批拒绝）。"""
    if not isinstance(item, dict):
        return None
    note_id = item.get('id')
    if not isinstance(note_id, str) or not _NOTE_ID.fullmatch(note_id):
        return None
    numbers = [item.get(field) for field in ('x', 'y', 'w', 'h')]
    if any(not _finite_number(number) for number in numbers):
        return None
    # 与前端 loadStickyNotes 的钳制一致：最小 120×60，且给上界防呆。
    x, y, w, h = map(float, numbers)
    if abs(x) > 1_000_000 or abs(y) > 1_000_000:
        return None
    if not 120 <= w <= 4000 or not 60 <= h <= 4000:
        return None
    text = item.get('text', '')
    if not isinstance(text, str) or len(text) > MAX_NOTE_TEXT:
        return None
    color = item.get('color', STICKY_DEFAULT_COLOR)
    color = color.strip().lower() if isinstance(color, str) else ''
    if color not in STICKY_COLORS:
        color = STICKY_DEFAULT_COLOR          # 与前端 normalizeStickyColor 同策略
    return {'id': note_id, 'x': x, 'y': y, 'w': w, 'h': h,
            'color': color, 'text': text, 'collapsed': item.get('collapsed') is True}


def load_notes(workspace: Path | str,
               path: Path | str | None = None) -> list[dict]:
    """Load this user's canvas sticky notes for one project.

    容错读：非法条目**丢弃**而非整批失败——与前端 loadStickyNotes 的过滤语义一致。
    """
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return []
    with _STATE_LOCK:
        entries = _read_state(state_file).get('sticky_notes')
        if not isinstance(entries, dict):
            return []
        raw = entries.get(key)
        if not isinstance(raw, list) or len(raw) > MAX_NOTES:
            return []
        notes: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            note = _validated_note(item)
            if note is None or note['id'] in seen:
                continue
            seen.add(note['id'])
            notes.append(note)
        return notes


def save_notes(workspace: Path | str, notes: list[dict],
               path: Path | str | None = None) -> bool:
    """Atomically save one project's canvas sticky notes.

    严格写：任一便签不合法即**整批拒绝**并返回 False——避免把坏数据静默落盘
    （与 save_member_icon_paths 同策略）。
    """
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return False
    if not isinstance(notes, list) or len(notes) > MAX_NOTES:
        return False
    validated: list[dict] = []
    seen: set[str] = set()
    for item in notes:
        note = _validated_note(item)
        if note is None or note['id'] in seen:
            return False
        seen.add(note['id'])
        validated.append(note)
    with _STATE_LOCK, _interprocess_lock(state_file) as locked:
        if not locked:
            # §13.11 失败封口：拿不到锁就绝不读改写，返回可解释失败
            return False
        state = _read_state(state_file)
        entries = state.get('sticky_notes')
        if not isinstance(entries, dict):
            entries = {}
        entries.pop(key, None)
        if validated:
            entries[key] = validated
        while len(entries) > 32:
            entries.pop(next(iter(entries)))
        state['sticky_notes'] = entries
        return _write_state(state_file, state)


# §7.30 卡片视觉优先级标记（P0/P1/P2/空）是**设备本地偏好标记**：
# 与便签/视口一样存入本机 `~/.agentbus_ui.json`，按工作区隔离，解决 InPrivate 模式不保留的问题。
_VALID_PRIORITIES = {"P0", "P1", "P2"}
MAX_PRIORITIES = 2000


def _validated_priorities(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or len(value) > MAX_PRIORITIES:
        return None
    validated = {}
    for leaf_id, prio in value.items():
        if not isinstance(leaf_id, str) or not (1 <= len(leaf_id) <= 128):
            return None
        if not isinstance(prio, str) or prio not in _VALID_PRIORITIES:
            return None
        validated[leaf_id] = prio
    return validated


def load_priorities(workspace: Path | str,
                    path: Path | str | None = None) -> dict[str, str]:
    """Load local card visual priority badges for one project."""
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return {}
    with _STATE_LOCK:
        entries = _read_state(state_file).get('card_priorities')
        if not isinstance(entries, dict):
            return {}
        raw = entries.get(key)
        validated = _validated_priorities(raw) if isinstance(raw, dict) else None
        return validated or {}


def save_priorities(workspace: Path | str, priorities: dict[str, str],
                    path: Path | str | None = None) -> bool:
    """Atomically save local card visual priority badges for one project."""
    state_file = Path(path) if path is not None else state_path()
    try:
        key = _workspace_key(workspace)
    except (OSError, RuntimeError, ValueError):
        return False
    validated = _validated_priorities(priorities)
    if validated is None:
        return False
    with _STATE_LOCK, _interprocess_lock(state_file) as locked:
        if not locked:
            # §13.11 失败封口：拿不到锁就绝不读改写，返回可解释失败
            return False
        state = _read_state(state_file)
        entries = state.get('card_priorities')
        if not isinstance(entries, dict):
            entries = {}
        entries.pop(key, None)
        if validated:
            entries[key] = validated
        while len(entries) > 32:
            entries.pop(next(iter(entries)))
        state['card_priorities'] = entries
        return _write_state(state_file, state)


def build_group_config(data: dict, selected_agents: list[str],
                       coordinator: str) -> dict:
    """Return a validated copy with only the selected group membership changed."""
    payload = json.loads(json.dumps(data))
    workers = payload.setdefault("workers", {})
    selected = set(selected_agents)
    unknown = selected - set(workers)
    if unknown:
        raise ConfigError(f"未知 Agent：{', '.join(sorted(unknown))}")
    if not selected:
        raise ConfigError("至少勾选一个 Agent 加入小组")
    if coordinator not in selected:
        raise ConfigError("组长需属于已勾选的小组成员")

    for name, worker in workers.items():
        worker["enabled"] = name in selected
        worker["executor_enabled"] = name in selected
    payload["director"] = coordinator
    validate(payload)
    return payload


def save_group_config(cfg: Config, selected_agents: list[str],
                      coordinator: str) -> Config:
    """Persist group selection only; this does not create or dispatch a goal."""
    payload = build_group_config(cfg.data, selected_agents, coordinator)
    return save(cfg, payload)


# Trae status projection, preserving existing GUI persistence API.
from . import design_system


__all__ = [
    "load_member_icon_paths",
    "save_member_icon_paths",
    "load_notes",
    "save_notes",
    "MAX_NOTES",
    "MAX_NOTE_TEXT",
    "STICKY_COLORS",
    "STICKY_DEFAULT_COLOR",
    "STATUS_LIGHTS",
    "STATUS_ORDER",
    "MEMBER_NEXT_ACTION",
    "derive_status_light",
    "build_member_next_action",
    "status_light_summary",
]

# state -> (label, tone, token_key, symbol, reason)
STATUS_LIGHTS: dict[str, tuple[str, str, str, str, str]] = {
    "dispatch_pending": ("待派发", "info", "secondary", "○",
                         "已有目标/作业，但尚未向该成员派发任务。"),
    "ready": ("待认领", "warn", "accent", "●",
              "信箱里有 ready 任务，等待认领。"),
    "claimed": ("已认领，运行未确认", "info", "accent", "◐",
                "已认领但没有会话/心跳证据；认领不等于执行。"),
    "running": ("正在执行（有心跳证据）", "ok", "ok", "▶",
                "有实际会话或心跳证据，判定为执行中。"),
    "waiting": ("等待文件事件", "neutral", "muted", "◌",
                "无待办任务，处于有界文件事件等待；不是模型轮询。"),
    "handoff": ("需人工接手", "warn", "warn", "⇥",
                "有任务明确需要恢复或人工接手。"),
    "blocked": ("阻塞", "warn", "warn", "■",
                "存在受阻（blocked）任务，优先处理。"),
    "done": ("完成", "ok", "ok", "✓",
             "已有正式回执且无待办任务。"),
    "disabled": ("停用", "neutral", "disabled_text", "⏸",
                 "成员已停用，不参与派工。"),
    "unknown": ("未知", "warn", "muted", "?",
                "证据不足或相互矛盾，保持未确认。"),
}

STATUS_ORDER: tuple[str, ...] = (
    "disabled", "blocked", "unknown", "handoff", "running",
    "claimed", "ready", "waiting", "dispatch_pending", "done",
)

MEMBER_NEXT_ACTION: dict[str, str] = {
    "disabled": "不要派工；需要时由负责人显式启用。",
    "blocked": "先处理阻塞或按需人工接手；核对产物后只恢复指定的一条。",
    "unknown": "读取当前项目计划与任务书后重新确认；不继承旧项目。",
    "handoff": "由负责人或获授权成员按恢复入口处理指定的一条；不抢占 processing。",
    "running": "保持运行，不要抢任务、不要重跑。",
    "claimed": "由原会话正式回执；没有证据不判失败、不抢 processing。",
    "ready": "当前回合直接认领并继续。",
    "waiting": "有界等待；不要反复读空信箱维持对话。",
    "dispatch_pending": "等待派发或由负责人确认目标已拆分；不自行抢他人任务。",
    "done": "立即再读自己的信箱；确无任务再结束本回合。",
}


def _count(evidence: dict, key: str):
    value = evidence.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _flag(evidence: dict, key: str):
    value = evidence.get(key)
    return value if isinstance(value, bool) else None


def _positive(value) -> bool:
    return value is not None and value > 0


def derive_status_light(evidence: dict) -> dict:
    """由明确证据派生单个成员状态灯。

    输入（除 workspace 外均可选）：enabled、dispatched、ready_count、claimed_count、
    blocked_count、done_count、heartbeat_verified、session_active_verified、
    waiting_file_event、needs_handoff、last_receipt_verified、file_mtime_seen、
    evidence_conflict、workspace。未知保持未确认；mtime 绝不等于心跳。
    """
    evidence = evidence if isinstance(evidence, dict) else {}

    ready = _count(evidence, "ready_count")
    claimed = _count(evidence, "claimed_count")
    blocked = _count(evidence, "blocked_count")
    done = _count(evidence, "done_count")
    heartbeat = _flag(evidence, "heartbeat_verified")
    session = _flag(evidence, "session_active_verified")
    receipt = _flag(evidence, "last_receipt_verified")
    execution_evidence = session is True or heartbeat is True
    enabled = evidence.get("enabled", True)
    dispatched = evidence.get("dispatched") is True
    waiting = evidence.get("waiting_file_event") is True
    handoff = evidence.get("needs_handoff") is True
    conflict = evidence.get("evidence_conflict") is True
    # file_mtime_seen 只作观测记录，不作为执行证据（见模块边界）。

    contradiction = False
    if enabled is False:
        state = "disabled"
        contradiction = execution_evidence
    elif _positive(blocked):
        state = "blocked"
        contradiction = execution_evidence
    elif conflict:
        state = "unknown"
        contradiction = True
    elif handoff:
        state = "handoff"
    elif execution_evidence:
        state = "running"
    elif _positive(claimed):
        state = "claimed"
    elif _positive(ready):
        state = "ready"
    elif waiting:
        state = "waiting"
    elif dispatched:
        state = "dispatch_pending"
    elif _positive(done) or receipt is True:
        state = "done"
    else:
        state = "unknown"

    label, tone, token_key, symbol, reason = STATUS_LIGHTS[state]
    if contradiction:
        reason = reason + " 注意：证据相互矛盾，已按确定性优先级取值并标记，请复核。"
    used = []
    for key in ("enabled", "dispatched", "ready_count", "claimed_count", "blocked_count",
                "done_count", "heartbeat_verified", "session_active_verified",
                "waiting_file_event", "needs_handoff", "last_receipt_verified",
                "evidence_conflict", "file_mtime_seen"):
        if key in evidence:
            used.append(key)
    return {
        "state": state,
        "label": label,
        "tone": tone,
        "color": design_system.TOKENS.get(token_key, design_system.TOKENS["muted"]),
        "symbol": symbol,
        "reason": reason,
        "next_action": MEMBER_NEXT_ACTION[state],
        "contradiction": contradiction,
        "execution_verified": execution_evidence,
        "evidence_used": used,
        "member": evidence.get("member") if isinstance(evidence.get("member"), str) else "",
        "workspace": evidence.get("workspace") if isinstance(evidence.get("workspace"), str) else "",
    }


def build_member_next_action(member: str, evidence: dict, workspace: str) -> dict:
    """成员下一步纯数据视图：复用状态灯，返回 state/label/action/reason 及续接提示。"""
    payload = dict(evidence) if isinstance(evidence, dict) else {}
    payload.setdefault("member", member if isinstance(member, str) else "")
    payload.setdefault("workspace", workspace if isinstance(workspace, str) else "")
    light = derive_status_light(payload)
    return {
        "state": light["state"],
        "label": light["label"],
        "action": light["next_action"],
        "reason": light["reason"],
        "member": payload["member"],
        "workspace": payload["workspace"],
        "execution_verified": light["execution_verified"],
        "contradiction": light["contradiction"],
        "resume_note": ("换聊天/新回合：读取当前项目的 AGENTS.md、计划指针与任务 JSON，"
                        "不继承旧项目；等待用有界事件等待，不反复读空信箱。"),
    }


def status_light_summary(members) -> dict:
    """汇总一组成员状态灯的计数（动态，不硬编码成员数）。"""
    counts = {state: 0 for state in STATUS_LIGHTS}
    total = 0
    for item in members if isinstance(members, list) else []:
        if not isinstance(item, dict):
            continue
        state = item.get("state")
        if state in counts:
            counts[state] += 1
            total += 1
    return {"total": total, "counts": counts}
