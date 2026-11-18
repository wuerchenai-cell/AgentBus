"""A Windows cleanup conflict must not hide the primary write failure."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbus.trigger import contacts, journal, service


class AtomicCleanupTests(unittest.TestCase):
    def test_primary_exception_survives_tempfile_cleanup_conflict(self):
        for module in (contacts, journal, service):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                primary = PermissionError(13, 'primary replace failure')
                cleanup = PermissionError(32, 'secondary Windows sharing conflict')
                with patch.object(module.os, 'replace', side_effect=primary), \
                     patch.object(module.os, 'remove', side_effect=cleanup), \
                     self.assertRaises(PermissionError) as caught:
                    module._atomic_write_json(Path(directory) / 'state.json', {'state': 'new'})
                self.assertIs(caught.exception, primary)
                self.assertFalse((Path(directory) / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
