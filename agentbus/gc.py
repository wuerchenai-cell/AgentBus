# -*- coding: utf-8 -*-
"""磁盘回收：备份与分发产物的**保守**轮转策略。

来源：任务树 §7.4 AUDIT-DISK-BLOAT（P1），任务 m-tree-138de207fe29d952ce763d71。
实测：`成果/AgentBus.exe.backup-*` 与 `.prev-*` 共 19 份 / 2.97 GB，
`成果/agentbus/pilots/` 12.60 GB，另有 2 份 158 MB+ 分发包 ZIP。
全局检索 `backup|retention|prune|cleanup|keep_last` 在源码中**零命中**——
备份与归档全是人工行为，无轮转、无上限、无自动回收。

本模块补上那个缺口，但**不替用户做决定**：

三条硬纪律
----------
1. **默认只报告、不删除。** ``plan()`` 是纯函数，只算出「若执行会删哪些」；
   真正的删除只在 ``apply()`` 且显式传 ``confirmed=True`` 时发生。
2. **绝不触碰不可重建的东西。** 只处理「明确的备份/分发产物」——
   ``<name>.backup-*``、``<name>.prev-*``、以及分发包 ZIP。
   源码、`.agentbus/`、`资料/`、`成果/agentbus/pilots/` 一律**永不入选**。
3. **保留规则保守且可解释。** 默认「按名字分组，每组保留最新 N 份」；
   任何被文档（``*.md``）显式引用其文件名的备份，**一律保留**，
   避免删掉 README/HANDOFF 里正在指引用户的那个版本。

本模块**零 I/O 副作用**（除 ``apply()`` 的删除），便于单元测试与 dry-run。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# 只有这些形态的文件才可能成为回收候选。
# 组名 = 前缀（用于「每组保留最新 N 份」）。
_BACKUP_RE = re.compile(r"^(?P<stem>.+?)\.(?:backup|prev)-.+$", re.IGNORECASE)
# 历史封存旧版二进制：只在显式 --include-deprecated 时纳入。
_DEPRECATED_RE = re.compile(r"^(?P<stem>.+?)\.deprecated_old_version(?:[._-].*)?$", re.IGNORECASE)
# 分发包 ZIP：只在显式 --include-packages 时才纳入。
_PACKAGE_RE = re.compile(r"^(?:AgentBus-)?.+\.zip$", re.IGNORECASE)

# 永不进入候选的路径片段（大小写不敏感）。这是硬红线，不是偏好。
_FORBIDDEN_PARTS = frozenset({
    ".agentbus", ".workbuddy", "资料", "skills", "skill辅助",
})

DEFAULT_KEEP = 3


@dataclass
class Candidate:
    path: Path
    size: int
    group: str
    mtime: float
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "bytes": self.size,
            "group": self.group,
            "referenced": bool(self.reason),
            "reason": self.reason,
        }


@dataclass
class GcPlan:
    """一次回收的完整计划。``delete`` 为空或 ``dry_run`` 为真时不会动盘。"""

    delete: list[Candidate] = field(default_factory=list)
    keep: list[Candidate] = field(default_factory=list)
    scanned: int = 0
    scanned_bytes: int = 0

    @property
    def reclaim_bytes(self) -> int:
        return sum(c.size for c in self.delete)

    def to_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "scanned_bytes": self.scanned_bytes,
            "reclaim_bytes": self.reclaim_bytes,
            "delete": [c.to_dict() for c in self.delete],
            "keep": [c.to_dict() for c in self.keep],
        }


class GcError(RuntimeError):
    """回收参数或环境不合法。继承 RuntimeError 以便被 CLI 异常网接住。"""


def _is_forbidden(path: Path, root: Path) -> bool:
    """候选路径必须位于 root 内且不含任何硬红线片段。"""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return True
    return any(part.casefold() in _FORBIDDEN_PARTS for part in rel.parts)


def _referenced_names(root: Path, names: set[str]) -> set[str]:
    """扫描 root 下的 *.md，返回在正文里被提及的候选文件名。

    只做子串匹配，不解析 Markdown 结构——目的是「宁可多保留」，不是精确引用分析。
    读文件失败一律跳过，不影响判定。
    """
    if not names:
        return set()
    hit: set[str] = set()
    try:
        docs = list(root.glob("*.md")) + list(root.glob("*/*.md"))
    except OSError:
        return hit
    for doc in docs:
        if _is_forbidden(doc, root):
            continue
        try:
            if doc.stat().st_size > 4 * 1024 * 1024:
                continue
            text = doc.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name in names:
            if name in text:
                hit.add(name)
    return hit


def _split_backup(stem_name: str) -> str:
    """从文件名提取「组名」：剥掉 .backup-xxx / .prev-xxx 后缀。"""
    m = _BACKUP_RE.match(stem_name)
    return (m.group("stem") if m else stem_name).casefold()


def plan(root: Path, *, keep: int = DEFAULT_KEEP, include_packages: bool = False,
         include_deprecated: bool = False,
         directories: tuple[str, ...] = ("成果",)) -> GcPlan:
    """算出回收计划。**纯只读**，绝不删任何东西。

    root               项目根（用于路径安全校验与文档引用扫描）
    keep               每个组保留最新几份（>=1）
    include_packages   是否把分发包 ZIP 纳入候选（默认否）
    include_deprecated 是否把历史封存的 .deprecated_old_version 二进制纳入候选（默认否）
    directories        只在这些子目录里找候选（默认 `成果`）
    """
    root = Path(root)
    if keep < 1:
        raise GcError("keep 必须 >= 1；不提供「全删」语义")

    result = GcPlan()
    per_group: dict[str, list[Candidate]] = {}

    for sub in directories:
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if _is_forbidden(path, root):
                continue
            name = path.name
            is_backup = bool(_BACKUP_RE.match(name))
            is_deprecated = bool(_DEPRECATED_RE.match(name))
            is_package = bool(_PACKAGE_RE.match(name))
            if not (is_backup or (include_deprecated and is_deprecated) or (include_packages and is_package)):
                continue
            try:
                size = path.stat().st_size
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if is_deprecated and include_deprecated:
                group = "__deprecated_binaries__"
            elif is_backup:
                group = _split_backup(name)
            else:
                group = "__packages__"
            result.scanned += 1
            result.scanned_bytes += size
            per_group.setdefault(group, []).append(
                Candidate(path=path, size=size, group=group, mtime=mtime))

    referenced = _referenced_names(
        root, {c.path.name for group in per_group.values() for c in group})

    for group, items in per_group.items():
        items.sort(key=lambda c: c.mtime, reverse=True)
        for index, cand in enumerate(items):
            if cand.path.name in referenced:
                cand.reason = "被文档引用，保留"
                result.keep.append(cand)
            elif index < keep:
                cand.reason = "在保留窗口内（最新 %d 份）" % keep
                result.keep.append(cand)
            else:
                cand.reason = "超出保留窗口且未被文档引用"
                result.delete.append(cand)

    result.delete.sort(key=lambda c: c.mtime)
    return result


def apply(plan_obj: GcPlan, *, confirmed: bool = False) -> int:
    """执行计划。**必须 confirmed=True**，否则只抛错不删。

    返回实际删除的文件数。单个删除失败不中断其余（记录后继续），
    但任何路径被判定越界即立即中止——那是攻击或配置错误的信号。
    """
    if not confirmed:
        raise GcError("apply() 需要显式 confirmed=True；默认禁止删除")
    removed = 0
    for cand in plan_obj.delete:
        try:
            os.remove(cand.path)
            removed += 1
        except OSError as exc:
            raise GcError("删除失败：%s（%s）" % (cand.path, exc)) from exc
    return removed
