#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""§7.24-② 项目技能清单字段对齐审计（**只读**）。

任务书原文：「技能清单字段对齐 Agent Skills 开放标准（补 license / compatibility /
allowed-tools）」。本工具把"对齐"变成**可复现的检查**，而不是一次性手工补字段：

  1. 枚举项目内 `.codebuddy/skills/*/SKILL.md`；
  2. 逐个解析 YAML frontmatter，核对必需字段是否齐备；
  3. **不把"未登记"当成合格**：`license` 若只是占位说明，单独标为 `needs_license_audit`；
  4. 输出人类可读报告或 `--json`（便于 Release Gate 消费）。

用法：
    python tools/skill_manifest_audit.py
    python tools/skill_manifest_audit.py --json
    python tools/skill_manifest_audit.py --strict      # 有未登记项即退出码 1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _find_skills_root() -> Path:
    """向上找第一个含 `.codebuddy/skills` 的目录（避免写死相对层级）。"""
    for base in [ROOT, *ROOT.parents]:
        candidate = base / ".codebuddy" / "skills"
        if candidate.is_dir():
            return candidate
    return ROOT / ".codebuddy" / "skills"


SKILLS_ROOT = _find_skills_root()

# 必需字段（任务书点名的三项 + 本地事实标准要求的 name/description）
REQUIRED_FIELDS = ("name", "description", "license", "allowed-tools", "compatibility")
# 这些值不构成有效的许可声明，只能算"显式未登记"
PLACEHOLDER_LICENSE = re.compile(r"未登记|unlicensed|TODO|FIXME|N/A|none", re.IGNORECASE)


def project_skills(skills_root: Path) -> list[Path]:
    if not skills_root.is_dir():
        return []
    return sorted(p for p in skills_root.glob("*/SKILL.md") if p.is_file())


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """取 YAML frontmatter 段；解析失败时返回 ({}, 原因)，不抛异常。"""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not match:
        return {}, "缺少 YAML frontmatter"
    block = match.group(1)
    fields: dict = {}
    current_key = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1] in (" ", "\t") and current_key:      # 续行
            fields[current_key] = str(fields[current_key]) + " " + raw.strip()
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = [item.strip().strip("'\"")
                           for item in inner.split(",") if item.strip()]
        else:
            fields[key] = value.strip("'\"")
        current_key = key
    return fields, ""


def audit_skill(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    fields, error = parse_frontmatter(text)
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    empty = [name for name in REQUIRED_FIELDS
             if name in fields and (fields[name] in ("", None, [], "null"))]

    license_value = fields.get("license", "")
    needs_license_audit = bool(PLACEHOLDER_LICENSE.search(str(license_value)))

    allowed = fields.get("allowed-tools")
    if isinstance(allowed, str):
        allowed = [item for item in re.split(r"[,\s]+", allowed) if item]
    compatibility = fields.get("compatibility")
    if isinstance(compatibility, str):
        compatibility = [item for item in re.split(r"[,\s]+", compatibility) if item]

    verdict = "ok"
    if error:
        verdict = "malformed"
    elif missing or empty:
        verdict = "missing_fields"
    elif needs_license_audit:
        verdict = "needs_license_audit"

    return {
        "skill": path.parent.name,
        "path": str(path),
        "bytes": path.stat().st_size,
        "fields_present": sorted(fields),
        "missing": missing,
        "empty": empty,
        "license": str(license_value),
        "needs_license_audit": needs_license_audit,
        "allowed_tools": allowed if isinstance(allowed, list) else [],
        "compatibility": compatibility if isinstance(compatibility, list) else [],
        "verdict": verdict,
        "error": error,
    }


def audit(skills_root: Path) -> dict:
    skills = project_skills(skills_root)
    rows = [audit_skill(path) for path in skills]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    return {
        "schema_version": 1,
        "read_only": True,
        "skills_root": str(skills_root),
        "required_fields": list(REQUIRED_FIELDS),
        "count": len(rows),
        "verdicts": counts,
        "skills": rows,
        "note": ("verdict=ok 表示字段齐备且 license 不是占位说明；"
                 "needs_license_audit 表示字段在、但许可尚未正式登记，"
                 "**不得**据此认为该技能可对外分发。"),
    }


def main(argv=None) -> int:
    # Windows PowerShell 5.1 commonly starts Python with a GBK console stream.
    # This report intentionally contains Chinese and warning symbols; force a
    # stable UTF-8 CLI contract so strict audit failures remain actionable
    # exit codes instead of turning into UnicodeEncodeError tracebacks.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")

    parser = argparse.ArgumentParser(description="§7.24 项目技能清单字段审计（只读）")
    parser.add_argument("--skills-root", default=str(SKILLS_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true",
                        help="存在 missing_fields/malformed/needs_license_audit 即退出码 1")
    args = parser.parse_args(argv)

    report = audit(Path(args.skills_root))
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print("技能根目录：%s" % report["skills_root"])
        print("必需字段：%s" % ", ".join(report["required_fields"]))
        print("项目内技能：%d 个    判定分布：%s"
              % (report["count"], report["verdicts"] or "（无）"))
        for row in report["skills"]:
            print("")
            print("  %-20s %s  (%d B)" % (row["skill"], row["verdict"], row["bytes"]))
            if row["missing"]:
                print("     缺字段: %s" % ", ".join(row["missing"]))
            if row["empty"]:
                print("     空字段: %s" % ", ".join(row["empty"]))
            print("     license          : %s" % row["license"])
            print("     allowed-tools    : %s" % (", ".join(row["allowed_tools"]) or "（空）"))
            print("     compatibility    : %s" % (", ".join(row["compatibility"]) or "（空）"))
            if row["needs_license_audit"]:
                print("     ⚠ license 仍为占位说明 → 对外分发前必须完成 license audit")
    if args.strict and any(row["verdict"] != "ok" for row in report["skills"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
