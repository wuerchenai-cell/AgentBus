#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agentbus 入口。

打包成 exe 之后的行为分两种：
  · 双击（没有任何参数）→ 直接开图形界面，不需要用户会敲命令
  · 带参数            → 走命令行（mcp / boss / status / detect ...）

双击启动失败时用弹窗告知用户；带参数的后台调用只写 stderr 并退出，
避免命令行出错后等待一个不可见弹窗，把文件接力挂住。
"""
from __future__ import annotations

import os
import sys


def _restore_windows_stdio() -> None:
    """Reattach inherited pipes for CLI mode in a PyInstaller windowed exe."""
    if os.name != "nt":
        return

    # A windowed PyInstaller executable may inherit valid streams that use the
    # Windows ANSI code page (for example CP936). Reconfigure those too: CLI
    # output is a machine-readable interface and must stay UTF-8 when captured
    # by another process.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not any(stream is None for stream in (sys.stdin, sys.stdout, sys.stderr)):
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE

    def open_handle(which: int, mode: str):
        handle = kernel32.GetStdHandle(which)
        if not handle or int(handle) == -1:
            return open(os.devnull, mode, encoding="utf-8", errors="replace")
        flags = os.O_BINARY | (os.O_RDONLY if mode == "r" else os.O_WRONLY)
        fd = msvcrt.open_osfhandle(int(handle), flags)
        return os.fdopen(fd, mode, encoding="utf-8", errors="replace", buffering=1)

    if sys.stdin is None:
        sys.stdin = sys.__stdin__ = open_handle(-10, "r")
    if sys.stdout is None:
        sys.stdout = sys.__stdout__ = open_handle(-11, "w")
    if sys.stderr is None:
        sys.stderr = sys.__stderr__ = open_handle(-12, "w")


def _run_gui() -> int:
    from .web_desktop import main as gui_main
    return gui_main([])


def main() -> int:
    frozen = bool(getattr(sys, "frozen", False))
    if frozen and len(sys.argv) == 1:
        return _run_gui()
    if frozen:
        _restore_windows_stdio()
    if len(sys.argv) > 1 and sys.argv[1] == 'gui':
        from .web_desktop import main as gui_main
        return gui_main(sys.argv[2:])
    if len(sys.argv) == 2 and sys.argv[1] == "_sdk_worker":
        from .sdk_worker import main as worker_main
        return worker_main()
    from .cli import main as cli_main
    return cli_main()


def run() -> int:
    """带兜底错误处理的外层入口（打包后的 exe 走这里）。"""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        import traceback
        detail = traceback.format_exc()
        try:
            print(detail, file=sys.stderr)
        except Exception:  # noqa: BLE001  stdout/stderr 可能是 None
            pass  # 资源清理类：CLI 退出码打印失败不影响进程退出码。
        # Source: actual peer process audit / Codex, 2026-10-04. Only a
        # double-click GUI startup can need a modal; CLI/MCP must terminate.
        if not (bool(getattr(sys, 'frozen', False)) and len(sys.argv) == 1):
            return 1
        try:
            from .web_desktop import show_startup_error
            show_startup_error(f"{exc}\n\n{detail[-1200:]}")
        except Exception:  # noqa: BLE001
            pass  # 资源清理类：启动错误弹窗渠道本身不可用时静默，主流程仍返回 1。
        return 1


if __name__ == "__main__":
    sys.exit(run())
