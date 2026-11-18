# -*- coding: utf-8 -*-
"""证据范围记录：每次结论必须写清"检查的是源码，还是正式程序"。

来源：§7.39 `PRODUCT-VS-SOURCE-EVIDENCE`（P1），依据 `成果/验收/20261011/
SYSTEM_REVIEW_TASK_REGISTER.md` 的 C3 映射——"每条判断注明所验源码/EXE 与入口，
修复后不得用绕开风险的路径验收"。

问题（同伴报告 + 只读核对确认）：历史上出现过
  - 用**落后的源树**去判断**正式 EXE** 的功能（把不同口径混用）；
  - 用**硬编码入口**绕开本应被检查的恢复逻辑；
且回执/裁定里"源码检查""候选包""正式 EXE"三者被互相代称，读者无法判断
一条结论到底验证了什么。

定位与边界（本模块**只读**）：
  - 不写文件、不加载模型、不读环境变量值、不访问凭据、不访问 `.workbuddy`。
  - 只做两件事：① 把"我检查了什么"结构化下来（EvidenceScope）；
    ② 校验这个声明**内部自洽**，并把不可信的代称判成错误。
  - 它**不能**证明结论本身正确——它只保证"来源口径写清楚、不自相矛盾"。

核心纪律：
  1. 源码检查 ≠ 候选包检查 ≠ 正式 EXE 检查；三者不得互相代称。
  2. 声称检查了正式 EXE，就必须给出**二进制路径 + sha256**，且解释器必须是
     冻结态（frozen）；声称检查了源码，就必须给出**源树相对路径 + 快照摘要**
     或明确的源树标识。
  3. 缺证据一律进 `unverified`，**不得**把"没查"写成"通过"。
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_TEXT = 400

# 检查对象的三种口径。禁止互相代称。
SUBJECT_SOURCE = "source_tree"        # 检查的是源码树（可能需要快照摘要）
SUBJECT_CANDIDATE_PACKAGE = "candidate_package"  # 候选发布包（未正式发布）
SUBJECT_FORMAL_EXE = "formal_exe"     # 登记在案的正式发布二进制

VALID_SUBJECTS = (SUBJECT_SOURCE, SUBJECT_CANDIDATE_PACKAGE, SUBJECT_FORMAL_EXE)

# 人类可读标签，用于渲染，避免各成员各写一套措辞。
SUBJECT_LABEL = {
    SUBJECT_SOURCE: "源码树（source tree）",
    SUBJECT_CANDIDATE_PACKAGE: "候选包（candidate package，尚未发布）",
    SUBJECT_FORMAL_EXE: "正式程序（registered formal EXE）",
}

# "互相代称"的判定：一句话里同时提到两种不同口径的**主体词**，却没有出现
# 明确的对比/区分标记，就判为疑似代称。这比脆弱的固定句式更稳，也不误伤
# "源码 ≠ 正式程序""对比源码与正式程序"这类正当叙述。
_SUBJECT_TOKENS = {
    "formal": (re.compile(r"正式\s*(?:程序|版|发布的?\s*包|EXE|exe|二进制)"),),
    "source": (re.compile(r"源码\s*(?:树)?"), re.compile(r"源\s*树")),
    "candidate": (re.compile(r"候选\s*(?:包|树|版本)"),),
}
# 出现这些标记即认为作者在**区分**（或明确点出代称问题）而非代称。
_CONTRAST_MARKERS = ("≠", "!=", "对比", "对照", "区分", "还是", "vs", "VS", "versus",
                     "不同", "分别", "不得混", "互不", "既非", "两者",
                     "混用", "代称", "互相")
_SENTENCE_SPLIT = re.compile(r"[。；\n;]")


class EvidenceScopeError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__("%s: %s" % (code, message) if message else code)


def _clean(value: Any, label: str, *, required: bool = False,
           max_len: int = MAX_TEXT) -> str:
    if value is None or value == "":
        if required:
            raise EvidenceScopeError("MISSING_FIELD", "%s 不能为空" % label)
        return ""
    if not isinstance(value, str):
        raise EvidenceScopeError("INVALID_FIELD", "%s 必须是文本" % label)
    text = " ".join(value.split())
    if required and not text:
        raise EvidenceScopeError("MISSING_FIELD", "%s 不能为空" % label)
    if len(text) > max_len:
        raise EvidenceScopeError("TOO_LONG", "%s 超过 %d 字符" % (label, max_len))
    return text


def sha256_file(path: Path) -> tuple[str | None, str]:
    """对单个文件算 sha256。返回 (hex|None, 错误名)。只读。"""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        return None, type(exc).__name__
    return digest.hexdigest(), ""


def hash_source_tree(root: Path, *, suffixes=(".py",), limit: int = 4096) -> str:
    """给出源码树的**内容快照摘要**（对排序后的相对路径+字节做累积哈希）。

    这是"源树快照摘要"，不是"包内某文件的摘要"。它让"我检查了哪个版本的源码"
    可被第三方逐字节复核。只读，不跟随越界符号链接。
    """
    root = Path(root)
    if limit <= 0:
        raise EvidenceScopeError("INVALID_LIMIT", "limit 必须为正")
    digest = hashlib.sha256()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in (".git", "__pycache__", ".workbuddy"))
        for name in sorted(filenames):
            if not name.endswith(suffixes):
                continue
            full = Path(dirpath) / name
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                continue
            blob, err = sha256_file(full)
            if err:
                raise EvidenceScopeError("UNREADABLE", "无法读取 %s：%s" % (rel, err))
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update((blob or "").encode("ascii"))
            digest.update(b"\n")
            count += 1
            if count > limit:
                raise EvidenceScopeError("TOO_MANY_FILES",
                                         "源码文件超过 %d 个" % limit)
    if count == 0:
        raise EvidenceScopeError("EMPTY_TREE", "源码树中没有匹配文件")
    return digest.hexdigest()


def detect_conflation(text: str) -> list[str]:
    """返回文本中"互相代称"的命中片段（空列表=未发现明显代称）。只读、纯函数。

    判定规则：在**同一句**（以 。；\\n; 切分）中同时出现两种不同口径的主体词，
    且**该句及其所在整行**都没有对比/区分标记时，判为疑似代称——把命中的
    主体词对作为片段返回。用整行兜底是为了容纳"…三者被互相代称"这类把
    说明标记放在后半句的写法。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    hits = []
    for line in text.splitlines() or [text]:
        line_has_marker = any(marker in line for marker in _CONTRAST_MARKERS)
        for sentence in _SENTENCE_SPLIT.split(line):
            if not sentence.strip():
                continue
            present = []
            for kind, patterns in _SUBJECT_TOKENS.items():
                for pattern in patterns:
                    match = pattern.search(sentence)
                    if match:
                        present.append((kind, match.group(0)))
                        break
            kinds = {kind for kind, _ in present}
            if len(kinds) < 2:
                continue
            if line_has_marker or any(m in sentence for m in _CONTRAST_MARKERS):
                continue
            hits.append("+".join(word for _, word in present))
    return hits


