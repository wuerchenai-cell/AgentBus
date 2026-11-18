"""Actual CLI subprocess file relay. No model or private host access."""
import sys
from pathlib import Path
_ROOT_DIR = Path(__file__).resolve().parents[1]
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TaskTreeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agentbus-树-')
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.command = [sys.executable, '-B', '-X', 'utf8', str(ROOT / 'entry.py')]
        self.call('init')
        self.call('member', 'add', '--id', 'lab-worker', '--name', 'New project member')
        self.call('team', 'configure', '--members', 'codex,trae,lab-worker', '--coordinator', 'codex')

    def call(self, *args, expected=0):
        proc = subprocess.run([*self.command, *args, '--workspace', str(self.workspace)],
                              capture_output=True, timeout=25)
        output = proc.stdout.decode('utf-8', errors='strict')
        error = proc.stderr.decode('utf-8', errors='strict')
        self.assertEqual(expected, proc.returncode, (args, output, error))
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return output

    def seed(self, identity='leaf1', **extra):
        leaf = {'id': identity, 'title': 'Write a checked file', 'goal': 'Create result.txt and return its verified bytes',
                'expected_artifacts': ['result.txt'], 'result_to': 'codex', **extra}
        (self.workspace / 'leaf.json').write_text(json.dumps(leaf), encoding='utf-8')
        self.call('tree', 'add', '--file', 'leaf.json', '--branch', 'implementation', '--goal', 'Complete a file relay')

    def ticket(self, message):
        return ['--id', message['id'], '--claim-epoch', str(message['claim_epoch']), '--claim-id', message['claim_id']]

    def report(self, message, agent, *, expected=0):
        artifact = self.workspace / 'result.txt'
        artifact.write_bytes('接力完成\n'.encode('utf-8'))
        (self.workspace / 'manifest.json').write_text(json.dumps([{'path': 'result.txt', 'bytes': artifact.stat().st_size,
            'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}]), encoding='utf-8')
        body = '目标已实现；成果 result.txt；接手说明：核验后继续。\n'.encode('utf-8')
        (self.workspace / 'receipt.txt').write_bytes(body)
        self.call('heartbeat', '--agent', agent, *self.ticket(message), '--note', 'test worker is active',
                  expected=expected)
        return self.call('answer', '--agent', agent, *self.ticket(message), '--body-file', 'receipt.txt',
                         '--expected-body-sha256', hashlib.sha256(body).hexdigest(),
                         '--artifacts-file', 'manifest.json', expected=expected)

    def test_new_member_can_claim_and_complete_same_project_leaf(self):
        self.seed()
        message = self.call('tree', 'claim', '--leaf', 'leaf1', '--agent', 'lab-worker')
        self.assertEqual('lab-worker', message['to'])
        self.report(message, 'lab-worker')
        view = self.call('tree', 'status')
        leaf = next(row for row in view['leaves'] if row['id'] == 'leaf1')
        self.assertEqual('delivered', leaf['state'])
        self.assertFalse(leaf['claimable'])
        self.assertFalse(view['auto_wakeup'])

    def test_checkpoint_handoff_rejects_old_result_and_new_chat_can_resume(self):
        self.seed()
        original = self.call('tree', 'claim', '--leaf', 'leaf1', '--agent', 'lab-worker')
        self.call('tree', 'checkpoint', '--leaf', 'leaf1', '--agent', 'lab-worker',
                  *self.ticket(original), '--note', '断点：已确认输入，下位成员写 result.txt。')
        self.call('tree', 'handoff', '--leaf', 'leaf1', '--agent', 'lab-worker',
                  *self.ticket(original), '--note', '额度用尽，保留目标和断点；由Trae接手。', '--successor', 'trae')
        self.report(original, 'lab-worker', expected=4)
        view = self.call('tree', 'status')
        leaf = next(row for row in view['leaves'] if row['id'] == 'leaf1')
        self.assertIn('额度用尽', leaf['handoff'])
        successor = self.call('tree', 'claim', '--leaf', 'leaf1', '--agent', 'trae')
        self.assertNotEqual(original['id'], successor['id'])
        self.report(successor, 'trae')
        leaf = next(row for row in self.call('tree', 'status')['leaves'] if row['id'] == 'leaf1')
        self.assertEqual('delivered', leaf['state'])

    def test_stop_prevents_claim_without_losing_leaf(self):
        self.seed()
        (self.workspace / '.agentbus/STOP').write_text('User stopped work', encoding='utf-8')
        self.call('tree', 'claim', '--leaf', 'leaf1', '--agent', 'lab-worker', expected=4)
        self.assertEqual('stopped', self.call('tree', 'status')['leaves'][0]['state'])

    def test_dependency_and_duplicate_claim_are_refused(self):
        self.seed()
        self.seed('leaf2', dependencies=['leaf1'])
        self.call('tree', 'claim', '--leaf', 'leaf2', '--agent', 'trae', expected=4)
        self.call('tree', 'claim', '--leaf', 'leaf1', '--agent', 'lab-worker')
        self.call('tree', 'claim', '--leaf', 'leaf1', '--agent', 'trae', expected=4)


if __name__ == '__main__':
    unittest.main()
