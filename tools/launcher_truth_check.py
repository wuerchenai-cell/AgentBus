#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launcher_truth_check.py: 正式入口唯一真源门禁（只读校验）。

来源：§10.21 AUDIT-360-LAUNCHER-SINGLE-SOURCE（P1）。

要解决的问题
------------
`AGENTS.md`「唯一正式执行程序铁律」把 `.agentbus/workflow.json` 的 `launcher`
登记值声明为正式程序身份（字节数 + SHA256）的**唯一真源**。但登记本身没有门禁：

* `launcher.mode` 一度由 `frozen` 退回 `source`，`bytes` / `sha256` 连同正式
  `成果/AgentBus.exe` 的身份**一起消失**，而全项目没有任何地方会发现这件事；
* §7.38 复核时该字段曾被人补上，随后又消失——说明它只靠"人记得"。

本工具把"登记必须自洽、且必须与 `AGENTS.md` 铁律所指的正式程序一致"变成可自动
执行的判定：**登记与铁律不得互相矛盾，且矛盾必须能被自动发现**。

判定规则（`launcher.mode` 的合法取值与必备字段）
------------------------------------------------
| mode | 必备字段 | 说明 |
|---|---|---|
| `frozen` | `bytes`(正整数) + `sha256`(64 位十六进制) | 冻结单文件即正式程序本体 |
| `source` | `python`(非空) + `entrypoint`(非空) | 另须登记正式 EXE 身份（见下） |

无论哪种 mode，**正式程序身份（bytes + sha256）都必须可从 `launcher` 读到**，
且必须知道它**描述的是哪个文件**：

* 顶层写法：`launcher.bytes` + `launcher.sha256` + `launcher.formal_binary_path`；
* 嵌套写法：`launcher.formal_binary = {path, bytes, sha256}`。

两种写法可同时使用，但必须**互相一致**。顶层写法缺 `formal_binary_path` 时，
若铁律只声明了一个 `*.exe`，则自动绑定到该程序（并记一条 note）。

铁律一致性
----------
工具从 `AGENTS.md` 的「唯一正式执行程序铁律」小节里提取被反引号包裹的 `*.exe`
路径（即铁律所指的正式程序），要求：

1. 登记里声明的正式程序路径（`formal_binary.path`，缺省时取铁律路径）**基名一致**；
2. 该路径的身份（bytes + sha256）**已被登记**。

`--verify-files` 时再与磁盘上的真实文件对账（大小 + 摘要）；文件缺失记为
「不可核验」，`--require-files` 时升级为错误。

纪律
----
* **只读**：不写任何文件。
* **不决定业务**：本工具不判断"正式入口应该是 EXE 还是 source"——那是发布负责人
  的决定；它只负责让"铁律与登记不一致"这件事**不可能静默存在**。
* `--strict`：判定非 pass 时以退出码 1 结束（供 CI / 发布门使用）。

用法
----
    python tools/launcher_truth_check.py [--workspace .] [--json] [--strict]
                                         [--verify-files] [--require-files]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

VALID_MODES = ("frozen", "source")

DOCTRINE_HEADING = "唯一正式执行程序铁律"
_EXE_IN_BACKTICKS = re.compile(r"`([^`\n]*\.exe)`")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def doctrine_section(agents_md: str) -> str:
    """截取 AGENTS.md 的「唯一正式执行程序铁律」小节正文（到下一个同级标题为止）。"""
    if not agents_md:
        return ""
    lines = agents_md.splitlines()
    start = None
    for index, line in enumerate(lines):
        if DOCTRINE_HEADING in line and line.lstrip().startswith("#"):
            start = index
            break
    if start is None:
        return ""
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def declared_formal_programs(agents_md: str) -> list[str]:
    """铁律小节里被反引号声明的正式程序路径（去重、保序）。"""
    out: list[str] = []
    for match in _EXE_IN_BACKTICKS.findall(doctrine_section(agents_md)):
        candidate = match.strip()
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _read_identity(payload: Any, label: str, errors: list[dict], prefix: str) -> dict | None:
    """校验一份 {bytes, sha256} 登记；不合法时把原因写进 errors 并返回 None。"""
    if not isinstance(payload, dict):
        return None
    broken = False
    size = payload.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        errors.append({"code": prefix + "_missing_bytes", "detail":
                       "%s 的 bytes 必须是正整数，实测 %r" % (label, size)})
        broken = True
    digest = payload.get("sha256")
    if not isinstance(digest, str) or not _HEX64.match(digest):
        errors.append({"code": prefix + "_missing_sha256", "detail":
                       "%s 的 sha256 必须是 64 位十六进制，实测 %r" % (label, digest)})
        broken = True
    if broken:
        return None
    return {"bytes": size, "sha256": digest.lower()}


def _basename(ref: str) -> str:
    return Path(str(ref).replace("\\", "/")).name.lower()


def _resolve(root: Path, ref: str) -> Path:
    text = str(ref).replace("\\", "/").strip()
    candidate = Path(text)
    return candidate if candidate.is_absolute() else root / candidate


