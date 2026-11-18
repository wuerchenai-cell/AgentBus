#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli_surface_check.py: CLI 子命令表面完整性与路径净化检查工具。

来源：§10.10 AUDIT-360-CLI-COGNITIVE-LOAD（P1）。
检查项：
1. 批量检验 CLI 顶层子命令的可用性与退出码语义；
2. 验证 --help 包含高频 5 场景快速入口与功能域分组；
3. 验证异常输入时不泄露内部工具链路径（如 PortableGit）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def get_all_subcommands() -> list[str]:
    # 高频及维护命令清单；新增维护入口同时进入表面回归。
    return [
        "test", "tree", "verify", "timeline", "attest", "heartbeat", "init", "config",
        "benchmark", "compact", "host-setup", "status", "boss", "run", "inbox", "claim",
        "claim-next", "wait", "worker", "escalations", "handoff", "answer", "recover", "retire",
        "doctor", "preflight", "install-skill", "setup", "workflow", "team", "member",
        "share", "files", "skills", "plan", "detect", "gui", "mcp", "lease", "board",
        "gc", "post", "watchtower", "handshake"
    ]


ROOT = Path(__file__).resolve().parents[1]
import os

def run_surface_check(python_exe: str = sys.executable) -> dict:
    commands = get_all_subcommands()
    results = []
    failed = []
    sub_env = {**os.environ, "PYTHONPATH": str(ROOT)}

    # 1. 验证每个子命令的 --help 可用性
    for cmd in commands:
        r = subprocess.run(
            [python_exe, "-m", "agentbus.cli", cmd, "--help"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            cwd=str(ROOT),
            env=sub_env,
        )
        ok = r.returncode == 0 and "usage:" in (r.stdout + r.stderr).lower()
        if not ok:
            failed.append(cmd)
        results.append({
            "command": cmd,
            "returncode": r.returncode,
            "available": ok,
        })

    # 2. 验证路径净化
    test_err = subprocess.run(
        [python_exe, "-m", "agentbus.cli", "tree", "status", "--workspace", "/test_nonexistent"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        cwd=str(ROOT),
        env=sub_env,
    )
    err_output = test_err.stderr + test_err.stdout
    path_sanitized = "portablegit" not in err_output.lower() and "下一步建议" in err_output

    # 3. 验证 help 导航结构
    main_help_proc = subprocess.run(
        [python_exe, "-m", "agentbus.cli", "--help"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        cwd=str(ROOT),
        env=sub_env,
    )
    main_help = main_help_proc.stdout
    has_quick_start = "高频快速入口" in main_help
    has_domain_grouping = "功能域分组" in main_help

    all_passed = len(failed) == 0 and path_sanitized and has_quick_start and has_domain_grouping

    return {
        "schema_version": 1,
        "total_commands": len(commands),
        "failed_commands": failed,
        "path_sanitized": path_sanitized,
        "has_quick_start": has_quick_start,
        "has_domain_grouping": has_domain_grouping,
        "all_passed": all_passed,
        "results": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CLI 表面完整性与路径净化检查 (cli_surface_check)")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args(argv)

    res = run_surface_check()
    if args.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0 if res["all_passed"] else 1

    print("=" * 65)
    print("AgentBus CLI 表面完整性与认知负载检查")
    print("=" * 65)
    print(f"清点子命令总数: {res['total_commands']} 个")
    print(f"不可用子命令:   {len(res['failed_commands'])} 个")
    print(f"高频 5 场景入口: {'[OK] 存在' if res['has_quick_start'] else '[-] 缺失'}")
    print(f"功能域分组呈现: {'[OK] 存在' if res['has_domain_grouping'] else '[-] 缺失'}")
    print(f"路径泄露净化:   {'[OK] 已净化（含下一步建议）' if res['path_sanitized'] else '[-] 存在泄露'}")
    print("=" * 65)

    if not res["all_passed"]:
        print("[失败] 检查未通过！")
        return 1

    print("[OK] 全项检查 100% 满分通过！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
