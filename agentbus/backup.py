"""
agentbus.backup —— 任务树与工作区偏好轻量备份与还原（§6.11 PROJECT-BACKUP-RESTORE）

核心原则与设计边界：
1. 轻量绿色、跨机器可移植：
   - 包含：任务树结构模板 (roots, branches, leaves, dependencies)、团队角色与协调者配置、
     画布便签 (sticky_notes)、分支折叠 (collapsed_branches)、个人卡片优先级 (card_priorities)。
   - 坚决剔除：任何机器绝对路径（workspace/output_root 强制归一化为相对占位符）、
     凭据密钥/私有会话/私有记忆、任何 .agentbus 内部运行时态（锁、信箱、心跳、租约）。
2. 预览式（Preview-First）安全设计：
   - 导出前预览 (preview_export)：计算包含项概要、节点统计、脱敏说明及哈希；
   - 恢复前预览对比 (preview_restore)：深度对比当前环境与备份包的差异（任务增删变动、成员配置差异、便签差异），
     必须经过确认 (confirm=True) 且校验 preview_id 后才落地执行；
   - 跨机器迁移安全：还原时要求重新绑定当前项目的本机目录，绝不盲目覆盖异地绝对路径；
3. 生成可验证清单 (manifest)：
   - 备份包自包含 SHA256 签名校验码与标准元数据清单。
"""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .config import Config, load, save, validate
from . import gui_state
from .silent_debug import note_silent
from .gui_state import (
    load_branch_folds,
    load_notes,
    load_priorities,
    save_branch_folds,
    save_notes,
    save_priorities,
)
from .operation_lock import task_lock
from .task_tree import TaskForest, TaskTreeError
from .tree_templates import (
    adapt_external_tree,
    contains_local_path,
    export_template,
    export_template_plan,
    import_template,
    preview_template,
    scrub_local_paths,
    scrub_path_value,
)

BACKUP_FORMAT = "agentbus-project-backup"
BACKUP_SCHEMA_VERSION = 1
MAX_BACKUP_BYTES = 8 * 1024 * 1024
RESTORE_PREVIEW_TTL_S = 300
_RESTORE_PREVIEWS: dict[str, dict[str, Any]] = {}
_PREVIEW_LOCK = threading.RLock()

#: §13.7 还原模式。三者语义不同，预览必须按所选模式计算**真实最终集合**。
RESTORE_MODES = ("append", "merge", "replace")
DEFAULT_RESTORE_MODE = "merge"
RESTORE_MODE_LABELS = {
    "append": "导入副本：把备份作为独立命名空间追加，本机既有节点全部保留",
    "merge": "合并：按确定性节点 id 补齐缺失节点，已存在的保持本机原样；重试幂等",
    "replace": "替换恢复：目标树变为备份结构，本机独有节点将被丢弃（危险，需显式确认）",
}
#: 备份覆盖范围声明：避免把「拓扑备份」误当「完整灾备」。
BACKUP_SCOPE_NOTE = (
    "本备份只包含任务树拓扑、团队设置与本机偏好（便签/折叠/优先级）；"
    "不含信箱、产物、租约与心跳，因此不是完整灾备。")
#: 视为「在途」的叶子状态；替换恢复不得破坏它们。
IN_FLIGHT_STATES = frozenset({"claimed", "running"})

#: 导出时本机绝对路径被替换成的占位符；导入时必须在本机重新绑定，绝不写回占位符。
REBIND_PLACEHOLDER = "{{REBIND:path}}"
#: 参数/环境变量里的凭据值被替换成的掩码。
REDACTED = "***"
#: 导出时写进 worker 的说明键；导入前会被剥除，不会污染本机配置。
IMPORT_NOTES_KEY = "import_notes"

_SENSITIVE_KEY_PATTERNS = [
    re.compile(r"(?i)(?:key|token|secret|password|credential|auth|bearer)"),
]
#: `--token=xxx` / `--api-key=xxx` / `api_key=xxx` 形式的**参数内**凭据。
_SECRET_ARGUMENT = re.compile(
    r"(?i)(-{1,2}[\w.-]*(?:key|token|secret|password|credential|auth|bearer)[\w.-]*=)(\S+)")
#: 单独成项的凭据开关（`--token`），其**下一个**列表元素才是值。
_SECRET_FLAG = re.compile(
    r"(?i)^-{1,3}[\w.-]*(?:key|token|secret|password|credential|auth|bearer)[\w.-]*$")
#: 语义上属于「本机机器配置」的 worker 字段：路径与启动参数必须脱敏并按本机重绑定。
MACHINE_CONFIG_FIELDS = frozenset({
    "path", "command", "candidate_command", "env", "cwd", "args",
    "executable", "desktop_icon_path", "workspace", "output_root",
})


#: 值为「原子路径/参数」的字段：带空格的路径必须整条替换（否则只清到第一个空格）。
ATOMIC_PATH_FIELDS = frozenset({
    "path", "command", "candidate_command", "args", "cwd", "executable",
    "workspace", "output_root", "env", "desktop_icon_path",
})


