#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境体检矩阵（§8.10）：doctor 的输出核心。

来源：§8.10 `AUDIT-TRAE-DOCTOR-MATRIX`（P2），依据 `成果/验收/20261011/
SYSTEM_REVIEW_TASK_REGISTER.md`。

问题：原 `doctor` 只自检 codex 路径与版本，错误信息**没有统一的「下一步」提示**
——用户看到"找不到"却不知道**该敲什么命令**。

本模块把体检结果做成**矩阵**：每行给出
    {项目, 状态(ok/warn/missing/error), 详情, 修复指引}
其中**修复指引是可执行命令**，而不是"请安装 Node.js"这类空话。

边界（硬约束）：
  - **只读**：不写文件、不装依赖、不改 PATH、不发起网络请求、不调用模型。
  - **不泄露秘密**：只报环境变量的**存在性/长度**，绝不打印其值（PATH 只报长度，
    凭据类变量只报"已设置/未设置"）。
  - 探测手段可注入（`runner`），便于测试在不触碰真实环境的前提下断言矩阵形状。

用法：
    python -m agentbus.doctor_matrix                 # 人可读矩阵
    python -m agentbus.doctor_matrix --json          # JSON
    python -m agentbus.doctor_matrix --workspace <项目路径>
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

OK = "ok"
WARN = "warn"
MISSING = "missing"
ERROR = "error"

# 需要"至少存在"的运行时。node 用于前端/工具链，python 用于本程序自身。
MIN_PYTHON = (3, 10)

# 本机常见的托管 Node 位置（不猜用户自定义路径；仅作提示，不作为唯一来源）。
_MANAGED_NODE_HINTS = ()


@dataclass
class Probe:
    """一行体检结果。`fix` 必须是**可执行**的下一步；没有可靠指引时留空。"""

    item: str
    status: str
    detail: str
    fix: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"item": self.item, "status": self.status, "detail": self.detail}
        if self.fix:
            out["fix"] = self.fix
        if self.extra:
            out["extra"] = dict(self.extra)
        return out


def _run(command, *, timeout=10):
    """默认探测执行器。只读：捕获输出，不继承交互式 stdin。"""
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    if proc.returncode != 0:
        return None, (proc.stderr or "").strip()
    return (proc.stdout or "").strip(), ""


def probe_python() -> Probe:
    version = "%d.%d.%d" % sys.version_info[:3]
    exe = sys.executable or "python"
    if sys.version_info[:2] < MIN_PYTHON:
        return Probe(
            item="python", status=WARN,
            detail="%s（低于要求的 %d.%d）" % (version, *MIN_PYTHON),
            fix="升级 Python 到 %d.%d+ 后重开终端：python --version" % MIN_PYTHON,
            extra={"version": version, "executable": exe,
                   "frozen": bool(getattr(sys, "frozen", False))})
    return Probe(
        item="python", status=OK, detail=version,
        extra={"version": version, "executable": exe,
               "interpreter": "frozen" if getattr(sys, "frozen", False) else "source"})


def probe_node(*, runner=None, which=None) -> Probe:
    """node 体检。缺失时给出**可执行**修复指引。"""
    which = which or shutil.which
    runner = runner or _run
    found = which("node") or which("node.exe")
    if not found:
        managed = [p for hint in _MANAGED_NODE_HINTS if Path(hint).is_dir()
                   for p in sorted(Path(hint).glob("*/node.exe"))
                   if p.is_file()]
        hint = managed[-1].as_posix() if managed else ""
        fix_parts = ["安装 Node.js 22 LTS（https://nodejs.org/），或复用本机托管运行时："]
        if hint:
            fix_parts.append('set PATH=%s;%%PATH%%  然后 node --version'
                             % str(Path(hint).parent))
        else:
            fix_parts.append('确认 node.exe 所在目录后执行 set PATH=<node目录>;%PATH%，然后执行 node --version')
        return Probe(item="node", status=MISSING,
                     detail="未在 PATH 中找到 node",
                     fix="\n".join(fix_parts),
                     extra={"managed_candidates": [p.as_posix() for p in managed]})
    out, err = runner([found, "--version"])
    version = (out or "").strip() or "未知"
    if err:
        return Probe(item="node", status=WARN,
                     detail="%s（%s）" % (found, version),
                     fix="node 可执行但版本查询失败；重装 Node.js 22 LTS",
                     extra={"path": found})
    return Probe(item="node", status=OK, detail="%s（%s）" % (version, found),
                 extra={"path": found, "version": version})


def probe_codex(*, resolver=None) -> Probe:
    resolver = resolver or _resolve_codex
    try:
        exe = resolver()
    except Exception as exc:                      # noqa: BLE001 - 探测失败即降级
        return Probe(item="codex", status=WARN, detail="解析失败：%s" % type(exc).__name__,
                     fix="运行 `agentbus doctor --env` 查看 PATH/PYTHONPATH 诊断")
    if not exe:
        return Probe(item="codex", status=MISSING, detail="未找到 codex.exe",
                     fix="安装 Codex CLI，或设置 ~/.codex/config.toml 的 CODEX_CLI_PATH；"
                         "纯文件协作可忽略此项")
    return Probe(item="codex", status=OK, detail=str(exe), extra={"path": str(exe)})


