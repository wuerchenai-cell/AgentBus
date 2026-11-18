#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""host_file_registration_check.py: `workflow.json` 登记与磁盘的一致性门禁（只读）。

来源：§10.21 的姊妹问题。§10.21 已把 `launcher`（正式 EXE 身份）钉住：
「登记与铁律不得互相矛盾，且矛盾必须能被自动发现」。但 `workflow.json` 里还有另一类
登记——`files` / `retired_files` 逐条记录了**宿主侧文件**（各成员的 `SKILL.md`、
`AGENTBUS.md` 等）的 SHA256。这类登记**没有任何门禁**：

* 宿主侧文件被同步/改写后，登记**静默过期**；
* 现有 `tools/skill_manifest_audit.py` 只校验 SKILL.md 的**必填字段**，
  **没有**逐条比对哈希（尽管其用途描述写着"规范与哈希一致性"）；
* 实测（2026-10-13 22:54）`.codebuddy/skills/agentbus-team/SKILL.md` 被改写后
  8 条登记中**恰好 1 条过期**，而全项目无任何一处会发现。

本门禁把这件事变成可自动发现的判定：**登记必须与磁盘一致；过期必须被指名。**

判定规则
--------
1. `files` 里的每条：文件必须存在，且 SHA256 必须与登记一致；
   * 不一致 → `stale_registration`（指明登记值与实测值）；
   * 不存在 → `missing_file`；
2. `retired_files` 里的每条：语义是"曾被登记、本次不再写入"的**审计留痕**，
   写入逻辑对磁盘上已存在且内容一致的文件明确**保留**；因此退役文件**应当仍存在**，
   不存在仅记 note；**存在但内容与登记不符**才算脱节（`retired_file_drift`）；
3. **复用 §10.21 的 `launcher_truth_check`** 对 `launcher` 做同一判定（不复制其规则）；
4. `--require-launcher`（默认开）：launcher 判定非 pass 时整体判红。

纪律
----
* **只读**：不写 `workflow.json`、不改任何宿主文件。
* **不代改登记**：本工具只报告"哪条过期了"；**更新登记应由改动该文件的成员负责**，
  否则等于替未审内容背书。
* `--strict`：判定非 pass 时退出码 1；缺输入退出码 2（无法验证 ≠ 通过）。

用法
----
    python tools/host_file_registration_check.py [--workspace .] [--json] [--strict]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent
_INTEGRATION = _TOOLS_DIR.parent
if str(_INTEGRATION) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_launcher_gate():
    """复用 §10.21 的 launcher 门禁（判据不复制）。"""
    try:
        import importlib.util
        path = _TOOLS_DIR / "launcher_truth_check.py"
        spec = importlib.util.spec_from_file_location("launcher_truth_check", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError) as exc:  # pragma: no cover - 环境异常
        return None, "无法加载 launcher_truth_check：%s: %s" % (type(exc).__name__, exc)


