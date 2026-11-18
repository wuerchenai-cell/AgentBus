#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus.detect —— 自动探测本机装了哪些 agent，以及它们到底能不能被脚本调用。

为什么必须探测而不是写死：
    本机实测（2026-09-26）PATH 里**一个 agent 的 CLI 都没有**。
    codex 不在 PATH，它藏在 %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<随机哈希>\\codex.exe；
    ZCode 装在 D:\\ZCode，注册表里 InstallLocation 是空的，只有 UninstallString 有线索。
    → 只靠 shutil.which() 会得出"什么都没装"的错误结论。

探测结论分三类：
    can_spawn=True  已找到命令入口；不证明真实模型任务已验收
    can_spawn=False 只能人工接手（GUI 应用）
    另外单独标出 http-llm 这类"有 API 凭证、能直接调用"的通道
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

IS_WINDOWS = os.name == "nt"
HOME = Path.home()
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")
APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
PROGRAMDATA = Path(os.environ.get("ProgramData") or r"C:\ProgramData")

# 通用 CLI agent 的候选命令名（装了就能被无头拉起）
GENERIC_CLI_NAMES = (
    "claude", "gemini", "qwen", "aider", "crush", "opencode",
    "cursor-agent", "goose", "droid", "amp", "copilot", "iflow", "codebuddy",
)


@dataclass
class Probe:
    name: str
    kind: str                      # codex-sdk / acp / codex-cli / cli / http-llm / gui
    installed: bool = False
    can_spawn: bool = False
    path: str = ""
    version: str = ""
    running: bool = False
    note: str = ""
    candidate_adapter: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 底层工具
# --------------------------------------------------------------------------- #


def _which(*names: str) -> Path | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return Path(p)
    return None


def _version(exe: Path, args: tuple[str, ...] = ("--version",),
             timeout: int = 25) -> str:
    try:
        r = subprocess.run([str(exe), *args], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL)
        text = (r.stdout or "") + (r.stderr or "")
        line = next((x.strip() for x in text.splitlines() if x.strip()), "")
        return line[:120]
    except Exception:  # noqa: BLE001  探测失败不该中断整个流程
        return ""


def _first_existing(*cands: Path) -> Path | None:
    for c in cands:
        if c and c.is_file():
            return c
    return None


def _running_images() -> set[str]:
    """一次 tasklist 拿到所有进程名，避免逐个查询。"""
    if not IS_WINDOWS:
        return set()
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, timeout=30, encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return set()
    names: set[str] = set()
    for line in (r.stdout or "").splitlines():
        m = re.match(r'"([^"]+)"', line.strip())
        if m:
            names.add(m.group(1).lower())
    return names


def _registry_installs() -> dict[str, str]:
    """返回 {DisplayName: 卸载项里能拿到的最有信息量的一条路径}。

    坑：ZCode 的 InstallLocation 是空的，DisplayIcon 指向 uninstallerIcon.ico，
    只有 UninstallString 里有真目录（"D:\\ZCode\\Uninstall ZCode.exe" /currentuser）。
    所以按 UninstallString → InstallLocation → DisplayIcon 的顺序取，
    并且在拿到 .ico 这种明显不是主程序的路径时继续往下找。
    """
    if not IS_WINDOWS:
        return {}
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return {}

    def qv(key, name: str) -> str:
        try:
            v, _ = winreg.QueryValueEx(key, name)
            return str(v)
        except OSError:
            return ""

    out: dict[str, str] = {}
    roots = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sk:
                            dn = qv(sk, "DisplayName")
                            if not dn:
                                continue
                            raw = (qv(sk, "UninstallString")
                                   or qv(sk, "InstallLocation")
                                   or qv(sk, "DisplayIcon"))
                            out.setdefault(dn, raw)
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def _unquoted(raw: str) -> Path | None:
    """从注册表那种 '"C:\\X\\Y.exe" /args' 的串里抠出第一个路径。"""
    if not raw:
        return None
    m = re.match(r'\s*"([^"]+)"', raw)
    if m:
        return Path(m.group(1))
    m = re.match(r"\s*([A-Za-z]:\\[^/]*)", raw)
    if m:
        return Path(m.group(1).strip())
    return None


def _from_registry(installs: dict[str, str], *keywords: str) -> Path | None:
    """在注册表卸载项里按关键词找主程序 exe。"""
    for dn, raw in installs.items():
        if not any(k in dn.lower() for k in keywords):
            continue
        p = _unquoted(raw)
        if p is None:
            continue
        if p.suffix.lower() == ".exe" and p.is_file() and "uninstall" not in p.name.lower():
            return p
        folder = p.parent if p.suffix.lower() == ".exe" else p
        if not folder.is_dir():
            continue
        # 目录里挑一个最像主程序的 exe（排除 uninstall / repair / crashpad）
        exes = [e for e in sorted(folder.glob("*.exe"))
                if not re.search(r"uninstall|repair|crashpad|update", e.name, re.I)]
        if exes:
            return exes[0]
    return None


