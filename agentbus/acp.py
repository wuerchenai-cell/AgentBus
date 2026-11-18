#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small persistent ACP client used by AgentBus adapters.

One instance owns one ACP subprocess and one conversation. Repeated ``run`` calls
send new ``session/prompt`` requests into that same session; the process does not
poll or invoke a model while idle.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from copy import deepcopy
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .adapters import AdapterError, Result
from .session_store import load_session, save_session
from .silent_debug import note_silent


class ACPAdapter:
    kind = "acp"

    def __init__(self, command: list[str], *,
                 permission_handler: Callable[[dict[str, Any]], str | None] | None = None,
                 on_update: Callable[[dict[str, Any]], None] | None = None,
                 startup_timeout_s: float = 20.0,
                 session_record_path: Path | None = None,
                 agent_name: str = "",
                 mcp_servers: list[dict[str, Any]] | None = None,
                 env: dict[str, str] | None = None):
        if not command or not command[0]:
            raise AdapterError("ACP 适配器缺少启动命令")
        self.command = [str(part) for part in command]
        self.permission_handler = permission_handler
        self.on_update = on_update
        self.startup_timeout_s = max(0.1, float(startup_timeout_s))
        self.session_record_path = Path(session_record_path) \
            if session_record_path is not None else None
        self.agent_name = agent_name
        self.mcp_servers = deepcopy(mcp_servers or [])
        self.env = dict(env) if env is not None else dict(os.environ)
        self.process: subprocess.Popen[str] | None = None
        self.session_id: str | None = None
        self.agent_capabilities: dict[str, Any] = {}
        self.cwd: Path | None = None
        self._reader_queue: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=80)
        self._lock = threading.RLock()
        self._next_id = 1

    @property
    def process_id(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def start(self, cwd: Path) -> str:
        """Start or reconnect the ACP process and return its session ID."""
        cwd = Path(cwd).resolve()
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                if self.cwd != cwd:
                    raise AdapterError(
                        f"ACP 会话已绑定工作区 {self.cwd}，不能改用于 {cwd}")
                if self.session_id:
                    return self.session_id

            previous_session = self.session_id or load_session(
                self.session_record_path, adapter=self.kind, cwd=cwd)
            self._stop_process()
            self.cwd = cwd
            self._launch(cwd)
            try:
                init = self._request(
                    "initialize",
                    {"protocolVersion": 1, "clientCapabilities": {},
                     "clientInfo": {"name": "AgentBus", "title": "AgentBus",
                                    "version": "1.0"}},
                    timeout_s=self.startup_timeout_s,
                )
                self.agent_capabilities = dict(
                    init.get("agentCapabilities") or {})
                self._notify("initialized")

                if (previous_session
                        and self.agent_capabilities.get("loadSession") is True):
                    loaded = self._request(
                        "session/load",
                        {"sessionId": previous_session, "cwd": str(cwd),
                         "mcpServers": deepcopy(self.mcp_servers)},
                        timeout_s=self.startup_timeout_s,
                    )
                    if loaded.get("sessionId") not in (None, previous_session):
                        raise AdapterError("ACP 恢复返回了不匹配的 sessionId")
                    self.session_id = previous_session
                    save_session(self.session_record_path, adapter=self.kind,
                                 agent=self.agent_name, cwd=cwd,
                                 session_id=self.session_id)
                    return self.session_id

                if previous_session:
                    raise AdapterError('ACP 宿主不支持恢复已有会话；未创建替代会话，请重新选择接入方式')
                self.session_id = None
                created = self._request(
                    "session/new", {"cwd": str(cwd),
                                    "mcpServers": deepcopy(self.mcp_servers)},
                    timeout_s=self.startup_timeout_s)
                sid = created.get("sessionId")
                if not isinstance(sid, str) or not sid:
                    raise AdapterError("ACP session/new 没有返回 sessionId")
                self.session_id = sid
                save_session(self.session_record_path, adapter=self.kind,
                             agent=self.agent_name, cwd=cwd,
                             session_id=sid)
                return sid
            except Exception:
                self._stop_process()
                raise

    def run(self, prompt: str, cwd: Path, timeout_s: int = 1800,
            output_schema: Path | None = None,
            extra: dict[str, Any] | None = None) -> Result:
        del extra
        if output_schema is not None:
            try:
                prompt += ("\n\n【输出格式】只输出符合下面 JSON Schema 的 JSON：\n"
                           + Path(output_schema).read_text(encoding="utf-8"))
            except OSError as exc:
                return Result(2, f"无法读取输出 schema：{exc}", 0.0)

        started = time.monotonic()
        try:
            sid = self.start(cwd)
            chunks: list[str] = []
            response = self._request(
                "session/prompt",
                {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
                timeout_s=max(0.1, float(timeout_s)),
                on_notification=lambda message: self._collect_update(message, chunks),
            )
            raw_reason = response.get("stopReason")
            reason = raw_reason if isinstance(raw_reason, str) and raw_reason else "missing_stop_reason"
            code = 0 if reason == "end_turn" else 1
            if reason == "cancelled":
                code = 130
            output = "".join(chunks).strip()
            if not output:
                output = f"ACP turn finished ({reason})"
            # Source: real WorkBuddy ACP probe, 2026-09-30. Its public error
            # extension explains refusals (e.g. Authentication required).
            # Never dump the remaining metadata, which may contain identity.
            if code:
                metadata = response.get("_meta")
                if isinstance(metadata, dict):
                    detail = metadata.get("codebuddy.ai/errorMessage")
                    if isinstance(detail, str) and detail.strip():
                        output += "\nACP 错误：" + detail.strip()[:1200]
            return Result(code, output, time.monotonic() - started,
                          retryable=code == 0)
        except (AdapterError, OSError, subprocess.SubprocessError) as exc:
            timeout = "ACP 请求 session/prompt 超时" in str(exc)
            if timeout:
                # Never release the workspace write lease while a timed-out
                # child agent could still be changing files.
                self._stop_process(force=True)
            return Result(124 if timeout else 1, str(exc), time.monotonic() - started,
                          raw_tail=self._stderr_tail())

    def close(self) -> None:
        with self._lock:
            self._stop_process()

    def _launch(self, cwd: Path) -> None:
        self._reader_queue = queue.Queue()
        self._stderr.clear()
        try:
            popen_options: dict[str, Any] = {}
            if os.name == "nt":
                popen_options["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            else:
                popen_options["start_new_session"] = True
            self.process = subprocess.Popen(
                self.command, cwd=str(cwd), env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                **popen_options)
        except OSError as exc:
            self.process = None
            raise AdapterError(f"无法启动 ACP Agent：{self.command[0]}：{exc}") from exc

        assert self.process.stdout is not None and self.process.stderr is not None
        threading.Thread(target=self._read_stdout, daemon=True,
                         name="agentbus-acp-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         name="agentbus-acp-stderr").start()

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        self._reader_queue.put(message)
                except json.JSONDecodeError:
                    self._reader_queue.put(AdapterError(
                        f"ACP stdout 不是 JSON-RPC：{line[:500]}"))
        except BaseException as exc:  # reader errors must wake the waiting request
            self._reader_queue.put(exc)
        finally:
            self._reader_queue.put(AdapterError("ACP Agent 进程已关闭"))

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                self._stderr.append(line.rstrip())
        except OSError:
            pass  # 资源清理类：进程已退出时读取 stderr 失败，无后续动作可做。

    def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise AdapterError("ACP Agent 进程未运行")
        try:
            # ASCII JSON is valid UTF-8 and preserves Unicode via escapes.
            # Legacy Windows CLI stdin must not reinterpret UTF-8 bytes as GBK.
            process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AdapterError(f"ACP Agent 写入失败：{exc}; {self._stderr_tail()}") from exc

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _request(self, method: str, params: dict[str, Any], timeout_s: float,
                 on_notification: Callable[[dict[str, Any]], None] | None = None
                 ) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": request_id,
                         "method": method, "params": params})
            deadline = time.monotonic() + max(0.1, timeout_s)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterError(
                        f"ACP 请求 {method} 超时（>{timeout_s:g}s）；{self._stderr_tail()}")
                try:
                    message = self._reader_queue.get(timeout=min(0.5, remaining))
                except queue.Empty:
                    process = self.process
                    if process is None or process.poll() is not None:
                        raise AdapterError(
                            f"ACP Agent 已退出（rc={process.returncode if process else 'unknown'}）；"
                            f"{self._stderr_tail()}")
                    continue
                if isinstance(message, BaseException):
                    raise AdapterError(f"ACP 接收失败：{message}; {self._stderr_tail()}")

                if message.get("id") == request_id:
                    if "error" in message:
                        error = message.get("error") or {}
                        raise AdapterError(
                            f"ACP {method} 失败：{error.get('message', error)}")
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise AdapterError(f"ACP {method} 返回了无效结果")
                    return result

                if message.get("method") == "session/request_permission":
                    self._answer_permission(message)
                elif message.get("method"):
                    if on_notification is not None:
                        on_notification(message)
                    if self.on_update is not None:
                        try:
                            self.on_update(message)
                        except Exception:
                            # 功能类：订阅者的 on_update 回调抛错不应中断 ACP 收包循环，
                            # 否则一次 UI 侧异常会静默掐断整个会话流。落一条调试记录。
                            note_silent("acp.py:292", "functional",
                                        "on_update 订阅者回调异常不得中断协议收包循环")
                            pass
                elif "id" in message:
                    self._write({"jsonrpc": "2.0", "id": message["id"],
                                 "error": {"code": -32601,
                                           "message": "ACP client method not supported"}})

    def _answer_permission(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        params = message.get("params") or {}
        options = params.get("options") or []
        chosen: str | None = None
        if self.permission_handler is not None:
            try:
                chosen = self.permission_handler(params)
            except Exception:
                chosen = None
        if chosen is not None and any(
                isinstance(option, dict) and option.get("optionId") == chosen
                for option in options):
            outcome = {"outcome": "selected", "optionId": chosen}
        else:
            reject = next((option.get("optionId") for option in options
                           if isinstance(option, dict)
                           and option.get("kind") == "reject_once"), None)
            outcome = ({"outcome": "selected", "optionId": reject}
                       if reject else {"outcome": "cancelled"})
        self._write({"jsonrpc": "2.0", "id": request_id,
                     "result": {"outcome": outcome}})

    @staticmethod
    def _collect_update(message: dict[str, Any], chunks: list[str]) -> None:
        params = message.get("params") or {}
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                chunks.append(str(content.get("text", "")))
        elif kind == "agent_message":
            content = update.get("content") or []
            if isinstance(content, list):
                chunks.extend(str(block.get("text", ""))
                              for block in content
                              if isinstance(block, dict)
                              and block.get("type") == "text")

    def _stderr_tail(self) -> str:
        return "\n".join(self._stderr)[-2000:]

    def _stop_process(self, *, force: bool = True) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass  # 资源清理类：关闭已断开的 stdin，失败不影响 kill 流程。
        if process.poll() is None and not force:
            try:
                process.terminate()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                force = True
        if process.poll() is None and force:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=5, check=False)
                else:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except OSError:
                    pass  # 资源清理类：进程已不存在，kill 失败属预期。
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass  # 资源清理类：等待回收超时不再阻塞停进程流程。
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass  # 资源清理类：逐个关闭管道，个别失败无碍其余管道释放。
