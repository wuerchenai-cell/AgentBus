import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import tomllib
import unittest
from pathlib import Path

from agentbus.mcp_bridge import (
    agentbus_mcp_command,
    acp_mcp_server_spec,
    codex_mcp_config_overrides,
)


class MCPBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_session_uses_current_python_module_and_fixed_identity(self):
        command = agentbus_mcp_command(
            self.root, "workbuddy", frozen=False, executable=r"C:\Python\python.exe")

        entrypoint = Path(__file__).resolve().parents[1] / "entry.py"
        self.assertEqual(command, [
            r"C:\Python\python.exe", str(entrypoint), "mcp",
            "--workspace", str(self.root), "--identity", "workbuddy",
            "--session-tools",
        ])

    def test_packaged_session_reuses_the_single_agentbus_executable(self):
        exe_dir = self.root / "application"
        exe_dir.mkdir()
        gui_exe = exe_dir / "AgentBus.exe"

        command = agentbus_mcp_command(
            self.root, "codex", frozen=True, executable=gui_exe)

        self.assertEqual(command, [
            str(gui_exe), "mcp", "--workspace", str(self.root),
            "--identity", "codex", "--session-tools",
        ])

    def test_acp_spec_splits_executable_from_args_without_shell_string(self):
        spec = acp_mcp_server_spec(
            self.root, "trae", frozen=False, executable="python")

        self.assertEqual(spec["name"], "agentbus")
        self.assertEqual(spec["command"], "python")
        self.assertTrue(spec["args"][0].endswith("entry.py"))
        self.assertEqual(spec["args"][1], "mcp")
        self.assertIn("--identity", spec["args"])
        self.assertEqual(spec["env"], [])

    def test_codex_sdk_overrides_encode_paths_as_toml_values(self):
        overrides = codex_mcp_config_overrides(
            self.root, "codex", frozen=False, executable=r"C:\Python\python.exe")

        self.assertEqual(len(overrides), 3)
        self.assertTrue(overrides[0].startswith("mcp_servers.agentbus.command="))
        self.assertTrue(overrides[1].startswith("mcp_servers.agentbus.args="))
        self.assertEqual(overrides[2], "mcp_servers.agentbus.enabled=true")
        config = tomllib.loads("\n".join(overrides))
        server = config["mcp_servers"]["agentbus"]
        self.assertEqual(server["command"], r"C:\Python\python.exe")
        self.assertIn("--identity", server["args"])
        self.assertIn("codex", server["args"])


if __name__ == "__main__":
    unittest.main()
