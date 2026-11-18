"""Pre-move claim failures preserve bytes and expose safe stage metadata."""
import errno
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_claim_observability as base


class ClaimIOTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.ClaimObservabilityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_original_bytes_read_failure_is_not_reported_as_a_lost_race(self):
        f = self.fixture
        original = Path.read_bytes
        error = PermissionError(errno.EACCES, 'PRIVATE_EXCEPTION_SENTINEL')
        def read(path):
            if path == f.ready:
                raise error
            return original(path)
        with patch.object(Path, 'read_bytes', read), self.assertRaises(PermissionError) as caught:
            f.bus.claim('workbuddy', f.ready)
        self.assertIs(caught.exception, error)
        f.assert_failure('read_bytes', 'PermissionError', errno.EACCES)
        self.assertEqual(f.ready.read_bytes(), f.before)

    def test_move_failure_is_audited_with_safe_codes_and_no_fake_claim(self):
        f = self.fixture
        error = PermissionError(errno.EACCES, 'PRIVATE_EXCEPTION_SENTINEL')
        error.winerror = 32
        with patch('agentbus.bus.os.replace', side_effect=error), self.assertRaises(PermissionError) as caught:
            f.bus.claim('workbuddy', f.ready)
        self.assertIs(caught.exception, error)
        f.assert_failure('move', 'PermissionError', errno.EACCES, 32)
        self.assertEqual(f.ready.read_bytes(), f.before)

    def test_disappeared_source_remains_an_ordinary_lost_race(self):
        f = self.fixture
        def lost(*args):
            f.ready.unlink()
            raise FileNotFoundError(errno.ENOENT, 'other worker won')
        with patch('agentbus.bus.os.replace', side_effect=lost):
            self.assertIsNone(f.bus.claim('workbuddy', f.ready))
        self.assertFalse(any(e['event'] in ('claim', 'claim_failed') for e in f.bus.events()))


if __name__ == '__main__':
    unittest.main()