@dataclass
class EvidenceScope:
    """一条结论"检查了什么"的结构化声明。字段固定，不接受任意扩展。"""

    subject: str                      # VALID_SUBJECTS 之一
    entry_point: str                  # 走了哪个入口（CLI 子命令 / 桌面按钮 / 函数名）
    # 正式 EXE 口径必填：
    binary_path: str = ""
    binary_sha256: str = ""
    # 源码 / 候选包口径必填：
    source_path: str = ""
    source_snapshot_sha256: str = ""
    # 环境与未验证项：
    environment: dict = field(default_factory=dict)
    unverified: list = field(default_factory=list)
    note: str = ""

    def validate(self) -> None:
        if self.subject not in VALID_SUBJECTS:
            raise EvidenceScopeError(
                "INVALID_SUBJECT",
                "subject 必须是 %s 之一" % sorted(VALID_SUBJECTS))
        if not self.entry_point:
            raise EvidenceScopeError("MISSING_ENTRY",
                                     "必须写明所走入口（禁止'已复现'这类无入口声明）")
        if self.subject == SUBJECT_FORMAL_EXE:
            if not self.binary_path:
                raise EvidenceScopeError("MISSING_BINARY",
                                         "声称检查正式 EXE，必须给出二进制路径")
            if not SHA256_RE.match(self.binary_sha256 or ""):
                raise EvidenceScopeError("MISSING_BINARY_SHA",
                                         "声称检查正式 EXE，必须给出 64 位二进制 sha256")
            interpreter = str((self.environment or {}).get("interpreter", ""))
            if interpreter != "frozen":
                raise EvidenceScopeError(
                    "SOURCE_MASQUERADING_AS_BINARY",
                    "声称检查正式 EXE，但解释器=%r 非 frozen——疑似把源码检查"
                    "写成了正式程序检查" % (interpreter or "unknown"))
        else:  # source_tree / candidate_package
            if not self.source_path:
                raise EvidenceScopeError("MISSING_SOURCE",
                                         "必须给出源码/候选包的相对路径")
            if not SHA256_RE.match(self.source_snapshot_sha256 or ""):
                raise EvidenceScopeError(
                    "MISSING_SOURCE_SHA",
                    "必须给出源树/候选包快照摘要（64 位 sha256），"
                    "否则无法证明检查的是哪个版本")

    def to_dict(self) -> dict:
        self.validate()
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "evidence_scope",
            "subject": self.subject,
            "subject_label": SUBJECT_LABEL[self.subject],
            "entry_point": self.entry_point,
            "binary": ({"path": self.binary_path, "sha256": self.binary_sha256}
                       if self.binary_path else None),
            "source": ({"path": self.source_path,
                        "snapshot_sha256": self.source_snapshot_sha256}
                       if self.source_path else None),
            "environment": dict(self.environment or {}),
            "unverified": sorted(set(str(x) for x in (self.unverified or []))),
            "note": self.note,
        }


