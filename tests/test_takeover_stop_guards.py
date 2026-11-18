"""Takeover needs explicit stopped-owner evidence, never inferred from silence."""
import unittest
from tests import test_task_takeover as fixture
from agentbus.task_tree import TaskTreeError

class TakeoverStopTests(unittest.TestCase):
    setUp=fixture.TestTaskTakeover.setUp
    tearDown=fixture.TestTaskTakeover.tearDown

    def test_ready_transfer_retires_predecessor_and_advances_allocation(self):
        old=self.forest.dispatch_leaf('LEAF-TEST-001','codex')
        result=self.forest.takeover_leaf('LEAF-TEST-001','antigravity',reason='ready transfer')
        self.assertNotEqual(result['new_task_id'],old.id)
        leaf=result['leaf']
        self.assertEqual(leaf['owner'],'antigravity')
        self.assertIn(old.id,leaf['history_task_ids'])
        self.assertFalse((self.bus.dirs('codex')['ready']/f'{old.id}.json').exists())
        successor=self.bus.find_message(result['new_task_id'])
        self.assertEqual(successor.deadline_ts,old.deadline_ts)
        self.assertEqual(successor.expected_artifacts,old.expected_artifacts)

    def test_missing_heartbeat_or_same_member_does_not_confirm_stop(self):
        tree=self.forest._structure();leaf=self.forest._leaf(tree,'LEAF-TEST-001')
        msg=self.forest._allocate(tree,leaf,'codex')
        self.forest.claim_leaf(leaf['id'],'codex')
        before=(self.agentbus_dir/'task-tree.json').read_bytes()
        for new in ('codex','antigravity'):
            with self.subTest(new=new),self.assertRaises(TaskTreeError):
                self.forest.takeover_leaf(leaf['id'],new,force=False)
        self.assertEqual((self.agentbus_dir/'task-tree.json').read_bytes(),before)
        self.assertTrue((self.bus.dirs('codex')['processing']/f'{msg.id}.json').is_file())

    def test_truthy_strings_never_confirm_stop(self):
        for value in ('false',1,None):
            with self.subTest(value=value),self.assertRaises(TaskTreeError):
                self.forest.takeover_leaf('LEAF-TEST-001','antigravity',force=value)

    def test_stop_blocks_even_confirmed_takeover_without_mutation(self):
        before=(self.agentbus_dir/'task-tree.json').read_bytes()
        (self.agentbus_dir/'STOP').write_text('stop')
        with self.assertRaises(TaskTreeError):
            self.forest.takeover_leaf('LEAF-TEST-001','antigravity',force=True)
        self.assertEqual((self.agentbus_dir/'task-tree.json').read_bytes(),before)
