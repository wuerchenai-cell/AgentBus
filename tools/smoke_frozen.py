"""No-model, isolated stdio smoke test for the onefile AgentBus executable."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


class _WindowsJob:
    """Own only smoke descendants; bind before the suspended root can run.

    Source: Codex, 2026-10-04; documented Win32 Job/Toolhelp/ResumeThread APIs.
    No breakaway flags or inheritable job handle are granted.
    """
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.c = ctypes
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active_limit', w.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ['reads', 'writes', 'others', 'read_bytes', 'write_bytes', 'other_bytes']]

        class Limits(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('tid', w.DWORD),
                        ('pid', w.DWORD), ('base_priority', w.LONG), ('delta_priority', w.LONG),
                        ('flags', w.DWORD)]
        self.ThreadEntry = ThreadEntry
        signatures = {
            'CreateJobObjectW': ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
            'Thread32First': ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            'Thread32Next': ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            'OpenThread': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'ResumeThread': ([w.HANDLE], w.DWORD),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            getattr(self.k, name).argtypes = arguments
            getattr(self.k, name).restype = result
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def attach_and_resume(self, proc: subprocess.Popen) -> None:
        if not self.k.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise self.c.WinError(self.c.get_last_error())
        snapshot = self.k.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == self.c.c_void_p(-1).value:
            raise self.c.WinError(self.c.get_last_error())
        try:
            entry = self.ThreadEntry()
            entry.size = self.c.sizeof(entry)
            more = self.k.Thread32First(snapshot, self.c.byref(entry))
            while more:
                if entry.pid == proc.pid:
                    thread = self.k.OpenThread(0x2, False, entry.tid)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise self.c.WinError(self.c.get_last_error())
                    try:
                        previous = self.k.ResumeThread(thread)
                        if previous == 0xffffffff:
                            raise self.c.WinError(self.c.get_last_error())
                        if previous != 1:
                            raise RuntimeError('Smoke root was not suspended exactly once')
                    finally:
                        self.k.CloseHandle(thread)
                    return
                more = self.k.Thread32Next(snapshot, self.c.byref(entry))
            raise RuntimeError('Could not find the suspended smoke root thread')
        finally:
            self.k.CloseHandle(snapshot)

    def close(self) -> None:
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == 'win32':
        # PyInstaller onefile uses a parent and child. Terminate the tree so
        # its inherited pipes cannot keep communicate alive after timeout.
        try:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def smoke(exe: Path, *, timeout_s: float = 90) -> None:
    """Source: Codex, 2026-10-04. Bound the entire MCP exchange, including EOF."""
    if timeout_s <= 0:
        raise ValueError('MCP smoke timeout must be positive')
    with tempfile.TemporaryDirectory(prefix="agentbus-onefile-mcp-") as directory:
        workspace = Path(directory).resolve()
        config_path = workspace / ".agentbus" / "config.json"
        config_path.parent.mkdir(parents=True)
        config = {
            "schema_version": 1,
            "workspace": str(workspace),
            "director": "codex",
            "workers": {
                name: {"enabled": True, "executor_enabled": True,
                       "can_spawn": False, "kind": "gui"}
                for name in ("codex", "workbuddy", "trae", "zcode")
            },
            "gates": {},
            "oversight": {},
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        job = _WindowsJob() if sys.platform == 'win32' else None
        proc = None
        smoke_env = dict(os.environ) if 'os' in globals() else dict(__import__('os').environ)
        smoke_env['PYTHONUTF8'] = '1'
        smoke_env['PYTHONIOENCODING'] = 'utf-8'
        try:
            proc = subprocess.Popen(
                [str(exe), 'mcp', '--workspace', str(workspace),
                 '--identity', 'workbuddy', '--session-tools'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace', bufsize=1, env=smoke_env,
                cwd=str(workspace),
                creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0) | 0x4
                               if job is not None else 0),  # CREATE_SUSPENDED
            )
            if job is not None:
                job.attach_and_resume(proc)
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "agentbus_send",
                    "arguments": {"to": "trae", "type": "info", "body": "smoke"},
                }},
            ]
            payload = ''.join(json.dumps(request, ensure_ascii=False) + '\n' for request in requests)
            try:
                stdout, stderr = proc.communicate(payload, timeout=timeout_s)
            except subprocess.TimeoutExpired as exc:
                if job is not None:
                    job.close()  # includes descendants even if root exited
                _stop(proc)
                raise RuntimeError(f'MCP smoke timed out after {timeout_s} seconds') from exc
            responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            if len(responses) != len(requests) or [item.get('id') for item in responses] != [1, 2, 3]:
                raise RuntimeError('MCP smoke received missing or mismatched response IDs')
            if any(item.get('error') for item in responses):
                raise RuntimeError('MCP smoke returned a JSON-RPC error')
            names = [tool["name"] for tool in responses[1]["result"]["tools"]]
            if names != ["agentbus_send"]:
                raise RuntimeError(f"session-scoped tools mismatch: {names}")
            if responses[2].get("error") or responses[2]["result"].get("isError"):
                raise RuntimeError(f"scoped MCP send failed: {responses[2]}")
            if proc.returncode != 0:
                raise RuntimeError(f'MCP helper exited with {proc.returncode}: {stderr[:1000]}')
        finally:
            if job is not None:
                job.close()
            if proc is not None:
                _stop(proc)
                for stream in [proc.stdin, proc.stdout, proc.stderr]:
                    if stream is not None:
                        stream.close()


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] in ('-h', '--help'):
        print('usage: smoke_frozen.py <AgentBus.exe>')
        print('Validate one newly built candidate in an isolated temporary workspace; model_calls=0.')
        return 0
    if len(sys.argv) != 2:
        print('usage: smoke_frozen.py <AgentBus.exe>', file=sys.stderr)
        return 2
    exe = Path(sys.argv[1]).resolve()
    if not exe.is_file():
        print(f'missing executable: {exe}', file=sys.stderr)
        return 2
    try:
        smoke(exe)
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print('onefile_mcp_stdio=ok; scoped_peer_send=ok; model_calls=0')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
