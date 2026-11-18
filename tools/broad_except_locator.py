#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""broad_except_locator.py: 宽泛异常捕获定位器与基线差分工具。

来源：§10.20 AUDIT-360-BROAD-EXCEPT-LOCATOR（P1）。
功能：
1. AST 遍历全包 .py 源码，精确定位所有宽泛异常捕获（Exception / OSError / BaseException / bare except）；
2. 提取文件相对路径、精确行号、所属函数/方法名、以及捕获类型；
3. 支持基线快照保存与定点差分（diff），精准定位新增/消除行；
4. 提供基线刷新审计记录（--update-baseline / --author / --reason），禁止静默放宽；
5. 提供 CLI 门禁（--check / --strict），防止宽泛异常无序反弹。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT / "agentbus"
DEFAULT_BASELINE = Path(__file__).resolve().parent / "broad_except_baseline.json"


class _FunctionLocator(ast.NodeVisitor):
    def __init__(self):
        self.scope_stack = []
        self.handlers = []

    def visit_FunctionDef(self, node):
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node):
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ClassDef(self, node):
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ExceptHandler(self, node):
        exc_str = ""
        kind = "unknown"
        if node.type is None:
            exc_str = "bare except"
            kind = "bare"
        elif isinstance(node.type, ast.Name):
            exc_str = node.type.id
            kind = node.type.id
        elif isinstance(node.type, ast.Tuple):
            parts = [e.id for e in node.type.elts if isinstance(e, ast.Name)]
            exc_str = ", ".join(parts)
            kind = "Tuple(" + exc_str + ")"
        elif isinstance(node.type, ast.Attribute):
            exc_str = ast.unparse(node.type)
            kind = exc_str

        is_broad = any(k in exc_str for k in ["Exception", "OSError", "BaseException", "bare except"])
        if is_broad:
            context = "::".join(self.scope_stack) if self.scope_stack else "<module>"
            self.handlers.append({
                "lineno": node.lineno,
                "kind": kind,
                "exc_str": exc_str,
                "context": context
            })
        self.generic_visit(node)


def scan_file(file_path: Path, base_dir: Path) -> list[dict[str, Any]]:
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except Exception:
        return []

    locator = _FunctionLocator()
    locator.visit(tree)

    try:
        rel_path = file_path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        rel_path = file_path.as_posix()

    for h in locator.handlers:
        h["file"] = rel_path
    return locator.handlers


def scan_directory(target_dir: Path) -> list[dict[str, Any]]:
    target_dir = Path(target_dir).resolve()
    all_handlers = []
    for pf in sorted(target_dir.rglob("*.py")):
        if "__pycache__" in pf.parts:
            continue
        all_handlers.extend(scan_file(pf, target_dir.parent))
    return all_handlers


