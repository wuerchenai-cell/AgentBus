"""JR-20261108-U05: directed tree claims supply the original receipt draft."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main
from agentbus.config import load
from agentbus.task_tree import TaskForest


class DirectedClaimDraftRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.forest = TaskForest(load(self.workspace, create=True))
        self.forest.seed_leaf({'id': 'directed-leaf', 'title': 'Directed work'}, branch_id='work')

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main([*args, '--workspace', str(self.workspace)])
        return rc, out.getvalue(), err.getvalue()

    def claim(self):
        rc, out, err = self.call('tree', 'claim', '--leaf', '1.1', '--agent', 'workbuddy')
        self.assertEqual(rc, 0, err)
        # The existing machine-readable stdout remains a plain message.
        packet = json.loads(out)
        return packet, err

    def fill(self, path):
        text = path.read_text(encoding='utf-8')
        text = '\n'.join(line for line in text.splitlines() if 'AGENTBUS_DRAFT_UNFINISHED' not in line)
        text = text.replace('（填写实际完成内容；未完成或受阻请如实说明。）', '实际工作已完成并核对。')
        path.write_text(text, encoding='utf-8')

    def test_directed_claim_supplies_original_draft_and_can_deliver(self):
        packet, output = self.claim()
        relative = f"成果/任务书维护/receipts/{packet['id']}.reply.md"
        path = self.workspace / relative
        self.assertTrue(path.is_file())
        self.assertIn(relative, output)
        self.assertIn('§1.1', path.read_text(encoding='utf-8'))
        self.assertIn(packet['claim_id'], path.read_text(encoding='utf-8'))
        self.fill(path)
        task = self.forest.bus._active_record('workbuddy', packet['id'])
        self.forest.bus.heartbeat('workbuddy', task, captured_ticket(task), note='actual directed task')
        rc, out, err = self.call('answer', '--agent', 'workbuddy', '--body-file', relative, '--auto-manifest')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.forest.bus.find_results(packet['id'])), 1)

    def test_reclaim_preserves_old_draft_and_cannot_submit_with_it(self):
        first, _ = self.claim()
        original = self.workspace / f"成果/任务书维护/receipts/{first['id']}.reply.md"
        self.assertTrue(original.is_file())
        self.fill(original)
        raw = original.read_bytes()
        self.forest.bus.recover_claim('workbuddy', first['id'], owner_confirmed_inactive=True)
        second, output = self.claim()
        self.assertGreater(second['claim_epoch'], first['claim_epoch'])
        replacement = self.workspace / f"成果/任务书维护/receipts/{second['id']}.{second['claim_id'][:12]}.reply.md"
        self.assertTrue(replacement.is_file())
        self.assertIn(replacement.relative_to(self.workspace).as_posix(), output)
        self.assertEqual(original.read_bytes(), raw)
        task = self.forest.bus._active_record('workbuddy', second['id'])
        self.forest.bus.heartbeat('workbuddy', task, captured_ticket(task), note='replacement task')
        rc, _, _ = self.call('answer', '--agent', 'workbuddy',
                            '--body-file', original.relative_to(self.workspace).as_posix(), '--auto-manifest')
        self.assertNotEqual(rc, 0)
        self.assertFalse(self.forest.bus.find_results(second['id']))
        self.fill(replacement)
        rc, out, err = self.call('answer', '--agent', 'workbuddy',
                            '--body-file', replacement.relative_to(self.workspace).as_posix(), '--auto-manifest')
        self.assertEqual(rc, 0, out + err)

    def test_duplicate_claim_does_not_rewrite_the_draft(self):
        packet, _ = self.claim()
        path = self.workspace / f"成果/任务书维护/receipts/{packet['id']}.reply.md"
        self.assertTrue(path.is_file())
        original = path.read_bytes()
        rc, _, _ = self.call('tree', 'claim', '--leaf', '1.1', '--agent', 'workbuddy')
        self.assertNotEqual(rc, 0)
        self.assertEqual(path.read_bytes(), original)

    def test_draft_failure_keeps_the_original_claim_visible(self):
        with patch('agentbus.cli._prepare_reply_draft', return_value=None):
            packet, output = self.claim()
        self.assertTrue(packet['claim_id'])
        self.assertIn('草稿未生成', output)
        self.assertIn('原票据', output)
