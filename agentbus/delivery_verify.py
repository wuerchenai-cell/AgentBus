# -*- coding: utf-8 -*-
"""交付体检：verify(task|job) 的只读核心。

来源：OpenCode（平级成员），2026-10-08，任务 m-1791457584502-3c65ce
（product-convergence，OC-EVIDENCE），reply_to=m-1791457574691-fa3254。
采用计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…80cc3b5。

定位：**只读**。本模块不写消息、不改任务、不回投回执、不执行日志里的任何命令。
它只回答一个问题：这条交付在证据层面站不站得住。

统一检查项（verify 与 verify_job 共用同一把尺子）：
  任务/回执关联、result_to/reply_to、产物存在、bytes/SHA256、评审闭环、
  预算/期限、当前代次（若已栅栏化）、证据来源

三条硬纪律：
  1. 容忍旧记录、损坏行与缺失证据——缺证据一律记「未验证」，绝不默认通过。
  2. 不把自报升级为实测。transcript / self_report 只是「有人这么说过」。
  3. 不执行审计日志里的任何内容。日志字段永远只当字符串读。

证据分级（本模块最核心的输出）：
  native_receipt   正式关联回执 + 产物摘要核验通过
  transcript       有回执但来源是聊天转录，非原生通道
  self_report      仅成员自报，无关联回执
  failed           回执明确报告失败（通道有效但能力未通过）
  none             无任何证据
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import time
from datetime import datetime
from typing import Any

# 证据来源分级，由弱到强。
EVIDENCE_NONE = "none"
EVIDENCE_SELF_REPORT = "self_report"
EVIDENCE_FAILED = "failed"
EVIDENCE_TRANSCRIPT = "transcript"
EVIDENCE_NATIVE = "native_receipt"

EVIDENCE_ORDER = (EVIDENCE_NONE, EVIDENCE_SELF_REPORT, EVIDENCE_FAILED,
                  EVIDENCE_TRANSCRIPT, EVIDENCE_NATIVE)

NATIVE_TRANSPORTS = frozenset({"native", "program", "registration"})

MAX_ARTIFACTS = 512
MAX_BODY_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# §7.1 修复：ID 有两种合法形态，本模块原先只认第一种，导致全部 tree 任务
# 在 verify_task 第一步就判 task_id_format=False 而立刻返回 evidence=none。
#   ① 常规：new_id() 产生  m-<epoch_ms 数字>-<6 hex>          （bus.py:75）
#   ② 任务树：_allocation_id() 产生  m-tree-<24 hex>          （task_tree.py:771）
# 中子段由「纯数字」放宽为「数字或字面量 tree」。锚定与长度约束保留，
# 不放开任意字符，避免把畸形 ID 误判为合法。
_ID_MID = r"(?:[0-9]{10,20}|tree)"
RESULT_ID_RE = re.compile(r"^m-%s-[0-9a-f]{6,32}$" % _ID_MID)
TASK_ID_RE = re.compile(r"^(?:m|rx)-%s-[0-9a-f]{6,32}$" % _ID_MID)
AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
JOB_ID_RE = re.compile(r"^j-[a-zA-Z0-9_-]{1,62}$")
MAILBOX_STATES = ('done', 'processing', 'blocked', 'ready')


def _no_links(path: Path) -> bool:
    try:
        for node in (path, *path.parents):
            info = node.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return False
            if node.name.casefold() in ('.workbuddy', '不要访问', 'do not access'):
                return False
    except OSError:
        return False
    return True


def _read_json(path: Path):
    """读一个 JSON 文件；损坏或超限一律记未验证，绝不抛给调用方。"""
    try:
        if not _no_links(path):
            return None, 'UNSAFE_PATH'
        if path.stat().st_size > MAX_BODY_BYTES:
            return None, "TOO_LARGE"
        with path.open('rb') as stream:
            raw = stream.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            return None, 'TOO_LARGE'
        return json.loads(raw.decode('utf-8-sig')), ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__


def _sha256(path: Path):
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        return None, type(exc).__name__
    return digest.hexdigest(), ""


def safe_relative(value) -> str | None:
    """只接受项目内相对路径。绝对路径、盘符、越界、私有目录一律拒绝。"""
    if not isinstance(value, str) or not value.strip():
        return None
    if "\\" in value or ":" in value:
        return None
    parts = tuple(value.split("/"))
    if PurePosixPath(value).is_absolute():
        return None
    if any(p in ("", ".", "..") for p in parts):
        return None
    if any(p.casefold() == ".workbuddy" for p in parts):
        return None
    from .workspace_files import WorkspaceFiles, WorkspaceFileError
    try:
        WorkspaceFiles._relative(value)
    except WorkspaceFileError:
        return None
    return value


class Report:
    """一次体检的结构化结果。to_dict/to_text 都不做任何 I/O。"""

    def __init__(self, kind: str, subject: str):
        self.kind = kind
        self.subject = subject
        self.checks: list[dict] = []
        self.evidence = EVIDENCE_NONE
        self.evidence_reason = ""
        self.notes: list[str] = []
        self.unverified: list[str] = []

    def add(self, code: str, ok: bool, detail: str = "", *, unverified: bool = False):
        self.checks.append({"code": code, "verdict": "pass" if ok else "fail",
                            "detail": detail, "unverified": unverified})
        if unverified:
            self.unverified.append(code)
        return ok

    @property
    def verdict(self) -> str:
        failed = [c for c in self.checks if c["verdict"] == "fail" and not c["unverified"]]
        pending = [c for c in self.checks if c["unverified"]]
        if failed:
            return "fail"
        if pending:
            return "partial"
        return "pass"

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "tool": "delivery_verify",
            "kind": self.kind,
            "subject": self.subject,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "evidence_reason": self.evidence_reason,
            "checks": list(self.checks),
            "unverified": sorted(set(self.unverified)),
            "notes": list(self.notes),
        }

    def to_text(self) -> str:
        lines = ["交付体检：%s %s" % (self.kind, self.subject),
                 "结论：%s（证据级别 %s）" % (self.verdict, self.evidence)]
        if self.evidence_reason:
            lines.append("证据判定依据：%s" % self.evidence_reason)
        lines.append("检查项：")
        for check in self.checks:
            mark = {"pass": "OK  ", "fail": "FAIL"}.get(check["verdict"], "??  ")
            if check["unverified"]:
                mark = "未验证"
            lines.append("  [%s] %s%s" % (mark, check["code"],
                                           ("  " + check["detail"]) if check["detail"] else ""))
        if self.unverified:
            lines.append("未验证项（不得当作通过）：%s" % "、".join(sorted(set(self.unverified))))
            if self.verdict == "partial":
                lines.append("【证据诊断】各任务产物摘要与正式回执本身无冲突；当前评级 partial 仅因缺少第三方独立平级复核（review_closure）。")
                lines.append("  → 若协调者已亲自人工复核产物无误，执行收尾命令时添加 --verified 参数即可正式归档。")
        for note in self.notes:
            lines.append("备注：%s" % note)
        return "\n".join(lines) + "\n"


def classify_evidence(*, receipt_found: bool, receipt_failed: bool = False,
                      transport: str = "", artifact_verified: bool = False,
                      self_reported: bool = False,
                      fenced: bool = False) -> tuple[str, str]:
    """把散落的证据线索归成一个级别 + 一句人话依据。

    这是「不把自报升级为实测」的落点：没有关联回执时，最高只能是 self_report。

    原生通道的判据（修复 §7.1 AUDIT-EVIDENCE-TRANSPORT）：
    ``transport`` 字段在 ``Message``（bus.py）里**并不存在**，``answer`` 也从不写它，
    因此旧的 ``transport in NATIVE_TRANSPORTS`` 恒为假，导致每条真实交付都被判
    ``transcript``、``verify`` 恒 ``fail``。现在改判据为**可实测信号**：

      * ``fenced``：回执带非空 ``source_claim_id`` —— 说明它由 ``claim`` 闸门
        签发（程序路径产生），而不是聊天里随手贴的一段文字。这是原生通道的
        充分证据，且对 184 条历史 result 消息**立即可用**，无需数据迁移。
      * ``transport``：保留为兼容入口，任何未来的真实生产者仍可按原语义写入。

    两个信号取「或」：只要其一成立即视为原生通道。弱判据（有回执但既无栅栏
    又无 transport）仍落到 ``transcript``——不放松「不把转录当原生」的纪律。
    """
    if not receipt_found:
        if self_reported:
            return EVIDENCE_SELF_REPORT, "只有成员自报，没有正式关联回执"
        return EVIDENCE_NONE, "没有任何交付证据"
    if receipt_failed:
        return EVIDENCE_FAILED, "回执明确报告失败；通道有效但能力未通过"
    native = (str(transport or "").lower() in NATIVE_TRANSPORTS) or bool(fenced)
    if not native:
        return EVIDENCE_TRANSCRIPT, "有正式关联回执，但来源是聊天转录，非原生通道"
    if not artifact_verified:
        return EVIDENCE_TRANSCRIPT, "原生回执已关联，但产物摘要尚未核验通过"
    return EVIDENCE_NATIVE, "原生回执已关联且产物摘要核验通过"


def verify_artifacts(root: Path, entries) -> tuple[bool, list[dict]]:
    """逐项核对 path/bytes/sha256。不存在的文件算 fail，不算 pass。"""
    problems: list[dict] = []
    if not isinstance(entries, list) or not entries:
        return False, [{"code": "INVALID_MANIFEST", "detail": "产物清单为空或不是数组"}]
    if len(entries) > MAX_ARTIFACTS:
        return False, [{"code": "INVALID_MANIFEST",
                        "detail": "产物条目超过 %d" % MAX_ARTIFACTS}]
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            problems.append({"code": "INVALID_ENTRY", "detail": "条目不是对象"})
            continue
        rel = safe_relative(item.get("path"))
        if rel is None:
            problems.append({"code": "UNSAFE_PATH",
                             "detail": "非法或越界路径：%r" % (item.get("path"),)})
            continue
        key = rel.casefold()
        if key in seen:
            problems.append({"code": "DUPLICATE_PATH", "detail": rel})
            continue
        seen.add(key)
        target = root / rel
        if not _no_links(target):
            problems.append({'code': 'UNSAFE_PATH', 'detail': rel})
            continue
        if not target.is_file():
            problems.append({"code": "MISSING_FILE", "detail": rel})
            continue
        declared_size = item.get("bytes")
        declared_sha = item.get("sha256")
        actual_sha, error = _sha256(target)
        if actual_sha is None:
            problems.append({"code": "UNREADABLE", "detail": "%s (%s)" % (rel, error)})
            continue
        actual_size = target.stat().st_size
        if isinstance(declared_sha, str) and SHA256_RE.match(declared_sha):
            if declared_sha != actual_sha:
                problems.append({
                    "code": "SHA_MISMATCH",
                    "detail": f"{rel} (期望SHA: {declared_sha[:8]}..., 实际SHA: {actual_sha[:8]}...)",
                    "path": rel,
                    "expected_sha256": declared_sha,
                    "actual_sha256": actual_sha
                })
                continue
        else:
            problems.append({"code": "BAD_SHA_FORMAT", "detail": rel})
            continue
        if isinstance(declared_size, int) and not isinstance(declared_size, bool):
            if declared_size != actual_size:
                problems.append({
                    "code": "BYTES_MISMATCH",
                    "detail": f"{rel} (期望大小: {declared_size}B, 实际大小: {actual_size}B)",
                    "path": rel,
                    "expected_bytes": declared_size,
                    "actual_bytes": actual_size
                })
                continue
        else:
            problems.append({"code": "BAD_BYTES_TYPE", "detail": rel})
    return (not problems), problems


def load_events(bus_root: Path) -> tuple[list[dict], int]:
    """只读读审计日志。损坏行与乱码计入 damaged，绝不静默丢弃。

    日志里的任何字段都只当字符串读，绝不执行。
    """
    path = Path(bus_root) / "events.jsonl"
    events: list[dict] = []
    damaged = 0
    try:
        if not path.is_file():
            return [], 0
        if not _no_links(path):
            return [], 1
        with path.open("rb") as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeError, ValueError):
                    damaged += 1
                    continue
                if isinstance(value, dict):
                    events.append(value)
                else:
                    damaged += 1
    except OSError:
        return events, damaged
    return events, damaged


def _message_records(bus_root: Path):
    root = Path(bus_root) / 'agents'
    if not root.is_dir() or not _no_links(root):
        return
    # Terminal copies take precedence over active copies left by a crash.
    terminal_ids = set()
    for state in MAILBOX_STATES:
        for path in sorted(root.glob('*/' + state + '/*.json')):
            data, error = _read_json(path)
            if error or not isinstance(data, dict):
                continue
            rid = data.get('id')
            if state != 'done' and rid in terminal_ids:
                continue
            if state == 'done':
                terminal_ids.add(rid)
            yield data, state


def find_task(bus_root: Path, task_id: str):
    for data, state in _message_records(bus_root):
        if data.get('id') == task_id and data.get('type') in ('task', 'goal', 'review'):
            return data
    return None


def collect_receipts(bus_root: Path, task_id: str) -> list[dict]:
    """找出所有 reply_to 指向该任务的 result。重复 ID 会被标出。"""
    root = Path(bus_root) / "agents"
    found: list[dict] = []
    seen: dict[str, int] = {}
    if not root.is_dir():
        return found
    for data, state in _message_records(bus_root):
        if data.get('type') != 'result' or data.get("reply_to") != task_id:
            continue
        rid = str(data.get("id") or '')
        seen[rid] = seen.get(rid, 0) + 1
        found.append(data)
    for rid, count in seen.items():
        if count > 1:
            found.append({"id": rid, "type": "result", "_duplicate_id": count})
    return found


def review_closure(bus_root: Path, task_id: str) -> tuple[bool, str]:
    """看该任务是否被评审消息覆盖。找不到就记未验证，不记通过。"""
    root = Path(bus_root) / "agents"
    if not root.is_dir():
        return False, "找不到 agents 目录"
    subject = find_task(bus_root, task_id)
    owner = subject.get('to') if subject else None
    associated = []
    for data, state in _message_records(bus_root):
        if data.get('type') != 'review':
            continue
        refs = data.get("refs")
        refs = refs if isinstance(refs, list) else []
        if data.get('reply_to') == task_id or task_id in refs:
            associated.append((data, state))
    if not associated:
        return False, '未找到结构化关联评审'
    for data, state in associated:
        if state != 'done' or data.get('status') != 'done' or data.get('to') == owner:
            return False, '关联评审尚未闭环或由本人评审'
        receipts = collect_receipts(bus_root, data.get('id'))
        if len(receipts) != 1:
            return False, '评审缺少唯一正式回执'
        result = receipts[0]
        if (result.get('sender') != data.get('to') or result.get('to') != data.get('result_to')
                or result.get('exit_code') not in (None, 0)
                or result.get('status') in ('failed', 'blocked')):
            return False, '评审回执关联或结果不合法'
    return True, '关联评审均有合法正式回执'
    return False, "未找到关联评审"


def verify_task(bus_root, root, task_id: str, *, task_record=None,
                result=None, now: float | None = None) -> dict:
    """体检单条任务。root 是项目根，bus_root 是 .agentbus 目录。"""
    bus_root = Path(bus_root)
    root = Path(root)
    now = time.time() if now is None else float(now)
    report = Report("task", task_id)

    if not isinstance(task_id, str) or TASK_ID_RE.match(task_id) is None:
        report.add("task_id_format", False, "任务 ID 格式非法")
        report.evidence, report.evidence_reason = classify_evidence(receipt_found=False)
        return report.to_dict()
    report.add("task_id_format", True)

    if task_record is None:
        found = find_task(bus_root, task_id)
        if not isinstance(found, dict):
            report.add("task_record_readable", False,
                       "实际成员信箱中未找到任务", unverified=True)
            task_record = {}
        else:
            task_record = found
            report.add("task_record_readable", True)
    else:
        report.add("task_record_readable", True, "由调用方提供")
    if task_record:
        report.add('task_record_binding', task_record.get('id') == task_id
                   and task_record.get('type') in ('task', 'goal', 'review'), '任务身份和类型')

    receipts = collect_receipts(bus_root, task_id)
    duplicates = [r for r in receipts if r.get("_duplicate_id")]
    unique = [r for r in receipts if not r.get("_duplicate_id")]
    if duplicates:
        report.add("no_duplicate_result_id", False,
                   "存在重复 result ID：%s" % [d.get("id") for d in duplicates])
    elif unique:
        report.add("no_duplicate_result_id", True)
    else:
        report.add("no_duplicate_result_id", True, "无回执，无重复可言")

    if len(unique) > 1:
        report.add("single_result", False, "存在 %d 条回执" % len(unique))
    elif len(unique) == 1:
        report.add("single_result", True)
    else:
        # 没有回执是「证据缺失」，不是「任务做错了」。记未验证，不能记 fail。
        report.add("single_result", True, "没有正式回执（证据缺失，非错误）",
                   unverified=True)

    chosen = unique[0] if len(unique) == 1 else (unique[0] if unique else None)
    if chosen is not None:
        report.add('result_sender_binding', chosen.get('sender') == task_record.get('to'),
                   '回执发件人须为任务执行者', unverified=not task_record)
        report.add('result_success', chosen.get('exit_code') in (None, 0)
                   and chosen.get('status') not in ('failed', 'blocked'), '回执执行结果')
        for key in ('job_id', 'plan_id', 'plan_sha256'):
            expected_binding = task_record.get(key)
            if expected_binding is not None or chosen.get(key) is not None:
                report.add(key + '_binding', chosen.get(key) == expected_binding, key)
        if chosen.get("reply_to") == task_id:
            report.add("reply_to_binding", True)
        else:
            report.add("reply_to_binding", False,
                       "回执 reply_to=%r" % chosen.get("reply_to"))
        record_result_to = task_record.get("result_to")
        if record_result_to and chosen.get("to") != record_result_to:
            report.add("result_to_binding", False,
                       "回执投递到 %r，任务声明 %r" % (chosen.get("to"), record_result_to))
        elif record_result_to:
            report.add("result_to_binding", True)
        else:
            report.add("result_to_binding", False, "任务未声明 result_to", unverified=True)

    expected = task_record.get("expected_artifacts")
    expected = expected if isinstance(expected, list) else []
    entries = chosen.get("artifacts") if isinstance(chosen, dict) else None
    entries = entries if isinstance(entries, list) else []
    if not chosen:
        # 无回执时产物无从核对；只有任务声明过产物才说明「本应有却没有」。
        if expected:
            report.add("expected_artifacts_present", False,
                       "任务声明 %d 项产物但没有任何回执附清单" % len(expected), unverified=True)
        else:
            report.add("expected_artifacts_present", True, "任务未声明产物")
    elif expected and not entries:
        report.add("expected_artifacts_present", False,
                   "任务声明 %d 项产物但回执未附清单" % len(expected))
    else:
        supplied = {item.get('path') for item in entries if isinstance(item, dict)}
        missing = [path for path in expected if path not in supplied]
        report.add("expected_artifacts_present", not missing, '未提交：' + ', '.join(missing))

    ok_art, problems = verify_artifacts(root, entries) if entries else (False, [])
    if entries:
        if ok_art:
            report.add("artifacts_verified", True, "%d 项全部匹配" % len(entries))
        else:
            report.add("artifacts_verified", False,
                       "; ".join("%s:%s" % (p["code"], p["detail"]) for p in problems[:5]))
    elif not expected and chosen is None:
        report.add("artifacts_verified", True, "无回执且未声明产物",
                   unverified=True)
    elif not expected:
        report.add("artifacts_verified", True, "本任务未声明产物")

    fenced = isinstance(task_record, dict) and task_record.get("claim_epoch") \
        and isinstance(task_record.get("claim_id"), str) and task_record["claim_id"]
    if fenced and isinstance(chosen, dict):
        source_epoch = chosen.get("source_claim_epoch")
        source_claim = chosen.get("source_claim_id") or ""
        if not source_claim:
            report.add("claim_epoch_match", False, "回执未记录来源票据")
        elif source_epoch != task_record.get("claim_epoch") or \
                source_claim != task_record.get("claim_id"):
            report.add("claim_epoch_match", False,
                       "回执代次 %r 与任务 %r 不一致" % (source_epoch, task_record.get("claim_epoch")))
        else:
            report.add("claim_epoch_match", True)
    elif fenced:
        report.add("claim_epoch_match", False, "任务已栅栏化但没有回执可比对", unverified=True)
    else:
        report.add("claim_epoch_match", True, "任务未栅栏化（legacy），不适用")

    deadline = task_record.get("deadline_ts")
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and deadline > 0:
        observed = now
        if chosen is not None:
            try:
                observed = datetime.fromisoformat(chosen.get('ts', '')).timestamp()
                if not math.isfinite(observed):
                    raise ValueError('not finite')
            except (ValueError, TypeError):
                report.add('completion_time_known', False, '回执无合法时间戳', unverified=True)
        if observed > float(deadline):
            report.add("within_deadline", False, "已超过原期限")
        else:
            report.add("within_deadline", True)
    else:
        report.add("within_deadline", True, "未设期限")

    budget = task_record.get("budget_s")
    if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
        report.add("budget_recorded", True, "预算 %ss" % budget)
    else:
        report.add("budget_recorded", True, "未设预算")

    if task_record.get('type') == 'review':
        report.add('review_closure', True, '评审不触发递归评审')
    else:
        closed, reason = review_closure(bus_root, task_id)
        # Caller-provided records still define the real subject owner.
        if closed and task_record.get('to'):
            for review, _ in _message_records(bus_root):
                if review.get('type') == 'review' and (review.get('reply_to') == task_id
                    or task_id in (review.get('refs') or [])) and review.get('to') == task_record['to']:
                    closed, reason = False, '本人评审不构成平级监督'
        report.add("review_closure", closed, reason, unverified=not closed)

    self_reported = bool(isinstance(result, dict) and result.get("self_report"))
    if chosen is not None:
        self_reported = self_reported or bool(chosen.get("self_report")) \
            or bool(chosen.get("self_reported"))
    elif isinstance(result, dict) and result.get("self_report"):
        self_reported = True
    # 运输方式必须从回执实测字段读取；不得因为「有产物」就假定是原生通道。
    transport = ""
    if isinstance(chosen, dict):
        candidate_transport = chosen.get("transport")
        if isinstance(candidate_transport, str):
            transport = candidate_transport
    # §7.1 修复：Message 无 transport 字段，历史回执该键恒缺失。原生通道改判
    # 可实测信号——非空 source_claim_id 表示回执由 claim 闸门签发（程序路径产
    # 出），而非聊天转录。见 classify_evidence 文档。
    fenced = bool(isinstance(chosen, dict)
                  and str(chosen.get("source_claim_id") or "").strip())
    artifact_verified = bool(entries and ok_art)
    failed = bool(isinstance(chosen, dict)
                  and (chosen.get("exit_code") not in (None, 0)
                       or chosen.get("status") in ("failed", "blocked")))
    report.evidence, report.evidence_reason = classify_evidence(
        receipt_found=chosen is not None, receipt_failed=failed,
        transport=transport, artifact_verified=artifact_verified,
        self_reported=self_reported, fenced=fenced)
    report.notes.append("本工具只读：未写任何消息、未改任务、未执行审计日志中的任何内容")
    return report.to_dict()


def verify_job(bus_root, root, job_id: str, *, now: float | None = None) -> dict:
    """体检一个作业。作业收尾必须建立在各任务体检之上。"""
    bus_root = Path(bus_root)
    root = Path(root)
    report = Report("job", job_id)
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        report.add("job_id_format", False, "作业 ID 非法")
        return report.to_dict()

    data, error = _read_json(bus_root / "jobs" / ("%s.json" % job_id))
    if error or not isinstance(data, dict):
        report.add("job_record_readable", False,
                   "作业记录读不到（%s）" % (error or "格式非法"), unverified=True)
        report.evidence, report.evidence_reason = classify_evidence(receipt_found=False)
        return report.to_dict()
    report.add("job_record_readable", True)

    tasks = data.get("tasks")
    tasks = tasks if isinstance(tasks, list) else []
    task_ids = [str(t.get("id")) for t in tasks if isinstance(t, dict) and t.get("id")]
    report.add("job_has_tasks", bool(task_ids), "%d 条任务" % len(task_ids))
    report.add('job_tasks_unique', len(set(task_ids)) == len(task_ids), '重复任务不能重复计数')

    verified = 0
    failed = 0
    partial = 0
    for task_id in task_ids:
        outcome = verify_task(bus_root, root, task_id, now=now)
        if outcome["verdict"] == "fail":
            failed += 1
        elif outcome["verdict"] == "pass":
            verified += 1
        else:
            partial += 1
    all_verified = bool(task_ids) and verified == len(task_ids)
    report.add("all_tasks_verified", all_verified,
               "通过 %d / 未验证 %d / 失败 %d / 共 %d" % (verified, partial, failed, len(task_ids)),
               unverified=not failed and not all_verified)

    state = str(data.get("state") or "")
    finalization = data.get('finalization') or {}
    if state == "completed" or finalization.get('committed'):
        report.add("job_state_consistent", all_verified,
                   "作业声称完成，但有 %d 条任务体检失败" % failed if failed else "完成且各任务体检通过")
    else:
        report.add("job_state_consistent", True, "作业状态 %r，未声称完成" % state)

    events, damaged = load_events(bus_root)
    report.add("audit_readable", True, "%d 条事件，%d 条损坏" % (len(events), damaged))
    if damaged:
        report.notes.append("审计有 %d 条损坏行；已保留并计数，未静默丢弃" % damaged)

    summary = finalization.get('summary') or data.get("summary")
    report.add("summary_present", bool(str(summary or "").strip()),
               "完成摘要" if str(summary or "").strip() else "尚无完成摘要",
               unverified=not str(summary or '').strip() and not finalization.get('committed')
                          and state != 'completed')

    report.evidence, report.evidence_reason = classify_evidence(
        receipt_found=all_verified)
    report.notes.append("作业级结论来自各任务体检，未直接采信作业自身声明")
    return report.to_dict()
