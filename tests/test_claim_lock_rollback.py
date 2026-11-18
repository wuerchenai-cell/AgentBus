"""A failed signed-claim write must return both mailbox and lock to ready."""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from agentbus.bus import Bus
from agentbus import exclusive_lock

class TestClaimLockRollback(unittest.TestCase):
    def test_claim_rollback_on_write_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            bus=Bus(Path(folder)/'.agentbus');bus.init()
            msg=bus.post(bus.make('owner','codex','task',result_to='owner'))
            path=bus.dirs('codex')['ready']/f'{msg.id}.json';before=path.read_bytes()
            with patch.object(bus,'_write',side_effect=OSError('injected write failure')):
                with self.assertRaises(OSError):bus.claim('codex',path)
            self.assertEqual(path.read_bytes(),before)
            self.assertIsNone(exclusive_lock.get_task_lock_info(bus.root,msg.id))
            claimed=bus.claim('codex',path)
            self.assertIsNotNone(claimed)

if __name__ == "__main__":
    unittest.main()
