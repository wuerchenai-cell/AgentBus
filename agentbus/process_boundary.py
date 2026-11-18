"""Source: Codex, 2026-10-04. Own one worker tree for a finite exchange."""
from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Source: Codex boundary audit, 2026-10-04. Limits apply while reading,
# not after communicate() has already accumulated an arbitrary byte stream.
MAX_PROTOCOL_BYTES = 4 * 1024 * 1024
MAX_PROTOCOL_FRAMES = 8
STDERR_TAIL_BYTES = 8192
_READ_BYTES = 8192


def public_error_text(value: Any, *, limit: int = 2000) -> str:
    """Publish bounded, scrubbed diagnostics; never call this on success bodies.

    Local health exports use an allowlist instead of free-form diagnostics.
    SDK failures still need a short vendor error, so recognize credential
    headers/assignments and common standalone tokens before bounding output.
    This does not claim that arbitrary unlabelled text can be classified.
    """
    text = str(value)
    truncated = len(text) > 32768
    text = text[:32768]
    text = re.sub(
        r'''(?im)((?:["']?)(?:authorization|proxy-authorization|cookie|set-cookie)["']?\s*[:=]\s*)[^\r\n]*''',
        r"\1[redacted]", text)
    text = re.sub(
        r'''(?i)((?<![\w-])["']?(?:api[-_ ]?key|x-api-key|access[-_ ]?token|refresh[-_ ]?token|id[-_ ]?token|token|password|passwd|client[-_ ]?secret|secret|session(?:id)?|x-auth-token)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s;,\r\n]+)''',
        r"\1[redacted]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{10,}\b", "[redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
                  "[redacted]", text)
    text = re.sub(r"(?i)(https?://)[^\s/:]+:[^\s@]+@", r"\1[redacted]@", text)
    if truncated:
        text += " [diagnostic truncated]"
    return text[:limit]


