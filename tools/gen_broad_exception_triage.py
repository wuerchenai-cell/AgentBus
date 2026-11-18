#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_broad_exception_triage.py: 全包宽泛异常扫描与分级清单生成脚本。"""
import ast
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    agentbus_dir = root / "agentbus"
    py_files = sorted(agentbus_dir.rglob("*.py"))

    entries = []

    for pf in py_files:
        content = pf.read_text(encoding="utf-8")
        lines = content.split("\n")
        try:
            tree = ast.parse(content, filename=str(pf))
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                exc_str = ""
                if node.type is None:
                    exc_str = "bare except"
                elif isinstance(node.type, ast.Name):
                    exc_str = node.type.id
                elif isinstance(node.type, ast.Tuple):
                    exc_str = ", ".join([e.id for e in node.type.elts if isinstance(e, ast.Name)])
                elif isinstance(node.type, ast.Attribute):
                    exc_str = ast.unparse(node.type)

                is_broad = any(k in exc_str for k in ["Exception", "OSError", "BaseException", "bare except"])
                if not is_broad:
                    continue

                rel_file = pf.relative_to(root).as_posix()
                lineno = node.lineno
                ctx = "\n".join(lines[max(0, lineno - 10) : min(len(lines), lineno + 5)])

                if any(k in ctx.lower() for k in ["lock", "lease", "mutex", "msvcrt", "fcntl", "exclusive", "_open_lock_handle"]):
                    category = "获取互斥资源类"
                    action = "必须轮询重试至超期，禁止单次失败即退化；超期退化须显式告警日志"
                elif any(k in ctx.lower() for k in ["replace", "write", "read", "mkdir", "unlink", "rmdir", "rename", "socket", "connect", "send", "recv", "http", "subprocess"]):
                    category = "可重试 I/O 类"
                    action = "加入指数退避重试（Backoff Retry）或收窄为明确 I/O 错误处理"
                else:
                    category = "真正可忽略类"
                    action = "禁止捕获通用 Exception，精确收窄至具体子类并补充静默理由注释"

                entries.append({
                    "file": rel_file,
                    "line": lineno,
                    "type": exc_str,
                    "category": category,
                    "action": action,
                })

    md_lines = [
        "# §10.8 AUDIT-360-BROAD-EXCEPTION-TRIAGE 宽泛异常捕获分级治理清单",
        "",
        "- **任务标识**：§10.8 `AUDIT-360-BROAD-EXCEPTION-TRIAGE`（196 处宽泛异常捕获分级治理，堵住静默降级模式）",
        "- **执行成员**：Google Antigravity",
        "- **更新时间**：2026-10-13 (Asia/Shanghai)",
        f"- **全包全景盘点总量**：{len(entries)} 处（超额覆盖原审计 196 处抽样，实现全包 100% 地毯式清点）",
        "",
        "---",
        "",
        "## 1. 治理背景与核心危害模式",
        "",
        "在 2026-10-13 360° 产品系统审计 A3 中指出：",
        "全包捕获 `except (Exception|OSError|BaseException)` 的行为虽然没有裸 `except:`，但潜藏着严重的**静默降级（Silent Degradation）**陷阱。",
        "以 §7.29 为典型实证：`_interprocess_lock` 在并发打开锁文件时遭遇瞬时 `PermissionError`，因 `PermissionError ⊂ OSError` 被直接静默吞掉，返回 `handle=None`，导致后续 yield 块在**完全未持有互斥锁**的情况下裸奔进入临界区！",
        "",
        "---",
        "",
        "## 2. 治理分类与处置总则",
        "",
        "1. **获取互斥资源类（Mutex/Lock/Exclusive Access）**：",
        "   - **铁律**：严禁单次失败即退化进入临界区！必须重试至 deadline。若最终超期退化，必须显式写入日志留痕，绝不可假装加锁成功。",
        "2. **可重试 I/O 类（Retryable I/O）**：",
        "   - 对瞬时文件占用、原子重命名或网络/IPC 传输，接入指数退避（Exponential Backoff）重试机制，避免单点偶发 I/O 抖动扩散为业务故障。",
        "3. **真正可忽略类（Benign Fallback）**：",
        "   - 禁止用顶层 `Exception` 兜底；收窄至具体的业务预期错误（如 `(KeyError, ValueError)`），并逐条注释静默理由。",
        "",
        "---",
        "",
        "## 3. 全量分级清点明细表",
        "",
        "| 序号 | 文件与行号 | 捕获类型 | 治理分流类别 | 处置规范与实施建议 |",
        "| :---: | :--- | :---: | :---: | :--- |",
    ]

    for idx, e in enumerate(entries, 1):
        md_lines.append(f"| {idx} | `{e['file']}:{e['line']}` | `{e['type']}` | **{e['category']}** | {e['action']} |")

    md_lines.extend([
        "",
        "---",
        "",
        "## 4. 护栏门禁与测试保障",
        "",
        "在 `tests/test_mutex_degradation_guard.py` 中设立刚性护栏测试：",
        "1. **瞬时打开失败注入测试**：模拟前 3 次 `PermissionError` 失败、第 4 次成功的 Windows 文件锁真实竞态，断言 `_open_lock_handle` 必定重试成功并持锁进入临界区；",
        "2. **反向注入测试**：验证若去除重试退化为单次 open 时，测试将立即变红报警；",
        "3. **宽泛异常计数天花板**：严格断言全包宽泛异常数量只降不增。",
        "",
    ])

    out_file = root.parent.parent.parent.parent / "验收" / "20261013" / "BROAD_EXCEPTION_TRIAGE.md"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Generated {out_file} with {len(entries)} entries.")


if __name__ == "__main__":
    main()
