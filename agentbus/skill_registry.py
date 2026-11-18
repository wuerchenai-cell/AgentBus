# -*- coding: utf-8 -*-
"""统一技能来源登记：本地目录 / Git 来源 / ZIP 来源的元数据索引与安全 Z检查。

来源：WorkBuddy，2026-10-08；任务 m-1791457584489-f05b6b（作业 j-b7ed91331128d934d22268834e306bd4）。
批次 PRODUCT-CONVERGENCE-20261008；复用正式 skill_packages / skill_preview / skill_install / skill_evidence，
不新建第二套技能格式，也不复制它们的扫描规则。

边界（写死）：
- 只登记**元数据**与做**有界校验**；不联网、不 clone、不执行任何技能脚本、不读宿主私有配置。
- Git 来源只记录 url/ref/commit 并做形状校验；**不做网络解析**，无法验证远端存在性（明确返回 unverified）。
- ZIP 校验只读条目表与有界摘要，不解压；解压发生在部署事务内（skill_deployer）并复用同一套校验。
- 路径必须由调用方显式给出，并且必须落在调用方给定的公共根之内；禁止探测 home / 宿主私有目录。
- 所有函数不修改传入对象。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 1

SOURCE_KINDS = ("directory", "git", "zip")
PINNED_KINDS = ("git", "zip")

FORBIDDEN_PARTS = (".workbuddy",)
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "clock$",
                    *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}

MAX_ZIP_ENTRIES = 512
MAX_ZIP_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ZIP_ENTRY_BYTES = 8 * 1024 * 1024

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_URL = re.compile(r"(?:https://|ssh://|git@)[^\s]{3,512}\Z")

# 与 skill_packages 同源判定：ZIP 里如果含 SKILL.md，登记时才允许 pinned 语义。
ZIP_LINK_MODE = 0o120000


class SkillRegistryError(ValueError):
    """登记输入或来源包无效。"""


def _fail(code: str, message: str) -> None:
    raise SkillRegistryError(f"{code}: {message}")


def _norm_name(value: object, *, label: str = "name") -> str:
    """规范化技能名：NFKC + 去首尾空白 + 小写；必须是安全单目录分量。"""
    if not isinstance(value, str):
        _fail("INVALID_NAME", f"{label} 必须是字符串")
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or len(text) > 64:
        _fail("INVALID_NAME", f"{label} 长度必须是 1-64 个字符")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        _fail("INVALID_NAME", f"{label} 不能包含控制字符")
    lowered = text.lower()
    if lowered != text:
        text = lowered
    if not _NAME.match(text) or text.split(".")[0] in WINDOWS_RESERVED:
        _fail("INVALID_NAME", f"{label} 含非法字符或是系统设备名：{value!r}")
    return text


def _forbidden(path_parts) -> bool:
    return any(str(part).casefold().rstrip(" .") in FORBIDDEN_PARTS for part in path_parts)


def safe_relative_root(allowed_root: Path | str, candidate: Path | str, *,
                       label: str = "path") -> Path:
    """把 candidate 约束在 allowed_root 之内；拒绝私有目录、父目录逃逸与控制字符。"""
    if allowed_root is None or candidate is None:
        _fail("INVALID_PATH", f"{label} 与 allowed_root 都必须显式给出，不做隐式探测")
    root = Path(allowed_root)
    raw = str(candidate)
    if any(ord(ch) < 32 for ch in raw):
        _fail("UNSAFE_PATH", f"{label} 含控制字符")
    target = Path(raw)
    if not target.is_absolute():
        target = root / target
    if _forbidden(target.parts) or _forbidden(root.parts):
        _fail("UNSAFE_PATH", f"{label} 位于禁止目录（宿主私有）")
    if ".." in target.parts or ".." in Path(raw.replace("\\", "/")).parts:
        _fail("UNSAFE_PATH", f"{label} 含父目录引用")
    try:
        resolved_root = root.resolve()
        resolved = target.resolve()
    except OSError as exc:  # pragma: no cover - 平台相关
        raise SkillRegistryError(f"UNSAFE_PATH: {label} 无法解析：{exc}") from exc
    if not resolved.is_relative_to(resolved_root):
        _fail("UNSAFE_PATH", f"{label} 越出给定公共根：{candidate}")
    return resolved


def normalize_git_source(url: object, ref: object, commit: object = None) -> dict:
    """校验 Git 来源元数据；不做网络解析，远端存在性保持 unverified。"""
    if not isinstance(url, str) or not _URL.match(url.strip()):
        _fail("INVALID_SOURCE", "Git 来源必须是 https:// / ssh:// / git@ 形式的地址")
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 128:
        _fail("INVALID_SOURCE", "Git 来源必须给出非空 ref（分支或标签）")
    record = {"url": url.strip(), "ref": ref.strip(), "commit": None,
              "pinned": False, "remote_verified": False}
    if commit is not None:
        if not isinstance(commit, str) or not _COMMIT.match(commit.strip()):
            _fail("INVALID_SOURCE", "commit 必须是 40 位小写十六进制")
        record["commit"] = commit.strip()
        record["pinned"] = True
    return record


def sha256_file(path: Path | str, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            block = stream.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _zip_entry_name_ok(name: str) -> None:
    if not name or name.endswith("/"):
        return
    if any(ord(ch) < 32 for ch in name):
        _fail("UNSAFE_ENTRY", f"ZIP 条目含控制字符：{name!r}")
    posix = PurePosixPath(name)
    if name.startswith("/") or posix.is_absolute():
        _fail("UNSAFE_ENTRY", f"ZIP 条目是绝对路径：{name!r}")
    if "\\" in name:
        _fail("UNSAFE_ENTRY", f"ZIP 条目含反斜杠，跨平台语义不一致：{name!r}")
    if re.match(r"[A-Za-z]:", name):
        _fail("UNSAFE_ENTRY", f"ZIP 条目含盘符：{name!r}")
    parts = name.split("/")
    if any(part in ("..",) for part in parts):
        _fail("UNSAFE_ENTRY", f"ZIP 条目含父目录引用：{name!r}")
    if any(part and part.casefold().rstrip(" .") in WINDOWS_RESERVED for part in parts):
        _fail("UNSAFE_ENTRY", f"ZIP 条目含系统设备名：{name!r}")
    if _forbidden(parts):
        _fail("UNSAFE_ENTRY", f"ZIP 条目落在禁止目录：{name!r}")


def _zip_entry_is_link(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == ZIP_LINK_MODE


def inspect_zip(archive: Path | str, *, allowed_root: Path | str) -> dict:
    """只读校验一个 ZIP 来源：结构、条目安全、规模与摘要；不解压。"""
    path = safe_relative_root(allowed_root, archive, label="zip")
    if not path.is_file():
        _fail("MISSING_SOURCE", f"ZIP 不存在：{archive}")
    if not zipfile.is_zipfile(path):
        _fail("INVALID_ZIP", f"不是可读 ZIP（坏包或非 ZIP）：{archive}")
    entries: list[dict] = []
    total = 0
    names: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive_file:
            infos = archive_file.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                _fail("ZIP_TOO_LARGE", f"ZIP 条目数超过上限 {MAX_ZIP_ENTRIES}")
            for info in infos:
                if info.is_dir():
                    continue
                _zip_entry_name_ok(info.filename)
                if _zip_entry_is_link(info):
                    _fail("UNSAFE_ENTRY", f"ZIP 含符号链接条目：{info.filename!r}")
                if info.file_size > MAX_ZIP_ENTRY_BYTES:
                    _fail("ZIP_TOO_LARGE", f"单个条目超过上限：{info.filename!r}")
                if info.filename in names:
                    _fail("DUPLICATE_ENTRY", f"ZIP 含重复条目：{info.filename!r}")
                names.add(info.filename)
                total += info.file_size
                if total > MAX_ZIP_TOTAL_BYTES:
                    _fail("ZIP_TOO_LARGE", "ZIP 解压后总体积超过上限")
                entries.append({"path": info.filename, "bytes": int(info.file_size),
                                "crc32": "%08x" % (info.CRC & 0xFFFFFFFF)})
    except zipfile.BadZipFile as exc:
        raise SkillRegistryError(f"INVALID_ZIP: ZIP 结构损坏：{exc}") from exc
    except OSError as exc:
        raise SkillRegistryError(f"UNREADABLE_SOURCE: 无法读取 ZIP：{exc}") from exc
    if not any(entry["path"] == "SKILL.md" for entry in entries):
        _fail("MISSING_ENTRY", "ZIP 根目录缺少 SKILL.md 入口")
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    return {"schema_version": SCHEMA_VERSION, "kind": "zip", "path": str(path),
            "bytes": path.stat().st_size, "sha256": sha256_file(path),
            "entries": entries, "entry_count": len(entries), "total_bytes": total,
            "content_digest": _entries_digest(entries)}


def _entries_digest(entries: list[dict]) -> str:
    """与“路径+原始字节规模”一致的确定性摘要，仅用于元数据对齐，不替代包内摘要。"""
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry["bytes"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def normalize_source(kind: object, *, name: object, location: object,
                     allowed_root: Path | str, ref: object = None,
                     commit: object = None, sha256: object = None,
                     bytes_: object = None) -> dict:
    """把一次来源登记规范化成统一记录；不做网络与宿主私有访问。"""
    normalized_name = _norm_name(name)
    if kind not in SOURCE_KINDS:
        _fail("INVALID_SOURCE", f"kind 必须是 {SOURCE_KINDS} 之一")
    record: dict = {"schema_version": SCHEMA_VERSION, "name": normalized_name,
                    "kind": kind, "pinned": False}
    if kind == "directory":
        path = safe_relative_root(allowed_root, location, label="directory")
        if not path.is_dir():
            _fail("MISSING_SOURCE", f"目录来源不存在或不是目录：{location}")
        record.update(location=str(path), exists=True)
    elif kind == "git":
        git = normalize_git_source(location, ref, commit)
        record.update(location=git["url"], ref=git["ref"], commit=git["commit"],
                      pinned=git["pinned"], remote_verified=False)
    else:
        if not isinstance(sha256, str) or not _SHA256.match(sha256):
            _fail("INVALID_SOURCE", "ZIP 来源必须给出 64 位小写 sha256")
        info = inspect_zip(location, allowed_root=allowed_root)
        if info["sha256"] != sha256:
            _fail("DIGEST_MISMATCH", "ZIP 实际摘要与声明 sha256 不一致")
        if bytes_ is not None and bytes_ != info["bytes"]:
            _fail("DIGEST_MISMATCH", "声明的 bytes 与 ZIP 实际大小不一致")
        record.update(location=info["path"], sha256=info["sha256"], bytes=info["bytes"],
                      content_digest=info["content_digest"],
                      entry_count=info["entry_count"], pinned=True)
    return record


def identity_of(record: dict) -> str:
    """来源身份：用于判断“同名不同源”。只取固定字段，不含时间。"""
    if not isinstance(record, dict):
        _fail("INVALID_RECORD", "记录必须是对象")
    kind = record.get("kind")
    parts = [str(record.get("name") or ""), str(kind)]
    if kind == "git":
        parts += [str(record.get("location") or ""), str(record.get("commit") or record.get("ref") or "")]
    elif kind == "zip":
        parts += [str(record.get("sha256") or "")]
    else:
        parts += [str(record.get("location") or "")]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]


class SkillRegistry:
    """内存来源索引。纯数据，不触盘（ZIP 校验在登记时由调用方显式触发）。"""

    def __init__(self) -> None:
        self._by_name: dict[str, dict] = {}

    def add(self, record: dict) -> dict:
        if not isinstance(record, dict):
            _fail("INVALID_RECORD", "记录必须是对象")
        name = _norm_name(record.get("name"))
        if not record.get("kind"):
            _fail("INVALID_RECORD", "记录缺少 kind")
        stored = dict(record)
        stored["name"] = name
        stored["identity"] = identity_of(stored)
        current = self._by_name.get(name)
        if current is None:
            self._by_name[name] = stored
            return dict(stored)
        if current["identity"] == stored["identity"]:
            # 幂等：同源重复登记不产生新条目，也不覆盖已固定的来源。
            return dict(current)
        _fail("DUPLICATE_CONFLICT",
              f"技能名 {name!r} 已登记为另一来源；不静默覆盖，请先显式移除")

    def remove(self, name: object) -> dict:
        normalized = _norm_name(name)
        if normalized not in self._by_name:
            _fail("MISSING_SOURCE", f"未登记技能：{name!r}")
        return dict(self._by_name.pop(normalized))

    def get(self, name: object) -> dict:
        normalized = _norm_name(name)
        if normalized not in self._by_name:
            _fail("MISSING_SOURCE", f"未登记技能：{name!r}")
        return dict(self._by_name[normalized])

    def list(self, *, kind: object = None, pinned: object = None,
             name_contains: object = None) -> list[dict]:
        if kind is not None and kind not in SOURCE_KINDS:
            _fail("INVALID_FILTER", f"kind 过滤值必须是 {SOURCE_KINDS} 之一")
        if pinned is not None and not isinstance(pinned, bool):
            _fail("INVALID_FILTER", "pinned 过滤值必须是布尔")
        needle = None
        if name_contains is not None:
            needle = _norm_name(name_contains, label="name_contains")
        out = []
        for record in self._by_name.values():
            if kind is not None and record.get("kind") != kind:
                continue
            if pinned is not None and bool(record.get("pinned")) is not pinned:
                continue
            if needle is not None and needle not in record["name"]:
                continue
            out.append(dict(record))
        out.sort(key=lambda item: item["name"].encode("utf-8"))
        return out

    def classify(self, record: dict) -> dict:
        """把一条候选来源与索引对照，给出 new / same / duplicate_conflict / newer_pin。"""
        name = _norm_name(record.get("name"))
        current = self._by_name.get(name)
        identity = identity_of(record)
        if current is None:
            return {"state": "new", "name": name, "identity": identity}
        if current["identity"] == identity:
            return {"state": "same", "name": name, "identity": identity}
        both_git = current.get("kind") == "git" and record.get("kind") == "git"
        if both_git and current.get("location") == record.get("location"):
            return {"state": "newer_pin", "name": name, "identity": identity,
                    "current": dict(current)}
        return {"state": "duplicate_conflict", "name": name, "identity": identity,
                "current": dict(current)}

    def snapshot(self) -> dict:
        return {"schema_version": SCHEMA_VERSION,
                "entries": [dict(value) for _, value in sorted(self._by_name.items())]}
