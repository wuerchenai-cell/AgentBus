#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistent Codex sessions backed by the official openai-codex Python SDK."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .adapters import AdapterError, Result
from .mcp_bridge import codex_mcp_config_overrides
from .session_store import load_session, save_session


_SANDBOX_NAMES = {
    "read-only": "read_only",
    "workspace-read": "read_only",
    "workspace-write": "workspace_write",
    "danger-full-access": "full_access",
}


def _resolve_sdk_codex_bin(configured: Path | None) -> Path | None:
    """Recover a removed desktop-managed binary without replacing custom paths."""
    # Source: Codex internal regression review, 2026-10-04. No private config reads.
    if configured is None:
        if not getattr(sys, 'frozen', False) or importlib.util.find_spec('codex_cli_bin') is not None:
            return None
        from .adapters import resolve_codex
        installed = resolve_codex()
        if installed and installed.is_file() and (os.name != 'nt' or installed.suffix.lower() == '.exe'):
            return installed
        raise AdapterError('请安装官方 Codex CLI，并在团队接入中选择 codex.exe。')
    if configured.is_file():
        return configured
    local = os.environ.get("LOCALAPPDATA")
    if local:
        binroot = Path(local) / "OpenAI" / "Codex" / "bin"

        def managed(path: Path) -> bool:
            return (binroot.is_absolute() and path.is_absolute()
                    and path.parent.parent == binroot
                    and path.name.lower() == "codex.exe"
                    and re.fullmatch(r"[0-9a-fA-F]{8,64}", path.parent.name) is not None)

        if managed(configured):
            current = shutil.which("codex")
            if current and managed(Path(current)) and Path(current).is_file():
                return Path(current)
            candidates: list[tuple[float, Path]] = []
            try:
                for path in binroot.glob("*/codex.exe"):
                    if managed(path) and path.is_file():
                        try:
                            candidates.append((path.stat().st_mtime, path))
                        except OSError:
                            continue  # A desktop update may remove a candidate mid-scan.
            except OSError:
                pass  # 资源清理类：bin 扫描目录不可读时跳过，继续其他解析策略。
            if candidates:
                return max(candidates, key=lambda item: item[0])[1]
    raise AdapterError(f"配置的 Codex 程序不存在：{configured}；请在团队接入中选择当前程序")


