#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_layer_budget.py: 测试分层墙钟耗时预算与可移植性门禁工具。

来源：§10.12 AUDIT-360-TEST-PORTABILITY（P2）。
核心功能：
1. 定义分层墙钟耗时预算：
   - smoke: <= 30s
   - contract: <= 120s
   - full: 记录实测墙钟值与测试总数（基线 >= 2793）
   - release: 独立打包与冻结二进制验证层（单设 300s 超时与跳过条件）
2. 自动化执行指定分层测试，严格统计运行耗时、测试数、跳过数与失败数；
3. 超出预算或测试失败时，在 --check 模式下非零退出拦截；
4. 落地测试实测落盘报告。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 各层预算指标字典 (单位: 秒)
LAYER_BUDGETS = {
    "smoke": 30.0,
    "contract": 120.0,
    "release": 300.0,
    "full": 2400.0,  # 基线全量 34 分 13 秒 (~2053s)，预算上限 2400s
}

TIER_PATTERNS = {
    "smoke": "test_ux_*.py",
    "contract": "test_*contract*.py",
    "release": "test_release_tools.py",
    "full": "test_*.py",
}


def check_symlink_capability() -> dict:
    """探测当前主机环境是否支持创建符号链接。"""
    import tempfile
    can_symlink = False
    reason = "Supported"
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "probe_src.txt"
            dst = Path(td) / "probe_dst.txt"
            src.write_text("ok", encoding="utf-8")
            dst.symlink_to(src)
            can_symlink = True
    except OSError as exc:
        can_symlink = False
        reason = f"OS privilege missing: {exc}"

    return {
        "can_symlink": can_symlink,
        "platform": sys.platform,
        "reason": reason,
    }


def run_tier(tier: str, extra_args: list[str] | None = None) -> dict:
    """运行指定分层的测试套件并测量精确耗时。"""
    if tier not in LAYER_BUDGETS:
        raise ValueError(f"Unknown tier: {tier}")

    budget_s = LAYER_BUDGETS[tier]
    pattern = TIER_PATTERNS[tier]

    python_exe = sys.executable
    check_offline_script = ROOT / "tools" / "check_offline.py"

    cmd = [python_exe, str(check_offline_script), "--tier", tier]
    if extra_args:
        cmd.extend(extra_args)

    start_t = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed_s = round(time.monotonic() - start_t, 3)

    # 解析 check_offline 的 JSON 输出
    report_data = {}
    for line in proc.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                report_data = json.loads(line)
                break
            except Exception:
                pass

    success = proc.returncode == 0 and report_data.get("success", False)
    within_budget = elapsed_s <= budget_s

    return {
        "schema_version": 1,
        "tier": tier,
        "pattern": pattern,
        "budget_s": budget_s,
        "elapsed_s": elapsed_s,
        "within_budget": within_budget,
        "success": success,
        "tests": report_data.get("tests", 0),
        "passed": report_data.get("passed", 0),
        "skipped": report_data.get("skipped", 0),
        "failures": report_data.get("failures", 0),
        "errors": report_data.get("errors", 0),
        "exit_code": proc.returncode,
    }


def main():
    parser = argparse.ArgumentParser(description="AgentBus 测试分层墙钟预算与耗时门禁工具")
    parser.add_argument(
        "--tier",
        choices=["smoke", "contract", "release", "full"],
        default="contract",
        help="执行的分层档位（默认 contract）",
    )
    parser.add_argument("--check", action="store_true", help="门禁模式：超预算或测试失败时退出码非零")
    parser.add_argument("--probe", action="store_true", help="探测本机环境能力（如符号链接权限）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")

    args = parser.parse_args()

    if args.probe:
        probe_res = check_symlink_capability()
        if args.json:
            print(json.dumps(probe_res, ensure_ascii=False, indent=2))
        else:
            print("=" * 60)
            print("AgentBus 环境能力探测")
            print("=" * 60)
            print(f"平台: {probe_res['platform']}")
            print(f"符号链接支持: {probe_res['can_symlink']}")
            print(f"详细信息: {probe_res['reason']}")
            print("=" * 60)
        return

    result = run_tier(args.tier)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print(f"AgentBus 测试分层耗时体检 [{args.tier.upper()}]")
        print("=" * 60)
        print(f"模式: {result['pattern']}")
        print(f"耗时: {result['elapsed_s']}s (预算: <={result['budget_s']}s) -> {'[合格]' if result['within_budget'] else '[超标]'}")
        print(f"用例数: {result['tests']} (通过: {result['passed']}, 跳过: {result['skipped']}, 失败: {result['failures']}, 错误: {result['errors']})")
        print(f"结果: {'[PASS]' if result['success'] else '[FAIL]'}")
        print("=" * 60)

    if args.check:
        if not result["success"]:
            print(f"[门禁拦截] 分层 {args.tier} 测试执行失败！", file=sys.stderr)
            sys.exit(1)
        if not result["within_budget"]:
            print(f"[门禁拦截] 分层 {args.tier} 耗时 {result['elapsed_s']}s 超过预算 {result['budget_s']}s！", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
