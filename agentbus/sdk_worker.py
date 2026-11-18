"""Source: Codex, 2026-10-04. Private stdin/NDJSON SDK worker entry."""
from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .codex_sdk import CodexSDKAdapter
from .process_boundary import public_error_text


def main() -> int:
    protocol = sys.stdout

    def emit(frame):
        protocol.write(json.dumps(frame, ensure_ascii=False) + "\n")
        protocol.flush()

    adapter = None
    try:
        request = json.loads(sys.stdin.readline())
        if not isinstance(request, dict) or request.get("action") not in {"start", "run"}:
            raise ValueError("Worker action must be start or run")
        cwd = request.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_dir():
            raise ValueError("Worker cwd must be an existing directory")
        config = request.get("config", {})
        if not isinstance(config, dict):
            raise ValueError("Worker config must be an object")
        # Only SDK constructor data is transported; no command/factory/object
        # deserialization, permission bypass flags, or shell expressions.
        with contextlib.redirect_stdout(sys.stderr):
            adapter = CodexSDKAdapter(
                sandbox=config.get("sandbox", "workspace-write"), model=config.get("model"),
                codex_bin=config.get("codex_bin"),
                session_record_path=Path(config["session_record_path"])
                if config.get("session_record_path") else None,
                agent_name=config.get("agent_name", "codex"),
            )
            adapter.thread_id = request.get("thread_id") or None
            if request["action"] == "start":
                thread_id = adapter.start(Path(cwd))
                emit({"type": "session", "session_id": thread_id})
            else:
                if not isinstance(request.get("prompt"), str):
                    raise ValueError("Worker prompt must be a string")
                result = adapter.run(
                    request["prompt"], Path(cwd), timeout_s=float(request["timeout_s"]),
                    output_schema=Path(request["output_schema"])
                    if request.get("output_schema") else None,
                )
                data = asdict(result)
                data["raw_tail"] = public_error_text(data["raw_tail"])
                if data["exit_code"] != 0:
                    data["output"] = public_error_text(data["output"])
                emit({"type": "result", "session_id": adapter.thread_id,
                      "result": data})
            # Completion includes SDK cleanup, not only an early result frame.
            adapter.close()
        return 0
    except Exception as exc:
        emit({"type": "error", "message": public_error_text(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
