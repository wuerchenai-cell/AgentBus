from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_paths import ENV_NAME, host_project_root


class HostProjectRootTests(unittest.TestCase):
    def test_missing_environment_means_portable_suite(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(host_project_root())

    def test_explicit_valid_root_is_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.agentbus').mkdir()
            (root / '.agentbus' / 'config.json').write_text('{}', encoding='utf-8')
            with patch.dict(os.environ, {ENV_NAME: str(root)}, clear=True):
                self.assertEqual(root.resolve(), host_project_root())

    def test_invalid_explicit_root_fails_loudly(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {ENV_NAME: temp}, clear=True):
                with self.assertRaisesRegex(ValueError, ENV_NAME):
                    host_project_root()


if __name__ == '__main__':
    unittest.main()
