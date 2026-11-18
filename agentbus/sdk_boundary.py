"""Source: Codex, 2026-10-04. Full-lifecycle owned-process Codex SDK adapter."""
from __future__ import annotations

import math
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .adapters import AdapterError, Result
from .process_boundary import ProcessBoundary, public_error_text


class _Cancelled(AdapterError):
    pass


def worker_command() -> list[str]:
    if bool(getattr(sys, "frozen", False)):
        return [sys.executable, "_sdk_worker"]
    return [sys.executable, "-B", "-X", "utf8", "-m", "agentbus.sdk_worker"]


class CodexProcessAdapter:
    kind = "codex-sdk"
    persistent_session = True

    def __init__(self, *, sandbox: str = "workspace-write", model: str | None = None,
                 codex_bin: Path | str | None = None,
                 session_record_path: Path | None = None, agent_name: str = "codex",
                 startup_timeout_s: float = 20):
        self.config = {"sandbox": sandbox, "model": model,
                       "codex_bin": str(Path(codex_bin).resolve()) if codex_bin else None,
                       "session_record_path": str(Path(session_record_path).resolve())
                       if session_record_path else None,
                       "agent_name": agent_name}
        self.startup_timeout_s = float(startup_timeout_s)
        if not math.isfinite(self.startup_timeout_s) or self.startup_timeout_s <= 0:
            raise AdapterError("Codex SDK 启动截止时间必须为有限正数")
        self.thread_id: str | None = None
        self.cwd: Path | None = None
        self._serial = threading.Lock()
        self._active_lock = threading.Lock()
        self._active: ProcessBoundary | None = None
        self._generation = 0

    @property
    def process_id(self) -> int | None:
        with self._active_lock:
            return self._active.process_id if self._active else None

    def _request(self, action: str, cwd: Path, timeout_s: float,
                 generation: int, **values) -> tuple[Any, Path]:
        cwd = Path(cwd).resolve()
        if self.cwd is not None and self.cwd != cwd:
            raise AdapterError(f"Codex 会话已绑定工作区 {self.cwd}，不能改用于 {cwd}")
        boundary = ProcessBoundary()
        with self._active_lock:
            if self._generation != generation:
                raise _Cancelled("Codex SDK 等待中的任务已取消")
            self._active = boundary
        try:
            # Source execution must be importable from an arbitrary project.
            # The module root is the child's cwd; the actual workspace is data.
            source_root = Path(__file__).resolve().parent.parent
            response = boundary.exchange(
                worker_command(), {"action": action, "cwd": str(cwd), "config": self.config,
                                   "thread_id": self.thread_id, "timeout_s": timeout_s, **values},
                cwd=source_root, timeout_s=timeout_s)
            return response, cwd
        finally:
            with self._active_lock:
                if self._active is boundary:
                    self._active = None

    def start(self, cwd: Path) -> str:
        started = time.monotonic()
        with self._active_lock:
            generation = self._generation
        if not self._serial.acquire(timeout=self.startup_timeout_s):
            raise AdapterError("Codex SDK 启动等待超时")
        try:
            cwd = Path(cwd).resolve()
            if self.cwd is not None and self.cwd != cwd:
                raise AdapterError(f"Codex 会话已绑定工作区 {self.cwd}，不能改用于 {cwd}")
            with self._active_lock:
                if self._generation != generation:
                    raise _Cancelled("Codex SDK 等待中的启动已取消")
            if self.cwd == cwd and self.thread_id is not None:
                return self.thread_id
            remaining = self.startup_timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                raise AdapterError("Codex SDK 启动等待超时")
            response, cwd = self._request("start", cwd, remaining, generation)
            if response.code == 124:
                raise AdapterError(f"Codex SDK 启动超时（>{self.startup_timeout_s}s），已停止本任务进程树")
            frame = response.frames[-1] if response.frames else {}
            if response.code != 0 or frame.get("type") != "session" or not frame.get("session_id"):
                raise AdapterError(public_error_text(frame.get("message") or response.stderr or
                                                     "Codex SDK worker 未返回会话"))
            if (len(response.frames) != 1 or not isinstance(frame.get("session_id"), str)):
                raise AdapterError("Codex SDK worker 会话协议无效")
            self.thread_id, self.cwd = str(frame["session_id"]), cwd
            return self.thread_id
        finally:
            self._serial.release()

    def run(self, prompt: str, cwd: Path, timeout_s: int = 1800,
            output_schema: Path | None = None, extra: dict[str, Any] | None = None) -> Result:
        del extra
        started = time.monotonic()
        try:
            timeout_s = float(timeout_s)
        except (ValueError, TypeError):
            return Result(1, "Codex SDK 任务截止时间必须为有限正数", 0, retryable=False)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            return Result(1, "Codex SDK 任务截止时间必须为有限正数", 0, retryable=False)
        with self._active_lock:
            generation = self._generation
        if not self._serial.acquire(timeout=timeout_s):
            return Result(124, "Codex SDK 等待执行超时，未启动新任务", time.monotonic() - started)
        try:
            try:
                remaining = timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    return Result(124, "Codex SDK 等待执行超时，未启动新任务", time.monotonic() - started)
                response, cwd = self._request(
                    "run", cwd, remaining, generation, prompt=prompt,
                    output_schema=str(Path(output_schema).resolve()) if output_schema else None)
                if response.code == 124:
                    return Result(124, f"Codex SDK 任务超时（>{timeout_s}s），已停止本任务进程树",
                                  time.monotonic() - started)
                if response.code == 130:
                    return Result(130, "Codex SDK 任务已取消，本任务进程树已停止",
                                  time.monotonic() - started, retryable=False)
                frame = response.frames[-1] if response.frames else {}
                if response.code != 0 or frame.get("type") != "result":
                    return Result(1, public_error_text(frame.get("message") or response.stderr or
                                                       "Codex SDK worker 未返回任务结果"),
                                  time.monotonic() - started,
                                  raw_tail=public_error_text(response.stderr))
                data = frame.get("result")
                if (len(response.frames) != 1 or not isinstance(data, dict)
                        or type(data.get("exit_code")) is not int
                        or not isinstance(data.get("output"), str)
                        or not isinstance(data.get("raw_tail"), str)
                        or type(data.get("retryable")) is not bool
                        or (frame.get("session_id") is not None and
                            (not isinstance(frame["session_id"], str) or not frame["session_id"]))):
                    raise ValueError("Codex SDK worker 结果协议无效")
                if frame.get("session_id"):
                    self.thread_id, self.cwd = str(frame["session_id"]), cwd
                output = data["output"] if data["exit_code"] == 0 else public_error_text(data["output"])
                return Result(data["exit_code"], output,
                              time.monotonic() - started, raw_tail=public_error_text(data["raw_tail"]),
                              retryable=data["retryable"])
            except _Cancelled as exc:
                return Result(130, str(exc), time.monotonic() - started, retryable=False)
            except Exception as exc:
                return Result(1, "Codex SDK worker 失败：" + public_error_text(exc),
                              time.monotonic() - started)
        finally:
            self._serial.release()

    def close(self) -> None:
        with self._active_lock:
            self._generation += 1
            active = self._active
        if active is not None:
            active.close()
