"""Portable project task structures; never import execution state or run code.

Owner task-tree workflow / Codex, 2026-10-09. Templates are data. Import appends
a fresh namespace under the task-forest lock and leaves every live mailbox,
claim, task binding and existing checkpoint untouched.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
import uuid

from .operation_lock import task_lock
from .task_tree import TaskForest, TaskTreeError

_KINDS = ("roots", "branches", "leaves")
_FIELDS = {"id", "title", "goal", "handoff", "parent_id", "dependencies",
           "required_capabilities", "expected_artifacts", "section_no"}
_TOP = {"schema_version", "format", "title", "description", *_KINDS}
_FORMAT = "agentbus-task-tree"
_GENERIC_TOP = {"schema_version", "format", "title", "description", "project_name",
                "generated_at", *_KINDS}
_RUNTIME_NODE_FIELDS = {
    "owner", "assignee", "assigned_to", "executor", "status", "state", "progress",
    "task_id", "job_id", "result_id", "results", "result", "artifacts", "heartbeat",
    "started_at", "completed_at", "finished_at", "attempt", "attempts", "claim_id",
    "claim_epoch", "claim_agent", "claim_fingerprint", "ticket", "lease", "execution",
    # Common exported views also carry derived eligibility, old task history,
    # progress and evidence/recommendation metadata. These are not reusable
    # task structure and must be disclosed as dropped, never imported as state.
    "claimable", "eligible_agents", "history_task_ids", "progress_percent",
    "recommended_agent", "recommendation_reason", "unmet_dependencies", "evidence_error",
}
_REGENERATED_NODE_FIELDS = {"section_no"}
_GENERIC_NODE_ALIASES = {"name": "title", "description": "goal", "instructions": "goal",
                         "depends_on": "dependencies"}
MAX_TEMPLATE_BYTES = 4 * 1024 * 1024
MAX_TEMPLATE_NODES = 1000
_LOCAL = re.compile(
    r"(?i)(?:\b[a-z]:[\\/][^\s<>\"'`，。；!?！？]*|\\\\[^\s<>\"'`，。；!?！？]+|"
    r"file://[^\s<>\"'`，。；!?！？]*|(?<![:\w/\]])/(?!/)[^\s<>\"'`，。；!?！？]+|"
    r"(?<![\w\\\]])\\[A-Za-z0-9_.-]+\\[^\s<>\"'`，。；!?！？]+)")
_QUOTED_LOCAL = re.compile(r"(?i)([\"'`])((?:[a-z]:[\\/]|\\\\|/(?!/))[^\r\n\"'`]+)\1")
_API_ROUTE = re.compile(
    r"(?i)(?P<prefix>(?:\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT|"
    r"endpoint|route|URI|URL)\b|接口|端点|路由)\s*[:：]?\s*[\"'`]?)"
    r"(?P<route>/(?!/)[^\s<>\"'`，。；!！？]+)")
_OLD_ID = re.compile(r"(?<![A-Za-z0-9_])(?:m|j|r)-[A-Za-z0-9][A-Za-z0-9._:-]{2,127}(?![A-Za-z0-9_])")
_RUNTIME_FIELD = re.compile(
    r"(?i)\b(?:claim_id|claim_epoch|source_claim_id|source_claim_epoch|claimed_by|claim_agent|"
    r"job_id|task_id|result_id|plan_id|plan_sha256|result_to|heartbeat)\b[\"']?\s*[:=]\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]\r\n]+)")
_EXECUTION_PATH = re.compile(r"(?i)\.agentbus[\\/](?:jobs|agents|claims|heartbeats?|sessions|"
    r"project-plan[\\/](?!current\.json(?:\b|$)))[\\/]?[^\s<>\"'`，。；!?！？]+")
_DEVICE = re.compile(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:[ .]|$)", re.I)
_CLEANUP_NOTICE = "导出时已清理本机路径或旧执行引用；请检查目标与接手说明，补充新项目的相对输入。"


def _protect_api_routes(text):
    """An explicitly labelled HTTP route is not a local filesystem input."""
    protected = {}
    prefix = "AGENTBUS_HTTP_ROUTE_" + uuid.uuid4().hex + "_"
    def protect(match):
        key = prefix + str(len(protected))
        protected[key] = match.group("route")
        return match.group("prefix") + key
    return _API_ROUTE.sub(protect, text), protected


def _contains_local_path(text):
    masked, _ = _protect_api_routes(text)
    return bool(_LOCAL.search(masked))
_FORBIDDEN = {".workbuddy", "不要访问", "do not access", ".agentbus"}


def _text(value, label, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise TaskTreeError(f"template {label} must be bounded text")
    if "\x00" in value:
        raise TaskTreeError("template text contains NUL")
    return value


def _relative(value):
    value = _text(value, "artifact path", 1024)
    # Interpret Windows paths even when validation runs on another platform.
    path = PureWindowsPath(value)
    if (not value or path.drive or path.root or PurePosixPath(value).is_absolute()
            or "\\" in value or ":" in value or "?" in value or "*" in value
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in '<>\"|' for char in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(part.endswith((".", " ")) or _DEVICE.match(part) for part in value.split("/"))
            or any(part.casefold() in _FORBIDDEN for part in value.split("/"))):
        raise TaskTreeError("template artifacts must be safe project-relative paths")
    return value


def validate_template(document):
    """Return a fresh, normalized allowlisted document; reject unsafe input."""
    if not isinstance(document, dict) or set(document) - _TOP:
        raise TaskTreeError("template accepts structure only; execution fields are not portable")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise TaskTreeError("unsupported task tree template version")
    if document.get("format") != _FORMAT:
        raise TaskTreeError("not an AgentBus task tree template")
    try:
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise TaskTreeError("template must be finite JSON data") from exc
    if len(encoded) > MAX_TEMPLATE_BYTES:
        raise TaskTreeError("template exceeds 4 MiB")
    title = _text(document.get("title", "任务树模板"), "title", 200)
    description = _text(document.get("description", ""), "description", 4000)
    structure = {"schema_version": 1}
    for kind in _KINDS:
        values = document.get(kind)
        if not isinstance(values, list):
            raise TaskTreeError(f"template {kind} must be a list")
        structure[kind] = []
        kind_label = {"roots": "总目标", "branches": "分支", "leaves": "叶子任务"}[kind]
        for index, item in enumerate(values, start=1):
            location = f"{kind_label}第 {index} 项"
            if not isinstance(item, dict):
                raise TaskTreeError(f"{location}必须是 JSON 对象。")
            extra_fields = set(item) - _FIELDS
            if extra_fields:
                runtime = sorted(extra_fields & (_RUNTIME_NODE_FIELDS | {"result_to"}))
                detail = f"请移除 {', '.join(runtime)} 等执行字段" if runtime else "请移除模板不支持的字段"
                raise TaskTreeError(f"{location}含不支持的字段。{detail}，模板只保留任务结构。")
            node = copy.deepcopy(item)
            for key in ("title", "goal", "handoff"):
                node[key] = _text(node.get(key, ""), key, 100000)
                if _contains_local_path(node[key]):
                    raise TaskTreeError("template text contains a local absolute path; use project-relative references")
                if _OLD_ID.search(node[key]) or _RUNTIME_FIELD.search(node[key]) or _EXECUTION_PATH.search(node[key]):
                    raise TaskTreeError("template text references old execution records; use reusable task goals")
            artifacts = node.get("expected_artifacts", [])
            if not isinstance(artifacts, list) or len(artifacts) > 1000:
                raise TaskTreeError("invalid template expected_artifacts")
            node["expected_artifacts"] = [_relative(value) for value in artifacts]
            if len({value.casefold() for value in node["expected_artifacts"]}) != len(node["expected_artifacts"]):
                raise TaskTreeError("template artifact paths collide on case-insensitive filesystems")
            structure[kind].append(node)
    count = sum(len(structure[key]) for key in _KINDS)
    if not structure["roots"] or not count or count > MAX_TEMPLATE_NODES:
        raise TaskTreeError("template needs a root and at most 1000 nodes")
    if _contains_local_path(title) or _contains_local_path(description):
        raise TaskTreeError("template metadata contains a local absolute path")
    if any(pattern.search(value) for pattern in (_OLD_ID, _RUNTIME_FIELD, _EXECUTION_PATH)
           for value in (title, description)):
        raise TaskTreeError("template metadata references old execution records")
    try:
        normalized = TaskForest._normalize(structure)
    except (ValueError, TypeError, RecursionError) as exc:
        raise TaskTreeError(f"invalid template structure: {exc}") from exc
    # _normalize adds empty runtime compatibility fields; the portable result
    # keeps only the published template vocabulary.
    out = {"schema_version": 1, "format": _FORMAT, "title": title, "description": description}
    for kind in _KINDS:
        out[kind] = [{key: copy.deepcopy(value) for key, value in node.items() if key in _FIELDS}
                     for node in normalized[kind]]
    return out


def adapt_external_tree(document):
    """Convert a bounded roots/branches/leaves JSON export to a clean template.

    This deliberately accepts one explicit, common interchange shape. It does
    not guess nested schemas or turn prior execution state into new work.
    """
    if not isinstance(document, dict):
        raise TaskTreeError("任务树文件的最外层必须是 JSON 对象")
    if document.get("format") == _FORMAT and not (set(document) - _TOP):
        return validate_template(document), []
    unknown_top = set(document) - _GENERIC_TOP
    if unknown_top:
        names = ", ".join(sorted(map(str, unknown_top))[:12])
        raise TaskTreeError(f"不认识任务树顶层字段：{names}。请提供 roots/branches/leaves 结构的 JSON 文件。")
    if "schema_version" in document and (type(document["schema_version"]) is not int
                                           or document["schema_version"] != 1):
        raise TaskTreeError("任务树版本不受支持；当前只接受 schema_version 1")
    if not all(isinstance(document.get(kind), list) for kind in _KINDS):
        missing = ", ".join(kind for kind in _KINDS if not isinstance(document.get(kind), list))
        raise TaskTreeError(f"任务树结构不完整，缺少数组：{missing}")

    warnings = []
    dropped = set()
    regenerated = set()
    converted = {"schema_version": 1, "format": _FORMAT,
                 "title": document.get("title") or document.get("project_name") or "导入的任务树",
                 "description": document.get("description") or ""}
    if document.get("format") not in (None, _FORMAT):
        warnings.append("已将外部任务树格式转换为 AgentBus 结构模板。")
    if document.get("generated_at") is not None:
        dropped.add("generated_at")
    for kind in _KINDS:
        converted[kind] = []
        for index, item in enumerate(document[kind]):
            if not isinstance(item, dict):
                raise TaskTreeError(f"{kind}[{index}] 必须是对象")
            node = {}
            for raw_key, value in item.items():
                key = str(raw_key)
                if key in _RUNTIME_NODE_FIELDS:
                    dropped.add(key)
                    continue
                if key in _REGENERATED_NODE_FIELDS:
                    regenerated.add(key)
                    continue
                target = _GENERIC_NODE_ALIASES.get(key, key)
                if target not in _FIELDS:
                    raise TaskTreeError(f"不支持的任务字段：{kind}[{index}].{key}")
                if target in node:
                    raise TaskTreeError(f"任务字段映射重复：{kind}[{index}].{target}")
                node[target] = value
            converted[kind].append(node)
    if dropped:
        names = ", ".join(sorted(dropped))
        warnings.append(f"旧执行状态不会导入；已剥离字段（{names}），不会继承旧负责人、进度、回执或认领。")
    if regenerated:
        warnings.append("源任务树的章节编号不会保留；导入后按当前项目重新编号。")
    if document.get("project_name") and not document.get("title"):
        warnings.append("已将 project_name 用作新任务树名称。")
    return validate_template(converted), warnings


def scrub_local_paths(text, replacement):
    """把文本中的**本机绝对路径**替换为 `replacement`，返回 `(新文本, 替换次数)`。

    「什么算本机绝对路径」在本模块只有一个判定入口（`_LOCAL` + `_protect_api_routes`）。
    备份导出（`agentbus.backup`）等其它出口必须调用本函数，而不是另写一套正则——
    否则同一个字符串可能在任务树模板出口被清理、在备份出口泄漏。
    显式标注的 HTTP 路由（URL/接口/端点）不算本机路径，会被原样保留。
    """
    masked, protected = _protect_api_routes(str(text or ""))
    scrubbed, count = _LOCAL.subn(replacement, masked)
    for marker, route in protected.items():
        scrubbed = scrubbed.replace(marker, route)
    return scrubbed, count


def contains_local_path(value):
    """公开入口：判断文本是否含本机绝对路径（与导出清理共用同一判定）。"""
    return _contains_local_path(str(value or ""))


def scrub_path_value(value, replacement):
    """清理一个**原子路径串**（整串本身就是路径，例如 argv 项、launcher 路径、env 值）。

    与 `scrub_local_paths` 的关键差别：后者面向自由文本，故意在空格处停下，
    以免把路径后面的正文一起吞掉（见 `_portable_text` 注释）。但机器配置里的值
    本身就是路径，`C:\\Program Files\\...` 必须**整条**替换，否则会留下
    ` Files\\...` 这样的残渣。判定「整串是路径」用 `_LOCAL.match` 锚定开头。
    """
    text = str(value or "")
    if _LOCAL.match(text):
        return replacement, 1
    return scrub_local_paths(text, replacement)


def _portable_text(value, forest):
    text = value or ""
    # Remove both configured project roots before a generic local-path scrub.
    for root in (forest.cfg.workspace, forest.cfg.output_root):
        for spelling in (str(root), str(root).replace("\\", "/")):
            text = text.replace(spelling, "[项目目录]")
    text, protected = _protect_api_routes(text)
    # Quotes delimit paths containing spaces. Unquoted paths stop at the next
    # whitespace/punctuation, so removing a path does not swallow the task's
    # remaining sentence. Keep the explicit project-relative marker intact.
    text = _QUOTED_LOCAL.sub("[本机路径已移除]", text)
    text = _LOCAL.sub("[本机路径已移除]", text)
    text = _EXECUTION_PATH.sub("[旧执行资料引用已移除]", text)
    text = _RUNTIME_FIELD.sub("[旧运行字段已移除]", text)
    text = _OLD_ID.sub("[旧执行引用已移除]", text)
    for marker, route in protected.items():
        text = text.replace(marker, route)
    return text


def export_template_plan(forest):
    """导出模板**并给出同一份 id 映射**：`(模板文档, {原节点id: 模板节点id})`。

    单独暴露映射是因为导出不止任务树一处用到节点 id：画布折叠（分支 id）与卡片
    优先级（叶子 id）也必须用**同一套映射**落到模板上，否则还原后它们指向的节点
    根本不存在（§13.7 的实测缺陷）。`export_template` 是本函数的薄封装。
    """
    tree = forest._structure()
    mapping = {node["id"]: f"node-{index + 1}" for index, node in enumerate(
        [node for kind in _KINDS for node in tree[kind]])}
    title = _portable_text(tree["roots"][0]["title"], forest) if tree["roots"] else "任务树模板"
    cleaned = bool(tree["roots"] and title != tree["roots"][0]["title"])
    out = {"schema_version": 1, "format": _FORMAT, "title": title[:200], "description": ""}
    for kind in _KINDS:
        out[kind] = []
        for node in tree[kind]:
            exported = {key: copy.deepcopy(value) for key, value in node.items() if key in _FIELDS}
            exported["id"] = mapping[node["id"]]
            exported["parent_id"] = mapping.get(node.get("parent_id"))
            exported["dependencies"] = [mapping[identity] for identity in node.get("dependencies", [])]
            for key in ("title", "goal", "handoff"):
                exported[key] = _portable_text(node.get(key, ""), forest)
                cleaned = cleaned or exported[key] != node.get(key, "")
            # A malformed/private old declaration is not safe to share. Preserve
            # no accidental absolute path, rather than leaking it in the export.
            exported["expected_artifacts"] = [_relative(value) for value in node.get("expected_artifacts", [])]
            out[kind].append(exported)
    if cleaned:
        out["description"] = _CLEANUP_NOTICE
    return validate_template(out), mapping


def export_template(forest):
    """Export structural intent, omitting historical IDs and machine state."""
    return export_template_plan(forest)[0]


def preview_template(document):
    """预览模板将导入什么；原样保留导入警示。"""
    valid = validate_template(document)
    warnings = ["导入将追加独立任务树，重新编号；不会导入原执行人、成果或认领状态。"]
    if _CLEANUP_NOTICE in valid["description"]:
        warnings.append(_CLEANUP_NOTICE)
    return {"title": valid["title"], "roots": len(valid["roots"]),
            "branches": len(valid["branches"]), "leaves": len(valid["leaves"]),
            "node_count": sum(len(valid[key]) for key in _KINDS),
            "warnings": warnings}


def render_template_outline(document, *, title: str = "") -> str:
    """Render a validated template's hierarchy as plain indented text.

    Read-only and deterministic: no IDs, no execution state, no reliance on a
    terminal width. Callers print this; nothing here writes to disk.
    """
    valid = validate_template(document)
    roots = {node["id"]: node for node in valid["roots"]}
    branches = valid["branches"]
    leaves = valid["leaves"]
    children = {}
    for node in branches + leaves:
        children.setdefault(node.get("parent_id"), []).append(node)
    for group in children.values():
        group.sort(key=lambda node: node["id"])

    lines = [f'{title or valid["title"]}（{valid["title"]}）'] if title and title != valid["title"] \
        else [valid["title"]]
    if valid.get("description"):
        lines.append("  " + valid["description"])

    counts = {"roots": len(valid["roots"]), "branches": len(branches), "leaves": len(leaves)}
    heading = f'  概览：根 {counts["roots"]} · 分支 {counts["branches"]} · 叶子 {counts["leaves"]}'
    lines.append(heading)

    def walk(node, depth):
        prefix = "  " * depth
        lines.append(f'{prefix}- {node.get("title") or node["id"]}')
        goal = (node.get("goal") or "").strip()
        if goal and goal != node.get("title"):
            lines.append(f'{prefix}    目标：{goal}')
        for child in children.get(node["id"], []):
            walk(child, depth + 1)

    for root in sorted(valid["roots"], key=lambda node: node["id"]):
        walk(root, 1)
    return "\n".join(lines)


_MAILBOX_FOLDERS = ("ready", "processing", "blocked", "tmp")


def _restore_cards(pairs):
    """Best-effort rollback for cards already moved into quarantine."""
    for source, dest in reversed(pairs):
        try:
            if dest.is_file() and not source.exists():
                dest.replace(source)
        except (PermissionError, FileNotFoundError, IsADirectoryError, NotADirectoryError):
            pass  # 资源清理类：回滚失败时保留隔离副本，由调用方报告原始失败。


def replacement_impact(forest, *, with_cards=False):
    """Read-only replacement impact, including every actual mailbox owner."""
    pending, processing, cards = [], [], []
    signature = hashlib.sha256(forest.path.read_bytes() if forest.path.is_file() else b'')
    if forest.bus.agents_dir.is_dir():
        for member in sorted(forest.bus.agents_dir.iterdir()):
            if not member.is_dir() or member.is_symlink():
                continue
            for folder_name in _MAILBOX_FOLDERS:
                folder = member / folder_name
                if not folder.is_dir():
                    continue
                for path in sorted(folder.glob('*.json')):
                    msg = forest.bus.read(path)  # Corrupt state blocks replacement, never silently erased.
                    if msg.type not in ('task', 'goal', 'review'):
                        continue
                    signature.update((member.name + '/' + folder_name + '/' + path.name).encode('utf-8'))
                    raw = path.read_bytes()
                    signature.update(raw)
                    cards.append((path, hashlib.sha256(raw).hexdigest()))
                    item = {'agent': member.name, 'task_id': msg.id,
                            'title': msg.title, 'folder': folder_name}
                    (processing if folder_name == 'processing' else pending).append(item)
    return {'pending': pending, 'processing': processing, 'can_replace': not processing,
            **({'_cards': cards} if with_cards else {}),
            'revision': signature.hexdigest(),
            'notice': '追加保留当前任务。替换会归档旧树和未领任务；在途任务须先完成或正式交回。'}


def _quarantine_mailboxes(bus, *, tree_path=None, cards=()):
    """Move pending task cards aside before a tree replacement.

    Key write: if any card cannot be moved, cards already moved are restored and
    the error propagates, so the old tree and its cards stay consistent. On
    success the quarantine directory is kept as recovery evidence; nothing is
    deleted here.
    """
    trash = bus.root / "trash" / ("mailboxes-" + uuid.uuid4().hex[:12])
    pairs = []
    moved = False
    try:
        trash.mkdir(parents=True, exist_ok=False)
        if tree_path is not None and tree_path.is_file():
            (trash / 'task-tree.snapshot').write_bytes(tree_path.read_bytes())
        (trash / 'RECOVERY.md').write_text(
            '# 任务树替换归档\n\n来源：AgentBus 显式替换导入。\n'
            'task-tree.snapshot 保留替换前原树。各成员子目录保留旧待办原始字节。\n'
            '产物与 done 回执未移动。恢复前先暂停新工作并核对当前在途任务，'
            '不要覆盖新任务或复用旧票据；原始卡片供正式恢复流程核查。\n', encoding='utf-8')
        # Only the exact cards confirmed during preflight may be archived.
        # A concurrent publisher is not required to hold our member-claim locks.
        for item, expected_digest in cards:
            if hashlib.sha256(item.read_bytes()).hexdigest() != expected_digest:
                raise TaskTreeError('待办内容在替换前发生变化，请重新预览。')
            dest = trash / item.relative_to(bus.agents_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            item.replace(dest)
            pairs.append((item, dest))
            if hashlib.sha256(dest.read_bytes()).hexdigest() != expected_digest:
                raise TaskTreeError('待办在归档时发生变化，已停止替换并保留恢复证据。')
        if pairs:
            bus.audit(event="mailboxes_quarantined", count=len(pairs), quarantine=str(trash))
        moved = True
        return trash
    finally:
        if not moved:
            _restore_cards(pairs)


def _restore_mailboxes(bus, trash):
    """Restore a completed quarantine when the new tree was not committed."""
    if not trash.is_dir():
        return
    pairs = [(bus.agents_dir / path.relative_to(trash), path)
             for path in sorted(trash.rglob("*.json"))]
    _restore_cards(pairs)


def import_template(forest, document, namespace=None, merge=False, return_mapping=False, replace=False,
                    replacement_info=None, expected_impact=None):
    """把模板节点写进任务树；**只写结构，不派发、不认领**。

    - `replace=True`：导入新模板时彻底关闭并替换原有旧任务树，清空历史旧节点（工作台导入默认启用）。
    - `replace=False`（默认）：保留已有任务节点，追加新模板节点。
    - `namespace=None`（默认）：随机命名空间，每次导入都追加一份独立副本。
    - `merge=True`：已存在的映射 id **保持原样不覆盖**，只补缺失的节点。
    - `return_mapping=True`：额外返回 `{模板节点id: 最终节点id}`。
    """
    valid = validate_template(document)
    if type(replace) is not bool:
        raise TaskTreeError('替换选项必须是布尔值')
    namespace = namespace or "import-" + uuid.uuid4().hex[:20]
    with task_lock(forest.root, "task-forest", "structure"), ExitStack() as claim_locks:
        tree = forest._structure()
        quarantine = None
        committed = False
        if replace:
            # Same lock used by Bus.claim: no member can begin work between
            # this preflight and the pending-card quarantine.
            members = set(forest.cfg.enabled_workers())
            if forest.bus.agents_dir.is_dir():
                members.update(p.name for p in forest.bus.agents_dir.iterdir() if p.is_dir())
            for member in sorted(members):
                claim_locks.enter_context(task_lock(forest.root, 'member-claims', member))
            impact = replacement_impact(forest, with_cards=True)
            if impact['processing']:
                names = '、'.join(sorted({row['agent'] for row in impact['processing']}))
                raise TaskTreeError(f'仍有在途任务（{names}），请先完成或正式交回；可选择追加导入。')
            if expected_impact is not None and impact['revision'] != expected_impact:
                raise TaskTreeError('当前任务或待办在预览后发生变化，请重新预览再替换。')
            existing = set()
            # Key write: quarantine pending cards before touching the tree file;
            # a failure aborts the replacement with the old tree still valid.
            quarantine = _quarantine_mailboxes(forest.bus, tree_path=forest.path, cards=impact['_cards'])
            if replacement_info is not None:
                replacement_info['recovery_path'] = quarantine.relative_to(forest.cfg.workspace).as_posix()
        else:
            existing = {node["id"] for kind in _KINDS for node in tree[kind]}
        try:
            if replace:
                for kind in _KINDS:
                    tree[kind] = []
            mapping = {node["id"]: namespace + ":" + hashlib.sha256(node["id"].encode("utf-8")).hexdigest()[:20]
                       for kind in _KINDS for node in valid[kind]}
            if len(set(mapping.values())) != len(mapping):
                raise TaskTreeError("import namespace collision; retry the import")
            if not merge and any(value in existing for value in mapping.values()):
                raise TaskTreeError("import namespace collision; retry the import")
            for kind in _KINDS:
                for item in valid[kind]:
                    node = copy.deepcopy(item)
                    node["id"] = mapping[item["id"]]
                    if merge and node["id"] in existing:
                        continue                      # 已导入过：保留既有节点，不覆盖
                    node["parent_id"] = mapping.get(item.get("parent_id"))
                    node["dependencies"] = [mapping[identity] for identity in item.get("dependencies", [])]
                    node.pop("section_no", None)
                    tree[kind].append(node)
            forest._write(tree)
            committed = True
        finally:
            if not committed and quarantine is not None:
                _restore_mailboxes(forest.bus, quarantine)
    snapshot = forest.snapshot()
    return (snapshot, mapping) if return_mapping else snapshot


def builtin_templates():
    """Small editable starting trees, with no vendor ownership or execution."""
    definitions = [
        ("software-project", "软件项目交付", "从需求、实现到验证与发布的通用任务树。", [
            ("scope", "需求与方案", [("requirements", "确定目标与验收", []),
                                    ("design", "完成实现方案", ["requirements"])]),
            ("build", "产品实现", [("implementation", "实现核心功能", ["design"])]),
            ("quality", "验证与交付", [("verification", "核对真实使用流程", ["implementation"]),
                                      ("release", "整理交付与接手说明", ["verification"])])]),
        ("research-project", "研究与报告", "明确研究问题、整理材料、分析结论并独立核验。", [
            ("question", "研究问题", [("scope", "明确问题与判断标准", [])]),
            ("evidence", "材料与分析", [("sources", "搜集并核实材料", ["scope"]),
                                       ("analysis", "分析证据与限制", ["sources"])]),
            ("report", "报告与复核", [("draft", "完成报告及来源说明", ["analysis"]),
                                      ("review", "独立核验结论", ["draft"])])]),
        ("agile-web-delivery", "全栈 Web 敏捷开发实战",
         "前端、后端、API 网关、自动化测试与 CI/CD 部署的端到端敏捷交付模板；每个节点自带目标与预期产物。", [
            ("discovery", "需求与迭代计划", [
                ("backlog", "拆分迭代待办与验收标准", [],
                 {"goal": "把目标拆成单个迭代内可完成的待办，并为每条写出可判定的验收标准。",
                  "expected_artifacts": ["成果/敏捷交付/迭代待办与验收标准.md"]}),
                ("design", "确定技术方案与接口边界", ["backlog"],
                 {"goal": "确定前后端分工、接口边界与数据流向，并写明本次明确不做的部分。",
                  "expected_artifacts": ["成果/敏捷交付/技术方案与接口边界.md"]})]),
            ("frontend", "前端实现", [
                ("ui", "实现页面与交互", ["design"],
                 {"goal": "按方案实现页面结构、状态与交互，保证关键流程可用。",
                  "expected_artifacts": ["成果/敏捷交付/前端页面与交互.md"]}),
                ("frontend-test", "前端组件与交互测试", ["ui"],
                 {"goal": "为组件与关键交互补自动化测试，覆盖空态、错误态与边界输入。",
                  "expected_artifacts": ["成果/敏捷交付/前端测试报告.md"]})]),
            ("backend", "后端与接口", [
                ("service", "实现服务端业务逻辑", ["design"],
                 {"goal": "实现业务逻辑与数据校验，失败路径要给出明确错误语义。",
                  "expected_artifacts": ["成果/敏捷交付/服务端实现说明.md"]}),
                ("gateway", "配置 API 网关与鉴权", ["service"],
                 {"goal": "配置路由、限流与鉴权，并核对未授权访问被正确拒绝。",
                  "expected_artifacts": ["成果/敏捷交付/网关与鉴权配置.md"]})]),
            ("quality", "自动化测试", [
                ("api-test", "接口自动化测试", ["gateway", "frontend-test"],
                 {"goal": "覆盖成功、失败与越权三类调用，并记录真实响应。",
                  "expected_artifacts": ["成果/敏捷交付/接口自动化测试报告.md"]}),
                ("e2e", "端到端回归", ["api-test"],
                 {"goal": "跑一遍完整用户路径，确认前端、后端与网关协同无阻断。",
                  "expected_artifacts": ["成果/敏捷交付/端到端回归记录.md"]})]),
            ("delivery", "CI/CD 与部署", [
                ("pipeline", "搭建 CI/CD 流水线", ["e2e"],
                 {"goal": "把构建、测试与部署串成流水线，失败即停并保留日志。",
                  "expected_artifacts": ["成果/敏捷交付/流水线配置说明.md"]}),
                ("release", "灰度发布与回滚演练", ["pipeline"],
                 {"goal": "先小流量发布再全量，并实际演练一次回滚。",
                  "expected_artifacts": ["成果/敏捷交付/发布与回滚演练.md"]})])]),
        ("ai-agent-research", "AI Agent 多智能体协同研发",
         "方案设计、Prompt 工程、工具集成、对抗评测与验收交付的多智能体研发模板；强调可复现评测与独立审核。", [
            ("design", "方案设计", [
                ("architecture", "确定智能体架构与协作协议", [],
                 {"goal": "确定成员角色、消息格式与交接边界，并写明协议不变式。",
                  "expected_artifacts": ["成果/多智能体研发/架构与协作协议.md"]}),
                ("roles", "划分成员角色与职责边界", ["architecture"],
                 {"goal": "为每个角色定义可交付产物与不允许越界的行为。",
                  "expected_artifacts": ["成果/多智能体研发/角色与职责边界.md"]})]),
            ("prompt", "Prompt 工程", [
                ("prompt-base", "编写基础提示词与约束", ["roles"],
                 {"goal": "写出可复用的基础提示词，明确指令层级与不可信内容标记。",
                  "expected_artifacts": ["成果/多智能体研发/基础提示词与约束.md"]}),
                ("prompt-eval", "建立提示词回归集", ["prompt-base"],
                 {"goal": "把典型输入固化成回归集，改提示词后能一键对比结果。",
                  "expected_artifacts": ["成果/多智能体研发/提示词回归集.md"]})]),
            ("tools", "工具集成", [
                ("tool-contract", "定义工具调用契约", ["roles"],
                 {"goal": "定义参数、返回值与失败语义，并标注只读与破坏性操作。",
                  "expected_artifacts": ["成果/多智能体研发/工具调用契约.md"]}),
                ("tool-wire", "完成工具接线与错误处理", ["tool-contract"],
                 {"goal": "完成接线，保证工具失败时不静默吞掉错误。",
                  "expected_artifacts": ["成果/多智能体研发/工具接线与错误处理.md"]})]),
            ("adversarial", "对抗评测", [
                ("redteam", "构造对抗样例与越界尝试", ["prompt-eval", "tool-wire"],
                 {"goal": "主动构造能绕过约束或诱导越权的样例，并记录真实结果。",
                  "expected_artifacts": ["成果/多智能体研发/对抗样例与越界尝试.md"]}),
                ("scorecard", "汇总评测评分与失败分类", ["redteam"],
                 {"goal": "给出评分口径与失败分类，未验证项要单独列出。",
                  "expected_artifacts": ["成果/多智能体研发/评测评分与失败分类.md"]})]),
            ("acceptance", "验收交付", [
                ("fix", "处置评测问题并复验", ["scorecard"],
                 {"goal": "逐条处置评测问题，并由未参与修改的成员复验。",
                  "expected_artifacts": ["成果/多智能体研发/问题处置与复验.md"]}),
                ("handover", "整理交付说明与接手入口", ["fix"],
                 {"goal": "写清已知限制与下一步入口，指出哪些结论仍未验证。",
                  "expected_artifacts": ["成果/多智能体研发/交付说明与接手入口.md"]})])]),
        ("business-mvp", "商业策划与 MVP 闭环",
         "用户画像与调研、需求故事、交互原型、MVP 验证与复盘的商业闭环模板；用可验证指标替代主观判断。", [
            ("research", "用户画像与调研", [
                ("persona", "建立目标用户画像", [],
                 {"goal": "写出目标用户是谁、在什么场景下使用，以及当前替代方案。",
                  "expected_artifacts": ["成果/商业MVP/目标用户画像.md"]}),
                ("interview", "完成用户访谈与需求归纳", ["persona"],
                 {"goal": "完成一定数量的真实访谈，并归纳出可验证的需求假设。",
                  "expected_artifacts": ["成果/商业MVP/用户访谈与需求归纳.md"]})]),
            ("story", "需求故事", [
                ("story-map", "梳理用户故事与优先级", ["interview"],
                 {"goal": "把需求转成用户故事并排优先级，说明取舍依据。",
                  "expected_artifacts": ["成果/商业MVP/用户故事与优先级.md"]}),
                ("metric", "定义可验证的指标口径", ["story-map"],
                 {"goal": "定义成功指标、统计口径与判定阈值，避免事后改标准。",
                  "expected_artifacts": ["成果/商业MVP/指标口径定义.md"]})]),
            ("prototype", "交互原型", [
                ("wireframe", "产出关键流程线框图", ["story-map"],
                 {"goal": "画出核心流程的线框，标出每一步的用户动作与系统反馈。",
                  "expected_artifacts": ["成果/商业MVP/关键流程线框图.md"]}),
                ("clickable", "完成可点击原型", ["wireframe"],
                 {"goal": "把主线流程做成可点击原型，供真实用户试用。",
                  "expected_artifacts": ["成果/商业MVP/可点击原型说明.md"]})]),
            ("mvp", "MVP 验证", [
                ("build", "实现最小可用版本", ["clickable", "metric"],
                 {"goal": "只实现验证假设所需的最小功能，明确暂不做的部分。",
                  "expected_artifacts": ["成果/商业MVP/最小可用版本说明.md"]}),
                ("experiment", "执行验证实验并收集数据", ["build"],
                 {"goal": "按既定口径收集真实数据，样本不足时如实说明。",
                  "expected_artifacts": ["成果/商业MVP/验证实验数据.md"]})]),
            ("review", "复盘与决策", [
                ("retro", "写出复盘结论与取舍理由", ["experiment"],
                 {"goal": "对照指标给出结论，说明哪些假设被证伪、哪些仍未验证。",
                  "expected_artifacts": ["成果/商业MVP/复盘结论与取舍.md"]}),
                ("next", "给出继续或转向的决策依据", ["retro"],
                 {"goal": "给出继续、转向或停止的建议，并写清依据与不确定性。",
                  "expected_artifacts": ["成果/商业MVP/继续或转向决策依据.md"]})])])]
    out = []
    for identity, title, description, groups in definitions:
        document = {"schema_version": 1, "format": _FORMAT, "title": title, "description": description,
                    "roots": [{"id": "project", "title": title, "goal": title, "parent_id": None}],
                    "branches": [], "leaves": []}
        for branch, label, leaves in groups:
            document["branches"].append({"id": "branch-" + branch, "title": label, "parent_id": "project"})
            for leaf, task_title, dependencies, *extra in leaves:
                node = {"id": "leaf-" + leaf, "title": task_title,
                        "goal": task_title, "parent_id": "branch-" + branch,
                        "dependencies": ["leaf-" + value for value in dependencies],
                        "handoff": "按目标记录实际成果、未验证项和下一位接手者的具体入口。"}
                # Newer presets may override the goal/handoff and declare expected
                # artifacts. The original three-tuple form keeps its exact output.
                if extra:
                    node.update(extra[0])
                document["leaves"].append(node)
        out.append({"id": identity, "title": title, "description": description,
                    "document": validate_template(document)})
    return out
