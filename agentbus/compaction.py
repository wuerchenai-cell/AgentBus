# -*- coding: utf-8 -*-
"""AgentBus 长周期协作任务信箱深度压缩与冷热分层归档模块 (AUDIT-TASK-COMPACTION-ARCHIVE)

解决项目在长周期、高频次协同后，信箱目录积压大量历史已完成 (done) 任务与回执导致的
文件扫描 (os.scandir) 与反序列化性能劣化。

设计原则：
1. 确定性安全 (Safe Compaction)：仅对终态 (done) 且超过保留窗口的陈旧任务生效；
2. 完整保留热数据：每个成员保留最新 N 条已完成任务，确保近期上下文依然随手可查；
3. 不可变哈希证据链：归档为带 SHA256 清单的紧凑包 (.agentbus/archive/bundle-*.json.gz)；
4. 历史可追溯 (Queryable)：支持通过 task_id 直接从冷存储检索历史任务正文与交付回执。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .bus import Bus
from .config import Config, load


@dataclass
class ArchiveItem:
    task_id: str
    agent: str
    file_type: str  # task / receipt
    rel_path: str
    sha256: str
    bytes: int
    content: dict[str, Any]


def plan_compaction(
    workspace: Path | str,
    keep_recent: int = 5,
    min_age_seconds: float = 0.0,
) -> dict[str, Any]:
    """生成信箱压缩规划（只读预演）。"""
    ws = Path(workspace).expanduser().resolve()
    bus_dir = ws / ".agentbus"
    agents_dir = bus_dir / "agents"
    now = time.time()

    candidates: list[dict[str, Any]] = []
    total_bytes = 0

    if not agents_dir.exists():
        return {
            "workspace": str(ws),
            "candidates_count": 0,
            "total_bytes": 0,
            "candidates": [],
        }

    for agent_entry in sorted(agents_dir.iterdir()):
        if not agent_entry.is_dir():
            continue
        agent_name = agent_entry.name
        done_dir = agent_entry / "done"
        if not done_dir.exists():
            continue

        done_files = sorted(
            [f for f in done_dir.glob("*.json") if f.is_file()],
            key=lambda p: p.stat().st_mtime,
        )

        # 保留最近的 keep_recent 个任务
        if len(done_files) <= keep_recent:
            continue

        to_archive = done_files[:-keep_recent] if keep_recent > 0 else done_files
        for f in to_archive:
            age = now - f.stat().st_mtime
            if age < min_age_seconds:
                continue

            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue

            raw_bytes = f.read_bytes()
            sha256 = hashlib.sha256(raw_bytes).hexdigest()
            task_id = f.stem

            item = {
                "task_id": task_id,
                "agent": agent_name,
                "file_type": "task",
                "rel_path": str(f.relative_to(bus_dir)).replace("\\", "/"),
                "sha256": sha256,
                "bytes": len(raw_bytes),
                "mtime": f.stat().st_mtime,
            }
            candidates.append(item)
            total_bytes += len(raw_bytes)

            # 寻找该任务在收件人处对应的已投递回执
            result_to = data.get("result_to", "")
            if result_to:
                receipt_file = agents_dir / result_to / "results" / f"{task_id}.json"
                if receipt_file.exists():
                    r_bytes = receipt_file.read_bytes()
                    r_sha = hashlib.sha256(r_bytes).hexdigest()
                    candidates.append({
                        "task_id": task_id,
                        "agent": result_to,
                        "file_type": "receipt",
                        "rel_path": str(receipt_file.relative_to(bus_dir)).replace("\\", "/"),
                        "sha256": r_sha,
                        "bytes": len(r_bytes),
                        "mtime": receipt_file.stat().st_mtime,
                    })
                    total_bytes += len(r_bytes)

    return {
        "workspace": str(ws),
        "candidates_count": len(candidates),
        "total_bytes": total_bytes,
        "candidates": candidates,
    }


def apply_compaction(
    workspace: Path | str,
    plan: dict[str, Any],
    bundle_tag: str | None = None,
) -> dict[str, Any]:
    """根据预演规划执行冷热分层压缩，生成冷存储归档并清理热信箱。"""
    ws = Path(workspace).expanduser().resolve()
    bus_dir = ws / ".agentbus"
    archive_dir = bus_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    candidates = plan.get("candidates", [])
    if not candidates:
        return {
            "ok": True,
            "archived_count": 0,
            "saved_bytes": 0,
            "bundle_file": None,
        }

    tag = bundle_tag or time.strftime("%Y%m%d-%H%M%S")
    bundle_filename = f"bundle-{tag}.json.gz"
    bundle_path = archive_dir / bundle_filename

    # Validate the index before creating a bundle or deleting any hot files.
    index_file = archive_dir / "index.json"
    index_data: dict[str, Any] = {"schema_version": 1, "bundles": []}
    if index_file.exists():
        try:
            index_data = json.loads(index_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("archive index is invalid; compaction was aborted without deleting source messages") from exc
        if not isinstance(index_data, dict) or not isinstance(index_data.get("bundles"), list):
            raise ValueError("archive index is invalid; compaction was aborted without deleting source messages")

    # 读取全部原始数据装载进归档包
    bundle_items: list[dict[str, Any]] = []
    files_to_remove: list[Path] = []

    for item in candidates:
        file_path = bus_dir / item["rel_path"]
        if not file_path.exists():
            continue
        try:
            content = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        bundle_items.append({
            "task_id": item["task_id"],
            "agent": item["agent"],
            "file_type": item["file_type"],
            "rel_path": item["rel_path"],
            "sha256": item["sha256"],
            "bytes": item["bytes"],
            "content": content,
        })
        files_to_remove.append(file_path)

    bundle_payload = {
        "schema_version": 1,
        "bundle_id": tag,
        "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_items": len(bundle_items),
        "items": bundle_items,
    }

    # 压缩写入 bundle.json.gz
    gz_data = gzip.compress(json.dumps(bundle_payload, ensure_ascii=False, indent=2).encode("utf-8"))
    bundle_path.write_bytes(gz_data)
    bundle_sha256 = hashlib.sha256(gz_data).hexdigest()

    # 更新索引清单 index.json；损坏索引已在写入归档包之前拒绝。
    index_data.setdefault("bundles", []).append({
        "bundle_id": tag,
        "filename": bundle_filename,
        "sha256": bundle_sha256,
        "bytes": len(gz_data),
        "items_count": len(bundle_items),
        "task_ids": sorted(list({item["task_id"] for item in bundle_items})),
        "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    index_file.write_text(json.dumps(index_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # 安全清理已归档文件
    removed_count = 0
    saved_bytes = 0
    for p in files_to_remove:
        if p.exists():
            saved_bytes += p.stat().st_size
            p.unlink()
            removed_count += 1

    return {
        "ok": True,
        "bundle_file": str(bundle_path),
        "bundle_sha256": bundle_sha256,
        "archived_items": len(bundle_items),
        "removed_files": removed_count,
        "saved_bytes": saved_bytes,
    }


def query_archive(
    workspace: Path | str,
    task_id: str,
) -> dict[str, Any] | None:
    """从冷存储归档中按 task_id 检索历史任务与回执。"""
    ws = Path(workspace).expanduser().resolve()
    archive_dir = ws / ".agentbus" / "archive"
    index_file = archive_dir / "index.json"
    if not index_file.exists():
        return None

    try:
        index_data = json.loads(index_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    target_bundles = []
    for b in index_data.get("bundles", []):
        if task_id in b.get("task_ids", []):
            target_bundles.append(b["filename"])

    for b_name in target_bundles:
        b_path = archive_dir / b_name
        if not b_path.exists():
            continue
        try:
            raw = gzip.decompress(b_path.read_bytes()).decode("utf-8")
            data = json.loads(raw)
            matched = [item for item in data.get("items", []) if item["task_id"] == task_id]
            if matched:
                return {
                    "task_id": task_id,
                    "bundle": b_name,
                    "records": matched,
                }
        except Exception:
            continue

    return None
