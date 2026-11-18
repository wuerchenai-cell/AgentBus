#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.config —— 配置加载与校验。

核心设计：配置里必须显式声明每个 agent 的「能不能被无头拉起」。
如果用户把 Trae / ZCode / WorkBuddy 配成"可被拉起的执行者"，直接报错并给出
正确做法（走人工接手通道），而不是等到运行时才莫名失败。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .silent_debug import note_silent

SCHEMA_VERSION = 1
_RESERVED_ROOT_PARTS = {
    '.workbuddy', '.agentbus', '.git', '.codex', '.ssh', '.aws', '.azure',
    '.gnupg', '.claude', '.trae', '.zcode', '不要访问', 'do not access',
}

# 已知 agent 的 AgentBus 接入门禁画像。
# can_spawn=True 表示本项目已经接受该适配器，不等同于厂商“有 CLI/ACP”。
# WorkBuddy/Trae 的 ACP 入口目前仍是候选；真实任务、权限和回执闭环验收后再放开。
# 未知名字仍由用户显式配置，以便接入新工具时不必等待本表更新。
KNOWN_AGENTS: dict[str, dict[str, Any]] = {
    "codex": {"can_spawn": True, "kind": "codex-sdk",
              "note": "官方 Codex Python SDK，持续 thread 会话"},
    "anthropic-api": {"can_spawn": True, "kind": "http-llm",
                      "note": "HTTP 直连大模型，第二条命；会消耗 API 额度"},
    "workbuddy": {"can_spawn": False, "kind": "acp",
                   "note": "厂商提供 CodeBuddy ACP；AgentBus 本机真实任务/权限/回执闭环尚未验收"},
    "trae": {"can_spawn": False, "kind": "acp",
             "note": "本机未发现 TraeCode CLI；官方 CLI 状态仍为 coming soon"},
    "zcode": {"can_spawn": False, "kind": "gui",
              "note": "上游有 CLI 源码；本机独立会话入口尚未验证"},
    "antigravity": {"can_spawn": False, "kind": "gui",
                     "note": "Google Antigravity 桌面对话接手；真实任务、产物与回执闭环尚未验收"},
    "opencode": {"can_spawn": True, "kind": "acp",
                 "note": "OpenCode 2.0.21 ACP 已通过文件任务、原生回执、会话恢复验收（2026-10-02）"},
}

DEFAULTS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "workspace": ".",
    # Control state stays in the project workspace. Artifacts may be placed
    # on another volume by setting an absolute output_root.
    "output_root": ".",
    "director": "codex",
    "workers": {
        "codex": {"enabled": True, "can_spawn": True, "executor_enabled": True,
                  "kind": "codex-sdk",
                  "sandbox": "workspace-write", "timeout_s": 1800, "model": None},
        "workbuddy": {"enabled": True, "can_spawn": False, "executor_enabled": True,
                      "human_in_loop": True, "transport": "file"},
        "trae": {"enabled": True, "can_spawn": False, "executor_enabled": True,
                 "human_in_loop": True, "transport": "file"},
        "zcode": {"enabled": True, "can_spawn": False, "executor_enabled": True,
                 "human_in_loop": True, "transport": "file"},
        "opencode": {"enabled": False, "can_spawn": True, "executor_enabled": False,
                     "human_in_loop": False, "kind": "acp", "transport": "file"},
        "antigravity": {"enabled": False, "can_spawn": False, "executor_enabled": True,
                         "human_in_loop": True, "kind": "gui", "transport": "file",
                         "display_name": "Google Antigravity"},
    },
    "gates": {
        "max_hops": 4,               # 一条任务链最多转手几次
        "max_retries": 2,            # 单个任务最多重试几次
        "max_tasks": 20,             # 一次目标最多拆出几个任务
        "task_timeout_s": 1800,      # 单个任务执行上限（实测中转链路约 2 分钟/次，别给小了）
        "heartbeat_timeout_s": 600,  # 文件未更新的提示阈值；不据此自动回收
        "total_budget_s": 7200,      # 整个目标的总墙钟预算
        "idle_timeout_s": 8,         # 信箱清空后空闲多久就收工（不必耗尽整个时间窗）
    },
    "notify": {"console": True, "windows_toast": False},
    "oversight": {"review_every_n": 3, "stuck_attempts": 2, "stuck_hops": 2},
    # Strict audit is opt-in. Commands are owner-authored argv arrays; task
    # text is never interpreted as a shell command.
    "supervision": {"strict_supervision": False, "designated_auditor": "", "checks": []},
}


