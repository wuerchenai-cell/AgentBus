#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""delivery_self_proof.py: 交付自证门禁（实跑交付自带测试并附原始输出）。

来源：§10.19 AUDIT-360-DELIVERY-SELF-PROOF-GATE（P1）。
要解决的问题：
已验收（delivered）的交付，其自带测试却有红灯，且验收方未发现。
本门禁确保：任何交付进入终态前，必须实跑该交付声称的自带测试模块并附原始输出；
红灯交付不得进入终态，除非具备有效且留痕的显式豁免。

整改与防御（2026-10-14 WorkBuddy 独立复核改进）：
1. 模块识别严格要求 test_*.py 或 *_test.py 文件名，杜绝误抓 tests/ 目录下的辅助模块；
2. 消除整段输出的子串模糊匹配，基于 unittest 退出码与标准总结行（OK / FAILED）判定，杜绝假阳性；
3. 豁免通道强制完整性与有效性校验（approver/reason/expires_at），过期豁免坚决拦截；
4. 豁免不篡改单测失败客观事实，顶层 verdict 标注为 pass_with_waiver；
5. --allow-no-tests 必须提供 --author 与 --reason 审计留痕，杜绝静默自服务免责。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_TEST_FILENAME_PATTERN = re.compile(r"^(test_.+\.py|.+_test\.py)$")
_RAN_PATTERN = re.compile(r"Ran\s+(\d+)\s+tests?")
_SUMMARY_LINE_PATTERN = re.compile(r"^(OK(?:\s*\(.*?\))?|FAILED\s*\(.*?\))\s*$", re.MULTILINE)


def extract_test_artifacts(artifacts: List[Dict[str, Any]] | List[str]) -> List[str]:
    """从交付产物清单中识别所有的测试文件。严格要求文件名必须以 test_ 开头或以 _test.py 结尾。"""
    test_files = []
    for art in artifacts:
        path_str = art["path"] if isinstance(art, dict) else str(art)
        filename = Path(path_str).name
        if _TEST_FILENAME_PATTERN.match(filename):
            test_files.append(path_str)
    return test_files


def run_single_test_module(
    test_path: Path,
    python_exe: str = sys.executable,
    cwd: Optional[Path] = None,
    timeout_s: float = 60.0
) -> Dict[str, Any]:
    """实际执行单个测试模块并收集完整原始输出。"""
    abs_test = test_path.resolve()
    if not abs_test.exists():
        return {
            "test_path": str(test_path),
            "passed": False,
            "exit_code": -1,
            "ran_count": 0,
            "failure_reason": f"测试文件不存在: {test_path}",
            "raw_output": f"FileNotFoundError: {test_path}"
        }

    work_dir = (cwd or abs_test.parent).resolve()

    # 以 unittest 模式执行
    cmd = [python_exe, "-m", "unittest", str(abs_test)]
    child_env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=timeout_s
        )
        combined_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as te:
        return {
            "test_path": str(test_path),
            "passed": False,
            "exit_code": -2,
            "ran_count": 0,
            "failure_reason": f"测试执行超时 ({timeout_s}s)",
            "raw_output": str(te)
        }
    except Exception as exc:
        return {
            "test_path": str(test_path),
            "passed": False,
            "exit_code": -3,
            "ran_count": 0,
            "failure_reason": f"测试启动异常: {exc}",
            "raw_output": str(exc)
        }

    # 解析 ran tests 数量
    ran_match = _RAN_PATTERN.search(combined_output)
    ran_count = int(ran_match.group(1)) if ran_match else 0

    # 基于 unittest 标准总结行结构判定，杜绝 print 文本包含 ERROR/FAILED 带来的假阳性
    summary_match = _SUMMARY_LINE_PATTERN.search(combined_output)
    has_failed_summary = False
    is_explicit_ok = False
    if summary_match:
        summary_str = summary_match.group(1).strip()
        if summary_str.startswith("FAILED"):
            has_failed_summary = True
        elif summary_str.startswith("OK"):
            is_explicit_ok = True
    elif exit_code != 0:
        has_failed_summary = True

    has_error_or_fail = (exit_code != 0 or has_failed_summary)
    passed = (exit_code == 0 and not has_failed_summary and ran_count > 0 and (is_explicit_ok or "OK" in combined_output))

    failure_reason = ""
    if not passed:
        if exit_code != 0:
            failure_reason = f"进程非零退出 (exit_code={exit_code})"
        elif ran_count == 0:
            failure_reason = "未执行任何有效测试 (Ran 0 tests)"
        elif has_failed_summary:
            failure_reason = "单测总结断言失败 (FAILED)"
        else:
            failure_reason = "单测未以有效 OK 总结行结束"

    return {
        "test_path": str(test_path).replace("\\", "/"),
        "passed": passed,
        "exit_code": exit_code,
        "ran_count": ran_count,
        "failure_reason": failure_reason,
        "raw_output": combined_output.strip()
    }


