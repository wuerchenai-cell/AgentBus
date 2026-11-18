"""Small, explicit-context quick-start documents for AgentBus workspaces.

This module never discovers agent installations, inspects environment variables,
reads project configuration, or calls a model. Callers pass a bounded public
snapshot; ``ensure_quickstart`` touches only the workspace-root quick-start file.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping


QUICKSTART_NAME = "00-系统自我坦白与快速上手.md"
_MARKER = re.compile(rb"<!-- AgentBus-managed-v1 body-sha256=([0-9a-f]{64}) -->\n\Z")
_TOP_LEVEL_KEYS = {
    "workspace", "output_root", "target", "tree_id", "tree_title",
    "coordinator", "members", "tasks", "inbox_counts",
}
_MEMBER_KEYS = {"id", "name", "mode"}
_TASK_KEYS = {"id", "section", "title", "state", "assignee"}
_COUNT_KEYS = {"ready", "processing", "blocked", "done"}
_MEMBER_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z", re.ASCII)
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z", re.ASCII)
_MAX_MEMBERS = 64
_MAX_TASKS = 256
_MAX_FILE_BYTES = 256 * 1024


def _bounded_text(value: Any, label: str, limit: int, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本")
    value = " ".join("".join(ch for ch in value if ch >= " " and ch != "\x7f").split())
    value = value.replace("|", "／").replace("`", "ˋ")
    if len(value) > limit:
        raise ValueError(f"{label}超过 {limit} 字符限制")
    if not optional and not value:
        raise ValueError(f"{label}不能为空")
    return value


def _identity(value: Any, label: str, *, member: bool = False) -> str:
    text = _bounded_text(value, label, 128)
    pattern = _MEMBER_ID if member else _RECORD_ID
    if not pattern.fullmatch(text):
        raise ValueError(f"{label}不是有效的项目标识")
    return text


def _workspace_text(value: Any) -> str:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    value = _bounded_text(value, "工作区", 2048)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("工作区必须是绝对路径，且不能包含上级目录跳转")
    return value


def _normalize_context(context: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        raise ValueError("快速上手上下文必须是字段明确的对象")
    extra = set(context) - _TOP_LEVEL_KEYS
    if extra:
        raise ValueError("快速上手上下文包含不支持的字段")

    normalized: dict[str, Any] = {
        "workspace": _workspace_text(context.get("workspace")),
        "output_root": _bounded_text(os.fspath(context.get("output_root"))
                                     if isinstance(context.get("output_root"), os.PathLike)
                                     else context.get("output_root"), "产出目录", 2048),
        "target": _bounded_text(context.get("target"), "项目目标", 500),
        "tree_id": _bounded_text(context.get("tree_id", ""), "任务树 ID", 128, optional=True),
        "tree_title": _bounded_text(context.get("tree_title", ""), "任务树名称", 200, optional=True),
        "coordinator": _bounded_text(context.get("coordinator", ""), "协调成员", 128,
                                      optional=True),
        "members": [],
        "tasks": [],
        "inbox_counts": {},
    }

    members = context.get("members", [])
    if not isinstance(members, list) or len(members) > _MAX_MEMBERS:
        raise ValueError(f"成员列表最多包含 {_MAX_MEMBERS} 项")
    member_ids: set[str] = set()
    member_names: dict[str, str] = {}
    for member in members:
        if not isinstance(member, Mapping) or set(member) - _MEMBER_KEYS:
            raise ValueError("成员信息只接受 ID、名称和接入方式")
        identity = _identity(member.get("id"), "成员 ID", member=True)
        name = _bounded_text(member.get("name"), "成员名称", 160)
        mode = _bounded_text(member.get("mode", "file"), "接入方式", 32)
        if identity in member_ids:
            raise ValueError("成员 ID 不能重复")
        member_ids.add(identity)
        member_names[identity] = name
        normalized["members"].append({"id": identity, "name": name, "mode": mode})
    if normalized["coordinator"] and normalized["coordinator"] not in member_ids:
        raise ValueError("协调成员必须在成员列表中")

    tasks = context.get("tasks", [])
    if not isinstance(tasks, list) or len(tasks) > _MAX_TASKS:
        raise ValueError(f"任务章节最多包含 {_MAX_TASKS} 项")
    for task in tasks:
        if not isinstance(task, Mapping) or set(task) - _TASK_KEYS:
            raise ValueError("任务信息只接受 ID、章节号、名称、状态和执行成员")
        identity = _identity(task.get("id"), "任务 ID")
        section = _bounded_text(task.get("section", ""), "任务章节号", 128, optional=True)
        title = _bounded_text(task.get("title"), "任务名称", 300)
        state = _bounded_text(task.get("state", "ready"), "任务状态", 40)
        assignee = (_identity(task.get("assignee"), "执行成员 ID", member=True)
                    if task.get("assignee") else "")
        if assignee and assignee not in member_ids:
            raise ValueError("任务执行成员必须在成员列表中")
        normalized["tasks"].append({"id": identity, "section": section, "title": title,
                                    "state": state, "assignee": assignee})

    counts = context.get("inbox_counts", {})
    if not isinstance(counts, Mapping) or set(counts) - member_ids:
        raise ValueError("信箱数量只能包含已列出的成员")
    for identity, counts_by_state in counts.items():
        if not isinstance(counts_by_state, Mapping) or set(counts_by_state) - _COUNT_KEYS:
            raise ValueError("信箱数量只接受待处理、处理中、受阻和已完成四类状态")
        row = {}
        for state in sorted(counts_by_state):
            value = counts_by_state[state]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("信箱数量必须是非负整数")
            row[state] = value
        normalized["inbox_counts"][identity] = row

    return normalized


def _mode_name(mode: str) -> str:
    return {"file": "文件交接", "cli": "独立命令行", "acp": "ACP 会话", "sdk": "SDK 会话"}.get(
        mode.casefold(), "自定义接入")


def _state_name(state: str) -> str:
    return {"ready": "待接手", "pending": "待接手", "queued": "待接手", "claimed": "已领取", "working": "正在处理",
            "processing": "正在处理", "blocked": "遇到阻碍", "awaiting_audit": "等待独立检查",
            "delivered": "已交付", "done": "已完成", "rejected": "需要返工"}.get(
                state.casefold(), "状态待确认")


def _render_body(data: dict[str, Any]) -> str:
    lines = [
        "# AgentBus 快速上手",
        "",
        "> 本页由 AgentBus 根据当前项目公开状态自动整理；只包含项目路径、任务摘要、成员和信箱数量。",
        "",
        "## 权威项目环境",
        f"- 当前项目工作区（控制目录）：`{data['workspace']}`",
        f"- 业务产出物理目录：`{data['output_root']}`",
        f"- 项目总目标：{data['target']}",
    ]
    if data["tree_id"] or data["tree_title"]:
        title = data["tree_title"] or "任务树"
        identity = f"（{data['tree_id']}）" if data["tree_id"] else ""
        lines.append(f"- 当前激活任务树：{title}{identity}（位置：`{data['workspace']}/.agentbus/task-tree.json`）")
    lines.extend([
        "",
        "> **架构边界提示**：当前项目状态以控制目录内的配置与任务树为唯一真源；外部安装程序目录与业务项目工作区严格物理隔离，互不混淆。",
        "",
        "## 当前任务章节"
    ])
    if data["tasks"]:
        for task in data["tasks"]:
            section = f"{task['section']} " if task["section"] else ""
            assignee = data.get("member_names", {}).get(task["assignee"], "待指派") if task["assignee"] else "待指派"
            lines.append(f"- {section}{task['title']}（任务 {task['id']}）："
                         f"{_state_name(task['state'])}；负责成员：{assignee}。")
    else:
        lines.append("- 当前没有可展示的任务章节。")
    lines.extend(["", "## 小组成员（彼此平级）"])
    for member in data["members"]:
        role = "协调成员" if member["id"] == data["coordinator"] else "执行/评审成员"
        counts = data["inbox_counts"].get(member["id"], {})
        ready = counts.get("ready", 0)
        processing = counts.get("processing", 0)
        blocked = counts.get("blocked", 0)
        lines.append(f"- {member['name']}（{member['id']}）：{role}，{_mode_name(member['mode'])}；"
                     f"ready {ready}，processing {processing}，blocked {blocked}。")
    lines.extend([
        "",
        "## 接手任务",
        "1. 先读项目根目录的 `AGENTS.md`，再查看属于自己的 `.agentbus/agents/<成员 ID>/ready/` 任务。",
        "2. 按任务写明的目标处理；业务产物写到上面的产出目录。不要直接改信箱记录或其他成员正在处理的任务。",
        "3. 交付时写清产物路径、文件大小与 SHA256，并说明实际检查过什么、哪些内容尚未验证。",
        "4. 没有明确任务时先报告空闲状态，不要自行改写项目计划或接管他人的任务。",
        "",
        "## 项目边界",
        "成员彼此平级；协调成员负责整理任务和汇总结果，不额外取得文件或系统权限。未经明确授权，不修改控制目录、配置、凭据或他人在途任务。",
        "",
    ])
    return "\n".join(lines)


def render_quickstart(context: Mapping[str, Any]) -> str:
    """Render a deterministic managed Markdown quick-start from explicit safe inputs."""
    data = _normalize_context(context)
    data["member_names"] = {member["id"]: member["name"] for member in data["members"]}
    body = _render_body(data).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    return f"<!-- AgentBus-managed-v1 body-sha256={digest} -->\n" + body.decode("utf-8")


def render_onboarding_capsule(context: Mapping[str, Any], member_id: str) -> str:
    """Build a concise member-specific instruction; it reads no workspace files."""
    data = _normalize_context(context)
    member = next((item for item in data["members"] if item["id"] == member_id), None)
    member_id = _identity(member_id, "member_id", member=True)
    if member is None:
        raise ValueError("指定成员不在当前项目成员列表中")
    role = "项目协调成员" if member_id == data["coordinator"] else "项目协作成员"
    relevant = [task for task in data["tasks"] if task["assignee"] == member_id]
    sections = "；".join(
        f"{task['section']} {task['title']}（{task['id']}，{_state_name(task['state'])}）" if task["section"]
        else f"{task['title']}（{task['id']}，{_state_name(task['state'])}）" for task in relevant[:5]
    ) or "当前没有明确指派给你的任务"
    counts = data["inbox_counts"].get(member_id, {})
    return "\n".join([
        f"我是 {member['name']}（{member_id}），担任{role}；本组成员彼此平级。",
        f"项目目标：{data['target']}",
        f"当前任务：{sections}",
        f"工作目录：{data['workspace']}；业务产出写到 output_root：{data['output_root']}。",
        f"任务入口：`.agentbus/agents/{member_id}/ready/`（待处理 {counts.get('ready', 0)} 条，处理中 {counts.get('processing', 0)} 条）。",
        f"第一步（自主向组长报到与绑定当前对话）：先执行 `agentbus contact checkin --workspace \"{data['workspace']}\" --agent \"{member_id}\"` 绑定当前窗口并向组长汇报就绪。",
        "先读根目录 AGENTS.md 与 PROJECT.md；任务树为交付清单，详细技术规范按成果目录方案在业务产出目录下实现，严禁修改外部参考库。",
        "按当前调度模式认领任务，执行期间定期发送 heartbeat 维持心跳；代码自测通过后提交 path/bytes/SHA256 清单与正式 answer，写清产物路径、实际检查和未验证项。",
        "不要修改控制目录、凭据或他人在途任务；自主调度可用 claim-next 领取已就绪任务，其他模式接本人信箱；无可接工作时报告空闲，不自行扩展目标。",
    ])


def _has_reparse_attribute(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _result(path: Path, status: str, *, reason: str = "", digest: str = "") -> dict[str, Any]:
    return {
        "path": str(path),
        "status": status,
        "created_or_updated": status in {"created", "updated"},
        "conflict": status == "conflict",
        "reason": reason,
        "sha256": digest,
    }


def _atomic_create(path: Path, content: bytes) -> bool:
    fd, temp_name = tempfile.mkstemp(prefix=".agentbus-quickstart-", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard-link publication is atomic and refuses to replace a file
        # created concurrently after our initial existence check.
        os.link(temp, path)
        temp.unlink()
        return True
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass  # 资源清理类：失败路径删除临时文件，失败即放弃。


def _atomic_replace(path: Path, content: bytes, expected_old: bytes) -> bool:
    # Recheck before publication so a concurrent user edit is preserved.
    if _has_reparse_attribute(path) or not path.is_file() or path.read_bytes() != expected_old:
        return False
    fd, temp_name = tempfile.mkstemp(prefix=".agentbus-quickstart-", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        return True
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass  # 资源清理类：同上，临时文件删除失败不改变主结果。


def ensure_quickstart(workspace: Path, context: Mapping[str, Any]) -> dict[str, Any]:
    """Create/update only the AgentBus-owned root guide; preserve all collisions.

    Ownership is recorded as a SHA256 of the exact body in the first-line
    marker. Any manual body edit invalidates that marker and becomes a conflict.
    The marker and generated body are deterministic and UTF-8 encoded.
    """
    try:
        supplied = Path(workspace)
        if not supplied.is_absolute() or not supplied.is_dir() or _has_reparse_attribute(supplied):
            raise ValueError("工作区必须是已存在的本地文件夹，且不能是链接或重解析点")
        resolved = supplied.resolve(strict=True)
        if str(Path(_workspace_text(context.get("workspace"))).resolve(strict=False)).casefold() != str(resolved).casefold():
            raise ValueError("上下文中的工作区与目标文件夹不一致")
        target = resolved / QUICKSTART_NAME
        if _has_reparse_attribute(target):
            raise ValueError("快速上手文件路径是链接；已保留原文件")
        desired = render_quickstart(context).encode("utf-8")
        digest = hashlib.sha256(desired).hexdigest()

        if not os.path.lexists(target):
            if not _atomic_create(target, desired):
                return _result(target, "conflict", reason="创建期间出现了同名文件；已保留原文件")
            return _result(target, "created", digest=digest)

        if not target.is_file():
            return _result(target, "conflict", reason="同名路径已被文件夹或其他对象占用；已保留原对象")
        if target.stat().st_size > _MAX_FILE_BYTES:
            return _result(target, "conflict", reason="已有文件超过安全读取上限；已保留原文件")
        current = target.read_bytes()
        first_line, separator, body = current.partition(b"\n")
        match = _MARKER.fullmatch(first_line + separator)
        if not match or hashlib.sha256(body).hexdigest().encode("ascii") != match.group(1):
            return _result(target, "conflict", reason="已有文件由用户创建或修改；已保留原文件")
        if current == desired:
            return _result(target, "unchanged", digest=digest)
        if not _atomic_replace(target, desired, current):
            return _result(target, "conflict", reason="刷新期间文件发生变化；已保留新内容")
        return _result(target, "updated", digest=digest)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        safe_path = Path(workspace) / QUICKSTART_NAME
        return _result(safe_path, "conflict", reason=str(exc)[:300])