class ConfigError(Exception):
    pass


# §7.16 稳定退出码表（单一事实源）。cli.main 以此为准，任何人不得再造随手数。
# 兼容保留旧值：2/3/4/130 与历史行为逐字节一致，1 继续用于「命令级非致命」结果。
EXIT_OK = 0              # 成功
EXIT_COMMAND_FALSE = 1   # 命令执行完毕但结果是否定（如 workflow 未就绪）
EXIT_CONFIG = 2          # 配置缺失或非法
EXIT_ADAPTER = 3         # 执行器/外部运行时不可用
EXIT_OPERATION = 4       # 操作未完成（总线/值/运行时/工作流/技能/OS 错误）
EXIT_INTERRUPT = 130     # 用户中断

EXIT_CODES = {
    EXIT_OK: "成功",
    EXIT_COMMAND_FALSE: "命令已执行但结果为否定（非错误）",
    EXIT_CONFIG: "配置缺失或非法",
    EXIT_ADAPTER: "执行器/外部运行时不可用",
    EXIT_OPERATION: "操作未完成（总线、校验、工作流或系统错误）",
    EXIT_INTERRUPT: "用户中断",
}


def launcher_name() -> str:
    """按当前运行形态给出用户可直接复制的启动串。

    冻结 EXE 用户手上没有 python -m agentbus；源码/开发环境才是模块调用。
    这里只影响提示文案，不改变任何行为。
    """
    import sys

    exe = getattr(sys, "frozen", False) and getattr(sys, "executable", "")
    if exe:
        return f'"{exe}"'
    return "python -m agentbus"