def build_scope(*, subject: str, entry_point: str, binary_path: str = "",
                binary_sha256: str = "", source_path: str = "",
                source_snapshot_sha256: str = "", environment: Mapping | None = None,
                unverified=None, note: str = "") -> dict:
    """构造并校验一条证据范围。校验不过即抛错，绝不返回半成品。"""
    if not isinstance(entry_point, str) or not entry_point.strip():
        raise EvidenceScopeError(
            "MISSING_ENTRY", "必须写明所走入口（禁止'已复现'这类无入口声明）")
    scope = EvidenceScope(
        subject=_clean(subject, "subject", required=True, max_len=32),
        entry_point=_clean(entry_point, "entry_point", required=True),
        binary_path=_clean(binary_path, "binary_path"),
        binary_sha256=_clean(binary_sha256, "binary_sha256", max_len=64),
        source_path=_clean(source_path, "source_path"),
        source_snapshot_sha256=_clean(source_snapshot_sha256,
                                      "source_snapshot_sha256", max_len=64),
        environment={str(k): str(v) for k, v in dict(environment or {}).items()},
        unverified=list(unverified or []),
        note=_clean(note, "note"),
    )
    return scope.to_dict()


def formal_exe_scope(root: Path, *, exe_relpath: str, entry_point: str,
                     registered_sha256: str = "", environment=None,
                     unverified=None) -> dict:
    """针对"检查正式程序"的便捷构造：现场读取 EXE 字节并核对登记摘要。

    若登记摘要非空且与实测不符，判 `REGISTERED_SHA_MISMATCH`——这正是
    "用错版本/落后包下结论"的拦截点。
    """
    exe = Path(root) / exe_relpath
    digest, err = sha256_file(exe)
    if err:
        raise EvidenceScopeError("BINARY_UNREADABLE",
                                 "无法读取正式程序 %s：%s" % (exe_relpath, err))
    if registered_sha256 and digest != registered_sha256:
        raise EvidenceScopeError(
            "REGISTERED_SHA_MISMATCH",
            "现场二进制的 sha256 与登记值不符——不得据此宣称'正式 EXE 已含该修复'")
    env = dict(environment or {})
    env.setdefault("interpreter", "frozen")
    return build_scope(subject=SUBJECT_FORMAL_EXE, entry_point=entry_point,
                       binary_path=exe_relpath, binary_sha256=digest,
                       environment=env, unverified=unverified)


def source_scope(root: Path, *, source_relpath: str = ".", entry_point: str,
                 environment=None, unverified=None, suffixes=(".py",)) -> dict:
    """针对"检查源码树"的便捷构造：现场计算源树内容快照摘要。"""
    base = Path(root) / source_relpath
    if not base.is_dir():
        raise EvidenceScopeError("SOURCE_MISSING",
                                 "源码路径不存在或不是目录：%s" % source_relpath)
    snapshot = hash_source_tree(base, suffixes=suffixes)
    env = dict(environment or {})
    env.setdefault("interpreter", "source")
    return build_scope(subject=SUBJECT_SOURCE, entry_point=entry_point,
                       source_path=source_relpath.replace("\\", "/"),
                       source_snapshot_sha256=snapshot, environment=env,
                       unverified=unverified)


def audit_conclusion(text: str) -> dict:
    """对一段结论正文做"禁止互相代称"的静态检查。只读、纯函数。

    返回 {"ok": bool, "conflation_hits": [...]}。ok=True 只表示"未发现明显代称"，
    不代表结论已带正确的结构化证据范围——后者应由 EvidenceScope 单独承载。
    """
    hits = detect_conflation(text)
    return {"ok": not hits, "conflation_hits": hits}


def to_markdown(scope: dict) -> str:
    """把证据范围渲染成人可读单页。确定性输出。"""
    data = scope if isinstance(scope, dict) else {}
    lines = ["## 证据范围（本结论检查了什么）", ""]
    lines.append("- 检查对象：**%s**" % data.get("subject_label", ""))
    lines.append("- 所走入口：`%s`" % data.get("entry_point", ""))
    binary = data.get("binary")
    if binary:
        lines.append("- 正式程序：`%s`" % binary.get("path", ""))
        lines.append("  - sha256：`%s`" % binary.get("sha256", ""))
    source = data.get("source")
    if source:
        lines.append("- 源码/候选包：`%s`" % source.get("path", ""))
        lines.append("  - 快照 sha256：`%s`" % source.get("snapshot_sha256", ""))
    env = data.get("environment") or {}
    if env:
        lines.append("- 环境：%s" % "、".join(
            "%s=%s" % (k, env[k]) for k in sorted(env)))
    unverified = data.get("unverified") or []
    lines.append("- 未验证项：%s" % ("、".join(unverified) if unverified else "（无）"))
    if data.get("note"):
        lines.append("- 说明：%s" % data["note"])
    lines.append("")
    return "\n".join(lines)


def render(scope: dict) -> str:
    return to_markdown(scope)