def validate_waiver(waived_info: Any) -> Tuple[bool, str]:
    """严格核验豁免元数据字段完整性与有效期限。"""
    if not isinstance(waived_info, dict):
        return False, "豁免配置必须是字典对象"
    approver = str(waived_info.get("approver") or "").strip()
    reason = str(waived_info.get("reason") or "").strip()
    expires_at = str(waived_info.get("expires_at") or "").strip()

    if not approver:
        return False, "豁免缺少批准人 (approver)"
    if not reason:
        return False, "豁免缺少理由说明 (reason)"
    if not expires_at:
        return False, "豁免缺少有效期截止时间 (expires_at)"

    try:
        exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        if exp_dt < now_dt:
            return False, f"豁免已过期 ({expires_at} < 当前 {now_dt.strftime('%Y-%m-%dT%H:%M:%SZ')})"
    except Exception as ex:
        return False, f"豁免有效期格式解析失败 ({expires_at}): {ex}"

    return True, ""


def verify_delivery_self_proof(
    delivery_record: Dict[str, Any],
    workspace_root: Path,
    python_exe: str = sys.executable,
    exemptions: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """对单个交付记录进行全景自证核验。"""
    exemptions = exemptions or {}
    artifacts = delivery_record.get("artifacts") or delivery_record.get("expected_artifacts") or []
    body_text = delivery_record.get("body") or delivery_record.get("result") or ""

    test_paths = extract_test_artifacts(artifacts)
    no_tests_explicitly_declared = "本交付不附带可执行测试" in body_text

    results = []
    has_unwaived_failures = False
    has_waived_failures = False
    reasons = []

    if not test_paths:
        if no_tests_explicitly_declared:
            declaration_meta = delivery_record.get("no_tests_meta") or {}
            return {
                "verdict": "pass",
                "no_tests": True,
                "explicitly_declared": True,
                "declaration_meta": declaration_meta,
                "reasons": ["本交付显式声明不附带可执行测试"],
                "results": []
            }
        else:
            return {
                "verdict": "fail",
                "no_tests": True,
                "explicitly_declared": False,
                "reasons": ["交付物中无测试模块，且未在说明中显式注明『本交付不附带可执行测试』"],
                "results": []
            }

    for tp in test_paths:
        abs_p = (workspace_root / tp) if not Path(tp).is_absolute() else Path(tp)
        res = run_single_test_module(abs_p, python_exe=python_exe, cwd=workspace_root)

        # 检查是否有豁免
        rel_key = tp.replace("\\", "/")
        waived_info = exemptions.get(rel_key)
        if not res["passed"] and waived_info:
            is_valid_waiver, waiver_err = validate_waiver(waived_info)
            if is_valid_waiver:
                res["waived"] = True
                res["waiver_details"] = waived_info
                has_waived_failures = True
            else:
                res["waived"] = False
                res["waiver_rejection"] = waiver_err
                has_unwaived_failures = True
                reasons.append(f"测试模块 {tp} 失败且豁免无效: {waiver_err}")
        else:
            res["waived"] = False
            if not res["passed"]:
                has_unwaived_failures = True
                reasons.append(f"测试模块 {tp} 未通过: {res['failure_reason']}")

        results.append(res)

    if not has_unwaived_failures and not has_waived_failures:
        verdict = "pass"
    elif not has_unwaived_failures and has_waived_failures:
        verdict = "pass_with_waiver"
    else:
        verdict = "fail"

    return {
        "verdict": verdict,
        "no_tests": False,
        "explicitly_declared": False,
        "has_waived_items": has_waived_failures,
        "reasons": reasons,
        "results": results
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="交付自证门禁运行工具")
    parser.add_argument("--delivery-json", help="交付消息或结果 JSON 文件")
    parser.add_argument("--test-file", action="append", default=[], help="显式指定要自证执行的测试文件")
    parser.add_argument("--workspace", default=".", help="工作区根目录")
    parser.add_argument("--allow-no-tests", action="store_true", help="显式声明本交付不附带测试（须附带 --author 与 --reason）")
    parser.add_argument("--author", default="", help="免测声明或豁免批准人")
    parser.add_argument("--reason", default="", help="免测声明或豁免理由说明")
    parser.add_argument("--exemptions-json", help="豁免配置文件 JSON")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出核验结果")
    parser.add_argument("--strict", action="store_true", help="门禁模式：失败退出码 1")
    args = parser.parse_args(argv)

    ws = Path(args.workspace).resolve()
    delivery_data: Dict[str, Any] = {}

    if args.delivery_json:
        p = Path(args.delivery_json).resolve()
        if p.exists():
            try:
                delivery_data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[ERROR] 读取交付 JSON 失败: {e}", file=sys.stderr)
                return 2

    if args.test_file:
        arts = delivery_data.get("artifacts", [])
        for tf in args.test_file:
            arts.append({"path": tf})
        delivery_data["artifacts"] = arts

    if args.allow_no_tests:
        body = delivery_data.get("body", "")
        # 如果不是正文自带免测声明，要求 CLI 必须提供 --author 与 --reason 审计留痕
        if "本交付不附带可执行测试" not in body:
            if not args.author or not args.reason:
                print("[ERROR] 命令行启用 --allow-no-tests 时必须通过 --author 与 --reason 提供责任人与理由留痕，严禁静默免责！", file=sys.stderr)
                return 2
            delivery_data["body"] = body + "\n本交付不附带可执行测试"
            delivery_data["no_tests_meta"] = {
                "author": args.author.strip(),
                "reason": args.reason.strip(),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

    exemptions = {}
    if args.exemptions_json:
        ep = Path(args.exemptions_json).resolve()
        if ep.exists():
            try:
                exemptions = json.loads(ep.read_text(encoding="utf-8"))
            except Exception as ex:
                print(f"[ERROR] 读取豁免 JSON 失败: {ex}", file=sys.stderr)
                return 2

    report = verify_delivery_self_proof(delivery_data, ws, exemptions=exemptions)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("=" * 65)
        print("AgentBus 交付自证门禁执行报告")
        print("=" * 65)
        print(f"门禁判定结论: {report['verdict'].upper()}")
        if report.get("reasons"):
            print("说明 / 拦截原因:")
            for r in report["reasons"]:
                print(f"  - {r}")
        print("\n测试执行结果明细:")
        for r in report["results"]:
            status_tag = "[PASS]" if r["passed"] else ("[WAIVED]" if r.get("waived") else "[FAIL]")
            print(f"  {status_tag} {r['test_path']} (Ran: {r['ran_count']} tests, Code: {r['exit_code']})")
            if not r["passed"]:
                print(f"    原因: {r['failure_reason']}")
                if r.get("waiver_rejection"):
                    print(f"    豁免被拒原因: {r['waiver_rejection']}")
                # 打印原始错误片段
                lines = r["raw_output"].splitlines()
                err_snippet = "\n    ".join(lines[-5:]) if lines else "无输出"
                print(f"    原始输出尾部:\n    {err_snippet}")
        print("=" * 65)

    if args.strict and report["verdict"] not in ("pass", "pass_with_waiver"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