@dataclass
class Config:
    path: Path
    workspace: Path
    data: dict[str, Any] = field(default_factory=dict)

    # ---- 便捷访问 ----
    @property
    def director(self) -> str:
        return str(self.data.get("director", "codex"))

    @property
    def gates(self) -> dict[str, Any]:
        return dict(self.data.get("gates", {}))

    @property
    def output_root(self) -> Path:
        """Return the business-artifact root, separate from control state."""
        raw = self.data.get("output_root", ".")
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def enabled_workers(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.data.get("workers", {}).items()
                if v.get("enabled", True)}

    def executor_workers(self) -> dict[str, dict[str, Any]]:
        """Enabled participants eligible to receive task/goal/review assignments."""
        return {k: v for k, v in self.enabled_workers().items()
                if v.get("executor_enabled", True)}

    def spawnable_workers(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.enabled_workers().items()
                if v.get("can_spawn") and v.get("executor_enabled", True)}

    def human_workers(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.enabled_workers().items()
                if v.get("executor_enabled", True)
                and (v.get("human_in_loop") or not v.get("can_spawn"))}

    def worker(self, name: str) -> dict[str, Any]:
        w = self.data.get("workers", {}).get(name)
        if w is None:
            raise ConfigError(
                f"配置里没有执行者 {name!r}。可选：{sorted(self.enabled_workers())}")
        return w


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def validate(data: dict[str, Any]) -> None:
    """配置自检：不合法的取值在启动时就报错，不要等运行时才炸。"""
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(
            f"schema_version 不匹配：配置是 {data.get('schema_version')}，"
            f"程序需要 {SCHEMA_VERSION}")

    workers = data.get("workers", {})
    if not workers:
        raise ConfigError("配置里没有任何执行者（workers 为空）")

    director = data.get("director")
    if director not in workers:
        raise ConfigError(f"director={director!r} 不在 workers 里。"
                          f"可选：{sorted(workers)}")
    if not workers[director].get("enabled", True):
        raise ConfigError(f"director={director!r} 必须是 enabled participant")

    workspace = data.get("workspace", ".")
    output_root = data.get("output_root", ".")
    if not isinstance(workspace, str) or not workspace.strip():
        raise ConfigError("workspace 必须是非空路径")
    if not isinstance(output_root, str) or not output_root.strip():
        raise ConfigError("output_root 必须是非空路径")
    resolved_output = Path(output_root).expanduser()
    if not resolved_output.is_absolute():
        resolved_output = Path(workspace).expanduser() / resolved_output
    if any(part.casefold() in _RESERVED_ROOT_PARTS
           for part in resolved_output.parts):
        raise ConfigError("output_root 不能进入控制目录、私有目录或受保护目录")

    # 关键校验：未验收的程序化适配器不能被配置成自动启动
    for name, w in workers.items():
        from .profiles import validate_agent_id
        try:
            validate_agent_id(name)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if "executor_enabled" in w and not isinstance(w["executor_enabled"], bool):
            raise ConfigError(f"workers.{name}.executor_enabled 必须是布尔值")
        transport = w.get("transport", "file")
        if transport not in {"file", "cli"}:
            raise ConfigError(f"workers.{name}.transport 必须是 file 或 cli")
        # File exchange is the product default. A CLI route is opt-in and can
        # only point at an adapter that has already passed local acceptance.
        if transport == "cli" and not w.get("can_spawn", False):
            raise ConfigError(
                f"workers.{name}.transport=cli 需要已验收的可启动适配器；请先使用 file 路线")
        if not w.get("enabled", True):
            continue
        known = KNOWN_AGENTS.get(name, {})
        declared = bool(w.get("can_spawn"))
        actual = known.get("can_spawn", None)
        if actual is False and declared:
            if known.get("kind") == "gui":
                reason = f"是 GUI 应用（{known.get('note', '尚无已验收的无头适配器')}）"
            else:
                reason = ("虽有厂商接入接口，但 AgentBus 适配尚未验收"
                          f"（{known.get('note', '尚无已验收的适配器')}）")
            raise ConfigError(
                f"{name!r} {reason}，不能设 can_spawn=true。\n"
                f"    → 请保持 can_spawn=false，直到真实会话任务与回执闭环通过验收。")
        if declared and actual is True and name in {"codex", "workbuddy", "trae", "opencode"}:
            required_kind = str(known.get("kind"))
            legacy_codex_cli = name == "codex" and w.get("kind") == "codex-cli"
            if w.get("kind") != required_kind and not legacy_codex_cli:
                raise ConfigError(
                    f"{name!r} 作为可启动成员必须使用 {required_kind!r} 适配器；"
                    f"当前 kind={w.get('kind')!r}")

    g = data.get("gates", {})
    for key in ("max_hops", "max_retries", "max_tasks"):
        v = g.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ConfigError(f"gates.{key} 必须是非负整数，当前是 {v!r}")

    oversight = data.get("oversight", {})
    for key in ("review_every_n", "stuck_attempts", "stuck_hops"):
        v = oversight.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ConfigError(f"oversight.{key} 必须是非负整数，当前是 {v!r}")

    try:
        from .supervision import validate_settings
        supervision = validate_settings(data.get("supervision", DEFAULTS["supervision"]), workers)
    except ValueError as exc:
        raise ConfigError(f"supervision 配置无效：{exc}") from exc
    if supervision["strict_supervision"] and supervision["designated_auditor"] == director:
        raise ConfigError("严格监督的审计员必须与组长分开，确保成员成果由另一人独立复核")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """以临时文件 + os.replace 原子写入 JSON，避免中断或并发写留下半截/0字节损坏文件。

    在原子替换前，如果目标文件已存在且有效，自动保留一份 .bak 备份，便于极端损坏时安全恢复。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            bak_path = path.with_name(path.name + ".bak")
            try:
                shutil.copy2(path, bak_path)
            except OSError:
                pass  # 资源清理类：备份可选；备份失败不阻止主写入（不影响正确性）。
        tmp_path.replace(path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass  # 资源清理类：写入失败后尽力删除临时文件，失败即放弃。
        raise


def load(workspace: Path | str, create: bool = False) -> Config:
    ws = Path(workspace).resolve()
    path = ws / ".agentbus" / "config.json"

    if not path.exists():
        if not create:
            display_ws = workspace if isinstance(workspace, str) else str(workspace)
            clean_display_path = Path(display_ws) / ".agentbus" / "config.json"
            raise ConfigError(
                f"找不到配置 '{clean_display_path}'。\n"
                f"    [下一步建议] 请确认工作区路径，或执行：{launcher_name()} init --quickstart --workspace \"{display_ws}\"")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(DEFAULTS)
        data["workspace"] = str(ws)
        _atomic_write_json(path, data)

    raw: dict[str, Any]
    try:
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError("配置文件内容为空（0 字节）")
        raw = json.loads(content)
        if not isinstance(raw, dict):
            raise ValueError("配置文件根对象必须为字典")
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError, OSError) as exc:
        bak_path = path.with_name(path.name + ".bak")
        recovered = False
        if bak_path.exists() and bak_path.is_file():
            try:
                bak_content = bak_path.read_text(encoding="utf-8")
                if bak_content.strip():
                    bak_raw = json.loads(bak_content)
                    if isinstance(bak_raw, dict):
                        # Keep the damaged bytes so the user's last edit is not
                        # silently lost, then tell them which file to inspect.
                        import sys as _sys
                        import time as _time
                        corrupt = path.with_name(
                            f"{path.name}.corrupt-{_time.strftime('%Y%m%d-%H%M%S')}")
                        try:
                            corrupt.write_bytes(path.read_bytes())
                        except OSError:
                            corrupt = None
                        _atomic_write_json(path, bak_raw)
                        raw = bak_raw
                        recovered = True
                        kept = f"损坏内容已另存为 {corrupt.name}" if corrupt else "损坏内容未能另存"
                        print(f"[警告] {path.name} 损坏（{exc}），已用 {bak_path.name} 恢复上一次保存的配置；"
                              f"{kept}，最近一次修改可能需要重新设置。", file=_sys.stderr)
            except Exception:
                # 功能类：主配置损坏时尝试回读 .bak 恢复；回读本身失败则保持
                # recovered=False，由下方 ConfigError 明确报错，绝不静默吞掉损坏。
                note_silent("config.py:337", "functional",
                            "主配置损坏回读 .bak 失败；仍会抛 ConfigError，不静默放行")
                pass
        if not recovered:
            raise ConfigError(
                f"配置文件 {path} 损坏或格式错误：{exc}。请检查文件内容或从备份恢复。"
            ) from exc

    data = _deep_merge(DEFAULTS, raw)
    data.setdefault("workspace", str(ws))
    data.setdefault("output_root", ".")
    validate(data)
    return Config(path=path, workspace=ws, data=data)


def save(cfg: Config, data: dict[str, Any] | None = None) -> Config:
    """写回配置（GUI 改主体/执行者后要落盘）。

    先校验再写 —— 宁可在界面上报错，也不要留一份运行时才炸的配置。
    """
    payload = dict(data if data is not None else cfg.data)
    payload["schema_version"] = SCHEMA_VERSION
    payload.setdefault("workspace", str(cfg.workspace))
    payload.setdefault("output_root", ".")
    validate(payload)
    _atomic_write_json(cfg.path, payload)
    return Config(path=cfg.path, workspace=cfg.workspace, data=payload)


def describe(data: dict[str, Any]) -> str:
    lines = [f"控制目录: {data.get('workspace', '.')}",
             f"业务产出目录: {data.get('output_root', '.')}",
             f"项目协调者/总司令（平级）: {data['director']}"]
    lines.append("已启用参与者:")
    for name, w in data.get("workers", {}).items():
        if not w.get("enabled", True):
            continue
        transport = w.get("transport", "file")
        route = "文件主路线" if transport == "file" else "独立 CLI（已验收）"
        if w.get("can_spawn"):
            status = "可执行" if w.get("executor_enabled", True) else "未选为执行者"
            lines.append(f"  - {name:<12} {status}，{route}，可无头拉起  sandbox={w.get('sandbox','-')}")
        else:
            status = "可执行" if w.get("executor_enabled", True) else "未选为执行者"
            lines.append(f"  - {name:<12} {status}，{route}，需人工接手（GUI，投信箱）")
    g = data.get("gates", {})
    lines.append(f"闸门: max_hops={g.get('max_hops')} max_retries={g.get('max_retries')} "
                 f"max_tasks={g.get('max_tasks')} task_timeout={g.get('task_timeout_s')}s "
                 f"total_budget={g.get('total_budget_s')}s")
    return "\n".join(lines)