def _resolve_codex():
    """延迟导入，避免体检模块在缺依赖时连带失败。"""
    from .adapters import resolve_codex
    return resolve_codex()


def probe_workspace(workspace) -> Probe:
    """工作空间关键路径体检。只读。"""
    if workspace is None:
        return Probe(item="workspace", status=WARN, detail="未提供 --workspace",
                     fix="加 --workspace <项目路径> 以检查项目目录与控制目录")
    path = Path(workspace)
    if not path.exists():
        return Probe(item="workspace", status=MISSING,
                     detail="目录不存在：%s" % path,
                     fix="确认路径存在，或运行 `agentbus init --workspace <路径>` 初始化")
    if not path.is_dir():
        return Probe(item="workspace", status=ERROR, detail="不是目录：%s" % path,
                     fix="--workspace 必须指向目录")
    control = path / ".agentbus"
    parts = []
    status = OK
    if control.is_dir():
        parts.append("控制目录 .agentbus 存在")
    else:
        status = WARN
        parts.append("控制目录 .agentbus 缺失")
    return Probe(item="workspace", status=status, detail="；".join(parts),
                 fix="" if status == OK else
                 "运行 `agentbus init --workspace %s` 建立控制目录" % path,
                 extra={"path": path.as_posix(), "control": control.as_posix(),
                        "control_exists": control.is_dir()})


def probe_environment() -> Probe:
    """环境变量体检。**只报存在性/长度，绝不打印值。**"""
    proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    set_proxies = [k for k in proxy_keys if os.environ.get(k)]
    path_len = len(os.environ.get("PATH", ""))
    status = OK
    detail = []
    fix = ""
    if path_len == 0:
        status = WARN
        detail.append("PATH 为空")
        fix = "检查系统环境变量 PATH 是否被清空"
    else:
        detail.append("PATH 长度 %d" % path_len)
    if set_proxies:
        status = WARN if status == OK else status
        detail.append("检测到代理变量已设置（%s）" % ", ".join(sorted(set_proxies)))
        fix = fix or ('如遇连接异常先清代理：set HTTP_PROXY= & set HTTPS_PROXY= '
                      '& set http_proxy= & set https_proxy=')
    return Probe(item="environment", status=status, detail="；".join(detail),
                 fix=fix,
                 extra={"path_length": path_len, "proxy_vars_set": sorted(set_proxies),
                        "os_name": platform.system(), "os_release": platform.release()})


def build_matrix(*, workspace=None, runner=None, which=None, resolver=None) -> list:
    """构建体检矩阵。只读、顺序固定（便于快照比对）。"""
    return [
        probe_python(),
        probe_node(runner=runner or _run, which=which or shutil.which),
        probe_codex(resolver=resolver),
        probe_workspace(workspace),
        probe_environment(),
    ]


def summarize(probes) -> dict:
    counts = {OK: 0, WARN: 0, MISSING: 0, ERROR: 0}
    for probe in probes:
        counts[probe.status] = counts.get(probe.status, 0) + 1
    overall = OK
    for level in (ERROR, MISSING, WARN):
        if counts.get(level):
            overall = level
            break
    return {"overall": overall, "counts": counts}


def as_dict(probes, *, workspace=None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "environment_matrix",
        "workspace": str(workspace) if workspace else None,
        "summary": summarize(probes),
        "items": [probe.to_dict() for probe in probes],
    }


_MARK = {OK: "[ OK ]", WARN: "[WARN]", MISSING: "[MISS]", ERROR: "[FAIL]"}


def render(probes) -> str:
    """确定性文本矩阵；缺失项下方缩进打印可执行修复指引。"""
    summary = summarize(probes)
    lines = ["环境体检矩阵（只读；不写文件、不装依赖、不改环境）", ""]
    width = max((len(p.item) for p in probes), default=8)
    for probe in probes:
        lines.append("%s %-*s  %s" % (_MARK.get(probe.status, "[????]"),
                                      width, probe.item, probe.detail))
        if probe.fix:
            for fix_line in probe.fix.splitlines():
                lines.append("       → " + fix_line)
    counts = summary["counts"]
    lines.append("")
    lines.append("汇总：overall=%s  ok=%d warn=%d missing=%d error=%d"
                 % (summary["overall"], counts[OK], counts[WARN],
                    counts[MISSING], counts[ERROR]))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="环境体检矩阵（只读）")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    probes = build_matrix(workspace=args.workspace)
    if args.json:
        json.dump(as_dict(probes, workspace=args.workspace), sys.stdout,
                  ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(render(probes))
    return 0 if summarize(probes)["overall"] in (OK, WARN) else 1


if __name__ == "__main__":
    raise SystemExit(main())