def _strip_secrets(data: Any) -> Any:
    """递归移除任何可能含有凭据/密钥的**字段**（按键名判定）。

    这是「键名级」过滤，只能挡住 `secret_token` 这类一眼可辨的字段；
    值内部的绝对路径与参数内凭据由 `_scrub_machine_value` 按语义处理。
    """
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            if any(p.search(str(k)) for p in _SENSITIVE_KEY_PATTERNS):
                continue
            cleaned[k] = _strip_secrets(v)
        return cleaned
    elif isinstance(data, list):
        return [_strip_secrets(item) for item in data]
    return data


def _scrub_machine_value(value: Any, trail: str, report: dict[str, Any],
                         atomic: bool = False) -> Any:
    """按**字段语义**清洗机器配置：绝对路径 → 重绑定占位符，参数内凭据 → 掩码。

    `trail` 形如 `workers.codex.command`，仅用于把每一处改动登记进 `report`，
    便于导出预览如实告诉用户「改了什么、哪里需要本机重新绑定」。
    `atomic=True` 表示该字段的值整体就是路径（argv 项 / launcher 路径 / env 值），
    带空格的路径必须整条替换；自由文本字段则保持保守的子串清理，绝不吞掉正文。
    普通业务文字（中文便签、说明）不会命中路径判定，因而不会被误删。
    """
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            sub = "%s.%s" % (trail, k)
            if any(p.search(str(k)) for p in _SENSITIVE_KEY_PATTERNS):
                report["credentials_removed"] += 1
                report["fields_removed"].append(sub)
                continue
            cleaned[k] = _scrub_machine_value(
                v, sub, report, atomic=atomic or k in ATOMIC_PATH_FIELDS)
        return cleaned
    if isinstance(value, list):
        out: list[Any] = []
        mask_next = False
        for i, item in enumerate(value):
            sub = "%s[%d]" % (trail, i)
            if mask_next:
                # 上一个是凭据开关，本项是它的值：值一律掩码。
                out.append(REDACTED)
                mask_next = False
                continue
            if isinstance(item, str) and _SECRET_FLAG.match(item):
                report["credentials_removed"] += 1
                report["fields_removed"].append(sub + "(凭据开关)")
                out.append(item)
                mask_next = True
                continue
            out.append(_scrub_machine_value(item, sub, report, atomic=atomic))
        return out
    if isinstance(value, str):
        scrub = scrub_path_value if atomic else scrub_local_paths
        scrubbed, paths = scrub(value, REBIND_PLACEHOLDER)
        if paths:
            report["machine_paths_removed"] += paths
            report["rebind_required"].append({"field": trail, "count": paths})
        # 单次替换即可覆盖全部出现；**不可**写成循环——掩码后的 `key=***` 仍能被
        # 同一条正则匹配，循环会永远返回 1 而陷入死循环（本叶开发时实际踩到）。
        scrubbed, arguments = _SECRET_ARGUMENT.subn(
            lambda m: m.group(1) + REDACTED, scrubbed)
        if arguments:
            report["credentials_removed"] += arguments
            report["fields_removed"].append(trail + "(参数内凭据)")
        return scrubbed
    return value


def _empty_report() -> dict[str, Any]:
    return {
        "machine_paths_removed": 0,
        "credentials_removed": 0,
        "fields_removed": [],
        "rebind_required": [],
        "review_items": [],
        "free_text_review": [],
        "scope": "machine_config_verified; free_text_unverified",
        "unverified_note": (
            "机器配置（path/command/env/参数）已按字段语义清理并核验；"
            "自由正文（成员说明、画布便签、任务目标/接手说明）无法逐字保证不含敏感信息，"
            "导出后请人工复核，本工具不作绝对已脱敏保证。"
        ),
    }


