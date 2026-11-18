"""Codex internal fixture verification, 2026-10-05; public offline objects only.

Some host runtimes return from os.symlink without producing a real link. These
tests distinguish that environment limitation from scanner link rejection.
"""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentbus.skill_packages import SkillPackageError, inspect_package
import test_skill_packages as package_tests


class SkillLinkEnvironmentTests(unittest.TestCase):
    def assert_fake_creation_is_skipped(self, method):
        def fake_creation(source, link, *args, **kwargs):
            source, link = Path(source), Path(link)
            if source.is_dir():
                link.mkdir()
            else:
                link.write_bytes(source.read_bytes())
            info = link.lstat()
            self.assertFalse(stat.S_ISLNK(info.st_mode))
            self.assertFalse(getattr(info, 'st_file_attributes', 0) & 0x400)
        case = package_tests.SkillPackageTests(method)
        result = unittest.TestResult()
        with patch.object(os, 'symlink', fake_creation):
            case.run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.skipped), 1)
        self.assertIn('host returned without creating real symlink/reparse point',
                      result.skipped[0][1])

    def test_plain_file_returned_by_host_is_not_a_link_test(self):
        self.assert_fake_creation_is_skipped('test_symlink_entry_is_rejected')

    def test_empty_directory_returned_by_host_is_not_a_link_test(self):
        self.assert_fake_creation_is_skipped('test_symlinked_package_directory_is_rejected')

    @unittest.skipUnless(os.name == 'nt', 'NTFS junction requires Windows')
    def test_real_junction_is_rejected_with_public_target_inside_workspace(self):
        with tempfile.TemporaryDirectory(prefix='codex-public-link-env-') as td:
            root = Path(td).resolve()
            target, link = root / 'target', root / 'junction'
            target.mkdir()
            (target / 'SKILL.md').write_bytes(b'# public fixture\n')
            self.assertTrue(target.is_relative_to(root))
            self.assertTrue(link.is_relative_to(root))
            result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                                     str(link), str(target)], capture_output=True,
                                    timeout=15)
            if result.returncode != 0:
                self.skipTest('host cannot create a real NTFS junction')
            try:
                info = link.lstat()
                if not (getattr(info, 'st_file_attributes', 0) & 0x400) or not link.is_junction():
                    self.skipTest('host returned without creating real junction/reparse point')
                self.assertTrue(getattr(info, 'st_file_attributes', 0) & 0x400)
                self.assertTrue(link.is_junction())
                with self.assertRaises(SkillPackageError) as error:
                    inspect_package(root, 'junction')
                self.assertEqual(error.exception.code, 'UNSAFE_PATH')
                self.assertEqual((target / 'SKILL.md').read_bytes(), b'# public fixture\n')
            finally:
                # Remove only the reparse object; never recursively follow it.
                try:
                    link.rmdir()
                except FileNotFoundError:
                    pass
                except NotADirectoryError:
                    link.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