def check(workspace: Path, agents_md_text: str = "") -> dict[str, Any]:
    errors: list[dict] = []
    notes: list[dict] = []
    wf_path = workspace / ".agentbus" / "workflow.json"
    if not wf_path.is_file():
        return {"ok": False, "error": "缺少 .agentbus/workflow.json", "verdict": "unverifiable"}
    try:
        workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "读取 workflow.json 失败：%s" % exc, "verdict": "unverifiable"}

    for group in ("files", "retired_files"):
        entries = workflow.get(group) or {}
        if not isinstance(entries, dict):
            errors.append({"code": group + "_not_object",
                           "detail": "workflow.json 的 %s 必须是对象" % group})
            continue
        for rel, registered in sorted(entries.items()):
            path = workspace / rel.replace("\\", "/")
            if group == "files":
                if not path.is_file():
                    errors.append({"code": "missing_file", "path": rel,
                                   "detail": "登记的文件不存在：%s" % rel})
                    continue
                actual = sha256_of(path)
                if actual != registered:
                    errors.append({"code": "stale_registration", "path": rel,
                                   "registered": registered, "actual": actual,
                                   "detail": "登记过期：%s 登记 %s…，磁盘实测 %s…"
                                             % (rel, str(registered)[:16], actual[:16])})
                else:
                    notes.append({"code": "file_ok", "path": rel})
            else:
                # `retired_files` 的语义（见 agentbus/project_workflow.py 的 prepare）：
                #   retired = {rel: digest for rel, digest in owned.items() if rel not in outputs}
                # 即"曾被登记、但本次不再写入"的**审计留痕**；写入逻辑对磁盘上已存在且
                # 内容与登记一致的文件明确"已保留"。因此退役文件**应当仍然存在**，
                # 不存在才是异常；存在但内容与登记不符 = 留痕与磁盘脱节。
                if not path.is_file():
                    notes.append({"code": "retired_absent", "path": rel})
                else:
                    actual = sha256_of(path)
                    if actual != registered:
                        errors.append({"code": "retired_file_drift", "path": rel,
                                       "registered": registered, "actual": actual,
                                       "detail": "退役留痕与磁盘脱节：%s 登记 %s…，实测 %s…"
                                                 % (rel, str(registered)[:16], actual[:16])})
                    else:
                        notes.append({"code": "retired_preserved", "path": rel})

    launcher = _load_launcher_gate()
    launcher_verdict = "skipped"
    if isinstance(launcher, tuple):
        errors.append({"code": "launcher_gate_unavailable", "detail": launcher[1]})
        launcher_verdict = "unavailable"
    else:
        report = launcher.check(workflow, agents_md_text, workspace, verify_files=False)
        launcher_verdict = report.get("verdict")
        for err in report.get("errors") or []:
            errors.append({"code": "launcher:" + err["code"],
                           "detail": err["detail"], "path": "launcher"})
        if launcher_verdict != "pass":
            errors.append({"code": "launcher_verdict", "path": "launcher",
                           "detail": "launcher 判定 %s（详见 §10.21 门禁）" % launcher_verdict})

    return {
        "ok": not errors,
        "checked_files": len(workflow.get("files") or {}),
        "checked_retired": len(workflow.get("retired_files") or {}),
        "launcher_verdict": launcher_verdict,
        "errors": errors,
        "notes": notes,
        "verdict": "pass" if not errors else "contradiction",
    }


def render(report: dict) -> str:
    if "error" in report:
        return "[无法验证] %s\n\n判定: unverifiable（无法验证 ≠ 通过）" % report["error"]
    lines = ["workflow.json 登记一致性门禁（只读）", "=" * 62]
    lines.append("已核对 files %d 条 ｜ retired_files %d 条 ｜ launcher 判定 %s"
                 % (report.get("checked_files"), report.get("checked_retired"),
                    report.get("launcher_verdict")))
    lines.append("")
    if report["errors"]:
        lines.append("【判红】%d 项不一致" % len(report["errors"]))
        for err in report["errors"]:
            lines.append("  - [%s] %s" % (err["code"], err.get("path", "")))
            lines.append("      %s" % err["detail"])
            if "registered" in err and "actual" in err:
                lines.append("      登记=%s" % err["registered"])
                lines.append("      实测=%s" % err["actual"])
    else:
        lines.append("【通过】全部登记与磁盘一致，launcher 判定通过。")
    lines.append("")
    lines.append("判定: %s" % report["verdict"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="判定非 pass 时退出码 1")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    agents_md = workspace / "AGENTS.md"
    md_text = ""
    if agents_md.is_file():
        try:
            md_text = agents_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print("读取 AGENTS.md 失败：%s" % exc, file=sys.stderr)
    report = check(workspace, md_text)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    if args.strict and report.get("verdict") != "pass":
        return 1 if report.get("verdict") != "unverifiable" else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
