"""Binary inventory must exclude private paths and identify unsealed launchers."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from tools import archived_binary_inventory as inventory

class TestUniqueFormalExecutable(unittest.TestCase):
    def test_single_formal_executable_assertion(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'成果').mkdir();(root/'成果/AgentBus.exe').write_bytes(b'formal')
            (root/'history').mkdir();(root/'history/AgentBus.exe.deprecated_old_version').write_bytes(b'old')
            self.assertEqual(inventory.unsealed_agentbus_launchers(root),['成果/AgentBus.exe'])
            (root/'history/AgentBus.exe').write_bytes(b'unsafe')
            self.assertEqual(inventory.unsealed_agentbus_launchers(root),['history/AgentBus.exe','成果/AgentBus.exe'])

    def test_private_tree_is_never_entered_by_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def simulated_walk(*args,**kwargs):
                directories=['.workbuddy']
                yield str(root),directories,[]
                self.assertEqual(directories,[], 'private names must be pruned before traversal')
            with patch.object(inventory.os,'walk',side_effect=simulated_walk):
                self.assertEqual(inventory.find_archived_binaries(root),[])
                self.assertEqual(inventory.unsealed_agentbus_launchers(root),[])

if __name__ == "__main__":
    unittest.main()
