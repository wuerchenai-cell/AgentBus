"""Build Manifest Generator and Verifier for AgentBus.

Source: Antigravity, 2026-10-11.
Provides deterministic, verifiable mapping from binary packaged modules to
candidate integration source code, and audits the drift against the formal source tree.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.export_source import APP_ICON_FILE, WEB_ASSET_FILES

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')


def compute_file_info(path: Path) -> dict:
    data = path.read_bytes()
    lines = None
    if path.suffix in ('.py', '.json', '.html', '.css', '.js', '.md', '.txt', '.yml', '.yaml'):
        try:
            lines = len(data.splitlines())
        except Exception:
            lines = None
    return {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "lines": lines
    }


def build_manifest_data(root_dir: Path, formal_dir: Path, exe_path: Path | None = None) -> dict:
    root_dir = root_dir.resolve()
    formal_dir = formal_dir.resolve()

    # 1. Inspect package python modules
    package_modules = {}
    agentbus_src = root_dir / "agentbus"
    if not agentbus_src.exists():
        raise FileNotFoundError(f"Integration source not found: {agentbus_src}")

    for file_path in sorted(agentbus_src.rglob("*")):
        if file_path.is_file() and not any(part.startswith('.') or part == '__pycache__' for part in file_path.relative_to(agentbus_src).parts):
            rel = file_path.relative_to(agentbus_src).as_posix()
            if rel.endswith('.py'):
                mod_name = "agentbus." + rel[:-3].replace('/', '.')
                if mod_name.endswith('.__init__'):
                    mod_name = mod_name[:-9]
                info = compute_file_info(file_path)
                package_modules[f"agentbus/{rel}"] = {
                    "module": mod_name,
                    "source_path": f"agentbus/{rel}",
                    "bytes": info["bytes"],
                    "sha256": info["sha256"],
                    "lines": info["lines"]
                }

    # Add entrypoints
    for entry in ['entry.py', 'entry_tower.py', 'tools/smoke_frozen.py', 'tools/export_source.py']:
        ep = root_dir / entry
        if ep.is_file():
            info = compute_file_info(ep)
            package_modules[entry] = {
                "module": entry.removesuffix('.py').replace('/', '.'),
                "source_path": entry,
                "bytes": info["bytes"],
                "sha256": info["sha256"],
                "lines": info["lines"]
            }

    # 2. Inspect data assets
    package_data = {}
    data_scan_dirs = [
        ("schemas", root_dir / "schemas"),
        ("skills/agentbus-team", root_dir / "skills/agentbus-team"),
        ("share_assets", root_dir / "share_assets"),
        ("agentbus/web_assets", root_dir / "agentbus/web_assets"),
        ("agentbus/assets", root_dir / "agentbus/assets"),
        ("agentbus/trigger/manifests", root_dir / "agentbus/trigger/manifests"),
        ("agentbus/host_assets", root_dir / "agentbus/host_assets"),
    ]
    for category, base_dir in data_scan_dirs:
        if base_dir.is_dir():
            for file_path in sorted(base_dir.rglob("*")):
                if file_path.is_file() and not any(part.startswith('.') or part == '__pycache__' for part in file_path.relative_to(base_dir).parts):
                    rel = file_path.relative_to(root_dir).as_posix()
                    if category == "agentbus/web_assets" and rel not in WEB_ASSET_FILES:
                        continue
                    if category == "agentbus/assets" and rel != APP_ICON_FILE:
                        continue
                    info = compute_file_info(file_path)
                    package_data[rel] = {
                        "category": category,
                        "source_path": rel,
                        "bytes": info["bytes"],
                        "sha256": info["sha256"],
                        "lines": info["lines"]
                    }

    # Fixed public resources explicitly embedded for in-app package exports.
    for relative in ('LICENSE', 'SOURCE_LICENSE_STATUS.md', 'docs/public/README.md',
                     'docs/public/RELEASE.md', 'docs/public/ECOSYSTEM.md', 'docs/public/SECURITY.md'):
        path = root_dir / relative
        if path.is_file():
            info = compute_file_info(path)
            package_data[relative] = {'category': 'public-export-resources',
                                      'source_path': relative, **info}

    # 3. Formal source tree audit
    formal_files = {}
    if formal_dir.is_dir():
        for file_path in sorted(formal_dir.rglob("*")):
            if file_path.is_file() and not any(part.startswith('.') or part == '__pycache__' for part in file_path.relative_to(formal_dir).parts):
                rel = file_path.relative_to(formal_dir).as_posix()
                formal_files[rel] = compute_file_info(file_path)

    missing_in_formal = []
    drift_in_formal = []
    synchronized_modules = []

    for rel, item in {**package_modules, **package_data}.items():
        item = {**item, 'module': item.get('module', rel)}
        if rel.startswith("agentbus/"):
            sub_rel = rel[len("agentbus/"):]
        else:
            sub_rel = '../' + rel
            candidate = formal_dir.parent / rel
            if candidate.is_file():
                formal_files[sub_rel] = compute_file_info(candidate)
        if sub_rel not in formal_files:
            missing_in_formal.append({
                "module": item["module"],
                "path": rel,
                "bytes": item["bytes"],
                "sha256": item["sha256"]
            })
        elif formal_files[sub_rel]["sha256"] != item["sha256"]:
            drift_in_formal.append({
                "module": item["module"],
                "path": rel,
                "candidate_sha256": item["sha256"],
                "candidate_bytes": item["bytes"],
                "formal_sha256": formal_files[sub_rel]["sha256"],
                "formal_bytes": formal_files[sub_rel]["bytes"]
            })
        else:
            synchronized_modules.append(item["module"])

    # 4. Target binary metadata
    binary_info = None
    if exe_path and exe_path.is_file():
        info = compute_file_info(exe_path)
        binary_info = {
            "path": str(exe_path).replace("\\", "/"),
            "bytes": info["bytes"],
            "sha256": info["sha256"],
            "build_source_root": root_dir.as_posix()
        }

    total_lines = sum((m["lines"] or 0) for m in package_modules.values())
    total_module_bytes = sum(m["bytes"] for m in package_modules.values())
    total_data_bytes = sum(d["bytes"] for d in package_data.values())

    manifest = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "manifest_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "tools/generate_build_manifest.py",
        "target_binary": binary_info,
        "formal_source_tree_audit": {
            "formal_source_path": "成果/agentbus/agentbus",
            "synchronized": len(missing_in_formal) == 0 and len(drift_in_formal) == 0,
            "status_statement": (
                "正式源码树成果/agentbus/agentbus/已与最新集成候选树完全同步一致（missing=0, drift=0）。"
                if len(missing_in_formal) == 0 and len(drift_in_formal) == 0 else
                "正式源码树成果/agentbus/agentbus/尚未同步最新集成树变更。当前存在缺失核心模块与代码漂移，"
                "任何人若仅从成果/agentbus/构建，将无法得到含任务树、六步流程、WebBridge与心跳防御的完整功能。"
            ),
            "summary": {
                "candidate_modules_count": len(package_modules),
                "formal_files_count": len(formal_files),
                "missing_in_formal_count": len(missing_in_formal),
                "drift_in_formal_count": len(drift_in_formal),
                "synchronized_count": len(synchronized_modules)
            },
            "missing_in_formal": missing_in_formal,
            "drift_in_formal": drift_in_formal,
            "synchronized_modules": synchronized_modules
        },
        "package_modules": package_modules,
        "package_data_assets": package_data,
        "manifest_summary": {
            "total_python_modules": len(package_modules),
            "total_python_lines": total_lines,
            "total_module_bytes": total_module_bytes,
            "total_data_assets": len(package_data),
            "total_data_bytes": total_data_bytes,
            "total_payload_bytes": total_module_bytes + total_data_bytes
        }
    }
    return manifest


def verify_manifest(manifest_path: Path, root_dir: Path) -> tuple[bool, list[str]]:
    if not manifest_path.is_file():
        return False, [f"Manifest file not found: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception as exc:
        return False, [f"Manifest parse error: {exc}"]

    errors = []
    root_dir = root_dir.resolve()
    modules = manifest.get("package_modules", {})
    live = build_manifest_data(root_dir, root_dir / 'agentbus')
    for category in ('package_modules', 'package_data_assets'):
        captured = manifest.get(category, {})
        if not isinstance(captured, dict) or set(captured) != set(live[category]):
            errors.append(f'Manifest membership differs from live sources: {category}')
    for rel_path, expected in modules.items():
        full_path = root_dir / rel_path
        if not full_path.is_file():
            errors.append(f"Missing module source: {rel_path}")
            continue
        actual_info = compute_file_info(full_path)
        if actual_info["sha256"] != expected["sha256"]:
            errors.append(f"SHA256 mismatch for {rel_path}: expected {expected['sha256'][:8]}, got {actual_info['sha256'][:8]}")
        if actual_info["bytes"] != expected["bytes"]:
            errors.append(f"Byte count mismatch for {rel_path}: expected {expected['bytes']}, got {actual_info['bytes']}")

    data_assets = manifest.get("package_data_assets", {})
    for rel_path, expected in data_assets.items():
        full_path = root_dir / rel_path
        if not full_path.is_file():
            errors.append(f"Missing data asset: {rel_path}")
            continue
        actual_info = compute_file_info(full_path)
        if actual_info["sha256"] != expected["sha256"]:
            errors.append(f"SHA256 mismatch for {rel_path}: expected {expected['sha256'][:8]}, got {actual_info['sha256'][:8]}")

    return len(errors) == 0, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    integration_dir = Path(__file__).resolve().parents[1]
    curr = Path(__file__).resolve()
    repo_root = curr
    for p in curr.parents:
        if (p / '成果' / 'AgentBus.exe').is_file() or ((p / '成果').is_dir() and (p / '.agentbus').is_dir() and p.name != 'agentbus'):
            repo_root = p
            break
    parser.add_argument('--root', type=Path, default=integration_dir)
    parser.add_argument('--formal-root', type=Path, default=repo_root / "成果/agentbus/agentbus")
    parser.add_argument('--exe', type=Path, default=repo_root / "成果/AgentBus.exe")
    parser.add_argument('--output', type=Path, default=None)
    parser.add_argument('--check', action='store_true', help="Check existing manifest without modifying")

    args = parser.parse_args()
    output_path = args.output or (args.root / "packaging/BUILD-MANIFEST.json")

    if args.check:
        ok, errors = verify_manifest(output_path, args.root)
        if ok:
            print(f"[OK] Build manifest verification succeeded: {output_path}")
            sys.exit(0)
        else:
            print(f"[FAIL] Build manifest verification failed with {len(errors)} error(s):")
            for err in errors[:20]:
                print(f"  - {err}")
            sys.exit(1)

    manifest = build_manifest_data(args.root, args.formal_root, args.exe)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding='utf-8')
    print(f"[SUCCESS] Wrote build manifest to {output_path}")
    print(f"  Modules: {manifest['manifest_summary']['total_python_modules']}")
    print(f"  Data assets: {manifest['manifest_summary']['total_data_assets']}")
    print(f"  Missing in formal tree: {manifest['formal_source_tree_audit']['summary']['missing_in_formal_count']}")
    print(f"  Drift in formal tree: {manifest['formal_source_tree_audit']['summary']['drift_in_formal_count']}")


if __name__ == '__main__':
    main()