# --------------------------------------------------------------------------- #
# 各 agent 的探测
# --------------------------------------------------------------------------- #


def probe_codex(running: set[str]) -> Probe:
    from .adapters import codex_version, resolve_codex

    p = Probe(name="codex", kind="codex-sdk",
              note="Codex Python SDK 持有并恢复独立 thread；当前运行时仍需握手验收")
    exe = resolve_codex()
    if exe is None:
        p.note = "没找到 codex.exe（已试 config.toml / bin 目录 / PATH）"
        return p
    p.installed = True
    p.can_spawn = True
    p.path = str(exe)
    p.version = codex_version(exe)
    p.running = "codex.exe" in running
    return p


def probe_generic_clis(running: set[str]) -> list[Probe]:
    out: list[Probe] = []
    for name in GENERIC_CLI_NAMES:
        exe = _which(name, f"{name}.cmd", f"{name}.exe")
        if exe is None:
            continue
        ver = _version(exe)
        out.append(Probe(
            name=name, kind="cli", installed=True, can_spawn=True,
            path=str(exe), version=ver, running=f"{name}.exe" in running,
            note=f"通用 CLI 执行者（自动发现：{exe.name}）"))
    return out


def probe_http_llm() -> Probe:
    """探测「有 API 凭证、可被直接 HTTP 调用」的通道。

    本机实测：~/.claude/settings.json 里有一套 Anthropic 兼容端点凭证
    （CC Switch 写入的第三方中转）。它不是 GUI 软件，但确实能被脚本调用，
    所以单列一类 http-llm —— 代价是消耗额度，默认不启用。
    """
    p = Probe(name="anthropic-api", kind="http-llm",
              note="走 HTTP 直连大模型，可无头执行；会消耗你的 API 额度")
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or \
        os.environ.get("ANTHROPIC_API_KEY", "")
    src = "环境变量"

    if not (base and token):
        cfg = HOME / ".claude" / "settings.json"
        if cfg.is_file():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                env = data.get("env", {}) if isinstance(data, dict) else {}
                base = base or str(env.get("ANTHROPIC_BASE_URL", ""))
                token = token or str(env.get("ANTHROPIC_AUTH_TOKEN", "")) or \
                    str(env.get("ANTHROPIC_API_KEY", ""))
                if base and token:
                    src = str(cfg)
            except (OSError, json.JSONDecodeError):
                pass  # 资源清理类：读取候选配置失败即跳过该来源，继续探测。

    if base and token:
        p.installed = True
        p.can_spawn = True
        p.detail = {"base_url": base, "token_source": src,
                    "token_present": True}
        p.version = base
        p.path = src
    return p


def _node_binary() -> Path | None:
    """Find Node for WorkBuddy's bundled CodeBuddy JS CLI without reading app config."""
    found = _which("node.exe", "node")
    if found is not None:
        return found
    codex_node = (HOME / ".cache" / "codex-runtimes" / "codex-primary-runtime"
                  / "dependencies" / "node" / "bin" / "node.exe")
    return codex_node if codex_node.is_file() else None


