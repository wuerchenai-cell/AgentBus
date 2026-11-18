# -*- coding: utf-8 -*-
"""开源多智能体成熟方案融合引擎（Open-Source Multi-Agent Patterns Engine）。

本模块将当前开源社区最成熟的 5 项多智能体编程与本地协作设计模式零依赖融入 AgentBus：
1. Git Worktree 并发物理沙箱隔离（借鉴 claude-squad / open-mercato/cezar / agent-worktree）
2. AST 级代码结构地图切片生成 RepoMap（借鉴 Aider RepoMap）
3. 交单前零配置静态语法与验证门禁 Pre-Answer Gate（借鉴 Pullboard / SWE-agent）
4. 任务执行中即时衍生子任务 Discovered-From（借鉴 steveyegge/beads）
5. 踩坑经验自动沉淀与定向匹配注入 Lessons Learned（借鉴 Reflexion / OpenHands）
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_IO_ERRORS = (
    FileNotFoundError,
    PermissionError,
    IsADirectoryError,
    NotADirectoryError,
    UnicodeDecodeError,
    ValueError,
)

_SAFE_TOKEN_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_JS_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+([a-zA-Z0-9_$]+)\s*\(([^)]*)\)|class\s+([a-zA-Z0-9_$]+))",
    re.MULTILINE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_slug(text: str, max_len: int = 36) -> str:
    cleaned = _SAFE_TOKEN_RE.sub("-", str(text or "").strip()).strip("-")
    return (cleaned[:max_len] or "task").lower()


# ============================================================================
# 1. Pre-Answer Verification Gate（借鉴 Pullboard / SWE-agent）
# ============================================================================

def run_pre_answer_gate(
    workspace: Path,
    artifact_paths: list[str],
    verify_cmd: str | list[str] | None = None,
    timeout_s: int = 30,
) -> dict[str, Any]:
    """在 Agent 提交回执前，对所有交付产物执行零配置静态语法门禁与可选测试命令校验。

    - 对 .py 文件执行 ast.parse 语法树检查；
    - 对 .json 文件执行 json.loads 严格语法检查；
    - 若指定了 verify_cmd，则在 workspace 下受限运行并检查返回码。
    """
    ws = Path(workspace).resolve()
    failures: list[dict[str, Any]] = []
    checked_files: list[str] = []

    for rel_raw in artifact_paths or []:
        rel = str(rel_raw or "").strip().replace("\\", "/")
        if not rel:
            continue
        target = (ws / rel).resolve()
        if not target.is_file():
            continue
        checked_files.append(rel)
        suffix = target.suffix.lower()

        if suffix == ".py":
            try:
                source = target.read_text(encoding="utf-8-sig")
                ast.parse(source, filename=rel)
            except UnicodeDecodeError as exc:
                failures.append({
                    "path": rel,
                    "kind": "ENCODING_ERROR",
                    "line": 0,
                    "detail": f"Python 源文件非合法 UTF-8 编码: {exc}",
                })
            except SyntaxError as exc:
                line_txt = (exc.text or "").strip()
                failures.append({
                    "path": rel,
                    "kind": "PYTHON_SYNTAX_ERROR",
                    "line": exc.lineno or 0,
                    "column": exc.offset or 0,
                    "detail": f"第 {exc.lineno} 行语法错误 ({exc.msg}): {line_txt}",
                })
            except _IO_ERRORS as exc:
                failures.append({
                    "path": rel,
                    "kind": "READ_ERROR",
                    "line": 0,
                    "detail": f"读取文件失败: {exc}",
                })

        elif suffix == ".json":
            try:
                raw_txt = target.read_text(encoding="utf-8-sig")
                json.loads(raw_txt)
            except UnicodeDecodeError as exc:
                failures.append({
                    "path": rel,
                    "kind": "ENCODING_ERROR",
                    "line": 0,
                    "detail": f"JSON 文件非合法 UTF-8 编码: {exc}",
                })
            except json.JSONDecodeError as exc:
                failures.append({
                    "path": rel,
                    "kind": "JSON_SYNTAX_ERROR",
                    "line": exc.lineno,
                    "column": exc.colno,
                    "detail": f"JSON 格式损坏 (第 {exc.lineno} 行, 第 {exc.colno} 列): {exc.msg}",
                })
            except _IO_ERRORS as exc:
                failures.append({
                    "path": rel,
                    "kind": "READ_ERROR",
                    "line": 0,
                    "detail": f"读取 JSON 失败: {exc}",
                })

    cmd_result: dict[str, Any] | None = None
    if verify_cmd:
        try:
            if isinstance(verify_cmd, str):
                import shlex
                argv = shlex.split(verify_cmd, posix=False)
            else:
                argv = [str(x) for x in verify_cmd]
            proc = subprocess.run(
                argv,
                cwd=str(ws),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1, int(timeout_s)),
                shell=False,
            )
            cmd_result = {
                "command": argv,
                "returncode": proc.returncode,
                "stdout_tail": (proc.stdout or "")[-800:],
                "stderr_tail": (proc.stderr or "")[-800:],
            }
            if proc.returncode != 0:
                failures.append({
                    "path": "<verify_cmd>",
                    "kind": "VERIFY_CMD_FAILED",
                    "line": 0,
                    "detail": f"验证命令返回码 {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:300]}",
                })
        except (subprocess.SubprocessError, FileNotFoundError, PermissionError, ValueError) as exc:
            failures.append({
                "path": "<verify_cmd>",
                "kind": "VERIFY_CMD_ERROR",
                "line": 0,
                "detail": f"验证命令执行失败: {exc}",
            })

    return {
        "passed": len(failures) == 0,
        "checked_count": len(checked_files),
        "checked_files": checked_files,
        "failures": failures,
        "verify_cmd_result": cmd_result,
    }


# ============================================================================
# 2. AST RepoMap Engine（借鉴 Aider RepoMap）
# ============================================================================

def _extract_python_symbols(file_path: Path, max_symbols: int = 25) -> list[str]:
    """使用标准库 ast 提取 Python 文件中的类定义、方法与顶层函数签名。"""
    try:
        src = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (SyntaxError, ValueError, FileNotFoundError, PermissionError, IsADirectoryError):
        return []

    lines: list[str] = []
    for node in tree.body:
        if len(lines) >= max_symbols:
            break
        if isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases[:3]] if hasattr(ast, "unparse") else []
            base_str = f"({', '.join(bases)})" if bases else ""
            lines.append(f"  class {node.name}{base_str}:  # L{node.lineno}")
            for sub in node.body:
                if len(lines) >= max_symbols:
                    break
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name.startswith("__") and sub.name != "__init__":
                        continue
                    args = [a.arg for a in sub.args.args[:6]]
                    if len(sub.args.args) > 6:
                        args.append("...")
                    lines.append(f"    def {sub.name}({', '.join(args)})  # L{sub.lineno}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args[:6]]
            if len(node.args.args) > 6:
                args.append("...")
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            lines.append(f"  {prefix} {node.name}({', '.join(args)})  # L{node.lineno}")
    return lines


def _extract_js_symbols(file_path: Path, max_symbols: int = 20) -> list[str]:
    """轻量提取 JS/TS 文件中的函数与类签名。"""
    try:
        src = file_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, IsADirectoryError, ValueError):
        return []
    lines: list[str] = []
    for m in _JS_SYMBOL_RE.finditer(src):
        if len(lines) >= max_symbols:
            break
        fn_name, fn_args, cls_name = m.group(1), m.group(2), m.group(3)
        line_no = src.count("\n", 0, m.start()) + 1
        if cls_name:
            lines.append(f"  class {cls_name}  # L{line_no}")
        elif fn_name:
            lines.append(f"  function {fn_name}({(fn_args or '').strip()[:40]})  # L{line_no}")
    return lines


def build_repomap_slice(
    workspace: Path,
    target_paths: list[str] | None = None,
    keywords: list[str] | None = None,
    max_files: int = 6,
    max_chars: int = 2400,
) -> dict[str, Any]:
    """构建面向特定任务的代码结构地图切片（RepoMap Slice）。"""
    ws = Path(workspace).resolve()
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _add_candidate(p: Path) -> None:
        try:
            rp = p.resolve()
        except _IO_ERRORS:
            return
        if rp in seen or not rp.is_file():
            return
        if rp.suffix.lower() not in (".py", ".js", ".ts"):
            return
        if any(part.startswith(".") or part in ("node_modules", "__pycache__", "dist", "build") for part in rp.parts):
            return
        seen.add(rp)
        candidates.append(rp)

    # 1. 优先纳入任务声明的 expected_artifacts 及其同目录关联源码文件
    for raw in target_paths or []:
        rel = str(raw or "").strip().replace("\\", "/")
        if not rel:
            continue
        p = ws / rel
        if p.is_file():
            _add_candidate(p)
        parent = p.parent
        if parent.is_dir() and parent != ws:
            try:
                for sib in sorted(parent.glob("*.py"))[:4]:
                    _add_candidate(sib)
            except _IO_ERRORS:
                continue

    # 2. 按关键词在常见源码目录中检索匹配文件名
    kw_list = [str(k).strip().lower() for k in (keywords or []) if str(k).strip() and len(str(k).strip()) >= 3]
    if len(candidates) < max_files and kw_list:
        search_dirs = [ws, ws / "src", ws / "agentbus", ws / "成果" / "agentbus" / "agentbus"]
        for sdir in search_dirs:
            if not sdir.is_dir():
                continue
            try:
                for f in sorted(sdir.glob("*.py")):
                    fname = f.stem.lower()
                    if any(k in fname or fname in k for k in kw_list):
                        _add_candidate(f)
                        if len(candidates) >= max_files:
                            break
            except _IO_ERRORS:
                continue

    entries: list[dict[str, Any]] = []
    md_lines: list[str] = []
    total_chars = 0

    for fp in candidates[:max_files]:
        try:
            rel_str = fp.relative_to(ws).as_posix()
        except ValueError:
            rel_str = fp.name
        syms = _extract_python_symbols(fp) if fp.suffix.lower() == ".py" else _extract_js_symbols(fp)
        if not syms:
            continue
        block = f"- `{rel_str}`:\n" + "\n".join(syms)
        if total_chars + len(block) > max_chars and md_lines:
            break
        md_lines.append(block)
        total_chars += len(block)
        entries.append({"path": rel_str, "symbols": [s.strip() for s in syms]})

    return {
        "file_count": len(entries),
        "entries": entries,
        "markdown": "\n".join(md_lines) if md_lines else "",
    }


# ============================================================================
# 3. Lessons Learned Memory（借鉴 Reflexion / OpenHands）
# ============================================================================

def _lessons_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".agentbus" / "lessons.jsonl"


def record_lesson(
    workspace: Path,
    agent: str,
    lesson: str,
    task_id: str = "",
    scope_paths: list[str] | None = None,
    kind: str = "insight",
) -> dict[str, Any]:
    """向 .agentbus/lessons.jsonl 追加一条实战避坑经验。"""
    text = str(lesson or "").strip()
    if not text:
        raise ValueError("经验内容不能为空")
    path = _lessons_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    scopes = [str(p).strip().replace("\\", "/") for p in (scope_paths or []) if str(p).strip()]
    item = {
        "id": f"les-{int(time.time() * 1000)}",
        "ts": _now_iso(),
        "agent": str(agent or "system").strip(),
        "task_id": str(task_id or "").strip(),
        "kind": str(kind or "insight").strip(),
        "scope_paths": scopes[:10],
        "lesson": text[:1000],
    }
    line = json.dumps(item, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    return item


def list_lessons(workspace: Path, limit: int = 50) -> list[dict[str, Any]]:
    """读取最近登记的实战避坑经验。"""
    path = _lessons_path(workspace)
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = []
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except _IO_ERRORS:
        return []
    for line in raw_lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict) and obj.get("lesson"):
                items.append(obj)
        except json.JSONDecodeError:
            continue
    return items[-max(1, int(limit)):]


def query_relevant_lessons(
    workspace: Path,
    target_paths: list[str] | None = None,
    keywords: list[str] | None = None,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """根据当前任务的产物路径或关键词匹配最相关的历史避坑经验。"""
    all_items = list_lessons(workspace, limit=200)
    if not all_items:
        return []
    targets = [str(p).strip().replace("\\", "/").lower() for p in (target_paths or []) if str(p).strip()]
    target_dirs = {p.rsplit("/", 1)[0] for p in targets if "/" in p}
    kws = [str(k).strip().lower() for k in (keywords or []) if str(k).strip() and len(str(k).strip()) >= 2]

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for idx, item in enumerate(all_items):
        score = 0
        scopes = [str(s).lower() for s in item.get("scope_paths", [])]
        lesson_lower = str(item.get("lesson", "")).lower()
        for t in targets:
            if any(t == s or t.startswith(s + "/") or s.startswith(t + "/") for s in scopes):
                score += 10
            elif any(d and s.startswith(d) for d in target_dirs for s in scopes):
                score += 5
        for kw in kws:
            if kw in lesson_lower:
                score += 3
        scored.append((score, idx, item))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [x[2] for x in scored[:max(1, int(limit))]]


# ============================================================================
# 4. Beads-Style Discovered-From Task Spawning（借鉴 steveyegge/beads）
# ============================================================================

def discover_subtask(
    cfg: Any,
    agent: str,
    title: str,
    goal: str = "",
    from_task_id: str | None = None,
    expected_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    """在执行当前任务期间发现新问题时，动态在同分支下衍生新待办叶子任务并记录 discovered_from 溯源。"""
    from .task_tree import TaskForest
    from .bus import Bus

    clean_title = str(title or "").strip()
    if not clean_title:
        raise ValueError("衍生任务标题不能为空")

    forest = TaskForest(cfg)
    snap = forest.snapshot()
    leaves = snap.get("leaves", [])
    branches = snap.get("branches", [])

    source_leaf = None
    ref = str(from_task_id or "").strip()
    if ref:
        for l in leaves:
            if l.get("id") == ref or l.get("section_no") == ref or l.get("task_id") == ref:
                source_leaf = l
                break
    if source_leaf is None and agent:
        for l in leaves:
            if l.get("owner") == agent and l.get("state") in ("running", "claimed", "dispatched", "awaiting_receipt"):
                source_leaf = l
                break

    if source_leaf is not None:
        parent_id = source_leaf.get("parent_id")
        origin_id = source_leaf.get("id")
        origin_section = source_leaf.get("section_no") or origin_id
    elif branches:
        parent_id = branches[0]["id"]
        origin_id = "manual-discovery"
        origin_section = "root"
    else:
        raise ValueError("当前任务树尚无分支，无法挂载衍生任务")

    # 计算该分支下的下一个章节号
    sib_sections = [
        str(l.get("section_no") or "")
        for l in leaves
        if l.get("parent_id") == parent_id and l.get("section_no")
    ]
    prefix = "1"
    max_sub = 0
    for sec in sib_sections:
        parts = sec.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[-1].isdigit():
            prefix = ".".join(parts[:-1])
            max_sub = max(max_sub, int(parts[-1]))
    next_section = f"{prefix}.{max_sub + 1}"
    existing_sections = {str(l.get("section_no") or "") for l in leaves}
    while next_section in existing_sections:
        max_sub += 1
        next_section = f"{prefix}.{max_sub + 1}"

    new_leaf_id = f"DISC-{int(time.time() * 1000) % 1000000:06d}"
    existing_ids = {str(l.get("id") or "") for l in leaves}
    while new_leaf_id in existing_ids:
        new_leaf_id = f"DISC-{int(time.time() * 1000 + 1) % 1000000:06d}"

    full_goal = str(goal or clean_title).strip()
    full_goal += f"\n\n[溯源: 由 {agent} 在执行 {origin_section} ({origin_id}) 期间发现并衍生 (discovered_from={origin_id})]"

    arts = [str(a).strip().replace("\\", "/") for a in (expected_artifacts or []) if str(a).strip()]
    leaf_payload = {
        "id": new_leaf_id,
        "parent_id": parent_id,
        "section_no": next_section,
        "title": clean_title,
        "goal": full_goal,
        "dependencies": [],
        "expected_artifacts": arts,
        "discovered_from": origin_id,
        "discovered_by": agent,
    }
    new_snap = forest.seed_leaf(leaf_payload, branch_id=parent_id)
    added = next((l for l in new_snap.get("leaves", []) if l.get("id") == new_leaf_id), leaf_payload)
    bus = Bus(cfg.workspace / ".agentbus")
    bus.audit(
        event="task_discovered",
        agent=agent,
        leaf_id=new_leaf_id,
        section_no=next_section,
        discovered_from=origin_id,
        title=clean_title,
    )
    return {
        "ok": True,
        "leaf": added,
        "leaf_id": new_leaf_id,
        "section_no": next_section,
        "discovered_from": origin_id,
        "discovered_by": agent,
    }


# ============================================================================
# 5. Git Worktree Sandbox Isolation（借鉴 claude-squad / cezar / agent-worktree）
# ============================================================================

def _worktree_registry_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".agentbus" / "worktrees" / "registry.json"


def _load_worktree_registry(workspace: Path) -> dict[str, dict[str, Any]]:
    reg_path = _worktree_registry_path(workspace)
    if not reg_path.is_file():
        return {}
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, FileNotFoundError, PermissionError, UnicodeDecodeError):
        return {}


def _save_worktree_registry(workspace: Path, reg: dict[str, dict[str, Any]]) -> None:
    reg_path = _worktree_registry_path(workspace)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_task_worktree(
    workspace: Path,
    agent: str,
    task_id: str,
    leaf_id: str = "",
) -> dict[str, Any]:
    """为指定 Agent 与任务创建物理隔离的 Git Worktree 沙箱目录。

    若当前目录是 Git 仓库且支持 git worktree，则创建 `.agentbus/worktrees/<agent>-<slug>`；
    若非 Git 仓库，则创建轻量沙箱目录结构并记录映射。
    """
    ws = Path(workspace).resolve()
    slug = _safe_slug(leaf_id or task_id, 28)
    agent_slug = _safe_slug(agent, 20)
    wt_dir = ws / ".agentbus" / "worktrees" / f"{agent_slug}-{slug}"
    branch_name = f"agentbus/{agent_slug}/{slug}"

    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    git_backed = False
    note = ""

    try:
        chk = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if chk.returncode == 0:
            if wt_dir.exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_dir)],
                    cwd=str(ws),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            add_proc = subprocess.run(
                ["git", "worktree", "add", "-B", branch_name, str(wt_dir), "HEAD"],
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if add_proc.returncode == 0 and wt_dir.is_dir():
                git_backed = True
                note = f"已挂载 Git Worktree 独立分支 {branch_name}"
            else:
                note = f"Git Worktree 回退为本地隔离目录 ({(add_proc.stderr or '').strip()[:120]})"
    except (subprocess.SubprocessError, FileNotFoundError, PermissionError):
        note = "当前环境未启用 Git Worktree，已创建本地隔离沙箱目录"

    if not git_backed:
        wt_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "task_id": str(task_id),
        "leaf_id": str(leaf_id or task_id),
        "agent": str(agent),
        "worktree_path": str(wt_dir),
        "branch": branch_name if git_backed else "",
        "git_backed": git_backed,
        "created_at": _now_iso(),
        "status": "active",
        "note": note,
    }
    reg = _load_worktree_registry(ws)
    reg[str(task_id)] = entry
    _save_worktree_registry(ws, reg)
    return entry


def sync_and_cleanup_worktree(
    workspace: Path,
    task_id: str,
    artifact_paths: list[str] | None = None,
    cleanup: bool = True,
) -> dict[str, Any]:
    """将任务沙箱 Worktree 中的产物文件同步回主工作区，并可选清理 Worktree。"""
    ws = Path(workspace).resolve()
    reg = _load_worktree_registry(ws)
    entry = reg.get(str(task_id))
    if not entry or entry.get("status") not in ("active", "synced"):
        return {"synced": False, "copied_files": [], "reason": "no_active_worktree"}

    wt_dir = Path(entry["worktree_path"])
    if not wt_dir.is_dir():
        entry["status"] = "cleaned"
        _save_worktree_registry(ws, reg)
        return {"synced": False, "copied_files": [], "reason": "worktree_dir_missing"}

    copied: list[str] = []
    rels_to_sync: set[str] = set()
    for raw in artifact_paths or []:
        rel = str(raw or "").strip().replace("\\", "/")
        if rel:
            rels_to_sync.add(rel)

    # 如果是 git worktree，额外通过 git status --porcelain 发现修改/新增文件
    if entry.get("git_backed"):
        try:
            st = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(wt_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if st.returncode == 0:
                for line in st.stdout.splitlines():
                    if len(line) > 3:
                        rel_p = line[3:].strip().strip('"')
                        if rel_p and not rel_p.startswith(".agentbus"):
                            rels_to_sync.add(rel_p)
        except (subprocess.SubprocessError, FileNotFoundError, PermissionError):
            rels_to_sync = rels_to_sync

    for rel in sorted(rels_to_sync):
        src_file = (wt_dir / rel).resolve()
        dst_file = (ws / rel).resolve()
        if not src_file.is_file() or not src_file.is_relative_to(wt_dir.resolve()):
            continue
        if not dst_file.is_relative_to(ws):
            continue
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)
        copied.append(rel)

    if cleanup:
        if entry.get("git_backed"):
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_dir)],
                    cwd=str(ws),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (subprocess.SubprocessError, FileNotFoundError, PermissionError):
                shutil.rmtree(wt_dir, ignore_errors=True)
        else:
            shutil.rmtree(wt_dir, ignore_errors=True)
        entry["status"] = "synced_and_cleaned"
    else:
        entry["status"] = "synced"

    entry["synced_at"] = _now_iso()
    entry["copied_files"] = copied
    _save_worktree_registry(ws, reg)
    return {
        "synced": True,
        "copied_files": copied,
        "status": entry["status"],
        "worktree_path": str(wt_dir),
    }


def list_worktrees(workspace: Path) -> list[dict[str, Any]]:
    """列出当前工作区登记的所有任务沙箱 Worktree。"""
    reg = _load_worktree_registry(workspace)
    items = list(reg.values())
    for item in items:
        wt_p = Path(item.get("worktree_path", ""))
        item["exists"] = wt_p.is_dir() if item.get("worktree_path") else False
    return items
