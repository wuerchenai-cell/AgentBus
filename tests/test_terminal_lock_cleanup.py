"""Terminal archive and relinquish must also retire the physical task lock."""
import unittest
import tempfile
from pathlib import Path
from agentbus.bus import Bus
from agentbus import exclusive_lock,claim_fencing
from agentbus.config import load
from unittest.mock import patch
from agentbus.bus import BusError

class TestTerminalLockCleanup(unittest.TestCase):
    def test_cleanup_failure_is_visible_and_retry_repairs_without_reexecution(self):
        with tempfile.TemporaryDirectory() as folder:
            bus=Bus(Path(folder)/'.agentbus');bus.init()
            msg=bus.post(bus.make('owner','codex','task',result_to='owner'))
            claimed=bus.claim('codex',bus.dirs('codex')['ready']/f'{msg.id}.json')
            ticket=claim_fencing.captured_ticket(claimed)
            with patch.object(exclusive_lock,'release_task_lock',side_effect=OSError('locked file')):
                with self.assertRaises(BusError):bus.finish('codex',claimed,status='failed',ticket=ticket)
            self.assertTrue((bus.dirs('codex')['done']/f'{msg.id}.json').is_file())
            with self.assertRaisesRegex(BusError,'已终结'):bus.commit_result('codex',claimed,ticket)
            self.assertIsNone(exclusive_lock.get_task_lock_info(bus.root,msg.id))
    def test_idempotent_cleanup_on_terminal_state(self):
        with tempfile.TemporaryDirectory() as folder:
            bus=Bus(Path(folder)/'.agentbus');bus.init()
            msg=bus.post(bus.make('owner','codex','task',result_to='owner'))
            claimed=bus.claim('codex',bus.dirs('codex')['ready']/f'{msg.id}.json')
            ticket=claim_fencing.captured_ticket(claimed)
            bus.finish('codex',claimed,status='failed',ticket=ticket)
            self.assertIsNone(exclusive_lock.get_task_lock_info(bus.root,msg.id))

    def test_relinquish_and_retry_clean_the_old_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            load(Path(folder),create=True)
            bus=Bus(Path(folder)/'.agentbus');bus.init()
            msg=bus.post(bus.make('owner','codex','task',result_to='owner'))
            bus.claim('codex',bus.dirs('codex')['ready']/f'{msg.id}.json')
            for _ in range(2):
                bus.relinquish_claim('codex',msg.id,reason='owner stopped',owner_confirmed_inactive=True)
                self.assertIsNone(exclusive_lock.get_task_lock_info(bus.root,msg.id))

if __name__ == "__main__":
    unittest.main()