def _sanitize_workers(workers_raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """清洗团队成员配置，返回 `(干净配置, 脱敏报告)`。"""
    report = _empty_report()
    cleaned: dict[str, Any] = {}
    for name, worker in workers_raw.items():
        w_copy = copy.deepcopy(worker)
        # 桌面图标路径是本机私有标识，一律不导出（沿用既有契约）。
        w_copy.pop("desktop_icon_path", None)
        sub = _empty_report()
        value = _scrub_machine_value(w_copy, "workers.%s" % name, sub)
        for key in ("machine_paths_removed", "credentials_removed"):
            report[key] += sub[key]
        report["fields_removed"].extend(sub["fields_removed"])
        report["rebind_required"].extend(sub["rebind_required"])
        # 缺失适配说明：机器配置被占位后，导入方需要知道要重新绑定什么。
        touched = {item["field"] for item in sub["rebind_required"]}
        if touched:
            fields = sorted(f.split(".", 2)[-1] for f in touched)
            value[IMPORT_NOTES_KEY] = (
                "导出时已把本机启动路径脱敏为 %s；导入后需在本机重新绑定：%s"
                % (REBIND_PLACEHOLDER, "、".join(fields)))
        cleaned[name] = value
    # 自由正文（成员说明）如实登记为「需复核」而非「已保证」。
    for name, worker in workers_raw.items():
        note = str(worker.get("note") or "")
        if note and (contains_local_path(note) or any(p.search(note) for p in _SENSITIVE_KEY_PATTERNS)):
            report["review_items"].append({
                "field": "workers.%s.note" % name,
                "reason": "成员说明为自由正文，可能含本机路径或凭据痕迹；已保留但需人工复核",
            })
        # 服务端点既不是路径也不是凭据（权威判定器不会清除它），
        # 但它指向私有中转基础设施，是否随包分发应由用户决定，故如实登记。
        if str(worker.get("base_url") or ""):
            report["review_items"].append({
                "field": "workers.%s.base_url" % name,
                "reason": "服务端点为私有基础设施标识，未自动清除；请确认是否随包分发",
            })
    return cleaned, report


def create_backup_bundle(workspace: Path | str) -> dict[str, Any]:
    """生成项目的标准化可移植备份包结构（脱敏且无绝对路径）。"""
    root = Path(workspace).resolve()
    cfg = load(root)
    forest = TaskForest(cfg)

    # 1. 导出任务树模板（已自动清理旧执行引用和本机路径）+ 同一份 id 映射
    tree_doc, node_map = export_template_plan(forest)

    # 2. 导出团队设置（按字段语义清理机器配置：路径 → 重绑定占位符，参数内凭据 → 掩码）
    cfg_raw_workers = None
    cfg_file = root / ".agentbus" / "config.json"
    if cfg_file.exists():
        try:
            cfg_raw = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(cfg_raw, dict) and "workers" in cfg_raw and isinstance(cfg_raw["workers"], dict):
                cfg_raw_workers = cfg_raw["workers"]
        except Exception as exc:
            note_silent("backup.py:249", "functional", f"配置原始文件解析失败，导出回退到已加载且可校验的配置：{exc}")
            pass  # 功能类：只影响备份中 workers 字段的来源，继续使用已加载配置并保留可诊断记录。
    workers_source = cfg_raw_workers if cfg_raw_workers is not None else cfg.data.get("workers", {})
    workers_clean, sanitize_report = _sanitize_workers(workers_source)

    team_settings = {
        "director": cfg.director,
        "assignment_mode": cfg.data.get("assignment_mode", "manual"),
        "gates": copy.deepcopy(cfg.gates),
        "workers": workers_clean,
    }

    # 3. 导出画布视觉偏好（便签、分支折叠、卡片优先级）
    notes = load_notes(root)
    branch_folds = load_branch_folds(root)
    priorities = load_priorities(root)

    # 便签是自由正文：只清理可判定的本机路径，并按需如实登记「需人工复核」。
    scrubbed_notes = []
    for note in notes:
        item = copy.deepcopy(note)
        text = str(item.get("text") or "")
        scrubbed, hits = scrub_local_paths(text, REBIND_PLACEHOLDER)
        if hits:
            sanitize_report["machine_paths_removed"] += hits
            item["text"] = scrubbed
        elif text and any(p.search(text) for p in _SENSITIVE_KEY_PATTERNS):
            sanitize_report["review_items"].append({
                "field": "ui_preferences.sticky_notes[%s].text" % item.get("id", "?"),
                "reason": "便签正文为自由正文，含疑似凭据字样；已保留但需人工复核",
            })
        scrubbed_notes.append(item)

    ui_preferences = {
        "sticky_notes": scrubbed_notes,
        # §13.7：折叠/优先级按节点 id 记录，必须与任务树用**同一映射**落到模板上，
        # 否则还原后它们指向的节点根本不存在（旧实现直接原样导出，必然解析失败）。
        "collapsed_branches": [node_map[b] for b in branch_folds if b in node_map],
        "card_priorities": {node_map[k]: v for k, v in priorities.items() if k in node_map},
    }
    preference_mapping = {
        "folds_total": len(branch_folds),
        "folds_mapped": len(ui_preferences["collapsed_branches"]),
        "priorities_total": len(priorities),
        "priorities_mapped": len(ui_preferences["card_priorities"]),
        "dropped": sorted(
            ["collapsed_branches:%s" % b for b in branch_folds if b not in node_map]
            + ["card_priorities:%s" % k for k in priorities if k not in node_map]),
    }

    sanitize_report["fields_removed"] = sorted(set(sanitize_report["fields_removed"]))
    sanitize_report["rebind_required"] = sorted(
        sanitize_report["rebind_required"], key=lambda item: item["field"])
    sanitize_report["free_text_review"] = sorted(sanitize_report["review_items"],
                                                 key=lambda item: item["field"])

    bundle = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "format": BACKUP_FORMAT,
        "metadata": {
            "title": tree_doc.get("title") or "项目备份",
            "description": tree_doc.get("description") or "",
            "requires_path_rebinding": True,
            "target_system_note": "导入后将绑定至当前本地工作区路径",
            "scope": "task_tree + team_settings + ui_preferences",
            "scope_note": BACKUP_SCOPE_NOTE,
            "preference_mapping": preference_mapping,
        },
        "task_tree": tree_doc,
        "team_settings": team_settings,
        "ui_preferences": ui_preferences,
        "sanitization": sanitize_report,
    }

    raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
    bundle["bundle_sha256"] = hashlib.sha256(raw).hexdigest()
    return bundle


export_project_backup = create_backup_bundle


