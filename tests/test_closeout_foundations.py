"""Isolated fault and concurrency tests for terminal/claim lock boundaries."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from agentbus import exclusive_lock as locks, operation_lock, claim_fencing as fencing
from agentbus.bus import Message, now_iso
from agentbus.jobs import receipt_binding_error

class FoundationScenarios:
    def test_local_mutex_wait_consumes_total_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            for mutex in (lambda: locks.task_exclusive_mutex(folder,'task-one',timeout_s=.05),
                          lambda: operation_lock.task_lock(folder,'codex','task-one',timeout_s=.05)):
                with self.subTest(mutex=mutex):
                    acquired=threading.Event()
                    def holder():
                        with mutex():acquired.set();time.sleep(.25)
                    thread=threading.Thread(target=holder);thread.start();self.assertTrue(acquired.wait(1))
                    start=time.monotonic()
                    try:
                        with self.assertRaises((TimeoutError,locks.ExclusiveLockError)):
                            with mutex():pass
                        self.assertLess(time.monotonic()-start,.2)
                    finally:thread.join(1)

    def test_corrupt_lock_is_preserved_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=locks.lock_record_path(folder,'task-one');path.write_bytes(b'{broken')
            with self.assertRaises(locks.ExclusiveLockError):locks.get_task_lock_info(folder,'task-one')
            with self.assertRaises(locks.ExclusiveLockError):
                locks.acquire_task_lock(folder,'task-one','codex',fencing.issue('task-one','codex',1))
            self.assertEqual(path.read_bytes(),b'{broken')

    def test_lock_ticket_epoch_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            ticket=fencing.issue('task-one','codex',1)
            locks.acquire_task_lock(folder,'task-one','codex',ticket)
            wrong={**ticket.to_dict(),'claim_epoch':2}
            with self.assertRaises(locks.ExclusiveLockError):locks.verify_task_lock(folder,'task-one','codex',wrong)
            self.assertEqual(locks.get_task_lock_info(folder,'task-one')['claim_epoch'],1)

    def test_result_cv_mismatch_cannot_reconcile(self):
        task=Message(id='task-one',ts=now_iso(),sender='owner',to='codex',type='task',result_to='owner')
        ticket=fencing.issue(task.id,'codex',1);fencing.apply_ticket(task,ticket)
        result=Message(id='result-one',ts=now_iso(),sender='codex',to='owner',type='result',reply_to=task.id,
                       source_claim_epoch=1,source_claim_id=ticket.claim_id,
                       source_conversation_id='cv-'+'1'*24)
        self.assertTrue(receipt_binding_error(task,result))
        result.source_conversation_id=ticket.conversation_id
        self.assertEqual(receipt_binding_error(task,result),'')

class AdditionalLockTests(unittest.TestCase):
    test_lock_ticket_epoch_mismatch_is_rejected=FoundationScenarios.test_lock_ticket_epoch_mismatch_is_rejected
