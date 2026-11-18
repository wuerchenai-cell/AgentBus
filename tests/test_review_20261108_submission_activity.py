"""JR-20261108-U02: submitting work is distinct from execution progress."""
import contextlib
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agentbus import heartbeat
from agentbus.claim_fencing import captured_ticket
from agentbus.cli import main
from agentbus.config import load
from agentbus.mcp_server import AgentBusMcp
from agentbus.task_tree import TaskForest
from agentbus.web_bridge import WebBridge


class SubmissionActivityRegression(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = Path(tmp.name)
        self.forest = TaskForest(load(self.workspace, create=True))
        self.forest.seed_leaf({'id': 'leaf', 'title': 'Bounded short task'}, branch_id='work')
        self.bridge = WebBridge(self.workspace)
        value = self.bridge._claim_leaf({'leaf_id': 'leaf', 'agent': 'workbuddy'})
        self.bus = self.forest.bus
        self.task = self.bus._active_record('workbuddy', value['task_id'])
        self.ticket = captured_ticket(self.task)

    def submit_desktop(self):
        return self.bridge._submit_leaf_result({
            'leaf_id': 'leaf', 'agent': 'workbuddy', 'summary': '实际短任务完成',
            'operator_confirmed': True})

    def observed(self):
        return heartbeat.state(self.forest.root, self.task.id, 'workbuddy',
                               task_record=self.task.to_dict())

    def test_desktop_short_task_can_be_submitted_without_an_extra_heartbeat_step(self):
        self.assertEqual(self.observed()['source'], heartbeat.SOURCE_NONE)
        value = self.submit_desktop()
        self.assertEqual(value['submission_state'], 'delivered')
        self.assertEqual(self.observed().get('activity'), 'submission')
        self.assertIn('操作人代录', self.bus.find_results(self.task.id)[0].body)

    def test_submission_record_does_not_light_up_execution_status(self):
        self.bus.record_submission('workbuddy', self.task, self.ticket, origin='operator')
        self.assertEqual(self.observed().get('activity'), 'submission')
        self.assertEqual(self.forest.snapshot()['leaves'][0]['state'], 'claimed')
        self.bus.heartbeat('workbuddy', self.task, self.ticket, note='Actual implementation progress')
        self.assertEqual(self.observed().get('activity'), 'execution')
        self.assertEqual(self.forest.snapshot()['leaves'][0]['state'], 'running')

    def test_valid_execution_heartbeat_is_preserved_at_submission(self):
        self.bus.heartbeat('workbuddy', self.task, self.ticket, note='Real execution')
        path = heartbeat.heartbeat_path(self.forest.root, self.task.id, 'workbuddy')
        original = path.read_bytes()
        self.submit_desktop()
        self.assertEqual(path.read_bytes(), original)

    def test_submission_record_write_failure_does_not_close_work(self):
        with patch('agentbus.heartbeat.record', side_effect=OSError('isolated write failure')):
            with self.assertRaisesRegex((ValueError, OSError), 'write failure'):
                self.submit_desktop()
        self.assertFalse(self.bus.find_results(self.task.id))
        self.assertIsNotNone(self.bus._active_record('workbuddy', self.task.id))

    def test_cli_submission_is_labeled_without_claiming_execution(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = main(['answer', '--workspace', str(self.workspace), '--agent', 'workbuddy',
                       '--body', '实际短任务完成', '--claim-epoch', str(self.ticket.claim_epoch),
                       '--claim-id', self.ticket.claim_id])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(self.observed().get('activity'), 'submission')

    def test_mcp_submission_is_labeled_without_claiming_execution(self):
        app = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        app.report('workbuddy', self.task.id, 'Actual short work completed', ticket=self.ticket.to_dict())
        self.assertEqual(self.observed().get('activity'), 'submission')

    def test_mcp_claim_next_cannot_take_an_existing_connections_ticket(self):
        # This fixture was claimed by the desktop connection, not this MCP.
        second = AgentBusMcp(self.workspace, identity='workbuddy', session_tools=True)
        import json
        value = json.loads(second.claim_next('workbuddy'))
        self.assertEqual(value['status'], 'already_has_processing')
        self.assertNotIn('ticket', value)
        self.assertFalse(second._tickets)
        self.assertEqual(self.observed()['source'], heartbeat.SOURCE_NONE)
        with self.assertRaises(ValueError):
            second.report('workbuddy', self.task.id, 'Cannot submit another connections work')

    def test_mcp_new_claim_only_records_claim_activity(self):
        import json
        app = AgentBusMcp(self.workspace, identity='trae', session_tools=True)
        self.forest.seed_leaf({'id': 'second', 'title': 'Another bounded task'}, branch_id='work')
        value = json.loads(app.claim_next('trae'))
        self.assertEqual(value['status'], 'claimed')
        current = self.bus._active_record('trae', value['task_id'])
        observed = heartbeat.state(self.forest.root, current.id, 'trae', task_record=current.to_dict())
        self.assertEqual(observed['activity'], 'claim')
        self.assertEqual(next(x for x in self.forest.snapshot()['leaves'] if x['id'] == 'second')['state'], 'claimed')
        before = heartbeat.heartbeat_path(self.forest.root, current.id, 'trae').read_bytes()
        other = AgentBusMcp(self.workspace, identity='trae', session_tools=True)
        again = json.loads(other.claim_next('trae'))
        self.assertNotIn('ticket', again)
        self.assertEqual(heartbeat.heartbeat_path(self.forest.root, current.id, 'trae').read_bytes(), before)

    def test_old_execution_record_is_refreshed_for_actual_delivery_threshold(self):
        heartbeat.record(self.forest.root, self.task.id, 'workbuddy', self.ticket,
                         task_record=self.task.to_dict(), now=time.time() - 700, note='Earlier execution')
        # A longer UI warning threshold must not preserve a record the delivery gate rejects.
        self.bus.record_submission('workbuddy', self.task, self.ticket, origin='operator', heartbeat_timeout_s=1800)
        self.assertEqual(self.observed()['activity'], 'submission')
        value = self.submit_desktop()
        self.assertEqual(value['submission_state'], 'delivered')
