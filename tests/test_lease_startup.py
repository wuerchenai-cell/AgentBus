"""Source: observed Windows fresh-guard race, Codex, 2026-10-02."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from agentbus.lease import LeaseManager


class LeaseStartupTests(unittest.TestCase):
    def test_empty_guard_needs_no_unlocked_byte_initialization(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=LeaseManager(Path(temp))
            # The current unlocked initialization can race another process's
            # range lock. The real OS must lock the empty file without writing.
            with patch('agentbus.lease.os.write',side_effect=PermissionError('range locked')):
                with manager._mutation_guard() as locked:
                    self.assertTrue(locked)

    def test_simultaneous_fresh_managers_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as temp:
            def worker(index):
                manager=LeaseManager(Path(temp))
                lease=manager.acquire_wait('shared.txt',str(index),ttl_s=20,timeout_s=5)
                self.assertIsNotNone(lease)
                self.assertTrue(manager.release(lease))
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(worker,range(32)))


if __name__ == '__main__':
    unittest.main()
