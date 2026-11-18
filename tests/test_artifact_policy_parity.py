"""Read-only delivery checks must respect the same private/control boundary."""
import unittest
from agentbus.delivery_verify import safe_relative
from agentbus.workspace_files import WorkspaceFiles,WorkspaceFileError

class TestArtifactPolicyParity(unittest.TestCase):
    def test_unified_artifact_security_policy(self):
        for path in ('.agentbus/config.json','.ssh/token','.codex/settings.json','不要访问/file','out/../secret','out/NUL.txt'):
            with self.subTest(path=path):
                with self.assertRaises(WorkspaceFileError):WorkspaceFiles._relative(path)
                self.assertIsNone(safe_relative(path))
        self.assertEqual(safe_relative('成果/报告.md'),'成果/报告.md')

if __name__ == "__main__":
    unittest.main()
