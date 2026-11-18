#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_tools_quality.py: 工具链质量门禁检查脚本。

来源：§10.11 AUDIT-360-TOOLS-QUALITY-GATE（P1）。
检查项：
1. tools/ 目录下所有 .py 脚本是否都在 tools/README.md 中登记；
2. tools/ 目录下所有 .py 脚本是否都在单元测试中被覆盖；
3. 新增工具若缺少 README 条目或测试，退出码非零。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def audit_tools(tools_dir: Path, tests_dir: Path) -> dict:
    tools_dir = Path(tools_dir).resolve()
    tests_dir = Path(tests_dir).resolve()
    readme_path = tools_dir / "README.md"

    if not readme_path.exists():
        readme_content = ""
    else:
        readme_content = readme_path.read_text(encoding="utf-8")

    all_scripts = sorted([f.name for f in tools_dir.glob("*.py") if f.name != "__init__.py"])
    
    # 扫描 tests 目录下所有引用的工具脚本名
    test_files = list(tests_dir.glob("*.py"))
    test_contents = "\n".join(t.read_text(encoding="utf-8", errors="ignore") for t in test_files)

    results = []
    missing_docs = []
    missing_tests = []

    for s in all_scripts:
        stem = s[:-3]
        has_doc = f"`{s}`" in readme_content or s in readme_content
        has_test = s in test_contents or stem in test_contents

        if not has_doc:
            missing_docs.append(s)
        if not has_test:
            missing_tests.append(s)

        results.append({
            "script": s,
            "documented": has_doc,
            "tested": has_test,
        })

    passed = len(missing_docs) == 0 and len(missing_tests) == 0
    return {
        "schema_version": 1,
        "tools_count": len(all_scripts),
        "missing_docs": missing_docs,
        "missing_tests": missing_tests,
        "all_passed": passed,
        "details": results,
    }


def main(argv=None) -> int:
    default_tools = Path(__file__).resolve().parent
    default_tests = default_tools.parent / "tests"
    parser = argparse.ArgumentParser(description="工具链质量门禁检查 (check_tools_quality)")
    parser.add_argument("--tools-dir", default=str(default_tools), help="tools 目录")
    parser.add_argument("--tests-dir", default=str(default_tests), help="tests 目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    res = audit_tools(Path(args.tools_dir), Path(args.tests_dir))
    if args.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0 if res["all_passed"] else 1

    print("=" * 60)
    print("AgentBus 工具链质量门禁检查报告")
    print("=" * 60)
    print(f"清点脚本总数: {res['tools_count']} 个")
    print(f"缺少文档登记: {len(res['missing_docs'])} 个")
    print(f"缺少测试覆盖: {len(res['missing_tests'])} 个")

    if not res["all_passed"]:
        if res["missing_docs"]:
            print("\n[!] 以下脚本未在 tools/README.md 中登记:")
            for m in res["missing_docs"]:
                print(f"  - {m}")
        if res["missing_tests"]:
            print("\n[!] 以下脚本未在测试套件中覆盖:")
            for m in res["missing_tests"]:
                print(f"  - {m}")
        print("\n[失败] 质量门禁未通过！")
        return 1

    print("\n[OK] 质量门禁全部通过！全部脚本均具备文档与最小测试。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
