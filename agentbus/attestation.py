# -*- coding: utf-8 -*-
"""产物履历：生成不含私有内容的 artifact passport / 验收单。

来源：OpenCode（平级成员），2026-10-08，任务 m-1791457584502-3c65ce
（product-convergence，OC-EVIDENCE），reply_to=m-1791457574691-fa3254。
采用计划 r-cdef881858336918798c18bb8f096f25 / content_sha256 c739eb9b…80cc3b5。

定位：**只读**。本模块只读磁盘上的产物并计算摘要，不写任何文件、不上传、
不执行产物内容。它产出一张可以交给第三方的「验收单」。

隐私边界（硬编码，不可通过参数放宽）：
  允许进入 passport：成员 ID、显示名、任务/结果 ID、输入输出的相对路径与
    bytes/sha256、评审结论、机器可公开环境、时间、限制声明、可信度徽章。
  禁止进入 passport：凭据、令牌、私有记忆、聊天全文、用户内容正文、
    环境变量值、绝对用户路径、产物的文件正文。

  因此产物只以「相对路径 + 摘要」出现，绝不嵌入文件内容片段。
  摘要能证明字节相同，不能证明内容质量或作者身份——这一点写进限制声明。

可信度徽章由证据级别决定，不由自称决定：
  A  原生回执 + 摘要核验通过
  B  正式关联回执，但来源是聊天转录
  C  仅成员自报，无关联回执
  D  无任何证据 / 证据损坏
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
from typing import Any

MAX_ARTIFACTS = 512
MAX_PATH_CHARS = 240
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# 绝不进入 passport 的字段名（小写比较）。
FORBIDDEN_FIELDS = frozenset({
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "credential", "credentials", "auth", "authorization", "cookie",
    "env", "environ", "environment", "private", "private_note", "memory",
    "chat", "transcript", "user_content", "body_text", "content", "abs_path",
    "absolute_path", "home", "user_path",
})

# 可以原样进入 passport 的公开环境字段。
PUBLIC_ENV_KEYS = ("os_name", "os_release", "python_version", "machine",
                   "interpreter")

BADGE_BY_EVIDENCE = {
    "native_receipt": "A",
    "transcript": "B",
    "self_report": "C",
    "none": "D",
}

BADGE_MEANING = {
    "A": "原生回执已关联且产物摘要核验通过",
    "B": "有正式关联回执，但来源是聊天转录，非原生通道",
    "C": "仅成员自报，无正式关联回执",
    "D": "无任何证据，或证据存在但损坏/未验证",
}


class AttestationError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__("%s: %s" % (code, message) if message else code)


def sha256_of(path: Path):
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


def _relpath(root: Path, target: Path) -> str | None:
    """只允许项目内相对路径。绝对路径与越界一律拒绝。"""
    try:
        rel = target.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    text = rel.as_posix()
    if len(text) > MAX_PATH_CHARS:
        return None
    if any(part.casefold() == ".workbuddy" for part in text.split("/")):
        return None
    return text


def public_environment() -> dict:
    """只取可公开的运行环境信息。不读环境变量值。"""
    return {
        "os_name": platform.system(),
        "os_release": platform.release(),
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "machine": platform.machine() or "unknown",
        "interpreter": "frozen" if getattr(sys, "frozen", False) else "source",
    }


def _scrub_mapping(value: dict) -> dict:
    """过滤掉禁止字段。用于把评审/限制等自由字典塞进 passport 前的最后一道闸。"""
    out = {}
    for key in sorted(value):
        lowered = str(key).lower()
        if lowered in FORBIDDEN_FIELDS:
            continue
        item = value[key]
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[str(key)] = item
        elif isinstance(item, (list, tuple)):
            out[str(key)] = [v for v in item
                             if isinstance(v, (str, int, float, bool)) or v is None]
        # 嵌套字典一律丢弃：无法在不深查的情况下保证它不含私有内容。
    return out


def collect_artifacts(root, entries) -> list[dict]:
    """逐项记录产物的相对路径与摘要。不读取、不嵌入文件内容。"""
    root = Path(root)
    if not isinstance(entries, list):
        raise AttestationError("INVALID_MANIFEST", "产物清单必须是数组")
    if len(entries) > MAX_ARTIFACTS:
        raise AttestationError("TOO_MANY", "产物超过 %d 项" % MAX_ARTIFACTS)
    out = []
    for item in entries:
        if not isinstance(item, dict):
            raise AttestationError("INVALID_ENTRY", "产物条目必须是对象")
        raw = item.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise AttestationError("INVALID_PATH", "产物缺少 path")
        if "\\" in raw or ":" in raw or raw.startswith("/"):
            raise AttestationError("UNSAFE_PATH", "只接受项目内相对路径：%r" % raw)
        parts = tuple(raw.split("/"))
        if any(p in ("", ".", "..") for p in parts):
            raise AttestationError("UNSAFE_PATH", "路径含越界段：%r" % raw)
        if any(p.casefold() == ".workbuddy" for p in parts):
            raise AttestationError("PRIVATE_PATH", "不得纳入私有目录产物")
        target = root / raw
        # Check boundaries before even opening/hashing a file. A relative
        # spelling alone does not protect against a symlink or junction.
        from .delivery_verify import _no_links
        if target.exists() and (not _no_links(target) or _relpath(root, target) != raw):
            raise AttestationError('UNSAFE_PATH', 'artifact resolves outside its public root')
        record = {"path": raw, "declared_bytes": item.get("bytes"),
                  "declared_sha256": item.get("sha256"),
                  "actual_bytes": None, "actual_sha256": None, "verdict": "unverified"}
        if target.is_file():
            from .workspace_files import WorkspaceFiles, WorkspaceFileError
            try:
                actual = WorkspaceFiles(root).describe_artifact(raw)
                size, digest = actual['bytes'], actual['sha256']
            except (WorkspaceFileError, OSError):
                record["verdict"] = "unreadable"
            else:
                record["actual_bytes"] = size
                record["actual_sha256"] = digest
                declared_sha = item.get("sha256")
                declared_size = item.get("bytes")
                sha_ok = isinstance(declared_sha, str) and SHA256_RE.match(declared_sha) \
                    and declared_sha == digest
                size_ok = isinstance(declared_size, int) \
                    and not isinstance(declared_size, bool) and declared_size == size
                record["verdict"] = "match" if (sha_ok and size_ok) else "mismatch"
        out.append(record)
    return out


@dataclass
class Passport:
    """一张验收单。字段名固定，不接受任意扩展。"""

    author: str
    task_id: str
    result_id: str = ""
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    reviews: dict = field(default_factory=dict)
    evidence: str = "none"
    limitations: list = field(default_factory=list)
    observed_at: float = 0.0

    def to_dict(self) -> dict:
        badge = BADGE_BY_EVIDENCE.get(self.evidence, "D")
        artifacts_ok = all(item["verdict"] == "match" for item in self.outputs) \
            if self.outputs else False
        if badge == "A" and not artifacts_ok:
            badge = "D"
        return {
            "schema_version": 1,
            "tool": "attestation",
            "kind": "artifact_passport",
            "author": self.author,
            "task_id": self.task_id,
            "result_id": self.result_id,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "reviews": _scrub_mapping(self.reviews) if isinstance(self.reviews, dict) else {},
            "evidence": self.evidence,
            "badge": badge,
            "badge_meaning": BADGE_MEANING[badge],
            "environment": public_environment(),
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                         time.localtime(self.observed_at or time.time())),
            "limitations": list(self.limitations),
            "privacy": {
                "included": ["成员 ID", "任务与结果 ID", "产物相对路径",
                             "产物 bytes 与 sha256", "评审结论", "公开运行环境", "限制"],
                "excluded": ["凭据与令牌", "私有记忆", "聊天全文", "用户内容正文",
                             "环境变量值", "绝对用户路径", "产物文件正文"],
                "note": "产物只以相对路径与摘要出现；摘要证明字节相同，"
                        "不证明内容质量，也不证明作者身份。",
            },
        }


def attest(root, *, author: str, task_id: str, result_id: str = "",
           inputs=None, outputs=None, reviews=None, evidence: str = "none",
           limitations=None, observed_at: float | None = None) -> dict:
    """生成验收单。inputs/outputs 走摘要化，reviews 走禁字段过滤。"""
    root = Path(root)
    if not isinstance(author, str) or not author.strip():
        raise AttestationError("INVALID_AUTHOR", "必须提供成员 ID")
    if not isinstance(task_id, str) or not task_id.strip():
        raise AttestationError("INVALID_TASK", "必须提供任务 ID")
    if evidence not in BADGE_BY_EVIDENCE:
        raise AttestationError("INVALID_EVIDENCE",
                               "证据级别必须是 %s 之一" % sorted(BADGE_BY_EVIDENCE))
    passport = Passport(
        author=author.strip(),
        task_id=task_id.strip(),
        result_id=str(result_id or "").strip(),
        inputs=collect_artifacts(root, list(inputs or [])),
        outputs=collect_artifacts(root, list(outputs or [])),
        reviews=reviews if isinstance(reviews, dict) else {},
        evidence=evidence,
        limitations=[str(x) for x in (limitations or [])],
        observed_at=time.time() if observed_at is None else float(observed_at),
    )
    return passport.to_dict()


def to_markdown(passport: dict) -> str:
    """把验收单渲染成人可读单页。确定性输出。"""
    data = passport if isinstance(passport, dict) else {}
    lines = ["# 产物验收单（artifact passport）", ""]
    lines.append("- 成员：`%s`" % data.get("author", ""))
    lines.append("- 任务：`%s`" % data.get("task_id", ""))
    lines.append("- 结果：`%s`" % (data.get("result_id") or "（无）"))
    lines.append("- 可信度徽章：**%s** — %s" % (data.get("badge", "?"),
                                              data.get("badge_meaning", "")))
    lines.append("- 观测时间：%s" % data.get("observed_at", ""))
    lines.append("")
    lines.append("## 运行环境（可公开部分）")
    lines.append("")
    lines.append("| 字段 | 值 |")
    lines.append("|---|---|")
    for key, value in sorted((data.get("environment") or {}).items()):
        lines.append("| %s | %s |" % (key, value))
    lines.append("")
    for title, key in (("输入", "inputs"), ("输出", "outputs")):
        items = data.get(key) or []
        lines.append("## %s（%d 项）" % (title, len(items)))
        lines.append("")
        if not items:
            lines.append("（无）")
            lines.append("")
            continue
        lines.append("| 相对路径 | bytes | sha256 | 判定 |")
        lines.append("|---|---|---|---|")
        for item in items:
            lines.append("| `%s` | %s | `%s` | %s |" % (
                item.get("path", ""), item.get("actual_bytes"),
                (item.get("actual_sha256") or "")[:16], item.get("verdict", "")))
        lines.append("")
    reviews = data.get("reviews") or {}
    if reviews:
        lines.append("## 评审")
        lines.append("")
        for key in sorted(reviews):
            lines.append("- **%s**：%s" % (key, reviews[key]))
        lines.append("")
    limitations = data.get("limitations") or []
    if limitations:
        lines.append("## 限制声明")
        lines.append("")
        for item in limitations:
            lines.append("- %s" % item)
        lines.append("")
    privacy = data.get("privacy") or {}
    lines.append("## 隐私边界")
    lines.append("")
    lines.append("- 包含：%s" % "、".join(privacy.get("included") or []))
    lines.append("- 排除：%s" % "、".join(privacy.get("excluded") or []))
    if privacy.get("note"):
        lines.append("- 说明：%s" % privacy["note"])
    lines.append("")
    return "\n".join(lines)


def render(passport: dict) -> str:
    return to_markdown(passport)
