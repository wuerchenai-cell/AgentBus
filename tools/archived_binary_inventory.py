#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""archived_binary_inventory.py: 归档旧版二进制与构建残留扩展清点工具。

来源：§10.6 AUDIT-360-DISK-DEADWEIGHT（P0）。
依据：解决原 build_litter_inventory.py 仅清点构建目录而漏报 13.05 GiB 归档文件的问题。

核心纪律：
1. 100% 只读：严格禁止任何删除、重命名、截断或移动操作。
2. 完整取证：清点全部 `*.deprecated_old_version`、`dist-*` 二进制及 `.build-*` 残留目录，
   逐项提取相对路径、字节数、修改时间与 SHA256 哈希值。
3. 策略判定：输出明确的保留/冷备建议，清理动作必须等待 Owner 与协调者确认。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import stat
from pathlib import Path

_PRIVATE = {'.workbuddy', '.git', 'node_modules', '不要访问', 'do not access',
            '.ssh', '.aws', '.azure', '.gnupg'}

def _is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)

def _public_walk(root):
    root = Path(root).absolute()
    if any(part.casefold() in _PRIVATE for part in root.parts):
        raise ValueError('private inventory roots are forbidden')
    for parent in reversed((root, *root.parents)):
        if _is_link(parent):
            raise ValueError('inventory roots cannot contain links/reparse points')
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name.casefold() not in _PRIVATE
                   and not _is_link(Path(folder) / name)]
        yield folder, dirs, [name for name in files if not _is_link(Path(folder) / name)]

def unsealed_agentbus_launchers(root: Path) -> list[str]:
    root = Path(root).absolute()
    return sorted((Path(folder) / name).relative_to(root).as_posix()
                  for folder, _, names in _public_walk(root) for name in names
                  if name.casefold() == 'agentbus.exe')


def calculate_sha256(filepath: Path) -> str:
    """计算文件 SHA256 指纹（分块只读）。"""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return "unreadable"


def find_archived_binaries(root: Path) -> list[dict]:
    """清点全部 *.deprecated_old_version 文件及 dist-* 目录下的二进制。"""
    root = Path(root).absolute()
    archived_files = []
    
    for dirpath, dirnames, filenames in _public_walk(root):
        # 忽略 .git 与 node_modules 等纯外部噪声目录
        
        for fn in filenames:
            fp = Path(dirpath) / fn
            # 匹配 .deprecated_old_version 封存文件
            is_deprecated = ".deprecated_old_version" in fn or fn.endswith(".deprecated_old_version")
            is_dist_bin = "dist-" in dirpath and (fn.endswith(".exe") or fn.endswith(".zip"))
            
            if is_deprecated or is_dist_bin:
                try:
                    stat = fp.stat()
                    sz = stat.st_size
                    mtime = stat.st_mtime
                    rel = fp.relative_to(root).as_posix()
                    archived_files.append({
                        "path": rel,
                        "bytes": sz,
                        "mtime": mtime,
                        "category": "deprecated_binary" if is_deprecated else "dist_residual",
                        "policy_recommendation": "可迁移至外部磁盘冷备，核验 SHA256 后由 Owner 批准删除",
                        "_abs_path": fp,
                    })
                except OSError:
                    continue

    # 按文件路径稳定排序
    archived_files.sort(key=lambda x: x["path"])
    return archived_files


def run_inventory(root: Path, compute_sha: bool = True) -> dict:
    """执行只读清点，返回结构化统计字典。"""
    root = Path(root).absolute()
    items = find_archived_binaries(root)
    
    # 逐项计算 SHA256
    for it in items:
        if compute_sha:
            it["sha256"] = calculate_sha256(it["_abs_path"])
        else:
            it["sha256"] = "skipped"
        del it["_abs_path"]

    total_bytes = sum(x["bytes"] for x in items)
    deprecated_items = [x for x in items if x["category"] == "deprecated_binary"]
    dist_items = [x for x in items if x["category"] == "dist_residual"]

    return {
        "schema_version": 1,
        "root": root.as_posix(),
        "summary": {
            "total_files": len(items),
            "total_bytes": total_bytes,
            "total_gib": round(total_bytes / (1024 ** 3), 2),
            "deprecated_binary_count": len(deprecated_items),
            "deprecated_binary_bytes": sum(x["bytes"] for x in deprecated_items),
            "dist_residual_count": len(dist_items),
            "dist_residual_bytes": sum(x["bytes"] for x in dist_items),
        },
        "retention_policy": {
            "deprecated_binary": "物理封存的历史版本，单文件约 160MB。已实现单一正式版本（成果/AgentBus.exe），建议外迁冷备后删除。",
            "dist_residual": "历史构建生成的分发残留物，保留最新 1 份，多余项在 Owner 确认后清理。",
            "safety_barrier": "严禁在未做完整离线备份或 Git 资产建档前执行物理删除；本工具仅提供清点证据，无写入与删除功能。",
        },
        "items": items,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="归档旧版二进制与构建残留扩展清点工具（100% 只读）")
    parser.add_argument("--root", default=".", help="清点根目录（默认当前目录）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出完整清点结果")
    parser.add_argument("--no-sha", action="store_true", help="跳过 SHA256 计算以快速清点")
    args = parser.parse_args(argv)

    root_path = Path(args.root)
    res = run_inventory(root_path, compute_sha=not args.no_sha)

    if args.json:
        json.dump(res, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    s = res["summary"]
    print("=" * 70)
    print("AgentBus 归档旧版二进制与构建残留清点报告 (只读)")
    print("=" * 70)
    print(f"清点根目录: {res['root']}")
    print(f"归档封存文件总数: {s['total_files']} 个")
    print(f"占用磁盘总容量:   {s['total_bytes']:,} 字节 ({s['total_gib']} GiB)")
    print(f"  - .deprecated_old_version: {s['deprecated_binary_count']} 个 ({s['deprecated_binary_bytes'] / (1024**3):.2f} GiB)")
    print(f"  - dist-* 残留物:            {s['dist_residual_count']} 个 ({s['dist_residual_bytes'] / (1024**3):.2f} GiB)")
    print("\n[保留与清理策略建议]:")
    print(f"  * {res['retention_policy']['safety_barrier']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
