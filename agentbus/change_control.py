# -*- coding: utf-8 -*-
"""变更控制：把一条新要求登记成可评估的变更记录，并给出处置建议。

来源：WorkBuddy，2026-10-08；任务 m-1791457584492-83bbee（作业 j-b7ed91331128d934d22268834e306bd4）。
借鉴 GitHub Spec Kit 的 constitution → specify → plan → tasks → converge 思路（把"要求"先变成可评估
对象，再变成工作项），但**不新增第二套计划接口**：正式计划的唯一写入口仍是现有
`project_plan.py` 的 CAS 采用流程；本模块只做纯函数判定，不写盘、不派工、不认领。

边界（写死）：
- 纯函数、零 I/O；不读文件、不联网、不写计划、不改任务书；不修改任何传入对象。
- 未明确采用（adopted=False）不得生成工作项、不得派工：`draft_work_items` 直接拒绝。
- 变更 ID 由**规范化后的要求全文**派生，同一要求重复提交幂等；长文本只截断展示字段，
  ID 仍按全文计算，避免长要求互相碰撞。
- 项目隔离：给出 project_id 时它参与 ID 计算，同一句要求在不同项目得到不同变更记录。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

SCHEMA_VERSION = 1
CHANGE_PREFIX = "CH"

MAX_REQUIREMENT_CHARS = 8000
MAX_DISPLAY_CHARS = 240
SIMILARITY_THRESHOLD = 0.55
DEFAULT_STALE_SECONDS = 7 * 24 * 3600

VALID_DISPOSITIONS = ("adopt", "research", "needs_clarification", "defer",
                      "duplicate_reuse", "reject")

STATUS_NEW = "proposed"
STATUS_ADOPTED = "adopted"
STATUS_WITHDRAWN = "withdrawn"
VALID_STATUSES = (STATUS_NEW, STATUS_ADOPTED, STATUS_WITHDRAWN)

WORK_ITEM_DONE_STATES = ("completed", "adopted_pending_evidence", "verified")

_WS = re.compile(r"\s+")


class ChangeControlError(ValueError):
    pass


def _fail(code: str, message: str) -> None:
    raise ChangeControlError(f"{code}: {message}")


def normalize_requirement(text: object) -> str:
    """NFKC + 折叠空白 + 去首尾；不做语义改写，不改变大小写以外的内容。"""
    if not isinstance(text, str):
        _fail("INVALID_REQUIREMENT", "新要求必须是字符串")
    normalized = _WS.sub(" ", unicodedata.normalize("NFKC", text)).strip()
    if not normalized:
        _fail("INVALID_REQUIREMENT", "新要求不能为空")
    if len(normalized) > MAX_REQUIREMENT_CHARS:
        _fail("REQUIREMENT_TOO_LONG",
              f"新要求超过 {MAX_REQUIREMENT_CHARS} 字符上限；请先拆分")
    return normalized


def stable_change_id(text: object, *, project_id: object = None,
                     source: object = None) -> str:
    """由规范化要求（可按需并入项目与来源）派生稳定 ID；同一输入恒定。"""
    normalized = normalize_requirement(text)
    parts = [normalized]
    if project_id is not None:
        parts.append(f"project:{project_id}")
    if source is not None:
        parts.append(f"source:{source}")
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{CHANGE_PREFIX}-{digest[:12]}"


def _tokens(text: str) -> set[str]:
    """CJK 友好的粗粒度分词：中日韩字符取二元组，其余按词。"""
    lowered = text.casefold()
    tokens: set[str] = set()
    cjk_run = ""
    for ch in lowered:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            cjk_run += ch
            continue
        if len(cjk_run) >= 2:
            tokens.update(cjk_run[i:i + 2] for i in range(len(cjk_run) - 1))
        elif cjk_run:
            tokens.add(cjk_run)
        cjk_run = ""
    if len(cjk_run) >= 2:
        tokens.update(cjk_run[i:i + 2] for i in range(len(cjk_run) - 1))
    elif cjk_run:
        tokens.add(cjk_run)
    tokens.update(word for word in re.split(r"[^0-9a-z_+-]+", lowered) if len(word) > 1)
    return tokens


def similarity(left: object, right: object) -> float:
    """Jaccard 相似度；用于发现"相似但不同"的要求，不用于合并。"""
    if not isinstance(left, str) or not isinstance(right, str):
        _fail("INVALID_REQUIREMENT", "相似度比较只接受字符串")
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


def _display(text: str) -> str:
    if len(text) <= MAX_DISPLAY_CHARS:
        return text
    return text[:MAX_DISPLAY_CHARS]


def _snapshot_fields(snapshot: dict) -> tuple[dict, list, list, dict]:
    if not isinstance(snapshot, dict):
        _fail("INVALID_SNAPSHOT", "任务书快照必须是对象")
    taskbook = snapshot.get("taskbook") or {}
    acceptance = taskbook.get("acceptance")
    if acceptance is None:
        acceptance = []
    work_items = taskbook.get("work_items")
    if work_items is None:
        work_items = []
    changes = snapshot.get("changes")
    if changes is None:
        changes = {}
    if not isinstance(acceptance, list) or not isinstance(work_items, list):
        _fail("INVALID_SNAPSHOT", "快照的 acceptance/work_items 必须是列表")
    if not isinstance(changes, dict):
        _fail("INVALID_SNAPSHOT", "快照的 changes 必须是对象")
    return taskbook, acceptance, work_items, changes


def _match_targets(requirement: str, acceptance: list, work_items: list,
                   *, limit: int = 6) -> tuple[list[str], list[str]]:
    """用相似度把要求映射到受影响验收项/工作项；只给建议映射，不改任务书。"""
    scored_a, scored_w = [], []
    for item in acceptance:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        text = f"{item.get('title', '')} {item.get('requirement', '')}"
        scored_a.append((similarity(requirement, text), str(item["id"])))
    for item in work_items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        text = f"{item.get('title', '')} {item.get('requirement', '')}"
        scored_w.append((similarity(requirement, text), str(item["id"])))
    scored_a.sort(key=lambda pair: (-pair[0], pair[1]))
    scored_w.sort(key=lambda pair: (-pair[0], pair[1]))
    hits_a = [identity for score, identity in scored_a[:limit] if score > 0]
    hits_w = [identity for score, identity in scored_w[:limit] if score > 0]
    return hits_a, hits_w


def _research_checklist(requirement: str, affected: list[str]) -> list[str]:
    checklist = [
        "核对现状：现有源码/测试是否已覆盖该要求（引用文件与行号，不猜）",
        "明确验收：写出可独立验收的通过条件与失败反例",
    ]
    if affected:
        checklist.append(f"评估对在途项的影响：{', '.join(affected)}（不得改动在途目标/预算/期限）")
    if "界面" in requirement or "GUI" in requirement.upper():
        checklist.append("界面类要求需补普通窗口实际走查证据，不能用单测代替")
    if "安装" in requirement or "升级" in requirement or "卸载" in requirement:
        checklist.append("分发类要求需在干净环境验证安装/升级/回滚/卸载")
    return checklist


def _dependencies(affected_work_items: list[str], changes: dict) -> list[str]:
    deps: list[str] = []
    for identity, record in sorted(changes.items()):
        if not isinstance(record, dict):
            continue
        if record.get("status") in (STATUS_NEW, "under_review") and identity:
            deps.append(identity)
    return sorted(set(deps + [f"work_item:{item}" for item in affected_work_items]))


def classify_change(requirement: object, *, snapshot: dict,
                    change_id: object = None, project_id: object = None,
                    source: object = None, now_ts: float,
                    max_age_s: float = DEFAULT_STALE_SECONDS,
                    evaluators: list | None = None,
                    reviews: list | None = None) -> dict:
    """把一条新要求登记成变更记录 + 处置建议；不写盘、不派工。"""
    normalized = normalize_requirement(requirement)
    identity = str(change_id) if change_id else stable_change_id(
        normalized, project_id=project_id, source=source)
    taskbook, acceptance, work_items, changes = _snapshot_fields(snapshot)

    flags: dict[str, Any] = {"duplicate": None, "similar": [], "conflict": [],
                             "stale": [], "review_missing": bool(evaluators) and not reviews}
    adopted_revision = snapshot.get("adopted_revision") or {}
    observed_at = snapshot.get("observed_at")
    if isinstance(observed_at, (int, float)) and now_ts - observed_at > max_age_s:
        flags["stale"].append("SNAPSHOT_STALE")
    if not isinstance(adopted_revision, dict) or not adopted_revision.get("content_sha256"):
        flags["stale"].append("BASELINE_UNPINNED")
    if snapshot.get("current_revision_sha256") and adopted_revision.get(
            "content_sha256") and snapshot["current_revision_sha256"] != adopted_revision[
            "content_sha256"]:
        flags["stale"].append("CAS_BASELINE_CHANGED")

    existing = changes.get(identity)
    for other_id, record in sorted(changes.items()):
        if other_id == identity or not isinstance(record, dict):
            continue
        other_text = record.get("requirement")
        if not isinstance(other_text, str):
            continue
        score = similarity(normalized, other_text)
        if score >= SIMILARITY_THRESHOLD:
            flags["similar"].append({"change_id": other_id, "similarity": score,
                                     "status": record.get("status", STATUS_NEW)})

    affected_acceptance, affected_work_items = _match_targets(
        normalized, acceptance, work_items)
    for item in work_items:
        if not isinstance(item, dict):
            continue
        identity_hit = str(item.get("id")) in affected_work_items
        if identity_hit and item.get("status") in ("in_progress", "in_review"):
            flags["conflict"].append({"work_item": str(item.get("id")),
                                      "status": item.get("status"),
                                      "reason": "IN_FLIGHT"})

    completed_reuse = next((item for item in flags["similar"]
                            if item["status"] in WORK_ITEM_DONE_STATES), None)
    if existing is not None and isinstance(existing, dict):
        status = existing.get("status", STATUS_NEW)
        disposition = {"adopted": "duplicate_reuse", "withdrawn": "research"}.get(status, "research")
        duplicate_of = identity
    elif completed_reuse is not None:
        disposition, duplicate_of = "duplicate_reuse", completed_reuse["change_id"]
    elif flags["conflict"]:
        disposition, duplicate_of = "defer", None
    elif flags["similar"]:
        disposition, duplicate_of = "needs_clarification", None
    elif flags["stale"]:
        disposition, duplicate_of = "research", None
    else:
        disposition, duplicate_of = "research", None
    flags["duplicate"] = duplicate_of

    return {
        "schema_version": SCHEMA_VERSION,
        "change_id": identity,
        "requirement": _display(normalized),
        "requirement_chars": len(normalized),
        "requirement_truncated": len(normalized) > MAX_DISPLAY_CHARS,
        "status": STATUS_NEW,
        "adopted": False,
        "may_dispatch": False,
        "affected_acceptance": affected_acceptance,
        "affected_work_items": affected_work_items,
        "research_checklist": _research_checklist(normalized, affected_work_items),
        "evaluators": [str(name) for name in (evaluators or [])],
        "dependencies": _dependencies(affected_work_items, changes),
        "flags": flags,
        "suggested_disposition": disposition,
        "duplicate_of": duplicate_of,
        "baseline_revision": adopted_revision.get("revision_id"),
        "baseline_sha256": adopted_revision.get("content_sha256"),
        "observed_at": now_ts,
        "note": "处置建议不等于采用；正式计划只能经 project_plan 的 CAS 采用流程变更。",
    }


def adopt_change(record: dict, *, adopted_by: object, at: float,
                 revision_id: object = None, content_sha256: object = None) -> dict:
    """显式采用：只有这一步之后才允许生成工作项；不修改传入对象。"""
    if not isinstance(record, dict) or not record.get("change_id"):
        _fail("INVALID_RECORD", "变更记录缺少 change_id")
    if record.get("status") == STATUS_WITHDRAWN:
        _fail("WITHDRAWN_CHANGE", "已撤回的变更必须重新登记后才能采用")
    if not isinstance(adopted_by, str) or not adopted_by.strip():
        _fail("INVALID_ADOPTION", "采用人不能为空")
    if not isinstance(revision_id, str) or not isinstance(content_sha256, str):
        _fail("INVALID_ADOPTION", "采用必须给出正式计划的 revision_id 与 content_sha256")
    out = dict(record)
    out.update(status=STATUS_ADOPTED, adopted=True, may_dispatch=True,
               adopted_by=adopted_by.strip(), adopted_at=at,
               adopted_revision=revision_id, adopted_revision_sha256=content_sha256)
    return out


def withdraw_change(record: dict, *, by: object, at: float, reason: object) -> dict:
    if not isinstance(record, dict) or not record.get("change_id"):
        _fail("INVALID_RECORD", "变更记录缺少 change_id")
    if not isinstance(reason, str) or not reason.strip():
        _fail("INVALID_WITHDRAWAL", "撤回必须给出原因")
    out = dict(record)
    out.update(status=STATUS_WITHDRAWN, adopted=False, may_dispatch=False,
               withdrawn_by=str(by), withdrawn_at=at, withdrawal_reason=reason.strip())
    return out


def draft_work_items(record: dict, *, owner_map: dict, artifacts_root: object) -> list[dict]:
    """仅为**已采用**的变更生成工作项草案；未采用一律拒绝派工。"""
    if not isinstance(record, dict):
        _fail("INVALID_RECORD", "变更记录必须是对象")
    if not record.get("adopted") or record.get("status") != STATUS_ADOPTED:
        _fail("NOT_ADOPTED_CANNOT_DISPATCH",
              "变更尚未采用，不得生成工作项或派工")
    if not isinstance(owner_map, dict) or not owner_map:
        _fail("INVALID_OWNER_MAP", "生成工作项需要 负责人 映射")
    owner = owner_map.get(record.get("change_id")) or owner_map.get("default")
    if not isinstance(owner, str) or not owner.strip():
        _fail("INVALID_OWNER_MAP", "未给该变更指定负责人，也没有 default")
    acceptance = record.get("affected_acceptance") or ["A?"]
    items = []
    for index, acceptance_id in enumerate(acceptance if isinstance(acceptance, list) else [], start=1):
        items.append({
            "id": f"{record['change_id']}-{index}",
            "change_id": record["change_id"],
            "title": _display(str(record.get("requirement") or "")),
            "owner": owner.strip(),
            "acceptance": str(acceptance_id),
            "artifacts_root": str(artifacts_root),
            "adopted_revision": record.get("adopted_revision"),
            "adopted_revision_sha256": record.get("adopted_revision_sha256"),
            "status": "planned",
        })
    return items