def diff_with_baseline(current: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> dict[str, Any]:
    def make_key(item):
        return f"{item['file']}#{item['lineno']}#{item['context']}#{item['kind']}"

    curr_map = {make_key(it): it for it in current}
    base_map = {make_key(it): it for it in baseline}

    added_keys = set(curr_map.keys()) - set(base_map.keys())
    removed_keys = set(base_map.keys()) - set(curr_map.keys())

    added = [curr_map[k] for k in sorted(added_keys)]
    removed = [base_map[k] for k in sorted(removed_keys)]

    by_file = defaultdict(int)
    for it in current:
        by_file[it["file"]] += 1

    return {
        "current_total": len(current),
        "baseline_total": len(baseline),
        "delta": len(current) - len(baseline),
        "added_count": len(added),
        "removed_count": len(removed),
        "added_details": added,
        "removed_details": removed,
        "by_file": dict(sorted(by_file.items(), key=lambda x: -x[1]))
    }


def save_baseline_snapshot(
    baseline_path: Path,
    current_items: list[dict[str, Any]],
    author: str = "",
    reason: str = ""
) -> dict[str, Any]:
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "author": author.strip() or "anonymous",
        "reason": reason.strip() or "全量基线设立与同步",
        "total": len(current_items),
        "items": current_items
    }
    baseline_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def load_baseline_snapshot(baseline_path: Path) -> Tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    if not baseline_path.exists():
        return None, []
    try:
        raw = json.loads(baseline_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            legacy_meta = {
                "author": "legacy",
                "reason": "历史未留痕裸数组基线",
                "timestamp": "",
                "total": len(raw),
                "is_legacy_array": True
            }
            return legacy_meta, raw
        if isinstance(raw, dict) and "items" in raw:
            meta = {k: v for k, v in raw.items() if k != "items"}
            return meta, raw["items"]
        return None, []
    except Exception:
        return None, []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="宽泛异常捕获定位器与基线差分工具")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="扫描目标目录 (默认 agentbus)")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="基线快照 JSON 路径")
    parser.add_argument("--save-baseline", action="store_true", help="保存当前扫描结果为新基线快照")
    parser.add_argument("--update-baseline", action="store_true", help="显式刷新基线 (须指定 --author 与 --reason)")
    parser.add_argument("--author", default="", help="基线更新责任人")
    parser.add_argument("--reason", default="", help="基线更新原因说明")
    parser.add_argument("--ceiling", type=int, default=None, help="允许的宽泛异常天花板上限")
    parser.add_argument("--check", action="store_true", help="门禁模式：超标或新增时返回退出码 1")
    parser.add_argument("--strict", action="store_true", help="严格门禁模式（同 --check）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结构")
    args = parser.parse_args(argv)

    target_dir = Path(args.target).resolve()
    current_items = scan_directory(target_dir)
    baseline_path = Path(args.baseline).resolve()

    if args.save_baseline or args.update_baseline:
        if not args.author or not args.reason:
            print("[ERROR] 刷新或保存基线时必须通过 --author 与 --reason 提供责任人与说明，严禁静默放宽！", file=sys.stderr)
            return 2
        save_baseline_snapshot(baseline_path, current_items, author=args.author, reason=args.reason)
        print(f"[OK] 已将当前 {len(current_items)} 处宽泛异常保存为基线: {baseline_path}")
        return 0

    base_meta, baseline_items = load_baseline_snapshot(baseline_path)
    diff = diff_with_baseline(current_items, baseline_items)

    if args.json:
        out = {
            "diff": diff,
            "baseline_meta": base_meta,
            "current_findings": current_items
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print("=" * 65)
        print("AgentBus 宽泛异常捕获静态审计与定位报告")
        print("=" * 65)
        print(f"当前实测捕获数: {diff['current_total']} 处")
        print(f"既有基线参考数: {diff['baseline_total']} 处 (差异: {diff['delta']:+d})")
        print(f"新增未登记项:   {diff['added_count']} 处")
        print(f"已消除优化项:   {diff['removed_count']} 处")
        if base_meta:
            print(f"基线最后责任人: {base_meta.get('author', '未知')} ({base_meta.get('timestamp', '')})")
            print(f"基线更新理由:   {base_meta.get('reason', '')}")
        print("\nTOP 6 宽泛异常集中的文件:")
        for idx, (f, cnt) in enumerate(list(diff["by_file"].items())[:6], 1):
            print(f"  {idx}. {f}: {cnt} 处")

        if diff["added_count"] > 0:
            print("\n[!] 新增宽泛异常定位清单 (前 20 项):")
            for it in diff["added_details"][:20]:
                print(f"  - {it['file']}:{it['lineno']} in {it['context']} ({it['kind']})")
            if len(diff["added_details"]) > 20:
                print(f"  ... 另有 {len(diff['added_details']) - 20} 项已省略，请使用 --json 查看全量清单")

    # 门禁判断
    failed = False
    reasons = []

    if args.ceiling is not None and diff["current_total"] > args.ceiling:
        failed = True
        reasons.append(f"总数 {diff['current_total']} 超过天花板 {args.ceiling}")

    is_gate = args.check or args.strict
    if is_gate and diff["added_count"] > 0 and len(baseline_items) > 0:
        failed = True
        reasons.append(f"检出 {diff['added_count']} 处未经审计的新增宽泛异常")

    if failed:
        if not args.json:
            print("\n[FAIL] 门禁拦截:", "; ".join(reasons))
        return 1
    else:
        if not args.json:
            print("\n[OK] 门禁通过！宽泛异常完全处于基线受控范围内。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