def _audit_export(bundle: dict[str, Any]) -> dict[str, Any]:
    """对**导出成品**做独立复检，而不是复述导出时的意图。

    这是 §13.10 的要害：旧实现把 `no_machine_paths=True` / `no_credentials=True`
    写死成声明，于是「包里有 8 条真实绝对路径」与「一切干净」在预览里长得一样。
    这里改为扫描真实产物后**计算**结论。
    """
    remaining_paths: list[str] = []
    remaining_secrets: list[str] = []
    scopes = {
        "team_settings": bundle.get("team_settings") or {},
        "ui_preferences": bundle.get("ui_preferences") or {},
    }

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                sub = "%s.%s" % (trail, k)
                if any(p.search(str(k)) for p in _SENSITIVE_KEY_PATTERNS) and v not in (None, "", [], {}):
                    remaining_secrets.append(sub)
                walk(v, sub)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, "%s[%d]" % (trail, i))
        elif isinstance(node, str):
            if contains_local_path(node):
                remaining_paths.append("%s = %s" % (trail, node[:80]))

    for scope_name, scope in scopes.items():
        walk(scope, scope_name)

    return {
        "machine_paths_remaining": len(remaining_paths),
        "credential_fields_remaining": len(remaining_secrets),
        "remaining_paths": remaining_paths,
        "remaining_credentials": remaining_secrets,
    }


def preview_backup_export(workspace: Path | str) -> dict[str, Any]:
    """导出前预览：分析将要打包的项，展示可验证清单与**经过复检的**脱敏结论。"""
    bundle = create_backup_bundle(workspace)
    tree_preview = preview_template(bundle["task_tree"])
    report = bundle.get("sanitization") or _empty_report()
    audit = _audit_export(bundle)

    manifest = [
        {"item": "任务树拓扑", "count": tree_preview["node_count"], "detail": f"{tree_preview['leaves']} 个叶子节点，{tree_preview['branches']} 个分支"},
        {"item": "团队成员配置", "count": len(bundle["team_settings"]["workers"]), "detail": f"组长: {bundle['team_settings']['director']}"},
        {"item": "画布便签", "count": len(bundle["ui_preferences"]["sticky_notes"]), "detail": "保留颜色、坐标与内容"},
        {"item": "分支折叠状态", "count": len(bundle["ui_preferences"]["collapsed_branches"]), "detail": "折叠分支 ID 清单"},
        {"item": "卡片视觉优先级", "count": len(bundle["ui_preferences"]["card_priorities"]), "detail": "个人看板 P0/P1/P2 标记"},
    ]

    return {
        "format": BACKUP_FORMAT,
        "bundle_sha256": bundle["bundle_sha256"],
        "node_count": tree_preview["node_count"],
        "manifest": manifest,
        "sanitized": True,
        # —— 以下两个布尔值由「扫描导出成品」得出，不再是写死的声明 ——
        "no_machine_paths": audit["machine_paths_remaining"] == 0,
        "no_credentials": audit["credential_fields_remaining"] == 0,
        "sanitization": {
            "machine_paths_removed": report.get("machine_paths_removed", 0),
            "credentials_removed": report.get("credentials_removed", 0),
            "rebind_placeholder": REBIND_PLACEHOLDER,
            "rebind_required": report.get("rebind_required", []),
            "review_items": report.get("review_items", []),
            "scope": report.get("scope"),
            "unverified_note": report.get("unverified_note"),
            "verified_of_product": audit,
        },
    }


def _import_namespace(bundle: dict[str, Any], root: Path) -> str:
    """由「包摘要 + 目标工作区」派生的**确定性**命名空间。

    旧实现每次 `uuid4()`，于是同一个包重复还原会不断追加副本（§13.7 实测 2→4）。
    确定性命名空间让「同一包 + 同一目标」重试算出相同的节点 id，从而可识别为已存在。
    """
    seed = json.dumps({"bundle": _bundle_digest(bundle), "workspace": str(root)},
                      sort_keys=True)
    return "import-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _import_id_map(bundle: dict[str, Any], root: Path) -> dict[str, str]:
    """模板节点 id → 还原后的真实节点 id（与 `import_template` 同一算法）。"""
    namespace = _import_namespace(bundle, root)
    doc = bundle.get("task_tree") or {}
    return {node["id"]: namespace + ":" + hashlib.sha256(node["id"].encode("utf-8")).hexdigest()[:20]
            for kind in ("roots", "branches", "leaves") for node in doc.get(kind, [])}


def _current_ids(forest: TaskForest) -> tuple[set[str], set[str]]:
    """返回 `(全部节点 id, 在途叶子 id)`。"""
    snap = forest.snapshot()
    ids: set[str] = set()
    for kind in ("roots", "branches", "leaves"):
        ids |= {node["id"] for node in snap.get(kind, []) or []}
    in_flight = {leaf["id"] for leaf in snap.get("leaves", []) or []
                 if leaf.get("state") in IN_FLIGHT_STATES}
    return ids, in_flight


