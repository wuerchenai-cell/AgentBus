#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_formal_sync.py: 正式源码树与集成树同步门禁检查工具（Release Gate）。

来源：§10.7 AUDIT-360-FORMAL-BINARY-SYNC-GATE（P0）。
核心功能：
1. 读取 packaging/BUILD-MANIFEST.json 清单；
2. 验证 formal_source_tree_audit.synchronized 必须为 True；
3. 验证 missing_in_formal_count 与 drift_in_formal_count 必须为 0；
4. 作为 Release Gate 刚性防线：任何未同步状态下禁止发布打包，并返回非零退出码。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_MANIFEST = ROOT / "packaging" / "BUILD-MANIFEST.json"


def audit_formal_sync(manifest_path: Path, *, root_dir: Path | None = None,
                      formal_dir: Path | None = None) -> dict:
    if not manifest_path.is_file():
        return {
            "schema_version": 1,
            "manifest_exists": False,
            "synchronized": False,
            "passed": False,
            "error": f"Manifest file not found: {manifest_path}",
        }

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "schema_version": 1,
            "manifest_exists": True,
            "synchronized": False,
            "passed": False,
            "error": f"Invalid JSON in manifest: {exc}",
        }

    if not isinstance(data, dict):
        return {"manifest_exists": True, "passed": False,
                "synchronized": False, "error": "Manifest must be an object"}
    audit = data.get("formal_source_tree_audit", {})
    if not isinstance(audit, dict) or not isinstance(audit.get('summary', {}), dict):
        return {"manifest_exists": True, "passed": False,
                "synchronized": False, "error": "Invalid audit metadata"}
    summary = audit.get("summary", {})

    synchronized = audit.get("synchronized") is True
    missing_count = summary.get("missing_in_formal_count", len(audit.get("missing_in_formal", [])))
    drift_count = summary.get("drift_in_formal_count", len(audit.get("drift_in_formal", [])))

    passed = synchronized and (missing_count == 0) and (drift_count == 0)
    live_errors = []
    if passed:
        from tools.generate_build_manifest import build_manifest_data, verify_manifest
        source = Path(root_dir) if root_dir is not None else manifest_path.parent.parent
        if formal_dir is not None:
            formal = Path(formal_dir)
        elif (source / 'agentbus').is_dir():
            formal = source / 'agentbus'
        elif (ROOT / 'agentbus').is_dir():
            formal = ROOT / 'agentbus'
        else:
            formal = ROOT.parents[2] / 'agentbus'
        try:
            valid, live_errors = verify_manifest(manifest_path, source)
            live = build_manifest_data(source, formal)['formal_source_tree_audit']
            synchronized = live['synchronized']
            missing_count = live['summary']['missing_in_formal_count']
            drift_count = live['summary']['drift_in_formal_count']
            passed = valid and synchronized
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            passed = False
            live_errors = [f'Live source verification failed: {type(exc).__name__}']

    return {
        "schema_version": 1,
        "manifest_path": str(manifest_path),
        "manifest_exists": True,
        "synchronized": synchronized,
        "missing_in_formal_count": missing_count,
        "drift_in_formal_count": drift_count,
        "status_statement": audit.get("status_statement", ""),
        "passed": passed,
        "live_errors": live_errors,
    }


def main():
    parser = argparse.ArgumentParser(description="正式源码树同步门禁（Release Gate）")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="BUILD-MANIFEST.json 路径")
    parser.add_argument("--strict", action="store_true", help="严格门禁模式：未同步时退出码为 1")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果")
    parser.add_argument('--root', type=Path)
    parser.add_argument('--formal-root', type=Path)

    args = parser.parse_args()

    res = audit_formal_sync(args.manifest, root_dir=args.root, formal_dir=args.formal_root)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("AgentBus 正式源码树同步状态体检 (Release Gate)")
        print("=" * 60)
        print(f"清单文件: {res.get('manifest_path')}")
        print(f"同步标识 synchronized: {res.get('synchronized')}")
        print(f"正式树缺失模块数: {res.get('missing_in_formal_count')} (要求: 0)")
        print(f"正式树漂移模块数: {res.get('drift_in_formal_count')} (要求: 0)")
        print(f"状态声明: {res.get('status_statement')}")
        print("-" * 60)
        if res.get("passed"):
            print("[OK] 正式源码树与集成树完全同步！Release Gate 校验通过。")
        else:
            print("[FAIL] 源码树存在未同步或代码漂移！Release Gate 拦截发布。")
        print("=" * 60)

    if args.strict and not res.get("passed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
