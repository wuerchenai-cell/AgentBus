# -*- coding: utf-8 -*-
"""Task 2：技能同步只读预览与维护者边界（纯函数）。

来源：Trae（平级成员），2026-10-05，任务 m-1791170402277-51f640（job j-cc56fdf40e0c01743b077f4793e34447）。
修正自 m-1791169024289-437239（job j-b2ea92e011777b6bcfa403cc91d2b68d），原 candidate 与原回执保留。
再修正：任务 m-1791174705583-dc25a7（OpenCode 实际互审反例，来源 Codex 2026-10-05）；
原 author-corrections-snapshot 与旧回执不修改。
计划快照 r-cec9c20aaaf9fa20f215ab8338a7efd7 / content_sha256 faa96eaf…5ac926。
依据：IMPLEMENTATION_PLAN.md Task 2、DESIGN_RESOLUTIONS.md 第 1/2/3/4/5 节、Global Constraints、
独立复核五项缺陷。

边界（写死，不可由调用方放宽）：
- 本模块**不访问磁盘**、不执行脚本、不安装、不读私有配置或凭据；source/target 由 Task1 传入。
- 预览永远不授予写权：`write_permitted` 恒为 False；本阶段也不授予删除权。
- 维护者缺省 unknown；已有目标没有可核 AgentBus 部署记录时，即使字节完全一致也不获得更新/删除权。
- 安装事实、原生发现、实际调用三态互不代证，初值均为 unverified。
- 目录内容摘要只证明所有普通文件原始字节（含脚本/BOM/换行），不覆盖执行权限、宿主解释条件；
  执行元数据独立记为 unknown，不混入产物协议。
- 本函数只解释“为什么不许写”，不构成任何实现完成声明。

五项复核修正：
1. 严格验证 files 三字段清单（键集/类型/相对路径/去重）并按 Task1 契约重算 canonical digest，
   声称的 content_digest 必须与重算一致；伪造或篡改清单不再可信。
2. 记录摘要只认契约字段 `before_content_digest`；旧 SKILL.md-only 的 `skill_sha256` 语义不等价，
   不再作为别名兼容。
3. 部署记录的宿主/作用域/路线必须与本次调用一致才可用于安装事实；已丢失的 target 不得显示 recorded。
4. 当前阶段没有任何固定执行载体（无带日期的可核执行证据），`execution_fixed` 恒为 False；
   将来接入 dated evidence 后另行实现，不能仅凭记录里的 flags 标绿。
5. 畸形 `active_task_ids` 既不抛异常，也不按字符串拆成单字符。
6. 清单统一 Task1 目录契约（OpenCode 互审反例）：私有/控制状态路径、UTF-8 整串排序、
   total_bytes 对账、根 SKILL.md 入口与规模上限（256 文件 / 32MiB / 单 8MiB）均须成立；
   name/source 只认合法类型；超界输入判失败而非截断。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

# 与 Task1 扫描器共用同一目录契约（安全路径分量、UTF-8 整串排序、规模上限、入口名），
# 不复制规则也不修改 skill_packages；只复用其现有校验接口。
from .skill_packages import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_TOTAL_BYTES,
    SKILL_ENTRY,
    SkillPackageError,
    _check_component,
    _sort_key,
)

# Codex internal independent audit integration correction, 2026-10-05.
# Author bytes are preserved separately; source_verified is produced here
# only after manifest/digest validation, not a user declaration of host usage.

SCHEMA_VERSION = 1
DIGEST_COVERS = "paths_and_raw_bytes_only"
DIGEST_EXCLUDES = ["posix_exec_mode", "windows_acl", "host_interpretation"]

ACTION_CREATE = "create"
ACTION_UNCHANGED = "unchanged"
ACTION_UPDATE = "update"
ACTION_CONFLICT = "conflict"

VALID_SCOPES = ("project", "global")
VALID_ROUTES = ("desktop", "cli", "independent")

MANIFEST_FIELDS = frozenset({"path", "bytes", "sha256"})
RECORD_DIGEST_FIELD = "before_content_digest"
# 当前未实现固定执行载体：没有带日期的可核执行证据，故恒为 False（修正 4）。
EXECUTION_FIX_IMPLEMENTED = False

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_NOTES = [
    "只读预览：write_permitted 恒为 False，本阶段不授予删除权",
    "维护者缺省 unknown；无可核 AgentBus 部署记录不获得更新/删除权",
    "安装/发现/调用三态互不代证，初值 unverified",
    "内容摘要只覆盖原始字节，不覆盖执行权限与宿主解释条件",
    "当前未实现固定执行载体，execution_fixed 恒为 False；将来以带日期的可核证据另接",
]


def _is_digest(value) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _safe_path(value) -> bool:
    """条目路径必须是安全的相对 POSIX 路径，且与 Task1 `_check_component` 同一规则。

    覆盖：空/./..、尾随空格或点、控制字符、保留字符、私有目录分量（.workbuddy/.git/
    .agentbus/.ssh 等）与 Windows 保留设备名；绝对路径、反斜杠与盘符另行拒绝。
    """
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("/") or "\\" in value or ":" in value:
        return False
    for part in value.split("/"):
        try:
            _check_component(part, where="manifest path")
        except SkillPackageError:
            return False
    return True


def _canonical_digest(files: list) -> str:
    """与 Task1 相同：三字段清单的 canonical JSON 原始字节 SHA256。"""
    payload = {"schema_version": SCHEMA_VERSION, "files": files}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _manifest_ok(files, *, max_files: int = DEFAULT_MAX_FILES,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> bool:
    """严格校验 Task1 三字段清单：键集/类型/安全路径/去重/规模/UTF-8 整串排序。

    超界输入一律判失败（返回 False），不截断也不部分接受。入口名（SKILL.md）属包级
    契约，由 `_package_ok` 施加，以便展示层复用本函数校验产物清单时不被误伤。
    """
    if not isinstance(files, list) or not files:
        return False
    if len(files) > max_files:
        return False
    seen: set[str] = set()
    paths: list[str] = []
    total = 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != MANIFEST_FIELDS:
            return False
        path = entry["path"]
        if not _safe_path(path):
            return False
        size = entry["bytes"]
        if type(size) is not int or size < 0 or size > max_file_bytes:
            return False
        if not _is_digest(entry["sha256"]):
            return False
        key = unicodedata.normalize("NFC", path).casefold()
        if key in seen:
            return False
        seen.add(key)
        total += size
        if total > max_total_bytes:
            return False
        paths.append(path)
    # 顺序契约：清单必须等于按 UTF-8 相对 POSIX 整串字节序排序的结果。
    if paths != sorted(paths, key=_sort_key):
        return False
    return True


def _package_ok(package) -> bool:
    """严格校验 Task1 包结构：清单契约 + total_bytes 对账 + 重算 canonical digest。

    name/source 只认非空字符串，total_bytes 只认严格非负整数且必须等于清单字节和；
    伪造数字不透传。声称的 content_digest 必须与重算一致。
    """
    if not isinstance(package, dict):
        return False
    if not (type(package.get("schema_version")) is int
            and package.get("schema_version") == SCHEMA_VERSION):
        return False
    for field in ("name", "source"):
        value = package.get(field)
        if not isinstance(value, str) or not value:
            return False
    digest = package.get("content_digest")
    if not _is_digest(digest):
        return False
    files = package.get("files")
    if not _manifest_ok(files):
        return False
    # 入口契约：与 Task1 一致，根目录必须有 SKILL.md（仅包级，产物清单不适用）。
    if SKILL_ENTRY not in {entry["path"] for entry in files}:
        return False
    total = package.get("total_bytes")
    if type(total) is not int or total < 0:
        return False
    if total != sum(entry["bytes"] for entry in files):
        return False
    return _canonical_digest(files) == digest


def _record_digest(record) -> str | None:
    """只认契约字段 before_content_digest；不接受旧 SKILL.md-only 的 skill_sha256。"""
    if not isinstance(record, dict):
        return None
    value = record.get(RECORD_DIGEST_FIELD)
    return value if isinstance(value, str) else None


def _record_ok(record) -> bool:
    if not isinstance(record, dict):
        return False
    if not (type(record.get("schema_version")) is int
            and record.get("schema_version") == SCHEMA_VERSION):
        return False
    if not isinstance(record.get("owner"), str) or not record["owner"].strip():
        return False
    return _is_digest(_record_digest(record))


def _record_context_ok(record, host, scope, route) -> bool:
    """记录的宿主/作用域/路线必须与本次调用完全一致，才可用于安装事实。"""
    if not _record_ok(record):
        return False
    return (record.get("host") == host and record.get("scope") == scope
            and record.get("route") == route)


def _active_pins(value) -> tuple[tuple, bool]:
    """返回 (pins, ok)。畸形输入既不抛异常，也不把字符串拆成单字符。"""
    if value is None:
        return (), True
    if isinstance(value, (str, bytes, bytearray)):
        return (), False
    if not isinstance(value, (list, tuple, set, frozenset)):
        return (), False
    pins: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return (), False
        pins.append(item)
    return tuple(pins), True


def _summary(package: dict | None) -> dict | None:
    """摘要字段只认合法类型，伪造数字/对象不透传（否则回落 None）。"""
    if package is None:
        return None
    files = package.get("files")
    name = package.get("name")
    source = package.get("source")
    digest = package.get("content_digest")
    total = package.get("total_bytes")
    return {
        "name": name if isinstance(name, str) else None,
        "source": source if isinstance(source, str) else None,
        "content_digest": digest if _is_digest(digest) else None,
        "total_bytes": total if type(total) is int and total >= 0 else None,
        "file_count": len(files) if isinstance(files, list) else None,
    }


def build_sync_preview(source: dict, target: dict | None, record: dict | None, *,
                       host: str, scope: str, route: str,
                       active_task_ids=()) -> dict:
    """对一次“源→目标”同步给出只读判定与原因，永不授予写权。

    返回 action ∈ create/unchanged/update/conflict、reason_codes、owner、
    installed/discovered/called 三态与内容摘要；write_permitted 恒为 False。
    """
    reasons: set[str] = {"WRITE_NOT_PERMITTED"}

    host_ok = isinstance(host, str) and bool(_HOST.fullmatch(host))
    if not host_ok:
        reasons.add("INVALID_HOST")
    scope_ok = scope in VALID_SCOPES
    if not scope_ok:
        reasons.add("INVALID_SCOPE")
    route_ok = route in VALID_ROUTES
    if not route_ok:
        reasons.add("INVALID_ROUTE")

    source_ok = _package_ok(source)
    if not source_ok:
        reasons.add("INVALID_SOURCE")
    target_ok = target is None or _package_ok(target)
    if not target_ok:
        reasons.add("INVALID_TARGET")
    record_ok = record is None or _record_ok(record)
    if not record_ok:
        reasons.add("INVALID_RECORD")

    pins, pins_ok = _active_pins(active_task_ids)
    if not pins_ok:
        reasons.add("INVALID_ACTIVE_TASK_IDS")
    elif pins:
        reasons.add("ACTIVE_TASK_PIN")

    owner = "unknown"
    if record_ok and isinstance(record, dict):
        owner = str(record.get("owner") or "unknown")

    source_digest = source.get("content_digest") if source_ok else None
    target_present = target is not None and target_ok
    target_digest = target.get("content_digest") if target_present else None

    context_ok = _record_context_ok(record, host, scope, route)
    owner_is_agentbus = (record_ok and isinstance(record, dict)
                         and str(record.get("owner")) == "agentbus")
    if owner_is_agentbus and not context_ok:
        reasons.add("RECORD_CONTEXT_MISMATCH")

    # 安装事实：须有可核记录、属于 AgentBus、上下文一致且目标确实存在（修正 3）。
    installed = ("recorded"
                 if (owner_is_agentbus and context_ok and target_present)
                 else "unverified")
    states = {"installed": installed, "discovered": "unverified",
              "called": "unverified"}

    if target_present:
        # 同内容也不授予删除权：删除权是独立契约，不由字节一致推导。
        reasons.add("DELETE_RIGHT_NOT_GRANTED")

    hard_invalid = not (host_ok and scope_ok and route_ok
                        and source_ok and target_ok and record_ok and pins_ok)

    if hard_invalid or pins:
        action = ACTION_CONFLICT
    elif target is None:
        if record is not None:
            # 记录声称已安装，但目标已丢失：不静默重建，交人工确认。
            action = ACTION_CONFLICT
            reasons.add("TARGET_LOST")
        else:
            action = ACTION_CREATE
            reasons.add("TARGET_ABSENT")
    elif record is None:
        action = ACTION_CONFLICT
        reasons.add("OWNER_UNKNOWN")
    elif str(record.get("owner")) != "agentbus":
        action = ACTION_CONFLICT
        reasons.add("OWNER_NOT_AGENTBUS")
    elif not context_ok:
        action = ACTION_CONFLICT
        reasons.add("RECORD_CONTEXT_MISMATCH")
    elif _record_digest(record) != target_digest:
        action = ACTION_CONFLICT
        reasons.add("TARGET_MODIFIED")
    elif source_digest == target_digest:
        action = ACTION_UNCHANGED
        reasons.add("UNCHANGED")
    else:
        action = ACTION_UPDATE
        reasons.add("UPDATE_AVAILABLE")

    # 修正 4：当前无固定执行载体，恒为 False；仅凭 flags 不得标绿。
    execution_fixed = EXECUTION_FIX_IMPLEMENTED
    if not execution_fixed:
        reasons.add("EXECUTION_FIX_UNVERIFIED")

    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "write_permitted": False,
        "delete_permitted": False,
        "reason_codes": sorted(reasons),
        "owner": owner,
        "host": host,
        "scope": scope,
        "route": route,
        "source": _summary(source if source_ok else None),
        "target": _summary(target if target_present else None),
        "content": {
            "source_verified": source_ok,
            "source_digest": source_digest,
            "target_digest": target_digest,
            "digest_covers": DIGEST_COVERS,
            "digest_excludes": list(DIGEST_EXCLUDES),
            "execution_metadata_digest": "unknown",
        },
        "states": states,
        "execution_fixed": execution_fixed,
        "notes": list(_NOTES),
    }
