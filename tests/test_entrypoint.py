import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from agentbus.__main__ import _restore_windows_stdio, main, run


class EntrypointTests(unittest.TestCase):
    def test_windows_stdio_is_reconfigured_to_utf8_even_when_already_present(self):
        calls = []

        class Stream:
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        fake_sys = SimpleNamespace(stdin=Stream(), stdout=Stream(), stderr=Stream())
        with patch("agentbus.__main__.os.name", "nt"), \
             patch("agentbus.__main__.sys", fake_sys):
            _restore_windows_stdio()

        self.assertEqual(calls, [
            {"encoding": "utf-8", "errors": "replace"},
            {"encoding": "utf-8", "errors": "replace"},
            {"encoding": "utf-8", "errors": "replace"},
        ])

    def test_frozen_argument_mode_restores_stdio_before_mcp_cli(self):
        order = []
        fake_sys = SimpleNamespace(frozen=True, argv=["AgentBus.exe", "mcp"])

        def restore():
            order.append("stdio")

        def cli_main():
            order.append("cli")
            return 0

        with patch("agentbus.__main__.sys", fake_sys), \
             patch("agentbus.__main__._restore_windows_stdio", side_effect=restore), \
             patch("agentbus.cli.main", side_effect=cli_main):
            self.assertEqual(main(), 0)

        self.assertEqual(order, ["stdio", "cli"])

    def test_unexpected_cli_failure_exits_without_a_hidden_modal_dialog(self):
        fake_sys = SimpleNamespace(frozen=True, argv=['AgentBus.exe', 'answer'], stderr=StringIO())
        with patch('agentbus.__main__.sys', fake_sys), \
             patch('agentbus.__main__.main', side_effect=RuntimeError('controlled CLI failure')), \
             patch('agentbus.web_desktop.show_startup_error') as popup:
            self.assertEqual(run(), 1)
        self.assertIn('controlled CLI failure', fake_sys.stderr.getvalue())
        popup.assert_not_called()

    def test_frozen_double_click_failure_keeps_visible_startup_error(self):
        fake_sys = SimpleNamespace(frozen=True, argv=['AgentBus.exe'], stderr=StringIO())
        with patch('agentbus.__main__.sys', fake_sys), \
             patch('agentbus.__main__.main', side_effect=RuntimeError('controlled GUI failure')), \
             patch('agentbus.web_desktop.show_startup_error') as popup:
            self.assertEqual(run(), 1)
        popup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