class WindowsJob:
    """Attach a suspended worker before it can create any descendants."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.c = ctypes
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]
        class Limits(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("tid", w.DWORD),
                        ("pid", w.DWORD), ("base_priority", w.LONG), ("delta_priority", w.LONG),
                        ("flags", w.DWORD)]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                        ("user", "kernel", "period_user", "period_kernel")] + [
                        (name, w.DWORD) for name in
                        ("faults", "total", "active", "terminated")]
        self.ThreadEntry, self.Accounting = ThreadEntry, Accounting
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                            ctypes.c_void_p], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "Thread32Next": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            getattr(self.k, name).argtypes = arguments
            getattr(self.k, name).restype = result
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway
        if not self.k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def attach_and_resume(self, proc: subprocess.Popen) -> None:
        if not self.k.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise self.c.WinError(self.c.get_last_error())
        snapshot = self.k.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == self.c.c_void_p(-1).value:
            raise self.c.WinError(self.c.get_last_error())
        try:
            entry = self.ThreadEntry()
            entry.size = self.c.sizeof(entry)
            more = self.k.Thread32First(snapshot, self.c.byref(entry))
            while more:
                if entry.pid == proc.pid:
                    thread = self.k.OpenThread(0x2, False, entry.tid)
                    if not thread:
                        raise self.c.WinError(self.c.get_last_error())
                    try:
                        previous = self.k.ResumeThread(thread)
                        if previous == 0xffffffff:
                            raise self.c.WinError(self.c.get_last_error())
                        if previous != 1:
                            raise RuntimeError("Worker was not suspended exactly once")
                    finally:
                        self.k.CloseHandle(thread)
                    return
                more = self.k.Thread32Next(snapshot, self.c.byref(entry))
            raise RuntimeError("Could not locate suspended worker thread")
        finally:
            self.k.CloseHandle(snapshot)

    def close(self) -> None:
        if not self.handle:
            return
        handle, self.handle = self.handle, None
        try:
            if not self.k.TerminateJobObject(handle, 1):
                raise self.c.WinError(self.c.get_last_error())
            deadline = time.monotonic() + 2
            while True:
                info = self.Accounting()
                if not self.k.QueryInformationJobObject(
                        handle, 1, self.c.byref(info), self.c.sizeof(info), None):
                    raise self.c.WinError(self.c.get_last_error())
                if info.active == 0:
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError("Owned worker tree did not stop within cleanup deadline")
                time.sleep(0.01)
        finally:
            self.k.CloseHandle(handle)


@dataclass
class Exchange:
    code: int
    frames: list[dict[str, Any]]
    stderr: str
    duration_s: float


class ProcessBoundary:
    """Cancellation/timeout kills only the process tree created by this object."""
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.job: WindowsJob | None = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def process_id(self) -> int | None:
        proc = self.proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def exchange(self, command: list[str], request: dict[str, Any], *,
                 cwd: Path, timeout_s: float) -> Exchange:
        started = time.monotonic()
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Worker deadline must be positive")
        code = 1
        stdout, stderr = bytearray(), bytearray()
        stderr_truncated = False
        output_overflow = threading.Event()
        changed = threading.Event()
        io_errors: list[str] = []
        threads: list[threading.Thread] = []
        completed: list[threading.Event] = []
        try:
            with self._lock:
                if self._closed:
                    return Exchange(130, [], "Worker cancelled before startup", 0)
                self.job = WindowsJob() if os.name == "nt" else None
                self.proc = subprocess.Popen(
                    command, cwd=str(cwd), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=(subprocess.CREATE_NO_WINDOW | 0x4 if self.job else 0),
                    start_new_session=os.name != "nt",
                )
                if self.job:
                    self.job.attach_and_resume(self.proc)
            payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")

            def read_pipe(stream, *, protocol: bool) -> None:
                nonlocal stderr_truncated
                while True:
                    chunk = stream.read1(_READ_BYTES)
                    if not chunk:
                        return
                    if protocol:
                        if len(stdout) + len(chunk) > MAX_PROTOCOL_BYTES:
                            output_overflow.set()
                            changed.set()
                            return
                        stdout.extend(chunk)
                    else:
                        stderr.extend(chunk)
                        if len(stderr) > STDERR_TAIL_BYTES:
                            stderr_truncated = True
                            del stderr[:-STDERR_TAIL_BYTES]

            def send_request() -> None:
                # A worker may never read stdin; writing in the caller would
                # otherwise bypass its finite deadline just like communicate.
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
                self.proc.stdin.close()

            def launch(target, name: str) -> None:
                done = threading.Event()
                completed.append(done)

                def collect() -> None:
                    try:
                        target()
                    except (OSError, ValueError) as exc:
                        # Pipe races during cancellation are expected. The
                        # final code below gives cancellation precedence.
                        io_errors.append(type(exc).__name__)
                    finally:
                        done.set()
                        changed.set()

                thread = threading.Thread(target=collect, name=name, daemon=True)
                threads.append(thread)
                thread.start()

            launch(lambda: read_pipe(self.proc.stdout, protocol=True), "agentbus-worker-stdout")
            launch(lambda: read_pipe(self.proc.stderr, protocol=False), "agentbus-worker-stderr")
            launch(send_request, "agentbus-worker-stdin")
            deadline = started + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                with self._lock:
                    cancelled = self._closed
                if cancelled:
                    code = 130
                    break
                if output_overflow.is_set():
                    code = 1
                    break
                if remaining <= 0:
                    code = 124
                    break
                if all(done.is_set() for done in completed) and self.proc.poll() is not None:
                    code = 1 if io_errors else self.proc.returncode
                    break
                changed.wait(min(remaining, 0.02))
                changed.clear()
        finally:
            self.close()
            for thread in threads:
                thread.join(timeout=0.5)
            proc = self.proc
            if proc is not None:
                for stream in (proc.stdin, proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()
        frames = []
        if code not in {124, 130} and not output_overflow.is_set():
            for line in stdout.decode("utf-8", errors="strict").splitlines():
                if line.strip():
                    if len(frames) >= MAX_PROTOCOL_FRAMES:
                        raise ValueError("Worker protocol frame limit exceeded")
                    frame = json.loads(line)
                    if not isinstance(frame, dict):
                        raise ValueError("Worker response is not an object")
                    frames.append(frame)
        if output_overflow.is_set():
            frames = []  # A partial protocol frame can never be a task result.
        diagnostic = stderr.decode("utf-8", errors="replace")
        if stderr_truncated:
            # The tail may start inside a credential value whose label was
            # discarded. Do not publish that incomplete first line.
            diagnostic = diagnostic.partition("\n")[2]
        diagnostic = public_error_text(diagnostic)
        if output_overflow.is_set():
            diagnostic = "Worker stdout protocol byte limit exceeded"
        elif io_errors and code not in {124, 130}:
            diagnostic = "Worker pipe I/O failed: " + ", ".join(sorted(set(io_errors)))
        return Exchange(code, frames, diagnostic,
                        time.monotonic() - started)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            proc, job = self.proc, self.job
            self.job = None
            if job is not None:
                job.close()
            elif proc is not None and os.name != "nt":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # 资源清理类：进程组已不存在时 killpg 失败属预期。
            if proc is not None and proc.poll() is None:
                # If assignment failed, this is still our suspended root, which
                # cannot have spawned children. Never target by executable name.
                proc.kill()
                proc.wait(timeout=2)