def _restore_plan(bundle: dict[str, Any], forest: TaskForest, root: Path,
                  mode: str) -> dict[str, Any]:
    """按模式计算**真实最终集合**——预览说「恢复后 N 个」就必须真的是 N 个。"""
    if mode not in RESTORE_MODES:
        raise ValueError("未知还原模式：%r；可选 %s" % (mode, list(RESTORE_MODES)))
    doc = bundle.get("task_tree") or {}
    template_leaves = len(doc.get("leaves", []))
    template_nodes = sum(len(doc.get(kind, [])) for kind in ("roots", "branches", "leaves"))
    mapping = _import_id_map(bundle, root)
    snap = forest.snapshot()
    existing: set[str] = set()
    for kind in ("roots", "branches", "leaves"):
        existing |= {node["id"] for node in snap.get(kind, []) or []}
    leaves_now = list(snap.get("leaves", []) or [])
    in_flight = {leaf["id"] for leaf in leaves_now if leaf.get("state") in IN_FLIGHT_STATES}
    bound_leaves = [leaf["id"] for leaf in leaves_now if leaf.get("task_id")]
    incoming = set(mapping.values())

    if mode == "replace":
        added, kept = len(incoming), 0
        dropped = len(existing - incoming)
        final_nodes = len(incoming)
        final_leaves = template_leaves
    else:
        added = len(incoming - existing)
        kept = len(existing)
        dropped = 0
        final_nodes = len(existing | incoming)
        # 追加/合并都保留本机叶子并补入模板叶子（模板叶子 id 与既有不冲突时）
        overlap = len({leaf["id"] for leaf in leaves_now} & incoming)
        final_leaves = len(leaves_now) + template_leaves - overlap

    return {
        "mode": mode,
        "mode_label": RESTORE_MODE_LABELS[mode],
        "current_nodes": len(existing),
        "current_leaves": len(leaves_now),
        "template_nodes": template_nodes,
        "final_nodes": final_nodes,
        "final_leaves": final_leaves,
        "added_nodes": added,
        "kept_local_nodes": kept,
        "dropped_local_nodes": dropped,
        "in_flight_leaves": sorted(in_flight),
        "bound_leaves": len(bound_leaves),
        "already_applied": mode != "replace" and not (incoming - existing),
        "namespace": _import_namespace(bundle, root),
        # id 映射要不要落盘？映射本身是可重算的，导出里只留计数与样本，避免体积膨胀。
        "id_map_sample": dict(sorted(mapping.items())[:3]),
        "id_map_size": len(mapping),
    }


def _bundle_digest(bundle: dict[str, Any]) -> str:
    content = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BACKUP_BYTES:
        raise ValueError("备份文件超过大小限制")
    digest = hashlib.sha256(raw).hexdigest()
    declared = bundle.get("bundle_sha256")
    if declared is not None and declared != digest:
        raise ValueError("备份 bundle_sha256 摘要不匹配；内容已改变或损坏")
    return digest