def probe_gui_agents(installs: dict[str, str], running: set[str]) -> list[Probe]:
    """Inspect desktop installs and upgrade only documented ACP CLI paths."""
    specs: list[tuple[str, str, list[Path], tuple[str, ...], str]] = [
        ("workbuddy", "WorkBuddy",
         [Path(r"D:\WorkBuddy\WorkBuddy.exe"),
          LOCALAPPDATA / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
          PROGRAMDATA / "WorkBuddy" / "WorkBuddy.exe"],
         ("workbuddy",), "Electron 桌面应用，无对外 CLI"),
        ("trae", "TRAE SOLO CN",
         [Path(r"D:\TRAE SOLO CN\TRAE SOLO CN.exe"),
          LOCALAPPDATA / "Programs" / "TRAE SOLO CN" / "TRAE SOLO CN.exe"],
         ("traework", "trae"), "VS Code 内核；其 CLI 只管编辑器与扩展，不能无头跑 AI"),
        ("zcode", "ZCode",
         [Path(r"D:\ZCode\ZCode.exe"),
          LOCALAPPDATA / "Programs" / "ZCode" / "ZCode.exe"],
         ("zcode",), "Electron 桌面应用；AgentBus 尚未验收本机可自动启动的多轮会话入口"),
        ("antigravity", "Antigravity",
         [LOCALAPPDATA / "Programs" / "antigravity" / "Antigravity.exe"],
         ("antigravity",), "Google Antigravity 桌面对话接手；真实任务与产物回执尚未验收"),
    ]
    out: list[Probe] = []
    for name, exe_name, cands, keys, note in specs:
        exe = _first_existing(*cands) or _from_registry(installs, *keys)
        if exe is None:
            continue
        if name == "workbuddy":
            cli = (exe.parent / "resources" / "app.asar.unpacked" / "cli"
                   / "bin" / "codebuddy")
            node = _node_binary()
            if cli.is_file() and node is not None:
                command = [str(node), str(cli), "--acp"]
                out.append(Probe(
                    name=name, kind="acp", installed=True, can_spawn=False,
                    path=str(cli), running=f"{exe_name}.exe".lower() in running,
                    candidate_adapter=True,
                    note="找到内置 CodeBuddy ACP 入口；真实任务/权限/回执闭环尚未验收",
                    detail={"command": command, "permission_mode": "default"}))
                continue
            if cli.is_file():
                note = "找到内置 CodeBuddy ACP CLI，但缺少可用 Node runtime"
            else:
                note = "桌面应用已安装，未找到内置 CodeBuddy ACP CLI"
        elif name == "trae":
            trae_cli = _which("traecli", "traecli.exe", "traecli.cmd")
            if trae_cli is not None:
                out.append(Probe(
                    name=name, kind="acp", installed=True, can_spawn=False,
                    path=str(trae_cli), running=f"{exe_name}.exe".lower() in running,
                    candidate_adapter=True,
                    note="找到 TraeCode ACP CLI；真实任务/权限/回执闭环尚未验收",
                    detail={"command": [str(trae_cli), "acp", "serve"]}))
                continue
            note = "桌面应用已安装；TraeCode CLI 未在 PATH 中找到"
        out.append(Probe(name=name, kind="gui", installed=True, can_spawn=False,
                         path=str(exe), running=f"{exe_name}.exe".lower() in running,
                         note=note))
    return out


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #


def probe_opencode(installs: dict[str, str], running: set[str]) -> Probe:
    """Inspect only official entrypoints; never read provider configuration."""
    desktop = _first_existing(
        Path(r"D:\Apps\OpenCode\OpenCode.exe"),
        LOCALAPPDATA / "Programs" / "OpenCode" / "OpenCode.exe")
    desktop = desktop or _from_registry(installs, "opencode")
    cli = _which("opencode.exe", "opencode", "opencode.cmd")
    if cli is None and desktop is not None:
        bundled = desktop.parent / "resources" / "opencode-cli.exe"
        if bundled.is_file():
            cli = bundled
    if cli is not None:
        version = _version(cli)
        # Actual local ACP task/native-receipt acceptance, 2026-10-02/03.
        # Exact releases only; detecting a future version is not acceptance.
        accepted = version in {"opencode v2.0.21", "opencode v2.0.22"}
        return Probe("opencode", "acp", installed=True, can_spawn=accepted,
                     path=str(cli), running="opencode.exe" in running,
                     candidate_adapter=not accepted, version=version,
                     detail={"command": [str(cli), "acp"],
                             "desktop_path": str(desktop) if desktop else None},
                     note=(f"{version} ACP 文件任务与原生回执已实测；保留默认权限"
                           if accepted else "找到 OpenCode ACP；此版本仍需任务验收，可用技能接手"))
    return Probe("opencode", "gui", installed=desktop is not None, can_spawn=False,
                 path=str(desktop) if desktop else None, running="opencode.exe" in running,
                 note="OpenCode 桌面技能接手" if desktop else "未检测到 OpenCode")


def detect_all() -> dict[str, Probe]:
    """探测本机所有 agent。任何单个探测失败都不会影响其它。"""
    running = _running_images()
    installs = _registry_installs()

    found: dict[str, Probe] = {}

    def safe(fn: Callable[[], Any], label: str) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            found[f"__error__{label}"] = Probe(
                name=f"__error__{label}", kind="error", note=f"探测失败：{exc}")
            return None

    cx = safe(lambda: probe_codex(running), "codex")
    if cx is not None:
        found[cx.name] = cx

    for pr in safe(lambda: probe_generic_clis(running), "cli") or []:
        found.setdefault(pr.name, pr)

    http = safe(probe_http_llm, "http-llm")
    if http is not None:
        found[http.name] = http

    for pr in safe(lambda: probe_gui_agents(installs, running), "gui") or []:
        found.setdefault(pr.name, pr)
    oc = safe(lambda: probe_opencode(installs, running), "opencode")
    if oc is not None:
        found[oc.name] = oc

    return found