class CodexSDKAdapter:
    kind = "codex-sdk"
    persistent_session = True

    def __init__(self, *, sandbox: str = "workspace-write",
                 model: str | None = None, codex_bin: Path | str | None = None,
                 session_record_path: Path | None = None,
                 agent_name: str = "codex",
                 sdk_factory: Callable[..., Any] | None = None,
                 sandbox_resolver: Callable[[str], Any] | None = None,
                 approval_resolver: Callable[[], Any] | None = None,
                 interrupt_grace_s: float = 5.0):
        self.sandbox_name = sandbox
        self.model = model
        self.codex_bin = Path(codex_bin) if codex_bin else None
        self.session_record_path = Path(session_record_path) \
            if session_record_path is not None else None
        self.agent_name = agent_name
        self.sdk_factory = sdk_factory
        self.sandbox_resolver = sandbox_resolver
        self.approval_resolver = approval_resolver
        self.interrupt_grace_s = max(0.1, float(interrupt_grace_s))
        self.codex: Any = None
        self.thread: Any = None
        self.thread_id: str | None = None
        self.cwd: Path | None = None
        self._lock = threading.RLock()

    @property
    def process_id(self) -> int | None:
        proc = getattr(getattr(self.codex, "_client", None), "_proc", None)
        return getattr(proc, "pid", None)

    def _sandbox(self) -> Any:
        if self.sandbox_resolver is not None:
            return self.sandbox_resolver(self.sandbox_name)
        try:
            from openai_codex import Sandbox
        except ImportError as exc:
            raise AdapterError(
                "缺少官方 Codex Python SDK openai-codex；请从 AgentBus 正常构建环境安装") from exc
        attr = _SANDBOX_NAMES.get(self.sandbox_name)
        if attr is None or not hasattr(Sandbox, attr):
            raise AdapterError(f"不支持的 Codex 沙箱设置：{self.sandbox_name}")
        return getattr(Sandbox, attr)

    def _new_codex(self, cwd: Path):
        if self.sdk_factory is not None:
            return self.sdk_factory()
        try:
            from openai_codex import Codex, CodexConfig
        except ImportError as exc:
            raise AdapterError(
                "缺少官方 Codex Python SDK openai-codex；请从 AgentBus 正常构建环境安装") from exc
        codex_bin = _resolve_sdk_codex_bin(self.codex_bin)
        config = CodexConfig(
            codex_bin=str(codex_bin) if codex_bin else None,
            config_overrides=codex_mcp_config_overrides(
                cwd, self.agent_name),
        )
        return Codex(config=config)

    def _approval_mode(self) -> Any:
        """Deny escalation explicitly; SDK defaults otherwise allow auto review."""
        # Source: local official SDK 0.157.1 audit, Codex, 2026-10-04.
        if self.approval_resolver is not None:
            return self.approval_resolver()
        try:
            from openai_codex import ApprovalMode
            return ApprovalMode.deny_all
        except (ImportError, AttributeError) as exc:
            raise AdapterError("官方 Codex SDK 缺少 deny_all 审批模式；拒绝自动运行") from exc

    def start(self, cwd: Path) -> str:
        cwd = Path(cwd).resolve()
        with self._lock:
            if self.thread is not None:
                if self.cwd != cwd:
                    raise AdapterError(
                        f"Codex 会话已绑定工作区 {self.cwd}，不能改用于 {cwd}")
                return str(self.thread_id)
            approval_mode = self._approval_mode()
            if self.codex is None:
                self.codex = self._new_codex(cwd)
            self.cwd = cwd
            sandbox = self._sandbox()
            previous_id = self.thread_id or load_session(
                self.session_record_path, adapter=self.kind, cwd=cwd)
            try:
                if previous_id:
                    thread = self.codex.thread_resume(
                        previous_id, cwd=str(cwd), sandbox=sandbox,
                        approval_mode=approval_mode)
                    self.thread_id = previous_id
                else:
                    options: dict[str, Any] = {
                        "cwd": str(cwd), "sandbox": sandbox,
                        "approval_mode": approval_mode,
                    }
                    if self.model:
                        options["model"] = self.model
                    thread = self.codex.thread_start(**options)
                    self.thread_id = str(thread.id)
                self.thread = thread
                save_session(self.session_record_path, adapter=self.kind,
                             agent="codex", cwd=cwd,
                             session_id=str(self.thread_id))
                return str(self.thread_id)
            except Exception as exc:  # noqa: BLE001
                diagnostic = self._runtime_tail()
                self._close_codex()
                message = f"Codex SDK 无法建立或恢复会话：{exc}"
                if diagnostic:
                    message += f"\n运行时诊断：{diagnostic}"
                failure = AdapterError(message)
                failure.raw_tail = diagnostic
                raise failure from exc

    def run(self, prompt: str, cwd: Path, timeout_s: int = 1800,
            output_schema: Path | None = None,
            extra: dict[str, Any] | None = None) -> Result:
        del extra
        started = time.monotonic()
        try:
            cwd = Path(cwd).resolve()
            self.start(cwd)
            kwargs: dict[str, Any] = {"cwd": str(cwd), "sandbox": self._sandbox(),
                                      "approval_mode": self._approval_mode()}
            if output_schema is not None:
                kwargs["output_schema"] = json.loads(
                    Path(output_schema).read_text(encoding="utf-8"))
            turn = self.thread.turn(prompt, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return Result(1, str(exc), time.monotonic() - started,
                          raw_tail=getattr(exc, "raw_tail", "") or self._runtime_tail())

        finished = threading.Event()
        outcome: dict[str, Any] = {}

        def collect() -> None:
            try:
                outcome["result"] = turn.run()
            except BaseException as exc:  # model/network errors flow to caller
                outcome["error"] = exc
            finally:
                finished.set()

        worker = threading.Thread(target=collect, daemon=True, name="agentbus-codex-turn")
        worker.start()
        if not finished.wait(max(0.1, float(timeout_s))):
            interrupt_error = ""
            try:
                turn.interrupt()
            except Exception as exc:
                interrupt_error = str(exc)[:1000]
            diagnostic = self._runtime_tail()
            if interrupt_error or not finished.wait(self.interrupt_grace_s):
                # A timed-out writable turn must not outlive the workspace lease.
                self._close_codex()
            message = f"Codex SDK 任务超时（>{timeout_s}s），已请求中断"
            if interrupt_error:
                message = (f"Codex SDK 任务超时（>{timeout_s}s）；"
                           f"中断请求失败：{interrupt_error}；已请求关闭会话")
            return Result(124, message,
                          time.monotonic() - started, raw_tail=diagnostic)
        if "error" in outcome:
            return Result(1, f"Codex SDK 任务失败：{outcome['error']}",
                          time.monotonic() - started, raw_tail=self._runtime_tail())

        result = outcome.get("result")
        error = getattr(result, "error", None)
        status = getattr(result, "status", None)
        final = getattr(result, "final_response", None)
        text = str(final or "").strip()
        status_name = str(getattr(status, "type", status) or "")
        code = 0 if error is None and status_name.lower() not in {
            "failed", "interrupted", "cancelled", "canceled"} else 1
        if error is not None:
            text = (text + "\n" if text else "") + str(error)
        return Result(code, text or "Codex turn finished", time.monotonic() - started,
                      raw_tail=self._runtime_tail() if code else "")

    def _runtime_tail(self) -> str:
        """Keep bounded diagnostics from our SDK-owned child before it closes."""
        client = getattr(self.codex, "_client", None)
        reader = getattr(client, "_stderr_tail", None)
        if not callable(reader):
            return ""
        try:
            return str(reader())[-2000:]
        except Exception:
            return ""

    def close(self) -> None:
        with self._lock:
            self._close_codex()

    def _close_codex(self) -> None:
        codex, self.codex = self.codex, None
        self.thread = None
        if codex is not None:
            close = getattr(codex, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass  # 资源清理类：SDK close() 失败时交由进程退出回收。
