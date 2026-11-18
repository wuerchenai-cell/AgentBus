"""JR-20261108-R02: all claim entries share one atomic work-slot limit."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from agentbus.bus import Bus, BusError
from agentbus.claim_fencing import captured_ticket
from agentbus.config import load
from agentbus.mcp_server import AgentBusMcp
from agentbus.task_tree import TaskForest, TaskTreeError


class MemberClaimLimitRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.forest = TaskForest(load(self.workspace, create=True))
        self.bus = self.forest.bus
        self.bus.init()

    def post(self, kind='task'):
        message = self.bus.make('codex', 'workbuddy', kind, body='Isolated work', result_to='codex')
        self.bus.post(message)
        return self.bus.dirs('workbuddy')['ready'] / f'{message.id}.json'

    def test_direct_claim_preserves_the_second_task_when_member_is_busy(self):
        first = self.bus.claim('workbuddy', self.post())
        second = self.post()
        raw = second.read_bytes()
        with self.assertRaisesRegex(BusError, '在途'):
            self.bus.claim('workbuddy', second)
        self.assertEqual(second.read_bytes(), raw)
        self.assertEqual([p.stem for p in self.bus.processing('workbuddy')], [first.id])

    def test_tree_and_mcp_cannot_claim_a_second_piece_of_work(self):
        self.bus.claim('workbuddy', self.post())
        self.forest.seed_leaf({'id': 'second', 'title': 'Second work item'}, branch_id='work')
        with self.assertRaisesRegex((TaskTreeError, BusError), '在途'):
            self.forest.claim_leaf('second', 'workbuddy')
        self.post()
        mcp = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        with self.assertRaisesRegex((ValueError, BusError), '在途'):
            mcp.next_task('workbuddy')
        self.assertEqual(len(self.bus.processing('workbuddy')), 1)

    def test_information_can_be_received_while_work_is_in_progress(self):
        self.bus.claim('workbuddy', self.post())
        info = self.bus.claim('workbuddy', self.post('info'))
        self.assertIsNotNone(info)
        self.assertEqual(info.type, 'info')

    def test_results_can_be_received_while_work_is_in_progress(self):
        self.bus.claim('workbuddy', self.post())
        source = self.bus.make('workbuddy', 'codex', 'task',
                               body='Produce an answer', result_to='workbuddy')
        self.bus.post(source)
        task = self.bus.claim('codex', self.bus.dirs('codex')['ready'] / f'{source.id}.json')
        ticket = captured_ticket(task)
        self.bus.heartbeat('codex', task, ticket, note='Independent result producer')
        receipt = self.bus.commit_result('codex', task, ticket, body='Completed answer', exit_code=0)
        received = self.bus.claim('workbuddy', self.bus.dirs('workbuddy')['ready'] / f'{receipt.id}.json')
        self.assertEqual(received.type, 'result')

    def test_processing_information_does_not_block_public_pool_work(self):
        info = self.bus.claim('workbuddy', self.post('info'))
        self.forest.seed_leaf({'id': 'pool-work', 'title': 'Pool work'}, branch_id='work')
        claimed = self.forest.claim_next('workbuddy')['message']
        self.assertEqual(claimed.type, 'task')
        self.assertTrue((self.bus.dirs('workbuddy')['processing'] / f'{info.id}.json').is_file())

    def test_preexisting_multiple_work_items_are_preserved_on_denial(self):
        for path in (self.post(), self.post()):
            message = self.bus.read(path)
            message.status = 'claimed'
            path.rename(self.bus.dirs('workbuddy')['processing'] / path.name)
            self.bus._write(self.bus.dirs('workbuddy')['processing'] / path.name, message)
        before = {path.name: path.read_bytes() for path in self.bus.dirs('workbuddy')['processing'].glob('*.json')}
        with self.assertRaisesRegex(BusError, '2 项在途'):
            self.bus.claim('workbuddy', self.post())
        after = {path.name: path.read_bytes() for path in self.bus.dirs('workbuddy')['processing'].glob('*.json')}
        self.assertEqual(after, before)

    def test_two_processes_cannot_take_two_tasks_for_the_same_member(self):
        self._assert_claim_race(same_task=False)

    def test_two_processes_can_claim_the_same_task_only_once(self):
        self._assert_claim_race(same_task=True)

    def _assert_claim_race(self, *, same_task):
        first = self.post()
        paths = [first, first if same_task else self.post()]
        source = Path(__file__).resolve().parents[1]
        script = """
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from agentbus.bus import Bus,BusError
root,task,number=Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4]
(root/('ready-'+number)).write_text('ready')
deadline=time.monotonic()+15
while not (root/'gate').exists():
 if time.monotonic()>deadline:raise RuntimeError('test gate timeout')
 time.sleep(.01)
try:
 m=Bus(root/'.agentbus').claim('workbuddy',task)
 print(json.dumps({'claimed':m is not None}))
except BusError as e:
 print(json.dumps({'claimed':False,'error':str(e)}))
"""
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        processes = [subprocess.Popen(
            [sys.executable, '-B', '-X', 'utf8', '-c', script, str(source),
             str(self.workspace), str(path), str(index)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            for index, path in enumerate(paths)]
        try:
            deadline = time.monotonic() + 15
            while not all((self.workspace / f'ready-{index}').exists() for index in range(2)):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            (self.workspace / 'gate').write_text('start')
            reports = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr.decode('utf-8', errors='replace'))
                reports.append(json.loads(stdout))
            self.assertEqual(sum(item['claimed'] for item in reports), 1, reports)
            self.assertEqual(len(self.bus.processing('workbuddy')), 1)
            self.assertEqual(len(self.bus.pending('workbuddy')), 0 if same_task else 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