def detect_group_agents() -> dict[str, Probe]:
    """Detect group products without probing API credentials.

    This is the launcher's automatic startup probe. It deliberately excludes
    generic HTTP/API channels and only inspects installed product entrypoints.
    """
    running = _running_images()
    installs = _registry_installs()
    found: dict[str, Probe] = {}
    try:
        codex = probe_codex(running)
        found[codex.name] = codex
    except (OSError, RuntimeError, ValueError) as exc:
        found["codex"] = Probe("codex", "error", note=f"探测失败：{exc}")
    try:
        for probe in probe_gui_agents(installs, running):
            found[probe.name] = probe
    except (OSError, RuntimeError, ValueError) as exc:
        found["__error__products"] = Probe(
            "__error__products", "error", note=f"桌面 Agent 探测失败：{exc}")
    try:
        opencode = probe_opencode(installs, running)
        found[opencode.name] = opencode
    except (OSError, RuntimeError, ValueError) as exc:
        found["opencode"] = Probe("opencode", "error", note=f"探测失败：{exc}")
    return found


def summary(probes: dict[str, Probe]) -> dict[str, Any]:
    items = [p.to_dict() for p in probes.values()]
    spawnable = sorted(p.name for p in probes.values() if p.can_spawn)
    candidates = sorted(p.name for p in probes.values()
                       if p.candidate_adapter and not p.can_spawn)
    gui = sorted(p.name for p in probes.values() if p.kind == "gui")
    return {
        "spawnable": spawnable,
        "candidate_adapters": candidates,
        "gui": gui,
        "installed": sorted(p.name for p in probes.values() if p.installed),
        "probes": items,
    }


def render(probes: dict[str, Probe]) -> str:
    lines = ["本机 agent 探测结果", "=" * 68]
    groups = [
        ("已找到命令入口（实际任务需验收）", lambda p: p.can_spawn and p.kind != "gui"),
        ("发现候选入口（尚未验收，仍需人工接手）",
         lambda p: p.candidate_adapter and not p.can_spawn),
        ("只能人工接手（GUI 应用）", lambda p: p.kind == "gui"),
    ]
    for title, pred in groups:
        rows = [p for p in probes.values() if pred(p) and p.installed]
        lines.append(f"\n【{title}】{len(rows)} 个")
        if not rows:
            lines.append("  （无）")
        for p in sorted(rows, key=lambda x: x.name):
            flag = "●运行中" if p.running else "○未启动"
            ver = f"  v{p.version}" if p.version and p.kind != "http-llm" else ""
            lines.append(f"  {p.name:<16}{flag}  {p.note}{ver}")
            if p.path:
                lines.append(f"      {p.path}")
    missing = [p for p in probes.values()
               if not p.installed and not p.name.startswith("__")]
    if missing:
        lines.append(f"\n【未检测到】{len(missing)} 个："
                     + ", ".join(sorted(p.name for p in missing)))
    errs = [p for p in probes.values() if p.kind == "error"]
    if errs:
        lines.append("\n【探测异常】")
        for p in errs:
            lines.append(f"  {p.note}")
    return "\n".join(lines)


def to_config_patch(probes: dict[str, Probe]) -> dict[str, Any]:
    """把探测结果转成可以合进 config.json 的 workers 片段。

    只对「确实存在」的 agent 生成条目；GUI 一律 human_in_loop=true。
    """
    workers: dict[str, Any] = {}
    for name, p in probes.items():
        if name.startswith("__") or not p.installed:
            continue
        if p.kind == "gui":
            workers[name] = {"enabled": True, "can_spawn": False,
                             "human_in_loop": True, "kind": "gui",
                             "path": p.path, "note": p.note}
        elif p.kind == "http-llm":
            # 默认关闭：调用会产生费用，让用户自己决定
            workers[name] = {"enabled": False, "can_spawn": True,
                             "kind": "http-llm", "base_url": p.detail.get("base_url", ""),
                             "token_source": p.detail.get("token_source", ""),
                             "note": p.note + "（默认关闭，开启前请确认额度）"}
        elif p.kind == "codex-sdk":
            workers[name] = {"enabled": True, "can_spawn": True, "kind": "codex-sdk",
                             "timeout_s": 1800, "model": None, "path": p.path}
        elif p.kind == "codex-cli":
            workers[name] = {"enabled": True, "can_spawn": True, "kind": "codex-cli",
                             "sandbox": "workspace-write", "timeout_s": 1800,
                             "model": None, "path": p.path}
        elif p.kind == "acp":
            accepted = bool(p.can_spawn and not p.candidate_adapter)
            workers[name] = {
                "enabled": True, "can_spawn": accepted, "kind": "acp",
                "command": p.detail["command"], "timeout_s": 1800,
                "startup_timeout_s": 20, "human_in_loop": not accepted,
                "candidate_command": (p.detail["command"]
                                      if p.candidate_adapter else None),
                "note": p.note,
            }
        else:
            workers[name] = {"enabled": True, "can_spawn": True, "kind": "cli",
                             "command": [p.path, "-p", "{prompt}"], "timeout_s": 1800,
                             "path": p.path, "note": p.note}
    return workers