def _read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _atomic_bytes(path: Path, data: bytes | None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _transaction_paths(root: Path) -> list[Path]:
    return [root / ".agentbus/task-tree.json", root / ".agentbus/config.json", gui_state.state_path()]


def _restore_context(root: Path, bundle: dict[str, Any], mode: str, forest: TaskForest) -> str:
    snapshot = forest.snapshot()
    data = {"workspace": os.path.normcase(str(root)), "mode": mode,
            "bundle": _bundle_digest(bundle),
            "files": [hashlib.sha256(value).hexdigest() if value is not None else None
                      for value in map(_read_bytes, _transaction_paths(root))],
            "states": [(leaf["id"], leaf.get("state"), leaf.get("task_id"))
                       for leaf in snapshot.get("leaves", [])]}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _recover_restore(root: Path) -> None:
    """Finish a interrupted prepared commit only when every target still matches its CAS."""
    journal = root / ".agentbus/restore-transaction.json"
    if not journal.exists():
        return
    with task_lock(root / ".agentbus", "task-forest", "structure"), gui_state._STATE_LOCK:
        with gui_state._interprocess_lock(gui_state.state_path()) as locked:
            if not locked:
                raise ValueError("本机偏好锁不可用，恢复事务未写入；请重试")
            if not journal.exists():
                return
            record = json.loads(journal.read_text(encoding="utf-8"))
            paths = _transaction_paths(root)
            if (record.get("schema_version") != 1
                    or record.get("paths") != [str(p) for p in paths]
                    or len(record.get("changes", [])) != len(paths)):
                raise ValueError("恢复事务记录无效，保留原文件供核查")
            changes = [(base64.b64decode(item["before"], validate=True) if item["before"] is not None else None,
                        base64.b64decode(item["after"], validate=True) if item["after"] is not None else None)
                       for item in record["changes"]]
            for path, (before, after) in zip(paths, changes):
                if _read_bytes(path) not in (before, after):
                    raise ValueError("中断恢复后目标发生变动，CAS 冲突；保留事务和当前文件供核查")
            for path, (_, after) in zip(paths, changes):
                if _read_bytes(path) != after:
                    _atomic_bytes(path, after)
            journal.unlink()


def preview_backup_restore(workspace: Path | str, bundle: dict[str, Any],
                           mode: str = DEFAULT_RESTORE_MODE) -> dict[str, Any]:
    """恢复前预览与差异比对：**按所选模式**给出真实最终集合。"""
    if not isinstance(bundle, dict):
        raise ValueError("备份文件内容必须是 JSON 对象")
    if bundle.get("format") != BACKUP_FORMAT:
        raise ValueError(f"备份格式不匹配：期望 {BACKUP_FORMAT}，实际为 {bundle.get('format')}")
    schema_ver = bundle.get("schema_version", 1)
    if type(schema_ver) is not int or schema_ver > BACKUP_SCHEMA_VERSION or schema_ver < 1:
        raise ValueError(f"不支持的备份架构版本 (schema_version: {schema_ver})，最高支持 {BACKUP_SCHEMA_VERSION}")
    digest = _bundle_digest(bundle)

    root = Path(workspace).resolve()
    _recover_restore(root)
    preview_originals = {path: _read_bytes(path) for path in _transaction_paths(root)}
    cfg = load(root)
    forest = TaskForest(cfg)

    preview_context = _restore_context(root, bundle, mode, forest)
    # 1. 任务树预览 + 按模式计算真实最终集合
    tree_doc = bundle.get("task_tree", {})
    tree_preview = preview_template(tree_doc)
    plan = _restore_plan(bundle, forest, root, mode)

    current_snap = forest.snapshot()
    current_leaves_count = len(current_snap.get("leaves", []))
    target_leaves_count = plan["final_leaves"]

    # 2. 团队差异比较（还原是**合并**语义：本机独有成员一律保留，不会被删除）
    target_team = bundle.get("team_settings", {})
    current_director = cfg.director
    target_director = target_team.get("director", current_director)

    current_workers = set(cfg.data.get("workers", {}).keys())
    target_workers = set(target_team.get("workers", {}).keys())

    worker_diff = {
        "added": sorted(list(target_workers - current_workers)),
        "kept_local_only": sorted(list(current_workers - target_workers)),
        "common": sorted(list(current_workers & target_workers)),
        "policy": "合并：本机独有成员保留（不是删除）",
    }
    final_workers = sorted(current_workers | target_workers)

    # 3. UI 偏好差异
    target_ui = bundle.get("ui_preferences", {})
    current_notes_count = len(load_notes(root))
    target_notes_count = len(target_ui.get("sticky_notes", []))
    mapped_folds = len(target_ui.get("collapsed_branches", []) or [])
    mapped_priorities = len(target_ui.get("card_priorities", {}) or {})

    # 4. 本机重绑定评估：包里被脱敏的机器字段不会覆盖本机配置
    rebind_workers = _rebind_plan(bundle, cfg)

    diff_summary = {
        "tree_leaves_change": (
            f"当前 {current_leaves_count} 个任务 → 恢复后 {target_leaves_count} 个任务"
            f"（模式：{plan['mode']}）"),
        "tree_nodes_change": (
            f"当前 {plan['current_nodes']} 个节点 → 恢复后 {plan['final_nodes']} 个节点"
            f"（新增 {plan['added_nodes']}、保留本机 {plan['kept_local_nodes']}、"
            f"丢弃本机 {plan['dropped_local_nodes']}）"),
        "director_change": f"{current_director} → {target_director}" if current_director != target_director else "保持不变",
        "worker_members_diff": worker_diff,
        "final_worker_members": final_workers,
        "sticky_notes_change": f"当前 {current_notes_count} 条便签 → 恢复后 {target_notes_count} 条便签",
        "preference_mapping": (
            f"折叠 {mapped_folds} 项、优先级 {mapped_priorities} 项已按同一 id 映射对齐到真实节点"),
        "rebind_workspace": str(root),
        "rebind_workers": rebind_workers,
    }

    if (_restore_context(root, bundle, mode, forest) != preview_context
            or any(_read_bytes(path) != original for path, original in preview_originals.items())):
        raise ValueError('预览期间项目或偏好发生变化；请重新预览并核对')
    # Approval belongs to the state actually displayed, never a later concurrent edit.
    preview_id = "prev-" + uuid.uuid4().hex
    issued = time.monotonic()
    with _PREVIEW_LOCK:
        for key in list(_RESTORE_PREVIEWS):
            if issued - _RESTORE_PREVIEWS[key]["issued"] > RESTORE_PREVIEW_TTL_S:
                del _RESTORE_PREVIEWS[key]
        while len(_RESTORE_PREVIEWS) >= 128:
            _RESTORE_PREVIEWS.pop(next(iter(_RESTORE_PREVIEWS)))
        _RESTORE_PREVIEWS[preview_id] = {"issued": issued,
            "context": preview_context}

    return {
        "preview_id": preview_id,
        "bundle_sha256": digest,
        "bundle_integrity": "verified" if bundle.get("bundle_sha256") else "computed_legacy_unsigned",
        "preview_expires_in_s": RESTORE_PREVIEW_TTL_S,
        "diff_summary": diff_summary,
        "tree_preview": tree_preview,
        "requires_path_rebinding": True,
        "bound_workspace": str(root),
        "rebind_workers": rebind_workers,
        # —— §13.7：模式与真实最终集合 ——
        "mode": plan["mode"],
        "mode_label": plan["mode_label"],
        "plan": plan,
        "scope_note": (bundle.get("metadata") or {}).get("scope_note") or BACKUP_SCOPE_NOTE,
        "requires_replace_confirmation": plan["mode"] == "replace",
        "replace_blocked_by_in_flight": bool(plan["mode"] == "replace" and plan["in_flight_leaves"]),
        "replace_blocked_by_bindings": bool(plan["mode"] == "replace" and plan["bound_leaves"]),
    }


def _rebind_plan(bundle: dict[str, Any], cfg: Config) -> dict[str, dict[str, Any]]:
    """列出「导入时不会覆盖、必须保留本机绑定」的 worker 字段。"""
    plan: dict[str, dict[str, Any]] = {}
    target_workers = (bundle.get("team_settings") or {}).get("workers") or {}
    local_workers = cfg.data.get("workers") or {}
    for name, w_info in target_workers.items():
        if not isinstance(w_info, dict):
            continue
        # `import_notes` 本身就是一句说明（其中自然含占位符字样），不算待绑定字段。
        placeholders = sorted(k for k, v in w_info.items()
                              if k != IMPORT_NOTES_KEY and _carries_placeholder(v))
        # 本机已有同名字段时，占位符不覆盖本机值；本机完全没有该字段才需人工补齐。
        unbound = [k for k in placeholders
                   if k not in (local_workers.get(name) or {})]
        if placeholders:
            plan[name] = {
                "kept_local": [k for k in placeholders if k not in unbound],
                "needs_manual_binding": unbound,
            }
    return plan


def _carries_placeholder(value: Any) -> bool:
    """值本身、列表元素**或字典取值**是否为重绑定占位符。

    必须递归到 dict：`env` 这类字段本身不是路径，但其内部取值可能是占位符，
    漏判会把占位符写进本机配置（本叶开发时实际踩到）。
    """
    if isinstance(value, str):
        return REBIND_PLACEHOLDER in value
    if isinstance(value, list):
        return any(_carries_placeholder(item) for item in value)
    if isinstance(value, dict):
        return any(_carries_placeholder(item) for item in value.values())
    return False


_MISSING_PID = object()


def apply_backup_restore(workspace: Path | str, bundle: dict[str, Any],
                         preview_id: Any = _MISSING_PID,
                         mode: str = DEFAULT_RESTORE_MODE,
                         confirm_replace: bool = False) -> dict[str, Any]:
    """在用户确认差异后，正式应用还原操作（受任务树排他锁保护）。

    `mode` 见 `RESTORE_MODES`：默认 `merge`（幂等，重试不新增重复节点）。
    `replace` 属危险模式，必须显式 `confirm_replace=True`，且在途任务/已有任务绑定存在时拒绝。
    """
    root = Path(workspace).resolve()

    if mode not in RESTORE_MODES:
        raise ValueError("未知还原模式：%r；可选 %s" % (mode, list(RESTORE_MODES)))
    if mode == "replace" and confirm_replace is not True:
        raise ValueError("替换恢复会丢弃本机独有节点，必须显式传入 confirm_replace=True 才执行")
    with _PREVIEW_LOCK:
        approval = _RESTORE_PREVIEWS.get(preview_id) if isinstance(preview_id, str) else None
    if approval is None or time.monotonic() - approval["issued"] > RESTORE_PREVIEW_TTL_S:
        raise ValueError("恢复确认票据 (preview_id) 缺失或已失效，请先预览差异确认后再执行还原")
    _recover_restore(root)
    with task_lock(root / ".agentbus", "task-forest", "structure"), gui_state._STATE_LOCK:
        with gui_state._interprocess_lock(gui_state.state_path()) as locked:
            if not locked:
                raise ValueError("本机偏好锁不可用，还原未写入；请重试")
            cfg = load(root)
            forest = TaskForest(cfg)
            context = _restore_context(root, bundle, mode, forest)
            if context != approval["context"]:
                raise ValueError("恢复确认票据 (preview_id) 的目标、模式、备份或当前状态已变动，请重新预览")
            plan = _restore_plan(bundle, forest, root, mode)
            if mode == "replace" and (plan["in_flight_leaves"] or plan["bound_leaves"]):
                raise ValueError("拒绝替换恢复：目标仍有在途任务或已有任务绑定；请改用 merge/append")
            paths = _transaction_paths(root)
            originals = list(map(_read_bytes, paths))
            tree_doc = bundle.get("task_tree", {})
            namespace = _import_namespace(bundle, root)
            mapping = _import_id_map(bundle, root)
            # Prepare and validate every output off-target. No original file changes before all succeed.
            with tempfile.TemporaryDirectory(prefix="agentbus-restore-") as directory:
                staging = Path(directory)
                control = staging / ".agentbus"
                control.mkdir()
                stage_cfg = Config(path=control / "config.json", workspace=staging,
                                   data=copy.deepcopy(cfg.data))
                stage_forest = TaskForest(stage_cfg)
                stage_forest._write(forest._structure())
                if mode == "replace":
                    rebuilt: dict[str, Any] = {"schema_version": 1, "section_registry": []}
                    for kind in ("roots", "branches", "leaves"):
                        rebuilt[kind] = []
                        for node in tree_doc.get(kind, []):
                            item = copy.deepcopy(node)
                            item.pop("section_no", None)
                            item["id"] = mapping[node["id"]]
                            item["parent_id"] = mapping.get(node.get("parent_id"))
                            item["dependencies"] = [mapping[d] for d in node.get("dependencies", [])]
                            rebuilt[kind].append(item)
                    stage_forest.replace_structure(rebuilt)
                    id_map = dict(mapping)
                else:
                    _, id_map = import_template(stage_forest, tree_doc, namespace=namespace,
                                                merge=True, return_mapping=True)
                target_team = bundle.get("team_settings", {})
                rebound: dict[str, Any] = {}
                if target_team:
                    stage_cfg.data["director"] = target_team.get("director", cfg.director)
                    for key in ("assignment_mode", "gates"):
                        if key in target_team:
                            stage_cfg.data[key] = target_team[key]
                    for w_name, w_info in target_team.get("workers", {}).items():
                        incoming = {k: v for k, v in w_info.items() if k != IMPORT_NOTES_KEY}
                        deferred = sorted(k for k, v in incoming.items() if _carries_placeholder(v))
                        keep = {k: v for k, v in incoming.items() if k not in deferred}
                        local = stage_cfg.data["workers"].setdefault(w_name, {})
                        local.update(keep)
                        if deferred or w_info.get(IMPORT_NOTES_KEY):
                            rebound[w_name] = {"kept_local": [k for k in deferred if k in local],
                                "needs_manual_binding": [k for k in deferred if k not in local],
                                "import_note": w_info.get(IMPORT_NOTES_KEY, "")}
                    save(stage_cfg)
                elif originals[1] is not None:
                    stage_cfg.path.write_bytes(originals[1])
                stage_ui = staging / "ui.json"
                if originals[2] is not None:
                    stage_ui.write_bytes(originals[2])
                target_ui = bundle.get("ui_preferences", {})
                if "sticky_notes" in target_ui and not save_notes(root, target_ui["sticky_notes"], path=stage_ui):
                    raise ValueError("保存画布便签偏好失败，还原未写入")
                live_ids = {node["id"] for kind in ("roots", "branches", "leaves")
                            for node in stage_forest._structure()[kind]}
                folds_in = list(target_ui.get("collapsed_branches", []) or [])
                folds_out = [f for f in (id_map.get(x, x) for x in folds_in) if f in live_ids]
                prio_in = dict(target_ui.get("card_priorities", {}) or {})
                prio_out = {id_map.get(k, k): v for k, v in prio_in.items() if id_map.get(k, k) in live_ids}
                if "collapsed_branches" in target_ui and not save_branch_folds(root, folds_out, path=stage_ui):
                    raise ValueError("保存分支折叠偏好失败，还原未写入")
                if "card_priorities" in target_ui and not save_priorities(root, prio_out, path=stage_ui):
                    raise ValueError("保存卡片优先级偏好失败，还原未写入")
                outputs = [stage_forest.path.read_bytes(), stage_cfg.path.read_bytes(), _read_bytes(stage_ui)]
            if originals != list(map(_read_bytes, paths)):
                raise ValueError("恢复提交前 CAS 冲突，目标发生变动；还原未写入")
            journal = root / ".agentbus/restore-transaction.json"
            record = {"schema_version": 1, "paths": [str(p) for p in paths], "changes": [
                {"before": base64.b64encode(before).decode("ascii") if before is not None else None,
                 "after": base64.b64encode(after).decode("ascii") if after is not None else None}
                for before, after in zip(originals, outputs)]}
            _atomic_bytes(journal, json.dumps(record).encode("utf-8"))
            try:
                for path, before, after in zip(paths, originals, outputs):
                    if _read_bytes(path) != before:
                        raise ValueError("恢复提交时 CAS 冲突；事务保留供核查")
                    if before != after:
                        _atomic_bytes(path, after)
            except BaseException:
                for path, before, after in zip(paths, originals, outputs):
                    current = _read_bytes(path)
                    if current == after and current != before:
                        _atomic_bytes(path, before)
                journal.unlink()
                raise
            journal.unlink()
            with _PREVIEW_LOCK:
                _RESTORE_PREVIEWS.pop(preview_id, None)

    final_leaves = len(forest.snapshot().get("leaves", []) or [])
    return {
        "status": "restored",
        "restored": True,
        "mode": mode,
        "workspace": str(root),
        "bundle_sha256": bundle.get("bundle_sha256"),
        # 如实回报**实际最终**叶子数（旧实现回报的是备份里的叶子数，与真实结果不符）
        "leaves_count": final_leaves,
        "plan": plan,
        "preference_binding": {
            "folds_in": len(folds_in), "folds_bound": len(folds_out),
            "priorities_in": len(prio_in), "priorities_bound": len(prio_out),
        },
        "notes_count": len(target_ui.get("sticky_notes", [])),
        "rebound_workers": rebound,
        "scope_note": (bundle.get("metadata") or {}).get("scope_note") or BACKUP_SCOPE_NOTE,
    }
