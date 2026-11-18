"""Build identity-scoped AgentBus MCP entries for persistent agent sessions."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def agentbus_mcp_command(workspace: Path | str, agent: str, *,
                         frozen: bool | None = None,
                         executable: Path | str | None = None) -> list[str]:
    """Return an argv list for the bundled CLI or the current source checkout."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    program = executable or sys.executable
    if is_frozen:
        # The onefile GUI executable also serves MCP mode when given arguments.
        # The frozen entrypoint reattaches inherited stdin/stdout before dispatch.
        prefix = [str(Path(program).resolve())]
    else:
        entrypoint = Path(__file__).resolve().parent.parent / "entry.py"
        prefix = [str(program), str(entrypoint)]
    return [
        *prefix, "mcp", "--workspace", str(Path(workspace).resolve()),
        "--identity", agent, "--session-tools",
    ]


def acp_mcp_server_spec(workspace: Path | str, agent: str, *,
                        frozen: bool | None = None,
                        executable: Path | str | None = None) -> dict[str, Any]:
    """Return the stdio MCP server shape required by ACP session/new/load."""
    command = agentbus_mcp_command(
        workspace, agent, frozen=frozen, executable=executable)
    return {"name": "agentbus", "command": command[0],
            "args": command[1:], "env": []}


def codex_mcp_config_overrides(workspace: Path | str, agent: str, *,
                               frozen: bool | None = None,
                               executable: Path | str | None = None) -> tuple[str, ...]:
    """Create Codex app-server TOML overrides without a shell or config file."""
    command = agentbus_mcp_command(
        workspace, agent, frozen=frozen, executable=executable)
    return (
        "mcp_servers.agentbus.command=" + json.dumps(command[0], ensure_ascii=False),
        "mcp_servers.agentbus.args=" + json.dumps(command[1:], ensure_ascii=False),
        "mcp_servers.agentbus.enabled=true",
    )