def check(workflow: dict, agents_md: str = "", root: Path | None = None,
          verify_files: bool = False, require_files: bool = False) -> dict[str, Any]:
    """核心判定：返回结构化报告（不抛异常，不写文件）。"""
    errors: list[dict] = []
    notes: list[dict] = []
    unverifiable: list[dict] = []

    declared = declared_formal_programs(agents_md)
    launcher = workflow.get("launcher")
    if not isinstance(launcher, dict):
        errors.append({"code": "launcher_missing",
                       "detail": "workflow.json 缺少 launcher 登记对象"})
        return _report("", [], declared, errors, notes, unverifiable)

    mode = launcher.get("mode")
    if not isinstance(mode, str) or mode not in VALID_MODES:
        errors.append({"code": "launcher_mode_invalid",
                       "detail": "launcher.mode 必须是 %s 之一，实测 %r"
                                 % (" 或 ".join(VALID_MODES), mode)})
        mode = mode if isinstance(mode, str) else ""

    if mode == "source":
        for field in ("python", "entrypoint"):
            value = launcher.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append({"code": "source_missing_" + field,
                               "detail": "mode=source 时 launcher.%s 必须是非空字符串，实测 %r"
                                         % (field, value)})

    # ---- 正式程序身份：顶层 / formal_binary 两处登记 ----
    registrations: list[dict] = []

    if "bytes" in launcher or "sha256" in launcher:
        top = _read_identity(launcher, "launcher 顶层", errors, "formal_identity")
        top_path: str | None = None
        raw_top_path = launcher.get("formal_binary_path")
        if raw_top_path is not None:
            if isinstance(raw_top_path, str) and raw_top_path.strip():
                top_path = raw_top_path.strip()
            else:
                errors.append({"code": "formal_binary_path_invalid",
                               "detail": "launcher.formal_binary_path 必须是非空字符串，实测 %r"
                                         % (raw_top_path,)})
        if top is not None:
            registrations.append({"source": "launcher", "path": top_path, **top})

    nested_raw = launcher.get("formal_binary")
    nested_path: str | None = None
    if nested_raw is not None:
        if not isinstance(nested_raw, dict):
            errors.append({"code": "formal_binary_not_object",
                           "detail": "launcher.formal_binary 必须是对象，实测 %r" % (nested_raw,)})
        else:
            raw_path = nested_raw.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                nested_path = raw_path.strip()
            else:
                errors.append({"code": "formal_binary_missing_path",
                               "detail": "launcher.formal_binary 必须显式声明 path"
                                         "（说明 bytes/sha256 描述的是哪个文件）"})
            nested = _read_identity(nested_raw, "launcher.formal_binary", errors, "formal_binary")
            if nested is not None:
                registrations.append({"source": "formal_binary", "path": nested_path, **nested})

    if not registrations:
        errors.append({"code": "formal_binary_identity_unregistered",
                       "detail": "launcher 未登记任何正式程序身份：顶层 bytes/sha256 与 "
                                 "launcher.formal_binary 均缺失（mode=%r）" % (mode,)})

    if mode == "frozen" and not any(r["source"] == "launcher" for r in registrations):
        if not any(e["code"].startswith("formal_identity") for e in errors):
            errors.append({"code": "frozen_missing_identity",
                           "detail": "mode=frozen 时 launcher 顶层必须给出 bytes 与 sha256"})

    top_reg = next((r for r in registrations if r["source"] == "launcher"), None)
    nested_reg = next((r for r in registrations if r["source"] == "formal_binary"), None)
    if top_reg and nested_reg and (top_reg["bytes"], top_reg["sha256"]) != (nested_reg["bytes"], nested_reg["sha256"]):
        errors.append({"code": "formal_identity_conflict",
                       "detail": "launcher 顶层 {bytes=%s, sha256=%s} 与 launcher.formal_binary "
                                 "{bytes=%s, sha256=%s} 不一致"
                                 % (top_reg["bytes"], top_reg["sha256"], nested_reg["bytes"], nested_reg["sha256"])})

    # 显式路径：`launcher.formal_binary_path`（顶层身份）与 `launcher.formal_binary.path` 等价。
    # 顶层身份若未显式声明路径，则在铁律只声明一个正式程序时绑定到它；否则记提示。
    if top_reg is not None and top_reg["path"] is None:
        if len(declared) == 1:
            top_reg["path"] = declared[0]
        else:
            notes.append({"code": "top_identity_path_implicit",
                          "detail": "launcher 顶层登记了身份但未声明它描述哪个文件；"
                                    "请补 launcher.formal_binary_path 或 launcher.formal_binary.path"})

    # ---- 铁律一致性 ----
    if not declared:
        notes.append({"code": "doctrine_not_found",
                      "detail": "未在 AGENTS.md 中找到「%s」小节或其中的 *.exe 声明，铁律对齐检查跳过"
                                % DOCTRINE_HEADING})
    declared_names = {_basename(target) for target in declared}
    for reg in registrations:
        if reg["path"] and declared_names and _basename(reg["path"]) not in declared_names:
            errors.append({"code": "formal_binary_path_mismatch",
                           "detail": "登记路径 %s 与铁律所指正式程序（%s）基名均不一致"
                                     % (reg["path"], "、".join(declared))})
    for target in declared:
        owners = [r["source"] for r in registrations if r["path"] and _basename(r["path"]) == _basename(target)]
        if not owners:
            errors.append({"code": "formal_binary_identity_unregistered",
                           "detail": "铁律所指正式程序 %s 未在 launcher 中登记 bytes/sha256" % target})
        else:
            notes.append({"code": "doctrine_aligned",
                          "detail": "铁律所指正式程序 %s 的身份已在 %s 登记"
                                    % (target, "、".join(owners))})

    # ---- 可选：与磁盘真实文件对账 ----
    if verify_files and root is not None:
        for reg in registrations:
            if not reg["path"]:
                continue
            path = _resolve(root, reg["path"])
            if not path.is_file():
                unverifiable.append({"path": reg["path"], "source": reg["source"],
                                     "detail": "登记存在但磁盘上找不到该文件，无法核验"})
                if require_files:
                    errors.append({"code": "formal_binary_file_missing",
                                   "detail": "%s 登记存在但磁盘缺文件" % reg["path"]})
                continue
            actual = {"bytes": path.stat().st_size, "sha256": _sha256_of(path)}
            if actual["bytes"] != reg["bytes"] or actual["sha256"] != reg["sha256"]:
                errors.append({"code": "formal_binary_digest_mismatch",
                               "detail": "%s: 登记 {bytes=%s, sha256=%s} 与磁盘实测 "
                                         "{bytes=%s, sha256=%s} 不一致"
                                         % (reg["path"], reg["bytes"], reg["sha256"],
                                            actual["bytes"], actual["sha256"])})
            else:
                notes.append({"code": "formal_binary_verified",
                              "detail": "%s: 登记与磁盘实测一致（%d 字节）" % (reg["path"], actual["bytes"])})

    return _report(mode, registrations, declared, errors, notes, unverifiable)


