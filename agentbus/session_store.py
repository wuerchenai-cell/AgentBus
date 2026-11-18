#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local, credential-free persistence for AgentBus-owned agent sessions."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def load_session(path: Path | None, *, adapter: str, cwd: Path) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (not isinstance(data, dict) or data.get("adapter") != adapter
            or data.get("cwd") != str(Path(cwd).resolve())):
        return None
    session_id = data.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def save_session(path: Path | None, *, adapter: str, agent: str,
                 cwd: Path, session_id: str) -> None:
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "adapter": adapter,
        "agent": agent,
        "cwd": str(Path(cwd).resolve()),
        "session_id": session_id,
    }
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(name).replace(path)
    except BaseException:
        try:
            Path(name).unlink(missing_ok=True)
        except OSError:
            pass  # 资源清理类：清理过期会话文件失败即放弃，不影响读写主流程。
        raise
