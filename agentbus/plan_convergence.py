# -*- coding: utf-8 -*-
"""计划收敛：覆盖矩阵、完成证据校验、ADR 记录与短状态/接手投影。

来源：WorkBuddy，2026-10-08；任务 m-1791457584492-83bbee。
借鉴 Spec Kit 的 converge 阶段（把已采用要求与产物对齐）与 constitution/ADR 思路，
但**正式计划接口仍只有 project_plan.py 的 CAS**：本模块不写计划、不改任务书、只做纯函数判定与
"引用型"投影，不复制长历史。

边界（写死）：纯函数、零 I/O、不修改传入对象、不派工；证据不足一律写 unverified，不猜。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA_VERSION = 1

STATUS_DONE = ("completed", "verified")
STATUS_OPEN = ("planned", "in_progress", "in_review", "blocked", "awaiting_evidence", "not_started")
VALID_STATUSES = STATUS_DONE + STATUS_OPEN

MAX_STATE_CHARS = 3000
MAX_HANDOFF_CHARS = 4000
MAX_ADR_REFS = 20

_WS = re.compile(r"\s+")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ConvergenceError(ValueError):
    pass


def _fail(code: str, message: str) -> None:
    raise ConvergenceError(f"{code}: {message}")


def _text(value: Any, *, label: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("INVALID_TEXT", f"{label} 必须是非空字符串")
    collapsed = _WS.sub(" ", value).strip()
    if len(collapsed) > limit:
        _fail("TEXT_TOO_LONG", f"{label} 超过 {limit} 字符上限")
    return collapsed


def _manifest_ok(entries: Any) -> tuple[bool, list[str]]:
    """清单形状校验：每项严格 path/bytes/sha256；不接受多字段或非法摘要。"""
    if entries is None:
        return False, ["MANIFEST_MISSING"]
    if not isinstance(entries, list) or not entries:
        return False, ["MANIFEST_INVALID"]
    problems: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            problems.append("MANIFEST_ENTRY_SHAPE")
            continue
        path = entry["path"]
        if (not isinstance(path, str) or not path.strip() or ".." in path.replace("\\", "/").split("/")
                or path.startswith("/") or re.match(r"[A-Za-z]:", path)):
            problems.append("MANIFEST_PATH_UNSAFE")
        digest = entry["sha256"]
        if not isinstance(digest, str) or not _SHA256.match(digest):
            problems.append("MANIFEST_DIGEST_INVALID")
        if type(entry["bytes"]) is not int or entry["bytes"] < 0:
            problems.append("MANIFEST_BYTES_INVALID")
        key = str(path).casefold()
        if key in seen:
            problems.append("MANIFEST_DUPLICATE")
        seen.add(key)
    return (not problems), sorted(set(problems))


def _receipt_ok(receipt: Any, *, item_id: str) -> tuple[bool, list[str]]:
    """完成的必要条件：合法关联的回执 + 校验通过的清单；缺一即 unverified。"""
    if not isinstance(receipt, dict):
        return False, ["RECEIPT_MISSING"]
    problems: list[str] = []
    if not isinstance(receipt.get("result_id"), str) or not receipt["result_id"].strip():
        problems.append("RECEIPT_ID_MISSING")
    if receipt.get("reply_to") != item_id:
        problems.append("RECEIPT_ROUTE_MISMATCH")
    if receipt.get("exit_code") not in (None, 0):
        problems.append("RECEIPT_FAILED")
    manifest_ok, manifest_problems = _manifest_ok(receipt.get("manifest"))
    if not manifest_ok:
        problems.extend(manifest_problems)
    if receipt.get("manifest_verified") is not True:
        problems.append("MANIFEST_NOT_VERIFIED")
    return (not problems), sorted(set(problems))


def _snapshot_parts(snapshot: dict) -> tuple[dict, list, list, dict]:
    if not isinstance(snapshot, dict):
        _fail("INVALID_SNAPSHOT", "快照必须是对象")
    taskbook = snapshot.get("taskbook") or {}
    work_items = taskbook.get("work_items") or []
    acceptance = taskbook.get("acceptance") or []
    changes = snapshot.get("changes") or {}
    if not isinstance(work_items, list):
        _fail("INVALID_SNAPSHOT", "work_items 必须是列表")
    if not isinstance(changes, dict):
        _fail("INVALID_SNAPSHOT", "changes 必须是对象")
    return taskbook, work_items, acceptance, changes


def convergence_report(snapshot: dict, receipts: dict | None = None, *,
                       now_ts: float, previous_plan_item_ids=None) -> dict:
    """覆盖矩阵 + 违规清单；只读，不改任务书也不改证据。"""
    taskbook, work_items, acceptance, changes = _snapshot_parts(snapshot)
    receipts = receipts if isinstance(receipts, dict) else {}
    coverage: list[dict] = []
    violations: list[dict] = []

    adopted_changes = {cid: record for cid, record in changes.items()
                       if isinstance(record, dict) and record.get("adopted")}
    mapped_changes: set[str] = set()
    seen_ids: set[str] = set()

    for item in work_items:
        if not isinstance(item, dict) or not item.get("id"):
            violations.append({"code": "MALFORMED_WORK_ITEM", "item_id": None,
                               "message": "工作项缺少 id 或不是对象"})
            continue
        item_id = str(item["id"])
        if item_id in seen_ids:
            violations.append({"code": "DUPLICATE_WORK_ITEM", "item_id": item_id,
                               "message": "同一工作项 ID 出现多次"})
            continue
        seen_ids.add(item_id)
        status = item.get("status", "not_started")
        if status not in VALID_STATUSES:
            violations.append({"code": "UNKNOWN_STATUS", "item_id": item_id,
                               "message": f"未知状态：{status}"})
        change_id = item.get("change_id")
        if change_id:
            mapped_changes.add(str(change_id))
        owner = item.get("owner")
        artifacts = item.get("artifacts") or []
        requirement_acceptance = item.get("acceptance") or []
        row = {"item_id": item_id, "change_id": change_id,
               "title": str(item.get("title") or ""), "owner": owner,
               "artifacts": [str(path) for path in artifacts] if isinstance(artifacts, list) else [],
               "acceptance": ([str(a) for a in requirement_acceptance]
                              if isinstance(requirement_acceptance, list) else []),
               "status": status, "evidence": None}
        if not owner:
            violations.append({"code": "MISSING_OWNER", "item_id": item_id,
                               "message": "已采用要求必须映射到负责人"})
        if not row["artifacts"]:
            violations.append({"code": "MISSING_ARTIFACTS", "item_id": item_id,
                               "message": "已采用要求必须指明产物路径"})
        if not row["acceptance"]:
            violations.append({"code": "MISSING_ACCEPTANCE", "item_id": item_id,
                               "message": "已采用要求必须映射到验收项"})
        if status in STATUS_DONE:
            ok, problems = _receipt_ok(receipts.get(item_id), item_id=item_id)
            row["evidence"] = {"result_id": (receipts.get(item_id) or {}).get("result_id"),
                               "verified": ok, "problems": problems}
            if not ok:
                violations.append({"code": "UNVERIFIED_COMPLETION", "item_id": item_id,
                                   "message": "完成项缺少合法关联回执或校验通过的清单："
                                              + ",".join(problems)})
        coverage.append(row)

    for change_id in sorted(adopted_changes):
        if change_id not in mapped_changes:
            violations.append({"code": "UNMAPPED_ADOPTED_REQUIREMENT",
                               "item_id": None, "change_id": change_id,
                               "message": "已采用要求没有对应工作项（新要求不得让旧要求消失）"})

    for item_id in sorted(set(previous_plan_item_ids or [])):
        if item_id in seen_ids:
            continue
        prior = next((item for item in work_items
                      if isinstance(item, dict) and str(item.get("id")) == item_id), None)
        if prior is None:
            violations.append({"code": "DISAPPEARED_WORK_ITEM", "item_id": str(item_id),
                               "message": "上一版计划里存在且未完成的工作项在当前计划中消失"})

    counts: dict[str, int] = {}
    for row in coverage:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"schema_version": SCHEMA_VERSION, "observed_at": now_ts,
            "coverage": coverage, "violations": violations,
            "counts": dict(sorted(counts.items())),
            "accepted_requirements": len(adopted_changes),
            "acceptance_items": len([a for a in acceptance if isinstance(a, dict) and a.get("id")]),
            "ok": not violations,
            "note": "覆盖检查只说明映射与证据形状；不等于产品验收通过。"}


def build_adr(change: dict, *, decision: str, decided_at: float, decided_by: str,
              context: str = "", consequences: str = "", supersedes=None,
              references=None, status: str = "accepted") -> dict:
    """生成一条 ADR（决策记录）；内容引用变更与证据，不复制长历史。"""
    if not isinstance(change, dict) or not change.get("change_id"):
        _fail("INVALID_CHANGE", "ADR 需要带 change_id 的变更记录")
    if decision not in ("adopt", "reject", "defer", "supersede", "withdraw"):
        _fail("INVALID_DECISION", f"未知决策：{decision}")
    if status not in ("proposed", "accepted", "superseded"):
        _fail("INVALID_STATUS", f"未知 ADR 状态：{status}")
    refs = [str(value) for value in (references or [])][:MAX_ADR_REFS]
    payload = {"change_id": change["change_id"], "decision": decision,
               "decided_at": decided_at, "decided_by": str(decided_by),
               "supersedes": supersedes}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                       sort_keys=True).encode("utf-8")).hexdigest()
    return {"schema_version": SCHEMA_VERSION, "adr_id": f"ADR-{digest[:10]}",
            "title": _text(change.get("requirement") or "", label="requirement", limit=200),
            "status": status, "decision": decision,
            "context": _WS.sub(" ", context).strip() or "见变更记录与任务书快照",
            "consequences": _WS.sub(" ", consequences).strip() or "未填写",
            "change_id": change["change_id"],
            "baseline_revision": change.get("baseline_revision") or change.get("adopted_revision"),
            "baseline_sha256": change.get("baseline_sha256") or change.get("adopted_revision_sha256"),
            "decided_at": decided_at, "decided_by": str(decided_by),
            "supersedes": supersedes, "references": refs}


def is_adopted_requirement(record: Any) -> bool:
    return isinstance(record, dict) and bool(record.get("adopted"))


def project_state(snapshot: dict, *, limit: int = 12) -> str:
    """短 STATE 投影：只放当前目标指针、计数、阻塞与下一步，引用而非复制历史。"""
    taskbook, work_items, acceptance, changes = _snapshot_parts(snapshot)
    adopted = [cid for cid, record in sorted(changes.items()) if is_adopted_requirement(record)]
    open_items = [item for item in work_items
                  if isinstance(item, dict) and item.get("status") not in STATUS_DONE]
    blocked = [item for item in open_items if item.get("status") == "blocked"]
    lines = [
        "# 当前状态（程序投影，引用型）",
        f"采用修订：{len(adopted)} 条变更纳入；验收项 {len([a for a in acceptance if isinstance(a, dict)])} 项",
        f"工作项：共 {len(work_items)}，未完成 {len(open_items)}，阻塞 {len(blocked)}",
        "## 未完成（前 %d 项）" % limit,
    ]
    for item in open_items[:limit]:
        lines.append(f"- {item.get('id')} [{item.get('status')}] 负责人={item.get('owner') or '未指定'}"
                     f" 验收={','.join(str(a) for a in (item.get('acceptance') or [])) or '未指定'}")
    if blocked:
        lines.append("## 阻塞")
        for item in blocked[:limit]:
            lines.append(f"- {item.get('id')}：{str(item.get('blocker') or '未填写原因')[:120]}")
    lines.append("历史与证据见任务书、LOG 与各产物 manifest；本投影不复制长历史。")
    text = "\n".join(lines)
    if len(text) > MAX_STATE_CHARS:
        text = text[:MAX_STATE_CHARS] + "\n…（截断，完整内容见引用文件）"
    return text


def handoff_package(snapshot: dict, *, next_action: str) -> str:
    """给新聊天/新成员的接手包：目标指针、当前项、阻塞、下一步与证据引用。"""
    taskbook, work_items, acceptance, changes = _snapshot_parts(snapshot)
    revision = snapshot.get("adopted_revision") or {}
    open_items = [item for item in work_items
                  if isinstance(item, dict) and item.get("status") not in STATUS_DONE]
    lines = [
        "# 接手包（引用型，不复制长历史）",
        f"项目：{snapshot.get('workspace') or '（见 AGENTS.md 与 current.json）'}",
        f"采用计划：{revision.get('revision_id') or 'unknown'} / {revision.get('content_sha256') or 'unknown'}",
        f"已采用变更：{len([c for c, r in changes.items() if is_adopted_requirement(r)])}",
        "## 当前未完成",
    ]
    for item in open_items[:12]:
        lines.append(f"- {item.get('id')} [{item.get('status')}] {str(item.get('title') or '')[:80]}"
                     f" → 产物 {(item.get('artifacts') or ['未指定'])[0]}")
    lines.append(f"## 下一步\n{_text(next_action, label='next_action', limit=600)}")
    lines.append("## 证据入口\n任务书 / 各产物 manifest / STATE 投影；未验证项须显式标注。")
    text = "\n".join(lines)
    if len(text) > MAX_HANDOFF_CHARS:
        text = text[:MAX_HANDOFF_CHARS] + "\n…（截断）"
    return text