def _report(mode, registrations, declared, errors, notes, unverifiable) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "launcher_mode": mode,
        "registered_identities": registrations,
        "doctrine_declared_programs": declared,
        "errors": errors,
        "notes": notes,
        "unverifiable": unverifiable,
        "verdict": "pass" if not errors else "contradiction",
    }


def render(report: dict[str, Any]) -> str:
    lines = ["正式入口唯一真源门禁（只读）", "=" * 62]
    lines.append("launcher.mode          : %s" % (report.get("launcher_mode") or "（缺失/非法）"))
    regs = report.get("registered_identities") or []
    if regs:
        for reg in regs:
            lines.append("已登记身份 [%-14s]: path=%s bytes=%s sha256=%s"
                         % (reg["source"], reg.get("path") or "（未声明）",
                            reg["bytes"], reg["sha256"][:16] + "…"))
    else:
        lines.append("已登记身份             : （无）")
    declared = report.get("doctrine_declared_programs") or []
    lines.append("AGENTS.md 铁律所指程序 : %s" % ("、".join(declared) if declared else "（未提取到）"))
    lines.append("")
    if report["errors"]:
        lines.append("【判红】登记与铁律存在矛盾（%d 项）" % len(report["errors"]))
        for err in report["errors"]:
            lines.append("  - [%s] %s" % (err["code"], err["detail"]))
    else:
        lines.append("【通过】登记自洽且与 AGENTS.md 铁律一致。")
    if report["unverifiable"]:
        lines.append("")
        lines.append("【不可核验】登记存在但磁盘缺文件（%d 项）" % len(report["unverifiable"]))
        for item in report["unverifiable"]:
            lines.append("  - %s（来源 %s）" % (item.get("path"), item.get("source")))
    lines.append("")
    lines.append("判定: %s" % report["verdict"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", default=".", help="工作空间目录（含 .agentbus/）")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--strict", action="store_true",
                        help="判定非 pass 时以非零退出（供 CI / 发布门使用）")
    parser.add_argument("--verify-files", action="store_true",
                        help="与磁盘上的真实文件对账大小与摘要")
    parser.add_argument("--require-files", action="store_true",
                        help="磁盘缺文件时判红（默认只记为不可核验）")
    args = parser.parse_args(argv)

    root = Path(args.workspace).resolve()
    wf_path = root / ".agentbus" / "workflow.json"
    if not wf_path.is_file():
        print("缺少输入文件：%s" % wf_path, file=sys.stderr)
        return 2

    try:
        workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("读取失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 2

    md_text = ""
    agents_md = root / "AGENTS.md"
    if agents_md.is_file():
        try:
            md_text = agents_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print("读取 AGENTS.md 失败：%s" % exc, file=sys.stderr)

    report = check(workflow, md_text, root,
                   verify_files=args.verify_files, require_files=args.require_files)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    if args.strict and report["verdict"] != "pass":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
